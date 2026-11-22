#!/usr/bin/env python3
"""The publication scan (M9, T9.6; site-separation.md D6.7 item 2): is the core tree fit to be published?

Builds a split rehearsal of this checkout in a temporary directory (`split-rehearsal.py`), then checks the core tree
that rehearsal holds:

  1. `tests/test_site_literals.py` against the site tree: no core file names a host, address or account of the site.
  2. The dated-line grep (`20\\d\\d-\\d\\d-\\d\\d`) over every committed core file: each line it finds must be listed as
     data in the site's `docs/implementation-plan/m9-dated-lines.md` (T9.4); a dated comment or message is a record,
     and records stay out of the core (D6.4).
  3. `gitleaks dir --redact --exit-code 1` with the core's `.gitleaks.toml` (default rules, no allowlist).

Exit 0 only when all three pass. `--rehearsal DEST` checks a rehearsal that already exists (and keeps it) instead of
building one; `--gitleaks PATH` names the binary (default `.venv-lint/bin/gitleaks`, installed by
`scripts/gitleaks-install.sh`). Nothing it prints quotes a finding's secret: gitleaks runs with `--redact`.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import layout  # noqa: E402

DATED = re.compile(r'20\d\d-\d\d-\d\d')
LIST = 'docs/implementation-plan/m9-dated-lines.md'


def tracked(tree):
    done = subprocess.run(['git', '-C', str(tree), 'ls-files', '-z'], capture_output=True, text=True, check=True)
    return [name for name in done.stdout.split('\0') if name]


def listed(site):
    """{(path, line text)} that the site's dated-line list accepts as data; empty when the list is missing."""
    path = Path(site) / LIST
    if not path.exists():
        return set()
    accepted = set()
    for row in path.read_text().splitlines():
        if not row.startswith('| `'):
            continue
        cells = row.strip().strip('|').split(' | ')
        if len(cells) < 3:
            continue
        where = cells[0].strip().strip('`').rsplit(':', 1)[0]
        text = ' | '.join(cells[1:-1]).strip()
        if len(text) > 1 and text.startswith('`') and text.endswith('`'):
            text = text[1:-1]                       # the list writes each line inside one pair of backticks
        accepted.add((where, text))
    return accepted


def unlisted_dated_lines(core, site):
    """`path:line: text` for every dated line of a committed core file that the list does not accept."""
    accepted = listed(site)
    found = []
    for name in tracked(core):
        if layout.classify(name) != 'core':
            continue
        try:
            text = (Path(core) / name).read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if DATED.search(line) and (name, line.strip()) not in accepted:
                found.append(f'{name}:{number}: {line.strip()}')
    return found


def site_literals(core, site):
    env = {**os.environ, 'AGENTIAC_ROOT': str(site), 'PYTHONPATH': str(Path(core) / 'src'),
           'AGENTIAC_STATE_ROOT': tempfile.mkdtemp()}
    try:
        done = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p',
                               'test_site_literals.py'], cwd=core, env=env, capture_output=True, text=True)
    finally:
        shutil.rmtree(env['AGENTIAC_STATE_ROOT'], ignore_errors=True)
    return done.returncode, (done.stdout + done.stderr).strip().splitlines()[-3:]


def scan(rehearsal, gitleaks):
    site = rehearsal / 'site'
    core = site / 'core'
    failed = False

    code, tail = site_literals(core, site)
    print(f'site-literal test against the site: {"ok" if code == 0 else "FAILED"} ({" / ".join(tail)})')
    failed |= code != 0

    dated = unlisted_dated_lines(core, site)
    print(f'dated lines outside {LIST}: {len(dated)}')
    for line in dated:
        print(f'  {line}')
    failed |= bool(dated)

    if not Path(gitleaks).exists():
        print(f'gitleaks is not installed at {gitleaks}: run scripts/gitleaks-install.sh', file=sys.stderr)
        return 2
    done = subprocess.run([str(gitleaks), 'dir', str(core), '--config', str(core / '.gitleaks.toml'), '--redact',
                           '--no-banner', '--exit-code', '1'], capture_output=True, text=True)
    summary = (done.stderr.strip().splitlines() or ['no output'])[-1]
    print(f'gitleaks: {"no leaks found" if done.returncode == 0 else "FINDINGS"} (exit {done.returncode}; {summary})')
    if done.returncode:
        print(done.stdout.strip())
    failed |= done.returncode != 0

    print('publish-scan:', 'FAILED' if failed else 'ok')
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--rehearsal', type=Path, metavar='DEST', help='check this existing rehearsal and keep it')
    parser.add_argument('--gitleaks', type=Path, default=CODE / '.venv-lint/bin/gitleaks', metavar='PATH')
    args = parser.parse_args(argv)
    if args.rehearsal:
        return scan(args.rehearsal, args.gitleaks)
    with tempfile.TemporaryDirectory() as directory:
        rehearsal = Path(directory) / 'rehearsal'
        subprocess.run([sys.executable, str(CODE / 'scripts/split-rehearsal.py'), str(rehearsal)], check=True,
                       stdout=subprocess.DEVNULL)
        return scan(rehearsal, args.gitleaks)


if __name__ == '__main__':
    sys.exit(main())
