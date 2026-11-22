"""Commit-history task suite: derive gradable tasks from a fix commit and grade a solution.

A task is (base, reference) in a read-only source clone. The agent sees the base
tree. Grading replaces the solution's tests/ with the reference commit's tests/
and runs the full pytest suite in the pinned suite image with no network:
acceptance tests (fail on base, pass on reference) must pass and regression tests
(pass on both) must keep passing.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROLE = 'suite:v1'   # the task suite's image: `<resource_prefix>-suite:v1` (D6.8)
OUTCOME = re.compile(r'^(PASSED|FAILED|ERROR|XFAIL|XPASS) (\S+)(?: - (.*))?$')


def image_name():
    """The task suite's image tag."""
    from agentiac import declared
    return declared.resource(ROLE)


def __getattr__(name):
    """`suite.IMAGE`, resolved on first use rather than at import (PEP 562)."""
    if name == 'IMAGE':
        return image_name()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def git(source, *args):
    return subprocess.run(['git', '-C', str(source)] + list(args), capture_output=True, text=True, check=True).stdout


def export(source, rev, dest):
    """Write the tree of REV into DEST (no .git)."""
    dest.mkdir(parents=True, exist_ok=True)
    with subprocess.Popen(['git', '-C', str(source), 'archive', rev], stdout=subprocess.PIPE) as archive:
        subprocess.run(['tar', '-x', '-C', str(dest)], stdin=archive.stdout, check=True)
        if archive.wait():
            raise RuntimeError(f'git archive {rev} failed')


def overlay_tests(tree, source, rev):
    """Replace TREE/tests with the tests/ directory of REV."""
    shutil.rmtree(tree / 'tests', ignore_errors=True)
    with tempfile.TemporaryDirectory() as scratch:
        export(source, rev, Path(scratch))
        shutil.copytree(Path(scratch) / 'tests', tree / 'tests')


def parse_outcomes(text):
    """Map pytest node id -> (outcome, message) from `pytest -rA` summary lines."""
    outcomes = {}
    for line in text.splitlines():
        match = OUTCOME.match(line)
        if match:
            outcomes[match.group(2)] = (match.group(1).lower(), (match.group(3) or '')[:300])
    return outcomes


def pytest(tree, image=None, timeout=900):
    image = image or image_name()
    """Run the tree's full pytest suite offline; return (outcomes, returncode, output).

    A test that hangs (an unfixed race, say) fails after 120 s instead of stalling the run.
    """
    command = ['docker', 'run', '--rm', '--network=none', '--read-only', '--label', 'io.agent-infra.owner=agent-infra',
               '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--memory=2g', '--cpus=2', '--pids-limit=256', '--tmpfs', '/tmp:rw,nosuid,size=256m',
               '-e', 'HOME=/tmp', '-e', 'PYTHONDONTWRITEBYTECODE=1', '-v', f'{tree}:/work:ro', '-w', '/work', image,
               'python3', '-m', 'pytest', '-q', '-rA', '--tb=line', '-p', 'no:cacheprovider', '--timeout=120']
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    return parse_outcomes(result.stdout), result.returncode, result.stdout + result.stderr


def derive(source, reference, base=None, image=None):
    image = image or image_name()
    """Acceptance/regression test ids for the fix commit REFERENCE against BASE (default: its parent)."""
    base = base or git(source, 'rev-parse', reference + '^').strip()
    reference = git(source, 'rev-parse', reference).strip()
    with tempfile.TemporaryDirectory() as scratch:
        before, after = Path(scratch) / 'base', Path(scratch) / 'reference'
        export(source, base, before)
        overlay_tests(before, source, reference)
        export(source, reference, after)
        at_base, _, _ = pytest(before, image)
        at_reference, _, _ = pytest(after, image)
    passed = lambda outcomes, node: outcomes.get(node, ('missing',))[0] == 'passed'
    return {'base': base, 'reference': reference,
            'changed_paths': git(source, 'diff', '--name-only', base, reference).split(),
            'acceptance': sorted(n for n in at_reference if passed(at_reference, n) and not passed(at_base, n)),
            'regression': sorted(n for n in at_base if passed(at_base, n) and passed(at_reference, n)),
            'broken_by_reference': sorted(n for n in at_base if passed(at_base, n) and not passed(at_reference, n)),
            'base_failures': {n: at_base.get(n, ('missing', ''))
                              for n in at_reference if passed(at_reference, n) and not passed(at_base, n)}}


def grade(solution, source, task, image=None):
    image = image or image_name()
    """Grade a solution tree (copied, never modified) against TASK's acceptance and regression ids."""
    with tempfile.TemporaryDirectory() as scratch:
        tree = Path(scratch) / 'tree'
        shutil.copytree(solution, tree, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        overlay_tests(tree, source, task['reference'])
        outcomes, returncode, output = pytest(tree, image)
    failed = lambda ids: sorted(n for n in ids if outcomes.get(n, ('missing',))[0] != 'passed')
    acceptance, regression = failed(task['acceptance']), failed(task['regression'])
    return {'passed': returncode == 0 and not acceptance and not regression,
            'acceptance_failed': acceptance, 'acceptance_total': len(task['acceptance']),
            'regression_failed': regression, 'regression_total': len(task['regression']),
            'pytest_returncode': returncode}, output
