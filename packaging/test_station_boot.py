"""Device boot safety tests: synthetic filesystem and intercepted commands."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).parent
INIT = ROOT / "S98quasarbridge"
SUPERVISOR = ROOT / "supervise.sh"


class BootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "bin").mkdir()
        (self.state / "bin/quasar-bridge").write_text("#!/bin/sh\nexit 0\n")
        (self.state / "bin/quasar-bridge").chmod(0o700)
        (self.state / "config.json").write_text("{}")
        (self.state / "runtime.env").write_text("QUASAR_LLM_API_KEY=synthetic-test\n")
        (self.state / "start.sh").write_text("#!/bin/sh\nexit 1\n")
        (self.state / "supervise.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.state / "original.cfg").write_bytes(b"original\n")
        (self.state / "candidate.cfg").write_bytes(b"candidate\n")
        shutil.copy(ROOT / "rollback-bind.sh", self.state / "rollback-bind.sh")
        self.target = self.root / "quasar.cfg"
        self.target.write_bytes(b"original\n")
        self.mountinfo = self.root / "mountinfo"
        self.mountinfo.write_text("")
        bindir = self.root / "bin"
        bindir.mkdir()
        commands = {
            "start-stop-daemon": 'printf "%s\\n" "$*" >> "$TEST_ROOT/daemon-calls"\nexit 0\n',
            "wget": 'printf "%s\\n" "$*" >> "$TEST_ROOT/health-calls"\ncase " $* " in *" -T 2 "*) :;; *) touch "$TEST_ROOT/unbounded-probe";; esac\ntest -f "$TEST_ROOT/healthy"\n',
            "sleep": 'exit 0\n',
            "logger": 'if test "$#" -eq 2; then while IFS= read -r line; do :; done; fi\nexit 0\n',
            "mount": 'cp "$TEST_STATE/candidate.cfg" "$TEST_TARGET"\nprintf "91 1 0:25 /candidate %s rw - ubifs ubi1 rw\\n" "$TEST_TARGET" > "$TEST_MOUNTINFO"\n',
            "umount": 'cp "$TEST_STATE/original.cfg" "$TEST_TARGET"\n: > "$TEST_MOUNTINFO"\n',
            "reboot": 'touch "$TEST_ROOT/reboot-requested"\n',
        }
        for name, body in commands.items():
            f = bindir / name
            f.write_text("#!/bin/sh\n" + body)
            f.chmod(0o700)
        self.env = dict(os.environ, PATH=str(bindir)+os.pathsep+os.environ["PATH"], QUASAR_DATA_DIR=str(self.state), QUASAR_DEVICE_CONFIG=str(self.target), QUASAR_MOUNTINFO=str(self.mountinfo), QUASAR_PIDFILE=str(self.root / "supervisor.pid"), TEST_ROOT=str(self.root), TEST_STATE=str(self.state), TEST_TARGET=str(self.target), TEST_MOUNTINFO=str(self.mountinfo))

    def run_init(self, action="start"):
        if not INIT.exists():
            self.fail("Safe device init is not implemented")
        return subprocess.run(["sh", str(INIT), action], env=self.env, capture_output=True, timeout=5)

    def test_disabled_boot_leaves_stock_and_starts_nothing(self):
        (self.state / "disabled").touch()
        r = self.run_init()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.target.read_bytes(), b"original\n")
        self.assertFalse((self.root / "daemon-calls").exists())

    def test_unhealthy_bridge_never_changes_firmware_route(self):
        r = self.run_init()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b"original\n")
        self.assertEqual(self.mountinfo.read_text(), "")

    def test_healthy_bridge_binds_once_and_stop_restores_original(self):
        (self.root / "healthy").touch()
        r = self.run_init()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.target.read_bytes(), b"candidate\n")
        self.assertTrue((self.state / "mount.record").exists())
        again = self.run_init()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(self.mountinfo.read_text().splitlines()), 1)
        r = self.run_init("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.target.read_bytes(), b"original\n")
        self.assertTrue((self.state / "disabled").exists())

    def test_each_health_probe_has_explicit_deadline(self):
        r = self.run_init()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.root / "unbounded-probe").exists(), "Health must not hold stock boot indefinitely")
        self.assertEqual(self.target.read_bytes(), b"original\n")

    def test_failed_health_on_existing_bind_restores_stock_and_reboots(self):
        (self.root / "healthy").touch()
        first = self.run_init()
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.root / "healthy").unlink()
        r = self.run_init()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b"original\n")
        self.assertEqual(self.mountinfo.read_text(), "")
        self.assertTrue((self.state / "disabled").exists())
        self.assertTrue((self.root / "reboot-requested").exists())

    def test_foreign_firmware_prevents_daemon_and_mount(self):
        self.target.write_bytes(b"foreign config")
        (self.root / "healthy").touch()
        r = self.run_init()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b"foreign config")
        self.assertFalse((self.root / "daemon-calls").exists())

    def test_symlink_state_uses_canonical_executable_for_process_ownership(self):
        alias = self.root / 'state-alias'
        alias.symlink_to(self.state, target_is_directory=True)
        self.env['QUASAR_DATA_DIR'] = str(alias)
        (self.root / 'healthy').touch()
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / 'daemon-calls').read_text()
        self.assertIn(str(self.state / 'supervise.sh'), calls)
        self.assertNotIn(str(alias), calls)

    def test_three_fast_crashes_disable_before_recovery_reboot(self):
        if not SUPERVISOR.exists():
            self.fail("Safe process supervisor is not implemented")
        start = self.state / "start.sh"
        start.write_text('#!/bin/sh\nn=0\nif test -f "$TEST_ROOT/start-count"; then IFS= read -r n < "$TEST_ROOT/start-count"; fi\nn=$((n+1))\nprintf "%s\\n" "$n" > "$TEST_ROOT/start-count"\nexit 1\n')
        r = subprocess.run(["sh", str(SUPERVISOR)], env=self.env, capture_output=True, timeout=5)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.root / "start-count").read_text().strip(), "3")
        self.assertTrue((self.state / "disabled").exists())
        self.assertTrue((self.root / "reboot-requested").exists())
        self.assertEqual(self.target.read_bytes(), b"original\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
