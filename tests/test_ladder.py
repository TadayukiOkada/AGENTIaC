import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('ladder', Path(__file__).resolve().parents[1] / 'scripts/ladder.py')
ladder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ladder)


class LadderTests(unittest.TestCase):
    def test_default_depths_follow_the_plan(self):
        self.assertEqual(ladder.default_depths(131072), [4096, 32768, 65536, 100000])
        self.assertEqual(ladder.default_depths(65536), [4096, 32768, 60000])

    def test_padding_is_the_longest_that_fits_within_two_percent(self):
        chars, tokens = ladder.fit_padding(lambda n: 100 + n // 4, 10000)
        self.assertLessEqual(tokens, 10000)
        self.assertGreaterEqual(tokens, 9800)
        with self.assertRaisesRegex(ValueError, 'no padding'):
            ladder.fit_padding(lambda n: 20000, 10000)

    def test_prompt_places_each_anchor_once_in_order_without_repeating_padding(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            for name, needle in ladder.ANCHORS:
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('x\n' * 3000 + needle + '\n' + 'y\n' * 3000)
            (repo / 'src').mkdir()
            for index in range(3):
                (repo / f'src/file{index}.cpp').write_text(f'int f{index}();\n' * 2000)
            (repo / 'src/rng.cpp').write_text('uint32_t seed;\n' * 10)
            prompt = ladder.build_prompt(repo, 20000, 'n1')
            offsets = [prompt.index(needle) for _, needle in ladder.ANCHORS]
            self.assertEqual(offsets, sorted(offsets))
            self.assertTrue(all(prompt.count(needle) == 1 for _, needle in ladder.ANCHORS))
            self.assertNotIn('uint32_t seed', prompt)
            self.assertTrue(prompt.startswith('Run n1.'))
            self.assertTrue(prompt.endswith(ladder.grader.QUESTION))

    def test_speeds_and_summary(self):
        cold = {'usage': {'prompt_tokens': 1000}, 'ttft_s': 2.0}
        warm = {'usage': {'prompt_tokens': 1100, 'completion_tokens': 101}, 'ttft_s': 0.5, 'elapsed_s': 5.5,
                'finish_reason': 'length'}
        row = ladder.measure(cold, warm)
        self.assertEqual((row['prefill_tok_s'], row['decode_tok_s']), (500.0, 20.0))
        rows = [{'case': '4096-1', 'depth': 4096, 'status': 'failed'},
                {'case': '4096-1', 'depth': 4096, 'status': 'ok', 'answer_passed': True, **row},
                {'case': '4096-2', 'depth': 4096, 'status': 'ok', 'answer_passed': False, **row, 'decode_tok_s': 30.0}]
        summary = ladder.summarize(rows)[0]
        self.assertEqual((summary['ok'], summary['cases'], summary['answers_passed'], summary['decode_tok_s']), (2, 2, 1, 25.0))


DECLARED = {'backend_id': 'strata-qwen38-iq3xxs-0.2.10', 'manifest_hash': 'a' * 64, 'context': 262144}
OTHER = {'backend_id': 'strata-qwen38-iq3xxs-0.2.12', 'manifest_hash': 'b' * 64, 'context': 262144}


class IdentityGuardTests(unittest.TestCase):
    """The ladder resolves an alias from the gateway config and then speaks to that base URL directly, so the
    gateway's own refusal (T4.1, IP-02) never sees the request. Otherwise a case could be recorded under one
    alias while the worker front served another backend without anything in the run saying so. The guard is the
    ladder's own read of what the front declares."""

    def test_the_declared_identity_is_read_from_the_rendered_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'routes.json'
            path.write_text(json.dumps({
                'local-coder-strata-worker-a-iq3xxs-0.2.10-256k': ['http://127.0.0.1:18101', 'iq3_xxs', DECLARED],
                'local-coder-worker-b': ['http://127.0.0.1:18103', 'worker-b-model']}, indent=2) + '\n')
            entry = ladder.gateway_route('local-coder-strata-worker-a-iq3xxs-0.2.10-256k', path)
            self.assertEqual(entry[2], DECLARED)
            self.assertEqual(len(ladder.gateway_route('local-coder-worker-b', path)), 2)  # a profile declares no manifest
            with self.assertRaisesRegex(SystemExit, 'not a rendered gateway alias'):
                ladder.gateway_route('local-coder-nonesuch', path)
            with self.assertRaisesRegex(SystemExit, 'is not rendered'):
                ladder.gateway_route('local-coder-worker-b', Path(directory) / 'absent.json')

    def test_a_front_serving_another_backend_is_refused_by_name(self):
        with mock.patch.object(ladder, 'served_identity', return_value=OTHER):
            with self.assertRaises(SystemExit) as caught:
                ladder.identity_gate('local-coder-strata-worker-a-iq3xxs-0.2.10-256k', 'http://127.0.0.1:18101', 'k',
                                     DECLARED)
        message = str(caught.exception)
        for name in ('backend_id', 'strata-qwen38-iq3xxs-0.2.10', 'strata-qwen38-iq3xxs-0.2.12'):
            self.assertIn(name, message)   # the field that disagrees, and both backend ids

    def test_the_same_backend_id_with_another_manifest_is_refused_too(self):
        # Two targets of one manifest differ only in context, and a manifest replaced under a front differs only in
        # its hash: identity_policy names whichever field contradicts, and the message has to carry it.
        moved = dict(OTHER, backend_id=DECLARED['backend_id'])
        with mock.patch.object(ladder, 'served_identity', return_value=moved):
            with self.assertRaisesRegex(SystemExit, 'manifest_hash differs'):
                ladder.identity_gate('local-coder-strata-worker-a-iq3xxs-0.2.10-256k', 'http://127.0.0.1:18101', 'k',
                                     DECLARED)

    def test_a_front_with_nothing_to_declare_is_not_a_contradiction(self):
        with mock.patch.object(ladder, 'served_identity', return_value=None):
            self.assertIsNone(ladder.identity_gate('local-coder-worker-b', 'http://127.0.0.1:18103', 'k', None))

    def test_one_label_never_measures_two_backends(self):
        with mock.patch.object(ladder, 'served_identity', return_value=OTHER):
            with self.assertRaisesRegex(SystemExit, 'start a new --label'):
                ladder.identity_gate('local-coder-strata-worker-a-iq3xxs-0.2.12-256k', 'http://127.0.0.1:18101', 'k',
                                     OTHER, baseline=DECLARED)   # the run began against 0.2.10 and the front moved

    def test_a_run_refuses_before_it_records_any_case(self):
        args = SimpleNamespace(alias='local-coder-strata-worker-a-iq3xxs-0.2.10-256k', label='guard-test',
                               context=262144, depth=[4096], repeat=1, max_tokens=64, retry_failed=False)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(ladder, 'OUT', Path(directory)), \
                mock.patch.object(ladder, 'resolve', return_value=('http://127.0.0.1:18101', 'iq3_xxs', 'k')), \
                mock.patch.object(ladder, 'gateway_route', return_value=['http://127.0.0.1:18101', 'iq3_xxs', DECLARED]), \
                mock.patch.object(ladder, 'served_identity', return_value=OTHER):
            with self.assertRaises(SystemExit):
                ladder.run(args)
            folder = Path(directory) / 'guard-test'
            for name in ('results.jsonl', 'manifest.json', 'identity.json'):
                self.assertFalse((folder / name).exists(), f'{name} was written by a run that refused')

    def test_the_readings_are_kept_with_the_label(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with mock.patch.object(ladder, 'served_identity', return_value=OTHER):
                served = ladder.check_identity(folder, 'local-coder-strata-worker-a-iq3xxs-0.2.12-256k',
                                               'http://127.0.0.1:18101', 'k', OTHER, None, 'before')
            checks = json.loads((folder / 'identity.json').read_text())
            self.assertEqual(served, OTHER)
            self.assertEqual([(entry['position'], entry['served']['backend_id']) for entry in checks],
                             [('before', 'strata-qwen38-iq3xxs-0.2.12')])
