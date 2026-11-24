#!/usr/bin/env python3
"""Download pinned public weights directly on worker; resumable and SHA256 verified."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import urllib.request

SPDX_STANDARDS = {
    'apache-2.0': ('Apache-2.0', '074e6e32c86a4c0ef8b3ed25b721ca23aca83df277cd88106ef7177c354615ff'),
    'mit': ('MIT', 'b05785f9f18e6716bab63424b11454513b9943a222595b70411009202fc592b5'),
}
REVIEW_KEYS = {'status', 'decided_by', 'decided_at', 'note'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def validate_license_review(review):
    """None when the review names who decided what and when; otherwise the reason it does not.

    `profiles/schema.json` states the same shape, but the worker only receives the profile file and never validates it
    against the schema, so the downloader holds the line itself.
    """
    if not isinstance(review, dict):
        return 'it is not an object'
    missing, extra = REVIEW_KEYS - review.keys(), review.keys() - REVIEW_KEYS
    if missing or extra:
        return f'missing {sorted(missing)}, unexpected {sorted(extra)}'
    if review['status'] != 'manual-approved':
        return f"status is {review['status']!r}, not 'manual-approved'"
    for key in ('decided_by', 'note'):
        if not isinstance(review[key], str) or not review[key].strip():
            return f'{key} is empty'
    when = review['decided_at']
    if not isinstance(when, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', when):
        return 'decided_at is not YYYY-MM-DD'
    try:
        datetime.date.fromisoformat(when)
    except ValueError:
        return 'decided_at is not a calendar date'
    return None


def license_provenance(p, target, open_url=None):
    """Record what the publisher declared, and take the bytes only when a named review accepted what was found.

    Three cases, each writing down what it actually found rather than smoothing the difference between them:

    1. the upstream repository carries a root `LICENSE`/`NOTICE` file: it is fetched and hashed;
    2. it declares an SPDX license on its card and omits the file: the standard reference text is fetched under its
       pinned hash, which is what `apache-2.0`/`mit` meant here before;
    3. it declares something else — a named agreement (`license: other`), or nothing at all — or its card disagrees with
       what the profile declares: the download only runs when the **profile** carries a `license_review` naming who
       accepted which declaration, and the record keeps the publisher's own words (`license`, `license_name`,
       `license_link`) beside that decision.

    Case 3 is the point: a download is not a license decision. The refusal is what makes the review exist somewhere.
    """
    open_url = open_url or urllib.request.urlopen
    upstream = p['upstream']
    profile_license = upstream.get('license')
    metadata = target / 'provenance'
    metadata.mkdir(exist_ok=True)
    api = f"https://huggingface.co/api/models/{upstream['repo']}/revision/{upstream['revision']}"
    with open_url(api, timeout=30) as stream:
        upstream_data = json.load(stream)
    card = upstream_data.get('cardData') or {}
    # Only the repository root speaks for the repository: `third_party/LICENSE` is somebody else's text.
    license_files = [x['rfilename'] for x in upstream_data.get('siblings', [])
                     if len(PurePosixPath(x['rfilename']).parts) == 1
                     and PurePosixPath(x['rfilename']).name.upper() in ('LICENSE', 'LICENSE.TXT', 'LICENSE.MD', 'NOTICE')]
    hashes = {}
    if not license_files:
        declared = card.get('license')
        if isinstance(declared, str) and declared == profile_license and declared in SPDX_STANDARDS:
            # Some official model repos declare SPDX license in their card but omit LICENSE.
            # Preserve the publisher declaration plus a clearly identified standard reference.
            name, expected = SPDX_STANDARDS[declared]
            spdx_revision = '31ba1a50e5397e00a304dbadc76531740e89ee48'
            license_url = f'https://raw.githubusercontent.com/spdx/license-list-data/{spdx_revision}/text/{name}.txt'
            with open_url(license_url, timeout=30) as stream:
                reference = stream.read()
            if hashlib.sha256(reference).hexdigest() != expected:
                raise RuntimeError('SPDX reference checksum mismatch')
            (metadata / 'LICENSE-standard-reference.txt').write_bytes(reference)
            declaration = {'publisher_repo': upstream['repo'], 'revision': upstream['revision'],
                           'declared_license': declared, 'publisher_license_file': None,
                           'standard_reference_url': license_url, 'standard_reference_sha256': expected}
            (metadata / 'publisher-license-declaration.json').write_text(json.dumps(declaration, indent=2) + '\n')
            hashes['SPDX-standard-reference'] = expected
        else:
            review = p.get('license_review')
            if review is None:
                raise RuntimeError(f"license requires manual review; upstream declares {declared!r}, "
                                   f"the profile declares {profile_license!r}, and no `license_review` is recorded "
                                   'in the profile (add one naming who accepted which declaration)')
            problem = validate_license_review(review)
            if problem:
                raise RuntimeError(f'license requires manual review; the profile `license_review` is not a review: {problem}')
            declaration = {'publisher_repo': upstream['repo'], 'revision': upstream['revision'],
                           'declared_license': declared, 'declared_license_name': card.get('license_name'),
                           'declared_license_link': card.get('license_link'),
                           'publisher_license_file': None, 'profile_declares': profile_license,
                           'accepted_by_review': review,
                           'record': 'no license text published by the upstream repository; the profile review accepted '
                                     'what the publisher declared'}
            text = json.dumps(declaration, indent=2, sort_keys=True) + '\n'
            (metadata / 'publisher-license-declaration.json').write_text(text)
            hashes['publisher-license-declaration'] = hashlib.sha256(text.encode()).hexdigest()

    for name in license_files:
        with open_url(f"https://huggingface.co/{upstream['repo']}/resolve/{upstream['revision']}/{name}", timeout=30) as stream:
            content = stream.read()
        (metadata / PurePosixPath(name).name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    return hashes


def download(p, root, open_url=None, run=subprocess.run):
    """Fetch and verify every file of the profile; write `verified.json` last.

    The license is settled **first**: a refusal that came after ~100 GiB had been moved onto a borrowed host would
    still have spent the disk and the bandwidth, so nothing is fetched until the provenance step has accepted it.
    """
    target = Path(root) / p['id'] / p['revision']
    target.mkdir(parents=True, exist_ok=True)
    with (target / '.download.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        hashes = license_provenance(p, target, open_url=open_url)
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
            run(['curl', '--fail', '--location', '--retry', '8', '--retry-all-errors',
                 '--connect-timeout', '30', '--speed-time', '120', '--speed-limit', '1024',
                 '--continue-at', '-', '--output', str(partial), url], check=True)
            if partial.stat().st_size != entry['bytes'] or digest(partial) != entry['sha256']:
                raise RuntimeError('download failed size/SHA256 verification: ' + str(partial))
            os.replace(partial, dest)
            print('verified downloaded', entry['path'], flush=True)
        metadata = target / 'provenance'
        (metadata / 'profile.json').write_text(json.dumps(p, indent=2) + '\n')
        (metadata / 'license-hashes.json').write_text(json.dumps(hashes, indent=2) + '\n')
        # Marker is written only after all weights and license texts were verified.
        marker = target / 'verified.json'
        temp = target / 'verified.json.tmp'
        temp.write_text(json.dumps({'profile': p, 'license_hashes': hashes}, indent=2) + '\n')
        os.replace(temp, marker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    download(json.loads(args.profile.read_text()), args.root)


if __name__ == '__main__':
    main()
