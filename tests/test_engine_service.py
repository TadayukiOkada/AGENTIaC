"""A manifest that declares how its engine is served, so `claim` is not limited to the engines this file names.

`scripts/borrow.py` can serve two ways today: the Strata rules (a trial root, `<prefix>-strata-inference`, the
inventory's on-demand port) and a `spark_tensorfold` host's pinned service. An engine that serves from an image or a
container has neither, so its manifest says how it is served (`workers.<host>.service`). These tests run against a
fixture checkout with `borrow.ROOT` pointed at it, so no shipped manifest is read and no host is contacted.
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def borrow_module():
    spec = importlib.util.spec_from_file_location('borrow_service', ROOT / 'scripts/borrow.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fixture(unittest.TestCase):
    def setUp(self):
        self.borrow = borrow_module()
        self.prefix = self.borrow.PREFIX
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = Path(directory.name)
        (self.fixture / 'backends').mkdir()
        self.borrow.ROOT = self.fixture  # the checkout the module reads manifests and unit.yml from
        (self.fixture / 'infra').mkdir()
        (self.fixture / 'infra/unit.yml').write_text((ROOT / 'infra/unit.yml').read_text())  # the shipped roles

    def manifest(self, name, payload):
        (self.fixture / f'backends/{name}.json').write_text(json.dumps(payload))
        return name

    def host(self, **extra):
        """A worker that runs neither Strata nor Spark TensorFold: only what the declared way needs."""
        return {'ansible_host': '10.0.0.9', 'backend_port': 18080, 'ondemand_backend_port': 18083,
                'project_root': '/srv/agentiac', **extra}


class DeclaredServiceTests(Fixture):
    def service(self, **overrides):
        declared_service = {'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
                            'root': '/srv/agentiac/evaluations/infernix/service', 'container': 'infernix-serving'}
        return {**declared_service, **overrides}

    def test_a_manifest_that_declares_its_service_is_read_from_it(self):
        name = self.manifest('infernix-fixture', {'workers': {'tinkerbell': {'service': self.service()}}})
        self.assertEqual(self.borrow.service_spec(json.loads((self.fixture / f'backends/{name}.json').read_text()),
                                                 'tinkerbell'),
                         {'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
                          'root': '/srv/agentiac/evaluations/infernix/service', 'start_timeout_s': None,
                          'container': 'infernix-serving', 'status_path': None})

    def test_a_manifest_that_declares_nothing_leaves_the_strata_rules_in_charge(self):
        manifest = {'source_version': '0.2.8', 'workers': {'orin1': {}}}
        self.assertIsNone(self.borrow.service_spec(manifest, 'orin1'))
        self.assertIsNone(self.borrow.manifest_service('orin1', None))  # a claim with no --backend declares nothing

    def test_a_service_for_another_host_is_not_used(self):
        manifest = {'workers': {'orin2': {'service': self.service()}}}
        self.assertIsNotNone(self.borrow.service_spec(manifest, 'orin2'))
        self.assertIsNone(self.borrow.service_spec(manifest, 'tinkerbell'))

    def test_what_the_service_does_not_declare_stays_the_watch_and_the_front_default(self):
        # `strata-trial-watch.py` answers /v1/status and the front waits its own readiness timeout: a service that
        # does not say otherwise is served with those, not with values invented here.
        spec = self.borrow.service_spec({'workers': {'tinkerbell': {'service': {
            'unit': f'{self.prefix}-infernix-inference', 'port': 18090, 'root': '/srv/agentiac/evaluations/infernix'}}}},
            'tinkerbell')
        self.assertIsNone(spec['status_path'])
        self.assertIsNone(spec['start_timeout_s'])
        self.assertIsNone(spec['container'])

    def test_a_unit_this_site_could_not_install_is_refused_by_name(self):
        # Every deployed name is <resource_prefix>-<role> (D6.8); a unit the front stops must be one of ours.
        service = self.service(unit='someone-elses-serving')
        with self.assertRaises(SystemExit) as caught:
            self.borrow.service_spec({'workers': {'tinkerbell': {'service': service}}}, 'tinkerbell')
        message = str(caught.exception)
        self.assertIn('`unit`', message)
        self.assertIn(f'{self.prefix}-', message)
        self.assertIn('workers', message)

    def test_a_port_that_is_not_a_port_is_refused_without_naming_the_other_keys(self):
        service = self.service(port='18090')
        with self.assertRaises(SystemExit) as caught:
            self.borrow.service_spec({'workers': {'tinkerbell': {'service': service}}}, 'tinkerbell')
        self.assertIn('`port` must be the internal port', str(caught.exception))
        self.assertNotIn('`unit`', str(caught.exception))

    def test_the_declared_units_of_a_host_are_what_another_claim_has_to_stop(self):
        self.manifest('infernix-fixture', {'workers': {'tinkerbell': {'service': self.service()}}})
        host = self.host(inference_unit=f'{self.prefix}-spark-tensorfold')
        self.assertIn(f'{self.prefix}-infernix-inference',
                      self.borrow.other_serving_units(host, f'{self.prefix}-ondemand', 'tinkerbell'))
        # without the host name the same call answers with only the units this file itself names
        self.assertNotIn(f'{self.prefix}-infernix-inference',
                         self.borrow.other_serving_units(host, f'{self.prefix}-ondemand'))


class ClaimResolutionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.name = self.manifest('infernix-fixture', {'workers': {'tinkerbell': {'service': {
            'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
            'root': '/srv/agentiac/evaluations/infernix/service', 'container': 'infernix-serving',
            'status_path': '/health', 'start_timeout_s': 60}}}})

    def test_the_root_unit_and_internal_port_come_from_the_declaration(self):
        root, config = self.borrow.ondemand_config('tinkerbell', self.host(), 15, self.name, 262144, None)
        self.assertEqual(root, '/srv/agentiac/evaluations/infernix/service')
        self.assertEqual(config['unit'], f'{self.prefix}-infernix-inference')
        self.assertEqual(config['backend_port'], 18090)          # internal: the front's port stays the inventory's
        self.assertEqual(config['listen_port'], 18080)
        self.assertEqual(config['container'], 'infernix-serving')  # so the watch can name the container
        self.assertEqual(config['status_path'], '/health')
        self.assertEqual(config['start_timeout_s'], 60)         # this engine's readiness, not TensorFold's
        self.assertEqual(config['stop'], ['sudo', '-n', 'systemctl', 'stop', f'{self.prefix}-infernix-inference'])
        self.assertEqual(config['manifest_file'], f'{root}/backend-manifest.json')

    def test_a_host_with_no_way_to_serve_the_manifest_is_refused_with_both_ways_named(self):
        with self.assertRaises(SystemExit) as caught:
            self.borrow.claim('orin3', self.host(), None, 15, False, backend=self.name)
        message = str(caught.exception)
        self.assertIn('declares neither way of serving it', message)
        self.assertIn('strata_trial_roots', message)
        self.assertIn('service', message)
        self.assertIn(f'ops prepare orin3 --backend {self.name} --check', message)

    def test_a_host_that_declares_the_service_is_not_refused_by_the_strata_rule(self):
        touched = []
        self.borrow.on_host = lambda host, command, **kw: touched.append(command) or types.SimpleNamespace(
            returncode=0, stdout='', stderr='')  # a front already running: nothing here may change the host
        with self.assertRaises(SystemExit) as caught:
            self.borrow.claim('tinkerbell', self.host(), None, 15, False, backend=self.name)
        self.assertIn('infra/unit.yml', str(caught.exception))
        self.assertEqual(touched, [['systemctl', 'is-active', '--quiet', f'{self.prefix}-ondemand']])


class PrepareUnitTests(Fixture):
    def test_a_service_with_no_unit_text_names_the_two_files_that_would_give_it_one(self):
        name = self.manifest('infernix-fixture', {'workers': {'tinkerbell': {'service': {
            'unit': f'{self.prefix}-infernix-inference', 'port': 18090, 'root': '/srv/agentiac/evaluations/infernix'}}}})
        root, config = self.borrow.ondemand_config('tinkerbell', self.host(), 15, name, 262144, None)
        touched = []
        self.borrow.on_host = lambda host, command, **kw: touched.append(command) or types.SimpleNamespace(
            returncode=0, stdout='', stderr='')
        with self.assertRaises(SystemExit) as caught:
            self.borrow.prepare_backend('tinkerbell', self.host(), config, name)
        message = str(caught.exception)
        self.assertIn(f'cannot install `{self.prefix}-infernix-inference`', message)
        self.assertIn('infra/templates/infernix-inference.service.j2', message)
        self.assertEqual(touched, [])

    def test_the_roles_this_site_can_install_are_read_from_the_file_that_installs_them(self):
        self.assertEqual(self.borrow.installed_unit_roles(), {'ondemand', 'spark-tensorfold'})


class WatchInvocationTests(Fixture):
    def test_the_watch_is_started_against_the_unit_and_container_this_claim_resolved(self):
        name = self.manifest('infernix-fixture', {'workers': {'tinkerbell': {'service': {
            'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
            'root': '/srv/agentiac/evaluations/infernix/service', 'container': 'infernix-serving',
            'status_path': '/health'}}}})
        root, config = self.borrow.ondemand_config('tinkerbell', self.host(), 15, name, 262144, None)
        started, pushed = [], []
        self.borrow.declared = types.SimpleNamespace(account=lambda host, root: 'tester')
        self.borrow.put = lambda host, local, remote: pushed.append((str(local), remote)) or 0
        self.borrow.on_host = lambda host, command, **kw: started.append(command) or types.SimpleNamespace(
            returncode=0, stdout='', stderr='')
        self.borrow.start_watch('tinkerbell', self.host(), root, config)
        argv = [arg for command in started for arg in (command if isinstance(command, list) else [command])]
        self.assertEqual(argv[argv.index('--unit') + 1], f'{self.prefix}-infernix-inference')
        self.assertEqual(argv[argv.index('--container') + 1], 'infernix-serving')
        self.assertEqual(argv[argv.index('--status-path') + 1], '/health')
        self.assertEqual(argv[argv.index('--port') + 1], '18090')
        self.assertEqual(pushed[0][1], f'{root}/trial-watch.py')

    def test_a_strata_claim_watches_the_strata_unit_and_names_no_container(self):
        name = self.manifest('strata-fixture-028', {'source_version': '0.2.8'})
        root, config = self.borrow.ondemand_config(
            'orin1', {'backend': 'strata', 'backend_port': 18080, 'ondemand_backend_port': 18083,
                      'strata_api_port': 18085, 'project_root': '/srv/agentiac', 'strata_default_backend': name,
                      'strata_trial_roots': {'0.2.8': '/prepared/028'}}, 15, name)
        self.assertEqual(config['unit'], f'{self.prefix}-strata-inference')
        self.assertNotIn('container', config)
        self.assertNotIn('status_path', config)


if __name__ == '__main__':
    unittest.main()
