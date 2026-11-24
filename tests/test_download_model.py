"""What the downloader records about a license, and what it refuses to decide on its own.

`scripts/download-model.py` fetches pinned public weights on a worker and finishes by recording the publisher's license.
Recording and deciding are different acts: a repository that publishes no license text, or declares a named agreement
instead of an SPDX id, is a *review* the profile has to carry — otherwise the download would make the decision silently,
after ~100 GiB had already been moved onto a borrowed host. These tests run the provenance step against a fake opener,
so nothing is downloaded and no host is contacted.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def downloader():
    spec = importlib.util.spec_from_file_location('download_model', ROOT / 'scripts/download-model.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ProvenanceFixture(unittest.TestCase):
    def setUp(self):
        self.downloader = downloader()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.models_root = Path(directory.name) / 'models'
        self.target = self.models_root / 'infernix-fixture' / ('a' * 40)
        self.target.mkdir(parents=True)  # `main` creates this before the provenance step runs

    def profile(self, upstream_license='other', **extra):
        return {'schema_version': 1, 'id': 'infernix-fixture', 'repo': 'someone/artifact', 'revision': 'a' * 40,
                'upstream': {'repo': 'someone/upstream', 'revision': 'b' * 40, 'license': upstream_license},
                'quantization': 'NVFP4', 'nodes': ['spark'], 'files': [{'path': 'm.bin', 'bytes': 1, 'sha256': '0' * 64}],
                'entrypoint': 'm.bin', 'backend': {'name': 'infernix', 'revision': 'c' * 40, 'context': 262144,
                                                   'slots': 1}, **extra}

    def opener(self, upstream_data, texts=None):
        texts = texts or {}

        def open_url(url, timeout=None):
            if url.startswith('https://huggingface.co/api/models/'):
                return Response(json.dumps(upstream_data).encode())
            if 'spdx/license-list-data' in url:
                return Response(b'Apache License Version 2.0, January 2004')
            return Response(texts[url].encode())
        return open_url

    def record(self):
        return json.loads((self.target / 'provenance/publisher-license-declaration.json').read_text())


class LicenseFileTests(ProvenanceFixture):
    def test_a_published_license_file_is_fetched_hashed_and_needs_no_review(self):
        profile = self.profile(upstream_license='apache-2.0')
        url = 'https://huggingface.co/someone/upstream/resolve/' + 'b' * 40 + '/LICENSE'
        hashes = self.downloader.license_provenance(profile, self.target,
                                                    self.opener({'siblings': [{'rfilename': 'LICENSE'}]}, {url: 'Apache-2.0 text'}))
        self.assertEqual(hashes, {'LICENSE': hashlib.sha256(b'Apache-2.0 text').hexdigest()})
        self.assertEqual((self.target / 'provenance/LICENSE').read_text(), 'Apache-2.0 text')
        self.assertFalse((self.target / 'provenance/publisher-license-declaration.json').exists())

    def test_a_nested_license_file_does_not_speak_for_the_repository(self):
        profile = self.profile(upstream_license='apache-2.0')
        with self.assertRaises(RuntimeError) as caught:
            self.downloader.license_provenance(profile, self.target,
                                               self.opener({'siblings': [{'rfilename': 'third_party/LICENSE'}]}))
        self.assertIn('license_review', str(caught.exception))
        self.assertFalse((self.target / 'provenance/LICENSE').exists())


class StandardDeclarationTests(ProvenanceFixture):
    def test_a_card_declaring_the_profiles_spdx_license_fetches_the_pinned_reference(self):
        # The accepted text is the SPDX file at a pinned commit, so a fake opener can only prove what the code insists
        # on: the payload is checked against the pinned digest, and a mismatch is refused rather than recorded.
        profile = self.profile(upstream_license='apache-2.0')
        with self.assertRaises(RuntimeError) as caught:
            self.downloader.license_provenance(profile, self.target,
                                               self.opener({'siblings': [], 'cardData': {'license': 'apache-2.0'}}))
        self.assertIn('SPDX reference checksum mismatch', str(caught.exception))
        self.assertFalse((self.target / 'provenance/publisher-license-declaration.json').exists())

    def test_a_matching_spdx_reference_is_written_and_hashed_offline(self):
        payload = b'Apache License Version 2.0, January 2004'
        expected = hashlib.sha256(payload).hexdigest()
        profile = self.profile(upstream_license='apache-2.0')
        with mock.patch.dict(self.downloader.SPDX_STANDARDS, {'apache-2.0': ('Apache-2.0', expected)}):
            hashes = self.downloader.license_provenance(
                profile, self.target,
                self.opener({'siblings': [], 'cardData': {'license': 'apache-2.0'}}))
        self.assertEqual(hashes, {'SPDX-standard-reference': expected})
        self.assertEqual((self.target / 'provenance/LICENSE-standard-reference.txt').read_bytes(), payload)
        record = self.record()
        self.assertEqual(record['declared_license'], 'apache-2.0')
        self.assertIsNone(record['publisher_license_file'])
        self.assertEqual(record['standard_reference_sha256'], expected)

    def test_a_card_that_disagrees_with_the_profile_is_not_treated_as_a_standard_declaration(self):
        profile = self.profile(upstream_license='apache-2.0')
        with self.assertRaises(RuntimeError) as caught:
            self.downloader.license_provenance(profile, self.target,
                                               self.opener({'siblings': [], 'cardData': {'license': 'mit'}}))
        self.assertIn('license_review', str(caught.exception))


class NamedAgreementTests(ProvenanceFixture):
    def card(self):
        return {'siblings': [], 'cardData': {'license': 'other', 'license_name': 'nvidia-open-model-agreement',
                                             'license_link': 'https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-agreement/'}}

    def test_an_upstream_with_no_license_text_refuses_until_the_profile_carries_the_review(self):
        profile = self.profile()
        with self.assertRaises(RuntimeError) as caught:
            self.downloader.license_provenance(profile, self.target, self.opener(self.card()))
        message = str(caught.exception)
        self.assertIn('manual review', message)
        self.assertIn('license_review', message)
        self.assertFalse((self.target / 'verified.json').exists())
        self.assertFalse((self.target / 'provenance/publisher-license-declaration.json').exists())

    def test_a_recorded_review_downloads_the_bytes_and_keeps_the_publishers_own_words(self):
        review = {'status': 'manual-approved', 'decided_by': 'owner', 'decided_at': '2026-11-24',
                  'note': 'evaluation proceeds with the license recorded as unknown; promotion needs its own decision'}
        profile = self.profile(license_review=review)
        hashes = self.downloader.license_provenance(profile, self.target, self.opener(self.card()))
        record = self.record()
        self.assertEqual(record['declared_license'], 'other')
        self.assertEqual(record['declared_license_name'], 'nvidia-open-model-agreement')
        self.assertEqual(record['declared_license_link'],
                         'https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-agreement/')
        self.assertEqual(record['accepted_by_review'], review)
        text = (self.target / 'provenance/publisher-license-declaration.json').read_bytes()
        self.assertEqual(hashes['publisher-license-declaration'], hashlib.sha256(text).hexdigest())

    def test_an_upstream_declaring_nothing_at_all_is_the_same_refusal(self):
        profile = self.profile(upstream_license=None)
        with self.assertRaises(RuntimeError):
            self.downloader.license_provenance(profile, self.target, self.opener({'siblings': []}))

    def test_a_reviewed_nothing_declared_upstream_records_the_absence_as_found(self):
        review = {'status': 'manual-approved', 'decided_by': 'owner', 'decided_at': '2026-11-24', 'note': 'unknown license'}
        profile = self.profile(upstream_license=None, license_review=review)
        self.downloader.license_provenance(profile, self.target, self.opener({'siblings': []}))
        self.assertIsNone(self.record()['declared_license'])
        self.assertIsNone(self.record()['declared_license_link'])

    def test_invalid_runtime_license_review_is_refused_without_writing_declaration(self):
        base = {'status': 'manual-approved', 'decided_by': 'owner', 'decided_at': '2026-11-24', 'note': 'ok'}
        bad_reviews = [
            'manual-approved',
            {**base, 'status': 'rejected'},
            {'status': 'manual-approved', 'note': 'missing who and when'},
            {**base, 'decided_by': '   '},
            {**base, 'decided_at': '2026-02-30'},
            {**base, 'extra': 'unexpected'},
        ]
        for bad in bad_reviews:
            with self.subTest(review=bad):
                profile = self.profile(license_review=bad)
                with self.assertRaises(RuntimeError) as caught:
                    self.downloader.license_provenance(profile, self.target, self.opener(self.card()))
                self.assertIn('license_review', str(caught.exception))
                self.assertFalse((self.target / 'provenance/publisher-license-declaration.json').exists())

    def test_list_card_license_and_omitted_upstream_license_do_not_crash(self):
        profile = self.profile()
        del profile['upstream']['license']
        with self.assertRaises(RuntimeError) as caught:
            self.downloader.license_provenance(
                profile, self.target,
                self.opener({'siblings': [], 'cardData': {'license': ['apache-2.0']}}))
        self.assertIn('license_review', str(caught.exception))


class DownloadOrderTests(ProvenanceFixture):
    def test_download_refuses_before_running_curl_when_review_is_missing(self):
        profile = self.profile()
        calls = []

        def fake_run(cmd, check=False):
            calls.append(cmd)

        with self.assertRaises(RuntimeError) as caught:
            self.downloader.download(
                profile, self.models_root,
                open_url=self.opener({'siblings': [], 'cardData': {'license': 'other'}}),
                run=fake_run)
        self.assertIn('license_review', str(caught.exception))
        self.assertEqual(calls, [])
        self.assertFalse((self.target / 'm.bin').exists())
        self.assertFalse((self.target / 'verified.json').exists())

    def test_download_verifies_weights_and_writes_provenance_and_verified_marker(self):
        payload = b'verified-model-bytes'
        digest = hashlib.sha256(payload).hexdigest()
        review = {'status': 'manual-approved', 'decided_by': 'owner', 'decided_at': '2026-11-24', 'note': 'approved'}
        profile = self.profile(license_review=review)
        profile['files'] = [{'path': 'm.bin', 'bytes': len(payload), 'sha256': digest}]
        calls = []

        def fake_run(cmd, check=False):
            calls.append(cmd)
            out_idx = cmd.index('--output') + 1
            Path(cmd[out_idx]).write_bytes(payload)

        fake_usage = mock.Mock(free=len(payload) + 30 * 1024**3)
        with mock.patch.object(self.downloader.shutil, 'disk_usage', return_value=fake_usage):
            self.downloader.download(
                profile, self.models_root,
                open_url=self.opener({'siblings': [], 'cardData': {'license': 'other'}}),
                run=fake_run)

        self.assertEqual(len(calls), 1)
        self.assertEqual((self.target / 'm.bin').read_bytes(), payload)
        self.assertEqual(json.loads((self.target / 'provenance/profile.json').read_text()), profile)
        hashes = json.loads((self.target / 'provenance/license-hashes.json').read_text())
        self.assertIn('publisher-license-declaration', hashes)
        verified = json.loads((self.target / 'verified.json').read_text())
        self.assertEqual(verified['profile'], profile)
        self.assertEqual(verified['license_hashes'], hashes)


class ProfileSchemaTests(unittest.TestCase):
    def schema(self):
        return json.loads((ROOT / 'profiles/schema.json').read_text())

    def valid(self, profile):
        jsonschema.Draft202012Validator(self.schema()).validate(profile)

    def test_a_profile_can_declare_an_engine_other_than_llama_cpp_within_its_own_slot_limit(self):
        profile = {'schema_version': 1, 'id': 'infernix-fixture', 'repo': 'someone/artifact', 'revision': 'a' * 40,
                   'upstream': {'repo': 'someone/upstream', 'revision': 'b' * 40, 'license': 'other'},
                   'quantization': 'NVFP4', 'nodes': ['spark'], 'files': [{'path': 'm', 'bytes': 1, 'sha256': '0' * 64}],
                   'entrypoint': 'm', 'backend': {'name': 'infernix', 'revision': 'c' * 40, 'context': 262144, 'slots': 8},
                   'license_review': {'status': 'manual-approved', 'decided_by': 'owner', 'decided_at': '2026-11-24',
                                      'note': 'recorded decision'}}
        self.valid(profile)

    def test_infernix_rejects_slots_above_its_ceiling(self):
        profile = {'schema_version': 1, 'id': 'infernix-fixture', 'repo': 'someone/artifact', 'revision': 'a' * 40,
                   'upstream': {'repo': 'someone/upstream', 'revision': 'b' * 40, 'license': 'apache-2.0'},
                   'quantization': 'NVFP4', 'nodes': ['spark'], 'files': [{'path': 'm', 'bytes': 1, 'sha256': '0' * 64}],
                   'entrypoint': 'm', 'backend': {'name': 'infernix', 'revision': 'c' * 40, 'context': 262144, 'slots': 9}}
        with self.assertRaises(jsonschema.ValidationError):
            self.valid(profile)

    def test_llama_cpp_keeps_its_own_stricter_slot_limit(self):
        profile = {'schema_version': 1, 'id': 'gpt-oss-fixture', 'repo': 'ggml-org/x', 'revision': 'a' * 40,
                   'upstream': {'repo': 'openai/x', 'revision': 'b' * 40, 'license': 'apache-2.0'},
                   'quantization': 'MXFP4', 'nodes': ['spark'], 'files': [{'path': 'm', 'bytes': 1, 'sha256': '0' * 64}],
                   'entrypoint': 'm', 'backend': {'name': 'llama.cpp', 'revision': 'c' * 40, 'context': 131072,
                                                  'slots': 4}}
        with self.assertRaises(jsonschema.ValidationError):
            self.valid(profile)

    def test_an_unknown_engine_name_is_still_not_a_profile(self):
        profile = {'schema_version': 1, 'id': 'x-fixture', 'repo': 'someone/x', 'revision': 'a' * 40,
                   'upstream': {'repo': 'someone/u', 'revision': 'b' * 40, 'license': 'apache-2.0'},
                   'quantization': 'Q4', 'nodes': ['spark'], 'files': [{'path': 'm', 'bytes': 1, 'sha256': '0' * 64}],
                   'entrypoint': 'm', 'backend': {'name': 'something-new', 'revision': 'c' * 40, 'context': 131072,
                                                  'slots': 1}}
        with self.assertRaises(jsonschema.ValidationError):
            self.valid(profile)

    def test_a_half_written_review_is_not_a_review(self):
        profile = {'schema_version': 1, 'id': 'x-fixture', 'repo': 'someone/x', 'revision': 'a' * 40,
                   'upstream': {'repo': 'someone/u', 'revision': 'b' * 40, 'license': 'other'},
                   'quantization': 'Q4', 'nodes': ['spark'], 'files': [{'path': 'm', 'bytes': 1, 'sha256': '0' * 64}],
                   'entrypoint': 'm', 'backend': {'name': 'infernix', 'revision': 'c' * 40, 'context': 131072, 'slots': 1},
                   'license_review': {'status': 'manual-approved', 'note': 'no name, no date'}}
        with self.assertRaises(jsonschema.ValidationError):
            self.valid(profile)

    def test_the_license_review_block_needs_both_a_name_and_a_date(self):
        # A review that names nobody is the refusal with extra steps, so the schema requires who and when.
        profile = {'schema_version': 1, 'id': 'x-fixture', 'repo': 'someone/x', 'revision': 'a' * 40,
                   'upstream': {'repo': 'someone/u', 'revision': 'b' * 40, 'license': 'other'},
                   'quantization': 'Q4', 'nodes': ['spark'], 'files': [{'path': 'm', 'bytes': 1, 'sha256': '0' * 64}],
                   'entrypoint': 'm', 'backend': {'name': 'infernix', 'revision': 'c' * 40, 'context': 131072, 'slots': 1},
                   'license_review': {'status': 'manual-approved', 'decided_by': 'owner', 'decided_at': '24/11/2026',
                                      'note': 'wrong date shape'}}
        with self.assertRaises(jsonschema.ValidationError):
            self.valid(profile)

    def test_whitespace_only_decided_by_or_note_is_rejected_by_schema(self):
        base = {'schema_version': 1, 'id': 'x-fixture', 'repo': 'someone/x', 'revision': 'a' * 40,
                'upstream': {'repo': 'someone/u', 'revision': 'b' * 40, 'license': 'other'},
                'quantization': 'Q4', 'nodes': ['spark'], 'files': [{'path': 'm', 'bytes': 1, 'sha256': '0' * 64}],
                'entrypoint': 'm', 'backend': {'name': 'infernix', 'revision': 'c' * 40, 'context': 131072, 'slots': 1}}
        for field in ('decided_by', 'note'):
            review = {'status': 'manual-approved', 'decided_by': 'owner', 'decided_at': '2026-11-24', 'note': 'ok'}
            review[field] = '   '
            with self.subTest(field=field):
                with self.assertRaises(jsonschema.ValidationError):
                    self.valid({**base, 'license_review': review})


if __name__ == '__main__':
    unittest.main()
