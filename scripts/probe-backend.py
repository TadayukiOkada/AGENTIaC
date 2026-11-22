#!/usr/bin/env python3
"""Worker-local isolated image pull/import/CUDA probe; no inference service changes."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--container', required=True, help='its container name: <resource_prefix>-backend-probe (D6.8)')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if not re.fullmatch(r'[a-z0-9-]+', manifest['id']):
        raise ValueError('invalid backend id')
    if not re.fullmatch(r'docker.io/vllm/vllm-openai@sha256:[a-f0-9]{64}', manifest['image']):
        raise ValueError('only reviewed digest-pinned official vLLM images permitted')
    node = json.loads((args.root / 'node.json').read_text())
    if node['node_class'] not in manifest['node_classes']:
        raise ValueError('backend manifest does not support node class')
    out = args.root / 'backends/probes' / manifest['id']; out.mkdir(parents=True, exist_ok=True)
    lock = (out / 'controller.lock').open('a'); fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    marker = out / 'verified.json'
    if marker.exists() and json.loads(marker.read_text()).get('manifest') == manifest:
        print('already verified ' + manifest['id'], flush=True); return
    deadline = time.monotonic() + 6 * 3600
    while manifest.get('wait_unit'):
        status = subprocess.run(['systemctl', 'show', manifest['wait_unit'], '-p', 'ActiveState', '--value'], capture_output=True, text=True, check=True).stdout.strip()
        if status not in ('active', 'activating'):
            break
        if time.monotonic() > deadline:
            raise TimeoutError('waiting for priority model download timed out')
        time.sleep(30)
    if shutil.disk_usage(args.root).free < 50 * 1024**3:
        raise RuntimeError('retain at least 50 GiB disk headroom; no cleanup performed')
    subprocess.run(['docker', 'pull', manifest['image']], check=True, timeout=3600)
    inspection = json.loads(subprocess.check_output(['docker', 'image', 'inspect', manifest['image']]))[0]
    if inspection['Architecture'] != manifest['architecture']:
        raise ValueError('unexpected image architecture')
    labels = inspection.get('Config', {}).get('Labels') or {}
    if labels.get('org.opencontainers.image.revision') != manifest['source_revision']:
        raise ValueError('unexpected backend source revision')
    program = """import json,torch,vllm
assert torch.cuda.is_available()
x=torch.arange(1024,device='cuda',dtype=torch.float32)
y=(x+1).sum();torch.cuda.synchronize()
assert y.item()==524800
print(json.dumps({'torch':torch.__version__,'vllm':vllm.__version__,'cuda':torch.version.cuda,'gpu':torch.cuda.get_device_name(0),'capability':torch.cuda.get_device_capability(0),'kernel_result':y.item()}))
"""
    command = ['docker', 'run', '--rm', '--name', args.container,
        '--label', 'io.agent-infra.owner=agent-infra', '--gpus', 'all', '--network=none',
        '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
        '--memory=4g', '--memory-swap=4g', '--cpus=2', '--pids-limit=256', '--shm-size=256m',
        '--tmpfs', '/tmp:rw,exec,nosuid,size=512m', '-e', 'HOME=/tmp', '-e', 'VLLM_CACHE_ROOT=/tmp/vllm',
        '-e', 'HF_HUB_OFFLINE=1', '-e', 'OMP_NUM_THREADS=1', '--entrypoint', 'python3', inspection['Id'], '-c', program]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    (out / 'probe.stdout').write_text(result.stdout); (out / 'probe.stderr').write_text(result.stderr)
    if result.returncode:
        raise RuntimeError('backend probe failed; inspect project-local probe.stderr')
    measurement = json.loads(result.stdout.strip().splitlines()[-1])
    record = {'manifest': manifest, 'image_id': inspection['Id'], 'measurement': measurement,
              'verified_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    temp = marker.with_suffix('.tmp'); temp.write_text(json.dumps(record, indent=2) + '\n'); temp.replace(marker)
    print(json.dumps({'backend': manifest['id'], 'measurement': measurement}), flush=True)


if __name__ == '__main__':
    main()
