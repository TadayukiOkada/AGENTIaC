"""Cross-file rules that keep the inventory, manifests, routes, templates and playbooks in agreement."""
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import declared, paths  # noqa: E402
import borrow  # noqa: E402  (the same resolver the CLI uses, so this test cannot drift from it)

SITE = paths.root()
locate = paths.locate   # assert_valid has a parameter named `paths`
INVENTORY = yaml.safe_load((SITE / 'infra/inventory.yml').read_text())['all']
HOSTS = INVENTORY['children']['workers']['hosts']
COMPONENTS = yaml.safe_load((SITE / 'infra/components.yml').read_text())
# infra/retention.yml is declared state like components.yml, not a playbook (PA §7.1, T5.1).
PLAYBOOKS = [p for p in sorted((ROOT / 'infra').glob('*.yml'))
             if p.name not in ('inventory.yml', 'components.yml', 'retention.yml')]
TASK_FILES = sorted((ROOT / 'infra/tasks').glob('*.yml'))
ROUTES = json.loads((SITE / 'gateway/local-coder.json').read_text())
STRATA = sorted((SITE / 'backends').glob('strata-*.json'))


def tasks_of(tasks):
    for task in tasks:
        yield task
        yield from tasks_of(task.get('block', []))


class UpdateCheckUnitTests(unittest.TestCase):
    """T9.5: the daily check is a sandboxed read, runs once a day, and leaves a failed read out of `failed units`."""
    SERVICE = (ROOT / 'infra/templates/update-check.service.j2').read_text()
    TIMER = (ROOT / 'infra/templates/update-check.timer.j2').read_text()
    PLAYBOOK = yaml.safe_load((ROOT / 'infra/control.yml').read_text())

    def watch_block(self):
        return next(task for play in self.PLAYBOOK for task in play.get('tasks', []) if task.get('tags') == ['watch'])

    def test_it_runs_the_check_and_asks_for_the_notification(self):
        self.assertIn('ExecStart={{ code_root }}/bin/agentiac ops update check --notify\n', self.SERVICE)

    def test_it_can_write_only_its_own_records(self):
        self.assertEqual(re.findall(r'^ReadWritePaths=(.*)$', self.SERVICE, re.M),
                         ['{{ instance_root }}/artifacts/components'])
        for line in ('ProtectSystem=strict', 'ProtectHome=read-only', 'NoNewPrivileges=yes',
                     'PrivateTmp=yes', 'User={{ ansible_user }}'):
            self.assertIn(line + '\n', self.SERVICE)

    def test_an_upstream_that_cannot_be_read_is_not_a_failed_unit(self):
        self.assertIn('SuccessExitStatus=3\n', self.SERVICE)

    def test_the_timer_runs_once_a_day_and_catches_up(self):
        self.assertEqual(re.findall(r'^OnCalendar=(.*)$', self.TIMER, re.M), ['*-*-* 06:17:00'])
        self.assertIn('Persistent=true\n', self.TIMER)
        self.assertIn('Unit={{ resource_prefix }}-update-check.service\n', self.TIMER)

    def test_the_playbook_installs_both_units_and_keeps_the_timer_running(self):
        block = self.watch_block()
        self.assertLessEqual({'update-check.service', 'update-check.timer'}, set(block['vars']['watch_units']))
        started = next(task for task in block['block'] if task.get('name', '').startswith('Keep the'))
        self.assertIn('update-check.timer', started['loop'])
        self.assertNotIn('update-check.service', started['loop'])

    def test_check_mode_skips_only_a_unit_that_is_not_installed_yet(self):
        """Check mode installs nothing, and starting a missing unit fails `ops deploy --check`. A real run never skips."""
        started = next(task for task in self.watch_block()['block'] if task.get('name', '').startswith('Keep the'))
        self.assertTrue(started['when'].startswith('not ansible_check_mode or '))
        self.assertIn("'/etc/systemd/system/' ~ resource_prefix ~ '-' ~ item", started['when'])

    def test_the_directory_it_writes_exists_before_the_timer_starts(self):
        """ReadWritePaths on a missing path fails the unit before it runs."""
        names = [task['name'] for task in self.watch_block()['block']]
        made = next(index for index, name in enumerate(names) if 'directory' in name)
        self.assertLess(made, next(index for index, name in enumerate(names) if name.startswith('Keep the')))

    def test_deploying_it_cannot_restart_a_gateway(self):
        block = self.watch_block()
        self.assertNotIn('gateway', yaml.safe_dump(block))
        self.assertEqual(block['tags'], ['watch'])


class SchemaTests(unittest.TestCase):
    def assert_valid(self, schema_path, candidates):
        validator = jsonschema.Draft202012Validator(json.loads((ROOT / schema_path).read_text()))
        for path in candidates:
            with self.subTest(path.name):
                errs = [e.message for e in validator.iter_errors(json.loads(path.read_text()))]
                self.assertEqual(errs, [])

    def test_model_profiles_match_their_schema(self):
        self.assert_valid('profiles/schema.json',
                          [p for p in (SITE / 'profiles').glob('*.json') if p.name != 'schema.json'])

    def test_strata_manifests_match_their_schema(self):
        self.assertTrue(STRATA)
        self.assert_valid('backends/schema.json', STRATA)


class CrossFileTests(unittest.TestCase):
    def test_runtime_image_tag_is_the_dockerfile_hash(self):
        tag = INVENTORY['vars']['runtime_image'].rsplit(':', 1)[1]
        if re.fullmatch(r'[0-9a-f]{7,64}', tag):
            self.assertTrue(hashlib.sha256((ROOT / 'runtime/Dockerfile').read_bytes()).hexdigest().startswith(tag))

    def test_default_profiles_exist_and_serve_their_node_class(self):
        for name, host in HOSTS.items():
            if 'default_profile' in host:
                profile = json.loads((SITE / f"profiles/{host['default_profile']}.json").read_text())
                with self.subTest(name):
                    self.assertIn(host['node_class'], profile['nodes'])

    def test_strata_manifests_name_inventory_hosts_with_the_same_gpu(self):
        for path in STRATA:
            for name, worker in json.loads(path.read_text())['workers'].items():
                with self.subTest(path.name, host=name):
                    host = HOSTS[name]
                    self.assertTrue(host.get('backend') == 'strata' or 'strata_backend' in host
                                    or name in host.get('strata_trial_roots', {}) or host.get('strata_trial_roots'),
                                    name)
                    self.assertEqual(worker['cuda_arch'], host['cuda_arch'])

    def test_a_strata_backend_of_a_dedicated_worker_is_a_manifest_for_that_worker(self):
        named = {name: host['strata_backend'] for name, host in HOSTS.items() if 'strata_backend' in host}
        self.assertTrue(named)
        for name, backend in named.items():
            manifest = json.loads((SITE / f'backends/{backend}.json').read_text())
            with self.subTest(name):
                self.assertIn(name, manifest['workers'])
                self.assertIn(manifest['source_version'], HOSTS[name]['strata_trial_roots'])
                self.assertNotEqual(HOSTS[name].get('backend'), 'strata')

    def test_tensorfold_hosts_keep_serving_tensorfold_by_default(self):
        for name, host in HOSTS.items():
            if host.get('spark_tensorfold'):
                self.assertNotIn('strata_backend', host, name)
                self.assertNotIn('strata_default_backend', host, name)
                self.assertEqual(host['inference_unit'], declared.resource('spark-tensorfold', SITE), name)
                self.assertTrue(host.get('spark_tensorfold'), name)

    def test_every_unit_role_has_the_template_unit_yaml_installs(self):
        """`claim` (`infra/unit.yml`) installs `infra/templates/<role>.service.j2` for a role in the map.

        The map is the list of roles a manifest may declare a service for; the template is what actually gets written
        to `/etc/systemd/system/`. A role added without its text fails inside the playbook, on the worker, after the
        claim has already been made — so the playbook refuses on the control side first, and this checks the pair.
        """
        play = yaml.safe_load((ROOT / 'infra/unit.yml').read_text())[0]
        for role in play['vars']['units']:
            with self.subTest(role=role):
                self.assertTrue((ROOT / f'infra/templates/{role}.service.j2').is_file(), role)
        steps = [task.get('name') for task in play['tasks']]
        self.assertIn('Require the unit text this role installs', steps)
        self.assertIn('Refuse a role that has no text', steps)

    def test_every_strata_engine_version_has_a_root_on_its_hosts(self):
        for path in STRATA:
            manifest = json.loads(path.read_text())
            if str(manifest.get('status', '')).startswith('retired'):
                continue
            for name in manifest['workers']:
                with self.subTest(path.name, host=name):
                    self.assertIn(manifest['source_version'], HOSTS[name].get('strata_trial_roots', {}))

    def test_a_retired_manifest_is_declared_by_nothing_that_could_start_it(self):
        declared_backends = {target.get('backend') for target in ROUTES['targets'].values()}
        pinned = {component.get('pin', {}).get('lock') for component in COMPONENTS}
        strata_host = next(h for h in HOSTS.values() if h.get('backend') == 'strata')
        for path in STRATA:
            manifest = json.loads(path.read_text())
            if not str(manifest.get('status', '')).startswith('retired'):
                continue
            with self.subTest(path.name):
                self.assertNotIn(path.stem, declared_backends)
                self.assertNotIn(f'backends/{path.name}', pinned)
                self.assertRaises(SystemExit, borrow.strata_root, strata_host, path.stem)

    def test_nothing_declares_a_singular_trial_root_or_retired_0213_engine(self):
        for path in STRATA:
            self.assertNotEqual(json.loads(path.read_text())['source_version'], '0.2.1.3', path.name)
        for name, host in HOSTS.items():
            self.assertNotIn('strata_trial_root', host, name)
        for name, target in ROUTES['targets'].items():
            if 'backend' in target:
                self.assertNotEqual(
                    json.loads((SITE / f"backends/{target['backend']}.json").read_text()).get('source_version'),
                    '0.2.1.3', name)

    def test_routes_name_a_known_node_alias_and_backend(self):
        aliases = [m['model_name'] for m in yaml.safe_load((SITE / 'gateway/config.yaml').read_text())['model_list']]
        self.assertIn(ROUTES['default'], ROUTES['targets'])
        for name, target in ROUTES['targets'].items():
            with self.subTest(name):
                self.assertIn(target['node'], HOSTS)
                self.assertIn(target['source_alias'], aliases)
                if 'backend' in target:
                    self.assertTrue((SITE / f"backends/{target['backend']}.json").exists())

    def test_the_fallback_route_is_not_on_a_borrowed_host(self):
        self.assertFalse(HOSTS[ROUTES['targets'][ROUTES['default']]['node']].get('borrowed'))


class TensorFoldPinTests(unittest.TestCase):
    def test_launch_and_worker_serve_the_pinned_image_and_checkpoint(self):
        for launch_path in sorted((SITE / 'backends').glob('*.launch.json')):
            stem = launch_path.name.removesuffix('.launch.json')
            manifest = json.loads((SITE / f'backends/{stem}.json').read_text())
            launch = json.loads(launch_path.read_text())
            self.assertIn(manifest['image']['id'], launch)
            self.assertIn(manifest['image']['id'], (ROOT / 'scripts/spark-tensorfold-worker.py').read_text())
            self.assertTrue(any(f"source={INVENTORY['vars']['project_root']}/{manifest['model']['directory']},"
                                in arg for arg in launch))
            args = manifest['build_args']
            self.assertEqual(args['TF_REF'], manifest['engine']['revision'])
            self.assertEqual(args['PATCHES_HASH'], manifest['recipe']['hash'])
            self.assertIn('@sha256:', args['BASE_IMAGE'])
            weights = [f for f in manifest['model']['files'] if f['path'].endswith('.safetensors')]
            self.assertTrue(weights)
            self.assertTrue(all(len(f.get('sha256', '')) == 64 for f in weights))


class PlaybookTests(unittest.TestCase):
    def test_host_playbooks_require_an_explicit_host(self):
        for path in PLAYBOOKS:
            plays = yaml.safe_load(path.read_text())
            if all(play['hosts'] != 'workers' or any('end_host' in str(t) for t in play['tasks']) for play in plays):
                continue
            with self.subTest(path.name):
                self.assertIn('tasks/require-limit.yml', str(plays[0]['tasks'][0]))

    def test_node_json_has_one_writer_that_two_plays_call(self):
        """T5.11: `node.json` has one shared writer in `infra/tasks/write-node-config.yml`."""
        shared = (ROOT / 'infra/tasks/write-node-config.yml').read_text()
        self.assertIn("'image': runtime_image", shared)
        self.assertIn('serve_port if serve_port is defined', shared)
        self.assertIn('node_before.content | b64decode | from_json).port', shared)
        writers = [path.name for path in PLAYBOOKS + TASK_FILES
                   if "dest: '{{ project_root }}/node.json'" in path.read_text()]
        self.assertEqual(writers, ['write-node-config.yml'])
        tasks = yaml.safe_load((ROOT / 'infra/node-config.yml').read_text())[0]['tasks']
        imported = [str(task) for task in tasks if 'import_tasks' in str(task)]
        self.assertTrue(any('write-node-config.yml' in text for text in imported), imported)
        self.assertTrue(any('tasks/require-limit.yml' in text for text in imported), imported)
        self.assertIn('systemctl is-active', str(tasks))
        self.assertIn('not existing.stat.exists', str(tasks))
        self.assertIn('Refuse a correction that reaches past the image', str(tasks))
        for path in ('infra/start.yml', 'infra/node-config.yml'):
            self.assertIn('tasks/write-node-config.yml', (ROOT / path).read_text())

    def test_the_prepare_playbook_chooses_by_the_backend_asked_for_and_not_by_the_host(self):
        text = (ROOT / 'infra/prepare.yml').read_text()
        self.assertIn("backend_id is not defined or backend_id == 'tf-1.1.4-qfn-mxfp8-nvfp4'", text)
        self.assertIn('prepare_tensorfold | bool', text)
        gate = yaml.safe_load((ROOT / 'infra/tasks/prepare-strata.yml').read_text())[0]['ansible.builtin.assert']['that']
        self.assertTrue(any('strata_trial_roots is defined' in line for line in gate), gate)

    def test_a_failed_preparation_is_shown_to_the_operator_and_only_then_resumed(self):
        tasks = yaml.safe_load((ROOT / 'infra/tasks/prepare-strata.yml').read_text())
        shown = [task for task in tasks if str(task.get('name', '')).startswith('Show what a preparation')]
        self.assertEqual(len(shown), 1, [task.get('name') for task in tasks])
        self.assertIn('saved', shown[0]['ansible.builtin.debug']['msg'])
        self.assertEqual([str(when) for when in [shown[0]['when']]], ['state.rc == 0'])
        read = [task for task in tasks if task.get('name') == 'Read the saved preparation stage'][0]
        self.assertEqual(read['ansible.builtin.set_fact']['saved'], '{{ state.stdout | from_json }}')
        started = [task for task in tasks if task.get('name') == 'Start the persistent preparation'][0]
        argv = started['ansible.builtin.command']['argv']
        self.assertIn("['--resume-failed'] if saved.stage | default('') == 'failed'", argv)

    def test_read_only_probes_also_run_in_check_mode(self):
        groups = [(path, play['tasks']) for path in PLAYBOOKS for play in yaml.safe_load(path.read_text())]
        for path, tasks in groups + [(path, yaml.safe_load(path.read_text())) for path in TASK_FILES]:
            for task in tasks_of(tasks):
                if task.get('changed_when') is False and 'ansible.builtin.command' in task and not task.get('no_log'):
                    if 'record' not in task['name'].lower():
                        with self.subTest(path.name, task=task['name']):
                            self.assertIs(task.get('check_mode'), False)

    def test_every_template_has_an_installer(self):
        sources = '\n'.join(p.read_text() for p in PLAYBOOKS + TASK_FILES)
        for template in (ROOT / 'infra/templates').glob('*.j2'):
            with self.subTest(template.name):
                self.assertIn(template.stem, sources)

    def test_playbooks_read_site_files_from_instance_root_not_parent_dir(self):
        for path in PLAYBOOKS + TASK_FILES:
            with self.subTest(path.name):
                self.assertIsNone(re.search(r'\.\./(?:backends|profiles|secrets)/', path.read_text()))
        prepare_strata = (ROOT / 'infra/tasks/prepare-strata.yml').read_text()
        self.assertIn('prepare-{{ manifest.id }}-state.json', prepare_strata)


if __name__ == '__main__':
    unittest.main()
