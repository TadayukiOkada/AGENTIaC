"""What one gateway inference line is allowed to say, and what it must never say (IP-06, T4.2).

`model` alone cannot tell two workers apart when both serve the same model id, and the tunnel port is what
separates them. These tests run the real callbacks module - litellm is stubbed only for its `CustomLogger`
base class, which our subclass never calls into - so the helpers are executed, not read. The body guard is
the important one: the kwargs litellm hands a callback contain `messages`, `original_response` and a
`standard_logging_object` holding the response, and none of that may reach the journal.

The same behaviour is rehearsed end to end against a throwaway proxy by
`scripts/gateway-rehearsal.py`; these tests are the part that runs on every `make check`.
"""
import asyncio
import datetime
import importlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
sys.path.insert(0, str(ROOT / 'scripts'))
import route  # noqa: E402

# Since M8 (T8.6) the callbacks have no built-in node tables; like the deployed gateway, they read the nodes.json
# scripts/route.py renders from this site's declarations. One rendering, kept for the whole run.
_NODES_DIRECTORY = tempfile.TemporaryDirectory()
NODES = Path(_NODES_DIRECTORY.name) / 'nodes.json'
NODES.write_text(route.rendered(route.current())['nodes.json'])


def load_callbacks():
    """Import gateway_callbacks with litellm's CustomLogger replaced by an empty base class."""
    litellm = sys.modules.get('litellm') or types.ModuleType('litellm')
    integrations = sys.modules.get('litellm.integrations') or types.ModuleType('litellm.integrations')
    custom_logger = types.ModuleType('litellm.integrations.custom_logger')
    custom_logger.CustomLogger = object
    litellm.integrations = integrations
    integrations.custom_logger = custom_logger
    saved = sys.modules.get('gateway_callbacks')
    sys.modules['litellm'], sys.modules['litellm.integrations'] = litellm, integrations
    sys.modules['litellm.integrations.custom_logger'] = custom_logger
    sys.modules.pop('gateway_callbacks', None)
    default = {} if 'AGENTIAC_NODES' in os.environ else {'AGENTIAC_NODES': str(NODES)}
    try:
        with mock.patch.dict(os.environ, default):
            return importlib.import_module('gateway_callbacks')
    finally:
        if saved is not None:
            sys.modules['gateway_callbacks'] = saved


callbacks = load_callbacks()
PORTS = sorted(callbacks.NODE_BY_PORT.items())
PORT_A, NODE_A = PORTS[0]
PORT_B, NODE_B = PORTS[1]


def kwargs_with_bodies(**overrides):
    """A kwargs dict shaped like the real one, with everything body-shaped filled in."""
    kwargs = {
        'model': 'worker-model-example',
        'litellm_params': {'api_base': f'http://127.0.0.1{PORT_A}/v1', 'api_key': 'WORKER-SECRET-KEY',
                           'metadata': {'user_api_key_alias': 'suite'}},
        'standard_logging_object': {'model_group': 'local-coder-worker-a-128k',
                                    'api_base': f'http://127.0.0.1{PORT_A}/v1',
                                    'messages': [{'role': 'user', 'content': 'THE PROMPT BODY'}],
                                    'response': {'choices': [{'message': {'content': 'THE RESPONSE BODY'}}]}},
        'messages': [{'role': 'user', 'content': 'THE PROMPT BODY'}],
        'original_response': 'THE RAW RESPONSE BODY',
        'stream': False,
    }
    kwargs.update(overrides)
    return kwargs


class CapturedLog:
    def __init__(self):
        self.records = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.records.append(record.getMessage())

    def __enter__(self):
        callbacks.log.addHandler(self.handler)
        callbacks.log.setLevel(logging.WARNING)
        return self

    def __exit__(self, *exc):
        callbacks.log.removeHandler(self.handler)

    def lines(self):
        return list(self.records)

    def record(self):
        assert len(self.records) == 1, f'expected one log line, got {self.records!r}'
        return json.loads(self.records[0])


class ServedByTests(unittest.TestCase):
    """The host must come from where the request ran, not from what was asked for."""

    def test_the_same_model_on_two_hosts_is_two_different_nodes(self):
        # the whole of IP-06: identical served model name, different worker
        shared = {'model': 'worker-model-example'}
        first = callbacks._served_by(dict(shared, litellm_params={'api_base': f'http://127.0.0.1{PORT_A}/v1'}))
        second = callbacks._served_by(dict(shared, litellm_params={'api_base': f'http://127.0.0.1{PORT_B}/v1'}))
        self.assertNotEqual(NODE_A, NODE_B)
        self.assertEqual((first, second), (NODE_A, NODE_B))

    def test_every_keyed_port_names_a_host(self):
        for port, key in callbacks.NODE_KEY.items():
            base = f'http://127.0.0.1{port}/v1'
            self.assertEqual(callbacks._served_by({'litellm_params': {'api_base': base}}),
                             callbacks.HOST_BY_KEY[key], base)
            self.assertEqual(callbacks.NODE_BY_PORT[port], callbacks.HOST_BY_KEY[key])

    def test_a_base_that_is_not_one_of_ours_is_nothing_rather_than_a_guess(self):
        self.assertIsNone(callbacks._served_by({'litellm_params': {'api_base': 'https://api.openai.com/v1'}}))
        self.assertIsNone(callbacks._served_by({'litellm_params': {}}))
        self.assertIsNone(callbacks._served_by({}))
        self.assertIsNone(callbacks._served_by({'litellm_params': {'api_base': None}}))

    def test_the_standard_logging_object_can_supply_the_base(self):
        kwargs = {'litellm_params': {}, 'standard_logging_object': {'api_base': f'http://127.0.0.1{PORT_B}/v1'}}
        self.assertEqual(callbacks._served_by(kwargs), NODE_B)


class RequestedAliasTests(unittest.TestCase):
    def test_the_alias_the_client_named_beats_the_backend_model_name(self):
        alias = callbacks._requested_alias(kwargs_with_bodies())
        self.assertEqual(alias, 'local-coder-worker-a-128k')
        self.assertNotEqual(alias, 'worker-model-example')

    def test_it_falls_back_to_the_model_name_rather_than_losing_the_line(self):
        for group in (None, '', 0):
            kwargs = kwargs_with_bodies(standard_logging_object={'model_group': group})
            self.assertEqual(callbacks._requested_alias(kwargs), 'worker-model-example')

    def test_no_alias_at_all_is_none_and_not_a_crash(self):
        self.assertIsNone(callbacks._requested_alias({}))
        self.assertIsNone(callbacks._requested_alias({'model': 12}))


class TimeToFirstTokenTests(unittest.TestCase):
    """TTFT exists only for a stream; litellm fills completion_start_time with end_time otherwise."""

    START = datetime.datetime(2026, 10, 31, 12, 0, 0)

    def test_a_non_streaming_call_has_no_first_token(self):
        # the trap: litellm sets completion_start_time = end_time when there is no stream,
        # so reporting it unconditionally would log the whole call as time to first token
        kwargs = kwargs_with_bodies(stream=False,
                                    completion_start_time=self.START + datetime.timedelta(seconds=9))
        self.assertIsNone(callbacks._time_to_first_token(kwargs, self.START))

    def test_a_stream_reports_the_gap_to_the_first_chunk(self):
        kwargs = kwargs_with_bodies(stream=True,
                                    completion_start_time=self.START + datetime.timedelta(seconds=2.5))
        self.assertAlmostEqual(callbacks._time_to_first_token(kwargs, self.START), 2.5)

    def test_a_stream_without_a_recorded_first_token_is_none(self):
        self.assertIsNone(callbacks._time_to_first_token(kwargs_with_bodies(stream=True), self.START))
        for value in (None, 'soon', 1793000000.0):
            kwargs = kwargs_with_bodies(stream=True, completion_start_time=value)
            self.assertIsNone(callbacks._time_to_first_token(kwargs, self.START), repr(value))

    def test_a_start_time_that_is_not_a_datetime_is_none(self):
        kwargs = kwargs_with_bodies(stream=True, completion_start_time=self.START)
        self.assertIsNone(callbacks._time_to_first_token(kwargs, 1793000000.0))


class LogLineTests(unittest.TestCase):
    """The emitted line: the fields we asked for, and nothing that smells like a body."""

    def start_end(self):
        start = datetime.datetime(2026, 10, 31, 12, 0, 0)
        return start, start + datetime.timedelta(seconds=3)

    def success(self, kwargs):
        class Usage:
            prompt_tokens, completion_tokens = 1200, 45

        class Response:
            usage = Usage()

        start, end = self.start_end()
        with CapturedLog() as captured:
            import asyncio
            asyncio.run(callbacks.MetadataLogger().async_log_success_event(kwargs, Response(), start, end))
        return captured

    def failure(self, kwargs):
        start, end = self.start_end()
        with CapturedLog() as captured:
            import asyncio
            asyncio.run(callbacks.MetadataLogger().async_log_failure_event(kwargs, None, start, end))
        return captured

    def test_the_line_carries_host_alias_timing_and_tokens(self):
        with self.success(kwargs_with_bodies()) as captured:
            record = captured.record()
        self.assertEqual(record, {'event': 'inference', 'status': 'success',
                                  'alias': 'local-coder-worker-a-128k', 'node': NODE_A,
                                  'model': 'worker-model-example', 'stream': False,
                                  'latency_s': 3.0, 'ttft_s': None,
                                  'input_tokens': 1200, 'output_tokens': 45})

    def test_a_failure_says_which_host_failed(self):
        with self.failure(kwargs_with_bodies()) as captured:
            record = captured.record()
        self.assertEqual((record['status'], record['node'], record['alias']),
                         ('error', NODE_A, 'local-coder-worker-a-128k'))
        self.assertIsNone(record['input_tokens'])

    def test_the_line_has_exactly_the_allowed_fields(self):
        with self.success(kwargs_with_bodies()) as captured:
            self.assertEqual(set(captured.record()), set(callbacks.LOG_FIELDS))

    def test_no_prompt_or_response_reaches_the_journal(self):
        for secret in ('THE PROMPT BODY', 'THE RESPONSE BODY', 'THE RAW RESPONSE BODY', 'WORKER-SECRET-KEY'):
            with self.success(kwargs_with_bodies()) as captured:
                self.assertNotIn(secret, captured.lines()[0])
            with self.failure(kwargs_with_bodies()) as captured:
                self.assertNotIn(secret, captured.lines()[0])

    def test_no_field_is_a_container_or_a_long_string(self):
        # bodies arrive as dicts, lists or long strings; scalars only, so a widening of
        # LOG_FIELDS cannot quietly start shipping content
        with self.success(kwargs_with_bodies()) as captured:
            record = captured.record()
        for field, value in record.items():
            self.assertNotIsInstance(value, (dict, list), field)
            if isinstance(value, str):
                self.assertLessEqual(len(value), 200, field)

    def test_a_streamed_call_reports_ttft_and_a_plain_one_does_not(self):
        with self.success(kwargs_with_bodies(stream=True,
                                             completion_start_time=self.start_end()[0]
                                             + datetime.timedelta(seconds=1.25))) as captured:
            streamed = captured.record()
        self.assertTrue(streamed['stream'])
        self.assertAlmostEqual(streamed['ttft_s'], 1.25)
        self.assertLess(streamed['ttft_s'], streamed['latency_s'])
        with self.success(kwargs_with_bodies(stream=False,
                                             completion_start_time=self.start_end()[0]
                                             + datetime.timedelta(seconds=3))) as captured:
            plain = captured.record()
        self.assertIsNone(plain['ttft_s'])

    def test_a_response_without_usage_reports_no_tokens_rather_than_raising(self):
        start, end = self.start_end()
        with CapturedLog() as captured:
            import asyncio
            asyncio.run(callbacks.MetadataLogger().async_log_success_event(kwargs_with_bodies(), None, start, end))
        record = captured.record()
        self.assertEqual((record['input_tokens'], record['output_tokens']), (None, None))


class FieldsAreDeliberateTests(unittest.TestCase):
    def test_the_allowed_fields_are_the_ones_we_agreed_on(self):
        self.assertEqual(set(callbacks.LOG_FIELDS),
                         {'event', 'status', 'alias', 'node', 'model', 'stream',
                          'latency_s', 'ttft_s', 'input_tokens', 'output_tokens'})

    def test_the_record_is_built_field_by_field_not_by_passing_kwargs_through(self):
        # a mutation that logs kwargs (or standard_logging_object) directly must fail here
        import ast
        tree = ast.parse((ROOT / 'gateway/gateway_callbacks.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_log':
                dumped = [call for call in ast.walk(node) if isinstance(call, ast.Call)
                          and getattr(call.func, 'attr', '') == 'dumps']
                self.assertEqual(len(dumped), 1)
                self.assertIn('LOG_FIELDS', ast.unparse(dumped[0]))
                break
        else:
            self.fail('_log is gone, so nothing controls what the gateway logs')


class IdentityStatusTests(unittest.TestCase):
    """The pre-call's read of `/ondemand/status`: only a backend with no front has nothing to say."""

    EXPECTED = {'backend_id': 'b', 'manifest_hash': 'h', 'context': 131072}

    def ask(self, status, body=None, text=None):
        class Response:
            status_code = status

            @staticmethod
            def json():
                if text is not None:
                    raise ValueError(text)
                return body

        class Client:
            def __init__(self, **_):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def get(self, *_, **__):
                return Response()

        fake = types.ModuleType('httpx')
        fake.AsyncClient, fake.Timeout = Client, lambda *a, **k: None
        with mock.patch.dict(sys.modules, {'httpx': fake}), mock.patch.dict(os.environ, {'NODE_API_KEY': 'k'}):
            return asyncio.run(callbacks._identity_problem('http://front', 'NODE', self.EXPECTED))

    def test_no_front_keeps_the_served_id_check(self):
        self.assertIsNone(self.ask(404))

    def test_a_front_that_will_not_say_is_not_a_pass(self):
        self.assertEqual(self.ask(401), 'status 401')
        self.assertEqual(self.ask(500), 'status 500')

    def test_a_body_that_cannot_be_read_is_not_a_pass(self):
        self.assertEqual(self.ask(200, text='not json'), 'status body')

    def test_a_matching_report_passes_and_a_partial_one_does_not(self):
        self.assertIsNone(self.ask(200, {'identity': self.EXPECTED}))
        self.assertEqual(self.ask(200, {'identity': {'backend_id': 'b'}}), 'manifest_hash')

    def test_a_front_with_no_manifest_says_nothing(self):
        self.assertIsNone(self.ask(200, {'identity': None}))
        self.assertIsNone(self.ask(200, {'state': 'ready'}))


if __name__ == '__main__':
    unittest.main()
