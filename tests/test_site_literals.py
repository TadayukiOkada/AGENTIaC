"""The core names no more of this site than it did: a per-file ratchet on site names (M6, T6.3).

docs/implementation-plan/site-separation.md D6.5. A list of forbidden names cannot live in a public repository
without leaking them, so the names come from the inventory of the instance root this runs against (`paths.root()`):
the worker host names except the local one, their `ansible_host` values and `all.vars.ansible_user`. In the unsplit
repository the per-file baseline may only shrink; after the extraction it is empty, and the site's CI runs the same
test against the real site. No name is written in this file. Every core-classified file is counted, tests and
documents included: whatever the core publishes is what must not name the site (M9 review, decision 5).
"""
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

import yaml

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import layout, paths  # noqa: E402

BASELINE = Path(__file__).with_name('site-literals-baseline.json')


def names(root):
    """The site's names, from ROOT's inventory; None when there is no inventory to read."""
    inventory = Path(root) / 'infra/inventory.yml'
    if not inventory.exists():
        return None
    everything = yaml.safe_load(inventory.read_text())['all']
    hosts = (everything.get('children') or {}).get('workers', {}).get('hosts') or {}
    found = set()
    for host, facts in hosts.items():
        facts = facts or {}
        if facts.get('ansible_connection') == 'local':
            continue
        found.add(str(host))
        if facts.get('ansible_host'):
            found.add(str(facts['ansible_host']))
    user = (everything.get('vars') or {}).get('ansible_user')
    if user:
        found.add(str(user))
    return {name for name in found if name}


def pattern(found):
    return re.compile(r'\b(' + '|'.join(re.escape(n) for n in sorted(found, key=len, reverse=True)) + r')\b')


def tracked(code):
    result = subprocess.run(['git', 'ls-files'], cwd=code, capture_output=True, text=True)
    return None if result.returncode else [line for line in result.stdout.splitlines() if line]


# Third-party identifiers that contain a site's name by coincidence, blanked before counting (T8.11, owner-approved):
# the suite's benchmark repository and the names its code uses. Any other occurrence of the name still counts.
THIRD_PARTY = ('open-genie-server', 'genie-server')
_THIRD_PARTY = re.compile('|'.join(re.escape(name) for name in sorted(THIRD_PARTY, key=len, reverse=True)))


def counts(code, found, files=None):
    """{path: occurrences} for the core files that name the site at least once."""
    files = tracked(code) if files is None else files
    regex = pattern(found)
    result = {}
    for path in files:
        # examples/site is a site of its own: its names belong there, and test_example_site checks that it holds
        # none of the site this runs against.
        if layout.classify(path) != 'core' or path.startswith('examples/site/'):
            continue
        file = Path(code) / path
        if not file.is_file():
            continue
        number = len(regex.findall(_THIRD_PARTY.sub(' ', file.read_text(encoding='utf-8', errors='ignore'))))
        if number:
            result[path] = number
    return result


def current():
    found, files = names(paths.root()), tracked(CODE)
    if found is None or files is None:
        return None
    return counts(CODE, found, files)


def baseline():
    return json.loads(BASELINE.read_text()) if BASELINE.exists() else {}


class SiteLiterals(unittest.TestCase):
    def setUp(self):
        self.current = current()
        if self.current is None:
            self.skipTest('git ls-files failed or the instance root has no infra/inventory.yml')
        self.baseline = baseline()

    def test_no_core_file_gains_a_site_name(self):
        risen = [f'{path}: {count} > {self.baseline.get(path, 0)}' for path, count in sorted(self.current.items())
                 if count > self.baseline.get(path, 0)]
        self.assertEqual(risen, [], 'core files that name the site more often than the baseline allows')

    def test_baseline_is_not_stale(self):
        stale = [f'{path}: baseline {count}, now {self.current.get(path, 0)}'
                 for path, count in sorted(self.baseline.items()) if self.current.get(path, 0) != count]
        self.assertEqual(stale, [], 'run: PYTHONPATH=src .venv/bin/python tests/test_site_literals.py --lower')



class ThirdPartyMaskTests(unittest.TestCase):
    """The mask blanks only the listed identifiers: a site name elsewhere in the same text still counts."""

    def test_only_the_listed_identifiers_are_blanked(self):
        host = THIRD_PARTY[1].split('-')[0]     # a site name that the listed identifiers contain
        regex = pattern({host})
        text = f'repo open-{host}-server, callback {host}-server-logprobs-x; claim {host}; the {host} host'
        self.assertEqual(len(regex.findall(_THIRD_PARTY.sub(' ', text))), 2)
        self.assertEqual(len(regex.findall(text)), 4)

def write(table):
    BASELINE.write_text(json.dumps(dict(sorted(table.items())), indent=1) + '\n')


if __name__ == '__main__':
    if sys.argv[1:] == ['--lower']:
        now, old = current(), baseline()
        write({path: min(old.get(path, count), count) for path, count in now.items() if count > 0})
    elif sys.argv[1:] == ['--init']:
        if BASELINE.exists():
            raise SystemExit(f'{BASELINE} exists; the baseline is created once and only lowered')
        write(current())
    else:
        unittest.main()
