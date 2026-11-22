#!/usr/bin/env python3
"""Collect exact model/backend/image identity without container environment/secrets."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import datetime

parser=argparse.ArgumentParser()
parser.add_argument('host')
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--container',required=True,help='the serving container: <resource_prefix>-inference (D6.8)')
args=parser.parse_args();os.umask(0o077)
script=r'''import json,pathlib,subprocess
r=pathlib.Path.home()/'agentiac'
p=json.loads((r/'active-profile.json').read_text());n=json.loads((r/'node.json').read_text())
b=r/'backends'/('llama-'+p['backend']['revision'])/('runtime-sm'+n['cuda_arch'])
c=json.loads(subprocess.check_output(['docker','inspect',__CONTAINER__]))[0]
mode=subprocess.run(['sh','-c','command -v nvpmodel >/dev/null && sudo -n nvpmodel -q'],capture_output=True,text=True)
driver=subprocess.run(['nvidia-smi','--query-gpu=driver_version','--format=csv,noheader'],capture_output=True,text=True)
print(json.dumps({'profile':p,'node':n,'power_mode':mode.stdout.strip() if mode.returncode==0 else None,'driver':driver.stdout.strip() if driver.returncode==0 else None,'backend_metadata':(b/'build-metadata.txt').read_text(),'image_id':c['Image'],'container_id':c['Id'],'container_args':c['Args'],'kernel':subprocess.check_output(['uname','-a'],text=True).strip()}))'''
script=script.replace('__CONTAINER__',repr(args.container))
r=subprocess.run(['ssh','-x','-o','BatchMode=yes',args.host,'python3','-'],input=script,capture_output=True,text=True,check=True,timeout=30)
d=json.loads(r.stdout);d['collected_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(d,indent=2)+'\n');print(args.output)
