"""agentiac command: argument mapping onto the existing scripts, worktree cleanup, completion."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import command, paths  # noqa: E402

SITE = paths.root()
HOSTS = command.inventory()
DEDICATED = next(n for n, h in HOSTS.items() if h.get('default_profile') and not h.get('borrowed'))
NO_DEFAULT_PROFILE = next(n for n, h in HOSTS.items() if h.get('borrowed') and not h.get('default_profile'))
BORROWED = next(n for n, h in HOSTS.items() if h.get('borrowed'))
REMOTE = [n for n, h in HOSTS.items() if h.get('ansible_connection') != 'local'][:2]
TARGETS = json.loads((SITE / 'gateway/local-coder.json').read_text())['targets']
TARGET_NAME = next(iter(TARGETS))
STRATA_HOST = next(n for n, h in HOSTS.items() if h.get('strata_trial_roots'))
STRATA_BACKEND = next(spec['backend'] for spec in TARGETS.values() if spec['node'] == STRATA_HOST and 'backend' in spec)


def planned(*argv):
    """The argv the command would exec, without running it (and without taking the lock `ops deploy` takes)."""
    with patch.object(command, 'run', side_effect=lambda plan: plan) as run, \
            patch.object(command.mutation, 'hold_for_exec', return_value={}), \
            patch.object(command, 'check_core_pin'):
        command.main(list(argv))
    return run.call_args.args[0]


def tail(plan):
    """The arguments after the interpreter and script/playbook path, as written by the user."""
    return plan[2:]


class PassThroughTests(unittest.TestCase):
    def test_code_resolves_repo_from_the_callers_directory_and_passes_pi_arguments(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            plan = planned('code', '-m', 'router', '--allow-cloud', '-p', 'summarize src/', '--', '--thinking', 'off')
        self.assertEqual(plan[1], str(ROOT / 'scripts/agent.py'))
        self.assertEqual(tail(plan), ['--repo', str(Path(tmp).resolve()), '--alias', 'router', '--allow-cloud',
                                      '--', '-p', 'summarize src/', '--thinking', 'off'])

    def test_code_defaults_to_local_coder_in_a_worktree(self):
        self.assertEqual(tail(planned('code', str(ROOT)))[2:], ['--alias', 'local-coder'])

    def test_code_accepts_and_passes_local_any(self):
        self.assertIn('local-any', command.code_aliases())
        self.assertEqual(tail(planned('code', '-m', 'local-any'))[2:], ['--alias', 'local-any'])

    def test_code_resume_passes_the_resume_flag(self):
        self.assertEqual(tail(planned('code', '--resume'))[-1], '--resume')

    def test_code_passes_the_starting_thinking_level(self):
        self.assertEqual(tail(planned('code', str(ROOT), '--thinking', 'high'))[2:],
                         ['--alias', 'local-coder', '--thinking', 'high'])

    def test_code_rejects_an_alias_pi_does_not_know(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            planned('code', '-m', 'unknown-baseline')

    def test_double_dash_is_only_split_for_code(self):
        self.assertEqual(tail(planned('eval', 'suite', 'run', '--alias', 'local-coder-worker-a', '--k', '3')),
                         ['run', '--alias', 'local-coder-worker-a', '--k', '3'])

    def test_start_defaults_to_the_hosts_tested_profile(self):
        self.assertEqual(planned('ops', 'start', DEDICATED)[-4:],
                         ['--limit', DEDICATED, '-e', f"model={HOSTS[DEDICATED]['default_profile']}"])
        self.assertEqual(planned('ops', 'download', DEDICATED, '--model', 'x')[-1], 'model=x')

    def test_start_without_a_default_profile_needs_one(self):
        with self.assertRaisesRegex(SystemExit, 'no default_profile'):
            planned('ops', 'start', NO_DEFAULT_PROFILE)

    def test_host_is_required_for_disruptive_actions(self):
        for argv in (['ops', 'stop'], ['unload'], ['claim'], ['release']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                planned(*argv)

    def test_release_only_accepts_borrowed_hosts(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            planned('release', DEDICATED)
        self.assertEqual(tail(planned('release', BORROWED)), ['release', BORROWED])

    def test_lifecycle_and_route_mapping(self):
        self.assertEqual(tail(planned('claim', BORROWED, '--idle', '60', '--no-warm')),
                         ['claim', BORROWED, '--idle', '60.0', '--no-warm'])
        self.assertEqual(tail(planned('warm')), ['warm'])
        self.assertEqual(tail(planned('route')), ['show'])
        self.assertEqual(tail(planned('route', TARGET_NAME)), ['set', TARGET_NAME])
        self.assertEqual(planned('ops', 'netwatch', *REMOTE)[-1], ','.join(REMOTE))

    def test_deploy_options_map_onto_the_playbook(self):
        self.assertEqual(planned('ops', 'deploy')[3:], [str(command.CODE / 'infra/control.yml')])
        self.assertEqual(planned('ops', 'deploy', '--check', '--only', 'gateway')[3:],
                         [str(command.CODE / 'infra/control.yml'), '--check', '--diff', '--tags', 'gateway'])
        self.assertEqual(planned('ops', 'deploy', '--allow-dirty')[-2:], ['-e', 'allow_dirty=true'])

    def test_prepare_maps_tensorfold_and_strata(self):
        self.assertEqual(planned('ops', 'prepare', REMOTE[0])[3:],
                         [str(command.CODE / 'infra/prepare.yml'), '--limit', REMOTE[0]])
        self.assertEqual(planned('ops', 'prepare', STRATA_HOST, '--backend', STRATA_BACKEND, '--isolated', '--check')[3:],
                         [str(command.CODE / 'infra/prepare.yml'), '--limit', STRATA_HOST, '--check', '--diff',
                          '-e', f'backend_id={STRATA_BACKEND}', '-e', 'isolated=true'])

    def test_reclaim_and_drift_map_onto_their_scripts(self):
        self.assertEqual(tail(planned('reclaim')), ['reclaim'])
        self.assertTrue(planned('ops', 'drift')[1].endswith('scripts/drift.py'))

    def test_recover_maps_onto_its_own_script_and_keeps_its_action_readable(self):
        self.assertTrue(planned('ops', 'recover', 'run')[1].endswith('scripts/recover.py'))
        self.assertEqual(tail(planned('ops', 'recover', 'run')), ['run'])
        self.assertEqual(tail(planned('ops', 'recover', 'run', '--check', '--json')), ['run', '--check', '--json'])
        self.assertEqual(tail(planned('ops', 'recover', 'pause', '30', '--reason', 'ops stop worker-a')),
                         ['pause', '30', '--reason', 'ops stop worker-a'])
        self.assertEqual(tail(planned('ops', 'recover', 'reset')), ['reset'])

    def test_storage_maps_onto_the_storage_script(self):
        self.assertTrue(planned('ops', 'storage')[1].endswith('scripts/storage.py'))
        self.assertEqual(tail(planned('ops', 'storage', *REMOTE))[-len(REMOTE):], REMOTE)

    def test_storage_candidates_is_a_read_of_the_ledger_not_a_second_report(self):
        # T5.2: `ops storage --candidates` is the same script, one flag, so a disk report and the ledger's own
        # mark cannot drift apart into two answers.
        self.assertEqual(tail(planned('ops', 'storage', '--candidates')), ['--candidates'])
        self.assertEqual(tail(planned('ops', 'storage', BORROWED, '--candidates')), [BORROWED, '--candidates'])

    def test_retire_defaults_to_the_check_and_names_the_ledger_line(self):
        plan = planned('ops', 'retire', BORROWED, '--backend', 'gyro-0.2.11')
        self.assertTrue(plan[1].endswith('scripts/retire.py'))
        self.assertEqual(tail(plan), ['check', BORROWED, '--backend', 'gyro-0.2.11'])

    def test_retires_two_stages_are_two_flags_not_two_commands(self):
        self.assertEqual(tail(planned('ops', 'retire', BORROWED, '--name', 'x-isolated', '--quarantine')),
                         ['quarantine', BORROWED, '--name', 'x-isolated'])
        self.assertEqual(tail(planned('ops', 'retire', BORROWED, '--name', 'x-isolated', '--purge')),
                         ['purge', BORROWED, '--name', 'x-isolated'])

    def test_a_shortened_isolation_is_passed_on_as_a_reason(self):
        # §5.5: the shortening is written down, so it travels as a reason and not as a flag with no text.
        self.assertEqual(tail(planned('ops', 'retire', BORROWED, '--name', 'x', '--purge',
                                      '--shorten-wait', 'the owner watched 3 h')),
                         ['purge', BORROWED, '--name', 'x', '--shorten-wait', 'the owner watched 3 h'])

    def test_thin_and_restore_of_a_root_go_to_the_same_script_as_retire(self):
        self.assertEqual(tail(planned('ops', 'thin', BORROWED, '--backend', 'gyro-0.2.8')),
                         ['thin', BORROWED, '--backend', 'gyro-0.2.8'])
        self.assertEqual(tail(planned('ops', 'restore', BORROWED, '--backend', 'gyro-0.2.8', '--check')),
                         ['restore', BORROWED, '--backend', 'gyro-0.2.8', '--dry-run'])

    def test_restore_keeps_its_other_form_the_backup_one(self):
        self.assertTrue(planned('ops', 'restore', '--into', '/tmp/here')[1].endswith('scripts/restore-control.py'))
        self.assertEqual(tail(planned('ops', 'restore', '--into', '/tmp/here')), ['--into', '/tmp/here'])

    def test_restore_of_a_root_without_a_backend_is_refused_with_the_shape_of_the_command(self):
        # `ops thin` is argparse-strict about --backend; `ops restore` cannot be, because its other form
        # (--into PATH) has no backend, so the two forms are told apart here instead.
        with self.assertRaises(SystemExit) as raised:
            planned('ops', 'restore', BORROWED)
        self.assertIn(f'ops restore {BORROWED} needs --backend ID', str(raised.exception))
        with self.assertRaises(SystemExit) as raised:
            planned('ops', 'restore', '--into', '/tmp/here', '--backend', 'gyro-0.2.8')
        self.assertIn('ops restore --into PATH is control, not a root', str(raised.exception))

    def test_eval_image_defaults_to_status_and_keeps_its_action_and_tag(self):
        # The action is a positional under `ops`, whose own subcommand also lands in `action`: naming the
        # same dest would leave `ops` unable to tell which action was asked for.
        self.assertTrue(planned('ops', 'eval-image')[1].endswith('scripts/evalimage.py'))
        self.assertEqual(tail(planned('ops', 'eval-image')), [])
        self.assertEqual(tail(planned('ops', 'eval-image', 'build')), ['build'])
        self.assertEqual(tail(planned('ops', 'eval-image', 'hash', '--tag', 'other:1')), ['hash', '--tag', 'other:1'])

    def test_chatgpt_runs_in_the_gateway_environment(self):
        plan = planned('chatgpt', 'status')
        self.assertEqual(plan[0], str(ROOT / '.venv-gateway/bin/python'))
        self.assertEqual(tail(plan), ['--status'])
        self.assertEqual(tail(planned('chatgpt', 'login')), [])


class DeployLockTests(unittest.TestCase):
    """`ops deploy` holds the mutation lock for the whole ansible run; `--check` changes nothing and takes none (T2.6)."""

    def deploy(self, *options, held=None, busy=None):
        held = {command.mutation.HELD: '1'} if held is None else held
        with patch.object(command, 'run') as run, \
                patch.object(command.mutation, 'hold_for_exec', return_value=held, side_effect=busy) as hold, \
                patch.object(command, 'check_core_pin'):
            command.main(['ops', 'deploy', *options])
        return run, hold

    def test_a_deploy_takes_the_lock_and_hands_it_to_ansible(self):
        run, hold = self.deploy()
        hold.assert_called_once_with(command.ROOT, 'ops deploy')
        plan, extra = run.call_args.args
        self.assertEqual(plan[3:], [str(command.CODE / 'infra/control.yml')])
        self.assertEqual(extra, {command.mutation.HELD: '1'})

    def test_a_check_takes_no_lock(self):
        run, hold = self.deploy('--check')
        hold.assert_not_called()
        self.assertEqual(run.call_args.args[0][3:], [str(command.CODE / 'infra/control.yml'), '--check', '--diff'])

    def test_a_deploy_started_by_a_holder_adds_nothing(self):
        run, hold = self.deploy(held={})
        self.assertEqual(len(run.call_args.args), 1)

    def test_a_busy_lock_refuses_the_deploy_and_names_the_holder(self):
        busy = command.mutation.Busy({'operation': 'claim worker-a', 'pid': 7})
        with patch.object(command, 'run') as run, patch.object(command.mutation, 'hold_for_exec', side_effect=busy), \
                self.assertRaises(SystemExit) as refused:
            command.main(['ops', 'deploy'])
        run.assert_not_called()
        self.assertIn('claim worker-a', str(refused.exception.code))
        self.assertIn('deploy again', str(refused.exception.code))

    def test_run_adds_the_handed_over_environment(self):
        with patch.object(command.os, 'chdir'), patch.object(command.os, 'execvpe') as execvpe:
            command.run(['true'], {command.mutation.HELD: '1'})
            command.run(['true'])
        self.assertEqual(execvpe.call_args_list[0].args[2][command.mutation.HELD], '1')
        self.assertNotIn(command.mutation.HELD, execvpe.call_args_list[1].args[2])


class CorePinDeployTests(unittest.TestCase):
    """`ops deploy` records both site and core revisions and refuses a dirty or unpinned `core/` submodule (T9.8, D6.3)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name)
        self.site = self.top / 'site'
        self.site.mkdir()
        (self.site / 'artifacts').mkdir()
        git(self.site, 'init', '-q')
        (self.site / 'a').write_text('site')
        git(self.site, 'add', 'a')
        git(self.site, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'site 1')
        self.site_rev = command.git(self.site, 'rev-parse', 'HEAD').stdout.strip()

    def add_core_submodule(self):
        origin = self.top / 'core-origin'
        origin.mkdir()
        git(origin, 'init', '-q')
        (origin / 'c').write_text('1')
        git(origin, 'add', 'c')
        git(origin, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'core 1')
        first = command.git(origin, 'rev-parse', 'HEAD').stdout.strip()
        (origin / 'c').write_text('2')
        git(origin, 'add', 'c')
        git(origin, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'core 2')
        second = command.git(origin, 'rev-parse', 'HEAD').stdout.strip()
        git(self.site, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(origin), 'core')
        git(self.site / 'core', 'checkout', '-q', first)
        git(self.site, 'add', 'core', '.gitmodules')
        git(self.site, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'pin core 1')
        self.site_rev = command.git(self.site, 'rev-parse', 'HEAD').stdout.strip()
        return first, second

    def test_without_core_records_core_equal_to_site(self):
        command.check_core_pin(self.site)
        command.record_deploy(self.site)
        recorded = json.loads((self.site / 'artifacts/deployed.json').read_text())
        self.assertEqual(recorded['site'], self.site_rev)
        self.assertEqual(recorded['core'], self.site_rev)
        self.assertEqual(recorded['revision'], self.site_rev)
        self.assertFalse(recorded['dirty'])

    def test_clean_pinned_core_passes_and_records_both_revisions(self):
        first, _ = self.add_core_submodule()
        command.check_core_pin(self.site)
        command.record_deploy(self.site)
        recorded = json.loads((self.site / 'artifacts/deployed.json').read_text())
        self.assertEqual(recorded['site'], self.site_rev)
        self.assertEqual(recorded['core'], first)
        self.assertEqual(recorded['revision'], self.site_rev)
        self.assertFalse(recorded['dirty'])

    def test_dirty_core_is_refused_unless_allow_dirty(self):
        self.add_core_submodule()
        (self.site / 'core/c').write_text('dirty')
        parser, nodes = command.build_parser(), command.inventory()
        with patch.object(command, 'ROOT', self.site), patch.object(command, 'build_parser', return_value=parser), \
                patch.object(command, 'inventory', return_value=nodes), \
                patch.object(command, 'run') as run, patch.object(command.mutation, 'hold_for_exec', return_value={}):
            with self.assertRaisesRegex(SystemExit, 'core/ has uncommitted changes'):
                command.main(['ops', 'deploy'])
            run.assert_not_called()
            command.main(['ops', 'deploy', '--allow-dirty'])
            self.assertEqual(run.call_count, 1)
        command.record_deploy(self.site)
        self.assertTrue(json.loads((self.site / 'artifacts/deployed.json').read_text())['dirty'])

    def test_core_at_another_commit_is_refused_unless_allow_dirty(self):
        first, second = self.add_core_submodule()
        git(self.site / 'core', 'checkout', '-q', second)
        parser, nodes = command.build_parser(), command.inventory()
        with patch.object(command, 'ROOT', self.site), patch.object(command, 'build_parser', return_value=parser), \
                patch.object(command, 'inventory', return_value=nodes), \
                patch.object(command, 'run') as run, patch.object(command.mutation, 'hold_for_exec', return_value={}):
            with self.assertRaisesRegex(SystemExit, f'core/ is at {second[:8]}, not the pinned {first[:8]}'):
                command.main(['ops', 'deploy'])
            run.assert_not_called()
            command.main(['ops', 'deploy', '--allow-dirty'])
            self.assertEqual(run.call_count, 1)


class StatusTests(unittest.TestCase):
    def state(self, status):
        import borrow  # scripts/ is on sys.path once host_state has run
        with patch.object(borrow, 'endpoint', return_value=(1, 'key')), \
                patch.object(borrow, 'ondemand_call', return_value=(200, status)):
            return command.host_state('worker-a', {})

    def test_on_demand_states_as_reported_by_the_front(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        self.assertEqual(self.state({'state': 'ready', 'unload_in_s': 1725}), 'loaded, unloads in 29 min')
        self.assertEqual(self.state({'state': 'ready', 'unload_in_s': None}), 'loaded, kept loaded')
        self.assertEqual(self.state({'state': 'unloaded', 'last_error': 'admission refused: GPU in use'}),
                         'unloaded (loads on the next request); last load failed: admission refused: GPU in use')
        self.assertEqual(self.state({'state': 'loading'}), 'loading')


def git(path, *argv):
    subprocess.run(['git', '-C', str(path), *argv], check=True, capture_output=True)


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name)
        self.repo, self.trees = self.top / 'repo', self.top / 'agent-worktrees'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'base')
        self.tree = self.trees / 'repo-20261020-000000'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'agent/20261020-000000', str(self.tree))
        parser, nodes = command.build_parser(), command.inventory()
        for attr, value in (('WORKTREES', self.trees), ('ROOT', self.repo),
                            ('build_parser', lambda former=True: parser), ('inventory', lambda: nodes)):
            patcher = patch.object(command, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def branches(self):
        return subprocess.run(['git', '-C', str(self.repo), 'branch', '--list', 'agent/*'],
                              capture_output=True, text=True).stdout.strip()

    def test_unmerged_work_is_kept_without_force(self):
        git(self.tree, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'agent work')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            command.main(['worktrees'])
        self.assertIn('1 commits, 0 uncommitted', out.getvalue())
        with self.assertRaisesRegex(SystemExit, '1 commits'):
            command.main(['worktrees', 'drop', self.tree.name])
        self.assertTrue(self.tree.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            command.main(['worktrees', 'drop', self.tree.name, '--force'])
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), '')
        with contextlib.redirect_stdout(io.StringIO()) as empty:
            command.main(['worktrees'])
        self.assertIn('no agent worktrees', empty.getvalue())

    def test_hand_made_agent_worktree_outside_the_folder_is_listed_and_marked_but_refused_by_drop(self):
        hand = self.top / 'repo-plan'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'agent/plan', str(hand))
        git(hand, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'plan work')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            command.main(['worktrees'])
        listing = out.getvalue()
        self.assertIn(str(hand), listing)
        self.assertIn('agent/plan', listing)
        self.assertIn('made by hand', listing)
        self.assertNotIn('made by hand', listing.splitlines()[0])
        with self.assertRaisesRegex(SystemExit, 'is not an agent worktree'):
            command.main(['worktrees', 'drop', hand.name])
        self.assertTrue(hand.exists())

    def test_a_session_worktree_of_another_repository_is_still_listed(self):
        other = self.top / 'other'
        other.mkdir()
        git(other, 'init', '-q')
        git(other, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'base')
        session = self.trees / 'other-20261021-000000'
        git(other, 'worktree', 'add', '-q', '-b', 'agent/20261021-000000', str(session))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            command.main(['worktrees'])
        line = next(line for line in out.getvalue().splitlines() if str(session) in line)
        self.assertIn(f'git -C {other} diff', line)
        self.assertNotIn('made by hand', line)

    def test_merged_is_judged_as_prune_judges_it(self):
        git(self.repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        squashed = self.top / 'repo-squashed'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'agent/squashed', str(squashed))
        git(squashed, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'work')
        tip = subprocess.run(['git', '-C', str(squashed), 'rev-parse', 'HEAD'],
                             capture_output=True, text=True, check=True).stdout.strip()

        def listing(heads):
            with patch.object(command.hygiene, 'merged_heads', return_value=heads), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                command.main(['worktrees'])
            return {line.split()[0]: line for line in out.getvalue().splitlines()}

        lines = listing({tip})  # the head of a squash-merged pull request, commits still ahead of HEAD
        self.assertIn('1 commits', lines[str(squashed)])
        self.assertIn(', merged', lines[str(squashed)])
        self.assertIn(', merged', lines[str(self.tree)])  # in origin/main
        self.assertIn('not merged', listing(set())[str(squashed)])
        self.assertIn('GitHub not asked', listing(None)[str(squashed)])

    def test_review_and_apply_fast_forward_preserves_worktree(self):
        (self.tree / 'result.txt').write_text('agent work')
        git(self.tree, 'add', 'result.txt')
        git(self.tree, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'agent work')
        (self.repo / 'unrelated').write_text('keep')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            command.main(['worktrees', 'review', self.tree.name])
        self.assertIn('result.txt', out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            command.main(['worktrees', 'apply', self.tree.name])
        self.assertEqual((self.repo / 'result.txt').read_text(), 'agent work')
        self.assertEqual((self.repo / 'unrelated').read_text(), 'keep')
        self.assertTrue(self.tree.exists())

    def test_apply_refuses_dirty_worktree_and_diverged_destination(self):
        (self.tree / 'uncommitted').write_text('x')
        with self.assertRaisesRegex(SystemExit, 'uncommitted'):
            command.main(['worktrees', 'apply', self.tree.name])
        (self.tree / 'uncommitted').unlink()
        git(self.tree, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'agent')
        git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'other')
        before = command.git(self.repo, 'rev-parse', 'HEAD').stdout
        with self.assertRaisesRegex(SystemExit, 'diverged'):
            command.main(['worktrees', 'apply', self.tree.name])
        self.assertEqual(command.git(self.repo, 'rev-parse', 'HEAD').stdout, before)

    def test_review_and_apply_select_latest_session_worktree(self):
        import agent
        with patch.object(agent, 'latest_session', return_value=(Path('/tmp/session'), self.tree)):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command.main(['worktrees', 'review'])
            self.assertIn(self.tree.name, out.getvalue())

    def test_apply_preserves_untracked_destination_collision(self):
        (self.tree / 'conflict').write_text('agent')
        git(self.tree, 'add', 'conflict')
        git(self.tree, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'agent')
        (self.repo / 'conflict').write_text('keep original')
        before = command.git(self.repo, 'rev-parse', 'HEAD').stdout
        with self.assertRaises(SystemExit):
            command.main(['worktrees', 'apply', self.tree.name])
        self.assertEqual((self.repo / 'conflict').read_text(), 'keep original')
        self.assertEqual(command.git(self.repo, 'rev-parse', 'HEAD').stdout, before)

    def test_apply_refuses_dirty_tracked_destination(self):
        (self.repo / 'tracked').write_text('base')
        git(self.repo, 'add', 'tracked')
        git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'base change')
        (self.repo / 'tracked').write_text('changed')
        with self.assertRaisesRegex(SystemExit, 'destination has tracked changes'):
            command.main(['worktrees', 'apply', self.tree.name])
        self.assertEqual((self.repo / 'tracked').read_text(), 'changed')

    def test_clean_worktree_is_dropped(self):
        (self.tree / 'note').write_text('x')
        with self.assertRaisesRegex(SystemExit, '1 uncommitted'):
            command.main(['worktrees', 'drop', self.tree.name])
        os.remove(self.tree / 'note')
        with contextlib.redirect_stdout(io.StringIO()):
            command.main(['worktrees', 'drop', self.tree.name])
        self.assertFalse(self.tree.exists())


class CompletionTests(unittest.TestCase):
    def test_script_completes_nested_commands_and_option_values(self):
        with patch.dict(os.environ):
            os.environ.pop('AGENTIAC_COMMAND', None)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command.main(['completion'])
        probe = out.getvalue() + '''
complete -p agentiac
t() { COMP_WORDS=("$@"); COMP_CWORD=$((${#COMP_WORDS[@]}-1)); COMPREPLY=(); _agentiac; echo "${COMPREPLY[*]}"; }
t agentiac ops st; t agentiac code -m rout; t agentiac release ""
'''
        result = subprocess.run(['bash', '-c', probe], capture_output=True, text=True, check=True)
        borrowed = ' '.join(n for n, h in HOSTS.items() if h.get('borrowed'))
        self.assertEqual(result.stdout.splitlines(),
                         ['complete -o default -F _agentiac agentiac',
                          'stop start storage', 'router', borrowed])

    def test_script_registers_invoked_command_name(self):
        with patch.dict(os.environ, {'AGENTIAC_COMMAND': 'my-site'}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                command.main(['completion'])
        probe = out.getvalue() + '''
complete -p my-site
if complete -p agentiac 2>/dev/null; then echo unexpected; fi
COMP_WORDS=(my-site ops st); COMP_CWORD=2; COMPREPLY=(); _my_site; echo "${COMPREPLY[*]}"
'''
        result = subprocess.run(['bash', '-c', probe], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(),
                         ['complete -o default -F _my_site my-site', 'stop start storage'])


if __name__ == '__main__':
    unittest.main()
