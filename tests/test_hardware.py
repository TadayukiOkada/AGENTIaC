"""T10.1: ops inventory records facts with source and confidence, and hw_fp follows from them.

The probes are not tested (they read a real machine); everything that turns their text into a
PA §9.1 record is, including the four ways the first real run on six hosts got it wrong:
`free -b` has no `MemTotal:` label, `uname -a` ends in `GNU/Linux`, an integrated GPU reports
`[N/A]` for memory, and a host answers under a tailnet name the inventory does not use.
"""
import datetime
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import hardware, identities, paths  # noqa: E402
SITE = paths.root()

SPARK = {
    'os': {'stdout': 'worker-a\nLinux worker-a 7.0.0-1019-nvidia #19~24.04.2-Ubuntu SMP PREEMPT_DYNAMIC '
                     'Sun Sep  6 18:35:11 UTC 2 aarch64 aarch64 aarch64 GNU/Linux\n'
                     'PRETTY_NAME="Ubuntu 24.04.5 LTS"\nNAME="Ubuntu"\n'},
    'resources': {'stdout': ' 02:14:31 up 5 days,  8:24\n               total        used        free\n'
                            'Mem:     130661138432 103423053824  7503802368\nSwap:    63999995904  1411\n'},
    'meminfo': {'stdout': ''},
    'cpu_facts': {'stdout': 'arch: aarch64\ncpus: 20\ndtmodel: \nsocid: jep106:0426:8901\nsocfamily: jep106:0426\n'},
    'gpu_facts': {'stdout': 'NVIDIA GB10, 580.178.04, [N/A]\n'},
    'gpu_list': {'stdout': 'GPU 0: NVIDIA GB10 (UUID: GPU-0df16968)\n'},
    'gpu': {'stdout': '| Driver Version: 580.178.04  |  CUDA Version: 13.0 |\n'},
    'cuda': {'stdout': 'Cuda compilation tools, release 13.0, V13.0.48\n'},
}
ORIN = {
    'os': {'stdout': 'worker-b\nLinux worker-b 6.8.12-1021-tegra #1 SMP PREEMPT Thu Aug  6 21:56:04 PDT 2026 '
                     'aarch64 aarch64 aarch64 GNU/Linux\nPRETTY_NAME="Ubuntu 24.04.5 LTS"\n'},
    'resources': {'stdout': 'Mem:     65868697600 10488692736 30865395712\n'},
    'cpu_facts': {'stdout': 'arch: aarch64\ncpus: 12\ndtmodel: NVIDIA Jetson AGX Orin Developer Kit\n'
                            'socid: 35\nsocfamily: Tegra\n'},
    'gpu_facts': {'stdout': 'Orin (nvgpu), 595.78, [N/A]\n'},
    'gpu_list': {'stdout': 'GPU 0: Orin (nvgpu) (UUID: 11ce98d5)\n'},
    'gpu': {'stdout': ''},
}
GENIE = {
    'os': {'stdout': 'worker-e\nLinux worker-e 6.8.0-138-generic #138-Ubuntu SMP x86_64 x86_64 x86_64 GNU/Linux\n'
                     'PRETTY_NAME="Ubuntu 22.04.5 LTS"\n'},
    'resources': {'stdout': 'Mem:     67120521216 1 2\n'},
    'cpu_facts': {'stdout': 'arch: x86_64\ncpus: 24\ndtmodel: \nsocid: \nsocfamily: \n'
                            'model\t\t: 151\nmodel name\t: 13th Gen Intel(R) Core(TM) i9-13900K\n'},
    'gpu_facts': {'stdout': 'NVIDIA GeForce RTX 4080, 615.71.10, 16376\n'},
    'gpu_list': {'stdout': 'GPU 0: NVIDIA GeForce RTX 4080 (UUID: GPU-1)\n'},
    'gpu': {'stdout': ''},
}
NODE = {'node_class': 'spark', 'inference_cpus': 14, 'memory_limit': '97G', 'ansible_host': 'worker-a'}


class ParseTests(unittest.TestCase):
    def test_uname_line_ending_in_gnu_linux_still_yields_kernel_and_arch(self):
        read = hardware.parse_os(ORIN['os']['stdout'])
        self.assertEqual(read['kernel'], '6.8.12-1021-tegra')
        self.assertEqual(read['arch'], 'aarch64')
        self.assertEqual(read['os'], 'Ubuntu 24.04.5 LTS')

    def test_memory_comes_from_the_free_row_and_from_proc_meminfo(self):
        self.assertEqual(hardware.parse_memory(ORIN['resources']['stdout']), 65868697600)
        self.assertEqual(hardware.parse_memory('MemTotal:       135059730432 kB'), 135059730432)
        self.assertIsNone(hardware.parse_memory('MemTotal:\nSwapTotal:\n'))

    def test_cpu_name_prefers_the_device_tree_then_the_soc_id_and_never_a_core_part_number(self):
        orin = hardware.parse_cpu(ORIN['cpu_facts']['stdout'])
        self.assertEqual(orin['cpu'], 'NVIDIA Jetson AGX Orin Developer Kit')
        self.assertEqual(orin['cpu_count'], 12)
        spark = hardware.parse_cpu(SPARK['cpu_facts']['stdout'])
        self.assertEqual(spark['cpu'], 'jep106:0426:8901')  # no device tree on the GB10 board
        self.assertEqual(hardware.cpu_source(SPARK['cpu_facts']['stdout']), '/sys/devices/soc0/soc_id')
        core_only = hardware.parse_cpu('arch: aarch64\ncpus: 8\nCPU part\t: 0xd87\n')
        self.assertIsNone(core_only['cpu'])  # a big.LITTLE host has two of these; picking one is a guess
        self.assertEqual(hardware.parse_cpu(GENIE['cpu_facts']['stdout'])['cpu'],
                         '13th Gen Intel(R) Core(TM) i9-13900K')  # model name, not model: 151

    def test_an_unknown_arch_is_not_an_arch(self):
        self.assertIsNone(hardware.parse_cpu('arch: whatever\ncpus: 4\n')['arch'])

    def test_an_integrated_gpu_reports_no_vram_rather_than_a_zero(self):
        read = hardware.parse_gpu(SPARK['gpu_facts']['stdout'], SPARK['gpu_list']['stdout'])
        self.assertEqual(read['gpu'], 'NVIDIA GB10')
        self.assertEqual(read['gpu_count'], 1)
        self.assertIsNone(read['gpu_vram_mib'])  # [N/A] is not a size

    def test_gpu_falls_back_to_the_listing_and_the_table(self):
        read = hardware.parse_gpu('', 'GPU 0: Orin (nvgpu) (UUID: x)\nGPU 1: Orin (nvgpu) (UUID: y)\n',
                                  '| Driver Version: 540.4.0 |')
        self.assertEqual((read['gpu'], read['gpu_count'], read['driver']), ('Orin (nvgpu)', 2, '540.4.0'))
        self.assertEqual(hardware.parse_gpu('', '', ''), dict.fromkeys(('gpu', 'gpu_count', 'gpu_vram_mib', 'driver')))

    def test_two_different_gpus_in_one_host_are_one_honest_name(self):
        self.assertEqual(hardware.parse_gpu('RTX 3090, 550, 24576\nRTX 4090, 550, 24576\n')['gpu'],
                         'RTX 3090 + RTX 4090')

    def test_cuda_release(self):
        self.assertEqual(hardware.parse_cuda(SPARK['cuda']['stdout']), '13.0')
        self.assertIsNone(hardware.parse_cuda(''))


class RecordTests(unittest.TestCase):
    def test_every_fact_is_present_and_the_detected_ones_name_their_command(self):
        read = hardware.record('worker-a', SPARK, NODE)
        for name in (*hardware.DETECTED, *hardware.DECLARED):
            self.assertIn(name, read['facts'])
        self.assertEqual(read['facts']['gpu'], {'value': 'NVIDIA GB10', 'source': 'nvidia-smi --query-gpu',
                                                'confidence': 'detected'})
        self.assertEqual(read['facts']['memory_total']['source'], 'free -b / /proc/meminfo')

    def test_declared_node_facts_are_manual_and_stay_apart_from_the_read_ones(self):
        read = hardware.record('worker-a', SPARK, NODE)
        self.assertEqual(read['facts']['inference_cpus'],
                         {'value': 14, 'source': 'infra/inventory.yml', 'confidence': 'manual'})
        self.assertEqual(read['facts']['memory_limit']['value'], '97G')
        self.assertEqual(read['facts']['memory_total']['value'], 130661138432)  # the cap did not replace the reading

    def test_a_host_nobody_could_read_is_all_unknown_and_still_valid(self):
        read = hardware.record('nowhere', {})
        self.assertEqual(read['hw_fp'], identities.UNKNOWN)
        self.assertEqual(len(read['unknowns']), len(read['facts']))
        self.assertEqual(hardware.validate(read), [])
        for entry in read['facts'].values():
            self.assertEqual(entry, {'value': None, 'source': None, 'confidence': 'unknown'})

    def test_nothing_is_invented_when_a_probe_is_missing(self):
        without_gpu = {name: text for name, text in SPARK.items() if not name.startswith('gpu')}
        read = hardware.record('worker-a', without_gpu, NODE)
        for name in ('gpu', 'gpu_count', 'gpu_vram_mib', 'driver'):
            self.assertEqual(read['facts'][name]['confidence'], 'unknown')
        self.assertEqual(read['hw_fp'], identities.UNKNOWN)  # hw_fp refuses to hash missing facts

    def test_memory_bandwidth_is_unknown_because_nothing_measures_it(self):
        self.assertEqual(hardware.record('worker-a', SPARK, NODE)['facts']['memory_bandwidth']['confidence'], 'unknown')

    def test_hw_fp_is_the_hash_of_the_facts_and_nothing_else(self):
        read = hardware.record('worker-a', SPARK, NODE)
        self.assertEqual(read['hw_fp'], identities.hardware_fingerprint(
            hardware.fingerprint_inputs({name: entry['value'] for name, entry in read['facts'].items()})))

    def test_two_identical_boards_separated_by_firmware_reservation_are_one_machine(self):
        other = json.loads(json.dumps(ORIN))
        other['resources']['stdout'] = 'Mem:     65932288000 1 2\n'  # 63 MiB more, same board
        self.assertEqual(hardware.record('worker-b', ORIN)['hw_fp'], hardware.record('worker-c', other)['hw_fp'])
        smaller = json.loads(json.dumps(ORIN))
        smaller['resources']['stdout'] = 'Mem:     32000000000 1 2\n'
        self.assertNotEqual(hardware.record('worker-b', ORIN)['hw_fp'], hardware.record('worker-d', smaller)['hw_fp'])

    def test_different_machines_do_not_share_an_identity(self):
        fingerprints = {hardware.record(host, probes)['hw_fp']
                        for host, probes in (('worker-a', SPARK), ('worker-b', ORIN), ('worker-e', GENIE))}
        self.assertEqual(len(fingerprints), 3)
        self.assertNotIn(identities.UNKNOWN, fingerprints)


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.good = hardware.record('worker-a', SPARK, NODE)

    def mutated(self, **changes):
        candidate = json.loads(json.dumps(self.good))
        candidate.update(changes)
        return candidate

    def test_the_honest_record_has_no_problems(self):
        self.assertEqual(hardware.validate(self.good), [])

    def test_a_missing_fact_is_refused_because_dropping_it_would_hide_the_gap(self):
        candidate = self.mutated()
        del candidate['facts']['driver']
        self.assertIn('fact driver is missing (an unreadable fact is written as unknown, never left out)',
                      hardware.validate(candidate))

    def test_an_unknown_fact_may_not_carry_a_value(self):
        candidate = self.mutated()
        candidate['facts']['memory_bandwidth']['value'] = '800 GB/s'
        self.assertIn('fact memory_bandwidth is unknown but carries a value', hardware.validate(candidate))

    def test_a_value_may_not_claim_confidence_without_a_source(self):
        candidate = self.mutated()
        candidate['facts']['gpu']['source'] = None
        self.assertIn('fact gpu has a value and no source', hardware.validate(candidate))

    def test_only_the_five_confidences_exist(self):
        candidate = self.mutated()
        candidate['facts']['gpu']['confidence'] = 'guessed'
        self.assertTrue(any('not one of' in problem for problem in hardware.validate(candidate)))

    def test_the_unknowns_list_may_not_disagree_with_the_facts(self):
        candidate = self.mutated(unknowns=[])
        self.assertIn('unknowns does not list exactly the facts with confidence unknown', hardware.validate(candidate))

    def test_a_record_may_not_claim_an_hw_fp_its_facts_do_not_support(self):
        for candidate in (self.mutated(hw_fp='deadbeefdeadbeef'),
                          self.mutated(hw_fp=identities.hardware_fingerprint(
                              dict.fromkeys(identities.HW_FP_FACTS, 'anything')))):
            self.assertIn('hw_fp does not follow from the facts of this record', hardware.validate(candidate))

    def test_a_record_without_a_host_or_a_time_is_refused(self):
        self.assertIn('host is missing', hardware.validate(self.mutated(host='')))
        self.assertIn("collected_at 'yesterday' is not an ISO timestamp",
                      hardware.validate(self.mutated(collected_at='yesterday')))

    def test_a_fact_that_is_not_a_value_source_confidence_entry_is_refused(self):
        candidate = self.mutated()
        candidate['facts']['gpu'] = 'NVIDIA GB10'
        self.assertTrue(any('not a value/source/confidence entry' in problem for problem in hardware.validate(candidate)))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.where = Path(self.root.name)
        self.addCleanup(self.root.cleanup)
        self.record = hardware.record('worker-a', SPARK, NODE)

    def test_a_honest_record_round_trips(self):
        path = hardware.write(self.where, self.record)
        self.assertEqual(path.name, 'worker-a.json')
        self.assertEqual(hardware.latest(self.where, 'worker-a')['hw_fp'], self.record['hw_fp'])

    def test_a_dishonest_record_is_never_written(self):
        with self.assertRaises(ValueError) as caught:
            hardware.write(self.where, dict(self.record, hw_fp='0' * 16))
        self.assertIn('hw_fp does not follow', str(caught.exception))
        self.assertIsNone(hardware.latest(self.where, 'worker-a'))

    def test_a_corrupt_or_unvalidated_file_reads_as_nothing_rather_than_as_empty(self):
        (self.where).mkdir(parents=True, exist_ok=True)
        (self.where / 'worker-a.json').write_text('{ not json')
        self.assertIsNone(hardware.latest(self.where, 'worker-a'))
        (self.where / 'worker-a.json').write_text(json.dumps(dict(self.record, hw_fp='0' * 16)))
        self.assertIsNone(hardware.latest(self.where, 'worker-a'))

    def test_fingerprint_of_a_host_nobody_inventoried_says_why(self):
        read = hardware.fingerprint(self.where, 'worker-a')
        self.assertEqual(read, {'hw_fp': identities.UNKNOWN, 'reason': 'no inventory record'})

    def test_a_fresh_record_gives_its_hw_fp_with_the_time_it_was_read(self):
        hardware.write(self.where, self.record)
        read = hardware.fingerprint(self.where, 'worker-a')
        self.assertEqual(read['hw_fp'], self.record['hw_fp'])
        self.assertLess(read['age_s'], 60)

    def test_a_stale_record_is_refused_rather_than_passed_off_as_the_machine(self):
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=hardware.MAX_AGE_S + 1)
        hardware.write(self.where, dict(self.record, collected_at=old.isoformat(timespec='seconds'),
                                        unknowns=self.record['unknowns']))
        read = hardware.fingerprint(self.where, 'worker-a')
        self.assertEqual(read['hw_fp'], identities.UNKNOWN)
        self.assertTrue(read['stale'])
        self.assertIn('older than', read['reason'])

    def test_a_record_from_the_future_is_not_believed(self):
        # a clock that jumped forward writes a record nobody read; believing it would pin the
        # host to an hw_fp produced by whatever the facts said at that moment
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3600)
        hardware.write(self.where, dict(self.record, collected_at=future.isoformat(timespec='seconds')))
        read = hardware.fingerprint(self.where, 'worker-a')
        self.assertEqual(read['hw_fp'], identities.UNKNOWN)
        self.assertIn('future', read['reason'])


class HostNameTests(unittest.TestCase):
    def test_a_host_answering_under_its_tailnet_name_is_filed_under_its_inventory_name(self):
        spec = importlib.util.spec_from_file_location('inventory', CODE / 'scripts/inventory.py')
        inventory = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inventory)
        known = inventory.host_names()
        inv = yaml.safe_load((SITE / 'infra/inventory.yml').read_text())['all']
        user = inv['vars']['ansible_user']
        hosts = inv['children']['workers']['hosts']
        cases = []
        for name, facts in hosts.items():
            if facts.get('ansible_connection') == 'local':
                cases.append(('local', name))
            else:
                if facts.get('ansible_host'):
                    cases.append((f'{user}@{facts["ansible_host"]}', name))
                cases.append((f'{user}@{name}', name))
        self.assertTrue(cases)
        for target, name in cases:
            self.assertEqual(inventory.host_name(target, known), name)

    def test_an_unknown_host_keeps_the_bare_name_it_answered_as(self):
        spec = importlib.util.spec_from_file_location('inventory', CODE / 'scripts/inventory.py')
        inventory = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inventory)
        self.assertEqual(inventory.host_name('operator@somewhere.else', {}), 'somewhere')


class GpuListingTests(unittest.TestCase):
    def test_a_message_that_starts_with_gpu_is_not_a_device(self):
        parsed = hardware.parse_gpu('', 'GPU access blocked by the operating system\n', '')
        self.assertIsNone(parsed.get('gpu'))


class UnreachableHostTests(unittest.TestCase):
    """A host that was off is not a host that changed."""

    def run_inventory(self, directory, probes):
        spec = importlib.util.spec_from_file_location('inventory', CODE / 'scripts/inventory.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        record = hardware.record('worker-a', probes)
        with mock.patch.object(module, 'collect', return_value={'name': 'worker-a', 'record': record, 'probes': {}}), \
                mock.patch.object(module, 'host_names', return_value={}), \
                mock.patch.object(sys, 'argv', ['inventory.py', '--hosts', 'worker-a', '--output', str(directory)]), \
                mock.patch('sys.stdout', io.StringIO()) as printed:
            module.main()
        return record, printed.getvalue()

    def test_the_last_good_record_survives_a_host_that_could_not_be_read(self):
        with tempfile.TemporaryDirectory() as directory:
            good = hardware.record('worker-a', SPARK)
            self.assertNotEqual(good['hw_fp'], identities.UNKNOWN)
            hardware.write(directory, good)
            _, printed = self.run_inventory(directory, {})
            self.assertEqual(hardware.latest(directory, 'worker-a')['hw_fp'], good['hw_fp'])
            self.assertIn('keeping the record', printed)

    def test_a_first_record_is_written_even_when_nothing_could_be_read(self):
        with tempfile.TemporaryDirectory() as directory:
            record, _ = self.run_inventory(directory, {})
            self.assertEqual(hardware.latest(directory, 'worker-a')['hw_fp'], record['hw_fp'])


if __name__ == '__main__':
    unittest.main()
