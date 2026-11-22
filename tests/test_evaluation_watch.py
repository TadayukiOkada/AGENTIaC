import unittest
from agentiac.evaluation_watch import classify, transition


class EvaluationWatchTests(unittest.TestCase):
    def snapshot(self, active='active', phase='loading'):
        return {'units': {'worker': {'ActiveState': active}},
                'status': {'worker': {'phase': phase, 'updated_at': 'heartbeat'}}}

    def test_clean_exit_requires_success_evidence(self):
        self.assertEqual(classify(self.snapshot('inactive', 'passed'), 0, 10, 20), 'completed')
        self.assertEqual(classify(self.snapshot('inactive'), 0, 10, 20), 'unexpected_stop')

    def test_failure_wins_over_completion_and_deadline(self):
        self.assertEqual(classify(self.snapshot('failed', 'passed'), 0, 30, 20), 'failed')

    def test_stall_and_deadline_are_distinct(self):
        self.assertEqual(classify(self.snapshot(), 1800, 10, 20), 'stalled')
        self.assertEqual(classify(self.snapshot(), 1800, 30, 20), 'overdue')

    def test_heartbeat_does_not_hide_stalled_progress(self):
        first, _ = transition({}, self.snapshot(), 0, 9000)
        snapshot = self.snapshot()
        snapshot['status']['worker']['updated_at'] = 'new heartbeat'
        second, alert = transition(first, snapshot, 1800, 9000)
        self.assertEqual(second['status'], 'stalled')
        self.assertTrue(alert)
        third, alert = transition(second, snapshot, 2700, 9000)
        self.assertFalse(alert)
        snapshot['result_bytes'] = 100
        fourth, alert = transition(third, snapshot, 2800, 9000)
        self.assertEqual(fourth['status'], 'running')

    def test_unreachable_is_not_model_failure(self):
        self.assertEqual(classify({'error': 'ssh'}, 0, 10, 20), 'unreachable')

    def test_completed_worker_cannot_mask_missing_control_results(self):
        s = self.snapshot('inactive', 'passed')
        s['expects_control'] = True
        self.assertEqual(classify(s, 0, 10, 20), 'unexpected_stop')
        s['status']['control'] = {'phase': 'benchmark_complete'}
        s.update(expected_requests=18, measured_requests=0)
        self.assertEqual(classify(s, 0, 10, 20), 'incomplete_results')
        s['measured_requests'] = 18
        self.assertEqual(classify(s, 0, 10, 20), 'completed')

    def test_backend_log_heartbeat_cannot_hide_stall(self):
        s = self.snapshot()
        s['log_hash'] = 'health-check-1'
        first, _ = transition({}, s, 0, 9000)
        s['log_hash'] = 'health-check-2'
        second, _ = transition(first, s, 1800, 9000)
        self.assertEqual(second['status'], 'stalled')

    def test_ack_survives_same_status_but_resets_on_new_problem(self):
        first, _ = transition({}, self.snapshot('failed'), 0, 9000)
        first['acknowledged'] = True
        second, _ = transition(first, self.snapshot('failed'), 10, 9000)
        self.assertTrue(second['acknowledged'])
        third, _ = transition(second, {'error': 'offline'}, 20, 9000)
        self.assertFalse(third['acknowledged'])

    def test_watchdog_health_catches_timer_stop_and_stale_heartbeat(self):
        from agentiac.evaluation_watch import health_problem
        self.assertIsNone(health_problem(True, 0, 1199))
        self.assertIsNotNone(health_problem(True, 0, 1201))
        self.assertIsNotNone(health_problem(False, 1200, 1201))
        self.assertIsNotNone(health_problem(True, None, 1))

    def test_unacknowledged_failure_repeats_until_explicit_ack(self):
        import json
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from agentiac import evaluation_watch as watch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'runs'
            (root / 'run').mkdir(parents=True)
            (root / 'run/manifest.json').write_text(json.dumps({'id': 'run', 'watch': {}, 'deadline_epoch': 9000}))
            state = Path(tmp) / 'state'
            args = ['check', '--root', str(root), '--state-dir', str(state)]
            with patch.object(watch, 'collect', return_value=self.snapshot('failed')), patch.object(watch, 'notify', return_value='desktop_accepted_unconfirmed'), patch.object(watch.time, 'time', return_value=1000) as clock:
                watch.main(args)
                self.assertFalse(json.loads((state / 'state.json').read_text())['batches']['run']['acknowledged'])
                clock.return_value = 1100
                watch.main(args)
                self.assertEqual(len((state / 'alerts.jsonl').read_text().splitlines()), 1)
                clock.return_value = 1900
                watch.main(args)
                self.assertEqual(len((state / 'alerts.jsonl').read_text().splitlines()), 2)
                watch.main(['ack', 'run', '--state-dir', str(state)])
                clock.return_value = 2800
                watch.main(args)
                self.assertEqual(len((state / 'alerts.jsonl').read_text().splitlines()), 2)

    def test_health_check_does_not_wait_for_main_check_lock(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from agentiac import evaluation_watch as watch
        with tempfile.TemporaryDirectory() as tmp, patch.object(watch, 'check_health') as health, patch.object(watch.fcntl, 'flock', side_effect=AssertionError('health must not lock')):
            watch.main(['health', '--state-dir', str(Path(tmp))])
            health.assert_called_once()

    def test_registered_before_launch_is_not_a_stop_alarm(self):
        snapshot = {'units': {'worker': {'ActiveState': 'inactive', 'LoadState': 'not-found'}},
                    'status': {}, 'launch_grace_until': 300}
        self.assertEqual(classify(snapshot, 0, 100, 9000), 'starting')
        _, alert = transition({}, snapshot, 100, 9000)
        self.assertFalse(alert)
        self.assertEqual(classify(snapshot, 0, 301, 9000), 'unexpected_stop')
        snapshot['units']['worker']['ActiveState'] = 'failed'
        self.assertEqual(classify(snapshot, 0, 100, 9000), 'failed')

    def test_collect_counts_concurrency_sweep_and_excludes_warmup(self):
        import json
        from pathlib import Path
        import subprocess
        import tempfile
        from unittest.mock import patch
        from agentiac import evaluation_watch as watch
        remote = {'units': {}, 'status': {'worker': {'phase': 'passed'}}}
        reply = subprocess.CompletedProcess([], 0, json.dumps(remote), '')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for concurrency, expected in [(1, 12), ([1, 2, 4], 84)]:
                with self.subTest(concurrency=concurrency):
                    manifest = {'watch': {'ssh_host': 'worker'},
                                'benchmark': {'depths': [4096, 32768],
                                              'cases': ['fresh', 'exact'],
                                              'repeats': 3, 'concurrency': concurrency}}
                    rows = [{'id': str(i), 'kind': 'fresh'} for i in range(expected)]
                    rows += [rows[0], {'id': 'warmup', 'kind': 'warmup'}]
                    (directory / 'results.jsonl').write_text(
                        ''.join(json.dumps(row) + '\n' for row in rows))
                    with patch.object(watch.subprocess, 'run', return_value=reply):
                        snapshot = watch.collect(manifest, directory)
                    snapshot['expects_control'] = True
                    snapshot['status']['control'] = {'phase': 'benchmark_complete'}
                    self.assertEqual(snapshot['expected_requests'], expected)
                    self.assertEqual(snapshot['measured_requests'], expected)
                    self.assertEqual(watch.classify(snapshot, 0, 10, 9000), 'completed')
                    snapshot['measured_requests'] -= 1
                    self.assertEqual(watch.classify(snapshot, 0, 10, 9000),
                                     'incomplete_results')
