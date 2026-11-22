"""Merged agent branches and worktrees are found, squash merges included, and nothing unmerged is touched."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import command, doctor, hygiene  # noqa: E402

IDENTITY = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}


def git(repo, *argv):
    return subprocess.run(['git', '-C', str(repo), *argv], check=True, capture_output=True, text=True).stdout.strip()


class Repository(unittest.TestCase):
    """origin (bare) and a clone with main, like ~/src/agentiac and GitHub."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        environment = mock.patch.dict(os.environ, IDENTITY)
        environment.start()
        self.addCleanup(environment.stop)
        self.top = Path(tmp.name)
        origin, self.repo = self.top / 'origin.git', self.top / 'repo'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(origin)], check=True)
        subprocess.run(['git', 'clone', '-q', str(origin), str(self.repo)], check=True, capture_output=True)
        git(self.repo, 'checkout', '-q', '-b', 'main')
        (self.repo / '.gitignore').write_text('artifacts/\n.venv\n')
        git(self.repo, 'add', '.gitignore')
        git(self.repo, 'commit', '-q', '-m', 'base')
        git(self.repo, 'push', '-q', 'origin', 'main')

    def commit(self, where, name):
        (Path(where) / name).write_text(name)
        git(where, 'add', name)
        git(where, 'commit', '-q', '-m', name)
        return git(where, 'rev-parse', 'HEAD')

    def squash_merge(self, branch):
        """What GitHub's squash merge does: main gains one new commit with the branch's tree changes."""
        git(self.repo, 'merge', '-q', '--squash', branch)
        git(self.repo, 'commit', '-q', '-m', f'squash {branch}')
        git(self.repo, 'push', '-q', 'origin', 'main')

    def worktree(self, branch):
        path = self.top / branch.replace('/', '-')
        git(self.repo, 'worktree', 'add', '-q', '-b', branch, str(path), 'origin/main')
        return path

    def agent_branches(self):
        return git(self.repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/agent/').split()

    def survey(self, heads=None):
        return hygiene.survey(self.repo, heads, here=self.repo)


class SurveyTests(Repository):
    def test_a_squash_merged_branch_goes_only_when_github_names_its_tip(self):
        tree = self.worktree('agent/squashed')
        tip = self.commit(tree, 'work')
        self.squash_merge('agent/squashed')
        asked = self.survey({tip})
        self.assertEqual([item['branch'] for item in asked['remove']], ['agent/squashed'])
        self.assertEqual(asked['remove'][0]['worktree'], tree)
        unasked = self.survey(None)
        self.assertEqual(unasked['remove'], [])
        self.assertIn('GitHub not asked', unasked['keep'][0]['reason'])
        self.assertIn('GitHub was not asked', hygiene.screen(unasked))

    def test_a_commit_after_the_merge_keeps_the_branch(self):
        tree = self.worktree('agent/later')
        tip = self.commit(tree, 'work')
        self.squash_merge('agent/later')
        self.commit(tree, 'more work')
        report = self.survey({tip})
        self.assertEqual(report['remove'], [])
        self.assertEqual(report['keep'], [{'branch': 'agent/later', 'reason': 'not merged'}])

    def test_a_branch_main_already_contains_goes_without_github(self):
        git(self.repo, 'branch', 'agent/ancestor', 'origin/main')
        self.assertEqual(self.survey(None)['remove'][0]['how'], 'in origin/main')

    def test_a_worktree_with_untracked_files_is_kept_with_its_branch(self):
        tree = self.worktree('agent/dirty')
        (tree / 'notes.txt').write_text('unsaved')
        report = self.survey(set())
        self.assertEqual(report['remove'], [])
        self.assertIn('uncommitted or untracked', report['keep'][0]['reason'])

    def test_ignored_files_are_listed_but_the_environment_links_are_not(self):
        tree = self.worktree('agent/tested')
        (tree / 'artifacts').mkdir()
        (tree / 'artifacts/state.sqlite').write_text('leftover')
        (tree / '.venv').symlink_to(self.top)
        self.assertEqual(self.survey(set())['remove'][0]['ignored'], ['artifacts'])

    def test_only_agent_branches_are_considered(self):
        git(self.repo, 'branch', 'mine', 'origin/main')
        report = self.survey(None)
        self.assertEqual(report['remove'] + report['keep'], [])

    def test_the_worktree_this_runs_in_is_kept(self):
        tree = self.worktree('agent/here')
        report = hygiene.survey(self.repo, set(), here=tree / 'src')
        self.assertEqual(report['remove'], [])
        self.assertIn('where this runs', report['keep'][0]['reason'])

    def test_a_branch_checked_out_in_the_main_checkout_is_kept(self):
        git(self.repo, 'checkout', '-q', '-b', 'agent/on-main')
        report = hygiene.survey(self.repo, set(), here=self.top)
        self.assertEqual(report['remove'], [])
        self.assertIn('checked out in', report['keep'][0]['reason'])


class ApplyTests(Repository):
    def test_apply_removes_what_the_survey_listed_and_nothing_else(self):
        merged = self.worktree('agent/merged')
        tip = self.commit(merged, 'work')
        (merged / 'artifacts').mkdir()
        (merged / 'artifacts/leftover').write_text('x')
        self.squash_merge('agent/merged')
        kept = self.worktree('agent/kept')
        self.commit(kept, 'unmerged work')
        done = hygiene.apply(self.repo, self.survey({tip}))
        self.assertEqual(done, [f'removed worktree {merged}', f'deleted branch agent/merged (was {tip[:7]})'])
        self.assertFalse(merged.exists())
        self.assertTrue(kept.exists())
        self.assertEqual(self.agent_branches(), ['agent/kept'])

    def test_a_worktree_whose_directory_is_gone_is_forgotten(self):
        tree = self.worktree('agent/gone')
        subprocess.run(['rm', '-rf', str(tree)], check=True)
        report = self.survey(None)
        self.assertEqual(report['remove'][0]['worktree'], tree)
        hygiene.apply(self.repo, report)
        self.assertNotIn(str(tree), git(self.repo, 'worktree', 'list'))
        self.assertEqual(self.agent_branches(), [])


class MergedHeadsTests(unittest.TestCase):
    def test_the_heads_github_reports(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout=json.dumps([{'headRefOid': 'abc'}])))
        self.assertEqual(hygiene.merged_heads('/repo', run=run), {'abc'})
        self.assertEqual(run.call_args.args[0][:4], ['gh', 'pr', 'list', '--state'])

    def test_no_answer_is_none_not_empty(self):
        # None keeps squash-merged branches (ancestry only); an empty set would claim GitHub said "none merged".
        self.assertIsNone(hygiene.merged_heads('/repo', run=mock.Mock(side_effect=FileNotFoundError('gh'))))
        self.assertIsNone(hygiene.merged_heads('/repo', run=mock.Mock(return_value=mock.Mock(returncode=4, stdout=''))))
        self.assertIsNone(hygiene.merged_heads('/repo', run=mock.Mock(return_value=mock.Mock(returncode=0, stdout='<'))))


class DoctorTests(Repository):
    def test_merged_leftovers_are_a_note_naming_the_command(self):
        git(self.repo, 'branch', 'agent/done', 'origin/main')
        found = doctor.hygiene_check(self.repo, heads=set())
        self.assertEqual((found['status'], found['next']), ('note', f'{doctor.cli_name()} worktrees prune'))
        self.assertIn('1 merged agent/ branches (0 with a worktree)', found['evidence'])

    def test_work_in_progress_is_ok(self):
        tree = self.worktree('agent/active')
        self.commit(tree, 'work')
        self.assertEqual(doctor.hygiene_check(self.repo, heads=set())['status'], 'ok')


class CommandTests(Repository):
    def prune(self, *argv):
        args = command.build_parser().parse_args(['worktrees', 'prune', *argv])  # the parser reads the real inventory
        with mock.patch.object(command, 'ROOT', self.repo), \
                mock.patch.object(hygiene, 'merged_heads', return_value=set()), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            command.worktrees(args)
        return out.getvalue()

    def test_prune_lists_by_default_and_removes_only_with_apply(self):
        git(self.repo, 'branch', 'agent/done', 'origin/main')
        listing = self.prune()
        self.assertIn('remove  agent/done', listing)
        self.assertIn(f'{doctor.cli_name()} worktrees prune --apply', listing)
        self.assertEqual(self.agent_branches(), ['agent/done'])
        self.assertIn('deleted branch agent/done', self.prune('--apply'))
        self.assertEqual(self.agent_branches(), [])


if __name__ == '__main__':
    unittest.main()
