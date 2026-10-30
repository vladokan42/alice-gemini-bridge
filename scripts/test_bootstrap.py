"""Exercise the real shell bootstrap without installing packages or using root."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class BootstrapTests(unittest.TestCase):
    def test_missing_c_compiler_is_installed_for_upstream_race_tests(self):
        # Dropping gcc from bootstrap discovery would skip apt on an otherwise
        # ready server, then fail the mandatory upstream race tests.
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            tools = root / 'tools'
            tools.mkdir()
            trace = root / 'trace'
            for name in ('git', 'ssh', 'go', 'docker', 'systemctl', 'apt-get', 'python3'):
                path = tools / name
                path.write_text('#!/bin/sh\nprintf "%s %s\\n" "' + name + '" "$*" >> "$BOOTSTRAP_TEST_LOG"\n')
                path.chmod(0o700)
            (tools / 'id').write_text('#!/bin/sh\nprintf "0\\n"\n')
            (tools / 'id').chmod(0o700)
            dirname = shutil.which('dirname')
            assert dirname is not None
            (tools / 'dirname').symlink_to(dirname)
            env = dict(os.environ, PATH=str(tools), BOOTSTRAP_TEST_LOG=str(trace))
            result = subprocess.run(['/bin/sh', str(ROOT / 'install.sh'), '--ssh', 'synthetic-speaker'],
                env=env, capture_output=True, timeout=10)
            if b'Supported bootstrap:' in result.stderr:
                self.skipTest('Bootstrap supports Debian 13 / Ubuntu 24.04 only')
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            lines = trace.read_text().splitlines()
            installs = [line.split() for line in lines if line.startswith('apt-get install ')]
            self.assertEqual(len(installs), 1, 'Missing gcc must trigger dependency installation')
            self.assertIn('gcc', installs[0])
            self.assertTrue(any(line.startswith('python3 ') and '--ssh synthetic-speaker' in line for line in lines))


if __name__ == '__main__':
    unittest.main()
