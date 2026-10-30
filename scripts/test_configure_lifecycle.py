"""New release bootstrap and existing gateway configuration remain separate."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr

spec = importlib.util.spec_from_file_location('current_installer', Path(__file__).with_name('install.py'))
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ConfigureLifecycleTests(unittest.TestCase):
    def arguments(self, state):
        return installer.argparse.Namespace(state_dir=state, base_url='http://127.0.0.1:20128/v1', model='alice-gemini', api_key_file=None)

    def test_configure_managed_bootstrap_without_overwriting_its_other_files(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            state = Path(tmp) / 'managed'
            installer.create_state(state)
            image = 'diegosouzapw/omniroute@sha256:' + 'a' * 64
            installer.write_private(state / 'stack.json', {'project':'alice-gemini-synthetic', 'image':image})
            installer.write_private(state / 'compose.json', installer.compose_config('127.0.0.1', 20128, 'alice-gemini-synthetic', image))
            before = (state / 'stack.json').read_bytes()
            with mock.patch.object(installer.getpass, 'getpass', return_value='synthetic-key'), \
                 mock.patch.object(installer, 'check_connection'), redirect_stdout(io.StringIO()):
                installer.configure(self.arguments(state))
                with self.assertRaises((FileExistsError, installer.SetupError)):
                    installer.configure(self.arguments(state))
            self.assertEqual((state / 'stack.json').read_bytes(), before)
            self.assertEqual((state / 'station.json').stat().st_mode & 0o777, 0o600)

    def test_failed_check_does_not_create_a_poisoned_new_directory(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            state = Path(tmp) / 'external'
            with mock.patch.object(installer.getpass, 'getpass', return_value='synthetic-key'), \
                 mock.patch.object(installer, 'check_connection', side_effect=installer.SetupError('unreachable')), \
                 redirect_stdout(io.StringIO()), self.assertRaises(installer.SetupError):
                installer.configure(self.arguments(state))
            self.assertFalse(state.exists())

    def test_speaker_dry_run_cannot_configure_or_create_state(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            state = Path(tmp) / 'absent'
            with mock.patch.object(installer, 'configure') as configure, mock.patch.object(installer, 'ssh'), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = installer.main(['speaker', '--dry-run', '--ssh', 'test-speaker', '--state-dir', str(state)])
            self.assertEqual(result, 1)
            configure.assert_not_called()
            self.assertFalse(state.exists())

    def test_all_pauses_for_oauth_then_configures_before_speaker(self):
        sequence = []
        with mock.patch.object(installer, 'server', side_effect=lambda args: sequence.append('server')), \
             mock.patch.object(installer, 'configure', side_effect=lambda args: sequence.append('configure')), \
             mock.patch.object(installer, 'speaker', side_effect=lambda args: sequence.append('speaker')), \
             mock.patch('builtins.input', return_value='READY'), redirect_stdout(io.StringIO()):
            result = installer.main(['all'])
        self.assertEqual(result, 0)
        self.assertEqual(sequence, ['server', 'configure', 'speaker'])

    def test_dynamic_own_image_can_be_stopped_without_current_version_match(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            state = Path(tmp) / 'managed'
            installer.create_state(state)
            image = 'diegosouzapw/omniroute@sha256:' + 'd' * 64
            installer.write_private(state / 'stack.json', {'project':'alice-gemini-owned', 'image':image})
            installer.write_private(state / 'compose.json', installer.compose_config('127.0.0.1', 20128, 'alice-gemini-owned', image))
            with mock.patch.object(installer, 'compose') as compose, redirect_stdout(io.StringIO()):
                result = installer.main(['rollback', '--target', 'server', '--state-dir', str(state), '--yes'])
            self.assertEqual(result, 0)
            compose.assert_called_once_with(state, 'down')


if __name__ == '__main__':
    unittest.main()
