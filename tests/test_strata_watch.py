import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('strata_watch', Path(__file__).resolve().parents[1] / 'scripts/strata-trial-watch.py')
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class StrataTrialWatchTests(unittest.TestCase):
    def test_meminfo_gives_available_and_swap_used_in_bytes(self):
        text = 'MemTotal: 100 kB\nMemAvailable: 40 kB\nSwapTotal: 10 kB\nSwapFree: 4 kB\n'
        self.assertEqual(watch.meminfo(text), (40 * 1024, 6 * 1024))

    def test_memory_stat_separates_the_pinned_experts_from_the_page_cache(self):
        # memory.current can be mostly page-locked experts (anon, unreclaimable), so it is not free memory
        # plus a few GiB of pack pages (file, reclaimable); only the first was the memory pressure
        text = 'anon 22548578304\nfile 6442450944\nslab 0\nkernel 1\nsock 0\n'
        self.assertEqual(watch.memory_stat(text), (22548578304, 6442450944))
        self.assertEqual(watch.memory_stat(''), (None, None))
        self.assertEqual(watch.memory_stat('anon 1\nfile x\n'), (1, None))

    def test_only_gpu_processes_outside_our_cgroup_are_foreign(self):
        self.assertEqual(watch.foreign_gpu_pids('101, 9000\n202, 500\nNo running processes found\n', {101}), [202])
        self.assertEqual(watch.foreign_gpu_pids('', set()), [])

    def test_stop_rules(self):
        floor = 2 * 1024**3
        self.assertIsNone(watch.stop_reason({'mem_available': floor, 'foreign_gpu_pids': []}, floor))
        self.assertIn('MemAvailable', watch.stop_reason({'mem_available': floor - 1, 'foreign_gpu_pids': []}, floor))
        self.assertIn('GPU', watch.stop_reason({'mem_available': floor, 'foreign_gpu_pids': [7]}, floor))

    def test_a_jetson_gpu_figure_is_none_instead_of_a_crash(self):
        self.assertEqual(watch.gpu_used_mib('15483\n'), 15483)
        self.assertIsNone(watch.gpu_used_mib('[N/A]\n'))
        self.assertIsNone(watch.gpu_used_mib(''))
        self.assertEqual(watch.foreign_gpu_pids('[N/A], [N/A]\n', set()), [])

    def test_a_key_file_is_an_env_line_or_a_bare_value(self):
        # a claim writes <root>/api-key-claim.env, a host's own front key is <root>/api.key with no '=' at all, and
        # reading the second as the first raised IndexError before the first sample
        self.assertEqual(watch.api_key('LITELLM_MASTER_KEY=sk-claim\n'), 'sk-claim')
        self.assertEqual(watch.api_key('sk-host-key\n'), 'sk-host-key')
        self.assertEqual(watch.api_key('KEY=sk-with-padding==\n'), 'sk-with-padding==')
        self.assertEqual(watch.api_key('  sk-padded  \n'), 'sk-padded')
        self.assertEqual(watch.api_key('dGVzdA==\n'), 'dGVzdA==')
        self.assertEqual(watch.api_key('dGVzdA=\n'), 'dGVzdA=')
        self.assertEqual(watch.api_key('sk-bare=token\n'), 'sk-bare=token')
        self.assertEqual(watch.api_key('# comment\n\nLITELLM_MASTER_KEY=sk-commented\n'), 'sk-commented')

    def test_docker_top_owns_a_header_and_the_container_pids_are_ours(self):
        self.assertEqual(watch.docker_pids('PID\n101\n202\n'), {101, 202})
        self.assertEqual(watch.docker_pids('PID\n101\n'), {101})
        self.assertEqual(watch.docker_pids('PID\n'), set())
        self.assertEqual(watch.docker_pids(''), set())

    def test_our_container_pid_is_not_a_foreign_gpu_process(self):
        # the bug this exists for: nvidia-smi reports the container's process, the unit cgroup does not contain it, so
        # the watch saw our own engine on the GPU as another tenant and stopped the service it was watching
        ours = watch.docker_pids('PID\n101\n')
        self.assertEqual(watch.foreign_gpu_pids('101, 9000\n202, 500\n', ours), [202])
        self.assertEqual(watch.foreign_gpu_pids('101, 9000\n', ours), [])

    def test_the_container_cgroup_is_where_the_model_memory_is_measured(self):
        cid = '3f2a' * 16
        default = watch.container_cgroup_paths(f'{cid}\t\n')
        self.assertEqual(default, [
            Path(f'/sys/fs/cgroup/system.slice/docker-{cid}.scope'),
            Path(f'/sys/fs/cgroup/docker/{cid}'),
        ])
        moved = watch.container_cgroup_paths('abc123\tk8s.io')
        self.assertEqual(moved[:2], [
            Path('/sys/fs/cgroup/k8s.io/docker-abc123.scope'),
            Path('/sys/fs/cgroup/k8s.io/abc123'),
        ])
        absolute = watch.container_cgroup_paths('abc123\t/custom/cgroup')
        self.assertEqual(absolute[:2], [
            Path('/sys/fs/cgroup/custom/cgroup/docker-abc123.scope'),
            Path('/sys/fs/cgroup/custom/cgroup/abc123'),
        ])
        self.assertNotIn(Path('/custom/cgroup'), absolute)
        self.assertNotIn(Path('/sys/fs/cgroup/custom/cgroup'), absolute)
        self.assertEqual(watch.container_cgroup_paths('\tabc'), [])

    def test_proc_cgroup_path_reads_v2_and_ignores_root(self):
        self.assertEqual(watch.proc_cgroup_path('0::/system.slice/docker-abc.scope\n'),
                         Path('/sys/fs/cgroup/system.slice/docker-abc.scope'))
        self.assertEqual(watch.proc_cgroup_path('0::/a.slice/a-b.slice/docker-abc.scope\n'),
                         Path('/sys/fs/cgroup/a.slice/a-b.slice/docker-abc.scope'))
        self.assertIsNone(watch.proc_cgroup_path('0::/\n'))
        self.assertIsNone(watch.proc_cgroup_path(''))

    def test_cli_parser_accepts_front_unit_and_container_options(self):
        args = watch.build_parser().parse_args([
            '--log', '/tmp/watch.jsonl',
            '--port', '18885',
            '--key-file', '/tmp/api.key',
            '--unit', 'agent-infra-strata-inference',
            '--front', 'agent-infra-ondemand',
        ])
        self.assertEqual(args.front, 'agent-infra-ondemand')
        self.assertEqual(args.unit, 'agent-infra-strata-inference')
        self.assertIsNone(args.container)

        container_args = watch.build_parser().parse_args([
            '--log', '/tmp/watch.jsonl',
            '--port', '18885',
            '--key-file', '/tmp/api.key',
            '--unit', 'agent-infra-infernix',
            '--front', 'agent-infra-ondemand',
            '--container', 'agent-infra-infernix',
            '--status-path', '/health',
            '--status-key', 'loaded',
        ])
        self.assertEqual(container_args.container, 'agent-infra-infernix')
        self.assertEqual(container_args.status_path, '/health')
        self.assertEqual(container_args.status_key, 'loaded')

        with self.assertRaises(SystemExit):
            watch.build_parser().parse_args([
                '--log', '/tmp/watch.jsonl',
                '--port', '18885',
                '--key-file', '/tmp/api.key',
                '--unit', 'agent-infra-strata-inference',
            ])

    def test_an_engine_answer_without_the_named_field_still_reads_as_an_answer(self):
        self.assertEqual(watch.status_figure({'engine': {'loaded': True}}, 'engine'), ({'loaded': True}, True))
        self.assertEqual(watch.status_figure({'status': 'ok'}, 'engine'), (None, True))
        self.assertEqual(watch.status_figure(None, 'engine'), (None, False))
        self.assertEqual(watch.status_figure('', 'engine'), (None, False))


class SampleTests(unittest.TestCase):
    def _meminfo(self):
        return 'MemTotal: 100000 kB\nMemAvailable: 50000 kB\nSwapTotal: 1000 kB\nSwapFree: 1000 kB\n'

    def _completed(self, stdout='', returncode=0):
        return mock.Mock(stdout=stdout, returncode=returncode)

    def test_container_sample_reads_cgroup_memory_and_accepts_container_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            cgroup = Path(tmp) / 'docker-abc.scope'
            cgroup.mkdir()
            (cgroup / 'memory.current').write_text('4096\n')
            (cgroup / 'memory.peak').write_text('8192\n')
            (cgroup / 'memory.stat').write_text('anon 3000\nfile 1096\n')
            (cgroup / 'cgroup.procs').write_text('101\n')

            meminfo_path = Path(tmp) / 'meminfo'
            meminfo_path.write_text(self._meminfo())
            real_path = Path

            def fake_path(arg):
                return meminfo_path if arg == '/proc/meminfo' else real_path(arg)

            with mock.patch.object(watch, 'Path', side_effect=fake_path), \
                 mock.patch.object(watch, 'container_cgroup_paths', return_value=[cgroup]), \
                 mock.patch.object(watch, 'engine_status', return_value={'status': 'ok'}), \
                 mock.patch.object(watch.subprocess, 'run', side_effect=[
                     self._completed('abc\t\n'),
                     self._completed('PID\n101\n'),
                     self._completed('1024\n'),
                     self._completed('101, 1024\n'),
                 ]):
                record = watch.sample(18885, 'sk-test', 'wrapper.service', container='c1',
                                      status_path='/health', status_key='engine')

            self.assertEqual(record['cgroup_path'], str(cgroup))
            self.assertEqual(record['cgroup_current'], 4096)
            self.assertEqual(record['cgroup_peak'], 8192)
            self.assertEqual((record['cgroup_anon'], record['cgroup_file']), (3000, 1096))
            self.assertEqual(record['pid_source'], 'docker-top')
            self.assertEqual(record['foreign_gpu_pids'], [])
            self.assertTrue(record['status_ok'])
            self.assertIsNone(record['engine'])

    def test_startup_race_recheck_prevents_false_foreign_pid_and_still_catches_real_foreign_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            cgroup = Path(tmp) / 'docker-abc.scope'
            cgroup.mkdir()
            (cgroup / 'memory.current').write_text('1024\n')
            meminfo_path = Path(tmp) / 'meminfo'
            meminfo_path.write_text(self._meminfo())
            real_path = Path

            def fake_path(arg):
                return meminfo_path if arg == '/proc/meminfo' else real_path(arg)

            # Case 1: docker top initially empty while container spawns; re-check after nvidia-smi finds PID 101.
            with mock.patch.object(watch, 'Path', side_effect=fake_path), \
                 mock.patch.object(watch, 'container_cgroup_paths', return_value=[cgroup]), \
                 mock.patch.object(watch, 'engine_status', return_value=None), \
                 mock.patch.object(watch.subprocess, 'run', side_effect=[
                     self._completed('abc\t\n'),
                     self._completed('PID\n'),
                     self._completed('512\n'),
                     self._completed('101, 512\n'),
                     self._completed('abc\t\n'),
                     self._completed('PID\n101\n'),
                 ]):
                race_record = watch.sample(18885, 'sk-test', 'wrapper.service', container='c1')
            self.assertEqual(race_record['foreign_gpu_pids'], [])

            # Case 2: real foreign PID 202 remains foreign after re-check.
            with mock.patch.object(watch, 'Path', side_effect=fake_path), \
                 mock.patch.object(watch, 'container_cgroup_paths', return_value=[cgroup]), \
                 mock.patch.object(watch, 'engine_status', return_value=None), \
                 mock.patch.object(watch.subprocess, 'run', side_effect=[
                     self._completed('abc\t\n'),
                     self._completed('PID\n101\n'),
                     self._completed('512\n'),
                     self._completed('101, 512\n202, 256\n'),
                     self._completed('abc\t\n'),
                     self._completed('PID\n101\n'),
                 ]):
                foreign_record = watch.sample(18885, 'sk-test', 'wrapper.service', container='c1')
            self.assertEqual(foreign_record['foreign_gpu_pids'], [202])

    def test_vanished_cgroup_during_unload_does_not_crash_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_cgroup = Path(tmp) / 'docker-gone.scope'
            meminfo_path = Path(tmp) / 'meminfo'
            meminfo_path.write_text(self._meminfo())
            real_path = Path

            def fake_path(arg):
                return meminfo_path if arg == '/proc/meminfo' else real_path(arg)

            fake_group = mock.MagicMock()
            fake_group.exists.return_value = True
            fake_group.__truediv__.return_value.read_text.side_effect = FileNotFoundError('gone')

            with mock.patch.object(watch, 'Path', side_effect=fake_path), \
                 mock.patch.object(watch, 'container_report', return_value=(fake_group, {101}, 'docker-top')), \
                 mock.patch.object(watch, 'engine_status', return_value=None), \
                 mock.patch.object(watch.subprocess, 'run', side_effect=[
                     self._completed('0\n'),
                     self._completed(''),
                 ]):
                record = watch.sample(18885, 'sk-test', 'wrapper.service', container='c1')

            self.assertIsNone(record['cgroup_current'])
            self.assertIsNone(record['cgroup_path'])
            self.assertEqual(record['foreign_gpu_pids'], [])
