"""`ops upgrade plan|reuse` reads an upgrade from the declared files, and a preparation must say where
its weights come from.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import command, declared, errors, upgrade  # noqa: E402

IQ3 = [
    {'path': 'qwen3.8-flash-next-iq3_xxs-00001-of-00002.gguf', 'size': 40000000000, 'sha256': 'a' * 64},
    {'path': 'qwen3.8-flash-next-iq3_xxs-00002-of-00002.gguf', 'size': 34000000000, 'sha256': 'b' * 64},
]
GYRO = [
    {'path': 'gyro-Gyro-M/Qwen3.8-Flash-Next-Gyro-M-TQ2_0.gguf', 'size': 91965774272, 'sha256': 'c' * 64},
]
UD = [
    {'path': 'qwen3.8-flash-next-unsloth-ud-iq3_xxs.gguf', 'size': 70000000000, 'sha256': 'd' * 64},
]


def manifest(id, version, files, hosts, context=262144, extra=None):
    spec = {
        'id': id, 'family': 'qfn', 'model': 'Qwen/Qwen3.8-Flash-Coder', 'context': context,
        'source_version': version, 'source_revision': '0' * 40, 'source_ref': f'v{version}',
        'ggml_revision': '3cf03257f219' + '0' * 30, 'cuda_toolkit_major': 13, 'notes': 'fixture',
        'files': files, 'workers': {host: {'cuda_arch': '87'} for host in hosts},
    }
    if extra:
        spec.update(extra)
    return spec


# The other shape a backend manifest takes: Spark's TensorFold, and every engine that serves from an image or a
# container. No source_version, no ggml_revision, no flat `files`; the weights sit under `model.files`, and some of
# them name a size and no hash (backends/tf-1.1.4-qfn-mxfp8-nvfp4.json pins 32 files that way, 9 of them size-only).
NESTED = {
    'id': 'infernix-1.0-qfn-fp4-local', 'family': 'qfn', 'context': 262144,
    'recipe': {'repository': 'https://example.invalid/recipe.git', 'revision': 'a' * 40},
    'engine': {'repository': 'https://example.invalid/engine.git', 'revision': 'b' * 40, 'version': '1.0.0'},
    'image': {'tag': 'agent-infra-infernix:1.0.0-bbbb', 'id': 'sha256:' + 'c' * 64},
    'model': {'repository': 'someone/Qwen3.8-Flash-Next-FP4-Local', 'revision': 'd' * 40, 'directory': 'evaluations/fp4',
              'files': [{'path': 'shard-00001.safetensors', 'size': 40_000_000_000, 'sha256': 'e' * 64},
                        {'path': 'shard-00002.safetensors', 'size': 30_000_000_000},
                        {'path': 'config.json', 'size': 75217}]},
    'workers': {'workere': {'cuda_arch': '121'}},
}


class WeightEntriesTests(unittest.TestCase):
    """The manifest reads that decide whether a root holds its weights must see both manifest shapes."""

    def test_a_flat_manifest_and_a_nested_one_pin_weights_in_different_places(self):
        flat = manifest('strata-0.2.12-qfn-gsq-rco-iq3_xxs', '0.2.12', IQ3, ['workera'])
        self.assertEqual([entry['path'] for entry in upgrade.weight_entries(flat)],
                         ['qwen3.8-flash-next-iq3_xxs-00001-of-00002.gguf',
                          'qwen3.8-flash-next-iq3_xxs-00002-of-00002.gguf'])
        self.assertEqual([entry['path'] for entry in upgrade.weight_entries(NESTED)],
                         ['shard-00001.safetensors', 'shard-00002.safetensors', 'config.json'])
        self.assertEqual(upgrade.weight_bytes(NESTED), 70_000_075_217)

    def test_a_nested_manifest_is_not_read_as_pinning_nothing(self):
        # the reading this exists for: `ops upgrade reuse` answered 0.0 GiB for a manifest that pins 102 GB, and F6
        # had nothing to check because the flat key was empty
        self.assertEqual(len(upgrade.pinned(NESTED)), 3)
        self.assertEqual(upgrade.pinned(NESTED)['shard-00002.safetensors'], (30_000_000_000, None))
        self.assertEqual(upgrade.asks(NESTED)['shard-00001.safetensors'], (40_000_000_000, 'e' * 64))

    def test_a_size_without_a_hash_is_recorded_as_unknown_not_as_a_pin_or_a_refusal(self):
        self.assertEqual(upgrade.unhashed_weights(NESTED), ['shard-00002.safetensors', 'config.json'])
        self.assertEqual(upgrade.unhashed_weights(manifest('x', '1', IQ3, ['workera'])), [])
        # one side hash-pinned and the other not is a mismatch: only the hash proves the bytes are the same file
        source = {'files': [{'path': 'a', 'size': 10, 'sha256': 'e' * 64}]}
        target = {'model': {'files': [{'path': 'a', 'size': 10}]}}
        self.assertEqual(upgrade.supplies(source, target), ([], ['a']))

    def test_an_entry_that_is_not_a_pin_is_dropped_rather_than_crashing_the_read(self):
        manifest_ = {'files': [{'path': 'no-size'}, {'size': 1}, {'path': 'ok', 'size': 1}, 'not-a-dict']}
        self.assertEqual([entry['path'] for entry in upgrade.weight_entries(manifest_)], ['ok'])

    def test_the_measured_half_of_f6_asks_about_the_nested_entries_too(self):
        from agentiac import retire
        present = {'shard-00001.safetensors': True, 'shard-00002.safetensors': False, 'config.json': True}
        missing = retire.weights_missing(NESTED, present)
        self.assertEqual([entry['path'] for entry in missing], ['shard-00002.safetensors'])


class ManifestShapeTests(unittest.TestCase):
    def test_the_upgrade_reading_is_for_a_strata_shaped_manifest_and_says_so_for_the_other(self):
        flat = manifest('strata-0.2.12-qfn-gsq-rco-iq3_xxs', '0.2.12', IQ3, ['workera'])
        self.assertTrue(upgrade.strata_shaped(flat))
        self.assertFalse(upgrade.strata_shaped(NESTED))
        text = upgrade.not_strata_shaped('workere', 'infernix-1.0-qfn-fp4-local', NESTED)
        self.assertIn('backends/infernix-1.0-qfn-fp4-local.json is not the kind of manifest', text)
        self.assertIn('strata_trial_roots', text)
        self.assertIn('recipe, engine, image, model', text)
        self.assertIn('ops prepare workere --check', text)

    def test_a_manifest_of_neither_shape_still_names_what_it_declares(self):
        text = upgrade.not_strata_shaped('workere', 'odd-1', {'id': 'odd-1'})
        self.assertIn('none of recipe/engine/image/model either', text)



def write_synthetic_site(root):
    for rel in ('infra', 'profiles', 'backends', 'gateway', 'agents/pi', 'artifacts/lifecycle'):
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel in ('infra/retention.schema.json', 'profiles/schema.json', 'backends/schema.json'):
        (root / rel).write_text((ROOT / rel).read_text())

    inventory = {
        'all': {
            'vars': {
                'ansible_user': 'operator', 'project_root': '/srv/agentiac',
                'backend_port': 18080, 'ondemand_backend_port': 18090, 'idle_unload_minutes': 30,
                'nodejs_binary': '/usr/bin/node', 'rclone_config_dir': '/srv/agentiac/rclone',
                'backup_destination': 'remote:backups', 'gateway_port': 14000, 'chatgpt_port': 14001,
                'resource_prefix': 'agentiac',
            },
            'children': {'workers': {'hosts': {
                'workera': {
                    'ansible_host': 'workera.example', 'node_class': 'orin',
                    'ondemand_persistent': True, 'strata_backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                    'strata_trial_roots': {'0.2.10': '/srv/agentiac/strata-0.2.10',
                                           '0.2.12': '/srv/agentiac/strata-0.2.12'},
                },
                'workerb': {
                    'ansible_host': 'workerb.example', 'node_class': 'orin',
                    'ondemand_persistent': True, 'strata_backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                    'strata_trial_roots': {'0.2.10': '/srv/agentiac/strata-0.2.10',
                                           '0.2.12': '/srv/agentiac/strata-0.2.12'},
                },
                'workerc': {
                    'ansible_host': 'workerc.example', 'node_class': 'rtx5090', 'borrowed': True,
                    'backend': 'strata', 'strata_default_backend': 'strata-0.2.11-qfn-ud-iq3_xxs',
                    'strata_trial_roots': {
                        '0.2.8': '/srv/agentiac/strata-0.2.8', '0.2.11': '/srv/agentiac/strata-0.2.11',
                        'strata-0.2.11-qfn-gyro-m-tq2_0': '/srv/agentiac/strata-0.2.11-gyro',
                    },
                },
                'workerd': {
                    'ansible_host': 'workerd.example', 'node_class': 'rtx5090', 'borrowed': True,
                    'backend': 'strata', 'strata_default_backend': 'strata-0.2.8-qfn-gyro-m-tq2_0',
                    'strata_trial_roots': {
                        '0.2.8': '/mnt/data/agentiac-strata-0.2.8',
                        'strata-0.2.11-qfn-gyro-m-tq2_0': '/mnt/data/agentiac-strata-0.2.11-gyro',
                    },
                },
                'workere': {
                    'ansible_host': 'workere.example', 'node_class': 'gb10', 'borrowed': True,
                    'spark_tensorfold': True, 'strata_trial_roots': {'0.2.13': '/srv/agentiac/strata-0.2.13'},
                },
                'control': {
                    'ansible_connection': 'local', 'ansible_host': 'localhost', 'node_class': 'laptop',
                },
            }}},
        },
    }
    (root / 'infra/inventory.yml').write_text(yaml.safe_dump(inventory, sort_keys=False))

    backends = {
        'strata-0.2.10-qfn-gsq-rco-iq3_xxs': manifest('strata-0.2.10-qfn-gsq-rco-iq3_xxs', '0.2.10', IQ3, ['workera', 'workerb']),
        'strata-0.2.12-qfn-gsq-rco-iq3_xxs': manifest('strata-0.2.12-qfn-gsq-rco-iq3_xxs', '0.2.12', IQ3, ['workera', 'workerb']),
        'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2': manifest(
            'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2', '0.2.12', IQ3, ['workerb'],
            extra={
                'workers': {'workerb': {'cuda_arch': '87', 'drafter': 'dflash2'}},
                'drafter_pack': {
                    'drafter': 'dflash2',
                    'path': 'dflash2/qwen3.8-flash-next-dflash2.q4_0.pack',
                    'size': 522458112,
                    'sha256': 'f' * 64,
                    'source_repository': 'synth/Qwen3.8-Flash-Next-DFlash2',
                },
            }),
        'strata-0.2.8-qfn-gyro-m-tq2_0': manifest('strata-0.2.8-qfn-gyro-m-tq2_0', '0.2.8', GYRO, ['workerc', 'workerd'], context=131072),
        'strata-0.2.11-qfn-gyro-m-tq2_0': manifest('strata-0.2.11-qfn-gyro-m-tq2_0', '0.2.11', GYRO, ['workerc', 'workerd'], context=131072),
        'strata-0.2.11-qfn-ud-iq3_xxs': manifest('strata-0.2.11-qfn-ud-iq3_xxs', '0.2.11', UD, ['workerc']),
    }
    for stem, spec in backends.items():
        (root / f'backends/{stem}.json').write_text(json.dumps(spec))

    components = [
        {
            'id': 'strata-workera', 'kind': 'engine', 'owner': 'project', 'hosts': ['workera'],
            'source': {'type': 'github', 'repository': 'synth/Strata'},
            'pin': {'version': '0.2.12', 'revision': '0' * 40,
                    'lock': 'backends/strata-0.2.12-qfn-gsq-rco-iq3_xxs.json'},
            'policy': 'manual', 'gate': 'staged upgrade',
        },
        {
            'id': 'strata-workerb', 'kind': 'engine', 'owner': 'project', 'hosts': ['workerb'],
            'source': {'type': 'github', 'repository': 'synth/Strata'},
            'pin': {'version': '0.2.12', 'revision': '0' * 40,
                    'lock': 'backends/strata-0.2.12-qfn-gsq-rco-iq3_xxs.json'},
            'policy': 'manual', 'gate': 'staged upgrade',
        },
    ]
    (root / 'infra/components.yml').write_text(yaml.safe_dump(components, sort_keys=False))

    gateway_config = {
        'model_list': [
            {'model_name': 'workera-worker', 'litellm_params': {'model': 'openai/iq3', 'api_base': 'http://127.0.0.1:18101/v1'}},
            {'model_name': 'workerb-worker', 'litellm_params': {'model': 'openai/iq3', 'api_base': 'http://127.0.0.1:18102/v1'}},
            {'model_name': 'workerc-worker', 'litellm_params': {'model': 'openai/ud', 'api_base': 'http://127.0.0.1:18115/v1'}},
            {'model_name': 'workerd-worker', 'litellm_params': {'model': 'openai/gyro', 'api_base': 'http://127.0.0.1:18114/v1'}},
            {'model_name': 'cloud-alias', 'litellm_params': {'model': 'litellm_proxy/cloud', 'api_base': 'http://127.0.0.1:19999/v1'}},
        ]
    }
    (root / 'gateway/config.yaml').write_text(yaml.safe_dump(gateway_config, sort_keys=False))
    route_spec = {
        'version': 2,
        'alias': 'local-coder',
        'default': 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
        'targets': {
            'workera-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k': {
                'source_alias': 'workera-worker', 'node': 'workera',
                'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'context_window': 262144, 'max_tokens': 16384},
            'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k': {
                'source_alias': 'workerb-worker', 'node': 'workerb',
                'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'context_window': 262144, 'max_tokens': 16384},
            'workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
                'source_alias': 'workerd-worker', 'node': 'workerd',
                'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'context_window': 131072, 'max_tokens': 16384},
        },
        'legacy_targets': {}, 'legacy_sources': {},
    }
    (root / 'gateway/local-coder.json').write_text(json.dumps(route_spec, indent=2))
    (root / 'agents/pi/models.json').write_text(json.dumps({'providers': {'local-infra': {'models': [{'id': 'local-coder'}]}}})
                                                )
    (root / 'agents/pi/router.json').write_text(json.dumps({'low': 'local-coder', 'high': 'cloud-alias'}))

    retention = {
        'workera': {
            'serving': [{'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'path': '/srv/agentiac/strata-0.2.12',
                         'class': 'persistent', 'form': 'full', 'reason': 'serving'}],
            'rollback': [{'backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs', 'path': '/srv/agentiac/strata-0.2.10',
                          'class': 'persistent', 'form': 'full', 'reason': 'N-1'}],
            'candidate': [], 'quarantine': [], 'retired': [], 'staging': [], 'areas': [],
        },
        'workerb': {
            'serving': [{'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'path': '/srv/agentiac/strata-0.2.12',
                         'class': 'persistent', 'form': 'full', 'reason': 'serving'}],
            'rollback': [{'backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs', 'path': '/srv/agentiac/strata-0.2.10',
                          'class': 'persistent', 'form': 'full', 'reason': 'N-1'}],
            'candidate': [], 'quarantine': [], 'retired': [], 'staging': [], 'areas': [],
        },
        'workerc': {
            'serving': [{'backend': 'strata-0.2.11-qfn-ud-iq3_xxs', 'path': '/srv/agentiac/strata-0.2.11',
                         'class': 'persistent', 'form': 'full', 'reason': 'serving'}],
            'rollback': [{'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'path': '/srv/agentiac/strata-0.2.8',
                          'class': 'persistent', 'form': 'full', 'reason': 'N-1'}],
            'candidate': [], 'quarantine': [], 'retired': [], 'staging': [], 'areas': [],
        },
        'workerd': {
            'serving': [{'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'path': '/mnt/data/agentiac-strata-0.2.8',
                         'class': 'persistent', 'form': 'full', 'reason': 'serving'}],
            'rollback': [], 'candidate': [], 'quarantine': [], 'retired': [], 'staging': [], 'areas': [],
        },
        'workere': {'serving': [], 'rollback': [], 'candidate': [], 'quarantine': [], 'retired': [], 'staging': [], 'areas': []},
        'control': {'serving': [], 'rollback': [], 'candidate': [], 'quarantine': [], 'retired': [], 'staging': [], 'areas': []},
    }
    (root / 'infra/retention.yml').write_text(yaml.safe_dump(retention, sort_keys=False))
    return root


class SyntheticSiteTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = write_synthetic_site(Path(directory.name))
        self.graph = declared.load(self.root)


class ShippedDeclarations(SyntheticSiteTestCase):
    def test_iq3xxs_0_2_12_reuses_0_2_10_on_both_dedicated_workers(self):
        for host, sources in (
            ('workera', [('strata-0.2.10-qfn-gsq-rco-iq3_xxs', 'rollback', '/srv/agentiac/strata-0.2.10')]),
            ('workerb', [('strata-0.2.10-qfn-gsq-rco-iq3_xxs', 'rollback', '/srv/agentiac/strata-0.2.10')]),
        ):
            found = upgrade.reuse_candidates(self.graph, host,
                                             upgrade.manifest_for(self.graph, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'))
            self.assertEqual([(entry['backend'], entry['state'], entry['root']) for entry in found['candidates']],
                             sources, f'{host} names the roots that hold these bytes')
            for entry in found['candidates']:
                self.assertEqual((entry['files'], entry['bytes']),
                                 (2, upgrade.weight_bytes(upgrade.manifest_for(self.graph,
                                                                               'strata-0.2.12-qfn-gsq-rco-iq3_xxs'))))
            self.assertEqual(found['download_bytes'], found['candidates'][0]['bytes'])

    def test_gyro_m_0_2_11_reuses_0_2_8_across_workers(self):
        backend = upgrade.manifest_for(self.graph, 'strata-0.2.11-qfn-gyro-m-tq2_0')
        held = {'workerc': '/srv/agentiac/strata-0.2.8', 'workerd': '/mnt/data/agentiac-strata-0.2.8'}
        for host, root in held.items():
            found = upgrade.reuse_candidates(self.graph, host, backend)
            self.assertEqual([entry['backend'] for entry in found['candidates']], ['strata-0.2.8-qfn-gyro-m-tq2_0'], host)
            self.assertEqual(found['candidates'][0]['root'], root)
            self.assertEqual(found['candidates'][0]['files'], 1)

    def test_the_two_weight_sets_are_not_interchangeable(self):
        gyro = upgrade.manifest_for(self.graph, 'strata-0.2.11-qfn-gyro-m-tq2_0')
        iq3 = upgrade.manifest_for(self.graph, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs')
        self.assertEqual(upgrade.reuse_candidates(self.graph, 'workera', gyro)['candidates'], [])
        self.assertEqual(upgrade.reuse_candidates(self.graph, 'workerc', iq3)['candidates'], [])
        self.assertNotEqual(upgrade.pinned(gyro), upgrade.pinned(iq3))
        self.assertEqual(upgrade.supplies(upgrade.manifest_for(self.graph, 'strata-0.2.10-qfn-gsq-rco-iq3_xxs'), iq3), ([], []))
        self.assertEqual(upgrade.supplies(upgrade.manifest_for(self.graph, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'), iq3), ([], []))

    def test_plan_of_the_declared_upgrade_has_no_blockers(self):
        for host in ('workera', 'workerb'):
            reading = upgrade.plan(self.graph, host, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs')
            self.assertEqual(reading['blockers'], [], reading)
            self.assertEqual(reading['root'], {'key': '0.2.12', 'path': '/srv/agentiac/strata-0.2.12'})
            self.assertEqual([step['step'] for step in reading['steps']],
                             ['declare the backend for this host', 'declare the root', 'declare what the host holds',
                              'read the host before preparing', 'prepare', 'measure before applying', 'move the pin',
                              'move what a claim resolves to', 'move the ledger', 'deploy and claim', 'verify'])
            prepared = [step['step'] for step in reading['steps'] if step['state'] == 'done']
            self.assertEqual(prepared, ['declare the backend for this host', 'declare the root',
                                        'declare what the host holds', 'move what a claim resolves to'])
            self.assertIn('--reuse-declared', [step['what'] for step in reading['steps'] if step['step'] == 'prepare'][0])

    def test_plan_names_the_host_read_it_does_not_run(self):
        cli = errors.command()
        reading = upgrade.plan(self.graph, 'workerb', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs')
        steps = {step['step']: step['what'] for step in reading['steps']}
        self.assertIn('ops storage workerb --candidates', steps['read the host before preparing'])
        self.assertIn('--check', steps['read the host before preparing'])
        self.assertIn(f'{cli} warm workerb', steps['verify'])
        self.assertIn(f'{cli} claim workerb', steps['measure before applying'])
        self.assertIn('no --route', steps['measure before applying'])

    def test_plan_of_an_undeclared_upgrade_refuses_in_three_places(self):
        reading = upgrade.plan(self.graph, 'workerb', 'strata-0.2.8-qfn-gyro-m-tq2_0')
        self.assertEqual(len(reading['blockers']), 3, reading['blockers'])
        self.assertTrue(any('does not name workerb in `workers`' in blocker for blocker in reading['blockers']))
        self.assertTrue(any('strata_trial_roots' in blocker for blocker in reading['blockers']))
        self.assertTrue(any('candidate:' in blocker for blocker in reading['blockers']))
        self.assertEqual(reading['reuse']['candidates'], [])

    def test_current_pin_comes_from_the_component_that_names_the_host(self):
        for host, component in (('workera', 'strata-workera'), ('workerb', 'strata-workerb')):
            self.assertEqual(upgrade.pinned_by(self.graph, host)['id'], component)

    def test_measuring_names_the_alias_the_route_table_publishes(self):
        step = lambda host: [entry['what'] for entry in upgrade.plan(self.graph, host, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs')['steps']
                             if entry['step'] == 'measure before applying'][0]
        for host, target in (('workera', 'workera-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k'),
                             ('workerb', 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k')):
            self.assertIn(f'local-coder-{target}', step(host), host)
        self.assertIn('declare one', upgrade.measure_alias(self.graph, 'workerc', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'))


class PreparedGate(SyntheticSiteTestCase):
    def gate(self, host, backend, reuse_root=None, reuse_declared=False, download=False, graph=None):
        return upgrade.prepare_gate(graph or self.graph, host, backend, reuse_root, reuse_declared, download)[3]

    def refusal(self, **kwargs):
        with self.assertRaises(SystemExit) as caught:
            self.gate(**kwargs)
        return str(caught.exception)

    def with_a_second_full_root(self):
        path = self.root / 'infra/retention.yml'
        ledger = yaml.safe_load(path.read_text())
        ledger['workerb']['candidate'].append({
            'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2',
            'path': '/srv/agentiac/strata-0.2.12-dflash2',
            'class': 'persistent', 'form': 'full',
            'reason': 'a second full root with the same pinned pair',
        })
        path.write_text(yaml.safe_dump(ledger))
        return declared.load(self.root)

    def test_named_root_is_checked_and_used(self):
        answer = self.gate(host='workera', backend='strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                           reuse_root='/srv/agentiac/strata-0.2.10')
        self.assertEqual(answer, ('reuse', '/srv/agentiac/strata-0.2.10', 'strata-0.2.10-qfn-gsq-rco-iq3_xxs'))

    def test_declared_root_is_chosen_when_there_is_exactly_one(self):
        self.assertEqual(self.gate(host='workera', backend='strata-0.2.12-qfn-gsq-rco-iq3_xxs', reuse_declared=True),
                         ('reuse', '/srv/agentiac/strata-0.2.10', 'strata-0.2.10-qfn-gsq-rco-iq3_xxs'))

    def test_two_roots_with_the_same_bytes_are_refused_rather_than_guessed(self):
        graph = self.with_a_second_full_root()
        text = self.refusal(host='workerb', backend='strata-0.2.12-qfn-gsq-rco-iq3_xxs', reuse_declared=True, graph=graph)
        self.assertIn('more than one full root with these bytes', text)
        self.assertIn('/srv/agentiac/strata-0.2.10', text)
        self.assertIn('/srv/agentiac/strata-0.2.12-dflash2', text)
        self.assertIn('--reuse-root', text)
        self.assertEqual(self.gate(host='workerb', backend='strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                                   reuse_root='/srv/agentiac/strata-0.2.12-dflash2', graph=graph),
                         ('reuse', '/srv/agentiac/strata-0.2.12-dflash2',
                          'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2'))
        self.assertEqual(self.gate(host='workerb', backend='strata-0.2.12-qfn-gsq-rco-iq3_xxs', reuse_declared=True),
                         ('reuse', '/srv/agentiac/strata-0.2.10', 'strata-0.2.10-qfn-gsq-rco-iq3_xxs'))

    def test_holding_the_bytes_without_saying_so_is_a_refusal_not_a_download(self):
        text = self.refusal(host='workerb', backend='strata-0.2.12-qfn-gsq-rco-iq3_xxs')
        self.assertIn('already holds what strata-0.2.12-qfn-gsq-rco-iq3_xxs pins', text)
        self.assertIn('/srv/agentiac/strata-0.2.10', text)
        self.assertIn('downloads', text)
        self.assertIn('--reuse-declared', text)

    def test_download_is_a_decision_not_a_default(self):
        bytes_ = upgrade.weight_bytes(upgrade.manifest_for(self.graph, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'))
        self.assertEqual(self.gate(host='workerb', backend='strata-0.2.12-qfn-gsq-rco-iq3_xxs', download=True),
                         ('download', bytes_))
        (self.root / 'backends/orphan-gyro-fixture.json').write_text(json.dumps(
            manifest('orphan-gyro-fixture', '9.9.9', GYRO, ['workerb'])))
        self.add_root('workerb', '9.9.9', '/srv/agentiac/strata-gyro-fixture')
        gyro_bytes = sum(entry['size'] for entry in GYRO)
        self.assertEqual(self.gate(host='workerb', backend='orphan-gyro-fixture', download=True),
                         ('download', gyro_bytes))
        self.assertEqual(self.gate(host='workerb', backend='orphan-gyro-fixture'), ('download', gyro_bytes))

    def test_a_manifest_that_pins_no_weights_decides_nothing(self):
        (self.root / 'backends/tensorfold-fixture.json').write_text(json.dumps(
            manifest('tensorfold-fixture', 'v114', [], ['workere'])))
        graph = declared.load(self.root)
        self.assertEqual(self.gate(host='workere', backend='tensorfold-fixture', graph=graph), ('skip', None))

    def test_a_host_the_manifest_does_not_name_is_refused(self):
        text = self.refusal(host='workerb', backend='strata-0.2.11-qfn-gyro-m-tq2_0')
        self.assertIn('does not name workerb in `workers`', text)

    def test_a_manifest_with_no_declared_root_is_refused_with_the_key_it_wants(self):
        (self.root / 'backends/orphan-fixture.json').write_text(json.dumps(
            manifest('orphan-fixture', '9.9.9', IQ3, ['workerb'])))
        graph = declared.load(self.root)
        text = self.refusal(host='workerb', backend='orphan-fixture', graph=graph)
        self.assertIn('has no declared root on workerb', text)
        self.assertIn('9.9.9', text)

    def test_a_root_the_declaration_does_not_know_is_refused(self):
        text = self.refusal(host='workerb', backend='strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                            reuse_root='/srv/agentiac/strata-0.2.8')
        self.assertIn('not a root this declaration knows on workerb', text)
        self.assertIn('/srv/agentiac/strata-0.2.10', text)

    def test_bytes_that_are_not_the_pinned_bytes_are_refused_by_name(self):
        other = json.loads(json.dumps(upgrade.manifest_for(self.graph, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs')))
        other['id'] = 'strata-qwen38-iq3xxs-0.2.13'
        other['source_version'] = '0.2.13'
        other['source_ref'] = 'v0.2.13'
        changed_path = other['files'][0]['path']
        other['files'] = [dict(other['files'][0], sha256='f' * 64)] + other['files'][1:]
        (self.root / 'backends/strata-qwen38-iq3xxs-0.2.13.json').write_text(json.dumps(other))
        self.add_root('workerb', '0.2.13', '/srv/agentiac/strata-0.2.13')
        text = self.refusal(host='workerb', backend='strata-qwen38-iq3xxs-0.2.13',
                            reuse_root='/srv/agentiac/strata-0.2.10')
        self.assertIn('does not hold what strata-qwen38-iq3xxs-0.2.13 asks for', text)
        self.assertIn(f'there at a different size or sha256: {changed_path}', text)
        self.assertIn('not there at all: none', text)

    def add_root(self, host, key, path):
        path_to_inventory = self.root / 'infra/inventory.yml'
        data = yaml.safe_load(path_to_inventory.read_text())
        hosts = data['all']['children']['workers']['hosts']
        hosts[host].setdefault('strata_trial_roots', {})[key] = path
        path_to_inventory.write_text(yaml.safe_dump(data))
        self.graph = declared.load(self.root)

    def test_more_than_one_source_is_a_refusal_that_names_both(self):
        self.edit_retention(lambda entry: entry.update({'backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs',
                                                        'path': '/srv/agentiac/strata-0.2.10'}),
                            state='candidate')
        text = self.refusal(host='workerb', backend='strata-0.2.12-qfn-gsq-rco-iq3_xxs', reuse_declared=True)
        self.assertIn('more than one full root with these bytes', text)
        self.assertIn('/srv/agentiac/strata-0.2.10', text)

    def edit_retention(self, mutate, state='rollback'):
        path = self.root / 'infra/retention.yml'
        ledger = yaml.safe_load(path.read_text())
        held = ledger['workerb'].setdefault(state, [])
        held.append(dict(held[0]) if held else {'backend': 'x', 'path': '/x', 'form': 'full', 'why': 'fixture'})
        mutate(held[-1])
        path.write_text(yaml.safe_dump(ledger))
        self.graph = declared.load(self.root)


class ThinRootsAreNotSources(SyntheticSiteTestCase):
    def mark_thin(self, host, state):
        path = self.root / 'infra/retention.yml'
        ledger = yaml.safe_load(path.read_text())
        ledger[host][state][0]['form'] = 'thin'
        ledger[host][state][0]['confirmed'] = '1970-01-01'
        path.write_text(yaml.safe_dump(ledger))
        return declared.load(self.root)

    def test_a_thin_root_is_not_offered_and_says_why(self):
        graph = self.mark_thin('workerb', 'rollback')
        found = upgrade.reuse_candidates(graph, 'workerb', upgrade.manifest_for(graph, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'))
        self.assertEqual([entry['backend'] for entry in found['candidates']], [], found)
        self.assertIn('form: thin', found['why_not'][0])
        self.assertIn('holds no weights', found['why_not'][0])

    def test_a_thin_root_named_as_reuse_is_refused_with_the_way_back(self):
        graph = self.mark_thin('workera', 'rollback')
        with self.assertRaises(SystemExit) as caught:
            upgrade.prepare_gate(graph, 'workera', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                                 reuse_root='/srv/agentiac/strata-0.2.10')
        text = str(caught.exception)
        self.assertIn('form: thin', text)
        self.assertIn('ops restore workera --backend strata-0.2.10-qfn-gsq-rco-iq3_xxs', text)

    def test_a_thin_incumbent_is_said_not_to_be_a_rollback_it_cannot_offer(self):
        graph = self.mark_thin('workerb', 'serving')
        reading = upgrade.plan(graph, 'workerb', 'strata-0.2.11-qfn-gyro-m-tq2_0')
        self.assertTrue(any('ops restore workerb --backend strata-0.2.12-qfn-gsq-rco-iq3_xxs' in note
                            for note in reading['notes']), reading['notes'])
        self.assertEqual(reading['reuse']['candidates'], [])


class LedgerLinesAreTheOnlySources(SyntheticSiteTestCase):
    def test_a_manifest_the_ledger_never_names_is_not_a_source(self):
        (self.root / 'backends/unheld-fixture.json').write_text(json.dumps(
            manifest('unheld-fixture', '0.2.9', IQ3, ['workerb'])))
        graph = declared.load(self.root)
        found = upgrade.reuse_candidates(graph, 'workerb', upgrade.manifest_for(graph, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'))
        self.assertNotIn('unheld-fixture', [entry['backend'] for entry in found['candidates']])
        self.assertNotIn('strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2', [entry['backend'] for entry in found['candidates']])
        self.assertEqual([entry['backend'] for entry in found['candidates']], ['strata-0.2.10-qfn-gsq-rco-iq3_xxs'])

    def test_a_ledger_line_whose_manifest_is_gone_is_said_not_silently_skipped(self):
        path = self.root / 'infra/retention.yml'
        ledger = yaml.safe_load(path.read_text())
        ledger['workerb']['candidate'].append({'backend': 'strata-qwen38-gone-0.9.9',
                                              'path': '/srv/agentiac/strata-gone', 'form': 'full',
                                              'why': 'a line whose manifest this checkout does not hold'})
        path.write_text(yaml.safe_dump(ledger))
        graph = declared.load(self.root)
        found = upgrade.reuse_candidates(graph, 'workerb', upgrade.manifest_for(graph, 'strata-0.2.10-qfn-gsq-rco-iq3_xxs'))
        self.assertTrue(any('the manifest is not in backends/' in reason for reason in found['why_not']),
                        found['why_not'])

    def test_the_manifest_being_prepared_is_never_its_own_source(self):
        for host in ('workera', 'workerb'):
            for backend in ('strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'strata-0.2.10-qfn-gsq-rco-iq3_xxs'):
                found = upgrade.reuse_candidates(self.graph, host, upgrade.manifest_for(self.graph, backend))
                self.assertNotIn(backend, [entry['backend'] for entry in found['candidates']])


class CommandTests(SyntheticSiteTestCase):
    def upgrade_cli(self, *argv):
        env = {**os.environ, 'AGENTIAC_ROOT': str(self.root)}
        return subprocess.run([sys.executable, str(ROOT / 'scripts/upgrade.py'), *argv],
                              capture_output=True, text=True, cwd=ROOT, env=env)

    def test_plan_exits_one_when_the_upgrade_is_not_declared(self):
        self.assertEqual(self.upgrade_cli('plan', 'workerb', '--backend', 'strata-0.2.8-qfn-gyro-m-tq2_0').returncode, 1)
        self.assertEqual(self.upgrade_cli('plan', 'workerb', '--backend', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs').returncode, 0)

    def test_an_undeclared_backend_exits_two_and_says_where_to_look(self):
        result = self.upgrade_cli('plan', 'workerb', '--backend', 'no-such-backend')
        self.assertEqual(result.returncode, 2)
        self.assertIn('no backend manifest names no-such-backend', result.stderr)

    def test_plan_and_reuse_refuse_a_nested_manifest_instead_of_answering_zero(self):
        # what this replaces: `plan` died in its formatter with a None engine revision, and `reuse` reported
        # "download 0.0 GiB" for a manifest pinning 70 GB in three files
        (self.root / 'backends/infernix-1.0-qfn-fp4-local.json').write_text(json.dumps(NESTED, indent=2))
        for action in ('plan', 'reuse'):
            result = self.upgrade_cli(action, 'workere', '--backend', 'infernix-1.0-qfn-fp4-local')
            self.assertEqual(result.returncode, 1, f'{action}: {result.stderr}')
            self.assertIn('is not the kind of manifest', result.stderr)
            self.assertNotIn('0.0 GiB', result.stdout)

    def test_reuse_json_names_the_root_and_the_cost_of_not_using_it(self):
        result = self.upgrade_cli('reuse', 'workera', '--backend', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document['candidates'][0]['root'], '/srv/agentiac/strata-0.2.10')
        self.assertEqual(document['download_bytes'], document['candidates'][0]['bytes'])

    def test_plan_json_is_the_same_reading_as_the_lines(self):
        document = json.loads(self.upgrade_cli('plan', 'workera', '--backend', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                                               '--json').stdout)
        self.assertEqual(document['blockers'], [])
        self.assertEqual(document['root']['path'], '/srv/agentiac/strata-0.2.12')
        self.assertEqual(document['weights']['files'], 2)

    def prepare_args(self, host, backend, **flags):
        return argparse.Namespace(action='prepare', host=host, backend=backend, reuse_root=flags.get('reuse_root'),
                                  reuse_declared=flags.get('reuse_declared', False),
                                  download=flags.get('download', False), check=flags.get('check', False),
                                  isolated=flags.get('isolated', False))

    def test_prepare_carries_the_declared_source_into_the_playbook(self):
        with mock.patch.object(command, 'ROOT', self.root), self.assertRaises(SystemExit) as caught:
            command.ops(self.prepare_args('workerb', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'))
        self.assertIn('already holds what strata-0.2.12-qfn-gsq-rco-iq3_xxs pins', str(caught.exception))

    def test_check_is_not_a_preparation_and_decides_nothing(self):
        with mock.patch.object(command, 'ROOT', self.root):
            argv = command.ops(self.prepare_args('workerb', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', check=True))
        self.assertIn('--check', argv)
        self.assertFalse(any(entry.startswith('reuse_root=') for entry in argv))

    def test_reuse_declared_reaches_the_playbook_as_the_root(self):
        with mock.patch.object(command, 'ROOT', self.root):
            argv = command.ops(self.prepare_args('workera', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', reuse_declared=True,
                                                 isolated=True))
        self.assertIn('-e', argv)
        self.assertIn('reuse_root=/srv/agentiac/strata-0.2.10', argv)
        self.assertIn('isolated=true', argv)

    def test_download_declared_reaches_the_playbook_without_a_root(self):
        with mock.patch.object(command, 'ROOT', self.root):
            argv = command.ops(self.prepare_args('workerb', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', download=True))
        self.assertFalse(any(entry.startswith('reuse_root=') for entry in argv))
        self.assertIn('backend_id=strata-0.2.12-qfn-gsq-rco-iq3_xxs', argv)

    def test_upgrade_dispatches_to_the_reading(self):
        with mock.patch.object(command, 'ROOT', self.root):
            argv = command.ops(argparse.Namespace(action='upgrade', upgrade_action='reuse', host='workera',
                                                  backend='strata-0.2.12-qfn-gsq-rco-iq3_xxs', json=True))
        self.assertEqual(argv[-4:], ['workera', '--backend', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', '--json'])
        self.assertTrue(argv[1].endswith('scripts/upgrade.py'), argv)


class NoWrites(SyntheticSiteTestCase):
    def test_plan_and_reuse_leave_the_checkout_as_they_found_it(self):
        before = subprocess.run(['git', 'status', '--porcelain', '--short'], capture_output=True, text=True,
                                cwd=ROOT).stdout
        for argv in (['plan', 'workera', '--backend', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', '--json'],
                     ['reuse', 'workerc', '--backend', 'strata-0.2.11-qfn-gyro-m-tq2_0'],
                     ['plan', 'workerb', '--backend', 'strata-0.2.8-qfn-gyro-m-tq2_0']):
            self.invoke(argv)
        after = subprocess.run(['git', 'status', '--porcelain', '--short'], capture_output=True, text=True,
                               cwd=ROOT).stdout
        self.assertEqual(after, before)

    def invoke(self, argv):
        env = {**os.environ, 'AGENTIAC_ROOT': str(self.root)}
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/upgrade.py'), *argv],
                                capture_output=True, text=True, cwd=ROOT, env=env)
        self.assertIn(result.returncode, (0, 1), f'{argv}: {result.stderr}')
        return result


class DrafterTrial(SyntheticSiteTestCase):
    def setUp(self):
        super().setUp()
        self.trial = upgrade.manifest_for(self.graph, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2')
        self.plain = upgrade.manifest_for(self.graph, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs')

    def test_the_trial_is_the_same_engine_and_the_same_bytes_as_what_workerb_serves(self):
        for field in ('source_version', 'source_revision', 'ggml_revision', 'cuda_toolkit_major', 'context'):
            self.assertEqual(self.trial[field], self.plain[field], field)
        self.assertEqual(upgrade.pinned(self.trial), upgrade.pinned(self.plain))

    def test_the_drafter_is_declared_for_one_host_and_not_for_the_other(self):
        self.assertEqual(upgrade.drafter_asked(self.trial, 'workerb'), 'dflash2')
        self.assertIsNone(upgrade.drafter_asked(self.trial, 'workera'))
        for host in ('workera', 'workerb'):
            self.assertIsNone(upgrade.drafter_asked(self.plain, host))
            self.assertIsNone(upgrade.drafter_pack(self.plain, host))

    def test_the_pack_is_pinned_with_the_engine_s_own_numbers(self):
        pack = upgrade.drafter_pack(self.trial, 'workerb')
        self.assertEqual(pack, {'drafter': 'dflash2', 'path': 'dflash2/qwen3.8-flash-next-dflash2.q4_0.pack',
                                'size': 522458112,
                                'sha256': 'f' * 64})
        self.assertEqual(self.trial['drafter_pack']['source_repository'], 'synth/Qwen3.8-Flash-Next-DFlash2')

    def test_the_reading_names_both_weight_roots_and_the_one_download(self):
        found = upgrade.reuse_candidates(self.graph, 'workerb', self.trial)
        self.assertEqual([entry['root'] for entry in found['candidates']],
                         ['/srv/agentiac/strata-0.2.10', '/srv/agentiac/strata-0.2.12'])
        self.assertEqual(found['packs'], [upgrade.drafter_pack(self.trial, 'workerb')])
        self.assertEqual(found['download_bytes'], upgrade.weight_bytes(self.trial))

    def test_a_retired_trial_is_a_blocked_plan_that_still_names_what_the_worker_fetched(self):
        reading = upgrade.plan(self.graph, 'workerb', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2')
        self.assertEqual([b.split(':')[0] for b in reading['blockers']],
                         ['/srv/agentiac/strata-0.2.12 is what workerb holds as '
                          'strata-0.2.12-qfn-gsq-rco-iq3_xxs (infra/retention.yml serving line)',
                          'infra/retention.yml workerb names nothing for '
                          'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2'], reading['blockers'])
        self.assertIn('fell back to the engine version key', reading['blockers'][0])
        self.assertEqual(reading['root']['path'], '/srv/agentiac/strata-0.2.12')
        self.assertTrue(any(note.startswith('the drafter workerb is asked to run is dflash2')
                            for note in reading['notes']), reading['notes'])

    def test_preparing_a_retired_backend_into_another_backends_root_is_a_refusal(self):
        with self.assertRaises(SystemExit) as caught:
            upgrade.prepare_gate(self.graph, 'workerb', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2',
                                 '/srv/agentiac/strata-0.2.10')
        text = str(caught.exception)
        self.assertIn('would be prepared into /srv/agentiac/strata-0.2.12, which workerb holds as '
                      'strata-0.2.12-qfn-gsq-rco-iq3_xxs', text)
        self.assertIn('fell back to the engine version key "0.2.12"', text)
        self.assertIn('infra/retention.yml workerb `serving:` names that path for strata-0.2.12-qfn-gsq-rco-iq3_xxs', text)
        self.assertIn('a retired manifest is the record of its pins, not a preparation target', text)
        self.assertEqual(upgrade.prepare_gate(self.graph, 'workera', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                                              '/srv/agentiac/strata-0.2.10')[3],
                         ('reuse', '/srv/agentiac/strata-0.2.10', 'strata-0.2.10-qfn-gsq-rco-iq3_xxs'))

    def test_a_drafter_with_no_pinned_pack_is_a_blocker(self):
        manifest_data = json.loads((self.root / 'backends/strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2.json').read_text())
        del manifest_data['drafter_pack']
        (self.root / 'backends/strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2.json').write_text(json.dumps(manifest_data))
        reading = upgrade.plan(declared.load(self.root), 'workerb', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2')
        self.assertTrue(any('names no `drafter_pack`' in blocker for blocker in reading['blockers']),
                        reading['blockers'])


class DraftUpgradeTests(SyntheticSiteTestCase):
    SETUP_UNCHANGED = (
        'HF_REVISIONS = {\n'
        '    "synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF": "' + '0' * 40 + '",\n'
        '}\n'
        'LLAMA_CPP_COMMIT = "' + '1' * 40 + '"\n'
        'FILES = {\n'
        '    "qwen3.8-flash-next-iq3_xxs-00001-of-00002.gguf":\n'
        '        (40000000000, "' + 'a' * 64 + '"),\n'
        '    "qwen3.8-flash-next-iq3_xxs-00002-of-00002.gguf":\n'
        '        (34000000000, "' + 'b' * 64 + '"),\n'
        '}\n'
    )
    BUILD_REQS = '# comment\ncmake==4.4.3\nninja==1.13.2\n'

    def _add_repository_fields(self):
        model_repos = {
            'strata-0.2.10-qfn-gsq-rco-iq3_xxs': 'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF',
            'strata-0.2.12-qfn-gsq-rco-iq3_xxs': 'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF',
            'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2': 'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF',
            'strata-0.2.8-qfn-gyro-m-tq2_0': 'synth/Qwen3.8-Flash-Next-Gyro-GGUF',
            'strata-0.2.11-qfn-gyro-m-tq2_0': 'synth/Qwen3.8-Flash-Next-Gyro-GGUF',
            'strata-0.2.11-qfn-ud-iq3_xxs': 'synth/Qwen3.8-Flash-Next-GGUF',
        }
        for stem, repo in model_repos.items():
            p = self.root / f'backends/{stem}.json'
            data = json.loads(p.read_text())
            data['repository'] = 'https://github.com/synth/Strata'
            data['model_repository'] = repo
            if 'gyro' in stem:
                data['model'] = 'Gyro-M'
            p.write_text(json.dumps(data, indent=2))
        ret_path = self.root / 'infra/retention.yml'
        ret = yaml.safe_load(ret_path.read_text())
        for host in ('workera', 'workerb', 'workerc', 'workere'):
            ret[host]['areas'] = [{'path': '/srv/agentiac', 'class': 'persistent', 'reason': 'roots'}]
        ret['workerd']['areas'] = [{'path': '/mnt/data', 'class': 'persistent', 'reason': 'roots'}]
        ret_path.write_text(yaml.safe_dump(ret, sort_keys=False))
        self.graph = declared.load(self.root)

    def test_parse_strata_setup_handles_unchanged_and_changed_weights(self):
        parsed = upgrade.parse_strata_setup(self.SETUP_UNCHANGED, 'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF', IQ3, self.BUILD_REQS)
        self.assertTrue(parsed['weights_unchanged'])
        self.assertEqual(parsed['ggml_revision'], '1' * 40)
        self.assertEqual(parsed['pip_build_tools'], ['cmake==4.4.3', 'ninja==1.13.2'])
        self.assertEqual(parsed['files'][0]['same_as'], IQ3[0]['path'])

        changed_setup = self.SETUP_UNCHANGED.replace('"' + 'a' * 64 + '"', '"' + 'e' * 64 + '"')
        changed = upgrade.parse_strata_setup(changed_setup, 'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF', IQ3)
        self.assertFalse(changed['weights_unchanged'])
        self.assertNotIn('same_as', changed['files'][0])
        self.assertIn('same_as', changed['files'][1])

    def test_draft_dry_run_writes_nothing_and_apply_creates_valid_declarations(self):
        self._add_repository_fields()
        inv_path = self.root / 'infra/inventory.yml'
        ret_path = self.root / 'infra/retention.yml'
        inv_path.write_text('# inventory comment\n' + inv_path.read_text())
        ret_path.write_text('# retention comment\n' + ret_path.read_text())
        self.graph = declared.load(self.root)

        setup_dir = self.root / 'upstream/terminal'
        setup_dir.mkdir(parents=True)
        (setup_dir / 'setup.py').write_text(self.SETUP_UNCHANGED)
        (setup_dir / 'requirements-build.txt').write_text(self.BUILD_REQS)

        draft = upgrade.draft_upgrade(
            self.graph, 'workerb', '0.2.19', revision='2' * 40,
            setup_file=setup_dir / 'setup.py', build_reqs=setup_dir / 'requirements-build.txt',
        )
        self.assertEqual(draft['backend'], 'strata-0.2.19-qfn-gsq-rco-iq3_xxs')
        self.assertEqual(draft['layout'], 'terminal')
        self.assertTrue(draft['weights_unchanged'])
        self.assertFalse((self.root / 'backends/strata-0.2.19-qfn-gsq-rco-iq3_xxs.json').exists())

        written = upgrade.apply_draft(self.root, draft)
        self.assertEqual(len(written), 4)
        self.assertIn('# inventory comment', inv_path.read_text())
        self.assertIn('# retention comment', ret_path.read_text())

        updated = declared.load(self.root)
        self.assertEqual(declared.references(updated), [])
        self.assertEqual(updated.route['default'], 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k')

        reading = upgrade.plan(updated, 'workerb', 'strata-0.2.19-qfn-gsq-rco-iq3_xxs')
        self.assertEqual(reading['blockers'], [])
        prep_cmd = [s['what'] for s in reading['steps'] if s['step'] == 'prepare'][0]
        self.assertIn('--reuse-root /srv/agentiac/strata-0.2.12', prep_cmd)
        self.assertNotIn('--reuse-declared', prep_cmd)

        with self.assertRaises(SystemExit):
            upgrade.apply_draft(self.root, draft)

    def test_apply_draft_rolls_back_all_files_on_validation_error(self):
        self._add_repository_fields()
        setup_dir = self.root / 'upstream/terminal'
        setup_dir.mkdir(parents=True)
        (setup_dir / 'setup.py').write_text(self.SETUP_UNCHANGED)
        draft = upgrade.draft_upgrade(
            self.graph, 'workerb', '0.2.19', revision='2' * 40, setup_file=setup_dir / 'setup.py',
        )
        draft['target_spec']['source_alias'] = 'missing-alias'
        before_inv = (self.root / 'infra/inventory.yml').read_text()
        before_ret = (self.root / 'infra/retention.yml').read_text()
        before_route = (self.root / 'gateway/local-coder.json').read_text()
        with self.assertRaises(SystemExit):
            upgrade.apply_draft(self.root, draft)
        self.assertFalse((self.root / 'backends/strata-0.2.19-qfn-gsq-rco-iq3_xxs.json').exists())
        self.assertEqual((self.root / 'infra/inventory.yml').read_text(), before_inv)
        self.assertEqual((self.root / 'infra/retention.yml').read_text(), before_ret)
        self.assertEqual((self.root / 'gateway/local-coder.json').read_text(), before_route)


if __name__ == '__main__':
    unittest.main()
