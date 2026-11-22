"""Node-first route targets (route file version 2) keep retired names working: routing, identity gates,
task-key scope, suite provenance and saved routes. The version 1 alias mechanism is checked on a fixture."""
import copy
import importlib.util
import io
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
from agentiac import catalog, command, declared, identities, model_names, paths, route_aliases  # noqa: E402
import agent  # noqa: E402
import route  # noqa: E402
import smoke  # noqa: E402
import task_policy  # noqa: E402

SITE = paths.root()

SPEC = importlib.util.spec_from_file_location('short_alias_suite', ROOT / 'scripts/suite.py')
suite_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(suite_cli)

TF = 'workera-tf-1.1.4-qfn-mxfp8-nvfp4-256k'
DS = 'workera-strata-0.2.13-dsv41-coder-iq2_xxs-128k'
WORKER_B = 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k'
WORKER_C = 'workerc-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k'
OLD_DEFAULT = 'strata-workerc-iq3xxs-0.2.12-256k'
RENAMED = {'oldhost-tf-1.1.4-qfn-mxfp8-nvfp4-256k': TF, 'oldhost-strata-0.2.13-dsv41-coder-iq2_xxs-128k': DS}


def write_synthetic_site(root):
    shutil.copytree(ROOT / 'examples/site', root, dirs_exist_ok=True)
    for path in (root / 'backends').glob('*.json'):
        path.unlink()
    inventory = {
        'all': {
            'vars': {'ansible_user': 'operator', 'gateway_port': 14000, 'chatgpt_port': 14001,
                     'nodejs_binary': '/usr/bin/node', 'rclone_config_dir': '/tmp/rclone',
                     'backup_destination': 'remote:b', 'resource_prefix': 'agentiac'},
            'children': {'workers': {'hosts': {
                'workera': {'node_class': 'gb10', 'former_names': ['oldhost']},
                'workerb': {'node_class': 'orin'},
                'workerc': {'node_class': 'orin'},
            }}},
        }
    }
    (root / 'infra/inventory.yml').write_text(yaml.safe_dump(inventory))
    backends = {
        'tf-1.1.4-qfn-mxfp8-nvfp4': {
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
            'workers': {'workera': {'slots': 1}},
        },
        'strata-0.2.13-dsv41-coder-iq2_xxs': {
            'id': 'strata-dsv41',
            'repository': 'https://example.invalid/Strata.git',
            'source_version': '0.2.13',
            'source_revision': '3' * 40,
            'model_repository': 'example/DeepSeek-V4.1-Flash-1017-GSQ-RCO-Coder-GGUF',
            'model_revision': '4' * 40,
            'files': [{'path': 'dsv41-iq2_xxs.gguf', 'sha256': 'b' * 64, 'size': 100}],
            'workers': {'workera': {'slots': 1}},
        },
        'strata-0.2.12-qfn-gsq-rco-iq3_xxs': {
            'id': 'strata-iq3',
            'repository': 'https://example.invalid/Strata.git',
            'source_version': '0.2.12',
            'source_revision': '5' * 40,
            'model': 'IQ3_XXS',
            'model_repository': 'example/Qwen3.8-Flash-Next-GSQ-RCO-GGUF',
            'model_revision': '6' * 40,
            'files': [{'path': 'qfn-iq3_xxs.gguf', 'sha256': 'c' * 64, 'size': 100}],
            'workers': {'workerb': {'slots': 1}, 'workerc': {'slots': 1}},
        },
    }
    for stem, spec in backends.items():
        (root / f'backends/{stem}.json').write_text(json.dumps(spec))
    route_spec = {
        'version': 2,
        'alias': 'local-coder',
        'default': WORKER_C,
        'targets': {
            TF: {'node': 'workera', 'backend': 'tf-1.1.4-qfn-mxfp8-nvfp4', 'source_alias': 'workera-worker-qfn',
                 'context_window': 262144, 'max_tokens': 16384},
            DS: {'node': 'workera', 'backend': 'strata-0.2.13-dsv41-coder-iq2_xxs',
                 'source_alias': 'workera-worker-dsv41', 'context_window': 131072, 'max_tokens': 8192},
            WORKER_B: {'node': 'workerb', 'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                       'source_alias': 'workerb-worker', 'context_window': 262144, 'max_tokens': 16384},
            WORKER_C: {'node': 'workerc', 'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                       'source_alias': 'workerc-worker', 'context_window': 262144, 'max_tokens': 16384},
        },
        'legacy_targets': {
            'oldhost': TF,
            'oldhost-ds': DS,
            'workerb': WORKER_B,
            OLD_DEFAULT: WORKER_C,
            **RENAMED,
        },
        'legacy_sources': {
            'oldhost-tensorfold': 'workera-worker-qfn',
            'strata-workerb-iq3xxs': 'workerb-worker',
        },
    }
    (root / 'gateway/local-coder.json').write_text(json.dumps(route_spec))
    gateway_config = {
        'model_list': [
            {'model_name': 'workera-worker-qfn',
             'litellm_params': {'model': 'openai/qfn', 'api_base': 'http://127.0.0.1:1', 'api_key': 'os.environ/A_API_KEY'}},
            {'model_name': 'workera-worker-dsv41',
             'litellm_params': {'model': 'openai/dsv41', 'api_base': 'http://127.0.0.1:2', 'api_key': 'os.environ/B_API_KEY'}},
            {'model_name': 'workerb-worker',
             'litellm_params': {'model': 'openai/iq3b', 'api_base': 'http://127.0.0.1:3', 'api_key': 'os.environ/C_API_KEY'}},
            {'model_name': 'workerc-worker',
             'litellm_params': {'model': 'openai/iq3c', 'api_base': 'http://127.0.0.1:4', 'api_key': 'os.environ/D_API_KEY'}},
            {'model_name': 'cloud-alias',
             'litellm_params': {'model': 'litellm_proxy/cloud', 'api_base': 'http://127.0.0.1:5'}},
        ]
    }
    (root / 'gateway/config.yaml').write_text(yaml.safe_dump(gateway_config))
    (root / 'agents/pi/router.json').write_text(json.dumps({
        'low': 'local-coder', 'high': 'cloud-alias', 'cloud_repos': ['example/repo'],
    }))
    (root / 'infra/components.yml').write_text('[]\n')
    return declared.load(root)


def version1(routes):
    """The same declarations as a version 1 route file: retired keys as targets, node-first names as short aliases."""
    legacy = {old: new for old, new in routes['legacy_targets'].items()
              if old.split('-', 1)[-1] != new.split('-', 1)[-1]}
    retired_default = [old for old, new in legacy.items() if new == routes['default']][-1]
    short = {}
    for old, new in legacy.items():
        if new not in short or '-' in old:
            short[new] = old
    return {'alias': routes['alias'], 'default': retired_default,
            'targets': {old: copy.deepcopy(routes['targets'][new]) for old, new in legacy.items()},
            'short_aliases': short}


class ShippedSiteTargetTests(unittest.TestCase):
    def setUp(self):
        self.graph = declared.load(SITE)
        self.routes = self.graph.route
        self.base = self.graph.gateway_config

    def test_shipped_targets_are_node_first_names(self):
        self.assertEqual(self.routes['version'], 2)
        self.assertNotIn('short_aliases', self.routes)
        self.assertEqual(model_names.alias_problems(self.graph), [])
        self.assertEqual(model_names.short_alias_coverage(self.graph), [])
        for target, spec in self.routes['targets'].items():
            self.assertEqual(target, model_names.short_name(self.graph, target))
            self.assertTrue(target.startswith(spec['node'] + '-'))

    def test_no_engine_first_transport_remains(self):
        names = [m['model_name'] for m in self.base['model_list']]
        self.assertFalse([name for name in names if name.startswith('strata-')], names)
        for target, spec in self.routes['targets'].items():
            self.assertIn(spec['source_alias'], names, target)
            self.assertNotIn(spec['source_alias'], self.routes.get('legacy_sources', {}), target)

    def test_completion_offers_targets_and_hides_retired_names(self):
        targets = list(self.routes['targets'])
        retired = route_aliases.retired_names(self.routes)
        for key in ('agentiac code --model', 'agentiac check -m', 'agentiac route', 'agentiac model'):
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(command.main(['completion']), 0)
            line = next(line for line in output.getvalue().splitlines() if f'["{key}"]=' in line)
            offered = line.split('=', 1)[1].strip('"').split()
            self.assertEqual([name for name in offered if name in targets], targets, key)
            self.assertFalse(set(offered) & retired, key)
            if key != 'agentiac code --model':
                self.assertEqual(len(offered), len(set(offered)), key)

    def test_catalogue_and_models_screen_agree_with_the_graph(self):
        rows = catalog.targets(SITE, current=self.routes['default'])
        self.assertEqual(model_names.catalog_names(rows), model_names.display_names(self.graph))
        if 'cloud_repos' in self.graph.router:
            with patch('sys.stdout', new_callable=io.StringIO) as output, \
                    patch.object(route, 'current', return_value=self.routes['default']):
                self.assertEqual(command.main(['models']), 0)
            first = output.getvalue().splitlines()[0]
            self.assertIn(f"-> {self.routes['default']} -> ", first)
            self.assertNotIn('route key', first)
            self.assertNotIn('-> strata-', first)
            self.assertIn('local-coder-<retired>', output.getvalue())


class NodeFirstTargetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = Path(directory.name)
        self.graph = write_synthetic_site(self.fixture)
        self.routes = self.graph.route
        self.base = self.graph.gateway_config
        for patcher in (patch.object(route, 'ROOT', self.fixture),
                        patch.object(route, 'ROUTES', self.fixture / 'gateway/local-coder.json'),
                        patch.object(smoke, 'ROOT', self.fixture),
                        patch.object(agent, 'ROOT', self.fixture)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_renaming_changes_no_identity_full_name_or_parameters(self):
        old_graph = copy.deepcopy(self.graph)
        old_graph.route = version1(self.routes)
        for old, new in self.routes['legacy_targets'].items():
            if old in RENAMED:
                continue
            with self.subTest(old=old):
                before = identities.compute_target(old_graph, old).as_dict()
                after = identities.compute_target(self.graph, new).as_dict()
                self.assertEqual((before.pop('target'), after.pop('target')), (old, new))
                self.assertEqual(before, after)
                self.assertEqual(model_names.describe(old_graph, old)['full_name'],
                                 model_names.describe(self.graph, new)['full_name'])
        old_config, old_callbacks = route.render(self.base, old_graph.route, old_graph.route['default'])
        config, callbacks = route.render(self.base, self.routes, self.routes['default'])
        before = {row['model_name']: row['litellm_params'] for row in old_config['model_list']}
        after = {row['model_name']: row['litellm_params'] for row in config['model_list']}
        for name in set(before) - set(self.routes['targets']):
            self.assertEqual(after[name], before[name], name)
        self.assertEqual({name: callbacks[name] for name in old_callbacks if name in callbacks},
                         {name: old_callbacks[name] for name in old_callbacks if name in callbacks})
        self.assertEqual(len(after), len(before) + len(self.routes['legacy_sources']) + len(RENAMED))

    def test_retired_api_names_share_the_new_names_parameters_and_identity_guard(self):
        config, callbacks = route.render(self.base, self.routes, self.routes['default'])
        entries = {row['model_name']: row['litellm_params'] for row in config['model_list']}
        for old, new in self.routes['legacy_targets'].items():
            with self.subTest(old=old):
                retired = 'local-coder-' + old
                self.assertEqual(entries[retired], entries[new])
                self.assertEqual(callbacks[retired], callbacks[new])
                self.assertEqual(callbacks[new][2]['context'], self.routes['targets'][new]['context_window'])
        self.assertNotIn('local-coder-' + TF, entries)
        self.assertNotEqual(callbacks[TF][2], callbacks[DS][2])

    def test_names_are_allowlisted_exactly_and_cannot_open_cloud(self):
        _, callbacks = route.render(self.base, self.routes, self.routes['default'])
        for name in [*self.routes['targets'], *('local-coder-' + old for old in self.routes['legacy_targets'])]:
            self.assertTrue(task_policy.model_allowed(name, callbacks), name)
            self.assertFalse(task_policy.model_allowed(name + '-typo', callbacks))
        for name in ('cloud-alias', 'workerb-worker', 'oldhost', OLD_DEFAULT, '*', None):
            self.assertFalse(task_policy.model_allowed(name, callbacks), name)

    def test_suite_provenance_resolves_new_and_retired_names(self):
        for old, new in self.routes['legacy_targets'].items():
            self.assertEqual(suite_cli.target_of(new, self.graph), new)
            self.assertEqual(suite_cli.target_of('local-coder-' + old, self.graph), new)
        self.assertIsNone(suite_cli.target_of(TF + '-typo', self.graph))
        self.assertIsNone(suite_cli.target_of('local-coder-' + TF, self.graph))
        self.assertIsNone(route_aliases.target_for_api(self.routes, 'local-coder', 'missing'))

    def test_a_route_switch_by_retired_key_saves_the_new_key(self):
        output = self.fixture / 'artifacts/gateway'
        with patch.object(route, 'OUT', output), patch.object(route, 'restart'), patch.object(route, 'record'):
            route.switch_to('oldhost')
        self.assertEqual(json.loads((output / 'route.json').read_text()), {'target': TF})
        self.assertEqual(json.loads(route.rendered('workerb')['route.json']), {'target': WORKER_B})

    def test_a_route_saved_under_a_retired_key_resolves_to_its_new_key(self):
        output = self.fixture / 'artifacts/gateway'
        output.mkdir(parents=True, exist_ok=True)
        (output / 'route.json').write_text(json.dumps({'target': OLD_DEFAULT}))
        with patch.object(route, 'OUT', output):
            self.assertEqual(route.current(), self.routes['default'])
        (output / 'route.json').write_text(json.dumps({'target': 'no-such-target'}))
        with patch.object(route, 'OUT', output), self.assertRaises(ValueError):
            route.current()

    def test_retired_key_chains_dangling_entries_and_collisions_are_refused(self):
        for retired in ({'old-a': 'old-b'}, {'old-a': 'missing'}, {TF: TF}, {'local-coder': TF},
                        {'cloud': 'x'}, {'Old': TF}):
            routes = copy.deepcopy(self.routes)
            routes['legacy_targets'] = retired
            with self.subTest(retired=retired), self.assertRaises(ValueError):
                route.render(self.base, routes, routes['default'])
        routes = copy.deepcopy(self.routes)
        routes['version'] = 1
        self.assertIn('legacy_targets needs route file version 2', route_aliases.structural_problems(routes))
        routes = copy.deepcopy(self.routes)
        routes['targets']['cloud-alias'] = routes['targets'][TF]
        with self.assertRaises(ValueError):
            route.render(self.base, routes, routes['default'])

    def test_a_key_that_is_not_node_first_is_a_declaration_problem_not_a_render_failure(self):
        graph = copy.deepcopy(self.graph)
        graph.route['targets']['plain'] = copy.deepcopy(graph.route['targets'][TF])
        problems = model_names.short_alias_coverage(graph)
        self.assertEqual(len(problems), 1)
        self.assertIn(f"target 'plain' must be named node-engine-version-model-quant-context: rename it to '{TF}'",
                      problems[0])
        self.assertIn(problems[0], declared.references(graph))
        route.render(graph.gateway_config, graph.route, 'plain')

    def test_additional_settings_can_use_a_declaration_qualified_name(self):
        graph = copy.deepcopy(self.graph)
        spec = copy.deepcopy(graph.route['targets'][TF])
        spec['max_tokens'] = 2048
        graph.route['targets']['tmp'] = spec
        qualified = model_names.short_name(graph, 'tmp') + '-d' + model_names.describe(graph, 'tmp')['declaration_id']
        graph.route['targets'][qualified] = graph.route['targets'].pop('tmp')
        self.assertEqual(model_names.short_alias_coverage(graph), [])
        self.assertNotEqual(model_names.describe(graph, qualified)['full_name'],
                            model_names.describe(graph, TF)['full_name'])

    def test_smoke_task_key_admission_uses_the_explicit_api_set(self):
        for name in (TF, DS, 'local-coder-oldhost', 'local-coder-' + OLD_DEFAULT):
            self.assertTrue(smoke.task_key_reaches(name), name)
        for name in ('oldhost', OLD_DEFAULT, 'cloud-alias', TF + '-typo', 'local-coder-' + TF):
            self.assertFalse(smoke.task_key_reaches(name), name)

    def test_a_malformed_table_is_a_validation_error_not_a_reader_crash(self):
        for key in ('short_aliases', 'legacy_targets'):
            for value in (None, [], [{'alias': 'target'}]):
                graph = copy.deepcopy(self.graph)
                graph.route[key] = value
                self.assertIn(f'{key} must be a mapping', declared.references(graph))

    def test_retired_transports_copy_their_transport_and_stay_outside_the_task_key(self):
        config, callbacks = route.render(self.base, self.routes, self.routes['default'])
        entries = {row['model_name']: row['litellm_params'] for row in config['model_list']}
        for old, new in self.routes['legacy_sources'].items():
            self.assertEqual(entries[old], entries[new], old)
            self.assertNotIn(old, callbacks)
            self.assertFalse(task_policy.model_allowed(old, callbacks))
            self.assertFalse(task_policy.model_allowed(new, callbacks))

    def test_inconsistent_retired_transport_tables_are_refused(self):
        names = [m['model_name'] for m in self.base['model_list']]
        for sources in ({'workera-worker-qfn': 'workerb-worker'}, {'old-x': 'missing'},
                        {'Old': 'workera-worker-qfn'}, {TF: 'workera-worker-qfn'},
                        {'local-coder': 'workera-worker-qfn'}):
            routes = copy.deepcopy(self.routes)
            routes['legacy_sources'] = sources
            with self.subTest(sources=sources):
                self.assertTrue(route_aliases.structural_problems(routes, names))
        routes = copy.deepcopy(self.routes)
        routes['version'] = 1
        routes.pop('legacy_targets')
        self.assertIn('legacy_sources needs route file version 2', route_aliases.structural_problems(routes, names))

    def test_pi_metadata_and_thinking_settings_match_for_new_and_retired_names(self):
        for old, new in self.routes['legacy_targets'].items():
            for thinking in (False, True):
                current, retired = agent.model_entry(new, thinking), agent.model_entry('local-coder-' + old, thinking)
                self.assertEqual(current.pop('id'), new)
                retired.pop('id')
                self.assertEqual(current, retired)

    def test_version1_api_names_and_resolution(self):
        v1 = version1(self.routes)
        names = route_aliases.api_targets(v1, v1['default'])
        self.assertEqual(names['local-coder-oldhost'], 'oldhost')
        self.assertEqual(names[TF], 'oldhost')
        self.assertNotIn(TF, v1['targets'])
        self.assertEqual(route_aliases.resolve_target(v1, TF), 'oldhost')
        self.assertEqual(route_aliases.target_for_api(v1, 'local-coder-workerb', None), 'workerb')
        self.assertEqual(route_aliases.retired_names(v1), set())
        self.assertEqual(route_aliases.structural_problems(v1), [])

    def test_fallbacks(self):
        self.assertEqual(model_names.names_from({'a': ('d1', ['a-short']), 'b': ('d1', []), 'c': ('d2', [])}),
                         {'a': 'a-short', 'b': 'a-short', 'c': 'c'})
        self.assertEqual(model_names.label('n', 'k'), 'n (route key k)')
        self.assertEqual(model_names.label('k', 'k'), 'k')


if __name__ == '__main__':
    unittest.main()
