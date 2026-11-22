import io
import json
import unittest
from unittest.mock import patch
from agentiac.client import request

class ClientTests(unittest.TestCase):
    def test_fragmented_streaming_tool_arguments(self):
        chunks=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':'call_1','function':{'name':'lookup','arguments':'{"n":'}}]}}]},
                {'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':'2}'}}]},'finish_reason':'tool_calls'}]},
                {'choices':[],'usage':{'prompt_tokens':12,'completion_tokens':8}}]
        body=b''.join(b'data: '+json.dumps(c).encode()+b'\n\n' for c in chunks)+b'data: [DONE]\n\n'
        with patch('urllib.request.urlopen',return_value=io.BytesIO(body)):
            result=request('http://127.0.0.1/v1','test',{'stream':True})
        self.assertEqual(result['message']['tool_calls'][0]['function']['arguments'],'{"n":2}')
        self.assertEqual(result['usage']['completion_tokens'],8)
        self.assertEqual(result['finish_reason'],'tool_calls')

    def test_stream_without_finish_reason_is_rejected(self):
        chunk = {'choices': [{'delta': {'content': 'partial'}}]}
        for ending in (b'', b'data: [DONE]\n\n'):
            with self.subTest(ending=ending):
                body = b'data: ' + json.dumps(chunk).encode() + b'\n\n' + ending
                with patch('urllib.request.urlopen', return_value=io.BytesIO(body)):
                    with self.assertRaisesRegex(RuntimeError, 'without finish reason'):
                        request('http://127.0.0.1/v1', 'test', {'stream': True})

    def test_output_cap_remains_visible_for_throughput_measurements(self):
        chunk = {'choices': [{'delta': {'content': 'capped'}, 'finish_reason': 'length'}]}
        body = b'data: ' + json.dumps(chunk).encode() + b'\n\n'
        with patch('urllib.request.urlopen', return_value=io.BytesIO(body)):
            result = request('http://127.0.0.1/v1', 'test', {'stream': True})
        self.assertEqual(result['finish_reason'], 'length')

if __name__=='__main__':unittest.main()
