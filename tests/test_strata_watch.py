import importlib.util
from pathlib import Path
import unittest

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
        default = watch.container_cgroup_paths('\t\n'.join(['3f2a' * 16, '']))
        self.assertEqual(default[0], Path('/sys/fs/cgroup/system.slice/docker-' + '3f2a' * 16 + '.scope'))
        moved = watch.container_cgroup_paths('abc123\tk8s.io')
        self.assertEqual(moved[0], Path('/sys/fs/cgroup/k8s.io/docker-abc123.scope'))
        absolute = watch.container_cgroup_paths('abc123\t/custom/cgroup')
        self.assertEqual(absolute[0], Path('/custom/cgroup'))
        self.assertEqual(watch.container_cgroup_paths('\tabc'), [])

    def test_an_engine_answer_without_the_named_field_still_reads_as_an_answer(self):
        self.assertEqual(watch.status_figure({'engine': {'loaded': True}}, 'engine'), ({'loaded': True}, True))
        self.assertEqual(watch.status_figure({'status': 'ok'}, 'engine'), (None, True))
        self.assertEqual(watch.status_figure(None, 'engine'), (None, False))
        self.assertEqual(watch.status_figure('', 'engine'), (None, False))
