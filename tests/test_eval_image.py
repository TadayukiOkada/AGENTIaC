"""IP-13: the suite's image is compared with the Dockerfile it is built from, by hash label."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest import mock
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
import evalimage  # noqa: E402
from agentiac import suite as suite_settings  # noqa: E402

YEAR = 2026


def iso(month_day, rest):
    return f'{YEAR}-{month_day}{rest}'


def utc_stamp(month_day, hm):
    return f'{YEAR}-{month_day} {hm} UTC'


def image(digest=None, created=None, image_id='sha256:old'):
    labels = {evalimage.LABEL: digest} if digest else {'org.opencontainers.image.version': '24.04'}
    return {'Id': image_id, 'Created': created or iso('10-17', 'T01:33:47.219815325-04:00'), 'Config': {'Labels': labels}}


def done(payload=None, returncode=0):
    return mock.Mock(returncode=returncode, stdout=json.dumps(payload) if payload is not None else '')


class DockerfileHashTests(unittest.TestCase):
    def test_the_hash_is_the_dockerfile_as_committed(self):
        self.assertEqual(evalimage.dockerfile_hash(),
                         hashlib.sha256((ROOT / 'benchmarks/Dockerfile').read_bytes()).hexdigest())

    def test_the_hash_covers_everything_the_image_gets(self):
        # The claim that one hash is enough holds only while the Dockerfile copies nothing in. If it ever
        # does, the hash of the Dockerfile alone stops describing the image and this check must widen.
        lines = (ROOT / 'benchmarks/Dockerfile').read_text().splitlines()
        self.assertEqual([line for line in lines if line.split()[:1] in ('COPY', 'ADD')], [])


class TagTests(unittest.TestCase):
    def test_the_default_is_the_tag_the_suite_runs(self):
        with mock.patch.dict(evalimage.os.environ, {}, clear=True):
            self.assertEqual(evalimage.image_tag(), evalimage.FALLBACK_TAG)

    def test_an_overridden_suite_image_is_the_one_checked(self):
        with mock.patch.dict(evalimage.os.environ, {'AGENTIAC_EVAL_IMAGE': 'custom-eval:candidate'}):
            with mock.patch.object(evalimage.subprocess, 'run', return_value=done()) as run:
                state, detail = evalimage.check()
        self.assertEqual(state, 'drift')
        self.assertIn('custom-eval:candidate is not present', detail)
        self.assertEqual(run.call_args.args[0][-1], 'custom-eval:candidate')


class CheckTests(unittest.TestCase):
    def inspect_with(self, payload, returncode=0, side_effect=None):
        return mock.patch.object(evalimage.subprocess, 'run',
                                 side_effect=side_effect or [done(payload, returncode)])

    def test_the_current_dockerfile_is_ok(self):
        with self.inspect_with(image(evalimage.dockerfile_hash())):
            state, detail = evalimage.check(evalimage.FALLBACK_TAG)
        self.assertEqual(state, 'ok')
        self.assertIn(evalimage.dockerfile_hash()[:12], detail)

    def test_an_image_built_from_an_edited_dockerfile_is_drift_with_the_way_back(self):
        with self.inspect_with(image('deadbeef' * 8)):
            state, detail = evalimage.check(evalimage.FALLBACK_TAG)
        self.assertEqual(state, 'drift')
        self.assertIn('label deadbeefdead', detail)
        self.assertIn('eval-image build', detail)

    def test_an_image_with_no_label_says_what_is_unknown_and_which_commit_it_predates(self):
        unlabelled = image(created=iso('10-17', 'T01:33:47.219815325-04:00'))
        commit = mock.Mock(returncode=0, stdout=f"{iso('10-17', 'T05:44:59+00:00')}\n2122dee\n")
        with mock.patch.object(evalimage.subprocess, 'run', side_effect=[done(unlabelled), commit]):
            state, detail = evalimage.check(evalimage.FALLBACK_TAG)
        self.assertEqual(state, 'note')            # unknown, not wrong: nothing here says the image is stale
        self.assertIn('no Dockerfile-hash label', detail)
        self.assertIn(f"built {utc_stamp('10-17', '05:33')}", detail)
        self.assertIn(f"last committed {utc_stamp('10-17', '05:44')} in 2122dee", detail)
        self.assertIn('the image predates that commit', detail)

    def test_a_later_commit_is_not_called_a_predating_one(self):
        unlabelled = image(created=iso('10-20', 'T09:00:00.5-04:00'))
        commit = mock.Mock(returncode=0, stdout=f"{iso('10-17', 'T05:44:59+00:00')}\n2122dee\n")
        with mock.patch.object(evalimage.subprocess, 'run', side_effect=[done(unlabelled), commit]):
            _, detail = evalimage.check()
        self.assertNotIn('predates that commit', detail)

    def test_a_missing_image_is_drift(self):
        with self.inspect_with(None, returncode=1):
            state, detail = evalimage.check()
        self.assertEqual(state, 'drift')
        self.assertIn('is not present', detail)

    def test_a_machine_without_docker_is_nothing_to_act_on(self):
        with mock.patch.object(evalimage.subprocess, 'run', side_effect=FileNotFoundError('docker')):
            state, detail = evalimage.check()
        self.assertEqual(state, 'note')
        self.assertIn('docker is not available', detail)


class TimestampTests(unittest.TestCase):
    def test_docker_nanoseconds_and_git_utc_both_become_one_clock(self):
        self.assertEqual(evalimage.stamp(evalimage.utc(iso('10-17', 'T01:33:47.219815325-04:00'))),
                         utc_stamp('10-17', '05:33'))
        self.assertEqual(evalimage.stamp(evalimage.utc(iso('10-17', 'T05:44:59+00:00'))),
                         utc_stamp('10-17', '05:44'))

    def test_a_timestamp_nobody_can_read_is_said_so_rather_than_guessed(self):
        self.assertIsNone(evalimage.utc(''))
        self.assertEqual(evalimage.stamp(None), 'unknown')


class BuildTests(unittest.TestCase):
    def test_the_build_labels_with_the_hash_and_keeps_the_local_base(self):
        before, after = image(image_id='sha256:old'), image(evalimage.dockerfile_hash(), image_id='sha256:new')
        calls = [done(before), mock.Mock(returncode=0), done(after)]
        with mock.patch('builtins.print'), \
                mock.patch.object(evalimage.subprocess, 'run', side_effect=calls) as run:
            self.assertEqual(evalimage.build(evalimage.FALLBACK_TAG), 0)
        argv = run.call_args_list[1].args[0]
        self.assertEqual(argv[:2], ['docker', 'build'])
        self.assertIn('--pull=false', argv)        # the base layer the records name, not a fresh one
        self.assertIn(f'{evalimage.LABEL}={evalimage.dockerfile_hash()}', argv)
        self.assertTrue(argv[-1].endswith('/benchmarks'))

    def test_the_image_it_replaces_is_left_behind_and_named(self):
        with mock.patch('builtins.print') as echoed, \
                mock.patch.object(evalimage.subprocess, 'run',
                                  side_effect=[done(image(image_id='sha256:old')), mock.Mock(returncode=0),
                                               done(image(evalimage.dockerfile_hash(), image_id='sha256:new'))]):
            self.assertEqual(evalimage.build(evalimage.FALLBACK_TAG), 0)
        printed = '\n'.join(call.args[0] for call in echoed.call_args_list)
        self.assertIn('sha256:old', printed)
        self.assertIn('untagged and left in place', printed)

    def test_a_label_that_did_not_reach_the_image_fails_the_build(self):
        with mock.patch('builtins.print'), \
                mock.patch.object(evalimage.subprocess, 'run',
                                  side_effect=[done(image()), mock.Mock(returncode=0), done(image())]):
            self.assertEqual(evalimage.build(evalimage.FALLBACK_TAG), 1)

    def test_a_failed_build_reports_the_build_exit(self):
        with mock.patch.object(evalimage.subprocess, 'run',
                               side_effect=[done(image()), mock.Mock(returncode=17)]):
            self.assertEqual(evalimage.build(evalimage.FALLBACK_TAG), 17)


class CliTests(unittest.TestCase):
    def setUp(self):
        # A machine's images are not this class's concern: CI has docker and no suite image.
        patcher = mock.patch.object(evalimage, 'check_suite', return_value=('ok', 'locked'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_status_exit_follows_the_state(self):
        with mock.patch('builtins.print'), mock.patch.object(evalimage, 'check', return_value=('ok', 'x')):
            self.assertEqual(evalimage.main([]), 0)
        with mock.patch('builtins.print'), mock.patch.object(evalimage, 'check', return_value=('note', 'x')):
            self.assertEqual(evalimage.main([]), 0)
        with mock.patch('builtins.print'), mock.patch.object(evalimage, 'check', return_value=('drift', 'x')):
            self.assertEqual(evalimage.main([]), 1)

    def test_hash_and_tag_reach_the_functions(self):
        with mock.patch('builtins.print'), mock.patch.object(evalimage, 'dockerfile_hash', return_value='a' * 64):
            self.assertEqual(evalimage.main(['hash']), 0)
        with mock.patch('builtins.print'), mock.patch.object(evalimage, 'check', return_value=('ok', 'x')) as check:
            evalimage.main(['status', '--tag', 'other:1'])
        self.assertEqual(check.call_args.args[0], 'other:1')


class SuiteLockTests(unittest.TestCase):
    """The suite image is locked rather than labelled: a rebuild would move every case id (X18)."""

    IMAGE = {'Id': 'sha256:' + 'a' * 64}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / 'Dockerfile').write_text('FROM x\n')
        (root / 'requirements.txt').write_text('a==1\n')
        for name, value in (('ROOT', root), ('SUITE_DIR', root), ('SUITE_LOCK', root / 'image.lock.json')):
            patcher = mock.patch.object(evalimage, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = root

    def state(self, image=IMAGE):
        with mock.patch.object(evalimage, 'inspect', return_value=image):
            return evalimage.check_suite()

    def pin(self):
        with mock.patch.object(evalimage, 'inspect', return_value=self.IMAGE), mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(evalimage.pin_suite(), 0)

    def test_the_tag_is_the_one_the_suite_runs(self):
        self.assertEqual(evalimage.SUITE_TAG, suite_settings.IMAGE)

    def test_every_file_the_dockerfile_copies_is_a_locked_input(self):
        text = (evalimage.SUITE_DIR / 'Dockerfile').read_text()
        copied = {line.split()[1] for line in text.splitlines() if line.startswith(('COPY', 'ADD'))}
        self.assertLessEqual(copied, set(evalimage.SUITE_INPUTS))

    def test_a_site_without_a_task_suite_is_a_note(self):
        with mock.patch.object(evalimage, 'SUITE_DIR', self.root / 'missing'):
            state, detail = evalimage.check_suite()
        self.assertEqual(state, 'note')
        self.assertIn('no task suite', detail)

    def test_no_lock_is_a_note_with_the_way_forward(self):
        state, detail = self.state()
        self.assertEqual(state, 'note')
        self.assertIn('pin-suite', detail)

    def test_the_pinned_image_is_ok(self):
        self.pin()
        self.assertEqual(self.state()[0], 'ok')

    def test_a_rebuilt_image_is_drift_and_names_the_cost(self):
        self.pin()
        state, detail = self.state({'Id': 'sha256:' + 'b' * 64})
        self.assertEqual(state, 'drift')
        self.assertIn('case ids', detail)

    def test_an_edited_input_is_drift_for_either_file(self):
        self.pin()
        for name in ('Dockerfile', 'requirements.txt'):
            with self.subTest(name=name):
                before = (self.root / name).read_text()
                (self.root / name).write_text(before + '# edited\n')
                self.assertEqual(self.state()[0], 'drift')
                (self.root / name).write_text(before)

    def test_moving_text_between_the_inputs_is_a_change(self):
        first = evalimage.suite_inputs_hash()
        (self.root / 'Dockerfile').write_text('FROM x\na')
        (self.root / 'requirements.txt').write_text('==1\n')
        self.assertNotEqual(evalimage.suite_inputs_hash(), first)

    def test_a_missing_image_is_drift(self):
        self.pin()
        self.assertEqual(self.state(None)[0], 'drift')

    def test_no_docker_is_nothing_to_act_on(self):
        self.pin()
        with mock.patch.object(evalimage, 'inspect', side_effect=OSError('no docker')):
            self.assertEqual(evalimage.check_suite()[0], 'note')

    def test_status_reports_both_images_and_either_can_fail_it(self):
        out = io.StringIO()
        with mock.patch.object(evalimage, 'check', return_value=('ok', 'fine')), \
                mock.patch.object(evalimage, 'check_suite', return_value=('drift', 'moved')), mock.patch('sys.stdout', out):
            self.assertEqual(evalimage.main(['status']), 1)
        self.assertIn('moved', out.getvalue())


if __name__ == '__main__':
    unittest.main()
