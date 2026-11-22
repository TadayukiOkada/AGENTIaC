"""T9.1: infra/components.yml, one test for each rule it must keep. No network: nothing here reads an upstream."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import components, declared, paths  # noqa: E402

SITE = paths.root()

HOSTS = {'control', 'worker-a'}
PYPI = {'id': 'litellm', 'kind': 'python', 'owner': 'project', 'hosts': ['control'],
        'source': {'type': 'pypi', 'package': 'litellm'}, 'pin': {'version': '1.0.0', 'lock': 'requirements.lock'},
        'policy': 'manual'}
NPM = {'id': 'pi', 'kind': 'agent', 'owner': 'project', 'hosts': ['control'],
       'source': {'type': 'npm', 'package': '@scope/pi'}, 'pin': {'version': '1.3.0', 'lock': 'package-lock.json'},
       'policy': 'notify'}
ENGINE = {'id': 'engine', 'kind': 'engine', 'owner': 'project', 'hosts': ['worker-a'],
          'source': {'type': 'github', 'repository': 'owner/engine'},
          'pin': {'version': '1.1.4', 'revision': 'a' * 40, 'lock': 'manifest.json'}, 'policy': 'manual'}
BASE = {'id': 'base', 'kind': 'base-image', 'owner': 'project', 'hosts': ['control'],
        'source': {'type': 'digest', 'image': 'ubuntu', 'tag': '24.04'},
        'pin': {'digest': 'sha256:' + 'b' * 64, 'lock': 'Dockerfile'},
        'policy': 'manual'}
DRIVER = {'id': 'driver', 'kind': 'driver', 'owner': 'vendor', 'hosts': ['worker-a'], 'source': {'type': 'observed'},
          'policy': 'frozen'}
PERSONAL = {'id': 'mine', 'kind': 'agent', 'owner': 'user', 'hosts': ['control'], 'source': {'type': 'observed'},
            'policy': 'frozen'}


class Rules(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'requirements.lock').write_text('litellm==1.0.0\nother==9.9.9\n')
        (self.root / 'package-lock.json').write_text(json.dumps({'packages': {
            'node_modules/@scope/pi': {'version': '1.3.0'}, 'node_modules/other': {'version': '2.0.0'}}}))
        (self.root / 'manifest.json').write_text(json.dumps({'version': '1.1.4', 'revision': 'a' * 40}))
        (self.root / 'Dockerfile').write_text('FROM ubuntu@sha256:' + 'b' * 64 + '\n')

    def problems(self, *entries):
        return components.problems(list(entries), HOSTS, self.root)

    def broken(self, entry, *path, value=None, drop=False):
        changed = copy.deepcopy(entry)
        place = changed
        for key in path[:-1]:
            place = place[key]
        if drop:
            del place[path[-1]]
        else:
            place[path[-1]] = value
        return changed

    def assertRefused(self, text, *entries):
        found = self.problems(*entries)
        self.assertTrue(any(text in problem for problem in found), found)


class SoundTests(Rules):
    def test_every_kind_of_entry_passes(self):
        self.assertEqual(self.problems(PYPI, NPM, ENGINE, BASE, DRIVER, PERSONAL), [])

    def test_a_ci_component_runs_on_no_host(self):
        self.assertEqual(self.problems(self.broken(ENGINE, 'hosts', value=[])), [])

    def test_frozen_is_allowed_on_a_project_component(self):
        frozen = self.broken(ENGINE, 'policy', value='frozen')
        self.assertEqual(self.problems(self.broken(frozen, 'gate', value='wait for T3.7')), [])

    def test_a_frozen_project_component_says_what_holds_it(self):
        self.assertRefused('says what holds it', self.broken(ENGINE, 'policy', value='frozen'))

    def test_an_empty_gate_says_nothing(self):
        self.assertRefused('gate', self.broken(PYPI, 'gate', value=''))

    def test_only_a_frozen_project_component_needs_a_gate(self):
        self.assertEqual(self.problems(PYPI, DRIVER, PERSONAL), [])


class ShapeTests(Rules):
    def test_owner_policy_and_kind_are_closed_lists(self):
        self.assertRefused("'somebody' is not one of", self.broken(DRIVER, 'owner', value='somebody'))
        self.assertRefused("'auto' is not one of", self.broken(ENGINE, 'policy', value='auto'))
        self.assertRefused("'tool' is not one of", self.broken(ENGINE, 'kind', value='tool'))

    def test_a_required_field_cannot_be_left_out(self):
        for field in ('id', 'kind', 'owner', 'hosts', 'source', 'policy'):
            self.assertRefused(f"'{field}' is a required property", self.broken(DRIVER, field, drop=True))

    def test_a_misspelt_field_is_refused(self):
        self.assertRefused('polcy', self.broken(DRIVER, 'polcy', value='frozen'))

    def test_a_version_written_as_a_yaml_number_is_refused(self):
        self.assertRefused('is not of type', self.broken(PYPI, 'pin', 'version', value=1.0))

    def test_a_pin_names_a_value_as_well_as_a_lock(self):
        self.assertRefused('pin', self.broken(BASE, 'pin', value={'lock': 'Dockerfile'}))

    def test_a_source_must_have_what_its_type_needs(self):
        self.assertRefused('source', self.broken(PYPI, 'source', value={'type': 'pypi'}))
        self.assertRefused('source', self.broken(PYPI, 'source', value={'type': 'ftp', 'package': 'x'}))

    def test_a_digest_source_names_the_tag_it_follows(self):
        self.assertRefused('source', self.broken(BASE, 'source', 'tag', drop=True))

    def test_a_name_that_will_become_a_url_cannot_leave_its_path(self):
        self.assertRefused('source', self.broken(PYPI, 'source', 'package', value='../secrets'))
        self.assertRefused('source', self.broken(NPM, 'source', 'package', value='a/../../b'))
        self.assertRefused('source', self.broken(ENGINE, 'source', 'repository', value='owner/repo/../../x'))
        self.assertRefused('source', self.broken(BASE, 'source', 'tag', value='24.04/../x'))
        self.assertRefused('source', self.broken(BASE, 'source', 'image', value='a/../../b'))

    def test_a_lock_stays_inside_the_checkout(self):
        self.assertRefused('does not match', self.broken(PYPI, 'pin', 'lock', value='/etc/passwd'))
        self.assertRefused('does not match', self.broken(PYPI, 'pin', 'lock', value='../secrets/gateway.env'))

    def test_a_short_revision_or_a_bare_digest_is_refused(self):
        self.assertRefused('does not match', self.broken(ENGINE, 'pin', 'revision', value='abc1234'))
        self.assertRefused('does not match', self.broken(BASE, 'pin', 'digest', value='b' * 64))


class RuleTests(Rules):
    def test_an_id_declared_twice(self):
        self.assertRefused('component litellm: declared twice', PYPI, PYPI)

    def test_a_host_the_inventory_does_not_have(self):
        self.assertRefused('component litellm: nowhere is not declared', self.broken(PYPI, 'hosts', value=['nowhere']))

    def test_a_lock_file_that_is_not_there(self):
        self.assertRefused('its lock gone.lock does not exist', self.broken(PYPI, 'pin', 'lock', value='gone.lock'))

    def test_a_vendor_or_user_component_is_frozen(self):
        self.assertRefused('policy must be frozen', self.broken(DRIVER, 'policy', value='notify'))
        self.assertRefused('policy must be frozen', self.broken(PERSONAL, 'policy', value='manual'))

    def test_a_vendor_or_user_component_has_no_pin(self):
        pin = {'version': '1.0.0', 'lock': 'requirements.lock'}
        self.assertRefused('so it has no pin', self.broken(DRIVER, 'pin', value=pin))
        self.assertRefused('so it has no pin', self.broken(PERSONAL, 'pin', value=pin))

    def test_a_project_component_is_pinned(self):
        self.assertRefused('a project component is pinned', self.broken(PYPI, 'pin', drop=True))


class DriftTests(Rules):
    """The pin and its lock must agree, or `ops update check` reports the wrong version with no error."""

    def test_a_python_pin_behind_its_lock(self):
        self.assertRefused('version 0.9.0 is not what requirements.lock holds',
                           self.broken(PYPI, 'pin', 'version', value='0.9.0'))

    def test_a_python_version_held_by_another_package_is_not_a_match(self):
        self.assertRefused('version 9.9.9 is not', self.broken(PYPI, 'pin', 'version', value='9.9.9'))

    def test_a_prefix_of_the_locked_version_is_not_a_match(self):
        self.assertRefused('version 1.0 is not', self.broken(PYPI, 'pin', 'version', value='1.0'))

    def test_an_npm_version_held_by_another_package_is_not_a_match(self):
        self.assertRefused('version 2.0.0 is not', self.broken(NPM, 'pin', 'version', value='2.0.0'))

    def test_a_revision_and_a_digest_that_moved(self):
        self.assertRefused('is not what manifest.json holds', self.broken(ENGINE, 'pin', 'revision', value='c' * 40))
        self.assertRefused('is not what Dockerfile holds', self.broken(BASE, 'pin', 'digest', value='sha256:' + 'c' * 64))

    def test_every_pinned_value_is_checked_not_just_the_first(self):
        self.assertRefused('version 9.9.9 is not', self.broken(ENGINE, 'pin', 'version', value='9.9.9'))


class ShippedFileTests(unittest.TestCase):
    def setUp(self):
        self.graph = declared.load()
        self.entries = {entry['id']: entry for entry in self.graph.components}

    def test_the_shipped_file_keeps_every_rule(self):
        self.assertEqual(components.problems(self.graph.components, set(self.graph.nodes), SITE), [])

    def test_a_broken_entry_fails_the_graph_check_that_make_check_runs(self):
        entry = copy.deepcopy(self.graph.components[0])
        entry['pin']['version'] = '0.0.1'
        self.graph.components = [entry]
        self.assertTrue(any('0.0.1' in problem for problem in declared.references(self.graph)))

    def test_the_personal_clis_are_observed_and_the_pinned_copies_are_not_them(self):
        if {'claude-user', 'codex-user'} <= set(self.entries):
            self.assertEqual({self.entries[name]['owner'] for name in ('claude-user', 'codex-user')}, {'user'})
            self.assertEqual({self.entries[name]['owner'] for name in ('claude-code', 'codex')}, {'project'})

    def test_drivers_and_jetpack_stay_frozen(self):
        for entry in self.graph.components:
            if entry['kind'] == 'driver' or entry['id'] in ('nvidia-driver', 'jetpack'):
                self.assertEqual(entry['policy'], 'frozen', entry['id'])

    def test_the_package_is_the_one_the_lock_file_names(self):
        if 'ansible' in self.entries:
            self.assertEqual(self.entries['ansible']['source']['package'], 'ansible-core')

    def test_the_recipe_that_carries_the_tensorfold_build_is_declared_apart_from_the_engine(self):
        """T9.11: an update of the TensorFold build starts from the recipe's HEAD, not from the engine's releases."""
        if 'tensorfold-recipe' in self.entries and 'tensorfold' in self.entries:
            recipe, engine = self.entries['tensorfold-recipe'], self.entries['tensorfold']
            manifest = json.loads((SITE / recipe['pin']['lock']).read_text())
            self.assertEqual((recipe['pin']['revision'], sorted(recipe['hosts'])),
                             (manifest['recipe']['revision'], sorted(manifest['workers'])))
            self.assertEqual(recipe['source']['repository'],
                             manifest['recipe']['repository'].removeprefix('https://github.com/').removesuffix('.git'))
            self.assertEqual(engine['source']['repository'],
                             manifest['engine']['repository'].removeprefix('https://github.com/').removesuffix('.git'))
            self.assertNotEqual(recipe['source']['repository'], engine['source']['repository'])
            self.assertEqual((recipe['kind'], recipe['policy']), ('engine', 'manual'))

    def test_it_is_a_list_of_data_and_not_a_playbook(self):
        data = yaml.safe_load((SITE / 'infra/components.yml').read_text())
        self.assertTrue(all('tasks' not in entry and 'hosts' in entry for entry in data))
        self.assertIn('infra/components.yml', (ROOT / 'Makefile').read_text().split('PLAYBOOKS =')[1].split('\n')[0])


if __name__ == '__main__':
    unittest.main()
