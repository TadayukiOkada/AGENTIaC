"""T6.7 / IP-15: the backup set keeps the records, drops what a lock reproduces, and reports its age.

Nothing here uploads anything. `rclone` is never run, and the one test that builds an archive does it in a
temporary directory with `--dry-run` semantics: it extracts the archive back and reads it.
"""
import datetime
import contextlib
import importlib.util
import shutil
import sqlite3
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import backup, declared, doctor  # noqa: E402

NOW = datetime.datetime(2026, 11, 4, 10, 0, tzinfo=datetime.timezone.utc)
NOW_ISO = NOW.isoformat(timespec='seconds')
UTC = datetime.timezone.utc
NEEDS_ZSTD = unittest.skipUnless(shutil.which('zstd'), 'zstd is not installed here')
SPEC = importlib.util.spec_from_file_location('backup_control', ROOT / 'scripts/backup-control.py')
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)


def fixture(root):
    """A small tree shaped like the real one: records, an install, a cache, a view and a live ledger."""
    files = {
        'artifacts/suite/state.sqlite': b'ledger',
        'artifacts/suite/state.sqlite-wal': b'half a page',
        'artifacts/suite/runs/c1/attempt-1/trace.jsonl': b'{}',
        'artifacts/suite/runs/c1/attempt-1/workspace/src/app.py': b'x',
        'artifacts/eval-run/b1/report.json': b'{}',
        'artifacts/t9-6/compare.py': b'print(1)',
        'artifacts/evidence/evidence.sqlite': b'view',
        'artifacts/frontends/pi/node_modules/pi/index.js': b'rebuilt from a lock',
        'artifacts/uv-cache/wheels/x.whl': b'redownloadable',
        'artifacts/corpora/corpus.jsonl': b'rederivable',
        'artifacts/backends/llama.cpp': b'rebuilt by ops build',
        'artifacts/tmp/scratch': b'scratch',
        'artifacts/backup/control-backup-old.tar.zst': b'the staging copy of itself',
        'secrets/gateway.env': b'a key',
    }
    for name, body in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


class SetTests(unittest.TestCase):
    def setUp(self):
        self.context = tempfile.TemporaryDirectory()
        self.root = Path(self.context.name)
        fixture(self.root)
        self.addCleanup(self.context.cleanup)

    def test_the_records_are_in_the_set(self):
        found = backup.entries(self.root)
        self.assertIn('suite/runs/c1/attempt-1/trace.jsonl', found)
        self.assertIn('suite/runs/c1/attempt-1/workspace/src/app.py', found)
        self.assertIn('eval-run/b1/report.json', found)
        self.assertIn('t9-6/compare.py', found)

    def test_what_a_lock_or_a_build_reproduces_is_not(self):
        found = backup.entries(self.root)
        self.assertFalse([path for path in found if path.startswith(('frontends/', 'uv-cache/', 'corpora/',
                                                                     'backends/', 'tmp/', 'backup/'))], found)

    def test_the_view_is_left_out_because_the_documented_restore_rebuilds_it(self):
        self.assertNotIn('evidence/evidence.sqlite', backup.entries(self.root))

    def test_the_live_database_side_files_are_left_out(self):
        found = backup.entries(self.root)
        self.assertIn('suite/state.sqlite', found)
        self.assertNotIn('suite/state.sqlite-wal', found)
        self.assertNotIn('suite/state.sqlite-shm', found)

    def test_a_directory_new_since_the_decision_is_kept_until_someone_calls_it_reproducible(self):
        (self.root / 'artifacts/a-new-evidence-dir').mkdir()
        (self.root / 'artifacts/a-new-evidence-dir/trace.jsonl').write_bytes(b'{}')
        self.assertIn('a-new-evidence-dir/trace.jsonl', backup.entries(self.root))

    def test_nothing_outside_artifacts_is_ever_listed(self):
        self.assertFalse([path for path in backup.entries(self.root) if 'secrets' in path])

    def test_an_empty_tree_refuses_rather_than_reporting_a_zero_file_backup(self):
        with tempfile.TemporaryDirectory() as empty:
            (Path(empty) / 'artifacts').mkdir()
            with self.assertRaises(ValueError):
                backup.entries(empty)

    def test_plan_counts_what_entries_lists(self):
        found = backup.entries(self.root)
        self.assertEqual(backup.plan(self.root)['files'], len(found))
        self.assertEqual(backup.plan(self.root)['set'], backup.SET)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.context = tempfile.TemporaryDirectory()
        self.root = Path(self.context.name)
        fixture(self.root)
        self.addCleanup(self.context.cleanup)
        (self.root / 'artifacts/suite/state.sqlite').unlink()      # the fixture's placeholder bytes
        with sqlite3.connect(self.root / 'artifacts/suite/state.sqlite') as db:
            db.execute('CREATE TABLE cases (id TEXT)')
            db.execute("INSERT INTO cases VALUES ('c1')")

    @NEEDS_ZSTD
    def test_the_archive_carries_the_ledger_as_a_consistent_snapshot(self):
        built = backup.build(self.root, name='control-backup-20261104T100000Z.tar.zst')
        out = self.root / 'artifacts/backup/control-backup-20261104T100000Z.tar.zst'
        self.assertTrue(out.exists())
        self.assertEqual(built['bytes'], out.stat().st_size)
        self.assertEqual(len(built['sha256']), 64)
        archive, child = backup.open_archive(out)
        names = {member.name for member in archive}
        archive.close()
        if child:
            child.wait()
            child.stdout.close()
        self.assertIn('artifacts/suite/runs/c1/attempt-1/trace.jsonl', names)
        self.assertIn('artifacts/suite/state.sqlite', names)
        self.assertNotIn('artifacts/suite/state.sqlite-wal', names)
        self.assertFalse([name for name in names if name.startswith('artifacts/frontends/')], names)
        archive, child = backup.open_archive(out)
        handle = None
        for member in archive:
            if member.name == 'artifacts/suite/state.sqlite':
                handle = archive.extractfile(member)
                break
        snapshot = Path(self.context.name) / 'snapshot.sqlite'
        snapshot.write_bytes(handle.read() if handle else b'')
        archive.close()
        if child:
            child.wait()
            child.stdout.close()
        with sqlite3.connect(snapshot) as db:
            self.assertEqual(db.execute('SELECT id FROM cases').fetchall(), [('c1',)])

    @NEEDS_ZSTD
    def test_the_staging_copy_is_removed_from_the_tree_it_staged_in(self):
        backup.build(self.root, name='control-backup-20261104T100000Z.tar.zst')
        archive, child = backup.open_archive(self.root / 'artifacts/backup/control-backup-20261104T100000Z.tar.zst')
        names = [member.name for member in archive]
        archive.close()
        if child:
            child.wait()
            child.stdout.close()
        self.assertFalse([name for name in names if 'backup/control-backup' in name], names)

    def test_a_failed_compressor_cannot_be_reported_as_a_backup(self):
        # Consume all input and write some output, then fail: no BrokenPipeError masks the exit status.
        compressor = (sys.executable, '-c',
                      'import pathlib,sys; sys.stdin.buffer.read(); '
                      'pathlib.Path(sys.argv[2]).write_bytes(b"incomplete"); sys.exit(7)')
        with patch.object(backup, 'ZSTD', compressor):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                backup.build(self.root, name='failed.tar.zst')
        self.assertEqual(caught.exception.returncode, 7)
        self.assertFalse((self.root / 'artifacts/backup/failed.tar.zst').exists())
        self.assertFalse((self.root / 'artifacts/backup/failed.tar.zst.state.sqlite').exists())

    @NEEDS_ZSTD
    def test_an_unreadable_record_is_not_silently_omitted(self):
        with patch.object(tarfile.TarFile, 'add', side_effect=PermissionError('unreadable evidence')):
            with self.assertRaisesRegex(PermissionError, 'unreadable evidence'):
                backup.build(self.root, name='unreadable.tar.zst')

    def test_a_complete_tar_with_a_failed_decompressor_is_not_a_verified_archive(self):
        path = self.root / 'stream.tar.zst'
        with tarfile.open(path, 'w') as archive:
            info = tarfile.TarInfo('artifacts/record')
            info.size = 1
            archive.addfile(info, io.BytesIO(b'x'))
        decoder = [sys.executable, '-c',
                   'import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[-1]).read_bytes()); '
                   'sys.stdout.buffer.flush(); sys.exit(7)']
        popen = subprocess.Popen
        with patch.object(backup.subprocess, 'Popen', side_effect=lambda argv, **kw: popen(decoder + [argv[-1]], **kw)):
            with self.assertRaisesRegex(ValueError, 'decompressor exited'):
                with backup.reading(path) as archive:
                    backup.extract(archive, self.root / 'restored')


class VerifyTests(unittest.TestCase):
    def test_a_stored_backup_records_partial_pruning_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / backup.STAGING
            stage.mkdir(parents=True)
            (stage / 'new.tar.zst').write_bytes(b'archive')
            built = {'archive': 'new.tar.zst', 'bytes': 7, 'files': 1, 'sha256': 'a' * 64}
            with patch.object(control, 'ROOT', root), patch.object(backup, 'build', return_value=built), \
                    patch.object(control, 'rclone', return_value=subprocess.CompletedProcess([], 0)), \
                    patch.object(control, 'remote_size', return_value=(7, None)), \
                    patch.object(control, 'remote_names', return_value=['old', 'new']), \
                    patch.object(backup, 'retention', return_value=(['new'], ['old'])), \
                    patch.object(control, 'prune_remote', return_value=(0, ['old'])), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(control.do_run('remote/backup', 4, False), 1)
            record = backup.load(root)
            self.assertEqual((record['pruned'], record['kept'], record['prune_failed']), (0, 2, ['old']))
            self.assertEqual(record['sha256'], built['sha256'])

    def test_pruning_reports_actual_deletions_and_returns_failure_on_partial_success(self):
        with patch.object(control, 'remote_names', return_value=['old-a', 'old-b']), \
                patch.object(backup, 'retention', return_value=(['new'], ['old-a', 'old-b'])), \
                patch.object(control, 'rclone', side_effect=[subprocess.CompletedProcess([], 0),
                                                            subprocess.CompletedProcess([], 1)]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(control.do_prune('remote/backup', 4), 1)
        self.assertIn('removed 1', output.getvalue())
        self.assertIn('old-b', output.getvalue())

    def test_patch_verification_uses_the_ledger_attempt_not_lexicographic_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'artifacts/suite/state.sqlite'
            state.parent.mkdir(parents=True)
            with sqlite3.connect(state) as db:
                db.executescript('CREATE TABLE cases (id TEXT, spec TEXT, attempts INTEGER); '
                                 'CREATE TABLE attempts (case_id TEXT, number INTEGER, result TEXT);')
                db.execute("INSERT INTO cases VALUES ('case', '{}', 10)")
                db.execute('INSERT INTO attempts VALUES (?, ?, ?)',
                           ('case', 10, '{"provenance":{"config_id":"config"}}'))
            tree = root / 'artifacts/suite/runs/case'
            for number in (9, 10):
                (tree / f'attempt-{number}/workspace/.git').mkdir(parents=True)
            paths = []

            def git(argv, **kwargs):
                paths.append(Path(argv[argv.index('-C') + 1]))
                return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

            with patch.object(control.subprocess, 'run', side_effect=git):
                self.assertIn('case', control.patch_hashes(root, 'config'))
            self.assertEqual(set(paths), {tree / 'attempt-10/workspace'})

    def test_a_digest_mismatch_stops_before_extracting_or_running_restored_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = 'bad.tar.zst'
            downloaded = root / backup.STAGING / 'verify' / name

            def download(*args, **kwargs):
                downloaded.write_bytes(b'wrong bytes')
                return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

            record = {'archive': name, 'sha256': '0' * 64, 'bytes': 11}
            with patch.object(control, 'ROOT', root), patch.object(backup, 'load', return_value=record), \
                    patch.object(control, 'rclone', side_effect=download), \
                    patch.object(backup, 'open_archive') as opened, \
                    patch.object(control, 'content_test') as executed, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(control.do_verify('remote/backup', 4), 1)
            opened.assert_not_called()
            executed.assert_not_called()
            self.assertFalse(downloaded.exists())


class RetentionTests(unittest.TestCase):
    NAMES = [f'control-backup-2026{month:02d}15T043000Z.tar.zst' for month in range(1, 12)]

    def test_the_newest_are_kept_and_the_oldest_go(self):
        keep, gone = backup.retention(self.NAMES, keep=4, months=6, now=datetime.datetime(2026, 11, 4))
        self.assertGreaterEqual(len(keep), 4)
        self.assertEqual(sorted(set(keep) & set(gone), ), [])
        self.assertIn('control-backup-20261115T043000Z.tar.zst', keep)
        self.assertIn('control-backup-20260115T043000Z.tar.zst', gone)

    def test_one_archive_per_kept_month_survives(self):
        keep, _ = backup.retention(self.NAMES, keep=4, months=6, now=datetime.datetime(2026, 11, 4))
        months = {backup.stamp(name).strftime('%Y-%m') for name in keep}
        expected = {datetime.date(2026, month, 1).strftime('%Y-%m') for month in range(5, 11)}
        self.assertTrue(expected <= months, keep)

    def test_a_name_it_cannot_read_a_time_from_is_never_deleted(self):
        keep, gone = backup.retention(self.NAMES + ['someone-elses-file.bin'], keep=4, months=6,
                                      now=datetime.datetime(2026, 11, 4))
        self.assertIn('someone-elses-file.bin', keep)
        self.assertNotIn('someone-elses-file.bin', gone)

    def test_the_stamp_round_trips_the_name_the_build_writes(self):
        name = backup.archive_name(datetime.datetime(2026, 11, 4, 10, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(name, 'control-backup-20261104T100000Z.tar.zst')
        self.assertEqual(backup.stamp(name).year, 2026)
        self.assertIsNone(backup.stamp('other.tar.zst'))

    def test_the_monthly_copy_is_the_earliest_even_when_weeklies_cover_that_month(self):
        names = [f'control-backup-202611{day:02d}T043000Z.tar.zst' for day in (1, 8, 15, 22, 29)]
        keep, gone = backup.retention(names, keep=2, now=datetime.datetime(2026, 11, 30))
        self.assertEqual(keep, [names[0], names[3], names[4]])
        self.assertEqual(gone, [names[1], names[2]])


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.context = tempfile.TemporaryDirectory()
        self.root = Path(self.context.name)
        self.addCleanup(self.context.cleanup)

    def record(self, at=NOW_ISO, **extra):
        stored = {'schema_version': 1, 'set': backup.SET, 'destination': backup.DESTINATION,
                  'archive': 'control-backup-20261104T100000Z.tar.zst', 'files': 170901,
                  'bytes': 1181116006, 'sha256': 'a' * 64, 'interval_days': 7}
        stored.update(extra)
        stored['at'] = at
        (self.root / 'artifacts').mkdir(parents=True, exist_ok=True)
        (self.root / backup.RECORD).write_text(__import__('json').dumps(stored))
        return stored

    def test_no_record_is_a_note_that_names_the_command(self):
        found = doctor.backup_check(self.root)
        self.assertEqual(found['status'], 'note')
        self.assertIn('ops backup run', found['next'])

    def test_a_fresh_record_is_ok_and_says_size_place_and_age(self):
        self.record()
        found = doctor.backup_check(self.root, now=NOW)
        self.assertEqual(found['status'], 'note', found)      # stored, never verified back
        self.assertIn('170901 files', found['evidence'])
        self.assertIn(backup.DESTINATION, found['evidence'])
        self.assertIn('days ago', found['evidence'])

    def test_a_record_verified_back_is_ok(self):
        self.record(verify={'at': NOW_ISO, 'sha256_match': True, 'content': 'pass'})
        self.assertEqual(doctor.backup_check(self.root, now=NOW)['status'], 'ok')

    def test_three_missed_weeks_is_a_problem(self):
        self.record(at=datetime.datetime(2026, 10, 1, 10, 0, tzinfo=UTC).isoformat(),
                    verify={'at': NOW_ISO, 'sha256_match': True})
        found = doctor.backup_check(self.root, now=NOW)
        self.assertEqual(found['status'], 'problem', found)
        self.assertIn(declared.resource('backup.timer'), found['next'])

    def test_a_record_from_a_different_set_is_a_note(self):
        self.record(set='A', verify={'at': NOW_ISO, 'sha256_match': True})
        found = doctor.backup_check(self.root, now=NOW)
        self.assertEqual(found['status'], 'note', found)
        self.assertIn('set A', found['evidence'])

    def test_a_record_without_a_hash_cannot_be_verified(self):
        self.record(sha256=None)
        found = doctor.backup_check(self.root, now=NOW)
        self.assertEqual(found['status'], 'note', found)
        self.assertIn('ops backup run', found['next'])

    def test_a_corrupt_record_is_a_note_not_a_crash(self):
        (self.root / 'artifacts').mkdir(parents=True, exist_ok=True)
        (self.root / backup.RECORD).write_text('not json at all')
        self.assertEqual(doctor.backup_check(self.root, now=NOW)['status'], 'note')

    def test_a_restore_test_two_months_old_is_a_note_naming_the_monthly_timer(self):
        self.record(verify={'at': datetime.datetime(2026, 9, 1, 10, 0, tzinfo=UTC).isoformat(), 'sha256_match': True})
        found = doctor.backup_check(self.root, now=NOW)
        self.assertEqual(found['status'], 'note', found)
        self.assertIn('last restore test 64 days ago', found['evidence'])
        self.assertIn(declared.resource('backup-verify.timer'), found['next'])


class InstalledTests(unittest.TestCase):
    """The unit and its timer are part of the deploy, so their removal is a test failure and not a surprise."""

    def service(self):
        return (ROOT / 'infra/templates/backup.service.j2').read_text()

    def test_the_service_runs_the_command_and_has_room_for_an_upload(self):
        text = self.service()
        self.assertIn('ops backup run', text)
        self.assertIn('TimeoutStartSec=7200', text)
        self.assertIn('ReadWritePaths={{ instance_root }}/artifacts', text)

    def test_the_playbook_installs_and_enables_the_backup_units(self):
        playbook = (ROOT / 'infra/control.yml').read_text()
        for name in ('backup.service', 'backup.timer', 'backup-verify.service', 'backup-verify.timer'):
            self.assertIn(name, playbook)
        enabled = playbook.split('loop: [')[1]
        for name in ('backup.timer', 'backup-verify.timer'):
            self.assertIn(name, enabled)

    def test_the_script_refuses_to_upload_a_hash_it_did_not_check(self):
        text = (ROOT / 'scripts/backup-control.py').read_text()
        self.assertIn('remote copy does not match', text)
        self.assertIn('--checksum', text)

    def test_the_destination_is_the_owner_drive_folder_and_not_the_repo(self):
        self.assertEqual(backup.DESTINATION, declared.setting('backup_destination'))   # D8.2: the site declares it
        self.assertNotIn('secrets', backup.DESTINATION)

    def test_the_restore_test_runs_on_its_own_monthly_timer(self):
        timer = (ROOT / 'infra/templates/backup-verify.timer.j2').read_text()
        self.assertIn('OnCalendar=*-*-08 05:10:00', timer)      # a day-of-month only: exactly once a month
        self.assertIn('Persistent=true', timer)
        self.assertIn('{{ resource_prefix }}-backup-verify.service', timer)
        playbook = (ROOT / 'infra/control.yml').read_text()
        for name in ('backup-verify.service', 'backup-verify.timer'):
            self.assertIn(name, playbook)
        service = (ROOT / 'infra/templates/backup-verify.service.j2').read_text()
        self.assertIn('ops backup verify', service)

    def test_two_backup_jobs_cannot_share_the_staging_directory(self):
        text = (ROOT / 'scripts/backup-control.py').read_text()
        self.assertIn('fcntl.flock', text)
        self.assertIn('another backup job holds the staging directory', text)


class ScreenTests(unittest.TestCase):
    class Stub:
        def sections(self, hosts):
            return []

        def check_host(self, *item):
            raise AssertionError('summary must not reach a host')

    def test_backup_replaced_the_undecided_note_in_the_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            found = {item['id']: item for item in doctor.summary(directory, {}, lambda: (True, 'ok'),
                                                                 drift=self.Stub(), preflight=self.Stub())['checks']}
        self.assertIn('backup', found)
        self.assertNotIn('decide IP-15 (T6.7)', found['backup'].get('next') or '')


class ExtractTests(unittest.TestCase):
    """`backup.extract` is the guard both the restore and the verify run through: an archive that would
    write outside the tree it is being put into is refused, member by member, on any interpreter."""

    def archive(self, directory, members):
        """members: (name, type, linkname) tuples written into a plain tar."""
        path = Path(directory) / 'test.tar'
        with tarfile.open(path, 'w') as archive:
            for name, kind, link in members:
                info = tarfile.TarInfo(name)
                if kind == 'file':
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b'x'))
                elif kind == 'dir':
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    archive.addfile(info)
                elif kind == 'symlink':
                    info.type = tarfile.SYMTYPE
                    info.linkname = link
                    archive.addfile(info)
                elif kind == 'hardlink':
                    info.type = tarfile.LNKTYPE
                    info.linkname = link
                    archive.addfile(info)
                elif kind == 'device':
                    info.type = tarfile.CHRTYPE
                    archive.addfile(info)
        return path

    def extracted(self, directory, members):
        path = self.archive(directory, members)
        where = Path(directory) / 'into'
        where.mkdir(parents=True, exist_ok=True)
        handle = backup.open_archive(path)[0]
        try:
            count = backup.extract(handle, where)
        finally:
            handle.close()
        return count, where

    def test_the_paths_a_backup_actually_holds_are_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            count, where = self.extracted(directory, [('artifacts/suite/state.sqlite', 'file', ''),
                                                      ('artifacts/suite', 'dir', ''),
                                                      ('artifacts/suite/runs', 'dir', '')])
            self.assertEqual(count, 3)
            self.assertTrue((where / 'artifacts/suite/state.sqlite').exists())

    def test_a_member_that_climbs_out_of_the_tree_is_refused(self):
        # PurePosixPath normalizes './', so './artifacts/escape' is a path inside the tree and is allowed.
        # Everything below is either outside artifacts/ or climbs out of it.
        for name in ('../escape', 'artifacts/../../escape', '/tmp/escape', 'other/thing', 'artifacts/./../escape',
                     'artifacts', 'artifacts/', 'escape'):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'not a path under artifacts/|reaches outside', msg=name):
                    self.extracted(directory, [(name, 'file', '')])

    def test_a_symlink_that_points_outside_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'outside the tree'):
                self.extracted(directory, [('artifacts/link', 'symlink', '/etc/passwd')])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'outside the tree'):
                self.extracted(directory, [('artifacts/link', 'symlink', '../../etc/passwd')])

    def test_a_symlink_inside_the_tree_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            self.extracted(directory, [('artifacts/keep', 'file', ''), ('artifacts/link', 'symlink', 'keep')])
            self.assertTrue((Path(directory) / 'into/artifacts/link').is_symlink())

    def test_a_hardlink_to_a_checkout_file_outside_artifacts_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / 'into/secrets/key'
            secret.parent.mkdir(parents=True)
            secret.write_text('keep private')
            with self.assertRaisesRegex(ValueError, 'outside artifacts/'):
                self.extracted(directory, [('artifacts/leak', 'hardlink', 'secrets/key')])
            self.assertEqual(secret.read_text(), 'keep private')
            self.assertFalse((secret.parents[1] / 'artifacts/leak').exists())

    def test_an_existing_directory_symlink_cannot_redirect_a_member(self):
        with tempfile.TemporaryDirectory() as directory:
            where = Path(directory) / 'into'
            (where / 'artifacts').mkdir(parents=True)
            outside = Path(directory) / 'outside'
            outside.mkdir()
            (where / 'artifacts/redirect').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'outside artifacts/'):
                self.extracted(directory, [('artifacts/redirect/escape', 'file', '')])
            self.assertFalse((outside / 'escape').exists())

    def test_hardlinks_inside_artifacts_remain_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            _, where = self.extracted(directory, [('artifacts/keep', 'file', ''),
                                                   ('artifacts/link', 'hardlink', 'artifacts/keep')])
            self.assertEqual((where / 'artifacts/keep').stat().st_ino, (where / 'artifacts/link').stat().st_ino)

    def test_an_interpreter_without_safe_extraction_refuses_before_writing(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tarfile, 'data_filter'):
            del tarfile.data_filter
            with self.assertRaisesRegex(ValueError, 'security-patched Python'):
                self.extracted(directory, [('artifacts/file', 'file', '')])

    def test_a_device_or_a_hole_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'neither a file, a directory, nor a link'):
                self.extracted(directory, [('artifacts/node', 'device', '')])

    def test_refusal_stops_before_the_bad_member_touches_the_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.extracted(directory, [('artifacts/good', 'file', ''), ('../escape', 'file', '')])
            self.assertTrue((Path(directory) / 'into/artifacts/good').exists())
            self.assertFalse((Path(directory) / 'escape').exists())


class RecordTravelTests(unittest.TestCase):
    """A rebuilt host has no local record, so the run has to publish the record next to the archives and say so
    when it cannot - a digest taken from inside the archive would verify nothing."""

    def test_the_run_publishes_the_record_to_the_remote(self):
        text = (ROOT / 'scripts' / 'backup-control.py').read_text()
        self.assertIn("f'{destination}/backup.json'", text)
        self.assertIn('the record is on this host but not on the remote', text)

    def test_the_restore_reads_that_copy_when_the_host_has_none(self):
        text = (ROOT / 'scripts' / 'restore-control.py').read_text()
        self.assertIn("rclone', 'cat', f'{destination}/backup.json'", text)
        self.assertIn('no backup record; pass --archive NAME with --sha256 HEX', text)
