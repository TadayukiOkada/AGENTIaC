"""Local-coder rendering, borrowed-host release report, claims, and smoke stream parsing."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'gateway'))
from agentiac import declared, errors, paths  # noqa: E402
import agent  # noqa: E402
import borrow  # noqa: E402
import identity_policy  # noqa: E402
import route  # noqa: E402
import smoke  # noqa: E402

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
                     contexts=None, served_model=None):
    return {
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
                'resource_prefix': borrow.PREFIX,
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
                    'strata_trial_roots': {'0.2.8': '/mnt/data/agentiac-strata-0.2.8'},
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
        'strata-0.2.8-qfn-gyro-m-tq2_0': _strata_manifest(
            'strata-0.2.8-qfn-gyro-m-tq2_0', '0.2.8', 'qfn', 'Gyro-M',
            GYRO_FILES, ['workerc', 'workerd'], context=131072, contexts=[65536, 131072],
            served_model='qwen3.8-flash-next-gyro-gyro-m'),
        'strata-0.2.11-qfn-gyro-m-tq2_0': _strata_manifest(
            'strata-0.2.11-qfn-gyro-m-tq2_0', '0.2.11', 'qfn', 'Gyro-M',
            GYRO_FILES, ['workerc', 'workerd'], context=131072, contexts=[65536, 131072],
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
    (root / 'backends/tf-1.1.4-qfn-mxfp8-nvfp4.launch.json').write_text(json.dumps(['--flag']))

    base = {
        'model_list': [
            {'model_name': 'workere-worker-qfn', 'litellm_params': {'model': 'openai/tf114-bf16', 'api_base': 'http://127.0.0.1:18103/v1'}},
            {'model_name': 'workerb-worker', 'litellm_params': {'model': 'openai/qwen3.8-flash-next-iq3_xxs', 'api_base': 'http://127.0.0.1:18102/v1'}},
            {'model_name': 'workere-worker-dsv41', 'litellm_params': {'model': 'openai/deepseek-v4.1-flash', 'api_base': 'http://127.0.0.1:18103/v1'}},
            {'model_name': 'workera-worker', 'litellm_params': {'model': 'openai/qwen3.8-flash-next-iq3_xxs', 'api_base': 'http://127.0.0.1:18101/v1'}},
            {'model_name': 'workerc-worker-gyro-m', 'litellm_params': {'model': 'openai/qwen3.8-flash-next-gyro-gyro-m', 'api_base': 'http://127.0.0.1:18115/v1'}},
            {'model_name': 'workerd-worker-gyro-m', 'litellm_params': {'model': 'openai/qwen3.8-flash-next-gyro-gyro-m', 'api_base': 'http://127.0.0.1:18114/v1'}},
            {'model_name': 'workerc-worker-ud-iq3xxs', 'litellm_params': {'model': 'openai/qwen3.8-flash-next-unsloth-ud-iq3_xxs', 'api_base': 'http://127.0.0.1:18115/v1'}},
        ],
        'router_settings': {'fallbacks': []},
    }
    (root / 'gateway/config.yaml').write_text(yaml.safe_dump(base, sort_keys=False))

    routes = {
        'version': 2,
        'alias': 'local-coder',
        'default': 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
        'targets': {
            'workera-strata-0.2.10-qfn-gsq-rco-iq3_xxs-256k': {
                'source_alias': 'workera-worker', 'node': 'workera',
                'backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs', 'context_window': 262144,
                'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'off'},
            'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k': {
                'source_alias': 'workerb-worker', 'node': 'workerb',
                'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'context_window': 262144,
                'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'off'},
            'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
                'source_alias': 'workerc-worker-gyro-m', 'node': 'workerc',
                'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'context_window': 131072,
                'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'medium'},
            'workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
                'source_alias': 'workerd-worker-gyro-m', 'node': 'workerd',
                'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'context_window': 131072,
                'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'medium'},
            'workerc-strata-0.2.11-qfn-ud-iq3_xxs-256k': {
                'source_alias': 'workerc-worker-ud-iq3xxs', 'node': 'workerc',
                'backend': 'strata-0.2.11-qfn-ud-iq3_xxs', 'context_window': 262144,
                'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'off'},
            'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k': {
                'source_alias': 'workere-worker-qfn', 'node': 'workere',
                'backend': 'tf-1.1.4-qfn-mxfp8-nvfp4', 'context_window': 262144,
                'max_tokens': 16384, 'thinking': 'qwen-chat-template',
                'thinking_start': 'medium',
                'compat': {'thinkingFormat': 'qwen-chat-template', 'chatTemplateKwargs': {'preserve_thinking': True}}},
            'workere-strata-0.2.13-dsv41-coder-iq2_xxs-128k': {
                'source_alias': 'workere-worker-dsv41', 'node': 'workere',
                'backend': 'strata-0.2.13-dsv41-coder-iq2_xxs', 'context_window': 131072,
                'max_tokens': 8192, 'thinking': 'qwen-chat-template', 'thinking_start': 'off'},
        },
        'legacy_targets': {
            'oldbox': 'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k',
            'workerb': 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
            'strata-workerc-gyro-m-0.2.8-128k': 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k',
        },
        'legacy_sources': {
            'oldbox-tensorfold': 'workere-worker-qfn',
        },
    }
    (root / 'gateway/local-coder.json').write_text(json.dumps(routes, indent=2))
    (root / 'agents/pi/models.json').write_text(json.dumps({
        'providers': {'local-infra': {'models': [
            {'id': 'local-coder'},
            {'id': 'workerb-worker', 'name': 'workerb', 'contextWindow': 262144, 'maxTokens': 16384, 'reasoning': True},
        ]}}
    }))
    (root / 'agents/pi/router.json').write_text(json.dumps({'low': 'local-coder', 'high': 'cloud-gpt-sol'}))

    retention = {
        host: {
            'serving': [], 'rollback': [], 'candidate': [], 'quarantine': [], 'retired': [],
            'staging': [{'path': '/srv/agentiac/retired', 'class': 'persistent', 'reason': 'staging'}],
            'areas': [{'path': '/srv/agentiac', 'class': 'persistent', 'reason': 'roots'}],
        }
        for host in ('workera', 'workerb', 'workerc', 'workerd', 'workere', 'control')
    }
    retention['workerc']['rollback'] = [{
        'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'path': '/srv/agentiac/strata-0.2.8',
        'class': 'persistent', 'form': 'full', 'reason': 'N-1',
    }]
    (root / 'infra/retention.yml').write_text(yaml.safe_dump(retention, sort_keys=False))
    return base, routes


class SyntheticSiteMixin:
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.base, cls.routes = write_synthetic_site(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        patchers = [
            mock.patch.object(route, 'ROOT', self.root),
            mock.patch.object(route, 'ROUTES', self.root / 'gateway/local-coder.json'),
            mock.patch.object(agent, 'ROOT', self.root),
            mock.patch.object(borrow, 'ROOT', self.root),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)


class LocalAnyTests(unittest.TestCase):
    MODELS = ('orin1-worker', 'orin2-worker', 'another-worker')

    @staticmethod
    def pi_env(alias):
        return {'PI_CODING_AGENT_DIR': str(agent.STATE), 'AGENTIAC_PI_MODEL': alias}

    def test_reads_proc_environments_and_skips_inaccessible_processes(self):
        own, unreadable, vanished, pi = (
            SimpleNamespace(parent=SimpleNamespace(name=pid), read_bytes=mock.Mock())
            for pid in ('100', '101', '102', '103')
        )
        unreadable.read_bytes.side_effect = PermissionError
        vanished.read_bytes.side_effect = FileNotFoundError
        pi.read_bytes.return_value = b'AGENTIAC_PI_MODEL=orin1-worker\0AGENTIAC_PI_PID=103\0OTHER=a=b\0malformed\0'
        # a child of a session (pi's bash tool) inherits the markers but not the recorded PID
        child = SimpleNamespace(parent=SimpleNamespace(name='104'), read_bytes=mock.Mock(
            return_value=b'AGENTIAC_PI_MODEL=orin1-worker\0AGENTIAC_PI_PID=103\0'))
        with mock.patch.object(Path, 'glob', return_value=[own, unreadable, vanished, pi, child]) as glob, \
                mock.patch.object(os, 'getpid', return_value=100):
            self.assertEqual(list(agent.process_envs()),
                             [{'AGENTIAC_PI_MODEL': 'orin1-worker', 'AGENTIAC_PI_PID': '103', 'OTHER': 'a=b'}])
        glob.assert_called_once_with('[0-9]*/environ')
        own.read_bytes.assert_not_called()

    def test_counts_only_local_pi_sessions_and_maps_local_coder_by_node(self):
        envs = [
            self.pi_env('orin1-worker'),
            self.pi_env('orin1-worker'),
            self.pi_env('local-coder'),
            self.pi_env('another-worker'),
            {**self.pi_env('orin2-worker'), 'PI_CODING_AGENT_DIR': str(agent.STATE.with_name('other'))},
            {'PI_CODING_AGENT_DIR': str(agent.STATE)},
            {'AGENTIAC_PI_MODEL': 'orin2-worker'},
        ]
        self.assertEqual(agent.count_local_sessions(iter(envs), ('orin1-worker', 'orin2-worker'),
                                                    {'node': 'orin2', 'source_alias': 'orin2-worker-qfn'}),
                         {'orin1-worker': 2, 'orin2-worker': 1})

    def test_maps_local_coder_by_source_alias(self):
        self.assertEqual(agent.count_local_sessions([self.pi_env('local-coder')], self.MODELS,
                                                    {'node': 'custom', 'source_alias': 'another-worker'}),
                         {'orin1-worker': 0, 'orin2-worker': 0, 'another-worker': 1})

    def test_picks_first_in_pool_on_a_tie(self):
        with mock.patch.dict(os.environ, {'AGENTIAC_LOCAL_POOL': 'orin2-worker, orin1-worker'}), \
                mock.patch.object(agent, 'target', side_effect=ValueError('unknown route')), \
                mock.patch.object(agent, 'process_envs', return_value=[self.pi_env('local-coder')]), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(agent.pick_local_alias(self.MODELS), 'orin2-worker')
        self.assertEqual(stderr.getvalue(), 'local-any: picked orin2-worker (sessions: orin2-worker=0, orin1-worker=0)\n')

    def test_picks_the_least_busy_worker(self):
        with mock.patch.dict(os.environ, clear=True), \
                mock.patch.object(agent, 'target', return_value=('routed-target', {'node': 'orin1'})), \
                mock.patch.object(agent, 'process_envs', return_value=[self.pi_env('local-coder')]), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(agent.pick_local_alias(self.MODELS), 'orin2-worker')
        self.assertEqual(stderr.getvalue(), 'local-any: picked orin2-worker (sessions: orin1-worker=1, orin2-worker=0, another-worker=0)\n')

    def test_default_pool_is_the_node_worker_models_in_declared_order(self):
        tracked = ('control-small', 'orin2-worker', 'genie-worker-gyro-m', 'orin1-worker', 'cloud-gpt-sol')
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AGENTIAC_LOCAL_POOL', None)
            self.assertEqual(agent.local_pool(tracked), ('orin2-worker', 'orin1-worker'))
            with self.assertRaisesRegex(SystemExit, 'no tracked local-infra model is named <node>-worker'):
                agent.local_pool(('control-small', 'cloud-gpt-sol'))

    def test_pool_override_requires_tracked_model_ids(self):
        with mock.patch.dict(os.environ, {'AGENTIAC_LOCAL_POOL': 'another-worker'}):
            self.assertEqual(agent.local_pool(self.MODELS), ('another-worker',))
        for override, invalid in [('orin1-worker,missing', 'missing'), ('', '')]:
            with self.subTest(override=override), mock.patch.dict(os.environ, {'AGENTIAC_LOCAL_POOL': override}):
                with self.assertRaisesRegex(SystemExit, f"AGENTIAC_LOCAL_POOL: {invalid!r} is not a tracked"):
                    agent.local_pool(self.MODELS)

    def test_resume_passes_the_new_worker_and_saved_session_to_pi(self):
        models = [{'id': alias, 'name': alias, 'contextWindow': 65536, 'maxTokens': 8192} for alias in self.MODELS]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'state'
            session = state / 'saved.jsonl'
            provider = {'models': models}
            tracked = root / 'agents/pi/models.json'
            tracked.parent.mkdir(parents=True)
            tracked.write_text(json.dumps({'providers': {'local-infra': provider}}))
            with mock.patch.object(agent, 'ROOT', root), mock.patch.object(agent, 'STATE', state), \
                    mock.patch.object(sys, 'argv', ['agent.py', '--repo', tmp, '--alias', 'local-any', '--resume']), \
                    mock.patch.object(agent, 'latest_session', return_value=(session, root)), \
                    mock.patch.object(agent, 'toplevel', return_value=root), \
                    mock.patch.object(agent, 'pick_local_alias', return_value='orin2-worker') as pick, \
                    mock.patch.object(agent, 'read_key', return_value='fake-key'), \
                    mock.patch.object(agent, 'gateway', return_value='http://gateway.invalid'), \
                    mock.patch.object(os, 'umask'), mock.patch.object(os, 'chdir'), \
                    mock.patch.object(os, 'execve') as launch, contextlib.redirect_stderr(io.StringIO()) as stderr:
                agent.main()
            pick.assert_called_once_with({model['id']: model for model in models})
            launch.assert_called_once()
            _, argv, env = launch.call_args.args
            self.assertEqual(env['AGENTIAC_PI_MODEL'], 'orin2-worker')
            self.assertEqual(env['AGENTIAC_PI_PID'], str(os.getpid()))  # execve keeps the PID
            self.assertEqual(env['PI_CODING_AGENT_DIR'], str(state))
            self.assertEqual(argv[argv.index('--model') + 1], 'orin2-worker')
            self.assertEqual(argv[argv.index('--session') + 1], str(session))
            saved = json.loads((state / 'models.json').read_text())
            self.assertEqual(saved['providers']['local-infra']['models'], [models[1]])
            self.assertIn('pi -> orin2-worker', stderr.getvalue())


class ThinkingTests(SyntheticSiteMixin, unittest.TestCase):
    GYRO = 'local-coder-strata-workerc-gyro-m-0.2.8-128k'
    PLAIN = 'no-switch'
    PLAIN_SPEC = {'source_alias': 'workerb-worker', 'node': 'workerb', 'context_window': 65536, 'max_tokens': 8192}

    @contextlib.contextmanager
    def routes_with_a_plain_target(self):
        routes = json.loads(json.dumps(self.routes))
        routes['targets']['no-switch'] = self.PLAIN_SPEC
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'local-coder.json'
            path.write_text(json.dumps(routes))
            with mock.patch.object(route, 'ROUTES', path):
                yield

    def test_default_entry_has_no_thinking_so_suite_v1_cases_keep_their_ids(self):
        self.assertEqual(agent.model_entry(self.GYRO), {
            'id': self.GYRO, 'name': 'local-coder (workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k)',
            'contextWindow': 131072, 'maxTokens': 16384, 'reasoning': False})

    def test_a_target_with_a_switch_is_described_as_reasoning_on_request(self):
        entry = agent.model_entry(self.GYRO, thinking=True)
        self.assertTrue(entry['reasoning'])
        self.assertEqual(entry['compat'], {'thinkingFormat': 'qwen-chat-template'})
        self.assertEqual([name for name, spec in self.routes['targets'].items() if 'thinking' not in spec], [])
        with self.routes_with_a_plain_target():
            self.assertFalse(agent.model_entry(self.PLAIN, thinking=True)['reasoning'])

    def test_switch_targets_start_at_their_level_and_tracked_models_keep_pi_default(self):
        _, args, banner = agent.single_model(self.GYRO, None)
        self.assertEqual(args[-2:], ['--thinking', 'medium'])
        self.assertIn('thinking medium (/thinking to change)', banner)
        self.assertEqual(agent.single_model(self.GYRO, 'high')[1][-2:], ['--thinking', 'high'])
        self.assertEqual(agent.single_model(self.GYRO, 'off')[1][-2:], ['--thinking', 'off'])
        with self.routes_with_a_plain_target():
            self.assertEqual(agent.single_model(self.PLAIN, None)[1],
                             ['--provider', 'local-infra', '--model', self.PLAIN])
        self.assertNotIn('--thinking', agent.single_model('workerb-worker', None)[1])
        self.assertEqual(agent.single_model('workerb-worker', 'low')[1][-2:], ['--thinking', 'low'])

    def test_a_starting_level_needs_a_switch_and_a_valid_level(self):
        for name, spec in self.routes['targets'].items():
            if 'thinking_start' in spec:
                self.assertIn('thinking', spec, name)
                self.assertIn(spec['thinking_start'], agent.THINKING, name)
        probe = ('probe', {'context_window': 1, 'max_tokens': 1, 'thinking': 'qwen-chat-template'})
        with mock.patch.object(agent, 'target', return_value=probe):
            self.assertEqual(agent.single_model('local-coder-probe', None)[1][-2:], ['--thinking', 'off'])

    def test_thinking_on_a_target_without_a_switch_is_refused(self):
        with self.routes_with_a_plain_target():
            with self.assertRaises(SystemExit) as raised:
                agent.single_model(self.PLAIN, 'medium')
            self.assertIn('no thinking switch', str(raised.exception))
            self.assertEqual(agent.single_model(self.PLAIN, 'off')[1][-2:], ['--thinking', 'off'])


class ResumeTests(unittest.TestCase):
    def test_latest_session_uses_saved_worktree_not_callers_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = root / 'worktree'
            saved.mkdir()
            sessions = root / 'sessions' / 'project'
            sessions.mkdir(parents=True)
            old = sessions / 'old.jsonl'
            new = sessions / 'new.jsonl'
            for path in (old, new):
                path.write_text(json.dumps({'type': 'session', 'cwd': str(saved)}) + '\n')
            os.utime(old, (100, 100))
            os.utime(new, (200, 200))
            with mock.patch.object(agent, 'toplevel', return_value=saved):
                self.assertEqual(agent.latest_session(root), (new, saved))

    def test_missing_or_invalid_latest_session_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(SystemExit, 'no saved pi sessions'):
                agent.latest_session(root)
            sessions = root / 'sessions'
            sessions.mkdir()
            (sessions / 'bad.jsonl').write_text(json.dumps({'type': 'session', 'cwd': '/not/a/worktree'}) + '\n')
            with self.assertRaisesRegex(SystemExit, 'saved worktree is missing'):
                agent.latest_session(root)


class BorrowEngineOptionTests(SyntheticSiteMixin, unittest.TestCase):
    def test_a_backend_claim_carries_the_option_into_every_load_of_the_claim(self):
        hosts = borrow.hosts()
        root, cfg = borrow.ondemand_config('workere', hosts['workere'], 30, 'strata-0.2.13-dsv41-coder-iq2_xxs', 131072,
                                           ['experts=6'])
        self.assertEqual(root, hosts['workere']['strata_trial_roots']['0.2.13'])
        self.assertEqual(cfg['start'][cfg['start'].index('--engine-option') + 1:], ['experts=6'])

    def test_a_claim_without_the_option_starts_the_root_as_the_manifest_declares_it(self):
        hosts = borrow.hosts()
        _root, cfg = borrow.ondemand_config('workere', hosts['workere'], 30, 'strata-0.2.13-dsv41-coder-iq2_xxs', 131072)
        self.assertNotIn('--engine-option', cfg['start'])


class RenderTests(SyntheticSiteMixin, unittest.TestCase):
    def test_worker_target_serves_strata_with_its_thinking_switch_and_verifies_its_manifest(self):
        count = len(self.base['model_list'])
        config, callback = route.render(self.base, self.routes, 'workerb')
        entry = config['model_list'][count]
        self.assertEqual(entry['model_name'], 'local-coder')
        self.assertEqual(entry['litellm_params']['api_base'], 'http://127.0.0.1:18102/v1')
        self.assertNotIn('extra_body', entry['litellm_params'])
        base, served, identity = callback['local-coder']
        self.assertEqual((base, served), ('http://127.0.0.1:18102', 'qwen3.8-flash-next-iq3_xxs'))
        self.assertEqual((identity['backend_id'], identity['context']), ('strata-0.2.12-qfn-gsq-rco-iq3_xxs', 262144))
        self.assertEqual(len(self.base['model_list']), count, 'base config must not be mutated')

    def test_every_target_is_also_published_under_its_own_alias(self):
        config, callback = route.render(self.base, self.routes, 'oldbox')
        names = [m['model_name'] for m in config['model_list'][len(self.base['model_list']):]]
        self.assertEqual(names, ['local-coder'] + list(self.routes['targets'])
                         + ['local-coder-' + old for old in self.routes['legacy_targets']]
                         + list(self.routes['legacy_sources']))
        by_name = {m['model_name']: m['litellm_params'] for m in config['model_list']}
        self.assertEqual(by_name['local-coder'], by_name['local-coder-oldbox'])
        self.assertEqual(by_name['local-coder-workerb'], by_name['workerb-worker'])
        base, served, identity = callback['local-coder-strata-workerc-gyro-m-0.2.8-128k']
        self.assertEqual((base, served), ('http://127.0.0.1:18115', 'qwen3.8-flash-next-gyro-gyro-m'))
        self.assertEqual(identity['backend_id'], 'strata-0.2.8-qfn-gyro-m-tq2_0')
        self.assertEqual(identity['context'], 131072)
        self.assertEqual(identity['manifest_hash'],
                         identity_policy.manifest_sha256(self.root / 'backends/strata-0.2.8-qfn-gyro-m-tq2_0.json'))

    def test_tensorfold_target_copies_source_unchanged(self):
        config, callback = route.render(self.base, self.routes, 'oldbox')
        self.assertEqual(config['model_list'][len(self.base['model_list'])]['litellm_params'],
                         self.base['model_list'][0]['litellm_params'])
        self.assertEqual(callback['local-coder'][1], 'tf114-bf16')

    def test_unknown_target_and_duplicate_alias_are_rejected(self):
        with self.assertRaises(ValueError):
            route.render(self.base, self.routes, 'workera')
        clash = {'model_list': self.base['model_list'] + [{'model_name': 'local-coder', 'litellm_params': {}}]}
        with self.assertRaises(ValueError):
            route.render(clash, self.routes, 'workerb')

    def test_tracked_base_has_every_target_source(self):
        site_graph = declared.load(SITE)
        site_hosts = site_graph.nodes
        with mock.patch.object(route, 'ROOT', SITE), \
                mock.patch.object(route, 'ROUTES', SITE / 'gateway/local-coder.json'):
            for target in site_graph.route['targets']:
                route.render(site_graph.gateway_config, site_graph.route, target)
        self.assertNotIn(site_graph.route['default'],
                         [t for t, s in site_graph.route['targets'].items() if site_hosts[s['node']].get('borrowed')])


class BorrowTests(SyntheticSiteMixin, unittest.TestCase):
    def test_only_borrowed_hosts_can_be_released(self):
        hosts = borrow.hosts()
        self.assertEqual(sorted(name for name, host in hosts.items() if host.get('borrowed')),
                         ['control', 'workerc', 'workerd', 'workere'])

    def test_dedicated_on_demand_is_persistent_but_never_released(self):
        hosts = borrow.hosts()
        self.assertEqual(sorted(name for name, host in hosts.items() if borrow.on_demand(host)),
                         ['control', 'workera', 'workerb', 'workerc', 'workerd', 'workere'])
        self.assertFalse(any(hosts[name].get('borrowed') for name in ('workera', 'workerb')))
        self.assertIn('WantedBy=multi-user.target', (ROOT / 'infra/templates/ondemand.service.j2').read_text())

    def test_a_claim_with_no_backend_resolves_the_hosts_default_manifest(self):
        hosts = borrow.hosts()
        with mock.patch.object(borrow, 'enable_ondemand') as front, mock.patch.object(borrow, 'start_tunnel'), \
                mock.patch.object(borrow, 'weight_guard'), mock.patch.object(borrow, 'warm'), \
                mock.patch.object(borrow, 'claims'), mock.patch.object(borrow, 'mutation'):
            borrow.claim('workerd', hosts['workerd'], False, 30, False)
            borrow.claim('workerc', hosts['workerc'], False, 30, False)
        self.assertEqual(front.call_args_list[0].args[3:], ('strata-0.2.8-qfn-gyro-m-tq2_0', 131072))
        self.assertEqual(front.call_args_list[1].args[3:], ('strata-0.2.11-qfn-ud-iq3_xxs', 262144))

    def test_llama_front_runs_the_launcher_admission_and_strata_its_own(self):
        hosts = borrow.hosts()
        root, config = borrow.ondemand_config('control', hosts['control'], 30)
        self.assertEqual(config['check'], ['/usr/bin/python3', f'{root}/scripts/serve-profile.py', '--check', root])
        self.assertEqual((config['listen_port'], config['backend_port'], config['idle_s']), (18106, 18090, 1800))
        self.assertNotIn('check', borrow.ondemand_config('workerc', hosts['workerc'], 30,
                                                         'strata-0.2.8-qfn-gyro-m-tq2_0', 131072)[1])

    def test_tensorfold_front_preserves_archive_and_uses_private_backend(self):
        root, config = borrow.ondemand_config('workere', borrow.hosts()['workere'], 30)
        self.assertEqual((config['listen_port'], config['backend_port'], config['unit']),
                         (18080, 18885, f'{borrow.PREFIX}-spark-tensorfold'))
        self.assertEqual(config['idle_s'], 1800)
        self.assertNotIn('check', config)
        self.assertIn('archive|cleanup', borrow.RELEASE)
        self.assertTrue(root.endswith('/service-tensorfold'))

    def test_a_claimed_front_is_told_which_manifest_it_must_serve(self):
        root, config = borrow.ondemand_config('workerc', borrow.hosts()['workerc'], 30,
                                              'strata-0.2.8-qfn-gyro-m-tq2_0', 131072)
        self.assertEqual(config['manifest_file'], f'{root}/backends/strata-0.2.8-qfn-gyro-m-tq2_0.json')
        root, config = borrow.ondemand_config('workere', borrow.hosts()['workere'], 30)
        self.assertEqual(config['manifest_file'], f'{root}/backend-manifest.json')
        llama = {k: v for k, v in borrow.hosts()['workerb'].items() if k not in ('strata_backend', 'strata_trial_roots')}
        self.assertNotIn('manifest_file', borrow.ondemand_config('workerb', llama, 30)[1])

    def test_each_dedicated_front_serves_its_strata_backend_on_its_own_port_and_key(self):
        for name, tunnel, root in (('workera', 18101, '/srv/agentiac/strata-0.2.12'),
                                   ('workerb', 18102, '/srv/agentiac/strata-0.2.12')):
            with self.subTest(name):
                host = borrow.hosts()[name]
                backend = host['strata_backend']
                got, config = borrow.ondemand_config(name, host, 30, backend, 262144)
                self.assertEqual(got, root)
                self.assertEqual((config['listen_port'], config['backend_port'], config['unit']),
                                 (18080, 18090, f'{borrow.PREFIX}-strata-inference'))
                self.assertEqual(config['key_file'], f'{root}/api-key-claim.env')
                self.assertNotIn('check', config)
                self.assertEqual(config['manifest_file'], f'{root}/backends/{backend}.json')
                self.assertEqual(config['start'][config['start'].index('--context') + 1], '262144')
                self.assertEqual(config['start'][config['start'].index('--node') + 1], name)
                self.assertTrue(borrow.serves_strata(host))
                with mock.patch.object(borrow.Path, 'read_text', autospec=True, return_value='worker-key\n') as read:
                    self.assertEqual(borrow.endpoint(name, host), (tunnel, 'worker-key'))
                self.assertEqual(read.call_args[0][0], borrow.ROOT / f'secrets/nodes/{name}.key')

    def test_each_dedicated_claim_defaults_to_its_strata_backend_at_the_prepared_context(self):
        for name, backend in (('workera', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'),
                              ('workerb', 'strata-0.2.12-qfn-gsq-rco-iq3_xxs')):
            host = borrow.hosts()[name]
            with self.subTest(name), mock.patch.object(borrow, 'enable_ondemand') as enable, \
                    mock.patch.object(borrow, 'start_tunnel'), mock.patch.object(borrow, 'weight_guard'), \
                    mock.patch.object(borrow, 'claims'), mock.patch.object(borrow, 'mutation'), \
                    mock.patch('builtins.print'):
                borrow.claim(name, host, False, 30, False)
                self.assertEqual(enable.call_args[0][3:], (backend, 262144))

    def test_tensorfold_reasoning_matches_trial(self):
        entry, args, _ = agent.single_model('local-coder-oldbox', None)
        self.assertEqual(args[-2:], ['--thinking', 'medium'])
        self.assertTrue(entry['compat']['chatTemplateKwargs']['preserve_thinking'])

    def test_control_release_never_touches_gateway_or_tunnels(self):
        script = borrow.RELEASE % borrow.CONTROL_SCOPE
        self.assertIn(f"'{borrow.PREFIX}-inference*'", script)
        self.assertNotIn(f"'{borrow.PREFIX}*'", script)
        self.assertNotIn('label=io.agent-infra.owner', script)

    def test_route_on_claim_follows_the_served_backend_and_context(self):
        self.assertEqual(borrow.claimed_target('workere', 'tf-1.1.4-qfn-mxfp8-nvfp4', 262144),
                         'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k')
        with self.assertRaisesRegex(SystemExit, 'no single local-coder target'):
            borrow.claimed_target('workerc')
        self.assertEqual(borrow.claimed_target('workerc', 'strata-0.2.8-qfn-gyro-m-tq2_0'),
                         'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')
        self.assertEqual(borrow.claimed_target('workera', 'strata-0.2.10-qfn-gsq-rco-iq3_xxs', 262144),
                         'workera-strata-0.2.10-qfn-gsq-rco-iq3_xxs-256k')
        self.assertEqual(borrow.claimed_target('workerd', 'strata-0.2.8-qfn-gyro-m-tq2_0', 131072),
                         'workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k')
        with self.assertRaisesRegex(SystemExit, 'no single local-coder target'):
            borrow.claimed_target('workerd', 'strata-0.2.8-qfn-gyro-m-tq2_0', 65536)

    def test_report_lists_leftovers_and_keeps_foreign_gpu_apps_as_text(self):
        report = borrow.parse_report(f'units=\nuser_units={borrow.PREFIX}-strata-x.service\ncontainers=abc def\ngpu_apps=123, python\n')
        self.assertEqual(report['units'], [])
        self.assertEqual(report['user_units'], [f'{borrow.PREFIX}-strata-x.service'])
        self.assertEqual(report['containers'], ['abc', 'def'])
        self.assertEqual(report['gpu_apps'], '123, python')


class SwitchBackendsOnOneHostTests(SyntheticSiteMixin, unittest.TestCase):
    HOST_FACTS = {
        'ansible_host': 'workera.example', 'backend': 'strata',
        'strata_backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs',
        'strata_trial_roots': {'0.2.10': '/srv/agentiac/strata-0.2.10',
                               '0.2.12': '/srv/agentiac/strata-0.2.12'},
        'ondemand_backend_port': 18090, 'strata_api_port': 18080, 'ondemand_persistent': True,
    }
    OLD = '/srv/agentiac/strata-0.2.12/ondemand'
    NEW = '/srv/agentiac/strata-0.2.10/ondemand'

    def setUp(self):
        super().setUp()
        self.wanted = json.dumps(borrow.ondemand_config('workera', self.HOST_FACTS, 30,
                                                        'strata-0.2.10-qfn-gsq-rco-iq3_xxs', 262144)[1],
                                 indent=2) + '\n'
        self.script = hashlib.sha256((borrow.CODE / 'scripts/ondemand-proxy.py').read_bytes()).hexdigest()

    def _on_host(self, front_directory):
        def answer(host, argv, **kwargs):
            command = argv if isinstance(argv, str) else ' '.join(argv)
            if command.startswith('cat '):
                return mock.Mock(returncode=0, stdout=self.wanted + self.script + '  -\n')
            if command.startswith(f'systemctl cat {borrow.PROXY}'):
                return mock.Mock(returncode=0, stdout=f'ExecStart=/usr/bin/python3 {front_directory}/proxy.py '
                                                      f'{front_directory}/config.json\n')
            return mock.Mock(returncode=0, stdout='', stderr='')
        return answer

    def _claim(self, front_directory):
        with mock.patch.object(borrow, 'on_host', side_effect=self._on_host(front_directory)), \
                mock.patch.object(borrow, 'prepare_backend'), mock.patch.object(borrow, 'start_watch'), \
                mock.patch.object(borrow, 'put', return_value=0), \
                mock.patch.object(borrow, 'install_front') as install:
            borrow.enable_ondemand('workera', self.HOST_FACTS, 30, 'strata-0.2.10-qfn-gsq-rco-iq3_xxs', 262144)
        return install

    def test_a_claim_of_the_other_backend_repoints_the_front(self):
        install = self._claim(self.OLD)
        install.assert_called_once_with('workera', self.HOST_FACTS, self.NEW)

    def test_a_claim_of_the_backend_already_running_reinstalls_nothing(self):
        self._claim(self.NEW).assert_not_called()

    def test_the_front_the_host_runs_is_read_from_the_unit(self):
        with mock.patch.object(borrow, 'on_host', side_effect=self._on_host(self.OLD)):
            self.assertEqual(borrow.running_front_directory(self.HOST_FACTS), self.OLD)
        with mock.patch.object(borrow, 'on_host', return_value=mock.Mock(returncode=1, stdout='')):
            self.assertIsNone(borrow.running_front_directory(self.HOST_FACTS))


class TensorFoldClaimWatchTests(SyntheticSiteMixin, unittest.TestCase):
    HOST_FACTS = {
        'ansible_host': 'workere', 'project_root': '/srv/agentiac', 'spark_tensorfold': True,
        'backend_port': 18080, 'ondemand_backend_port': 18090,
        'strata_trial_roots': {'0.2.13': '/srv/agentiac/strata-0.2.13'},
    }

    def test_the_own_tensorfold_claim_starts_no_trial_watch_and_stops_a_stale_one(self):
        backend = borrow.tensorfold_backend('workere')
        with mock.patch.object(borrow, 'on_host', return_value=mock.Mock(returncode=0, stdout='', stderr='')) as on_host, \
                mock.patch.object(borrow, 'prepare_backend'), mock.patch.object(borrow, 'start_watch') as watch, \
                mock.patch.object(borrow, 'put', return_value=0), mock.patch.object(borrow, 'install_front') as install:
            borrow.enable_ondemand('workere', self.HOST_FACTS, 30, backend, 262144)
        watch.assert_not_called()
        self.assertIn(['sudo', '-n', 'systemctl', 'stop', borrow.WATCH], [c.args[1] for c in on_host.call_args_list])
        install.assert_called_once()


class TransientUnitTests(SyntheticSiteMixin, unittest.TestCase):
    HOST_FACTS = {'ansible_host': 'workerd', 'backend': 'strata', 'strata_tunnel_port': 18114, 'strata_api_port': 18085}

    def test_front_on_a_borrowed_host_is_transient(self):
        with mock.patch.object(borrow, 'on_host', return_value=mock.Mock(returncode=0, stderr='')) as on_host, \
                mock.patch.object(borrow, 'run_playbook') as playbook:
            borrow.install_front('workerd', self.HOST_FACTS, '/r/ondemand')
        playbook.assert_not_called()
        account = declared.account(self.HOST_FACTS, self.root)
        self.assertEqual(on_host.call_args.args[1], ['sudo', '-n', 'systemd-run', f'--unit={borrow.PROXY}', f'--uid={account}',
                                                     '--property=Restart=on-failure', '--property=RestartSec=5', '/usr/bin/python3',
                                                     '/r/ondemand/proxy.py', '/r/ondemand/config.json'])

    def test_front_on_a_dedicated_host_is_the_template(self):
        with mock.patch.object(borrow, 'on_host', return_value=mock.Mock(returncode=0, stderr='')), \
                mock.patch.object(borrow, 'run_playbook') as playbook:
            borrow.install_front('workera', {'ansible_host': 'workera', 'ondemand_persistent': True}, '/r/ondemand')
        playbook.assert_called_once_with('unit', 'workera', '-e', 'unit=ondemand', '-e', 'directory=/r/ondemand')

    def test_a_strata_tunnel_is_transient(self):
        with mock.patch.object(borrow.subprocess, 'run', return_value=mock.Mock(returncode=1)) as run:
            borrow.start_tunnel('workerd', self.HOST_FACTS)
        account = declared.account(self.HOST_FACTS, self.root)
        self.assertEqual(run.call_args.args[0], [
            'sudo', '-n', 'systemd-run', f'--unit={borrow.PREFIX}-tunnel-workerd', f'--uid={account}', '--property=Restart=on-failure',
            '--property=RestartSec=10', '/usr/bin/ssh', '-x', '-NT', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=3', '-L', '127.0.0.1:18114:127.0.0.1:18085', f'{account}@workerd'])


class PreparedManifestTests(SyntheticSiteMixin, unittest.TestCase):
    HOST_FACTS = {'ansible_host': 'workere', 'project_root': '/srv/agentiac', 'spark_tensorfold': True, 'backend_port': 18080}

    def test_the_tensorfold_claim_pushes_its_manifest(self):
        root = borrow.tensorfold_root(self.HOST_FACTS)
        _config = borrow.ondemand_config('workere', self.HOST_FACTS, 30)[1]
        with mock.patch.object(borrow, 'on_host', return_value=mock.Mock(returncode=0)), \
                mock.patch.object(borrow, 'put', return_value=0) as put, mock.patch.object(borrow, 'run_playbook'):
            borrow.prepare_backend('workere', self.HOST_FACTS, _config, 'tf-1.1.4-qfn-mxfp8-nvfp4')
        pushed = {str(source): dest for (_host, source, dest), _ in put.call_args_list}
        self.assertEqual(pushed[str(self.root / 'backends/tf-1.1.4-qfn-mxfp8-nvfp4.json')], f'{root}/backend-manifest.json')
        self.assertEqual(_config['manifest_file'], f'{root}/backend-manifest.json')


class ReclaimTests(SyntheticSiteMixin, unittest.TestCase):
    def reclaim(self, target):
        hosts = {
            'workerd': {'backend': 'strata', 'borrowed': True, 'idle_unload_minutes': 30},
            'workere': {'borrowed': True, 'idle_unload_minutes': 30},
            'workerb': {'ondemand_persistent': True, 'idle_unload_minutes': 30},
            'workera': {},
            'workerc': {'backend': 'strata', 'borrowed': True, 'idle_unload_minutes': 30},
        }
        with mock.patch.object(borrow.route, 'current', return_value=target), mock.patch.object(borrow, 'claim') as claim:
            borrow.reclaim(hosts)
        return claim.call_args.args

    def test_the_route_entry_says_what_to_serve_and_the_route_stays(self):
        args = self.reclaim('workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k')
        self.assertEqual(args[0], 'workerd')
        self.assertEqual(args[2:], (False, 30, True, 'strata-0.2.8-qfn-gyro-m-tq2_0', 131072))

    def test_a_target_serves_what_it_declares(self):
        self.assertEqual(self.reclaim('workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k')[2:],
                         (False, 30, True, 'tf-1.1.4-qfn-mxfp8-nvfp4', 262144))
        self.assertEqual(self.reclaim('workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k')[2:],
                         (False, 30, True, 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 262144))
        self.assertEqual(self.reclaim('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')[2:],
                         (False, 30, True, 'strata-0.2.8-qfn-gyro-m-tq2_0', 131072))


class ThinRootTests(SyntheticSiteMixin, unittest.TestCase):
    BACKEND = 'strata-0.2.8-qfn-gyro-m-tq2_0'
    TARGET = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'
    ROOT_ON_HOST = '/srv/agentiac/strata-0.2.8'
    WEIGHT = 'gyro-Gyro-M/Qwen3.8-Flash-Next-Gyro-M-TQ2_0.gguf'

    def ledger(self, form):
        one = {host: {state: [dict(entry) for entry in entries] if isinstance(entries, list) else entries
                      for state, entries in block.items()}
               for host, block in declared.load(self.root).retention.items()}
        for entry in one['workerc']['rollback']:
            if entry.get('backend') == self.BACKEND:
                entry['form'] = form
        return one

    def test_the_shipped_ledger_routes_a_full_rollback(self):
        self.assertIsNone(route.thin_target(self.TARGET))

    def test_route_set_refuses_a_thin_target_and_names_the_way_out(self):
        with mock.patch.object(route.declared, 'load', return_value=SimpleNamespace(retention=self.ledger('thin'))):
            block = route.thin_target(self.TARGET)
        self.assertIsNotNone(block)
        self.assertIn('is `form: thin`', block)
        self.assertIn(f'{errors.command()} ops restore workerc --backend {self.BACKEND}', block)

    def test_the_refusal_stops_route_set_before_the_route_is_written(self):
        with mock.patch.object(route.declared, 'load', return_value=SimpleNamespace(retention=self.ledger('thin'))), \
             mock.patch.object(route, 'switch_to') as switched, \
             mock.patch.object(route, 'mutation'), \
             mock.patch.object(sys, 'argv', ['route.py', 'set', self.TARGET]):
            with self.assertRaises(SystemExit) as got:
                route.main()
        self.assertIn('route unchanged', str(got.exception))
        self.assertEqual(switched.call_count, 0)

    def claim(self, retention, answer):
        hosts = borrow.hosts()
        with mock.patch.object(borrow.declared, 'load', return_value=SimpleNamespace(retention=retention)), \
             mock.patch.object(borrow, 'enable_ondemand') as front, mock.patch.object(borrow, 'start_tunnel'), \
             mock.patch.object(borrow, 'warm'), mock.patch('builtins.print'), \
             mock.patch.object(borrow, 'claims'), mock.patch.object(borrow, 'mutation'), \
             mock.patch.object(borrow, 'on_host', return_value=mock.Mock(returncode=0, stdout=answer, stderr='')) \
                as on_host:
            try:
                borrow.claim('workerc', hosts['workerc'], False, 30, False, self.BACKEND, 131072)
                return None, front.call_count, on_host.call_args_list
            except SystemExit as refusal:
                return str(refusal), front.call_count, on_host.call_args_list

    def test_a_thin_backend_is_refused_from_the_ledger_alone_before_anything_starts(self):
        refusal, fronts, reads = self.claim(self.ledger('thin'), '')
        self.assertIn('ops restore workerc --backend strata-0.2.8-qfn-gyro-m-tq2_0', refusal)
        self.assertEqual(fronts, 0)
        self.assertEqual(reads, [])

    def test_a_weight_the_host_does_not_hold_is_refused_by_name_and_size(self):
        refusal, fronts, reads = self.claim(self.ledger('full'), f'missing {self.ROOT_ON_HOST}/data/models/'
                                                                 f'{self.WEIGHT}\n')
        self.assertIn(self.WEIGHT, refusal)
        self.assertIn('91965774272 B', refusal)
        self.assertIn('ops restore workerc --backend strata-0.2.8-qfn-gyro-m-tq2_0', refusal)
        self.assertEqual(fronts, 0)
        self.assertIn(self.WEIGHT, ' '.join(reads[0].args[1]))

    def test_a_root_that_holds_its_weight_claims_as_it_always_did(self):
        refusal, fronts, _ = self.claim(self.ledger('full'), f'present {self.ROOT_ON_HOST}/data/models/'
                                                              f'{self.WEIGHT}\n')
        self.assertIsNone(refusal)
        self.assertEqual(fronts, 1)

    def test_the_host_is_asked_for_the_pinned_size_and_not_only_the_name(self):
        refusal, fronts, reads = self.claim(self.ledger('full'), f'missing {self.ROOT_ON_HOST}/data/models/'
                                                                 f'{self.WEIGHT}\n')
        self.assertIn(f'{self.ROOT_ON_HOST}/data/models/{self.WEIGHT}', reads[0].args[1])
        self.assertIn('91965774272', reads[0].args[1])
        self.assertEqual(fronts, 0)


class SmokeTests(unittest.TestCase):
    def test_streamed_tool_call_is_joined(self):
        lines = ['data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [{'function': {'name': 'read_file', 'arguments': ''}}]}}]}),
                 'data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [{'function': {'arguments': '{"path": "READ'}}]}}]}),
                 'data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [{'function': {'arguments': 'ME.md"}'}}]}}]}),
                 'data: [DONE]']
        self.assertEqual(smoke.streamed_tool_call('\n'.join(lines)), ('read_file', '{"path": "README.md"}'))


class CodeKeyTests(unittest.TestCase):
    def test_the_cloud_key_only_where_the_cloud_alias_is_reachable(self):
        self.assertEqual(agent.code_key_name('local-coder', False), 'LITELLM_CODE_KEY')
        self.assertEqual(agent.code_key_name('router', False), 'LITELLM_CODE_KEY')
        self.assertEqual(agent.code_key_name('router', True), 'LITELLM_CODE_CLOUD_KEY')
        self.assertEqual(agent.code_key_name('cloud-gpt-sol', False), 'LITELLM_CODE_CLOUD_KEY')

    def test_the_agent_never_passes_the_master_key_and_points_pi_at_the_code_key(self):
        source = (ROOT / 'scripts/agent.py').read_text()
        self.assertNotIn('LITELLM_MASTER_KEY=', source)
        self.assertIn("'${LITELLM_CODE_KEY}'", source)


if __name__ == '__main__':
    unittest.main()
