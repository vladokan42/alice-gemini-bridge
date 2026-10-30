"""Fresh-device install and acceptance timeout using synthetic firmware/commands."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).parent


class DeviceInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.state / "bin").mkdir()
        self.hook = self.root / "S98quasarbridge"
        self.stock = self.root / "S99quasar"
        self.stock.write_text("#!/bin/sh\n")
        self.stock.chmod(0o700)
        self.target = self.root / "quasar.cfg"
        self.target.write_bytes(b"original\n")
        self.mountinfo = self.root / "mountinfo"
        self.mountinfo.write_text("")
        self.mounts = self.root / "mounts"
        self.mounts.write_text("synthetic / synthetic ro 0 0\n")
        self.boot = self.root / "boot-id"
        self.boot.write_text("synthetic-first-boot\n")
        for name in ("install-device.sh", "S98quasarbridge", "recovery.sh", "start.sh", "supervise.sh", "rollback-bind.sh"):
            source = ROOT / name
            if source.exists():
                shutil.copyfile(source, self.state / name)
        for name, body in {"original.cfg": b"original\n", "candidate.cfg": b"candidate\n", "config.json": b"{}", "runtime.env": b"QUASAR_LLM_API_KEY=synthetic\n"}.items():
            (self.state / name).write_bytes(body)
        executable = self.state / "bin/quasar-bridge"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        # The real bridge wrapper is replaced only at the process boundary.
        (self.state / "start.sh").write_text("#!/bin/sh\ntest ! -f \"$TEST_ROOT/backend-fail\"\n")
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ["PATH"], QUASAR_DATA_DIR=str(self.state),
            QUASAR_DEVICE_CONFIG=str(self.target), QUASAR_MOUNTINFO=str(self.mountinfo), QUASAR_MOUNTS=str(self.mounts),
            QUASAR_BOOT_ID_FILE=str(self.boot), QUASAR_INIT_HOOK=str(self.hook), QUASAR_STOCK_INIT=str(self.stock),
            QUASAR_RECOVERY_SECONDS="1", QUASAR_PIDFILE=str(self.root / 'supervisor.pid'),
            TEST_ROOT=str(self.root), TEST_STATE=str(self.state), TEST_TARGET=str(self.target),
            TEST_MOUNTINFO=str(self.mountinfo))
        commands = {"id": "printf '0\\n'", "uname": "printf '%s\\n' \"${TEST_ARCH:-aarch64}\"",
            "mount": "printf '%s\\n' \"$*\" >> \"$TEST_ROOT/mount-calls\"", "sleep": "exit 0",
            "sync": "exit 0", "reboot": "touch \"$TEST_ROOT/reboot-called\"", "logger": "exit 0",
            "wget": "test -f \"$TEST_ROOT/healthy\"", "start-stop-daemon": "exit 0",
            "umount": 'cp "$TEST_STATE/original.cfg" "$TEST_TARGET"\n: > "$TEST_MOUNTINFO"'}
        for name, body in commands.items():
            p = self.bin / name
            p.write_text("#!/bin/sh\n" + body + "\n")
            p.chmod(0o700)

    def action(self, command):
        return subprocess.run(["sh", str(ROOT / "install-device.sh"), command], env=self.env, capture_output=True, timeout=5)

    def test_install_preserves_stock_and_arms_pending_without_cutover(self):
        r = self.action("install")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook.read_bytes(), (ROOT / "S98quasarbridge").read_bytes())
        self.assertEqual(self.target.read_bytes(), b"original\n")
        self.assertTrue((self.state / "acceptance.pending").exists())
        self.assertFalse((self.root / "reboot-called").exists())
        self.assertIn("remount,ro /", (self.root / "mount-calls").read_text())

    def test_wrong_arch_and_backend_failure_cannot_install_hook(self):
        self.env["TEST_ARCH"] = "armv7l"
        self.assertNotEqual(self.action("install").returncode, 0)
        self.assertFalse(self.hook.exists())
        self.env["TEST_ARCH"] = "aarch64"
        (self.root / "backend-fail").touch()
        self.assertNotEqual(self.action("install").returncode, 0)
        self.assertFalse(self.hook.exists())

    def test_foreign_hook_and_foreign_mount_are_not_overwritten(self):
        self.hook.write_bytes(b"foreign")
        self.assertNotEqual(self.action("install").returncode, 0)
        self.assertEqual(self.hook.read_bytes(), b"foreign")
        self.hook.unlink()
        self.mountinfo.write_text(f"41 1 0:25 /foreign {self.target} ro - synthetic x ro\n")
        self.assertNotEqual(self.action("install").returncode, 0)
        self.assertFalse(self.hook.exists())

    def test_confirmation_refuses_same_boot_or_unhealthy_bridge(self):
        self.assertEqual(self.action("install").returncode, 0)
        self.assertNotEqual(self.action("confirm").returncode, 0)
        self.assertTrue((self.state / "acceptance.pending").exists())
        self.boot.write_text("synthetic-new-boot\n")
        self.assertNotEqual(self.action("confirm").returncode, 0)
        self.assertTrue((self.state / "acceptance.pending").exists())

    def test_timeout_disables_then_guarded_restore_and_reboot(self):
        self.assertEqual(self.action("install").returncode, 0)
        r = subprocess.run(["sh", str(ROOT / "recovery.sh")], env=self.env, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.state / "disabled").exists())
        self.assertTrue((self.state / "acceptance.failed").exists())
        self.assertFalse((self.state / "acceptance.pending").exists())
        self.assertTrue((self.root / "reboot-called").exists())
        self.assertEqual(self.target.read_bytes(), b"original\n")

    def test_confirmed_or_absent_pending_never_reboots(self):
        r = subprocess.run(["sh", str(ROOT / "recovery.sh")], env=self.env, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.root / "reboot-called").exists())


    def activate_fixture_mount(self):
        self.boot.write_text('synthetic-second-boot\n')
        self.target.write_bytes((self.state / 'candidate.cfg').read_bytes())
        record = f'98 1 0:25 /candidate {self.target} ro - synthetic x ro\n'
        self.mountinfo.write_text(record)
        (self.state / 'mount.record').write_text(record)
        (self.root / 'healthy').touch()

    def test_accepted_healthy_owned_mount_disarms_deadline(self):
        self.assertEqual(self.action('install').returncode, 0)
        self.activate_fixture_mount()
        result = self.action('confirm')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state / 'acceptance.pending').exists())
        self.assertTrue((self.state / 'acceptance.confirmed').exists())
        result = subprocess.run(['sh', str(ROOT / 'recovery.sh')], env=self.env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'reboot-called').exists())

    def test_active_owned_mount_timeout_restores_stock_before_reboot(self):
        self.assertEqual(self.action('install').returncode, 0)
        self.activate_fixture_mount()
        result = subprocess.run(['sh', str(ROOT / 'recovery.sh')], env=self.env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_bytes(), b'original\n')
        self.assertEqual(self.mountinfo.read_text(), '')
        self.assertTrue((self.root / 'reboot-called').exists())

    def test_foreign_mount_cannot_be_confirmed_or_unmounted_by_deadline(self):
        self.assertEqual(self.action('install').returncode, 0)
        self.activate_fixture_mount()
        self.mountinfo.write_text(f'99 1 0:25 /foreign {self.target} ro - synthetic x ro\n')
        before = self.target.read_bytes()
        self.assertNotEqual(self.action('confirm').returncode, 0)
        result = subprocess.run(['sh', str(ROOT / 'recovery.sh')], env=self.env, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.state / 'disabled').exists())
        self.assertEqual(self.target.read_bytes(), before)
        self.assertIn('/foreign', self.mountinfo.read_text())
        self.assertFalse((self.root / 'reboot-called').exists())

    def test_real_daemon_starts_recovery_despite_other_shell_and_stale_pid(self):
        daemon = next((str(p) for p in (Path('/usr/sbin/start-stop-daemon'), Path('/sbin/start-stop-daemon')) if p.is_file()), None)
        if daemon is None:
            self.skipTest('dpkg start-stop-daemon is unavailable')
        (self.bin / 'start-stop-daemon').write_text('#!/bin/sh\nexec "' + daemon + '" "$@"\n')
        (self.state / 'acceptance.pending').touch()
        (self.state / 'disabled').touch()
        (self.state / 'recovery.pid').write_text(str(os.getpid()) + '\n')
        (self.state / 'recovery.sh').write_text('#!/bin/sh\nprintf "%s\\n" "$$" > "$TEST_ROOT/recovery-started"\n')
        result = subprocess.run(['sh', str(ROOT / 'S98quasarbridge'), 'start'], env=self.env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        until = time.monotonic() + 3
        while not (self.root / 'recovery-started').exists() and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue((self.root / 'recovery-started').exists(), 'Independent watchdog was not launched')
        self.assertNotEqual((self.state / 'recovery.pid').read_text().strip(), str(os.getpid()))

    def test_interrupted_confirm_cannot_prevent_deadline_recovery(self):
        self.assertEqual(self.action('install').returncode, 0)
        self.activate_fixture_mount()
        move = self.bin / 'mv'
        move.write_text('#!/bin/sh\nkill -HUP "$PPID"\nexit 1\n')
        move.chmod(0o700)
        result = self.action('confirm')
        self.assertNotEqual(result.returncode, 0)
        move.unlink()
        result = subprocess.run(['sh', str(ROOT / 'recovery.sh')], env=self.env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / 'disabled').exists())
        self.assertEqual(self.target.read_bytes(), b'original\n')
        self.assertTrue((self.root / 'reboot-called').exists())

    def test_deadline_is_not_blocked_by_obsolete_lock_from_previous_boot(self):
        self.assertEqual(self.action('install').returncode, 0)
        self.activate_fixture_mount()
        (self.state / 'acceptance.lock').mkdir()
        result = subprocess.run(['sh', str(ROOT / 'recovery.sh')], env=self.env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / 'disabled').exists())
        self.assertTrue((self.root / 'reboot-called').exists())

    def test_fixture_never_passes_host_runtime_path_to_remove_command(self):
        self.assertEqual(self.action('install').returncode, 0)
        remove = self.bin / 'rm'
        remove.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_ROOT/remove-calls"\n')
        remove.chmod(0o700)
        result = subprocess.run(['sh', str(ROOT / 'recovery.sh')], env=self.env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = (self.root / 'remove-calls').read_text().splitlines()
        for line in lines:
            for value in line.split():
                if value.startswith('/'):
                    self.assertTrue(Path(value).is_relative_to(self.root), 'Synthetic test touched a host runtime path')


if __name__ == "__main__":
    unittest.main()
