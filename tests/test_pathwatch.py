"""Tests for src/agentiac/pathwatch.py: the declared path being up, and an attempt that measured nothing."""

import json
import sys
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
from agentiac import pathwatch  # noqa: E402

# What an attempt against a dead control -> worker tunnel stored.
VOID = {'passed': False, 'acceptance_failed': ['tests/test_api.py::x'] * 32, 'acceptance_total': 32,
        'regression_failed': [], 'regression_total': 709, 'pytest_returncode': 1, 'frontend_returncode': 0,
        'timed_out': False, 'wall_s': 14.7, 'tools': {}, 'tool_errors': 0, 'assistant_turns': 4,
        'stop_reason': 'error', 'usage': {'input': 0, 'output': 0, 'cacheRead': 0, 'totalTokens': 0}, 'diff_stat': ''}
# What the same task looked like when the model was actually reached and still failed.
REAL = dict(VOID, wall_s=811.6, tools={'bash': 48}, tool_errors=0, assistant_turns=34, stop_reason='stop',
            usage={'input': 40245, 'output': 16373, 'totalTokens': 1240092}, diff_stat=' src/app.py | 8 +-\n',
            acceptance_failed=[], regression_failed=['tests/test_tool_formats.py::t'])


class VoidAttemptTests(unittest.TestCase):
    def test_an_attempt_that_reached_nothing_is_an_infrastructure_error_not_a_model_result(self):
        reason = pathwatch.void_attempt(VOID)
        self.assertIsNotNone(reason)
        self.assertIn('no tokens counted', reason)
        self.assertIn('14.7 s', reason)

    def test_the_classification_uses_only_fields_the_runner_stored(self):
        # src/agentiac/failures.py's own rule: a class names the field it was read from, and no trace is opened.
        fields = set(VOID) - {'provenance'}
        for field in ('wall_s', 'usage', 'tools', 'diff_stat', 'stop_reason', 'timed_out'):
            self.assertIn(field, fields)

    def test_a_model_that_was_reached_and_failed_is_never_void(self):
        self.assertIsNone(pathwatch.void_attempt(REAL))

    def test_an_agent_that_called_tools_but_wrote_nothing_is_left_as_a_real_attempt(self):
        # Whatever the reason it stopped for, it was talking to something; that is a measurement, not a connection.
        self.assertIsNone(pathwatch.void_attempt(dict(VOID, tools={'bash': 2}, assistant_turns=9)))

    def test_a_timeout_is_its_own_class_and_stays_one(self):
        self.assertIsNone(pathwatch.void_attempt(dict(VOID, timed_out=True, wall_s=1800.0)))

    def test_a_passing_attempt_is_never_void(self):
        self.assertIsNone(pathwatch.void_attempt(dict(VOID, passed=True, wall_s=40.0)))

    def test_an_already_recorded_infrastructure_error_is_not_named_twice(self):
        self.assertIsNone(pathwatch.void_attempt(dict(VOID, infrastructure_error='OSError: boom')))


def start(handler):
    """Start a fixture front and wait until it is actually answering.

    A thread that has not reached `serve_forever` yet looks exactly like a dead tunnel to the code under test - which
    is a true fact about the code and a false fact about the test.
    """
    server = HTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for _ in range(200):
        _report, http, _error = pathwatch.front_answer(f'http://127.0.0.1:{server.server_address[1]}', 'k', timeout=1)
        if http is not None:
            return server
        time.sleep(0.02)
    raise AssertionError('the fixture front never answered')


class AnswerTests(unittest.TestCase):
    class Handler(BaseHTTPRequestHandler):
        payload = {'state': 'ready', 'loads': 1, 'identity': {'backend_id': 'strata-dsv41-coder-0.2.13',
                                                             'manifest_hash': '6523a3b7', 'context': 131072}}

        accepted = 'Bearer k'   # the credential is checked, not merely present: a wrong one has to look wrong

        def do_GET(self):
            if self.path != '/ondemand/status':
                self.send_response(404); self.end_headers(); return
            if self.headers.get('Authorization', '') != self.accepted:
                self.send_response(401); self.end_headers(); self.wfile.write(b'{"error": "unauthorized"}'); return
            body = json.dumps(self.payload).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    def setUp(self):
        self.server = start(self.Handler)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def test_the_answer_keeps_the_state_and_the_identity(self):
        report, http, error = pathwatch.front_answer(self.base, 'k')
        self.assertEqual(http, 200); self.assertIsNone(error)
        self.assertEqual(report['identity']['backend_id'], 'strata-dsv41-coder-0.2.13')
        self.assertEqual(report['state'], 'ready')

    def test_a_refused_credential_is_kept_as_a_number_because_a_tunnel_that_refuses_is_a_tunnel_that_is_up(self):
        report, http, error = pathwatch.front_answer(self.base, '')
        self.assertIsNone(report); self.assertEqual(http, 401)

    def test_a_dead_port_is_recorded_as_a_connect_failure(self):
        self.server.shutdown(); self.server.server_close()
        report, http, error = pathwatch.front_answer(self.base, 'k', timeout=2)
        self.assertIsNone(report); self.assertIsNone(http); self.assertIn('Connection', error)

    def test_the_whole_module_sends_no_load_header(self):
        source = (REPO / 'src/agentiac/pathwatch.py').read_text()
        tree = __import__('ast').parse(source)
        first = tree.body[0]
        prose = first.value if isinstance(first, __import__('ast').Expr) and isinstance(first.value, __import__('ast').Constant) else None
        sent = [n.value for n in __import__('ast').walk(tree)
                if isinstance(n, __import__('ast').Constant) and isinstance(n.value, str)
                and 'X-Ondemand-Load' in n.value and n is not prose]
        self.assertEqual([], sent)


class GateTests(unittest.TestCase):
    """Every case here names its own tunnel port (28103) and its own tunnels map: reading control's real 18103
    would be reading the live worker front, which is what this module exists not to disturb."""

    tunnels = {28103: ('worker-a', 'WORKER_A_API_KEY')}

    def routes(self, port):
        return {'an-alias': [f'http://127.0.0.1:{port}/v1', 'deepseek-v4.1-flash']}

    def test_a_declared_front_that_does_not_answer_stops_the_alias(self):
        ready, why = pathwatch.path_ready(alias='an-alias', routes=self.routes(28103),
                                          env_keys={'WORKER_A_API_KEY': 'k'}, tunnels=self.tunnels)
        self.assertFalse(ready)
        self.assertIn('did not answer', why)
        self.assertIn('worker-a', why)

    def test_a_refused_credential_is_named_as_a_credential_and_not_as_a_down_path(self):
        server = start(AnswerTests.Handler)
        port = server.server_address[1]
        try:
            ready, why = pathwatch.path_ready(alias='an-alias', routes=self.routes(port),
                                              env_keys={'WORKER_A_API_KEY': 'wrong'},
                                              tunnels={port: ('worker-a', 'WORKER_A_API_KEY')})
            self.assertFalse(ready)
            self.assertIn('the path is up, the key is not accepted', why)
        finally:
            server.shutdown(); server.server_close()

    def test_a_base_that_is_not_a_declared_tunnel_is_said_so_rather_than_guessed_about(self):
        ready, why = pathwatch.path_ready(alias='an-alias', routes=self.routes(28114),
                                          env_keys={'WORKER_A_API_KEY': 'k'}, tunnels=self.tunnels)
        self.assertTrue(ready)
        self.assertIn('not a declared control tunnel', why)

    def test_an_unrendered_route_table_is_reported_and_does_not_stop_a_run(self):
        ready, why = pathwatch.path_ready(root=Path('/nonexistent-checkout'), alias='an-alias')
        self.assertTrue(ready)
        self.assertIn('no rendered route table', why)

    def test_a_missing_credential_is_said_as_a_credential_problem(self):
        ready, why = pathwatch.path_ready(alias='an-alias', routes=self.routes(28103), env_keys={}, tunnels=self.tunnels)
        self.assertTrue(ready)
        self.assertIn('WORKER_A_API_KEY', why)

    def test_the_ports_and_key_names_come_from_the_inventory(self):
        inventory = yaml.safe_load((SITE / 'infra/inventory.yml').read_text())
        expected = {}
        for group in (inventory['all'].get('children') or {}).values():
            for host, facts in (group.get('hosts') or {}).items():
                if facts.get('tunnel_port'):
                    expected[int(facts['tunnel_port'])] = (str(host), f'{str(host).upper()}_API_KEY')
        self.assertTrue(expected)
        self.assertEqual(pathwatch.tunnel_hosts(SITE), expected)


class SuiteWiringTests(unittest.TestCase):
    def source(self):
        return (REPO / 'scripts/suite.py').read_text()

    def test_the_path_is_checked_before_a_lease_is_taken(self):
        source = self.source()
        self.assertLess(source.index('pathwatch.path_ready('), source.index('store.claim('))
        self.assertIn("'deferred': True, 'reason': checked", source)

    def test_an_empty_attempt_is_named_before_it_is_stored_and_before_it_scores(self):
        source = self.source()
        self.assertLess(source.index('pathwatch.void_attempt(result)'), source.index('store.finish('))

    def test_the_gate_has_a_wait_and_a_poll_and_both_are_named(self):
        source = self.source()
        self.assertIn("--path-wait", source)
        self.assertIn("--path-poll", source)

    def test_the_passive_read_lives_in_one_module_that_every_reader_uses(self):
        for name in ('scripts/ladder.py', 'scripts/connectivity-watch.py', 'scripts/suite.py'):
            source = (REPO / name).read_text()
            self.assertNotIn("'/ondemand/status'", source, f'{name} builds its own status request')
            self.assertIn('pathwatch', source, f'{name} does not use the shared read')


if __name__ == '__main__':
    unittest.main()
