import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('transfer_probe', Path(__file__).resolve().parents[1] / 'scripts/transfer-test-artifact.py')
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fixture(extra=None):
    commit = 'a' * 40
    archive = 'robie-hermes-' + commit[:12] + '.tgz'
    files = {archive: b'release bytes', 'deploy-test-release.sh': b'installer bytes'}
    files[archive + '.sha256'] = (digest(files[archive]) + '  ' + archive + '\n').encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
        if extra:
            bundle.writestr(*extra)
    blob = output.getvalue()
    payload = {'commit': commit, 'archive_sha256': digest(files[archive]),
               'installer_sha256': digest(files['deploy-test-release.sh']), 'artifact_sha256': digest(blob)}
    return blob, payload, files


class TransferTests(unittest.TestCase):
    def test_artifact_metadata_must_match_exact_run_build_and_digest(self):
        env = {'GITHUB_SHA': 'a' * 40, 'ARTIFACT_SHA256': 'b' * 64,
               'ARTIFACT_ID': '123', 'GITHUB_RUN_ID': '456',
               'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_TOKEN': 'private-token'}
        metadata = {'expired': False, 'workflow_run': {'id': 456, 'head_sha': 'a' * 40},
                    'name': 'test-release-' + 'a' * 40 + '-2', 'size_in_bytes': 100,
                    'digest': 'sha256:' + 'b' * 64}
        url = 'https://example.blob.core.windows.net/artifact?sig=secret'
        class Opener:
            def __init__(self, data):
                self.data = data
                self.calls = 0
            def open(self, request, timeout):
                self.calls += 1
                if self.calls == 1:
                    return io.BytesIO(json.dumps(self.data).encode())
                raise urllib.error.HTTPError(request.full_url, 302, 'Found', {'Location': url}, None)
        with patch.object(transfer.urllib.request, 'build_opener', return_value=Opener(metadata)):
            self.assertEqual(transfer.artifact_url(env), url)
        for change in ({'expired': True}, {'workflow_run': {'id': 999, 'head_sha': 'a' * 40}},
                       {'workflow_run': {'id': 456, 'head_sha': 'c' * 40}},
                       {'name': 'another-build'}, {'size_in_bytes': transfer.MAX_BYTES + 1},
                       {'digest': 'sha256:' + 'c' * 64}):
            opener = Opener(dict(metadata, **change))
            with self.subTest(change=change), patch.object(transfer.urllib.request, 'build_opener', return_value=opener):
                with self.assertRaises(ValueError):
                    transfer.artifact_url(env)
                self.assertEqual(opener.calls, 1)

    def test_oversized_bundle_rejected_before_extraction(self):
        blob, payload, _ = fixture()
        with patch.object(transfer, 'MAX_BYTES', len(blob) - 1):
            with self.assertRaises(ValueError):
                transfer.validate_bundle(blob, payload)

    def test_exact_three_files_and_both_content_hashes(self):
        blob, payload, files = fixture()
        self.assertEqual(transfer.validate_bundle(blob, payload), files)
        for key in ('artifact_sha256', 'archive_sha256', 'installer_sha256'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                transfer.validate_bundle(blob, dict(payload, **{key: '0' * 64}))

    def test_extra_duplicate_and_traversal_members_rejected(self):
        for name in ('../outside', 'extra', 'deploy-test-release.sh', 'directory/'):
            with self.subTest(name=name):
                blob, payload, _ = fixture((name, b'untrusted'))
                with self.assertRaises(ValueError):
                    transfer.validate_bundle(blob, payload)

    def test_symlink_rejected_even_with_matching_content_digests(self):
        _, payload, files = fixture()
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as bundle:
            for name, content in files.items():
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(entry, content)
        blob = output.getvalue()
        payload['artifact_sha256'] = digest(blob)
        with self.assertRaises(ValueError):
            transfer.validate_bundle(blob, payload)

    def test_bad_download_endpoints_rejected(self):
        for url in ('http://x.blob.core.windows.net/a', 'https://evil.example/a',
                    'https://user:secret@x.blob.core.windows.net/a',
                    'https://x.blob.core.windows.net:444/a',
                    'https://x.blob.core.windows.net/a\nsecret'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                transfer.validate_url(url)

    def test_signed_url_uses_stdin_and_github_token_never_leaves_controller(self):
        secret_url = 'https://example.blob.core.windows.net/build?sig=private-download-token'
        env = {'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': transfer.REPOSITORY,
               'TEST_VM': 'hermes-test-01', 'PROJECT_ID': 'streetsmart-hermes-poc',
               'ZONE': 'us-east1-b', 'SSH_KEY': '/tmp/hermes-test-deploy',
               'GITHUB_SHA': 'a' * 40, 'ARCHIVE_SHA256': 'b' * 64,
               'INSTALLER_SHA256': 'c' * 64, 'ARTIFACT_SHA256': 'd' * 64,
               'GITHUB_TOKEN': 'private-github-token'}
        def runner(command, **kwargs):
            self.assertNotIn(secret_url, ' '.join(command))
            self.assertNotIn(env['GITHUB_TOKEN'], ' '.join(command) + kwargs['input'])
            self.assertEqual(json.loads(kwargs['input'])['url'], secret_url)
            self.assertIn('--tunnel-through-iap', command)
            self.assertEqual(kwargs['timeout'], 150)
            return SimpleNamespace(returncode=0, stdout='TEST_ARTIFACT_TRANSFER_OK\n')
        with patch.object(transfer, 'artifact_url', return_value=secret_url):
            transfer.transfer(env, runner)
            with self.assertRaises(ValueError):
                transfer.transfer(dict(env, TEST_VM='hermes-poc-01'), runner)

    def test_production_receiver_refuses_before_reading_any_input(self):
        with patch.object(transfer.socket, 'gethostname', return_value='hermes-poc-01'):
            with self.assertRaisesRegex(ValueError, 'Test host'):
                transfer.receive()
