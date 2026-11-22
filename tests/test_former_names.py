"""T8.10: a host renamed in the inventory keeps what was recorded under its old name (class A, synthetic names).

`former_names` (newest first) on an inventory host makes a record, an argument or an alias written under an old name
mean the current host, and keeps every target identity (`config_id`) where it was.
"""
import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import claims, declared, evidence, identities, telemetry  # noqa: E402
from agentiac import command  # noqa: E402

NODES = {'new-host': {'former_names': ['middle-host', 'first-host']}, 'other-host': {}}


class ResolverTests(unittest.TestCase):
    def test_a_former_name_names_the_current_host(self):
        self.assertEqual(declared.former_names(NODES), {'middle-host': 'new-host', 'first-host': 'new-host'})
        self.assertEqual(declared.current_host('first-host', NODES), 'new-host')
        self.assertEqual(declared.current_host('other-host', NODES), 'other-host')
        self.assertEqual(declared.current_host('unknown', NODES), 'unknown')

    def test_identities_use_the_first_name(self):
        self.assertEqual(declared.identity_name('new-host', NODES), 'first-host')
        self.assertEqual(declared.identity_name('other-host', NODES), 'other-host')

    def test_a_former_name_must_resolve_to_exactly_one_host(self):
        self.assertEqual(declared.former_name_problems(NODES), [])
        clash = {'a-host': {'former_names': ['b-host']}, 'b-host': {}}
        self.assertEqual(declared.former_name_problems(clash),
                         ['infra/inventory.yml host a-host former_names: b-host is a current host'])
        twice = {'a-host': {'former_names': ['old-host']}, 'b-host': {'former_names': ['old-host']}}
        self.assertEqual(len(declared.former_name_problems(twice)), 1)
        self.assertEqual(len(declared.former_name_problems({'a-host': {'former_names': 'old-host'}})), 1)


class RecordTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'infra').mkdir()
        (self.root / 'infra/inventory.yml').write_text(yaml.safe_dump(
            {'all': {'vars': {}, 'children': {'workers': {'hosts': NODES}}}}))
        state = mock.patch.dict(os.environ, {'AGENTIAC_STATE_ROOT': str(self.root / 'state')})
        state.start()
        self.addCleanup(state.stop)

    def test_a_claim_recorded_under_a_former_name_is_the_current_hosts(self):
        claims.write(self.root, 'first-host', 'backend-a', 4096, 30, 'claim')
        self.assertEqual(claims.read(self.root, 'new-host')['backend'], 'backend-a')
        self.assertEqual(list(claims.all(self.root)), ['new-host'])
        claims.clear(self.root, 'new-host')
        self.assertIsNone(claims.read(self.root, 'new-host'))
        self.assertEqual(claims.all(self.root), {})

    def test_telemetry_reads_the_files_filed_under_a_former_name(self):
        folder = self.root / 'telemetry'
        folder.mkdir()
        row = {'time': datetime.datetime(2026, 11, 16, tzinfo=datetime.timezone.utc).isoformat(),
               'mem_kib': {'MemAvailable': 1024}}
        (folder / 'first-host-20261116T000000.jsonl').write_text(json.dumps(row) + '\n')
        self.assertIsNone(telemetry.latest(folder, 'new-host'))
        self.assertEqual(telemetry.latest(folder, 'new-host', former=['first-host'])[1], 1024)

    def test_an_alias_naming_a_former_host_belongs_to_the_current_one(self):
        workers = evidence._workers(self.root)
        self.assertEqual(evidence._node_of('strata-first-host-gyro', workers), 'new-host')
        self.assertEqual(evidence._node_of('other-host-worker', workers), 'other-host')


class CommandTests(unittest.TestCase):
    def test_a_host_argument_given_by_a_former_name_becomes_the_current_host(self):
        args = SimpleNamespace(host='first-host', hosts=['middle-host', 'other-host'], node=None)
        with mock.patch.object(command, 'inventory', return_value=NODES), \
                mock.patch('sys.stderr') as stderr:
            command.resolve_former_hosts(args)
        self.assertEqual((args.host, args.hosts), ('new-host', ['new-host', 'other-host']))
        written = ''.join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn('first-host is now new-host', written)


class IdentityTests(unittest.TestCase):
    """Renaming a host in a copy of examples/site changes no target's config_id."""

    def test_a_rename_with_the_old_name_recorded_keeps_every_config_id(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / 'site'
            shutil.copytree(CODE / 'examples/site', site)
            initial = declared.load(site)
            old = next(h for h, f in initial.nodes.items() if f.get('borrowed'))
            new = 'renamed-worker'
            before = {spec.class_id + spec.config_id for spec in identities.compute_all(initial).values()}
            for path in site.rglob('*'):
                if path.is_file():
                    path.write_text(path.read_text().replace(old, new))
            inventory = yaml.safe_load((site / 'infra/inventory.yml').read_text())
            inventory['all']['children']['workers']['hosts'][new]['former_names'] = [old]
            (site / 'infra/inventory.yml').write_text(yaml.safe_dump(inventory, sort_keys=False))
            graph = declared.load(site)
            after = {spec.class_id + spec.config_id for spec in identities.compute_all(graph).values()}
            self.assertEqual(declared.former_name_problems(graph.nodes), [])
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
