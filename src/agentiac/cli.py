import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
from . import declared
from .secrets import read_key, read_env, redact
from .state import Store, stable_id
from .repo import repository_case
from .tasks import coding_case, tool_case, structured_case, abstention_case, invoke


def source_hash():
    h=hashlib.sha256()
    for p in sorted(Path(__file__).parent.glob('*.py')):
        h.update(p.name.encode());h.update(p.read_bytes())
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser(description='Incremental local coding/tool benchmark')
    parser.add_argument('action',choices=['run','status','export'])
    parser.add_argument('--base',default=declared.gateway_url(path='/v1'))
    parser.add_argument('--model',required=True,help='the gateway alias to measure')
    parser.add_argument('--profile',type=Path)
    parser.add_argument('--deployment',type=Path)
    parser.add_argument('--repo-task',type=Path)
    parser.add_argument('--suite',choices=['tools','coding','stage-a','repository'],default='stage-a')
    parser.add_argument('--root',type=Path,default=Path('artifacts/benchmarks'))
    parser.add_argument('--concurrency',type=int,choices=[1,2,3],default=1)
    parser.add_argument('--trials',type=int,default=1)
    parser.add_argument('--retry-failed',action='store_true')
    parser.add_argument('--key-file',type=Path,default=Path('secrets/gateway.env'))
    args=parser.parse_args()
    os.umask(0o077)
    args.root.mkdir(parents=True,exist_ok=True)
    store=Store(args.root/'state.sqlite')
    if args.action in ('status','export'):
        rows=store.summary()
        if args.action=='status':
            for row in rows:
                spec=json.loads(row['spec']);print(row['id'][:12],spec['alias'],spec['task'],row['status'],row['attempts'])
        else:
            # No raw traces/prompts/code. Export only explicit scalar metrics.
            with store.connect() as db:
                for row in db.execute('SELECT c.id,c.spec,a.result,a.number FROM cases c JOIN attempts a ON a.case_id=c.id WHERE a.finished IS NOT NULL'):
                    spec=json.loads(row['spec']);r=json.loads(row['result'] or '{}')
                    print(json.dumps({'id':row['id'],'model':spec['alias'],'task':spec['task'],'concurrency':spec['concurrency'],
                                      'attempt':row['number'],**{k:r.get(k) for k in ['passed','turns','tool_calls','elapsed_s','ttft_s','generation_tokens_s','usage','parallel_observed','request_metrics','task_wall_s']}}))
        return
    if not args.profile:
        parser.error('--profile is required to attribute a run to exact model/backend configuration')
    profile=json.loads(args.profile.read_text())
    deployment=json.loads(args.deployment.read_text()) if args.deployment else {'status':'unverified deployment; exploratory only'}
    if args.deployment and deployment['profile']!=profile:
        parser.error('deployment profile differs from requested profile')
    deployment.pop('collected_at',None)
    deployment.pop('container_id',None)
    eval_image=None
    if args.suite!='tools':
        import subprocess
        eval_image=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',declared.resource('eval:baseline')],text=True).strip()
    key=os.environ.get('AGENTIAC_API_KEY','')
    if not key and args.key_file.exists():
        key=read_key(args.key_file)
    if eval_image:
        os.environ['AGENTIAC_EVAL_IMAGE']=eval_image
    if any(c in key for c in '\r\n'):parser.error('credential contains a newline')
    secret_values=[key]
    if args.key_file.exists():secret_values+=list(read_env(args.key_file).values())
    repo_spec=None
    if args.suite=='repository':
        if not args.repo_task:parser.error('--repo-task required for repository suite')
        repo_spec=json.loads(args.repo_task.read_text())
        import subprocess
        repo_spec['image']=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',repo_spec['image']],text=True).strip()
    tasks=[]
    if args.suite in ('tools','stage-a'):tasks+=['throughput','tool-single','tool-parallel','tool-error-recovery','tool-structured','tool-nonstream','tool-abstention']
    if args.suite in ('coding','stage-a'):tasks+=['coding-python','coding-cpp','coding-rust']
    if args.suite=='repository':tasks=['repository']
    specs=[{'repo_task':repo_spec,'profile':profile,'deployment':deployment,'eval_image':eval_image,'benchmark_source_sha256':source_hash(),'alias':args.model,'endpoint':args.base,
            'task':task,'trial':trial,'seed':42,'concurrency':args.concurrency}
           for trial in range(args.trials) for task in tasks]
    # Exclusive controller lock makes immediate recovery after killed/logged-out runner safe.
    with (args.root/'controller.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with store.connect() as db:
            db.execute("UPDATE attempts SET status='interrupted' WHERE status='running'")
            db.execute("UPDATE cases SET status='pending',owner=NULL,lease_until=NULL WHERE status='running'")
        def run(spec):
            cid=store.register(spec);claim=store.claim(cid,args.retry_failed,lease_seconds=14400)
            if not claim:
                with store.connect() as db:
                    status=db.execute('SELECT status FROM cases WHERE id=?',(cid,)).fetchone()[0]
                return {'id':cid,'skipped':True,'status':status,'passed':status=='complete'}
            owner,number=claim
            folder=args.root/cid/str(number);folder.mkdir(parents=True,exist_ok=True)
            try:
                wall_started=time.monotonic()
                task=spec['task']
                if task=='repository':
                    result=repository_case(args.base,key,args.model,repo_spec,folder)
                elif task=='throughput':
                    r=invoke(args.base,key,args.model,[{'role':'user','content':'Write a complete C++17 bounded FIFO ring buffer template with push, pop, capacity and empty. Include explanatory comments and a usage example.'}],max_tokens=512)
                    result={'passed':bool(r.get('usage',{}).get('completion_tokens')),'truncated':r.get('finish_reason')=='length',**r}
                elif task=='tool-abstention':
                    result=abstention_case(args.base,key,args.model)
                elif task in ('tool-structured','tool-nonstream'):
                    result=structured_case(args.base,key,args.model,stream=task!='tool-nonstream')
                elif task.startswith('tool-'):
                    result=tool_case(args.base,key,args.model,task[5:])
                else:
                    result=coding_case(args.base,key,args.model,task[7:],folder/'workspace')
                result['task_wall_s']=time.monotonic()-wall_started
                if result.get('trace'):
                    metrics=[{k:r.get(k) for k in ['elapsed_s','ttft_s','generation_tokens_s','usage','finish_reason','timings']} for r in result['trace']]
                    result['request_metrics']=metrics
                    result['elapsed_s']=sum(r.get('elapsed_s') or 0 for r in metrics)
                trace=folder/'result.json';temp=folder/'result.json.tmp'
                temp.write_text(json.dumps({'spec':spec,'result':result},indent=2)+'\n');temp.replace(trace)
                safe={k:v for k,v in result.items() if k not in ['message','trace','test_output','request_payload','diff_stat']}
                safe['artifact']=str(trace)
                store.finish(cid,owner,number,result['passed'],safe)
                return {'id':cid,'task':task,**safe}
            except Exception as exc:
                # Detailed exception stays local; console deliberately prints only class.
                error={'passed':False,'error_type':type(exc).__name__,'failure_category':'infrastructure'}
                (folder/'error.txt').write_text(redact(str(exc),secret_values))
                store.finish(cid,owner,number,False,error)
                return {'id':cid,**error}
        failed=False
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for result in pool.map(run,specs):
                print(json.dumps(result),flush=True)
                failed=failed or not result.get('passed',False)
        if failed:raise SystemExit(1)

if __name__=='__main__':
    main()
