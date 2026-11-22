#!/usr/bin/env python3
"""Wi-Fi watch for a worker: JSON lines of link state, a diagnostic snapshot on every change.

run     samples every --interval seconds: the Wi-Fi link (BSSID, frequency,
        signal, bitrates), wpa_supplicant and NetworkManager state, and a ping
        to the default gateway. A sample is written when that state changes,
        otherwise once per --heartbeat seconds. Entering or leaving a degraded
        state (no link or no gateway answer), and every --diag-every seconds
        while degraded, also writes a `diag` record: station dump, Realtek
        driver state (power save, firmware, signal), wpa_cli status,
        addresses, routes and the recent kernel, NetworkManager and
        wpa_supplicant journal. The log rotates at --max-mb (one old file kept).
        No keys: only allowlisted wpa_cli fields are kept.
        With --recover-after (default 120 s; 0 = off), a link degraded that
        long is reconnected through NetworkManager, again every
        --recover-every seconds while it stays degraded; every second attempt
        first switches the Wi-Fi radio off and on. Each attempt is logged as a
        `recover` record. NetworkManager otherwise gives up after a few
        failures (e.g. `no-secrets`) and needs a reboot.
report  lists the outages in the log: start, end, duration, the link before,
        the wpa_supplicant states and the reconnect attempts during the outage.

Standard library only; runs as root (wpa_cli, kernel log, nmcli) from
agentiac-netwatch.service (infra/netwatch.yml).
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time

WPA_FIELDS = ('wpa_state', 'bssid', 'freq', 'ssid', 'key_mgmt', 'wifi_generation')
DRIVER_FILES = ('fwstate', 'ps_info', 'rx_signal', 'ap_info', 'sta_tp_info', 'trx_info')
STATE = ('connected', 'bssid', 'freq', 'wpa_state', 'nm_state', 'gateway_ok')


def run(argv, timeout=10):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


def now():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec='seconds')


def wifi_interface():
    names = sorted(p.name for p in Path('/sys/class/net').glob('wl*'))
    return names[0] if names else None


def parse_link(text):
    """`iw dev <if> link` -> connected, bssid, ssid, freq, signal and bitrates."""
    match = re.match(r'Connected to ([0-9a-f:]{17})', text.strip())
    if not match:
        return {'connected': False}
    link = {'connected': True, 'bssid': match.group(1)}
    for line in text.splitlines()[1:]:
        key, _, value = line.strip().partition(': ')
        if key == 'SSID':
            link['ssid'] = value
        elif key == 'freq':
            link['freq'] = int(float(value))
        elif key == 'signal':
            link['signal_dbm'] = int(value.split()[0])
        elif key in ('rx bitrate', 'tx bitrate'):
            link[key.replace(' ', '_')] = value.split(' MBit/s')[0] + ' MBit/s'
    return link


def parse_wpa(text):
    """Allowlisted `wpa_cli status` fields only (never a passphrase or key)."""
    fields = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    return {key: fields[key] for key in WPA_FIELDS if key in fields}


def gateway(interface):
    match = re.search(r'default via (\S+)', run(['ip', 'route', 'show', 'default', 'dev', interface]))
    return match.group(1) if match else None


def sample(interface):
    record = {'t': now(), 'event': 'sample', 'iface': interface,
              **parse_link(run(['iw', 'dev', interface, 'link']))}
    record['wpa_state'] = parse_wpa(run(['wpa_cli', '-i', interface, 'status'])).get('wpa_state')
    record['nm_state'] = run(['nmcli', '-t', '-f', 'STATE', 'general']).strip() or None
    record['gateway'] = gateway(interface)
    record['gateway_ok'] = bool(record['gateway']) and subprocess.run(
        ['ping', '-c', '1', '-W', '2', record['gateway']], capture_output=True).returncode == 0
    return record


def degraded(record):
    return not (record.get('connected') and record.get('gateway_ok'))


def diag(interface, reason):
    """Snapshot for the moment of a change; each part truncated."""
    parts = {
        'station': ['iw', 'dev', interface, 'station', 'dump'],
        'addresses': ['ip', '-br', 'addr'],
        'routes': ['ip', 'route'],
        'wireless': ['cat', '/proc/net/wireless'],
        'kernel': ['journalctl', '-k', '--since', '-3min', '-n', '60', '--no-pager', '-o', 'short-iso'],
        'network': ['journalctl', '-u', 'NetworkManager', '-u', 'wpa_supplicant', '--since', '-3min',
                    '-n', '80', '--no-pager', '-o', 'short-iso'],
    }
    record = {'t': now(), 'event': 'diag', 'reason': reason, 'iface': interface,
              'wpa': parse_wpa(run(['wpa_cli', '-i', interface, 'status']))}
    record.update({name: run(argv)[-4000:] for name, argv in parts.items()})
    record['driver'] = driver_state(interface)
    return record


def driver_state(interface, root=Path('/proc/net')):
    """Realtek vendor drivers (rtl88x2ce on the Orins) report power save, firmware and
    signal state in procfs, where `iw station dump` stays empty."""
    for directory in sorted(root.glob(f'*/{interface}')):
        if (directory / 'ps_info').exists():
            return {name: run(['cat', str(directory / name)], timeout=3)[-1500:]
                    for name in DRIVER_FILES if (directory / name).exists()}
    return {}


class Log:
    def __init__(self, path, max_bytes):
        self.path, self.max_bytes = Path(path), max_bytes

    def write(self, record):
        if self.path.exists() and self.path.stat().st_size > self.max_bytes:
            self.path.replace(self.path.with_name(self.path.name + '.1'))
        with self.path.open('a') as handle:
            handle.write(json.dumps(record) + '\n')


def reconnect(interface, attempt):
    """Odd attempts reconnect through NetworkManager; even ones cycle the radio first."""
    steps = [] if attempt % 2 else [['nmcli', 'radio', 'wifi', 'off'], ['sleep', '5'], ['nmcli', 'radio', 'wifi', 'on'],
                                    ['sleep', '10']]
    steps.append(['nmcli', 'device', 'connect', interface])
    record = {'t': now(), 'event': 'recover', 'attempt': attempt,
              'action': 'reconnect' if attempt % 2 else 'radio off/on + reconnect'}
    for argv in steps:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=90)
            output = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', done.stdout + done.stderr)  # nmcli progress escapes
            record['returncode'], record['output'] = done.returncode, output.strip()[-500:]
        except (OSError, subprocess.TimeoutExpired) as error:
            record['returncode'], record['output'] = None, type(error).__name__
    return record


def watch(log, interface, interval, heartbeat, diag_every, recover_after=0, recover_every=180,
          samples=None, snapshot=diag, recover=reconnect, clock=time.monotonic):
    """Write samples on change or heartbeat, diagnostics on degraded transitions and reconnect
    a link degraded for recover_after seconds; tests pass samples, snapshot, recover and clock."""
    previous, last_written, last_diag = None, 0.0, 0.0
    degraded_since, last_recover, attempts = None, float('-inf'), 0
    log.write({'t': now(), 'event': 'start', 'iface': interface, 'interval_s': interval, 'recover_after_s': recover_after})
    for record in samples if samples is not None else iter(lambda: sample(interface), None):
        clock_now = clock()
        changed = previous is None or any(record.get(k) != previous.get(k) for k in STATE)
        if changed or clock_now - last_written >= heartbeat:
            log.write(record)
            last_written = clock_now
        was = previous is not None and degraded(previous)
        if degraded(record) != was and previous is not None:
            log.write(snapshot(interface, 'degraded' if degraded(record) else 'recovered'))
            last_diag = clock_now
        elif degraded(record) and clock_now - last_diag >= diag_every:
            log.write(snapshot(interface, 'still degraded' if was else 'degraded at start'))
            last_diag = clock_now
        if not degraded(record):
            degraded_since, last_recover, attempts = None, float('-inf'), 0
        else:
            degraded_since = clock_now if degraded_since is None else degraded_since
            if (recover_after and clock_now - degraded_since >= recover_after
                    and clock_now - last_recover >= recover_every):
                attempts += 1
                log.write(recover(interface, attempts))
                last_recover = clock_now
        previous = record
        if samples is None:
            time.sleep(interval)


def records(path):
    for each in (Path(str(path) + '.1'), Path(path)):
        if each.exists():
            for line in each.read_text().splitlines():
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def outages(entries):
    """Degraded periods from samples: start, end (None if ongoing), link before, wpa states, reconnects."""
    found, current, before = [], None, None
    for record in entries:
        if record.get('event') == 'recover' and current is not None:
            current['reconnects'] += 1
        if record.get('event') != 'sample':
            continue
        if degraded(record):
            if current is None:
                current = {'start': record['t'], 'end': None, 'before': before, 'wpa_states': [], 'reconnects': 0}
                found.append(current)
            if record.get('wpa_state') and record['wpa_state'] not in current['wpa_states']:
                current['wpa_states'].append(record['wpa_state'])
        else:
            if current is not None:
                current['end'] = record['t']
                current = None
            before = {k: record.get(k) for k in ('bssid', 'freq', 'signal_dbm', 'tx_bitrate')}
    for each in found:
        if each['end']:
            seconds = (datetime.datetime.fromisoformat(each['end']) - datetime.datetime.fromisoformat(each['start']))
            each['seconds'] = round(seconds.total_seconds())
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('run', 'report'))
    parser.add_argument('--log', default='/var/log/agent-infra/netwatch.jsonl')
    parser.add_argument('--iface', help='default: the first wl* interface')
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--heartbeat', type=float, default=60)
    parser.add_argument('--diag-every', type=float, default=300)
    parser.add_argument('--max-mb', type=float, default=20)
    parser.add_argument('--recover-after', type=float, default=120, help='seconds degraded before reconnecting; 0 = never')
    parser.add_argument('--recover-every', type=float, default=180)
    args = parser.parse_args()
    if args.action == 'report':
        for each in outages(records(args.log)):
            print(json.dumps(each))
        return
    interface = args.iface or wifi_interface()
    if not interface:
        raise SystemExit('no Wi-Fi interface')
    os.umask(0o027)  # readable by the log directory's group (adm)
    watch(Log(args.log, int(args.max_mb * 2**20)), interface, args.interval, args.heartbeat, args.diag_every,
          args.recover_after, args.recover_every)


if __name__ == '__main__':
    main()
