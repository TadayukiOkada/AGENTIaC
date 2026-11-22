"""The real pi binary compacting a split turn, against a backend that behaves like the one that failed.

tests/test_compaction_guard.py checks the bodies pi builds. This checks the outcome: does a session
end with a compaction entry on disk. The fake backend answers a summary request the way the model did
when asked to think: it spends the whole budget thinking and stops on `length`, so a summary request
the guard misses fails here just as it fails in production.
"""
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import paths  # noqa: E402
import agent  # noqa: E402

SITE = paths.root()

SUMMARY_PROMPT = 'You are a context summarization assistant.'
TRIGGER_USAGE = 120000  # over 98304, the trigger of the model below
TOOL = {'name': 'bash', 'command': "python3 -c \"print('x' * 39000)\""}  # ~9750 estimated tokens


def chunk(delta=None, finish=None, usage=None):
    body = {'id': 'c', 'object': 'chat.completion.chunk', 'model': 'local-coder',
            'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}] if usage is None else []}
    if usage is not None:
        body['usage'] = {'prompt_tokens': usage, 'completion_tokens': 10, 'total_tokens': usage + 10}
    return f'data: {json.dumps(body)}\n\n'.encode()


class Backend(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, script, always_fail=False):
        super().__init__(('127.0.0.1', 0), Handler)
        self.script, self.always_fail = list(script), always_fail
        self.summaries = []
        self.turns = 0


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        first = request['messages'][0]
        content = first['content'] if isinstance(first['content'], str) else first['content'][0]['text']
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if content.startswith(SUMMARY_PROMPT):
            self.summary(request)
        else:
            self.turn()
        self.wfile.write(b'data: [DONE]\n\n')

    def summary(self, request):
        user = request['messages'][1]['content']
        user = user if isinstance(user, str) else user[0]['text']
        thinking = request.get('chat_template_kwargs', {}).get('enable_thinking')
        self.server.summaries.append({'start': user[:14], 'thinking': thinking,
                                      'budget': request.get('max_tokens') or request.get('max_completion_tokens')})
        if thinking or self.server.always_fail:
            self.wfile.write(chunk({'reasoning_content': 'thinking about it'}))
            self.wfile.write(chunk(finish='length'))
        else:
            self.wfile.write(chunk({'content': 'the summary'}))
            self.wfile.write(chunk(finish='stop'))
        self.wfile.write(chunk(usage=1000))

    def turn(self):
        self.server.turns += 1
        kind, usage = self.server.script.pop(0) if self.server.script else ('text', 20000)
        if kind in ('tool', 'read'):
            name, arguments = ((TOOL['name'], {'command': TOOL['command']}) if kind == 'tool'
                               else ('read', {'path': 'notes.txt'}))
            call = {'index': 0, 'id': f'call_{self.server.turns}', 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(arguments)}}
            self.wfile.write(chunk({'tool_calls': [call]}))
            self.wfile.write(chunk(finish='tool_calls'))
        else:
            self.wfile.write(chunk({'content': 'done'}))
            self.wfile.write(chunk(finish='stop'))
        self.wfile.write(chunk(usage=usage))


@unittest.skipUnless(agent.NODE.exists() and agent.PI.exists(), 'the pinned pi install is not here')
class RealPiCompaction(unittest.TestCase):
    def session(self, script, always_fail=False, guard=True, compat=None):
        """Run pi at `high` against the fake backend: (backend, session entries, guard log)."""
        backend = Backend(script, always_fail)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        self.addCleanup(backend.server_close)
        self.addCleanup(backend.shutdown)
        routes = json.loads((SITE / 'gateway/local-coder.json').read_text())
        default_compat = next(
            (spec['compat'] for spec in routes['targets'].values() if isinstance(spec, dict) and 'compat' in spec),
            {'thinkingFormat': 'chat-template'},
        )
        model = {'id': 'local-coder', 'name': 'test', 'contextWindow': 131072, 'maxTokens': 16384,
                 'reasoning': True, 'compat': compat or default_compat}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / 'notes.txt').write_text('notes\n')
            config = json.loads((SITE / 'agents/pi/models.json').read_text())
            config['providers']['local-infra'].update(baseUrl=f'http://127.0.0.1:{backend.server_port}/v1', models=[model])
            (home / 'models.json').write_text(json.dumps(config))
            agent.write_settings(home, agent.compaction([model]))
            log = home / 'guard.jsonl'
            argv = [str(agent.NODE), str(agent.PI), '--no-extensions', '--provider', 'local-infra',
                    '--model', 'local-coder', '--thinking', 'high', '--session-dir', str(home / 'sessions'),
                    '-p', 'first task', 'second task']
            if guard:
                argv[2:2] = ['-e', str(agent.COMPACTION_GUARD)]
            env = dict(os.environ, PI_CODING_AGENT_DIR=str(home), PI_OFFLINE='1', NODE_NO_WARNINGS='1',
                       LITELLM_MASTER_KEY='test', AGENTIAC_COMPACTION_GUARD_LOG=str(log))
            result = subprocess.run(argv, cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            files = list((home / 'sessions').glob('*.jsonl'))
            self.assertEqual(len(files), 1, result.stderr[-800:])
            entries = [json.loads(line) for line in files[0].read_text().splitlines()]
            lines = log.read_text().splitlines() if log.exists() else []
        return backend, entries, [json.loads(line) for line in lines]

    # An earlier turn to summarize, then one long turn the cut lands inside.
    SPLIT = [('tool', 20000), ('text', 20000), ('tool', 30000), ('tool', 40000), ('tool', 50000),
             ('tool', TRIGGER_USAGE), ('text', 20000)]

    # Targets that declare pi's other chat_template_kwargs switch: a guard that knows only "chat-template"
    # leaves these to pi's own path (thinking high, summary spends the whole budget thinking).
    QWEN_COMPAT = {'thinkingFormat': 'qwen-chat-template'}

    def test_a_split_turn_compacts_on_the_qwen_chat_template_switch_too(self):
        backend, entries, log = self.session(self.SPLIT, compat=self.QWEN_COMPAT)
        compactions = [e for e in entries if e['type'] == 'compaction']
        self.assertEqual(len(compactions), 1, backend.summaries)
        self.assertIn('Turn Context (split turn)', compactions[0]['summary'])
        self.assertEqual([(s['start'], s['thinking'], s['budget']) for s in backend.summaries],
                         [('<conversation>', False, 16384), ('# Conversation', False, 16384)])
        self.assertEqual([(e['event'], e['split_turn']) for e in log], [('summarized', True)])

    def test_without_the_guard_a_qwen_chat_template_session_never_compacts(self):
        # pi's own path asks at thinking high, the backend spends the budget thinking, and no compaction is written.
        backend, entries, _ = self.session(self.SPLIT, guard=False, compat=self.QWEN_COMPAT)
        self.assertTrue(backend.summaries)
        self.assertEqual({s['thinking'] for s in backend.summaries}, {True})
        self.assertEqual([e for e in entries if e['type'] == 'compaction'], [])

    def test_without_the_guard_the_split_turn_is_never_compacted(self):
        backend, entries, _ = self.session(self.SPLIT, guard=False)
        self.assertTrue(backend.summaries)
        self.assertEqual([e for e in entries if e['type'] == 'compaction'], [])

    def test_a_split_turn_compacts_and_neither_summary_thought(self):
        backend, entries, log = self.session(self.SPLIT)
        compactions = [e for e in entries if e['type'] == 'compaction']
        self.assertEqual(len(compactions), 1, backend.summaries)
        self.assertIn('Turn Context (split turn)', compactions[0]['summary'])
        self.assertEqual([(s['start'], s['thinking'], s['budget']) for s in backend.summaries],
                         [('<conversation>', False, 16384), ('# Conversation', False, 16384)])
        self.assertEqual([(e['event'], e['split_turn']) for e in log], [('summarized', True)])

    def test_a_summary_that_keeps_failing_is_not_retried_at_every_check(self):
        steps = 12
        script = self.SPLIT[:4] + [('tool', TRIGGER_USAGE)] * steps + [('text', 20000)]
        backend, entries, log = self.session(script, always_fail=True)
        self.assertEqual([e for e in entries if e['type'] == 'compaction'], [])
        events = [e['event'] for e in log]
        self.assertIn('failed', events)
        self.assertIn('skipped', events)
        self.assertEqual(backend.turns, len(script))  # the session itself kept working
        # Twice as many checks as steps run without the backoff; one summary per failed check here.
        self.assertLessEqual(len(backend.summaries), steps // 2)
        # A failure never falls through to pi's own path, which would ask again with thinking on.
        self.assertEqual({s['thinking'] for s in backend.summaries}, {False})

    def test_a_second_compaction_still_lists_the_files_of_the_first(self):
        # pi drops the lists of a compaction an extension wrote, so the guard has to carry them.
        script = [('read', 20000), ('text', 20000)] + self.SPLIT[2:6] + self.SPLIT[3:6] + [('text', 20000)]
        backend, entries, _ = self.session(script)
        compactions = [e for e in entries if e['type'] == 'compaction']
        self.assertEqual(len(compactions), 2, backend.summaries)
        self.assertEqual(len(compactions[0]['details']['readFiles']), 1)
        self.assertEqual(compactions[1]['details']['readFiles'], compactions[0]['details']['readFiles'])


if __name__ == '__main__':
    unittest.main()
