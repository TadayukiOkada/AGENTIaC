import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location('strata_prepare', ROOT / 'scripts/strata-prepare-worker.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class StrataPreparationTests(unittest.TestCase):
    def args(self, root, files=None):
        """A preparation that names its manifest: no preparation falls back to a pinned-in-code root."""
        manifest = {'id': 'fixture-prepare', 'family': 'qwen3', 'model': 'Qwen/Qwen3-8B', 'context': 262144,
                    'source_revision': '0' * 40, 'source_ref': 'v0.2.8', 'cuda_toolkit_major': 12,
                    'workers': {'worker-a': {'reserve_mib': 4915}},
                    'files': files or [{'name': 'fixture.gguf', 'path': 'models/fixture.gguf',
                                        'size': 1, 'sha256': '0' * 64}]}
        backend = Path(root) / 'fixture-prepare.json'
        backend.write_text(json.dumps(manifest))
        return argparse.Namespace(root=Path(root), node='worker-a', reserve_mib=4915, backend=backend)

    def test_isolated_environment_confines_settings_and_drops_overrides(self):
        env = runner.isolated_environment({
            'HOME': '/foreign', 'XDG_CONFIG_HOME': '/foreign/config',
            'STRATA_CONTROL_INSTALL': '1', 'STRATA_INSTALL_DIR': '/foreign/install',
            'STRATA_SETTINGS_FILE': '/foreign/settings', 'STRATA_ALLOW_UNPINNED': '1',
            'HF_ENDPOINT': 'https://foreign.invalid', 'PIP_INDEX_URL': 'https://foreign.invalid',
            'PYTHONPATH': '/foreign', 'GIT_CONFIG_GLOBAL': '/foreign/git', 'USER': 'test',
        }, Path('/safe'), Path('/safe/.venv'))
        self.assertEqual(env['HOME'], '/safe/home')
        self.assertEqual(env['XDG_CONFIG_HOME'], '/safe/home/.config')
        self.assertEqual(env['STRATA_SETTINGS_FILE'], '/safe/home/strata-settings.json')
        self.assertEqual(env['USER'], 'test')
        for name in ('STRATA_CONTROL_INSTALL', 'STRATA_INSTALL_DIR', 'STRATA_ALLOW_UNPINNED',
                     'HF_ENDPOINT', 'PIP_INDEX_URL', 'PYTHONPATH', 'GIT_CONFIG_GLOBAL'):
            self.assertNotIn(name, env)

    def test_isolated_refuses_an_existing_root_before_writing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'existing'
            root.mkdir()
            (root / 'sentinel').write_text('unchanged')
            script = ROOT / 'scripts/strata-prepare-worker.py'
            base = [sys.executable, str(script), '--node', 'worker-b', '--root', str(root),
                    '--reserve-mib', '3276', '--backend', 'unused.json', '--isolated']
            for extra in ([], ['--reuse-root', directory]):
                result = subprocess.run(base + extra, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([p.name for p in root.iterdir()], ['sentinel'])
                self.assertEqual((root / 'sentinel').read_text(), 'unchanged')

    def test_privileged_commands_are_rejected_and_build_parallelism_is_bounded(self):
        import tempfile
        commands = []
        fake = SimpleNamespace(run=lambda argv, *a, **k: commands.append(argv), download=lambda *a, **k: None)

        def main():
            fake.run(['cmake', '--build', 'build', '-j', '16'])
            with self.assertRaises(RuntimeError):
                fake.run(['sudo', 'apt-get', 'install', 'cuda-drivers'])
            return 0

        fake.main = main
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'setup': fake}), \
                patch.object(sys, 'argv', []), patch.object(sys, 'path', list(sys.path)):
            self.assertEqual(runner.phase_setup(self.args(directory)), 0)
        self.assertEqual(commands, [['cmake', '--build', 'build', '-j', '4']])

    def test_downloaded_shard_must_match_expected_bytes_before_setup_continues(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            shard = Path(directory) / 'fixture.gguf'
            fake = SimpleNamespace(run=lambda *a, **k: None,
                                   download=lambda url, path, *a, **k: Path(path).write_bytes(b'bad'))
            fake.main = lambda: fake.download('https://fixture.invalid/fixed-revision', shard)
            with patch.dict(sys.modules, {'setup': fake}), \
                    patch.object(sys, 'argv', []), patch.object(sys, 'path', list(sys.path)):
                with self.assertRaisesRegex(RuntimeError, 'SHA256 mismatch'):
                    runner.phase_setup(self.args(directory, files=[{
                        'name': 'fixture.gguf', 'path': 'models/fixture.gguf', 'size': 3,
                        'sha256': hashlib.sha256(b'yes').hexdigest()}]))

    def test_in_place_repair_of_a_multiply_linked_model_is_refused(self):
        # upstream 0.2.8 fixes a mismatched download by rewriting the file with open(path, 'r+b'); that
        # would corrupt a preserved model sharing the inode, so a second link is an outright refusal.
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            preserved = directory / 'preserved.gguf'
            preserved.write_bytes(b'x' * 10)
            shared = directory / 'copy.gguf'
            os.link(preserved, shared)
            fake = SimpleNamespace(run=lambda *a, **k: None, download=lambda *a, **k: None,
                                   repair_download=lambda url, path, *a, **k: 'repaired')
            fake.main = lambda: fake.repair_download('https://fixture.invalid/x', shared, 10, 'copy.gguf', 4)
            with patch.dict(sys.modules, {'setup': fake}), patch.object(sys, 'argv', []), \
                    patch.object(sys, 'path', list(sys.path)):
                with self.assertRaisesRegex(RuntimeError, 'multiply linked'):
                    runner.phase_setup(self.args(directory))
            self.assertEqual(preserved.read_bytes(), b'x' * 10)
            own = directory / 'own.gguf'
            own.write_bytes(b'x' * 10)
            fake.main = lambda: fake.repair_download('https://fixture.invalid/x', own, 10, 'own.gguf', 4)
            with patch.dict(sys.modules, {'setup': fake}), patch.object(sys, 'argv', []), \
                    patch.object(sys, 'path', list(sys.path)):
                self.assertEqual(runner.phase_setup(self.args(directory)), 'repaired')


class StrataBackendManifestTests(unittest.TestCase):
    ROOT = ROOT

    def backend(self, payload, same_as='old/shard.gguf'):
        entry = {'path': 'new/shard.gguf', 'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
                 'same_as': same_as}
        return {'files': [entry], 'weights': {'shard.gguf': (entry['size'], entry['sha256'])}}

    def test_setup_argv_and_state_name_from_synthetic_manifest(self):
        backend = {
            'id': 'strata-qwen-fixture-0.2.8',
            'family': 'qwen',
            'model': 'Q2_0',
            'context': 131072,
            'source_revision': '0' * 40,
            'source_ref': 'v0.2.8',
            'cuda_toolkit_major': 12,
            'memory_cap': False,
            'workers': {'worker-b': {'reserve_mib': 3276}},
        }
        argv = runner.setup_argv('worker-b', Path('/r'), 3276, backend)
        self.assertEqual(argv[:2], ['setup.py', '--yes'])
        self.assertEqual(argv[argv.index('--cuda') + 1], '12')
        self.assertEqual(argv[argv.index('--context') + 1], '131072')
        self.assertEqual(argv[argv.index('--model') + 1], 'Q2_0')
        self.assertNotIn('--low-ram', argv)
        self.assertIn('--build', argv)
        self.assertNotIn('--dsv41-package', argv)
        self.assertEqual(runner.state_name(backend), 'prepare-strata-qwen-fixture-0.2.8-state.json')

    def test_a_manifest_that_names_its_low_ram_mode_hands_it_to_setup(self):
        unified = {
            'id': 'strata-qwen-fixture-0.2.10',
            'family': 'qwen',
            'model': 'IQ3_XXS',
            'context': 262144,
            'cuda_toolkit_major': 13,
            'workers': {'worker-c': {'low_ram': 'off'}},
        }
        argv = runner.setup_argv('worker-c', Path('/r'), 700, unified)
        self.assertEqual(argv[argv.index('--cuda') + 1], '13')
        self.assertEqual(argv[argv.index('--low-ram') + 1], 'off')
        self.assertIn('--build', argv)
        auto = dict(unified, workers={'worker-c': {'low_ram': 'auto'}})
        self.assertNotIn('--low-ram', runner.setup_argv('worker-c', Path('/r'), 700, auto))

    def test_identical_file_is_copied_independently_and_counted_as_present(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / 'old', Path(directory) / 'new'
            (old / 'data/models/old').mkdir(parents=True)
            new.mkdir()
            (old / 'data/models/old/shard.gguf').write_bytes(b'ngram')
            backend = self.backend(b'ngram')
            before = runner.needed_bytes(backend, new)
            self.assertEqual(runner.reuse_files(backend, new, old, lambda *a, **k: None), ['shard.gguf'])
            copied = new / 'data/models/new/shard.gguf'
            self.assertEqual(copied.read_bytes(), b'ngram')
            # 0.2.8's repair_download rewrites a mismatched file in place, so a reused model must never
            # share an inode with the preserved root it came from.
            self.assertEqual(copied.stat().st_nlink, 1)
            self.assertNotEqual(copied.stat().st_ino, (old / 'data/models/old/shard.gguf').stat().st_ino)
            self.assertEqual(before - runner.needed_bytes(backend, new), 5)
            copied.write_bytes(b'other!')
            self.assertEqual((old / 'data/models/old/shard.gguf').read_bytes(), b'ngram')

    def test_isolated_root_may_already_hold_the_inputs_it_was_handed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'backends').mkdir()
            (root / 'prepare-worker.py').write_text('# runner')
            (root / 'prepare-strata-qwen38-gyro-m-0.2.8-state.json').write_text('{}')
            (root / 'prepare.lock').touch()
            self.assertEqual(runner.isolated_leftovers(root), [])
            (root / 'source').mkdir()
            (root / 'data').mkdir()
            self.assertEqual(runner.isolated_leftovers(root), ['data', 'source'])

    def test_an_interrupted_isolated_preparation_resumes_only_from_its_own_failed_state(self):
        # An interrupted preparation stops at a gate with saved stage `failed`; resuming is allowed only
        # from a saved `failed` state of the same manifest.
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            root.mkdir()
            (root / 'backends').mkdir()
            manifest = root / 'backends/fixture-resume.json'
            manifest.write_text(json.dumps({'id': 'fixture-resume'}))
            (root / 'source').mkdir()
            (root / 'prepare-fixture-resume-state.json').write_text(
                json.dumps({'stage': 'failed', 'error_type': 'RuntimeError', 'error': 'Strata setup exited 1'}))
            script = ROOT / 'scripts/strata-prepare-worker.py'
            base = [sys.executable, str(script), '--node', 'worker-d', '--root', str(root),
                    '--reserve-mib', '1', '--backend', str(manifest), '--isolated']
            before = sorted(p.name for p in root.iterdir())
            refused = subprocess.run(base, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('empty root', refused.stderr)
            self.assertIn('saved stage: failed', refused.stderr)      # it names what it found, not just the rule
            self.assertEqual(sorted(p.name for p in root.iterdir()), before)
            resumed = subprocess.run(base + ['--resume-failed'], capture_output=True, text=True)
            self.assertNotIn('empty root', resumed.stderr)
            self.assertNotIn('Saved state exists', resumed.stderr)
            self.assertEqual(sorted(p.name for p in root.iterdir()), before)

    def test_a_root_another_manifest_left_is_refused_even_with_resume_failed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            root.mkdir()
            (root / 'backends').mkdir()
            manifest = root / 'backends/fixture-other.json'
            manifest.write_text(json.dumps({'id': 'fixture-other'}))
            (root / 'data').mkdir()
            (root / 'prepare-fixture-resume-state.json').write_text(json.dumps({'stage': 'failed'}))
            script = ROOT / 'scripts/strata-prepare-worker.py'
            refused = subprocess.run([sys.executable, str(script), '--node', 'worker-d', '--root', str(root),
                                      '--reserve-mib', '1', '--backend', str(manifest), '--isolated',
                                      '--resume-failed'], capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('empty root', refused.stderr)
            self.assertIn('saved stage: none of this manifest', refused.stderr)

    def test_isolated_setup_may_still_copy_from_a_preserved_root(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            manifest = directory / 'backend.json'
            manifest.write_text('{}')
            root, reuse = directory / 'root', directory / 'reuse'
            root.mkdir()
            reuse.mkdir()
            (root / 'kept').touch()
            script = ROOT / 'scripts/strata-prepare-worker.py'
            refused = subprocess.run([sys.executable, str(script), '--node', 'worker-b', '--root', str(root),
                                      '--reserve-mib', '1', '--backend', str(manifest), '--isolated',
                                      '--reuse-root', str(reuse)], capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('empty root', refused.stderr)
            self.assertNotIn('forbids --reuse-root', refused.stderr)

    def test_reuse_candidate_with_wrong_hash_is_refused_and_not_linked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / 'old', Path(directory) / 'new'
            (old / 'data/models/old').mkdir(parents=True)
            new.mkdir()
            (old / 'data/models/old/shard.gguf').write_bytes(b'other')
            with self.assertRaisesRegex(RuntimeError, 'hash mismatch'):
                runner.reuse_files(self.backend(b'ngram'), new, old, lambda *a, **k: None)
            self.assertFalse((new / 'data/models/new/shard.gguf').exists())

    def roots(self, directory, payload=b'ngram'):
        old, new = Path(directory) / 'old', Path(directory) / 'new'
        (old / 'data/models/old').mkdir(parents=True)
        new.mkdir()
        (old / 'data/models/old/shard.gguf').write_bytes(payload)
        return old, new

    def test_headroom_is_refused_before_a_reused_weight_is_copied(self):
        import tempfile
        floor = (runner.FLOOR_GIB + runner.SCRATCH_GIB) * 1024**3
        with tempfile.TemporaryDirectory() as directory:
            old, new = self.roots(directory)
            backend = self.backend(b'ngram')
            with self.assertRaisesRegex(RuntimeError, r'need \d+ GiB free'):
                runner.reserve_then_reuse(backend, new, runner.SCRATCH_GIB, old, lambda *a, **k: None,
                                          free=floor - 1)
            self.assertFalse((new / 'data/models/new/shard.gguf').exists())
            self.assertEqual((old / 'data/models/old/shard.gguf').read_bytes(), b'ngram')

    def test_a_resumed_preparation_still_passes_with_the_floor_left(self):
        import tempfile
        floor = (runner.FLOOR_GIB + runner.SCRATCH_GIB) * 1024**3
        with tempfile.TemporaryDirectory() as directory:
            old, new = self.roots(directory)
            backend = self.backend(b'ngram')
            self.assertEqual(runner.reuse_files(backend, new, old, lambda *a, **k: None), ['shard.gguf'])
            self.assertEqual(runner.reserve_then_reuse(backend, new, runner.SCRATCH_GIB, old,
                                                       lambda *a, **k: None, free=floor), [])
            self.assertEqual((new / 'data/models/new/shard.gguf').read_bytes(), b'ngram')

    def test_a_host_scratch_raises_the_ask_and_the_default_keeps_the_old_number(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            root.mkdir()
            backend = {'weights': {'shard.gguf': (1024, 'x')}, 'files': []}
            default = runner.needed_bytes(backend, root)
            self.assertEqual(default, 1024 + (runner.FLOOR_GIB + runner.SCRATCH_GIB) * 1024**3)
            self.assertEqual(runner.needed_bytes(backend, root, 45) - default, 30 * 1024**3)

    def test_scratch_gib_comes_from_the_host_and_rejects_a_bad_value(self):
        manifest = {'workers': {'worker-a': {'scratch_gib': 45}, 'worker-b': {}}}
        self.assertEqual(runner.resolved_scratch('worker-a', manifest), 45)
        self.assertEqual(runner.resolved_scratch('worker-b', manifest), runner.SCRATCH_GIB)
        for bad in (-1, '45', True, 45.5):
            with self.assertRaisesRegex(SystemExit, 'invalid scratch_gib'):
                runner.resolved_scratch('worker-a', {'workers': {'worker-a': {'scratch_gib': bad}}})

    def test_preparation_takes_the_gpu_reserve_from_the_manifest(self):
        manifest = {'workers': {'worker-a': {'vram_reserve_mib': 700}, 'worker-b': {'vram_reserve_mib': 700}}}

        def asked(node, reserve=None):
            return argparse.Namespace(node=node, reserve_mib=reserve)

        self.assertEqual(runner.resolved_reserve(asked('worker-a'), manifest), 700)
        self.assertEqual(runner.resolved_reserve(asked('worker-b'), manifest), 700)
        self.assertEqual(runner.resolved_reserve(asked('worker-a', 4915), manifest), 4915)
        for bad in (-1, '700', True, 1.5):
            with self.assertRaisesRegex(SystemExit, 'invalid vram_reserve_mib'):
                runner.resolved_reserve(asked('worker-a'), {'workers': {'worker-a': {'vram_reserve_mib': bad}}})

    def test_config_is_the_one_naming_the_first_shard(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source').mkdir()
            models = root / 'data/models'
            models.mkdir(parents=True)
            (root / 'source/strata-iq3_xxs.json').write_text(json.dumps({'args': ['--native', str(models / 'a-00001.gguf')]}))
            (root / 'source/strata-gyro-gyro-m.json').write_text(json.dumps({'args': ['--native', str(models / 'g.gguf')]}))
            weights = {'a-00002.gguf': (1, 'x'), 'a-00001.gguf': (1, 'y')}
            self.assertEqual(runner.find_config(root, weights).name, 'strata-iq3_xxs.json')

    def test_a_config_of_the_upstream_checkout_is_not_this_preparations_config(self):
        # Upstream checkouts can ship sample `strata-*.json` files under `source/bench/results/**` (including
        # UTF-8 BOM or truncated JSON); a config is ours only when it points at a file inside this root.
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source').mkdir()
            models = root / 'data/models/coder'
            models.mkdir(parents=True)
            shard = 'DeepSeek-V4.1-Flash-1017-GSQ-RCO-Coder-IQ2_XXS-00001-of-00006.gguf'
            mine = root / 'source/strata-coder.json'
            mine.write_text(json.dumps({'args': ['--model', str(models / shard)]}))
            bench = root / 'source/bench/results/sample-community-gpu-iq3s'
            bench.mkdir(parents=True)
            (bench / 'strata-iq3_s.json').write_text('\ufeff' + json.dumps(
                {'args': ['--native', '/home/other/Strata-data/models/' + shard]}))
            (bench / 'strata-broken.json').write_text('{"args": [')      # not JSON at all
            weights = {shard: (1, '0' * 64)}
            self.assertEqual(runner.find_config(root, weights), mine)

    def test_prepared_config_is_audited_for_engine_and_model_paths(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = root / 'source/engine-cuda12/strata'
            engine.parent.mkdir(parents=True)
            engine.write_bytes(b'engine')

            def prepared(**changes):
                cfg = {'exe': str(engine), 'args': [
                    '--pack', str(root / 'data/packs/qwen'),
                    '--native', str(root / 'data/models/Q2_0/x.gguf'),
                    '--expert-profile', str(root / 'source/data/expert-profile.bin'),
                    '--mtp', str(root / 'data/mtp/rt')]}
                cfg.update(changes)
                path = root / 'source/strata-qwen.json'
                path.write_text(json.dumps(cfg))
                return path

            self.assertEqual(runner.audit_config(prepared(), root, engine),
                             {'exe': str(engine), 'model_files': 2, 'mtp_dir': str(root / 'data/mtp'), 'pack': None})
            with self.assertRaisesRegex(RuntimeError, 'outside the root'):
                runner.audit_config(prepared(args=['--native', '/srv/other/agentiac/strata/data/models/x.gguf']),
                                    root, engine)
            with self.assertRaisesRegex(RuntimeError, 'not name the built engine'):
                runner.audit_config(prepared(exe='/srv/other/agentiac/strata/source/engine-cuda12/strata'),
                                    root, engine)

    def test_mtp_tensors_are_verified_only_when_the_config_uses_them(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(runner.verify_mtp('python', root, None, {}))
            self.assertIsNone(runner.verify_mtp('python', root, str(root / 'mtp'), {}))
            (root / 'mtp/tensors').mkdir(parents=True)
            with patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(returncode=0,
                                                                                    stdout='', stderr='')) as run:
                self.assertTrue(runner.verify_mtp('python', root, str(root / 'mtp'), {}))
                self.assertIn('mtp_fetch.py', run.call_args.args[0][1])
                self.assertEqual(run.call_args.args[0][-1], str(root / 'mtp'))
            with patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(returncode=1,
                                                                                    stdout='hash mismatch', stderr='')):
                with self.assertRaisesRegex(RuntimeError, 'MTP tensor verification failed'):
                    runner.verify_mtp('python', root, str(root / 'mtp'), {})


class PlatformManifestTests(unittest.TestCase):
    def test_cuda_toolkit_defaults_to_12_accepts_13_and_refuses_others(self):
        self.assertEqual(runner.cuda_toolkit({}), 12)
        self.assertEqual(runner.cuda_toolkit({'cuda_toolkit_major': 13}), 13)
        with self.assertRaises(SystemExit):
            runner.cuda_toolkit({'cuda_toolkit_major': 11})

    def test_the_isolated_path_reaches_a_jetson_nvidia_smi_after_the_usual_directories(self):
        path = runner.isolated_environment({}, Path('/r'), Path('/r/.venv'))['PATH'].split(':')
        self.assertIn('/usr/sbin', path)
        self.assertLess(path.index('/usr/bin'), path.index('/usr/sbin'))


class RetiredLegacyRootTests(unittest.TestCase):
    """No preparation can fall back to an unnamed legacy root without a manifest."""

    def test_a_preparation_that_names_no_manifest_refuses_and_says_what_it_refuses(self):
        with self.assertRaises(SystemExit) as refused:
            runner.load_backend(None)
        message = str(refused.exception)
        self.assertIn('names its manifest', message)
        self.assertIn('was retired', message)
        self.assertIn('artifacts/pipelines/', message)

    def test_the_worker_holds_no_legacy_pins_and_no_unnamed_state(self):
        source = (ROOT / 'scripts/strata-prepare-worker.py').read_text()
        for retired in ('LEGACY', 'WEIGHTS = {', "'prepare-state.json'", "'prepare-manifest.json'",
                        '477ac13cbe8e582c20484871ce459d20d82df09d'):
            self.assertNotIn(retired, source)

    def test_every_preparation_state_is_named_after_the_manifest_id_not_its_file_name(self):
        backend = {'id': 'strata-qwen38-fixture-0.2.10'}
        self.assertEqual(runner.state_name(backend), 'prepare-strata-qwen38-fixture-0.2.10-state.json')


class DrafterPreparationTests(unittest.TestCase):
    """Setup is told which drafter a root uses, and its pack is fetched and verified against the manifest."""

    def fixture(self, directory, **extra):
        """A manifest with a small pinned pack, so the byte check runs on bytes a test can hold."""
        manifest = {'id': 'fixture-drafter', 'family': 'qwen3', 'model': 'Qwen/Qwen3-8B', 'context': 262144,
                    'source_revision': '0' * 40, 'source_ref': 'v0.2.12', 'cuda_toolkit_major': 13,
                    'workers': {'worker-c': {'drafter': 'dflash2', 'reserve_mib': 6144}},
                    'drafter_pack': {'path': 'dflash2/fixture.pack', 'size': 10,
                                     'sha256': hashlib.sha256(b'pack bytes').hexdigest()},
                    'files': [{'name': 'shard.gguf', 'path': 'models/shard.gguf', 'size': 1, 'sha256': '0' * 64}]}
        manifest.update(extra)
        (Path(directory) / 'fixture-drafter.json').write_text(json.dumps(manifest))
        return manifest

    def fetch(self, root, manifest, payload=None, creates=True):
        def fake(argv, **kwargs):
            self.assertEqual(argv[1:4], [str(Path('/source') / 'strata.py'), 'drafting', 'fetch'])
            self.assertEqual(argv[4], '--data-dir')
            if creates:
                target = root / 'data' / manifest['drafter_pack']['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload if payload is not None else b'nothing')
            return SimpleNamespace(returncode=0, stdout='fetched', stderr='')

        with patch.object(runner.subprocess, 'run', side_effect=fake):
            return runner.fetch_drafter_pack('python', Path('/source'), root, manifest, 'worker-c', {})

    def test_the_declared_drafter_reaches_setup_and_an_undeclared_manifest_is_left_alone(self):
        trial = {'id': 'fixture-trial', 'family': 'qwen3', 'model': 'Qwen3-8B',
                 'context': 262144, 'workers': {'worker-c': {'drafter': 'dflash2'}}}
        argv = runner.setup_argv('worker-c', Path('/r'), 6144, trial)
        self.assertEqual(argv[argv.index('--drafter') + 1], 'dflash2')
        plain = {'id': 'fixture-plain', 'family': 'qwen3', 'model': 'Qwen3-8B',
                 'context': 262144, 'workers': {'worker-c': {}}}
        self.assertNotIn('--drafter', runner.setup_argv('worker-c', Path('/r'), 6144, plain))

    def test_the_pack_that_came_through_the_engine_is_recorded_with_its_pinned_bytes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            record = self.fetch(Path(directory), self.fixture(directory), payload=b'pack bytes')
            self.assertEqual(record['bytes'], 10)
            self.assertEqual(record['sha256'], hashlib.sha256(b'pack bytes').hexdigest())
            self.assertEqual(record['fetched_for'], 'dflash2')

    def test_a_pack_that_is_not_the_pinned_bytes_fails_the_preparation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'drafter pack is 11 bytes, the manifest pins 10'):
                self.fetch(Path(directory), self.fixture(directory), payload=b'other bytes')
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.fixture(directory)
            manifest['drafter_pack']['size'] = 4          # the right length, the wrong file
            with self.assertRaisesRegex(RuntimeError, 'drafter pack hash mismatch'):
                self.fetch(Path(directory), manifest, payload=b'xxxx')

    def test_a_fetch_that_left_nothing_behind_is_a_failure_not_an_empty_root(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'the drafter fetch left no pack'):
                self.fetch(Path(directory), self.fixture(directory), creates=False)

    def test_a_declared_drafter_with_no_pinned_pack_is_refused_before_anything_downloads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.fixture(directory)
            del manifest['drafter_pack']
            with patch.object(runner.subprocess, 'run') as run:
                with self.assertRaisesRegex(SystemExit, 'names no drafter_pack'):
                    runner.fetch_drafter_pack('python', Path('/source'), Path(directory), manifest, 'worker-c', {})
                run.assert_not_called()


class DeepSeekV41PreparationTests(unittest.TestCase):
    """What a DeepSeek-V4.1 root looks like to the preparation."""

    CODER = {'id': 'strata-dsv41-coder-0.2.13', 'engine_dir': 'engine', 'engine_binary': 'strata-dsv41',
             'engine_package_dir': 'source/engine-dsv41', 'workers': {'worker-d': {}}}

    def setUp(self):
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'source').mkdir()

    def test_a_qwen_engine_is_still_found_where_the_qwen_root_builds_it(self):
        (self.root / 'source/engine').mkdir()
        exe = self.root / 'source/engine/strata'
        exe.write_text('engine')
        backend = {'id': 'fixture', 'engine_dir': 'engine', 'engine_binary': 'strata'}
        self.assertEqual(runner.prepared_engine(self.root, self.root / 'source', backend), exe)

    def test_the_deepseek_engine_is_found_in_its_own_package(self):
        package = self.root / 'source/engine-dsv41/0.1.59-linux-aarch64-cuda-cuda13'
        package.mkdir(parents=True)
        exe = package / 'strata-dsv41'
        exe.write_text('engine')
        self.assertEqual(runner.prepared_engine(self.root, self.root / 'source', self.CODER), exe)

    def test_two_engines_in_the_package_is_a_refusal_and_not_a_choice(self):
        for version in ('0.1.59-linux-aarch64-cuda-cuda13', '0.1.58-linux-aarch64-cuda-cuda13'):
            package = self.root / 'source/engine-dsv41' / version
            package.mkdir(parents=True)
            (package / 'strata-dsv41').write_text('engine')
        with self.assertRaisesRegex(RuntimeError, 'has to be one file'):
            runner.prepared_engine(self.root, self.root / 'source', self.CODER)

    def pinned_package(self, payload=b'engine package'):
        package = self.root / 'source/engine-dsv41'
        package.mkdir(parents=True)
        name = 'strata-dsv41-0.1.59-linux-aarch64-cuda-cuda13.tar.gz'
        (package / name).write_bytes(payload)
        return package / name, dict(self.CODER, engine_package={
            'name': name, 'dir': 'source/engine-dsv41', 'size': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
            'url': 'https://github.com/Niko1221/Strata/releases/download/v0.2.13/' + name,
            'source_ref': 'v0.2.13'})

    def test_the_engine_package_handed_to_the_root_is_checked_against_the_manifest(self):
        package, manifest = self.pinned_package()
        record = runner.verify_engine_package(self.root, manifest)
        self.assertEqual((record['path'], record['bytes']), (str(package), package.stat().st_size))
        self.assertEqual(record['sha256'], manifest['engine_package']['sha256'])
        package.write_bytes(b'truncated')
        with self.assertRaisesRegex(RuntimeError, 'is not what strata-dsv41-coder-0.2.13 pins'):
            runner.verify_engine_package(self.root, manifest)

    def test_a_weight_set_whose_engine_is_a_package_stops_when_that_package_is_not_there(self):
        _, manifest = self.pinned_package()
        (self.root / 'source/engine-dsv41/strata-dsv41-0.1.59-linux-aarch64-cuda-cuda13.tar.gz').unlink()
        with self.assertRaisesRegex(RuntimeError, 'which is not in .*source/engine-dsv41'):
            runner.verify_engine_package(self.root, manifest)

    def test_a_qwen_root_hands_no_engine_package_and_checks_nothing(self):
        self.assertIsNone(runner.verify_engine_package(self.root, {'id': 'fixture', 'engine_dir': 'engine'}))

    def test_no_engine_anywhere_says_where_it_looked(self):
        with self.assertRaisesRegex(RuntimeError, 'is not where strata-dsv41-coder-0.2.13 says it is'):
            runner.prepared_engine(self.root, self.root / 'source', self.CODER)

    def test_a_drafter_that_arrived_with_the_set_is_checked_rather_than_packed(self):
        pack = self.root / 'data/deepseek41shared/dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf'
        pack.parent.mkdir(parents=True)
        pack.write_bytes(b'drafter')
        manifest = dict(self.CODER, drafter_pack={'path': 'deepseek41shared/dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf',
                                                 'size': len(b'drafter'), 'sha256': hashlib.sha256(b'drafter').hexdigest(),
                                                 'fetched_by': 'setup'})
        record = runner.verify_drafter_pack(self.root, manifest)
        self.assertEqual((record['bytes'], record['fetched_by'], record['path']), (7, 'setup', str(pack)))

    def test_a_drafter_that_is_not_the_pinned_bytes_is_refused(self):
        pack = self.root / 'data/deepseek41shared/dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf'
        pack.parent.mkdir(parents=True)
        pack.write_bytes(b'truncat')
        manifest = dict(self.CODER, drafter_pack={'path': 'deepseek41shared/dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf',
                                                 'size': len(b'truncat'), 'sha256': '0' * 64, 'fetched_by': 'setup'})
        with self.assertRaisesRegex(RuntimeError, 'is not what strata-dsv41-coder-0.2.13 pins'):
            runner.verify_drafter_pack(self.root, manifest)

    def test_a_prepared_config_under_the_data_dir_is_found(self):
        shard = 'DeepSeek-V4.1-Flash-1017-GSQ-RCO-Coder-IQ2_XXS-00002-of-00006.gguf'
        weights = {shard: (1, '0' * 64)}
        where = self.root / 'data/models/deepseek41ISTA-DAS-Coder/IQ2_XXS'
        where.mkdir(parents=True)
        config = where.parent / 'strata-coder.json'
        config.write_text(json.dumps({'exe': 'x', 'args': ['--model', str(where / shard)]}))
        self.assertEqual(runner.find_config(self.root, weights), config)

    def pinned_pack(self, sizes=None):
        """A pack folder as setup.py leaves it: `pack-source.json` with the model_dir and every file."""
        sizes = sizes or {'a-00001.gguf': 100, 'a-00002.gguf': 200, 'z-engram.gguf': 300}
        (self.root / 'data/models/set').mkdir(parents=True)
        pack = self.root / 'data/packs/set-tag'
        pack.mkdir(parents=True)
        (pack / 'pack-source.json').write_text(json.dumps({
            'model_dir': str(self.root / 'data/models/set'),
            'files': {name: [size, 1] for name, size in sizes.items()}}))
        config = self.root / 'source/strata-set.json'
        config.write_text(json.dumps({
            'exe': str(self.root / 'source/engine-dsv41/0.1.59/strata-dsv41'),
            'args': ['--pack', str(pack), '--max-context', '131072']}))
        return pack, config

    def test_a_pack_based_config_is_matched_by_the_files_its_pack_was_made_from(self):
        pack, config = self.pinned_pack()
        weights = {'a-00001.gguf': (100, '0' * 64), 'a-00002.gguf': (200, '0' * 64), 'z-engram.gguf': (300, '0' * 64)}
        self.assertEqual(runner.find_config(self.root, weights), config)
        audited = runner.audit_config(config, self.root, self.root / 'source/engine-dsv41/0.1.59/strata-dsv41', weights)
        self.assertEqual(audited['model_files'], 3)
        self.assertEqual(audited['pack'], str(pack))

    def test_a_pack_that_holds_other_bytes_than_the_manifest_pins_is_refused(self):
        _, config = self.pinned_pack(sizes={'a-00001.gguf': 100, 'a-00002.gguf': 199, 'z-engram.gguf': 300})
        weights = {'a-00001.gguf': (100, '0' * 64), 'a-00002.gguf': (200, '0' * 64), 'z-engram.gguf': (300, '0' * 64)}
        engine = self.root / 'source/engine-dsv41/0.1.59/strata-dsv41'
        with self.assertRaisesRegex(RuntimeError, 'not built from what the manifest pins: a-00002.gguf'):
            runner.audit_config(config, self.root, engine, weights)

    def test_a_pack_whose_first_shard_is_not_the_pinned_bytes_is_not_taken_as_this_preparations_config(self):
        self.pinned_pack(sizes={'a-00001.gguf': 99, 'a-00002.gguf': 200, 'z-engram.gguf': 300})
        weights = {'a-00001.gguf': (100, '0' * 64), 'a-00002.gguf': (200, '0' * 64), 'z-engram.gguf': (300, '0' * 64)}
        with self.assertRaisesRegex(RuntimeError, 'no prepared config names a-00001.gguf'):
            runner.find_config(self.root, weights)

    def test_a_pack_naming_a_file_outside_the_root_is_refused(self):
        _, config = self.pinned_pack()
        record = self.root / 'data/packs/set-tag/pack-source.json'
        data = json.loads(record.read_text())
        data['model_dir'] = '/home/other/Strata-data/models/set'
        record.write_text(json.dumps(data))
        weights = {'a-00001.gguf': (100, '0' * 64), 'a-00002.gguf': (200, '0' * 64), 'z-engram.gguf': (300, '0' * 64)}
        with self.assertRaisesRegex(RuntimeError, 'outside the root'):
            runner.audit_config(config, self.root,
                                self.root / 'source/engine-dsv41/0.1.59/strata-dsv41', weights)

    def test_the_node_is_the_manifests_to_decide_and_an_undeclared_one_is_named_back(self):
        self.assertEqual(runner.node_of(self.CODER, 'worker-d'), {})
        with self.assertRaisesRegex(SystemExit, 'it declares worker-d'):
            runner.node_of(self.CODER, 'worker-a')

    def test_the_coder_is_handed_its_engine_package_instead_of_a_build_it_cannot_use(self):
        _, coder = self.pinned_package()
        coder['family'] = 'deepseek'
        coder['model'] = 'IQ2_XXS'
        coder['context'] = 131072
        coder['cuda_toolkit_major'] = 13
        argv = runner.setup_argv('worker-d', Path('/r'), 2048, coder)
        self.assertNotIn('--build', argv)
        self.assertEqual(argv[argv.index('--dsv41-package') + 1],
                         '/r/source/engine-dsv41/strata-dsv41-0.1.59-linux-aarch64-cuda-cuda13.tar.gz')
        qwen = {'id': 'fixture-qwen', 'family': 'qwen', 'model': 'IQ3_XXS',
                'context': 262144, 'cuda_toolkit_major': 13, 'workers': {'worker-a': {}}}
        self.assertIn('--build', runner.setup_argv('worker-a', Path('/r'), 6144, qwen))
        self.assertNotIn('--dsv41-package', runner.setup_argv('worker-a', Path('/r'), 6144, qwen))


class TerminalSubdirLayoutTests(unittest.TestCase):
    """Strata 0.2.16+ places setup.py, tools/, strata.py, and compiled engine/ under source/terminal/."""

    def test_terminal_root_and_prepared_engine_detect_terminal_subdir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            term = source / 'terminal'
            (term / 'engine').mkdir(parents=True)
            (term / 'setup.py').write_text('# setup')
            exe = term / 'engine/strata'
            exe.write_text('engine')
            self.assertEqual(runner.terminal_root(source), term)
            backend = {'id': 'fixture', 'engine_dir': 'engine', 'engine_binary': 'strata'}
            self.assertEqual(runner.prepared_engine(root, source, backend), exe)

    def test_verify_mtp_and_find_config_work_with_terminal_subdir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            term = source / 'terminal'
            (term / 'tools').mkdir(parents=True)
            (term / 'setup.py').write_text('# setup')
            (root / 'mtp/tensors').mkdir(parents=True)
            with patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as run:
                self.assertTrue(runner.verify_mtp('python', source, str(root / 'mtp'), {}))
                self.assertEqual(run.call_args.args[0][1], str(term / 'tools/mtp_fetch.py'))
            models = root / 'data/models'
            models.mkdir(parents=True)
            cfg = term / 'strata-iq3_xxs.json'
            cfg.write_text(json.dumps({'args': ['--native', str(models / 'a-00001.gguf')]}))
            self.assertEqual(runner.find_config(root, {'a-00001.gguf': (1, 'x')}), cfg)

    def test_fetch_drafter_pack_invokes_terminal_strata_py(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            term = source / 'terminal'
            term.mkdir(parents=True)
            (term / 'setup.py').write_text('# setup')
            payload = b'pack'
            manifest = {'id': 'fixture', 'workers': {'worker-c': {'drafter': 'dflash2'}},
                        'drafter_pack': {'path': 'dflash2/fixture.pack', 'size': len(payload),
                                         'sha256': hashlib.sha256(payload).hexdigest()}}

            def fake(argv, **kwargs):
                self.assertEqual(argv[1], str(term / 'strata.py'))
                self.assertEqual(kwargs['cwd'], term)
                target = root / 'data' / manifest['drafter_pack']['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                return SimpleNamespace(returncode=0, stdout='ok', stderr='')

            with patch.object(runner.subprocess, 'run', side_effect=fake):
                record = runner.fetch_drafter_pack('python', source, root, manifest, 'worker-c', {})
            self.assertEqual(record['bytes'], len(payload))


if __name__ == '__main__':
    unittest.main()
