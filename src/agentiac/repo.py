"""Plug-in repository evaluation using an isolated clone and fixed local test image."""
import json
from pathlib import Path
import subprocess
from .tasks import invoke, tool


def repository_case(base,key,model,spec,folder):
    """spec is a control-local task manifest; it must never be committed for private repos."""
    if 'reasoning_effort' in spec and spec['reasoning_effort'] != 'none':raise ValueError('only explicit reasoning_effort=none is supported')
    work=folder/'workspace'
    subprocess.run(['git','clone','--no-hardlinks','--no-checkout',spec['repo_path'],str(work)],check=True,capture_output=True)
    resolved=subprocess.check_output(['git','-C',str(work),'rev-parse',spec['revision']+'^{commit}'],text=True).strip()
    if resolved!=spec['revision']:raise ValueError('repository task needs full immutable commit')
    subprocess.run(['git','-C',str(work),'checkout','--detach',resolved],check=True,capture_output=True)
    tracked=subprocess.check_output(['git','-C',str(work),'ls-files','-z']).decode().split('\0')
    tracked=[p for p in tracked if p]
    def safe(name):
        p=(work/name).resolve()
        if not p.is_relative_to(work.resolve()) or '.git' in Path(name).parts or p==work.resolve():raise ValueError('path not allowed')
        return p
    def test():
        argv=['docker','run','--rm','--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
              '--memory=4g','--memory-swap=4g','--cpus=4','--pids-limit=256','--user','1000:1000',
              '-e','PYTHONDONTWRITEBYTECODE=1','--tmpfs','/tmp:rw,exec,nosuid,size=1g','-v',str(work.resolve())+':/work:rw','-w','/work']
        if (work/'tests').is_dir():argv+=['-v',str((work/'tests').resolve())+':/work/tests:ro']
        if spec.get('grader_dir'):argv+=['-v',str(Path(spec['grader_dir']).resolve())+':/grader:ro']
        argv += [spec['image']]+spec['test_command']
        return subprocess.run(argv,capture_output=True,text=True,timeout=min(spec.get('test_timeout_s',300),900))
    baseline=test()
    context=[]
    for name in spec.get('context_paths',[]):
        content=safe(name).read_text()
        context.append(f'FILE: {name}\n{content}')
    messages=[{'role':'system','content':'Implement the requested repository change using tools. Read relevant files, edit source and run tests. Tests run in an isolated network-disabled container. Do not change tests to hide defects.'},
              {'role':'user','content':spec['instruction']+'\nTracked files:\n'+'\n'.join(tracked[:5000])+'\nRepository context:\n'+'\n\n'.join(context)}]
    tools=[tool('list_files','List tracked repository files',{},[]),tool('read_file','Read UTF-8 source',{'path':{'type':'string'}},['path']),
           tool('write_file','Replace source file',{'path':{'type':'string'},'content':{'type':'string'}},['path','content']),
           tool('run_tests','Run fixed repository build/tests',{},[])]
    trace=[];wrote=False;tested=False;last_test_returncode=None
    def checkpoint(stage, turn):
        # Private control-local evidence survives an HTTP/tool failure before final result.json.
        temporary=folder/'partial-trace.json.tmp'
        temporary.write_text(json.dumps({'stage':stage,'turn':turn,'repository_revision':resolved,
                                         'trace':trace,'messages':messages,'wrote':wrote,'tested':tested,
                                         'baseline_returncode':baseline.returncode,
                                         'last_test_returncode':last_test_returncode},indent=2)+'\n')
        temporary.replace(folder/'partial-trace.json')
    for turn in range(spec.get('max_turns',30)):
        checkpoint('request',turn+1)
        r=invoke(base,key,model,messages,tools,max_tokens=4096,**({'reasoning_effort':'none'} if 'reasoning_effort' in spec else {}));trace.append(r);msg=r['message'];messages.append(msg)
        checkpoint('response',turn+1)
        if r.get('finish_reason') == 'length':
            return {'passed':False,'reason':'repository response output limit reached',
                    'failure_category':'model_output_limit','truncated':True,
                    'trace':trace,'turns':turn+1,'repository_revision':resolved}
        if not msg.get('tool_calls'):
            final=test();diff=subprocess.check_output(['git','-C',str(work),'diff','--stat'],text=True)
            return {'passed':final.returncode==0 and wrote and tested,'baseline_returncode':baseline.returncode,
                    'test_returncode':final.returncode,'test_output':(final.stdout+final.stderr)[-16000:],
                    'trace':trace,'turns':turn+1,'diff_stat':diff,'repository_revision':resolved}
        for call in msg['tool_calls']:
            try:
                a=json.loads(call['function']['arguments']);name=call['function']['name']
                if name=='list_files':result={'files':tracked[:5000]}
                elif name=='read_file':result={'content':safe(a['path']).read_text()[:200000]}
                elif name=='write_file':
                    dest=safe(a['path'])
                    canonical=dest.relative_to(work.resolve()).as_posix()
                    protected={safe(p).relative_to(work.resolve()).as_posix() for p in spec.get('protected_paths',[])}
                    if canonical.split('/')[0]=='tests' or canonical in protected:raise ValueError('protected grading path')
                    dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(a['content']);wrote=True;result={'ok':True}
                elif name=='run_tests':
                    last_test_returncode=None
                    t=test();tested=True;last_test_returncode=t.returncode
                    result={'exit_code':t.returncode,'output':(t.stdout+t.stderr)[-16000:]}
                else:raise ValueError('unknown tool')
            except (ValueError,KeyError,OSError,TypeError,subprocess.TimeoutExpired) as exc:result={'error':str(exc)[:1000]}
            messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(result)})
        checkpoint('tools',turn+1)
    checkpoint('turn-budget-exhausted',len(trace))
    return {'passed':False,'reason':'repository turn budget exceeded','trace':trace,
            'turns':len(trace),'repository_revision':resolved,'baseline_returncode':baseline.returncode,
            'last_test_returncode':last_test_returncode,'wrote':wrote,'tested':tested}
