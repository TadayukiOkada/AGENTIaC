"""Merged agent branches and their worktrees, found so they can go.

Every change here is made on an `agent/` branch in its own worktree and lands on `main` as a squash merge. A squash
merge leaves the branch's own commits outside `main`'s history, so `git branch -d` and `git branch --merged` call
every such branch unmerged, and they pile up. This module reads which of them `main` already holds and says so;
deleting stays one explicit step.

A branch counts as merged when its tip is an ancestor of `origin/main`, or when GitHub reports a merged pull
request whose head was exactly that tip. A later commit on the branch makes the tip differ, so work added after
the merge is never called merged. Only `agent/` branches are considered: other branches are someone's own.
A worktree can go when its branch can and `git status` shows nothing; its ignored files (test leftovers under
`artifacts/`) are listed so the reader sees what the removal discards. The main checkout and the worktree the
command runs in are never candidates.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

PREFIX = 'agent/'
# Ignored entries every worktree has by construction (validation.md section 1 links the environments).
EXPECTED_IGNORED = ('.venv', '.venv-gateway', '.venv-dashboard', '.venv-lint')


def _git(repo, *argv):
    return subprocess.run(['git', '-C', str(repo), *argv], capture_output=True, text=True, timeout=30)


def merged_heads(repo, run=subprocess.run):
    """The head commits of merged pull requests, or None when GitHub could not be asked (no gh, no network)."""
    try:
        result = run(['gh', 'pr', 'list', '--state', 'merged', '--limit', '500', '--json', 'headRefOid'],
                     cwd=str(repo), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        return {pr['headRefOid'] for pr in json.loads(result.stdout)}
    except (ValueError, KeyError, TypeError):
        return None


def worktrees(repo):
    """[{path, branch or None, prunable}] from `git worktree list --porcelain`; the first is the main checkout."""
    found, entry = [], None
    for line in _git(repo, 'worktree', 'list', '--porcelain').stdout.splitlines():
        if line.startswith('worktree '):
            entry = {'path': Path(line.split(' ', 1)[1]), 'branch': None, 'prunable': False}
            found.append(entry)
        elif line.startswith('branch refs/heads/') and entry is not None:
            entry['branch'] = line.removeprefix('branch refs/heads/')
        elif line.startswith('prunable') and entry is not None:
            entry['prunable'] = True
    return found


def merged_how(repo, tip, base, heads):
    """Why TIP counts as merged ('in origin/main' or 'head of a merged pull request'), or None. BASE is origin/main."""
    if _git(repo, 'merge-base', '--is-ancestor', tip, base).returncode == 0:
        return 'in origin/main'
    if heads is not None and tip in heads:
        return 'head of a merged pull request'
    return None


def _ignored(path):
    """Top-level ignored entries of a worktree other than the environment links it is expected to have."""
    lines = _git(path, 'status', '--porcelain', '--ignored').stdout.splitlines()
    names = sorted({line[3:].split('/', 1)[0] for line in lines if line.startswith('!! ')})
    return [name for name in names if name not in EXPECTED_IGNORED and name != '__pycache__']


def survey(repo, heads=None, here=None):
    """What can go, and what stays with the reason. HEADS is merged_heads(); None means ancestry only."""
    repo = Path(repo)
    here = Path(here or os.getcwd()).resolve()
    trees = worktrees(repo)
    main_tree = trees[0]['path'] if trees else repo
    base = _git(repo, 'rev-parse', '--verify', '--quiet', 'origin/main').stdout.strip()
    tree_of = {tree['branch']: tree for tree in trees if tree['branch']}
    branches, kept = [], []
    listing = _git(repo, 'for-each-ref', '--format=%(refname:short) %(objectname)', f'refs/heads/{PREFIX}')
    for line in listing.stdout.splitlines():
        name, tip = line.split(' ', 1)
        tree = tree_of.get(name)
        if not base:
            kept.append({'branch': name, 'reason': 'origin/main is unknown here'})
            continue
        how = merged_how(repo, tip, base, heads)
        if how is None:
            kept.append({'branch': name, 'reason': 'not merged' + ('' if heads is not None else
                                                                   ' (GitHub not asked: a squash merge looks unmerged)')})
            continue
        item = {'branch': name, 'tip': tip[:7], 'how': how, 'worktree': None, 'ignored': []}
        if tree is not None:
            path = tree['path']
            if tree['prunable']:
                item['worktree'] = path  # the directory is gone; `git worktree prune` forgets it
            elif path.resolve() == main_tree.resolve() or here.is_relative_to(path.resolve()):
                kept.append({'branch': name, 'reason': f'checked out in {path}, where this runs'})
                continue
            elif _git(path, 'status', '--porcelain').stdout.strip():
                kept.append({'branch': name, 'reason': f'{path} has uncommitted or untracked files'})
                continue
            else:
                item['worktree'], item['ignored'] = path, _ignored(path)
        branches.append(item)
    return {'remove': branches, 'keep': kept, 'github': heads is not None}


def apply(repo, report):
    """Remove what survey() found. --force only discards ignored files: survey() required a clean status."""
    done = []
    _git(repo, 'worktree', 'prune')
    for item in report['remove']:
        path = item['worktree']
        if path is not None and Path(path).exists():
            result = _git(repo, 'worktree', 'remove', '--force', str(path))
            if result.returncode:
                raise RuntimeError(f"{path}: {result.stderr.strip()}")
            done.append(f'removed worktree {path}')
        result = _git(repo, 'branch', '-D', item['branch'])
        if result.returncode:
            raise RuntimeError(f"{item['branch']}: {result.stderr.strip()}")
        done.append(f"deleted branch {item['branch']} (was {item['tip']})")
    _git(repo, 'worktree', 'prune')
    return done


def screen(report):
    lines = []
    for item in report['remove']:
        where = f"  worktree {item['worktree']}" if item['worktree'] else ''
        extra = f"  (discards ignored: {', '.join(item['ignored'])})" if item['ignored'] else ''
        lines.append(f"remove  {item['branch']:<36} {item['tip']}  {item['how']}{where}{extra}")
    lines += [f"keep    {item['branch']:<36} {item['reason']}" for item in report['keep']]
    if not report['github']:
        lines.append('note    GitHub was not asked (gh unavailable); squash-merged branches are kept')
    lines.append(f"{len(report['remove'])} to remove, {len(report['keep'])} kept")
    return '\n'.join(lines)
