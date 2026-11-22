"""agentiac services: the read boundary reused by the dashboard management API.

The services must read the same declaration/read values as the CLI, not a second copy of
them. This test compares inventory, catalog and status against the existing modules and
checks that Agent versions are pinned, not invented.
"""
import datetime
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import command, declared, paths  # noqa: E402
from agentiac.services import inventory, status, catalog as catalog_service, agents, configuration  # noqa: E402

SITE = paths.root()


class ServiceTests(unittest.TestCase):
    def test_inventory_is_the_same_yaml_read_as_the_cli(self):
        self.assertEqual(inventory.nodes(SITE), command.inventory())

    def test_catalog_keeps_the_same_three_blocks_apart(self):
        current = command._route_now()
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertEqual(catalog_service.targets(SITE, current, now=now),
                         catalog_service.catalog.targets(SITE, current=current, now=now))

    def test_agent_version_is_read_from_the_pin_not_from_an_observation(self):
        rows = {row['id']: row for row in agents.load(SITE)['agents']}
        if not (SITE / 'agents/agents.yml').exists():
            self.assertEqual(rows, {})
            return
        pi = json.loads((ROOT / 'agents/pi/package.json').read_text())['dependencies']['@earendil-works/pi-coding-agent']
        self.assertEqual(rows['pi']['declared_version'], pi)
        for agent_id in ('claude-code', 'codex'):
            pin_path = SITE / f'agents/{agent_id}/pin.json'
            if pin_path.exists():
                expected = json.loads(pin_path.read_text())['version']
                self.assertEqual(rows[agent_id]['declared_version'], expected)

    def test_status_service_uses_the_same_transport_values_as_the_cli(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import borrow
        with patch.object(borrow, 'endpoint', return_value=(1, 'key')), \
                patch.object(borrow, 'ondemand_call', return_value=(200, {'state': 'ready', 'unload_in_s': 1725})):
            transport = SimpleNamespace(endpoint=borrow.endpoint, ondemand_call=borrow.ondemand_call)
            self.assertEqual(status.host_state('worker-a', {}, transport), command.host_state('worker-a', {}))
            self.assertEqual(status.host_state('worker-a', {}, transport), 'loaded, unloads in 29 min')

    def test_configuration_reads_the_declaration_graph_without_merging_runtime_state(self):
        graph = declared.load(SITE)
        report = configuration.load(SITE)
        self.assertEqual(report['route'], graph.route)
        self.assertEqual(report['references'], [])

    def test_explicit_agent_ids_do_not_depend_on_the_declaration_graph(self):
        entry = SimpleNamespace(id='pi', installed=lambda: False, allowed_paths=lambda root: ['/fixture'],
                                install='fixture', gateway_only=True, baseline=False)
        with patch.object(agents.adapters, 'adapter', return_value=entry), \
                patch.object(agents.declared, 'load', side_effect=AssertionError('graph loaded')):
            rows = agents.rows(SITE, ids=['pi'])
        self.assertEqual([row['id'] for row in rows], ['pi'])

    def test_api_agent_read_never_executes_installed_binary(self):
        with patch.object(agents, 'rows', side_effect=AssertionError('unexpected executable probe')):
            report = agents.load(SITE)
        if (SITE / 'agents/agents.yml').exists():
            self.assertTrue(report['agents'])
        for row in report['agents']:
            self.assertIsNone(row['version'])
            self.assertTrue(all(cap['value'] == 'unverified' for cap in row['capabilities'].values()))
