"""Upstream resolution stays current at install, immutable afterwards."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import upstream


class UpstreamTests(unittest.TestCase):
    def receipt(self, root):
        return {'repository': upstream.BRIDGE_REPO, 'tag': 'v9.8.7', 'commit': 'a' * 40,
            'modified_build': True, 'tests': 'race/vet/config/routes PASS',
            'routing_cases': len(json.loads((upstream.ROOT / 'examples/route-cases.json').read_text())),
            'patch_sha256': upstream.hashlib.sha256(upstream.PATCH.read_bytes()).hexdigest(),
            'source_sha256': upstream.source_digest(root)}

    def fake_go(self, argv, **kwargs):
        if argv[0] == 'git':
            return ('a' * 40 + '\n').encode()
        if argv[1:4] == ['mod', 'edit', '-json']:
            return b'{"Replace":[]}'
        return b''

    def test_latest_is_selected_once_then_resolved_to_exact_commit(self):
        sha = 'a' * 40
        with mock.patch.object(upstream, 'github', side_effect=[{'tag_name': 'v9.8.7', 'draft': False, 'prerelease': False}, {'sha': sha}]) as get:
            result = upstream.release(upstream.BRIDGE_REPO)
        self.assertEqual(result['tag'], 'v9.8.7')
        self.assertEqual(result['commit'], sha)
        self.assertEqual(get.call_count, 2)
        self.assertIn('/releases/latest', get.call_args_list[0].args[0])
        self.assertIn('/commits/v9.8.7', get.call_args_list[1].args[0])

    def test_untrusted_repo_and_preview_releases_are_rejected(self):
        with self.assertRaises(upstream.UpstreamError), mock.patch.object(upstream, 'github') as get:
            upstream.release('someone/unrelated')
        get.assert_not_called()
        for tag in ('v9.8.7-rc1', '../unsafe', 'main', 'v1.2.3;id'):
            with self.subTest(tag=tag), self.assertRaises(upstream.UpstreamError):
                upstream.release(upstream.BRIDGE_REPO, tag)
        with mock.patch.object(upstream, 'github', return_value={'tag_name': 'v9.8.7', 'draft': False, 'prerelease': True}):
            with self.assertRaises(upstream.UpstreamError):
                upstream.release(upstream.BRIDGE_REPO)

    def test_patch_conflict_never_falls_back_to_copied_old_source(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            target = Path(tmp) / 'source'
            sha = 'a' * 40
            def git(command, **kwargs):
                if command[1:3] == ['rev-parse', 'HEAD']:
                    return (sha + '\n').encode()
                if 'apply' in command:
                    raise upstream.UpstreamError('Patch does not apply')
                return b''
            with mock.patch.object(upstream, 'release', return_value={'repository': upstream.BRIDGE_REPO, 'tag': 'v9.8.7', 'commit': sha}), \
                 mock.patch.object(upstream, 'run', side_effect=git):
                with self.assertRaises(upstream.UpstreamError):
                    upstream.prepare_bridge(target)
            self.assertFalse((target / 'integration-receipt.json').exists())
            with mock.patch.object(upstream, 'release') as get, self.assertRaises(FileExistsError):
                upstream.prepare_bridge(target)
            get.assert_not_called()

    def test_official_image_is_frozen_and_version_verified(self):
        digest = upstream.IMAGE_REPO + '@sha256:' + 'b' * 64
        identity = 'sha256:' + 'c' * 64
        def command(argv, **kwargs):
            if argv[1:3] == ['image', 'inspect']:
                return json.dumps([{'Id': identity, 'RepoDigests': [digest]}]).encode()
            if argv[1] == 'run':
                self.assertIn('--network=none', argv)
                self.assertIn(identity, argv)
                return b'9.8.7\n'
            return b''
        with mock.patch.object(upstream, 'release', return_value={'repository': upstream.OMNI_REPO, 'tag': 'v9.8.7', 'commit': 'a'*40}), \
             mock.patch.object(upstream, 'run', side_effect=command) as commands:
            result = upstream.omniroute()
        self.assertEqual(result['image'], digest)
        self.assertEqual(result['version'], '9.8.7')
        self.assertIn(upstream.IMAGE_REPO + ':9.8.7', commands.call_args_list[0].args[0])
        with mock.patch.object(upstream, 'release', return_value={'repository': upstream.OMNI_REPO, 'tag': 'v9.8.7', 'commit': 'a'*40}), \
             mock.patch.object(upstream, 'run', side_effect=[b'', json.dumps([{'Id': identity, 'RepoDigests':[digest]}]).encode(), b'1.0.0\n']):
            with self.assertRaises(upstream.UpstreamError):
                upstream.omniroute()


    def test_modified_source_cannot_use_a_previous_green_receipt(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            (root / 'cmd').mkdir()
            (root / 'internal').mkdir()
            for name in ('go.mod', 'go.sum', 'cmd/main.go'):
                (root / name).write_text('synthetic input\n')
            receipt = self.receipt(root)
            (root / 'integration-receipt.json').write_text(json.dumps(receipt))
            (root / 'cmd/main.go').write_text('modified input\n')
            with mock.patch.object(upstream, 'run') as run, self.assertRaisesRegex(upstream.UpstreamError, 'Source changed'):
                upstream.build_bridge(root, root / 'binary')
            run.assert_not_called()

    def test_imported_source_outside_cmd_internal_cannot_reuse_a_green_receipt(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            (root / 'pkg').mkdir()
            (root / 'go.mod').write_text('module fixture\ngo 1.20\n')
            (root / 'go.sum').write_text('')
            package = root / 'pkg/value.go'
            package.write_text('package shared\nconst Value = "checked"\n')
            (root / 'integration-receipt.json').write_text(json.dumps(self.receipt(root)))
            package.write_text('package shared\nconst Value = "changed-after-checks"\n')
            with mock.patch.object(upstream, 'run', side_effect=self.fake_go) as run, \
                 self.assertRaisesRegex(upstream.UpstreamError, 'Source changed'):
                upstream.build_bridge(root, root / 'binary')
            run.assert_not_called()

    def test_failed_incomplete_or_invalid_receipt_is_rejected_before_compilation(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            (root / 'go.mod').write_text('module fixture\ngo 1.20\n')
            (root / 'go.sum').write_text('')
            valid = self.receipt(root)
            for field, value in (('tests', 'FAILED'), ('repository', 'unapproved/project'),
                    ('commit', 'invalid'), ('tag', 'main'), ('modified_build', False),
                    ('routing_cases', True), ('routing_cases', 0), ('source_sha256', 'invalid')):
                with self.subTest(field=field, value=value):
                    (root / 'integration-receipt.json').write_text(json.dumps({**valid, field: value}))
                    with mock.patch.object(upstream, 'run', side_effect=self.fake_go) as run, \
                         self.assertRaises(upstream.UpstreamError):
                        upstream.build_bridge(root, root / 'binary')
                    run.assert_not_called()
            (root / 'integration-receipt.json').write_text(json.dumps({'patch_sha256': valid['patch_sha256'], 'source_sha256': valid['source_sha256']}))
            with mock.patch.object(upstream, 'run', side_effect=self.fake_go) as run, self.assertRaises(upstream.UpstreamError):
                upstream.build_bridge(root, root / 'binary')
            run.assert_not_called()

    def test_receipt_commit_must_match_the_actual_checkout(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            (root / 'go.mod').write_text('module fixture\ngo 1.20\n')
            (root / 'go.sum').write_text('')
            receipt = self.receipt(root)
            receipt['commit'] = 'b' * 40
            (root / 'integration-receipt.json').write_text(json.dumps(receipt))
            with mock.patch.object(upstream, 'run', side_effect=self.fake_go), \
                 self.assertRaisesRegex(upstream.UpstreamError, 'commit'):
                upstream.build_bridge(root, root / 'binary')

    def test_parent_go_workspace_cannot_change_a_verified_build(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            (root / 'go.mod').write_text('module fixture\ngo 1.20\n')
            (root / 'go.sum').write_text('')
            (root / 'integration-receipt.json').write_text(json.dumps(self.receipt(root)))
            with mock.patch.dict(os.environ, GOWORK='untrusted-parent-workspace', GOFLAGS='-overlay=untrusted.json'), \
                 mock.patch.object(upstream, 'run', side_effect=self.fake_go) as run:
                upstream.build_bridge(root, root / 'binary')
            build = [c for c in run.call_args_list if c.args[0][:2] == ['go', 'build']][0]
            self.assertEqual(build.kwargs['env'].get('GOWORK'), 'off')
            self.assertEqual(build.kwargs['env'].get('GOENV'), 'off')
            self.assertEqual(build.kwargs['env'].get('GOFLAGS'), '-mod=readonly')

    def test_local_replacement_outside_verified_module_is_refused(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            (root / 'go.mod').write_text('module fixture\ngo 1.20\nreplace example.invalid/dependency => ../outside\n')
            (root / 'go.sum').write_text('')
            (root / 'integration-receipt.json').write_text(json.dumps(self.receipt(root)))
            def command(argv, **kwargs):
                if argv[1:4] == ['mod', 'edit', '-json']:
                    return b'{"Replace":[{"New":{"Path":"../outside"}}]}'
                return self.fake_go(argv, **kwargs)
            with mock.patch.object(upstream, 'run', side_effect=command) as run, \
                 self.assertRaisesRegex(upstream.UpstreamError, 'local replacement'):
                upstream.build_bridge(root, root / 'binary')
            self.assertFalse(any(c.args[0][:2] == ['go', 'build'] for c in run.call_args_list))

    def test_exact_requested_release_cannot_be_replaced_by_another_stable_tag(self):
        with mock.patch.object(upstream, 'github', side_effect=[{'tag_name': 'v8.0.0'}, {'sha': 'a' * 40}]), \
             self.assertRaises(upstream.UpstreamError):
            upstream.release(upstream.BRIDGE_REPO, 'v9.8.7')


if __name__ == '__main__':
    unittest.main()
