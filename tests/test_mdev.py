"""VM lifecycle tests with a fake backend; never start QEMU."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / 'mdev'
loader = importlib.machinery.SourceFileLoader('mdev', str(ENTRY))
spec = importlib.util.spec_from_loader(loader.name, loader)
mdev = importlib.util.module_from_spec(spec)
loader.exec_module(mdev)


class VMTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = self.root / 'develop'
        self.image = self.root / 'base.img'
        self.image.write_bytes(b'base image')
        self.runner = self.root / 'backend'
        self.runner.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
p = Path(os.environ['MDEV_IMAGE'])
assert p.read_bytes() == b'base image'
p.write_bytes(b'guest writes')
Path(os.environ['MDEV_RUNTIME_DIR'], 'firmware').touch()
''')
        self.runner.chmod(0o755)

    def create(self, persistent=False):
        mdev.create(self.store, 'test', self.image, self.runner, persistent)

    def test_empty_list(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            mdev.list_vms(self.store)
        self.assertIn('STATE', out.getvalue())
        self.assertFalse(self.store.exists())

    def test_fresh_private_disk_each_boot(self):
        self.create()
        with patch.object(mdev.hashlib, 'file_digest', side_effect=AssertionError('unnecessary full image read')):
            self.assertEqual(mdev.run_vm(self.store, 'test'), 0)
        self.assertEqual(self.image.read_bytes(), b'base image')
        self.assertEqual(mdev.run_vm(self.store, 'test'), 0)
        self.assertTrue((self.store / 'test/runtime/firmware').exists())

    def test_persistent_disk_survives_changed_image(self):
        self.create(True)
        mdev.run_vm(self.store, 'test')
        self.image.write_bytes(b'new image')
        with self.assertRaises(mdev.Error):
            mdev.run_vm(self.store, 'test')
        self.assertEqual((self.store / 'test/runtime/disk.img').read_bytes(), b'guest writes')

    def test_idempotent_registration_and_mismatch(self):
        self.create()
        mdev.create(self.store, 'test', self.image, self.runner, if_missing=True)
        with self.assertRaises(mdev.Error):
            mdev.create(self.store, 'test', self.image, self.runner, persistent=True, if_missing=True)

    def test_missing_image_does_not_create_vm(self):
        self.image.unlink()
        with self.assertRaises(mdev.Error):
            self.create()
        self.assertFalse((self.store / 'test').exists())

    def test_bad_names(self):
        for name in ('..', '../escape', '/tmp/escape', '.hidden', 'a/b'):
            with self.subTest(name=name), self.assertRaises(mdev.Error):
                mdev.vm_path(self.store, name)

    def test_lock_prevents_run_and_removal(self):
        self.create()
        path = self.store / 'test'
        with mdev.lock(path):
            self.assertEqual(mdev.status(path), 'running')
            with self.assertRaises(mdev.Error):
                mdev.run_vm(self.store, 'test')
            with self.assertRaises(mdev.Error):
                mdev.remove(self.store, 'test', True)
        self.assertEqual(mdev.status(path), 'stopped')
        with self.assertRaises(mdev.Error):
            mdev.remove(self.store, 'test', False)
        mdev.remove(self.store, 'test', True)
        self.assertFalse(path.exists())

    def test_backend_failure_propagates(self):
        self.runner.write_text('#!/bin/sh\nexit 42\n')
        self.create()
        self.assertEqual(mdev.run_vm(self.store, 'test'), 42)
        self.assertEqual(mdev.status(self.store / 'test'), 'stopped')

    def test_live_list_and_signal_forwarding(self):
        self.runner.write_text('''#!/usr/bin/env python3
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, lambda n, f: exit(0))
Path(os.environ['MDEV_RUNTIME_DIR'], 'ready').touch()
while True: time.sleep(.01)
''')
        self.create()
        proc = subprocess.Popen([sys.executable, str(ENTRY), '--directory', str(self.store), 'run', 'test'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (self.store / 'test/runtime/ready').exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    self.fail('fake VM did not start')
                time.sleep(.01)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                mdev.list_vms(self.store)
            self.assertIn('test\trunning', out.getvalue())
            proc.terminate()
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertEqual(mdev.status(self.store / 'test'), 'stopped')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.communicate()


if __name__ == '__main__':
    unittest.main()
