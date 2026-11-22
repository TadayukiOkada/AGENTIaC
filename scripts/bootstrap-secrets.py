#!/usr/bin/env python3
"""Generate/reuse local gateway and worker keys without printing credentials."""
import os
from pathlib import Path
import secrets
import sys
import yaml
CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
hosts=yaml.safe_load((ROOT/'infra/inventory.yml').read_text())['all']['children']['workers']['hosts']
root=ROOT/'secrets';os.umask(0o077);root.mkdir(exist_ok=True)
workers=root/'nodes';workers.mkdir(exist_ok=True)
env=root/'gateway.env';values={}
if env.exists():
    for line in env.read_text().splitlines():
        if '=' in line:
            k,v=line.split('=',1);values[k]=v
if '--rotate' in sys.argv:values['LITELLM_MASTER_KEY']='sk-'+secrets.token_hex(32);values['LITELLM_TASK_KEY']='sk-'+secrets.token_hex(32);values['LITELLM_CODE_KEY']='sk-'+secrets.token_hex(32);values['LITELLM_CODE_CLOUD_KEY']='sk-'+secrets.token_hex(32)
values.setdefault('LITELLM_MASTER_KEY','sk-'+secrets.token_hex(32))
# IP-01 (T4.3): the key a suite/smoke task container carries; the gateway pins it to the
# rendered local-coder aliases (gateway/task_policy.py). It is not the master key.
values.setdefault('LITELLM_TASK_KEY','sk-'+secrets.token_hex(32))
# The daily agent's keys (D1.2), carried by scripts/agent.py instead of the master key; gateway/task_policy.py
# limits both to the inference routes, and only the cloud one to the subscription alias.
values.setdefault('LITELLM_CODE_KEY','sk-'+secrets.token_hex(32))
values.setdefault('LITELLM_CODE_CLOUD_KEY','sk-'+secrets.token_hex(32))
for node in [n for n,h in hosts.items() if h.get('backend')!='strata']:
    dest=workers/(node+'.key')
    if not dest.exists() or '--rotate' in sys.argv:dest.write_text(secrets.token_hex(32)+'\n')
    dest.chmod(0o600);values[node.upper()+'_API_KEY']=dest.read_text().strip()
for node in [n for n,h in hosts.items() if h.get('backend')=='strata']:  # Strata API keys, pushed to the worker by agentiac claim
    dest=workers/('strata-'+node+'.key')
    if not dest.exists() or '--rotate' in sys.argv:dest.write_text(secrets.token_urlsafe(48)+'\n')
    dest.chmod(0o600);values['STRATA_'+node.upper()+'_API_KEY']=dest.read_text().strip()
tmp=env.with_suffix('.tmp');tmp.write_text(''.join(k+'='+v+'\n' for k,v in values.items()));tmp.chmod(0o600);tmp.replace(env)
print('Gateway and worker credentials ready (values suppressed).')
