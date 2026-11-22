#!/usr/bin/env python3
"""Download pinned public weights directly on worker; resumable and SHA256 verified."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import urllib.request


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    p = json.loads(args.profile.read_text())
    target = args.root / p['id'] / p['revision']
    target.mkdir(parents=True, exist_ok=True)
    with (target / '.download.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for entry in p['files']:
            rel = PurePosixPath(entry['path'])
            if rel.is_absolute() or '..' in rel.parts:
                raise ValueError('unsafe model file path')
            dest = target / entry['path']
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if dest.stat().st_size == entry['bytes'] and digest(dest) == entry['sha256']:
                    print('verified existing', entry['path'], flush=True)
                    continue
                raise RuntimeError('existing final file failed verification; preserve for inspection: ' + str(dest))
            partial = dest.with_name(dest.name + '.part')
            remaining = entry['bytes'] - (partial.stat().st_size if partial.exists() else 0)
            if shutil.disk_usage(target).free < remaining + 20 * 1024**3:
                raise RuntimeError('insufficient disk headroom; no automatic deletion')
            url = f"https://huggingface.co/{p['repo']}/resolve/{p['revision']}/{entry['path']}?download=true"
            subprocess.run(['curl', '--fail', '--location', '--retry', '8', '--retry-all-errors',
                            '--connect-timeout', '30', '--speed-time', '120', '--speed-limit', '1024',
                            '--continue-at', '-', '--output', str(partial), url], check=True)
            if partial.stat().st_size != entry['bytes'] or digest(partial) != entry['sha256']:
                raise RuntimeError('download failed size/SHA256 verification: ' + str(partial))
            os.replace(partial, dest)
            print('verified downloaded', entry['path'], flush=True)
        upstream = p['upstream']
        metadata = target / 'provenance'
        metadata.mkdir(exist_ok=True)
        api = f"https://huggingface.co/api/models/{upstream['repo']}/revision/{upstream['revision']}"
        with urllib.request.urlopen(api, timeout=30) as stream:
            upstream_data = json.load(stream)
        license_files = [x['rfilename'] for x in upstream_data.get('siblings', [])
                         if PurePosixPath(x['rfilename']).name.upper() in ('LICENSE', 'LICENSE.TXT', 'LICENSE.MD', 'NOTICE')]
        hashes = {}
        if not license_files:
            # Some official model repos declare SPDX license in their card but omit LICENSE.
            # Preserve the publisher declaration plus a clearly identified standard reference.
            declared = upstream_data.get('cardData', {}).get('license')
            standards = {
                'apache-2.0': ('Apache-2.0', '074e6e32c86a4c0ef8b3ed25b721ca23aca83df277cd88106ef7177c354615ff'),
                'mit': ('MIT', 'b05785f9f18e6716bab63424b11454513b9943a222595b70411009202fc592b5'),
            }
            if declared != upstream['license'] or declared not in standards:
                raise RuntimeError('license requires manual review; no known standard declaration')
            name, expected = standards[declared]
            spdx_revision = '31ba1a50e5397e00a304dbadc76531740e89ee48'
            license_url = f'https://raw.githubusercontent.com/spdx/license-list-data/{spdx_revision}/text/{name}.txt'
            with urllib.request.urlopen(license_url, timeout=30) as stream:
                reference = stream.read()
            if hashlib.sha256(reference).hexdigest() != expected:
                raise RuntimeError('SPDX reference checksum mismatch')
            (metadata / 'LICENSE-standard-reference.txt').write_bytes(reference)
            declaration = {'publisher_repo': upstream['repo'], 'revision': upstream['revision'],
                           'declared_license': declared, 'publisher_license_file': None,
                           'standard_reference_url': license_url, 'standard_reference_sha256': expected}
            (metadata / 'publisher-license-declaration.json').write_text(json.dumps(declaration, indent=2)+'\n')
            hashes['SPDX-standard-reference'] = expected

        for name in license_files:
            with urllib.request.urlopen(f"https://huggingface.co/{upstream['repo']}/resolve/{upstream['revision']}/{name}", timeout=30) as stream:
                content = stream.read()
            (metadata / PurePosixPath(name).name).write_bytes(content)
            hashes[name] = hashlib.sha256(content).hexdigest()
        (metadata / 'profile.json').write_text(json.dumps(p, indent=2) + '\n')
        (metadata / 'license-hashes.json').write_text(json.dumps(hashes, indent=2) + '\n')
        # Marker is written only after all weights and license texts were verified.
        marker = target / 'verified.json'
        temp = target / 'verified.json.tmp'
        temp.write_text(json.dumps({'profile': p, 'license_hashes': hashes}, indent=2) + '\n')
        os.replace(temp, marker)

if __name__ == '__main__':
    main()
