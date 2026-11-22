"""`model list`, `model show`, `model compatibility`, and `model recommend`."""
import argparse
import copy
import datetime
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import catalog, command, declared, evidence, hardware, identities, paths, route_aliases, state  # noqa: E402

SITE = paths.root()
PI_VERSION = '1.3.0'
START = datetime.datetime(1970, 1, 30, 14, 0, tzinfo=datetime.timezone.utc)

NODE_PROBES = {
    'os': {'stdout': 'workere\nLinux workere 7.0.0-1019-nvidia #19 SMP aarch64 aarch64 GNU/Linux\n'
                     'PRETTY_NAME="Ubuntu 24.04.5 LTS"\n'},
    'resources': {'stdout': 'Mem:     130661138432 103423053824  7503802368\n'},
    'cpu_facts': {'stdout': 'arch: aarch64\ncpus: 20\nsocid: jep106:0426:8901\nsocfamily: jep106\n'},
    'gpu_facts': {'stdout': 'NVIDIA GB10, 580.178.04, [N/A]\n'},
    'gpu_list': {'stdout': 'GPU 0: NVIDIA GB10 (UUID: GPU-1)\n'},
    'gpu': {'stdout': '| Driver Version: 580.178.04  |  CUDA Version: 13.0 |\n'},
    'cuda': {'stdout': 'Cuda compilation tools, release 13.0, V13.0.48\n'},
}


def probes_with_memory(gib):
    probes = copy.deepcopy(NODE_PROBES)
    total = int(gib * 2 ** 30)
    probes['resources'] = {'stdout': f'Mem: {total:>14} {total // 4:>14} {total // 4:>14}\n'}
    return probes


def provenance(target, node, identity, hw_fp):
    return {'class_id': identity.class_id, 'config_id': identity.config_id, 'hw_fp': hw_fp,
            'node': node, 'target': target, 'agent': {'name': 'pi', 'version': PI_VERSION},
            'revision': 'deadbeef', 'recorded_at': '1970-01-30T14:00:00+00:00'}


def stamp(offset_minutes):
    return (START + datetime.timedelta(minutes=offset_minutes)).timestamp()


def iso(offset_minutes):
    return (START + datetime.timedelta(minutes=offset_minutes)).isoformat(timespec='seconds')


def history_entry(alias, target, identity, node='workere', offset=0, hw_fp='a' * 16):
    return {'alias': alias, 'from': iso(offset - 60), 'to': iso(offset + 60), 'node': node,
            'target': target, 'class_id': identity.class_id, 'config_id': identity.config_id,
            'hw_fp': hw_fp, 'why': 'the fixture states what it served'}


def attempt(passed=True, wall=100.0, tool_errors=1, regression_total=100, regression_failed=(), **extra):
    return {'passed': passed, 'wall_s': wall, 'tool_errors': tool_errors, 'assistant_turns': 4,
            'regression_total': regression_total, 'regression_failed': list(regression_failed),
            'timed_out': False, **extra}


def write_synthetic_site(root):
    for rel in ('infra', 'profiles', 'backends', 'gateway', 'agents/pi', 'agents/claude-code', 'agents/codex',
                'docs', 'evidence'):
        (root / rel).mkdir(parents=True, exist_ok=True)
    inventory = {
        'all': {
            'vars': {
                'ansible_user': 'operator', 'nodejs_binary': '/usr/bin/node',
                'rclone_config_dir': '/srv/agentiac/rclone', 'backup_destination': 'remote:backups',
                'gateway_port': 14000, 'chatgpt_port': 14001, 'resource_prefix': 'agentiac',
            },
            'children': {'workers': {'hosts': {
                'workera': {'node_class': 'orin', 'min_available_gib': 80},
                'workerb': {'node_class': 'orin', 'min_available_gib': 80},
                'workerc': {'node_class': 'rtx5090', 'borrowed': True, 'min_available_gib': 80},
                'workerd': {'node_class': 'rtx5090', 'borrowed': True, 'min_available_gib': 80},
                'workere': {'node_class': 'gb10', 'borrowed': True, 'former_names': ['oldbox'], 'min_available_gib': 80},
            }}},
        },
    }
    (root / 'infra/inventory.yml').write_text(yaml.safe_dump(inventory, sort_keys=False))
    (root / 'profiles/small-q6.json').write_text(json.dumps({'nodes': ['orin'], 'model': 'q6'}))

    backends = {
        'tf-1.1.4-qfn-mxfp8-nvfp4': {
            'id': 'tf-1.1.4-qfn-mxfp8-nvfp4',
            'engine': {'repository': 'https://example.invalid/TensorFold.git', 'version': '1.1.4',
                       'revision': '1' * 40},
            'model': {'repository': 'nvidia/Qwen3.8-Flash-Next-FP4-Local', 'revision': '2' * 40,
                      'files': [{'path': 'w.safetensors', 'sha256': 'a' * 64, 'size': 90000000000}]},
            'quantization': 'mixed-MXFP8-NVFP4', 'family': 'qfn', 'kv_cache_type': 'bf16',
            'workers': {'workere': {'slots': 1}},
        },
        'strata-0.2.8-qfn-gyro-m-tq2_0': {
            'id': 'strata-0.2.8-qfn-gyro-m-tq2_0',
            'repository': 'https://example.invalid/Strata.git', 'source_version': '0.2.8',
            'source_revision': '3' * 40, 'model': 'Gyro-M', 'variant': 'Gyro-M',
            'model_repository': 'synth/Qwen3.8-Flash-Next-Gyro-GGUF', 'model_revision': '4' * 40,
            'files': [{'path': 'gyro.gguf', 'sha256': 'b' * 64, 'size': 91965774272}],
            'workers': {'workerc': {'cuda_arch': '89', 'min_available_gib': 95},
                        'workerd': {'cuda_arch': '89', 'min_available_gib': 95}},
        },
        'strata-0.2.10-qfn-gsq-rco-iq3_xxs': {
            'id': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs',
            'repository': 'https://example.invalid/Strata.git', 'source_version': '0.2.10',
            'source_revision': '5' * 40,
            'model_repository': 'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF', 'model_revision': '6' * 40,
            'files': [{'path': 'iq3_xxs.gguf', 'sha256': 'c' * 64, 'size': 74000000000}],
            'workers': {'workera': {'cuda_arch': '87'}, 'workerb': {'cuda_arch': '87'}},
        },
        'strata-0.2.12-qfn-gsq-rco-iq3_xxs': {
            'id': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
            'repository': 'https://example.invalid/Strata.git', 'source_version': '0.2.12',
            'source_revision': '7' * 40,
            'model_repository': 'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF', 'model_revision': '8' * 40,
            'files': [{'path': 'iq3_xxs.gguf', 'sha256': 'c' * 64, 'size': 74000000000}],
            'workers': {'workera': {'cuda_arch': '87'}, 'workerb': {'cuda_arch': '87'}},
        },
        'strata-0.2.11-qfn-ud-iq3_xxs': {
            'id': 'strata-0.2.11-qfn-ud-iq3_xxs',
            'repository': 'https://example.invalid/Strata.git', 'source_version': '0.2.11',
            'source_revision': '9' * 40,
            'model_repository': 'synth/Qwen3.8-Flash-Next-GGUF', 'model_revision': '0' * 40,
            'files': [{'path': 'ud-iq3_xxs.gguf', 'sha256': 'd' * 64, 'size': 70000000000}],
            'workers': {'workerc': {'cuda_arch': '89'}},
        },
    }
    for stem, spec in backends.items():
        (root / f'backends/{stem}.json').write_text(json.dumps(spec))

    gateway_config = {
        'model_list': [
            {'model_name': 'workere-worker-qfn', 'litellm_params': {'model': 'openai/tf', 'api_base': 'http://127.0.0.1:18103/v1'}},
            {'model_name': 'workerc-worker-gyro-m', 'litellm_params': {'model': 'openai/gyro', 'api_base': 'http://127.0.0.1:18115/v1'}},
            {'model_name': 'workerc-worker-ud-iq3xxs', 'litellm_params': {'model': 'openai/ud', 'api_base': 'http://127.0.0.1:18115/v1'}},
            {'model_name': 'workerd-worker-gyro-m', 'litellm_params': {'model': 'openai/gyro', 'api_base': 'http://127.0.0.1:18114/v1'}},
            {'model_name': 'workera-worker', 'litellm_params': {'model': 'openai/iq3', 'api_base': 'http://127.0.0.1:18101/v1'}},
            {'model_name': 'workerb-worker', 'litellm_params': {'model': 'openai/iq3', 'api_base': 'http://127.0.0.1:18102/v1'}},
        ]
    }
    (root / 'gateway/config.yaml').write_text(yaml.safe_dump(gateway_config, sort_keys=False))
    routes = {
        'version': 2,
        'alias': 'local-coder',
        'default': 'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k',
        'targets': {
            'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k': {
                'source_alias': 'workere-worker-qfn', 'node': 'workere',
                'backend': 'tf-1.1.4-qfn-mxfp8-nvfp4', 'context_window': 262144, 'max_tokens': 16384},
            'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k': {
                'source_alias': 'workerb-worker', 'node': 'workerb',
                'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'context_window': 262144, 'max_tokens': 16384},
            'workerb-strata-0.2.10-qfn-gsq-rco-iq3_xxs-256k': {
                'source_alias': 'workerb-worker', 'node': 'workerb',
                'backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs', 'context_window': 262144, 'max_tokens': 16384},
            'workera-strata-0.2.10-qfn-gsq-rco-iq3_xxs-256k': {
                'source_alias': 'workera-worker', 'node': 'workera',
                'backend': 'strata-0.2.10-qfn-gsq-rco-iq3_xxs', 'context_window': 262144, 'max_tokens': 16384},
            'workera-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k': {
                'source_alias': 'workera-worker', 'node': 'workera',
                'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'context_window': 262144, 'max_tokens': 16384},
            'workerc-strata-0.2.11-qfn-ud-iq3_xxs-256k': {
                'source_alias': 'workerc-worker-ud-iq3xxs', 'node': 'workerc',
                'backend': 'strata-0.2.11-qfn-ud-iq3_xxs', 'context_window': 262144, 'max_tokens': 16384},
            'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
                'source_alias': 'workerc-worker-gyro-m', 'node': 'workerc',
                'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'context_window': 131072, 'max_tokens': 16384},
            'workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
                'source_alias': 'workerd-worker-gyro-m', 'node': 'workerd',
                'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'context_window': 131072, 'max_tokens': 16384},
        },
        'legacy_targets': {
            'oldbox': 'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k',
            'workerb': 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
        },
        'legacy_sources': {},
    }
    (root / 'gateway/local-coder.json').write_text(json.dumps(routes, indent=2))
    (root / 'agents/pi/models.json').write_text(json.dumps({'providers': {'local-infra': {'models': [{'id': 'local-coder'}]}}})
                                                )
    (root / 'agents/pi/router.json').write_text(json.dumps({'low': 'local-coder', 'high': 'local-coder'}))
    (root / 'agents/agents.yml').write_text(yaml.safe_dump([
        {'id': 'pi', 'endpoint': '/v1', 'lock': 'agents/pi/package-lock.json'},
        {'id': 'claude-code', 'endpoint': '/v1', 'pin': 'agents/claude-code/pin.json'},
        {'id': 'codex', 'endpoint': '/v1', 'pin': 'agents/codex/pin.json'},
    ], sort_keys=False))
    (root / 'agents/pi/package-lock.json').write_text(json.dumps({
        'packages': {
            '': {'dependencies': {'@mariozechner/pi-coding-agent': '^1.5.0'}},
            'node_modules/@mariozechner/pi-coding-agent': {'version': '1.5.0'},
        }
    }))
    (root / 'agents/claude-code/pin.json').write_text(json.dumps({'version': '2.1.316'}))
    (root / 'agents/codex/pin.json').write_text(json.dumps({'version': '0.165.0'}))
    (root / 'docs/roadmap.md').write_text('Promotion rule: pass rate, regression class, 25%, capacity gain.\n')
    (root / 'docs/license-review.json').write_text('[]')
    return root


class Checkout:
    def __init__(self, case):
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = write_synthetic_site(Path(directory.name))
        for host in ('workere', 'workerc', 'workerd', 'workera', 'workerb'):
            hardware.write(hardware.directory(self.root), hardware.record(host, NODE_PROBES, {'node_class': host}))

    def graph(self):
        return declared.load(self.root)

    def identity(self, target):
        return identities.compute_target(self.graph(), target)

    def task(self, name):
        folder = self.root / 'benchmarks/suite/tasks'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{name}.json'
        path.write_text(json.dumps({}, sort_keys=True))
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def suite(self, attempts):
        path = self.root / 'artifacts/suite/state.sqlite'
        store = state.Store(path)
        cases = []
        for alias, task, trial, result in attempts:
            task_path = self.root / 'benchmarks/suite/tasks' / f'{task}.json'
            sha = hashlib.sha256(task_path.read_bytes()).hexdigest() if task_path.exists() else self.task(task)
            spec = {'suite': 'v1', 'frontend': f'pi {PI_VERSION}', 'alias': alias, 'model': {'id': alias},
                    'image': 'sha256:abc', 'task': task, 'task_sha256': sha, 'trial': trial, 'timeout_s': 1800}
            cases.append((store.register(spec), trial, result))
        db = store.connect()
        try:
            for case, trial, result in cases:
                db.execute("UPDATE cases SET status=?, attempts=1 WHERE id=?",
                           ('complete' if result.get('passed') else 'failed', case))
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',
                           (case, 1, stamp(trial), stamp(trial), 'complete' if result.get('passed') else 'failed',
                            json.dumps(result)))
            db.commit()
        finally:
            db.close()
        return path

    def history(self, entries):
        (self.root / 'evidence/alias-history.yml').write_text(yaml.safe_dump(entries, sort_keys=False))

    def import_(self):
        return evidence.import_evidence(self.root)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.checkout = Checkout(self)
        self.root = self.checkout.root
        self.checkout.import_()

    def measured(self, target, node=None, *, passed=True, wall=100.0, regression=(), regression_total=100,
                 task='ogs-m6-strict-config', agent=None):
        graph = self.checkout.graph()
        node = node or graph.route['targets'][route_aliases.canonical(graph.route, target)]['node']
        identity = self.checkout.identity(target)
        hw_fp = hardware.latest(hardware.directory(self.root), node)['hw_fp']
        recorded = provenance(target, node, identity, hw_fp)
        if agent:
            recorded['agent'] = {'name': agent, 'version': '1.0'}
        self.checkout.suite([(f'local-coder-{target}', task, trial,
                              attempt(passed=passed, wall=wall * (1 + trial / 10),
                                      regression_total=regression_total, regression_failed=list(regression),
                                      provenance=recorded))
                             for trial in range(4)])
        self.checkout.import_()
        return identity

    def check(self, entry, name):
        return next(item for item in entry['filters'] if item['check'] == name)

    def free(self, gib=400):
        return {host: {'reachable': True, 'free_gib': gib}
                for host in ('workere', 'workerc', 'workerd', 'workera', 'workerb')}


class ScreenTests(Fixture):
    def rows(self, current='oldbox'):
        return catalog.targets(self.root, current=current)

    def one(self, target='oldbox', current='oldbox'):
        return catalog.show(self.root, target, current=current)

    def test_every_declared_target_appears_once_in_the_order_the_route_declares_them(self):
        self.assertEqual([row['target'] for row in self.rows()], list(self.checkout.graph().route['targets']))

    def test_exactly_one_row_is_marked_as_what_local_coder_serves_now(self):
        rows = self.rows(current='workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')
        self.assertEqual([row['target'] for row in rows if row['current']], ['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'])

    def test_a_declared_version_is_never_repeated_as_an_observation(self):
        row = self.one('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')
        self.assertEqual(row['declared']['engine']['version'], '0.2.8')
        self.assertEqual(row['state']['observed_versions']['engine'], 'unknown')
        self.assertIn('ops update observe', row['state']['observed_versions_why'])

    def observed(self, backend_id, value='0.2.8', node='workerc'):
        record = self.root / 'artifacts/components/observed.json'
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({'schema_version': 1, 'observed_at': '1970-01-02T10:00:00+00:00', 'notes': [],
                                      'components': [{'id': 'strata', 'owner': 'project', 'pinned': '0.2.8', 'hosts': {
                                          node: {'value': value, 'backend': backend_id}}}]}))

    def manifest_id(self, target):
        graph = self.checkout.graph()
        return graph.backends[graph.route['targets'][target]['backend']]['id']

    def test_a_reading_of_this_target_s_manifest_is_the_observed_engine(self):
        target = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'
        self.observed(self.manifest_id(target))
        row = self.one(target)
        self.assertEqual(row['state']['observed_versions']['engine'], '0.2.8')
        self.assertIn('from the front on workerc', row['state']['observed_versions_why'])

    def test_the_list_reads_the_same_record_as_show(self):
        target = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'
        self.observed(self.manifest_id(target))
        row = next(item for item in self.rows() if item['target'] == target)
        self.assertEqual(row['state']['observed_versions']['engine'], '0.2.8')

    def test_a_front_serving_another_manifest_says_nothing_about_this_target(self):
        self.observed('some-other-manifest')
        row = self.one('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')
        self.assertEqual(row['state']['observed_versions']['engine'], 'unknown')
        self.assertIn('no front on workerc reported', row['state']['observed_versions_why'])

    def test_the_same_manifest_on_another_host_is_not_this_target(self):
        self.observed(self.manifest_id('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'), node='workerd')
        self.assertEqual(self.one('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')['state']['observed_versions']['engine'], 'unknown')

    def test_a_host_that_could_not_be_read_is_not_a_version(self):
        self.observed(self.manifest_id('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'), value='unknown')
        self.assertIn('no front on workerc reported',
                      self.one('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')['state']['observed_versions_why'])

    def test_a_record_that_cannot_be_used_is_no_reading(self):
        record = self.root / 'artifacts/components/observed.json'
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('not json')
        self.assertIn('ops update observe', self.one('oldbox')['state']['observed_versions_why'])

    def test_a_target_nobody_measured_reports_no_measurement_rather_than_zero(self):
        block = self.one('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')['evidence']
        self.assertEqual(block['rows'], 0)
        self.assertEqual(block['metrics'], {})
        self.assertIsNone(block.get('why'))

    def test_a_missing_database_says_why_instead_of_looking_like_an_unmeasured_target(self):
        (self.root / 'artifacts/evidence/evidence.sqlite').unlink()
        block = self.one('oldbox')['evidence']
        self.assertIn('evidence import', block['why'])
        self.assertEqual(block['rows'], 0)

    def test_the_rows_of_one_target_are_not_shown_for_another_that_shares_its_class(self):
        self.assertEqual(self.checkout.identity('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k').class_id,
                         self.checkout.identity('workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k').class_id)
        self.measured('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')
        self.assertEqual(self.one('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')['evidence']['rows'], 4)
        self.assertEqual(self.one('workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k')['evidence']['rows'], 0)

    def test_a_node_nobody_inventoried_has_no_hardware_and_says_so(self):
        (hardware.directory(self.root) / 'workere.json').unlink()
        state_block = self.one('oldbox')['state']
        self.assertEqual(state_block['hw_fp'], 'unknown')
        self.assertIsNone(state_block['memory_total_gib'])
        self.assertIsNone(state_block['age_s'])
        self.assertIsNone(state_block['collected_at'])

    def test_the_state_block_carries_the_age_of_what_it_claims(self):
        record = hardware.latest(hardware.directory(self.root), 'workere')
        record['collected_at'] = '1970-01-01T00:00:00+00:00'
        hardware.write(hardware.directory(self.root), record)
        state_block = self.one('oldbox')['state']
        self.assertEqual(state_block['collected_at'], '1970-01-01T00:00:00+00:00')
        self.assertEqual(state_block['hw_fp'], record['hw_fp'])
        self.assertGreater(state_block['age_s'], 30 * 24 * 3600)

    def test_the_evidence_block_keeps_the_reason_of_a_row_it_will_not_rank(self):
        self.measured('oldbox')
        record = hardware.latest(hardware.directory(self.root), 'workere')
        record['facts']['driver'] = hardware.fact('590.111.23', 'nvidia-smi')
        record['hw_fp'] = hardware.fingerprint_of(record['facts'])
        hardware.write(hardware.directory(self.root), record)
        self.checkout.import_()
        block = self.one('oldbox')['evidence']
        self.assertEqual((block['rows'], block['usable']), (4, 0))
        self.assertTrue(block['excluded'] and all('stale' in reason for reason in block['excluded']))

    def test_a_measurement_with_no_keys_is_not_credited_to_a_configuration_it_might_have_been(self):
        self.measured('oldbox')
        self.checkout.suite([('local-coder-oldbox', 'ogs-m2-logprobs-defaults', trial, attempt())
                             for trial in range(4)])
        self.checkout.import_()
        self.assertEqual((self.one('oldbox')['evidence']['rows'],
                          self.one('oldbox')['evidence']['usable']), (4, 4))
        self.assertEqual(len(evidence.query(self.root, identity='unknown')), 4)

    def test_a_metric_is_summarised_over_every_row_of_it_and_says_when_it_was_measured(self):
        self.measured('oldbox', wall=100.0)
        metric = self.one('oldbox')['evidence']['metrics']['wall_time_s']
        self.assertEqual(metric['n'], 4)
        self.assertEqual(metric['value'], 115.0)
        self.assertTrue(metric['measured_at'])


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.checkout = Checkout(self)
        self.root = self.checkout.root

    def test_the_committed_file_is_what_the_generator_produces(self):
        doc = SITE / 'docs/compatibility.md'
        if doc.exists():
            self.assertEqual(catalog.compatibility(SITE), doc.read_text())
        else:
            text = catalog.compatibility(SITE)
            for target in declared.load(SITE).route['targets']:
                self.assertIn(f'| {target} |', text)

    def test_the_document_is_stable_across_calls_and_names_every_target(self):
        text = catalog.compatibility(self.root)
        self.assertEqual(text, catalog.compatibility(self.root))
        for target in self.checkout.graph().route['targets']:
            self.assertIn(f'| {target} |', text)

    def test_a_field_that_no_manifest_declares_is_written_as_a_gap_and_not_as_a_value(self):
        text = catalog.compatibility(self.root)
        self.assertIn('## What is not declared', text)
        self.assertIn('declares no class_args', text)

    def test_no_measurement_leaks_into_a_document_about_declarations(self):
        target, node = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', 'workerc'
        identity = self.checkout.identity(target)
        hw_fp = hardware.latest(hardware.directory(self.root), node)['hw_fp']
        self.checkout.suite([(f'local-coder-{target}', 'ogs-m6-strict-config', 0,
                              attempt(passed=True, wall=42.25,
                                      provenance=provenance(target, node, identity, hw_fp)))])
        self.checkout.import_()
        text = catalog.compatibility(self.root)
        for value in ('42.25', 'pass_rate', 'wall_time_s'):
            self.assertNotIn(value, text)

    def test_the_agents_are_recorded_with_the_pin_that_lets_them_run(self):
        text = catalog.compatibility(self.root)
        for agent, version in (('pi', '1.5.0'), ('claude-code', '2.1.316'), ('codex', '0.165.0')):
            row = next(line for line in text.splitlines() if line.startswith(f'| {agent} |'))
            self.assertIn(version, row)
            self.assertIn('not reviewed', row)


class RecommendTests(Fixture):
    def setUp(self):
        super().setUp()
        self.licenses()

    def licenses(self, *repositories):
        entries = [{'model': repository, 'detected_apache2': True, 'detected_mit': False}
                   for repository in repositories]
        (self.root / 'docs/license-review.json').write_text(json.dumps(entries))

    def review(self, *targets, node=None, context=None, storage=None, current='oldbox', task='coding'):
        report = catalog.recommend(self.root, node=node, context=context, storage=storage,
                                   current=current, task=task)
        return report, {entry['target']: entry for entry in report['candidates']}

    def both(self, candidate, wall, *, reviewed=True, **kwargs):
        self.measured('oldbox', wall=100.0)
        self.measured(candidate, wall=wall)
        repositories = ['nvidia/Qwen3.8-Flash-Next-FP4-Local', 'synth/Qwen3.8-Flash-Next-Gyro-GGUF',
                        'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF']
        self.licenses(*(repositories if reviewed else repositories[:1]))
        return self.review(candidate, storage=self.free(), **kwargs)

    def test_every_candidate_answers_every_check_whether_or_not_it_was_excluded(self):
        report, entries = self.review()
        for entry in entries.values():
            self.assertEqual([item['check'] for item in entry['filters']],
                             ['license', 'node', 'context', 'memory', 'disk'])
            self.assertTrue(all(item['reason'] for item in entry['filters'] if item['status'] != 'ok'),
                            f"{entry['target']} was excluded or unknown without a reason")
        survivors = [entry['target'] for entry in entries.values() if not entry['incumbent']
                     and not any(item['status'] == 'excluded' for item in entry['filters'])]
        self.assertEqual(survivors, ['workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
                                     'workerb-strata-0.2.10-qfn-gsq-rco-iq3_xxs-256k',
                                     'workera-strata-0.2.10-qfn-gsq-rco-iq3_xxs-256k',
                                     'workera-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
                                     'workerc-strata-0.2.11-qfn-ud-iq3_xxs-256k'])
        self.assertEqual(report['conclusion'], 'insufficient-evidence')

    def test_a_candidate_is_excluded_when_the_node_cannot_hold_it_and_the_reason_names_both_numbers(self):
        need = json.loads((self.root / 'backends/strata-0.2.8-qfn-gyro-m-tq2_0.json').read_text()
                          )['workers']['workerc']['min_available_gib']
        hardware.write(hardware.directory(self.root), hardware.record('workerc', probes_with_memory(24)))
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=32768)
        memory = self.check(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'memory')
        self.assertEqual(memory['status'], 'excluded')
        self.assertIn(f'{need} GiB', memory['reason'])
        self.assertIn('24.0 GiB', memory['reason'])
        self.assertIn('measured', memory['reason'])

    def test_the_inventory_figure_is_used_and_named_when_the_manifest_declares_none(self):
        manifest = self.root / 'backends/strata-0.2.8-qfn-gyro-m-tq2_0.json'
        spec = json.loads(manifest.read_text())
        del spec['workers']['workerc']['min_available_gib']
        manifest.write_text(json.dumps(spec))
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=32768)
        memory = self.check(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'memory')
        self.assertEqual(memory['status'], 'ok')
        self.assertIn('infra/inventory.yml (declared)', memory['reason'])

    def test_a_node_with_no_hardware_record_is_a_gap_and_not_a_pass(self):
        (hardware.directory(self.root) / 'workerc.json').unlink()
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=32768)
        memory = self.check(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'memory')
        self.assertEqual(memory['status'], 'unknown')
        self.assertIn('ops inventory', memory['reason'])

    def test_a_candidate_serving_less_context_than_the_route_gives_today_is_excluded(self):
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k')
        context = self.check(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'context')
        self.assertEqual(context['status'], 'excluded')
        self.assertIn('131072', context['reason'])
        self.assertIn('262144', context['reason'])

    def test_the_context_a_candidate_must_serve_is_askable_and_not_only_whatever_runs_today(self):
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=131072)
        self.assertEqual(self.check(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'context')['status'], 'ok')

    def test_a_candidate_on_another_node_is_excluded_when_a_node_was_asked_for(self):
        _, entries = self.review('workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k', node='workerc', context=32768)
        node = self.check(entries['workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'node')
        self.assertEqual(node['status'], 'excluded')
        self.assertIn('not on workerc', node['reason'])

    def test_a_manifest_that_declares_no_worker_for_the_node_it_is_served_on_is_excluded(self):
        manifest = self.root / 'backends/strata-0.2.8-qfn-gyro-m-tq2_0.json'
        spec = json.loads(manifest.read_text())
        del spec['workers']['workerc']
        manifest.write_text(json.dumps(spec))
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=32768)
        node = self.check(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'node')
        self.assertEqual(node['status'], 'excluded')
        self.assertIn('workerd', node['reason'])

    def test_a_candidate_whose_weights_do_not_fit_is_excluded_with_the_arithmetic_shown(self):
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=32768,
                                 storage={'workerc': {'reachable': True, 'free_gib': 10}})
        disk = self.check(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'disk')
        self.assertEqual(disk['status'], 'excluded')
        self.assertIn('85.6 GiB', disk['reason'])
        self.assertIn('10 GiB free', disk['reason'])

    def test_no_storage_reading_is_a_gap_and_not_a_pass(self):
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=32768,
                                 storage={'workerc': {'reachable': False, 'free_gib': 400}})
        disk = self.check(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'disk')
        self.assertEqual(disk['status'], 'unknown')
        self.assertIn('ops storage', disk['reason'])

    def test_an_unreviewed_license_is_a_gap_and_keeps_a_winning_candidate_out_of_the_conclusion(self):
        report, entries = self.both('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', 40.0, context=131072, reviewed=False)
        entry = entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k']
        self.assertEqual(self.check(entry, 'license')['status'], 'unknown')
        self.assertIn('docs/license-review.json', self.check(entry, 'license')['reason'])
        self.assertTrue(entry['comparison']['passes'])
        self.assertEqual(report['conclusion'], 'insufficient-evidence')

    def test_a_reviewed_license_is_the_one_thing_that_lets_a_winning_candidate_be_named(self):
        report, entries = self.both('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', 40.0, context=131072)
        self.assertEqual(self.check(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'license')['status'], 'ok')
        self.assertEqual(report['conclusion'], 'candidate')

    def test_a_reviewed_license_that_is_neither_apache_nor_mit_excludes_the_candidate(self):
        (self.root / 'docs/license-review.json').write_text(json.dumps(
            [{'model': 'synth/Qwen3.8-Flash-Next-Gyro-GGUF', 'detected_apache2': False, 'detected_mit': False}]))
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=32768)
        self.assertEqual(self.check(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'], 'license')['status'], 'excluded')

    def test_a_candidate_that_is_25_percent_faster_at_the_same_quality_is_named(self):
        report, entries = self.both('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', 40.0, context=131072)
        conditions = entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k']['comparison']['conditions']
        self.assertTrue(conditions['wall_time_s']['ok'])
        self.assertTrue(conditions['pass_rate']['ok'])
        self.assertEqual(report['conclusion'], 'candidate')

    def test_a_candidate_that_is_only_ten_percent_faster_and_no_bigger_is_not_a_promotion(self):
        report, entries = self.both('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', 90.0, context=131072)
        conditions = entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k']['comparison']['conditions']
        self.assertFalse(conditions['wall_time_s']['ok'])
        self.assertFalse(conditions['capacity_gain']['ok'])
        self.assertFalse(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k']['comparison']['passes'])
        self.assertEqual(report['conclusion'], 'insufficient-evidence')

    def test_a_candidate_that_serves_more_context_is_a_capacity_gain_even_at_the_same_speed(self):
        self.measured('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', wall=100.0)
        self.measured('workerb', wall=100.0)
        self.licenses('synth/Qwen3.8-Flash-Next-Gyro-GGUF', 'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF')
        report, entries = self.review('workerb', current='workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k',
                                      context=131072, storage=self.free())
        self.assertTrue(entries['workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k']['comparison']['conditions']['capacity_gain']['ok'])
        self.assertEqual(report['conclusion'], 'candidate')

    def test_a_candidate_that_passes_less_than_the_incumbent_lower_bound_is_not_promoted(self):
        self.measured('oldbox', wall=40.0)
        self.measured('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', wall=40.0, passed=False)
        self.licenses('nvidia/Qwen3.8-Flash-Next-FP4-Local', 'synth/Qwen3.8-Flash-Next-Gyro-GGUF')
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=131072, storage=self.free())
        self.assertFalse(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k']['comparison']['conditions']['pass_rate']['ok'])
        self.assertFalse(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k']['comparison']['passes'])

    def test_a_new_regression_failure_is_the_other_condition_that_refuses_a_fast_candidate(self):
        self.measured('oldbox', wall=100.0, regression_total=10)
        self.measured('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', wall=40.0, regression_total=10,
                      regression=['tests/test_a.py::one', 'tests/test_b.py::two'])
        self.licenses('nvidia/Qwen3.8-Flash-Next-FP4-Local', 'synth/Qwen3.8-Flash-Next-Gyro-GGUF')
        _, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=131072, storage=self.free())
        condition = entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k']['comparison']['conditions']['no_new_regression_class']
        self.assertFalse(condition['ok'])
        self.assertIn('T11.2', condition['measured_as'])
        self.assertFalse(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k']['comparison']['passes'])

    def test_a_candidate_with_no_rows_of_its_own_cannot_be_compared_at_all(self):
        self.measured('oldbox', wall=100.0)
        self.licenses('nvidia/Qwen3.8-Flash-Next-FP4-Local')
        report, entries = self.review('workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', context=131072, storage=self.free())
        self.assertEqual(entries['workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k']['comparison']['state'], 'insufficient-evidence')
        self.assertEqual(report['conclusion'], 'insufficient-evidence')

    def license_all(self):
        self.licenses('nvidia/Qwen3.8-Flash-Next-FP4-Local', 'synth/Qwen3.8-Flash-Next-Gyro-GGUF')

    def test_both_sides_are_pooled_over_the_tasks_they_share_and_nothing_else(self):
        candidate = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'
        self.measured('oldbox', wall=100.0, task='ogs-a')
        self.measured('oldbox', wall=900.0, passed=False, task='ogs-hard')
        self.measured(candidate, wall=90.0, task='ogs-a')
        self.license_all()
        _, entries = self.review(candidate, context=131072, storage=self.free())
        comparison = entries[candidate]['comparison']
        self.assertEqual(comparison['state'], 'compared')
        self.assertEqual(comparison['shared_tasks'], ['ogs-a (pi 1.3.0)'])
        self.assertEqual(comparison['conditions']['pass_rate']['incumbent'], 1.0)
        self.assertEqual(comparison['conditions']['wall_time_s']['incumbent'], 115.0)
        self.assertEqual(comparison['incumbent_only'], ['ogs-hard (pi 1.3.0)'])

    def test_a_winner_that_skipped_a_task_of_the_incumbent_settles_nothing(self):
        candidate = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'
        self.measured('oldbox', wall=100.0, task='ogs-a')
        self.measured('oldbox', wall=100.0, task='ogs-b')
        self.measured(candidate, wall=40.0, task='ogs-a')
        self.license_all()
        report, entries = self.review(candidate, context=131072, storage=self.free())
        self.assertTrue(entries[candidate]['comparison']['passes'])
        self.assertEqual(report['conclusion'], 'insufficient-evidence')

    def test_a_task_only_the_candidate_ran_is_listed_and_decides_nothing(self):
        candidate = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'
        self.measured('oldbox', wall=100.0, task='ogs-a')
        self.measured(candidate, wall=40.0, task='ogs-a')
        self.measured(candidate, wall=900.0, passed=False, task='ogs-extra')
        self.license_all()
        report, entries = self.review(candidate, context=131072, storage=self.free())
        comparison = entries[candidate]['comparison']
        self.assertEqual(comparison['candidate_only'], ['ogs-extra (pi 1.3.0)'])
        self.assertTrue(comparison['passes'])
        self.assertEqual(report['conclusion'], 'candidate')

    def test_two_configurations_with_no_task_in_common_are_not_compared(self):
        candidate = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'
        self.measured('oldbox', wall=100.0, task='ogs-a')
        self.measured(candidate, wall=40.0, task='ogs-b')
        self.license_all()
        report, entries = self.review(candidate, context=131072, storage=self.free())
        comparison = entries[candidate]['comparison']
        self.assertEqual(comparison['state'], 'insufficient-evidence')
        self.assertEqual(comparison['conditions'], {})
        self.assertIn('different work', comparison['note'])
        self.assertEqual(report['conclusion'], 'insufficient-evidence')

    def test_the_same_task_under_another_agent_is_not_the_same_work(self):
        candidate = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'
        self.measured('oldbox', wall=100.0, task='ogs-a')
        self.measured(candidate, wall=40.0, task='ogs-a', agent='claude-code')
        self.license_all()
        _, entries = self.review(candidate, context=131072, storage=self.free())
        self.assertEqual(entries[candidate]['comparison']['state'], 'insufficient-evidence')

    def conclude(self, *states):
        return catalog._conclude(
            [{'excluded': [], 'incumbent': True, 'gaps': [], 'comparison': None}] +
            [{'excluded': [], 'incumbent': False, 'gaps': list(gaps),
              'comparison': {'state': state_val, 'passes': passes, 'incumbent_only': list(skipped)}}
             for state_val, passes, skipped, gaps in states])

    def test_every_survivor_compared_and_beaten_is_what_incumbent_means(self):
        self.assertEqual(self.conclude(('compared', False, (), ()), ('compared', False, (), ())), 'incumbent')

    def test_one_loser_does_not_answer_for_a_survivor_nobody_measured(self):
        self.assertEqual(self.conclude(('compared', False, (), ()), ('insufficient-evidence', False, (), ())),
                         'insufficient-evidence')

    def test_one_full_winner_is_enough_even_beside_an_unmeasured_survivor(self):
        self.assertEqual(self.conclude(('compared', True, (), ()), ('insufficient-evidence', False, (), ())),
                         'candidate')

    def test_a_winner_behind_an_unread_gate_does_not_conclude(self):
        self.assertEqual(self.conclude(('compared', True, (), ('license',))), 'insufficient-evidence')

    def disk(self, reading):
        row = {'node': 'workerc', 'declared': {'backend': 'b', 'weights': {'bytes': 90 * catalog.GIB}}}
        return catalog._check_disk(row, {'scratch_gib': 15}, {'workerc': {'reachable': True, **reading}})

    def test_weights_that_do_not_fit_and_no_root_that_could_hold_them_is_an_exclusion(self):
        check = self.disk({'free_gib': 50, 'roots': [{'path': '/small', 'gib': 89.9}]})
        self.assertEqual(check['status'], 'excluded')
        self.assertEqual(self.disk({'free_gib': 50})['status'], 'excluded')

    def test_a_root_large_enough_to_hold_the_weights_is_a_gap_and_not_an_exclusion(self):
        check = self.disk({'free_gib': 50, 'roots': [{'path': '/small', 'gib': 1.0}, {'path': '/big', 'gib': 95.0}]})
        self.assertEqual(check['status'], 'unknown')
        self.assertIn('/big', check['reason'])
        self.assertNotIn('/small', check['reason'])

    def test_room_for_a_fresh_copy_includes_the_floor_the_owner_keeps(self):
        self.assertEqual(self.disk({'free_gib': 145})['status'], 'ok')
        self.assertEqual(self.disk({'free_gib': 144})['status'], 'excluded')

    def test_a_node_whose_roots_do_not_exist_yet_measured_nothing(self):
        self.assertEqual(self.disk({'free_gib': 0.0, 'roots': []})['status'], 'unknown')

    def test_rows_that_may_not_rank_are_not_a_comparison(self):
        self.measured('oldbox', wall=100.0)
        target = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'
        alias = f'local-coder-{target}'
        self.checkout.history([history_entry(alias, target, self.checkout.identity(target), node='workerc')])
        self.checkout.suite([(alias, 'ogs-m6-strict-config', trial, attempt(wall=40.0 * (1 + trial / 10)))
                             for trial in range(4)])
        self.checkout.import_()
        self.licenses('nvidia/Qwen3.8-Flash-Next-FP4-Local', 'synth/Qwen3.8-Flash-Next-Gyro-GGUF')
        report, entries = self.review(target, context=131072, storage=self.free())
        self.assertGreater(entries[target]['evidence']['rows'], 0)
        self.assertEqual(entries[target]['evidence']['usable'], 0)
        self.assertEqual(report['conclusion'], 'insufficient-evidence')

    def test_the_incumbent_is_the_baseline_and_is_never_its_own_candidate(self):
        self.measured('oldbox', wall=100.0)
        report, entries = self.review(storage=self.free())
        self.assertTrue(entries['workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k']['incumbent'])
        self.assertIsNone(entries['workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k']['comparison'])
        self.assertEqual(report['incumbent']['target'], 'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k')

    def test_a_task_without_measurements_is_refused_rather_than_answered_with_a_guess(self):
        with self.assertRaises(ValueError):
            catalog.recommend(self.root, task='summarising', current='oldbox')

    def test_a_route_that_points_at_nothing_declared_is_refused_rather_than_ranked_against_nothing(self):
        with self.assertRaises(ValueError):
            catalog.recommend(self.root, current='nowhere')

    def test_the_rule_quoted_on_the_screen_is_the_rule_the_roadmap_states(self):
        for word in ('pass rate', 'regression class', '25%', 'capacity gain'):
            self.assertIn(word, catalog.PROMOTION_RULE)
        roadmap = SITE / 'docs/roadmap.md'
        if roadmap.exists():
            stated = next(line for line in roadmap.read_text().splitlines() if 'Promotion rule:' in line)
            for word in ('pass rate', 'regression class', '25%', 'capacity gain'):
                self.assertIn(word, stated)

    def test_recommending_opens_no_connection_runs_nothing_and_writes_nothing(self):
        self.measured('oldbox', wall=100.0)
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with patch.object(subprocess, 'run', side_effect=AssertionError('a recommendation runs nothing')), \
                patch.object(subprocess, 'Popen', side_effect=AssertionError('a recommendation runs nothing')), \
                patch('socket.socket', side_effect=AssertionError('a recommendation opens no connection')):
            catalog.recommend(self.root, storage=self.free(), current='oldbox')
        after = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(set(before), set(after))
        self.assertTrue(all(before[path] == after[path] for path in before))

    def test_the_conclusion_is_always_one_of_the_three_values(self):
        for kwargs in ({}, {'storage': self.free(), 'context': 32768},
                       {'storage': self.free(), 'context': 131072, 'current': 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'}):
            self.assertIn(catalog.recommend(self.root, **kwargs)['conclusion'], catalog.CONCLUSIONS)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.checkout = Checkout(self)
        self.root = self.checkout.root
        self.command = command
        self.route = sys.modules.get('route')

    def capture(self, *argv):
        out = io.StringIO()
        patchers = [
            patch.object(self.command, 'ROOT', self.root),
            patch.object(declared, 'ROOT', self.root),
            patch('sys.stdout', out),
            patch.object(catalog, '_observed', return_value=None),
        ]
        if self.route is not None:
            patchers += [
                patch.object(self.route, 'ROOT', self.root),
                patch.object(self.route, 'ROUTES', self.root / 'gateway/local-coder.json'),
            ]
        for p in patchers:
            p.start()
        try:
            code = self.command.main(list(argv))
        finally:
            for p in reversed(patchers):
                p.stop()
        return code, out.getvalue()

    def test_the_bare_command_still_lists_every_target_with_both_ids(self):
        code, out = self.capture('model')
        self.assertEqual(code, 0)
        self.assertRegex(out, r'class [0-9a-f]{16}  config [0-9a-f]{16}')

    def test_a_target_name_still_prints_its_inputs_as_json(self):
        code, out = self.capture('model', 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['class_inputs']['engine']['version'], '0.2.8')

    def test_list_and_show_answer_in_json_with_the_three_blocks_apart(self):
        code, out = self.capture('model', 'show', 'oldbox', '--json')
        self.assertEqual(code, 0)
        record = json.loads(out)
        self.assertEqual(set(record) - {'target', 'node', 'current'}, {'declared', 'identity', 'state', 'evidence'})
        self.assertEqual(record['state']['observed_versions']['engine'], 'unknown')

    def test_show_without_a_target_is_a_usage_error_and_not_a_traceback(self):
        err = io.StringIO()
        with patch('sys.stderr', err):
            code, out = self.capture('model', 'show')
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('needs a TARGET', err.getvalue())

    def test_probe_reads_the_suite_image_and_runs_the_adapter(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.command, 'ROOT', Path(directory)), \
                patch.object(self.command.subprocess, 'check_output', return_value='sha256:image\n') as inspected, \
                patch.object(self.command.adapters, 'probe', return_value={'value': 'native'}) as probed, \
                patch('agentiac.secrets.read_key', return_value='task-key'), \
                patch.object(self.command.adapters.PiAdapter, 'model_for', return_value={'id': 'local-coder'}), \
                patch.object(self.command.adapters.PiAdapter, 'prepare'), \
                patch('sys.stdout', io.StringIO()) as printed, patch('sys.stderr', io.StringIO()):
            code = self.command.agent_probe(argparse.Namespace(agent_id='pi', alias='local-coder',
                                                               feature='structured_noninteractive'))
            out = printed.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(f"{declared.resource('suite', SITE)}:v1", inspected.call_args.args[0])
        self.assertEqual(probed.call_args.args[2].spec['image'], 'sha256:image')
        self.assertEqual(probed.call_args.args[2].spec['model'], {'id': 'local-coder'})
        self.assertEqual(json.loads(out)['value'], 'native')

    def test_a_probe_keeps_its_trace_with_the_key_redacted(self):
        def fake_probe(_, __, ___, run):
            run(['true'], {'LITELLM_TASK_KEY': 'task-key'}, 5)
            return {'value': 'native'}

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.command, 'ROOT', Path(directory)), \
                patch.object(self.command.subprocess, 'check_output', return_value='sha256:image\n'), \
                patch.object(self.command.subprocess, 'run',
                             return_value=subprocess.CompletedProcess([], 0, 'said task-key\n', 'warned task-key\n')), \
                patch.object(self.command.adapters, 'probe', fake_probe), \
                patch('agentiac.secrets.read_key', return_value='task-key'), \
                patch.object(self.command.adapters.PiAdapter, 'model_for', return_value={'id': 'local-coder'}), \
                patch.object(self.command.adapters.PiAdapter, 'prepare'), \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            self.command.agent_probe(argparse.Namespace(agent_id='pi', alias='local-coder',
                                                        feature='structured_noninteractive'))
            folder = next((Path(directory) / 'artifacts/agents/probes').iterdir())
            self.assertEqual((folder / 'trace.jsonl').read_text(), 'said [REDACTED]\n')
            self.assertEqual((folder / 'stderr.txt').read_text(), 'warned [REDACTED]\n')

    def test_recommend_reports_one_conclusion_in_json_and_needs_no_network(self):
        code, out = self.capture('model', 'recommend', '--no-storage', '--json')
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report['schema_version'], 1)
        self.assertIn(report['conclusion'], catalog.CONCLUSIONS)

    def test_compatibility_writes_the_document_it_was_generated_from(self):
        self.capture('model', 'compatibility', '--write')
        self.assertEqual((self.root / 'docs/compatibility.md').read_text(), catalog.compatibility(self.root))
        code, out = self.capture('model', 'compatibility', '--write')
        self.assertEqual(code, 0)
        self.assertIn('unchanged', out)

    def test_a_task_without_evidence_is_a_usage_error_and_not_a_silent_incumbent(self):
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.capture('model', 'recommend', '--task', 'summarising')
        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
