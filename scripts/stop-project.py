#!/usr/bin/env python3
"""ExecStop of the inference unit: stop the serving container it names (argv[1]), if it is ours."""
import json
import subprocess
import sys

name = sys.argv[1]   # <resource_prefix>-inference, passed by infra/templates/inference.service.j2 (D6.8)
p=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
if p.returncode:
    if 'no such object' not in p.stderr.lower():
        raise RuntimeError('cannot inspect project container')
else:
    c=json.loads(p.stdout)[0]
    if c['Config'].get('Labels',{}).get('io.agent-infra.owner')!='agent-infra':
        raise RuntimeError('refusing to stop foreign container')
    subprocess.run(['docker','stop','--time','60',c['Id']],check=True)
