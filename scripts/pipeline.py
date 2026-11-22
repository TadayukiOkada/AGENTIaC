#!/usr/bin/env python3
"""Persistent bounded Stage A controller: wait for prerequisites, deploy, gate, test."""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import yaml

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import declared, paths  # noqa: E402
root=paths.root()
inventory=yaml.safe_load((root/'infra/inventory.yml').read_text())
nodes=inventory['all']['children']['workers']['hosts']
p=argparse.ArgumentParser(description=__doc__);p.add_argument('node',choices=nodes);p.add_argument('--max-wait-hours',type=float,default=6)
a=p.parse_args();node=nodes[a.node]
if not node.get('default_profile') or not node.get('gateway_alias'):
    p.error('node is registered but has no validated default profile/gateway alias; complete provisioning first')
host,model,alias,port=node['ansible_host'],node['default_profile'],node['gateway_alias'],node['tunnel_port']
login=declared.account(node,root)+'@'+host  # the service account it declares (D8.2)
os.chdir(root);os.umask(0o077)
profile=json.loads((root/'profiles'/(model+'.json')).read_text());rev=profile['backend']['revision'];arch=node['cuda_arch']
state=root/'artifacts/pipelines'/(a.node+'.json');state.parent.mkdir(parents=True,exist_ok=True)
def record(stage,**extra):
    d={'node':a.node,'model':model,'stage':stage,'updated':datetime.datetime.now(datetime.timezone.utc).isoformat(),**extra}
    tmp=state.with_suffix('.tmp');tmp.write_text(json.dumps(d,indent=2)+'\n');tmp.replace(state);print(json.dumps(d),flush=True)
def run(argv):subprocess.run(argv,check=True)
try:
    deadline=time.monotonic()+a.max_wait_hours*3600;retries=0
    record('waiting-for-verified-model-and-backend')
    while True:
        script=f'''import json,pathlib,subprocess
r=pathlib.Path.home()/'agentiac'
b=(r/'backends/llama-{rev}/runtime-sm{arch}/build-metadata.txt').exists()
m=(r/'models/{model}/{profile['revision']}/verified.json').exists()
j=subprocess.run(['systemctl','is-active','{declared.resource('download-'+model,root)}'],capture_output=True,text=True)
print(json.dumps({{'build':b,'model':m,'downloading':j.stdout.strip()=='active'}}))'''
        probe=subprocess.run(['ssh','-x','-o','BatchMode=yes','-o','ConnectTimeout=10',login,'python3','-'],input=script,capture_output=True,text=True,timeout=30)
        if probe.returncode==0:
            d=json.loads(probe.stdout)
            if d['build'] and d['model']:break
            if not d['model'] and not d['downloading']:
                if retries>=3:raise RuntimeError('download failed three retries; inspect worker journal and partial file')
                run(['make','download','NODE='+a.node,'MODEL='+model]);retries+=1
        if time.monotonic()>deadline:raise TimeoutError('prerequisite wait expired; raw state preserved')
        time.sleep(30)
    record('deploying')
    run(['make','start','NODE='+a.node,'MODEL='+model])
    deadline=time.monotonic()+900
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/health',headers={'Authorization':'Bearer '+(root/'secrets/nodes'/(a.node+'.key')).read_text().strip()}),timeout=10) as response:
                if response.status==200:break
        except Exception:pass
        if time.monotonic()>deadline:raise TimeoutError('worker health did not become ready')
        time.sleep(10)
    deployment=root/'artifacts/deployments'/(a.node+'-'+model+'.json')
    run(['python3',str(CODE/'scripts/collect-deployment.py'),login,'--output',str(deployment),'--container',declared.resource('inference',root)])
    bench=root/'artifacts/benchmarks'/a.node/model
    base=['python3','-m','agentiac.cli','run','--model',alias,'--profile','profiles/'+model+'.json','--deployment',str(deployment),'--root',str(bench)]
    os.environ['PYTHONPATH']=str(CODE/'src')
    record('stage-a-tools',benchmark_root=str(bench))
    run(base+['--suite','tools','--trials','3'])
    record('stage-a-coding',benchmark_root=str(bench))
    run(base+['--suite','coding'])
    record('stage-a-complete',benchmark_root=str(bench))
except Exception as exc:
    record('failed',error_type=type(exc).__name__,detail=str(exc)[:1000])
    raise
