"""ChatGPT sign-in gate of the subscription alias: refuse instead of an in-gateway device-code login."""
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'gateway'))
from agentiac import declared, paths  # noqa: E402
SITE = paths.root()
import chatgpt_auth  # noqa: E402


class FakeAuth:
    def __init__(self, data, expired=False, refresh_fails=False):
        self.data, self.expired, self.refresh_fails, self.refreshed = data, expired, refresh_fails, 0

    def _read_auth_file(self):
        return self.data

    def _is_token_expired(self, data, access_token):
        return self.expired

    def _refresh_tokens(self, refresh_token):
        self.refreshed += 1
        if self.refresh_fails:
            raise RuntimeError('Refresh response missing fields: {"access_token": "secret-token"}')
        return {'access_token': 'new'}


class SigninTests(unittest.TestCase):
    def test_missing_or_incomplete_token_file_asks_for_login(self):
        for data in (None, {}, {'access_token': 'a'}):
            self.assertEqual(chatgpt_auth.signin_problem(FakeAuth(data)), chatgpt_auth.NOT_SIGNED_IN)

    def test_valid_token_passes_without_refresh(self):
        auth = FakeAuth({'access_token': 'a', 'refresh_token': 'r'})
        self.assertIsNone(chatgpt_auth.signin_problem(auth))
        self.assertEqual(auth.refreshed, 0)

    def test_expired_token_is_refreshed(self):
        auth = FakeAuth({'access_token': 'a', 'refresh_token': 'r'}, expired=True)
        self.assertIsNone(chatgpt_auth.signin_problem(auth))
        self.assertEqual(auth.refreshed, 1)

    def test_without_refresh_a_stored_refresh_token_is_enough(self):
        auth = FakeAuth({'access_token': 'a', 'refresh_token': 'r'}, expired=True, refresh_fails=True)
        self.assertIsNone(chatgpt_auth.signin_problem(auth, refresh=False))
        self.assertEqual(auth.refreshed, 0)
        self.assertEqual(chatgpt_auth.signin_problem(FakeAuth({}), refresh=False), chatgpt_auth.NOT_SIGNED_IN)

    def test_failed_refresh_reports_without_token_text(self):
        auth = FakeAuth({'access_token': 'a', 'refresh_token': 'r'}, expired=True, refresh_fails=True)
        problem = chatgpt_auth.signin_problem(auth)
        self.assertEqual(problem, chatgpt_auth.EXPIRED)
        self.assertNotIn('secret', problem)


class ConfigTests(unittest.TestCase):
    def test_main_gateway_never_resolves_chatgpt_itself(self):
        # LiteLLM fetches a ChatGPT token when it loads a chatgpt/ deployment, so one
        # in the main config would make the local aliases depend on the sign-in.
        config = yaml.safe_load((SITE / 'gateway/config.yaml').read_text())
        for model in config['model_list']:
            self.assertTrue(model['litellm_params']['api_base'].startswith('http://127.0.0.1:'), model['model_name'])
        subs = [m for m in config['model_list'] if m['litellm_params']['model'].startswith('litellm_proxy/')]
        self.assertTrue(subs)
        routes = yaml.safe_load((SITE / 'gateway/local-coder.json').read_text())
        source_aliases = [t['source_alias'] for t in routes['targets'].values()]
        chatgpt_port = declared.setting('chatgpt_port')
        for cloud in subs:
            self.assertEqual(cloud['litellm_params'], {'model': f"litellm_proxy/{cloud['model_name']}",
                                                      'api_base': f'http://127.0.0.1:{chatgpt_port}',
                                                      'api_key': 'os.environ/LITELLM_MASTER_KEY'})
            if 'model_info' in cloud:
                self.assertEqual(cloud['model_info']['cooldown_time'], 0)
            self.assertNotIn(cloud['model_name'], source_aliases)
        if 'router_settings' in config:
            self.assertEqual(config['router_settings']['fallbacks'], [])

    def test_chatgpt_process_serves_only_the_subscription_alias(self):
        chatgpt_path = SITE / 'gateway/chatgpt.yaml'
        if not chatgpt_path.exists():
            return
        gateway = yaml.safe_load((SITE / 'gateway/config.yaml').read_text())
        sub_names = {m['model_name'] for m in gateway['model_list']
                     if m['litellm_params']['model'].startswith('litellm_proxy/')}
        config = yaml.safe_load(chatgpt_path.read_text())
        self.assertEqual(len(config['model_list']), len(sub_names))
        for model in config['model_list']:
            self.assertIn(model['model_name'], sub_names)
            self.assertTrue(model['litellm_params']['model'].startswith('chatgpt/'))
            self.assertNotIn('api_key', model['litellm_params'], 'subscription sign-in, not an API key')
            self.assertEqual(model['model_info']['mode'], 'responses')
        self.assertEqual(config['router_settings']['fallbacks'], [])
        self.assertTrue(config['litellm_settings']['turn_off_message_logging'])


if __name__ == '__main__':
    unittest.main()
