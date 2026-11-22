import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('aggregate_results', Path(__file__).resolve().parents[1] / 'scripts/aggregate-results.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AggregateTests(unittest.TestCase):
    def test_latest_attempts_provenance_separation_and_trace_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite'
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE cases(id, spec, status, attempts); CREATE TABLE attempts(case_id, number, result);')
                rows = [
                    ('retry', 'complete', 2, 'v1', {'passed': True, 'generation_tokens_s': 6, 'message': 'PRIVATE_SENTINEL'}),
                    ('infra', 'failed', 1, 'v1', {'passed': False, 'failure_category': 'infrastructure'}),
                    ('model', 'failed', 1, 'v1', {'passed': False}),
                    ('active', 'running', 1, 'v1', {}),
                    ('legacy', 'failed', 1, 'v1', {'passed': False, 'error_type': 'HTTPError'}),
                    ('other', 'complete', 1, 'v2', {'passed': True}),
                ]
                for key, status, number, revision, result in rows:
                    identity = {'task': 'tool-single', 'trial': key, 'alias': 'worker', 'benchmark_source_sha256': revision}
                    db.execute('INSERT INTO cases VALUES(?,?,?,?)', (key, json.dumps(identity), status, number))
                    db.execute('INSERT INTO attempts VALUES(?,?,?)', (key, number, json.dumps(result)))
                db.execute('INSERT INTO attempts VALUES(?,?,?)', ('retry', 1, json.dumps({'passed': False})))
            report = module.aggregate(path)
            self.assertEqual(len(report['groups']), 2)
            task = report['groups'][0]['tasks']['tool-single']
            self.assertEqual((task['passed'], task['model_failed'], task['infrastructure_failed'], task['unfinished']), (1, 1, 1, 1))
            self.assertEqual(task['unclassified_failed'], 1)
            self.assertEqual(task['pass_rate'], 0.5)
            self.assertEqual(task['generation_tokens_s_median'], 6)
            self.assertNotIn('PRIVATE_SENTINEL', json.dumps(report))
            self.assertIn('1/2', module.markdown(report))
