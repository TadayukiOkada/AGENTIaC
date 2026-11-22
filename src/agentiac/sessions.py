"""T11.1: the inventory of routine work — pi's sessions counted, with no body in the count.

L3 wants the routine work found before anyone writes it down as a Skill (T11.3), and the only place
that inventory exists is pi's own session files. Every one of them is a full transcript: the user's
words, the model's thinking, the commands it ran and everything that came back. So this module reads
all of it and prints a number, and the completion condition is the rule that makes that safe —
本文を保存せず件数だけ集計, top ten and a baseline.

Three things keep the transcript in the file.

**Nothing that arrives from a message is printed.** The only strings this screen can show are tool
names, program names and subcommands, and each is checked against a list declared here: a tool name
that is not a name, a program that is not one of ours, a subcommand nobody defined all become
`unnamed` or `other`, counted in aggregate and never quoted. A sentinel placed in a prompt, a thinking
block, a bash command line, a tool result and an assistant reply fails the run if any of it reaches
the screen.

**The top ten is a top ten of categories, not of strings.** `bash × 162` and `git commit × 12` say
what the Agent does repeatedly, which is what T11.3 needs to decide whether a Skill is worth writing;
the word after `git commit` is the commit message and is nobody's inventory.

**A baseline says what it is a baseline of.** T11.5 compares before and after a Skill on tokens and
elapsed time, and a number without its denominator and its corpus is not comparable with anything —
so every rate here names the thing it is divided by, and a session that never asked a question yields
`None` rather than a zero.

The working directory is reported and is not body: it is the same kind of metadata the router log's
`repo` key already is (T13.2), and "which repository the routine work happens in" is half the answer.
"""
from __future__ import annotations

import collections
import datetime
import json
from pathlib import Path
import re

from agentiac import paths

ROOT = paths.root()

# pi's own state directory, whose `sessions/` holds the transcripts. The writer is `scripts/agent.py`
# (`STATE`), which exports it as PI_CODING_AGENT_DIR; tests/test_sessions.py compares the two texts.
PI_STATE = 'artifacts/agents/pi-daily'
SESSIONS = f'{PI_STATE}/sessions'

# The programs whose *name* is worth reporting. Anything else is counted as `other`, because the first
# word of a shell line is usually a program and sometimes a path, and a path is not a category.
PROGRAMS = ('agentiac', 'ansible', 'ansible-playbook', 'awk', 'base64', 'basename', 'bash',
            'break', 'cat', 'cd', 'chmod', 'column', 'comm', 'cp', 'curl', 'cut', 'date', 'dd',
            'df', 'diff', 'dirname', 'dmesg', 'docker', 'du', 'echo', 'exit', 'export', 'false',
            'fg', 'file', 'find', 'free', 'gh', 'git', 'grep', 'gzip', 'head', 'hostname',
            'htop', 'jobs', 'journalctl', 'jq', 'kill', 'ln', 'ls', 'lsblk', 'lscpu', 'lsof',
            'make', 'md5sum', 'mkdir', 'mktemp', 'mv', 'nc', 'npm', 'nproc', 'npx', 'nsys',
            'nvcc', 'nvidia-smi', 'paste', 'patch', 'ping', 'pip', 'pip3', 'podman', 'printf',
            'ps', 'pytest', 'python', 'python3', 'qairt-converter', 'qairt-dlc-info', 'read',
            'realpath', 'rg', 'rm', 'rmdir', 'rsync', 'ruff', 'scp', 'sed', 'seq', 'set', 'sh',
            'sha256sum', 'sleep', 'sort', 'source', 'sqlite3', 'ss', 'ssh', 'stat', 'strace',
            'systemctl', 'systemd-run', 'tail', 'tar', 'tee', 'test', 'top', 'touch', 'tr',
            'tree', 'true', 'uname', 'uniq', 'unset', 'uv', 'wait', 'watch', 'wc', 'wget',
            'which', 'whoami', 'xargs', 'yamllint', 'yes')

# pi's built-in tools. Only these names are printed: a model can invent a tool, and an invented name is
# text that arrived in a message, however much it looks like a word.
TOOLS = ('bash', 'edit', 'find', 'grep', 'ls', 'read', 'write')

# Words that run another program, and so are not the thing the Agent was doing. Shell control words are
# here too: `do git status` is `git status`, and a bare `done` is nothing at all.
WRAPPERS = ('sudo', 'timeout', 'time', 'env', 'nohup', 'command', 'nice', 'stdbuf', 'doas',
            'if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'until', 'do', 'done', 'case', 'esac',
            'select', 'in', 'function', '!', '{', '}', '(', ')')
# Subcommands are reported for the two programs where the subcommand *is* the routine work. Both lists
# are closed, so no word the Agent typed can extend what this screen is able to say. `agentiac`'s
# list is compared with the real parser by tests/test_sessions.py, the way T13.2 compares the gateway fields.
GIT_SUBCOMMANDS = ('add', 'apply', 'blame', 'branch', 'checkout', 'cherry-pick', 'clean', 'commit',
                   'config', 'create', 'describe', 'diff', 'fetch', 'format-patch', 'init', 'list',
                   'log', 'ls-files', 'merge', 'mv', 'prune', 'push', 'rebase', 'remote', 'reset',
                   'restore', 'rev-parse', 'rm', 'show', 'stash', 'status', 'switch', 'tag', 'worktree')
AGENTIAC_SUBCOMMANDS = ('agent', 'chatgpt', 'check', 'claim', 'code', 'completion', 'dashboard', 'doctor', 'eval',
                           'evidence', 'log-task', 'model', 'models', 'observe', 'ops', 'reclaim',
                           'release', 'route', 'sessions', 'status', 'unload', 'warm', 'worktrees')
SUBCOMMANDS = {'git': GIT_SUBCOMMANDS, 'agentiac': AGENTIAC_SUBCOMMANDS}

NAME = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')             # a tool name, and nothing else
ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')       # `FOO=bar python …`: not the program
DURATION = re.compile(r'^\d+(\.\d+)?[smhd]?$')              # `timeout 30s git status`, `nice 5 …`
# A wrapper's options that take the next word as their value: `sudo -u root git status`, `nice -n 5 make`.
VALUE_OPTIONS = {'sudo': ('-u', '-g'), 'doas': ('-u',), 'env': ('-u',), 'nice': ('-n',), 'timeout': ('-k', '-s')}
HEREDOC = re.compile(r'<<-?\s*[\'"]?([A-Za-z_][A-Za-z0-9_]*)[\'"]?')


# --- naming, which is the whole leak surface -------------------------------------------------------------

def tool_name(name):
    """A tool name is a category from a declared list; anything else becomes `unnamed`."""
    text = str(name or '')
    return text if text in TOOLS else 'unnamed'


def _skippable(word):
    """Options, durations and assignments come before the program a wrapper is about to run."""
    return (word.startswith('-') or bool(DURATION.match(word)) or bool(ASSIGNMENT.match(word))
            or Path(word).name.lower() in WRAPPERS)


def _segments(command):
    """The statements of one bash call, split where the shell would split it — and no further.

    Two rules here are safety rules and not accuracy rules.

    **The split is quote-aware.** `grep -E "ok|failed"` is one command; splitting on the `|` inside the
    quotes would count `failed"` as a program, which is a fragment of a search pattern wearing a
    program's name.

    **A heredoc body is skipped.** The twenty lines after `python - <<'PY'` are the body of one python
    call, not twenty programs, and reading their first words is reading what the Agent typed rather
    than what it ran. Scanning resumes after the closing delimiter, or stops if there is none. `<<<`
    is a here-string on one line, not a heredoc, and a `#` comment runs to the end of its line.
    """
    text = str(command or '')
    segments, current, quote, index = [], [], '', 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == '\\' and quote == '"':        # only double quotes have escapes
                index += 2
                continue
            if char == quote:
                quote = ''
            index += 1
            continue
        if char in '"\'':
            quote = char
            index += 1
            continue
        if char == '\\':
            index += 2
            continue
        pair = text[index:index + 2]
        if char == '#' and (not current or current[-1].isspace()):
            newline = text.find('\n', index)
            index = newline if newline != -1 else len(text)
            continue
        if text[index:index + 3] == '<<<':
            current.append(' ')
            index += 3
            continue
        if pair == '<<':
            segments.append(''.join(current))
            current = []
            opened = HEREDOC.match(text, index)
            closing = re.compile(rf'^[ \t]*{opened.group(1)}[ \t]*$', re.M).search(text, index) if opened else None
            if closing is None:
                break                                    # no way to find its end: the rest is a body
            index = closing.end()
            continue
        if pair in ('&&', '||'):
            segments.append(''.join(current))
            current = []
            index += 2
            continue
        lone_ampersand = char == '&' and pair != '&>' and text[index - 1:index] not in ('>', '<')
        if char in ';\n' or (char == '|' and pair != '||') or lone_ampersand:
            segments.append(''.join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    else:
        segments.append(''.join(current))
    return [segment.strip() for segment in segments if segment.strip()]


def _first_program(words):
    """The word of one segment that names the program, or `''` when no word does.

    `for f in *.jsonl` runs what comes after the `in`, not `f`; a segment that starts with `#` is a
    comment and runs nothing. Both are handled here rather than by widening `PROGRAMS`, because the
    alternative is a list that grows every time the Agent writes a loop.
    """
    index = 0
    while index < len(words):
        word = words[index]
        if word.startswith('#'):
            return '', index
        if Path(word).name.lower() in ('for', 'select'):
            index += 1
            while index < len(words) and words[index] != 'in':
                index += 1
            return '', index + 1                       # the list follows the `in`; the body is the next segment
        if _skippable(word):
            takes_value = word in VALUE_OPTIONS.get(Path(words[index - 1]).name.lower() if index else '', ())
            index += 2 if takes_value else 1
            continue
        return Path(word).name.lower(), index
    return '', index


def programs(command):
    """The declared program names one bash call ran, with the subcommand where the subcommand is the point.

    Only `arguments.command` of a bash call is ever looked at, and only the first unskipped word of each
    segment — and only after that word is reduced to a basename and found in `PROGRAMS`. Everything else
    in the line, including every argument, stays in the file.
    """
    found = []
    for segment in _segments(command):
        words = segment.split()
        name, index = _first_program(words)
        if not name:
            continue
        if name not in PROGRAMS:
            found.append('other')
            continue
        listed = SUBCOMMANDS.get(name, ())
        following = words[index + 1] if index + 1 < len(words) else ''
        found.append(f'{name} {following}' if NAME.fullmatch(following) and following in listed else name)
    return found


# --- counting one session --------------------------------------------------------------------------------

def _stamp(text):
    try:
        return datetime.datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def _usage(message):
    """The token counts of one assistant turn. `cost` is not read: a price is not a measurement."""
    usage = message.get('usage')
    if not isinstance(usage, dict):
        return {}
    return {key: usage[key] for key in ('input', 'output', 'reasoning', 'cacheRead', 'cacheWrite')
            if isinstance(usage.get(key), (int, float))}


def blank(path):
    return {'file': path.name, 'bytes': path.stat().st_size if path.exists() else None, 'cwd': None,
            'started': None, 'ended': None, 'broken': 0, 'unreadable': False,
            'turns': 0, 'assistant': 0, 'tool_calls': 0, 'tool_errors': 0,
            'compactions': 0, 'compacted_tokens': 0, 'seconds': 0.0,
            'tools': collections.Counter(), 'programs': collections.Counter(),
            'tokens': collections.Counter()}


def read_session(path):
    """One transcript, counted. Every field is a number except the file name and the working directory."""
    found = blank(path)
    try:
        lines = path.read_text(errors='replace').splitlines()
    except OSError:
        found['unreadable'] = True
        return found
    for text in lines:
        if not text.strip():
            continue
        try:
            record = json.loads(text)
        except ValueError:
            found['broken'] += 1
            continue
        if not isinstance(record, dict):
            found['broken'] += 1
            continue
        moment = _stamp(record.get('timestamp'))
        if moment:
            stamp = moment.isoformat(timespec='seconds')
            found['started'] = min(found['started'] or stamp, stamp)
            found['ended'] = max(found['ended'] or stamp, stamp)
        kind = record.get('type')
        if kind == 'session' and isinstance(record.get('cwd'), str):
            found['cwd'] = record['cwd']
        elif kind == 'compaction':
            found['compactions'] += 1
            if isinstance(record.get('tokensBefore'), (int, float)):
                found['compacted_tokens'] += record['tokensBefore']
        elif kind == 'message':
            _count_message(found, record.get('message'))
    return found


def _count_message(found, message):
    """One turn. The content is walked for tool calls and then dropped; no text of it is kept."""
    if not isinstance(message, dict):
        return
    role = message.get('role')
    if role == 'user':
        found['turns'] += 1
        return
    if role == 'toolResult':
        found['tool_errors'] += 1 if message.get('isError') else 0
        return
    if role != 'assistant':
        return
    found['assistant'] += 1
    if isinstance(message.get('durationMs'), (int, float)):
        found['seconds'] += message['durationMs'] / 1000.0
    for key, value in _usage(message).items():
        found['tokens'][key] += value
    for part in (message.get('content') or []):
        if not isinstance(part, dict) or part.get('type') != 'toolCall':
            continue
        name = tool_name(part.get('name'))
        found['tool_calls'] += 1
        found['tools'][name] += 1
        if name == 'bash':
            arguments = part.get('arguments')
            found['programs'].update(programs(arguments.get('command') if isinstance(arguments, dict) else None))


# --- choosing which sessions -------------------------------------------------------------------------------

def session_files(root=ROOT):
    """Every transcript pi saved, oldest name first. `None` when there is no session directory at all."""
    directory = Path(root) / SESSIONS
    if not directory.is_dir():
        return None
    return sorted(directory.rglob('*.jsonl'), key=lambda path: (path.name, str(path)))


def summary(root=ROOT, days=None, top=10, now=None):
    """The inventory: the top ten of what the Agent does, and the rates a before/after comparison needs."""
    files = session_files(root)
    report = {'schema_version': 1, 'generated': datetime.datetime.now(datetime.timezone.utc)
              .isoformat(timespec='seconds'), 'dir': str(Path(root) / SESSIONS), 'days': days,
              'top': top, 'status': 'absent', 'sessions': [], 'unreadable': 0, 'broken': 0, 'unstamped': 0,
              'tools': {}, 'programs': {}, 'top_tools': [], 'top_programs': [], 'baseline': {}}
    if files is None:
        report['note'] = 'no session directory; that is not the same as nobody used pi'
        report['baseline'] = _baseline(0, {})
        return report
    if not files:
        report['status'] = 'empty'
        report['note'] = 'the directory is there and holds no transcript; a zero here would claim nobody asked'
        report['baseline'] = _baseline(0, {})
        return report

    cutoff = None
    if days is not None:
        base = now or datetime.datetime.now(datetime.timezone.utc)
        cutoff = (base - datetime.timedelta(days=days)).isoformat(timespec='seconds')

    tools, programs_, kept = collections.Counter(), collections.Counter(), []
    unreadable = broken = unstamped = 0
    for path in files:
        found = read_session(path)
        if found['unreadable']:             # counted beside the baseline: a file nobody read is not a session of zeros
            unreadable += 1
            continue
        if cutoff and not found['started']:  # it may be inside the window; the window cannot say, so the count does
            unstamped += 1
            continue
        if cutoff and found['started'] < cutoff:
            continue
        broken += found['broken']
        tools.update(found['tools'])
        programs_.update(found['programs'])
        kept.append(found)

    report['status'] = 'ok' if kept else 'empty'
    report['sessions'] = kept
    report['unreadable'], report['broken'], report['unstamped'] = unreadable, broken, unstamped
    report['tools'], report['programs'] = dict(tools), dict(programs_)
    report['top_tools'] = _top(tools, top)
    report['top_programs'] = _top(programs_, top)
    report['oldest'], report['newest'] = _span(kept)
    report['bytes'] = sum(s['bytes'] or 0 for s in kept)
    report['baseline'] = _baseline(len(kept), {'turns': sum(s['turns'] for s in kept),
                                               'assistant': sum(s['assistant'] for s in kept),
                                               'tool_calls': sum(s['tool_calls'] for s in kept),
                                               'tool_errors': sum(s['tool_errors'] for s in kept),
                                               'compactions': sum(s['compactions'] for s in kept),
                                               'seconds': sum(s['seconds'] for s in kept),
                                               'output': sum(s['tokens'].get('output', 0) for s in kept),
                                               'input': sum(s['tokens'].get('input', 0) for s in kept)})
    if not kept:
        report['note'] = f'no transcript in the last {days} day(s)'
    return report


def _top(counts, limit):
    """Most frequent first; ties broken by name so the same corpus always prints the same screen."""
    return [{'name': name, 'count': count} for name, count in
            sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _span(sessions):
    stamps = sorted(s['started'] for s in sessions if s['started'])
    ends = sorted(s['ended'] for s in sessions if s['ended'])
    return (stamps[0] if stamps else None, ends[-1] if ends else None)


def _rate(numerator, denominator, places=2):
    """A rate, or `None`. A zero here would say the Agent did it that often, which is a claim, not a gap."""
    if not denominator:
        return None
    return round(numerator / denominator, places)


def _baseline(sessions, totals):
    """The numbers T11.5 compares. Every one of them names what it is divided by, in the key."""
    return {'sessions': sessions, 'turns': totals.get('turns', 0), 'assistant_turns': totals.get('assistant', 0),
            'tool_calls': totals.get('tool_calls', 0), 'tool_errors': totals.get('tool_errors', 0),
            'compactions': totals.get('compactions', 0), 'seconds': round(totals.get('seconds', 0.0), 1),
            'input_tokens': totals.get('input', 0), 'output_tokens': totals.get('output', 0),
            'tool_calls_per_turn': _rate(totals.get('tool_calls', 0), totals.get('turns', 0)),
            'assistant_turns_per_turn': _rate(totals.get('assistant', 0), totals.get('turns', 0)),
            'tool_errors_per_call': _rate(totals.get('tool_errors', 0), totals.get('tool_calls', 0), 3),
            'seconds_per_assistant_turn': _rate(totals.get('seconds', 0.0), totals.get('assistant', 0), 1),
            'output_tokens_per_turn': _rate(totals.get('output', 0), totals.get('turns', 0), 1),
            'compactions_per_session': _rate(totals.get('compactions', 0), sessions)}


# --- the screen --------------------------------------------------------------------------------------------

def _number(value, suffix=''):
    return '-' if value is None else f'{value}{suffix}'


def markdown(report):
    """Two top-tens and a baseline. Nothing on it came from inside a message."""
    if report['status'] == 'absent':
        return f"Sessions: {report['dir']} does not exist — {report['note']}\n"
    if report['status'] == 'empty':
        return f"Sessions: {report['dir']} holds no transcript — {report['note']}\n"

    lines = [f"# pi sessions: {len(report['sessions'])} counted from `{report['dir']}`", '']
    window = f", last {report['days']} day(s)" if report['days'] is not None else ''
    lines += [f"Span {report['oldest'] or '-'} → {report['newest'] or '-'}, "
              f"{report['bytes'] / 1048576:.1f} MiB of transcript{window}. "
              f"{report['unreadable']} unreadable, {report['broken']} line(s) did not parse.", '']

    lines += ['## What the Agent does, top ten', '', '| tool call | count |', '|---|---:|']
    lines += [f"| {entry['name']} | {entry['count']} |" for entry in report['top_tools']] or ['| - | - |']
    lines += ['', '| program | count |', '|---|---:|']
    lines += [f"| {entry['name']} | {entry['count']} |" for entry in report['top_programs']] or ['| - | - |']
    lines += ['', 'A program is named only from a list declared in the code; anything else is `other`. Only the '
                  'first word of each shell segment is read and only as a basename, and no argument of any '
                  'command is read — so the count of `git commit` does not know what was committed.', '']

    baseline = report['baseline']
    lines += ['## Baseline', '', '| measure | value |', '|---|---:|',
              f"| sessions | {baseline['sessions']} |",
              f"| user turns | {baseline['turns']} |",
              f"| assistant turns | {baseline['assistant_turns']} |",
              f"| tool calls | {baseline['tool_calls']} |",
              f"| tool errors | {baseline['tool_errors']} |",
              f"| compactions | {baseline['compactions']} |",
              f"| assistant seconds | {baseline['seconds']} |",
              f"| input tokens | {baseline['input_tokens']} |",
              f"| output tokens | {baseline['output_tokens']} |",
              f"| tool calls per turn | {_number(baseline['tool_calls_per_turn'])} |",
              f"| assistant turns per turn | {_number(baseline['assistant_turns_per_turn'])} |",
              f"| tool errors per call | {_number(baseline['tool_errors_per_call'])} |",
              f"| seconds per assistant turn | {_number(baseline['seconds_per_assistant_turn'], 's')} |",
              f"| output tokens per turn | {_number(baseline['output_tokens_per_turn'])} |",
              f"| compactions per session | {_number(baseline['compactions_per_session'])} |", '']
    lines += ['These are the numbers T11.5 compares before and after a Skill. A `-` is a missing denominator, '
              'not a zero: a session that never asked a question has no rate per turn.', '']

    lines += ['## Sessions', '', '| started | cwd | turns | asst | calls | errs | comp | out tok | s |',
              '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for session in sorted(report['sessions'], key=lambda s: (s['started'] or '', s['file'])):
        lines.append(f"| {session['started'] or '-'} | {session['cwd'] or '-'} | {session['turns']} | "
                     f"{session['assistant']} | {session['tool_calls']} | {session['tool_errors']} | "
                     f"{session['compactions']} | {session['tokens'].get('output', 0)} | "
                     f"{session['seconds']:.0f} |")
    lines += ['', 'No prompt, reply, thinking block, command line or tool output is on this screen: the '
                  'transcripts are read for counts and nothing else is kept (T11.1).', '']
    return '\n'.join(lines)
