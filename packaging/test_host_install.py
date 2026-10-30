"""Full host-to-firmware install with synthetic SSH/ARM64 boundaries only."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

import test_install_device as device_fixture

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('host_installer', ROOT / 'scripts/install.py')
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class HostInstallTests(unittest.TestCase):
    def setUp(self):
        self.fixture = device_fixture.DeviceInstallTests('test_install_preserves_stock_and_arms_pending_without_cutover')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        shutil.rmtree(f.state)  # Only a synthetic fixture, never a device directory.
        self.original = json.dumps({'aliced': {'uniProxyUrl': 'wss://stock.example.invalid/uni.ws',
            'uniProxyQuicUrl': 'quic://stock.example.invalid', 'other': 'preserved'}, 'account': 'synthetic-account'}).encode()
        f.target.write_bytes(self.original)
        self.server = f.root / 'server'
        installer.create_state(self.server)
        installer.write_private(self.server / 'station.json', {'llm_base_url': 'http://127.0.0.1:20128/v1',
            'llm_model': 'alice-gemini', 'api_key': 'synthetic-station-only-key'})
        self.args = installer.argparse.Namespace(ssh='synthetic-speaker', state_dir=self.server, yes=True, dry_run=False)
        self.commands = []
        self.corrupt_hash = False

    def remote(self, target, script, data=None, timeout=120):
        self.assertEqual(target, 'synthetic-speaker')
        self.commands.append(script)
        f = self.fixture
        if script == 'cat /system/vendor/quasar/quasar.cfg':
            return f.target.read_bytes()
        command = ['sh', '-s', '--', 'preflight'] if script == 'sh -s -- preflight' else ['sh', '-c', script]
        result = subprocess.run(command, input=data, env=f.env, capture_output=True, timeout=10)
        if result.returncode:
            raise installer.SetupError('Synthetic SSH command failed, body hidden')
        if self.corrupt_hash and 'sha256sum' in script:
            return b'0' * 64 + result.stdout[64:]
        return result.stdout

    def prepare(self, source, version):
        self.assertEqual(version, 'latest')
        source.mkdir()
        for name in ('LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES'):
            (source / name).write_text('synthetic upstream notice\n')
        return {'repository':'skrashevich/quasar-bridge', 'tag':'v9.8.7', 'commit':'a'*40}

    def build(self, source, output, arch=None):
        self.assertEqual(arch, 'arm64')
        # Real Go/ARM64 compilation is tested separately. This executable is only
        # the deterministic boundary needed by the synthetic firmware fixture.
        output.write_text('#!/bin/sh\nexit 0\n')
        output.chmod(0o700)

    def test_full_staging_readback_backend_hook_and_repeat_refusal(self):
        # Use a local facade to keep SSH's real shell execution unpatched.
        with mock.patch.object(installer, 'ssh', side_effect=self.remote), \
             mock.patch.object(installer.upstream, 'prepare_bridge', side_effect=self.prepare), \
             mock.patch.object(installer.upstream, 'verify_bridge', side_effect=lambda source, receipt: receipt), \
             mock.patch.object(installer.upstream, 'build_bridge', side_effect=self.build), \
             mock.patch.object(installer, 'DEVICE_STATE', str(self.fixture.state)), redirect_stdout(io.StringIO()):
            installer.speaker(self.args)
            with self.assertRaises(installer.SetupError):
                installer.speaker(self.args)
        f = self.fixture
        self.assertEqual(f.target.read_bytes(), self.original)
        self.assertEqual((self.server / 'speaker-backup/original.cfg').read_bytes(), self.original)
        self.assertTrue(f.hook.exists())
        self.assertTrue((f.state / 'acceptance.pending').exists())
        self.assertFalse((f.root / 'reboot-called').exists())
        self.assertEqual(f.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((f.state / 'runtime.env').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic-station-only-key', ' '.join(self.commands))
        manifest = json.loads((f.state / 'manifest.json').read_text())
        self.assertFalse(set(manifest).intersection(('runtime.env', 'original.cfg', 'candidate.cfg')))
        self.assertEqual(json.loads((f.state / 'candidate.cfg').read_text()), json.loads(installer.speaker_candidate(self.original)))

    def test_hash_mismatch_aborts_before_any_hook_or_firmware_change(self):
        self.corrupt_hash = True
        with mock.patch.object(installer, 'ssh', side_effect=self.remote), \
             mock.patch.object(installer.upstream, 'prepare_bridge', side_effect=self.prepare), \
             mock.patch.object(installer.upstream, 'verify_bridge', side_effect=lambda source, receipt: receipt), \
             mock.patch.object(installer.upstream, 'build_bridge', side_effect=self.build), \
             mock.patch.object(installer, 'DEVICE_STATE', str(self.fixture.state)), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(installer.SetupError, 'hash readback mismatch'):
                installer.speaker(self.args)
        self.assertFalse(self.fixture.hook.exists())
        self.assertFalse((self.fixture.state / 'acceptance.pending').exists())
        self.assertEqual(self.fixture.target.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
