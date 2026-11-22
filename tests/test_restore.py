"""IP-15 item 4: the mechanical half of restoring control is a script, and it stops at the human half.

Nothing here reaches the Drive remote: `fetch` is injected, and the archive is a real one built in a temporary
directory.  What the tests hold is the shape of the decision - the sha256 is not optional, the live tree is never
a target, evidence is rebuilt inside the restored checkout only, and the steps left to a human are the ones
docs/operations.md documents.
"""
import contextlib
import datetime
import importlib.util
import io
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from agentiac import backup, command  # noqa: E402


def fixture(root):
    """A small tree shaped like the real one: records, an install, a cache, a view and a live ledger."""
    files = {
        'artifacts/suite/state.sqlite': b'ledger',
        'artifacts/suite/state.sqlite-wal': b'half a page',
        'artifacts/suite/runs/c1/attempt-1/trace.jsonl': b'{}',
        'artifacts/suite/runs/c1/attempt-1/workspace/src/app.py': b'x',
        'artifacts/spark-flash-next/b1/report.json': b'{}',
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


def planned(*argv):
    """The argv the command would exec, without running it."""
    with patch.object(command, 'run', side_effect=lambda plan: plan) as run:
        command.main(list(argv))
    return run.call_args.args[0]

SPEC = importlib.util.spec_from_file_location('restore_control', ROOT / 'scripts' / 'restore-control.py')
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)

NEEDS_ZSTD = unittest.skipUnless(shutil.which('zstd'), 'zstd is not installed here')
NAME = 'control-backup-20261104T100000Z.tar.zst'


class Runs:
    """Records what the script would have run, and never runs it."""

    def __init__(self, returncode=0, stdout='rebuilt'):
        self.calls, self.returncode, self.stdout = [], returncode, stdout

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr='')


def checkout(parent, records=False):
    """A target that looks like a fresh clone: src/agentiac, and no records unless asked for."""
    where = Path(parent) / 'rebuilt'
    (where / 'src' / 'agentiac').mkdir(parents=True, exist_ok=True)
    if records:
        (where / 'artifacts' / 'suite').mkdir(parents=True)
        (where / 'artifacts' / 'suite' / 'state.sqlite').write_bytes(b'already here')
    return where


class TargetTests(unittest.TestCase):
    """`target` with a synthetic live tree, so both directions can be tested on one machine."""

    def live(self, directory):
        """A checkout that holds records and its own backup record: what `agentiac` runs on."""
        root = Path(directory) / 'live'
        (root / 'src' / 'agentiac').mkdir(parents=True, exist_ok=True)
        (root / 'artifacts' / 'suite').mkdir(parents=True)
        (root / 'artifacts' / 'backup.json').write_text('{}')
        return root

    def empty(self, directory, name='fresh'):
        root = Path(directory) / name
        (root / 'src' / 'agentiac').mkdir(parents=True, exist_ok=True)
        return root

    def test_the_tree_that_holds_the_records_is_refused_for_itself_and_for_its_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.live(directory)
            for path in (root, root / 'artifacts', root / 'artifacts' / 'restored-here'):
                with self.assertRaises(SystemExit, msg=str(path)) as caught:
                    restore.target(root, path, holds_the_records=True)
                self.assertRegex(str(caught.exception),
                                 'the tree the backup was taken from|sits inside the live artifacts tree')

    def test_a_host_that_holds_no_records_may_restore_into_the_very_checkout_it_runs_from(self):
        # This is the rebuilt-control case: the checkout was cloned a minute ago and the restore runs from it.
        with tempfile.TemporaryDirectory() as directory:
            root = self.empty(directory)
            self.assertEqual(restore.target(root, root), root.resolve())

    def test_a_fresh_checkout_may_restore_into_a_sibling_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(restore.target(self.empty(directory, 'where-the-script-lives'),
                                            self.empty(directory, 'rebuilt')),
                             self.empty(directory, 'rebuilt').resolve())

    def test_a_target_that_is_not_a_checkout_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit) as caught:
                restore.target(self.empty(directory), Path(directory) / 'nowhere')
            self.assertIn('not an agentiac checkout', str(caught.exception))

    def test_a_checkout_that_already_holds_records_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'sibling'
            (target / 'src' / 'agentiac').mkdir(parents=True, exist_ok=True)
            (target / 'artifacts' / 'suite').mkdir(parents=True, exist_ok=True)
            (target / 'artifacts' / 'suite' / 'state.sqlite').write_text('already here')
            with self.assertRaises(SystemExit) as caught:
                restore.target(self.empty(directory), target)
            self.assertIn('already holds records', str(caught.exception))

    def test_the_real_checkout_holds_records_and_is_refused_for_itself(self):
        with self.assertRaises(SystemExit) as caught:
            restore.target(restore.ROOT, restore.ROOT, holds_the_records=True)
        self.assertIn('the tree the backup was taken from', str(caught.exception))


class ChosenTests(unittest.TestCase):
    def test_a_recorded_archive_without_a_valid_digest_is_refused(self):
        for digest in (None, '', 'abc', 'z' * 64, 123):
            with self.subTest(digest=digest), self.assertRaisesRegex(SystemExit, 'valid 64-digit SHA256'):
                restore.chosen({'archive': NAME, 'sha256': digest}, None, None)

    def test_an_archive_name_cannot_escape_the_download_directory(self):
        for name in ('../outside.tar', '/tmp/outside.tar', 'sub/file.tar', '..', '', 'sub\\file.tar'):
            with self.subTest(name=name), self.assertRaisesRegex(SystemExit, 'single filename'):
                restore.chosen({'archive': name, 'sha256': 'a' * 64}, name, 'a' * 64)

    def test_nothing_recorded_and_nothing_given_is_a_refusal(self):
        with self.assertRaises(SystemExit) as caught:
            restore.chosen(None, None, None)
        self.assertIn('no backup record', str(caught.exception))

    def test_an_unrecorded_archive_has_to_carry_its_own_digest(self):
        with self.assertRaises(SystemExit) as caught:
            restore.chosen({'archive': NAME, 'sha256': 'a' * 64}, 'other.tar.zst', None)
        self.assertIn('must be given with --sha256', str(caught.exception))

    def test_the_recorded_archive_comes_with_the_recorded_digest(self):
        record = {'archive': NAME, 'sha256': 'a' * 64, 'destination': 'remote/x'}
        self.assertEqual(restore.chosen(record, None, None), (NAME, 'a' * 64))
        self.assertEqual(restore.chosen(record, NAME, 'b' * 64), (NAME, 'a' * 64))

    def test_another_archive_uses_the_digest_the_caller_gave(self):
        self.assertEqual(restore.chosen({'archive': NAME}, 'older.tar.zst', 'c' * 64), ('older.tar.zst', 'c' * 64))


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.where = Path(self.directory.name)
        self.source = self.where / 'source'
        fixture(self.source)
        self.fetched = []
        self.imported = []

    def archive(self):
        (self.source / 'artifacts' / 'suite' / 'state.sqlite').unlink()   # the fixture's placeholder bytes
        with sqlite3.connect(self.source / 'artifacts' / 'suite' / 'state.sqlite') as db:
            db.execute('create table attempts (id integer primary key, patch text)')
            db.execute('insert into attempts (patch) values ("8ca8211407c2")')
        built = backup.build(self.source, name=NAME,
                             when=datetime.datetime(2026, 11, 4, 10, 0, tzinfo=datetime.timezone.utc))
        path = self.source / 'artifacts' / 'backup' / NAME
        return path, built.get('sha256') or restore.sha256(path)

    def fetcher(self, path):
        def take(destination, name, into):
            self.fetched.append((destination, name))
            stage = into / '.restore'
            stage.mkdir(parents=True, exist_ok=True)
            target = stage / name
            shutil.copyfile(path, target)
            return target
        return take

    @NEEDS_ZSTD
    def test_it_downloads_checks_extracts_and_stops_at_the_human_steps(self):
        path, digest = self.archive()
        into = checkout(self.where)
        runs = Runs()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.main(['--into', str(into)], destination='remote/x', fetcher=self.fetcher(path),
                                runner=runs, record={'archive': NAME, 'sha256': digest, 'destination': 'remote/x',
                                                     'bytes': path.stat().st_size, 'files': 7},
                                importer=self.imported.append)
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual(self.fetched, [('remote/x', NAME)])
        self.assertIn('sha256     matches', text)
        self.assertIn('evidence   rebuilt: rebuilt inside the restored checkout', text)
        self.assertIn('what is left to a human', text)
        for command, _ in restore.HUMAN_STEPS:
            self.assertIn(command, text)
        self.assertEqual(self.imported, [into])
        restored = into / 'artifacts'
        self.assertTrue((restored / 'suite' / 'runs' / 'c1' / 'attempt-1' / 'workspace' / 'src' / 'app.py').exists())
        self.assertFalse((restored / 'frontends').exists())          # the set excluded the installs
        self.assertFalse((restored / 'evidence' / 'evidence.sqlite').exists())  # the view is rebuilt, not carried
        self.assertEqual(list((into / '.restore').iterdir()), [])    # the download is not left behind

    @NEEDS_ZSTD
    def test_a_digest_that_does_not_match_is_refused_and_nothing_is_extracted(self):
        path, _ = self.archive()
        into = checkout(self.where)
        with self.assertRaises(SystemExit) as caught:
            restore.main(['--into', str(into)], destination='remote/x', fetcher=self.fetcher(path),
                         runner=Runs(), record={'archive': NAME, 'sha256': 'b' * 64})
        self.assertIn('sha256 is', str(caught.exception))
        self.assertFalse((into / 'artifacts').exists())
        self.assertFalse((into / '.restore' / NAME).exists())

    @NEEDS_ZSTD
    def test_a_dry_run_fetches_nothing_and_writes_nothing(self):
        path, digest = self.archive()
        into = checkout(self.where)

        def never(destination, name, where):
            raise AssertionError(f'a dry run fetched {name}')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.main(['--into', str(into), '--dry-run'], destination='remote/x', fetcher=never,
                                runner=Runs(), record={'archive': NAME, 'sha256': digest})
        self.assertEqual((code, (into / 'artifacts').exists()), (0, False))
        self.assertIn('dry run', out.getvalue())

    def test_a_step_that_fails_is_said_and_not_raised(self):
        stage = self.where / 'pre'
        stage.mkdir(parents=True)
        made = stage / 'plain.tar'          # an uncompressed archive: open_archive branches on the suffix
        with tarfile.open(made, 'w') as archive:
            info = tarfile.TarInfo('artifacts/empty')
            info.size = 1
            archive.addfile(info, io.BytesIO(b'x'))
        digest = restore.sha256(made)
        into = checkout(self.where)
        (into / 'artifacts').mkdir()

        def take(destination, name, where):
            target = where / '.restore' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(made, target)
            return target

        def broken(root):
            raise RuntimeError('evidence import failed')

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.main(['--into', str(into), '--archive', 'plain.tar', '--sha256', digest],
                                destination='remote/x', fetcher=take, runner=Runs(), record=None, importer=broken)
        self.assertEqual(code, 0)
        self.assertIn('evidence   not rebuilt: RuntimeError: evidence import failed', out.getvalue())
        self.assertIn('what is left to a human', out.getvalue())



    def test_an_archive_that_writes_elsewhere_is_refused_by_name(self):
        stage = self.where / 'bad'
        stage.mkdir(parents=True)
        made = stage / 'bad.tar'
        with tarfile.open(made, 'w') as archive:
            for name in ('artifacts/good', '../escape'):
                info = tarfile.TarInfo(name)
                info.size = 1
                archive.addfile(info, io.BytesIO(b'x'))
        into = checkout(self.where)
        digest = restore.sha256(made)

        def take(destination, name, where):
            target = where / '.restore' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(made, target)
            return target

        with self.assertRaises(SystemExit) as caught:
            restore.main(['--into', str(into), '--archive', 'bad.tar', '--sha256', digest], destination='remote/x',
                         fetcher=take, runner=Runs(), record=None)
        self.assertIn("restore refused: the archive holds something a backup does not", str(caught.exception))
        self.assertIn("'../escape' is not a path under artifacts/", str(caught.exception))
        self.assertFalse((self.where / 'escape').exists())


    def test_a_host_with_no_local_record_reads_the_one_the_backup_published(self):
        path, digest = self.archive()
        into = checkout(self.where)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.main(['--into', str(into)], destination='remote/x', fetcher=self.fetcher(path),
                                runner=Runs(), loader=lambda root: None,
                                reader=lambda destination: {'archive': NAME, 'sha256': digest,
                                                            'destination': destination})
        self.assertEqual(code, 0)
        self.assertIn('record    read from the remote copy (this host has none)', out.getvalue())
        self.assertIn('sha256     matches', out.getvalue())

    def test_no_record_anywhere_is_a_refusal_that_names_what_to_pass(self):
        into = checkout(self.where)
        with self.assertRaises(SystemExit) as caught:
            restore.main(['--into', str(into)], destination='remote/x', fetcher=self.fetcher(Path('/dev/null')),
                         runner=Runs(), loader=lambda root: None, reader=lambda destination: None)
        self.assertIn('no backup record', str(caught.exception))
        self.assertIn('--archive NAME with --sha256 HEX', str(caught.exception))

    def test_the_remote_record_is_only_used_when_it_names_an_archive_and_a_digest(self):
        runs = Runs(stdout='{"archive": "a.tar.zst", "sha256": "' + 'a' * 64 + '"}')
        self.assertEqual(restore.remote_record('remote/x', runs)['archive'], 'a.tar.zst')
        for stdout, returncode in (('not json', 0), ('{"archive": "a.tar.zst"}', 0), ('{}', 0), ('', 1),
                                  ('[]', 0), ('null', 0), ('42', 0)):
            broken = Runs(returncode=returncode, stdout=stdout)
            self.assertIsNone(restore.remote_record('remote/x', broken), stdout or f'rc {returncode}')
