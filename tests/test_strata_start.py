"""Strata worker start admission, resource limits, engine options, GPU page pool, and drafter verification."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import paths  # noqa: E402

SITE = paths.root()

spec = importlib.util.spec_from_file_location('strata_start', ROOT / 'scripts/strata-start-worker.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

GYRO_MANIFEST = {
    'id': 'strata-qwen-gyro-0.2.8',
    'source_version': '0.2.8',
    'context': 131072,
    'contexts': [131072],
    'memory_cap': False,
    'workers': {
        'worker-a': {'min_available_gib': 20, 'pool_workers': 7,
                     'cpu_quota_percent': 800, 'vram_reserve_mib': 700,
                     'env': {'STRATA_RESIDENT_HEADROOM_GIB': '6'}},
        'worker-b': {'min_available_gib': 44, 'pool_workers': 15,
                     'cpu_quota_percent': 1600, 'vram_reserve_mib': 700},
    },
}

LEGACY_MANIFEST = {
    'id': 'strata-qwen-legacy-0.1.45',
    'source_version': '0.1.45',
    'workers': {
        'worker-a': {'vram_reserve_mib': 4915},
        'worker-b': {'vram_reserve_mib': 3276},
    },
}

UNIFIED_MANIFEST = {
    'id': 'strata-qwen-iq3xxs-0.2.10',
    'source_version': '0.2.10',
    'context': 262144,
    'contexts': [262144],
    'workers': {
        'worker-c': {'min_available_gib': 53, 'memory_max': '80G', 'unified_memory': True,
                     'vram_reserve_mib': 6144},
    },
}

CODER_MANIFEST = {
    'id': 'strata-dsv41-coder-0.2.13',
    'source_version': '0.2.13',
    'context': 131072,
    'contexts': [131072],
    'engine_options': {'vram_reserve_mib': None, 'pool_workers': None, 'mmap_experts': None},
    'engine_flags': {
        'experts': 'integrated: 8064 of 8064 in VRAM',
        'cpu_threads': 'threads',
        'dspark': 'mode',
        'dspark_conf': 'conf',
        'prefill_chunk': 'chunk',
        'parallel': 'slots',
        'reserve_mib': 'vram',
        'mmap-experts': 'mmap',
        'vision': 'vision',
        'tp': 'tp',
    },
    'engine_settings': {'experts': '6'},
    'workers': {
        'worker-d': {'unified_memory': True},
    },
}


def config(args=(), user_args=None):
    """A prepared engine config as the start reads it: the generated "args", plus the person's own "user_args"
    that serve/server.py merges over them at every start, when the root has any."""
    cfg = {'args': list(args)}
    if user_args is not None:
        cfg['user_args'] = list(user_args)
    return cfg


def write_manifest(directory, name, payload):
    path = Path(directory) / f'{name}.json'
    path.write_text(json.dumps(payload))
    return path


class StrataAdmissionTests(unittest.TestCase):
    def check_denial(self, available_kib, gpu_output, message):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = write_manifest(root, 'gyro', GYRO_MANIFEST)
            (root / 'prepare-strata-qwen-gyro-0.2.8-state.json').write_text(json.dumps({'stage': 'prepared'}))
            original = Path.read_text

            def read(path, *args, **kwargs):
                if str(path) == '/proc/meminfo':
                    return f'MemAvailable: {available_kib} kB\n'
                return original(path, *args, **kwargs)

            with patch.object(sys, 'argv', ['start', '--unit', 'agentiac-strata-inference', '--node', 'worker-a',
                                            '--root', directory, '--backend', str(backend)]), \
                    patch.object(Path, 'read_text', autospec=True, side_effect=read), \
                    patch.object(runner.subprocess, 'check_output', return_value=gpu_output), \
                    patch.object(runner.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, message):
                    runner.main()
                run.assert_not_called()
            self.assertFalse(list(root.glob('api-key*')))
            self.assertFalse(list(root.glob('start-*-state.json')))

    def test_low_ram_is_rejected_before_service_or_credentials_are_created(self):
        self.check_denial(4 * 1024**2, '0, 24576', 'insufficient free host RAM')

    def test_unrelated_gpu_workload_is_preserved(self):
        self.check_denial(30 * 1024**2, '21000, 24576', 'another GPU workload')


class StrataRealGpuGateTests(unittest.TestCase):
    def test_mock_or_foreign_gpu_does_not_establish_real_inference(self):
        self.assertFalse(runner.has_owned_gpu_process('', {101}))
        self.assertFalse(runner.has_owned_gpu_process('202, 16000', {101}))
        self.assertFalse(runner.has_owned_gpu_process('101, 100', {101}))
        self.assertTrue(runner.has_owned_gpu_process('101, 8000', {101}))


class StrataBackendStartTests(unittest.TestCase):
    def test_trial_backend_has_no_ram_cap_and_is_killed_first(self):
        backend = copy.deepcopy(GYRO_MANIFEST)
        properties = runner.resource_properties(backend, 'worker-b', True)
        self.assertFalse([p for p in properties if p.startswith('--property=MemoryMax')])
        self.assertIn('--property=OOMScoreAdjust=1000', properties)
        self.assertIn('--property=MemorySwapMax=0', properties)
        self.assertEqual(runner.required_bytes(backend, 'worker-b'), (44 + 4) * 1024**3)

    def test_a_start_that_names_no_manifest_is_refused(self):
        """F10: a start without a manifest stops before it reads a root, admits anything or writes a key."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'prepare-state.json').write_text(json.dumps({'stage': 'prepared'}))
            (root / 'source').mkdir()
            (root / 'source/strata-qwen.json').write_text(json.dumps({'args': [], 'model_name': 'x'}))
            with patch.object(sys, 'argv', ['start', '--unit', 'agentiac-strata-inference', '--node', 'worker-a',
                                            '--root', directory]), \
                    patch.object(runner.subprocess, 'check_output') as gpu, \
                    patch.object(runner.subprocess, 'run') as run:
                with self.assertRaises(SystemExit) as refused:
                    runner.main()
                self.assertIn('needs --backend', str(refused.exception))
                gpu.assert_not_called()
                run.assert_not_called()
            self.assertFalse(list(root.glob('api-key*')))
            self.assertFalse(list(root.glob('start-*-state.json')))
        with self.assertRaises(SystemExit):
            runner.load_backend(None)

    def test_context_is_checked_against_preparation_and_only_lowered(self):
        args = ['--pack', 'p', '--max-context', '131072', '--kv', 'int8']
        runner.set_context(args, 131072, 65536)
        self.assertEqual(args[args.index('--max-context') + 1], '65536')
        with self.assertRaisesRegex(RuntimeError, 'prepared context differs'):
            runner.set_context(['--max-context', '32768'], 131072, 131072)
        with self.assertRaisesRegex(RuntimeError, 'above the prepared'):
            runner.set_context(['--max-context', '65536'], 65536, 131072)

    def test_host_engine_env_is_merged_into_the_config(self):
        backend = copy.deepcopy(GYRO_MANIFEST)
        cfg = {'env': {'OTHER': '1'}}
        runner.add_engine_env(cfg, backend, 'worker-a')
        self.assertEqual(cfg['env'], {'OTHER': '1', 'STRATA_RESIDENT_HEADROOM_GIB': '6'})
        cfg = {}
        runner.add_engine_env(cfg, backend, 'worker-b')
        self.assertNotIn('env', cfg)

    def test_pool_workers_come_from_the_manifest_per_host(self):
        gyro = copy.deepcopy(GYRO_MANIFEST)
        cfg = config(['--pack', 'p', '--pool-workers', '8'])
        self.assertEqual(runner.set_pool_workers(cfg, gyro, 'worker-a'), 7)
        self.assertEqual(cfg['args'][cfg['args'].index('--pool-workers') + 1], '7')
        self.assertEqual(cfg['args'].count('--pool-workers'), 1)
        self.assertEqual(runner.set_pool_workers(config(), gyro, 'worker-b'), 15)

    def test_a_manifest_without_pool_workers_keeps_the_historical_count(self):
        for node in ('worker-a', 'worker-b'):
            cfg = config()
            self.assertEqual(runner.set_pool_workers(cfg, copy.deepcopy(LEGACY_MANIFEST), node), 8)
            self.assertEqual(cfg['args'], ['--pool-workers', '8'])

    def test_an_impossible_worker_count_fails_before_the_service_starts(self):
        for asked in (0, 33, '8', True, None):
            with self.assertRaisesRegex(RuntimeError, 'invalid pool_workers'):
                runner.set_pool_workers(config(), {'workers': {'worker-a': {'pool_workers': asked}}}, 'worker-a')

    def test_the_cpu_quota_covers_the_hosts_pool_plan(self):
        gyro = copy.deepcopy(GYRO_MANIFEST)
        self.assertEqual(runner.cpu_quota_percent(gyro, 'worker-a'), 800)
        self.assertEqual(runner.cpu_quota_percent(gyro, 'worker-b'), 1600)
        self.assertIn('--property=CPUQuota=800%', runner.resource_properties(gyro, 'worker-a', True))
        self.assertIn('--property=CPUQuota=1600%', runner.resource_properties(gyro, 'worker-b', True))
        for path in sorted((SITE / 'backends').glob('strata-*.json')):
            backend = runner.load_backend(path)
            for node, worker in backend.get('workers', {}).items():
                if 'pool_workers' in worker:
                    self.assertGreaterEqual(runner.cpu_quota_percent(backend, node), worker['pool_workers'] * 100,
                                            f'{path.name}/{node}')

    def test_a_quota_that_cannot_feed_the_pool_is_refused(self):
        self.assertEqual(runner.check_cpu_quota(7, 800), None)
        self.assertEqual(runner.check_cpu_quota(8, 800), None)
        with self.assertRaisesRegex(RuntimeError, 'cannot feed 15 CPU expert workers'):
            runner.check_cpu_quota(15, 800)
        for asked in (99, 4001, '800', True, None):
            with self.assertRaisesRegex(RuntimeError, 'invalid cpu_quota_percent'):
                runner.cpu_quota_percent({'workers': {'worker-b': {'cpu_quota_percent': asked}}}, 'worker-b')

    def test_the_deepseek_engine_is_handed_its_own_option_set_and_not_the_qwen_names(self):
        coder = copy.deepcopy(CODER_MANIFEST)
        self.assertEqual(runner.engine_option_names(coder),
                         {'vram_reserve_mib': None, 'pool_workers': None, 'mmap_experts': None})
        cfg = config(['--pack', 'p', '--max-context', '131072', '--vram-reserve-mib', '2048', '--pool-workers', '6'],
                     user_args=['--vram-reserve-mib', '2048'])
        self.assertEqual(runner.set_vram_reserve(cfg, coder, 'worker-d'), None)
        self.assertEqual(runner.set_pool_workers(cfg, coder, 'worker-d'), None)
        runner.add_unified_memory_flags(cfg['args'], coder)
        for flag in ('--vram-reserve-mib', '--pool-workers', '--mmap-experts'):
            self.assertNotIn(flag, cfg['args'])
            self.assertNotIn(flag, cfg['user_args'])
        self.assertEqual(cfg['args'], ['--pack', 'p', '--max-context', '131072'])
        self.assertEqual(runner.check_cpu_quota(None, 800), None)

    def test_an_engine_that_takes_the_same_setting_under_another_name_gets_that_name(self):
        coder = copy.deepcopy(CODER_MANIFEST)
        coder['engine_options'] = {'vram_reserve_mib': '--reserve-mib', 'mmap_experts': '--mmap-experts auto'}
        coder['workers']['worker-d']['vram_reserve_mib'] = 600
        cfg = config(['--pack', 'p', '--vram-reserve-mib', '2048'], user_args=['--vram-reserve-mib', '2048'])
        self.assertEqual(runner.set_vram_reserve(cfg, coder, 'worker-d'), 600)
        self.assertEqual(cfg['args'], ['--pack', 'p', '--reserve-mib', '600'])
        self.assertEqual(cfg['user_args'], ['--reserve-mib', '600'])
        args = ['--max-context', '131072']
        runner.add_unified_memory_flags(args, coder)
        runner.add_unified_memory_flags(args, coder)
        self.assertEqual(args, ['--max-context', '131072', '--mmap-experts', 'auto'])

    def test_a_qwen_manifest_keeps_the_names_every_root_of_ours_was_started_with(self):
        gyro = copy.deepcopy(GYRO_MANIFEST)
        self.assertEqual(runner.engine_option_names(gyro)['vram_reserve_mib'], '--vram-reserve-mib')
        self.assertEqual(runner.engine_option_names(gyro)['pool_workers'], '--pool-workers')
        self.assertEqual(runner.engine_option_names(None)['mmap_experts'], '--mmap-experts')

    def test_a_host_without_a_quota_keeps_the_historical_eight_cores(self):
        backend = copy.deepcopy(LEGACY_MANIFEST)
        for node in backend['workers']:
            self.assertEqual(runner.cpu_quota_percent(backend, node), 800, node)

    def test_the_gpu_reserve_is_handed_to_the_experts(self):
        gyro = copy.deepcopy(GYRO_MANIFEST)
        self.assertEqual({gyro['workers'][n]['vram_reserve_mib'] for n in ('worker-a', 'worker-b')}, {700})
        cfg = config(['--vram-reserve-mib', '4915'])
        self.assertEqual(runner.set_vram_reserve(cfg, gyro, 'worker-a'), 700)
        self.assertEqual(cfg['args'], ['--vram-reserve-mib', '700'])
        cfg = config()
        self.assertEqual(runner.set_vram_reserve(cfg, gyro, 'worker-b'), 700)
        self.assertEqual(cfg['args'], ['--vram-reserve-mib', '700'])

    def test_a_value_the_engine_keeps_as_the_persons_own_is_rewritten_too(self):
        gyro = copy.deepcopy(GYRO_MANIFEST)
        cfg = config(['--vram-reserve-mib', '4915', '--pool-workers', '8'], user_args=['--vram-reserve-mib', '4915'])
        runner.set_vram_reserve(cfg, gyro, 'worker-a')
        runner.set_pool_workers(cfg, gyro, 'worker-a')
        self.assertEqual(cfg['args'], ['--vram-reserve-mib', '700', '--pool-workers', '7'])
        self.assertEqual(cfg['user_args'], ['--vram-reserve-mib', '700'])
        cfg = config(['--vram-reserve-mib', '4915'])
        runner.set_vram_reserve(cfg, gyro, 'worker-a')
        self.assertNotIn('user_args', cfg)

    def test_a_reserve_above_what_preparation_measured_is_still_refused(self):
        legacy_engine = copy.deepcopy(LEGACY_MANIFEST)
        self.assertEqual((legacy_engine['workers']['worker-a']['vram_reserve_mib'],
                          legacy_engine['workers']['worker-b']['vram_reserve_mib']), (4915, 3276))
        with self.assertRaisesRegex(RuntimeError, 'prepared GPU headroom differs'):
            runner.set_vram_reserve(config(['--vram-reserve-mib', '700']), legacy_engine, 'worker-a')
        with self.assertRaisesRegex(RuntimeError, 'prepared GPU headroom differs'):
            runner.set_vram_reserve(config(), legacy_engine, 'worker-a')

    def test_an_impossible_reserve_fails_before_the_service_starts(self):
        for asked in (-1, 65537, '700', True, None):
            with self.assertRaisesRegex(RuntimeError, 'invalid vram_reserve_mib'):
                runner.set_vram_reserve(config(), {'workers': {'worker-a': {'vram_reserve_mib': asked}}}, 'worker-a')
        self.assertEqual(runner.set_vram_reserve(config(), {'workers': {'worker-a': {'vram_reserve_mib': 0}}},
                                                 'worker-a'), 0)
        self.assertEqual(runner.set_vram_reserve(config(), {'workers': {'worker-a': {}}}, 'worker-a'), 700)

    def test_serve_command_confines_settings_and_forbids_acquisition(self):
        command = runner.serve_command(Path('/r'), Path('/r/runtime-claim-config.json'), 18090,
                                       runner.resource_properties(copy.deepcopy(GYRO_MANIFEST), 'worker-b', True),
                                       'agentiac-strata-inference')
        self.assertIn('--setenv=STRATA_NO_ACQUIRE=1', command)
        self.assertIn('--setenv=STRATA_SETTINGS_FILE=/r/home/strata-settings.json', command)
        self.assertIn('--setenv=XDG_CONFIG_HOME=/r/home/.config', command)
        self.assertIn('--setenv=HOME=/r/home', command)
        self.assertIn('--working-directory=/r/source', command)
        self.assertEqual(command[-2:], ['--config', '/r/runtime-claim-config.json'])
        self.assertNotIn('--select', command)

    def test_unlisted_context_is_refused_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_manifest(directory, 'gyro', GYRO_MANIFEST)
            with patch.object(sys, 'argv', ['start', '--unit', 'agentiac-strata-inference', '--node', 'worker-b',
                                            '--root', directory, '--attempt', 'claim',
                                            '--backend', str(manifest), '--context', '100000']), \
                    patch.object(runner.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'not one of'):
                    runner.main()
                run.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [manifest])


class UnifiedMemoryTests(unittest.TestCase):
    def test_unified_memory_follows_the_worker_declaration(self):
        self.assertTrue(runner.unified_memory(copy.deepcopy(UNIFIED_MANIFEST), 'worker-c'))
        self.assertFalse(runner.unified_memory(copy.deepcopy(GYRO_MANIFEST), 'worker-b'))
        self.assertFalse(runner.unified_memory(copy.deepcopy(LEGACY_MANIFEST), 'worker-a'))

    def test_mmap_experts_is_added_once(self):
        args = ['--max-context', '262144']
        runner.add_unified_memory_flags(args)
        runner.add_unified_memory_flags(args)
        self.assertEqual(args.count('--mmap-experts'), 1)
        self.assertIn('add_unified_memory_flags(engine_args, backend)',
                      (ROOT / 'scripts/strata-start-worker.py').read_text())

    def test_the_gpu_is_held_when_a_unit_process_opened_nvmap(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid, target in ((11, '/dev/null'), (12, '/dev/nvmap'),
                                (13, '/dev/nvidia-uvm'), (14, '/dev/nvidia-caps/nvidia-cap1')):
                (proc / str(pid) / 'fd').mkdir(parents=True)
                (proc / str(pid) / 'fd' / '3').symlink_to(target)
            self.assertTrue(runner.holds_gpu_device({11, 12}, proc))
            self.assertTrue(runner.holds_gpu_device({11, 13}, proc))
            self.assertTrue(runner.holds_gpu_device({11, 14}, proc))
            self.assertFalse(runner.holds_gpu_device({11}, proc))
            self.assertFalse(runner.holds_gpu_device({99}, proc))

    def test_unified_memory_admission_reads_ram_only_and_never_nvidia_smi(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(root, 'unified', UNIFIED_MANIFEST)
            (root / 'prepare-strata-qwen-iq3xxs-0.2.10-state.json').write_text(json.dumps({'stage': 'prepared'}))
            original = Path.read_text

            def read(path, *args, **kwargs):
                if str(path) == '/proc/meminfo':
                    return 'MemAvailable: 1048576 kB\n'
                return original(path, *args, **kwargs)

            with patch.object(sys, 'argv', ['start', '--unit', 'agentiac-strata-inference', '--node', 'worker-c',
                                            '--root', directory, '--backend', str(manifest), '--attempt', 'claim']), \
                    patch.object(runner, 'NVMAP_PARAMETERS', root / 'no-nvmap'), \
                    patch.object(Path, 'read_text', autospec=True, side_effect=read), \
                    patch.object(runner.subprocess, 'check_output') as check_output, \
                    patch.object(runner.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'insufficient free host RAM: 1.0 GiB available, 57.0 GiB'):
                    runner.main()
                check_output.assert_not_called()
                run.assert_not_called()


class GpuPagePoolTests(unittest.TestCase):
    """nvmap's page pool: RAM a Jetson's GPU freed, which MemAvailable omits."""
    GIB = 1024**3

    def nvmap(self, directory):
        parameters = Path(directory)
        (parameters / 'pool_size').write_text('2012093\n')
        (parameters / 'shrink_page_pools').write_text('0\n')
        return parameters

    def test_a_load_short_only_by_the_pool_empties_it_and_is_admitted(self):
        readings, released = iter([51.9 * self.GIB, 59.6 * self.GIB]), []
        available, gained = runner.admit_host_ram(57 * self.GIB, True, release=lambda: released.append(1) or True,
                                                  measure=lambda: next(readings))
        self.assertEqual(released, [1])
        self.assertEqual(available, 59.6 * self.GIB)
        self.assertAlmostEqual(gained / self.GIB, 7.7)

    def test_the_second_reading_decides_and_the_refusal_gives_both_figures(self):
        readings = iter([40 * self.GIB, 50 * self.GIB])
        with self.assertRaisesRegex(RuntimeError, r'insufficient free host RAM: 50\.0 GiB available, 57\.0 GiB needed'):
            runner.admit_host_ram(57 * self.GIB, True, release=lambda: True, measure=lambda: next(readings))

    def test_enough_ram_or_a_discrete_gpu_never_touches_the_pool(self):
        def release():
            raise AssertionError('pool released')
        self.assertEqual(runner.admit_host_ram(57 * self.GIB, True, release=release, measure=lambda: 60 * self.GIB),
                         (60 * self.GIB, 0))
        with self.assertRaisesRegex(RuntimeError, 'insufficient free host RAM'):
            runner.admit_host_ram(57 * self.GIB, False, release=release, measure=lambda: 10 * self.GIB)

    def test_release_writes_the_pool_capacity_through_sudo(self):
        with tempfile.TemporaryDirectory() as directory:
            parameters = self.nvmap(directory)
            with patch.object(runner.subprocess, 'run', return_value=runner.subprocess.CompletedProcess([], 0)) as run:
                self.assertTrue(runner.release_gpu_page_pool(parameters))
            argv, kwargs = run.call_args.args[0], run.call_args.kwargs
            self.assertEqual(argv, ['sudo', '-n', 'tee', str(parameters / 'shrink_page_pools')])
            self.assertEqual(kwargs['input'], '2012093\n')
            with patch.object(runner.subprocess, 'run', return_value=runner.subprocess.CompletedProcess([], 1)):
                self.assertFalse(runner.release_gpu_page_pool(parameters))
        with patch.object(runner.subprocess, 'run') as run:
            self.assertFalse(runner.release_gpu_page_pool(Path(directory) / 'gone'))
            run.assert_not_called()

    def test_the_engine_unit_empties_the_pool_as_root_whenever_it_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            parameters = self.nvmap(directory)
            self.assertEqual(runner.gpu_page_pool_properties(parameters),
                             [f'--property=ExecStopPost=+/bin/sh -c "cat {parameters}/pool_size > '
                              f'{parameters}/shrink_page_pools"'])
            self.assertEqual(runner.gpu_page_pool_properties(parameters / 'gone'), [])

    def test_a_unified_claim_after_an_unload_starts_with_the_pool_emptied_and_the_stop_hook(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as nvmap:
            root, parameters = Path(directory), self.nvmap(nvmap)
            manifest = write_manifest(root, 'unified', UNIFIED_MANIFEST)
            (root / 'prepare-strata-qwen-iq3xxs-0.2.10-state.json').write_text(
                json.dumps({'stage': 'prepared', 'config': str(root / 'cfg.json')}))
            (root / 'api-key-claim.env').write_text('LITELLM_MASTER_KEY=k\n')
            (root / 'cfg.json').write_text(json.dumps({'args': ['--max-context', '262144', '--vram-reserve-mib', '6144']}))
            readings = iter(['MemAvailable: 54421900 kB\n', 'MemAvailable: 62459544 kB\n'])
            original = Path.read_text

            def read(path, *args, **kwargs):
                return next(readings) if str(path) == '/proc/meminfo' else original(path, *args, **kwargs)

            calls = []

            def run(argv, **kwargs):
                calls.append(argv)
                if argv[:3] == ['sudo', '-n', 'systemd-run']:
                    raise SystemExit('started')
                return runner.subprocess.CompletedProcess(argv, 0)

            server = type(sys)('serve.server')
            server.clean_shared_defaults = lambda defaults: None
            with patch.object(sys, 'argv', ['start', '--unit', 'agentiac-strata-inference', '--node', 'worker-c',
                                            '--root', directory, '--port', '0',
                                            '--backend', str(manifest), '--attempt', 'claim']), \
                    patch.object(runner, 'NVMAP_PARAMETERS', parameters), \
                    patch.dict(sys.modules, {'serve': type(sys)('serve'), 'serve.server': server}), \
                    patch.object(Path, 'read_text', autospec=True, side_effect=read), \
                    patch.object(runner.subprocess, 'check_output') as check_output, \
                    patch.object(runner.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(SystemExit, 'started'):
                    runner.main()
                check_output.assert_not_called()
            self.assertEqual(calls[0], ['sudo', '-n', 'tee', str(parameters / 'shrink_page_pools')])
            self.assertIn(runner.gpu_page_pool_properties(parameters)[0], calls[-1])
            state = json.loads((root / 'start-strata-qwen-iq3xxs-0.2.10-256k-claim-state.json').read_text())
            self.assertEqual(state['mem_available_before'], 62459544 * 1024)
            self.assertEqual(state['gpu_page_pool_released'], (62459544 - 54421900) * 1024)


class DrafterAdmissionTests(unittest.TestCase):
    """What a claim does with the drafter a manifest declares."""

    def backend(self, drafter='dflash2', pack=True, payload=b'pack bytes'):
        manifest = {'id': 'fixture-drafter', 'workers': {'worker-c': {'drafter': drafter}}}
        if pack:
            manifest['drafter_pack'] = {'path': 'dflash2/fixture.pack', 'size': len(payload),
                                        'sha256': hashlib.sha256(payload).hexdigest()}
        return manifest, payload

    def root_holding(self, directory, manifest, payload):
        root = Path(directory)
        if manifest.get('drafter_pack'):
            pack = root / 'data' / manifest['drafter_pack']['path']
            pack.parent.mkdir(parents=True, exist_ok=True)
            pack.write_bytes(payload)
        return root

    def test_the_declared_drafter_is_written_into_the_config_the_front_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, payload = self.backend()
            cfg = config(args=('--max-context', '262144', '--mtp', '/r/data/models/mtp'))
            root = self.root_holding(directory, manifest, payload)
            self.assertEqual(runner.set_drafter(cfg, manifest, 'worker-c', root), 'dflash2')
            self.assertEqual(cfg['drafting'], 'dflash2')

    def test_a_root_that_holds_no_pack_is_refused_rather_than_serving_the_mtp(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = self.backend()
            with self.assertRaisesRegex(RuntimeError, 'holds no dflash2/fixture.pack'):
                runner.set_drafter(config(), manifest, 'worker-c', self.root_holding(directory, {'drafter_pack': None},
                                                                                    b''))

    def test_a_pack_that_is_not_the_pinned_bytes_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = self.backend()
            root = self.root_holding(directory, manifest, b'other bytes')
            with self.assertRaisesRegex(RuntimeError, 'the drafter pack this manifest pins is not what the root holds'):
                runner.set_drafter(config(), manifest, 'worker-c', root)

    def test_a_drafter_named_without_a_pack_is_a_refusal_not_the_engines_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = self.backend(pack=False)
            with self.assertRaisesRegex(RuntimeError, 'names no drafter_pack'):
                runner.set_drafter(config(), manifest, 'worker-c', Path(directory))

    def test_the_mtp_and_an_undeclared_manifest_leave_the_engine_default_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = self.backend(drafter='mtp')
            cfg = config()
            self.assertEqual(runner.set_drafter(cfg, manifest, 'worker-c', Path(directory)), 'mtp')
            self.assertEqual(cfg['drafting'], 'mtp')
            plain = {'id': 'plain', 'workers': {'worker-c': {}}}
            cfg = config()
            self.assertIsNone(runner.set_drafter(cfg, plain, 'worker-c', Path(directory)))
            self.assertNotIn('drafting', cfg)

    def test_the_shipped_drafter_manifests_pin_their_pack(self):
        for path in sorted((SITE / 'backends').glob('strata-*.json')):
            manifest = json.loads(path.read_text())
            dflash_hosts = [h for h, w in manifest.get('workers', {}).items() if w.get('drafter') == 'dflash2']
            if dflash_hosts:
                self.assertTrue(manifest['drafter_pack']['path'], path.name)
                self.assertGreater(manifest['drafter_pack']['size'], 0, path.name)
                self.assertEqual(len(manifest['drafter_pack']['sha256']), 64, path.name)


class ClaimEngineOptionTests(unittest.TestCase):
    """`claim --engine-option NAME=VALUE`: the measurement knob, and the list that fences it."""

    def setUp(self):
        self.coder = copy.deepcopy(CODER_MANIFEST)
        self.qwen = copy.deepcopy(GYRO_MANIFEST)

    def test_the_list_is_the_options_the_engine_named(self):
        for name in ('experts', 'cpu_threads', 'dspark', 'dspark_conf', 'prefill_chunk', 'parallel', 'reserve_mib',
                     'mmap-experts', 'vision', 'tp'):
            with self.subTest(option=name):
                self.assertIn(name, self.coder['engine_flags'])

    def test_a_named_option_reaches_the_engine_in_the_engines_own_spelling(self):
        self.assertEqual(runner.parse_engine_options(self.coder, ['experts=6']), [('--experts', '6')])
        self.assertEqual(runner.parse_engine_options(self.coder, ['dspark_conf=0.8']), [('--dspark-conf', '0.8')])

    def test_an_option_the_manifest_does_not_name_is_refused_with_the_list(self):
        with self.assertRaises(SystemExit) as caught:
            runner.parse_engine_options(self.coder, ['pool_workers=6'])
        self.assertIn('names no such engine option', str(caught.exception))
        self.assertIn('--experts', '--' + 'experts')
        self.assertIn('experts', str(caught.exception))

    def test_a_manifest_with_no_engine_flags_cannot_be_overridden_at_all(self):
        with self.assertRaises(SystemExit) as caught:
            runner.parse_engine_options(self.qwen, ['experts=6'])
        self.assertIn('declares no engine_flags', str(caught.exception))

    def test_an_option_without_a_value_is_refused(self):
        with self.assertRaises(SystemExit):
            runner.parse_engine_options(self.coder, ['experts'])

    def test_the_option_lands_in_args_and_in_the_saved_options(self):
        cfg = config(['--dspark', 'auto'], user_args=['--cpu-threads', '14', '--experts', '5'])
        applied = runner.apply_engine_options(cfg, cfg['args'], [('--experts', '6')])
        self.assertEqual(cfg['args'][cfg['args'].index('--experts') + 1], '6')
        self.assertEqual(cfg['user_args'][cfg['user_args'].index('--experts') + 1], '6')
        self.assertEqual(applied, {'experts': '6'})

    def test_a_new_option_goes_to_args_and_is_not_invented_into_the_saved_options(self):
        cfg = config(['--dspark', 'auto'], user_args=['--cpu-threads', '14'])
        runner.apply_engine_options(cfg, cfg['args'], [('--experts', '6')])
        self.assertEqual(cfg['args'][cfg['args'].index('--experts') + 1], '6')
        self.assertNotIn('--experts', cfg['user_args'])

    def test_the_coder_manifest_declares_full_quality_and_why(self):
        self.assertEqual(self.coder['engine_settings'], {'experts': '6'})
        self.assertIn('integrated', self.coder['engine_flags']['experts'])
        self.assertIn('8064 of 8064 in VRAM', self.coder['engine_flags']['experts'])
        for path in sorted((SITE / 'backends').glob('strata-*.json')):
            manifest = json.loads(path.read_text())
            if manifest.get('engine_settings'):
                self.assertTrue(runner.manifest_engine_options(manifest), path.name)

    def test_a_declared_setting_reaches_the_engine_in_the_engines_own_spelling(self):
        self.assertEqual(runner.manifest_engine_options(self.coder), [('--experts', '6')])

    def test_a_declared_setting_outside_the_engines_list_is_refused_too(self):
        backend = {'id': 'fake', 'engine_flags': {'experts': 'the engine takes this one'},
                   'engine_settings': {'pool_workers': 6}}
        with self.assertRaises(SystemExit) as caught:
            runner.manifest_engine_options(backend)
        self.assertIn('names no such engine option', str(caught.exception))

    def test_a_claims_word_wins_over_the_manifests(self):
        cfg = config(['--dspark', 'auto'])
        runner.apply_engine_options(cfg, cfg['args'], runner.manifest_engine_options(self.coder))
        runner.apply_engine_options(cfg, cfg['args'], [('--experts', '5')])
        self.assertEqual(cfg['args'][cfg['args'].index('--experts') + 1], '5')

    def test_the_start_records_where_each_option_came_from(self):
        source = (ROOT / 'scripts/strata-start-worker.py').read_text()
        self.assertIn("(manifest_engine_options(backend), 'manifest')", source)
        self.assertIn("(parse_engine_options(backend, args.engine_option), 'claim')", source)
        self.assertIn("'engine_option_sources': engine_option_sources or None", source)

    def test_every_stage_of_the_start_records_what_the_claim_added(self):
        source = (ROOT / 'scripts/strata-start-worker.py').read_text()
        self.assertIn("'engine_options': applied_options or None", source)
        self.assertIn("applied_options.update(apply_engine_options(", source)


if __name__ == '__main__':
    unittest.main()
