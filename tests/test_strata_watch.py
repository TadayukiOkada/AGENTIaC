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
