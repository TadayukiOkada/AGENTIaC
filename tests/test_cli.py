import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agentiac.cli import main
from agentiac.state import Store

class CliTests(unittest.TestCase):
    def test_completed_tool_cases_serialize_and_skip_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);profile=root/'profile.json';profile.write_text('{}');key=root/'key';key.write_text('LITELLM_MASTER_KEY=fake-test-key\nWORKER_KEY=fake-worker-key\n')
            reply={'message':{'role':'assistant','content':'ok'},'elapsed_s':1.0,'ttft_s':0.1,'usage':{'completion_tokens':2}}
            tool={'passed':True,'trace':[reply],'tool_calls':1,'turns':1}
            argv=['agentiac','run','--model','example-alias','--suite','tools','--profile',str(profile),'--root',str(root/'run'),'--key-file',str(key)]
            with patch('sys.argv',argv),patch('agentiac.cli.invoke',return_value=reply),patch('agentiac.cli.tool_case',side_effect=lambda *a,**k:dict(tool)) as call,patch('agentiac.cli.structured_case',side_effect=lambda *a,**k:dict(tool)),patch('agentiac.cli.abstention_case',side_effect=lambda *a,**k:dict(tool)),contextlib.redirect_stdout(io.StringIO()):
                main();n=call.call_count;main();self.assertEqual(call.call_count,n)
            rows=Store(root/'run/state.sqlite').summary()
            self.assertTrue(all(r['status']=='complete' and r['attempts']==1 for r in rows))
            for path in (root/'run').glob('*/*/result.json'):
                result=json.loads(path.read_text())['result']
                if 'trace' in result:self.assertIn('request_metrics',result)
