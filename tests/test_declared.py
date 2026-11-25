"""declared.py reads the declared files as one graph and `make check` fails on a dangling reference.

The first test is the standing one: the shipped graph is sound. The rest mutate a copy
of the declared files and require the exact dangling reference back.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import declared, paths  # noqa: E402

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
                     contexts=None, served_model=None, model_repo='synth/Model-GGUF', extra=None):
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
        'model_repository': model_repo,
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
    """Build a self-contained 6-node synthetic site satisfying declared.references() == []."""
    for rel in ('infra', 'profiles', 'backends', 'gateway', 'agents/pi', 'artifacts/lifecycle'):
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel in ('infra/retention.schema.json', 'profiles/schema.json', 'backends/schema.json'):
        (root / rel).write_text((ROOT / rel).read_text())

    inventory = {
        'all': {
            'vars': {
                'ansible_user': 'operator',
                'project_root': '/srv/agentiac',
                'backend_port': 18080,
                'ondemand_backend_port': 18090,
                'idle_unload_minutes': 30,
                'nodejs_binary': '/usr/bin/node',
                'rclone_config_dir': '/srv/agentiac/rclone',
                'backup_destination': 'remote:backups',
                'gateway_port': 14000,
                'chatgpt_port': 14001,
                'resource_prefix': 'agentiac',
            },
            'children': {'workers': {'hosts': {
                'workera': {
                    'ansible_host': 'workera.example', 'node_class': 'orin', 'cuda_arch': '87',
                    'ondemand_persistent': True, 'default_profile': 'small-q6',
                    'gateway_alias': 'workera-worker', 'tunnel_port': 18101,
                    'backend_port': 18080, 'ondemand_backend_port': 18090,
                    'strata_backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                    'strata_trial_roots': {
                        '0.2.10': '/srv/agentiac/strata-0.2.10',
                        '0.2.12': '/srv/agentiac/strata-0.2.12',
                    },
                },
                'workerb': {
                    'ansible_host': 'workerb.example', 'node_class': 'orin', 'cuda_arch': '87',
                    'ondemand_persistent': True, 'default_profile': 'small-q6',
                    'gateway_alias': 'workerb-worker', 'tunnel_port': 18102,
                    'backend_port': 18080, 'ondemand_backend_port': 18090,
                    'strata_backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
                    'strata_trial_roots': {
                        '0.2.10': '/srv/agentiac/strata-0.2.10',
                        '0.2.12': '/srv/agentiac/strata-0.2.12',
                    },
                },
                'workerc': {
                    'ansible_host': 'workerc.example', 'node_class': 'rtx5090', 'borrowed': True,
                    'backend': 'strata', 'inference_unit': 'agentiac-strata-inference',
                    'strata_api_port': 18085, 'strata_tunnel_port': 18115,
                    'ondemand_backend_port': 18090,
                    'strata_default_backend': 'strata-0.2.11-qfn-ud-iq3_xxs',
                    'strata_trial_roots': {
                        '0.2.8': '/srv/agentiac/strata-0.2.8',
                        '0.2.11': '/srv/agentiac/strata-0.2.11',
                        'strata-0.2.11-qfn-gyro-m-tq2_0': '/srv/agentiac/strata-0.2.11-gyro',
                    },
                },
                'workerd': {
                    'ansible_host': 'workerd.example', 'node_class': 'rtx5090', 'borrowed': True,
                    'backend': 'strata', 'inference_unit': 'agentiac-strata-inference',
                    'strata_api_port': 18085, 'strata_tunnel_port': 18114,
                    'ondemand_backend_port': 18090,
                    'strata_default_backend': 'strata-0.2.8-qfn-gyro-m-tq2_0',
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
            IQ3_FILES, ['workera', 'workerb'], context=262144, served_model='qwen3.8-flash-next-iq3_xxs',
            model_repo='synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF'),
        'strata-0.2.12-qfn-gsq-rco-iq3_xxs': _strata_manifest(
            'strata-0.2.12-qfn-gsq-rco-iq3_xxs', '0.2.12', 'qfn', 'Qwen/Qwen3.8-Flash-Next',
            IQ3_FILES, ['workera', 'workerb'], context=262144, served_model='qwen3.8-flash-next-iq3_xxs',
            model_repo='synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF'),
        'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2': _strata_manifest(
            'strata-0.2.12-qfn-gsq-rco-iq3_xxs-dflash2', '0.2.12', 'qfn', 'Qwen/Qwen3.8-Flash-Next',
            IQ3_FILES, ['workerb'], context=262144, served_model='qwen3.8-flash-next-iq3_xxs',
            model_repo='synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF',
            extra={
                'workers': {'workerb': {'cuda_arch': '87', 'drafter': 'dflash2'}},
                'drafter_pack': {
                    'drafter': 'dflash2',
                    'path': 'dflash2/model-dflash2.q4_0.pack',
                    'size': 522458112,
                    'sha256': 'f' * 64,
                    'source_repository': 'synth/Model-DFlash2',
                },
            }),
        'strata-0.2.8-qfn-gyro-m-tq2_0': _strata_manifest(
            'strata-0.2.8-qfn-gyro-m-tq2_0', '0.2.8', 'qfn', 'Gyro-M',
            GYRO_FILES, ['workerc', 'workerd'], context=131072, contexts=[65536, 131072],
            served_model='qwen3.8-flash-next-gyro-gyro-m', model_repo='synth/Qwen3.8-Flash-Next-Gyro-GGUF',
            extra={'variant': 'Gyro-M'}),
        'strata-0.2.11-qfn-gyro-m-tq2_0': _strata_manifest(
            'strata-0.2.11-qfn-gyro-m-tq2_0', '0.2.11', 'qfn', 'Gyro-M',
            GYRO_FILES, ['workerc', 'workerd'], context=131072, contexts=[65536, 131072],
            served_model='qwen3.8-flash-next-gyro-gyro-m', model_repo='synth/Qwen3.8-Flash-Next-Gyro-GGUF',
            extra={'variant': 'Gyro-M'}),
        'strata-0.2.11-qfn-ud-iq3_xxs': _strata_manifest(
            'strata-0.2.11-qfn-ud-iq3_xxs', '0.2.11', 'qfn', 'UD-IQ3_XXS',
            UD_FILES, ['workerc'], context=262144, served_model='qwen3.8-flash-next-unsloth-ud-iq3_xxs',
            model_repo='synth/Qwen3.8-Flash-Next-GGUF'),
        'strata-0.2.13-dsv41-coder-iq2_xxs': _strata_manifest(
            'strata-0.2.13-dsv41-coder-iq2_xxs', '0.2.13', 'deepseek', 'DeepSeek-V4.1-Flash',
            DS_FILES, ['workere'], context=131072, served_model='deepseek-v4.1-flash',
            model_repo='synth/DeepSeek-V4.1-Flash-1017-GSQ-RCO-Coder-GGUF'),
        'tf-1.1.4-qfn-mxfp8-nvfp4': {
            'id': 'tf-1.1.4-qfn-mxfp8-nvfp4',
            'engine': {'repository': 'https://example.invalid/TensorFold.git', 'version': '1.1.4',
                       'revision': '3' * 40},
            'model': {'repository': 'nvidia/Qwen3.8-Flash-Next-FP4-Local', 'revision': '4' * 40,
                      'directory': 'evaluations/tf-flash/weights-rev1'},
            'quantization': 'mixed-MXFP8-NVFP4',
            'family': 'qfn',
            'kv_cache_type': 'bf16',
            'class_args': ['--temperature=0'],
            'config_args': ['--parallel=1'],
            'workers': {'workere': {'slots': 1}},
        },
    }
    for stem, spec in backends.items():
        (root / f'backends/{stem}.json').write_text(json.dumps(spec))
    (root / 'backends/tf-1.1.4-qfn-mxfp8-nvfp4.launch.json').write_text(json.dumps(['--flag']))

    components = [
        {
            'id': 'strata-workera', 'kind': 'engine', 'owner': 'project', 'hosts': ['workera'],
            'source': {'type': 'github', 'repository': 'synth/Strata'},
            'pin': {'version': '0.2.12', 'revision': '1' * 40,
                    'lock': 'backends/strata-0.2.12-qfn-gsq-rco-iq3_xxs.json'},
            'policy': 'manual', 'gate': 'staged upgrade',
        },
        {
            'id': 'strata-workerb', 'kind': 'engine', 'owner': 'project', 'hosts': ['workerb'],
            'source': {'type': 'github', 'repository': 'synth/Strata'},
            'pin': {'version': '0.2.12', 'revision': '1' * 40,
                    'lock': 'backends/strata-0.2.12-qfn-gsq-rco-iq3_xxs.json'},
            'policy': 'manual', 'gate': 'staged upgrade',
        },
        {
            'id': 'pi', 'kind': 'agent', 'owner': 'project', 'hosts': ['control'],
            'source': {'type': 'npm', 'package': '@mariozechner/pi-coding-agent'},
            'pin': {'version': '1.3.0', 'lock': 'agents/pi/package-lock.json'},
            'policy': 'notify',
        },
    ]
    (root / 'agents/pi/package-lock.json').write_text(json.dumps({
        'packages': {'node_modules/@mariozechner/pi-coding-agent': {'version': '1.3.0'}}
    }))
    (root / 'infra/components.yml').write_text(yaml.safe_dump(components, sort_keys=False))

    agents = [
        {'id': 'pi', 'owner': 'project', 'hosts': ['control'], 'pin': {'version': '1.3.0'}},
        {'id': 'claude-code', 'owner': 'project', 'hosts': ['control'], 'pin': {'version': '2.1.0'}},
        {'id': 'codex', 'owner': 'project', 'hosts': ['control'], 'pin': {'version': '0.1.0'}},
    ]
    (root / 'agents/agents.yml').write_text(yaml.safe_dump(agents, sort_keys=False))

    gateway_config = {
        'model_list': [
            {'model_name': 'workera-worker', 'litellm_params': {'model': 'openai/qwen3.8-flash-next-iq3_xxs', 'api_base': 'http://127.0.0.1:18101/v1'}},
            {'model_name': 'workerb-worker', 'litellm_params': {'model': 'openai/qwen3.8-flash-next-iq3_xxs', 'api_base': 'http://127.0.0.1:18102/v1'}},
            {'model_name': 'workerc-worker-gyro-m', 'litellm_params': {'model': 'openai/qwen3.8-flash-next-gyro-gyro-m', 'api_base': 'http://127.0.0.1:18115/v1'}},
            {'model_name': 'workerc-worker-ud-iq3xxs', 'litellm_params': {'model': 'openai/qwen3.8-flash-next-unsloth-ud-iq3_xxs', 'api_base': 'http://127.0.0.1:18115/v1'}},
            {'model_name': 'workerd-worker-gyro-m', 'litellm_params': {'model': 'openai/qwen3.8-flash-next-gyro-gyro-m', 'api_base': 'http://127.0.0.1:18114/v1'}},
            {'model_name': 'workere-worker-qfn', 'litellm_params': {'model': 'openai/tf114-bf16', 'api_base': 'http://127.0.0.1:18103/v1'}},
            {'model_name': 'workere-worker-dsv41', 'litellm_params': {'model': 'openai/deepseek-v4.1-flash', 'api_base': 'http://127.0.0.1:18103/v1'}},
            {'model_name': 'control-small', 'litellm_params': {'model': 'openai/small', 'api_base': 'http://127.0.0.1:18106/v1'}},
            {'model_name': 'cloud-alias', 'litellm_params': {'model': 'litellm_proxy/cloud', 'api_base': 'http://127.0.0.1:19999/v1'}},
        ],
        'router_settings': {'fallbacks': []},
    }
    (root / 'gateway/config.yaml').write_text(yaml.safe_dump(gateway_config, sort_keys=False))

    route_targets = {
        'workera-strata-0.2.10-qfn-gsq-rco-iq3_xxs-256k': {
            'source_alias': 'workera-worker', 'node': 'workera', 'backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs',
            'context_window': 262144, 'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'off',
        },
        'workera-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k': {
            'source_alias': 'workera-worker', 'node': 'workera', 'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
            'context_window': 262144, 'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'off',
        },
        'workerb-strata-0.2.10-qfn-gsq-rco-iq3_xxs-256k': {
            'source_alias': 'workerb-worker', 'node': 'workerb', 'backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs',
            'context_window': 262144, 'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'off',
        },
        'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k': {
            'source_alias': 'workerb-worker', 'node': 'workerb', 'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
            'context_window': 262144, 'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'off',
        },
        'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
            'source_alias': 'workerc-worker-gyro-m', 'node': 'workerc', 'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0',
            'context_window': 131072, 'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'medium',
        },
        'workerc-strata-0.2.11-qfn-gyro-m-tq2_0-128k': {
            'source_alias': 'workerc-worker-gyro-m', 'node': 'workerc', 'backend': 'strata-0.2.11-qfn-gyro-m-tq2_0',
            'context_window': 131072, 'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'medium',
        },
        'workerc-strata-0.2.11-qfn-ud-iq3_xxs-256k': {
            'source_alias': 'workerc-worker-ud-iq3xxs', 'node': 'workerc', 'backend': 'strata-0.2.11-qfn-ud-iq3_xxs',
            'context_window': 262144, 'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'off',
        },
        'workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
            'source_alias': 'workerd-worker-gyro-m', 'node': 'workerd', 'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0',
            'context_window': 131072, 'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'medium',
        },
        'workerd-strata-0.2.11-qfn-gyro-m-tq2_0-128k': {
            'source_alias': 'workerd-worker-gyro-m', 'node': 'workerd', 'backend': 'strata-0.2.11-qfn-gyro-m-tq2_0',
            'context_window': 131072, 'max_tokens': 16384, 'thinking': 'qwen-chat-template', 'thinking_start': 'medium',
        },
        'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k': {
            'source_alias': 'workere-worker-qfn', 'node': 'workere', 'backend': 'tf-1.1.4-qfn-mxfp8-nvfp4',
            'context_window': 262144, 'max_tokens': 16384, 'thinking': 'qwen-chat-template',
            'thinking_start': 'medium', 'preserve_thinking': True,
        },
        'workere-strata-0.2.13-dsv41-coder-iq2_xxs-128k': {
            'source_alias': 'workere-worker-dsv41', 'node': 'workere', 'backend': 'strata-0.2.13-dsv41-coder-iq2_xxs',
            'context_window': 131072, 'max_tokens': 8192, 'thinking': 'qwen-chat-template', 'thinking_start': 'off',
        },
    }
    route_spec = {
        'version': 2,
        'alias': 'local-coder',
        'default': 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
        'targets': route_targets,
        'legacy_targets': {
            'workerb': 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
            'oldbox': 'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k',
            'strata-workerc-gyro-m-0.2.8-128k': 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k',
        },
        'legacy_sources': {
            'oldbox-tensorfold': 'workere-worker-qfn',
        },
    }
    (root / 'gateway/local-coder.json').write_text(json.dumps(route_spec, indent=2))

    (root / 'agents/pi/models.json').write_text(json.dumps({
        'providers': {'local-infra': {'models': [{'id': 'local-coder'}, {'id': 'workerb-worker'}]}}
    }))
    (root / 'agents/pi/router.json').write_text(json.dumps({
        'low': 'local-coder', 'high': 'cloud-alias', 'cloud_repos': ['synth/repo'],
    }))

    retention = {
        'workera': {
            'note': 'no N-1 note before 0.2.10',
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
            'candidate': [{'backend': 'strata-0.2.13-dsv41-coder-iq2_xxs',
                           'path': '/srv/agentiac/strata-0.2.13',
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
    (root / 'infra/retention.yml').write_text(yaml.safe_dump(retention, sort_keys=False))
    return root


class Mutated(unittest.TestCase):
    """Base: run a test against a private copy of the synthetic declared files."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = write_synthetic_site(Path(directory.name))

    def problems(self):
        return declared.references(declared.load(self.root))

    def edit_json(self, name, mutate):
        self.edit(name, json.loads, json.dumps, mutate)

    def edit(self, name, reads, writes, mutate):
        path = self.root / name
        spec = reads(path.read_text())
        mutate(spec)
        path.write_text(writes(spec))


class GraphTests(Mutated):
    def test_the_shipped_graph_has_no_dangling_reference(self):
        self.assertEqual(declared.references(declared.load(SITE)), [])

    def test_load_reads_every_declared_group(self):
        graph = declared.load(self.root)
        self.assertEqual(set(graph.nodes), {'workera', 'workerb', 'workerc', 'workerd', 'workere', 'control'})
        self.assertIn('small-q6', graph.profiles)
        self.assertIn('strata-0.2.8-qfn-gyro-m-tq2_0', graph.backends)
        self.assertEqual(graph.route['alias'], 'local-coder')
        self.assertIn('workere-worker-qfn', graph.gateway_models)
        self.assertIn('pi', [component['id'] for component in graph.components])
        self.assertEqual([entry['id'] for entry in graph.agents], ['pi', 'claude-code', 'codex'])

    def test_a_launch_argument_list_is_not_a_referenceable_backend(self):
        self.assertNotIn('tf-1.1.4-qfn-mxfp8-nvfp4.launch', declared.load(self.root).backends)

    def test_the_engine_a_dedicated_worker_is_told_to_serve_is_pinned_for_that_worker(self):
        graph = declared.load(self.root)
        self.assertEqual([problem for problem in declared.references(graph) if 'strata_backend' in problem], [])
        components = {component['id']: component for component in graph.components}
        for name, version, hosts, lock in (
            ('strata-workera', '0.2.12', ['workera'], 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'),
            ('strata-workerb', '0.2.12', ['workerb'], 'strata-0.2.12-qfn-gsq-rco-iq3_xxs'),
        ):
            entry = components[name]
            with self.subTest(name):
                self.assertEqual((entry['pin']['version'], sorted(entry['hosts'])), (version, hosts))
                self.assertEqual(entry['pin']['lock'], f'backends/{lock}.json')
                self.assertEqual(graph.nodes[hosts[0]]['strata_backend'], lock)
                self.assertEqual(graph.backends[lock]['source_version'], version)
                self.assertEqual(graph.backends[lock]['source_revision'], entry['pin']['revision'])


class DanglingReferenceTests(Mutated):
    def test_a_route_target_on_an_undeclared_host(self):
        self.edit_json('gateway/local-coder.json',
                       lambda r: r['targets']['workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k'].__setitem__('node', 'workerd3'))
        self.assertIn('gateway/local-coder.json target workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k node: workerd3 is not declared',
                      self.problems())

    def test_a_route_target_reading_an_alias_the_gateway_does_not_route(self):
        self.edit_json('gateway/local-coder.json',
                       lambda r: r['targets']['workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k'].__setitem__('source_alias', 'oldbox-tensorfal'))
        self.assertIn('gateway/local-coder.json target workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k source_alias: oldbox-tensorfal is not declared',
                      self.problems())

    def test_a_route_target_naming_a_backend_that_is_not_there(self):
        self.edit_json('gateway/local-coder.json',
                       lambda r: r['targets']['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'].__setitem__('backend', 'strata-qwen38-gyro-m-0.2.9'))
        self.assertIn('gateway/local-coder.json target workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k backend: '
                      'strata-qwen38-gyro-m-0.2.9 is not declared', self.problems())

    def test_a_default_that_is_not_a_target(self):
        self.edit_json('gateway/local-coder.json', lambda r: r.__setitem__('default', 'workera'))
        self.assertIn('gateway/local-coder.json default: workera is not declared', self.problems())

    def test_a_host_pointing_at_a_profile_that_was_removed(self):
        (self.root / 'profiles/small-q6.json').unlink()
        self.assertIn('infra/inventory.yml host workerb default_profile: small-q6 is not declared', self.problems())

    def test_a_host_alias_the_gateway_does_not_route(self):
        self.edit('infra/inventory.yml', yaml.safe_load, yaml.safe_dump,
                  lambda i: i['all']['children']['workers']['hosts']['workera']
                  .__setitem__('gateway_alias', 'workera-worker-old'))
        self.assertIn('infra/inventory.yml host workera gateway_alias: workera-worker-old is not declared', self.problems())

    def test_a_profile_claiming_a_node_class_no_host_has(self):
        self.edit_json('profiles/small-q6.json', lambda p: p.__setitem__('nodes', ['orin', 'thor']))
        self.assertIn('profiles/small-q6.json node class: thor is not declared', self.problems())

    def test_a_backend_manifest_served_to_a_host_that_is_not_one(self):
        self.edit_json('backends/strata-0.2.8-qfn-gyro-m-tq2_0.json', lambda b: b['workers'].__setitem__('workerc2', {'cuda_arch': '89'}))
        self.assertIn('backends/strata-0.2.8-qfn-gyro-m-tq2_0.json worker: workerc2 is not declared', self.problems())

    def test_a_worker_naming_an_engine_version_nothing_pins_for_it(self):
        self.edit('infra/inventory.yml', yaml.safe_load, yaml.safe_dump,
                  lambda i: i['all']['children']['workers']['hosts']['workerb']
                  .__setitem__('strata_backend', 'strata-qwen38-iq3xxs-0.2.11'))
        self.assertIn('infra/inventory.yml host workerb strata_backend: strata-qwen38-iq3xxs-0.2.11 is not declared',
                      self.problems())

    def test_the_inventory_moving_a_worker_default_away_from_the_ledger_is_a_broken_reference(self):
        self.edit('infra/inventory.yml', yaml.safe_load, yaml.safe_dump,
                  lambda i: i['all']['children']['workers']['hosts']['workerc']
                  .__setitem__('strata_default_backend', 'strata-0.2.8-qfn-gyro-m-tq2_0'))
        self.assertIn('infra/retention.yml workerc serving: a claim there resolves to strata-0.2.8-qfn-gyro-m-tq2_0, '
                      'the ledger names strata-0.2.11-qfn-ud-iq3_xxs', self.problems())

    def test_a_worker_serving_a_build_no_component_declares(self):
        self.edit_json('backends/strata-0.2.12-qfn-gsq-rco-iq3_xxs.json', lambda b: b['workers'].pop('workerb'))
        self.assertIn('infra/inventory.yml host workerb strata_backend: strata-0.2.12-qfn-gsq-rco-iq3_xxs is not declared',
                      self.problems())

    def test_pi_tracking_a_model_nothing_can_call(self):
        self.edit_json('agents/pi/models.json',
                       lambda m: m['providers']['local-infra']['models'].append({'id': 'missing-coder'}))
        self.assertIn('agents/pi/models.json model: missing-coder is not declared', self.problems())

    def test_the_router_naming_an_alias_that_is_not_rendered_or_routed(self):
        self.edit_json('agents/pi/router.json', lambda r: r.__setitem__('high', 'cloud-alias-missing'))
        self.assertIn('agents/pi/router.json high: cloud-alias-missing is not declared', self.problems())

    def test_a_rendered_route_alias_is_callable_even_though_config_yaml_does_not_list_it(self):
        self.edit_json('agents/pi/router.json', lambda r: r.__setitem__('low', 'local-coder-oldbox'))
        self.assertEqual(self.problems(), [])

    def test_default_backend_must_exist_and_name_the_host_and_patches_must_match_sha256(self):
        import hashlib
        self.edit('infra/inventory.yml', yaml.safe_load, yaml.safe_dump,
                  lambda i: i['all']['children']['workers']['hosts']['workerc'].__setitem__('default_backend', 'missing-b'))
        self.assertIn('infra/inventory.yml host workerc default_backend: missing-b is not declared', self.problems())

        self.edit('infra/inventory.yml', yaml.safe_load, yaml.safe_dump,
                  lambda i: i['all']['children']['workers']['hosts']['workerc'].__setitem__('default_backend', 'tf-1.1.4-qfn-mxfp8-nvfp4'))
        self.assertIn('infra/inventory.yml host workerc default_backend: backends/tf-1.1.4-qfn-mxfp8-nvfp4.json does not name workerc in workers',
                      self.problems())

        # Restore valid inventory before checking patches.
        self.edit('infra/inventory.yml', yaml.safe_load, yaml.safe_dump,
                  lambda i: i['all']['children']['workers']['hosts']['workerc'].pop('default_backend', None))
        patch_file = self.root / 'backends/patches/fix.patch'
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        patch_file.write_text('diff --git a/a b/a\n')
        good_sha = hashlib.sha256(patch_file.read_bytes()).hexdigest()
        self.edit_json('backends/tf-1.1.4-qfn-mxfp8-nvfp4.json',
                       lambda b: b.__setitem__('patches', [{'path': 'backends/patches/fix.patch', 'sha256': good_sha}]))
        self.assertEqual(self.problems(), [])

        patch_file.write_text('modified\n')
        self.assertTrue(any('does not match' in p for p in self.problems()))
        patch_file.unlink()
        self.assertTrue(any('file is missing' in p for p in self.problems()))


class FutureFileTests(Mutated):
    """A checkout without components.yml or agents.yml still reads, and their hosts are references."""

    def write_yaml(self, name, text):
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(text)

    def test_components_are_optional_and_their_hosts_are_references(self):
        self.write_yaml('infra/components.yml',
                        '- {id: pi, kind: agent, owner: project, hosts: [control], policy: notify}\n'
                        '- {id: ghost, kind: tool, owner: user, hosts: [nowhere], policy: frozen}\n')
        graph = declared.load(self.root)
        self.assertEqual([c['id'] for c in graph.components], ['pi', 'ghost'])
        self.assertIn('infra/components.yml component ghost: nowhere is not declared', declared.references(graph))

    def test_agents_are_optional_and_their_hosts_are_references(self):
        self.write_yaml('agents/agents.yml',
                        '- {id: claude-code, owner: project, hosts: [control], pin: {version: 2.1.316}}\n')
        self.assertEqual(declared.references(declared.load(self.root)), [])
        self.write_yaml('agents/agents.yml', '- {id: claude-code, owner: project, hosts: [workere2]}\n')
        self.assertIn('agents/agents.yml agent claude-code: workere2 is not declared',
                      declared.references(declared.load(self.root)))


if __name__ == '__main__':
    unittest.main()
