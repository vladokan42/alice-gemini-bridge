"""Installer contracts: synthetic data only; never contacts a real speaker."""
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr

path = Path(__file__).with_name("install.py")
spec = importlib.util.spec_from_file_location("installer", path)
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
LAN_IP = str(ipaddress.IPv4Address(0xC0A80A32))


class InstallerTests(unittest.TestCase):
    def test_rejects_unsafe_address_model_and_ssh_inputs(self):
        for value in ("0.0.0.0", "8.8.8.8", "::", "localhost", "127.0.0.1;id"):
            with self.subTest(bind=value), self.assertRaises(ValueError):
                installer.lan_ip(value)
        self.assertEqual(installer.lan_ip(LAN_IP), LAN_IP)
        for url in ("http://public.example.invalid/v1", "https://user:key@example.invalid/v1", "https://api.example.invalid/v1?token=x", "https://api.example.invalid/v1#x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                installer.validate_endpoint(url)
        self.assertEqual(installer.validate_endpoint("http://127.0.0.1:20128/v1/"), "http://127.0.0.1:20128/v1")
        for value in ("--bad", "root@host;id", "root@host\necho", "$(id)"):
            with self.subTest(ssh=value), self.assertRaises(ValueError):
                installer.ssh_target(value)
        for value in ("", "model\nsecret", "a b", "../bad"):
            with self.subTest(model=value), self.assertRaises(ValueError):
                installer.model_id(value)

    def test_private_creation_and_repeat_refusal(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            state = Path(tmp) / "state"
            installer.create_state(state)
            installer.write_private(state / "credential.json", {"key": "synthetic-test-value"})
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual((state / "credential.json").stat().st_mode & 0o777, 0o600)
            before = (state / "credential.json").read_bytes()
            with self.assertRaises(FileExistsError):
                installer.create_state(state)
            with self.assertRaises(FileExistsError):
                installer.write_private(state / "credential.json", {})
            self.assertEqual((state / "credential.json").read_bytes(), before)

    def test_compose_is_pinned_scoped_and_authenticated(self):
        compose = installer.compose_config(LAN_IP, 20128, "alice-test", 'diegosouzapw/omniroute@sha256:' + 'a' * 64)
        service = compose["services"]["omniroute"]
        self.assertIn("@sha256:", service["image"])
        self.assertEqual(service["environment"]["REQUIRE_API_KEY"], "true")
        self.assertFalse(service.get("privileged", False))
        self.assertNotIn("network_mode", service)
        self.assertEqual({row["host_ip"] for row in service["ports"]}, {"127.0.0.1", LAN_IP})
        self.assertTrue(all("docker.sock" not in str(v) for v in service["volumes"]))
        self.assertEqual(service["logging"]["options"]["max-file"], "3")

    def test_restricted_key_payload_has_no_management_or_direct_model_access(self):
        data = installer.key_payload("alice-gemini")
        self.assertEqual(data["modelAccessMode"], "restricted")
        self.assertEqual(data["allowedModels"], [])
        self.assertEqual(data["allowedCombos"], ["alice-gemini"])
        self.assertEqual(data["scopes"], ["inference"])
        self.assertTrue(data["noLog"])

    def test_candidate_changes_only_two_endpoints(self):
        original = {"aliced": {"uniProxyUrl": "wss://old.example.invalid/uni.ws", "uniProxyQuicUrl": "quic://old.example.invalid", "other": 9}, "account": "synthetic-account"}
        raw = json.dumps(original).encode()
        changed = json.loads(installer.speaker_candidate(raw))
        changed["aliced"]["uniProxyUrl"] = original["aliced"]["uniProxyUrl"]
        changed["aliced"]["uniProxyQuicUrl"] = original["aliced"]["uniProxyQuicUrl"]
        self.assertEqual(changed, original)
        with self.assertRaises(ValueError):
            installer.speaker_candidate(b'{"aliced":{}}')

    def test_secret_is_read_from_file_not_command_argument(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            p = Path(tmp) / "key"
            p.write_text("synthetic-key\n")
            p.chmod(0o600)
            self.assertEqual(installer.read_secret(p), "synthetic-key")
            p.chmod(0o644)
            with self.assertRaises(ValueError):
                installer.read_secret(p)


    def test_dry_run_never_executes_a_device_rollback(self):
        with mock.patch.object(installer, 'ssh') as remote, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = installer.main(['rollback', '--dry-run', '--yes', '--ssh', 'synthetic-speaker'])
        self.assertEqual(result, 1)
        remote.assert_not_called()

    def test_provider_failure_stops_only_new_stack_and_hides_raw_error(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            state = Path(tmp) / 'own-stack'
            args = installer.argparse.Namespace(state_dir=state, bind_ip='127.0.0.1', port=49763,
                model='synthetic-model', dry_run=False, api_key_file=None, yes=True)
            output = io.StringIO()
            with mock.patch.object(installer, 'run'), mock.patch.object(installer, 'compose') as compose, \
                 mock.patch.object(installer.getpass, 'getpass', return_value='synthetic-password-long'), \
                 mock.patch.object(installer.upstream, 'omniroute', return_value={'image':'diegosouzapw/omniroute@sha256:' + 'a'*64}), \
                 mock.patch.object(installer, 'Gateway') as gateway, redirect_stdout(output):
                gateway.return_value.login.side_effect = installer.SetupError('Gateway unavailable; body hidden')
                with self.assertRaises(installer.SetupError):
                    installer.server(args)
            self.assertEqual([call.args[1:] for call in compose.call_args_list], [('up', '-d', '--wait', '--wait-timeout', '180'), ('down',)])
            self.assertTrue((state / 'gateway.env').exists())
            self.assertFalse((state / 'station.json').exists())
            self.assertNotIn('synthetic-provider-secret', output.getvalue())
            self.assertNotIn('synthetic-password-long', output.getvalue())

    def test_unserializable_password_is_rejected_before_state_or_container(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            state = Path(tmp) / 'own-stack'
            args = installer.argparse.Namespace(state_dir=state, bind_ip='127.0.0.1', port=49763,
                model='synthetic-model', dry_run=False, api_key_file=None, yes=True)
            with mock.patch.object(installer, 'run'), mock.patch.object(installer, 'compose') as compose, \
                 mock.patch.object(installer.getpass, 'getpass', return_value='synthetic-password' + chr(92)), \
                 mock.patch.object(installer, 'Gateway'), \
                 mock.patch.object(installer, 'check_connection'), redirect_stdout(io.StringIO()):
                with self.assertRaises(installer.SetupError):
                    installer.server(args)
            compose.assert_not_called()
            self.assertFalse(state.exists())

    def test_configure_saves_and_verifies_station_connection(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            state = Path(tmp) / 'state'
            args = installer.argparse.Namespace(state_dir=state, base_url='http://127.0.0.1:20128/v1',
                model='omni-alice-lite', api_key_file=None)
            with mock.patch.object(installer.getpass, 'getpass', return_value='omniroute-station-key'), \
                 mock.patch.object(installer, 'check_connection') as mock_check, redirect_stdout(io.StringIO()):
                installer.configure(args)
            mock_check.assert_called_once()
            station = json.loads((state / 'station.json').read_text())
            self.assertEqual(station['llm_base_url'], 'http://127.0.0.1:20128/v1')
            self.assertEqual(station['llm_model'], 'omni-alice-lite')
            self.assertEqual(station['api_key'], 'omniroute-station-key')
            self.assertEqual((state / 'station.json').stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
