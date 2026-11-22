"""What a gateway loaded is recorded only after a good restart, and a failed switch restores the route."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import route  # noqa: E402

CONFIG = ('model_list:\n- model_name: a-source\n'
          '  litellm_params: {model: openai/a, api_base: "http://127.0.0.1:1/v1", api_key: os.environ/A_API_KEY}\n')
INVENTORY = 'all:\n  vars: {backend_port: 9}\n  children:\n    workers:\n      hosts:\n        a: {tunnel_port: 1}\n'
ROUTES = {'alias': 'local-coder', 'default': 'a', 'targets': {'a': {'source_alias': 'a-source'}, 'b': {'source_alias': 'a-source', 'extra_body': {'x': 1}}}}


class Checkout(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for path, text in (('gateway/config.yaml', CONFIG), ('gateway/local-coder.json', json.dumps(ROUTES)), ('gateway/chatgpt.yaml', 'x: 1\n'),
                           ('gateway/gateway_callbacks.py', '# callbacks\n'), ('gateway/task_policy.py', '# policy\n'),
                           ('gateway/identity_policy.py', '# identity\n'), ('gateway/chatgpt_auth.py', '# auth\n'), ('requirements-gateway.lock', 'litellm==1\n'), ('secrets/gateway.env', 'KEY=1\n'),
                           ('infra/inventory.yml', INVENTORY)):
            (root / path).parent.mkdir(exist_ok=True)
            (root / path).write_text(text)
        (root / 'artifacts').mkdir()
        self.root = root
        for name, value in (('ROOT', root), ('ROUTES', root / 'gateway/local-coder.json'), ('OUT', root / 'artifacts/gateway')):
            patcher = mock.patch.object(route, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class SignatureTests(Checkout):
    def test_a_first_deploy_is_pending_and_recording_settles_it(self):
        self.assertTrue(route.pending('gateway'))
        route.record('gateway')
        self.assertFalse(route.pending('gateway'))

    def test_changing_the_target_or_a_source_is_pending_again(self):
        route.write('a')
        route.record('gateway')
        route.write('b')
        self.assertTrue(route.pending('gateway'))
        route.write('a')
        self.assertFalse(route.pending('gateway'))
        (self.root / 'secrets/gateway.env').write_text('KEY=2\n')
        self.assertTrue(route.pending('gateway'))

    def test_a_new_lock_is_pending_for_both_gateways_because_they_share_the_venv(self):
        route.write('a')
        route.record('gateway')
        route.record('chatgpt')
        (self.root / 'requirements-gateway.lock').write_text('litellm==2\n')
        self.assertTrue(route.pending('gateway'))
        self.assertTrue(route.pending('chatgpt'))

    def test_the_signature_needs_no_rendered_files(self):
        # ops deploy --check must see a pending change without writing artifacts/gateway/.
        before = route.signature('gateway')
        self.assertFalse(route.OUT.exists())
        route.write('a')
        self.assertEqual(route.signature('gateway'), before)

    def test_a_tunnel_port_change_in_the_inventory_is_pending(self):
        # nodes.json (T3.1) maps the key to the host by the inventory port, and the gateway loads it at start.
        route.write('a')
        route.record('gateway')
        (self.root / 'infra/inventory.yml').write_text(INVENTORY.replace('tunnel_port: 1', 'tunnel_port: 2'))
        self.assertTrue(route.pending('gateway'))

    def test_the_two_gateways_are_tracked_separately(self):
        route.record('gateway')
        self.assertTrue(route.pending('chatgpt'))
        route.record('chatgpt')
        (self.root / 'gateway/chatgpt.yaml').write_text('x: 2\n')
        self.assertTrue(route.pending('chatgpt'))
        self.assertFalse(route.pending('gateway'))


class SwitchTests(Checkout):
    def test_a_good_switch_records_and_logs(self):
        route.write('a')
        with mock.patch.object(route, 'restart'):
            route.switch_to('b')
        self.assertEqual(route.current(), 'b')
        self.assertFalse(route.pending('gateway'))
        self.assertEqual(json.loads((route.OUT / 'route-log.jsonl').read_text())['to'], 'b')

    def test_a_failed_restart_restores_the_route_without_recording(self):
        route.write('a')
        route.record('gateway')
        with mock.patch.object(route, 'restart', side_effect=[RuntimeError('no answer'), None]) as restart:
            with self.assertRaisesRegex(SystemExit, 'still a'):
                route.switch_to('b')
        self.assertEqual(restart.call_count, 2)
        self.assertEqual(route.current(), 'a')
        self.assertFalse(route.pending('gateway'))
        self.assertFalse((route.OUT / 'route-log.jsonl').exists())

    def test_a_gateway_that_stays_down_is_reported(self):
        route.write('a')
        failure = subprocess.CalledProcessError(1, 'systemctl')
        with mock.patch.object(route, 'restart', side_effect=[failure, failure]):
            with self.assertRaisesRegex(SystemExit, 'gateway is down'):
                route.switch_to('b')
        self.assertEqual(route.current(), 'a')


class SetLockTests(unittest.TestCase):
    """`route.py set` changes what the gateway serves, so it holds the mutation lock (F05, T2.4)."""

    def set(self, hold):
        target = json.loads(route.ROUTES.read_text())['default']
        with mock.patch.object(sys, 'argv', ['route.py', 'set', target]), \
             mock.patch.object(route, 'thin_target', return_value=None), \
             mock.patch.object(route, 'switch_to') as switch_to, \
             mock.patch.object(route.mutation, 'hold_or_exit', hold), mock.patch('builtins.print'):
            try:
                route.main()
            except SystemExit as refusal:
                return switch_to, str(refusal)
        return switch_to, None

    def test_set_holds_the_lock_around_the_switch(self):
        hold = mock.MagicMock()
        switch_to, refusal = self.set(hold)
        self.assertIsNone(refusal)
        hold.assert_called_once()
        self.assertEqual(hold.call_args.args[0], route.ROOT)
        self.assertTrue(hold.call_args.args[1].startswith('route '))
        hold.return_value.__enter__.assert_called_once()
        switch_to.assert_called_once()

    def test_a_busy_lock_stops_set_before_the_switch(self):
        switch_to, refusal = self.set(mock.MagicMock(side_effect=SystemExit('busy')))
        self.assertEqual(refusal, 'busy')
        switch_to.assert_not_called()


class SourceListTests(unittest.TestCase):
    def test_every_gateway_module_the_callbacks_import_makes_the_gateway_pending(self):
        """A policy module edited without this list is a running gateway that drift calls current."""
        root = Path(route.__file__).resolve().parents[1]
        tree = ast.parse((root / 'gateway/gateway_callbacks.py').read_text())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        local = {f'gateway/{name}.py' for name in imported if (root / 'gateway' / f'{name}.py').exists()}
        self.assertTrue(local)
        for gateway in route.SOURCES:
            self.assertLessEqual(local, set(route.SOURCES[gateway]), gateway)


if __name__ == '__main__':
    unittest.main()
