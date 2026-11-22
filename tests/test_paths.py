"""The instance root is CODE unless AGENTIAC_ROOT names another directory (M6, T6.2, D6.2)."""
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import paths  # noqa: E402


class Roots(unittest.TestCase):
    def test_unset_means_an_unsplit_checkout(self):
        self.assertEqual(paths.root({}), paths.CODE)

    def test_empty_means_unset(self):
        self.assertEqual(paths.root({'AGENTIAC_ROOT': ''}), paths.CODE)

    def test_a_directory_is_the_instance_root_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(paths.root({'AGENTIAC_ROOT': directory}), Path(directory).resolve())

    def test_a_relative_value_is_made_absolute(self):
        found = paths.root({'AGENTIAC_ROOT': 'site'})
        self.assertTrue(found.is_absolute())
        self.assertEqual(found, (Path(os.getcwd()) / 'site').resolve())

    def test_code_is_the_checkout_holding_this_module(self):
        self.assertTrue((paths.CODE / 'src/agentiac/paths.py').exists())


class Locate(unittest.TestCase):
    """A path an installation names is found on the side `layout` gives it, in the core that installation runs."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.site = Path(directory.name)

    def test_an_unsplit_checkout_holds_both_sides(self):
        self.assertEqual(paths.installed_core(self.site), self.site)
        self.assertEqual(paths.locate('requirements-gateway.lock', self.site), self.site / 'requirements-gateway.lock')
        self.assertEqual(paths.locate('backends/x.json', self.site), self.site / 'backends/x.json')

    def test_a_site_that_pins_the_core_finds_code_in_it_and_declarations_in_itself(self):
        (self.site / 'core').mkdir()
        self.assertEqual(paths.locate('requirements-gateway.lock', self.site),
                         self.site / 'core/requirements-gateway.lock')
        self.assertEqual(paths.locate('agents/codex/pin.json', self.site), self.site / 'agents/codex/pin.json')
        self.assertEqual(paths.locate('benchmarks/suite/tasks', self.site), self.site / 'benchmarks/suite/tasks')
        self.assertEqual(paths.locate('benchmarks/Dockerfile', self.site), self.site / 'core/benchmarks/Dockerfile')

    def test_without_an_instance_root_the_configured_one_is_used(self):
        self.assertEqual(paths.locate('backends/x.json'), paths.root() / 'backends/x.json')


class OneRootDerivation(unittest.TestCase):
    """Only the two paths modules work out a root from their own location: everything else asks them."""

    def test_no_other_package_module_derives_a_root_from_its_file(self):
        found = sorted(str(path.relative_to(paths.CODE)) for path in (paths.CODE / 'src/agentiac').rglob('*.py')
                       if re.search(r'parents\[\d\]', path.read_text()))
        self.assertEqual(found, ['src/agentiac/management/paths.py', 'src/agentiac/paths.py'])

    def test_the_context_probe_takes_only_its_code_from_its_location(self):
        text = (paths.CODE / 'benchmarks/context-probe.py').read_text()
        self.assertEqual(re.findall(r'^(\w+) = .*parents\[\d\]', text, re.M), ['CODE'])
        self.assertIn('ROOT = paths.root()', text)


if __name__ == '__main__':
    unittest.main()
