"""pi router extension: rules, escalation-only routing, cloud gating and no failover (ADR 0002/0005)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
import agent  # noqa: E402

CONFIG = {'provider': 'local-infra', 'low': 'local-coder', 'high': 'cloud-gpt-sol', 'repo': 'allowed-repo',
          'cloudAllowed': True, 'confirm': True, 'threshold': 4, 'contextWindow': 65536, 'maxTokens': 16384}


@unittest.skipUnless(agent.NODE.exists(), 'Node 24 not installed')
class RouterTests(unittest.TestCase):
    def run_harness(self, scenario, **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'router.json'
            path.write_text(json.dumps({**CONFIG, **overrides}))
            result = subprocess.run([str(agent.NODE), str(Path(__file__).with_name('router_harness.mjs')),
                                     str(ROOT / 'agents/pi/extensions/router.ts'), str(path), scenario],
                                    capture_output=True, text=True, timeout=60, env=dict(os.environ, NODE_NO_WARNINGS='1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_rules_score_hard_easy_and_plain_messages(self):
        out = self.run_harness('rules')
        self.assertGreaterEqual(out['score']['hard']['score'], 4)
        self.assertLess(out['score']['easy']['score'], 0)
        self.assertLess(out['score']['plain']['score'], 4)
        decide = out['decide']
        self.assertEqual((decide['low']['tier'], decide['hard']['tier'], decide['hard']['ask']), ('low', 'high', True))
        self.assertEqual(decide['notAllowed']['tier'], 'low')
        self.assertEqual(decide['declined']['tier'], 'low')
        self.assertEqual((decide['sticky']['tier'], decide['sticky']['why']), ('high', 'sticky'))
        self.assertEqual((decide['locked']['tier'], decide['locked']['why']), ('low', 'manual'))
        self.assertEqual((decide['noConfirm']['tier'], decide['noConfirm']['ask']), ('high', False))

    def test_route_asks_once_stays_local_without_consent_and_never_fails_over(self):
        out = self.run_harness('route')
        self.assertEqual(out['easy']['model'], 'local-coder')
        self.assertEqual(out['approved'], {'model': 'cloud-gpt-sol', 'state': {'tier': 'high', 'cloud': 'approved'}})
        self.assertEqual(out['askedOnce'], 1)
        self.assertEqual(out['declined'], {'model': 'local-coder', 'state': {'tier': 'low', 'cloud': 'declined'}})
        self.assertEqual(out['noUi']['model'], 'local-coder', 'print mode never sends code out without --allow-cloud')
        self.assertEqual(out['continuation']['model'], 'local-coder')
        self.assertEqual(out['retry']['model'], 'cloud-gpt-sol', 'a retry stays on the failed model')
        self.assertEqual(out['direct']['model'], 'local-coder')
        self.assertEqual(out['sticky']['model'], 'cloud-gpt-sol')
        self.assertEqual(out['lockedLow']['model'], 'local-coder')
        self.assertEqual(out['unlocked']['model'], 'cloud-gpt-sol')
        self.assertEqual(out['notes'], ['info', 'info'])

    def test_repository_outside_the_allowlist_never_escalates(self):
        out = self.run_harness('route', cloudAllowed=False)
        self.assertEqual((out['approved']['model'], out['askedOnce']), ('local-coder', 0))


class RouterConfigTests(unittest.TestCase):
    def test_launcher_config_gates_cloud_by_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'agents/pi').mkdir(parents=True)
            (root / 'agents/pi/router.json').write_text(json.dumps({
                'low': 'local-coder', 'high': 'cloud-alias',
                'cloud_repos': ['allowed-repo', 'second-repo'], 'threshold': 4,
            }))
            with patch.object(agent, 'ROOT', root), \
                    patch.object(agent, 'model_entry', return_value={'contextWindow': 65536, 'maxTokens': 16384}):
                allowed = agent.router_config('allowed-repo', allow_cloud=False)
                self.assertEqual((allowed['low'], allowed['high'], allowed['cloudAllowed'], allowed['confirm']),
                                 ('local-coder', 'cloud-alias', True, True))
                self.assertFalse(agent.router_config('some-other-repo', allow_cloud=True)['cloudAllowed'])
                self.assertFalse(agent.router_config('second-repo', allow_cloud=True)['confirm'])


if __name__ == '__main__':
    unittest.main()
