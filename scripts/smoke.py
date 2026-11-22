#!/usr/bin/env python3
"""Ten-minute end-to-end smoke test of one gateway alias.

Checks the alias is listed, Chat Completions (plain and streamed tool call),
Anthropic Messages and Responses, then one pi read/edit/test repair in the
pinned evaluation container, graded independently. Prints one line per check.
Detailed request/response text stays in artifacts/smoke/ only.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
sys.path.insert(0, str(CODE / 'scripts'))
from agentiac import declared, route_aliases
from agentiac.secrets import read_key, redact  # noqa: E402
from agentiac.tasks import FIXTURES, sandbox  # noqa: E402
import agent  # noqa: E402

GATEWAY = declared.gateway_url(ROOT)
QUESTION = 'What is 17*23? Reply with the number only.'
READ_TOOL = {'type': 'function', 'function': {'name': 'read_file', 'description': 'Read a file',
             'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}}


def post(path, key, body, timeout=300):
    request = urllib.request.Request(GATEWAY + path, data=json.dumps(body).encode(), method='POST', headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'anthropic-version': '2023-06-01'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode()


def streamed_tool_call(text):
    """Join streamed Chat Completions deltas into (tool name, arguments)."""
    name, arguments = '', ''
    for line in text.splitlines():
        if not line.startswith('data: ') or line == 'data: [DONE]':
            continue
        for choice in json.loads(line[6:]).get('choices', []):
            for call in choice.get('delta', {}).get('tool_calls') or []:
                name += call.get('function', {}).get('name') or ''
                arguments += call.get('function', {}).get('arguments') or ''
    return name, arguments


def protocol_checks(alias, key):
    def listed():
        request = urllib.request.Request(GATEWAY + '/v1/models', headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(request, timeout=10) as response:
            return alias in [m['id'] for m in json.load(response)['data']], ''

    def chat():
        message = json.loads(post('/v1/chat/completions', key, {'model': alias, 'max_tokens': 1024,
                              'messages': [{'role': 'user', 'content': QUESTION}]}))['choices'][0]['message']
        reasoning = len(message.get('reasoning_content') or '')
        return '391' in (message.get('content') or ''), f'reasoning chars {reasoning}'

    def tool_stream():
        name, arguments = streamed_tool_call(post('/v1/chat/completions', key, {
            'model': alias, 'max_tokens': 1024, 'stream': True, 'tools': [READ_TOOL],
            'messages': [{'role': 'user', 'content': 'Read README.md with the tool.'}]}))
        return name == 'read_file' and json.loads(arguments or '{}').get('path', '').endswith('README.md'), name

    def messages():
        content = json.loads(post('/v1/messages', key, {'model': alias, 'max_tokens': 1024,
                              'messages': [{'role': 'user', 'content': QUESTION}]}))['content']
        return any('391' in (c.get('text') or '') for c in content), ''

    def responses():
        output = json.loads(post('/v1/responses', key, {'model': alias, 'max_output_tokens': 1024, 'input': QUESTION}))['output']
        text = ''.join(part.get('text', '') for item in output for part in item.get('content') or [])
        return '391' in text, ''

    return [('listed', listed), ('chat', chat), ('chat stream tool call', tool_stream),
            ('anthropic messages', messages), ('responses', responses)]


def pi_repair(alias, key, folder):
    """One pi read/edit/bash repair in the evaluation container; the grade comes from a separate sandbox run."""
    state, work, grader = folder / 'agent-state', folder / 'workspace', folder / 'grading'
    for path in (state, work, grader):
        path.mkdir(parents=True)
    config = json.loads((ROOT / 'agents/pi/models.json').read_text())
    config['providers']['local-infra']['models'] = [agent.model_entry(alias)]
    # IP-01 (T4.3): the container authenticates with the task key, never the master key.
    config['providers']['local-infra']['apiKey'] = '${LITELLM_TASK_KEY}'
    (state / 'models.json').write_text(json.dumps(config, indent=2) + '\n')
    filename, source, test = FIXTURES['python']
    (work / filename).write_text(source)
    (grader / 'test.py').write_text(test[2] + '\n')
    if sandbox(work, test).returncode == 0:
        raise RuntimeError('unrepaired fixture must fail')
    name = declared.resource('smoke-' + folder.parent.name, ROOT)  # unique: smokes of several aliases may run at once
    command = ['docker', 'run', '--rm', '--name', name, '--label', 'io.agent-infra.owner=agent-infra',
               '--network=host', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--memory=1g', '--memory-swap=1g', '--cpus=2', '--pids-limit=128',
               '--user', f'{os.getuid()}:{os.getgid()}', '--tmpfs', '/tmp:rw,exec,nosuid,size=128m',
               '-e', 'HOME=/tmp', '-e', 'PI_CODING_AGENT_DIR=/state', '-e', 'PI_OFFLINE=1',
               '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=/work', '-e', 'LITELLM_TASK_KEY',
               '-v', f'{agent.NODE}:/opt/node:ro', '-v', f'{agent.PI_TREE}:/app:ro',
               '-v', f'{state}:/state:rw', '-v', f'{work}:/work:rw', '-v', f'{grader}:/grader:ro',
               '-w', '/work', '--entrypoint', '/opt/node', declared.resource('eval:baseline', ROOT),
               '/app/node_modules/.bin/pi', '--offline', '--no-session', '--no-extensions', '--no-mcp',
               '--no-skills', '--no-context-files', '--no-prompt-templates', '--no-themes', '--no-approve',
               '--tools', 'read,edit,write,bash', '--provider', 'local-infra', '--model', alias, '--mode', 'json',
               '--print', 'Fix parser.py to decode unsigned little-endian 16-bit values and reject odd payload lengths '
               'with ValueError. Preserve the decode API. Read the source, edit it, then run python3 /grader/test.py '
               'with bash (the grader is read-only). Finish once it passes.']
    try:
        result = subprocess.run(command, env=dict(os.environ, LITELLM_TASK_KEY=key), capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        subprocess.run(['docker', 'stop', '-t', '5', name], capture_output=True, timeout=30)
        return False, 'pi timed out after 600 s'
    (folder / 'trace.jsonl').write_text(redact(result.stdout, [key]))
    (folder / 'stderr.txt').write_text(redact(result.stderr, [key]))
    graded = sandbox(work, test).returncode
    return result.returncode == 0 and graded == 0, f'pi rc {result.returncode}, independent test rc {graded}'


def task_key_reaches(alias):
    """True for the aliases gateway/route.py renders, which are the only ones the task key is allowed (T4.3)."""
    routes = json.loads((ROOT / 'gateway/local-coder.json').read_text())
    return alias in route_aliases.api_targets(routes, routes['default'])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--alias', default='local-coder')
    parser.add_argument('--no-pi', action='store_true', help='protocol checks only')
    args = parser.parse_args()
    os.umask(0o077)
    key = read_key(ROOT / 'secrets/gateway.env')
    folder = ROOT / 'artifacts/smoke' / f"{args.alias}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    folder.mkdir(parents=True)
    checks = protocol_checks(args.alias, key)
    if not args.no_pi and task_key_reaches(args.alias):
        task_key = read_key(ROOT / 'secrets/gateway.env', 'LITELLM_TASK_KEY')  # IP-01 (T4.3): the pi container's credential
        checks.append(('pi repair', lambda: pi_repair(args.alias, task_key, folder / 'pi')))
    elif not args.no_pi:
        print(f'skip  pi repair: the task key only reaches explicitly rendered inference aliases, not {args.alias}', flush=True)
    results = []
    for name, check in checks:
        started = time.monotonic()
        try:
            passed, detail = check()
        except Exception as error:  # report and continue: one line per check
            passed, detail = False, f'{type(error).__name__}: {error}'
        results.append({'check': name, 'passed': passed, 'seconds': round(time.monotonic() - started, 1), 'detail': detail})
        print(f"{'PASS' if passed else 'FAIL'}  {name:<24} {results[-1]['seconds']:>6.1f}s  {detail}", flush=True)
    summary = {'alias': args.alias, 'passed': all(r['passed'] for r in results), 'checks': results}
    (folder / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(f"{'PASS' if summary['passed'] else 'FAIL'}  {args.alias}: {sum(r['passed'] for r in results)}/{len(results)}  ({folder})")
    raise SystemExit(0 if summary['passed'] else 1)


if __name__ == '__main__':
    main()
