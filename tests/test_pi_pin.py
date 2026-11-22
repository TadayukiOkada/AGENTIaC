"""T8.6: pi is installed from tracked files, at one exact version (PA F4)."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '@earendil-works/pi-coding-agent'


class PiPin(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT / 'agents/pi/package.json').read_text())
        self.lock = json.loads((ROOT / 'agents/pi/package-lock.json').read_text())

    def test_the_manifest_names_one_exact_version(self):
        self.assertRegex(self.manifest['dependencies'][PACKAGE], re.compile(r'^\d+\.\d+\.\d+$'))

    def test_the_lock_was_made_from_this_manifest(self):
        self.assertEqual(self.lock['packages']['']['dependencies'], self.manifest['dependencies'])

    def test_the_lock_installs_the_version_the_manifest_names(self):
        self.assertEqual(self.lock['packages'][f'node_modules/{PACKAGE}']['version'],
                         self.manifest['dependencies'][PACKAGE])


if __name__ == '__main__':
    unittest.main()
