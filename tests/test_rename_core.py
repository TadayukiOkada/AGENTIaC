"""T9.9: scripts/rename-core.py renames a clean core Git tree to AGENTIaC / agentiac and preserves only survivors."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rename_core', CODE / 'scripts/rename-core.py')
renamer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renamer)

OLD_PKG = renamer.OLD_PKG
OLD_CMD = renamer.OLD_CMD
OLD_ENV = renamer.OLD_ENV


def git(tree, *argv):
    subprocess.run(['git', '-C', str(tree), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                    '-c', 'commit.gpgsign=false', *argv], check=True, capture_output=True)


class RenameCoreTests(unittest.TestCase):
    def synthetic_core(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        core = Path(directory.name) / 'core'
        core.mkdir()
        git(core, 'init', '-q')
        files = {
            f'src/{OLD_PKG}/__init__.py': f'from {OLD_PKG} import paths\n',
            f'src/{OLD_PKG}/paths.py': f"ENV = '{OLD_ENV}ROOT'\n",
            f'src/{OLD_PKG}/errors.py': f"COMMAND = '{OLD_CMD}'\nINVOKED = '{OLD_ENV}COMMAND'\n",
            f'src/{OLD_PKG}/declared.py': f'"""This site declares `{OLD_CMD}`, so nothing it has deployed is renamed."""\n',
            f'src/{OLD_PKG}/adapters.py': f"OWNER_LABEL = 'io.{OLD_CMD}.owner={OLD_CMD}'\n",
            f'src/{OLD_PKG}/operations/probe_remote.py': f"MARKER = '--{OLD_CMD}-minimal-probe-v1'\n",
            f'bin/{OLD_CMD}': (
                '#!/bin/sh\n'
                f'{OLD_ENV}COMMAND=$(basename "$0")\n'
                f'export {OLD_ENV}COMMAND\n'
                f'exec python3 -m {OLD_PKG} "$@"\n'
            ),
            'pyproject.toml': (
                '[project]\n'
                f'name = "{OLD_CMD}"\n'
                '[project.scripts]\n'
                f'{OLD_CMD} = "{OLD_PKG}.command:main"\n'
            ),
            'README.md': f'# {OLD_CMD}\n\nFrom `~/src/{OLD_CMD}`, run `{OLD_CMD} status`.\n',
            'docs/development.md': f'Run from `~/src/{OLD_CMD}` with `{OLD_ENV}STATE_ROOT` and `src/{OLD_PKG}`.\n',
            'infra/templates/update-check.service.j2': (
                '[Unit]\n'
                f'Description={OLD_CMD} upstream update check\n'
                '[Service]\n'
                f'Environment={OLD_ENV}ROOT={{{{ instance_root }}}}\n'
                f'ExecStart={{{{ code_root }}}}/bin/{OLD_CMD} ops update check --notify\n'
            ),
            'scripts/evalimage.py': f"LABEL = '{OLD_CMD}.dockerfile-sha256'\n",
            'tests/test_declared_settings.py': (
                f"VALID = {{'resource_prefix': '{OLD_CMD}'}}\n"
                f"INVALID = ('{OLD_PKG}',)\n"
            ),
        }
        for rel_path, content in files.items():
            target = core / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
        git(core, 'add', '-A')
        git(core, 'commit', '-q', '-m', 'initial')
        return core

    def test_refuses_a_directory_that_is_not_a_git_work_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SystemExit, 'not a Git work tree'):
                renamer.rename(Path(directory))

    def test_refuses_a_dirty_git_work_tree(self):
        core = self.synthetic_core()
        (core / 'README.md').write_text('dirty\n', encoding='utf-8')
        with self.assertRaisesRegex(SystemExit, 'uncommitted changes'):
            renamer.rename(core)

    def test_renames_package_binary_env_and_docs_and_leaves_only_survivors(self):
        core = self.synthetic_core()
        renamer.rename(core)

        self.assertFalse((core / 'src' / OLD_PKG).exists())
        self.assertTrue((core / 'src/agentiac/__init__.py').is_file())
        self.assertFalse((core / 'bin' / OLD_CMD).exists())
        self.assertTrue((core / 'bin/agentiac').is_file())

        self.assertIn('from agentiac import paths', (core / 'src/agentiac/__init__.py').read_text())
        self.assertIn("ENV = 'AGENTIAC_ROOT'", (core / 'src/agentiac/paths.py').read_text())
        self.assertIn("COMMAND = 'agentiac'", (core / 'src/agentiac/errors.py').read_text())
        self.assertIn("INVOKED = 'AGENTIAC_COMMAND'", (core / 'src/agentiac/errors.py').read_text())

        pyproject = (core / 'pyproject.toml').read_text()
        self.assertIn('name = "agentiac"', pyproject)
        self.assertIn('agentiac = "agentiac.command:main"', pyproject)

        readme = (core / 'README.md').read_text()
        self.assertTrue(readme.startswith(f'# AGENTIaC (formerly {OLD_CMD})\n'))
        self.assertIn('~/src/AGENTIaC', readme)
        self.assertIn('`agentiac status`', readme)

        dev_doc = (core / 'docs/development.md').read_text()
        self.assertIn('~/src/AGENTIaC', dev_doc)
        self.assertIn('AGENTIAC_STATE_ROOT', dev_doc)
        self.assertIn('src/agentiac', dev_doc)

        unit = (core / 'infra/templates/update-check.service.j2').read_text()
        self.assertIn('Environment=AGENTIAC_ROOT={{ instance_root }}', unit)
        self.assertIn('ExecStart={{ code_root }}/bin/agentiac ops update check --notify', unit)

        survivors, unexpected = renamer.scan_occurrences(core)
        self.assertEqual(unexpected, [])
        self.assertGreaterEqual(len(survivors), 6)


if __name__ == '__main__':
    unittest.main()
