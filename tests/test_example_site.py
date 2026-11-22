"""D8.3: examples/site is a complete, valid site with synthetic names, and `make example-check` has a list to run.

Class A: it reads examples/site, which is a core path, and the core's own files. The last test also compares
examples/site with the site this runs against, and skips when that site is examples/site itself.
"""
from pathlib import Path
import sys
import unittest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
sys.path.insert(0, str(CODE / 'tests'))
from agentiac import declared, paths  # noqa: E402
import test_site_literals as literals  # noqa: E402

EXAMPLE = CODE / 'examples/site'
LIST = CODE / 'tests/example-site.txt'


def listed():
    rows = [line.split() for line in LIST.read_text().splitlines() if line.strip() and not line.startswith('#')]
    return [row[0] for row in rows]


class ExampleSiteTests(unittest.TestCase):
    def test_the_list_names_existing_modules_once(self):
        modules = listed()
        self.assertTrue(modules)
        self.assertEqual(len(modules), len(set(modules)))
        for module in modules:
            with self.subTest(module=module):
                self.assertTrue((CODE / f'tests/{module}.py').is_file())

    def test_the_example_site_is_a_valid_site(self):
        self.assertTrue((EXAMPLE / 'infra/inventory.yml').is_file())
        self.assertEqual(declared.references(declared.load(EXAMPLE)), [])

    def test_its_names_are_synthetic_and_appear_nowhere_in_the_core(self):
        names = literals.names(EXAMPLE)
        self.assertTrue(names)
        self.assertEqual(sorted(name for name in names if not name.startswith('example-')), [])
        self.assertEqual(literals.counts(CODE, names), {})   # the site-literal test is clean against it

    def test_no_name_of_the_site_this_runs_against_appears_in_it(self):
        site = paths.root()
        if site.resolve() == EXAMPLE.resolve():
            self.skipTest('running against examples/site itself')
        names = literals.names(site) or set()
        pattern = literals.pattern(names) if names else None
        found = [f'{path.relative_to(EXAMPLE)}: {match}' for path in sorted(EXAMPLE.rglob('*')) if path.is_file()
                 and pattern for match in pattern.findall(path.read_text())]
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
