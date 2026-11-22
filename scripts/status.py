#!/usr/bin/env python3
import concurrent.futures
import subprocess
import sys
from pathlib import Path
import yaml
CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import declared, paths  # noqa: E402
ROOT = paths.root()
P=declared.setting('resource_prefix',ROOT)  # every unit of ours is <prefix>-<role> (D6.8)
CMD=f"systemctl show {P}-build {P}-inference {P}-ondemand -p Id -p ActiveState -p SubState -p ExecMainStatus; systemctl --user show {P}-build -p Id -p ActiveState -p SubState -p ExecMainStatus; systemctl list-units --all '{P}-download*' --no-pager; find ~/agentiac/models -name '*.part' -printf '%f %s bytes\\n'; free -h | head -2"
def argv(host):
    # ansible_connection: local (the control host) runs here, not over SSH to localhost (no known_hosts entry).
    if host.get('ansible_connection')=='local':return ['bash','-c',CMD]
    return ['ssh','-x','-o','BatchMode=yes','-o','ConnectTimeout=10',declared.account(host,ROOT)+'@'+host['ansible_host'],CMD]
def f(pair):
    name,host=pair
    r=subprocess.run(argv(host),capture_output=True,text=True,timeout=30)
    return name,r.stdout,r.stderr
def main():
    inventory=yaml.safe_load((ROOT/'infra/inventory.yml').read_text())
    hosts=inventory['all']['children']['workers']['hosts']
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as p:
        for name,out,err in p.map(f,hosts.items()):print(name+'\n'+out+err,flush=True)
    tunnels=[P+'-tunnel-'+name for name,host in hosts.items() if host.get('ansible_connection')!='local']
    subprocess.run(['systemctl','show',P+'-gateway',*tunnels,'-p','Id','-p','ActiveState','-p','SubState'])
if __name__=='__main__':main()
