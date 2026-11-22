"""Integration: deterministic graders must reject bugs and accept valid fixes."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agentiac.tasks import coding_case

@unittest.skipUnless(os.environ.get('AGENTIAC_SANDBOX_TEST')=='1','requires local sandbox Docker image')
class GraderTests(unittest.TestCase):
    def test_reference_repairs(self):
        fixes={
          'python':('parser.py',"def decode(data):\n if len(data)%2: raise ValueError('odd length')\n return [data[i]+256*data[i+1] for i in range(0,len(data),2)]\n"),
          'cpp':('ring.cpp',None),
          'rust':('lib.rs','pub fn scale(raw:i16,factor:i16)->i16 { (raw as i32 * factor as i32).clamp(i16::MIN as i32,i16::MAX as i32) as i16 }\n')}
        for lang,(name,fix) in fixes.items():
            with self.subTest(lang=lang),tempfile.TemporaryDirectory() as tmp:
                observed=[]
                work=Path(tmp)/'workspace';calls=iter(['read_file','run_tests','write_file','run_tests','final'])
                def fake(base,key,model,messages,tools,max_tokens):
                    import json
                    stage=next(calls)
                    if messages[-1].get('role')=='tool' and messages[-1].get('tool_call_id')=='run_tests':
                        observed.append(json.loads(messages[-1]['content'])['exit_code'])
                    if stage=='final':return {'message':{'role':'assistant','content':'Done'}}
                    args={}
                    if stage=='read_file':args={'path':name}
                    if stage=='write_file':
                        content=fix or (work/name).read_text().replace('++tail;', 'tail=(tail+1)%values.size();')
                        args={'path':name,'content':content}
                    return {'message':{'role':'assistant','content':None,'tool_calls':[{'id':stage,'type':'function','function':{'name':stage,'arguments':json.dumps(args)}}]}}
                with patch('agentiac.tasks.invoke',side_effect=fake):
                    result=coding_case('','','',lang,work)
                self.assertTrue(result['passed'],result)
                # The first run_tests executed the buggy source; error must have been observed.
                self.assertNotEqual(observed[0],0)
                self.assertEqual(observed[-1],0)
