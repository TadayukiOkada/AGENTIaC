"""D8.2: site-wide settings come from `all.vars` of the inventory, with no core default (class A, synthetic sites).

`declared.setting`, `account` and `gateway_url` are how the core reads them; `declared.setting_problems` is the check
`declared.references` runs, so `make check`, the configuration API and the dashboard deployment see a missing key.
"""
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agentiac import declared  # noqa: E402

VALID = {'ansible_user': 'operator', 'nodejs_binary': '/opt/node/bin/node', 'rclone_config_dir': '/srv/operator/rclone',
         'backup_destination': 'remote:archives/control', 'gateway_port': 24000, 'chatgpt_port': 24001}


class SiteSettingTests(unittest.TestCase):
    def site(self, group_vars):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / 'infra').mkdir()
        inventory = {'all': {'vars': group_vars, 'children': {'workers': {'hosts': {'alpha': {}}}}}}
        (root / 'infra/inventory.yml').write_text(yaml.safe_dump(inventory))
        return root

    def test_a_declared_setting_is_read_from_all_vars(self):
        root = self.site(VALID)
        self.assertEqual(declared.setting('backup_destination', root), 'remote:archives/control')
        self.assertEqual(declared.setting('gateway_port', root), 24000)

    def test_a_missing_setting_exits_naming_the_key_and_the_file(self):
        root = self.site({key: value for key, value in VALID.items() if key != 'nodejs_binary'})
        with self.assertRaises(SystemExit) as raised:
            declared.setting('nodejs_binary', root)
        first = str(raised.exception).split('\n')[0]
        self.assertEqual(first, 'nodejs_binary is not declared in infra/inventory.yml')
        self.assertIn('D8.2', str(raised.exception))

    def test_only_the_resource_prefix_has_a_core_default(self):
        root = self.site(VALID)
        self.assertEqual(declared.setting('resource_prefix', root), 'agentiac')
        self.assertEqual(declared.setting('resource_prefix', self.site({**VALID, 'resource_prefix': 'site-a'})), 'site-a')
        self.assertEqual(set(declared.DEFAULTS), {'resource_prefix'})

    def test_the_account_is_the_hosts_own_then_the_group_value(self):
        root = self.site(VALID)
        self.assertEqual(declared.account({}, root), 'operator')
        self.assertEqual(declared.account({'ansible_user': 'builder'}, root), 'builder')

    def test_the_gateway_url_is_loopback_on_the_declared_port(self):
        root = self.site(VALID)
        self.assertEqual(declared.gateway_url(root), 'http://127.0.0.1:24000')
        self.assertEqual(declared.gateway_url(root, '/v1'), 'http://127.0.0.1:24000/v1')


class SettingProblemTests(unittest.TestCase):
    def test_a_complete_declaration_has_no_problem(self):
        self.assertEqual(declared.setting_problems(VALID), [])
        self.assertEqual(declared.setting_problems({**VALID, 'resource_prefix': 'agent-infra'}), [])

    def test_each_missing_key_is_one_problem(self):
        problems = declared.setting_problems({})
        self.assertEqual(len(problems), len(declared.SETTINGS))
        for key in declared.SETTINGS:
            self.assertTrue(any(f'all.vars: {key} (' in problem for problem in problems), key)

    def test_malformed_values_are_problems(self):
        cases = {
            'gateway_port': ('14000', 0, 70000, True),
            'nodejs_binary': ('node', 7),
            'rclone_config_dir': ('relative/rclone',),
            'backup_destination': (['a', 'b'],),
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    problems = declared.setting_problems({**VALID, key: value})
                    self.assertEqual(len(problems), 1, problems)
                    self.assertIn(key, problems[0])

    def test_the_two_gateways_cannot_share_a_port(self):
        problems = declared.setting_problems({**VALID, 'chatgpt_port': VALID['gateway_port']})
        self.assertEqual(problems, ['infra/inventory.yml all.vars: gateway_port and chatgpt_port are both 24000'])

    def test_the_resource_prefix_is_a_lower_case_name(self):
        for prefix in ('Agent', 'agent_infra', '-x', 'x-', '9x', ''):
            with self.subTest(prefix=prefix):
                self.assertEqual(len(declared.setting_problems({**VALID, 'resource_prefix': prefix})), 1)


if __name__ == '__main__':
    unittest.main()
