#!/usr/bin/env python3
"""Launch only verified project model in resource-limited, read-only Docker runtime."""
import argparse
import grp
import json
import os
from pathlib import Path
import subprocess
import sys


def gpu_used_mib():
    output = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'], text=True)
    return sum(int(line) for line in output.split())


def command(root, p, node, image, cors_origin=None, container=None):
    b = p['backend']
    arch = node['cuda_arch']
    backend = root / 'backends' / ('llama-' + b['revision']) / ('runtime-sm' + arch)
    weights = root / 'models' / p['id'] / p['revision']
    verified = json.loads((weights / 'verified.json').read_text())
    if verified['profile'] != p:
        raise ValueError('profile differs from verified download manifest')
    if node['node_class'] not in p['nodes']:
        raise ValueError('profile not supported on this node class')
    if not (backend / 'build-metadata.txt').is_file():
        raise ValueError('backend build incomplete')
    mem={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
    available=mem['MemAvailable']//1024
    configured=int(node['memory_limit'].removesuffix('G'))*1024**3
    budget=min(configured,int(mem['MemAvailable']-0.20*mem['MemTotal']))
    if budget<=0:raise RuntimeError('no inference budget after host headroom reserve')
    if available < node['min_available_gib'] * 1024**2:
        raise RuntimeError('insufficient available host memory; existing workloads preserved')
    if os.getloadavg()[0] > node['max_load']:
        raise RuntimeError('host busy; defer model start')
    if node.get('max_gpu_used_mib') is not None:  # discrete GPU shared with its owner
        used = gpu_used_mib()
        if used > node['max_gpu_used_mib']:
            raise RuntimeError(f'GPU in use by another workload ({used} MiB); existing workloads preserved')
    # Resolve a pinned local image ID before launch; never pull a moving tag at startup.
    inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
    argv = ['docker', 'run', '--rm', *(['--name', container] if container else []),
            '--label', 'io.agent-infra.owner=agent-infra', '--label', 'io.agent-infra.profile=' + p['id'],
            '-e', 'NVIDIA_VISIBLE_DEVICES=all', '-e', 'NVIDIA_DRIVER_CAPABILITIES=compute,utility',
            '--user', f'{root.stat().st_uid}:{root.stat().st_gid}', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=512m',
            '--memory', str(budget), '--memory-swap', str(budget),
            '--cpus', str(node['cpus']), '--pids-limit', '512', '--shm-size', '256m',
            '-p', f"127.0.0.1:{node['port']}:8080",
            '-v', f'{root / "api.key"}:/run/secrets/api.key:ro',
            '-v', f'{backend}:/backend:ro', '-v', f'{weights}:/models:ro',
            '-v', f"{node['cuda_lib']}:/cuda-runtime:ro",
            '-e', 'LD_LIBRARY_PATH=/backend:/cuda-runtime']
    argv += ['--gpus', 'all'] if node['node_class']=='spark' else ['--runtime=nvidia']
    for group in ('video', 'render'):
        try:
            argv += ['--group-add', str(grp.getgrnam(group).gr_gid)]
        except KeyError:
            pass
    if p.get('local_model_path'):
        argv += ['-v', p['local_model_path']+':/existing-model.gguf:ro']
    model_path='/existing-model.gguf' if p.get('local_model_path') else '/models/'+p['entrypoint']
    argv += [inspected['Id'], '/backend/llama-server', '-m', model_path,
             '--alias', p['id'], '--api-key-file', '/run/secrets/api.key',
             *(['--cors-origins', cors_origin] if cors_origin else []),
             '--host', '0.0.0.0', '--port', '8080', '-ngl', '999',
             '-c', str(b['context'] * b['slots']), '--parallel', str(b['slots']),
             '--kv-unified', '--kv-unified-per-slot', str(b['context']),
             '-b', str(b['batch']), '-ub', str(b['ubatch']), '-fa', 'on',
             '--cache-ram', str(b['cache_ram_mib']), '--ctx-checkpoints', '4',
             '--jinja', '--metrics', '--no-ui', '--log-disable'] + b['extra_args']
    if os.environ.get('AGENTIAC_STARTUP_DEBUG')=='1':
        argv.remove('--log-disable')
        argv += ['--log-verbosity', '3']
    return argv, inspected['Id']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--check', action='store_true', help='run the admission checks only (on-demand front)')
    parser.add_argument('--container', help="the serving container's name, which the unit passes: "
                                             '<resource_prefix>-inference (D6.8)')
    parser.add_argument('--cors-origin', help="the gateway's origin, which llama-server allows; the playbook passes "
                                              'http://127.0.0.1:<gateway_port> (D8.2)')
    args = parser.parse_args()
    if not args.check and not (args.cors_origin and args.container):
        parser.error('--cors-origin and --container are required to serve (infra/templates/inference.service.j2 passes them)')
    root = args.root
    p = json.loads((root / 'active-profile.json').read_text())
    node = json.loads((root / 'node.json').read_text())
    try:
        argv, image = command(root, p, node, node['image'], cors_origin=args.cors_origin, container=args.container)
    except (RuntimeError, ValueError) as error:
        if not args.check:
            raise
        raise SystemExit(str(error))
    if args.check:
        print(json.dumps({'admitted': p['id']}))
        return
    # Metadata only; request text is never printed by this launcher.
    print(json.dumps({'profile': p['id'], 'revision': p['revision'], 'backend': p['backend'], 'image': image}), flush=True)
    os.execvp(argv[0], argv)

if __name__ == '__main__':
    main()
