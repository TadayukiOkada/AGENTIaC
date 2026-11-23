"""infra/retention.yml is the generation ledger, and `make check` fails when it disagrees with the declaration."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import declared, paths, retention  # noqa: E402

SITE = paths.root()

IQ3_FILES = [
    {'path': 'qwen3.8-flash-next-iq3_xxs-00001-of-00002.gguf', 'size': 40000000000, 'sha256': 'a' * 64},
    {'path': 'qwen3.8-flash-next-iq3_xxs-00002-of-00002.gguf', 'size': 34000000000, 'sha256': 'b' * 64},
]
GYRO_FILES = [
    {'path': 'gyro-Gyro-M/Qwen3.8-Flash-Next-Gyro-M-TQ2_0.gguf', 'size': 91965774272, 'sha256': 'c' * 64},
]
UD_FILES = [
    {'path': 'qwen3.8-flash-next-unsloth-ud-iq3_xxs.gguf', 'size': 70000000000, 'sha256': 'd' * 64},
]
DS_FILES = [
    {'path': 'dsv41-iq2_xxs.gguf', 'size': 90000000000, 'sha256': 'e' * 64},
]


def _strata_manifest(stem, version, family, model, files, workers, context=262144,
                     contexts=None, served_model=None, extra=None):
    spec = {
        'id': stem,
        'repository': 'https://example.invalid/Strata.git',
        'source_version': version,
        'source_revision': '1' * 40,
        'source_ref': f'v{version}',
        'ggml_revision': '3cf03257f219' + '0' * 30,
        'cuda_toolkit_major': 13,
        'family': family,
        'model': model,
        'model_repository': 'synth/Model-GGUF',
        'model_revision': '2' * 40,
        'context': context,
        'contexts': contexts or [context],
        'served_model': served_model or 'qwen3.8-flash-next',
        'files': files,
        'workers': {host: {'cuda_arch': '87'} for host in workers},
    }
    if extra:
        spec.update(extra)
    return spec


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
                    'ondemand_persistent': True, 'default_profile': 'small-q6',
                    'gateway_alias': 'workera-worker', 'tunnel_port': 18101,
                    'backend_port': 18080, 'ondemand_backend_port': 18090,
                    'strata_backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                    'strata_trial_roots': {'0.2.10': '/srv/agentiac/strata-0.2.10',
                                           '0.2.12': '/srv/agentiac/strata-0.2.12'},
                },
                'workerb': {
                    'ansible_host': 'workerb.example', 'node_class': 'orin',
                    'ondemand_persistent': True, 'default_profile': 'small-q6',
                    'gateway_alias': 'workerb-worker', 'tunnel_port': 18102,
                    'backend_port': 18080, 'ondemand_backend_port': 18090,
                    'strata_backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                    'strata_trial_roots': {'0.2.10': '/srv/agentiac/strata-0.2.10',
                                           '0.2.12': '/srv/agentiac/strata-0.2.12'},
                },
                'workerc': {
                    'ansible_host': 'workerc.example', 'node_class': 'rtx5090', 'borrowed': True,
                    'backend': 'strata', 'strata_api_port': 18085, 'strata_tunnel_port': 18115,
                    'ondemand_backend_port': 18090, 'strata_default_backend': 'strata-0.2.11-qfn-ud-iq3_xxs',
                    'strata_trial_roots': {
                        '0.2.8': '/srv/agentiac/strata-0.2.8', '0.2.11': '/srv/agentiac/strata-0.2.11',
                        'strata-0.2.11-qfn-gyro-m-tq2_0': '/srv/agentiac/strata-0.2.11-gyro',
                    },
                },
                'workerd': {
                    'ansible_host': 'workerd.example', 'node_class': 'rtx5090', 'borrowed': True,
                    'backend': 'strata', 'strata_api_port': 18085, 'strata_tunnel_port': 18114,
                    'ondemand_backend_port': 18090, 'strata_default_backend': 'strata-0.2.8-qfn-gyro-m-tq2_0',
                    'strata_trial_roots': {
                        '0.2.8': '/mnt/data/agentiac-strata-0.2.8',
                        'strata-0.2.11-qfn-gyro-m-tq2_0': '/mnt/data/agentiac-strata-0.2.11-gyro',
                    },
                },
                'workere': {
                    'ansible_host': 'workere.example', 'node_class': 'gb10', 'borrowed': True,
                    'former_names': ['oldbox'], 'project_root': '/srv/agentiac',
                    'spark_tensorfold': True, 'backend_port': 18080, 'ondemand_backend_port': 18090,
                    'strata_trial_roots': {'0.2.13': '/srv/agentiac/strata-0.2.13'},
                },
                'control': {
                    'ansible_connection': 'local', 'ansible_host': 'localhost', 'node_class': 'laptop',
                    'borrowed': True, 'default_profile': 'tiny-q4', 'project_root': '/srv/agentiac',
                    'backend_port': 18106, 'ondemand_backend_port': 18090,
                },
            }}},
        },
    }
    (root / 'infra/inventory.yml').write_text(yaml.safe_dump(inventory, sort_keys=False))
    (root / 'profiles/small-q6.json').write_text(json.dumps({'nodes': ['orin'], 'model': 'q6'}))
    (root / 'profiles/tiny-q4.json').write_text(json.dumps({'nodes': ['laptop'], 'model': 'q4'}))

    backends = {
        'strata-0.2.10-qfn-gsq-rco-iq3_xxs': _strata_manifest(
            'strata-0.2.10-qfn-gsq-rco-iq3_xxs', '0.2.10', 'qfn', 'Qwen/Qwen3.8-Flash-Next',
            IQ3_FILES, ['workera', 'workerb'], context=262144, served_model='qwen3.8-flash-next-iq3_xxs'),
        'strata-0.2.12-qfn-gsq-rco-iq3_xxs': _strata_manifest(
            'strata-0.2.12-qfn-gsq-rco-iq3_xxs', '0.2.12', 'qfn', 'Qwen/Qwen3.8-Flash-Next',
            IQ3_FILES, ['workera', 'workerb'], context=262144, served_model='qwen3.8-flash-next-iq3_xxs'),
        'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2': _strata_manifest(
            'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2', '0.2.12', 'qfn', 'Qwen/Qwen3.8-Flash-Next',
            IQ3_FILES, ['workerb'], context=262144, served_model='qwen3.8-flash-next-iq3_xxs',
            extra={'workers': {'workerb': {'cuda_arch': '87', 'drafter': 'dflash2'}}}),
        'strata-0.2.8-qfn-gyro-m-tq2_0': _strata_manifest(
            'strata-0.2.8-qfn-gyro-m-tq2_0', '0.2.8', 'qfn', 'Gyro-M',
            GYRO_FILES, ['workerc', 'workerd'], context=131072,
            served_model='qwen3.8-flash-next-gyro-gyro-m'),
        'strata-0.2.11-qfn-gyro-m-tq2_0': _strata_manifest(
            'strata-0.2.11-qfn-gyro-m-tq2_0', '0.2.11', 'qfn', 'Gyro-M',
            GYRO_FILES, ['workerc', 'workerd'], context=131072,
            served_model='qwen3.8-flash-next-gyro-gyro-m'),
        'strata-0.2.11-qfn-ud-iq3_xxs': _strata_manifest(
            'strata-0.2.11-qfn-ud-iq3_xxs', '0.2.11', 'qfn', 'UD-IQ3_XXS',
            UD_FILES, ['workerc'], context=262144, served_model='qwen3.8-flash-next-unsloth-ud-iq3_xxs'),
        'strata-0.2.13-dsv41-coder-iq2_xxs': _strata_manifest(
            'strata-0.2.13-dsv41-coder-iq2_xxs', '0.2.13', 'deepseek', 'DeepSeek-V4.1-Flash',
            DS_FILES, ['workere'], context=131072, served_model='deepseek-v4.1-flash'),
        'tf-1.1.4-qfn-mxfp8-nvfp4': {
            'id': 'tf-1.1.4-qfn-mxfp8-nvfp4',
            'engine': {'repository': 'https://example.invalid/TensorFold.git', 'version': '1.1.4',
                       'revision': '3' * 40},
            'model': {'repository': 'nvidia/Qwen3.8-Flash-Next-FP4-Local', 'revision': '4' * 40,
                      'directory': 'evaluations/tf-flash/weights-rev1'},
            'quantization': 'mixed-MXFP8-NVFP4', 'family': 'qfn', 'kv_cache_type': 'bf16',
            'workers': {'workere': {'slots': 1}},
        },
    }
    for stem, spec in backends.items():
        (root / f'backends/{stem}.json').write_text(json.dumps(spec))

    gateway_config = {
        'model_list': [
            {'model_name': name, 'litellm_params': {'model': 'openai/m', 'api_base': f'http://127.0.0.1:{18100 + idx}/v1'}}
            for idx, name in enumerate((
                'workera-worker', 'workerb-worker', 'workerc-worker-gyro-m', 'workerc-worker-ud-iq3xxs',
                'workerd-worker-gyro-m', 'workere-worker-qfn', 'workere-worker-dsv41', 'control-small', 'cloud-alias'))
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
            'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
                'source_alias': 'workerc-worker-gyro-m', 'node': 'workerc',
                'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'context_window': 131072, 'max_tokens': 16384},
            'workerc-strata-0.2.11-qfn-gyro-m-tq2_0-128k': {
                'source_alias': 'workerc-worker-gyro-m', 'node': 'workerc',
                'backend': 'strata-0.2.11-qfn-gyro-m-tq2_0', 'context_window': 131072, 'max_tokens': 16384},
            'workerc-strata-0.2.11-qfn-ud-iq3_xxs-256k': {
                'source_alias': 'workerc-worker-ud-iq3xxs', 'node': 'workerc',
                'backend': 'strata-0.2.11-qfn-ud-iq3_xxs', 'context_window': 262144, 'max_tokens': 16384},
            'workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
                'source_alias': 'workerd-worker-gyro-m', 'node': 'workerd',
                'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'context_window': 131072, 'max_tokens': 16384},
            'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k': {
                'source_alias': 'workere-worker-qfn', 'node': 'workere',
                'backend': 'tf-1.1.4-qfn-mxfp8-nvfp4', 'context_window': 262144, 'max_tokens': 16384},
        },
        'legacy_targets': {},
        'legacy_sources': {},
    }
    (root / 'gateway/local-coder.json').write_text(json.dumps(route_spec, indent=2))
    (root / 'agents/pi/models.json').write_text(json.dumps({
        'providers': {'local-infra': {'models': [{'id': 'local-coder'}]}}
    }))
    (root / 'agents/pi/router.json').write_text(json.dumps({'low': 'local-coder', 'high': 'cloud-alias'}))

    retention_spec = {
        'workera': {
            'note': 'no N-1',
            'serving': [{'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'path': '/srv/agentiac/strata-0.2.12',
                         'class': 'persistent', 'form': 'full', 'reason': 'serving'}],
            'rollback': [{'backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs', 'path': '/srv/agentiac/strata-0.2.10',
                          'class': 'persistent', 'form': 'full', 'reason': 'N-1'}],
            'candidate': [], 'quarantine': [], 'retired': [],
            'staging': [{'path': '/srv/agentiac/retired', 'class': 'persistent', 'reason': 'staging'}],
            'areas': [{'path': '/srv/agentiac', 'class': 'persistent', 'reason': 'roots'}],
        },
        'workerb': {
            'note': 'way back to the rollback root',
            'serving': [{'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'path': '/srv/agentiac/strata-0.2.12',
                         'class': 'persistent', 'form': 'full', 'reason': 'serving'}],
            'rollback': [{'backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs', 'path': '/srv/agentiac/strata-0.2.10',
                          'class': 'persistent', 'form': 'full', 'reason': 'N-1'}],
            'candidate': [], 'quarantine': [],
            'retired': [{'name': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2-isolated',
                         'path': '/srv/agentiac/retired/strata-0.2.12-dflash2',
                         'class': 'persistent', 'reason': 'purged after 46.3 h with in-use check'}],
            'staging': [{'path': '/srv/agentiac/retired', 'class': 'persistent', 'reason': 'staging'}],
            'areas': [{'path': '/srv/agentiac', 'class': 'persistent', 'reason': 'roots'}],
        },
        'workerc': {
            'serving': [{'backend': 'strata-0.2.11-qfn-ud-iq3_xxs', 'path': '/srv/agentiac/strata-0.2.11',
                         'class': 'persistent', 'form': 'full', 'reason': 'serving'}],
            'rollback': [{'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'path': '/srv/agentiac/strata-0.2.8',
                          'class': 'persistent', 'form': 'full', 'reason': 'N-1'}],
            'candidate': [{'backend': 'strata-0.2.11-qfn-gyro-m-tq2_0', 'path': '/srv/agentiac/strata-0.2.11-gyro',
                           'class': 'persistent', 'form': 'full', 'retirement': 'candidate', 'reason': 'superseded'}],
            'quarantine': [], 'retired': [],
            'staging': [{'path': '/srv/agentiac/retired', 'class': 'persistent', 'reason': 'staging'}],
            'areas': [{'path': '/srv/agentiac', 'class': 'persistent', 'reason': 'roots'}],
        },
        'workerd': {
            'serving': [{'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'path': '/mnt/data/agentiac-strata-0.2.8',
                         'class': 'persistent', 'form': 'full', 'reason': 'serving'}],
            'rollback': [],
            'candidate': [{'backend': 'strata-0.2.11-qfn-gyro-m-tq2_0',
                           'path': '/mnt/data/agentiac-strata-0.2.11-gyro',
                           'class': 'persistent', 'form': 'full', 'retirement': 'declined', 'reason': 'keep'}],
            'quarantine': [], 'retired': [],
            'staging': [{'path': '/mnt/data/agentiac-retired', 'class': 'persistent', 'reason': 'staging'}],
            'areas': [
                {'path': '/mnt/data/agentiac-strata-0.2.8', 'class': 'persistent', 'reason': 'serving root'},
                {'path': '/mnt/data/agentiac-strata-0.2.11-gyro', 'class': 'persistent', 'reason': 'candidate root'},
                {'path': '/mnt/data/agentiac-retired', 'class': 'persistent', 'reason': 'staging root'},
                {'path': '/mnt/data/alice', 'class': 'user-owned', 'reason': 'owner directory'},
                {'path': '/mnt/data/bob', 'class': 'user-owned', 'reason': 'owner directory'},
            ],
        },
        'workere': {
            'serving': [{'backend': 'tf-1.1.4-qfn-mxfp8-nvfp4', 'image': 'agentiac-tensorfold:v114',
                         'class': 'persistent', 'form': 'full', 'reason': 'serving'}],
            'rollback': [{'image': 'agentiac-tensorfold:v113', 'class': 'persistent', 'form': 'full',
                          'retirement': 'declined', 'reason': '113 MB unique layers'}],
            'candidate': [{'backend': 'strata-0.2.13-dsv41-coder-iq2_xxs', 'path': '/srv/agentiac/strata-0.2.13',
                           'class': 'persistent', 'form': 'full', 'retirement': 'candidate',
                           'reason': 'trial root'}],
            'quarantine': [],
            'retired': [{'image': 'agentiac-runtime:old', 'class': 'persistent', 'reason': 'removed'}],
            'staging': [{'path': '/srv/agentiac/retired', 'class': 'persistent', 'reason': 'staging'}],
            'areas': [
                {'path': '/srv/agentiac', 'class': 'persistent', 'reason': 'roots'},
                {'path': '/srv/agentiac/evaluations/tf-flash/weights-rev1',
                 'class': 'persistent', 'reason': 'pinned weights'},
            ],
        },
        'control': {
            'serving': [], 'rollback': [], 'candidate': [], 'quarantine': [],
            'retired': [{'image': 'agentiac-runtime:old', 'class': 'persistent', 'reason': 'removed'}],
            'staging': [],
            'areas': [{'path': '/srv/agentiac/artifacts', 'class': 'persistent', 'reason': 'results'}],
        },
    }
    (root / 'infra/retention.yml').write_text(yaml.safe_dump(retention_spec, sort_keys=False))
    return root


class Mutated(unittest.TestCase):
    """Base: run a test against a private synthetic checkout holding the files the ledger is checked against."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = write_synthetic_site(Path(directory.name))

    def graph(self):
        return declared.load(self.root)

    def problems(self):
        graph = self.graph()
        return retention.problems(graph.retention, graph.nodes, graph.backends, graph.route)

    def candidates(self):
        return retention.candidates(self.graph().retention)

    def edit(self, mutate):
        path = self.root / 'infra/retention.yml'
        ledger = yaml.safe_load(path.read_text())
        mutate(ledger)
        path.write_text(yaml.safe_dump(ledger, sort_keys=False, width=100))


class TheShippedLedgerTests(Mutated):
    """What the ledger says, read against the rest of the declaration rather than against itself."""

    def ledger(self):
        return self.graph().retention

    def test_it_agrees_with_the_inventory_the_manifests_and_the_route(self):
        site_graph = declared.load(SITE)
        if site_graph.retention:
            self.assertEqual(retention.problems(site_graph.retention, site_graph.nodes,
                                                site_graph.backends, site_graph.route), [])
        graph = self.graph()
        self.assertEqual(retention.problems(graph.retention, graph.nodes, graph.backends, graph.route), [])

    def test_every_declared_node_has_a_ledger_of_its_own(self):
        graph = self.graph()
        self.assertEqual(set(self.ledger()), set(graph.nodes))

    def test_a_claim_with_no_backend_serves_what_the_ledger_calls_serving(self):
        graph = self.graph()
        for host in ('workerc', 'workerd'):
            self.assertEqual([entry['backend'] for entry in self.ledger()[host]['serving']],
                             [graph.nodes[host]['strata_default_backend']])
        for host in ('workera', 'workerb'):
            self.assertEqual([entry['backend'] for entry in self.ledger()[host]['serving']],
                             [graph.nodes[host]['strata_backend']])

    def test_a_held_generation_lives_where_the_inventory_declares_it(self):
        graph = self.graph()
        for host, block in self.ledger().items():
            for state in retention.HELD:
                for entry in block[state]:
                    if 'backend' not in entry:
                        continue
                    want = retention.inventory_root(graph.nodes[host], entry['backend'], graph.backends[entry['backend']])
                    if want is None:
                        continue
                    with self.subTest(f'{host} {entry["backend"]}'):
                        self.assertEqual(entry['path'], want)

    def test_a_checkpoint_a_manifest_pins_outside_a_root_is_held_too(self):
        graph = self.graph()
        pinned = sorted((stem, manifest['model']['directory'])
                        for stem, manifest in graph.backends.items()
                        if isinstance(manifest.get('model'), dict) and manifest['model'].get('directory'))
        self.assertEqual(pinned, [('tf-1.1.4-qfn-mxfp8-nvfp4', 'evaluations/tf-flash/weights-rev1')])
        for stem, directory in pinned:
            held = [entry for block in self.ledger().values() for state in ('serving', 'rollback', 'candidate')
                    for entry in block[state] if entry.get('path', '').endswith('/' + directory)]
            self.assertEqual(held, [], f'{stem}: {directory} is no engine root, so it cannot be a HELD line')
            areas = [entry for block in self.ledger().values() for entry in block['areas']
                     if entry.get('path', '').endswith('/' + directory)]
            self.assertEqual([entry['class'] for entry in areas], ['persistent'], f'{stem}: {directory} is ours')
            self.assertIsNone(areas[0].get('retirement'), f'{stem}: what the serving backend loads is not goable')

    def test_genie_equivalent_holds_three_generations_and_only_one_of_them_serves(self):
        block = self.ledger()['workerc']
        self.assertEqual([entry['backend'] for entry in block['serving']], ['strata-0.2.11-qfn-ud-iq3_xxs'])
        self.assertEqual([(entry['backend'], entry['form']) for entry in block['rollback']],
                         [('strata-0.2.8-qfn-gyro-m-tq2_0', 'full')])
        self.assertEqual([(entry['backend'], entry['retirement']) for entry in block['candidate']],
                         [('strata-0.2.11-qfn-gyro-m-tq2_0', 'candidate')])

    def test_frank_equivalent_holds_a_candidate_it_has_decided_to_keep(self):
        block = self.ledger()['workerd']
        self.assertEqual(block['rollback'], [])
        self.assertEqual([(entry['backend'], entry['retirement']) for entry in block['candidate']],
                         [('strata-0.2.11-qfn-gyro-m-tq2_0', 'declined')])

    def test_the_dedicated_workers_hold_their_own_n_minus_1(self):
        for host in ('workera', 'workerb'):
            block = self.ledger()[host]
            with self.subTest(host):
                self.assertEqual([(entry['backend'], entry['form']) for entry in block['rollback']],
                                 [('strata-0.2.10-qfn-gsq-rco-iq3_xxs', 'full')])
                self.assertEqual([entry['backend'] for entry in block['serving']], ['strata-0.2.12-qfn-gsq-rco-iq3_xxs'])
                self.assertEqual([(entry['backend'], entry['form'], entry.get('retirement'))
                                  for entry in block['candidate']], [])
                self.assertEqual(block['quarantine'], [])
        retired = {entry.get('name'): entry for entry in self.ledger()['workerb']['retired']}
        drafter = retired['strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2-isolated']
        self.assertEqual(drafter['path'], '/srv/agentiac/retired/strata-0.2.12-dflash2')
        self.assertEqual(drafter['class'], 'persistent')
        self.assertIn('in-use', drafter['reason'])
        self.assertTrue((self.root / 'backends/strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2.json').exists())
        self.assertIn('no N-1', self.ledger()['workera']['note'])
        self.assertIn('way back to the rollback root', self.ledger()['workerb']['note'])

    def test_nothing_retired_is_still_declared_somewhere_else(self):
        graph = self.graph()
        for host, block in self.ledger().items():
            roots = retention.host_roots(graph.nodes[host])
            served = {spec['backend'] for spec in graph.route['targets'].values() if spec['node'] == host}
            for entry in block['retired']:
                with self.subTest(f'{host} {retention.handle(entry)}'):
                    self.assertNotIn(entry.get('path'), roots)
                    self.assertNotIn(entry.get('backend'), served)

    def test_the_owner_of_a_shared_disk_is_named_path_by_path(self):
        ledger = self.ledger()
        ours = {entry['path'] for host in ledger for state in retention.STATES + retention.AREAS
                for entry in ledger[host][state]
                if entry.get('class') in retention.OUT_OF_SCOPE and entry.get('path')}
        graph = self.graph()
        declared_roots = {root for host in graph.nodes for root in retention.host_roots(graph.nodes[host])}
        self.assertTrue({'/mnt/data/alice', '/mnt/data/bob'} <= ours)
        self.assertEqual(ours & declared_roots, set())
        for path in ours:
            with self.subTest(path):
                self.assertFalse(any(path.startswith(root + '/') or root.startswith(path + '/')
                                     for root in declared_roots))

    def test_control_holds_evidence_and_images_and_no_engine(self):
        block = self.ledger()['control']
        self.assertEqual([block[state] for state in ('serving', 'rollback', 'candidate', 'quarantine')], [[], [], [], []])
        self.assertEqual([entry['class'] for entry in block['areas'] if entry.get('path', '').endswith('artifacts')],
                         ['persistent'])

    def test_what_was_decided_about_images_stays_decided(self):
        rollback = self.ledger()['workere']['rollback']
        self.assertEqual([entry['image'] for entry in rollback], ['agentiac-tensorfold:v113'])
        self.assertEqual(rollback[0]['retirement'], 'declined')
        self.assertEqual(rollback[0]['form'], 'full')
        self.assertIn('113 MB unique', rollback[0]['reason'])
        for host in ('control', 'workere'):
            block = self.ledger()[host]
            gone = [entry for entry in block['retired'] if entry.get('image') == 'agentiac-runtime:old']
            self.assertEqual(len(gone), 1, host)
            self.assertNotIn('form', gone[0])
            self.assertNotIn('retirement', gone[0])
            self.assertEqual([entry for entry in block['areas']
                              if entry.get('image') == 'agentiac-runtime:old'], [], host)
        self.assertEqual([entry for entry in self.ledger()['workere']['rollback']
                          if entry.get('image') == 'agentiac-runtime:old'], [])

    def test_the_candidates_are_exactly_the_lines_marked_goable(self):
        self.assertEqual([(item['host'], item['handle']) for item in retention.candidates(self.ledger())],
                         [('workerc', 'strata-0.2.11-qfn-gyro-m-tq2_0'),
                          ('workere', 'strata-0.2.13-dsv41-coder-iq2_xxs')])

    def test_a_retirement_candidate_is_a_line_and_not_an_absence(self):
        graph = self.graph()
        self.assertTrue(any(item['host'] == 'workerc' for item in retention.candidates(graph.retention)))
        mutated = {host: dict(block) for host, block in graph.retention.items()}
        mutated['workerc'] = dict(mutated['workerc'],
                                  candidate=[dict(entry, retirement='declined') for entry in mutated['workerc']['candidate']])
        self.assertEqual([item['host'] for item in retention.candidates(mutated)], ['workere'])

    def test_it_is_a_ledger_of_data_and_not_a_playbook(self):
        data = self.ledger()
        self.assertTrue(all(state in block for block in data.values() for state in retention.STATES))
        self.assertIn('infra/retention.yml', (ROOT / 'Makefile').read_text().split('PLAYBOOKS =')[1].split('\n')[0])


class DisagreementTests(Mutated):
    """Each one breaks the ledger in one way and asks for that one sentence back."""

    def test_it_holds_an_engine_no_manifest_declares(self):
        self.edit(lambda ledger: ledger['workerc']['candidate'].append(
            {'backend': 'strata-qwen38-gyro-m-0.2.12', 'path': '/srv/agentiac/strata-0.2.12',
             'class': 'persistent', 'form': 'full', 'reason': 'a version that does not exist'}))
        self.assertIn('infra/retention.yml workerc candidate strata-qwen38-gyro-m-0.2.12: '
                      'no backends/strata-qwen38-gyro-m-0.2.12.json declares it', self.problems())

    def test_it_holds_an_engine_that_is_not_prepared_for_that_host(self):
        self.edit(lambda ledger: ledger['workerd']['rollback'].append(
            {'backend': 'strata-0.2.11-qfn-ud-iq3_xxs', 'path': '/mnt/data/agentiac-strata-0.2.11',
             'class': 'persistent', 'form': 'full', 'reason': 'workerc weight on workerd'}))
        self.assertIn('infra/retention.yml workerd rollback strata-0.2.11-qfn-ud-iq3_xxs: '
                      'backends/strata-0.2.11-qfn-ud-iq3_xxs.json does not name workerd as a worker, '
                      'so nothing there is prepared for it', self.problems())

    def test_a_root_says_one_thing_and_the_inventory_another(self):
        self.edit(lambda ledger: ledger['workerd']['serving'][0].__setitem__('path', '/mnt/data/agentiac-strata-0.2.8-old'))
        self.assertIn('infra/retention.yml workerd serving strata-0.2.8-qfn-gyro-m-tq2_0: the ledger says '
                      '/mnt/data/agentiac-strata-0.2.8-old, infra/inventory.yml declares /mnt/data/agentiac-strata-0.2.8',
                      self.problems())

    def test_serving_disagrees_with_what_a_claim_resolves_to(self):
        self.edit(lambda ledger: ledger['workerc']['serving'][0].__setitem__('backend', 'strata-0.2.8-qfn-gyro-m-tq2_0'))
        self.assertIn('infra/retention.yml workerc serving: a claim there resolves to strata-0.2.11-qfn-ud-iq3_xxs, '
                      'the ledger names strata-0.2.8-qfn-gyro-m-tq2_0', self.problems())

    def test_a_route_target_serves_what_the_ledger_does_not_hold(self):
        self.edit(lambda ledger: ledger['workerc']['candidate'].remove(ledger['workerc']['candidate'][0]))
        self.assertIn('gateway/local-coder.json target workerc-strata-0.2.11-qfn-gyro-m-tq2_0-128k: '
                      'serves strata-0.2.11-qfn-gyro-m-tq2_0 on workerc, which the ledger does not hold there',
                      self.problems())

    def test_a_route_target_names_a_root_the_ledger_calls_thin(self):
        self.edit(lambda ledger: ledger['workerc']['rollback'][0].update({'form': 'thin'}))
        found = self.problems()
        self.assertTrue(any('target workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k: names strata-0.2.8-qfn-gyro-m-tq2_0 on workerc, '
                            'which the ledger records as `form: thin`' in one for one in found), found)

    def test_one_thing_held_as_two_generations(self):
        self.edit(lambda ledger: ledger['workerd']['rollback'].append(dict(ledger['workerd']['serving'][0])))
        self.assertIn('infra/retention.yml workerd strata-0.2.8-qfn-gyro-m-tq2_0: held as serving, rollback — '
                      'one thing is one generation', self.problems())

    def test_a_retired_line_names_a_root_that_is_still_declared(self):
        self.edit(lambda ledger: ledger['workerd']['retired'].append(
            {'name': 'workerd-0.2.8', 'path': '/mnt/data/agentiac-strata-0.2.8', 'class': 'persistent',
             'reason': 'deleted, said the ledger'}))
        self.assertIn('infra/retention.yml workerd retired workerd-0.2.8: /mnt/data/agentiac-strata-0.2.8 is retired '
                      'here and still a declared root in infra/inventory.yml', self.problems())

    def test_a_retired_line_that_is_still_served(self):
        self.edit(lambda ledger: ledger['workerb']['retired'].append(
            {'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'class': 'persistent', 'reason': 'gone, said the ledger'}))
        self.assertIn('infra/retention.yml workerb retired strata-0.2.12-qfn-gsq-rco-iq3_xxs: a route target still serves '
                      'strata-0.2.12-qfn-gsq-rco-iq3_xxs on workerb, which this file calls retired', self.problems())

    def test_a_held_generation_marked_out_of_scope(self):
        self.edit(lambda ledger: ledger['workerd']['serving'][0].__setitem__('class', 'user-owned'))
        self.assertIn('infra/retention.yml workerd serving strata-0.2.8-qfn-gyro-m-tq2_0: user-owned is out of scope '
                      '(§3.4), so it cannot be a held generation', self.problems())

    def test_an_out_of_scope_path_that_overlaps_ours(self):
        self.edit(lambda ledger: ledger['workerd']['areas'].append(
            {'path': '/mnt/data/agentiac-retired', 'class': 'user-owned', 'reason': 'the owner said so'}))
        self.assertIn('infra/retention.yml workerd areas /mnt/data/agentiac-retired: out of scope, yet it overlaps '
                      '/mnt/data/agentiac-retired, which is ours (§3.4)', self.problems())

    def test_a_declared_root_no_persistent_line_covers(self):
        self.edit(lambda ledger: ledger['workerc']['areas'].remove(ledger['workerc']['areas'][0]))
        for root in ('/srv/agentiac/strata-0.2.8', '/srv/agentiac/strata-0.2.11', '/srv/agentiac/strata-0.2.11-gyro'):
            self.assertIn(f'infra/retention.yml workerc: {root} is a declared root and no `class: persistent` '
                          f'line says where the roots of this host live', self.problems())

    def test_a_node_the_ledger_says_nothing_about(self):
        self.edit(lambda ledger: ledger.pop('workere'))
        self.assertEqual(self.problems(),
                         ['infra/retention.yml: workere appears in infra/inventory.yml and holds nothing in the ledger'])

    def test_a_ledger_line_for_a_host_that_does_not_exist(self):
        self.edit(lambda ledger: ledger.__setitem__('workerc2', {state: [] for state in retention.STATES + retention.AREAS}))
        self.assertEqual(self.problems(), ['infra/retention.yml host workerc2: is not a node in infra/inventory.yml'])

    def test_a_quarantine_outside_the_staging_the_file_names(self):
        self.edit(lambda ledger: ledger['workerc']['quarantine'].append(
            {'name': 'workerc-0.2.8-being-removed', 'path': '/srv/agentiac/strata-0.2.8',
             'class': 'persistent', 'form': 'full', 'reason': 'mid-removal in the wrong place'}))
        self.assertIn('infra/retention.yml workerc quarantine workerc-0.2.8-being-removed: '
                      '/srv/agentiac/strata-0.2.8 is not under a staging directory this file names '
                      'for workerc (§5.5)', self.problems())

    def test_a_quarantine_inside_the_staging_is_a_state_the_file_can_hold(self):
        self.edit(lambda ledger: ledger['workerc']['quarantine'].append(
            {'name': 'workerc-0.2.8-isolated', 'path': '/srv/agentiac/retired/strata-0.2.8',
             'class': 'persistent', 'form': 'full', 'reason': 'isolated 24 hours before any approval'}))
        self.assertEqual(self.problems(), [])

    def test_a_retired_thing_that_is_still_full_or_thin(self):
        self.edit(lambda ledger: ledger['workerc']['retired'].append(
            {'name': 'workerc-thin-thing', 'path': '/srv/agentiac/gone', 'class': 'persistent',
             'form': 'thin', 'reason': 'deleted'}))
        self.assertIn('infra/retention.yml workerc retired workerc-thin-thing: a retired thing is neither full nor '
                      'thin nor a candidate — it is gone', self.problems())

    def test_a_class_the_file_does_not_define(self):
        self.edit(lambda ledger: ledger['workerd']['areas'][0].__setitem__('class', 'ours'))
        self.assertEqual(self.problems(),
                         ['infra/retention.yml workerd/areas/0/class: \'ours\' is not one of '
                          '[\'persistent\', \'managed-cache\', \'ephemeral\', \'user-owned\', \'unknown\']'])

    def test_a_thing_without_a_reason(self):
        self.edit(lambda ledger: ledger['workerd']['areas'][0].pop('reason'))
        self.assertEqual(self.problems(), ['infra/retention.yml workerd/areas/0: \'reason\' is a required property'])

    def test_what_serves_is_never_a_retirement_candidate(self):
        self.edit(lambda ledger: ledger['workerc']['serving'][0].__setitem__('retirement', 'candidate'))
        self.assertIn('infra/retention.yml workerc serving strata-0.2.11-qfn-ud-iq3_xxs: what serves is never a '
                      'retirement candidate (§4.3)', self.problems())

    def test_an_out_of_scope_line_marked_goable(self):
        self.edit(lambda ledger: ledger['workerd']['areas'][3].__setitem__('retirement', 'candidate'))
        self.assertIn('infra/retention.yml workerd areas /mnt/data/alice: user-owned is out of scope (§3.4), '
                      'so nothing here may mark it goable', self.problems())

    def test_a_held_thing_that_never_says_its_form(self):
        self.edit(lambda ledger: ledger['workerc']['rollback'][0].pop('form'))
        self.assertIn('infra/retention.yml workerc rollback strata-0.2.8-qfn-gyro-m-tq2_0: holds something without '
                      'saying whether it is full or thin (ADR 0008)', self.problems())

    def test_a_path_that_is_a_glob(self):
        self.edit(lambda ledger: ledger['workerd']['areas'][3].__setitem__('path', '/mnt/data/*'))
        self.assertIn("infra/retention.yml workerd/areas/3/path: '/mnt/data/*' does not match", self.problems()[0])

    def test_the_ledger_is_part_of_the_graph_too(self):
        self.assertEqual([p for p in declared.references(self.graph()) if 'infra/retention.yml' in p], [])
        self.edit(lambda ledger: ledger['workerd']['serving'][0].pop('path'))
        self.assertIn('infra/retention.yml workerd serving strata-0.2.8-qfn-gyro-m-tq2_0: infra/inventory.yml declares '
                      '/mnt/data/agentiac-strata-0.2.8 for it and the ledger names no root',
                      declared.references(self.graph()))


class DeclaredServiceRootsTests(unittest.TestCase):
    """A manifest that declares its `service` says which root of that host it is served from, and the ledger has to
    know that root or it cannot hold the generation (`infra/retention.yml`, plan §2.3)."""

    MANIFEST = {'id': 'infernix-fixture',
                'engine': {'repository': 'https://example.invalid/Infernix-dgx-spark-gb10.git', 'version': '1.0.0'},
                'workers': {'workere': {'service': {'unit': 'agentiac-infernix-inference', 'port': 18090,
                                                     'root': '/srv/agentiac/evaluations/infernix/service'}}}}
    FACTS = {'project_root': '/srv/agentiac', 'strata_trial_roots': {'0.2.8': '/mnt/data/agentiac-strata-0.2.8'}}

    def test_a_host_with_no_trial_root_still_has_the_root_its_manifest_declares(self):
        self.assertEqual(retention.host_roots({'project_root': '/srv/agentiac'}, 'workere', [self.MANIFEST]),
                         ['/srv/agentiac/evaluations/infernix/service'])

    def test_an_inventory_root_and_a_declared_root_are_both_roots_of_that_host(self):
        self.assertEqual(retention.host_roots(self.FACTS, 'workere', [self.MANIFEST]),
                         ['/mnt/data/agentiac-strata-0.2.8', '/srv/agentiac/evaluations/infernix/service'])

    def test_a_manifest_that_declares_nothing_leaves_the_inventory_answering_as_before(self):
        self.assertEqual(retention.host_roots(self.FACTS), ['/mnt/data/agentiac-strata-0.2.8'])
        self.assertEqual(retention.host_roots(self.FACTS, 'workere', [{'workers': {'workere': {}}}]),
                         ['/mnt/data/agentiac-strata-0.2.8'])

    def test_the_manifest_is_asked_first_because_it_is_the_more_specific_declaration(self):
        self.assertEqual(retention.inventory_root(self.FACTS, 'infernix-fixture', self.MANIFEST, 'workere'),
                         '/srv/agentiac/evaluations/infernix/service')

    def test_a_manifest_with_no_service_is_resolved_by_the_inventory_rule(self):
        flat = {'id': 'strata-fixture', 'source_version': '0.2.8', 'workers': {'workere': {'cuda_arch': '121'}}}
        self.assertEqual(retention.inventory_root(self.FACTS, 'strata-fixture', flat, 'workere'),
                         '/mnt/data/agentiac-strata-0.2.8')

    def test_retired_and_unsafe_service_roots_are_ignored(self):
        from agentiac import identities
        retired = {**self.MANIFEST, 'status': 'retired 2026-11-23'}
        relative = {'workers': {'workere': {'service': {'root': 'evaluations/infernix'}}}}
        traversal = {'workers': {'workere': {'service': {'root': '/srv/../etc'}}}}
        for bad in (retired, relative, traversal):
            with self.subTest(bad=bad):
                self.assertIsNone(identities.service_root(bad, 'workere'))
        self.assertEqual(retention.host_roots({'project_root': '/srv/agentiac'}, 'workere',
                                              [retired, relative, traversal]), [])

    def test_quarantined_declared_service_under_staging_has_no_problem(self):
        ledger = {
            'workere': {
                'serving': [], 'rollback': [], 'candidate': [], 'retired': [],
                'areas': [{'path': '/srv/agentiac', 'class': 'persistent', 'reason': 'roots'}],
                'staging': [{'path': '/srv/agentiac/retired', 'class': 'persistent', 'reason': 'staging'}],
                'quarantine': [{'backend': 'infernix-fixture', 'path': '/srv/agentiac/retired/infernix-service',
                                'class': 'persistent', 'form': 'full', 'reason': 'quarantined'}],
            }
        }
        nodes = {'workere': {'project_root': '/srv/agentiac'}}
        backends = {'infernix-fixture': self.MANIFEST}
        self.assertEqual(retention.problems(ledger, nodes, backends, {'targets': {}}), [])


if __name__ == '__main__':
    unittest.main()
