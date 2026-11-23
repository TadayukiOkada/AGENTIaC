"""A manifest that declares how its engine is served, so `claim` is not limited to the engines this file names.

`scripts/borrow.py` can serve two ways today: the Strata rules (a trial root, `<prefix>-strata-inference`, the
inventory's on-demand port) and a `spark_tensorfold` host's pinned service. An engine that serves from an image or a
container has neither, so its manifest says how it is served (`workers.<host>.service`): the unit, port and directory,
the container, and the `*.py` under `scripts/` that `ops prepare` installs as the `<root>/worker.py` the unit runs.
These tests run against a fixture checkout with `borrow.ROOT` pointed at it, so no shipped manifest is read and no host
is contacted; where the unit's text or launch script matters, `borrow.CODE` is pointed at a fixture core checkout.
"""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import backend_names  # noqa: E402


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
        self.borrow.ROOT = self.fixture  # the checkout the module reads manifests from; unit.yml stays in CODE

    def manifest(self, name, payload):
        (self.fixture / f'backends/{name}.json').write_text(json.dumps(payload))
        return name

    def host(self, **extra):
        """A worker that runs neither Strata nor Spark TensorFold: only what the declared way needs."""
        return {'ansible_host': '10.0.0.9', 'backend_port': 18080, 'ondemand_backend_port': 18083,
                'project_root': '/srv/agentiac', **extra}

    def service(self, **overrides):
        """What a manifest declares for an engine that serves from a container: unit, port, directory, image, script."""
        declared_service = {'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
                            'root': '/srv/agentiac/evaluations/infernix/service', 'container': 'infernix-serving',
                            'worker': 'infernix-worker.py'}
        return {**declared_service, **overrides}


class DeclaredServiceTests(Fixture):

    def test_a_manifest_that_declares_its_service_is_read_from_it(self):
        name = self.manifest('infernix-fixture', {'workers': {'workere': {'service': self.service()}}})
        self.assertEqual(self.borrow.service_spec(json.loads((self.fixture / f'backends/{name}.json').read_text()),
                                                 'workere'),
                         {'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
                          'root': '/srv/agentiac/evaluations/infernix/service', 'start_timeout_s': None,
                          'container': 'infernix-serving', 'status_path': None, 'worker': 'infernix-worker.py'})

    def test_a_manifest_that_declares_nothing_leaves_the_strata_rules_in_charge(self):
        manifest = {'source_version': '0.2.8', 'workers': {'workera': {}}}
        self.assertIsNone(self.borrow.service_spec(manifest, 'workera'))
        self.assertIsNone(self.borrow.manifest_service('workera', None))  # a claim with no --backend declares nothing

    def test_a_service_for_another_host_is_not_used(self):
        manifest = {'workers': {'workerb': {'service': self.service()}}}
        self.assertIsNotNone(self.borrow.service_spec(manifest, 'workerb'))
        self.assertIsNone(self.borrow.service_spec(manifest, 'workere'))

    def test_what_the_service_does_not_declare_stays_the_watch_and_the_front_default(self):
        # `strata-trial-watch.py` answers /v1/status and the front waits its own readiness timeout: a service that
        # does not say otherwise is served with those, not with values invented here.
        spec = self.borrow.service_spec({'workers': {'workere': {'service': {
            'unit': f'{self.prefix}-infernix-inference', 'port': 18090, 'root': '/srv/agentiac/evaluations/infernix'}}}},
            'workere')
        self.assertIsNone(spec['status_path'])
        self.assertIsNone(spec['start_timeout_s'])
        self.assertIsNone(spec['container'])

    def test_a_unit_this_site_could_not_install_is_refused_by_name(self):
        # Every deployed name is <resource_prefix>-<role> (D6.8); a unit the front stops must be one of ours.
        service = self.service(unit='someone-elses-serving')
        with self.assertRaises(SystemExit) as caught:
            self.borrow.service_spec({'workers': {'workere': {'service': service}}}, 'workere')
        message = str(caught.exception)
        self.assertIn('`unit`', message)
        self.assertIn(f'{self.prefix}-', message)
        self.assertIn('workers', message)

    def test_a_port_that_is_not_a_port_is_refused_without_naming_the_other_keys(self):
        service = self.service(port='18090')
        with self.assertRaises(SystemExit) as caught:
            self.borrow.service_spec({'workers': {'workere': {'service': service}}}, 'workere')
        self.assertIn('`port` must be the internal port', str(caught.exception))
        self.assertNotIn('`unit`', str(caught.exception))

    def test_malformed_service_shapes_and_unsafe_values_are_refused(self):
        for bad in ({}, 'bad'):
            with self.subTest(service=bad), self.assertRaises(SystemExit):
                self.borrow.service_spec({'workers': {'workere': {'service': bad}}}, 'workere')
        for field, value in (('unit', f'{self.prefix}-'), ('root', '/'), ('root', '/srv/../etc'),
                             ('root', '/srv/bad;rm'), ('container', 'bad name'), ('worker', 'infernix worker.py'),
                             ('worker', 'not_a_python_file'), ('worker', '../elsewhere.py'), ('worker', 3)):
            with self.subTest(field=field, value=value), self.assertRaises(SystemExit) as caught:
                self.borrow.service_spec({'workers': {'workere': {'service': self.service(**{field: value})}}},
                                         'workere')
            self.assertIn(f'`{field}`', str(caught.exception))

    def test_the_declared_units_of_a_host_are_what_another_claim_has_to_stop(self):
        self.manifest('infernix-fixture', {'workers': {'workere': {'service': self.service()}}})
        host = self.host(inference_unit=f'{self.prefix}-spark-tensorfold')
        self.assertIn(f'{self.prefix}-infernix-inference',
                      self.borrow.other_serving_units(host, f'{self.prefix}-ondemand', 'workere'))
        # without the host name the same call answers with only the units this file itself names
        self.assertNotIn(f'{self.prefix}-infernix-inference',
                         self.borrow.other_serving_units(host, f'{self.prefix}-ondemand'))


class ClaimResolutionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.name = self.manifest('infernix-fixture', {
            'id': 'infernix-fixture',
            'workers': {'workere': {'service': {
                'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
                'root': '/srv/agentiac/evaluations/infernix/service', 'container': 'infernix-serving',
                'status_path': '/health', 'start_timeout_s': 60}}}})

    def test_the_root_unit_and_internal_port_come_from_the_declaration(self):
        root, config = self.borrow.ondemand_config('workere', self.host(), 15, self.name, 262144, None)
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
            self.borrow.claim('workerc', self.host(), None, 15, False, backend=self.name)
        message = str(caught.exception)
        self.assertIn('declares neither way of serving it', message)
        self.assertIn('strata_trial_roots', message)
        self.assertIn('service', message)
        self.assertIn(f'ops prepare workerc --backend {self.name} --check', message)

    def test_a_host_that_declares_the_service_is_not_refused_by_the_strata_rule(self):
        touched = []
        self.borrow.declared = types.SimpleNamespace(load=lambda root: types.SimpleNamespace(retention={}))
        self.borrow.on_host = lambda host, command, **kw: touched.append(command) or types.SimpleNamespace(
            returncode=0, stdout='', stderr='')  # a front already running: nothing here may change the host
        with self.assertRaises(SystemExit) as caught:
            self.borrow.claim('workere', self.host(), None, 15, False, backend=self.name)
        self.assertIn('infra/unit.yml', str(caught.exception))
        self.assertEqual(touched, [['systemctl', 'is-active', '--quiet', f'{self.prefix}-ondemand']])

    def test_claim_succeeds_for_declared_service_without_default_profile(self):
        printed = []
        with mock.patch.object(self.borrow, 'weight_guard'), \
             mock.patch.object(self.borrow, 'enable_ondemand'), \
             mock.patch.object(self.borrow, 'start_tunnel'), \
             mock.patch.object(self.borrow, 'claims'), \
             mock.patch('builtins.print', side_effect=lambda line: printed.append(json.loads(line))):
            self.borrow.claim('workere', self.host(), False, 15, False, backend=self.name)
        self.assertEqual(printed, [{'claimed': 'workere', 'profile': self.name, 'context': None,
                                    'idle_unload_minutes': 15}])

    def test_backend_cli_choices_exclude_launch_lists_and_schema(self):
        (self.fixture / 'backends/infernix-fixture.launch.json').write_text('["--port", "18090"]\n')
        (self.fixture / 'backends/schema.json').write_text('{"$schema": "https://json-schema.org/draft/2020-12/schema"}\n')
        self.manifest('tf-1.1.4-fixture', {'id': 'tf-1.1.4-fixture'})
        choices = sorted(stem for stem in backend_names.manifests(self.fixture) if not stem.startswith('tf-'))
        self.assertEqual(choices, [self.name])

    def test_infernix_backend_name_derivation(self):
        manifest = {
            'engine': {'repository': 'https://github.com/example/Infernix-dgx-spark-gb10.git', 'version': '1.0.0'},
            'model': {'repository': 'example/Qwen3.8-Flash-Next-NVIDIA-FP4-Local-Infernix-HC4-W4Dense'},
            'quantization': 'HC4-W4Dense',
        }
        self.assertEqual(backend_names.derived(manifest), 'infernix-1.0.0-qfn-hc4-w4dense')


class PrepareUnitTests(Fixture):
    def recipe(self, roles=('infernix-inference',), worker=True, template=True):
        """A stand-in for the core checkout a declared service's unit text and launch script come from (D7.1)."""
        code = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, code)
        (code / 'infra/templates').mkdir(parents=True)
        (code / 'scripts').mkdir(parents=True)
        units = ''.join(f"      {role}: {{file: '{{{{ resource_prefix }}}}-{role}.service', run: false}}\n"
                        for role in roles)
        (code / 'infra/unit.yml').write_text(f'- name: Install a project systemd unit\n  vars:\n    units:\n{units}')
        if template:
            (code / 'infra/templates/infernix-inference.service.j2').write_text(
                '[Service]\nExecStart=/usr/bin/python3 {{ directory }}/worker.py\n')
        if worker:
            (code / 'scripts/infernix-worker.py').write_text('# the launch this engine declares\n')
        self.borrow.CODE = code
        return code

    def attempt(self, name, record=True):
        """`ops prepare` for the fixture manifest, with the host commands recorded instead of run."""
        root, config = self.borrow.ondemand_config('workere', self.host(), 15, name, 262144, None)
        self.touched, self.pushed, self.played = [], [], []
        if record:
            def on_host(host, command, **kw):
                self.touched.append((command, kw.get('input')))
                return types.SimpleNamespace(returncode=0, stdout='', stderr='')

            self.borrow.on_host = on_host
            self.borrow.put = lambda host, local, remote: self.pushed.append((str(local), remote)) or 0
            self.borrow.run_playbook = lambda *args: self.played.append(args) or 0
        self.borrow.prepare_backend('workere', self.host(), config, name)
        return root, self.touched, self.pushed, self.played

    def refused(self, name):
        """The message a preparation refuses with, and the host commands it reached: a refusal must reach none."""
        with self.assertRaises(SystemExit) as caught:
            self.attempt(name)
        return str(caught.exception), self.touched

    def test_a_service_with_no_unit_text_names_every_file_that_would_give_it_one(self):
        name = self.manifest('infernix-fixture', {'workers': {'workere': {'service': {
            'unit': f'{self.prefix}-infernix-inference', 'port': 18090, 'root': '/srv/agentiac/evaluations/infernix'}}}})
        message, touched = self.refused(name)
        self.assertIn(f'cannot install `{self.prefix}-infernix-inference`', message)
        self.assertIn('infra/templates/infernix-inference.service.j2', message)
        self.assertIn('scripts/<worker>.py', message)
        self.assertEqual(touched, [])

    def test_a_role_with_text_but_no_worker_to_install_is_refused_before_the_host_is_asked(self):
        # The unit text this site has runs `<root>/worker.py`; installing a unit whose script is nowhere is how a
        # claim ends up holding a GPU with a service that cannot start.
        name = self.manifest('infernix-fixture', {'workers': {'workere': {'service': self.service(worker=None)}}})
        self.recipe()
        message, touched = self.refused(name)
        self.assertIn('service.worker', message)
        self.assertIn('scripts/<worker>.py', message)
        self.assertEqual(touched, [])

    def test_a_declared_worker_that_the_code_does_not_have_is_refused_by_name(self):
        name = self.manifest('infernix-fixture', {'workers': {'workere': {'service': self.service()}}})
        self.recipe(worker=False)
        message, touched = self.refused(name)
        self.assertIn('scripts/infernix-worker.py', message)
        self.assertEqual(touched, [])

    def test_the_map_is_not_the_text_a_role_in_it_without_a_template_is_refused(self):
        name = self.manifest('infernix-fixture', {'workers': {'workere': {'service': self.service()}}})
        self.recipe(template=False)
        message, touched = self.refused(name)
        self.assertIn('no `infra/templates/infernix-inference.service.j2`', message)
        self.assertEqual(touched, [])

    def test_preparation_installs_the_script_the_unit_runs_and_the_facts_that_script_needs(self):
        name = self.manifest('infernix-fixture', {'workers': {'workere': {'service': self.service(
            status_path='/health')}}})
        code = self.recipe()
        (self.fixture / 'secrets/nodes').mkdir(parents=True)
        (self.fixture / 'secrets/nodes/workere.key').write_text('fixture-key\n')
        root, touched, pushed, played = self.attempt(name)
        self.assertIn((str(self.fixture / f'backends/{name}.json'), f'{root}/backend-manifest.json'), pushed)
        self.assertIn((str(code / 'scripts/infernix-worker.py'), f'{root}/worker.py'), pushed)
        written = {command[0].split('> ')[1]: body for command, body in touched
                   if isinstance(command, list) and 'service.json' in command[0]}
        self.assertEqual(list(written), [f'{root}/service.json'])
        facts = json.loads(written[f'{root}/service.json'])
        self.assertEqual(facts, {'unit': f'{self.prefix}-infernix-inference', 'port': 18090, 'root': root,
                                 'container': 'infernix-serving', 'status_path': '/health'})
        self.assertEqual(played, [('unit', 'workere', '-e', 'unit=infernix-inference',
                                   '-e', f'directory={root}')])

    def test_the_roles_this_site_can_install_are_read_from_the_file_that_installs_them(self):
        self.assertEqual(self.borrow.installed_unit_roles(), {'ondemand', 'spark-tensorfold'})


class WatchInvocationTests(Fixture):
    def test_the_watch_is_started_against_the_unit_and_container_this_claim_resolved(self):
        name = self.manifest('infernix-fixture', {'workers': {'workere': {'service': {
            'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
            'root': '/srv/agentiac/evaluations/infernix/service', 'container': 'infernix-serving',
            'status_path': '/health'}}}})
        root, config = self.borrow.ondemand_config('workere', self.host(), 15, name, 262144, None)
        started, pushed = [], []
        self.borrow.declared = types.SimpleNamespace(account=lambda host, root: 'tester')
        self.borrow.put = lambda host, local, remote: pushed.append((str(local), remote)) or 0
        self.borrow.on_host = lambda host, command, **kw: started.append(command) or types.SimpleNamespace(
            returncode=0, stdout='', stderr='')
        self.borrow.start_watch('workere', self.host(), root, config)
        argv = [arg for command in started for arg in (command if isinstance(command, list) else [command])]
        self.assertEqual(argv[argv.index('--unit') + 1], f'{self.prefix}-infernix-inference')
        self.assertEqual(argv[argv.index('--container') + 1], 'infernix-serving')
        self.assertEqual(argv[argv.index('--status-path') + 1], '/health')
        self.assertEqual(argv[argv.index('--port') + 1], '18090')
        self.assertEqual(pushed[0][1], f'{root}/trial-watch.py')

    def test_a_strata_claim_watches_the_strata_unit_and_names_no_container(self):
        name = self.manifest('strata-fixture-028', {'source_version': '0.2.8'})
        root, config = self.borrow.ondemand_config(
            'workera', {'backend': 'strata', 'backend_port': 18080, 'ondemand_backend_port': 18083,
                        'strata_api_port': 18085, 'project_root': '/srv/agentiac', 'strata_default_backend': name,
                        'strata_trial_roots': {'0.2.8': '/prepared/028'}}, 15, name)
        self.assertEqual(config['unit'], f'{self.prefix}-strata-inference')
        self.assertNotIn('container', config)
        self.assertNotIn('status_path', config)

    def test_rolling_back_to_tensorfold_stops_watch_even_without_strata_trial_roots(self):
        self.manifest('infernix-fixture', {'workers': {'workere': {'service': {
            'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
            'root': '/srv/agentiac/evaluations/infernix/service'}}}})
        tf_name = self.manifest('tf-1.1.4-fixture', {
            'id': 'tf-1.1.4-fixture',
            'engine': {'repository': 'https://github.com/example/TensorFold.git', 'version': '1.1.4'},
            'workers': {'workere': {'slots': 1}},
        })
        host = self.host(spark_tensorfold=True)
        commands = []
        self.borrow.on_host = lambda h, cmd, **kw: commands.append(cmd) or types.SimpleNamespace(
            returncode=0, stdout='', stderr='')
        with mock.patch.object(self.borrow, 'prepare_backend'), \
             mock.patch.object(self.borrow, 'running_front_directory', return_value=None), \
             mock.patch.object(self.borrow, 'install_front'), \
             mock.patch.object(self.borrow, 'put', return_value=0):
            self.borrow.enable_ondemand('workere', host, 15, backend=tf_name)
        self.assertIn(['sudo', '-n', 'systemctl', 'stop', self.borrow.WATCH], commands)


class WeightGuardTests(Fixture):
    """F6 on the declared way: the manifest says where its weights are, so the guard can look there (T5.3)."""

    def setUp(self):
        super().setUp()
        # The ledger half of the guard reads the whole declaration graph; these tests give it an empty one, which is
        # what a host with no `infra/retention.yml` line for the backend answers, and keep the measured half real.
        self.borrow.declared = types.SimpleNamespace(load=lambda root: types.SimpleNamespace(retention={}))
        self.asked = []
        self.answers = types.SimpleNamespace(returncode=0, stdout='', stderr='')
        self.borrow.on_host = lambda host, command, **kw: self.asked.append(command) or self.answers

    def service(self, model):
        return self.manifest('infernix-fixture', {
            'id': 'infernix-fixture',
            'engine': {'repository': 'https://github.com/example/Infernix-dgx-spark-gb10.git',
                       'version': '1.0.0.dev-fc913e30'},
            'model': {'repository': 'example/Qwen3.8-Flash-Next-NVIDIA-FP4-Local-Infernix-HC4-W4Dense',
                      'files': [{'path': 'model-00001-of-00003.safetensors', 'size': 7, 'sha256': 'a' * 64},
                                {'path': 'ngram.bin', 'size': 5}], **model},
            'workers': {'workere': {'service': {'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
                                                'root': '/srv/agentiac/evaluations/infernix/service'}}}})

    def test_the_guard_looks_where_the_manifest_says_its_model_is(self):
        backend = self.service({'directory': 'evaluations/infernix/hc4-w4dense'})
        self.answers.stdout = '\n'.join(
            f"present /srv/agentiac/evaluations/infernix/hc4-w4dense/{name}" for name in
            ('model-00001-of-00003.safetensors', 'ngram.bin'))
        self.borrow.weight_guard('workere', self.host(), backend)
        asked = self.asked[0]
        self.assertEqual(asked[0], 'bash')
        self.assertIn('/srv/agentiac/evaluations/infernix/hc4-w4dense/model-00001-of-00003.safetensors', asked)
        self.assertIn('7', asked)  # the size the manifest pins is what the host is asked about

    def test_a_service_manifest_that_names_no_directory_is_refused_before_the_host_is_asked(self):
        backend = self.service({})   # pins two weight files and says nothing about where they are
        with self.assertRaises(SystemExit) as caught:
            self.borrow.weight_guard('workere', self.host(), backend)
        message = str(caught.exception)
        self.assertIn('names no model directory', message)
        self.assertIn('ops prepare workere --backend infernix-fixture --check', message)
        self.assertEqual(self.asked, [])

    def test_a_weight_that_is_not_there_refuses_the_claim_naming_where_it_looked(self):
        backend = self.service({'directory': 'evaluations/infernix/hc4-w4dense'})
        self.answers.stdout = ('present /srv/agentiac/evaluations/infernix/hc4-w4dense/model-00001-of-00003.safetensors\n'
                               'missing /srv/agentiac/evaluations/infernix/hc4-w4dense/ngram.bin')
        with self.assertRaises(SystemExit) as caught:
            self.borrow.weight_guard('workere', self.host(), backend)
        message = str(caught.exception)
        self.assertIn('not in /srv/agentiac/evaluations/infernix/hc4-w4dense', message)
        self.assertIn('ngram.bin (5 B)', message)

    def test_a_flat_manifest_on_a_host_with_no_trial_root_is_refused_rather_than_checked_nowhere(self):
        name = self.manifest('fixture-flat', {'id': 'fixture-flat', 'source_version': '0.2.8',
                                              'files': [{'path': 'weights.gguf', 'size': 3}],
                                              'workers': {'workere': {'service': {
                                                  'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
                                                  'root': '/srv/agentiac/evaluations/infernix/service'}}}})
        with self.assertRaises(SystemExit) as caught:
            self.borrow.weight_guard('workere', self.host(), name)
        self.assertIn('strata_trial_roots', str(caught.exception))
        self.assertEqual(self.asked, [])

    def test_a_manifest_that_pins_nothing_is_not_a_reason_to_read_the_host(self):
        name = self.manifest('fixture-nothing', {'id': 'fixture-nothing', 'source_version': '0.2.8',
                                                'workers': {'workere': {'service': {
                                                    'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
                                                    'root': '/srv/agentiac/evaluations/infernix/service'}}}})
        self.borrow.weight_guard('workere', self.host(), name)
        self.assertEqual(self.asked, [])

    def test_the_spark_tensorfold_claim_is_left_as_it_is_today(self):
        # Spark serves its TensorFold manifest as its own backend; making that claim start checking 32 pinned files
        # is a change to a live host, recorded in the plan as its own batch. This test is the fence.
        name = self.manifest('tf-fixture', {'id': 'tf-fixture',
                                            'engine': {'repository': 'https://github.com/example/TensorFold.git',
                                                       'version': '1.1.4'},
                                            'model': {'repository': 'example/Model', 'directory': 'evaluations/x',
                                                      'files': [{'path': 'model.safetensors', 'size': 9}]},
                                            'workers': {'workere': {'slots': 1}}})
        self.borrow.weight_guard('workere', self.host(spark_tensorfold=True), name)
        self.assertEqual(self.asked, [])

    def test_slash_or_traversal_model_directory_is_refused_without_touching_host(self):
        for bad_dir in ('/', '../escape'):
            with self.subTest(directory=bad_dir):
                backend = self.service({'directory': bad_dir})
                with self.assertRaises(SystemExit) as caught:
                    self.borrow.weight_guard('workere', self.host(), backend)
                self.assertIn('names no model directory', str(caught.exception))
                self.assertEqual(self.asked, [])

    def test_thin_ledger_refusal_runs_even_when_manifest_pins_no_files(self):
        name = self.manifest('fixture-nothing', {
            'id': 'fixture-nothing', 'source_version': '0.2.8',
            'workers': {'workere': {'service': {'unit': f'{self.prefix}-infernix-inference', 'port': 18090,
                                                'root': '/srv/agentiac/evaluations/infernix/service'}}}})
        self.borrow.declared = types.SimpleNamespace(load=lambda root: types.SimpleNamespace(retention={
            'workere': {'serving': [], 'rollback': [], 'candidate': [], 'quarantine': [], 'retired': [],
                        'areas': [], 'staging': [],
                        'candidate': [{'backend': name, 'path': '/srv/agentiac/evaluations/infernix/service',
                                       'form': 'thin'}]}}))
        with self.assertRaises(SystemExit) as caught:
            self.borrow.weight_guard('workere', self.host(), name)
        self.assertIn('form: thin', str(caught.exception))
        self.assertEqual(self.asked, [])


if __name__ == '__main__':
    unittest.main()
