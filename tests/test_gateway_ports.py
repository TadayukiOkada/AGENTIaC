"""D8.2 contract (class B): the site files that repeat a gateway port agree with the declared one.

`gateway_port` and `chatgpt_port` are declared once, in `all.vars` of the inventory; the unit templates and the core's
clients read them from there. Two site files still carry the literal, because their formats have no way to refer to
the inventory: the subscription deployment of `gateway/config.yaml` and pi's provider in `agents/pi/models.json`.
"""
import json
from pathlib import Path
import sys
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agentiac import declared, paths  # noqa: E402

SITE = paths.root()


class GatewayPortContractTests(unittest.TestCase):
    def test_the_subscription_deployments_reach_the_declared_chatgpt_port(self):
        wanted = f'http://{declared.GATEWAY_HOST}:{declared.setting("chatgpt_port", SITE)}'
        config = yaml.safe_load((SITE / 'gateway/config.yaml').read_text())
        proxied = [m for m in config['model_list'] if m['litellm_params']['model'].startswith('litellm_proxy/')]
        for model in proxied:
            with self.subTest(model=model['model_name']):
                self.assertEqual(model['litellm_params']['api_base'], wanted)

    def test_pi_reaches_the_gateway_on_the_declared_port(self):
        models = json.loads((SITE / 'agents/pi/models.json').read_text())
        self.assertEqual(models['providers']['local-infra']['baseUrl'], declared.gateway_url(SITE, '/v1'))


if __name__ == '__main__':
    unittest.main()
