"""`retire-worker.py`: the part that runs on the host, over a real filesystem, on a scale a test can afford.

The gate in `src/agentiac/retire.py` decides from facts someone measured. These tests are about the measuring
and the moving: that a thin leaves a root whose file set is what §4.1 says it is, that a restore's copy is on its
own inode (F8, ADR 0008), and that a shared inode is reported rather than counted as reclaimable.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('retire_worker', ROOT / 'scripts/retire-worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

NAMES = {'q8_0.gguf': 'gyro-M/q8_0.gguf', 'q4_k.gguf': 'gyro-M/q4_k.gguf'}   # as backends/*.json names them


def fill(where, bodies):
    where = Path(where)
    for name, body in bodies.items():
        file = where / 'data/models' / NAMES[name]
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(body)
    return where


def manifest_at(where, pins):
    """The manifest's own list of pins, in the shape `backends/<stem>.json` writes it."""
    files = [{'path': NAMES[name], 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
             for name, body in pins.items()]
    out = Path(where) / 'manifest.json'
    out.write_text(json.dumps({'source_version': '0.2.8', 'files': files}))
    return out


PINNED = {name: b'x' * 100 for name in NAMES}      # the bytes a manifest pins, and what a root is copied from
HEAVY = {name: b'weight ' * 200 for name in NAMES}  # the same shape, large enough for a move to be visible


def tree():
    """A serving root, a trial root prepared from it, the manifest that pins both, and a staging directory."""
    with tempfile.TemporaryDirectory() as where:
        base = Path(where)
        serving = fill(base / 'serving' / 'strata-0.2.11', HEAVY)
        target = fill(base / 'target' / 'strata-0.2.8', HEAVY)
        manifest = manifest_at(base, HEAVY)
        staging = base / 'retired'
        staging.mkdir()
        yield target, serving, manifest, staging


class WeightStateTests(unittest.TestCase):
    def states(self, root_body=None, serving_body=None, pins=None, **over):
        with tempfile.TemporaryDirectory() as where:
            base = Path(where)
            target = fill(base / 'target', {name: (root_body or {}).get(name, b'x' * 100) for name in NAMES})
            serving = fill(base / 'serving', {name: (serving_body or {}).get(name, b'x' * 100) for name in NAMES})
            # The pin is the serving root's bytes as they were when the trial root was prepared (`same_as` in
            # backends/*.json): that copy apart is what §3.2 asks for and what a restore puts back.
            manifest = manifest_at(base, pins or PINNED)
            yield worker.weight_states(str(target), [str(serving)], str(manifest), **over)

    def test_every_weight_agreeing_is_identical(self):
        for states in self.states():
            self.assertEqual({name: entry['state'] for name, entry in states.items()},
                             {'q8_0.gguf': 'identical', 'q4_k.gguf': 'identical'})

    def test_a_root_whose_bytes_are_not_the_pinned_ones_is_refused_by_name(self):
        for states in self.states(root_body={'q8_0.gguf': b'y' * 100}):    # same length, different bytes
            self.assertEqual(states['q8_0.gguf']['state'], 'hash-differs-from-pin')
            self.assertEqual(states['q4_k.gguf']['state'], 'identical')

    def test_a_serving_root_that_does_not_hold_the_pin_is_its_own_state(self):
        for states in self.states(serving_body={'q4_k.gguf': b'z' * 100}):
            self.assertEqual(states['q4_k.gguf']['state'], 'copy-apart-differs')

    def test_a_weight_shared_by_inode_is_its_own_state(self):
        with tempfile.TemporaryDirectory() as where:
            base = Path(where)
            target = fill(base / 'target', PINNED)
            serving = fill(base / 'serving', PINNED)
            os.link(str(target / 'data/models' / NAMES['q8_0.gguf']), str(base / 'elsewhere.gguf'))
            manifest = manifest_at(base, PINNED)
            states = worker.weight_states(str(target), [str(serving)], str(manifest))
            self.assertEqual(states['q8_0.gguf']['state'], 'shared-inode')
            self.assertEqual(states['q8_0.gguf']['nlink'], 2)

    def test_a_missing_weight_is_reported_as_missing_not_as_zero(self):
        with tempfile.TemporaryDirectory() as where:
            base = Path(where)
            target = fill(base / 'target', PINNED)
            (target / 'data/models' / NAMES['q8_0.gguf']).unlink()
            serving = fill(base / 'serving', PINNED)
            manifest = manifest_at(base, PINNED)
            states = worker.weight_states(str(target), [str(serving)], str(manifest))
            self.assertEqual(states['q8_0.gguf']['state'], 'missing-or-ambiguous')
            self.assertIsNone(states['q8_0.gguf']['path'])

    def test_a_weight_of_the_wrong_size_is_not_the_pinned_one(self):
        with tempfile.TemporaryDirectory() as where:
            base = Path(where)
            target = fill(base / 'target', PINNED)
            serving = fill(base / 'serving', PINNED)
            manifest = manifest_at(base, PINNED)
            (target / 'data/models' / NAMES['q8_0.gguf']).write_bytes(b'x' * 55)
            states = worker.weight_states(str(target), [str(serving)], str(manifest))
            self.assertEqual(states['q8_0.gguf']['state'], 'size-differs')


class ThinTests(unittest.TestCase):
    def test_a_thin_moves_the_weights_to_a_directory_named_after_the_root(self):
        for target, serving, manifest, staging in tree():
            moved = worker.move_weights(str(target), str(staging), str(manifest), [str(serving)])
            self.assertEqual(len(moved), 2)
            staged = worker.stage_dir(str(staging), str(target))
            self.assertEqual(Path(staged).name, 'strata-0.2.8-weights')
            for name in NAMES:
                self.assertFalse((target / 'data/models' / NAMES[name]).exists())
                self.assertTrue((Path(staged) / 'data/models' / NAMES[name]).is_file())
            for entry in moved:
                self.assertEqual(Path(entry['to']).stat().st_nlink, 1)
                self.assertEqual(Path(entry['to']).stat().st_size, entry['size_bytes'])

    def test_a_thin_leaves_everything_that_is_not_a_weight(self):
        for target, serving, manifest, staging in tree():
            (target / 'data/models/gyro-M/q8_0.pack4').write_bytes(b'pack')
            (target / 'bin').mkdir()
            (target / 'bin/strata-engine').write_bytes(b'engine')
            worker.move_weights(str(target), str(staging), str(manifest), [str(serving)])
            self.assertTrue((target / 'data/models/gyro-M/q8_0.pack4').is_file())   # §4.1: the packs stay
            self.assertTrue((target / 'bin/strata-engine').is_file())

    def test_a_thin_refuses_when_a_copy_is_already_there(self):
        for target, serving, manifest, staging in tree():
            worker.move_weights(str(target), str(staging), str(manifest), [str(serving)])
            fill(target, HEAVY)
            with self.assertRaises(SystemExit) as raised:
                worker.move_weights(str(target), str(staging), str(manifest), [str(serving)])
            self.assertIn('already exists', str(raised.exception))

    def test_a_thin_refuses_a_weight_that_is_not_identical_to_the_pin(self):
        for target, serving, manifest, staging in tree():
            (target / 'data/models' / NAMES['q4_k.gguf']).write_bytes(b'y' * (len(b'weight ') * 200))
            with self.assertRaises(SystemExit) as raised:
                worker.move_weights(str(target), str(staging), str(manifest), [str(serving)])
            self.assertIn('hash-differs-from-pin', str(raised.exception))

    def test_a_thin_refuses_without_a_copy_apart_from_the_root_it_thins(self):
        # §3.2's "a copy on that host apart from the root it thins", read by the worker rather than assumed.
        for target, serving, manifest, staging in tree():
            shutil.rmtree(serving)
            with self.assertRaises(SystemExit) as raised:
                worker.move_weights(str(target), str(staging), str(manifest), [str(serving)])
            self.assertIn('no-copy-apart', str(raised.exception))

    def test_a_root_the_inventory_does_not_declare_is_never_a_target(self):
        with tempfile.TemporaryDirectory() as where:
            argv = ['thin', '--root', f'{where}/strata-0.2.8', '--roots', f'{where}/somewhere-else',
                    '--staging', f'{where}/retired', '--manifest', f'{where}/manifest.json']
            with self.assertRaises(SystemExit) as raised:
                worker.main(argv)
            self.assertIn('not among the roots this host declares', str(raised.exception))

    def test_the_worker_finds_the_copy_apart_in_any_other_root(self):
        # A rollback root's weight may be held by a candidate root rather than by the serving one.
        with tempfile.TemporaryDirectory() as where:
            base = Path(where)
            ud = fill(base / 'serving' / 'strata-0.2.11-ud',
                      {name: b'a different quantization ' * 137 for name in NAMES})   # same model, other bytes
            other = fill(base / 'candidate' / 'strata-0.2.11-gyro', HEAVY)   # the copy apart
            target = fill(base / 'rollback' / 'strata-0.2.8', HEAVY)
            manifest = manifest_at(base, HEAVY)
            states = worker.weight_states(str(target), [str(ud), str(other)], str(manifest))
            self.assertEqual({name: entry['state'] for name, entry in states.items()},
                             {name: 'identical' for name in NAMES})
            self.assertTrue(all(entry['apart_path'].startswith(str(other)) for entry in states.values()))


class RestoreTests(unittest.TestCase):
    """The restore half, over a real filesystem, with the free-space question answered by the test.

    `put_weights_back` asks how much is free where the root lives before it copies anything (the owner's 40 GiB
    floor). That is a fact about the machine, not about the restore: a runner whose `/tmp` holds 13 GiB free would
    see every test below refused for space before the thing it means to prove is reached. So the reading is declared
    here, generously for the paths that copy and low for the one that checks the floor.
    """

    def setUp(self):
        patch = mock.patch.object(worker, 'free_bytes', return_value=120 * 1024**3)
        patch.start()
        self.addCleanup(patch.stop)

    def thinned(self):
        for target, serving, manifest, staging in tree():
            worker.move_weights(str(target), str(staging), str(manifest), [str(serving)])
            yield target, serving, manifest, staging

    def test_restore_copies_back_from_the_isolated_copy(self):
        for target, serving, manifest, staging in self.thinned():
            restored = worker.put_weights_back(str(target), [str(serving)], str(staging), str(manifest))
            self.assertEqual(len(restored), 2)
            for name, relative in NAMES.items():
                back = target / 'data/models' / relative
                self.assertTrue(back.is_file())
                self.assertEqual(back.stat().st_nlink, 1)
                source = Path(next(entry['from'] for entry in restored if entry['to'] == str(back)))
                self.assertNotEqual(back.stat().st_ino, source.stat().st_ino)   # F8: an independent inode
                self.assertEqual(hashlib.sha256(back.read_bytes()).hexdigest(),
                                 worker.manifest_weights(str(manifest))[name][2])

    def test_restore_falls_back_to_the_serving_root_when_the_isolated_copy_is_gone(self):
        for target, serving, manifest, staging in self.thinned():
            shutil.rmtree(worker.stage_dir(str(staging), str(target)))
            restored = worker.put_weights_back(str(target), [str(serving)], str(staging), str(manifest))
            self.assertTrue(all(entry['from'].startswith(str(serving)) for entry in restored))

    def test_restore_refuses_before_the_floor_is_breached(self):
        # the one test that wants the machine's number: the gate itself, measured on a declared free space
        with mock.patch.object(worker, 'free_bytes', return_value=13 * 1024**3):
            for target, serving, manifest, staging in self.thinned():
                with self.assertRaises(SystemExit) as raised:
                    worker.put_weights_back(str(target), [str(serving)], str(staging), str(manifest))
                self.assertIn('the owner keeps 40 GiB free at all times', str(raised.exception))
                self.assertIn('13 GiB free', str(raised.exception))
                self.assertFalse((target / 'data/models' / NAMES['q8_0.gguf']).exists())   # refused, so nothing copied

    def test_restore_refuses_when_the_root_is_already_full(self):
        for target, serving, manifest, staging in self.thinned():
            fill(target, HEAVY)
            with self.assertRaises(SystemExit) as raised:
                worker.put_weights_back(str(target), [str(serving)], str(staging), str(manifest))
            self.assertIn('this root is full', str(raised.exception))

    def test_restore_refuses_when_nowhere_holds_the_bytes(self):
        for target, serving, manifest, staging in self.thinned():
            shutil.rmtree(worker.stage_dir(str(staging), str(target)))
            shutil.rmtree(serving)
            with self.assertRaises(SystemExit) as raised:
                worker.put_weights_back(str(target), [str(serving)], str(staging), str(manifest))
            self.assertIn('Re-prepare this root', str(raised.exception))   # a download is not a restore

    def test_restore_refuses_a_source_that_does_not_hash_to_the_pin(self):
        for target, serving, manifest, staging in self.thinned():
            staged = Path(worker.stage_dir(str(staging), str(target))) / 'data/models' / NAMES['q8_0.gguf']
            staged.write_bytes(b'z' * (len(b'weight ') * 200))   # the same length, not the same bytes
            with self.assertRaises(SystemExit) as raised:
                worker.put_weights_back(str(target), [str(serving)], str(staging), str(manifest))
            self.assertIn('not the pinned weight', str(raised.exception))

    def test_restore_refuses_below_the_floor_the_owner_keeps_free(self):
        for target, serving, manifest, staging in self.thinned():
            with mock.patch.object(worker, 'FLOOR_GIB', 10 ** 6):
                with self.assertRaises(SystemExit) as raised:
                    worker.put_weights_back(str(target), [str(serving)], str(staging), str(manifest))
            self.assertIn('free at all times', str(raised.exception))


class CopyIndependenceTests(unittest.TestCase):
    def test_a_restored_copy_is_a_different_inode_even_where_the_fs_cannot_reflink(self):
        with tempfile.TemporaryDirectory() as where:
            source = Path(where) / 'source.gguf'
            source.write_bytes(b'a' * 4096)
            target = Path(where) / 'nested' / 'target.gguf'
            worker.clone_or_copy(source, target)
            self.assertEqual(target.stat().st_size, 4096)
            self.assertEqual(target.stat().st_nlink, 1)
            self.assertNotEqual(target.stat().st_ino, source.stat().st_ino)
            target.write_bytes(b'b' * 4096)                 # an in-place repair of the copy...
            self.assertEqual(source.read_bytes(), b'a' * 4096)  # ...leaves the generation it came from alone

    def test_no_partial_file_is_left_behind_under_the_restoring_name(self):
        with tempfile.TemporaryDirectory() as where:
            source = Path(where) / 'source.gguf'
            source.write_bytes(b'a' * 1024)
            target = Path(where) / 'target.gguf'
            worker.clone_or_copy(source, target)
            self.assertEqual(sorted(p.name for p in Path(where).iterdir()), ['source.gguf', 'target.gguf'])


class SharedInodeTests(unittest.TestCase):
    def test_du_apart_bytes_that_a_deletion_would_not_give_back_are_counted(self):
        with tempfile.TemporaryDirectory() as where:
            base = Path(where)
            inside = base / 'root'
            (inside / 'data').mkdir(parents=True)
            (inside / 'data/a.gguf').write_bytes(b'x' * 1000)
            (inside / 'data/shared.gguf').write_bytes(b'x' * 26800)
            os.link(str(inside / 'data/shared.gguf'), str(base / 'outside.gguf'))
            entries, shared_files, shared_bytes = worker.walk(str(inside))
            self.assertEqual(shared_files, 1)
            self.assertEqual(shared_bytes, 26800)
            self.assertGreaterEqual(entries, 2)


def front_at(root, names=None):
    """A root's own `ondemand`, holding a config that names NAMES (the root itself by default) — what a claim
    installs there, and what it leaves there after the host's front is re-pointed at another root."""
    directory = Path(root) / 'ondemand'
    directory.mkdir()
    config = directory / 'config.json'
    config.write_text(json.dumps({'root': str(names or root), 'unit': 'agentiac-strata-inference'}))
    return config


class FrontTests(unittest.TestCase):
    """The question §4.3 asks of a front: does the front this host RUNS live in this root.

    `front_names` takes the running front as an argument in these tests because the fact comes from a unit, and a
    test has no systemd to ask. `running_front`'s own read is pinned below against the text `systemctl cat` prints.
    """

    def test_the_running_front_living_in_this_root_serves_it(self):
        with tempfile.TemporaryDirectory() as where:
            target = fill(Path(where) / 'target', {name: b'x' for name in NAMES})
            front_at(target)
            self.assertTrue(worker.front_names(str(target), front=worker.front_directory(target)))

    def test_a_front_installed_here_serves_it_even_with_no_model_loaded(self):
        # The refusal this check exists for: an unloaded front would load THIS root on the next request.
        with tempfile.TemporaryDirectory() as where:
            target = fill(Path(where) / 'target', {name: b'x' for name in NAMES})
            front_at(target)
            self.assertTrue(worker.front_names(str(target), front=str(target / 'ondemand')))

    def test_the_residue_of_a_claim_that_moved_is_reported_and_not_serving(self):
        # A stale root can still hold the config of an earlier claim while
        # agentiac-ondemand runs /srv/agentiac/strata-0.2.12/ondemand/proxy.py. Reading the file
        # instead of the unit called that serving and refused the retirement.
        with tempfile.TemporaryDirectory() as where:
            base = Path(where)
            target = fill(base / 'target', {name: b'x' for name in NAMES})
            other = fill(base / 'other', {name: b'x' for name in NAMES})
            residue = front_at(target)
            self.assertFalse(worker.front_names(str(target), front=worker.front_directory(other)))
            self.assertEqual(worker.front_configs(str(target)), [str(residue)])
            self.assertEqual(worker.front_configs(str(other)), [])

    def test_no_front_installed_serves_no_root(self):
        with tempfile.TemporaryDirectory() as where:
            target = fill(Path(where) / 'target', {name: b'x' for name in NAMES})
            front_at(target)
            self.assertFalse(worker.front_names(str(target), front=None))

    def test_a_root_with_no_front_is_not_being_served(self):
        with tempfile.TemporaryDirectory() as where:
            target = fill(Path(where) / 'target', {name: b'x' for name in NAMES})
            self.assertFalse(worker.front_names(str(target), front=None))
            self.assertEqual(worker.front_configs(str(target)), [])

    def test_a_front_config_naming_another_root_does_not_hold_this_one(self):
        with tempfile.TemporaryDirectory() as where:
            target = fill(Path(where) / 'target', {name: b'x' for name in NAMES})
            front_at(target, names='/somewhere/else')
            self.assertFalse(worker.front_names(str(target), front=None))
            self.assertEqual(worker.front_configs(str(target)), [])

    def test_a_root_named_through_a_symlink_is_still_the_running_front(self):
        with tempfile.TemporaryDirectory() as where:
            base = Path(where)
            target = fill(base / 'real', {name: b'x' for name in NAMES})
            front_at(target)
            link = base / 'versioned'
            link.symlink_to(target)
            self.assertTrue(worker.front_names(str(link), front=str(target / 'ondemand')))

    def test_running_front_reads_the_unit_execstart(self):
        # The text `systemctl cat agentiac-ondemand` prints on a worker (its front runs the 0.2.12 root).
        unit = ('# /etc/systemd/system/agentiac-ondemand.service\n[Unit]\n'
                'ExecStart=/usr/bin/python3 /srv/agentiac/strata-0.2.12/ondemand/proxy.py '
                '/srv/agentiac/strata-0.2.12/ondemand/config.json\n')
        with mock.patch.object(worker.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=unit, stderr='')
            self.assertEqual(worker.running_front('agentiac-ondemand'), ('/srv/agentiac/strata-0.2.12/ondemand', None))

    def test_a_host_with_no_front_unit_answers_without_an_error(self):
        with mock.patch.object(worker.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(returncode=1, stdout='',
                                              stderr='No files found for agentiac-ondemand.service.\n')
            self.assertEqual(worker.running_front('agentiac-ondemand'), (None, None))

    def test_a_front_that_cannot_be_read_is_said_not_assumed(self):
        with mock.patch.object(worker.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(returncode=1, stdout='',
                                              stderr='Failed to get unit file list: Transport endpoint is not connected\n')
            directory, error = worker.running_front('agentiac-ondemand')
            self.assertIsNone(directory)
            self.assertIn('Transport endpoint', error)

    def test_measure_reports_the_residue_and_the_running_front_together(self):
        with tempfile.TemporaryDirectory() as where:
            base = Path(where)
            target = fill(base / 'target', {name: b'x' for name in NAMES})
            other = fill(base / 'other', {name: b'x' for name in NAMES})
            residue = front_at(target)
            args = SimpleNamespace(path=[str(target)], staging=None, privileged=False)
            facts = worker.measure(args, front=worker.front_directory(other))[str(target)]
            self.assertFalse(facts['front_names_path'])
            self.assertEqual(facts['front_running'], str(other / 'ondemand'))
            self.assertEqual(facts['front_residue'], [str(residue)])
            self.assertNotIn('front_error', facts)

    def test_measure_asks_about_the_front_only_when_a_root_carries_one(self):
        with tempfile.TemporaryDirectory() as where:
            target = fill(Path(where) / 'target', {name: b'x' for name in NAMES})
            args = SimpleNamespace(path=[str(target)], staging=None, privileged=False)
            with mock.patch.object(worker, 'running_front') as asked:
                facts = worker.measure(args)[str(target)]
            asked.assert_not_called()                      # a root with no `ondemand` cannot make the question
            self.assertIsNone(facts['front_running'])


class MeasureTests(unittest.TestCase):
    class Args:
        def __init__(self, paths, staging=None, serving=None, manifest=None, weights=False, privileged=False):
            self.path, self.staging, self.serving = paths, staging, serving
            self.front_unit = 'agentiac-ondemand'
            self.manifest, self.weights, self.privileged = manifest, weights, privileged

    def test_a_path_that_is_not_there_is_reported_as_not_there(self):
        with tempfile.TemporaryDirectory() as where:
            out = worker.measure(self.Args([f'{where}/nope']))
            self.assertFalse(out[f'{where}/nope']['exists'])
            self.assertEqual(out[f'{where}/nope']['size_bytes'], 0)

    def test_a_symlink_is_a_symlink(self):
        with tempfile.TemporaryDirectory() as where:
            real = Path(where) / 'real'
            real.mkdir()
            link = Path(where) / 'link'
            link.symlink_to(real)
            out = worker.measure(self.Args([str(link)]))
            self.assertTrue(out[str(link)]['symlink'])
            self.assertEqual(out[str(link)]['resolved'], str(real))

    def test_a_mount_point_is_named_as_one(self):
        with mock.patch.object(worker, 'mount_point', return_value=True):
            with tempfile.TemporaryDirectory() as where:
                out = worker.measure(self.Args([where]))
                self.assertTrue(out[where]['mount_point'])

    def test_the_staging_filesystem_is_reported_so_the_gate_can_compare_it(self):
        with tempfile.TemporaryDirectory() as where:
            root = Path(where) / 'root'
            root.mkdir()
            staging = Path(where) / 'retired'
            staging.mkdir()
            out = worker.measure(self.Args([str(root)], staging=[str(staging)]))
            self.assertEqual(out[str(root)]['staging_device'], out[str(root)]['device'])
            missing = worker.measure(self.Args([str(root)], staging=[f'{where}/no-such-staging']))
            self.assertIsNone(missing[str(root)]['staging_device'])

    def test_an_unprivileged_read_says_so_rather_than_reporting_zero(self):
        with tempfile.TemporaryDirectory() as where:
            out = worker.measure(self.Args([where], privileged=False))
            self.assertEqual(out[where]['openers']['method'], 'proc')
            self.assertIn('processes', out[where]['openers'])


if __name__ == '__main__':
    unittest.main()
