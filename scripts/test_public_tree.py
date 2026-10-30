"""Small regressions for the publication scanner; no real credentials."""
import unittest
import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("public_tree", Path(__file__).with_name("check-public-tree.py"))
assert spec is not None and spec.loader is not None
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class PublicationScannerTests(unittest.TestCase):
    def test_bundle_refuses_existing_directory_before_modifying_it(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            (root / "dist").mkdir()
            marker = root / "dist/runtime.env"
            marker.write_text("synthetic-local-setting\n")
            result = subprocess.run(["make", "-f", str(Path(__file__).resolve().parent.parent / "Makefile"), "bundle"], cwd=root, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sorted(p.name for p in (root / "dist").iterdir()), ["runtime.env"])
            self.assertEqual(marker.read_text(), "synthetic-local-setting\n")

    def test_recognizes_github_fine_grained_and_google_keys(self):
        for value in (b"github" + b"_pat_" + b"A" * 70, b"AI" + b"za" + b"B" * 35):
            self.assertTrue(scanner.findings(value), "credential pattern must be rejected without echoing it")

    def test_allows_documentation_examples(self):
        self.assertEqual(scanner.findings(b"https://gateway.example.invalid/v1\n127.0.0.1\n192.0.2.10\nYOUR_GEMINI_MODEL\n"), [])


if __name__ == "__main__":
    unittest.main()
