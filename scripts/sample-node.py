#!/usr/bin/env python3
"""Metadata-only worker RAM/load/backend metrics sampler; output stays on control."""
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
root=paths.root();inventory=yaml.safe_load((root/'infra/inventory.yml').read_text())
nodes=inventory['all']['children']['workers']['hosts'];p=argparse.ArgumentParser(description=__doc__)
p.add_argument('node',choices=nodes);p.add_argument('--seconds',type=int,default=600);p.add_argument('--interval',type=int,default=10)
a=p.parse_args();node=nodes[a.node];os.umask(0o077)
folder=root/'artifacts/telemetry';folder.mkdir(parents=True,exist_ok=True)
path=folder/(a.node+'-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')+'.jsonl')
key=(root/'secrets/nodes'/(a.node+'.key')).read_text().strip()
end=time.monotonic()+a.seconds
probe="import json,pathlib,os; m={s.split(':')[0]:int(s.split()[1]) for s in pathlib.Path('/proc/meminfo').read_text().splitlines() if s.startswith(('MemTotal:','MemAvailable:','SwapTotal:','SwapFree:'))}; print(json.dumps({'mem_kib':m,'load':os.getloadavg()}))"
with path.open('x') as log:
    while time.monotonic()<end:
        r={'time':datetime.datetime.now(datetime.timezone.utc).isoformat(),'node':a.node}
        try:
            c=subprocess.run(['ssh','-x','-o','BatchMode=yes','-o','ConnectTimeout=5',declared.account(node,root)+'@'+node['ansible_host'],'python3','-c',__import__('shlex').quote(probe)],capture_output=True,text=True,timeout=15)
            if c.returncode==0:r.update(json.loads(c.stdout))
            else:r['host_probe_error']=c.returncode
            req=urllib.request.Request(f"http://127.0.0.1:{node['tunnel_port']}/metrics",headers={'Authorization':'Bearer '+key})
            with urllib.request.urlopen(req,timeout=5) as response:
                metrics={}
                for line in response.read().decode().splitlines():
                    if line and not line.startswith('#'):
                        name,value=line.rsplit(None,1)
                        try:metrics[name]=float(value)
                        except ValueError:pass
                r['backend_metrics']=metrics
        except Exception as exc:r['probe_error']=type(exc).__name__
        log.write(json.dumps(r)+'\n');log.flush()
        time.sleep(max(1,a.interval))
print(path)
