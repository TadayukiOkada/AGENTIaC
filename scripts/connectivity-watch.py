#!/usr/bin/env python3
"""Watch the control -> worker path and say when it has been stable long enough to run something on that host.

Why this exists: an outside outage can drop the control -> worker SSH tunnel and leave the local tunnel port
closed, causing suite attempts to fail against a dead path before anyone stops the run. This watches the declared
path itself, so a decision to resume rests on a measurement instead of a guess.

What it reads, and what it deliberately does not:

  * `GET /ondemand/status` on the control tunnel port for that host, with that host's key and **no** load header. The
    front answers with its real state (identity, idle countdown) and a passive read can never load a model, touch an
    idle timer or bill a request. It is `agentiac.pathwatch.front_answer`, the same read `scripts/ladder.py` uses
    to refuse a mismatched backend and the Suite gate uses before it claims a case. A missing tunnel or a dead backend
    fails at connect, which is the event being watched.
  * `systemctl is-active agentiac-tunnel-<host>` - the unit that owns the forward, so a failing ssh process is
    distinguished from a dead front.

Ports and key names come from the declaration (infra/inventory.yml `tunnel_port`, secrets/gateway.env), not a table.
It does not send an inference request, does not claim, warm, unload or start anything, and writes only under
artifacts/connectivity/. Exit 0 while watching; 1 when --once finds the path down.
"""

import argparse
import calendar
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
REPO = paths.root()
OUT = REPO / 'artifacts/connectivity'
from agentiac import declared, pathwatch  # noqa: E402
from agentiac.secrets import read_env  # noqa: E402

TUNNEL_PORT = {host: port for port, (host, _key) in pathwatch.tunnel_hosts(REPO).items()}
KEY_NAME = {host: key for port, (host, key) in pathwatch.tunnel_hosts(REPO).items()}


def env_keys(path=None):
    """The gateway service's EnvironmentFile as a mapping. Values never leave this function except in a header."""
    try:
        return read_env(path or (REPO / 'secrets/gateway.env'))
    except OSError:
        return {}


def tunnel_unit(host, active='unknown'):
    """`systemctl is-active` for the unit that owns the forward; 'unknown' when systemctl is not available."""
    if shutil.which('systemctl') is None:
        return active
    try:
        return subprocess.run(['systemctl', 'is-active', declared.resource(f'tunnel-{host}')],
                              capture_output=True, text=True, timeout=10).stdout.strip() or 'unknown'
    except Exception:
        return active


def probe(host, port=None, key=None, timeout=10):
    """One passive read of what the front says. Returns the sample; never raises."""
    port = port or TUNNEL_PORT[host]
    started = time.time()
    sample = {'t': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'host': host, 'port': port}
    report, http, error = pathwatch.front_answer(f'http://127.0.0.1:{port}', key, timeout=timeout)
    sample.update(http=http, latency_ms=round((time.time() - started) * 1000), ok=report is not None)
    if report is not None:
        identity = report.get('identity') or {}
        sample['identity'] = {k: identity.get(k) for k in ('backend_id', 'manifest_hash', 'context')}
        sample['front'] = {k: report.get(k) for k in ('state', 'loads', 'unloads', 'idle_for_s', 'last_error')}
    else:
        sample['error'] = error
        sample['identity'], sample['front'] = {}, {}
    sample['tunnel_unit'] = tunnel_unit(host)
    return sample


def stable_seconds(samples, now=None):
    """How long the path has been answering continuously, walked backwards from the newest sample.

    A failed sample ends the streak. So does a gap larger than twice the sampling interval plus 30 s: that stretch is
    the watcher not running, which is not evidence of a stable path.
    """
    ordered = sorted((s for s in samples if s.get('epoch') is not None), key=lambda s: s['epoch'])
    if not ordered or not ordered[-1].get('ok'):
        return 0.0
    since = ordered[-1]['epoch']
    index = len(ordered) - 1
    while index > 0:
        later, earlier = ordered[index], ordered[index - 1]
        if not earlier.get('ok'):
            break
        if later['epoch'] - earlier['epoch'] > (later.get('interval_s') or 0) * 2 + 30:
            break
        since = earlier['epoch']
        index -= 1
    return max(0.0, (now or time.time()) - since)


def read_samples(path, limit=5000):
    if not Path(path).exists():
        return []
    samples = []
    for line in [l for l in Path(path).read_text().splitlines() if l.strip()][-limit:]:
        try:
            sample = json.loads(line)
            # `Z` in the stamp is the truth: calendar.timegm reads it as UTC. time.mktime would read the same
            # string as local time (control runs EST), which puts every sample five hours in the future and makes a
            # stable path look like a negative one.
            sample['epoch'] = calendar.timegm(time.strptime(sample['t'], '%Y-%m-%dT%H:%M:%SZ'))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        samples.append(sample)
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('host', choices=sorted(TUNNEL_PORT))
    parser.add_argument('--interval', type=int, default=30, help='seconds between samples (default 30)')
    parser.add_argument('--stable-for', type=int, default=3600,
                        help='seconds of continuous answers that counts as ready (default 3600)')
    parser.add_argument('--once', action='store_true', help='one sample, exit 1 if the path is down')
    parser.add_argument('--report', action='store_true', help='read the recorded samples and say where the watch stands')
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / f'{args.host}.jsonl'

    if args.report:
        samples = read_samples(log)
        stable = stable_seconds(samples)
        spans, current = [], None
        for sample in samples:
            if not sample['ok'] and current is None:
                current = sample['epoch']
            elif sample['ok'] and current is not None:
                spans.append((current, sample['epoch']))
                current = None
        if current is not None:
            spans.append((current, samples[-1]['epoch']))
        print(f'{args.host}: {len(samples)} samples, {sum(1 for s in samples if not s["ok"])} failed, '
              f'stable {stable / 60:.1f} min ' + ('READY' if stable >= args.stable_for else
                                                  f'(below the {args.stable_for} s bar)'))
        for start, end in spans[-8:]:
            print(f'  down {time.strftime("%H:%MZ", time.gmtime(start))} -> recovered '
                  f'{time.strftime("%H:%MZ", time.gmtime(end))}  ({(end - start) / 60:.1f} min)')
        if samples:
            newest = samples[-1]
            print(f'  newest {newest["t"]} ok={newest["ok"]} http={newest.get("http")} unit={newest.get("tunnel_unit")} '
                  f'declared={newest["identity"].get("backend_id")}')
        return 0

    key = env_keys().get(KEY_NAME[args.host], '')
    while True:
        sample = probe(args.host, key=key)
        sample['interval_s'] = args.interval
        with log.open('a') as handle:
            handle.write(json.dumps(sample, sort_keys=True) + '\n')
        stable = stable_seconds(read_samples(log), now=time.time())
        print(f"{sample['t']} {args.host} {'up' if sample['ok'] else 'DOWN'} http={sample.get('http')} "
              f"unit={sample['tunnel_unit']} stable {stable / 60:.1f} min", flush=True)
        if stable >= args.stable_for:
            print(f'{args.host}: stable for {stable / 60:.0f} min - ready to run there', flush=True)
            (OUT / f'{args.host}.ready').write_text(json.dumps({
                'host': args.host, 'stable_since': sample['t'], 'stable_for_s': args.stable_for,
                'declared_backend': sample['identity'].get('backend_id'),
                'manifest_hash': sample['identity'].get('manifest_hash'),
                'front_state': sample['front'].get('state'), 'at': sample['t']}) + '\n')
            if args.once:
                return 0
        if args.once:
            return 0 if sample['ok'] else 1
        time.sleep(args.interval)


if __name__ == '__main__':
    sys.exit(main())
