"""The gateway's alias tables agree with the declarations they are generated from (IP-03, IP-04, D3.1).

`gateway/config.yaml` is what LiteLLM routes with; `ROUTES` is what the callbacks' pre-call identity check verifies,
`NODE_KEY` says which API key belongs to which port, and `SUBSCRIPTION` which aliases need the ChatGPT sign-in
instead. Those three were once hand-written literals in `gateway_callbacks.py`, which is how an alias sat advertised
in the catalog while every request to it was refused with `Unknown local model alias`. Since M8 (T8.6) the callbacks
hold no table of their own: `scripts/route.py` renders them into `artifacts/gateway/nodes.json`, and these tests read
that rendering.

The fourth place is `infra/inventory.yml`, which holds the same ports and alias names as host facts
(`tunnel_port`, `strata_tunnel_port`, `backend_port`, `gateway_alias`, `default_profile`). IP-04's remaining
half: a host added to the inventory alone, or removed from it, is now a `make check` failure rather than a
404 or a wrong credential at the first request.
"""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
from agentiac import paths  # noqa: E402
SITE = paths.root()
import route  # noqa: E402

CALLBACKS = ROOT / 'gateway/gateway_callbacks.py'


CONFIG = yaml.safe_load((SITE / 'gateway/config.yaml').read_text())
TABLES = route.node_tables(CONFIG, route.inventory_hosts())
ROUTES = {alias: tuple(entry) for alias, entry in TABLES['routes'].items()}
NODE_KEY = TABLES['node_key']
SUBSCRIPTION = set(TABLES['subscription'])
ALIASES = {entry['model_name']: entry['litellm_params'] for entry in CONFIG['model_list']}
TARGETS = json.loads((SITE / 'gateway/local-coder.json').read_text())['targets']
HOSTS = yaml.safe_load((SITE / 'infra/inventory.yml').read_text())['all']['children']['workers']['hosts']


def key_part(host):
    return host.upper().replace('-', '_')


def host_ports(host):
    """The control-side ports one inventory host publishes, by what is behind each: llama.cpp front, Strata
    front, or (control only, no tunnel) the backend's own port. The key name the gateway must use for it."""
    name = key_part(host)
    return {':' + str(HOSTS[host][key]): value for key, value in
            (('tunnel_port', name), ('strata_tunnel_port', 'STRATA_' + name),
             ('backend_port', name)) if HOSTS[host].get(key)}


def worker(alias):
    """Whether the pre-call check has to know this alias: a ChatGPT alias has no worker to verify."""
    return alias not in SUBSCRIPTION and ALIASES[alias]['model'].startswith('openai/')


def port(base):
    return ':' + base.removesuffix('/v1').rsplit(':', 1)[1]


class AliasTable(unittest.TestCase):
    def test_every_local_alias_is_verified_before_the_call(self):
        self.assertEqual(sorted(alias for alias in ALIASES if worker(alias) and alias not in ROUTES), [])

    def test_the_check_verifies_the_same_base_and_id_the_config_routes_to(self):
        for alias in (alias for alias in ALIASES if worker(alias)):
            with self.subTest(alias=alias):
                self.assertEqual(ROUTES[alias][0], ALIASES[alias]['api_base'].removesuffix('/v1'))
                self.assertEqual(ROUTES[alias][1], ALIASES[alias]['model'].removeprefix('openai/'))

    def test_the_check_knows_no_alias_the_gateway_does_not_advertise(self):
        # ROUTES also gains the rendered local-coder aliases at start, so those count as advertised too.
        rendered = set(json.loads(route.rendered(route.current())['routes.json']))
        self.assertEqual(sorted(alias for alias in ROUTES if alias not in ALIASES and alias not in rendered), [])

    def test_the_subscription_alias_is_the_one_with_no_worker_to_verify(self):
        self.assertTrue(SUBSCRIPTION)
        self.assertFalse(any(worker(alias) for alias in SUBSCRIPTION))


class Keys(unittest.TestCase):
    def test_the_key_of_a_port_is_the_one_the_config_uses_for_it(self):
        for alias in (alias for alias in ALIASES if worker(alias)):
            with self.subTest(alias=alias):
                self.assertEqual(NODE_KEY[port(ALIASES[alias]['api_base'])],
                                 ALIASES[alias]['api_key'].removeprefix('os.environ/').removesuffix('_API_KEY'))

    def test_no_port_the_rendered_routes_use_is_unknown(self):
        # A new host is exactly when an unknown port would appear, so every target is checked, not just the current.
        for target in TARGETS:
            for alias, (base, _served, *_identity) in json.loads(route.rendered(target)['routes.json']).items():
                with self.subTest(target=target, alias=alias):
                    self.assertIn(port(base), NODE_KEY)

    def test_the_key_lookup_has_no_default(self):
        # `.get(base[-6:], 'SPARK')` answered an unknown port with a wrong credential (IP-03 problem 2).
        self.assertNotIn('.get(base[-6:], ', CALLBACKS.read_text())


class Inventory(unittest.TestCase):
    """IP-04: the inventory is the fourth copy of these facts, so it is compared rather than trusted."""

    def test_every_host_alias_is_advertised_verified_and_keyed_on_the_inventory_port(self):
        for host, facts in HOSTS.items():
            alias = facts.get('gateway_alias')
            if not alias:
                continue
            with self.subTest(host=host, alias=alias):
                self.assertIn(alias, ALIASES)
                self.assertIn(alias, ROUTES)
                published_port = ':' + str(facts.get('tunnel_port') or facts['backend_port'])
                self.assertEqual(ROUTES[alias][0], 'http://127.0.0.1' + published_port)
                self.assertEqual(port(ALIASES[alias]['api_base']), published_port)
                self.assertEqual(NODE_KEY[published_port], key_part(host))

    def test_a_strata_alias_uses_the_tunnel_of_the_host_that_names_it(self):
        strata = {host: facts for host, facts in HOSTS.items() if facts.get('strata_tunnel_port')}
        self.assertTrue(strata)
        for host, facts in strata.items():
            strata_port = ':' + str(facts['strata_tunnel_port'])
            # naming stage 3: a Strata host's transports are <host>-worker-<model> (node-first)
            named = [alias for alias in ALIASES if alias.startswith(f'{host}-worker-')]
            with self.subTest(host=host):
                self.assertTrue(named)  # a prepared Strata host with no alias is unreachable by name
                for alias in named:
                    self.assertEqual(port(ALIASES[alias]['api_base']), strata_port)
                    self.assertEqual(ROUTES[alias][0], 'http://127.0.0.1' + strata_port)
                    self.assertEqual(NODE_KEY[strata_port], 'STRATA_' + key_part(host))

    def test_the_served_id_of_a_host_alias_is_the_inventory_default_profile(self):
        for host, facts in HOSTS.items():
            alias, profile = facts.get('gateway_alias'), facts.get('default_profile')
            if not alias or not profile:
                continue  # a pinned backend names its served id in gateway/config.yaml alone
            if 'strata_backend' in facts:  # the front serves that manifest, under the manifest's served id
                profile = json.loads((SITE / f"backends/{facts['strata_backend']}.json").read_text())['served_model']
            with self.subTest(host=host, alias=alias):
                self.assertEqual(ALIASES[alias]['model'], 'openai/' + profile)
                self.assertEqual(ROUTES[alias][1], profile)

    def test_no_port_the_gateway_uses_belongs_to_no_inventory_host(self):
        published = {port: name for host in HOSTS for port, name in host_ports(host).items()}
        used = {port(base): alias for alias, (base, _served, *_identity) in ROUTES.items() if alias in ALIASES}
        for target in TARGETS:  # a host is exactly when an unowned port would appear
            used.update({port(base): alias
                         for alias, (base, _served, *_identity) in json.loads(route.rendered(target)['routes.json']).items()})
        for owned, alias in sorted(used.items()):
            with self.subTest(port=owned, alias=alias):
                self.assertIn(owned, published)
                self.assertEqual(NODE_KEY[owned], published[owned])

    def test_every_route_target_names_an_inventory_host_and_an_advertised_alias(self):
        for target, facts in TARGETS.items():
            with self.subTest(target=target):
                self.assertIn(facts['node'], HOSTS)
                self.assertIn(facts['source_alias'], ALIASES)
                self.assertIn(facts['source_alias'], ROUTES)


class GeneratedTables(unittest.TestCase):
    """D3.1, M8 (T8.6): nodes.json is the callbacks' only source, so it has to be complete on its own."""

    def test_the_rendered_file_holds_the_same_tables(self):
        self.assertEqual(json.loads(route.rendered(route.current())['nodes.json']), TABLES)

    def test_every_key_a_port_uses_belongs_to_an_inventory_host(self):
        # NODE_BY_PORT is built from both maps at import; a key no host carries would fail the gateway's start.
        self.assertEqual(set(TABLES['node_key'].values()), set(TABLES['host_by_key']))

    def test_the_subscription_aliases_are_the_ones_forwarded_to_the_subscription_gateway(self):
        forwarded = {alias for alias, params in ALIASES.items() if params['model'].startswith('litellm_proxy/')}
        self.assertEqual(SUBSCRIPTION, forwarded)

    def test_the_callbacks_hold_no_table_of_their_own(self):
        # A literal here would be a second copy of the declarations, checked against nothing (the M3 fallback).
        for node in ast.parse(CALLBACKS.read_text()).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Dict, ast.Set)):
                names = [getattr(target, 'id', None) for target in node.targets]
                with self.subTest(names=names):
                    self.assertFalse(node.value.keys if isinstance(node.value, ast.Dict) else node.value.elts)


class GeneratedTablesLoaded(unittest.TestCase):
    """T3.2, T8.6: a gateway started with AGENTIAC_NODES checks with the file's tables, and without it with none.

    This executes the module (litellm stubbed as test_gateway_log does) so the environment variable takes effect.
    """

    NAMES = ('ROUTES', 'NODE_KEY', 'HOST_BY_KEY', 'NODE_BY_PORT', 'SUBSCRIPTION')

    def load(self, **env):
        from test_gateway_log import load_callbacks
        clean = {name: value for name, value in os.environ.items()
                 if name not in ('AGENTIAC_NODES', 'AGENTIAC_ROUTES')}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
            module = load_callbacks()
        return {name: getattr(module, name) for name in self.NAMES}

    def test_the_loaded_tables_are_the_generated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            nodes = Path(directory) / 'nodes.json'
            nodes.write_text(route.rendered(route.current())['nodes.json'])
            loaded = self.load(AGENTIAC_NODES=str(nodes))
        self.assertEqual(loaded['NODE_KEY'], NODE_KEY)
        self.assertEqual(loaded['HOST_BY_KEY'], TABLES['host_by_key'])
        self.assertEqual(loaded['SUBSCRIPTION'], SUBSCRIPTION)
        # ROUTES also carries the retired transports the callbacks add from local-coder.json
        self.assertEqual({alias: entry for alias, entry in loaded['ROUTES'].items() if alias in ALIASES}, ROUTES)

    def test_a_missing_file_leaves_every_table_empty_and_says_so(self):
        with self.assertLogs('agentiac_metadata', 'ERROR') as logged:
            loaded = self.load(AGENTIAC_NODES='/nonexistent/nodes.json')
        self.assertEqual(loaded, {'ROUTES': {}, 'NODE_KEY': {}, 'HOST_BY_KEY': {}, 'NODE_BY_PORT': {},
                                  'SUBSCRIPTION': set()})
        self.assertIn('nodes_missing', logged.output[0])


class InstanceRoot(unittest.TestCase):
    """T7.3: the callbacks read gateway/local-coder.json from the instance root, not from beside their own file.

    A separate process, as the gateway is: PYTHONPATH is gateway/ and nothing of agentiac is importable. litellm
    is replaced by the same empty base class test_gateway_log uses, because the control environment has none.
    """

    PROGRAM = (
        "import sys, types\n"
        "litellm = types.ModuleType('litellm')\n"
        "litellm.integrations = types.ModuleType('litellm.integrations')\n"
        "litellm.integrations.custom_logger = types.ModuleType('litellm.integrations.custom_logger')\n"
        "litellm.integrations.custom_logger.CustomLogger = object\n"
        "sys.modules['litellm'] = litellm\n"
        "sys.modules['litellm.integrations'] = litellm.integrations\n"
        "sys.modules['litellm.integrations.custom_logger'] = litellm.integrations.custom_logger\n"
        "import gateway_callbacks\n"
        "print('m7-instance-probe' in gateway_callbacks.RETIRED_TRANSPORTS)\n")

    def read(self, gateway=None, **env):
        clean = {name: value for name, value in os.environ.items()
                 if name not in ('AGENTIAC_NODES', 'AGENTIAC_ROUTES', 'AGENTIAC_ROOT', 'PYTHONPATH')}
        done = subprocess.run([sys.executable, '-c', self.PROGRAM], capture_output=True, text=True, timeout=60,
                              env={**clean, 'PYTHONPATH': str(gateway or ROOT / 'gateway'), **env})
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return done.stdout.strip()

    def declared(self, directory):
        """DIRECTORY/gateway/local-coder.json: the site's file plus one retired name no real file has."""
        declared = json.loads((SITE / 'gateway/local-coder.json').read_text())
        declared['legacy_sources'] = {**declared.get('legacy_sources', {}), 'm7-instance-probe': next(iter(ROUTES))}
        (Path(directory) / 'gateway').mkdir(exist_ok=True)
        (Path(directory) / 'gateway/local-coder.json').write_text(json.dumps(declared))

    def test_the_declared_routes_come_from_the_instance_root(self):
        with tempfile.TemporaryDirectory() as directory:
            self.declared(directory)
            self.assertEqual(self.read(AGENTIAC_ROOT=directory), 'True')

    def test_without_the_variable_the_checkout_the_callbacks_are_in_is_the_instance_root(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'gateway').mkdir()
            for module in (ROOT / 'gateway').glob('*.py'):
                shutil.copy(module, Path(directory) / 'gateway' / module.name)
            self.declared(directory)
            self.assertEqual(self.read(gateway=Path(directory) / 'gateway'), 'True')


class NodeTableErrors(unittest.TestCase):
    def config(self, *entries):
        return {'model_list': [{'model_name': name, 'litellm_params': {
            'model': 'openai/m', 'api_base': base, 'api_key': key}} for name, base, key in entries]}

    def test_a_key_not_named_like_a_worker_key_is_refused_by_alias(self):
        with self.assertRaisesRegex(ValueError, 'odd'):
            route.node_tables(self.config(('odd', 'http://127.0.0.1:1/v1', 'literal-value')), {})

    def test_two_aliases_on_one_port_must_use_one_key(self):
        with self.assertRaisesRegex(ValueError, 'second'):
            route.node_tables(self.config(('first', 'http://127.0.0.1:1/v1', 'os.environ/A_API_KEY'),
                                          ('second', 'http://127.0.0.1:1/v1', 'os.environ/B_API_KEY')), {})

    def test_a_host_whose_port_no_alias_uses_is_skipped(self):
        tables = route.node_tables(self.config(('a', 'http://127.0.0.1:1/v1', 'os.environ/A_API_KEY')),
                                   {'a': {'tunnel_port': 1, 'backend_port': 9}, 'idle': {'tunnel_port': 2}})
        self.assertEqual(tables['host_by_key'], {'A': 'a'})

    def test_a_strata_host_is_found_on_its_strata_tunnel_and_a_tunnelless_host_on_its_backend_port(self):
        tables = route.node_tables(self.config(('s', 'http://127.0.0.1:4/v1', 'os.environ/S_API_KEY'),
                                               ('c', 'http://127.0.0.1:6/v1', 'os.environ/C_API_KEY')),
                                   {'s': {'backend': 'strata', 'strata_tunnel_port': 4, 'tunnel_port': 3},
                                    'c': {'backend_port': 6}})
        self.assertEqual(tables['host_by_key'], {'S': 's', 'C': 'c'})


if __name__ == '__main__':
    unittest.main()
