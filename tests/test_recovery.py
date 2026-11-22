"""The recovery-only failure automation: what stands it down, what it does when nothing does, and what it never does.

The scope is the owner's recovery decision: re-claim the host the route already points at, never move the
route. These tests hold that boundary - the gates that refuse to act, the one claim it makes, and the rule that
a borrowed host is not armed for it.
"""
import contextlib
import datetime
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import paths  # noqa: E402
SITE = paths.root()
import recover  # noqa: E402
import route  # noqa: E402
from agentiac import declared, doctor, errors, recovery  # noqa: E402

TARGETS = {
    'worker-a': {'source_alias': 'worker-a-worker', 'node': 'worker-a', 'backend': 'strata-0.2.10-tiny-coder-q4',
                 'context_window': 262144},
    'worker-c': {'source_alias': 'worker-c-backend', 'node': 'worker-c', 'backend': 'worker-c-backend',
                 'context_window': 262144},
}
ARMED = {'ondemand_persistent': True, 'auto_reclaim': True, 'idle_unload_minutes': 30, 'ansible_host': 'worker-a.example'}
BORROWED = {'borrowed': True, 'idle_unload_minutes': 30, 'ansible_host': 'worker-c'}
HOSTS = {'worker-a': ARMED, 'worker-c': BORROWED}
DOWN = ('drift', f'worker-a: no on-demand front on worker-a; {errors.command()} reclaim')
UP = ('ok', 'worker-a: tunnel and front answer')
FAILED = {'LoadState': 'loaded', 'ActiveState': 'failed', 'Result': 'failed'}
STOPPED = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'Result': 'success'}
RUNNING = {'LoadState': 'loaded', 'ActiveState': 'active', 'Result': 'success'}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.routes = self.root / 'local-coder.json'
        self.routes.write_text(json.dumps({'alias': 'local-coder', 'default': 'worker-a', 'targets': TARGETS}))

    def decide(self, *, line=DOWN, front=FAILED, hosts=HOSTS, target='worker-a', state=None, pause=None, tunnel=True,
               key=True):
        """What `scripts/recover.py` would do here, with every host read replaced by what this test says."""
        if state is not None:
            recovery.save(self.root, state)
        if pause is not None:
            recovery.pause(self.root, **pause)
        with contextlib.ExitStack() as stack:
            for patch in (mock.patch.object(route, 'current', return_value=target),
                          mock.patch.object(route, 'ROUTES', self.routes),
                          mock.patch.object(recover, 'route_line', return_value=line),
                          mock.patch.object(recover, 'front_state', return_value=front),
                          mock.patch.object(recover.drift, 'tunnel_active', return_value=tunnel),
                          mock.patch.object(recover.borrow, 'endpoint',
                                            return_value=(18102, 'k') if key else None,
                                            side_effect=None if key else OSError('no key in this checkout'))):
                stack.enter_context(patch)
            return recover.decide(hosts, self.root)

    def stopped_at(self, gate, **kwargs):
        out = self.decide(**kwargs)
        self.assertEqual(out['decision'], 'stand_down', out['gates'])
        self.assertEqual(out['gate'], gate)
        return out['reason']

    def test_nothing_is_touched_while_the_route_answers(self):
        out = self.decide(line=UP)
        self.assertEqual(out['decision'], 'idle')
        self.assertEqual(out['reason'], 'worker-a: tunnel and front answer')

    def test_a_route_line_that_is_neither_up_nor_down_is_not_claimed_for(self):
        out = self.decide(line=('note', 'control-small serves on control'))
        self.assertEqual((out['decision'], out['gate']), ('stand_down', 'route'))

    def test_only_a_host_the_owner_arms_is_re_claimed_on_its_own(self):
        reason = self.stopped_at('armed', target='worker-c')  # the route on worker-c: borrowed, and no auto_reclaim on it
        self.assertIn('auto_reclaim', reason)
        self.assertIn(f'{errors.command()} reclaim', reason)  # the person's command, named

    def test_a_front_control_cannot_read_is_not_a_front_to_repair(self):
        """A checkout with no secrets/ (a worktree, a rebuilt control) sees every front as down. That reading
        is not evidence, and acting on it would re-claim a host nobody asked for."""
        out = self.decide(key=False)
        self.assertEqual((out['decision'], out['gate']), ('stand_down', 'credential'))
        self.assertIn('secrets/nodes', out['reason'])

    def test_an_armed_host_passes_the_armed_gate(self):
        out = self.decide(target='worker-a')
        self.assertEqual(out['decision'], 'reclaim', out['gates'])

    def test_a_pause_stands_it_down(self):
        reason = self.stopped_at('pause', pause={'minutes': 60, 'reason': 'ops stop worker-a'})
        self.assertIn('paused until', reason)
        self.assertIn('ops recover resume', reason)

    def test_a_claim_is_not_attempted_twice_inside_the_cooldown(self):
        out = self.decide(target='worker-a', state={'decision': 'reclaim_failed', 'node': 'worker-a',
                                                    'last_action_at': recovery.stamp()})
        self.assertEqual((out['decision'], out['gate']), ('stand_down', 'cooldown'))
        self.assertIn('not attempted for another', out['reason'])
        self.assertGreater(recovery.cooldown_left({'last_action_at': recovery.stamp()}), recovery.COOLDOWN_S - 2)

    def test_a_later_run_passes_the_cooldown_gate(self):
        minute = recovery.now()
        out = self.decide(target='worker-a', state={'decision': 'reclaim_failed', 'node': 'worker-a',
                                                    'last_action_at': recovery.stamp(minute - datetime.timedelta(
                                                        seconds=recovery.COOLDOWN_S + 1))})
        self.assertEqual(out['decision'], 'reclaim', out['gates'])

    def test_after_three_failed_claims_a_person_goes_first(self):
        out = self.decide(target='worker-a', state={'decision': 'reclaim_failed', 'node': 'worker-a',
                                                    'consecutive_failures': recovery.MAX_FAILURES,
                                                    'last_action_at': None})
        self.assertEqual((out['decision'], out['gate']), ('stand_down', 'breaker'))
        self.assertIn('3 reclaims in a row', out['reason'])

    def test_a_host_that_does_not_answer_is_a_stand_down_and_not_a_fault(self):
        out = self.decide(front=None)
        self.assertEqual(out['decision'], 'unreachable')
        self.assertEqual(out['gate'], 'host')

    def test_a_front_that_failed_is_an_accident_it_repairs(self):
        self.assertEqual(self.decide(front=FAILED)['decision'], 'reclaim')

    def test_a_front_up_while_the_forward_to_it_is_down_is_an_accident_it_repairs(self):
        out = self.decide(front=RUNNING, tunnel=False)
        self.assertEqual(out['decision'], 'reclaim')
        self.assertIn('forward to worker-a is down', out['reason'])

    def test_a_front_stopped_cleanly_is_left_alone(self):
        """`ops stop` keeps a host stopped until the next claim; a timer must not undo that."""
        out = self.decide(front=STOPPED)
        self.assertEqual((out['decision'], out['gate']), ('stand_down', 'host'))
        self.assertIn('stopped cleanly', out['reason'])

    def test_a_front_that_is_not_installed_there_is_left_alone(self):
        out = self.decide(front={'LoadState': 'not-found', 'ActiveState': 'inactive', 'Result': 'success'})
        self.assertEqual((out['decision'], out['gate']), ('stand_down', 'host'))
        self.assertIn('not installed', out['reason'])

    def test_a_masked_front_is_left_alone(self):
        out = self.decide(front={'LoadState': 'masked', 'ActiveState': 'inactive', 'Result': 'success'})
        self.assertIn('masked', out['reason'])


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.routes = self.root / 'local-coder.json'
        self.routes.write_text(json.dumps({'alias': 'local-coder', 'default': 'worker-a', 'targets': TARGETS}))

    def run_it(self, *, line=(DOWN, UP), act=(True, ''), state=None, check_only=False):
        if state is not None:
            recovery.save(self.root, state)
        with contextlib.ExitStack() as stack:
            for patch in (mock.patch.object(route, 'current', return_value='worker-a'),
                          mock.patch.object(route, 'ROUTES', self.routes),
                          mock.patch.object(recover.borrow, 'hosts', return_value=dict(HOSTS)),
                          mock.patch.object(recover.borrow, 'endpoint', return_value=(18102, 'k')),
                          mock.patch.object(recover.drift, 'tunnel_active', return_value=True),
                          mock.patch.object(recover, 'route_line', side_effect=list(line)),
                          mock.patch.object(recover, 'front_state', return_value=FAILED)):
                stack.enter_context(patch)
            self.act = stack.enter_context(mock.patch.object(recover, 'act', return_value=(act[0], act[1], 85.0)))
            return recover.run(self.root, check_only=check_only)

    def test_the_action_is_the_claim_reclaim_makes_and_it_never_switches_the_route(self):
        with mock.patch.object(route, 'current', return_value='worker-a'), mock.patch.object(route, 'ROUTES', self.routes), \
                mock.patch.object(recover.borrow, 'claim') as claim, \
                mock.patch.object(recover.borrow.subprocess, 'run') as spawned:
            ok, detail, seconds = recover.act(HOSTS)
        self.assertTrue(ok)
        self.assertEqual(claim.call_args.args[0], 'worker-a')
        self.assertEqual(claim.call_args.args[2:], (False, 30, True, 'strata-0.2.10-tiny-coder-q4', 262144))
        spawned.assert_not_called()  # claim touches the route only through `route.py set`, reached with switch=True

    def test_a_claim_that_leaves_the_route_up_is_recorded_and_costs_nothing_more(self):
        record, code = self.run_it()
        self.assertEqual((record['decision'], code), ('reclaimed', 0))
        self.assertEqual(record['action']['seconds'], 85)
        self.assertTrue(record['action']['verified'])
        self.assertEqual(record['consecutive_failures'], 0)
        self.assertEqual(json.loads((self.root / recovery.STATE).read_text())['decision'], 'reclaimed')
        self.assertEqual(len((self.root / recovery.LOG).read_text().splitlines()), 1)

    def test_a_failed_claim_counts_and_the_run_exits_one(self):
        record, code = self.run_it(act=(False, 'worker-a did not load: admission refused'))
        self.assertEqual((record['decision'], code), ('reclaim_failed', 1))
        self.assertEqual(record['consecutive_failures'], 1)
        self.assertIn('admission refused', record['action']['detail'])

    def test_a_claim_that_leaves_the_route_down_counts_as_a_failure_too(self):
        record, code = self.run_it(line=(DOWN, DOWN))
        self.assertEqual((record['decision'], code), ('reclaim_failed', 1))
        self.assertIn('the route still is not up', record['action']['detail'])

    def test_the_failure_counter_adds_up_across_runs(self):
        record, _ = self.run_it(act=(False, 'still down'), state={'decision': 'reclaim_failed', 'node': 'worker-a',
                                                                   'consecutive_failures': 1, 'last_action_at': None})
        self.assertEqual((record['decision'], record['consecutive_failures']), ('reclaim_failed', 2))

    def test_an_idle_run_clears_the_counter_because_the_fault_is_gone(self):
        record, code = self.run_it(line=(UP,), state={'decision': 'reclaim_failed', 'node': 'worker-a',
                                                      'consecutive_failures': 2})
        self.assertEqual((record['decision'], code), ('idle', 0))
        self.assertEqual(record['consecutive_failures'], 0)
        self.assertIn('failure counter is cleared', record['reason'])

    def test_a_busy_mutation_lock_is_a_stand_down_and_not_a_failure(self):
        state = {'decision': 'reclaim_failed', 'node': 'worker-a', 'consecutive_failures': 1, 'last_action_at': None}
        busy = recover.mutation.Busy({'operation': 'claim worker-b', 'pid': 1})
        with mock.patch.object(recover.mutation, 'hold', side_effect=busy):
            record, code = self.run_it(state=state)
        self.act.assert_not_called()
        self.assertEqual((record['decision'], record['gate'], code), ('stand_down', 'lock', 0))
        self.assertIn('claim worker-b', record['reason'])
        self.assertEqual(record['consecutive_failures'], 1)
        self.assertIsNone(record['last_action_at'])
        self.assertNotIn('action', record)
        self.assertEqual(json.loads((self.root / recovery.STATE).read_text())['gate'], 'lock')

    def test_check_only_acts_nothing_and_writes_nothing(self):
        record, code = self.run_it(check_only=True)
        self.assertEqual((record['decision'], code), ('reclaim', 0))
        self.assertIn('checked only', record['reason'])
        self.assertFalse((self.root / recovery.STATE).exists())
        self.assertFalse((self.root / recovery.LOG).exists())


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_there_is_no_record_before_the_first_run(self):
        self.assertIsNone(recovery.load(self.root))
        self.assertIsNone(recovery.paused(self.root))

    def test_a_pause_expires_on_its_own(self):
        recovery.pause(self.root, 60, 'ops stop worker-a')
        self.assertIn('ops stop worker-a', recovery.paused(self.root)['reason'])
        later = recovery.now() + datetime.timedelta(minutes=61)
        self.assertIsNone(recovery.paused(self.root, later))
        recovery.resume(self.root)
        self.assertIsNone(recovery.paused(self.root))

    def test_the_cooldown_and_the_breaker_read_the_same_record_the_script_writes(self):
        recovery.save(self.root, {'decision': 'reclaim_failed', 'node': 'worker-a', 'consecutive_failures': 3,
                                  'last_action_at': recovery.stamp()})
        record = recovery.load(self.root)
        self.assertTrue(recovery.breaker_open(record))
        self.assertEqual(recovery.failures(record), 3)
        self.assertGreater(recovery.cooldown_left(record), recovery.COOLDOWN_S - 2)
        self.assertIn(f'{errors.command()} ops recover reset', recovery.next_step(record))
        self.assertIn('3 failed reclaims in a row', recovery.line(record))

    def test_the_line_says_what_happened_in_the_words_the_record_carries(self):
        self.assertEqual(recovery.line({'decision': 'reclaimed', 'node': 'worker-a', 'action': {'seconds': 85}}),
                         're-claimed worker-a in 85 s; the route answers')
        self.assertIn('stands down (armed)', recovery.line({'decision': 'stand_down', 'gate': 'armed',
                                                            'reason': 'worker-c declares no auto_reclaim'}))
        self.assertIn('did not answer', recovery.line({'decision': 'unreachable', 'node': 'worker-d'}))

    def test_a_record_with_no_time_is_not_an_age(self):
        self.assertIsNone(recovery.age_hours({'decision': 'idle'}))


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def check(self, record):
        if record is not None:
            recovery.save(self.root, record)
        return doctor.recovery_check(self.root)

    def test_the_screen_carries_it(self):
        found = {item['id']: item for item in doctor.summary(self.root, {'worker-b': {}}, lambda: (True, 'ok'),
                                                             drift=mock.Mock(sections=lambda _hosts: iter(())),
                                                             preflight=mock.Mock(
                                                                 check_host=lambda *item: {'host': item[0], 'ok': True,
                                                                                           'failed': [], 'free_gib': 100}))['checks']}
        self.assertIn('recovery', found)
        self.assertEqual(found['recovery']['status'], 'note')

    def test_no_record_yet_is_a_note_naming_the_timer(self):
        found = self.check(None)
        self.assertEqual(found['status'], 'note')
        self.assertIn(declared.resource('recovery.timer'), found['next'])

    def test_an_idle_automation_is_ok(self):
        self.assertEqual(self.check({'decision': 'idle', 'node': 'worker-a', 'reason': 'tunnel and front answer'})['status'], 'ok')

    def test_a_recovery_that_worked_is_worth_knowing_but_is_not_a_problem(self):
        found = self.check({'decision': 'reclaimed', 'node': 'worker-a', 'action': {'seconds': 85, 'verified': True}})
        self.assertEqual((found['status'], found['next']), ('note', None))
        self.assertIn('re-claimed worker-a', found['evidence'])

    def test_a_failed_reclaim_and_an_open_breaker_are_problems(self):
        self.assertEqual(self.check({'decision': 'reclaim_failed', 'node': 'worker-a',
                                     'action': {'detail': 'admission refused'}})['status'], 'problem')
        found = self.check({'decision': 'stand_down', 'gate': 'breaker', 'node': 'worker-a',
                            'consecutive_failures': recovery.MAX_FAILURES, 'reason': 'the host needs a look'})
        self.assertEqual(found['status'], 'problem')
        self.assertIn('3 failed reclaims in a row', found['evidence'])
        self.assertIn('reset', found['next'])


class UnitTests(unittest.TestCase):
    SERVICE = (ROOT / 'infra/templates/recovery.service.j2').read_text()
    TIMER = (ROOT / 'infra/templates/recovery.timer.j2').read_text()
    PLAYBOOK = (ROOT / 'infra/control.yml').read_text()

    def test_the_service_runs_the_command_the_timer_and_a_person_run(self):
        self.assertIn('ExecStart={{ code_root }}/bin/agentiac ops recover run\n', self.SERVICE)

    def test_it_has_room_for_a_claim_that_has_to_load_the_model(self):
        self.assertIn('TimeoutStartSec=1800\n', self.SERVICE)

    def test_it_writes_only_its_own_records_and_ansible_home(self):
        self.assertEqual(re.findall(r'^ReadWritePaths=(.*)$', self.SERVICE, re.M),
                         ['{{ instance_root }}/artifacts/recovery'])
        self.assertEqual(re.findall(r'^Environment=ANSIBLE_HOME=(.*)$', self.SERVICE, re.M),
                         ['{{ instance_root }}/artifacts/recovery/ansible'])
        for line in ('ProtectSystem=strict', 'ProtectHome=read-only', 'PrivateTmp=yes', 'User={{ ansible_user }}'):
            self.assertIn(line + '\n', self.SERVICE)

    def test_a_sudo_free_unit_could_not_restart_the_forward_a_claim_restarts(self):
        """scripts/borrow.py start_tunnel runs `sudo -n systemctl start` on control: NoNewPrivileges would refuse it."""
        self.assertNotIn('NoNewPrivileges=yes', self.SERVICE)
        self.assertIn('start_tunnel', self.SERVICE)

    def test_the_timer_detects_and_the_script_holds_the_retry(self):
        self.assertEqual(re.findall(r'^OnCalendar=(.*)$', self.TIMER, re.M), ['*:0/5'])
        self.assertIn('Persistent=true\n', self.TIMER)
        self.assertIn('Unit={{ resource_prefix }}-recovery.service\n', self.TIMER)

    def test_the_playbook_installs_both_and_keeps_only_the_timer_running(self):
        for name in ('recovery.service', 'recovery.timer'):
            self.assertIn(name, self.PLAYBOOK)
        started = self.PLAYBOOK.split('loop: [')[1].split(']')[0]
        self.assertIn('recovery.timer', started)
        self.assertNotIn('recovery.service', started)

    def test_drift_compares_it_with_what_is_installed(self):
        import drift
        self.assertLessEqual({'recovery.service', 'recovery.timer'}, set(drift.CONTROL_UNITS))

    def test_only_a_dedicated_worker_is_armed_for_it(self):
        import yaml
        hosts = yaml.safe_load((SITE / 'infra/inventory.yml').read_text())['all']['children']['workers']['hosts']
        self.assertTrue(any(host.get('ondemand_persistent') for host in hosts.values()))
        armed = sorted(name for name, host in hosts.items() if host.get('auto_reclaim'))
        for name in armed:
            self.assertTrue(hosts[name].get('ondemand_persistent'), name)
            self.assertNotIn('borrowed', hosts[name], name)
