"""T10.2: class_id and config_id are computed from the declared graph, and they answer the PA §6 questions."""
import copy
import io
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import command, declared, identities, paths  # noqa: E402
from agentiac.identities import UNKNOWN, compute_all, compute_target, hardware_fingerprint  # noqa: E402

SITE = paths.root()
HEX16 = re.compile(r'^[0-9a-f]{16}$')

SYNTH_GYRO = 'strata-0.2.8-gyro-m-tq2_0'
SYNTH_IQ3 = 'strata-0.2.10-iq3_xxs'
SYNTH_TF = 'tf-1.1.4-fixture'
WORKER_A_TARGET = 'worker-a-strata-0.2.8-gyro-m-tq2_0-128k'
WORKER_B_TARGET = 'worker-b-strata-0.2.8-gyro-m-tq2_0-128k'


def synthetic_graph():
    backends = {
        SYNTH_GYRO: {
            'id': SYNTH_GYRO,
            'repository': 'https://example.invalid/Strata',
            'source_version': '0.2.8',
            'source_revision': '1' * 40,
            'model': 'Gyro-M',
            'model_repository': 'example/Model-Gyro',
            'model_revision': '2' * 40,
            'files': [
                {'path': 'model-00001.gguf', 'sha256': 'a' * 64, 'size': 100},
                {'path': 'model-00002.gguf', 'sha256': 'b' * 64, 'size': 200},
            ],
            'contexts': [65536, 131072],
            'workers': {'worker-a': {'pool_workers': 15}, 'worker-b': {'pool_workers': 7}},
        },
        SYNTH_IQ3: {
            'id': SYNTH_IQ3,
            'repository': 'https://example.invalid/Strata',
            'source_version': '0.2.10',
            'source_revision': '3' * 40,
            'model': 'IQ3_XXS',
            'model_repository': 'example/Model-IQ3',
            'model_revision': '4' * 40,
            'files': [
                {'path': 'a.gguf', 'sha256': 'c' * 64, 'size': 100},
                {'path': 'b.gguf', 'sha256': 'd' * 64, 'size': 200},
            ],
            'contexts': [262144],
            'workers': {'worker-c': {'slots': 1}},
        },
        SYNTH_TF: {
            'id': SYNTH_TF,
            'engine': {'repository': 'https://example.invalid/TensorFold.git', 'version': '1.1.4',
                       'revision': '5' * 40},
            'model': {'repository': 'example/Model-TF', 'revision': '6' * 40,
                      'files': [{'path': 'w.safetensors', 'sha256': 'e' * 64, 'size': 300}]},
            'quantization': 'mixed-MXFP8-NVFP4',
            'family': 'qfn',
            'kv_cache_type': 'bf16',
            'class_args': ['--temperature=0', '--top-p=1', '--no-thinking'],
            'config_args': ['--parallel=1', '--kv-cache=bf16'],
            'contexts': [262144],
            'workers': {'worker-d': {'slots': 1}},
        },
    }
    route = {
        'version': 2,
        'alias': 'local-coder',
        'default': WORKER_A_TARGET,
        'targets': {
            WORKER_A_TARGET: {'node': 'worker-a', 'backend': SYNTH_GYRO,
                              'source_alias': 'worker-a-worker-gyro',
                              'context_window': 131072, 'max_tokens': 16384},
            WORKER_B_TARGET: {'node': 'worker-b', 'backend': SYNTH_GYRO,
                              'source_alias': 'worker-b-worker-gyro',
                              'context_window': 131072, 'max_tokens': 16384},
            'worker-c': {'node': 'worker-c', 'backend': SYNTH_IQ3,
                         'source_alias': 'worker-c-worker-iq3',
                         'context_window': 262144, 'max_tokens': 16384},
            'worker-d': {'node': 'worker-d', 'backend': SYNTH_TF,
                         'source_alias': 'worker-d-worker-tf',
                         'context_window': 262144, 'max_tokens': 16384},
        },
    }
    nodes = {n: {'node_class': 'gpu'} for n in ('worker-a', 'worker-b', 'worker-c', 'worker-d')}
    return declared.Declared(Path('/tmp/synth'), nodes, {}, backends, route, [], [], {}, [], [], {})


class Mutated(unittest.TestCase):
    """Base: compute an identity from a private, mutated copy of a synthetic declared graph."""

    def setUp(self):
        self.graph = synthetic_graph()

    def backend(self, stem):
        return self.graph.backends[stem]

    def edit_backend(self, stem, mutate):
        mutate(self.backend(stem))

    def identity(self, target):
        return compute_target(self.graph, target)


class AcceptanceTests(unittest.TestCase):
    """Identity properties over the shipped declarations."""

    def setUp(self):
        self.graph = declared.load()
        self.all = compute_all(self.graph)

    def replica_pair(self):
        by_backend = {}
        for target, spec in self.graph.route['targets'].items():
            by_backend.setdefault(spec.get('backend'), []).append((target, spec))
        for items in by_backend.values():
            for i, (t1, s1) in enumerate(items):
                for t2, s2 in items[i + 1:]:
                    if s1['node'] != s2['node'] and s1['context_window'] == s2['context_window']:
                        return t1, t2
        self.fail('no replica pair found in route targets')

    def test_the_two_replicas_share_a_class_id_and_keep_their_own_config_id(self):
        first, second = self.replica_pair()
        self.assertEqual(self.all[first].class_id, self.all[second].class_id)
        self.assertEqual(self.all[first].class_inputs, self.all[second].class_inputs)
        self.assertNotEqual(self.all[first].config_id, self.all[second].config_id)
        self.assertEqual(self.all[first].config_inputs['node'], self.graph.route['targets'][first]['node'])
        self.assertEqual(self.all[second].config_inputs['node'], self.graph.route['targets'][second]['node'])

    def test_different_contexts_and_engine_versions_are_different_classes(self):
        graph = copy.deepcopy(self.graph)
        target, spec = next(iter(graph.route['targets'].items()))
        other_ctx = spec['context_window'] // 2
        other_name = f'{target}-{other_ctx // 1024}k'
        graph.route['targets'][other_name] = dict(spec, context_window=other_ctx)
        self.assertNotEqual(compute_target(graph, target).class_id, compute_target(graph, other_name).class_id)

        newer = compute_target(graph, target)
        backend = graph.backends[spec['backend']]
        backend.update({'source_version': '0.0.1-other', 'source_revision': '9' * 40})
        older = compute_target(graph, target)
        self.assertEqual(newer.class_inputs['weights'], older.class_inputs['weights'])
        self.assertNotEqual(newer.class_id, older.class_id)

    def test_every_target_computes_and_the_ids_are_16_hex(self):
        for target, identity in self.all.items():
            self.assertRegex(identity.class_id, HEX16, target)
            self.assertRegex(identity.config_id, HEX16, target)
            self.assertEqual(identity.config_inputs['class_id'], identity.class_id)

    def test_recomputing_changes_nothing(self):
        self.assertEqual({t: i.class_id for t, i in self.all.items()},
                         {t: i.class_id for t, i in compute_all(declared.load()).items()})


class ClassIdTests(Mutated):
    def test_a_source_alias_is_never_an_input(self):
        before = self.identity(WORKER_A_TARGET)
        self.graph.route['targets'][WORKER_A_TARGET]['source_alias'] = 'worker-a-renamed'
        after = self.identity(WORKER_A_TARGET)
        self.assertEqual(before.class_id, after.class_id)
        self.assertEqual(before.config_id, after.config_id)

    def test_a_manifest_id_is_never_an_input(self):
        before = self.identity(WORKER_A_TARGET)
        self.edit_backend(SYNTH_GYRO, lambda b: b.__setitem__('id', 'strata-gyro-m-fork'))
        self.assertEqual(before.class_id, self.identity(WORKER_A_TARGET).class_id)

    def test_a_different_weight_hash_is_a_different_class(self):
        before = self.identity(WORKER_A_TARGET).class_id
        self.edit_backend(SYNTH_GYRO, lambda b: b['files'][0].__setitem__('sha256', '0' * 64))
        self.assertNotEqual(before, self.identity(WORKER_A_TARGET).class_id)

    def test_a_different_file_order_is_the_same_class(self):
        before = self.identity('worker-c').class_id
        self.edit_backend(SYNTH_IQ3, lambda b: b['files'].reverse())
        self.assertEqual(before, self.identity('worker-c').class_id)

    def test_a_different_model_revision_is_a_different_class(self):
        before = self.identity(WORKER_A_TARGET).class_id
        self.edit_backend(SYNTH_GYRO, lambda b: b.__setitem__('model_revision', '0' * 40))
        self.assertNotEqual(before, self.identity(WORKER_A_TARGET).class_id)

    def test_the_engine_version_is_an_input(self):
        before = self.identity(WORKER_A_TARGET).class_id
        self.edit_backend(SYNTH_GYRO, lambda b: b.__setitem__('source_version', '0.2.1.3'))
        self.assertNotEqual(before, self.identity(WORKER_A_TARGET).class_id)

    def test_the_context_and_max_tokens_are_inputs(self):
        for name in ('context_window', 'max_tokens'):
            before = self.identity(WORKER_A_TARGET).class_id
            self.graph.route['targets'][WORKER_A_TARGET][name] += 1024
            self.assertNotEqual(before, self.identity(WORKER_A_TARGET).class_id, name)

    def test_an_output_affecting_launch_argument_moves_both_ids(self):
        before = self.identity('worker-d')
        self.edit_backend(SYNTH_TF, lambda b: b['class_args'].append('--mtp=6'))
        after = self.identity('worker-d')
        self.assertNotEqual(before.class_id, after.class_id)
        self.assertNotEqual(before.config_id, after.config_id)

    def test_argument_order_in_the_declaration_is_not_an_identity(self):
        before = self.identity('worker-d').class_id
        self.edit_backend(SYNTH_TF, lambda b: b.__setitem__('class_args', list(reversed(b['class_args']))))
        self.assertEqual(before, self.identity('worker-d').class_id)

    def test_an_empty_declared_list_is_a_declaration_not_an_unknown(self):
        self.edit_backend(SYNTH_TF, lambda b: (b.__setitem__('class_args', []), b.__setitem__('config_args', [])))
        identity = self.identity('worker-d')
        self.assertEqual(identity.class_inputs['class_args'], [])
        self.assertEqual(identity.config_inputs['config_args'], [])
        self.assertEqual(set(identity.unknowns), set())

    def test_the_quantization_falls_back_to_the_model_name(self):
        self.assertEqual(self.identity(WORKER_A_TARGET).class_inputs['quantization'], 'Gyro-M')
        self.assertEqual(self.identity('worker-c').class_inputs['quantization'], 'IQ3_XXS')


class ConfigIdTests(Mutated):
    def test_a_replica_fact_moves_only_the_config_id(self):
        before = self.identity(WORKER_A_TARGET)
        self.edit_backend(SYNTH_GYRO, lambda b: b['workers']['worker-a'].__setitem__('pool_workers', 31))
        after = self.identity(WORKER_A_TARGET)
        self.assertEqual(before.class_id, after.class_id)
        self.assertNotEqual(before.config_id, after.config_id)

    def test_the_slots_come_from_the_worker_of_the_target_node(self):
        self.assertEqual(self.identity(WORKER_A_TARGET).config_inputs['slots'], 15)
        self.assertEqual(self.identity(WORKER_B_TARGET).config_inputs['slots'], 7)

    def test_a_performance_only_argument_moves_only_the_config_id(self):
        before = self.identity(WORKER_A_TARGET)
        self.edit_backend(SYNTH_GYRO, lambda b: b.setdefault('config_args', []).append('--kv-cache=fp8'))
        after = self.identity(WORKER_A_TARGET)
        self.assertEqual(before.class_id, after.class_id)
        self.assertNotEqual(before.config_id, after.config_id)

    def test_a_manifest_without_declared_lists_stays_stable_and_says_unknown(self):
        before = self.identity('worker-d')
        self.edit_backend(SYNTH_TF, lambda b: (b.pop('class_args'), b.pop('config_args')))
        after = self.identity('worker-d')
        self.assertEqual(set(after.unknowns), {'class_args', 'config_args'})
        self.assertEqual(after.class_inputs['class_args'], UNKNOWN)
        self.assertNotEqual(before.class_id, after.class_id)


class UnmanifestedTargetTests(Mutated):
    """A target that names no backend manifest hashes all-unknowns rather than guessing."""

    PLAIN = 'plain'

    def setUp(self):
        super().setUp()
        self.graph.route['targets'][self.PLAIN] = {'source_alias': 'worker-c-fallback', 'node': 'worker-c',
                                                   'context_window': 65536, 'max_tokens': 8192}

    def test_a_target_without_a_manifest_hashes_unknowns_rather_than_guessing(self):
        identity = self.identity(self.PLAIN)
        self.assertIn('backend', identity.unknowns)
        for name in ('weights', 'quantization', 'chat_template', 'class_args', 'config_args'):
            self.assertIn(name, identity.unknowns)
        self.assertEqual(identity.class_inputs['weights']['sha256'], [])
        self.assertEqual(identity.class_inputs['quantization'], UNKNOWN)

    def test_two_unmanifested_targets_do_not_merge_when_only_one_declares_a_fact(self):
        bare = self.identity(self.PLAIN)
        self.graph.backends['worker-c-fallback'] = {'id': 'worker-c-fallback', 'files': [{'sha256': '1' * 64}],
                                                    'quantization': 'Q4_K_M', 'class_args': [], 'config_args': []}
        declared_up = self.identity(self.PLAIN)
        self.assertNotEqual(bare.class_id, declared_up.class_id)
        self.assertNotEqual(bare.config_id, declared_up.config_id)

    def test_a_missing_route_fact_is_recorded_as_unknown(self):
        del self.graph.route['targets'][WORKER_A_TARGET]['max_tokens']
        identity = self.identity(WORKER_A_TARGET)
        self.assertEqual(identity.class_inputs['max_tokens'], UNKNOWN)
        self.assertIn('max_tokens', identity.unknowns)


class TensorFoldDeclarationTests(unittest.TestCase):
    """Any TensorFold launch file's declared lists must not drift from the manifest it serves."""

    def test_launch_files_and_manifests_agree_and_have_no_unknowns(self):
        graph = declared.load(SITE)
        for path in sorted((SITE / 'backends').glob('*.launch.json')):
            stem = path.name.removesuffix('.launch.json')
            targets = [name for name, spec in graph.route['targets'].items() if spec.get('backend') == stem]
            self.assertTrue(targets, stem)
            launch = json.loads(path.read_text())
            env = [launch[i + 1] for i, a in enumerate(launch) if a == '--env'
                   and launch[i + 1].startswith('TF_FLASHNEXT')]
            for target in targets:
                identity = compute_target(graph, target)
                self.assertEqual(identity.unknowns, {}, target)
                class_args = identity.class_inputs['class_args']
                config_args = identity.config_inputs['config_args']
                self.assertEqual(sorted(env), sorted(a for a in class_args if a.startswith('TF_FLASHNEXT')))
                self.assertFalse(set(class_args) & set(config_args))


class HardwareFingerprintTests(unittest.TestCase):
    FACTS = {'cpu': 'cortex-x925 x14', 'gpu': 'GB10', 'gpu_count': 1,
             'memory_total': '124619876352', 'driver': '580.95.05'}

    def test_complete_facts_fingerprint_and_are_stable(self):
        fp = hardware_fingerprint(self.FACTS)
        self.assertRegex(fp, HEX16)
        self.assertEqual(fp, hardware_fingerprint(dict(self.FACTS, unrelated='ignored')))

    def test_a_missing_fact_is_unknown_not_a_hash_of_nothing(self):
        for name in identities.HW_FP_FACTS:
            facts = dict(self.FACTS)
            del facts[name]
            self.assertEqual(hardware_fingerprint(facts), UNKNOWN, name)
        self.assertEqual(hardware_fingerprint({}), UNKNOWN)

    def test_different_hardware_fingerprints_differently(self):
        self.assertNotEqual(hardware_fingerprint(self.FACTS),
                            hardware_fingerprint(dict(self.FACTS, driver='580.82.07')))


class CommandTests(unittest.TestCase):
    def test_the_model_command_lists_every_target_with_both_ids(self):
        out = io.StringIO()
        with patch('sys.stdout', out):
            self.assertEqual(command.main(['model']), 0)
        lines = out.getvalue().splitlines()
        aliases = [line.removeprefix('  aliases: ') for line in lines if line.startswith('  aliases: ')]
        self.assertEqual({name for line in aliases for name in line.split(', ')},
                         set(declared.load().route['targets']))
        self.assertRegex(out.getvalue(), r'class [0-9a-f]{16}  config [0-9a-f]{16}')

    def test_one_target_in_json_carries_the_inputs_and_the_unknowns(self):
        graph = declared.load()
        target = next(iter(graph.route['targets']))
        out = io.StringIO()
        with patch('sys.stdout', out):
            command.main(['model', target, '--json'])
        record = json.loads(out.getvalue())
        self.assertEqual(record['target'], target)
        self.assertEqual(record['config_inputs']['node'],
                         declared.identity_name(graph.route['targets'][target]['node'], graph.nodes))
        self.assertEqual(set(record), {'target', 'node', 'class_id', 'config_id',
                                       'class_inputs', 'config_inputs', 'unknowns'})


if __name__ == '__main__':
    unittest.main()
