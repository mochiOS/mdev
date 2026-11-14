"""Exercise detached lifecycle and control through real subprocesses and Unix sockets."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from test_mdev import mdev, VMFixture, ENTRY
import control


class LifecycleTests(VMFixture):
    def launch_backend(self, ignore_term=False):
        self.runner.write_text(f'''#!/usr/bin/env python3
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, {'signal.SIG_IGN' if ignore_term else 'lambda n, f: exit(0)'})
print('backend output', flush=True)
Path(os.environ['MDEV_RUNTIME_DIR'], 'ready').touch()
while True: time.sleep(.01)
''')
        self.create()
        process = mdev.start_vm(self.store, 'test', timeout=5)
        def cleanup():
            if process.poll() is None:
                try:
                    mdev.stop_vm(self.store, 'test', timeout=.2, force=True)
                finally:
                    process.wait(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 3
        while not (self.store / 'test/runtime/ready').exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        return process

    def test_background_show_stop_and_logs(self):
        process = self.launch_backend()
        with contextlib.redirect_stdout(io.StringIO()):
            info = mdev.show(self.store, 'test', True)
        self.assertEqual(info['state'], 'running')
        self.assertEqual(info['live']['supervisor_pid'], process.pid)
        for operation in (lambda: mdev.configure(self.store, 'test', environment={'TEST': '1'}),
                          lambda: mdev.reset(self.store, 'test', True),
                          lambda: mdev.remove(self.store, 'test', True),
                          lambda: mdev.start_vm(self.store, 'test')):
            with self.assertRaises(mdev.Error):
                operation()
        mdev.stop_vm(self.store, 'test', timeout=.5)
        self.assertEqual(process.wait(timeout=3), 0)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            mdev.logs(self.store, 'test', lines=10)
        self.assertIn('backend output', output.getvalue())
        value = json.loads((self.store / 'test/state.json').read_text())
        self.assertTrue(value['stop_requested'])
        self.assertFalse(value['forced'])
        mdev.stop_vm(self.store, 'test')  # Idempotent, no stale PID signal.

    def test_stop_escalates_for_unresponsive_backend(self):
        process = self.launch_backend(ignore_term=True)
        mdev.stop_vm(self.store, 'test', timeout=.15)
        process.wait(timeout=3)
        value = json.loads((self.store / 'test/state.json').read_text())
        self.assertTrue(value['forced'])
        self.assertEqual(value['exit_code'], 137)

    def test_invalid_control_does_not_stop_vm(self):
        process = self.launch_backend()
        with self.assertRaises(ValueError):
            control.request(self.store / 'test', {'command': 'run-arbitrary-command'})
        self.assertIsNone(process.poll())
        self.assertEqual(control.request(self.store / 'test', {'command': 'status'})['state'], 'running')

    def test_stale_pid_is_never_signalled(self):
        self.create()
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            mdev.save(self.store / 'test/state.json', {'launcher_pid': unrelated.pid, 'state': 'running'})
            mdev.stop_vm(self.store, 'test')
            self.assertIsNone(unrelated.poll())
            with mdev.lock(self.store / 'test'), self.assertRaises(mdev.Error):
                mdev.stop_vm(self.store, 'test')
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_background_failure_is_reported_and_lock_released(self):
        self.runner.write_text('#!/bin/sh\necho fixture-failure\nexit 42\n')
        self.create()
        # A short-lived launch may become running before failing. If it does,
        # its recorded exit and retained logs must still report the failure.
        try:
            process = mdev.start_vm(self.store, 'test', timeout=3)
        except mdev.Error as error:
            self.assertIn('42', str(error))
        else:
            self.assertEqual(process.wait(timeout=3), 42)
        self.assertEqual(mdev.status(self.store / 'test'), 'stopped')
        self.assertIn('fixture-failure', (self.store / 'test/console.log').read_text())
        self.assertEqual(json.loads((self.store / 'test/state.json').read_text())['exit_code'], 42)

    def test_launcher_descendants_are_cleaned_up(self):
        self.runner.write_text('''#!/usr/bin/env python3
import os, subprocess, sys
from pathlib import Path
p = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
Path(os.environ['MDEV_RUNTIME_DIR'], 'child-pid').write_text(str(p.pid))
''')
        self.create()
        self.assertEqual(mdev.run_vm(self.store, 'test'), 0)
        pid = int((self.store / 'test/runtime/child-pid').read_text())
        deadline = time.monotonic() + 3
        while Path(f'/proc/{pid}/stat').exists():
            state = Path(f'/proc/{pid}/stat').read_text().split(') ')[1].split()[0]
            if state == 'Z':
                break  # Dead; the host init owns reaping this orphan.
            self.assertLess(time.monotonic(), deadline, 'launcher child survived cleanup')
            time.sleep(.01)

    def test_configuration_environment_and_reset(self):
        self.create()
        mdev.configure(self.store, 'test', environment={'EXAMPLE': 'configured'}, working_directory=self.root)
        self.runner.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
assert os.environ['EXAMPLE'] == 'configured'
assert Path.cwd() == Path(__file__).parent
print('configured backend', flush=True)
''')
        self.assertEqual(mdev.run_vm(self.store, 'test'), 0)
        with self.assertRaises(mdev.Error):
            mdev.configure(self.store, 'test', persistent=True)
        with self.assertRaises(mdev.Error):
            mdev.reset(self.store, 'test', False)
        mdev.reset(self.store, 'test', True)
        self.assertFalse((self.store / 'test/runtime').exists())
        self.assertTrue((self.store / 'test/console.log').exists())
        mdev.configure(self.store, 'test', persistent=True, unset=['EXAMPLE'])
        self.assertTrue(mdev.load(self.store / 'test')['persistent'])
        self.assertEqual(mdev.load(self.store / 'test')['environment'], {})

    def test_doctor_and_reserved_settings(self):
        self.create()
        with contextlib.redirect_stdout(io.StringIO()):
            mdev.doctor(self.store, 'test')
        with self.assertRaises(mdev.Error):
            mdev.configure(self.store, 'test', environment={'MDEV_IMAGE': '/dev/sda'})
        self.image.unlink()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(mdev.Error):
            mdev.doctor(self.store, 'test')

    def test_reject_symlink_private_disk(self):
        self.create(True)
        runtime = self.store / 'test/runtime'
        runtime.mkdir()
        (runtime / 'disk.img').symlink_to(self.image)
        with self.assertRaises(mdev.Error):
            mdev.run_vm(self.store, 'test')
        self.assertEqual(self.image.read_bytes(), b'base image')

    def test_preflight_failure_records_failure_not_success(self):
        self.create()
        self.image.unlink()
        with self.assertRaises(mdev.Error):
            mdev.run_vm(self.store, 'test')
        value = json.loads((self.store / 'test/state.json').read_text())
        self.assertEqual(value['state'], 'failed')
        self.assertIn('image not found', value['error'])
        self.assertEqual(mdev.status(self.store / 'test'), 'stopped')

    def test_reject_another_users_vm_directory(self):
        self.create()
        uid = os.getuid()
        with patch.object(mdev.os, 'getuid', return_value=uid + 1), self.assertRaises(mdev.Error):
            mdev.vm_path(self.store, 'test')

    def test_log_tail_is_bounded(self):
        self.create()
        log = self.store / 'test/console.log'
        log.write_bytes(b'old\n' * 100000 + b'last 1\nlast 2\n')
        data, position = mdev.tail(log, 2)
        self.assertEqual(data, b'last 1\nlast 2\n')
        self.assertEqual(position, log.stat().st_size)

    def test_json_list_and_invalid_descriptor(self):
        self.create()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            mdev.list_vms(self.store, True)
        self.assertEqual(json.loads(output.getvalue())[0]['name'], 'test')
        path = self.store / 'test/vm.json'
        value = json.loads(path.read_text())
        value['persistent'] = 'false'
        path.write_text(json.dumps(value))
        result = subprocess.run([sys.executable, str(ENTRY), '--directory', str(self.store), 'show', 'test'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)

    def test_real_qemu_lifecycle(self):
        if not __import__('shutil').which('qemu-system-x86_64'):
            self.skipTest('QEMU is not installed')
        self.image.write_bytes(b'\0' * 512)
        self.runner.write_text('''#!/bin/sh
exec qemu-system-x86_64 -machine pc,accel=tcg -m 32M -display none -monitor none -serial stdio -no-reboot -no-shutdown -drive "if=ide,format=raw,file=$MDEV_IMAGE"
''')
        self.create()
        process = mdev.start_vm(self.store, 'test', timeout=5)
        try:
            self.assertEqual(control.request(self.store / 'test', {'command': 'status'})['state'], 'running')
            time.sleep(.25)
            self.assertIsNone(process.poll())
            mdev.stop_vm(self.store, 'test', timeout=1)
            process.wait(timeout=3)
        finally:
            if process.poll() is None:
                mdev.stop_vm(self.store, 'test', timeout=.1, force=True)
                process.wait(timeout=3)
        self.assertEqual(self.image.read_bytes(), b'\0' * 512)
        self.assertEqual(mdev.status(self.store / 'test'), 'stopped')

    def test_two_vm_control_channels_are_isolated(self):
        first = self.launch_backend()
        mdev.create(self.store, 'second', self.image, self.runner)
        second = mdev.start_vm(self.store, 'second', timeout=5)
        try:
            mdev.stop_vm(self.store, 'test', timeout=.5)
            first.wait(timeout=3)
            self.assertIsNone(second.poll())
            self.assertEqual(control.request(self.store / 'second', {'command': 'status'})['state'], 'running')
        finally:
            mdev.stop_vm(self.store, 'second', timeout=.5)
            second.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
