"""Backend file names follow `<engine>-<engine version>-<model>-<quantization>[-<variant>]`; retired names and
manifest ids still resolve, and renaming a file changes nothing a worker reports."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'gateway'))
from agentiac import backend_names, command, declared, paths, upgrade  # noqa: E402
from agentiac.services import status  # noqa: E402
import identity_policy  # noqa: E402

SITE = paths.root()


def planned(*argv):
    with patch.object(command, 'run', side_effect=lambda plan: plan) as run:
        command.main(list(argv))
    return run.call_args.args[0]


class ShippedNamesTests(unittest.TestCase):
    def setUp(self):
        self.graph = declared.load(SITE)
        self.legacy = backend_names.legacy(SITE)

    def test_every_backend_file_is_named_by_the_rule(self):
        self.assertEqual(backend_names.problems(self.graph.backends, self.legacy), [])
        for stem, manifest in self.graph.backends.items():
            if stem in backend_names.UNNAMED:
                continue
            with self.subTest(stem=stem):
                self.assertTrue(stem == backend_names.derived(manifest)
                                or stem.startswith(backend_names.derived(manifest) + '-'))
        self.assertEqual(sorted(self.legacy), sorted(set(self.legacy)))

    def test_a_target_key_is_its_node_its_backend_name_and_its_context(self):
        for target, spec in self.graph.route['targets'].items():
            with self.subTest(target=target):
                window = f"{spec['context_window'] // 1024}k"
                self.assertEqual(target, f"{spec['node']}-{spec['backend']}-{window}")

    def test_a_rename_keeps_the_runtime_id_and_bytes_the_workers_report(self):
        for old, new in self.legacy.items():
            with self.subTest(old=old):
                manifest_id = self.graph.backends[new]['id']
                expected = identity_policy.expected_identity(SITE, new)
                self.assertEqual(expected['backend_id'], manifest_id)
                self.assertEqual(expected['manifest_hash'],
                                 identity_policy.manifest_sha256(SITE / f'backends/{new}.json'))
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            (fixture / 'backends').mkdir()
            (fixture / 'backends/strata-0.2.12-tiny-q4_k_m.json').write_text(
                json.dumps({'id': 'legacy-worker-reported-id', 'source_version': '0.2.12'}))
            expected = identity_policy.expected_identity(fixture, 'strata-0.2.12-tiny-q4_k_m')
            self.assertEqual(expected['backend_id'], 'legacy-worker-reported-id')
            self.assertEqual(expected['manifest_hash'],
                             identity_policy.manifest_sha256(fixture / 'backends/strata-0.2.12-tiny-q4_k_m.json'))

    def test_retired_names_and_ids_resolve_and_unknown_names_do_not(self):
        backends = {
            'tf-1.1.4-fixture': {'id': 'tf-v114-id'},
            'strata-0.2.11-gyro': {'id': 'strata-gyro-0.2.11'},
        }
        legacy = {'old-tf': 'tf-1.1.4-fixture', 'strata-gyro-0.2.11': 'strata-0.2.11-gyro'}
        self.assertEqual(backend_names.resolve(backends, legacy, 'old-tf'), 'tf-1.1.4-fixture')
        self.assertEqual(backend_names.resolve(backends, legacy, 'tf-v114-id'), 'tf-1.1.4-fixture')
        self.assertEqual(backend_names.resolve(backends, legacy, 'strata-gyro-0.2.11'), 'strata-0.2.11-gyro')
        self.assertEqual(backend_names.resolve(backends, legacy, 'strata-0.2.11-gyro'), 'strata-0.2.11-gyro')
        self.assertEqual(backend_names.resolve(backends, legacy, 'no-such-backend'), 'no-such-backend')
        self.assertIsNone(backend_names.resolve(backends, legacy, None))
        for old, new in self.legacy.items():
            self.assertEqual(backend_names.resolve(self.graph.backends, self.legacy, old), new)
            self.assertIs(upgrade.manifest_for(self.graph, old), self.graph.backends[new])


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.graph = declared.load(SITE)
        self.sample = next(stem for stem in self.graph.backends if stem not in backend_names.UNNAMED)

    def test_a_misnamed_file_and_an_inconsistent_legacy_table_are_declaration_problems(self):
        backends = copy.deepcopy(self.graph.backends)
        backends['strata-misnamed-something'] = backends.pop(self.sample)
        problems = backend_names.problems(backends, {})
        self.assertEqual(problems, [f'backends/strata-misnamed-something.json must be named '
                                    f'engine-version-model-quant[-variant]: rename it to {self.sample}.json and add '
                                    f'the old name to {backend_names.LEGACY_FILE}'])
        for legacy in ({'old': 'missing'}, {self.sample: self.sample}, {'Old': self.sample}):
            with self.subTest(legacy=legacy):
                self.assertTrue(backend_names.problems(self.graph.backends, legacy))
        self.assertEqual(backend_names.problems(self.graph.backends, []),
                         [f'{backend_names.LEGACY_FILE} legacy_names must be a mapping'])

    def test_a_manifest_that_pins_too_little_cannot_be_named(self):
        backends = {'mystery': {'id': 'mystery', 'image': 'x'}}
        self.assertIn('a backend name needs a declared engine', backend_names.problems(backends, {})[0])

    def test_a_variant_suffix_is_allowed_after_the_derived_name(self):
        backends = {self.sample + '-trial': self.graph.backends[self.sample]}
        self.assertEqual(backend_names.problems(backends, {}), [])

    def test_the_naming_check_is_part_of_the_declaration_check(self):
        with patch.object(backend_names, 'problems', return_value=['sentinel']):
            self.assertIn('sentinel', declared.references(self.graph))


class RetiredNamesStillWorkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = Path(directory.name)
        shutil.copytree(ROOT / 'examples/site', self.fixture, dirs_exist_ok=True)
        graph = declared.load(self.fixture)
        self.backend = next(iter(graph.backends))
        self.strata_host = next(name for name, host in graph.nodes.items() if host.get('backend') == 'strata')
        (self.fixture / 'backends/legacy-names.yml').write_text(
            yaml.safe_dump({'legacy_names': {'old-tiny-backend': self.backend}}))
        alt = dict(graph.backends[self.backend], id='legacy-tiny-id')
        (self.fixture / f'backends/{self.backend}-alt.json').write_text(json.dumps(alt))

    def test_cli_backend_arguments_resolve_retired_names(self):
        with patch.object(command, 'ROOT', self.fixture):
            argv = planned('ops', 'prepare', self.strata_host, '--backend', 'old-tiny-backend', '--check')
        self.assertIn(f'backend_id={self.backend}', argv)

    def test_claim_accepts_a_retired_name(self):
        spec = importlib.util.spec_from_file_location('borrow_names', ROOT / 'scripts/borrow.py')
        borrow = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(borrow)
        with patch.object(borrow, 'ROOT', self.fixture), \
                patch.object(sys, 'argv', ['borrow', 'claim', self.strata_host, '--backend', 'old-tiny-backend']), \
                patch.object(borrow, 'claim') as claim, \
                patch.object(borrow, 'claims'), patch.object(borrow, 'mutation'):
            borrow.main()
        self.assertEqual(claim.call_args.args[5], self.backend)

    def test_a_root_keyed_by_file_name_wins_over_the_version_root(self):
        nodes = {'worker-a': {'strata_trial_roots': {
            '0.2.11': '/srv/worker/strata-0.2.11',
            'strata-0.2.11-gyro': '/srv/worker/strata-0.2.11-gyro',
        }}}
        backends = {
            'strata-0.2.11-gyro': {'id': 'strata-0.2.11-gyro', 'source_version': '0.2.11'},
            'strata-0.2.11-ud': {'id': 'strata-0.2.11-ud', 'source_version': '0.2.11'},
        }
        graph = declared.Declared(self.fixture, nodes, {}, backends, {'targets': {}}, [], [], {}, [], [], {})
        self.assertEqual(upgrade.trial_root(graph, 'worker-a', graph.backends['strata-0.2.11-gyro']),
                         ('strata-0.2.11-gyro', '/srv/worker/strata-0.2.11-gyro'))
        self.assertEqual(upgrade.trial_root(graph, 'worker-a', graph.backends['strata-0.2.11-ud'])[0], '0.2.11')
        text = (ROOT / 'infra/tasks/prepare-strata.yml').read_text()
        self.assertIn('trial_roots[backend_id] if backend_id in trial_roots else trial_roots[manifest.id]', text)

    def test_status_names_the_file_of_the_id_a_front_reports(self):
        with patch.object(status, 'ROOT', self.fixture):
            self.assertEqual(status.backend_name('legacy-tiny-id'), f'{self.backend}-alt')
            self.assertEqual(status.backend_name('not-declared'), 'not-declared')

    def test_the_legacy_table_is_yaml_outside_the_manifest_globs(self):
        self.assertFalse((SITE / 'backends/legacy-names.json').exists())
        self.assertNotIn('legacy-names', declared.load(SITE).backends)
        self.assertIsInstance(backend_names.legacy(SITE), dict)


if __name__ == '__main__':
    unittest.main()
