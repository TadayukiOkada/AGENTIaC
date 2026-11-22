import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('protocol_smoke', Path(__file__).resolve().parents[1] / 'scripts/protocol-smoke.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProtocolTests(unittest.TestCase):
    def test_anthropic_fragmented_json_and_final_event_required(self):
        events = [
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'call-1', 'name': 'read_status', 'input': {}}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{"bus":'}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '2}'}},
            {'type': 'message_stop'},
        ]
        self.assertEqual(module.unpack('messages', events, True)[0]['input'], {'bus': 2})
        with self.assertRaises(ValueError):
            module.unpack('messages', events[:-1], True)

    def test_responses_uses_completed_output_and_rejects_truncation(self):
        output = [{'type': 'function_call', 'name': 'read_status', 'call_id': 'c', 'arguments': '{"bus":2}'}]
        self.assertEqual(module.unpack('responses', [{'type': 'response.completed', 'response': {'output': output}}], True), output)
        with self.assertRaises(ValueError):
            module.unpack('responses', [{'type': 'response.incomplete'}], True)

    def test_chat_stream_rejects_missing_finish_and_collects_calls(self):
        events = [{'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'read_status', 'arguments': '{"bus":'}}]}}]},
                  {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '2}'}}]}, 'finish_reason': 'tool_calls'}]}]
        self.assertEqual(module.unpack('chat', events, True)['tool_calls'][0]['function']['arguments'], '{"bus":2}')
        with self.assertRaises(ValueError):
            module.unpack('chat', events[:-1], True)
