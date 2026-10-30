"""Synthetic configs and mountinfo; never unmount a real path."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("rollback-bind.sh")


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.target = self.root / "config.cfg"
        self.mountinfo = self.root / "mountinfo"
        self.own_record = f"71 10 0:25 /pilot/candidate.cfg {self.target} rw - ubifs ubi1 rw\n"
        self.mountinfo.write_text(self.own_record)
        (self.stage / "mount.record").write_text(self.own_record)
        (self.stage / "original.cfg").write_bytes(b"original\x00exact\n")
        (self.stage / "candidate.cfg").write_bytes(b"candidate\n")
        (self.stage / "armed").touch()
        self.target.write_bytes(b"candidate\n")
        bindir = self.root / "bin"
        bindir.mkdir()
        fake = bindir / "umount"
        fake.write_text('#!/bin/sh\nprintf called > "$TEST_STAGE/unmount-called"\nif test "${FAIL_UMOUNT:-0}" = 1; then exit 1; fi\ncp "$TEST_STAGE/original.cfg" "$1"\n: > "$TEST_MOUNTINFO"\n')
        fake.chmod(0o700)
        self.env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"], TEST_STAGE=str(self.stage), TEST_MOUNTINFO=str(self.mountinfo))

    def run_script(self):
        return subprocess.run(["sh", str(SCRIPT), str(self.stage), str(self.target), str(self.mountinfo)], env=self.env, capture_output=True)

    def test_candidate_restores_exact_original_but_keeps_watchdog_armed(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_bytes(), b"original\x00exact\n")
        self.assertTrue((self.stage / "armed").exists(), "Process recovery is not yet confirmed")
        self.assertTrue((self.stage / "restored").exists())
        self.assertTrue((self.stage / "unmount-called").exists())

    def test_symlink_target_is_matched_by_canonical_mountpoint(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.target = alias / "config.cfg"
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_bytes(), b"original\x00exact\n")
        self.assertTrue((self.stage / "armed").exists())

    def test_foreign_content_is_not_unmounted(self):
        self.target.write_bytes(b"concurrent foreign config")
        self.assert_refused_without_unmount()

    def test_foreign_bind_with_candidate_bytes_is_not_unmounted(self):
        self.mountinfo.write_text(self.own_record.replace("71 10", "72 10"))
        self.assert_refused_without_unmount()

    def test_foreign_bind_with_original_bytes_is_not_accepted(self):
        self.target.write_bytes((self.stage / "original.cfg").read_bytes())
        self.mountinfo.write_text(self.own_record.replace("71 10", "72 10"))
        self.assert_refused_without_unmount()

    def test_stacked_bind_is_not_unmounted(self):
        self.mountinfo.write_text(self.own_record + self.own_record.replace("71 10", "72 71"))
        self.assert_refused_without_unmount()

    def test_missing_mount_record_refuses_without_unmount(self):
        (self.stage / "mount.record").unlink()
        self.assert_refused_without_unmount()

    def test_original_without_bind_is_idempotent_but_keeps_watchdog_armed(self):
        self.target.write_bytes((self.stage / "original.cfg").read_bytes())
        self.mountinfo.write_text("")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.stage / "unmount-called").exists())
        self.assertTrue((self.stage / "armed").exists())

    def test_missing_backup_fails_before_unmount(self):
        (self.stage / "original.cfg").unlink()
        self.assert_refused_without_unmount()

    def test_unmount_failure_keeps_watchdog_armed(self):
        self.env["FAIL_UMOUNT"] = "1"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b"candidate\n")
        self.assertTrue((self.stage / "armed").exists())
        self.assertFalse((self.stage / "restored").exists())

    def assert_refused_without_unmount(self):
        before = self.target.read_bytes()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse((self.stage / "unmount-called").exists())
        self.assertTrue((self.stage / "armed").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
