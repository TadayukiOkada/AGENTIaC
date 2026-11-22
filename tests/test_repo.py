import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from agentiac.repo import repository_case

@unittest.skipUnless(os.environ.get('AGENTIAC_SANDBOX_TEST')=='1','requires local sandbox Docker image')
class RepositoryTests(unittest.TestCase):
    def test_primary_clone_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);original=root/'original';original.mkdir();(original/'answer.py').write_text('VALUE=1\n')
            subprocess.run(['git','init',str(original)],check=True,capture_output=True)
            subprocess.run(['git','-C',str(original),'add','answer.py'],check=True)
            subprocess.run(['git','-C',str(original),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','fixture'],check=True,capture_output=True)
            rev=subprocess.check_output(['git','-C',str(original),'rev-parse','HEAD'],text=True).strip()
            stages=iter(['read_file','write_file','run_tests','final'])
            def fake(*args,**kwargs):
                self.assertEqual(kwargs.get('reasoning_effort'),'none')
                stage=next(stages)
                if stage=='final':return {'message':{'role':'assistant','content':'Done'}}
                a={'path':'answer.py'} if stage=='read_file' else {'path':'answer.py','content':'VALUE=2\n'} if stage=='write_file' else {}
                return {'message':{'role':'assistant','tool_calls':[{'id':stage,'type':'function','function':{'name':stage,'arguments':json.dumps(a)}}]}}
            spec={'repo_path':str(original),'revision':rev,'image':'agentiac-eval:baseline','instruction':'Set VALUE to2','reasoning_effort':'none','test_command':['python3','-c','from answer import VALUE; assert VALUE==2']}
            attempt=root/'attempt';attempt.mkdir()
            with patch('agentiac.repo.invoke',side_effect=fake):result=repository_case('','','',spec,attempt)
            self.assertTrue(result['passed'],result);self.assertNotEqual(result['baseline_returncode'],0)
            self.assertEqual((original/'answer.py').read_text(),'VALUE=1\n')
            self.assertEqual(subprocess.check_output(['git','-C',str(original),'status','--porcelain'],text=True),'')

    def test_tests_are_protected_by_canonical_path_and_read_only_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);original=root/'original';original.mkdir()
            (original/'tests').mkdir();(original/'src').mkdir()
            (original/'tests/marker.py').write_text('MARKER=1\n')
            (original/'answer.py').write_text('VALUE=1\n')
            subprocess.run(['git','init','-q',str(original)],check=True)
            subprocess.run(['git','-C',str(original),'add','.'],check=True)
            subprocess.run(['git','-C',str(original),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
            rev=subprocess.check_output(['git','-C',str(original),'rev-parse','HEAD'],text=True).strip()
            stages=iter(['protected','write','run_tests','final'])
            def fake(*args,**kwargs):
                messages=args[3]
                stage=next(stages)
                if stage=='final':return {'message':{'role':'assistant','content':'Done'}}
                if stage=='write':self.assertIn('protected grading path',messages[-1]['content'])
                name='run_tests' if stage=='run_tests' else 'write_file'
                arguments={} if name=='run_tests' else {'path':'src/../tests/marker.py' if stage=='protected' else 'answer.py','content':'MARKER=2\n' if stage=='protected' else 'VALUE=2\n'}
                return {'message':{'role':'assistant','tool_calls':[{'id':stage,'type':'function','function':{'name':name,'arguments':json.dumps(arguments)}}]}}
            grader="from pathlib import Path\np=Path('tests/marker.py')\ntry: p.write_text('changed')\nexcept OSError: pass\nelse: raise AssertionError('test mount is writable')\nassert p.read_text()=='MARKER=1\\n'\nfrom answer import VALUE\nassert VALUE==2"
            attempt=root/'attempt';attempt.mkdir()
            with patch('agentiac.repo.invoke',side_effect=fake):
                result=repository_case('','','',{'repo_path':str(original),'revision':rev,'image':'agentiac-eval:baseline','instruction':'repair','test_command':['python3','-c',grader]},attempt)
            self.assertTrue(result['passed'],result)
            self.assertEqual((attempt/'workspace/tests/marker.py').read_text(),'MARKER=1\n')
