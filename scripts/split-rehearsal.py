#!/usr/bin/env python3
"""Copy the tracked files into a site tree that pins the core at `core/`, so the two roots are physically apart.

docs/implementation-plan/site-separation.md D6.1/D6.2 (T7.6, T7.7). While the code root and the instance root are one
directory, a code path reached through the instance root (or the reverse) resolves silently. This builds `DEST/site`
and `DEST/site/core` from `git ls-files` and `layout.side`, the rule `paths.locate` uses, so that every such
reference becomes a missing file when the tests run in `DEST/site/core` with `AGENTIAC_ROOT=DEST/site`: the layout
D6.1 gives a site checkout. Each tree is made a Git repository with one commit, as the site and its core submodule
will be, so tests that ask Git about a root run as they would there. No ignored file is copied: no `secrets/`, no
`artifacts/`. The virtual environments are linked into the core, because they belong to the code.

`--check` builds the trees in a temporary directory, runs the suite there and exits with its status (`make split-check`).
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import layout, paths  # noqa: E402
ROOT = paths.root()


def tracked():
    done = subprocess.run(['git', '-C', str(CODE), 'ls-files', '-z'], capture_output=True, text=True, check=True)
    return [name for name in done.stdout.split('\0') if name]


def build(dest):
    """Copy every tracked file to its side; return how many went to each."""
    site = dest / 'site'
    core = site / paths.CORE_DIR
    core.mkdir(parents=True)
    counts = {'core': 0, 'site': 0}
    for name in tracked():
        side = layout.side(name)
        target = (site if side == 'site' else core) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CODE / name, target, follow_symlinks=False)
        counts[side] += 1
    for environment in sorted(CODE.glob('.venv*')):
        (core / environment.name).symlink_to(environment.resolve())
    commit(core)
    commit(site, exclude=f'/{paths.CORE_DIR}/')   # the core is its own repository, as a submodule is
    return counts


def commit(tree, exclude=None):
    """One commit of everything in TREE, under a fixed identity: a repository, not a history."""
    git = ['git', '-C', str(tree), '-c', 'user.name=split-rehearsal', '-c', 'user.email=split-rehearsal@localhost',
           '-c', 'commit.gpgsign=false']
    subprocess.run([*git, 'init', '-q'], check=True)
    if exclude:
        (tree / '.git/info/exclude').write_text(exclude + '\n')
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-q', '-m', 'split rehearsal'], check=True)


def stage_site_tests(dest):
    """Copy the site's class C tests (layout.SITE_TESTS) into the core tree's tests/, after its commit.

    They use this site as their fixture and run against the core as they do in an unsplit checkout, until M10 ports
    them (D6.6). The committed core tree, the one that is scanned and published, does not contain them.
    """
    core = dest / 'site' / paths.CORE_DIR
    staged = 0
    for name in sorted(layout.SITE_TESTS):
        source = dest / 'site' / name
        if name.startswith('tests/') and source.is_file():
            target = core / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            staged += 1
    return staged


def check():
    """The whole suite in a fresh split tree; its exit status. The tree is removed afterwards."""
    with tempfile.TemporaryDirectory(prefix='split-rehearsal-') as base:
        dest, state = Path(base) / 'dest', Path(base) / 'state'
        counts = build(dest)
        core = dest / 'site' / paths.CORE_DIR
        print(f"site: {counts['site']} files, core: {counts['core']} files; "
              f"{stage_site_tests(dest)} site test files staged into the core for the run", flush=True)
        env = {**os.environ, 'AGENTIAC_ROOT': str(dest / 'site'), 'AGENTIAC_STATE_ROOT': str(state),
               'PYTHONPATH': 'src'}
        return subprocess.run([str(core / '.venv/bin/python'), '-m', 'unittest', 'discover', '-s', 'tests'],
                              cwd=core, env=env).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('dest', type=Path, metavar='DEST', nargs='?', help='a directory that does not exist yet')
    parser.add_argument('--check', action='store_true', help='build in a temporary directory and run the suite there')
    args = parser.parse_args(argv)
    if args.check:
        return check()
    if args.dest is None:
        parser.error('give DEST, or --check')
    dest = args.dest.resolve()
    if dest.exists():
        print(f'{dest} exists; give a path that does not, or remove it first', file=sys.stderr)
        return 2
    counts = build(dest)
    core = dest / 'site' / paths.CORE_DIR
    print(f"{dest}/site: {counts['site']} files, {core}: {counts['core']} files")
    print(f'cd {core} && AGENTIAC_ROOT={dest}/site PYTHONPATH=src .venv/bin/python -m unittest discover '
          f'-s tests 2>&1 | tail -40')
    print(f'cd {core} && AGENTIAC_ROOT={dest}/site PYTHONPATH=src .venv-dashboard/bin/python -m unittest '
          f"discover -s tests -p 'test_management*.py' 2>&1 | tail -5")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
