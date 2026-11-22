"""W12: the adapter boundary, the frozen baseline (X18), the gateway-only rule and the capability matrix.

The golden in tests/golden/ was captured from `scripts/suite.py` *before* the adapter existed.
Everything else here is written against behaviour, not against the implementation.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import adapters, declared, paths  # noqa: E402
from agentiac.state import stable_id  # noqa: E402

SITE = paths.root()

spec = importlib.util.spec_from_file_location('suite_cli', ROOT / 'scripts/suite.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

GOLDEN = json.loads((Path(__file__).resolve().parent / 'golden/pi-invocation.json').read_text())['cases']
CASE = 'deadbeef-0000-1111-2222-333344445555'
IMAGE = 'sha256:' + '0123456789abcdef' * 4
TASK = {'id': 'example', 'repo': 'example', 'base': 'f' * 40, 'reference': 'e' * 40,
        'issue': 'the issue text', 'acceptance': ['test_a'], 'regression': ['test_b']}

SYNTH_AGENTS = [
    {'id': 'pi', 'allowed_paths': ['/v1/chat/completions'], 'install': 'artifacts/frontends/pi'},
    {'id': 'claude-code', 'allowed_paths': ['/v1/messages'],
     'pin': 'agents/claude-code/pin.json', 'install': 'artifacts/frontends/claude-code'},
    {'id': 'codex', 'allowed_paths': ['/v1/responses'],
     'pin': 'agents/codex/pin.json', 'install': 'artifacts/frontends/codex'},
]
_AGENTS_PATCH = None


def setUpModule():
    global _AGENTS_PATCH
    if not (SITE / 'agents/agents.yml').exists():
        orig_load = declared.load

        def load_with_agents(root=None):
            graph = orig_load(root)
            if not graph.agents:
                graph.agents = list(SYNTH_AGENTS)
            return graph

        _AGENTS_PATCH = patch('agentiac.declared.load', side_effect=load_with_agents)
        _AGENTS_PATCH.start()


def tearDownModule():
    global _AGENTS_PATCH
    if _AGENTS_PATCH is not None:
        _AGENTS_PATCH.stop()
        _AGENTS_PATCH = None


class Completed:
    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def attempt(spec, folder, key='task-key-under-test', module=cli):
    """Run one attempt with every side effect stubbed; return the argv, env and result."""
    captured = {}
    folder.mkdir(parents=True, exist_ok=True)

    def fake_run(command, **kwargs):
        if list(command)[:1] == ['docker'] and 'run' in list(command)[:3]:
            captured['argv'] = [str(part) for part in command]
            captured['env'] = kwargs.get('env')
            captured['timeout'] = kwargs.get('timeout')
            return Completed('{}')
        return Completed('')

    with patch.object(module.subprocess, 'run', fake_run), \
            patch.object(module.suite, 'export', lambda *a, **k: None), \
            patch.object(module.suite, 'grade', lambda *a, **k: ({'passed': True, 'acceptance_passed': 1}, 'out')):
        result = module.run_case(CASE, spec, TASK, key, folder)
    argv = [part.replace(str(folder / 'agent-state'), '{state}').replace(str(folder / 'workspace'), '{work}')
            .replace(f'{os.getuid()}:{os.getgid()}', '{uid}') for part in captured.get('argv', [])]
    return argv, captured, result


class BaselineGoldenTests(unittest.TestCase):
    """T12.1's completion condition: pi's argv and spec are what the suite built before adapters."""

    def test_argv_is_the_captured_command_argument_for_argument(self):
        node = str(adapters.node_binary())
        suite_prefix = declared.resource('suite', SITE)
        for name, case in GOLDEN.items():
            with self.subTest(variant=name):
                with tempfile.TemporaryDirectory() as directory:
                    argv, captured, _ = attempt(case['spec'], Path(directory) / 'attempt-1')
                prefixes = sorted({str(SITE), str(ROOT)}, key=len, reverse=True)
                normalized = []
                for part in argv:
                    item = part.replace(node, '{node}')
                    for prefix in prefixes:
                        item = item.replace(prefix, '{checkout}')
                    normalized.append(item.replace(suite_prefix, '{suite}'))
                self.assertEqual(normalized, case['argv'])
                self.assertEqual(captured['timeout'], case['timeout_s'])
                self.assertEqual(captured['env'].get('LITELLM_TASK_KEY'), case['task_key_env'])

    def test_the_result_carries_exactly_the_captured_keys(self):
        for name, case in GOLDEN.items():
            with self.subTest(variant=name):
                with tempfile.TemporaryDirectory() as directory:
                    _, _, result = attempt(case['spec'], Path(directory) / 'attempt-1')
                self.assertEqual(sorted(result), case['result_keys'])

    def test_the_case_id_of_a_captured_spec_has_not_moved(self):
        for name, case in GOLDEN.items():
            with self.subTest(variant=name):
                self.assertEqual(stable_id(case['spec']), case['case_id'])

    def test_the_baseline_adds_nothing_to_the_spec(self):
        self.assertIsNone(adapters.adapter('pi').spec_entry())
        self.assertEqual(adapters.adapter('pi').id, 'pi')
        self.assertTrue(adapters.adapter('pi').baseline)

    def test_a_spec_without_an_agent_key_is_the_baseline(self):
        self.assertIs(adapters.adapter({'suite': 'v1'}), adapters.adapter('pi'))
        self.assertIs(adapters.adapter(None), adapters.adapter('pi'))

    def test_the_prompt_text_survived_the_move(self):
        self.assertEqual(cli.PROMPT, adapters.PiAdapter.PROMPT)
        with tempfile.TemporaryDirectory() as directory:
            argv, _, _ = attempt(GOLDEN['plain']['spec'], Path(directory) / 'attempt-1')
        self.assertIn(cli.PROMPT.format(issue=TASK['issue']), argv)


class SecondAgentTests(unittest.TestCase):
    """A second Agent must not be able to share a case id with the first (T12.5: never pool them)."""

    def spec_for(self, agent_id):
        return {'suite': 'v1', 'frontend': f'{agent_id} 1.0.0', 'alias': 'local-coder-spark',
                'model': {'id': 'local-coder-spark'}, 'image': IMAGE, 'task': 'example',
                'task_sha256': 'a' * 64, 'trial': 1, 'timeout_s': 1800, **(adapters.adapter(agent_id).spec_entry() or {})}

    def test_a_second_agent_names_itself_in_the_spec(self):
        self.assertEqual(adapters.adapter('claude-code').spec_entry(), {'agent': 'claude-code'})

    def test_the_same_alias_under_two_agents_is_two_cases(self):
        self.assertNotEqual(stable_id(self.spec_for('pi')), stable_id(self.spec_for('claude-code')))

    def test_an_unknown_agent_is_refused_not_defaulted(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.adapter('hermes')
        with self.assertRaises(adapters.AdapterError):
            adapters.adapter({'agent': 'opencode'})

    def test_the_suite_refuses_the_pi_loop_guard_for_another_agent(self):
        argv = ['run', '--alias', 'local-coder-spark', '--agent', 'claude-code', '--task', 'nope',
                '--loop-guard', '--trial', '0']
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cli, 'DATA', Path(directory) / 'data'), \
                patch.object(cli, 'TASKS', Path(directory)), \
                patch.object(sys, 'argv', ['suite.py'] + argv), \
                patch.object(cli.subprocess, 'check_output', return_value=IMAGE + '\n'), \
                patch.object(cli.adapters.adapter('claude-code'), 'version', return_value='9.9.9'), \
                patch.object(cli, 'load_task', return_value=TASK), \
                patch.object(cli, 'read_key', return_value='k'):
            with self.assertRaises(SystemExit) as raised:
                cli.main()
        self.assertIn('loop-guard', str(raised.exception))


class ClaudeAdapterTests(unittest.TestCase):
    """T12.2: bare mode, local gateway only, nothing from the personal install or credentials."""

    def setUp(self):
        self.adapter = adapters.adapter('claude-code')
        self.spec = {'suite': 'v1', 'frontend': 'claude-code 2.1.316', 'alias': 'local-coder-spark',
                     'agent': 'claude-code', 'model': {'id': 'local-coder-spark', 'maxTokens': 16384}, 'image': IMAGE,
                     'task': 'example', 'task_sha256': 'a' * 64, 'trial': 1, 'timeout_s': 1800}

    def invocation(self, **spec_extra):
        request = adapters.RunRequest(CASE, {**self.spec, **spec_extra}, TASK, Path('/tmp/attempt'), 'task-key')
        with patch.object(self.adapter, 'installed', return_value=True), \
                patch.object(self.adapter, 'require_installed', return_value=Path('/pinned/claude')):
            return self.adapter.invocation(request)

    def test_an_agent_that_is_not_pinned_cannot_run(self):
        request = adapters.RunRequest(CASE, self.spec, TASK, Path('/tmp/attempt'), 'task-key')
        with patch.object(self.adapter, 'installed', return_value=False):
            with self.assertRaises(adapters.AdapterError) as raised:
                self.adapter.invocation(request)
        self.assertIn('pinned', str(raised.exception))

    def test_bare_mode_with_no_settings_no_mcp_no_slash_commands_and_no_session_persistence(self):
        argv = list(self.invocation().argv)
        tail = argv[argv.index('/opt/claude') + 1:]
        self.assertIn('--bare', tail)
        self.assertIn('--no-session-persistence', tail)
        self.assertEqual(tail[tail.index('--setting-sources') + 1], '')
        self.assertEqual(tail[tail.index('--mcp-config') + 1], '{"mcpServers":{}}')
        self.assertIn('--strict-mcp-config', tail)
        self.assertIn('--disable-slash-commands', tail)
        self.assertEqual(tail[tail.index('--tools') + 1], 'Read,Edit,Write,Bash')
        self.assertEqual(tail[tail.index('--output-format') + 1], 'stream-json')

    def test_every_model_name_claude_may_ask_for_is_pinned_to_the_alias(self):
        env = self.invocation().container_env()
        for name in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                     'ANTHROPIC_DEFAULT_OPUS_MODEL'):
            self.assertIn(f'{name}=local-coder-spark', env, f'{name} is unpinned: it could name another model')

    def test_the_sandbox_is_the_same_for_every_agent(self):
        pi = [part for part in adapters.adapter('pi').invocation(
            adapters.RunRequest(CASE, GOLDEN['plain']['spec'], TASK, Path('/tmp/a'), 'k')).argv
            if part.startswith(('--memory', '--cpus', '--pids', '--cap-drop', '--read-only', '--security'))]
        claude = [part for part in self.invocation().argv
                  if part.startswith(('--memory', '--cpus', '--pids', '--cap-drop', '--read-only', '--security'))]
        self.assertEqual(pi, claude, 'a comparison where one Agent had more resources is not a comparison')

    def test_no_credential_is_read_from_the_environment(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'personal-secret',
                                     'LITELLM_MASTER_KEY': 'master-secret',
                                     'OPENAI_API_KEY': 'cloud-secret'}, clear=False):
            invocation = self.invocation()
        joined = json.dumps(list(invocation.argv)) + json.dumps(invocation.env)
        for secret in ('personal-secret', 'master-secret', 'cloud-secret'):
            self.assertNotIn(secret, joined)
        self.assertIn('ANTHROPIC_API_KEY', invocation.container_env())
        self.assertEqual(invocation.env, {'ANTHROPIC_API_KEY': 'task-key'})

    def test_the_cloud_alias_never_appears(self):
        invocation = self.invocation()
        self.assertNotIn('cloud-alias', json.dumps([list(invocation.argv), invocation.env,
                                                    list(invocation.container_env())]))

    def test_a_provider_override_is_refused_because_it_is_a_pi_object(self):
        with self.assertRaises(adapters.AdapterError):
            self.adapter.model_for('local-coder-spark', False, {'baseUrl': 'http://127.0.0.1:9', 'models': []})

    def test_thinking_is_refused_because_no_switch_was_verified(self):
        with self.assertRaises(adapters.AdapterError):
            self.adapter.model_for('local-coder-spark', True)

    def test_stream_json_is_read_into_the_common_result(self):
        events = [{'type': 'system'},
                  {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                      {'type': 'tool_use', 'id': 't1', 'name': 'Read'}]}},
                  {'type': 'user', 'message': {'role': 'user', 'content': [
                      {'type': 'tool_result', 'tool_use_id': 't1', 'is_error': False}]}},
                  {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                      {'type': 'tool_use', 'id': 't2', 'name': 'Bash'}]}},
                  {'type': 'user', 'message': {'role': 'user', 'content': [
                      {'type': 'tool_result', 'tool_use_id': 't2', 'is_error': True}]}},
                  {'type': 'result', 'subtype': 'success', 'num_turns': 2,
                   'usage': {'input_tokens': 10, 'output_tokens': 4}}]
        observed = self.adapter.parse('\n'.join(json.dumps(e) for e in events) + '\nnot json', '')
        self.assertEqual(observed['tools'], {'Read': 1, 'Bash': 1})
        self.assertEqual(observed['tool_errors'], 1)
        self.assertEqual(observed['assistant_turns'], 2)
        self.assertEqual(observed['stop_reason'], 'success')
        self.assertEqual(observed['usage'], {'input_tokens': 10, 'output_tokens': 4})

    def test_an_unpaired_tool_use_is_not_counted_as_completed(self):
        events = [{'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 't1', 'name': 'Edit'}]}}]
        self.assertEqual(self.adapter.parse(json.dumps(events[0]), '')['tools'], {})


class CodexAdapterTests(unittest.TestCase):
    """T12.6: the recorded fixture shape, a tree pin, and `apply_patch` probed rather than assumed."""

    def setUp(self):
        self.adapter = adapters.adapter('codex')
        self.spec = {'suite': 'v1', 'frontend': 'codex 0.165.0', 'alias': 'local-coder-spark',
                     'agent': 'codex', 'model': {'id': 'local-coder-spark', 'contextWindow': 262144}, 'image': IMAGE,
                     'task': 'example', 'task_sha256': 'a' * 64, 'trial': 1, 'timeout_s': 1800}

    def invocation(self, **spec_extra):
        request = adapters.RunRequest(CASE, {**self.spec, **spec_extra}, TASK, Path('/tmp/attempt'), 'task-key')
        with patch.object(self.adapter, 'installed', return_value=True), \
                patch.object(self.adapter, 'require_installed', return_value=Path('/pinned/codex')):
            return self.adapter.invocation(request)

    def events(self, *items, usage=None):
        lines = [json.dumps({'type': 'item.completed', 'item': item}) for item in items]
        if usage is not None:
            lines.append(json.dumps({'type': 'turn.completed', 'usage': usage}))
        return '\n'.join(lines) + '\n'

    def test_the_command_is_the_recorded_fixture_shape(self):
        argv = list(self.invocation().argv)
        self.assertEqual(argv[argv.index('--entrypoint') + 1], '/opt/codex/bin/codex')
        tail = argv[argv.index(IMAGE) + 1:]
        self.assertEqual(tail[0], 'exec')
        for flag in ('--strict-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
                     '--dangerously-bypass-approvals-and-sandbox', '--color', 'never', '--json'):
            self.assertIn(flag, tail)
        self.assertIn('Do not commit', tail[-1])

    def test_the_release_is_mounted_read_only_and_its_state_is_written_elsewhere(self):
        argv = list(self.invocation().argv)
        joined = ' '.join(argv[i + 1] for i, part in enumerate(argv) if part == '-v')
        self.assertIn(f'{self.adapter.install_dir}:/opt/codex:ro', joined)
        self.assertIn('/tmp/attempt/agent-state:/state:rw', joined)
        self.assertIn('/tmp/attempt/workspace:/work:rw', joined)
        self.assertIn('CODEX_HOME=/state', ' '.join(self.invocation().container_env()))

    def test_the_config_names_the_alias_the_loopback_gateway_and_the_responses_api(self):
        with tempfile.TemporaryDirectory() as where:
            self.adapter.prepare(self.spec, Path(where))
            config = (Path(where) / 'config.toml').read_text()
        self.assertIn('model = "local-coder-spark"', config)
        self.assertIn(f'base_url = "{declared.gateway_url(SITE, "/v1")}"', config)
        self.assertIn('wire_api = "responses"', config)
        self.assertIn('env_key = "LITELLM_TASK_KEY"', config)
        self.assertNotIn('api_key', config)

    def test_the_config_turns_off_everything_that_could_reach_the_vendor(self):
        with tempfile.TemporaryDirectory() as where:
            self.adapter.prepare(self.spec, Path(where))
            config = (Path(where) / 'config.toml').read_text()
        for line in ('web_search = "disabled"', 'check_for_update_on_startup = false', 'apps = false',
                     'multi_agent = false', 'apply_patch_freeform = false'):
            self.assertIn(line, config)

    def test_a_personal_codex_home_is_never_read(self):
        env = self.invocation().container_env()
        self.assertIn('CODEX_HOME=/state', env)
        self.assertNotIn('OPENAI_API_KEY', ' '.join(env))
        self.assertEqual(self.invocation().env, {'LITELLM_TASK_KEY': 'task-key'})

    def test_a_non_loopback_gateway_refuses_to_start(self):
        with self.assertRaises(adapters.AdapterError):
            self.invocation(base_url='http://127.0.0.1.evil.test:14000/v1')

    def test_the_sandbox_is_the_same_for_codex_as_for_pi(self):
        pi = [part for part in adapters.adapter('pi').invocation(
            adapters.RunRequest(CASE, GOLDEN['plain']['spec'], TASK, Path('/tmp/a'), 'k')).argv
            if part.startswith(('--memory', '--cpus', '--pids', '--cap-drop', '--read-only', '--security'))]
        codex = [part for part in self.invocation().argv
                 if part.startswith(('--memory', '--cpus', '--pids', '--cap-drop', '--read-only', '--security'))]
        self.assertEqual(pi, codex)

    def test_events_are_read_into_the_common_result(self):
        trace = self.events({'type': 'agent_message'}, {'type': 'command_execution', 'exit_code': 0},
                            {'type': 'file_change', 'status': 'completed'},
                            usage={'input_tokens': 12, 'output_tokens': 3})
        observed = self.adapter.parse(trace, '')
        self.assertEqual(observed['tools'], {'agent_message': 1, 'command_execution': 1, 'file_change': 1})
        self.assertEqual(observed['tool_errors'], 0)
        self.assertEqual(observed['assistant_turns'], 1)
        self.assertEqual(observed['stop_reason'], 'completed')
        self.assertEqual(observed['usage'], {'input_tokens': 12, 'output_tokens': 3})

    def test_a_failed_command_or_a_failed_change_is_a_tool_error(self):
        trace = self.events({'type': 'command_execution', 'exit_code': 1},
                            {'type': 'file_change', 'status': 'failed'}) + json.dumps({'type': 'turn.failed'})
        observed = self.adapter.parse(trace, '')
        self.assertEqual(observed['tool_errors'], 3)
        self.assertIsNone(observed['stop_reason'])

    def test_unparsable_lines_are_ignored_not_fatal(self):
        self.assertEqual(self.adapter.parse('not json\n[]\n"a string"\n', '')['tools'], {})

    def test_apply_patch_is_native_only_when_a_file_change_completed(self):
        trace = self.events({'type': 'file_change', 'status': 'completed'})
        self.assertEqual(self.adapter.probe_verdict('apply_patch', trace, '', 0),
                         ('native', 'a file_change item completed'))
        value, reason = self.adapter.probe_verdict('apply_patch', self.events(
            {'type': 'command_execution', 'exit_code': 0}), '', 0)
        self.assertEqual(value, 'unverified')
        self.assertIn('file_change', reason)

    def test_reads_and_edits_are_emulated_because_codex_has_no_read_tool(self):
        trace = self.events({'type': 'command_execution', 'exit_code': 0},
                            {'type': 'file_change', 'status': 'completed'})
        value, reason = self.adapter.probe_verdict('local_alias_read_edit_bash', trace, '', 0)
        self.assertEqual(value, 'emulated')
        self.assertIn('no distinct read tool', reason)

    def test_a_probe_that_never_finished_is_unsupported_not_unverified(self):
        value, reason = self.adapter.probe_verdict('structured_noninteractive', '', 'boom', 1)
        self.assertEqual(value, 'unsupported')
        self.assertIn('returncode 1', reason)

    def test_a_turn_that_completed_but_crashed_after_is_not_a_passing_probe(self):
        trace = self.events({'type': 'agent_message'}, usage={'output_tokens': 1})
        value, reason = self.adapter.probe_verdict('structured_noninteractive', trace, 'panic', 1)
        self.assertEqual(value, 'unsupported')
        self.assertIn('returncode 1', reason)

    def test_an_unprobed_feature_is_refused_rather_than_guessed(self):
        with self.assertRaises(adapters.AdapterError):
            self.adapter.probe_verdict('skills', '', '', 0)

    def test_the_probe_asks_for_the_smallest_thing_that_proves_the_feature(self):
        request = adapters.RunRequest(CASE, self.spec, TASK, Path('/tmp/attempt'), 'task-key')
        with patch.object(self.adapter, 'installed', return_value=True), \
                patch.object(self.adapter, 'require_installed', return_value=Path('/pinned/codex')):
            probe = self.adapter.probe_invocation('apply_patch', request)
        ask = probe.argv[-1]
        self.assertIn('patched.txt', ask)
        self.assertLess(probe.timeout_s, self.spec['timeout_s'])
        self.assertIsNone(self.adapter.probe_invocation('skills', request))


class TreePinTests(unittest.TestCase):
    """A release tree is pinned as a tree: replacing a sibling is the same attack as the entrypoint."""

    def setUp(self):
        self.where = tempfile.TemporaryDirectory()
        self.addCleanup(self.where.cleanup)
        self.root = Path(self.where.name) / 'codex'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'bin' / 'codex').write_text('#!/bin/sh\necho codex-cli 9.9.9\n')
        (self.root / 'codex-path').mkdir()
        (self.root / 'codex-path' / 'rg').write_text('ripgrep')
        (self.root / 'codex').symlink_to('bin/codex')
        self.pin = Path(self.where.name) / 'pin.json'
        self.pin.write_text(json.dumps({'version': '9.9.9', 'tree_sha256': adapters.tree_digest(self.root),
                                        'entrypoint': 'bin/codex'}))

        class Pinned(adapters.BaseAdapter):
            id = 'pinned-tree'
            install = self.root / 'bin' / 'codex'
            install_dir = self.root
            pin_path = self.pin

        self.adapter = Pinned()

    def test_the_pinned_tree_runs(self):
        self.assertEqual(self.adapter.require_installed(), self.root / 'bin' / 'codex')

    def test_a_replaced_sibling_is_refused(self):
        (self.root / 'codex-path' / 'rg').write_text('trojan')
        with self.assertRaises(adapters.AdapterError) as raised:
            self.adapter.require_installed()
        self.assertIn('tree', str(raised.exception))
        self.assertIn('pin-agent.py', str(raised.exception))

    def test_a_replaced_entrypoint_is_refused(self):
        (self.root / 'bin' / 'codex').write_text('#!/bin/sh\necho other\n')
        with self.assertRaises(adapters.AdapterError):
            self.adapter.require_installed()

    def test_a_renamed_file_is_refused_even_with_the_same_bytes(self):
        (self.root / 'codex-path' / 'rg').rename(self.root / 'codex-path' / 'grep')
        with self.assertRaises(adapters.AdapterError):
            self.adapter.require_installed()

    def test_a_symlink_is_pinned_by_its_target_not_its_contents(self):
        (self.root / 'codex').unlink()
        (self.root / 'codex').symlink_to('/somewhere/else')
        with self.assertRaises(adapters.AdapterError):
            self.adapter.require_installed()

    def test_a_pin_naming_neither_digest_cannot_run(self):
        self.pin.write_text(json.dumps({'version': '9.9.9'}))
        with self.assertRaises(adapters.AdapterError) as raised:
            self.adapter.require_installed()
        self.assertIn('tree_sha256', str(raised.exception))

    def test_the_pinned_codex_on_disk_matches_its_tracked_pin(self):
        chosen = adapters.adapter('codex')
        if not chosen.installed():
            self.skipTest(f'{chosen.install} is not installed here')
        chosen.require_installed()
        self.assertEqual(chosen.pin()['version'], '0.165.0')
        self.assertEqual(chosen.pin()['entrypoint'], 'bin/codex')

    def test_the_check_command_makes_the_same_comparison_as_a_run(self):
        pin_agent = importlib.util.spec_from_file_location('pin_agent', ROOT / 'scripts/pin-agent.py')
        module = importlib.util.module_from_spec(pin_agent)
        pin_agent.loader.exec_module(module)
        with contextlib.redirect_stdout(io.StringIO()):
            module.check(self.adapter, self.root)
        (self.root / 'codex-path' / 'rg').write_text('trojan')
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(io.StringIO()):
            module.check(self.adapter, self.root)
        message = str(raised.exception)
        self.assertIn('not the tree pinned', message)
        self.assertIn('pin-agent.py', message)


class GatewayOnlyTests(unittest.TestCase):
    """`gateway_only` is a check, not a comment (U3, ADR 0005: the exception does not widen)."""

    def test_loopback_is_accepted_and_anything_else_is_refused(self):
        adapter = adapters.adapter('claude-code')
        for url in ('http://127.0.0.1:14000', 'http://localhost:14000'):
            self.assertEqual(adapter.check_gateway_only(url), url)
        for url in ('https://api.anthropic.com', 'http://10.0.0.5:14000', 'http://example.com:14000',
                    'http://192.168.1.7'):
            with self.subTest(url=url):
                with self.assertRaises(adapters.AdapterError):
                    adapter.check_gateway_only(url)

    def test_a_spec_cannot_send_an_agent_to_another_host(self):
        adapter = adapters.adapter('claude-code')
        request = adapters.RunRequest(CASE, {'alias': 'local-coder-spark', 'image': IMAGE, 'timeout_s': 1,
                                             'base_url': 'http://203.0.113.9:14000'}, TASK, Path('/tmp/a'), 'k')
        with patch.object(adapter, 'installed', return_value=True), \
                patch.object(adapter, 'require_installed', return_value=Path('/pinned/claude')):
            with self.assertRaises(adapters.AdapterError):
                adapter.invocation(request)

    def test_loopback_helper_rejects_lookalikes(self):
        for url in ('http://127.0.0.1.evil.test', 'http://[::2]', 'http://10.1.2.3', 'not a url'):
            self.assertFalse(adapters.loopback(url), url)
        self.assertTrue(adapters.loopback('http://127.0.0.1:14000'))


class AllowedPathTests(unittest.TestCase):
    """T12.3 / X16: the relay allowlist is per adapter, because /v1/chat/completions alone locks out
    every Agent that is not pi."""

    def test_each_adapter_declares_the_paths_its_protocol_needs(self):
        self.assertEqual(adapters.adapter('pi').allowed_paths(), ('/v1/chat/completions',))
        self.assertEqual(adapters.adapter('claude-code').allowed_paths(), ('/v1/messages',))

    def test_the_endpoint_is_always_in_the_allowlist(self):
        for agent_id, adapter in adapters.ADAPTERS.items():
            with self.subTest(agent=agent_id):
                self.assertIn(adapter.endpoint, adapter.allowed_paths())

    def test_an_allowlist_is_paths_only_and_never_a_host(self):
        for agent_id, adapter in adapters.ADAPTERS.items():
            with self.subTest(agent=agent_id):
                for path in adapter.allowed_paths():
                    self.assertTrue(path.startswith('/'), f'{agent_id} allows {path!r}, which is not a path')
                    self.assertNotIn('://', path)

    def test_the_allowlist_comes_from_the_declaration_not_the_code(self):
        with patch('agentiac.declared.load', return_value=type('G', (), {
                'agents': [{'id': 'claude-code', 'allowed_paths': ['/v1/messages', '/v1/messages/count_tokens']}]})()):
            self.assertEqual(adapters.adapter('claude-code').allowed_paths(),
                             ('/v1/messages', '/v1/messages/count_tokens'))

    def test_an_agent_that_is_not_declared_has_no_allowlist(self):
        with patch('agentiac.declared.load', return_value=type('G', (), {'agents': []})()):
            with self.assertRaises(adapters.AdapterError):
                adapters.adapter('claude-code').allowed_paths()


class PinTests(unittest.TestCase):
    """T12.2 / U4: the automated path runs pinned bytes, and the pin is the thing that says which."""

    def setUp(self):
        self.adapter = adapters.adapter('claude-code')

    def require_bytes(self):
        """A gitignored install is a machine fact: control checks it, a clean clone has nothing to check."""
        if not self.adapter.installed():
            self.skipTest(f'{self.adapter.install} is not installed here')

    def test_the_pinned_bytes_are_the_bytes_the_pin_names(self):
        self.require_bytes()
        pin = self.adapter.pin()
        self.assertTrue(Path(self.adapter.install).is_file(), f'{self.adapter.install} is missing')
        self.assertEqual(adapters.sha256_file(self.adapter.install), pin['sha256'],
                         'the pinned Agent was edited in place; re-run scripts/pin-agent.py')

    def test_the_pinned_binary_reports_the_pinned_version(self):
        self.require_bytes()
        self.assertEqual(self.adapter.version().split()[0], self.adapter.pin()['version'],
                         'the pin names one version and the bytes report another')

    def test_the_declaration_points_at_the_pin_the_adapter_reads(self):
        entry = self.adapter.declaration()
        self.assertEqual(entry['pin'], str(Path(self.adapter.pin_path).relative_to(SITE)))
        self.assertTrue(str(self.adapter.install).startswith(str(SITE / entry['install']) + os.sep),
                        'the pinned binary is not under the directory the declaration names')

    def test_bytes_that_do_not_match_the_pin_cannot_run(self):
        with patch.object(adapters, 'sha256_file', return_value='dead' * 16), \
                patch.object(self.adapter, 'pin', return_value={'sha256': '0' * 64}), \
                patch.object(self.adapter, 'installed', return_value=True):
            with self.assertRaises(adapters.AdapterError) as raised:
                self.adapter.require_installed()
        self.assertIn('pin-agent.py', str(raised.exception))

    def test_a_missing_pin_file_stops_the_agent(self):
        with patch.object(self.adapter, 'pin_path', ROOT / 'agents/nope/pin.json'), \
                patch.object(self.adapter, 'installed', return_value=True):
            with self.assertRaises(adapters.AdapterError) as raised:
                self.adapter.require_installed()
        self.assertIn('no readable pin', str(raised.exception))

    def test_a_pin_with_no_hash_stops_the_agent(self):
        with patch.object(self.adapter, 'pin', return_value={'version': '2.1.316'}):
            with self.assertRaises(adapters.AdapterError):
                self.adapter.require_installed()

    def test_an_agent_pinned_by_lockfile_is_not_repinned_by_bytes(self):
        self.assertIsNone(adapters.adapter('pi').pin_path)
        with self.assertRaises(adapters.AdapterError):
            adapters.adapter('pi').pin()

    def test_sha256_file_matches_the_whole_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'blob'
            path.write_bytes(b'x' * (1 << 20) + b'yesterday')
            self.assertEqual(adapters.sha256_file(path), hashlib.sha256(path.read_bytes()).hexdigest())


class CapabilityTests(unittest.TestCase):
    """T12.4 / PA §12.3: a value is only believed at the version it was probed at."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.now = 1_800_000_000

    def row(self, agent_id='pi', version='1.3.0'):
        return adapters.capabilities_for(agent_id, version, root=self.root, now=self.now)

    def test_nothing_probed_is_unverified_for_every_feature(self):
        row = self.row()
        self.assertEqual(set(row), set(adapters.CAPABILITIES))
        self.assertEqual({value['value'] for value in row.values()}, {'unverified'})
        self.assertEqual({value['reason'] for value in row.values()}, {adapters.UNPROBED})

    def test_a_recorded_value_is_believed_and_carries_version_and_time(self):
        adapters.record_capability('pi', '1.3.0', 'skills', 'unsupported', 'run with --no-skills',
                                   root=self.root, now=self.now)
        entry = self.row()['skills']
        self.assertEqual(entry['value'], 'unsupported')
        self.assertEqual(entry['version'], '1.3.0')
        self.assertEqual(entry['probed_at'], self.now)

    def test_a_new_version_returns_the_row_to_unverified(self):
        adapters.record_capability('pi', '1.3.0', 'skills', 'unsupported', 'run with --no-skills',
                                   root=self.root, now=self.now)
        entry = self.row(version='1.4.0')['skills']
        self.assertEqual(entry['value'], 'unverified')
        self.assertIn('1.3.0', entry['reason'])
        self.assertIn('1.4.0', entry['reason'])

    def test_an_old_probe_is_not_evidence(self):
        adapters.record_capability('pi', '1.3.0', 'skills', 'unsupported', 'note', root=self.root,
                                   now=self.now - 365 * 86400)
        self.assertEqual(self.row()['skills']['value'], 'unverified')
        self.assertIn('days old', self.row()['skills']['reason'])

    def test_a_capability_outside_the_matrix_is_refused(self):
        with self.assertRaises(ValueError):
            adapters.record_capability('pi', '1.3.0', 'web_search', 'native', root=self.root)

    def test_a_value_outside_the_four_is_refused(self):
        for value in ('yes', 'partial', 'NATIVE', ''):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    adapters.record_capability('pi', '1.3.0', 'skills', value, root=self.root)

    def test_a_value_without_a_version_is_refused(self):
        for version in (None, ''):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    adapters.record_capability('pi', version, 'skills', 'native', root=self.root)

    def test_a_corrupt_record_reads_as_unprobed_rather_than_crashing(self):
        adapters.record_capability('pi', '1.3.0', 'skills', 'native', root=self.root, now=self.now)
        adapters.capability_path(self.root).write_text('{not json')
        self.assertEqual(self.row()['skills']['value'], 'unverified')

    def test_a_probe_that_observed_the_tools_records_native(self):
        events = '\n'.join(json.dumps(event) for event in [
            {'type': 'tool_execution_end', 'toolName': 'read', 'isError': False},
            {'type': 'tool_execution_end', 'toolName': 'edit', 'isError': False},
            {'type': 'tool_execution_end', 'toolName': 'bash', 'isError': False},
            {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop',
                                                'usage': {'output_tokens': 3}}}])
        adapter = adapters.adapter('pi')
        request = adapters.RunRequest(CASE, {'alias': 'local-coder-spark', 'image': IMAGE, 'timeout_s': 1},
                                      TASK, Path('/tmp/a'), 'k')
        with patch.object(adapter, 'installed', return_value=True), \
                patch.object(adapter, 'version', return_value='1.3.0'), \
                patch.object(adapters, 'ROOT', self.root):
            outcome = adapters.probe(adapter, 'local_alias_read_edit_bash', request,
                                     lambda argv, env, timeout: (events, '', 0))
        self.assertEqual(outcome['value'], 'native')
        self.assertEqual(self.row()['local_alias_read_edit_bash']['value'], 'native')

    def test_a_probe_that_did_not_observe_them_records_unverified_instead_of_passing(self):
        events = json.dumps({'type': 'tool_execution_end', 'toolName': 'read', 'isError': False})
        adapter = adapters.adapter('pi')
        request = adapters.RunRequest(CASE, {'alias': 'local-coder-spark', 'image': IMAGE, 'timeout_s': 1},
                                      TASK, Path('/tmp/a'), 'k')
        with patch.object(adapter, 'installed', return_value=True), \
                patch.object(adapter, 'version', return_value='1.3.0'), \
                patch.object(adapters, 'ROOT', self.root):
            outcome = adapters.probe(adapter, 'local_alias_read_edit_bash', request,
                                     lambda argv, env, timeout: (events, '', 0))
        self.assertEqual(outcome['value'], 'unverified')
        self.assertIn('bash', outcome['reason'])

    def test_an_unpinned_agent_raises_and_records_nothing(self):
        adapter = adapters.adapter('claude-code')
        request = adapters.RunRequest(CASE, {'alias': 'local-coder-spark', 'image': IMAGE, 'timeout_s': 1},
                                      TASK, Path('/tmp/a'), 'k')
        with patch.object(adapter, 'installed', return_value=False):
            with self.assertRaises(adapters.AdapterError):
                adapters.probe(adapter, 'structured_noninteractive', request, lambda *a: ('', '', 0))
        self.assertEqual(
            adapters.capabilities_for('claude-code', '2.1.316', root=self.root)['structured_noninteractive']['value'],
            'unverified')

    def test_a_feature_with_no_probe_for_that_agent_is_unverified_not_unsupported(self):
        adapter = adapters.adapter('pi')
        request = adapters.RunRequest(CASE, {'alias': 'a', 'image': IMAGE, 'timeout_s': 1}, TASK, Path('/tmp/a'), 'k')
        with patch.object(adapter, 'installed', return_value=True):
            outcome = adapters.probe(adapter, 'apply_patch', request, lambda *a: ('', '', 0))
        self.assertEqual(outcome['value'], 'unverified')
        self.assertIn('no probe', outcome['reason'])


class SuiteWiringTests(unittest.TestCase):
    """The suite must go through the adapter, not around it."""

    def setUp(self):
        for item in (patch.object(adapters.BaseAdapter, 'installed', return_value=True),
                     patch.object(adapters.BaseAdapter, 'verify_pin', return_value=None)):
            item.start()
            self.addCleanup(item.stop)

    def harness(self):
        directory = tempfile.mkdtemp()
        data = Path(directory) / 'artifacts/suite'
        data.mkdir(parents=True)
        (data / 'runs').mkdir()
        tasks = Path(directory) / 'tasks'
        tasks.mkdir()
        (tasks / 'example.json').write_text(json.dumps(TASK))
        return data, tasks

    def test_run_case_asks_the_spec_which_agent_to_use(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'attempt-1'
            spec = {**GOLDEN['plain']['spec'], 'agent': 'claude-code'}
            with patch.object(cli.adapters, 'adapter', wraps=cli.adapters.adapter) as chosen:
                attempt(spec, folder)
            self.assertEqual(chosen.call_args_list[0].args[0], spec)

    def test_a_second_agent_gets_its_own_command_and_the_same_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'attempt-1'
            spec = {**GOLDEN['plain']['spec'], 'agent': 'claude-code', 'frontend': 'claude-code 2.1.316'}
            argv, _, _ = attempt(spec, folder)
        self.assertIn('--bare', argv)
        self.assertIn('/opt/claude', argv)
        self.assertIn('--memory=3g', argv)
        self.assertIn('--pids-limit=256', argv)

    def test_a_graded_attempt_records_the_suite_grading_capability(self):
        data, tasks = self.harness()
        with patch.object(cli, 'DATA', data), patch.object(cli, 'TASKS', tasks), \
                patch.object(cli.subprocess, 'check_output', side_effect=lambda command, **kw:
                             IMAGE + '\n' if command[:2] == ['docker', 'image'] else '1.3.0\n'), \
                patch.object(cli, 'load_task', return_value=TASK), \
                patch.object(cli, 'run_case', return_value={'passed': True, 'wall_s': 1.0}), \
                patch.object(cli, 'provenance', return_value={}), \
                patch.object(cli.adapters, 'record_capability') as recorded:
            provider = Path(data) / 'provider.json'
            provider.write_text(json.dumps({'baseUrl': 'http://127.0.0.1:14000', 'api': 'openai-completions',
                                            'apiKey': 'x', 'models': [{'id': 'eval'}]}))
            with patch.object(sys, 'argv', ['suite.py', 'run', '--alias', 'eval', '--task', 'example',
                                            '--trial', '0', '--provider-config', str(provider)]):
                cli.main()
        self.assertIn('suite_grading', [call.args[2] for call in recorded.call_args_list])
        self.assertEqual(recorded.call_args_list[0].args[0], 'pi')

    def test_an_infrastructure_failure_does_not_claim_the_agent_can_be_graded(self):
        data, tasks = self.harness()
        provider = Path(data) / 'provider.json'
        provider.write_text(json.dumps({'baseUrl': 'http://127.0.0.1:14000', 'api': 'openai-completions',
                                        'apiKey': 'x', 'models': [{'id': 'eval'}]}))
        with patch.object(cli, 'DATA', data), patch.object(cli, 'TASKS', tasks), \
                patch.object(cli.subprocess, 'check_output', side_effect=lambda command, **kw:
                             IMAGE + '\n' if command[:2] == ['docker', 'image'] else '1.3.0\n'), \
                patch.object(cli, 'load_task', return_value=TASK), \
                patch.object(cli, 'run_case', side_effect=OSError('docker is down')), \
                patch.object(cli, 'provenance', return_value={}), \
                patch.object(cli.adapters, 'record_capability') as recorded, \
                patch.object(sys, 'argv', ['suite.py', 'run', '--alias', 'eval', '--task', 'example',
                                           '--trial', '0', '--provider-config', str(provider)]):
            cli.main()
        recorded.assert_not_called()

    def test_the_agent_version_reaches_the_spec_and_the_provenance(self):
        data, tasks = self.harness()
        provider = Path(data) / 'provider.json'
        provider.write_text(json.dumps({'baseUrl': 'http://127.0.0.1:14000', 'api': 'openai-completions',
                                        'apiKey': 'x', 'models': [{'id': 'eval'}]}))
        with patch.object(cli, 'DATA', data), patch.object(cli, 'TASKS', tasks), \
                patch.object(cli.subprocess, 'check_output', side_effect=lambda command, **kw:
                             IMAGE + '\n' if command[:2] == ['docker', 'image'] else '9.9.9\n'), \
                patch.object(cli, 'load_task', return_value=TASK), \
                patch.object(cli, 'run_case', return_value={'passed': True}), \
                patch.object(cli, 'provenance', return_value={}) as provenance, \
                patch.object(cli.adapters, 'record_capability'), \
                patch.object(sys, 'argv', ['suite.py', 'run', '--alias', 'eval', '--task', 'example',
                                           '--trial', '0', '--provider-config', str(provider)]):
            cli.main()
        self.assertEqual(provenance.call_args.args[1], '9.9.9')
        rows = list(cli.latest_results(cli.Store(data / 'state.sqlite')))
        self.assertEqual(rows[0][0]['frontend'], 'pi 9.9.9')


class CredentialTests(unittest.TestCase):
    """Only the task key enters a container, under the name its Agent reads (PA §12, IP-01)."""

    def build(self, agent_id, alias='local-coder-spark'):
        adapter = adapters.adapter(agent_id)
        spec = {'suite': 'v1', 'frontend': f'{agent_id} 1', 'alias': alias, 'image': IMAGE, 'timeout_s': 60,
                'model': {'id': alias, 'contextWindow': 262144, 'maxTokens': 16384}}
        request = adapters.RunRequest(CASE, spec, TASK, Path('/tmp/attempt'), 'task-key')
        with patch.object(adapters.BaseAdapter, 'installed', return_value=True), \
                patch.object(adapters.BaseAdapter, 'require_installed', return_value=Path('/pinned/agent')):
            return adapter, request, adapter.invocation(request)

    def test_every_variable_passed_by_name_gets_its_value_from_the_invocation(self):
        """`-e NAME` copies the launching process: a name the invocation does not set is the operator's own."""
        for agent_id in adapters.ADAPTERS:
            with self.subTest(agent=agent_id):
                _, _, invocation = self.build(agent_id)
                bare = [name for name in invocation.container_env() if '=' not in name]
                self.assertTrue(bare)
                self.assertEqual(sorted(bare), sorted(invocation.env))

    def test_claude_code_receives_the_task_key_as_its_api_key(self):
        _, _, invocation = self.build('claude-code')
        self.assertEqual(invocation.env, {'ANTHROPIC_API_KEY': 'task-key'})
        environment = {**os.environ, 'ANTHROPIC_API_KEY': 'personal-secret', **invocation.env}
        self.assertEqual(environment['ANTHROPIC_API_KEY'], 'task-key')

    def test_mounts_reports_the_real_mounts(self):
        _, _, invocation = self.build('claude-code')
        self.assertEqual(invocation.mounts(), ('/pinned/agent:/opt/claude:ro', '/tmp/attempt/workspace:/work:rw'))
        self.assertEqual(invocation.as_dict()['mounts'], list(invocation.mounts()))

    def test_a_mount_after_the_entrypoint_is_a_mount(self):
        adapter = adapters.adapter('claude-code')
        invocation = adapter.build('n', IMAGE, '/opt/x', ['run'], [], ['/a:/a:ro'], 'k', 1, extra_mounts=['/b:/b:ro'])
        self.assertEqual(invocation.mounts(), ('/a:/a:ro', '/b:/b:ro'))


class ProbeArgvTests(unittest.TestCase):
    """A probe is the Agent's command with another question: one image, and nothing docker was told."""

    build = CredentialTests.build

    def agent_words(self, adapter, request, feature):
        with patch.object(adapters.BaseAdapter, 'installed', return_value=True), \
                patch.object(adapters.BaseAdapter, 'require_installed', return_value=Path('/pinned/agent')):
            probe = adapter.probe_invocation(feature, request)
        words = list(probe.argv)
        self.assertEqual(words.count(IMAGE), 1)
        return probe, words[words.index(IMAGE) + 1:]

    def test_claude_code_gets_its_own_arguments_and_the_probe_question(self):
        adapter, request, _ = self.build('claude-code')
        probe, words = self.agent_words(adapter, request, 'structured_noninteractive')
        self.assertEqual(words[0], '--bare')
        self.assertIn('Reply with exactly the word OK', words[words.index('--print') + 1])
        self.assertEqual(probe.env, {'ANTHROPIC_API_KEY': 'task-key'})

    def test_codex_gets_its_own_arguments_and_the_probe_question(self):
        adapter, request, _ = self.build('codex')
        with patch.object(adapter, 'base_url', return_value='http://127.0.0.1:14000/v1'):
            probe, words = self.agent_words(adapter, request, 'apply_patch')
        self.assertEqual(words[0], 'exec')
        self.assertNotIn('docker', words)
        self.assertIn('patched.txt', words[-1])
        self.assertEqual(probe.env, {'LITELLM_TASK_KEY': 'task-key'})


class CodexWindowTests(unittest.TestCase):
    def config(self, **extra):
        return adapters.adapter('codex').config_text({'alias': 'local-coder-spark', **extra})

    def test_the_window_is_the_target_s_and_not_a_default(self):
        self.assertIn('262144', self.config(model={'contextWindow': 262144}))

    def test_no_declared_window_is_refused_rather_than_guessed(self):
        with self.assertRaises(adapters.AdapterError):
            self.config(model={'id': 'x'})


class CapabilityWriterTests(unittest.TestCase):
    def test_concurrent_writers_lose_nothing_and_raise_nothing(self):
        features = adapters.CAPABILITIES[:4]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            errors = []

            def write(feature):
                try:
                    for _ in range(10):
                        adapters.record_capability('pi', '1.3.0', feature, 'native', root=root)
                except Exception as error:     # noqa: BLE001
                    errors.append(error)

            threads = [threading.Thread(target=write, args=(feature,)) for feature in features]
            [thread.start() for thread in threads]
            [thread.join() for thread in threads]
            self.assertEqual(errors, [])
            self.assertEqual(set(adapters.load_capabilities(root)['pi']), set(features))


class ProbeVerdictTests(unittest.TestCase):
    """A tool the model did not reach for was not shown to be missing: `unsupported` needs evidence."""

    def test_pi_without_an_edit_is_unverified(self):
        adapter = adapters.adapter('pi')
        with patch.object(adapter, 'parse', return_value={'tools': {'read': 1, 'bash': 1}}):
            self.assertEqual(adapter.probe_verdict('local_alias_read_edit_bash', '', '', 0)[0], 'unverified')

    def test_claude_code_without_an_edit_is_unverified(self):
        adapter = adapters.adapter('claude-code')
        with patch.object(adapter, 'parse', return_value={'tools': {'Bash': 1}}):
            self.assertEqual(adapter.probe_verdict('local_alias_read_edit_bash', '', '', 0)[0], 'unverified')

    def test_codex_without_a_file_change_is_unverified(self):
        adapter = adapters.adapter('codex')
        with patch.object(adapter, 'parse', return_value={'tools': {'command_execution': 1}}):
            for feature in ('apply_patch', 'local_alias_read_edit_bash'):
                self.assertEqual(adapter.probe_verdict(feature, '', '', 0)[0], 'unverified', feature)

    def test_every_tool_probe_names_the_tool_and_edits_a_file_that_exists(self):
        self.assertIn('not a shell command', adapters.EDIT_PROBE)
        self.assertIn('probe.txt', adapters.EDIT_PROBE)


class ClaudeParityTests(unittest.TestCase):
    """T12.5 compares two Agents, so what differs must be the Agent and not its limits."""

    def setUp(self):
        self.adapter = adapters.adapter('claude-code')

    def test_the_limits_are_the_ones_pi_gets_for_the_same_alias(self):
        entry = {'id': 'x', 'contextWindow': 262144, 'maxTokens': 16384}
        with patch('agent.model_entry', return_value=entry):
            model = self.adapter.model_for('local-coder-spark')
        self.assertEqual((model['contextWindow'], model['maxTokens']), (262144, 16384))

    def test_the_output_limit_comes_from_the_model_and_is_not_a_constant(self):
        spec = {'alias': 'a', 'image': IMAGE, 'timeout_s': 60, 'model': {'maxTokens': 12345}}
        request = adapters.RunRequest(CASE, spec, TASK, Path('/tmp/attempt'), 'k')
        with patch.object(adapters.BaseAdapter, 'installed', return_value=True), \
                patch.object(adapters.BaseAdapter, 'require_installed', return_value=Path('/pinned/claude')):
            invocation = self.adapter.invocation(request)
        self.assertIn('CLAUDE_CODE_MAX_OUTPUT_TOKENS=12345', invocation.argv)

    def test_the_reason_a_turn_stopped_is_read_from_stop_reason(self):
        line = json.dumps({'type': 'result', 'subtype': 'success', 'stop_reason': 'max_tokens'})
        self.assertEqual(self.adapter.parse(line, '')['stop_reason'], 'max_tokens')


if __name__ == '__main__':
    unittest.main()
