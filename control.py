"""Linux local control channel; clients never signal PIDs read from files."""
import hashlib
import json
import os
from pathlib import Path
import socket
import struct

MAX_MESSAGE = 8192


def address(path):
    """Use a per-user abstract Unix address, including for very long store paths."""
    identity = hashlib.sha256(os.fsencode(Path(path).resolve())).hexdigest()
    return '\0mdev-' + str(os.getuid()) + '-' + identity


def peer_is_owner(stream):
    """Authenticate the socket peer using Linux kernel credentials."""
    _, uid, _ = struct.unpack('3i', stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid == os.getuid()


def receive(stream):
    """Read one bounded newline-delimited JSON message."""
    data = bytearray()
    while b'\n' not in data:
        chunk = stream.recv(min(4096, MAX_MESSAGE + 1 - len(data)))
        if not chunk:
            raise ValueError('incomplete control message')
        data.extend(chunk)
        if len(data) > MAX_MESSAGE:
            raise ValueError('control message is too large')
    return json.loads(data.split(b'\n', 1)[0])


def send(stream, value):
    """Send a single bounded response."""
    data = json.dumps(value).encode() + b'\n'
    if len(data) > MAX_MESSAGE:
        raise ValueError('control response is too large')
    stream.sendall(data)


def request(path, message, timeout=2):
    """Request a lifecycle operation from the same user's live VM supervisor."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(timeout)
        stream.connect(address(path))
        if not peer_is_owner(stream):
            raise ValueError('control server is owned by another user')
        send(stream, message)
        value = receive(stream)
        if not isinstance(value, dict) or not value.get('ok'):
            raise ValueError(value.get('error', 'invalid control response') if isinstance(value, dict)
                             else 'invalid control response')
        return value


def listen(path):
    """Bind the VM's control endpoint; the kernel removes it when closed."""
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        stream.bind(address(path))
        stream.listen(8)
        stream.setblocking(False)
        return stream
    except BaseException:
        stream.close()
        raise
