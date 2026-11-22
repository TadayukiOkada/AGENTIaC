"""Wi-Fi watch: link parsing, key-free wpa fields, change-only samples, diagnostics on transitions, outage report."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('netwatch', Path(__file__).resolve().parents[1] / 'scripts/netwatch.py')
netwatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(netwatch)

LINK = """Connected to 98:de:d0:25:77:67 (on wlP1p1s0)
\tSSID: OKDFML
\tfreq: 2412.0
\tRX: 26810689 bytes (22089 packets)
\tsignal: -42 dBm
\trx bitrate: 130.0 MBit/s MCS 15
\ttx bitrate: 144.4 MBit/s MCS 15 short GI
"""


def point(t, connected=True, gateway_ok=True, wpa_state='COMPLETED', signal=-42):
    record = {'t': f'2026-10-20T04:{t}-04:00', 'event': 'sample', 'connected': connected, 'gateway_ok': gateway_ok,
              'wpa_state': wpa_state, 'nm_state': 'connected' if connected else 'disconnected'}
    if connected:
        record.update(bssid='98:de:d0:25:77:67', freq=2412, signal_dbm=signal)
    return record


class NetwatchTests(unittest.TestCase):
    def test_link_and_wpa_parsing_keep_no_keys(self):
        self.assertEqual(netwatch.parse_link(LINK), {
            'connected': True, 'bssid': '98:de:d0:25:77:67', 'ssid': 'OKDFML', 'freq': 2412, 'signal_dbm': -42,
            'rx_bitrate': '130.0 MBit/s', 'tx_bitrate': '144.4 MBit/s'})
        self.assertEqual(netwatch.parse_link('Not connected.\n'), {'connected': False})
        wpa = netwatch.parse_wpa('bssid=98:de:d0:25:77:67\nwpa_state=COMPLETED\npassphrase=secret\npsk=abc\nuuid=x\n')
        self.assertEqual(wpa, {'wpa_state': 'COMPLETED', 'bssid': '98:de:d0:25:77:67'})

    def test_samples_on_change_and_diagnostics_on_each_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            log = netwatch.Log(Path(directory) / 'w.jsonl', 2**20)
            samples = [point('11:30'), point('11:40', signal=-50), point('11:52', False, False, 'ASSOCIATING'),
                       point('12:02', False, False, 'ASSOCIATING'), point('12:12', False, False, 'DISCONNECTED'),
                       point('22:59')]
            netwatch.watch(log, 'wl0', 10, 3600, 3600, samples=samples,
                           snapshot=lambda interface, reason: {'event': 'diag', 'reason': reason})
            events = [json.loads(line) for line in log.path.read_text().splitlines()]
        self.assertEqual([e['event'] if e['event'] != 'sample' else e['t'][14:19] for e in events],
                         ['start', '11:30', '11:52', 'diag', '12:12', '22:59', 'diag'])
        self.assertEqual([e['reason'] for e in events if e['event'] == 'diag'], ['degraded', 'recovered'])

    def test_a_lasting_outage_is_reconnected_with_backoff_and_attempts_reset_after_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            log = netwatch.Log(Path(directory) / 'w.jsonl', 2**20)
            down = point('12:00', False, False, 'DISCONNECTED')
            times = [0, 10, 60, 130, 200, 320, 330, 340, 500, 640]
            samples = [point('11:30'), down, down, down, down, down, down, point('14:00'), down, down]
            ticks = iter(times)
            netwatch.watch(log, 'wl0', 10, 3600, 3600, recover_after=120, recover_every=180, samples=samples,
                           snapshot=lambda interface, reason: {'event': 'diag'},
                           recover=lambda interface, attempt: {'event': 'recover', 'attempt': attempt},
                           clock=lambda: next(ticks))
            events = [json.loads(line) for line in log.path.read_text().splitlines()]
        # down from t=10: first attempt at 130 (>=120), next only at 320 (>=180 later); back at 340;
        # down again from 500: the count restarts, first attempt at 640.
        self.assertEqual([e['attempt'] for e in events if e['event'] == 'recover'], [1, 2, 1])

    def test_report_lists_outages_with_the_link_before(self):
        entries = [{'event': 'start'}, point('11:30'), point('11:52', False, False, 'ASSOCIATING'),
                   {'event': 'diag'}, point('12:12', False, False, 'DISCONNECTED'), {'event': 'recover'}, point('22:59'),
                   point('30:00', True, False)]
        found = netwatch.outages(entries)
        self.assertEqual(found[0], {'start': '2026-10-20T04:11:52-04:00', 'end': '2026-10-20T04:22:59-04:00',
                                    'before': {'bssid': '98:de:d0:25:77:67', 'freq': 2412, 'signal_dbm': -42,
                                               'tx_bitrate': None},
                                    'wpa_states': ['ASSOCIATING', 'DISCONNECTED'], 'reconnects': 1, 'seconds': 667})
        self.assertEqual((found[1]['end'], len(found)), (None, 2))

    def test_driver_state_reads_only_a_realtek_style_procfs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'other/wl0').mkdir(parents=True)
            (root / 'other/wl0/stats').write_text('x')
            self.assertEqual(netwatch.driver_state('wl0', root), {})
            driver = root / 'rtl88x2ce/wl0'
            driver.mkdir(parents=True)
            (driver / 'ps_info').write_text('pwr_mode=1\n')
            (driver / 'rx_signal').write_text('rssi:-42\n')
            self.assertEqual(netwatch.driver_state('wl0', root), {'ps_info': 'pwr_mode=1\n', 'rx_signal': 'rssi:-42\n'})

    def test_log_rotates_and_report_reads_both_files(self):
        with tempfile.TemporaryDirectory() as directory:
            log = netwatch.Log(Path(directory) / 'w.jsonl', 250)  # two records, then rotate
            for minute in ('10:00', '10:10', '10:20'):
                log.write(point(minute))
            self.assertTrue((Path(directory) / 'w.jsonl.1').exists())
            self.assertEqual(len(list(netwatch.records(log.path))), 3)


if __name__ == '__main__':
    unittest.main()
