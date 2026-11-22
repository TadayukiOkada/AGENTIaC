#!/usr/bin/env python3
"""Whether the evaluation image still matches `benchmarks/Dockerfile` (IP-13).

`agentiac-eval:baseline` is built from `benchmarks/Dockerfile`; without a label, no record says which
Dockerfile revision the local image came from. The build labels the image with the
sha256 of the Dockerfile it was built from, and this reads that label back: `check()` answers `ok`, `note`
or `drift`, and `agentiac ops drift` prints it as one line. `agentiac ops eval-image` reports, and
`agentiac ops eval-image build` rebuilds it; nothing is deleted, and the image a rebuild replaces is
left untagged rather than removed.

The hash covers the Dockerfile alone, and that is the whole of it: the Dockerfile copies no file in, so
the rest of `benchmarks/` (graders, suite task JSON) never reaches the image. The Dockerfile is code; the task
suite (`benchmarks/suite/`, its build inputs and lock) belongs to the site that runs it, and a site without one
gets a note instead of a suite check. A label is a fact about a
build, not a promise: an image built before this existed carries no label and is reported as unknown
rather than assumed fresh.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import declared, paths  # noqa: E402
from agentiac.errors import command as cli_name  # noqa: E402
ROOT = paths.root()
DOCKERFILE = CODE / 'benchmarks/Dockerfile'
LABEL = 'agent-infra.dockerfile-sha256'
FALLBACK_TAG = declared.resource('eval:baseline', ROOT)
REBUILD = f'{cli_name()} ops eval-image build'

# The task suite's image is not labelled, and labelling it means a rebuild, which moves the image id every
# case id is hashed over (X18). So its record is a tracked lock: which image, and which build inputs.
SUITE_TAG = declared.resource('suite:v1', ROOT)     # agentiac.suite.IMAGE; a test compares the two
SUITE_DIR = paths.locate('benchmarks/suite', ROOT)   # the site's task suite and its build inputs
SUITE_INPUTS = ('Dockerfile', 'requirements.txt')   # everything its Dockerfile COPYs or is
SUITE_LOCK = ROOT / 'benchmarks/suite/image.lock.json'   # this installation's build of the image: site
SUITE_PIN = f'{cli_name()} ops eval-image pin-suite'


def image_tag():
    """The benchmark image (src/agentiac/tasks.py). The task suite's `agentiac-suite:v1` is not checked here."""
    return os.environ.get('AGENTIAC_EVAL_IMAGE') or FALLBACK_TAG


def dockerfile_hash():
    """sha256 of the Dockerfile bytes as committed: the same digest the build labels with."""
    return hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()


def inspect(tag):
    """The image's config, or None when no image carries that tag. Raises when docker itself cannot run."""
    done = subprocess.run(['docker', 'image', 'inspect', '--format', '{{json .}}', tag],
                          capture_output=True, text=True)
    if done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        return None


def utc(text):
    """Docker's and git's timestamps in UTC. Python 3.12 reads Docker's nanoseconds."""
    try:
        return datetime.datetime.fromisoformat(text).astimezone(datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def stamp(when):
    return when.strftime('%Y-%m-%d %H:%M UTC') if when else 'unknown'


def dockerfile_commit():
    """When the Dockerfile was last committed, and by which commit — the record the image lacks."""
    done = subprocess.run(['git', '-C', str(CODE), 'log', '-1', '--date=iso-strict', '--format=%ad%n%h',
                           '--', str(DOCKERFILE.relative_to(CODE))], capture_output=True, text=True)
    if done.returncode != 0 or not done.stdout.strip():
        return None, None
    committed, _, commit = done.stdout.partition('\n')
    return utc(committed), commit.strip()


def labels(image):
    return (image.get('Config') or {}).get('Labels') or {}


def check(tag=None):
    """(state, detail) for one tag: does the image on this machine carry the current Dockerfile's hash?"""
    wanted = tag or image_tag()
    try:
        image = inspect(wanted)
    except OSError:  # no docker on this machine: nothing to compare, which is not drift
        return 'note', 'docker is not available here; image not checked'
    if image is None:
        return 'drift', f'{wanted} is not present; {REBUILD}'
    recorded = labels(image).get(LABEL)
    digest = dockerfile_hash()
    if recorded == digest:
        return 'ok', f'{wanted} carries the current benchmarks/Dockerfile ({digest[:12]})'
    if recorded is None:
        created, committed, commit = utc(image.get('Created')), *dockerfile_commit()
        return 'note', (f'{wanted} has no Dockerfile-hash label; built {stamp(created)}, '
                        f'benchmarks/Dockerfile last committed {stamp(committed)} in {commit}'
                        + ('; the image predates that commit' if created and committed and created <= committed
                           else '') + f'; label it with {REBUILD}')
    return 'drift', (f'{wanted} was built from a different benchmarks/Dockerfile '
                     f'(label {recorded[:12]}, now {digest[:12]}); {REBUILD}')


def suite_inputs_hash():
    """sha256 over the name and bytes of each build input, so moving text between them is a change."""
    digest = hashlib.sha256()
    for name in SUITE_INPUTS:
        digest.update(name.encode() + b'\0' + (SUITE_DIR / name).read_bytes() + b'\0')
    return digest.hexdigest()


def check_suite():
    """(state, detail): is the suite's image the locked one, and are its inputs the locked ones?

    This detects a change after the pin. It cannot prove the image was built from these files, which is
    what a build label would prove and a lock does not.
    """
    if not SUITE_DIR.is_dir():
        return 'note', f'this site has no task suite ({SUITE_DIR} does not exist); its image is not checked'
    try:
        image = inspect(SUITE_TAG)
    except OSError:
        return 'note', 'docker is not available here; image not checked'
    if not SUITE_LOCK.exists():
        return 'note', f'{SUITE_LOCK.relative_to(ROOT)} does not exist; {SUITE_PIN}'
    lock = json.loads(SUITE_LOCK.read_text())
    if image is None:
        return 'drift', f'{SUITE_TAG} is not present; the lock names {lock["image_id"][:19]}'
    if image.get('Id') != lock['image_id']:
        return 'drift', (f'{SUITE_TAG} is {image.get("Id", "")[:19]}, the lock names {lock["image_id"][:19]}: '
                         f'new case ids from here on; review it, then {SUITE_PIN}')
    if suite_inputs_hash() != lock['inputs_sha256']:
        return 'drift', (f'{" or ".join(SUITE_INPUTS)} changed since {SUITE_TAG} was locked: the image does not '
                         f'have the edit; rebuild it and {SUITE_PIN}')
    return 'ok', f'{SUITE_TAG} is the locked image, inputs unchanged ({lock["inputs_sha256"][:12]})'


def pin_suite():
    """Record the image here and the inputs here. An operator's statement that they belong together."""
    image = inspect(SUITE_TAG)
    if image is None:
        print(f'{SUITE_TAG} is not present; nothing to lock')
        return 1
    lock = {'tag': SUITE_TAG, 'image_id': image['Id'], 'inputs': list(SUITE_INPUTS),
            'inputs_sha256': suite_inputs_hash(),
            'pinned_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}
    SUITE_LOCK.write_text(json.dumps(lock, indent=1) + '\n')
    print(f'{SUITE_LOCK.relative_to(ROOT)}: {image["Id"]}')
    return 0


def build(tag=None):
    """Rebuild the tag, labelled with the Dockerfile's hash. Leaves the image it replaces untagged, not removed."""
    wanted = tag or image_tag()
    digest = dockerfile_hash()
    previous = inspect(wanted)
    done = subprocess.run(['docker', 'build', '--pull=false', '-t', wanted, '--label', f'{LABEL}={digest}',
                           str(DOCKERFILE.parent)], cwd=ROOT)
    if done.returncode != 0:
        return done.returncode
    image = inspect(wanted)
    if image is None or labels(image).get(LABEL) != digest:
        print(f'{wanted} does not carry {LABEL}={digest} after the build; the label did not reach the image')
        return 1
    print(f'{LABEL}={digest}')
    print(f'image {image.get("Id")}')
    if previous and previous.get('Id') != image.get('Id'):
        print(f'the image it replaces, {previous["Id"]}, is untagged and left in place; '
              'records that name that id still describe it')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', nargs='?', choices=('status', 'hash', 'build', 'pin-suite'), default='status',
                        help='status: report (default); hash: print the Dockerfile hash; build: rebuild, labelled')
    parser.add_argument('--tag', help=f'image to check (default: $AGENTIAC_EVAL_IMAGE or {FALLBACK_TAG})')
    args = parser.parse_args(argv)
    if args.action == 'hash':
        print(dockerfile_hash())
        return 0
    if args.action == 'build':
        return build(args.tag)
    if args.action == 'pin-suite':
        return pin_suite()
    state, detail = check(args.tag)
    print(f'{state:6}{detail}')
    if args.tag:                       # one named image was asked about, and that is the answer
        return 1 if state == 'drift' else 0
    suite_state, suite_detail = check_suite()
    print(f'{suite_state:6}{suite_detail}')
    return 1 if 'drift' in (state, suite_state) else 0


if __name__ == '__main__':
    sys.exit(main())
