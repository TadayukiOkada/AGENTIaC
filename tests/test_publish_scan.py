"""T9.6: the dated-line step of the publication scan reads the site's list and reports only what it does not accept."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publish_scan', CODE / 'scripts/publish-scan.py')
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

# The dates are assembled here so that this file itself holds no dated line for the scan to find.
FIXTURE = '-'.join(('2001', '02', '03'))
COMMENT = '-'.join(('2002', '03', '04'))
STAMP = f"stamp = '{FIXTURE}T00:00:00Z'"
TABLE = ('| Where | Line | Reason |\n|---|---|---|\n'
         f'| `src/agentiac/a.py:3` | `{STAMP}` | a fixture timestamp |\n')


def git(tree, *argv):
    subprocess.run(['git', '-C', str(tree), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', *argv],
                   check=True, capture_output=True)


class DatedLineTests(unittest.TestCase):
    def tree(self, files, table=TABLE):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        core, site = root / 'core', root / 'site'
        core.mkdir()
        git(core, 'init', '-q')
        for name, text in files.items():
            (core / name).parent.mkdir(parents=True, exist_ok=True)
            (core / name).write_text(text)
        git(core, 'add', '-A')
        git(core, 'commit', '-q', '-m', 'one')
        if table is not None:
            (site / scan.LIST).parent.mkdir(parents=True)
            (site / scan.LIST).write_text(table)
        return core, site

    def test_a_listed_line_is_accepted_wherever_it_moved_to(self):
        core, site = self.tree({'src/agentiac/a.py': f'x = 1\n\n\n{STAMP}\n'})
        self.assertEqual(scan.unlisted_dated_lines(core, site), [])

    def test_a_dated_comment_is_reported_with_its_place(self):
        core, site = self.tree({'src/agentiac/a.py': f'# fixed on {COMMENT}\n{STAMP}\n'})
        self.assertEqual(scan.unlisted_dated_lines(core, site), [f'src/agentiac/a.py:1: # fixed on {COMMENT}'])

    def test_a_listed_text_in_another_file_is_not_accepted(self):
        core, site = self.tree({'src/agentiac/b.py': f'{STAMP}\n'})
        self.assertEqual(len(scan.unlisted_dated_lines(core, site)), 1)

    def test_without_a_list_every_dated_line_is_reported(self):
        core, site = self.tree({'src/agentiac/a.py': f'{STAMP}\n'}, table=None)
        self.assertEqual(len(scan.unlisted_dated_lines(core, site)), 1)

    def test_files_the_site_owns_are_not_scanned(self):
        core, site = self.tree({'docs/progress.md': f'{COMMENT} a record\n', 'src/agentiac/a.py': 'x = 1\n'})
        self.assertEqual(scan.unlisted_dated_lines(core, site), [])


if __name__ == '__main__':
    unittest.main()
