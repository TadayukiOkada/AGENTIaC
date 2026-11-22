import importlib.util
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import paths  # noqa: E402
SITE = paths.root()
spec = importlib.util.spec_from_file_location('status_script', ROOT / 'scripts/status.py')
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class StatusScriptTest(unittest.TestCase):
    def test_local_host_runs_without_ssh(self):
        argv = status.argv({'ansible_connection': 'local', 'ansible_host': 'localhost'})
        self.assertEqual(argv[:2], ['bash', '-c'])
        self.assertNotIn('ssh', argv)

    def test_remote_host_uses_batch_ssh(self):
        user = yaml.safe_load((SITE / 'infra/inventory.yml').read_text())['all']['vars']['ansible_user']
        argv = status.argv({'ansible_host': 'worker-a.example'})
        self.assertEqual(argv[0], 'ssh')
        self.assertIn('BatchMode=yes', argv)
        self.assertEqual(argv[-2], f'{user}@worker-a.example')

    def test_declared_local_hosts_are_not_reached_over_ssh(self):
        inventory = yaml.safe_load((SITE / 'infra/inventory.yml').read_text())
        hosts = inventory['all']['children']['workers']['hosts']
        local = [name for name, host in hosts.items() if host.get('ansible_connection') == 'local']
        self.assertTrue(local)
        for name in local:
            self.assertNotEqual(status.argv(hosts[name])[0], 'ssh', name)


if __name__ == '__main__':
    unittest.main()
