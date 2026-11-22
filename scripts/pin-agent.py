#!/usr/bin/env python3
"""Pin an Agent's bytes so an automated job runs a known install and nothing else (U4).

    scripts/pin-agent.py claude-code --source ~/.local/share/claude/versions/2.1.316
    scripts/pin-agent.py codex --source ~/.codex/packages/standalone/releases/0.165.0-.../
    scripts/pin-agent.py claude-code --check

A source may be a single binary or a release directory (Codex ships a tree: a binary, a code-mode
host, ripgrep and bwrap). A directory is copied whole and pinned by one digest over the name, size
and bytes of every file in it: replacing a sibling is the same attack as replacing the entrypoint.

The pin is tracked; the bytes under artifacts/ are not. `--check` is what CI and the suite rely on,
and an Agent whose bytes do not match its pin cannot run.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
from agentiac import adapters  # noqa: E402


def sha256_file(path):
    return adapters.sha256_file(path)


def check(chosen, install):
    """The same comparison the suite makes before it runs an Agent; a check may not be more lenient."""
    try:
        pin = chosen.pin()
        chosen.verify_pin()
    except adapters.AdapterError as error:
        raise SystemExit(str(error))
    field = 'tree_sha256' if 'tree_sha256' in pin else 'sha256'
    path = install.relative_to(ROOT) if str(install).startswith(str(ROOT) + os.sep) else install
    print(json.dumps({'agent': chosen.id, 'path': str(path), field: pin[field],
                      'version': pin.get('version'), 'check': 'ok'}))


def pin_directory(chosen, source, tree):
    # Copy aside and swap: a half-written tree would either fail the pin (annoying) or, if the digest
    # were recomputed from what survived, pass it while lying (worse).
    staging = tree.with_name(tree.name + '.new')
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(source, staging, symlinks=True)
    relative = Path(chosen.install).relative_to(chosen.install_dir) if chosen.install_dir else Path(chosen.install).name
    entrypoint = staging / relative
    if not entrypoint.is_file():
        shutil.rmtree(staging)
        raise SystemExit(f'{source} has no {relative}; refusing to pin an Agent that cannot start')
    os.chmod(entrypoint, 0o755)
    reported = subprocess.check_output([str(entrypoint), '--version'], text=True).strip()
    old = tree.with_name(tree.name + '.old')
    if old.exists():
        shutil.rmtree(old)
    if tree.exists():
        tree.rename(old)
    staging.rename(tree)
    shutil.rmtree(old, ignore_errors=True)
    return {'agent': chosen.id, 'version': reported.split()[-1], 'path': str(tree.relative_to(ROOT)),
            'entrypoint': str(relative), 'tree_sha256': adapters.tree_digest(tree),
            'files': sum(1 for path in tree.rglob('*') if path.is_file() and not path.is_symlink()),
            'bytes': sum(path.stat().st_size for path in tree.rglob('*') if path.is_file() and not path.is_symlink()),
            'source': f'copied once from {source}; the automated path runs this copy and verifies these bytes, '
                      'never the self-updating original (U4)',
            'how_to_replace': 'install the release beside it, re-run scripts/pin-agent.py, and commit the new '
                              'tree_sha256; an Agent whose bytes do not match its pin cannot run',
            'license_review': 'not reviewed; docs/license-review.json covers model licenses only'}


def pin_file(chosen, source, install):
    install.parent.mkdir(parents=True, exist_ok=True)
    temporary = install.with_suffix('.tmp')
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o755)
    os.replace(temporary, install)
    reported = subprocess.check_output([str(install), '--version'], text=True).strip()
    return {'agent': chosen.id, 'version': reported.split()[-1], 'path': str(install.relative_to(ROOT)),
            'sha256': sha256_file(install), 'bytes': install.stat().st_size,
            'source': f'copied once from {source}; the automated path runs this copy and verifies these bytes, '
                      'never the self-updating original (U4)',
            'how_to_replace': 'install the version beside it, re-run scripts/pin-agent.py, and commit the new '
                              'sha256; an Agent whose bytes do not match its pin cannot run',
            'license_review': 'not reviewed; docs/license-review.json covers model licenses only'}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('agent', choices=sorted(adapters.ADAPTERS))
    parser.add_argument('--source', type=Path, help='the binary or release directory to pin')
    parser.add_argument('--check', action='store_true',
                        help='only verify the pinned bytes against the pin; change nothing')
    args = parser.parse_args()
    chosen = adapters.adapter(args.agent)
    if chosen.pin_path is None:
        raise SystemExit(f'{chosen.id} is pinned by lockfile (agents/{chosen.id}/), not by bytes; '
                         're-run the install recorded there instead')
    pin_path = Path(chosen.pin_path)
    if args.check:
        check(chosen, Path(chosen.install_dir or chosen.install))
        return
    if args.source is None:
        raise SystemExit('--source names the binary or release directory to pin')
    source = args.source.expanduser().resolve()
    if source.is_dir():
        pin = pin_directory(chosen, source, Path(chosen.install_dir or Path(chosen.install).parent))
    elif source.is_file():
        pin = pin_file(chosen, source, Path(chosen.install))
    else:
        raise SystemExit(f'{source} is neither a file nor a directory')
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = pin_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(pin, indent=1) + '\n')
    os.replace(temporary, pin_path)
    print(json.dumps({k: v for k, v in pin.items() if k in ('agent', 'version', 'path', 'sha256',
                                                             'tree_sha256', 'bytes', 'files')}))


if __name__ == '__main__':
    main()
