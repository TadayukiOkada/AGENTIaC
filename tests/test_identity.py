"""IP-02 (T4.1): an alias is served only by the backend its declaration describes.

A front can serve one model id across multiple engine revisions and context windows, so the
served-id check `_verify_loaded_model` made could pass while the alias delivered
something else. These tests cover the three parts: the front reports what it serves
(test_ondemand.py), the render carries what the alias expects (here, from
`backends/*.json`), and `gateway/identity_policy.py` compares the two (here).

gateway_callbacks.py cannot be imported from the control venv (litellm lives in
.venv-gateway), so its wiring is read out of the source, the way test_task_key.py
reads the task-key wiring.
"""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'gateway'))
from agentiac import paths  # noqa: E402
SITE = paths.root()
import identity_policy  # noqa: E402
import route  # noqa: E402

CALLBACKS = ROOT / 'gateway/gateway_callbacks.py'
_ROUTES = json.loads((SITE / 'gateway/local-coder.json').read_text())['targets']
TARGET_A, TARGET_B = next(
    (a, b)
    for a, spec_a in _ROUTES.items()
    for b, spec_b in _ROUTES.items()
    if a < b and 'backend' in spec_a and spec_a.get('backend') == spec_b.get('backend')
    and spec_a.get('context_window') == spec_b.get('context_window')
    and spec_a.get('node') != spec_b.get('node')
)
MANIFEST = _ROUTES[TARGET_A]['backend']
MANIFEST_CTX = _ROUTES[TARGET_A]['context_window']


def front(path):
    """The report a front with this manifest file would give its status endpoint."""
    manifest = json.loads(Path(path).read_text())
    return {'backend_id': manifest.get('id', Path(path).stem),
            'manifest_hash': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'context': manifest.get('context')}


class ExpectedIdentityTests(unittest.TestCase):
    def test_nothing_is_expected_where_no_manifest_is_declared(self):
        self.assertIsNone(identity_policy.expected_identity(SITE, 'no-such-backend'))

    def test_the_expectation_names_the_backend_its_bytes_and_its_context(self):
        identity = identity_policy.expected_identity(SITE, MANIFEST)
        manifest = json.loads((SITE / f'backends/{MANIFEST}.json').read_text())
        self.assertEqual(identity['backend_id'], manifest['id'])
        self.assertEqual(identity['manifest_hash'], hashlib.sha256((SITE / f'backends/{MANIFEST}.json').read_bytes()).hexdigest())
        self.assertEqual(identity['context'], manifest['context'])

    def test_the_route_target_says_which_context_of_a_manifest_is_expected(self):
        # one manifest, two targets: only the context distinguishes a 64K alias from a 128K one
        sixty_four = identity_policy.expected_identity(SITE, MANIFEST, 65536)
        one_twenty_eight = identity_policy.expected_identity(SITE, MANIFEST, 131072)
        self.assertEqual({k: v for k, v in sixty_four.items() if k != 'context'},
                         {k: v for k, v in one_twenty_eight.items() if k != 'context'})
        self.assertEqual((sixty_four['context'], one_twenty_eight['context']), (65536, 131072))


class IdentityProblemTests(unittest.TestCase):
    def setUp(self):
        self.expected = identity_policy.expected_identity(SITE, MANIFEST, MANIFEST_CTX)

    def test_nothing_is_compared_without_an_expectation_or_without_a_report(self):
        self.assertIsNone(identity_policy.identity_problem(None, front(SITE / f'backends/{MANIFEST}.json')))
        self.assertIsNone(identity_policy.identity_problem(self.expected, None))  # a front predating T4.1

    def test_the_report_of_the_declared_manifest_passes(self):
        self.assertIsNone(identity_policy.identity_problem(self.expected, front(SITE / f'backends/{MANIFEST}.json')))

    def test_a_different_version_of_the_same_backend_is_a_mismatch(self):
        # one model, one served id, the same weight bytes, two engine revisions
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'backends').mkdir()
            shared = {'served_model': 'shared-model', 'context': 65536,
                      'files': [{'path': 'weights.gguf', 'size': 1024, 'sha256': 'a' * 64}]}
            v1 = root / 'backends/engine-v1.json'
            v2 = root / 'backends/engine-v2.json'
            v1.write_text(json.dumps({**shared, 'id': 'engine-v1', 'source_version': '0.1.0'}))
            v2.write_text(json.dumps({**shared, 'id': 'engine-v2', 'source_version': '0.2.0'}))
            expected = identity_policy.expected_identity(root, 'engine-v2')
            self.assertEqual(identity_policy.identity_problem(expected, front(v1)), 'backend_id')
        # and a manifest whose bytes changed under the same id is caught too: that is what the hash is for
        tampered = front(SITE / f'backends/{MANIFEST}.json') | {'manifest_hash': '0' * 64}
        self.assertEqual(identity_policy.identity_problem(self.expected, tampered), 'manifest_hash')

    def test_the_other_context_of_the_same_manifest_is_a_mismatch(self):
        reported = front(SITE / f'backends/{MANIFEST}.json') | {'context': MANIFEST_CTX // 2}
        self.assertEqual(identity_policy.identity_problem(self.expected, reported), 'context')

    def test_a_front_serving_another_backend_is_a_mismatch(self):
        reported = front(SITE / f'backends/{MANIFEST}.json') | {'backend_id': 'other-backend'}
        self.assertEqual(identity_policy.identity_problem(self.expected, reported), 'backend_id')

    def test_a_manifest_the_front_cannot_read_is_never_a_pass(self):
        for reported in ({'error': f'FileNotFoundError: {MANIFEST}.json'}, 'nonsense'):
            with self.subTest(reported=reported):
                self.assertEqual(identity_policy.identity_problem(self.expected, reported), 'manifest_hash')
        self.assertEqual(identity_policy.identity_problem(self.expected, {}), 'backend_id')  # says nothing

    def test_a_field_the_front_left_out_does_not_match(self):
        # Every front has reported all three since T4.1, so a missing one is not an old front.
        partial = {'backend_id': self.expected['backend_id']}
        self.assertEqual(identity_policy.identity_problem(self.expected, partial), 'manifest_hash')
        self.assertEqual(identity_policy.identity_problem(self.expected, dict(self.expected, context=MANIFEST_CTX // 2)), 'context')

    def test_no_report_at_all_keeps_the_served_id_check(self):
        self.assertIsNone(identity_policy.identity_problem(self.expected, None))


class RenderTests(unittest.TestCase):
    def routes(self):
        return json.loads(route.ROUTES.read_text())

    def callback(self, target):
        return json.loads(route.rendered(target)['routes.json'])

    def test_a_manifested_target_carries_its_identity_and_a_plain_one_carries_nothing(self):
        spec = self.routes()['targets'][TARGET_A]
        entry = self.callback(TARGET_A)[TARGET_A]
        self.assertEqual(len(entry), 3)
        self.assertEqual(entry[2], identity_policy.expected_identity(SITE, spec['backend'], spec['context_window']))
        # A target with no `backend` is rendered as [base, served id] and nothing else.
        routes = json.loads(json.dumps(self.routes()))
        routes['targets']['plain'] = {'source_alias': spec['source_alias'], 'node': spec['node'],
                                      'context_window': 65536, 'max_tokens': 8192}
        _config, callback = route.render(yaml.safe_load((SITE / 'gateway/config.yaml').read_text()), routes, 'plain')
        self.assertEqual(len(callback['plain']), 2)

    def test_two_nodes_serving_one_manifest_expect_one_identity(self):
        # two nodes serve the same delivery under different source aliases: identity is the facts, not the alias
        first = self.callback(TARGET_A)[TARGET_A][2]
        second = self.callback(TARGET_B)[TARGET_B][2]
        self.assertEqual(first, second)

    def test_the_two_contexts_of_one_manifest_are_distinguishable(self):
        # A different context window of the same manifest must not be servable as the declared one.
        routes = json.loads(route.ROUTES.read_text())
        name = f'{TARGET_A}-half'
        routes['targets'][name] = dict(routes['targets'][TARGET_A], context_window=MANIFEST_CTX // 2)
        callback = route.render(yaml.safe_load((SITE / 'gateway/config.yaml').read_text()), routes, name)[1]
        half = callback[name][2]
        full = callback[TARGET_A][2]
        self.assertNotEqual(half['context'], full['context'])
        self.assertEqual(half['manifest_hash'], full['manifest_hash'])

    def test_every_manifested_target_of_the_shipped_route_is_verifiable(self):
        for target, spec in self.routes()['targets'].items():
            entry = self.callback(target)[target]
            with self.subTest(target=target):
                if 'backend' in spec:
                    self.assertEqual(entry[2], identity_policy.expected_identity(SITE, spec['backend'], spec.get('context_window')))
                else:
                    self.assertEqual(len(entry), 2)


class SignatureTests(unittest.TestCase):
    """A manifest edit must make the gateway pending: it holds the hashes it compares against."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for path in ('gateway/config.yaml', 'gateway/local-coder.json', 'gateway/chatgpt.yaml', 'infra/inventory.yml'):
            src = SITE / path
            if path == 'gateway/chatgpt.yaml' and not src.exists():
                continue
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, self.root / path)
        for path in ('gateway/gateway_callbacks.py', 'gateway/task_policy.py', 'gateway/identity_policy.py',
                     'gateway/chatgpt_auth.py', 'requirements-gateway.lock'):
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, self.root / path)
        (self.root / 'secrets').mkdir()         # the signature reads the file; a clean clone has no real one to copy
        (self.root / 'secrets/gateway.env').write_text('FIXTURE=not-a-secret\n')
        (self.root / 'backends').mkdir()
        for manifest in (SITE / 'backends').glob('*.json'):
            shutil.copyfile(manifest, self.root / 'backends' / manifest.name)
        (self.root / 'artifacts').mkdir()
        for name, value in (('ROOT', self.root), ('ROUTES', self.root / 'gateway/local-coder.json'),
                            ('OUT', self.root / 'artifacts/gateway')):
            patcher = mock.patch.object(route, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_manifest_change_is_pending_because_the_gateway_compares_its_hash(self):
        route.record('gateway')
        self.assertFalse(route.pending('gateway'))
        path = self.root / 'backends' / f'{MANIFEST}.json'
        manifest = json.loads(path.read_text()) | {'context': manifest_context(path) // 2}
        path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.assertTrue(route.pending('gateway'))

    def test_an_unrelated_file_is_not_pending(self):
        route.record('gateway')
        (self.root / 'backends' / 'unrelated.json').write_text('{}\n')
        self.assertFalse(route.pending('gateway'))


def manifest_context(path):
    return json.loads(Path(path).read_text())['context']


class WiringTests(unittest.TestCase):
    """The comparison is only real if the pre-call hook asks the front and refuses on a mismatch.

    Read from the AST rather than the text: `if problem:` appears for the served-id check too,
    so a substring test passes even when the identity refusal is gone.
    """

    def function(self, name):
        tree = ast.parse(CALLBACKS.read_text())
        return next(node for node in ast.walk(tree)
                    if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name)

    def strings(self, node):
        return {child.value for child in ast.walk(node) if isinstance(child, ast.Constant) and isinstance(child.value, str)}

    def test_the_pre_call_asks_the_front_what_it_serves(self):
        self.assertIn('/ondemand/status', self.strings(self.function('_identity_problem')))
        called = {ast.unparse(child.func) for child in ast.walk(self.function('_identity_problem')) if isinstance(child, ast.Call)}
        self.assertIn('identity_policy.identity_problem', called)

    def test_a_mismatch_refuses_the_call(self):
        body = self.function('_verify_loaded_model')
        called = {ast.unparse(child.func) for child in ast.walk(body) if isinstance(child, ast.Call)}
        self.assertIn('_identity_problem', called)  # asking is not enough to be refusing, and vice versa
        refusals = [node for node in ast.walk(body) if isinstance(node, ast.If)
                    and ast.unparse(node.test) == 'problem'
                    and any(isinstance(raise_, ast.Raise) for raise_ in node.body)]
        self.assertEqual(len(refusals), 1, 'the identity result must be refused, not logged')

    def test_the_front_is_read_passively_and_never_loads_through_it(self):
        # a check that keeps a model loaded would be worse than no check at all
        self.assertNotIn('X-Ondemand-Load', self.strings(self.function('_identity_problem')))
        self.assertIn('X-Ondemand-Load', self.strings(self.function('_verify_loaded_model')))

    def test_the_callback_reads_the_third_entry_as_the_identity(self):
        self.assertIn("route[2] if len(route) > 2 else None", ast.unparse(self.function('_verify_loaded_model')))


if __name__ == '__main__':
    unittest.main()
