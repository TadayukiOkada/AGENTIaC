"""IP-01 (T4.3): the task key is low-privilege and its aliases are pinned.

The suite and the smoke run a model's own code in a container with host networking,
so the credential they carry must not be the master key and must not reach every
alias (docs/improvement-proposals.md IP-01: measured from inside such a container,
the master key authenticated for all aliases including the subscription alias, bypassing
the ADR 0005 cloud_repos limit).

gateway/task_policy.py holds the policy and is pure stdlib, so it is tested here
directly. gateway_callbacks.py wraps it in litellm types and cannot be imported from
the control venv (litellm lives in .venv-gateway), so its wiring is read out of the
source the way test_gateway_aliases.py reads the alias tables.
"""
import ast
import json
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'gateway'))
from agentiac import paths  # noqa: E402
SITE = paths.root()
import task_policy  # noqa: E402
import route  # noqa: E402

CALLBACKS = ROOT / 'gateway/gateway_callbacks.py'
TASK_KEY = 'sk-' + 'a' * 64
MASTER_KEY = 'sk-' + 'b' * 64


class PolicyTests(unittest.TestCase):
    def test_only_the_exact_task_key_authenticates(self):
        self.assertTrue(task_policy.is_task_key(TASK_KEY, TASK_KEY))
        self.assertFalse(task_policy.is_task_key(MASTER_KEY, TASK_KEY))
        self.assertFalse(task_policy.is_task_key(None, TASK_KEY))
        self.assertFalse(task_policy.is_task_key('', TASK_KEY))
        self.assertFalse(task_policy.is_task_key(TASK_KEY, None))  # unconfigured: nothing authenticates
        self.assertFalse(task_policy.is_task_key(TASK_KEY, ''))

    def test_the_task_key_is_limited_to_inference_routes(self):
        for path in ('/v1/chat/completions', '/v1/responses', '/v1/messages', '/v1/completions'):
            self.assertTrue(task_policy.route_allowed(path), path)
        for path in ('/v1/models', '/key/generate', '/model/new', '/', '/health', '/v1/chat/completions/'):
            self.assertFalse(task_policy.route_allowed(path), path)

    def test_the_alias_list_is_pinned_not_a_wildcard(self):
        allowed = {'local-coder', 'local-coder-worker-a'}
        self.assertTrue(task_policy.model_allowed('local-coder-worker-a', allowed))
        for model in ('cloud-alias', 'worker-a-worker', 'control-small', None, 'local-coder-worker-a ', '*'):
            self.assertFalse(task_policy.model_allowed(model, allowed), model)

    def test_a_code_key_names_its_own_user_and_nothing_else(self):
        code, cloud = 'sk-' + 'c' * 64, 'sk-' + 'd' * 64
        keys = ((task_policy.CODE_USER, code), (task_policy.CODE_CLOUD_USER, cloud))
        self.assertEqual(task_policy.key_user(code, keys), task_policy.CODE_USER)
        self.assertEqual(task_policy.key_user(cloud, keys), task_policy.CODE_CLOUD_USER)
        for presented in (MASTER_KEY, TASK_KEY, None, ''):
            self.assertIsNone(task_policy.key_user(presented, keys), presented)
        # an unconfigured key authenticates nothing, and never falls through to another user's key
        for configured in (None, ''):
            unset = ((task_policy.CODE_USER, configured), (task_policy.CODE_CLOUD_USER, cloud))
            self.assertIsNone(task_policy.key_user(code, unset))
            self.assertIsNone(task_policy.key_user(configured, unset))
            self.assertEqual(task_policy.key_user(cloud, unset), task_policy.CODE_CLOUD_USER)

    def test_only_the_code_cloud_key_reaches_the_subscription_alias(self):
        self.assertEqual(task_policy.code_aliases(task_policy.CODE_USER, {'a'}, {'cloud'}), {'a'})
        self.assertEqual(task_policy.code_aliases(task_policy.CODE_CLOUD_USER, {'a'}, {'cloud'}), {'a', 'cloud'})


class WiringTests(unittest.TestCase):
    """The policy is only real if the proxy loads it and the containers carry the new key."""

    def config(self):
        return yaml.safe_load((SITE / 'gateway/config.yaml').read_text())

    def test_the_proxy_authenticates_the_task_key_through_the_callbacks(self):
        general = self.config().get('general_settings')
        if general is not None:
            self.assertEqual(general['custom_auth'], 'gateway_callbacks.custom_auth')
            self.assertTrue(general['custom_auth_run_common_checks'])  # litellm's own key-model check on top
            self.assertEqual(general['master_key'], 'os.environ/LITELLM_MASTER_KEY')  # unchanged

    def test_the_callbacks_define_custom_auth_and_enforce_the_alias_list_before_the_cloud_check(self):
        tree = ast.parse(CALLBACKS.read_text())
        functions = {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))}
        self.assertIn('custom_auth', functions)
        source = CALLBACKS.read_text()
        for needle in ('task_policy.is_task_key(api_key, TASK_KEY)',      # the only key intercepted
                       'return None',                                      # everything else: litellm's own auth
                       'task_policy.route_allowed(request.url.path)',     # inference routes only
                       'task_policy.TASK_USER',                           # the identity the hook recognises
                       'task_policy.model_allowed(data.get(\'model\'), TASK_ALIASES)',
                       "os.environ.get('LITELLM_TASK_KEY')"):
            self.assertIn(needle, source)
        # The alias refusal must precede the ChatGPT sign-in check, or a task container
        # could still probe the subscription alias (the ADR 0005 bypass IP-01 measured).
        hook = ast.unparse(functions['async_pre_call_hook'])
        self.assertLess(hook.index('model_allowed'), hook.index('SUBSCRIPTION'))

    def test_the_callbacks_authenticate_the_two_code_keys(self):
        source = CALLBACKS.read_text()
        for needle in ("os.environ.get('LITELLM_CODE_KEY')", "os.environ.get('LITELLM_CODE_CLOUD_KEY')",
                       'task_policy.CODE_USERS'):
            self.assertIn(needle, source)

    def test_the_task_aliases_are_the_rendered_local_coder_set(self):
        rendered = json.loads(route.rendered(route.current())['routes.json'])
        self.assertTrue(rendered)
        declared_routes = json.loads((SITE / 'gateway/local-coder.json').read_text())
        # Version 2: the selector, each target key, and local-coder-<retired key> for every retired key.
        expected = {'local-coder'} | set(declared_routes['targets'])
        expected |= {'local-coder-' + old for old in declared_routes.get('legacy_targets', {})}
        expected |= set(declared_routes.get('short_aliases', {}))
        self.assertEqual(set(rendered), expected)
        subscription = {m['model_name'] for m in self.config()['model_list']
                        if m['litellm_params']['model'].startswith('litellm_proxy/')}
        self.assertTrue(subscription)
        self.assertFalse(subscription & set(rendered))
        catalog = {m['model_name'] for m in self.config()['model_list']}
        self.assertTrue(rendered.keys() <= catalog | rendered.keys())

    def test_bootstrap_generates_and_rotates_the_task_key(self):
        source = (ROOT / 'scripts/bootstrap-secrets.py').read_text()
        self.assertIn("values.setdefault('LITELLM_TASK_KEY'", source)
        rotate = next(node for node in ast.walk(ast.parse(source))
                      if isinstance(node, ast.If) and "--rotate' in sys.argv" in ast.unparse(node.test))
        self.assertIn('LITELLM_TASK_KEY', ast.unparse(rotate))

    def test_bootstrap_generates_and_rotates_the_code_keys(self):
        source = (ROOT / 'scripts/bootstrap-secrets.py').read_text()
        rotate = ast.unparse(next(node for node in ast.walk(ast.parse(source))
                                  if isinstance(node, ast.If) and "--rotate' in sys.argv" in ast.unparse(node.test)))
        for name in ('LITELLM_CODE_KEY', 'LITELLM_CODE_CLOUD_KEY'):
            self.assertIn(f"values.setdefault('{name}'", source)
            self.assertIn(f"'{name}'", rotate)

    def test_the_task_containers_carry_the_task_key_and_never_the_master_key(self):
        # Every script that runs a model's code in a container: the suite and the smoke. The
        # per-frontend task runners were archived (D8.1); tests/test_archived_task_keys.py checks them.
        for name in ('suite.py', 'smoke.py'):
            source = (ROOT / 'scripts' / name).read_text()
            self.assertNotIn('LITELLM_MASTER_KEY', source, f'{name} still carries the master key')
            self.assertIn("'LITELLM_TASK_KEY'", source)
        for name in ('smoke.py',):
            self.assertIn('LITELLM_TASK_KEY=key', (ROOT / 'scripts' / name).read_text())
        for name in ('smoke.py',):
            self.assertIn("'${LITELLM_TASK_KEY}'", (ROOT / 'scripts' / name).read_text())
        # The suite hands its container command to the pi adapter (T12.1), so the key is handed over
        # there: check the pair, or the guarantee has only moved.
        pair = (ROOT / 'scripts/suite.py').read_text() + (ROOT / 'src/agentiac/adapters.py').read_text()
        self.assertNotIn('LITELLM_MASTER_KEY', pair, 'the adapter carries the master key')
        self.assertIn("key_env='LITELLM_TASK_KEY'", pair)
        self.assertIn("'${LITELLM_TASK_KEY}'", pair)

    def test_the_suite_reads_the_task_key_from_the_gateway_secret_file(self):
        tree = ast.parse((ROOT / 'scripts/suite.py').read_text())
        runs = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, 'id', None) == 'read_key' and len(n.args) == 2]
        self.assertEqual([ast.unparse(a) for a in runs[0].args],
                         ["ROOT / 'secrets/gateway.env'", "'LITELLM_TASK_KEY'"])


class ProviderOverrideTests(unittest.TestCase):
    """An external --provider-config keeps its own credential; only the local path is repinned."""

    def test_local_provider_uses_the_task_key(self):
        import suite as suite_script
        config = suite_script.provider_config({'model': {'id': 'local-coder'}})
        self.assertEqual(config['providers']['local-infra']['apiKey'], '${LITELLM_TASK_KEY}')
        self.assertEqual(config['providers']['local-infra']['models'], [{'id': 'local-coder'}])

    def test_an_explicit_provider_keeps_its_own_api_key(self):
        import suite as suite_script
        provider = {'baseUrl': 'https://example.invalid/v1', 'apiKey': 'sk-something-else'}
        config = suite_script.provider_config({'model': {'id': 'm'}, 'provider': provider})
        self.assertEqual(config['providers']['local-infra']['apiKey'], 'sk-something-else')


class NonAsciiCredentialTests(unittest.TestCase):
    def test_a_non_ascii_credential_is_a_mismatch_and_not_an_exception(self):
        self.assertFalse(task_policy.is_task_key('pässwörd', 'task-key'))
        self.assertTrue(task_policy.is_task_key('pässwörd', 'pässwörd'))


class SmokeReachTests(unittest.TestCase):
    def test_the_task_key_reaches_the_rendered_aliases_only(self):
        import smoke
        routes = json.loads((SITE / 'gateway/local-coder.json').read_text())
        transport = next(m['model_name'] for m in yaml.safe_load((SITE / 'gateway/config.yaml').read_text())['model_list']
                         if m['litellm_params']['model'].startswith('openai/'))
        self.assertTrue(smoke.task_key_reaches(routes['alias']))
        for target in routes['targets']:
            self.assertTrue(smoke.task_key_reaches(target), target)
        for old in routes['legacy_targets']:
            self.assertTrue(smoke.task_key_reaches(f"{routes['alias']}-{old}"), old)
        self.assertFalse(smoke.task_key_reaches(transport))
        self.assertFalse(smoke.task_key_reaches(f"{routes['alias']}-nowhere"))


if __name__ == '__main__':
    unittest.main()
