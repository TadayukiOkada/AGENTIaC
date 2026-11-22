"""Tests for scripts/connectivity-watch.py: the watch that decides whether a host can be run on."""

import ast
import calendar
import datetime
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
from agentiac import paths  # noqa: E402
SITE = paths.root()
spec = importlib.util.spec_from_file_location('connectivity_watch', REPO / 'scripts/connectivity-watch.py')
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def sample(epoch, ok, interval=30):
    return {'t': '1970-01-01T00:00:00Z', 'epoch': epoch, 'ok': ok, 'interval_s': interval}


class StabilityTests(unittest.TestCase):
    def test_a_continuously_answering_path_accumulates(self):
        base = 1_000_000.0
        samples = [sample(base + i * 30, True) for i in range(11)]   # 5 minutes of 30 s samples
        self.assertAlmostEqual(watch.stable_seconds(samples, now=base + 300), 300.0)

    def test_one_failed_sample_cuts_the_streak_to_zero(self):
        base = 1_000_000.0
        samples = [sample(base + i * 30, True) for i in range(10)] + [sample(base + 300, False)]
        self.assertEqual(watch.stable_seconds(samples, now=base + 300), 0.0)

    def test_a_streak_after_an_outage_starts_at_the_recovery(self):
        base = 1_000_000.0
        samples = ([sample(base + i * 30, True) for i in range(5)]
                   + [sample(base + 150 + i * 30, False) for i in range(4)]
                   + [sample(base + 270 + i * 30, True) for i in range(3)])
        self.assertAlmostEqual(watch.stable_seconds(samples, now=base + 330), 60.0)

    def test_a_gap_in_the_samples_is_not_evidence_of_a_stable_path(self):
        # The watcher was not running: three minutes of silence, then answers again. Only the new stretch counts.
        base = 1_000_000.0
        samples = [sample(base, True), sample(base + 60, True), sample(base + 1_200, True), sample(base + 1_230, True)]
        self.assertAlmostEqual(watch.stable_seconds(samples, now=base + 1_230), 30.0)

    def test_no_samples_is_not_stable(self):
        self.assertEqual(watch.stable_seconds([]), 0.0)


class SampleStampTests(unittest.TestCase):
    """The stamps are UTC and the clock is UTC; control's own clock is five hours behind, so a local-time
    reading of a `Z` stamp moves every sample into the future and a stable path measures as zero."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        stamp_dt = datetime.datetime(2026, 11, 11, 8, 40, 8, tzinfo=datetime.timezone.utc)
        self.stamp = stamp_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        earlier = (stamp_dt - datetime.timedelta(seconds=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.log = Path(self._tmp.name)
        self.log.write_text(json.dumps({'t': earlier, 'ok': True, 'interval_s': 30}) + '\n'
                            + json.dumps({'t': self.stamp, 'ok': True, 'interval_s': 30}) + '\n')

    def test_a_sample_stamped_in_utc_is_placed_on_the_utc_clock(self):
        samples = watch.read_samples(self.log)
        self.assertAlmostEqual(max(s['epoch'] for s in samples),
                               calendar.timegm(time.strptime(self.stamp, '%Y-%m-%dT%H:%M:%SZ')), delta=1)

    def test_a_streak_measured_against_the_real_clock_is_not_zero(self):
        now = calendar.timegm(time.strptime(self.stamp, '%Y-%m-%dT%H:%M:%SZ'))
        self.assertAlmostEqual(watch.stable_seconds(watch.read_samples(self.log), now=now), 30.0, delta=1)

    def tearDown(self):
        self._tmp.close()


class ProbeTests(unittest.TestCase):
    class Handler(BaseHTTPRequestHandler):
        payload = {'state': 'ready', 'loads': 1, 'unloads': 0, 'idle_for_s': 12, 'last_error': None,
                   'identity': {'backend_id': 'strata-dsv41-coder-0.2.13',
                                'manifest_hash': '6523a3b7b75a3e6b', 'context': 131072}}

        def do_GET(self):
            if self.path != '/ondemand/status':
                self.send_response(404); self.end_headers(); return
            if not self.headers.get('Authorization', '').startswith('Bearer '):
                self.send_response(401); self.end_headers()
                self.wfile.write(b'{"error": "unauthorized"}'); return
            body = json.dumps(self.payload).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    def setUp(self):
        self.server = HTTPServer(('127.0.0.1', 0), self.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def test_a_front_that_answers_is_read_as_what_it_declares(self):
        got = watch.probe('worker-a', port=self.port, key='k')
        self.assertTrue(got['ok'])
        self.assertEqual(got['http'], 200)
        self.assertEqual(got['identity']['backend_id'], 'strata-dsv41-coder-0.2.13')
        self.assertEqual(got['front']['state'], 'ready')

    def test_an_unauthorized_read_is_a_failure_not_a_mystery(self):
        got = watch.probe('worker-a', port=self.port, key=None)
        self.assertFalse(got['ok'])
        self.assertEqual(got['http'], 401)
        self.assertEqual(got['identity'], {})

    def test_a_dead_tunnel_is_recorded_as_a_connect_failure(self):
        self.server.shutdown(); self.server.server_close()      # close the port, then probe it
        got = watch.probe('worker-a', port=self.port, key='k', timeout=2)
        self.assertFalse(got['ok'])
        self.assertIsNone(got['http'])
        self.assertIn('Connection', got['error'])


class DeclaredPathTests(unittest.TestCase):
    def source(self):
        return (REPO / 'scripts/connectivity-watch.py').read_text()

    def test_the_ports_it_probes_come_from_the_inventory_that_declares_them(self):
        inventory = yaml.safe_load((SITE / 'infra/inventory.yml').read_text())
        declared = {}
        for group in (inventory['all'].get('children') or {}).values():
            for host, facts in (group.get('hosts') or {}).items():
                if facts.get('tunnel_port'):
                    declared[host] = int(facts['tunnel_port'])
        self.assertEqual(watch.TUNNEL_PORT, declared)

    def test_the_hosts_this_plan_runs_on_are_addressed_through_the_ports_it_watches(self):
        import re
        inventory = yaml.safe_load((SITE / 'infra/inventory.yml').read_text())
        config = yaml.safe_load((SITE / 'gateway/config.yaml').read_text())
        ports = {int(m.group(1)) for d in config['model_list']
                 for m in [re.search(r'127\.0\.0\.1:(\d+)', str(d['litellm_params']['api_base']))] if m}
        hosts = [host for group in (inventory['all'].get('children') or {}).values()
                 for host, facts in (group.get('hosts') or {}).items()
                 if facts.get('tunnel_port') and facts.get('backend') != 'strata']
        self.assertTrue(hosts)
        for host in hosts:
            self.assertIn(watch.TUNNEL_PORT[host], ports, f'{host}\'s declared tunnel port is not an api_base the gateway uses')

    def test_the_read_is_passive_so_the_watch_can_never_load_a_model_or_count_as_use(self):
        # The load header may appear in the file's prose - what must not exist is a string constant that sends it.
        tree = ast.parse(self.source())
        first = tree.body[0]
        prose = first.value if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) else None
        sent = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and 'X-Ondemand-Load' in node.value and node is not prose]
        self.assertEqual([], sent)
        self.assertIn('/ondemand/status', self.source())

    def test_it_never_prints_a_key_and_never_builds_the_header_itself(self):
        watch_source, pathwatch_source = self.source(), (REPO / 'src/agentiac/pathwatch.py').read_text()
        for name, source in (('the watch', watch_source), ('pathwatch', pathwatch_source)):
            self.assertNotIn('print(key', source, name)
            self.assertNotIn("print(f'{key}", source, name)
        self.assertIn("headers = {'Authorization': 'Bearer ' + key} if key else {}", pathwatch_source)
        self.assertNotIn('Authorization', watch_source)   # only the shared read sends a credential

    def test_it_writes_only_under_artifacts_connectivity(self):
        self.assertIn("OUT = REPO / 'artifacts/connectivity'", self.source())


if __name__ == '__main__':
    unittest.main()
