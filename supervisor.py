"""Foreground VM supervision, authenticated control, console capture and cleanup."""
import datetime
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import time

import control


def now():
    """Return an explicit UTC timestamp for local lifecycle records."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def exited_without_reaping(process):
    """Keep the group leader unreaped until descendant cleanup prevents PID reuse."""
    return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def supervise(path, descriptor, environment, held_lock, save, echo=True):
    """Run one trusted launcher and retain its lock until process-group cleanup."""
    previous = {}
    process = None
    reaped = False
    stop_requested = False
    stop_deadline = None
    killed = False
    state = {'format': 1, 'state': 'starting', 'supervisor_pid': os.getpid(),
             'started_at': now(), 'stopped_at': None, 'exit_code': None}
    save(path / 'state.json', state)
    # Bind before spawning so a failure cannot leave an unmanaged launcher.
    with control.listen(path) as server, selectors.DefaultSelector() as events:
        events.register(server, selectors.EVENT_READ, 'control')
        with (path / 'console.log').open('ab', buffering=0) as console:
            console.write(f'\n--- mdev start {state["started_at"]} ---\n'.encode())
            def stop(number=signal.SIGTERM, timeout=10):
                nonlocal stop_requested, stop_deadline, killed
                stop_requested = True
                if process is not None:
                    try:
                        os.killpg(process.pid, number)
                    except ProcessLookupError:
                        pass
                if number == signal.SIGKILL:
                    killed = True
                stop_deadline = min(stop_deadline or float('inf'), time.monotonic() + timeout)
                state['state'] = 'stopping'
                save(path / 'state.json', state)

            def handle_signal(number, frame):
                stop(number)

            try:
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    previous[number] = signal.signal(number, handle_signal)
                process = subprocess.Popen(
                    [descriptor['runner']], env=environment, cwd=descriptor['working_directory'], start_new_session=True,
                    stdin=None if echo else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    pass_fds=(held_lock.fileno(),))
                state.update(state='running', launcher_pid=process.pid)
                save(path / 'state.json', state)
                events.register(process.stdout, selectors.EVENT_READ, 'output')
                os.set_blocking(process.stdout.fileno(), False)

                def output():
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        events.unregister(process.stdout)
                        return
                    console.write(chunk)
                    if echo:
                        try:
                            if hasattr(sys.stdout, 'buffer'):
                                sys.stdout.buffer.write(chunk)
                                sys.stdout.buffer.flush()
                            else:
                                sys.stdout.write(chunk.decode(errors='replace'))
                                sys.stdout.flush()
                        except BrokenPipeError:
                            # Losing a pipe consumer must not orphan the VM.
                            stop()

                while not exited_without_reaping(process):
                    for event, _ in events.select(.1):
                        if event.data == 'output':
                            output()
                        else:
                            connection, _ = server.accept()
                            with connection:
                                connection.settimeout(.5)
                                try:
                                    if not control.peer_is_owner(connection):
                                        raise ValueError('control client belongs to another user')
                                    message = control.receive(connection)
                                    if not isinstance(message, dict):
                                        raise ValueError('invalid control request')
                                    operation = message.get('command')
                                    if operation == 'stop':
                                        timeout = message.get('timeout', 10)
                                        if not isinstance(timeout, (float, int)) or not 0 <= timeout <= 300:
                                            raise ValueError('stop timeout must be between 0 and 300 seconds')
                                        if type(message.get('force', False)) is not bool:
                                            raise ValueError('force must be a boolean')
                                        stop(signal.SIGKILL if message.get('force') else signal.SIGTERM, timeout)
                                    elif operation != 'status':
                                        raise ValueError('unsupported control command')
                                    control.send(connection, {'ok': True, **state})
                                except (OSError, ValueError) as error:
                                    try:
                                        control.send(connection, {'ok': False, 'error': str(error)})
                                    except OSError:
                                        pass
                    if stop_deadline is not None and time.monotonic() >= stop_deadline and not killed:
                        stop(signal.SIGKILL, 0)
                # Do not poll()/wait() yet: retaining the zombie leader reserves
                # its PID/PGID while cleaning up children left by a launcher.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    time.sleep(.05)
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # Capture remaining output after all writers have been stopped.
                while events.get_map().get(process.stdout.fileno()) is not None:
                    ready = events.select(.2)
                    outputs = [event for event, _ in ready if event.data == 'output']
                    if not outputs:
                        break
                    output()
                result = process.wait()
                reaped = True
                code = result if result >= 0 else 128 - result
                state.update(state='stopped', stopped_at=now(), exit_code=code,
                             stop_requested=stop_requested, forced=killed)
                save(path / 'state.json', state)
                console.write(f'\n--- mdev exit {code} {state["stopped_at"]} ---\n'.encode())
                return code
            except BaseException as error:
                if process is not None and not reaped:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                state.update(state='failed', stopped_at=now(), error=str(error))
                save(path / 'state.json', state)
                raise
            finally:
                if process is not None and process.stdout is not None:
                    process.stdout.close()
                for number, handler in previous.items():
                    signal.signal(number, handler)
