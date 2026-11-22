"""Agent adapters: what an Agent contributes, and nothing else (PA §12.1; execution plan W12).

An adapter owns four things: the version of its pinned install, its capability record, how the
process is started (argv, env, mounts and the gateway paths its protocol needs) and how its output
reads. Everything else stays with the Executor — start, stop, timeout and log storage belong to the
suite; cancel is a container stop for every Agent; resume belongs to the worktree and session design
of OA §10.1, not to an adapter. `install`, `auth-status`, `start-session`, `stream-events` and
`cancel` are deliberately absent: a session format from another product is not a compatibility we
are willing to maintain.

Two rules the tests hold:

* **The baseline Agent's case spec is frozen.** pi's argv and spec are byte-identical to what
  `scripts/suite.py` built before this module existed (X18). `tests/golden/pi-invocation.json` is
  that shape, captured from the code *before* the move; `tests/test_adapters.py` re-derives it.
* **An Agent talks to the local gateway or not at all.** `gateway_only` is enforced in
  `check_gateway_only`, the key is the task key handed in by the caller (IP-01, T4.3), and no
  adapter reads a credential from the process environment.
"""
import collections
from dataclasses import dataclass
import fcntl
from ipaddress import ip_address
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlparse

from agentiac import declared, paths
from agentiac.errors import explain
from agentiac.errors import command as cli_name

ROOT = paths.root()
CODE = paths.CODE

# Pinned installs (U4): a personal CLI updates itself, so an automated job may not use one.
FRONTENDS = ROOT / 'artifacts/frontends'
PI_TREE = FRONTENDS / 'pi'
PI = PI_TREE / 'node_modules/.bin/pi'
CLAUDE = FRONTENDS / 'claude-code/bin/claude'
CODEX = FRONTENDS / 'codex/bin/codex'
CODEX_TREE = FRONTENDS / 'codex'

OWNER_LABEL = 'io.agent-infra.owner=agent-infra'
TASK_KEY = 'LITELLM_TASK_KEY'
EXTENSIONS = CODE / 'agents/pi/extensions'


def node_binary():
    """The Node.js binary the adapters run and mount (`nodejs_binary`, D8.2), read when an adapter needs it.

    From the instance root's declarations (`declared.ROOT`); `ROOT` here is where the adapters keep their state.
    """
    return Path(declared.setting('nodejs_binary'))


def gateway():
    """The local gateway the adapters may talk to: loopback on the declared port (D8.2)."""
    return declared.gateway_url()


def __getattr__(name):
    """`adapters.NODE` and `adapters.GATEWAY` for callers that read them as values (scripts/agent.py, smoke.py, tests).

    Resolved on first use rather than at import, so importing the package reads no declaration (PEP 562).
    """
    if name == 'NODE':
        return node_binary()
    if name == 'GATEWAY':
        return gateway()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

# The same sandbox for every Agent: a comparison where one Agent had more memory is not a comparison.
LIMITS = ['--network=host', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
          '--memory=3g', '--memory-swap=3g', '--cpus=3', '--pids-limit=256']

# PA §12.3. A feature is `native` only because a probe observed it at this version.
CAPABILITIES = ('local_alias_read_edit_bash', 'structured_noninteractive', 'suite_grading',
                'apply_patch', 'skills')
CAPABILITY_VALUES = ('native', 'emulated', 'unsupported', 'unverified')
CAPABILITY_AGE_S = 90 * 24 * 3600
UNPROBED = 'never probed'

# A local model given a choice writes a file with the shell, and that proves nothing about the editor, so
# the tool is named. The file already exists because a tool that only creates files is not an edit.
EDIT_PROBE = ('Read probe.txt in the current directory, then use your {tool} and not a shell command to replace '
              'the word green with the word blue in that file, then run `cat probe.txt` in the shell. '
              'Finish immediately after.')


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(root):
    """One digest over a directory: the name, size and bytes of every file, in a fixed order.

    An Agent shipped as a tree (Codex: a binary, a code-mode host, ripgrep, bwrap) cannot be pinned by
    one file: replacing a sibling is the same attack as replacing the entrypoint.
    """
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        name = str(path.relative_to(root))
        if path.is_symlink():
            digest.update(f'link {name} {os.readlink(path)}\n'.encode())
        elif path.is_file():
            digest.update(f'file {name} {path.stat().st_size} {sha256_file(path)}\n'.encode())
    return digest.hexdigest()


class AdapterError(Exception):
    """An Agent cannot run here: not pinned, not declared, or asked to leave the gateway."""


@dataclass(frozen=True)
class RunRequest:
    """What the Executor knows about one attempt. An adapter may read none of it but this."""
    case: str
    spec: dict
    task: dict
    folder: Path
    key: str


@dataclass(frozen=True)
class Invocation:
    """argv, env, mount and the gateway paths this Agent's protocol needs (PA §12.1)."""
    argv: tuple
    env: dict
    timeout_s: int
    allowed_paths: tuple
    base_url: str
    name: str = ''

    def _head(self):
        return self.argv[:self.argv.index('--entrypoint')]

    def mounts(self):
        return tuple(_mount_values(self.argv))

    def container_env(self):
        """The variables the *container* is given. The process env is not the container env."""
        head = self._head()
        inline = [part[2:] for part in head if part.startswith('-e') and '=' in part]
        bare = [head[i + 1] for i, part in enumerate(head) if part == '-e']
        return tuple(inline + bare)

    def as_dict(self):
        return {'name': self.name, 'argv': list(self.argv), 'env': dict(self.env), 'timeout_s': self.timeout_s,
                'allowed_paths': list(self.allowed_paths), 'base_url': self.base_url,
                'mounts': list(self.mounts()), 'container_env': list(self.container_env())}


def loopback(url):
    """True when a URL can only reach this host. An Agent that can name another host can leave it.

    A *name* is never loopback: `http://127.0.0.1.evil.test` starts like an address and resolves
    wherever its owner says, so only a literal loopback address or `localhost` is accepted.
    """
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if host is None:
        return False
    if host in ('localhost', '::1'):
        return True
    try:
        return ip_address(host.strip('[]')).is_loopback
    except ValueError:
        return False


class BaseAdapter:
    """Shared behaviour. The three methods PA §12.1 names that carry no default are the ones to supply."""

    id = None
    baseline = False        # True only for the Agent whose case spec is frozen (X18)
    endpoint = None         # the gateway path its protocol speaks
    gateway_only = True
    install = None
    install_dir = None      # for an Agent shipped as a tree: the directory that is mounted and pinned
    pin_path = None         # tracked record of the bytes this Agent may run; None pins by lockfile instead
    tools = ()
    thinking = False        # whether this Agent is run with a thinking switch at all

    # ---- the PA §12.1 boundary -------------------------------------------------------------
    def version(self, install=None):
        raise NotImplementedError

    def invocation(self, request):
        raise NotImplementedError

    def parse(self, stdout, stderr):
        raise NotImplementedError

    def prepare(self, spec, state):
        """The write side of the mount `invocation` declares. pi writes its models.json here."""

    # ---- declaration -------------------------------------------------------------------------
    def declaration(self, root=ROOT):
        from agentiac import declared
        for agent in declared.load(root).agents:
            if agent.get('id') == self.id:
                return agent
        raise AdapterError(f'{self.id} is not declared in agents/agents.yml')

    def allowed_paths(self, root=ROOT):
        """The gateway paths the task relay may pass for this Agent (X16: the list is per adapter)."""
        return tuple(self.declaration(root).get('allowed_paths') or [self.endpoint])

    def model_for(self, alias, thinking=False, provider=None):
        raise NotImplementedError

    def spec_entry(self):
        """The case-spec addition that keeps two Agents from sharing a case id. None for the baseline."""
        return None if self.baseline else {'agent': self.id}

    # ---- guards ------------------------------------------------------------------------------
    def installed(self):
        return self.install is not None and Path(self.install).exists()

    def not_pinned(self, consequence):
        """Only a missing install lands here: a version that differs from the pin is verify_pin's refusal."""
        repair = (f'scripts/pin-agent.py {self.id} --source PATH' if self.pin_path is not None
                  else f'reinstall from agents/{self.id}/ (docs/operations.md, "Reinstalling {self.id}")')
        return AdapterError(explain(f'{self.id} is not pinned at {self.install}; {consequence}',
                                    [f'nothing is installed at {self.install}',
                                     'this is a worktree or a clean clone, and artifacts/ is gitignored'],
                                    f'{cli_name()} agent list', f'{repair}, or run from ~/src/agentiac'))

    def require_installed(self):
        if not self.installed():
            raise self.not_pinned('an automated job may not use a personal CLI that updates itself (U4)')
        self.verify_pin()
        return Path(self.install)

    def verify_pin(self):
        """Compare the bytes on disk with the tracked pin. Refusing to run is the only safe answer."""
        if self.pin_path is None:
            return          # pinned by install lockfile (agents/pi/): there is no byte pin to compare with
        pin = self.pin()
        if pin.get('tree_sha256'):
            actual, what = tree_digest(self.install_dir or Path(self.install).parent), 'tree'
            expected = pin['tree_sha256']
        elif pin.get('sha256'):
            actual, what = sha256_file(self.install), 'file'
            expected = pin['sha256']
        else:
            raise AdapterError(f'{self.id} has a pin with no sha256; it cannot run')
        if actual != expected:
            raise AdapterError(f'{self.install_dir or self.install} is not the {what} pinned in '
                               f'{Path(self.pin_path).name} ({expected[:12]}…); a pinned Agent is replaced '
                               'by re-running scripts/pin-agent.py, never edited in place')

    def pin(self):
        """The tracked record of the bytes this Agent is allowed to run."""
        if self.pin_path is None:
            raise AdapterError(f'{self.id} has no pin file; it is pinned by lockfile, not by bytes')
        try:
            record = json.loads(Path(self.pin_path).read_text())
        except (OSError, ValueError) as error:
            raise AdapterError(f'{self.id} has no readable pin at {self.pin_path} ({error}); it cannot run')
        if not (record.get('sha256') or record.get('tree_sha256')):
            raise AdapterError(f'{self.id} has a pin with no sha256 or tree_sha256; it cannot run')
        return record

    def check_gateway_only(self, base_url):
        """`gateway_only` is enforced here, not documented there."""
        if self.gateway_only and not loopback(base_url):
            raise AdapterError(f'{self.id} may only talk to the local gateway; refused {base_url!r}')
        return base_url

    # ---- the shared container shape ----------------------------------------------------------
    def build(self, name, image, entrypoint, argv, env, mounts, key, timeout_s, extra_mounts=(), base_url=None,
              key_env='LITELLM_TASK_KEY'):
        base_url = base_url or gateway()
        self.check_gateway_only(base_url)
        command = ['docker', 'run', '--rm', '--name', name, '--label', OWNER_LABEL] + list(LIMITS) + \
            ['--user', f'{os.getuid()}:{os.getgid()}', '--tmpfs', '/tmp:rw,exec,nosuid,size=256m']
        command += [item for pair in [('-e', value) for value in env] for item in pair]
        command += [item for pair in [('-v', value) for value in mounts] for item in pair]
        command += ['-w', '/work', '--entrypoint', entrypoint]
        command += [item for pair in [('-v', value) for value in extra_mounts] for item in pair]
        # key_env is the only variable given a value from the caller, so a name passed bare with `-e` that is
        # not this one would take whatever the operator's shell happens to hold into the container.
        return Invocation(argv=tuple(command + [image] + list(argv)), env={key_env: key},
                          timeout_s=timeout_s, allowed_paths=self.allowed_paths(), base_url=base_url, name=name)

    # ---- capability probe (T12.4) ------------------------------------------------------------
    def probe_invocation(self, feature, request):
        """A minimal run that would demonstrate one feature, or None when this Agent cannot be probed."""
        return None

    def probe_verdict(self, feature, stdout, stderr, returncode):
        """What the observed output proves: (value, note). Only `native` needs evidence."""
        raise AdapterError(f'{self.id} has no verdict rule for {feature}')


class PiAdapter(BaseAdapter):
    """pi, the baseline. Its argv and spec are what `scripts/suite.py` built before adapters existed."""

    id = 'pi'
    baseline = True
    endpoint = '/v1/chat/completions'
    install = PI
    tools = ('read', 'edit', 'write', 'bash')
    thinking = True
    STATE = '/state'
    PROMPT = ('{issue}\n\n---\n'
              'The repository is checked out in the current directory (/work). Fix the problem described above in '
              'the source code and add or update tests for it. The test suite runs offline with `python3 -m pytest -q` '
              '(about a minute); make sure it passes before you finish. Do not commit.')

    def version(self, install=None):
        import subprocess
        self.require_installed()
        return subprocess.check_output([str(node_binary()), str(install or self.install), '--version'], text=True).strip()

    def model_for(self, alias, thinking=False, provider=None):
        if provider is not None:  # an evaluation-only provider object, never gateway or daily-use settings
            models = [model for model in provider.get('models', []) if model.get('id') == alias]
            if len(models) != 1:
                raise ValueError(f'{alias}: provider config needs exactly one matching model')
            return models[0]
        import agent  # scripts/agent.py builds pi's model metadata; it stays the one place
        return agent.model_entry(alias, thinking=thinking)

    def provider_config(self, spec):
        """The models.json of one attempt, built from the tracked file and this spec only."""
        config = json.loads((ROOT / 'agents/pi/models.json').read_text())
        if spec.get('provider') is not None:
            config['providers']['local-infra'] = dict(spec['provider'])
        else:
            # IP-01 (T4.3): the container authenticates with the task key, never the master key.
            config['providers']['local-infra']['apiKey'] = '${LITELLM_TASK_KEY}'  # literal: greppable
        config['providers']['local-infra']['models'] = [spec['model']]
        return config

    def prepare(self, spec, state):
        """models.json for this attempt only; gateway and daily-use settings are never touched."""
        state = Path(state)
        state.mkdir(parents=True, exist_ok=True)
        (state / 'models.json').write_text(json.dumps(self.provider_config(spec), indent=2) + '\n')

    def invocation(self, request):
        spec, task, folder = request.spec, request.task, request.folder
        state, work = folder / 'agent-state', folder / 'workspace'
        guard = (spec.get('loop_guard') or {}).get('extension') if isinstance(spec.get('loop_guard'), dict) else None
        env = ['HOME=/tmp', f'PI_CODING_AGENT_DIR={self.STATE}', 'PI_OFFLINE=1', TASK_KEY]
        mounts = [f'{node_binary()}:/opt/node:ro', f'{PI_TREE}:/app:ro',
                  f'{state}:{self.STATE}:rw', f'{work}:/work:rw']
        argv = ['/app/node_modules/.bin/pi', '--offline', '--no-session', '--no-extensions', '--no-mcp']
        if guard:
            argv += ['-e', f'/guard/{guard}']
        argv += ['--no-skills', '--no-context-files', '--no-prompt-templates', '--no-themes', '--no-approve',
                 '--tools', ','.join(self.tools), '--provider', 'local-infra', '--model', spec['alias'],
                 '--thinking', spec.get('thinking', 'off'), '--mode', 'json', '--print',
                 self.PROMPT.format(issue=task['issue'])]
        extra = [f'{EXTENSIONS / guard}:/guard/{guard}:ro'] if guard else []
        return self.build(declared.resource('suite-' + request.case[:12]), spec['image'], '/opt/node', argv, env,
                          mounts, request.key, spec['timeout_s'], extra)

    def parse(self, stdout, stderr):
        """Tool counts, tool errors, assistant turns, final stop reason and summed usage."""
        tools, errors, turns, final, usage = collections.Counter(), 0, 0, {}, collections.Counter()
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'tool_execution_end':
                errors += bool(event.get('isError'))
                tools[event.get('toolName', 'unknown')] += 1
            elif event.get('type') == 'message_end' and event.get('message', {}).get('role') == 'assistant':
                final, turns = event['message'], turns + 1
                for name, value in (final.get('usage') or {}).items():
                    if isinstance(value, (int, float)):
                        usage[name] += value
        return {'tools': dict(tools), 'tool_errors': errors, 'assistant_turns': turns,
                'stop_reason': final.get('stopReason'), 'usage': dict(usage)}

    # ---- probe ------------------------------------------------------------------------------
    def probe_invocation(self, feature, request):
        if feature == 'structured_noninteractive':
            argv = ['/app/node_modules/.bin/pi', '--offline', '--no-session', '--no-extensions', '--no-mcp',
                    '--no-skills', '--no-context-files', '--no-prompt-templates', '--no-themes', '--no-approve',
                    '--tools', '', '--provider', 'local-infra', '--model', request.spec['alias'],
                    '--mode', 'json', '--print', 'Reply with exactly the word OK and nothing else.']
            return self.build(declared.resource('probe-pi-struct'), request.spec['image'], '/opt/node', argv,
                              ['HOME=/tmp', f'PI_CODING_AGENT_DIR={self.STATE}', 'PI_OFFLINE=1', TASK_KEY],
                              [f'{node_binary()}:/opt/node:ro', f'{PI_TREE}:/app:ro',
                               f'{request.folder / "agent-state"}:{self.STATE}:rw',
                               f'{request.folder / "workspace"}:/work:rw'],
                              request.key, 300)
        if feature == 'local_alias_read_edit_bash':
            argv = ['/app/node_modules/.bin/pi', '--offline', '--no-session', '--no-extensions', '--no-mcp',
                    '--no-skills', '--no-context-files', '--no-prompt-templates', '--no-themes', '--no-approve',
                    '--tools', 'read,edit,bash', '--provider', 'local-infra', '--model', request.spec['alias'],
                    '--mode', 'json', '--print',
                    EDIT_PROBE.format(tool='edit tool')]
            return self.build(declared.resource('probe-pi-tools'), request.spec['image'], '/opt/node', argv,
                              ['HOME=/tmp', f'PI_CODING_AGENT_DIR={self.STATE}', 'PI_OFFLINE=1', TASK_KEY],
                              [f'{node_binary()}:/opt/node:ro', f'{PI_TREE}:/app:ro',
                               f'{request.folder / "agent-state"}:{self.STATE}:rw',
                               f'{request.folder / "workspace"}:/work:rw'],
                              request.key, 600)
        return None

    def probe_verdict(self, feature, stdout, stderr, returncode):
        observed = self.parse(stdout, stderr)
        if feature == 'structured_noninteractive':
            if returncode != 0 or not observed['usage'] or not observed['assistant_turns']:
                return 'unsupported', f'no structured assistant turn (returncode {returncode})'
            return 'native', f"{observed['assistant_turns']} assistant turn, usage reported"
        if feature == 'local_alias_read_edit_bash':
            needed = {'read', 'edit', 'bash'}
            done = {name for name, count in observed['tools'].items() if count}
            if not needed <= done:      # the model chose not to: that says nothing about whether the tool works
                return 'unverified', f'observed tools {sorted(done)}, needed {sorted(needed)}'
            return 'native', 'read, edit and bash all completed'
        raise AdapterError(f'pi has no probe for {feature}')


class ClaudeCodeAdapter(BaseAdapter):
    """Claude Code in bare mode against the local gateway (T12.2). Shape taken from the recorded
    fixture run in docs/agents.md, with the task key instead of the master key (T4.3)."""

    id = 'claude-code'
    endpoint = '/v1/messages'
    install = CLAUDE
    pin_path = ROOT / 'agents/claude-code/pin.json'
    tools = ('Read', 'Edit', 'Write', 'Bash')
    SYSTEM = ('Work only in /work. Fix the problem described in the task below in the source code and add or '
              'update tests for it. The test suite runs offline with `python3 -m pytest -q` (about a minute); '
              'make sure it passes before you finish. Do not commit.')

    def version(self, install=None):
        import subprocess
        return subprocess.check_output([str(install or self.require_installed()), '--version'], text=True).strip()

    def model_for(self, alias, thinking=False, provider=None):
        if provider is not None:
            raise AdapterError('--provider-config is a pi provider object; for claude-code the gateway is '
                               'fixed by ANTHROPIC_BASE_URL, so there is nothing to override')
        if thinking:
            raise AdapterError('claude-code runs with thinking off (MAX_THINKING_TOKENS=0); there is no '
                               'verified switch to turn it on')
        import agent  # the same limits pi gets, or the comparison measures the limit (T12.5)
        entry = agent.model_entry(alias)
        return {'id': alias, 'name': f'claude-code ({alias})', 'reasoning': False, 'gateway_only': True,
                'contextWindow': entry.get('contextWindow'), 'maxTokens': entry.get('maxTokens')}

    def invocation(self, request):
        spec, task, folder = request.spec, request.task, request.folder
        binary = self.require_installed()
        alias = spec['alias']
        base = self.check_gateway_only(spec.get('base_url') or gateway())
        # Every model name Claude Code may ask for is pinned to the alias: an unpinned name is a route
        # to a model nobody declared, and `--bare` does not promise to ask for nothing else.
        env = ['HOME=/tmp', 'CLAUDE_CONFIG_DIR=/tmp/claude', f'ANTHROPIC_BASE_URL={base}', 'ANTHROPIC_API_KEY',
               f'ANTHROPIC_MODEL={alias}', f'ANTHROPIC_DEFAULT_HAIKU_MODEL={alias}',
               f'ANTHROPIC_DEFAULT_SONNET_MODEL={alias}', f'ANTHROPIC_DEFAULT_OPUS_MODEL={alias}',
               'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1', 'DISABLE_AUTOUPDATER=1',
               'DISABLE_ERROR_REPORTING=1', 'DISABLE_TELEMETRY=1',
               f"CLAUDE_CODE_MAX_OUTPUT_TOKENS={spec['model']['maxTokens']}",
               'MAX_THINKING_TOKENS=0', 'PYTHONDONTWRITEBYTECODE=1', 'PYTHONPATH=/work']
        argv = ['--bare', '--no-session-persistence', '--setting-sources', '', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands',
                '--dangerously-skip-permissions', '--tools', ','.join(self.tools), '--model', alias,
                '--effort', 'low', '--output-format', 'stream-json', '--verbose',
                '--system-prompt', self.SYSTEM, '--print', task['issue']]
        return self.build(declared.resource('suite-' + request.case[:12]), spec['image'], '/opt/claude', argv, env,
                          [f'{binary}:/opt/claude:ro', f'{folder / "workspace"}:/work:rw'],
                          request.key, spec['timeout_s'], key_env='ANTHROPIC_API_KEY')

    def parse(self, stdout, stderr):
        """stream-json: tool_use paired with tool_result, the final `result` event carries usage."""
        tools, errors, turns, usage, final = collections.Counter(), 0, 0, collections.Counter(), {}
        pending = {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'result':
                final = event
            for block in (event.get('message') or {}).get('content', []) or []:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use':
                    pending[block.get('id')] = block.get('name', 'unknown')
                elif block.get('type') == 'tool_result':
                    name = pending.pop(block.get('tool_use_id'), 'unknown')
                    tools[name] += 1
                    errors += bool(block.get('is_error'))
            if (event.get('message') or {}).get('role') == 'assistant' and event.get('type') == 'assistant':
                turns += 1
        for name, value in (final.get('usage') or {}).items():
            if isinstance(value, (int, float)):
                usage[name] += value
        return {'tools': dict(tools), 'tool_errors': errors,
                'assistant_turns': turns or final.get('num_turns'),
                'stop_reason': final.get('stop_reason') or final.get('subtype'),
                'usage': dict(usage)}

    def probe_invocation(self, feature, request):
        if feature not in ('structured_noninteractive', 'local_alias_read_edit_bash'):
            return None
        invocation = self.invocation(request)
        tail = list(_agent_argv(invocation.argv, request.spec['image']))
        tail[tail.index('--print') + 1] = EDIT_PROBE.format(tool='Edit tool') \
            if feature == 'local_alias_read_edit_bash' else \
            'Reply with exactly the word OK and nothing else. Do not use any tool.'
        return self.build(declared.resource('probe-claude'), request.spec['image'], '/opt/claude', tail,
                          [item for item in _env_values(invocation.argv)],
                          [mount for mount in _mount_values(invocation.argv)], request.key, 600,
                          key_env='ANTHROPIC_API_KEY')

    def probe_verdict(self, feature, stdout, stderr, returncode):
        observed = self.parse(stdout, stderr)
        if feature == 'structured_noninteractive':
            if returncode != 0 or not observed['stop_reason']:
                return 'unsupported', f'no result event (returncode {returncode})'
            return 'native', f'result {observed["stop_reason"]}, {observed["assistant_turns"]} turns'
        if feature == 'local_alias_read_edit_bash':
            needed = {'Read', 'Edit', 'Bash'}
            done = {name for name, count in observed['tools'].items() if count}
            if not needed <= done:      # the model chose not to: that says nothing about whether the tool works
                return 'unverified', f'observed tools {sorted(done)}, needed {sorted(needed)}'
            return 'native', 'Read, Edit and Bash all completed'
        raise AdapterError(f'claude-code has no probe for {feature}')


class CodexAdapter(BaseAdapter):
    """Codex CLI in `exec --json` against the local Responses provider (T12.6).

    The shape is the recorded fixture run in `scripts/archive/codex-container-task.py`, with the task key in
    place of any other credential. Its config is written by `prepare` into `CODEX_HOME`: it names the
    alias, the loopback gateway and the `responses` wire API, and turns off everything that could
    reach the vendor cloud or the user's own setup (update check, web search, approval, rules, apps,
    multi-agent). The release is mounted as a tree, so the pin covers the whole tree.
    """

    id = 'codex'
    endpoint = '/v1/responses'
    install = CODEX
    install_dir = CODEX_TREE
    pin_path = ROOT / 'agents/codex/pin.json'
    tools = ('read', 'edit', 'bash')
    STATE = '/state'
    CONFIG = '''model = "MODEL_ALIAS"
model_provider = "local_infra"
model_context_window = MODEL_CONTEXT
model_reasoning_effort = "none"
model_reasoning_summary = "none"
check_for_update_on_startup = false
web_search = "disabled"
approval_policy = "never"
[features]
multi_agent = false
apps = false
apply_patch_freeform = false
unified_exec = false
[model_providers.local_infra]
name = "Private local evaluation gateway"
base_url = "MODEL_BASE_URL"
wire_api = "responses"
env_key = "LITELLM_TASK_KEY"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 120000
'''
    PROMPT = ('Work only in /work. {issue} Read the source, edit it, then run `python3 -m pytest -q`. '
              'The test suite runs offline and takes about a minute; make sure it passes before you '
              'finish. Do not commit, do not delegate, and do not use external services.')

    def base_url(self):
        return gateway() + '/v1'

    def version(self, install=None):
        import subprocess
        return subprocess.check_output([str(install or self.require_installed()), '--version'], text=True).strip()

    def model_for(self, alias, thinking=False, provider=None):
        if provider is not None:
            raise AdapterError('--provider-config is a pi provider object; for codex the provider is the '
                               'config.toml written by prepare(), so there is nothing to override')
        if thinking:
            raise AdapterError('codex runs with model_reasoning_effort = "none"; no thinking switch has '
                               'been verified for it')
        import agent  # scripts/agent.py builds the model metadata; the window is the target's, as it is for pi
        return {'id': alias, 'name': f'codex ({alias})', 'reasoning': False, 'gateway_only': True,
                'contextWindow': agent.model_entry(alias).get('contextWindow')}

    def config_text(self, spec):
        window = spec.get('context_window') or (spec.get('model') or {}).get('contextWindow')
        if not window:      # a guessed window compacts too early or overruns, and the comparison with pi is lost
            raise AdapterError(f"{spec['alias']} declares no context window; codex cannot be configured for it")
        return (self.CONFIG.replace('MODEL_ALIAS', spec['alias'])
                .replace('MODEL_CONTEXT', str(window))
                .replace('MODEL_BASE_URL', self.check_gateway_only(spec.get('base_url') or self.base_url())))

    def prepare(self, spec, state):
        """config.toml for this attempt only. A personal ~/.codex is never read: CODEX_HOME points here."""
        state = Path(state)
        state.mkdir(parents=True, exist_ok=True)
        (state / 'config.toml').write_text(self.config_text(spec))

    def invocation(self, request):
        spec, task, folder = request.spec, request.task, request.folder
        self.require_installed()
        base = self.check_gateway_only(spec.get('base_url') or self.base_url())
        env = ['HOME=/tmp', f'CODEX_HOME={self.STATE}', 'LITELLM_TASK_KEY', 'PYTHONDONTWRITEBYTECODE=1',
               'PYTHONPATH=/work']
        argv = ['exec', '--strict-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
                '--dangerously-bypass-approvals-and-sandbox', '--color', 'never', '--json',
                self.PROMPT.format(issue=task['issue'])]
        return self.build(declared.resource('suite-' + request.case[:12]), spec['image'], '/opt/codex/bin/codex', argv, env,
                          [f'{self.install_dir}:/opt/codex:ro', f'{folder / "agent-state"}:{self.STATE}:rw',
                           f'{folder / "workspace"}:/work:rw'], request.key, spec['timeout_s'], base_url=base)

    def parse(self, stdout, stderr):
        """`--json` events: item.completed carries the work, turn.completed the usage.

        Codex has no distinct read tool: a read is a command. Counting it as one would claim a tool the
        Agent does not have, so items are counted by their own type and `emulated` is said out loud.
        """
        tools, errors, turns, usage, final = collections.Counter(), 0, 0, collections.Counter(), {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get('type')
            if kind == 'turn.completed':
                final = event
            elif kind in ('error', 'turn.failed'):
                errors += 1
            elif kind == 'item.completed':
                item = event.get('item') or {}
                tools[item.get('type', 'unknown')] += 1
                if item.get('type') == 'agent_message':
                    turns += 1
                elif item.get('type') == 'command_execution' and item.get('exit_code') not in (0, None):
                    errors += 1
                elif item.get('type') == 'file_change' and item.get('status') not in ('completed', None):
                    errors += 1
        for name, value in (final.get('usage') or {}).items():
            if isinstance(value, (int, float)):
                usage[name] += value
        return {'tools': dict(tools), 'tool_errors': errors, 'assistant_turns': turns,
                'stop_reason': 'completed' if final else None, 'usage': dict(usage)}

    def probe_invocation(self, feature, request):
        ask = {'structured_noninteractive': 'Reply with exactly the word OK and nothing else. Do not use any tool.',
               'local_alias_read_edit_bash': EDIT_PROBE.format(tool='apply_patch tool'),
               'apply_patch': 'Using your apply_patch tool and not a shell command, create patched.txt in the '
                              'current directory containing exactly the word OK. Finish immediately after.'}.get(feature)
        if ask is None:
            return None
        invocation = self.invocation(request)
        argv = list(_agent_argv(invocation.argv, request.spec['image']))
        argv[argv.index('--json') + 1] = ask
        return self.build(declared.resource('probe-codex'), request.spec['image'], '/opt/codex/bin/codex', argv,
                          [item for item in _env_values(invocation.argv)],
                          [mount for mount in _mount_values(invocation.argv)], request.key, 600)

    def probe_verdict(self, feature, stdout, stderr, returncode):
        observed = self.parse(stdout, stderr)
        done = {name for name, count in observed['tools'].items() if count}
        if feature == 'structured_noninteractive':
            if returncode != 0 or observed['stop_reason'] != 'completed':
                return 'unsupported', f'no turn.completed (returncode {returncode}, errors {observed["tool_errors"]})'
            return 'native', f'turn.completed with {dict(observed["usage"])}'
        if feature == 'apply_patch':
            if 'file_change' not in done:
                return 'unverified', f'no completed file_change item, observed {sorted(done)}'
            return 'native', 'a file_change item completed'
        if feature == 'local_alias_read_edit_bash':
            if not {'file_change', 'command_execution'} <= done:
                return 'unverified', f'observed {sorted(done)}, needed a file change and a command'
            return 'emulated', 'reads and edits go through commands; codex has no distinct read tool'
        raise AdapterError(f'codex has no probe for {feature}')


def _env_values(argv):
    head = argv[:argv.index('--entrypoint')]
    for index, part in enumerate(head):
        if part == '-e':
            yield head[index + 1]
        elif part.startswith('-e='):
            yield part[3:]


def _mount_values(argv):
    """Every mount of a docker argv. `extra_mounts` come after `--entrypoint`, so the head alone misses them."""
    words = list(argv)
    for index, part in enumerate(words[:-1]):
        if part == '-v':
            yield words[index + 1]
        elif part.startswith('-v='):
            yield part[3:]


def _agent_argv(argv, image):
    """What the Agent itself receives: everything after the image, and nothing docker was told."""
    words = list(argv)
    return tuple(words[words.index(image, words.index('--entrypoint')) + 1:])


ADAPTERS = {PiAdapter().id: PiAdapter()}
BASELINE = 'pi'


def register(adapter):
    ADAPTERS[adapter.id] = adapter


def adapter(name=None):
    """The adapter for an Agent id, or for a case spec: its `agent` key, or the baseline when absent."""
    if isinstance(name, dict):
        name = name.get('agent')
    key = name or BASELINE
    if key not in ADAPTERS:
        raise AdapterError(f'no adapter named {name!r}; known: ' + ', '.join(sorted(ADAPTERS)))
    return ADAPTERS[key]


register(ClaudeCodeAdapter())
register(CodexAdapter())


# ---- capability matrix (PA §12.3, T12.4) ----------------------------------------------------

def capability_path(root=ROOT):
    return Path(root) / 'artifacts/agents/capabilities.json'


def load_capabilities(root=ROOT):
    """The probe record, or {} when nothing has ever been probed. Corrupt is empty, not fatal."""
    try:
        record = json.loads(capability_path(root).read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def record_capability(agent_id, version, feature, value, note=None, root=ROOT, now=None, probe='probe'):
    """One observed feature at one version. A value with no version and no time is not evidence."""
    if feature not in CAPABILITIES:
        raise ValueError(f'{feature} is not a capability of the matrix; known: ' + ', '.join(CAPABILITIES))
    if value not in CAPABILITY_VALUES:
        raise ValueError(f'{value} is not a capability value; known: ' + ', '.join(CAPABILITY_VALUES))
    if not version:
        raise ValueError('a capability value needs the Agent version it was observed at')
    path = capability_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The suite runs one thread per alias and a probe may run beside it, so the read, the change and the
    # replace are one step, and each writer has its own temporary file.
    with open(path.with_suffix('.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = load_capabilities(root)
        entry = record.setdefault(agent_id, {}).setdefault(feature, {})
        entry.update({'value': value, 'version': version, 'probed_at': (now or time.time()),
                      'note': note, 'probe': probe})
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
        with os.fdopen(descriptor, 'w') as out:
            out.write(json.dumps(record, indent=1, sort_keys=True) + '\n')
        os.replace(temporary, path)
    return entry


def capabilities_for(agent_id, version, root=ROOT, now=None):
    """The matrix row for one Agent at one version. Anything unproven at *this* version is `unverified`."""
    record = load_capabilities(root).get(agent_id, {})
    now = time.time() if now is None else now
    row = {}
    for feature in CAPABILITIES:
        entry = record.get(feature)
        value, reason = 'unverified', UNPROBED
        if isinstance(entry, dict) and entry.get('value') in CAPABILITY_VALUES:
            if entry.get('version') != version:
                reason = f"probed at {entry.get('version')}, the Agent is now {version}"
            elif now - float(entry.get('probed_at') or 0) > CAPABILITY_AGE_S:
                reason = f"probe is {int((now - float(entry['probed_at'])) / 86400)} days old"
            else:
                value, reason = entry['value'], entry.get('note')
        row[feature] = {'value': value, 'reason': reason, 'version': version,
                        'probed_at': (entry or {}).get('probed_at')}
    return row


def probe(adapter, feature, request, run):
    """Run one probe and record what it showed. `run(argv, env, timeout_s)` returns the three outputs.

    A feature this Agent cannot be probed for is recorded as `unverified` with the reason, never as
    `unsupported`: the difference is whether we looked.
    """
    if feature not in CAPABILITIES:
        raise ValueError(f'{feature} is not a capability of the matrix')
    if not adapter.installed():
        # Nothing was observed, so nothing is recorded: an Agent that is not pinned has no version to
        # attach a claim to, and `unverified` written without a version would be exactly the kind of
        # entry the matrix exists to keep out.
        raise adapter.not_pinned('nothing was probed')
    invocation = adapter.probe_invocation(feature, request)
    if invocation is None:
        return {'feature': feature, 'value': 'unverified', 'reason': f'{adapter.id} has no probe for {feature}'}
    stdout, stderr, returncode = run(invocation.argv, invocation.env, invocation.timeout_s)
    value, note = adapter.probe_verdict(feature, stdout, stderr, returncode)
    entry = record_capability(adapter.id, adapter.version(), feature, value, note, root=ROOT)
    return {'feature': feature, 'value': value, 'reason': note, 'probed_at': entry['probed_at']}
