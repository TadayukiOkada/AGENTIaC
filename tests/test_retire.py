"""The gate (T5.2): every refusal `ops retire`, `ops thin` and `ops restore` is supposed to make.

The gate is a function of what the declaration says and what the host measured, so these tests are a table of
those two inputs and the row the report has to print. A refusal is asserted by its check id and the word in
its evidence, never by a whole sentence, so a rewording is not a test failure while a missing refusal is.
"""
import contextlib
import datetime
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

from agentiac import declared, errors, retire, retention  # noqa: E402

GIB = 1024**3


def day(year, month, dom):
    return datetime.date(year, month, dom).isoformat()


def ts(year, month, dom, hour=0, minute=0, second=0):
    return datetime.datetime(year, month, dom, hour, minute, second, tzinfo=datetime.timezone.utc).isoformat()


# A host with two roots in one declared area, a staging directory, one area out of scope.
LEDGER = {
    'serving': [{'backend': 'ud-0.2.11', 'path': '/srv/worker/agentiac/strata-0.2.11',
                 'class': 'persistent', 'form': 'full', 'reason': 'what claim worker-a resolves to'}],
    'rollback': [{'backend': 'gyro-0.2.8', 'path': '/srv/worker/agentiac/strata-0.2.8',
                  'class': 'persistent', 'form': 'full', 'confirmed': day(2026, 11, 7),
                  'evidence': 'artifacts/lifecycle/gyro-0.2.8.json', 'reason': 'the rollback'}],
    'candidate': [{'backend': 'gyro-0.2.11', 'path': '/srv/worker/agentiac/strata-0.2.11-gyro',
                   'class': 'persistent', 'form': 'full', 'retirement': 'candidate',
                   'reason': 'measured and not served'}],
    'quarantine': [{'name': 'gyro-0.2.11-isolated', 'path': '/srv/worker/agentiac/retired/strata-0.2.11-gyro',
                    'class': 'persistent', 'reason': f'isolated {day(2026, 11, 7)}'}],
    'retired': [{'name': 'worker-a-strata', 'path': '/srv/worker/agentiac/strata', 'class': 'persistent',
                 'reason': f'deleted {day(2026, 11, 6)}'}],
    'staging': [{'path': '/srv/worker/agentiac/retired', 'class': 'persistent', 'reason': '§5.5'}],
    'areas': [{'path': '/srv/worker/agentiac', 'class': 'persistent', 'reason': 'where our roots live'},
              {'path': '/srv/worker/.cache/huggingface', 'class': 'user-owned', 'reason': '§3.4'}],
}
AREAS = {'serving': [], 'rollback': [], 'candidate': [], 'quarantine': [], 'retired': [],
         'staging': [], 'areas': []}
ROOTS = ['/srv/worker/agentiac/strata-0.2.11', '/srv/worker/agentiac/strata-0.2.8']
ROUTE = {'targets': {'worker-a-strata-0.2.11-qfn-gyro-m-tq2_0-128k': {'node': 'worker-a', 'backend': 'gyro-0.2.11'},
                     'strata-worker-a-ud-0.2.11-256k': {'node': 'worker-a', 'backend': 'ud-0.2.11'},
                     'strata-worker-b-other': {'node': 'worker-b', 'backend': 'gyro-0.2.11'}}}
NODES = {'worker-a': {'backend': 'strata', 'ansible_host': 'worker-a.example',
                      'strata_root': '/srv/worker/agentiac/strata-0.2.11',
                      'strata_trial_roots': {'gyro-0.2.8': '/srv/worker/agentiac/strata-0.2.8',
                                             'gyro-0.2.11': '/srv/worker/agentiac/strata-0.2.11-gyro'},
                      'strata_default_backend': 'ud-0.2.11'}}
WEIGHTS = {'q8_0.gguf': {'path': 'models/q8_0.gguf', 'size': 20 * GIB, 'sha256': 'a' * 64},
           'q4_k.gguf': {'path': 'models/q4_k.gguf', 'size': 10 * GIB, 'sha256': 'b' * 64}}


def ledger(**over):
    """A copy of the fixture ledger with a state replaced."""
    one = {state: list(entries) for state, entries in LEDGER.items()}
    one.update({state: list(entries) for state, entries in over.items()})
    return one


def measured(path='/srv/worker/agentiac/strata-0.2.11-gyro', **over):
    """What `retire-worker.py measure` answers, in the shape it answers it (S §5.1's read)."""
    facts = {'path': path, 'exists': True, 'symlink': False, 'resolved': path, 'mount_point': False,
             'filesystem': '/srv/worker', 'device': 66306, 'size_bytes': 94 * GIB, 'entries': 20614,
             'shared_files': 0, 'shared_bytes': 0, 'free_bytes': 198 * GIB, 'openers': {'processes': 0,
             'method': 'sudo lsof +D'}, 'units': [], 'front_names_path': False, 'front_residue': [],
             'front_running': None,
             'staging_path': '/srv/worker/agentiac/retired', 'staging_device': 66306}
    facts.update(over)
    return facts


def weights(states):
    """`weights` as the worker reports them, with one state per pinned file."""
    out = {}
    for name, pin in WEIGHTS.items():
        over = states.get(name, {})
        entry = dict(pin, path=f'/root/data/models/{pin["path"]}', nlink=1, size_bytes=pin['size'],
                     state='identical')
        entry.update(over)
        if entry['nlink'] != 1 and 'state' not in over:
            entry['state'] = 'shared-inode'   # what the worker reports for a weight with two names
        out[name] = entry
    return out


NO_READ = object()   # what the command passes on when the host did not answer at all


def run(stage='check', handle='gyro-0.2.11', target_state='candidate', entry=None, refs=None, facts=None,
        record=None, evidence=True, roots=None, ledger_host=None):
    """The gate's answer for one stage of one ledger line."""
    one = ledger_host or ledger()
    line = entry
    if line is None and target_state is not None:
        line = next((e for e in one[target_state] if retention.handle(e) == handle), None)
    target = retire.target(one, target_state, line, 'worker-a') if line is not None else \
        retire.target(one, None, {'name': handle, 'reason': ''}, 'worker-a')
    read = measured() if facts is None else (None if facts is NO_READ else facts)
    if refs is None:
        refs = retire.references(NODES, ROUTE, 'worker-a-strata-0.2.11-qfn-gyro-m-tq2_0-128k', 'worker-a', handle)
    gates = retire.gates(stage, target, one, refs, read, record, evidence, ROOTS if roots is None else roots)
    return {gate['id']: gate for gate in gates}, gates


def status(gates, check_id):
    """A gate's status, or `ok` for a check the gate had no reason to raise."""
    return gates.get(check_id, {}).get('status', retire.OK)


class LedgerGateTests(unittest.TestCase):
    def test_a_line_the_ledger_does_not_carry_is_refused_before_anything_is_measured(self):
        gates, _ = run(handle='never-named', target_state=None)
        self.assertEqual(status(gates, 'ledger'), retire.REFUSE)
        self.assertEqual(list(gates), ['ledger'])

    def test_what_serves_is_never_a_target(self):
        gates, _ = run(target_state='serving', handle='ud-0.2.11')
        self.assertEqual(status(gates, 'serving'), retire.REFUSE)

    def test_a_target_that_still_routes_there_is_refused_until_the_route_goes_first(self):
        # The §5.3 order, as a refusal: the ledger line stays `candidate` while the route names the backend.
        gates, _ = run(facts=measured())
        self.assertEqual(status(gates, 'serving'), retire.REFUSE)
        self.assertIn('remove the reference in a commit first', gates['serving']['evidence'])

    def test_a_claim_with_no_backend_resolves_to_it_too(self):
        # `claim <host>` with no --backend resolves through inventory's strata_default_backend: the same fact as
        # the ledger's `serving` line, read a second way (§4.3).
        refs = retire.references(NODES, ROUTE, 'none', 'worker-a', 'ud-0.2.11')
        gates, _ = run(handle='ud-0.2.11', target_state='rollback',
                       entry={'backend': 'ud-0.2.11', 'path': '/srv/worker/agentiac/strata-0.2.11',
                              'class': 'persistent', 'reason': 'x'}, refs=refs)
        self.assertEqual(status(gates, 'serving'), retire.REFUSE)
        self.assertIn('claim default', gates['serving']['evidence'])

    def test_the_front_of_an_ondemand_claim_is_a_third_way_to_serve(self):
        gates, _ = run(refs=[], facts=measured(front_names_path=True,
                                               front_running='/srv/worker/agentiac/strata-0.2.11-gyro/ondemand'))
        self.assertEqual(status(gates, 'serving'), retire.REFUSE)
        self.assertIn('released first', gates['serving']['evidence'])

    def test_the_residue_of_a_claim_that_moved_is_said_not_refused(self):
        # A root can still hold the config of an earlier claim while the host's front runs another root's proxy.
        # That root serves nothing, so the line has to say the residue out loud rather than refuse on it.
        gates, _ = run(refs=[], facts=measured(front_names_path=False, front_running='/elsewhere/ondemand',
                                               front_residue=['/srv/worker/agentiac/strata-0.2.11-gyro/ondemand/config.json']))
        self.assertEqual(status(gates, 'serving'), retire.OK)
        self.assertIn('residue', gates['serving']['evidence'])
        self.assertIn('/elsewhere/ondemand', gates['serving']['evidence'])

    def test_a_front_the_host_could_not_be_asked_about_is_unknown_when_the_root_holds_one(self):
        gates, _ = run(refs=[], facts=measured(front_residue=['/x/ondemand/config.json'],
                                               front_error='systemctl cat agentiac-ondemand exited 1'))
        self.assertEqual(status(gates, 'serving'), retire.UNKNOWN)
        self.assertIn('could not say', gates['serving']['evidence'])

    def test_a_root_with_no_front_residue_needs_no_answer_from_the_host(self):
        gates, _ = run(refs=[], facts=measured(front_running='/elsewhere/ondemand'))
        self.assertEqual(status(gates, 'serving'), retire.OK)

    def test_a_reference_on_another_host_does_not_refuse_this_one(self):
        gates, _ = run(refs=[])
        self.assertEqual(status(gates, 'serving'), retire.OK)

    def test_references_are_read_for_the_backend_this_line_names_and_the_host_being_asked(self):
        found = retire.references(NODES, ROUTE, 'worker-a-strata-0.2.11-qfn-gyro-m-tq2_0-128k',
                                  'worker-a', 'gyro-0.2.11')
        self.assertEqual([entry['name'] for entry in found], ['worker-a-strata-0.2.11-qfn-gyro-m-tq2_0-128k'])
        self.assertTrue(found[0]['current'])
        on_worker_b = retire.references(NODES, ROUTE, 'none', 'worker-b', 'gyro-0.2.11')
        self.assertEqual([entry['name'] for entry in on_worker_b], ['strata-worker-b-other'])
        self.assertEqual(retire.references(NODES, ROUTE, 'none', 'worker-a', 'something-else'), [])


class StageMatrixTests(unittest.TestCase):
    def test_the_states_are_a_one_way_arrow(self):
        # candidate -> quarantine -> retired, and a rollback may be thinned or put back (§3.1).
        allowed = {('candidate', 'quarantine'): retire.OK, ('candidate', 'purge'): retire.REFUSE,
                   ('candidate', 'thin'): retire.REFUSE, ('quarantine', 'purge'): retire.OK,
                   ('quarantine', 'quarantine'): retire.REFUSE, ('rollback', 'thin'): retire.OK,
                   ('rollback', 'quarantine'): retire.OK, ('retired', 'purge'): retire.REFUSE,
                   ('staging', 'purge'): retire.REFUSE, ('areas', 'quarantine'): retire.REFUSE,
                   ('serving', 'quarantine'): retire.REFUSE}
        for (state, stage), want in allowed.items():
            entry = {'name': 'x', 'path': LEDGER[state][0]['path'] if LEDGER[state] else None,
                     'class': 'persistent', 'reason': 'x', 'form': 'full',
                     **({'retirement': 'candidate'} if state == 'candidate' else {})}
            gates, _ = run(stage=stage, handle='x', target_state=state, entry=entry, refs=[],
                           facts=measured(entry['path'] or '/srv/worker/agentiac/retired/x'))
            self.assertEqual(status(gates, 'stage'), want,
                             f'{state} --{stage}: {gates.get("stage", {}).get("evidence", "no stage refusal")}')

    def test_check_is_allowed_for_every_state_because_it_changes_nothing(self):
        for state in ('serving', 'rollback', 'candidate', 'quarantine', 'retired', 'staging', 'areas'):
            entry = {'name': 'x', 'path': LEDGER[state][0]['path'] if LEDGER[state] else None,
                     'class': 'persistent', 'reason': 'x'}
            gates, _ = run(stage='check', handle='x', target_state=state, entry=entry, refs=[],
                           facts=measured(entry['path'] or '/srv/worker/agentiac/retired/x'))
            self.assertNotEqual(gates.get('stage', {}).get('status'), retire.REFUSE, state)

    def test_quarantine_needs_the_candidate_mark(self):
        entry = {'backend': 'gyro-0.2.11', 'path': '/srv/worker/agentiac/strata-0.2.11-gyro',
                 'class': 'persistent', 'form': 'full', 'reason': 'x'}  # candidate without `retirement:`
        gates, _ = run(stage='quarantine', entry=entry, refs=[])
        self.assertEqual(status(gates, 'candidate'), retire.REFUSE)

    def test_purge_only_deletes_under_the_staging_directory_this_host_names(self):
        entry = {'name': 'x', 'path': '/srv/worker/agentiac/strata-0.2.11-gyro', 'class': 'persistent',
                 'reason': 'x'}
        gates, _ = run(stage='purge', handle='x', target_state='quarantine', entry=entry, refs=[],
                       facts=measured(entry['path']), record={'path': entry['path'], 'quarantined_at': 'x'})
        self.assertEqual(status(gates, 'stage'), retire.REFUSE)
        self.assertIn('never a live one', gates['stage']['evidence'])


class PathGateTests(unittest.TestCase):
    def line(self, path):
        return {'backend': 'gyro-0.2.11', 'path': path, 'class': 'persistent', 'form': 'full',
                'retirement': 'candidate', 'reason': 'x'}

    def test_a_relative_path_or_one_with_a_dotdot_is_refused(self):
        for path in ('agentiac/strata', '/srv/worker/../worker/agentiac/strata'):
            gates, _ = run(stage='quarantine', entry=self.line(path), refs=[], facts=measured(path))
            self.assertEqual(status(gates, 'path'), retire.REFUSE, path)

    def test_a_path_that_is_not_a_declared_root_nor_under_staging_is_refused(self):
        path = '/srv/worker/somewhere-else/root'
        gates, _ = run(stage='quarantine', entry=self.line(path), refs=[], facts=measured(path))
        self.assertEqual(status(gates, 'path'), retire.REFUSE)
        self.assertIn('neither a root', gates['path']['evidence'])

    def test_a_declared_root_passes_the_path_gate(self):
        path = '/srv/worker/agentiac/strata-0.2.8'
        gates, _ = run(stage='quarantine', entry=self.line(path), refs=[], facts=measured(path))
        self.assertEqual(status(gates, 'path'), retire.OK)

    def test_a_staging_path_passes_it_because_isolation_lives_there(self):
        path = '/srv/worker/agentiac/retired/strata-0.2.11-gyro'
        gates, _ = run(stage='purge', handle='gyro-0.2.11-isolated', target_state='quarantine', refs=[],
                       facts=measured(path), record={'path': path, 'quarantined_at': ts(2026, 11, 6)})
        self.assertEqual(status(gates, 'path'), retire.OK)

    def test_a_line_naming_no_path_is_refused(self):
        entry = {'backend': 'gyro-0.2.11', 'class': 'persistent', 'reason': 'x'}
        gates, _ = run(entry=entry, refs=[])
        self.assertEqual(status(gates, 'path'), retire.REFUSE)


class AreaGateTests(unittest.TestCase):
    def line(self, path):
        return {'name': 'x', 'path': path, 'class': 'persistent', 'form': 'full', 'retirement': 'candidate',
                'reason': 'x'}

    def test_a_path_inside_a_user_owned_area_is_refused(self):
        path = '/srv/worker/.cache/huggingface/x'
        gates, _ = run(entry=self.line(path), roots=ROOTS + [path], refs=[], facts=measured(path))
        self.assertEqual(status(gates, 'area'), retire.REFUSE)
        self.assertIn('out of scope', gates['area']['evidence'])

    def test_a_path_outside_every_area_the_ledger_calls_ours_is_refused(self):
        one = ledger(areas=[{'path': '/mnt/data', 'class': 'persistent', 'reason': 'x'}])
        path = '/srv/worker/agentiac/strata-0.2.11-gyro'
        gates, _ = run(entry=self.line(path), roots=[path], refs=[], facts=measured(path), ledger_host=one)
        self.assertEqual(status(gates, 'area'), retire.REFUSE)

    def test_a_host_with_no_persistent_area_refuses_everything(self):
        one = ledger(areas=[{'path': '/srv/worker', 'class': 'unknown', 'reason': 'x'}])
        path = '/srv/worker/agentiac/strata-0.2.11-gyro'
        gates, _ = run(entry=self.line(path), roots=[path], refs=[], facts=measured(path), ledger_host=one)
        self.assertEqual(status(gates, 'area'), retire.REFUSE)

    def test_the_class_of_the_line_itself_is_checked(self):
        gates, _ = run(entry={'name': 'x', 'path': '/srv/worker/agentiac/strata-0.2.11-gyro',
                              'class': 'user-owned', 'reason': 'x'}, refs=[])
        self.assertEqual(status(gates, 'class'), retire.REFUSE)


class StagingAndMountTests(unittest.TestCase):
    def test_quarantine_needs_a_directory_to_isolate_into(self):
        one = ledger(staging=[])
        path = '/srv/worker/agentiac/strata-0.2.11-gyro'
        gates, _ = run(stage='quarantine', refs=[], facts=measured(path, staging_path=None,
                                                                   staging_device=None), ledger_host=one,
                       roots=[path])
        self.assertEqual(status(gates, 'staging'), retire.REFUSE)

    def test_isolation_across_a_filesystem_is_refused_because_it_cannot_be_moved_back(self):
        path = '/mnt/data/agentiac-strata-0.2.11-gyro'
        entry = {'backend': 'gyro-0.2.11', 'path': path, 'class': 'persistent', 'form': 'full',
                 'retirement': 'candidate', 'reason': 'x'}
        one = ledger(staging=[{'path': '/mnt/data/retired', 'class': 'persistent', 'reason': 'x'}],
                     areas=[{'path': '/mnt/data', 'class': 'persistent', 'reason': 'x'}])
        gates, _ = run(stage='quarantine', entry=entry, refs=[], facts=measured(path, staging_device=99999,
                                                                                staging_path='/mnt/data/retired'),
                       ledger_host=one, roots=[path])
        self.assertEqual(status(gates, 'mount'), retire.REFUSE)
        self.assertIn('reversible', gates['mount']['evidence'])

    def test_a_mount_point_is_never_a_target(self):
        path = '/mnt/data'
        gates, _ = run(entry={'name': 'x', 'path': path, 'class': 'persistent', 'reason': 'x'},
                       refs=[], facts=measured(path, mount_point=True), roots=[path],
                       ledger_host=ledger(areas=[{'path': '/mnt/data', 'class': 'persistent', 'reason': 'x'}],
                                          staging=[{'path': '/mnt/data', 'class': 'persistent', 'reason': 'x'}]))
        self.assertEqual(status(gates, 'mount'), retire.REFUSE)

    def test_restore_refuses_when_the_copy_a_thin_left_is_on_another_filesystem(self):
        # §5.5: a thin's weights stay on the filesystem of the root they came from, which is what makes the
        # restore a copy rather than a cross-mount operation the gate cannot vouch for.
        entry = {'backend': 'gyro-0.2.8', 'path': '/srv/worker/agentiac/strata-0.2.8',
                 'class': 'persistent', 'form': 'thin', 'reason': 'x'}
        gates, _ = run(stage='restore', handle='gyro-0.2.8', target_state='rollback', entry=entry, refs=[],
                       facts=measured(entry['path'], staging_path='/mnt/data/retired', staging_device=777))
        self.assertEqual(status(gates, 'mount'), retire.REFUSE)

    def test_the_symlink_rules(self):
        path = '/srv/worker/agentiac/strata-0.2.11-gyro'
        gates, _ = run(refs=[], facts=measured(path, symlink=True, resolved='/mnt/elsewhere'))
        self.assertEqual(status(gates, 'symlink'), retire.REFUSE)
        gates, _ = run(refs=[], facts=measured(path, resolved='/mnt/elsewhere'))
        self.assertEqual(status(gates, 'symlink'), retire.REFUSE)


class InUseTests(unittest.TestCase):
    def test_a_process_holding_it_open_is_a_refusal(self):
        gates, _ = run(refs=[], facts=measured(openers={'processes': 3, 'method': 'sudo lsof +D'}))
        self.assertEqual(status(gates, 'in-use'), retire.REFUSE)

    def test_a_transient_unit_that_restarts_it_is_a_refusal(self):
        gates, _ = run(refs=[], facts=measured(units=['/run/systemd/system/strata-x.service']))
        self.assertEqual(status(gates, 'in-use'), retire.REFUSE)
        self.assertIn('release the claim', gates['in-use']['evidence'])

    def test_an_unread_in_use_is_unknown_not_ok(self):
        gates, _ = run(refs=[], facts=measured(openers={'unknown': 'sudo -n lsof failed', 'method': 'lsof'}))
        self.assertEqual(status(gates, 'in-use'), retire.UNKNOWN)

    def test_a_host_that_did_not_answer_is_unknown(self):
        gates, rows = run(refs=[], facts=NO_READ)
        self.assertEqual(status(gates, 'host'), retire.UNKNOWN)
        self.assertEqual(list(gates)[-1], 'host')  # nothing after a failed read pretends to have read anything


class PresentTests(unittest.TestCase):
    def test_a_missing_path_is_a_ledger_correction_not_a_deletion(self):
        gates, _ = run(refs=[], facts=measured(exists=False))
        self.assertEqual(status(gates, 'present'), retire.REFUSE)
        self.assertIn('the ledger is what needs correcting', gates['present']['evidence'])


class RecordAndWaitTests(unittest.TestCase):
    PATH = '/srv/worker/agentiac/retired/strata-0.2.11-gyro'

    def purge(self, record, entry=None):
        entry = entry or {'name': 'gyro-0.2.11-isolated', 'path': self.PATH, 'class': 'persistent', 'reason': 'x'}
        return run(stage='purge', handle='gyro-0.2.11-isolated', target_state='quarantine', entry=entry,
                   refs=[], facts=measured(self.PATH), record=record)

    def check(self, record, entry=None):
        entry = entry or {'name': 'gyro-0.2.11-isolated', 'path': self.PATH, 'class': 'persistent', 'reason': 'x'}
        return run(stage='check', handle='gyro-0.2.11-isolated', target_state='quarantine', entry=entry,
                   refs=[], facts=measured(self.PATH), record=record)

    def test_a_check_reads_the_wait_the_purge_will_hit(self):
        # The isolation age belongs to the state, so `--check` evaluates `wait` too.
        with mock.patch.object(retire, 'hours_since', return_value=11.9):
            gates, _ = self.check({'to': self.PATH, 'quarantined_at': ts(2026, 11, 9, 9, 1, 35)})
        self.assertEqual(status(gates, 'wait'), retire.REFUSE)
        self.assertIn('11.9 h', gates['wait']['evidence'])
        with mock.patch.object(retire, 'hours_since', return_value=25.0):
            gates, _ = self.check({'to': self.PATH, 'quarantined_at': ts(2026, 11, 8, 9, 1, 35)})
        self.assertEqual(status(gates, 'wait'), retire.OK)

    def test_a_check_names_a_quarantine_line_whose_record_is_missing_or_already_purged(self):
        gates, _ = self.check(None)
        self.assertEqual(status(gates, 'record'), retire.REFUSE)
        gates, _ = self.check({'to': self.PATH, 'quarantined_at': 'x', 'purged_at': ts(2026, 11, 7)})
        self.assertEqual(status(gates, 'record'), retire.REFUSE)

    def test_the_check_of_a_candidate_still_asks_nothing_of_a_record_it_has_no_reason_to_read(self):
        # `wait` is a gate of a `quarantine` state; a candidate rehearsal does not grow one.
        gates, _ = run(refs=[])
        self.assertNotIn('wait', gates)
        self.assertNotIn('record', gates)

    def test_purge_needs_the_record_the_quarantine_wrote(self):
        gates, _ = self.purge(None)
        self.assertEqual(status(gates, 'record'), retire.REFUSE)

    def test_a_record_naming_a_different_path_is_a_refusal(self):
        gates, _ = self.purge({'to': '/srv/worker/agentiac/retired/other', 'quarantined_at': 'x'})
        self.assertEqual(status(gates, 'record'), retire.REFUSE)
        self.assertIn('one path, named once', gates['record']['evidence'])

    def test_the_24_hours_are_counted(self):
        gates, _ = self.purge({'to': self.PATH, 'quarantined_at': 'not a time'})
        self.assertEqual(status(gates, 'wait'), retire.UNKNOWN)  # nothing is pretended about a stamp that fails
        with mock.patch.object(retire, 'hours_since', return_value=6.0):
            gates, _ = self.purge({'to': self.PATH, 'quarantined_at': ts(2026, 11, 7)})
            self.assertEqual(status(gates, 'wait'), retire.REFUSE)
            self.assertIn('--shorten-wait', gates['wait']['evidence'])
        with mock.patch.object(retire, 'hours_since', return_value=30.0):
            gates, _ = self.purge({'to': self.PATH, 'quarantined_at': ts(2026, 11, 6)})
            self.assertEqual(status(gates, 'wait'), retire.OK)

    def test_a_shortened_wait_is_allowed_when_the_reason_is_written_down(self):
        gates, _ = self.purge({'to': self.PATH, 'quarantined_at': ts(2026, 11, 7),
                               'wait_shortened': 'the owner watched it for 3 h and asked for it',
                               'purge_approved_at': ts(2026, 11, 7, 3)})
        self.assertEqual(status(gates, 'wait'), retire.OK)

    def test_a_path_already_purged_refuses(self):
        gates, _ = self.purge({'to': self.PATH, 'quarantined_at': 'x', 'purged_at': ts(2026, 11, 7)})
        self.assertEqual(status(gates, 'record'), retire.REFUSE)


class ThinningTests(unittest.TestCase):
    ENTRY = {'backend': 'gyro-0.2.8', 'path': '/srv/worker/agentiac/strata-0.2.8', 'class': 'persistent',
             'form': 'full', 'confirmed': day(2026, 11, 7), 'evidence': 'artifacts/lifecycle/gyro-0.2.8.json',
             'reason': 'the rollback'}

    def thin(self, facts=None, entry=None, evidence=True):
        entry = entry or self.ENTRY
        return run(stage='thin', handle='gyro-0.2.8', target_state='rollback', entry=entry, refs=[],
                   facts=facts or measured(entry['path'], weights=weights({})), evidence=evidence)

    def test_all_identical_thins(self):
        gates, _ = self.thin()
        self.assertEqual(status(gates, 'hashes'), retire.OK)
        self.assertEqual(status(gates, 'confirmed'), retire.OK)
        self.assertEqual(status(gates, 'inode'), retire.OK)

    def test_a_weight_the_serving_root_does_not_hold_identically_is_a_refusal(self):
        gates, _ = self.thin(facts=measured(self.ENTRY['path'], weights=weights({'q8_0.gguf': {'state':
                                                                                                 'hash-differs'}})))
        self.assertEqual(status(gates, 'hashes'), retire.REFUSE)
        self.assertIn('q8_0.gguf', gates['hashes']['evidence'])

    def test_a_weight_shared_by_inode_is_a_refusal_because_thinning_would_move_the_serving_copy(self):
        gates, _ = self.thin(facts=measured(self.ENTRY['path'], weights=weights({'q4_k.gguf': {'nlink': 2}})))
        self.assertEqual(status(gates, 'inode'), retire.REFUSE)

    def test_without_the_confirmed_line_nothing_judges_3_2_met(self):
        entry = dict(self.ENTRY)
        entry.pop('confirmed')
        gates, _ = self.thin(entry=entry)
        self.assertEqual(status(gates, 'confirmed'), retire.REFUSE)

    def test_a_thin_refused_above_is_not_asked_for_its_weight_plan(self):
        """A thin hashes every pinned weight on both sides, so a stage that is already refused does not pay for a
        plan. The `hashes` row says that, instead of reading like a failed read on the host (§5.7's 2 min 19 s)."""
        entry = dict(self.ENTRY)
        entry.pop('confirmed')
        gates, _ = self.thin(entry=entry, facts=measured(self.ENTRY['path']))
        self.assertEqual(status(gates, 'confirmed'), retire.REFUSE)
        self.assertEqual(status(gates, 'hashes'), retire.UNKNOWN)
        self.assertIn('not asked for its weight plan', gates['hashes']['evidence'])

    def test_a_thin_nobody_has_refused_yet_reports_an_unreadable_list_as_itself(self):
        gates, _ = self.thin(facts=measured(self.ENTRY['path']))
        self.assertEqual(status(gates, 'confirmed'), retire.OK)
        self.assertEqual(status(gates, 'hashes'), retire.UNKNOWN)
        self.assertIn('no prepare state or manifest weight list', gates['hashes']['evidence'])

    def test_a_confirmed_line_without_evidence_is_a_refusal(self):
        entry = dict(self.ENTRY)
        entry.pop('evidence')
        gates, _ = self.thin(entry=entry)
        self.assertEqual(status(gates, 'confirmed'), retire.REFUSE)

    def test_evidence_that_is_not_under_artifacts_is_a_refusal(self):
        gates, _ = self.thin(evidence=False)
        self.assertEqual(status(gates, 'confirmed'), retire.REFUSE)
        self.assertIn('artifacts/lifecycle/', gates['confirmed']['evidence'])

    def test_a_root_with_no_readable_weight_list_is_unknown_not_a_refusal(self):
        gates, _ = self.thin(facts=measured(self.ENTRY['path'], weights={}))
        self.assertEqual(status(gates, 'hashes'), retire.UNKNOWN)

    def test_thinning_past_the_rollback_spare_is_a_refusal(self):
        # 30 GiB free + 30 GiB of weights back: less than the 85.6 GiB of shared weights plus the 40 GiB floor.
        gates, _ = self.thin(facts=measured(self.ENTRY['path'], free_bytes=5 * GIB, weights=weights({})))
        self.assertEqual(status(gates, 'spare'), retire.REFUSE)
        self.assertIn('rolled back', gates['spare']['evidence'])

    def test_thinning_a_root_the_ledger_calls_thin_is_a_refusal(self):
        entry = dict(self.ENTRY, form='thin')
        gates, _ = self.thin(entry=entry)
        self.assertEqual(status(gates, 'form'), retire.REFUSE)

    def test_restore_needs_the_opposite_form_and_no_new_judgement(self):
        entry = dict(self.ENTRY, form='thin')
        gates, rows = run(stage='restore', handle='gyro-0.2.8', target_state='rollback', entry=entry, refs=[],
                          facts=measured(entry['path'], serving_device=66306))
        self.assertEqual(status(gates, 'form'), retire.OK)
        self.assertEqual(status(gates, 'confirmed'), retire.OK)
        self.assertNotIn('hashes', gates)


class ReclaimTests(unittest.TestCase):
    def test_shared_inodes_are_excluded_from_the_reclaim(self):
        target = retire.target(ledger(), 'candidate', LEDGER['candidate'][0], 'worker-a')
        # When `du` includes bytes shared by inode with another root, the reclaim is only what is really freed (F8).
        self.assertEqual(retire.reclaim_bytes(target, measured(shared_bytes=26 * GIB)), 68 * GIB)
        self.assertEqual(retire.reclaim_bytes(target, measured(shared_bytes=139 * GIB)), 0)
        self.assertEqual(retire.reclaim_bytes(target, measured()), 94 * GIB)
        self.assertEqual(retire.reclaim_bytes(target, measured(exists=False)), 0)

    def test_the_report_says_what_it_excluded(self):
        gates, _ = run(stage='quarantine', refs=[], facts=measured(shared_files=4, shared_bytes=139 * GIB))
        self.assertEqual(status(gates, 'shared-inodes'), retire.OK)
        self.assertIn('139.0 GiB', gates['shared-inodes']['evidence'])


# --- the command around the gate ---------------------------------------------------------------------------------------

spec = importlib.util.spec_from_file_location('retire_cmd', ROOT / 'scripts/retire.py')
retire_cmd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retire_cmd)


class FakeGraph:
    def __init__(self, retention=None, nodes=None, route=None, backends=None, group_vars=None):
        self.retention = retention or {'worker-a': ledger()}
        self.nodes = nodes or NODES
        self.route = route or ROUTE
        self.backends = backends or {}
        self.vars = dict(group_vars or {'runtime_image': 'agentiac-runtime:current0000'})


def plan(stage='check', host='worker-a', handle='gyro-0.2.11', reading=None, graph=None, records=None,
         image_read=None):
    """`plan` with the host read and the quarantine records given instead of taken."""
    graph = graph or FakeGraph()
    with mock.patch.object(retire_cmd, 'measured', return_value=reading if reading is not None else
                           {'/srv/worker/agentiac/strata-0.2.11-gyro': measured()}), \
         mock.patch.object(retire_cmd, 'image_reading',
                           return_value=CLEAN_READ if image_read is None else image_read), \
         mock.patch.object(retire_cmd.route, 'current', return_value='none'), \
         mock.patch.object(retire_cmd, 'record_for', return_value=records):
        return retire_cmd.plan(host, handle, stage, graph)


class CommandTests(unittest.TestCase):
    def test_a_candidate_the_route_still_names_is_refused_by_the_check_too(self):
        # A candidate root that the route still serves is refused by `--check` too.
        graph = FakeGraph(backends={'gyro-0.2.11': {}},
                          route={'targets': {'worker-a-strata-0.2.11-qfn-gyro-m-tq2_0-128k': {'node': 'worker-a',
                                                                                              'backend': 'gyro-0.2.11'}}})
        found = plan(handle='gyro-0.2.11', graph=graph)
        self.assertEqual(retire.refusals(found['gates'])[0]['id'], 'serving')
        self.assertIn('refused: serving', retire_cmd.lines(found)[-1])
        self.assertEqual(retire_cmd.exit_code(found), 1)

    def test_the_last_line_names_every_refusal_not_just_the_first(self):
        """A route target names the rollback and §3.2 is not recorded: both refusals must be printed."""
        entry = {'backend': 'gyro-0.2.8', 'path': '/srv/worker/agentiac/strata-0.2.8', 'class': 'persistent',
                 'form': 'full', 'reason': 'the rollback'}
        graph = FakeGraph(retention={'worker-a': ledger(rollback=[entry])}, backends={'gyro-0.2.8': {}},
                          route={'targets': {'worker-a-strata-0.2.8-qfn-gyro-m-tq2_0-128k': {'node': 'worker-a',
                                                                                             'backend': 'gyro-0.2.8'}}})
        with mock.patch.object(retire_cmd, 'manifest_path', return_value=Path('/tmp/manifest.json')):
            found = plan('thin', handle='gyro-0.2.8', graph=graph,
                         reading={'/srv/worker/agentiac/strata-0.2.8':
                                  measured('/srv/worker/agentiac/strata-0.2.8')})
        self.assertEqual([row['id'] for row in retire.refusals(found['gates'])], ['serving', 'confirmed'])
        self.assertIn('refused: serving, confirmed (2 refusals)', retire_cmd.lines(found)[-1])

    def test_the_check_of_a_quarantined_path_refuses_at_the_wait_and_says_so_last(self):
        """`ops retire <host> --name ...-isolated --check` refuses while the isolation record is under 24 h."""
        path = '/srv/worker/agentiac/retired/strata-0.2.11-gyro'
        reading = {path: measured(path)}
        records = {'to': path, 'quarantined_at': ts(2026, 11, 9, 9, 1, 35)}
        with mock.patch.object(retire, 'hours_since', return_value=11.9):
            found = plan('check', handle='gyro-0.2.11-isolated', reading=reading, records=records)
        self.assertEqual([row['id'] for row in retire.refusals(found['gates'])], ['wait'])
        self.assertIn('refused: wait (1 refusal)', retire_cmd.lines(found)[-1])
        self.assertEqual(retire_cmd.exit_code(found), 1)
        with mock.patch.object(retire, 'hours_since', return_value=25.0):
            found = plan('check', handle='gyro-0.2.11-isolated', reading=reading, records=records)
        self.assertEqual(retire_cmd.exit_code(found), 0, [g for g in found['gates'] if g['status'] != 'ok'])
        self.assertIn(f'what follows is: {errors.command()} ops retire worker-a --name gyro-0.2.11-isolated --purge',
                      retire_cmd.lines(found)[-1])

    def test_a_name_the_ledger_does_not_carry_names_the_names_it_does(self):
        found = plan(handle='no-such-root')
        evidence = retire.refusals(found['gates'])[0]['evidence']
        self.assertIn('names no line for no-such-root', evidence)
        self.assertIn('gyro-0.2.8', evidence)

    def test_an_image_candidate_is_read_and_reported_but_never_touched(self):
        """T5.9 replaced the blanket "this command deletes paths" refusal: the gate now reads what points at the
        tag, and the report hands over the one command without running it (§3.3, §5.6)."""
        one = ledger()
        one['candidate'] = [{'image': 'agentiac-tensorfold:old', 'class': 'persistent', 'form': 'full',
                             'retirement': 'candidate', 'reason': 'the rollback is an image (§3.3)'}]
        found = plan(handle='agentiac-tensorfold:old', graph=FakeGraph(retention={'worker-a': one}))
        self.assertEqual(retire.refusals(found['gates']), [])
        self.assertEqual(found['image_reading'], CLEAN_READ)
        step = retire_cmd.next_step(found)
        self.assertIn('docker rmi agentiac-tensorfold:old', step)
        self.assertIn('worker-a', step)

    def test_an_image_something_on_the_host_still_points_at_is_refused_by_the_check(self):
        one = ledger()
        one['candidate'] = [{'image': 'agentiac-tensorfold:old', 'class': 'persistent', 'form': 'full',
                             'retirement': 'candidate', 'reason': 'the rollback is an image (§3.3)'}]
        found = plan(handle='agentiac-tensorfold:old', graph=FakeGraph(retention={'worker-a': one}),
                     image_read={'references': ['/etc/systemd/system/agentiac-inference.service runs it'],
                                 'present': True, 'size': '24.5GB'})
        refusal = retire.refusals(found['gates'])[0]
        self.assertEqual(refusal['id'], 'references')
        self.assertIn('agentiac-inference.service', refusal['evidence'])
        self.assertIn('refused: references', retire_cmd.next_step(found))

    def test_a_host_that_is_not_in_the_inventory_is_refused_before_a_read_is_attempted(self):
        found = plan(host='nowhere', graph=FakeGraph(nodes={}))
        self.assertEqual(retire.refusals(found['gates'])[0]['id'], 'ledger')

    def test_following_names_the_stage_check_points_at(self):
        cases = {('quarantine', 'full', True): 'purge', ('rollback', 'full', True): 'thin',
                 ('rollback', 'full', False): 'quarantine', ('rollback', 'thin', False): 'restore',
                 ('candidate', 'full', False): 'quarantine'}
        for (state, form, confirmed), want in cases.items():
            entry = {'name': 'x', 'path': '/srv/worker/agentiac/strata-0.2.8', 'class': 'persistent',
                     'form': form, 'reason': 'x', **({'confirmed': day(2026, 11, 7),
                                                      'evidence': 'artifacts/lifecycle/x.json'} if confirmed else {})}
            one = ledger(**{state: [entry]})
            found = plan('check', handle='x', reading={'/srv/worker/agentiac/strata-0.2.8':
                                                       measured('/srv/worker/agentiac/strata-0.2.8')},
                         graph=FakeGraph(retention={'worker-a': one}))
            self.assertEqual(retire_cmd.following(found), want, f'{state} form {form} confirmed {confirmed}')

    def test_check_rehearses_a_thin_only_when_the_ledger_has_already_judged_it(self):
        entry = {'backend': 'gyro-0.2.8', 'path': '/srv/worker/agentiac/strata-0.2.8', 'class': 'persistent',
                 'form': 'full', 'reason': 'x'}
        one = ledger(rollback=[entry])
        graph = FakeGraph(retention={'worker-a': one}, backends={'gyro-0.2.8': {}})
        reading = {'/srv/worker/agentiac/strata-0.2.8': measured('/srv/worker/agentiac/strata-0.2.8')}
        with tempfile.TemporaryDirectory() as where:
            root = Path(where)
            (root / 'artifacts/lifecycle').mkdir(parents=True)
            (root / 'artifacts/lifecycle/x.json').write_text('{}')
            with mock.patch.object(retire_cmd, 'manifest_path', return_value=Path('/tmp/manifest.json')), \
                 mock.patch.object(retire_cmd, 'run_worker') as worker, \
                 mock.patch.object(retire_cmd, 'ROOT', root):
                found = plan('check', handle='gyro-0.2.8', reading=reading, graph=graph)
                self.assertIsNone(found['rehearsal'])      # no `confirmed` line: nothing to rehearse
                self.assertEqual(worker.call_count, 0)
                one['rollback'] = [dict(entry, confirmed=day(2026, 11, 7), evidence='artifacts/lifecycle/x.json')]
                worker.return_value = ({'weights': weights({}), 'bytes': 30 * GIB,
                                        'staging': '/srv/worker/agentiac/retired/strata-0.2.8-weights'}, None)
                found = plan('check', handle='gyro-0.2.8', reading=reading, graph=graph)
            self.assertEqual(found['rehearsal']['stage'], 'thin')
            self.assertEqual(worker.call_args[0][1], 'thin')
            self.assertTrue(worker.call_args[1]['check'])
            rehearsed = {gate['id']: gate['status'] for gate in found['rehearsal']['gates']}
            self.assertEqual({gate: rehearsed[gate] for gate in ('form', 'confirmed', 'hashes', 'spare', 'inode')},
                             {'form': 'ok', 'confirmed': 'ok', 'hashes': 'ok', 'spare': 'ok', 'inode': 'ok'})
            rows = retire_cmd.lines(found)
            self.assertTrue(any('rehearses --thin' in row for row in rows), rows)
            self.assertIn('ops thin worker-a --backend gyro-0.2.8', rows[-1])

    def test_a_worker_that_refuses_makes_the_check_exit_1(self):
        entry = {'backend': 'gyro-0.2.8', 'path': '/srv/worker/agentiac/strata-0.2.8', 'class': 'persistent',
                 'form': 'full', 'confirmed': day(2026, 11, 7), 'evidence': 'artifacts/lifecycle/x.json', 'reason': 'x'}
        one = ledger(rollback=[entry])
        graph = FakeGraph(retention={'worker-a': one}, backends={'gyro-0.2.8': {}})
        with tempfile.TemporaryDirectory() as where:
            root = Path(where)
            (root / 'artifacts/lifecycle').mkdir(parents=True)
            (root / 'artifacts/lifecycle/x.json').write_text('{}')
            with mock.patch.object(retire_cmd, 'manifest_path', return_value=Path('/tmp/manifest.json')), \
                 mock.patch.object(retire_cmd, 'ROOT', root), \
                 mock.patch.object(retire_cmd, 'run_worker', return_value=(None, 'hash-differs-from-pin')):
                found = plan('check', handle='gyro-0.2.8',
                             reading={'/srv/worker/agentiac/strata-0.2.8':
                                      measured('/srv/worker/agentiac/strata-0.2.8')}, graph=graph)
        self.assertIn('refused: rehearsal', retire_cmd.lines(found)[-1])
        self.assertEqual(retire_cmd.exit_code(found), 1)

    def test_the_manifest_a_thin_pins_against_is_named_by_the_backend(self):
        with tempfile.TemporaryDirectory() as where:
            root = Path(where)
            (root / 'backends').mkdir()
            (root / 'backends/gyro-0.2.8.json').write_text('{}')
            with mock.patch.object(retire_cmd, 'ROOT', root):
                self.assertEqual(retire_cmd.manifest_path('gyro-0.2.8'), root / 'backends/gyro-0.2.8.json')
                self.assertIsNone(retire_cmd.manifest_path('no-such-backend'))


class RecordTests(unittest.TestCase):
    def record(self, **over):
        return {'host': 'worker-a', 'handle': 'x-isolated', 'from': '/srv/worker/agentiac/strata-x',
                'to': '/srv/worker/agentiac/retired/strata-x', 'quarantined_at': ts(2026, 11, 6),
                **over}

    def test_a_record_is_one_file_per_retirement_and_is_readable_as_the_gate_reads_it(self):
        with tempfile.TemporaryDirectory() as where:
            root = Path(where)
            with mock.patch.object(retire_cmd, 'METADATA', root):
                saved = retire_cmd.write_record(self.record())
                again = retire_cmd.write_record(self.record(purged_at=ts(2026, 11, 7)))
                self.assertEqual(saved, again)
                written = json.loads(saved.read_text())
                self.assertEqual(written['purged_at'], ts(2026, 11, 7))
                self.assertEqual(sorted(p.name for p in saved.parent.glob('*.json')), ['strata-x.json'])
                found = retire_cmd.record_for('worker-a', '/srv/worker/agentiac/retired/strata-x')
                self.assertEqual(found['quarantined_at'], ts(2026, 11, 6))

    def test_a_record_is_0600_because_it_names_paths_on_the_owner_s_hosts(self):
        with tempfile.TemporaryDirectory() as where:
            with mock.patch.object(retire_cmd, 'METADATA', Path(where)):
                self.assertEqual(retire_cmd.write_record(self.record()).stat().st_mode & 0o777, 0o600)


class CredentialScanTests(unittest.TestCase):
    def archive(self, members):
        with tempfile.TemporaryDirectory() as where:
            root = Path(where)
            for name, body in members.items():
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(body)
            out = root / 'metadata.tgz'
            with tarfile.open(out, 'w:gz') as archive:
                for name in members:
                    archive.add(str(root / name), arcname=name)
            yield out

    def test_a_credential_shaped_member_is_reported_by_name_and_never_by_value(self):
        for archive in self.archive({'prepare-x-manifest.json': '{"model": "x"}',
                                     'watch/x.jsonl': '{"api_key": "ghp_' + 'verysecret"}'}):
            hits = retire_cmd.scan_credentials(archive)
            self.assertEqual(hits, ['watch/x.jsonl'])

    def test_a_clean_archive_passes(self):
        for archive in self.archive({'prepare-x-manifest.json': '{"files": ["q8_0.gguf"]}',
                                     'python-freeze.txt': 'torch==2.4', 'start-x-state.json': '{"unit": "x"}'}):
            self.assertEqual(retire_cmd.scan_credentials(archive), [])

    def test_a_member_that_is_not_text_is_still_scanned(self):
        # The scan is a safety net, not a classifier: a binary member with credential-shaped bytes in it is a hit.
        for archive in self.archive({'weights.bin': '\x00\x01\x02password: hunter2'}):
            self.assertEqual(retire_cmd.scan_credentials(archive), ['weights.bin'])

    def test_a_health_payload_reporting_that_a_key_is_required_is_not_a_credential(self):
        # `api_key: true` is the backend saying "this server requires a key", not a credential value.
        for archive in self.archive({'start-strata-x-claim-state.json':
                                     '{"stage": "ready", "health": {"status": "ok", "max_context": 262144, '
                                     '"api_key": true, "loaded": true, "service": "strata"}, '
                                     '"owned_cuda_process": true}'}):
            self.assertEqual(retire_cmd.scan_credentials(archive), [])

    def test_a_key_named_in_python_style_with_a_boolean_is_still_not_a_credential(self):
        for archive in self.archive({'x-state.json': 'api_key = None\npassword = False\n'}):
            self.assertEqual(retire_cmd.scan_credentials(archive), [])

    def test_a_key_name_followed_by_anything_else_is_still_a_credential(self):
        for body in ('"api_key": "hunter2"', '"api_key":sk-abcdefghij', 'api_key = Bearer-ghp_abcdefghijklmn',
                     '"password": "let me in"'):
            for archive in self.archive({'x.json': body}):
                self.assertEqual(retire_cmd.scan_credentials(archive), ['x.json'], body)


class ArchiveDirectionTests(unittest.TestCase):
    """`put` copies control -> host. A metadata archive has to come the other way, and that is a different call."""

    def scp(self, function, *argv):
        with mock.patch.object(retire_cmd.borrow.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout='', stderr='')
            function({'ansible_host': 'worker-a.example', 'ansible_user': 'operator'}, *argv)
            return run.call_args.args[0]

    def test_a_fetch_names_the_host_on_the_source_side(self):
        self.assertEqual(self.scp(retire_cmd.borrow.get, '/remote/x.tgz', '/local/x.tgz'),
                         ['scp', '-q', 'operator@worker-a.example:/remote/x.tgz', '/local/x.tgz'])

    def test_a_push_names_the_host_on_the_destination_side(self):
        self.assertEqual(self.scp(retire_cmd.borrow.put, '/local/x.tgz', '/remote/x.tgz'),
                         ['scp', '-q', '/local/x.tgz', 'operator@worker-a.example:/remote/x.tgz'])

    def test_the_archive_stage_brings_the_archive_home_before_it_scans_it(self):
        with tempfile.TemporaryDirectory() as where:
            records = Path(where) / 'records'
            crossed = {}

            def fetch(node, source, dest):
                crossed['source'], crossed['dest'] = source, dest
                with tarfile.open(dest, 'w:gz') as tar:          # a real archive, so the scan reads real bytes
                    tar.addfile(tarfile.TarInfo('prepare-x.json'), io.BytesIO(b'{"backend": "x"}'))
                return 0

            with mock.patch.object(retire_cmd.borrow, 'get', side_effect=fetch), \
                 mock.patch.object(retire_cmd.borrow, 'on_host',
                                   return_value=SimpleNamespace(returncode=0, stdout='', stderr='')), \
                 mock.patch.object(retire_cmd, 'record_dir', return_value=records):
                archive, hits = retire_cmd.archive_metadata({'ansible_host': 'worker-a.example'}, 'worker-a',
                                                            '/srv/worker/agentiac/strata-x',
                                                            '/srv/worker/agentiac/retired')
            self.assertEqual(crossed['source'], '/srv/worker/agentiac/retired/strata-x.metadata.tgz')
            self.assertEqual(crossed['dest'], str(records / 'strata-x.tgz'))
            self.assertEqual(archive, records / 'strata-x.tgz')
            self.assertEqual(hits, [])
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)


SUDO = subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode == 0


def quiet(function, *argv):
    """Run a stage, keeping its report out of the test output while keeping the return code."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        return function(*argv), buffer.getvalue()


@unittest.skipUnless(SUDO, 'the stages need `sudo -n`, which this machine does not give')
class StagingTests(unittest.TestCase):
    """The staging fix-up itself, with the host call patched.

    `ensure_staging` runs `sudo -n install -d -o <user> -g <user> -m 700`: the user is the node's declared
    `ansible_user`. The shape of the command and what a failure means are pinned here; the stages that use it
    run for real in `StageTests`.
    """

    def run_staging(self, staging, returncode=0, stderr=''):
        calls = []

        def on_host(node, argv, **kwargs):
            calls.append(list(argv))
            return SimpleNamespace(returncode=returncode, stderr=stderr)
        with mock.patch.object(retire_cmd.borrow, 'on_host', side_effect=on_host):
            ok, error = retire_cmd.ensure_staging({'ansible_host': 'worker-a.example',
                                                   'ansible_user': 'operator'}, staging)
        return ok, error, calls

    def test_the_staging_directory_is_created_owned_and_private(self):
        ok, error, calls = self.run_staging('/srv/worker/agentiac/retired')
        self.assertTrue(ok)
        self.assertEqual(error, '')
        self.assertEqual(calls, [['sudo', '-n', 'install', '-d', '-o', 'operator', '-g', 'operator', '-m', '700',
                                  '/srv/worker/agentiac/retired']])

    def test_a_staging_that_cannot_be_prepared_is_a_refusal_the_stage_reports(self):
        ok, error, _ = self.run_staging('/srv/worker/agentiac/retired', returncode=1,
                                        stderr='install: cannot create directory: Permission denied')
        self.assertFalse(ok)
        self.assertIn('Permission denied', error)


class StageTests(unittest.TestCase):
    """The stages that change a disk, run for real on a scratch tree under a temp directory on this machine.

    Nothing here reaches a declared host: the layout is a root, its staging directory and its area, built for the
    test, and `borrow.on_host` runs the commands locally because the node says `ansible_connection: local`. What
    it proves is the order §5.5 and §5.6 describe - metadata, then the move, then a record, then 24 h, then the
    deletion - with real inodes, a real `mv`, a real `rm` and a real `df`.
    """

    def layout(self, base):
        """A root, its staging directory and the area both live in, exactly as the ledger describes them."""
        area, staging = base / 'artifacts', base / 'retired'
        root = area / 't52-root'
        (root / 'watch').mkdir(parents=True)
        (root / 'data/models').mkdir(parents=True)
        (root / 'prepare-t52-manifest.json').write_text('{"backend": "t52"}')
        (root / 'python-freeze.txt').write_text('torch==2.4\n')
        (root / 'watch/t52.jsonl').write_text('{"tokens": 12}\n')
        (root / 'data/models/q8_0.gguf').write_bytes(b'x' * 4096)
        staging.mkdir()
        ledger_host = ledger(candidate=[{'name': 't52', 'path': str(root), 'class': 'persistent', 'form': 'full',
                                         'retirement': 'candidate', 'reason': 'the test root'}],
                             quarantine=[{'name': 't52-isolated', 'path': str(staging / 't52-root'),
                                          'class': 'persistent', 'reason': 'isolated by this test'}],
                             staging=[{'path': str(staging), 'class': 'persistent', 'reason': '§5.5'}],
                             areas=[{'path': str(area), 'class': 'persistent', 'reason': 'ours'}])
        graph = FakeGraph(retention={'local': ledger_host},
                          nodes={'local': {'backend': 'strata', 'ansible_connection': 'local',
                                           'strata_root': str(root)}},
                          route={'targets': {}})
        return root, staging, graph

    def setUp(self):
        self.where = tempfile.TemporaryDirectory()
        self.addCleanup(self.where.cleanup)
        self.base = Path(self.where.name)
        self.records = self.base / 'records'
        self.root, self.staging, self.graph = self.layout(self.base)
        # The quarantine record is written where the command writes it, so the patch has to outlive `plan`.
        for patch in (mock.patch.object(retire_cmd, 'METADATA', self.records),
                      mock.patch.object(retire_cmd.route, 'current', return_value='none'),
                      mock.patch.object(retire_cmd, 'ensure_staging', self.stage_locally)):
            patch.start()
            self.addCleanup(patch.stop)

    def stage_locally(self, node, staging):
        """The staging fix-up without naming a user, preserving mode 700 for local test runs."""
        done = subprocess.run(['install', '-d', '-m', '700', str(staging)], capture_output=True, text=True)
        return done.returncode == 0, done.stderr.strip()[:200]

    def plan(self, stage, handle):
        return retire_cmd.plan('local', handle, stage, self.graph)

    def quarantine(self, handle='t52'):
        return quiet(retire_cmd.quarantine, self.plan('quarantine', handle))

    def record(self):
        return json.loads(next(self.records.rglob('*.json')).read_text())

    def test_the_gates_pass_for_a_declared_candidate_and_the_check_changes_nothing(self):
        found = self.plan('check', 't52')
        self.assertEqual(retire_cmd.exit_code(found), 0, [g for g in found['gates'] if g['status'] != 'ok'])
        self.assertIn('gates pass', retire_cmd.lines(found)[-1])
        self.assertTrue(self.root.is_dir() and self.staging.is_dir() and list(self.root.iterdir()))
        self.assertFalse(self.records.exists())                     # a check writes no record
        self.assertEqual((self.root / 'data/models/q8_0.gguf').stat().st_size, 4096)

    def test_quarantine_saves_the_metadata_then_moves_the_root_and_records_it(self):
        code, report = self.quarantine()
        self.assertEqual(code, 0, report)
        moved = self.staging / 't52-root'
        self.assertFalse(self.root.exists())
        self.assertTrue(moved.is_dir() and (moved / 'data/models/q8_0.gguf').is_file())
        self.assertEqual((moved / 'data/models/q8_0.gguf').stat().st_size, 4096)   # the same inode, moved
        written = self.record()
        self.assertEqual((written['from'], written['to']), (str(self.root), str(moved)))
        self.assertEqual(next(self.records.rglob('*.json')).stat().st_mode & 0o777, 0o600)
        archived = Path(written['metadata'])
        self.assertTrue(archived.is_file() and archived.stat().st_mode & 0o777 == 0o600)
        with tarfile.open(archived) as tar:
            names = sorted(member.name.removeprefix('./') for member in tar.getmembers())
        self.assertEqual(names, ['prepare-t52-manifest.json', 'python-freeze.txt', 'watch/t52.jsonl'])  # §5.5
        self.assertNotIn('data/models/q8_0.gguf', names)                                              # not the weight
        self.assertIn('the ledger line to commit now', report)

    def test_the_command_itself_reaches_the_stage_it_just_cleared(self):
        with mock.patch.object(retire_cmd.declared, 'load', return_value=self.graph):
            code, report = quiet(retire_cmd.main, ['quarantine', 'local', '--name', 't52'])
        self.assertEqual(code, 0, report)
        self.assertTrue((self.staging / 't52-root').is_dir())
        self.assertFalse(self.root.exists())
        self.assertTrue(list(self.records.rglob('*.json')))                # the record, written by the real stage

    def test_the_check_after_a_quarantine_refuses_at_the_wait_a_purge_would_hit(self):
        self.assertEqual(self.quarantine()[0], 0)
        found = self.plan('check', 't52-isolated')
        self.assertEqual(retire.refusals(found['gates'])[0]['id'], 'wait')
        self.assertIn('refused: wait', retire_cmd.lines(found)[-1])
        self.assertEqual(retire_cmd.exit_code(found), 1)
        self.assertTrue((self.staging / 't52-root').is_dir())

    def test_purge_refuses_before_24_hours_and_leaves_the_root_where_it_is(self):
        self.assertEqual(self.quarantine()[0], 0)
        found = self.plan('purge', 't52-isolated')
        self.assertEqual(retire_cmd.exit_code(found), 1)
        self.assertEqual(retire.refusals(found['gates'])[0]['id'], 'wait')
        self.assertIn('24 h', retire.refusals(found['gates'])[0]['evidence'])
        self.assertTrue((self.staging / 't52-root').is_dir())

    def test_a_written_down_shortening_is_what_lets_the_deletion_run(self):
        self.assertEqual(self.quarantine()[0], 0)
        code, report = quiet(retire_cmd.purge, self.plan('purge', 't52-isolated'), 'the test watched it and asked')
        self.assertEqual(code, 0, report)
        self.assertFalse((self.staging / 't52-root').exists())
        written = self.record()
        self.assertEqual(written['wait_shortened'], 'the test watched it and asked')
        self.assertIn('purged_at', written)
        self.assertGreaterEqual(written['free_bytes_after'], written['free_bytes_before'])

    def test_a_root_whose_metadata_would_carry_a_key_is_not_moved_at_all(self):
        # §5.5: the archive is scanned before anything moves, and a hit stops the stage rather than filtering it.
        (self.root / 'prepare-t52-manifest.json').write_text('{"api_key": "ghp_' + 'x' * 24 + '"}')
        code, report = self.quarantine()
        self.assertEqual(code, 1, report)
        self.assertIn('credential-shaped text', report)
        self.assertTrue(self.root.is_dir())
        self.assertFalse((self.staging / 't52-root').exists())
        self.assertFalse(list(self.records.rglob('*.json')))
        # And the refusal keeps its own promise: the archive that failed the scan does not stay on control either.
        self.assertFalse(list(self.records.rglob('*.tgz')), report)


IMAGE_LEDGER = {
    'serving': [],
    'rollback': [{'image': 'agentiac-tensorfold:v114-new', 'class': 'persistent', 'form': 'full',
                  'reason': 'what the backend manifest pins'}],
    'candidate': [{'image': 'agentiac-runtime:oldoldold1', 'class': 'persistent', 'retirement': 'candidate',
                   'reason': 'the tag before the declared one'}],
    'quarantine': [],
    'retired': [{'image': 'agentiac-runtime:gonesoon0000', 'class': 'persistent',
                 'reason': f'removed on {day(2026, 11, 11)} by the command its candidate line named'}],
    'staging': [{'path': '/srv/worker/agentiac/retired', 'class': 'persistent', 'reason': '§5.5'}],
    'areas': [{'path': '/srv/worker/agentiac', 'class': 'persistent', 'reason': 'ours'}],
}
IMAGE_NODES = {'worker-a': {'backend': 'strata'}, 'worker-b': {'backend': 'strata'}}
IMAGE_VARS = {'runtime_image': 'agentiac-runtime:declared0000'}
CLEAN_READ = {'references': [], 'present': True, 'size': '102MB'}


def image_run(stage='check', handle='agentiac-runtime:oldoldold1', state='candidate', entry=None,
              reading=CLEAN_READ, declared_in=(), vars_=None, nodes=None):
    """The image gate's answer for one stage of one ledger line (T5.9)."""
    one = {key: list(value) for key, value in IMAGE_LEDGER.items()}
    if entry is not None:
        one['candidate'] = [entry]
    line = next((e for e in one[state] if retention.handle(e) == handle), None) if state else None
    target = retire.target(one, state, line, 'worker-a') if line is not None else \
        retire.target(one, None, {'name': handle, 'reason': ''}, 'worker-a')
    gates = retire.image_gates(stage, target, IMAGE_NODES if nodes is None else nodes,
                               CLEAN_READ if reading is CLEAN_READ else reading, declared_in=declared_in,
                               graph_vars=IMAGE_VARS if vars_ is None else vars_)
    return {gate['id']: gate for gate in gates}, gates


class ImageGateTests(unittest.TestCase):
    """T5.9: the gate an image retirement uses. It decides *whether*, and reports the one command without running it."""

    def test_a_candidate_with_nothing_pointing_at_it_passes_and_names_the_command(self):
        gates, _ = image_run()
        for check_id in ('ledger', 'declaration', 'references', 'present'):
            self.assertEqual(status(gates, check_id), retire.OK)
        self.assertIn('retirement: candidate', gates['ledger']['evidence'])

    def test_the_next_step_reports_the_removal_and_does_not_run_it(self):
        gates, ordered = image_run()
        found = {'host': 'worker-d', 'handle': 'agentiac-runtime:oldoldold1', 'stage': 'check',
                 'gates': ordered, 'target': {'image': 'agentiac-runtime:oldoldold1', 'backend': None},
                 'facts': {'ansible_host': 'worker-d.example'}}
        text = retire_cmd.next_step(found)
        self.assertIn('docker rmi agentiac-runtime:oldoldold1', text)
        self.assertIn('this script does not run', text)

    def test_an_image_has_no_quarantine_and_no_purge_because_it_is_not_a_directory(self):
        for stage in ('quarantine', 'purge', 'thin'):
            gates, _ = image_run(stage=stage)
            self.assertEqual(status(gates, 'stage'), retire.REFUSE)
            self.assertIn('one stage, --check', gates['stage']['evidence'])

    def test_a_line_without_the_candidate_mark_is_refused_however_it_is_held(self):
        gates, _ = image_run(handle='agentiac-tensorfold:v114-new', state='rollback')
        self.assertEqual(status(gates, 'ledger'), retire.REFUSE)
        self.assertIn('rollback', gates['ledger']['evidence'])

    def test_a_line_the_ledger_does_not_carry_is_refused_as_a_tag_not_a_path(self):
        gates, _ = image_run(handle='agentiac-runtime:never-named')
        self.assertEqual(status(gates, 'ledger'), retire.REFUSE)
        self.assertIn('not as a tag', gates['ledger']['evidence'])

    def test_the_declared_runtime_image_is_refused_because_the_declaration_names_it(self):
        """`runtime_image` lives under `all.vars`, so every worker inherits it: the refusal says so rather than
        listing six hosts, and it is a declaration refusal, not a missing-line one."""
        gates, _ = image_run(handle='agentiac-runtime:declared0000',
                             entry={'image': 'agentiac-runtime:declared0000', 'class': 'persistent',
                                    'retirement': 'candidate', 'reason': 'the tag the inventory names'})
        self.assertEqual(status(gates, 'declaration'), retire.REFUSE)
        self.assertIn('all vars runtime_image', gates['declaration']['evidence'])

    def test_a_per_host_override_of_the_runtime_image_is_named_by_host(self):
        gates, _ = image_run(handle='agentiac-runtime:oldoldold1',
                             nodes={'worker-a': {'backend': 'strata',
                                                 'runtime_image': 'agentiac-runtime:oldoldold1'}},
                             vars_={})
        self.assertEqual(status(gates, 'declaration'), retire.REFUSE)
        self.assertIn('infra/inventory.yml worker-a runtime_image', gates['declaration']['evidence'])

    def test_a_backend_manifest_naming_the_tag_is_a_declaration_too(self):
        gates, _ = image_run(declared_in=('backends/tf-1.1.4-qfn-mxfp8-nvfp4.json image agentiac-runtime:oldoldold1',))
        self.assertEqual(status(gates, 'declaration'), retire.REFUSE)
        self.assertIn('backends/tf-1.1.4-qfn-mxfp8-nvfp4.json', gates['declaration']['evidence'])

    def test_a_unit_file_or_node_config_naming_the_tag_refuses_the_removal(self):
        gates, _ = image_run(reading={'references': ['node.json names it'], 'present': False, 'size': ''})
        self.assertEqual(status(gates, 'references'), retire.REFUSE)
        self.assertIn('node.json names it', gates['references']['evidence'])

    def test_a_container_still_running_the_tag_refuses_it(self):
        gates, _ = image_run(reading={'references': ['container agentiac-worker (inference) runs it'],
                                      'present': True, 'size': '102MB'})
        self.assertEqual(status(gates, 'references'), retire.REFUSE)

    def test_a_candidate_whose_tag_is_absent_is_a_ledger_correction_not_a_deletion(self):
        gates, _ = image_run(reading={'references': [], 'present': False, 'size': ''})
        self.assertEqual(status(gates, 'present'), retire.REFUSE)
        self.assertIn('correction, not a deletion', gates['present']['evidence'])

    def test_a_retired_line_whose_tag_is_absent_is_confirmed_not_refused(self):
        gates, _ = image_run(handle='agentiac-runtime:gonesoon0000', state='retired',
                             reading={'references': [], 'present': False, 'size': ''})
        self.assertEqual(status(gates, 'present'), retire.UNKNOWN)
        self.assertIn('records a removal and this reading confirms it', gates['present']['evidence'])
        self.assertEqual(status(gates, 'ledger'), retire.REFUSE)   # a retired line is not a candidate line

    def test_a_host_that_did_not_answer_leaves_the_reading_unknown(self):
        gates, _ = image_run(reading=None)
        self.assertEqual(status(gates, 'host'), retire.UNKNOWN)

    def test_the_shipped_ledger_offers_no_image_candidate_today(self):
        graph = declared.load()
        images = [entry for host, block in graph.retention.items() for state in retention.STATES
                  for entry in block[state] if isinstance(entry, dict) and entry.get('image')
                  and entry.get('retirement') == 'candidate']
        self.assertEqual(images, [])


if __name__ == '__main__':
    unittest.main()
