"""Lifecycle classification is conservative and never authorizes disk operations."""
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import backend_lifecycle, declared, identities, model_names, paths  # noqa: E402
from agentiac.management import releases  # noqa: E402
from agentiac.services import engine_catalog  # noqa: E402

SITE = paths.root()


def archived(**extra):
    return {'catalog_status': 'archived', 'reason': 'Retain measured pins.',
            'evidence': 'docs/storage-lifecycle-plan.md', **extra}


def ledger_entry(backend=None, **extra):
    return {**({'backend': backend} if backend else {}), 'class': 'persistent', 'form': 'full',
            'reason': 'Fixture retention decision.', **extra}


def graph():
    return SimpleNamespace(root=SITE, nodes={'worker-a': {}, 'worker-b': {}},
                           backends={'gyro': {'status': 'retired in free prose'}, 'old': {}, 'trial': {}},
                           backend_legacy_names={'old-gyro': 'gyro'},
                           backend_lifecycle_evidence={'docs/storage-lifecycle-plan.md': True},
                           backend_lifecycle={'version': 1, 'backends': {'old': archived()}},
                           retention={'worker-a': {'serving': [ledger_entry('gyro')]},
                                      'worker-b': {'rollback': [ledger_entry('gyro')],
                                                   'candidate': [ledger_entry('trial', retirement='declined')]}},
                           components=[], route={'version': 2, 'targets': {}, 'legacy_targets': {}})


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.graph = graph()

    def row(self, backend='gyro'):
        return engine_catalog.project(self.graph)['backends'][backend]

    def codes(self, backend=None):
        result = engine_catalog.project(self.graph)
        issues = result['issues'] if backend is None else result['backends'][backend]['issues']
        return {item['code'] for item in issues}

    def test_a_release_keeps_different_node_uses_and_does_not_parse_free_prose(self):
        row = self.row()
        self.assertEqual(row['roles'], ['default', 'rollback'])
        self.assertEqual({(a['node'], a['role']) for a in row['associations']},
                         {('worker-a', 'default'), ('worker-b', 'rollback')})
        self.assertFalse(row['history_only'])
        self.assertEqual(row['coverage'], 'complete')

    def test_evaluation_does_not_imply_retirement_permission(self):
        row = self.row('trial')
        self.assertEqual(row['roles'], ['evaluation'])
        self.assertEqual(row['associations'][0]['retirement_intent'], 'declined')
        self.graph.retention['worker-b']['candidate'][0]['retirement'] = 'candidate'
        self.assertEqual(self.row('trial')['associations'][0]['retirement_intent'], 'candidate')
        self.assertFalse(self.row('trial')['history_only'])

    def test_archived_backend_with_no_references_is_history(self):
        self.assertTrue(self.row('old')['history_only'])
        self.assertEqual(self.row('old')['coverage'], 'complete')

    def test_mixed_release_with_archived_and_retained_backends_stays_visible(self):
        result = engine_catalog.release_lifecycle(engine_catalog.project(self.graph), ['old', 'gyro'])
        self.assertFalse(result['history_only'])
        self.assertEqual(result['roles'], ['default', 'rollback'])
        self.assertTrue(engine_catalog.release_lifecycle(engine_catalog.project(self.graph), ['old'])['history_only'])

    def test_no_decision_is_declared_only_and_never_inferred_retired(self):
        self.graph.backend_lifecycle['backends'].clear()
        self.graph.backends['old']['status'] = 'retired yesterday'
        row = self.row('old')
        self.assertEqual(row['roles'], ['declared_only'])
        self.assertFalse(row['history_only'])
        self.assertEqual(row['coverage'], 'partial')

    def test_old_roots_without_sidecar_keep_everything_visible(self):
        self.graph.backend_lifecycle = None
        self.assertTrue(all(row['coverage'] == 'unknown' and not row['history_only']
                            for row in engine_catalog.project(self.graph)['backends'].values()))
        self.assertEqual(backend_lifecycle.problems(self.graph), [])

    def test_current_target_is_a_reference_not_a_default_use(self):
        self.graph.route['targets']['pick'] = {'node': 'worker-b', 'backend': 'trial'}
        self.graph.route['legacy_targets']['old-pick'] = 'pick'
        row = engine_catalog.project(self.graph, current='old-pick')['backends']['trial']
        self.assertEqual(row['roles'], ['evaluation'])
        self.assertEqual({ref['kind'] for ref in row['references']}, {'target', 'current'})

    def test_an_unknown_current_target_prevents_any_release_from_being_hidden(self):
        result = engine_catalog.project(self.graph, current='missing')
        self.assertFalse(engine_catalog.release_lifecycle(result, ['old'])['history_only'])

    def test_archival_conflicts_with_each_reference_kind(self):
        for kind in ('target', 'claim_default', 'component'):
            with self.subTest(kind=kind):
                self.graph = graph()
                if kind == 'target':
                    self.graph.route['targets']['pick'] = {'backend': 'old', 'node': 'worker-a'}
                elif kind == 'claim_default':
                    self.graph.nodes['worker-a']['strata_backend'] = 'old'
                else:
                    self.graph.components = [{'id': 'pin', 'hosts': ['worker-a'], 'pin': {'lock': 'backends/old.json'}}]
                self.assertFalse(self.row('old')['history_only'])
                self.assertIn('archived_in_use', self.codes('old'))
                self.assertTrue(backend_lifecycle.problems(self.graph))

    def test_unknown_catalogue_key_is_an_unassignable_error_not_a_hidden_row(self):
        self.graph.backend_lifecycle['backends']['missing'] = archived()
        result = engine_catalog.project(self.graph)
        self.assertIn('unknown_catalog_backend', self.codes())
        self.assertFalse(engine_catalog.release_lifecycle(result, ['old'])['history_only'])

    def test_defaults_resolve_legacy_backend_names_but_do_not_change_manifest(self):
        self.graph.nodes['worker-a']['strata_backend'] = 'old-gyro'
        self.assertEqual(self.row()['references'][0]['backend'], 'gyro')
        self.assertEqual(self.codes(), set())

    def test_missing_default_retention_is_visible_and_not_claimed_as_observed(self):
        self.graph.nodes['worker-a']['strata_default_backend'] = 'trial'
        self.assertIn('missing_retention', self.codes('trial'))
        row = self.row('trial')
        self.assertIn('default', row['roles'])
        self.assertTrue(all('loaded' not in a for a in row['associations']))

    def test_target_plus_default_without_ledger_is_incomplete_not_a_conflicting_ledger(self):
        self.graph.route['targets']['pick'] = {'node': 'worker-a', 'backend': 'trial'}
        self.graph.nodes['worker-a']['strata_backend'] = 'trial'
        self.assertIn('missing_retention', self.codes('trial'))
        self.assertNotIn('default_retention_conflict', self.codes('trial'))

    def test_a_profile_target_does_not_become_an_unknown_engine_issue(self):
        self.graph.profiles = {'cpu-profile': {}}
        self.graph.route['targets']['cpu'] = {'node': 'worker-a', 'backend': 'cpu-profile'}
        self.assertNotIn('unknown_backend', self.codes())
        self.assertTrue(self.row('old')['history_only'])

    def test_retired_references_need_review_even_without_an_archival_marker(self):
        self.graph.retention['worker-b']['retired'] = [ledger_entry('trial')]
        self.graph.route['targets']['pick'] = {'node': 'worker-b', 'backend': 'trial'}
        self.assertIn('retired_still_referenced', self.codes('trial'))

    def test_an_inventory_default_that_disagrees_with_retention_is_an_error(self):
        self.graph.nodes['worker-b']['strata_default_backend'] = 'gyro'
        self.assertIn('default_retention_conflict', self.codes('gyro'))

    def test_thin_rollback_stays_rollback(self):
        self.graph.retention['worker-b']['rollback'][0]['form'] = 'thin'
        self.assertEqual(self.row()['associations'][1]['form'], 'thin')
        self.assertFalse(self.row()['history_only'])

    def test_quarantine_cannot_be_hidden_even_when_marked_archived(self):
        self.graph.retention['worker-b']['quarantine'] = [ledger_entry('old')]
        self.assertFalse(self.row('old')['history_only'])
        self.assertIn('quarantined', self.row('old')['roles'])

    def test_conflicting_retention_states_remain_visible(self):
        self.graph.retention['worker-a']['retired'] = [ledger_entry('gyro')]
        self.assertIn('conflicting_retention_states', self.codes('gyro'))
        self.assertEqual(self.row()['coverage'], 'partial')

    def test_missing_or_uncaptured_archival_evidence_cannot_hide_a_release(self):
        self.graph.backend_lifecycle_evidence['docs/storage-lifecycle-plan.md'] = False
        self.assertIn('missing_evidence', self.codes('old'))
        self.assertFalse(self.row('old')['history_only'])
        self.graph.backend_lifecycle_evidence = {}
        self.assertIn('unverified_evidence', self.codes('old'))
        self.assertFalse(self.row('old')['history_only'])

    def test_named_history_joins_only_an_explicit_exact_reference(self):
        self.graph.retention['worker-b']['retired'] = [ledger_entry(name='old-isolated')]
        self.assertEqual(self.row('old')['associations'], [])
        self.graph.backend_lifecycle['backends']['old']['retention_refs'] = [
            {'node': 'worker-b', 'state': 'retired', 'handle': 'old-isolated'}]
        self.assertEqual(self.row('old')['roles'], ['retired'])
        self.assertTrue(self.row('old')['history_only'])
        self.graph.backend_lifecycle['backends']['old']['retention_refs'][0]['handle'] = 'missing'
        self.assertIn('unresolved_retention_ref', self.codes('old'))
        self.assertFalse(self.row('old')['history_only'])

    def test_one_ledger_entry_cannot_belong_to_two_backends(self):
        self.graph.retention['worker-b']['retired'] = [ledger_entry(name='isolated')]
        ref = {'node': 'worker-b', 'state': 'retired', 'handle': 'isolated'}
        self.graph.backend_lifecycle['backends'].update({
            'old': archived(retention_refs=[ref]), 'trial': archived(retention_refs=[ref])})
        self.assertIn('ambiguous_retention_ref', self.codes('old'))
        self.assertIn('ambiguous_retention_ref', self.codes('trial'))
        self.assertFalse(self.row('old')['history_only'])

    def test_a_sidecar_cannot_reassign_a_backend_bearing_ledger_entry(self):
        self.graph.backend_lifecycle['backends']['old']['retention_refs'] = [
            {'node': 'worker-b', 'state': 'retired', 'handle': 'trial'}]
        self.graph.retention['worker-b']['retired'] = [ledger_entry('trial')]
        self.assertIn('redundant_retention_ref', self.codes('old'))
        self.assertIn('conflicting_retention_ref', self.codes('trial'))
        self.assertFalse(self.row('old')['history_only'])

    def test_duplicate_ledger_addresses_are_not_silently_chosen(self):
        self.graph.retention['worker-a']['serving'].append(ledger_entry('gyro'))
        self.assertIn('duplicate_ledger_entry', self.codes('gyro'))

    def test_unknown_ledger_backend_is_preserved_as_an_issue(self):
        self.graph.retention['worker-b']['retired'] = [ledger_entry('missing')]
        self.assertIn('unknown_backend', self.codes())
        self.assertFalse(engine_catalog.release_lifecycle(engine_catalog.project(self.graph), ['old'])['history_only'])

    def test_projection_does_not_mutate_inputs_or_execute_commands(self):
        before = copy.deepcopy(self.graph)
        with patch('subprocess.run', side_effect=AssertionError('unexpected command')), \
                patch('subprocess.Popen', side_effect=AssertionError('unexpected process')), \
                patch.object(Path, 'is_file', side_effect=AssertionError('projection must use captured facts')):
            engine_catalog.project(self.graph)
        self.assertEqual(vars(self.graph), vars(before))


class MetadataTests(unittest.TestCase):
    def load(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'backends').mkdir()
            (root / backend_lifecycle.FILE).write_text(content)
            return backend_lifecycle.load(root)

    def test_missing_file_is_distinct_from_empty_or_invalid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(backend_lifecycle.load(tmp))
        for content in ('', '[broken', 'version: 7\nbackends: {}', 'version: 1\nbackends: []'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.load(content)

    def test_duplicate_yaml_keys_and_duplicate_refs_are_rejected(self):
        with self.assertRaises(ValueError):
            self.load('version: 1\nbackends: {}\nbackends: {}')
        ref = {'node': 'worker-b', 'state': 'retired', 'handle': 'isolated'}
        with self.assertRaises(ValueError):
            self.load(yaml.safe_dump({'version': 1, 'backends': {'old': archived(retention_refs=[ref, ref])}}))

    def test_evidence_cannot_reference_a_private_path_or_parent(self):
        for evidence in ('private/key.md', 'docs/../private/key.md', '/tmp/key.md'):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.load(yaml.safe_dump({'version': 1, 'backends': {'old': archived(evidence=evidence)}}))

    def test_unknown_fields_and_unsafe_handles_are_not_accepted(self):
        for extra in ({'loaded': True}, {'retention_refs': [{'node': 'worker-b', 'state': 'retired', 'handle': '/tmp/x'}]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                backend_lifecycle.validate({'version': 1, 'backends': {'old': archived(**extra)}})

    def test_missing_evidence_is_a_strict_declaration_problem(self):
        g = graph()
        g.backend_lifecycle['backends']['old']['evidence'] = 'docs/missing.md'
        self.assertTrue(any('evidence docs/missing.md' in item for item in backend_lifecycle.problems(g)))


class ShippedGraphTests(unittest.TestCase):
    def test_shipped_lifecycle_decisions_and_mixed_uses(self):
        g = declared.load(SITE)
        self.assertEqual(declared.references(g), [])
        view = engine_catalog.project(g)
        rows = view['backends']
        self.assertEqual(set(rows), set(g.backends))
        if g.backend_lifecycle is None:
            self.assertTrue(all(row['coverage'] == 'unknown' and not row['history_only'] for row in rows.values()))
        else:
            self.assertTrue(any(row['history_only'] for row in rows.values()))
            self.assertTrue(any(not row['history_only'] for row in rows.values()))
            self.assertTrue(any({'default', 'rollback'} <= set(row['roles']) for row in rows.values()))

    def test_display_metadata_cannot_change_identity_names_or_gateway_render(self):
        g = declared.load(SITE)
        baseline = copy.deepcopy(g)
        baseline.backend_lifecycle = None
        for target in g.route['targets']:
            with self.subTest(target=target):
                self.assertEqual(identities.compute_target(g, target), identities.compute_target(baseline, target))
                self.assertEqual(model_names.describe(g, target), model_names.describe(baseline, target))
        sys.path.insert(0, str(ROOT / 'scripts'))
        import route
        before = route.render(g.gateway_config, g.route, g.route['default'])
        engine_catalog.project(g)
        self.assertEqual(route.render(g.gateway_config, g.route, g.route['default']), before)
        self.assertEqual(g.backends, baseline.backends)

    def test_sidecar_semantic_validation_is_part_of_make_check(self):
        g = declared.load(SITE)
        if g.backend_lifecycle is None:
            g.backend_lifecycle = {'version': 1, 'backends': {}}
        g.backend_lifecycle['backends']['missing'] = archived()
        self.assertTrue(any('catalogue key missing' in p for p in declared.references(g)))

    def test_lifecycle_schema_is_shipped_in_immutable_releases(self):
        self.assertIn('infra/backend-lifecycle.schema.json', releases.CODE_DATA)
        self.assertIn(ROOT / 'infra/backend-lifecycle.schema.json', releases.code_files(ROOT))


if __name__ == '__main__':
    unittest.main()
