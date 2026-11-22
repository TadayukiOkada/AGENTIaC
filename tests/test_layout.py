"""Every tracked file is core or site (M6, T6.1); M9 settled the last `review` paths (T9.3)."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import layout  # noqa: E402


def tracked():
    result = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True)
    return None if result.returncode else [line for line in result.stdout.splitlines() if line]


class Classification(unittest.TestCase):
    def test_every_tracked_file_is_classified(self):
        paths = tracked()
        if paths is None:
            self.skipTest('git ls-files failed')
        unclassified = [path for path in paths if layout.classify(path) == 'review'
                        and path not in layout.REVIEW_FILES and not path.startswith(layout.REVIEW_PREFIXES)]
        self.assertEqual(unclassified, [], 'no list in src/agentiac/layout.py covers these paths')

    def test_spot_checks(self):
        for path, expected in (('infra/inventory.yml', 'site'), ('infra/control.yml', 'core'),
                               ('gateway/gateway_callbacks.py', 'core'), ('gateway/config.yaml', 'site'),
                               ('profiles/schema.json', 'core'), ('profiles/x.json', 'site'),
                               ('agents/codex/pin.json', 'site'), ('docs/progress.md', 'site'),
                               ('docs/usage.md', 'site'), ('docs/development.md', 'core'),
                               ('docs/adr/README.md', 'core'),
                               ('docs/adr/0001-x.md', 'core'), ('docs/adr/0002-x.md', 'core'),
                               ('docs/adr/0003-x.md', 'core'), ('docs/adr/0006-x.md', 'core'),
                               ('docs/adr/0008-x.md', 'core'), ('docs/adr/0010-x.md', 'site'),
                               ('docs/adr/0012-x.md', 'site'),
                               ('agents/skills/session-start/SKILL.md', 'site'), ('agents/jetson-skills.json', 'site'),
                               ('tests/test_catalog.py', 'core'), ('tests/test_eval_image.py', 'site'), ('tests/test_layout.py', 'core'),
                               ('tests/records.py', 'site'), ('dashboard/tests/smoke.mjs', 'core'),
                               ('secrets/gateway.env', 'site'), ('requirements-control.lock', 'core'),
                               ('LICENSE', 'core'), ('NOTICE', 'core'), ('benchmarks/Dockerfile', 'core'), ('benchmarks/context-probe.py', 'core'),
                               ('benchmarks/bfcl-rescore.py', 'core'),
                               ('benchmarks/repository-task.example.json', 'core'),
                               ('benchmarks/suite/tasks/x.json', 'site'), ('benchmarks/suite/Dockerfile', 'site'),
                               ('benchmarks/suite/image.lock.json', 'site'), ('benchmarks/graders/test_x.py', 'site'),
                               ('benchmarks/bfcl-provenance.json', 'site')):
            with self.subTest(path=path):
                self.assertEqual(layout.classify(path), expected)


    def test_no_tracked_file_awaits_review(self):
        paths = tracked()
        if paths is None:
            self.skipTest('git ls-files failed')
        self.assertEqual([path for path in paths if layout.classify(path) == 'review'], [])
        self.assertEqual((layout.REVIEW_PREFIXES, layout.REVIEW_FILES), ((), frozenset()))


class SiteTests(unittest.TestCase):
    """Class C tests are the exact complement of the modules that run against examples/site (D6.6, T9.3)."""

    def setUp(self):
        self.example = {line.split()[0] for line in (ROOT / 'tests/example-site.txt').read_text().splitlines()
                        if line.strip() and not line.startswith('#')}
        self.paths = tracked()
        if self.paths is None:
            self.skipTest('git ls-files failed')

    def test_no_example_site_module_is_a_site_test(self):
        self.assertEqual(sorted(self.example & layout.SITE_TEST_MODULES), [])

    def test_every_tracked_test_module_is_on_exactly_one_list(self):
        modules = {Path(path).stem for path in self.paths if path.startswith('tests/test_') and path.endswith('.py')}
        self.assertEqual(sorted(modules - self.example - layout.SITE_TEST_MODULES), [],
                         'add the module to tests/example-site.txt (core) or layout.SITE_TEST_MODULES (site)')

    def test_the_site_tests_are_all_here_or_none_are(self):
        # An unsplit checkout tracks every one (so a stale entry fails); the published core tracks none.
        here = [name for name in sorted(layout.SITE_TESTS) if name in set(self.paths)]
        self.assertIn(len(here), (0, len(layout.SITE_TESTS)), sorted(set(layout.SITE_TESTS) - set(here)))


if __name__ == '__main__':
    unittest.main()
