#!/usr/bin/env python3
"""Rename an extracted core tree from the site's legacy names to AGENTIaC / `agentiac` (M9, T9.9; D6.8).

Given a clean Git work tree holding the core files of a split rehearsal:
  - moves `src/<old_pkg>` -> `src/agentiac` and `bin/<old_cmd>` -> `bin/agentiac` with `git mv`;
  - rewrites imports, the distribution name and entry point, environment variable prefix -> `AGENTIAC_`,
    and the project name in `layout.CORE_DOCS` and across the core files;
  - leaves the legacy hyphenated/underscored token only where T9.1 section 6 lists it as surviving (the
    `"formerly ..."` note in `README.md`, the `resource_prefix` declaration notes/tests, the deployed Docker
    labels and probe protocol marker, and the systemd unit templates' `Description=`/`ExecStart=`/`ReadWritePaths=`);
  - refuses a directory that is not a clean Git work tree.

The old tokens are assembled from parts below so this file itself holds no literal occurrence when `grep -rn` runs on
the renamed core tree.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import layout, paths  # noqa: E402

OLD_PKG = '_'.join(('agent', 'infra'))
OLD_CMD = '-'.join(('agent', 'infra'))
OLD_ENV = '_'.join(('AGENT', 'INFRA', ''))
NEW_PKG = 'agentiac'
NEW_CMD = 'agentiac'
NEW_ENV = 'AGENTIAC_'
PROJECT = 'AGENTIaC'

# Substrings that identify a line where the legacy token survives by design (T9.1 section 6, D6.8):
#   - the "formerly ..." note in README.md;
#   - resource_prefix declaration notes and tests (`declared.py`, `test_declared_settings.py`);
#   - deployed container/image labels (`io.<old>.owner=<old>`, `<old>.dockerfile-sha256`) and the minimal probe marker;
#   - systemd unit template directives (`Description=`, `ExecStart=`, `ReadWritePaths=`) so `ops deploy --check`
#     renders the same units except `AGENTIAC_*` -> `AGENTIAC_*` environment lines.
SURVIVOR_MARKERS = (
    f'formerly {OLD_CMD}',
    f'io.{OLD_CMD}.owner',
    f'io.{OLD_CMD}.profile',
    f'{OLD_CMD}.dockerfile-sha256',
    f'--{OLD_CMD}-minimal-probe-v1',
    f'/var/log/{OLD_CMD}',
)
UNIT_DIRECTIVES = ('Description=', 'ReadWritePaths=')


def is_survivor(rel_path, line):
    """True when LINE in repository-relative REL_PATH is an intentional survivor of the rename."""
    stripped = line.strip()
    if any(marker in line for marker in SURVIVOR_MARKERS):
        return True
    if rel_path in (f'src/{OLD_PKG}/declared.py', f'src/{NEW_PKG}/declared.py') and f'`{OLD_CMD}`, so nothing' in line:
        return True
    if rel_path == 'tests/test_declared_settings.py' and (
            f"'resource_prefix': '{OLD_CMD}'" in line or f"'{OLD_PKG}'" in line):
        return True
    if rel_path.startswith('infra/templates/') and stripped.startswith(UNIT_DIRECTIVES):
        return True
    return False


def check_clean_git_tree(tree):
    """Refuse unless TREE is the root of a clean Git work tree."""
    tree = Path(tree).resolve()
    if not (tree / '.git').exists():
        raise SystemExit(f'{tree} is not a Git work tree (missing .git)')
    inside = subprocess.run(['git', '-C', str(tree), 'rev-parse', '--show-toplevel'],
                            capture_output=True, text=True)
    if inside.returncode != 0 or Path(inside.stdout.strip()).resolve() != tree:
        raise SystemExit(f'{tree} is not the top level of a Git work tree')
    status = subprocess.run(['git', '-C', str(tree), 'status', '--porcelain'],
                            capture_output=True, text=True, check=True)
    if status.stdout.strip():
        raise SystemExit(f'{tree} has uncommitted changes; rename-core.py requires a clean Git work tree')


def tracked(tree):
    done = subprocess.run(['git', '-C', str(tree), 'ls-files', '-z'], capture_output=True, text=True, check=True)
    return [name for name in done.stdout.split('\0') if name]


def transform_line(rel_path, line):
    """Apply the D6.8 rename to one line of REL_PATH while preserving T9.1 section 6 survivors."""
    if rel_path == 'README.md' and line.strip() == f'# {OLD_CMD}':
        return f'# {PROJECT} (formerly {OLD_CMD})'
    if rel_path in ('bin/' + OLD_CMD, 'bin/' + NEW_CMD) and f'{OLD_ENV}COMMAND=$(basename "$0")' in line:
        return line.replace(f'{OLD_ENV}COMMAND=$(basename "$0")',
                            f'{NEW_ENV}COMMAND=${{{NEW_ENV}COMMAND:-$(basename "$0")}}')
    if rel_path == 'tests/test_audit.py' and f"'{OLD_CMD} ops update check'" in line:
        line = line.replace(f"'{OLD_CMD} ops update check'", "f'{doctor.cli_name()} ops update check'")
    if rel_path == 'tests/test_hygiene.py':
        line = line.replace(f"'{OLD_CMD} worktrees prune --apply'", "f'{doctor.cli_name()} worktrees prune --apply'")
        line = line.replace(f"'{OLD_CMD} worktrees prune'", "f'{doctor.cli_name()} worktrees prune'")
    if rel_path == 'scripts/drift.py' and f'f"{OLD_CMD} {{\'claim \'' in line:
        line = line.replace(f'f"{OLD_CMD} {{\'claim \'', 'f"{cli_name()} {\'claim \'')

    # Environment variable prefix always renames, even on unit template lines.
    line = line.replace(OLD_ENV, NEW_ENV)

    if is_survivor(rel_path, line):
        return line

    name = Path(rel_path).name
    if rel_path == 'README.md' or (rel_path.startswith('docs/') and name in layout.CORE_DOCS):
        line = line.replace(f'~/src/{OLD_CMD}-', f'~/src/{PROJECT}-')
        line = line.replace(f'~/src/{OLD_CMD}', f'~/src/{PROJECT}')

    return line.replace(OLD_PKG, NEW_PKG).replace(OLD_CMD, NEW_CMD)


def rename(tree):
    """Rename a clean core Git tree in place and commit the result."""
    tree = Path(tree).resolve()
    check_clean_git_tree(tree)
    old_pkg_dir = tree / 'src' / OLD_PKG
    old_bin = tree / 'bin' / OLD_CMD
    if not old_pkg_dir.is_dir():
        raise SystemExit(f'{tree} has no src/{OLD_PKG} directory to rename')
    if not old_bin.is_file():
        raise SystemExit(f'{tree} has no bin/{OLD_CMD} to rename')

    subprocess.run(['git', '-C', str(tree), 'mv', f'src/{OLD_PKG}', f'src/{NEW_PKG}'], check=True)
    subprocess.run(['git', '-C', str(tree), 'mv', f'bin/{OLD_CMD}', f'bin/{NEW_CMD}'], check=True)
    (tree / 'bin' / NEW_CMD).chmod(0o755)

    changed = 0
    for rel_path in tracked(tree):
        path = tree / rel_path
        try:
            original = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        lines = original.splitlines(keepends=True)
        updated = ''.join(transform_line(rel_path, line) for line in lines)
        if updated != original:
            path.write_text(updated, encoding='utf-8')
            changed += 1

    git = ['git', '-C', str(tree), '-c', 'user.name=rename-core', '-c', 'user.email=rename-core@localhost',
           '-c', 'commit.gpgsign=false']
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-q', '-m', f'Rename core to {PROJECT}'], check=True)
    return changed


def scan_occurrences(tree):
    """Return (survivors, unexpected) across all tracked files of TREE for the legacy names and env prefix."""
    tree = Path(tree).resolve()
    survivors, unexpected = [], []
    for rel_path in tracked(tree):
        path = tree / rel_path
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            has_env = OLD_ENV in line
            has_name = OLD_PKG in line or OLD_CMD in line
            if not has_env and not has_name:
                continue
            entry = f'{rel_path}:{number}: {line.strip()}'
            if not has_env and is_survivor(rel_path, line):
                survivors.append(entry)
            else:
                unexpected.append(entry)
    return survivors, unexpected


def write_site_wrapper(site):
    """Write the D6.1 wrapper `bin/<old_cmd>` into a rehearsal's site directory."""
    wrapper = Path(site) / 'bin' / OLD_CMD
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        '#!/bin/sh\n'
        f'{NEW_ENV}ROOT=$(dirname "$(dirname "$(readlink -f "$0")")")\n'
        f'{NEW_ENV}COMMAND=$(basename "$0")\n'
        f'export {NEW_ENV}ROOT {NEW_ENV}COMMAND\n'
        f'exec "${NEW_ENV}ROOT/{paths.CORE_DIR}/bin/{NEW_CMD}" "$@"\n',
        encoding='utf-8',
    )
    wrapper.chmod(0o755)
    return wrapper


def stage_rehearsal_site(site, core):
    """Adapt the temporary rehearsal's site files and stage class C tests into CORE for the full-suite run."""
    site, core = Path(site), Path(core)
    archive = site / 'scripts/archive'
    if archive.is_dir():
        for path in sorted(archive.glob('*.py')):
            text = path.read_text(encoding='utf-8')
            path.write_text(text.replace(OLD_ENV, NEW_ENV).replace(OLD_PKG, NEW_PKG), encoding='utf-8')

    for doc in ('docs/compatibility.md', 'docs/model-names.md'):
        path = site / doc
        if path.is_file():
            text = path.read_text(encoding='utf-8')
            text = text.replace(f'src/{OLD_PKG}/', f'src/{NEW_PKG}/')
            text = text.replace(f'`{OLD_CMD} model', f'`{NEW_CMD} model')
            path.write_text(text, encoding='utf-8')

    staged = 0
    for name in sorted(layout.SITE_TESTS):
        source = site / name
        if not (name.startswith('tests/') and source.is_file()):
            continue
        target = core / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = source.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            target.write_bytes(source.read_bytes())
            staged += 1
            continue
        text = text.replace(OLD_ENV, NEW_ENV).replace(OLD_PKG, NEW_PKG)
        if name == 'tests/test_command.py':
            text = text.replace(f't {OLD_CMD} ', f't {NEW_CMD} ')
        elif name == 'tests/test_short_aliases.py':
            text = text.replace(f'"{OLD_CMD} ', f'"{NEW_CMD} ').replace(f"'{OLD_CMD} ", f"'{NEW_CMD} ")
        elif name == 'tests/test_sessions.py':
            text = text.replace(f"'{OLD_CMD} ops", f"'{NEW_CMD} ops")
            text = text.replace(f"'{OLD_CMD} not-a-command", f"'{NEW_CMD} not-a-command")
            text = text.replace(f"['{OLD_CMD}']", f"['{NEW_CMD}']")
        elif name in ('tests/test_iac.py', 'tests/test_recovery.py'):
            text = text.replace(f'{{{{ code_root }}}}/bin/{OLD_CMD} ', f'{{{{ code_root }}}}/bin/{NEW_CMD} ')
        target.write_text(text, encoding='utf-8')
        staged += 1
    return staged


def check():
    """Build a split rehearsal, rename its core, verify survivors, and run example-check and the full suite."""
    spec = importlib.util.spec_from_file_location('split_rehearsal', CODE / 'scripts/split-rehearsal.py')
    rehearsal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rehearsal)

    with tempfile.TemporaryDirectory(prefix='rename-check-') as base:
        base = Path(base)
        dest, state_example, state_site = base / 'dest', base / 'state-example', base / 'state-site'
        state_example.mkdir()
        state_site.mkdir()
        counts = rehearsal.build(dest)
        site = dest / 'site'
        core = site / paths.CORE_DIR
        changed = rename(core)
        survivors, unexpected = scan_occurrences(core)
        print(f"renamed {changed} core files (site: {counts['site']}, core: {counts['core']}); "
              f"{len(survivors)} survivor lines, {len(unexpected)} unexpected", flush=True)
        for line in survivors:
            print(f'  survivor: {line}')
        if unexpected:
            for line in unexpected:
                print(f'  UNEXPECTED: {line}', file=sys.stderr)
            return 1

        # 1. Full core suite against examples/site (without the wrapper: command name is agentiac).
        example_code = subprocess.run(
            ['make', 'example-check', f'PYTHON={core / ".venv/bin/python"}'],
            cwd=core,
        ).returncode
        if example_code != 0:
            return example_code

        # 2. Write the D6.1 wrapper into the rehearsal's site and verify it execs core/bin/agentiac.
        wrapper = write_site_wrapper(site)
        help_run = subprocess.run([str(wrapper), '--help'], capture_output=True, text=True)
        if help_run.returncode != 0 or f'usage: {OLD_CMD}' not in help_run.stdout:
            print(f'wrapper {wrapper} --help failed: {help_run.stdout} {help_run.stderr}', file=sys.stderr)
            return 1

        # 3. Full suite in the renamed core against the rehearsal's site.
        staged = stage_rehearsal_site(site, core)
        print(f'{staged} site test files staged into the renamed core for the site run', flush=True)
        env = {
            **os.environ,
            f'{NEW_ENV}ROOT': str(site),
            f'{NEW_ENV}STATE_ROOT': str(state_site),
            f'{NEW_ENV}COMMAND': OLD_CMD,
            'PYTHONPATH': 'src',
        }
        return subprocess.run(
            [str(core / '.venv/bin/python'), '-m', 'unittest', 'discover', '-s', 'tests'],
            cwd=core,
            env=env,
        ).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('tree', type=Path, metavar='CORE_TREE', nargs='?', help='clean Git work tree of the core')
    parser.add_argument('--check', action='store_true', help='run the full rename rehearsal and test suites')
    args = parser.parse_args(argv)
    if args.check:
        return check()
    if args.tree is None:
        parser.error('give CORE_TREE, or --check')
    changed = rename(args.tree)
    survivors, unexpected = scan_occurrences(args.tree)
    print(f'renamed {changed} files in {args.tree}; {len(survivors)} survivor lines, {len(unexpected)} unexpected')
    if unexpected:
        for line in unexpected:
            print(f'  UNEXPECTED: {line}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
