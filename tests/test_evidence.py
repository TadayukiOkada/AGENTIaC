"""The Evidence DB is derived, stale when a key moves, and silent about what it cannot key (X21).

* the database is *rebuilt* from the primary records, so deleting it costs nothing and a
  second import changes nothing;
* a key that moved leaves the row in place with a reason, rather than deleting history or
  quietly re-keying it;
* a row whose configuration is not established is displayed and never ranked.
"""
import datetime
import gc
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import declared, evidence, hardware, identities, paths, state  # noqa: E402

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
    for rel in ('infra', 'profiles', 'backends', 'gateway', 'agents/pi', 'docs', 'evidence'):
        (root / rel).mkdir(parents=True, exist_ok=True)
    inventory = {
        'all': {
            'vars': {
                'ansible_user': 'operator', 'nodejs_binary': '/usr/bin/node',
                'rclone_config_dir': '/srv/agentiac/rclone', 'backup_destination': 'remote:backups',
                'gateway_port': 14000, 'chatgpt_port': 14001, 'resource_prefix': 'agentiac',
            },
            'children': {'workers': {'hosts': {
                'workera': {'node_class': 'orin'},
                'workerb': {'node_class': 'orin'},
                'workerc': {'node_class': 'rtx5090', 'borrowed': True},
                'workerd': {'node_class': 'rtx5090', 'borrowed': True},
                'workere': {'node_class': 'gb10', 'borrowed': True, 'former_names': ['oldbox']},
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
                      'files': [{'path': 'w.safetensors', 'sha256': 'a' * 64, 'size': 100}]},
            'quantization': 'mixed-MXFP8-NVFP4', 'family': 'qfn', 'kv_cache_type': 'bf16',
            'workers': {'workere': {'slots': 1}},
        },
        'strata-0.2.8-qfn-gyro-m-tq2_0': {
            'id': 'strata-0.2.8-qfn-gyro-m-tq2_0',
            'repository': 'https://example.invalid/Strata.git', 'source_version': '0.2.8',
            'source_revision': '3' * 40, 'model': 'Gyro-M', 'variant': 'Gyro-M',
            'model_repository': 'synth/Qwen3.8-Flash-Next-Gyro-GGUF', 'model_revision': '4' * 40,
            'files': [{'path': 'gyro.gguf', 'sha256': 'b' * 64, 'size': 91965774272}],
            'workers': {'workerc': {'cuda_arch': '89'}, 'workerd': {'cuda_arch': '89'}},
        },
        'strata-0.2.12-qfn-gsq-rco-iq3_xxs': {
            'id': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs',
            'repository': 'https://example.invalid/Strata.git', 'source_version': '0.2.12',
            'source_revision': '5' * 40,
            'model_repository': 'synth/Qwen3.8-Flash-Next-GSQ-RCO-GGUF', 'model_revision': '6' * 40,
            'files': [{'path': 'iq3_xxs.gguf', 'sha256': 'c' * 64, 'size': 74000000000}],
            'workers': {'workera': {'cuda_arch': '87'}, 'workerb': {'cuda_arch': '87'}},
        },
    }
    for stem, spec in backends.items():
        (root / f'backends/{stem}.json').write_text(json.dumps(spec))
    gateway_config = {
        'model_list': [
            {'model_name': 'workere-worker-qfn', 'litellm_params': {'model': 'openai/tf', 'api_base': 'http://127.0.0.1:18103/v1'}},
            {'model_name': 'workerc-worker-gyro-m', 'litellm_params': {'model': 'openai/gyro', 'api_base': 'http://127.0.0.1:18115/v1'}},
            {'model_name': 'workerd-worker-gyro-m', 'litellm_params': {'model': 'openai/gyro', 'api_base': 'http://127.0.0.1:18114/v1'}},
            {'model_name': 'workerb-worker', 'litellm_params': {'model': 'openai/iq3', 'api_base': 'http://127.0.0.1:18102/v1'}},
        ]
    }
    (root / 'gateway/config.yaml').write_text(yaml.safe_dump(gateway_config, sort_keys=False))
    routes = {
        'version': 2,
        'alias': 'local-coder',
        'default': 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
        'targets': {
            'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k': {
                'source_alias': 'workere-worker-qfn', 'node': 'workere',
                'backend': 'tf-1.1.4-qfn-mxfp8-nvfp4', 'context_window': 262144, 'max_tokens': 16384},
            'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
                'source_alias': 'workerc-worker-gyro-m', 'node': 'workerc',
                'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'context_window': 131072, 'max_tokens': 16384},
            'workerd-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {
                'source_alias': 'workerd-worker-gyro-m', 'node': 'workerd',
                'backend': 'strata-0.2.8-qfn-gyro-m-tq2_0', 'context_window': 131072, 'max_tokens': 16384},
            'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k': {
                'source_alias': 'workerb-worker', 'node': 'workerb',
                'backend': 'strata-0.2.12-qfn-gsq-rco-iq3_xxs', 'context_window': 262144, 'max_tokens': 16384},
        },
        'legacy_targets': {
            'oldbox': 'workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k',
            'workerb': 'workerb-strata-0.2.12-qfn-gsq-rco-iq3_xxs-256k',
            'strata-workerc-gyro-m-0.2.8-128k': 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k',
        },
        'legacy_sources': {},
    }
    (root / 'gateway/local-coder.json').write_text(json.dumps(routes, indent=2))
    (root / 'agents/pi/models.json').write_text(json.dumps({'providers': {'local-infra': {'models': [{'id': 'local-coder'}]}}})
                                                )
    (root / 'agents/pi/router.json').write_text(json.dumps({'low': 'local-coder', 'high': 'local-coder'}))
    (root / 'docs/roadmap.md').write_text('# Roadmap\n')
    (root / 'docs/operations.md').write_text('# Operations\n`artifacts/evidence` is regenerable.\n')
    return root


class Checkout:
    """A private synthetic checkout: declared files, hardware records, suite DB, and evidence files."""

    def __init__(self, case, manifests=False):
        self.manifests = manifests
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = write_synthetic_site(Path(directory.name))
        for host in ('workere', 'workerc', 'workerd', 'workera', 'workerb'):
            self.write_hardware(host, hardware.record(host, NODE_PROBES, {'node_class': host}))

    def write_hardware(self, host, record):
        hardware.write(hardware.directory(self.root), record)

    def graph(self):
        return declared.load(self.root)

    def identity(self, target):
        return identities.compute_target(self.graph(), target)

    def history(self, entries):
        (self.root / 'evidence/alias-history.yml').write_text(yaml.safe_dump(entries, sort_keys=False))

    def reports(self, entries):
        (self.root / 'evidence/reports.yml').write_text(yaml.safe_dump(entries, sort_keys=False))

    def task(self, name, **manifest):
        folder = self.root / 'benchmarks/suite/tasks'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{name}.json'
        path.write_text(json.dumps(manifest, sort_keys=True))
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def sha256_of(self, name):
        path = self.root / 'benchmarks/suite/tasks' / f'{name}.json'
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'sha'

    def suite(self, attempts):
        path = self.root / 'artifacts/suite/state.sqlite'
        store = state.Store(path)
        cases = []
        for alias, task, trial, result in attempts:
            if self.manifests and not (self.root / 'benchmarks/suite/tasks' / f'{task}.json').exists():
                self.task(task)
            spec = {'suite': 'v1', 'frontend': f'pi {PI_VERSION}', 'alias': alias, 'model': {'id': alias},
                    'image': 'sha256:abc', 'task': task, 'task_sha256': self.sha256_of(task), 'trial': trial,
                    'timeout_s': 1800}
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

    def ladder(self, label, options, cases):
        folder = self.root / 'artifacts/ladder' / label
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps(options))
        (folder / 'results.jsonl').write_text('\n'.join(json.dumps(case) for case in cases) + '\n')

    def import_(self, **kwargs):
        return evidence.import_evidence(self.root, **kwargs)

    def rows(self, **filters):
        return evidence.query(self.root, **filters)


class Fixture(unittest.TestCase):
    checkout = None

    def import_(self, **kwargs):
        return self.checkout.import_(**kwargs)

    def rows(self, **filters):
        return self.checkout.rows(**filters)

    def entry(self, alias='local-coder-oldbox', target='oldbox', node='workere', offset=0):
        return history_entry(alias, target, self.checkout.identity(target), node=node, offset=offset,
                             hw_fp=hardware.latest(hardware.directory(self.checkout.root), node)['hw_fp'])


class ImportTests(Fixture):
    def setUp(self):
        self.checkout = Checkout(self, manifests=True)
        self.identity = self.checkout.identity('oldbox')
        self.checkout.history([self.entry()])
        self.checkout.suite([('local-coder-oldbox', 'ogs-m6-strict-config', 0, attempt(passed=True, wall=100)),
                             ('local-coder-oldbox', 'ogs-m6-strict-config', 1, attempt(passed=True, wall=140)),
                             ('local-coder-oldbox', 'ogs-m6-strict-config', 2, attempt(passed=False, wall=200,
                                                                                        tool_errors=5))])

    def test_a_run_produces_quality_and_speed_as_separate_rows(self):
        _summary, rows = self.import_()
        metrics = {row['metric'] for row in rows}
        self.assertEqual(metrics, {'pass_rate', 'regression_pass_rate', 'wall_time_s', 'tool_errors_per_run'})
        by_metric = {row['metric']: row for row in rows}
        self.assertEqual(by_metric['pass_rate']['value'], 0.6667)
        self.assertEqual(by_metric['pass_rate']['n'], 3)
        self.assertEqual(by_metric['pass_rate']['ci_low'], 0.208)
        self.assertEqual(by_metric['wall_time_s']['value'], 140.0)
        self.assertEqual((by_metric['wall_time_s']['ci_low'], by_metric['wall_time_s']['ci_high']), (100.0, 200.0))
        self.assertEqual(by_metric['regression_pass_rate']['n'], 300)
        self.assertEqual(by_metric['regression_pass_rate']['value'], 1.0)

    def test_wilson_is_the_same_number_the_suite_report_prints(self):
        import suite
        for passed, total in ((0, 1), (1, 1), (2, 3), (7, 9), (41, 50)):
            self.assertEqual(evidence.wilson(passed, total), suite.wilson(passed, total))

    def test_the_database_is_rebuilt_from_the_primary_records_and_twice_is_once(self):
        first, first_rows = self.import_()
        again, again_rows = self.import_()
        self.assertEqual([row['id'] for row in first_rows], [row['id'] for row in again_rows])
        self.assertEqual({k: v for k, v in first.items() if k != 'imported_at'},
                         {k: v for k, v in again.items() if k != 'imported_at'})
        path = evidence.database(self.checkout.root)
        path.unlink()
        self.import_()
        stored = evidence.query(self.checkout.root)
        self.assertEqual(sorted(stored, key=lambda row: row['id']),
                         sorted(again_rows, key=lambda row: row['id']))

    def test_import_never_writes_to_the_primary_record(self):
        path = self.checkout.root / 'artifacts/suite/state.sqlite'
        gc.collect()
        before = path.read_bytes()
        self.import_()
        self.assertEqual(path.read_bytes(), before)

    def test_a_row_deleted_from_the_derived_view_returns_and_only_the_sources_decide_it(self):
        self.import_()
        path = evidence.database(self.checkout.root)
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM evidence WHERE metric = 'wall_time_s'")
        self.import_()
        self.assertIn('wall_time_s', {row['metric'] for row in self.rows()})

    def test_the_last_attempt_of_a_case_is_the_one_that_stands(self):
        path = self.checkout.suite([('local-coder-oldbox', 'ogs-m7-error-envelope', 0, attempt(passed=True))])
        store = state.Store(path)
        db = store.connect()
        try:
            case = db.execute("SELECT id FROM cases WHERE spec LIKE '%ogs-m7%'").fetchone()['id']
            db.execute('UPDATE cases SET attempts=2 WHERE id=?', (case,))
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',
                       (case, 2, stamp(9), stamp(9), 'failed', json.dumps(attempt(passed=False))))
            db.commit()
        finally:
            db.close()
        self.import_()
        row = next(row for row in self.rows(metric='pass_rate') if row['workload'] == 'ogs-m7-error-envelope')
        self.assertEqual((row['value'], row['n']), (0.0, 1))

    def test_an_infrastructure_failure_is_not_a_measurement_and_says_so_in_the_summary(self):
        self.checkout.suite([('local-coder-oldbox', 'ogs-m2-logprobs-defaults', 0, attempt(passed=True)),
                             ('local-coder-oldbox', 'ogs-m2-logprobs-defaults', 1,
                              {'passed': False, 'infrastructure_error': 'TimeoutExpired'})])
        summary, _ = self.import_()
        self.assertEqual(summary['sources'][0]['skipped'], {'infrastructure_error': 1})
        row = next(row for row in self.rows(metric='pass_rate', workload='ogs-m2-logprobs-defaults'))
        self.assertEqual((row['value'], row['n']), (1.0, 1))

    def test_rows_are_grouped_by_keys_not_by_alias_so_a_reconfiguration_splits_them(self):
        self.checkout.suite([('local-coder-strata-workerc-gyro-m-0.2.8-128k', 'ogs-m6-strict-config', 0, attempt())])
        self.checkout.history([self.entry(),
                               self.entry('local-coder-strata-workerc-gyro-m-0.2.8-128k',
                                          'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', 'workerc')])
        self.import_()
        rows = [row for row in self.rows(metric='pass_rate') if row['identity'] != 'unknown']
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]['config_id'], rows[1]['config_id'])


class IdentityTests(Fixture):
    def setUp(self):
        self.checkout = Checkout(self, manifests=True)
        self.identity = self.checkout.identity('oldbox')

    def test_a_measurement_the_run_recorded_is_believed_over_the_hand_written_one(self):
        recorded = {'target': 'oldbox', 'node': 'oldbox', 'class_id': 'a' * 16, 'config_id': 'b' * 16,
                    'hw_fp': 'c' * 16, 'agent': {'name': 'pi', 'version': '1.3.0'}}
        self.checkout.suite([('local-coder-oldbox', 'ogs-m6-strict-config', 0, attempt(provenance=recorded))])
        self.checkout.history([self.entry()])
        self.import_()
        row = next(row for row in self.rows(metric='pass_rate'))
        self.assertEqual(row['identity'], 'recorded')
        self.assertEqual(row['config_id'], 'b' * 16)

    def test_a_measurement_with_no_record_and_no_history_is_kept_displayed_and_never_ranked(self):
        self.checkout.suite([('local-coder-oldbox', 'ogs-m6-strict-config', 0, attempt())])
        self.checkout.history([])
        summary, _ = self.import_()
        self.assertEqual(summary['usable'], 0)
        row = next(row for row in self.rows(metric='pass_rate'))
        self.assertEqual(row['identity'], 'unknown')
        self.assertEqual(row['config_id'], identities.UNKNOWN)
        self.assertIn('no alias-history entry', row['identity_reason'])
        self.assertFalse(evidence.usable(row))
        self.assertEqual(self.rows(usable_only=True), [])
        self.assertEqual(len(self.rows()), 4)

    def test_a_history_entry_carrying_no_keys_leaves_the_row_unknown_with_the_why(self):
        self.checkout.suite([('local-coder-oldbox', 'ogs-m6-strict-config', 0, attempt())])
        entry = self.entry()
        for name in ('class_id', 'config_id', 'hw_fp'):
            entry.pop(name)
        entry['why'] = 'the manifest of the day named no weights'
        self.checkout.history([entry])
        self.import_()
        row = next(row for row in self.rows(metric='pass_rate'))
        self.assertEqual(row['identity'], 'unknown')
        self.assertIn('named no weights', row['identity_reason'])

    def test_two_windows_that_disagree_about_one_alias_bind_nothing(self):
        self.checkout.suite([('local-coder-oldbox', 'ogs-m6-strict-config', 0, attempt())])
        entry = self.entry()
        other = dict(entry, **{'from': iso(-90), 'to': iso(+90), 'config_id': 'f' * 16})
        with self.assertRaises(ValueError) as caught:
            self.checkout.history([entry, other])
            evidence.load_history(self.checkout.root)
        self.assertIn('overlaps', str(caught.exception))

    def test_an_id_that_names_no_weights_is_refused_because_four_machines_shared_one(self):
        routes = json.loads((self.checkout.root / 'gateway/local-coder.json').read_text())
        routes['targets']['plain'] = {'source_alias': 'workerb-worker', 'node': 'workerb',
                                      'context_window': 65536, 'max_tokens': 8192}
        (self.checkout.root / 'gateway/local-coder.json').write_text(json.dumps(routes))
        bare = self.checkout.identity('plain')
        self.assertIn('weights', bare.unknowns)
        entry = self.entry('local-coder-plain', 'plain', 'workerb')
        entry['class_id'], entry['config_id'] = bare.class_id, bare.config_id
        with self.assertRaises(ValueError) as caught:
            self.checkout.history([entry])
            evidence.load_history(self.checkout.root)
        self.assertIn('does not name the weights', str(caught.exception))

    def test_a_history_entry_is_refused_for_a_partial_id_a_bad_hash_or_no_why(self):
        good = self.entry()
        for mutate, message in ((lambda e: e.pop('hw_fp'), 'without the others'),
                                (lambda e: e.__setitem__('config_id', 'nothex'), '16 hex digits'),
                                (lambda e: e.__setitem__('why', ''), 'is a guess'),
                                (lambda e: e.__setitem__('served_by', 'oldbox'), 'unknown field'),
                                (lambda e: e.__setitem__('to', iso(-120)), 'is not after')):
            entry = dict(good)
            mutate(entry)
            with self.subTest(message=message), self.assertRaises(ValueError) as caught:
                self.checkout.history([entry])
                evidence.load_history(self.checkout.root)
            self.assertIn(message, str(caught.exception))

    def test_a_history_file_may_be_absent(self):
        self.checkout.history([])
        (self.checkout.root / 'evidence/alias-history.yml').unlink()
        self.assertEqual(evidence.load_history(self.checkout.root), [])

    def test_a_row_marked_unknown_never_ranks_even_if_it_somehow_carries_keys(self):
        row = {'identity': 'unknown', 'identity_reason': 'built by the test', 'confidence': 'suite',
               'stale_reason': None, 'class_id': 'a' * 16, 'config_id': 'b' * 16, 'hw_fp': 'c' * 16}
        self.assertFalse(evidence.usable(row))
        self.assertEqual(evidence.unusable_reason(row), 'built by the test')

    def test_two_entries_that_both_cover_one_measurement_bind_nothing(self):
        entry = self.entry()
        measurement = {'alias': 'local-coder-oldbox', 'measured_at': iso(0)}
        self.assertEqual(evidence.resolve_identity(measurement, [entry])['identity'], 'history')
        ambiguous = evidence.resolve_identity(measurement, [entry, dict(entry, config_id='f' * 16)])
        self.assertEqual(ambiguous['identity'], 'unknown')
        self.assertIn('ambiguous', ambiguous['identity_reason'])


class StalenessTests(Fixture):
    def setUp(self):
        self.checkout = Checkout(self, manifests=True)
        self.identity = self.checkout.identity('oldbox')
        self.checkout.history([history_entry('local-coder-oldbox', 'oldbox', self.identity, node='workere',
                                             hw_fp=hardware.latest(hardware.directory(self.checkout.root),
                                                                   'workere')['hw_fp'])])
        self.checkout.suite([('local-coder-oldbox', 'ogs-m6-strict-config', 0, attempt())])

    def test_redeclaring_a_backend_makes_the_rows_stale_and_keeps_them(self):
        self.import_()
        before = next(row for row in self.rows(metric='pass_rate'))
        self.assertIsNone(before['stale_reason'])
        manifest = self.checkout.root / 'backends/tf-1.1.4-qfn-mxfp8-nvfp4.json'
        spec = json.loads(manifest.read_text())
        spec['quantization'] = 'nvfp4'
        manifest.write_text(json.dumps(spec))
        self.import_()
        after = next(row for row in self.rows(metric='pass_rate'))
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['class_id'], before['class_id'])
        self.assertIn('class_id', after['stale_reason'])
        self.assertIn('is not what workere-tf-1.1.4-qfn-mxfp8-nvfp4-256k now declares', after['stale_reason'])
        self.assertFalse(evidence.usable(after))
        self.assertEqual(self.rows(usable_only=True), [])
        self.assertIn('stale:', evidence.unusable_reason(after))

    def test_a_hardware_change_stales_the_rows_of_that_node_alone(self):
        self.import_()
        record = hardware.latest(hardware.directory(self.checkout.root), 'workere')
        record['facts']['driver'] = hardware.fact('590.111.23', 'nvidia-smi')
        record['hw_fp'] = hardware.fingerprint_of(record['facts'])
        hardware.write(hardware.directory(self.checkout.root), record)
        self.import_()
        rows = {row['metric']: row for row in self.rows()}
        self.assertIn('hw_fp', rows['pass_rate']['stale_reason'])
        self.assertIn('is not what the inventory of workere says now', rows['pass_rate']['stale_reason'])

    def test_a_missing_hardware_record_leaves_the_row_kept_and_unconfirmed(self):
        self.import_()
        (hardware.directory(self.checkout.root) / 'workere.json').unlink()
        self.import_()
        row = next(row for row in self.rows(metric='pass_rate'))
        self.assertIsNotNone(row['value'])
        self.assertIn('could not be checked', row['stale_reason'])
        self.assertEqual(self.rows(usable_only=True), [])

    def test_an_unknown_row_is_never_stale_because_it_was_never_keyed(self):
        self.checkout.history([])
        self.import_()
        self.assertEqual(self.rows(stale=True), [])
        self.assertTrue(all(row['identity'] == 'unknown' for row in self.rows()))


class LadderTests(Fixture):
    def setUp(self):
        self.checkout = Checkout(self, manifests=True)
        self.checkout.ladder('workerc-gyro-m-128k',
                             {'alias': 'strata-workerc-gyro-m', 'model': 'qwen3.8-flash-next-gyro-gyro-m',
                              'context': 131072, 'depths': [4096], 'repeat': 2},
                             [{'case': '4096-1', 'depth': 4096, 'rep': 1, 'status': 'ok', 'answer_passed': True,
                               'cold_ttft_s': 5.0, 'prefill_tok_s': 800.0, 'decode_tok_s': 70.0,
                               'finished': iso(1)},
                              {'case': '4096-2', 'depth': 4096, 'rep': 2, 'status': 'ok', 'answer_passed': False,
                               'cold_ttft_s': 7.0, 'prefill_tok_s': 600.0, 'decode_tok_s': 50.0,
                               'finished': iso(2)},
                              {'case': '4096-3', 'depth': 4096, 'rep': 3, 'status': 'failed',
                               'error_type': 'OSError', 'finished': iso(3)}])

    def test_speed_rows_exist_and_a_failed_case_contributes_no_number(self):
        self.import_()
        rows = {row['metric']: row for row in self.rows(workload='context-4096')}
        self.assertEqual(set(rows), {'ttft_s', 'prefill_tok_s', 'decode_tok_s', 'answer_pass_rate'})
        self.assertEqual(rows['ttft_s']['n'], 2)
        self.assertEqual(rows['ttft_s']['value'], 6.0)
        self.assertEqual((rows['ttft_s']['ci_low'], rows['ttft_s']['ci_high']), (5.0, 7.0))
        self.assertEqual(rows['answer_pass_rate']['value'], 0.5)
        self.assertEqual({row['confidence'] for row in rows.values()}, {'probe'})

    def test_a_ladder_run_that_recorded_no_keys_is_display_only(self):
        self.import_()
        rows = self.rows(workload='context-4096')
        self.assertTrue(all(row['identity'] == 'unknown' for row in rows))
        self.assertIn('no alias-history entry', rows[0]['identity_reason'])
        self.assertEqual(self.rows(usable_only=True), [])

    def test_a_history_entry_can_bind_a_ladder_run_when_it_can_name_the_configuration(self):
        self.checkout.history([dict(self.entry('strata-workerc-gyro-m', 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k', 'workerc'),
                                    **{'from': iso(-60), 'to': iso(+60)})])
        self.import_()
        rows = self.rows(workload='context-4096')
        self.assertTrue(all(row['identity'] == 'history' for row in rows))
        self.assertTrue(all(row['node'] == 'workerc' for row in rows))
        self.assertEqual(len(self.rows(usable_only=True)), 4)


class SupersededTests(unittest.TestCase):
    def setUp(self):
        self.checkout = Checkout(self, manifests=True)
        self.task = 'ogs-m7-error-envelope'
        identity = self.checkout.identity('oldbox')
        self.checkout.history([history_entry('local-coder-oldbox', 'oldbox', identity)])
        hw_fp = 'a' * 16
        self.recorded = {'class_id': identity.class_id, 'config_id': identity.config_id, 'hw_fp': hw_fp,
                         'node': 'oldbox', 'target': 'oldbox'}

    def test_a_case_of_an_edited_manifest_produces_no_row_and_is_counted(self):
        self.checkout.suite([('local-coder-oldbox', self.task, 0, attempt(provenance=self.recorded))])
        self.checkout.task(self.task, edited=True)
        summary, rows = self.checkout.import_()
        self.assertEqual(rows, [])
        self.assertEqual(summary['sources'][0]['skipped'], {'superseded': 1})

    def test_only_the_cases_of_the_current_manifest_are_pooled(self):
        self.checkout.suite([('local-coder-oldbox', self.task, 0, attempt(passed=False, provenance=self.recorded))])
        self.checkout.task(self.task, edited=True)
        self.checkout.suite([('local-coder-oldbox', self.task, 1, attempt(passed=True, provenance=self.recorded))])
        _, rows = self.checkout.import_()
        row = next(row for row in rows if row['metric'] == 'pass_rate')
        self.assertEqual((row['value'], row['n']), (1.0, 1))


class UncheckedKeysTests(unittest.TestCase):
    def setUp(self):
        self.checkout = Checkout(self, manifests=True)
        self.identity = self.checkout.identity('oldbox')
        self.hw_fp = hardware.latest(hardware.directory(self.checkout.root), 'workere')['hw_fp']

    def imported(self, target='oldbox'):
        recorded = {'class_id': self.identity.class_id, 'config_id': self.identity.config_id, 'hw_fp': self.hw_fp,
                    'node': 'oldbox', 'target': target}
        self.checkout.suite([('local-coder-oldbox', 'ogs-m7-error-envelope', 0, attempt(provenance=recorded))])
        return self.checkout.import_()

    def test_a_declared_target_with_its_keys_intact_still_ranks(self):
        summary, _ = self.imported()
        self.assertGreater(summary['usable'], 0)
        self.assertEqual(summary['stale'], 0)

    def test_declarations_that_cannot_be_read_leave_no_row_rankable(self):
        with patch.object(evidence.declared, 'load', side_effect=ValueError('broken yaml')):
            summary, rows = self.imported()
        self.assertTrue(rows)
        self.assertEqual(summary['usable'], 0)
        self.assertEqual(summary['stale'], len(rows))
        self.assertIn('could not be read', rows[0]['stale_reason'])

    def test_a_target_the_route_no_longer_declares_cannot_be_confirmed(self):
        summary, rows = self.imported(target='retired-target')
        self.assertEqual(summary['usable'], 0)
        self.assertIn('retired-target is no longer declared', rows[0]['stale_reason'])

    def test_a_row_with_no_target_ranks_only_while_some_target_declares_its_keys(self):
        row = {'identity': 'recorded', 'class_id': self.identity.class_id, 'config_id': self.identity.config_id,
               'hw_fp': self.hw_fp, 'node': 'workere', 'target': None}
        graph, inventory = self.checkout.graph(), hardware.directory(self.checkout.root)
        self.assertIsNone(evidence.stale_reason(row, graph, inventory))
        moved = dict(row, config_id='f' * 16)
        self.assertIn('no declared target carries', evidence.stale_reason(moved, graph, inventory))

    def test_an_unknown_row_is_still_not_stale_when_nothing_could_be_checked(self):
        row = {'identity': 'unknown', 'class_id': 'unknown', 'config_id': 'unknown', 'hw_fp': 'unknown', 'target': None}
        self.assertIsNone(evidence.stale_reason(row, None, hardware.directory(self.checkout.root)))


class BareCommandTests(unittest.TestCase):
    def test_bare_evidence_is_show_with_its_filters_unset(self):
        from agentiac import command
        checkout = Checkout(self, manifests=True)
        checkout.import_()
        out = io.StringIO()
        with patch.object(command, 'ROOT', checkout.root), patch('sys.stdout', out):
            code = command.main(['evidence'])
        self.assertEqual(code, 0)
        self.assertIn('no rows', out.getvalue())


class ReportTests(Fixture):
    def setUp(self):
        self.checkout = Checkout(self, manifests=True)
        self.identity = self.checkout.identity('oldbox')
        self.row = {'metric': 'min_available_gib', 'value': 18.7, 'n': 1, 'workload': 'context-220000',
                    'node': 'oldbox', 'class_id': self.identity.class_id, 'config_id': self.identity.config_id,
                    'hw_fp': hardware.latest(hardware.directory(self.checkout.root), 'workere')['hw_fp'],
                    'confidence': 'report', 'measured_at': iso(0),
                    'source_ref': 'docs/roadmap.md', 'why': 'transcribed from a report'}

    def test_a_transcribed_value_is_kept_and_cited(self):
        self.checkout.reports([self.row])
        self.import_()
        row = next(row for row in self.rows(metric='min_available_gib'))
        self.assertEqual(row['value'], 18.7)
        self.assertEqual(row['source_ref'], 'docs/roadmap.md')
        self.assertEqual(row['confidence'], 'report')
        self.assertTrue(evidence.usable(row))

    def test_a_transcribed_value_must_cite_a_file_that_exists(self):
        self.checkout.reports([dict(self.row, source_ref='docs/results/never-written.md')])
        with self.assertRaises(ValueError) as caught:
            evidence.report_rows(self.checkout.root)
        self.assertIn('does not exist', str(caught.exception))

    def test_a_vendor_number_is_kept_and_never_ranks(self):
        self.checkout.reports([dict(self.row, confidence='published')])
        self.import_()
        row = next(row for row in self.rows(metric='min_available_gib'))
        self.assertEqual(row['confidence'], 'published')
        self.assertFalse(evidence.usable(row))
        self.assertIn('reference only', evidence.unusable_reason(row))
        self.assertEqual(self.rows(usable_only=True), [])

    def test_a_transcribed_row_is_refused_for_an_unknown_metric_or_a_lone_key(self):
        for mutate, message in ((lambda r: r.__setitem__('metric', 'vibes'), 'is not one of'),
                                (lambda r: r.pop('config_id'), 'go together or none of them'),
                                (lambda r: r.__setitem__('value', '18.7'), 'not a number'),
                                (lambda r: r.__setitem__('n', 0), 'positive count'),
                                (lambda r: r.__setitem__('confidence', 'suite'), 'report or published')):
            row = dict(self.row)
            mutate(row)
            self.checkout.reports([row])
            with self.subTest(message=message), self.assertRaises(ValueError) as caught:
                evidence.report_rows(self.checkout.root)
            self.assertIn(message, str(caught.exception))

    def test_a_transcribed_row_without_keys_is_display_only_rather_than_refused(self):
        self.checkout.reports([{k: v for k, v in self.row.items()
                                if k not in ('class_id', 'config_id', 'hw_fp')}])
        self.import_()
        row = next(row for row in self.rows(metric='min_available_gib'))
        self.assertEqual(row['identity'], 'unknown')
        self.assertEqual(self.rows(usable_only=True), [])


class ShippedDataTests(unittest.TestCase):
    def test_the_shipped_alias_history_is_valid_and_binds_only_declared_configurations(self):
        if not (SITE / 'evidence/alias-history.yml').exists():
            checkout = Checkout(self, manifests=True)
            entry = history_entry('local-coder-oldbox', 'oldbox', checkout.identity('oldbox'), node='workere',
                                  hw_fp=hardware.latest(hardware.directory(checkout.root), 'workere')['hw_fp'])
            checkout.history([entry])
            self.assertEqual(evidence.validate_history(evidence.load_history(checkout.root), checkout.graph()), [])
            return
        entries = evidence.load_history(SITE)
        graph = declared.load(SITE)
        self.assertTrue(entries)
        self.assertEqual(evidence.validate_history(entries, graph), [])
        for entry in entries:
            if entry['target'] not in graph.route['targets']:
                self.assertRegex(entry['why'], r'\b[0-9a-f]{7,40}\b',
                                 f"{entry['alias']}: an entry for a target nobody declares must name the revision it recites")
            record = hardware.directory(SITE) / f"{entry['node']}.json"
            if not record.exists():
                continue
            self.assertEqual(hardware.fingerprint(hardware.directory(SITE), entry['node'])['hw_fp'],
                             entry['hw_fp'], f"{entry['alias']}: the hardware it claims is not what the inventory says")

    def test_a_retired_targets_ids_are_reproduced_from_the_revision_it_cites(self):
        if not (SITE / 'evidence/alias-history.yml').exists():
            checkout = Checkout(self, manifests=True)
            repo = checkout.root
            git = lambda *args: subprocess.run(('git', *args), cwd=repo, capture_output=True, text=True, check=True)
            git('init', '-q')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            target = 'workerc-strata-0.2.8-qfn-gyro-m-tq2_0-128k'
            expected = identities.compute_target(declared.load(repo), target)
            git('add', '.')
            git('commit', '-q', '-m', 'declare')
            rev = git('rev-parse', 'HEAD').stdout.strip()
            routes = json.loads((repo / 'gateway/local-coder.json').read_text())
            routes['targets'].pop(target)
            (repo / 'gateway/local-coder.json').write_text(json.dumps(routes))
            entry = history_entry('local-coder-workerc', target, expected, node='workerc',
                                  hw_fp=hardware.latest(hardware.directory(repo), 'workerc')['hw_fp'])
            entry['why'] = f'retired after {rev}'
            checkout.history([entry])
            git('add', '.')
            git('commit', '-q', '-m', 'retire')
            archive = subprocess.run(('git', 'archive', '--format=tar', rev), cwd=repo, capture_output=True, check=True).stdout
            with tempfile.TemporaryDirectory() as then:
                tarfile.open(fileobj=io.BytesIO(archive)).extractall(then, filter='data')
                identity = identities.compute_target(declared.load(then), target)
            self.assertEqual((identity.class_id, identity.config_id), (expected.class_id, expected.config_id))
            return

        if subprocess.run(('git', 'rev-parse', '--git-dir'), cwd=SITE, capture_output=True).returncode:
            self.skipTest('this checkout has no git history to read the cited revisions from')
        declared_now = declared.load(SITE)
        retired = [entry for entry in evidence.load_history(SITE) if entry['target'] not in declared_now.route['targets']]
        self.assertTrue(retired)
        cited = []
        for entry in retired:
            revision = re.search(r'\b[0-9a-f]{7,40}\b', entry['why'])[0]
            if subprocess.run(('git', 'cat-file', '-e', f'{revision}^{{commit}}'), cwd=SITE,
                              capture_output=True).returncode:
                self.skipTest(f'{revision} is not in this clone: a shallow checkout cannot re-verify a retired record')
            cited.append((entry, revision))
        for entry, revision in cited:
            with self.subTest(target=entry['target'], revision=revision):
                archive = subprocess.run(('git', 'archive', '--format=tar', revision), cwd=SITE,
                                         capture_output=True, check=True).stdout
                with tempfile.TemporaryDirectory() as then:
                    tarfile.open(fileobj=io.BytesIO(archive)).extractall(then, filter='data')
                    identity = identities.compute_target(declared.load(then), entry['target'])
                self.assertEqual(identity.class_id, entry['class_id'])
                self.assertEqual(identity.config_id, entry['config_id'])

    def test_the_shipped_reports_file_carries_no_values_yet(self):
        self.assertEqual(evidence.report_rows(SITE)[0], [])

    def test_the_evidence_store_is_not_a_source_of_truth(self):
        self.assertTrue(str(evidence.database(SITE)).endswith('artifacts/evidence/evidence.sqlite'))
        ops_doc = SITE / 'docs/operations.md'
        if ops_doc.exists():
            self.assertIn('artifacts/evidence', ops_doc.read_text())


class VariantTests(unittest.TestCase):
    def setUp(self):
        self.checkout = Checkout(self, manifests=True)
        self.identity = self.checkout.identity('oldbox')
        self.hw_fp = hardware.latest(hardware.directory(self.checkout.root), 'workere')['hw_fp']
        self.recorded = {'class_id': self.identity.class_id, 'config_id': self.identity.config_id,
                         'hw_fp': self.hw_fp, 'node': 'oldbox', 'target': 'oldbox'}

    def store(self, alias, task, *cases):
        db_path = self.checkout.suite([(alias, task, trial, result) for trial, result, _ in cases])
        with sqlite3.connect(db_path) as db:
            for row_id, spec in db.execute('SELECT id, spec FROM cases').fetchall():
                spec = json.loads(spec)
                extra = next((extra for trial, _, extra in cases if trial == spec['trial']), {})
                if spec['alias'] == alias and spec['task'] == task:
                    db.execute('UPDATE cases SET spec=? WHERE id=?', (json.dumps({**spec, **extra}), row_id))

    def test_thinking_and_the_loop_guard_are_not_pooled_with_a_plain_run(self):
        self.store('local-coder-oldbox', 'ogs-m7', (0, attempt(passed=True, provenance=self.recorded), {}),
                   (1, attempt(passed=False, provenance=self.recorded), {'thinking': 'medium'}),
                   (2, attempt(passed=False, provenance=self.recorded), {'loop_guard': {'sha256': 'x'}}))
        _, rows = self.checkout.import_()
        rates = {row['workload']: (row['value'], row['n']) for row in rows if row['metric'] == 'pass_rate'}
        self.assertEqual(rates, {'ogs-m7': (1.0, 1), 'ogs-m7 +thinking-medium': (0.0, 1),
                                 'ogs-m7 +loop-guard': (0.0, 1)})

    def test_two_unknown_aliases_are_not_one_configuration(self):
        self.checkout.suite([('alias-a', 'ogs-m7', 0, attempt(passed=True)), ('alias-b', 'ogs-m7', 0, attempt(passed=False))])
        _, rows = self.checkout.import_()
        rates = sorted((row['identity_reason'].split(':')[0], row['value']) for row in rows if row['metric'] == 'pass_rate')
        self.assertEqual(rates, [('alias-a', 1.0), ('alias-b', 0.0)])


class TranscribedTimeTests(unittest.TestCase):
    def test_an_unquoted_yaml_timestamp_does_not_stop_the_import(self):
        checkout = Checkout(self, manifests=True)
        (checkout.root / 'evidence/reports.yml').write_text(
            '- {metric: min_available_gib, value: 18.7, n: 1, workload: w, confidence: report, '
            'source_ref: docs/roadmap.md, why: y, measured_at: 1970-01-01T06:20:00+00:00}\n')
        _, rows = checkout.import_()
        self.assertEqual(rows[0]['measured_at'], '1970-01-01T06:20:00+00:00')


class HistoryOverlapTests(unittest.TestCase):
    def test_overlapping_windows_are_refused_even_with_the_same_keys(self):
        checkout = Checkout(self)
        entry = history_entry('local-coder-oldbox', 'oldbox', checkout.identity('oldbox'))
        checkout.history([entry, dict(entry, **{'from': iso(-30), 'to': iso(+90)})])
        with self.assertRaises(ValueError) as caught:
            evidence.load_history(checkout.root)
        self.assertIn('overlaps', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
