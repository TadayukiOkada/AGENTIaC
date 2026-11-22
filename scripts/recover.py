#!/usr/bin/env python3
"""Re-claim the host the route already points at, and never move the route (recovery-only).

The scope is **recovery-only**: nothing here chooses a host, moves the route or touches a gateway.
What is automated is the mechanical half of post-reboot recovery: on the
host the route already names, the very claim `agentiac reclaim` makes, with that target's backend and context.

Each gate that stops it is reported by name rather than assumed:

  route      `ops drift`'s own route line says the tunnel and the front answer, so there is nothing to repair.
  armed      a host is re-claimed automatically only when `infra/inventory.yml` declares `auto_reclaim: true`.
             Borrowed hosts are unarmed by decision: the owner may be working on them, and the same doc that
             tells a person to reclaim one tells them to check first that the owner has not done it.
  credential control holds the key that front answers to (`secrets/nodes/…`): without it the front only looks
             down, and a host this checkout has never claimed is not a host to re-claim. A worktree has no
             `secrets/`, which is what this gate is for.
  pause      `agentiac ops recover pause MIN`, written down in `artifacts/recovery/pause.json`, so a timer
             cannot undo an `ops stop`, a release or hands-on repair.
  cooldown   a reclaim installs a front, restarts a forward and loads a model; it is a repair, not a retry.
  breaker    after `agentiac.recovery.MAX_FAILURES` failed reclaims in a row a person looks at the host.
  lock       another agentiac operation (a claim, a release, a route switch, a deploy) is running: stand down, the next tick looks again.
  host       the host answers, and what is missing has the shape of an accident: a failed front, or a front
             that answers for systemd while the forward to it does not. A front that was stopped cleanly, or
             that is not installed at all, has the shape of a decision (`ops stop`, a release) and is left alone.

Exit 0 when it did nothing or re-claimed, 1 when a reclaim ran and failed, 3 when the decision itself could not
be made. A host that does not answer is a stand-down, not a fault — the same reading `agentiac doctor` uses
for a borrowed machine that is off.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
sys.path.insert(0, str(CODE / 'scripts'))
import borrow  # noqa: E402
import drift  # noqa: E402
import route  # noqa: E402
from agentiac import declared, mutation, recovery  # noqa: E402
from agentiac.errors import command as cli_name  # noqa: E402

FRONT = borrow.PROXY  # the on-demand front unit, on every host this automation may act on


def route_line(hosts):
    """(state, detail) of `ops drift`'s route line: the same two facts (forward, front) this acts on."""
    for state, subject, detail in drift.route_path(hosts):
        if subject == 'route':
            return state, detail
    return 'note', 'no route line was produced'


def front_state(host):
    """LoadState/ActiveState/Result of the host's on-demand front, or None when the host does not answer."""
    try:
        done = borrow.on_host(host, ['systemctl', 'show', FRONT, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'Result'],
                              timeout=25)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 255 or not done.stdout.strip():
        return None
    return dict(line.split('=', 1) for line in done.stdout.splitlines() if '=' in line)


def accident(front, node, tunnel_up):
    """(act, why). The two shapes this repairs, and the two it recognizes as something a person did.

    `infra/stop.yml` says it in its own first line: a stopped host "stays stopped until the next claim". On an
    armed host the front is an *enabled* unit that returns by itself after a reboot, so a front that is stopped
    cleanly, masked or never installed is a decision, and re-claiming it would undo that decision. A front that
    failed, and a front that answers for systemd while the control-side forward to it does not, are accidents.
    """
    load_, active, result = front.get('LoadState'), front.get('ActiveState'), front.get('Result')
    if load_ == 'not-found':
        return False, f'{FRONT} is not installed there: nothing of ours is claiming this host'
    if load_ == 'masked':
        return False, f'{FRONT} is masked, which only a person does'
    if active in ('inactive', 'deactivating') and result in ('success', 'done', ''):
        return False, f'{FRONT} is stopped cleanly, which is what ops stop leaves behind'
    shape = f'{FRONT} is {load_}/{active}/{result}'
    if not tunnel_up:
        return True, f'{shape} and the forward to {node} is down'
    return True, f'{shape} while its API does not answer'


def askable(node, host):
    """Can control even read this front's answer? `borrow.endpoint` needs the key this checkout holds for it.

    `ops drift` reports the same fact as "no key: never claimed" (its front_answers catches the OSError).
    An unreadable front and a dead one look identical in that line, and only one of them is a repair.
    """
    try:
        borrow.endpoint(node, host)
    except OSError:
        return False
    return True


def decide(hosts, root, at=None):
    """Every gate in the order a reader would ask them, then what follows from them. Writes nothing."""
    at = at or recovery.now()
    record = recovery.load(root)
    target = route.current()
    spec = json.loads(route.ROUTES.read_text())['targets'][target]
    node, host = spec['node'], hosts[spec['node']]
    out = {'target': target, 'node': node, 'decision': 'stand_down', 'gate': None, 'reason': '', 'gates': []}

    def stand_down(gate, why):
        out['gate'], out['reason'] = gate, why
        out['gates'].append({'gate': gate, 'ok': False, 'evidence': why})
        return out

    def passed(gate, evidence):
        out['gates'].append({'gate': gate, 'ok': True, 'evidence': evidence})

    state, detail = route_line(hosts)
    out['route'] = detail
    if state == 'ok':
        out['decision'] = 'idle'
        out['reason'] = detail
        out['gates'].append({'gate': 'route', 'ok': True, 'evidence': detail})
        return out
    if state != 'drift':
        return stand_down('route', f'{detail}, which is not a route this automation can re-claim')
    passed('route', detail)

    if not host.get('auto_reclaim'):
        return stand_down('armed', f'{node} declares no auto_reclaim in infra/inventory.yml; only a host the '
                                   "owner arms is re-claimed automatically, and a borrowed host needs the owner's "
                                   f'word first: {cli_name()} reclaim')
    passed('armed', 'auto_reclaim: true')

    if not askable(node, host):
        return stand_down('credential', f'control holds no key for {node} under secrets/nodes/, so this checkout '
                                        f'has never claimed it: "the front does not answer" is what an unreadable '
                                        f'front looks like, not a dead one')
    passed('credential', f'control holds the key for {node}')

    stopped = recovery.paused(root, at)
    if stopped:
        return stand_down('pause', f"paused until {stopped['until']} "
                                   f"({stopped['reason'] or 'no reason given'}): {cli_name()} ops recover resume")
    passed('pause', 'not paused')

    left = recovery.cooldown_left(record, at)
    if left:
        return stand_down('cooldown', f'a reclaim was attempted {recovery.COOLDOWN_S - left:.0f} s ago; the next is '
                                      f'not attempted for another {left:.0f} s')
    passed('cooldown', f'no reclaim attempted in the last {recovery.COOLDOWN_S // 60} min')

    if recovery.breaker_open(record):
        return stand_down('breaker', f'{recovery.failures(record)} reclaims in a row failed'
                                     f'{"" if record.get("node") == node else " on " + str(record.get("node"))}; '
                                     'a person looks at the host first')
    passed('breaker', f'no more than {recovery.MAX_FAILURES - 1} failed reclaim in a row')

    front = front_state(host)
    out['front'] = front
    if front is None:
        out['decision'] = 'unreachable'
        return stand_down('host', f'{node} did not answer over ssh, so nothing was attempted there')
    should, why = accident(front, node, drift.tunnel_active(node))
    if not should:
        return stand_down('host', why)
    passed('host', why)
    out['decision'] = 'reclaim'
    out['reason'] = why
    return out


def act(hosts):
    """`agentiac reclaim` itself — borrow.reclaim reads the route and claims that host with its own backend."""
    started = time.monotonic()
    try:
        borrow.reclaim(hosts)
    except (SystemExit, OSError, ValueError, subprocess.SubprocessError) as error:
        return False, str(error).strip()[:400] or type(error).__name__, time.monotonic() - started
    return True, '', time.monotonic() - started


def run(root, check_only=False, at=None):
    """Decide, act when every gate passes, and record both. `--check` writes nothing. Returns (record, code)."""
    hosts = borrow.hosts()
    out = decide(hosts, root, at)
    previous = recovery.load(root) or {}
    record = {k: out.get(k) for k in ('target', 'node', 'gate', 'reason', 'gates', 'route', 'front')}
    record['consecutive_failures'] = recovery.failures(previous)
    record['last_action_at'] = previous.get('last_action_at')
    if check_only:
        record['decision'] = 'reclaim' if out['decision'] == 'reclaim' else out['decision']
        record['reason'] = ('checked only, nothing acted: ' if record['decision'] == 'reclaim' else '') + out['reason']
        return record, 0
    if out['decision'] == 'idle':
        record['decision'] = 'idle'
        if record['consecutive_failures']:      # the fault is gone: whoever fixed it, the counter stops applying
            record['consecutive_failures'] = 0
            record['reason'] += '; the failure counter is cleared'
        return recovery.save(root, record), 0
    if out['decision'] != 'reclaim':
        record['decision'] = 'unreachable' if out['decision'] == 'unreachable' else 'stand_down'
        return recovery.save(root, record), 0

    try:
        with mutation.hold(root, 'recover reclaim'):
            ok, detail, seconds = act(hosts)
    except mutation.Busy as busy:
        record['decision'] = 'stand_down'
        record['gate'], record['reason'] = 'lock', str(busy)
        return recovery.save(root, record), 0
    after, verifying = route_line(borrow.hosts())
    record['action'] = {'attempted': True, 'ok': ok, 'seconds': round(seconds), 'verified': after == 'ok',
                        'detail': detail or (None if after == 'ok' else f'the claim ran; the route still '
                                                                        f'is not up: {verifying}')}
    record['last_action_at'] = recovery.stamp()
    record['decision'] = 'reclaimed' if ok and after == 'ok' else 'reclaim_failed'
    record['consecutive_failures'] = 0 if record['decision'] == 'reclaimed' else record['consecutive_failures'] + 1
    return recovery.save(root, record), 0 if record['decision'] == 'reclaimed' else 1


def screen(record):
    """The gates in order, then what follows from them."""
    lines = [f"{'gate ' + item['gate']:22}{'ok' if item['ok'] else 'stops'}  {item['evidence']}" for item in record['gates']]
    lines.append(f"{'decision':22}{record['decision']}")
    if record.get('action'):
        action = record['action']
        lines.append(f"{'reclaim':22}{action['seconds']} s, "
                     + ('the route answers' if action['verified'] else action['detail']))
    if record.get('consecutive_failures') and record['decision'] != 'idle':
        lines.append(f"{'failures':22}{record['consecutive_failures']} in a row")
    return '\n'.join(lines)


def status(root, at=None):
    record = recovery.load(root)
    stopped = recovery.paused(root, at)
    if record is None and stopped is None:
        return f'no automatic reclaim has run here yet, and none is paused; systemctl status {declared.resource("recovery.timer", ROOT)}'
    parts = []
    if record:
        parts.append(f"{record.get('at', '?')}  {recovery.line(record, at)}")
    if stopped:
        parts.append(f"paused until {stopped['until']} ({stopped['reason'] or 'no reason given'})")
    if recovery.breaker_open(record):
        parts.append(f"breaker open after {recovery.failures(record)} failed reclaims; "
                     f'{cli_name()} ops recover reset clears it after a person has looked')
    return '\n'.join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('run', 'status', 'pause', 'resume', 'reset'),
                        help='run: decide, and re-claim the route host when every gate passes (what the timer runs); '
                             'status: what the last run decided; pause/resume: stand down for a while; '
                             'reset: clear the failure counter after a person has looked at the host')
    parser.add_argument('minutes', type=float, nargs='?', help='pause: minutes to stand down')
    parser.add_argument('--reason', default='', help='pause: what is being protected from the timer (ops stop, a '
                                                      'release, hands-on repair)')
    parser.add_argument('--check', action='store_true', help='run: report the decision, act nothing and write nothing')
    parser.add_argument('--json', action='store_true', help='one JSON document instead of the lines')
    args = parser.parse_args(argv)

    if args.action == 'status':
        record = recovery.load(ROOT)
        print(json.dumps({'record': record, 'paused': recovery.paused(ROOT), 'armed': [
            name for name, host in borrow.hosts().items() if host.get('auto_reclaim')]}, sort_keys=True)
              if args.json else status(ROOT))
        return 0
    if args.action == 'pause':
        if not args.minutes or args.minutes <= 0:
            parser.error('pause needs a positive number of minutes')
        stopped = recovery.pause(ROOT, args.minutes, args.reason)
        record = recovery.save(ROOT, {'decision': 'paused', 'reason': stopped['reason'] or 'no reason given',
                                      'node': None, 'target': None, 'gate': 'pause', 'gates': [],
                                      'consecutive_failures': recovery.failures(recovery.load(ROOT)),
                                      'last_action_at': (recovery.load(ROOT) or {}).get('last_action_at'),
                                      'paused_until': stopped['until']})
        print(json.dumps(record, sort_keys=True) if args.json else f"paused until {stopped['until']}")
        return 0
    if args.action == 'resume':
        recovery.resume(ROOT)
        print('resumed' if not args.json else json.dumps({'resumed': True}, sort_keys=True))
        return 0
    if args.action == 'reset':
        record = recovery.save(ROOT, {'decision': 'reset', 'node': None, 'target': None, 'gate': 'breaker',
                                      'reason': 'a person looked at the host', 'gates': [], 'consecutive_failures': 0,
                                      'last_action_at': (recovery.load(ROOT) or {}).get('last_action_at')})
        print(json.dumps(record, sort_keys=True) if args.json else 'the failure counter is cleared')
        return 0

    try:
        record, code = run(ROOT, check_only=args.check)
    except (OSError, ValueError, LookupError, subprocess.SubprocessError) as error:
        print(f'the decision could not be made: {type(error).__name__}: {error}', file=sys.stderr)
        return 3
    print(json.dumps(record, sort_keys=True) if args.json else screen(record))
    return code


if __name__ == '__main__':
    sys.exit(main())
