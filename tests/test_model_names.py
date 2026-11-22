"""Naming is a read-only binding to pinned declarations; node/legacy aliases do not define identities."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import catalog, command, declared, identities, model_names, paths  # noqa: E402
from agentiac.services import status  # noqa: E402
import borrow  # noqa: E402

SITE = paths.root()

TF_BACKEND = 'tf-1.1.4-qfn-mxfp8-nvfp4'
DS_BACKEND = 'strata-0.2.13-dsv41-coder-iq2_xxs'
GYRO_BACKEND = 'strata-0.2.8-qfn-gyro-m-tq2_0'
IQ3_BACKEND = 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'

TF_TARGET = f'worker-d-{TF_BACKEND}-256k'
DS_TARGET = f'worker-d-{DS_BACKEND}-128k'
GYRO_TARGET = f'worker-a-{GYRO_BACKEND}-128k'
IQ3_TARGET = f'worker-c-{IQ3_BACKEND}-256k'


def synthetic_graph():
    nodes = {
        'worker-a': {'node_class': 'gpu'},
        'worker-c': {'node_class': 'orin'},
        'worker-d': {'node_class': 'gb10', 'former_names': ['spark']},
    }
    backends = {
        TF_BACKEND: {
            'id': 'tf-v114',
            'engine': {'repository': 'https://example.invalid/TensorFold.git', 'version': '1.1.4',
                       'revision': '1' * 40},
            'model': {'repository': 'nvidia/Qwen3.8-Flash-Next-FP4-Local', 'revision': '2' * 40,
                      'files': [{'path': 'w.safetensors', 'sha256': 'a' * 64, 'size': 100}]},
            'quantization': 'mixed-MXFP8-NVFP4',
            'family': 'qfn',
            'kv_cache_type': 'bf16',
            'class_args': ['--temperature=0'],
            'config_args': ['--parallel=1'],
            'workers': {'worker-d': {'slots': 1}},
        },
        DS_BACKEND: {
            'id': 'strata-dsv41-coder-0.2.13',
            'repository': 'https://example.invalid/Strata.git',
            'source_version': '0.2.13',
            'source_revision': '3' * 40,
            'model_repository': 'example/DeepSeek-V4.1-Flash-1017-GSQ-RCO-Coder-GGUF',
            'model_revision': '4' * 40,
            'family': 'deepseek',
            'files': [
                {'path': 'dsv41-iq2_xxs.gguf', 'sha256': 'b' * 64, 'size': 100},
                {'path': 'dsv41-iq4_nl.gguf', 'sha256': 'c' * 64, 'size': 50},
            ],
            'engine_package': {'name': 'strata-dsv41', 'version': '0.1.59'},
            'workers': {'worker-d': {'slots': 1}},
        },
        GYRO_BACKEND: {
            'id': 'strata-gyro-0.2.8',
            'repository': 'https://example.invalid/Strata.git',
            'source_version': '0.2.8',
            'source_revision': '5' * 40,
            'model': 'Gyro-M',
            'variant': 'Gyro-M',
            'model_repository': 'example/Qwen3.8-Flash-Next-Gyro-GGUF',
            'model_revision': '6' * 40,
            'files': [{'path': 'model-tq2_0.gguf', 'sha256': 'd' * 64, 'size': 100}],
            'workers': {'worker-a': {'pool_workers': 15}},
        },
        IQ3_BACKEND: {
            'id': 'strata-iq3-0.2.12',
            'repository': 'https://example.invalid/Strata.git',
            'source_version': '0.2.12',
            'source_revision': '7' * 40,
            'model': 'IQ3_XXS',
            'model_repository': 'example/Qwen3.8-Flash-Next-GSQ-RCO-GGUF',
            'model_revision': '8' * 40,
            'files': [{'path': 'model-iq3_xxs.gguf', 'sha256': 'e' * 64, 'size': 100}],
            'workers': {'worker-c': {'slots': 1}},
        },
    }
    route = {
        'version': 2,
        'alias': 'local-coder',
        'default': TF_TARGET,
        'targets': {
            TF_TARGET: {'node': 'worker-d', 'backend': TF_BACKEND, 'source_alias': 'worker-d-worker-qfn',
                        'context_window': 262144, 'max_tokens': 16384},
            DS_TARGET: {'node': 'worker-d', 'backend': DS_BACKEND, 'source_alias': 'worker-d-worker-dsv41',
                        'context_window': 131072, 'max_tokens': 8192},
            GYRO_TARGET: {'node': 'worker-a', 'backend': GYRO_BACKEND, 'source_alias': 'worker-a-worker-gyro',
                          'context_window': 131072, 'max_tokens': 8192},
            IQ3_TARGET: {'node': 'worker-c', 'backend': IQ3_BACKEND, 'source_alias': 'worker-c-worker-iq3',
                         'context_window': 262144, 'max_tokens': 16384},
        },
        'legacy_targets': {
            'spark': TF_TARGET,
            'worker-c': IQ3_TARGET,
            'strata-worker-c-iq3xxs-0.2.12-256k': IQ3_TARGET,
        },
    }
    gateway_config = {
        'model_list': [
            {'model_name': 'worker-d-worker-qfn', 'litellm_params': {'model': 'openai/qfn', 'temperature': 0.0}},
            {'model_name': 'worker-d-worker-dsv41', 'litellm_params': {'model': 'openai/dsv41'}},
            {'model_name': 'worker-a-worker-gyro', 'litellm_params': {'model': 'openai/gyro'}},
            {'model_name': 'worker-c-worker-iq3', 'litellm_params': {'model': 'openai/iq3'}},
        ]
    }
    return declared.Declared(Path('/tmp/synth'), nodes, {}, backends, route,
                             ['worker-d-worker-qfn', 'worker-d-worker-dsv41', 'worker-a-worker-gyro',
                              'worker-c-worker-iq3'],
                             [], {}, [], [], {}, gateway_config=gateway_config)


class ModelNameTests(unittest.TestCase):
    def setUp(self):
        self.graph = synthetic_graph()
        self.site_graph = declared.load(SITE)

    def test_tensorfold_and_deepseek_targets_have_distinct_full_names(self):
        tf = model_names.describe(self.graph, 'spark')
        ds = model_names.describe(self.graph, DS_TARGET)
        self.assertIn('worker-d--tensorfold-1.1.4--nvidia-qwen3.8-flash-next-fp4-local', tf['full_name'])
        self.assertEqual(tf['quantization'], 'mixed-MXFP8-NVFP4')
        self.assertIn('worker-d--strata-0.2.13--', ds['full_name'])
        self.assertIn('deepseek-v4.1-flash', ds['full_name'])
        self.assertEqual(ds['quantization'], 'iq2_xxs+iq4_nl')
        self.assertEqual(ds['quantization_source'], 'weight filename labels')
        self.assertEqual(ds['engine_package']['version'], '0.1.59')
        self.assertNotEqual(tf['full_name'], ds['full_name'])

    def test_gyro_variant_and_quantization_are_both_visible(self):
        row = model_names.describe(self.graph, GYRO_TARGET)
        self.assertIn('gyro-m--tq2_0--', row['full_name'])
        self.assertEqual(row['variant'], 'Gyro-M')

    def test_duplicate_targets_bind_to_one_declared_name(self):
        row = next(row for row in model_names.bindings(self.graph) if IQ3_TARGET in row['target_aliases'])
        self.assertEqual(row['target_aliases'], [IQ3_TARGET])
        self.assertEqual(row['legacy_names'], ['worker-c', 'strata-worker-c-iq3xxs-0.2.12-256k'])
        self.assertEqual(row['api_aliases'],
                         [IQ3_TARGET, 'local-coder-worker-c', 'local-coder-strata-worker-c-iq3xxs-0.2.12-256k'])
        self.assertNotIn(row['full_name'], row['api_aliases'])

    def test_target_alias_renaming_does_not_change_the_named_configuration(self):
        before = model_names.describe(self.graph, 'spark')
        self.graph.route['targets']['new-alias'] = self.graph.route['targets'].pop(TF_TARGET)
        self.assertEqual(model_names.describe(self.graph, 'new-alias')['full_name'], before['full_name'])

    def test_pin_context_and_output_changes_cannot_share_a_full_name(self):
        before = model_names.describe(self.graph, 'spark')['full_name']
        for change in ('weights', 'engine', 'context', 'output', 'thinking'):
            graph = copy.deepcopy(self.graph)
            spec = graph.route['targets'][TF_TARGET]
            manifest = graph.backends[spec['backend']]
            if change == 'weights':
                manifest['model']['revision'] = 'a' * 40
            elif change == 'engine':
                manifest['engine']['revision'] = 'b' * 40
            elif change == 'context':
                spec['context_window'] += 1
            elif change == 'output':
                spec['max_tokens'] += 1
            else:
                spec['thinking_start'] = 'high'
            with self.subTest(change=change):
                self.assertNotEqual(model_names.describe(graph, 'spark')['full_name'], before)

    def test_names_do_not_mutate_declarations_or_existing_configuration_ids(self):
        before = copy.deepcopy(self.site_graph.route)
        ids = {name: identity.config_id for name, identity in identities.compute_all(self.site_graph).items()}
        rows = model_names.bindings(self.site_graph)
        self.assertEqual(self.site_graph.route, before)
        self.assertEqual({name: identity.config_id for name, identity in identities.compute_all(self.site_graph).items()},
                         ids)
        self.assertEqual(sum(len(row['target_aliases']) for row in rows), len(before['targets']))

    def test_unknown_quantization_is_not_invented_from_a_variant_label(self):
        manifest = self.graph.backends[DS_BACKEND]
        manifest['files'] = []
        row = model_names.describe(self.graph, DS_TARGET)
        self.assertEqual(row['quantization'], 'unknown')
        self.assertEqual(row['quantization_source'], 'not declared')

    def test_names_command_needs_no_host_or_evidence_database(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output, \
                patch.object(command.subprocess, 'run', side_effect=AssertionError('no subprocess expected')):
            self.assertEqual(command.main(['model', 'names', '--json']), 0)
        rows = json.loads(output.getvalue())
        self.assertEqual(rows['scope'], 'declared-configuration')
        self.assertTrue(rows['bindings'])

    def test_status_uses_reported_backend_instead_of_assuming_the_node_default(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            shutil.copytree(ROOT / 'examples/site', fixture, dirs_exist_ok=True)
            (fixture / f'backends/{DS_BACKEND}.json').write_text(json.dumps({'id': 'strata-dsv41-coder-0.2.13'}))
            state = {'state': 'unloaded', 'identity': {'backend_id': 'strata-dsv41-coder-0.2.13', 'context': 131072}}
            with patch.object(status, 'ROOT', fixture), \
                    patch.object(borrow, 'endpoint', return_value=(1, 'key')), \
                    patch.object(borrow, 'ondemand_call', return_value=(200, state)):
                text = command.host_state('worker-d', {'borrowed': True, 'spark_tensorfold': True})
        self.assertIn('unloaded', text)
        self.assertIn(DS_BACKEND, text)
        self.assertNotIn('tensorfold', text)

    def test_generated_alias_document_matches_the_declared_bindings(self):
        doc = model_names.documentation(self.site_graph)
        if (SITE / 'docs/model-names.md').exists():
            self.assertEqual(doc, (SITE / 'docs/model-names.md').read_text())
        else:
            for binding in model_names.bindings(self.site_graph):
                self.assertIn(binding['full_name'], doc)

    def test_unclaimed_tensorfold_host_reports_a_default_without_claiming_that_it_is_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            (fixture / 'backends').mkdir()
            (fixture / f'backends/{TF_BACKEND}.json').write_text(json.dumps({
                'id': TF_BACKEND,
                'engine': {'repository': 'https://example.invalid/TensorFold.git', 'version': '1.1.4'},
                'workers': {'worker-d': {}},
            }))
            with patch.object(status, 'ROOT', fixture), \
                    patch.object(borrow, 'endpoint', return_value=(1, 'key')), \
                    patch.object(borrow, 'ondemand_call', return_value=(503, {})), \
                    patch.object(command.urllib.request, 'urlopen', side_effect=OSError('no front')):
                text = command.host_state('worker-d', {'borrowed': True, 'spark_tensorfold': True})
        self.assertEqual(text, f'not claimed; default backend {TF_BACKEND}')

    def test_missing_flat_model_metadata_is_a_gap_instead_of_a_type_error(self):
        manifest = self.graph.backends[DS_BACKEND]
        manifest['files'] = []
        manifest.pop('model_repository')
        manifest.pop('model_revision')
        row = catalog.target_row(self.graph, DS_TARGET)
        self.assertEqual(row['declared']['weights']['repository'], 'unknown')
        self.assertEqual(row['declared']['naming']['quantization'], 'unknown')
        self.assertIn('weights', row['identity']['unknowns'])

    def test_gateway_inference_defaults_change_the_name_but_credentials_do_not_leak(self):
        before = model_names.describe(self.graph, TF_TARGET)['full_name']
        source = next(row['litellm_params'] for row in self.graph.gateway_config['model_list']
                      if row['model_name'] == self.graph.route['targets'][TF_TARGET]['source_alias'])
        source['temperature'] = 0.12345
        self.assertNotEqual(model_names.describe(self.graph, TF_TARGET)['full_name'], before)
        source['api_key'] = 'synthetic-credential-do-not-display'
        self.assertNotIn(source['api_key'], json.dumps(model_names.describe(self.graph, TF_TARGET)))

    def test_bare_model_shows_full_names_first_and_groups_existing_aliases(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(command.main(['model']), 0)
        text = output.getvalue()
        for binding in model_names.bindings(self.site_graph):
            self.assertEqual(text.splitlines().count(binding['full_name']), 1)
            self.assertIn('  aliases: ' + ', '.join(binding['target_aliases']), text)
        self.assertRegex(text, r'class [0-9a-f]{16}  config [0-9a-f]{16}')


if __name__ == '__main__':
    unittest.main()
