"""`ops memory`: the stored reading of one host, refused when it is too old to mean anything."""
import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import telemetry  # noqa: E402

NOW = datetime.datetime(2026, 11, 1, 16, 40, tzinfo=datetime.timezone.utc)


def row(seconds_ago, gib):
    time = NOW - datetime.timedelta(seconds=seconds_ago)
    return json.dumps({'time': time.isoformat(), 'mem_kib': {'MemAvailable': int(gib * 2**20)}})


class Report(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def report(self):
        return telemetry.report(self.folder, 'worker-a', 10, 300, NOW)

    def test_a_recent_reading_above_the_floor_is_fine(self):
        (self.folder / 'worker-a-20261101T163205.jsonl').write_text(row(60, 25.8) + '\n')
        code, line = self.report()
        self.assertEqual(code, telemetry.FINE)
        self.assertIn('25.8 GiB', line)

    def test_a_recent_reading_below_the_floor_is_a_problem(self):
        (self.folder / 'worker-a-20261101T163205.jsonl').write_text(row(60, 9.9) + '\n')
        self.assertEqual(self.report()[0], telemetry.BELOW_FLOOR)

    def test_exactly_the_floor_is_fine(self):
        (self.folder / 'worker-a-20261101T163205.jsonl').write_text(row(60, 10) + '\n')
        self.assertEqual(self.report()[0], telemetry.FINE)

    def test_an_old_reading_says_nothing_even_when_it_looks_healthy(self):
        (self.folder / 'worker-a-20261101T135411.jsonl').write_text(row(9000, 31.0) + '\n')
        code, line = self.report()
        self.assertEqual(code, telemetry.UNDETERMINED)
        self.assertNotIn('31.0', line)

    def test_no_file_is_undetermined_not_a_pass(self):
        self.assertEqual(self.report()[0], telemetry.UNDETERMINED)

    def test_another_hosts_file_is_never_read(self):
        (self.folder / 'worker-b-20261101T163205.jsonl').write_text(row(60, 25.8) + '\n')
        self.assertEqual(self.report()[0], telemetry.UNDETERMINED)

    def test_the_newest_file_wins(self):
        (self.folder / 'worker-a-20261101T135411.jsonl').write_text(row(9000, 31.0) + '\n')
        (self.folder / 'worker-a-20261101T163205.jsonl').write_text(row(60, 9.0) + '\n')
        self.assertEqual(self.report()[0], telemetry.BELOW_FLOOR)

    def test_the_last_line_of_a_file_wins(self):
        (self.folder / 'worker-a-20261101T163205.jsonl').write_text(row(120, 30) + '\n' + row(60, 5) + '\n')
        self.assertEqual(self.report()[0], telemetry.BELOW_FLOOR)

    def test_a_sample_whose_probe_failed_falls_back_to_the_reading_before_it(self):
        failed = json.dumps({'time': NOW.isoformat(), 'probe_error': 'TimeoutError'})
        (self.folder / 'worker-a-20261101T163205.jsonl').write_text(row(60, 25) + '\n' + failed + '\n')
        self.assertEqual(self.report()[0], telemetry.FINE)

    def test_a_half_written_line_does_not_crash(self):
        (self.folder / 'worker-a-20261101T163205.jsonl').write_text(row(60, 25) + '\n{"time": "2026')
        self.assertEqual(self.report()[0], telemetry.FINE)


if __name__ == '__main__':
    unittest.main()
