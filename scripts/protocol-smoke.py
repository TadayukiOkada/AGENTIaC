#!/usr/bin/env python3
"""Resumable local-only API tool-loop probes, with traces kept on control."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import declared  # noqa: E402
from agentiac.secrets import read_key, redact

SCRIPT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

SCHEMA = {'type': 'object', 'properties': {'bus': {'type': 'integer', 'enum': [2]}},
          'required': ['bus'], 'additionalProperties': False}
PROMPT = 'Call read_status with bus 2. Then report the exact status returned by the tool. Do not invent it.'
TOOL_RESULT = '{"status":"CAN_READY_73"}'


def unpack(protocol, events, stream):
    """Return a canonical assistant message/output after all streamed deltas."""
    if not stream:
        if protocol == 'chat':
            return events['choices'][0]['message']
        return events['output'] if protocol == 'responses' else events['content']
    if protocol == 'responses':
        for event in reversed(events):
            if event.get('type') == 'response.completed':
                return event['response']['output']
        raise ValueError('Responses stream has no completed event')
    if protocol == 'messages':
        blocks = {}
        stopped = False
        for event in events:
            if event.get('type') == 'content_block_start':
                block = dict(event['content_block'])
                if block.get('type') == 'tool_use':
                    block['input'] = ''; block['_json'] = True
                blocks[event['index']] = block
            elif event.get('type') == 'content_block_delta':
                block = blocks[event['index']]; delta = event['delta']
                for source, target in [('text', 'text'), ('partial_json', 'input'), ('thinking', 'thinking'), ('signature', 'signature')]:
                    if source in delta:
                        block[target] = block.get(target, '') + delta[source]
            elif event.get('type') == 'message_stop':
                stopped = True
        if not stopped:
            raise ValueError('Messages stream has no stop event')
        result = [blocks[i] for i in sorted(blocks)]
        for block in result:
            if block.pop('_json', False):
                block['input'] = json.loads(block['input'] or '{}')
        return result
    message = {'role': 'assistant', 'content': ''}; calls = {}; finished = False
    for event in events:
        if event.get('error'):
            raise ValueError('upstream stream error event')
        for choice in event.get('choices', []):
            finished |= choice.get('finish_reason') is not None
            delta = choice.get('delta', {})
            message['content'] += delta.get('content') or ''
            for call in delta.get('tool_calls', []):
                item = calls.setdefault(call['index'], {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                item['id'] += call.get('id') or ''
                for key in ('name', 'arguments'):
                    item['function'][key] += call.get('function', {}).get(key) or ''
    if not finished:
        raise ValueError('Chat stream has no finish event')
    if calls:
        message['tool_calls'] = [calls[i] for i in sorted(calls)]
    return message


def probe(base, key, model, protocol, stream):
    traces = []
    endpoint = {'chat': '/v1/chat/completions', 'responses': '/v1/responses', 'messages': '/v1/messages'}[protocol]
    if protocol == 'responses':
        tools = [{'type': 'function', 'name': 'read_status', 'description': 'Read CAN status.', 'parameters': SCHEMA}]
        history = [{'role': 'user', 'content': PROMPT}]
    else:
        tools = [{'name': 'read_status', 'description': 'Read CAN status.', 'input_schema': SCHEMA}] if protocol == 'messages' else [
            {'type': 'function', 'function': {'name': 'read_status', 'description': 'Read CAN status.', 'parameters': SCHEMA}}]
        history = [{'role': 'user', 'content': PROMPT}]
    for turn in range(2):
        payload = {'model': model, 'tools': tools, 'stream': stream,
                   'input' if protocol == 'responses' else 'messages': history,
                   'max_output_tokens' if protocol == 'responses' else 'max_tokens': 2048}
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}
        if protocol == 'messages':
            headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
        started = time.monotonic(); first = None
        request = urllib.request.Request(base + endpoint, data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                if stream:
                    events = []
                    for line in response:
                        if not line.startswith(b'data:'):
                            continue
                        data = line[5:].strip()
                        if data == b'[DONE]':
                            continue
                        if data:
                            if first is None:
                                first = time.monotonic() - started
                            events.append(json.loads(data))
                else:
                    events = json.load(response)
        except urllib.error.HTTPError as exc:
            # Never print upstream response bodies; error text remains local/redacted.
            detail = redact(exc.read().decode(errors='replace'), [key])
            raise RuntimeError(f'HTTP {exc.code}: {detail[:2000]}') from None
        traces.append({'request': payload, 'events': events, 'elapsed_s': time.monotonic() - started, 'first_event_s': first})
        output = unpack(protocol, events, stream)
        if protocol == 'chat':
            calls = output.get('tool_calls', [])
            text = output.get('content') or ''
        else:
            calls = [block for block in output if block.get('type') == ('function_call' if protocol == 'responses' else 'tool_use')]
            text = ''.join(block.get('text', '') for block in output) if protocol == 'messages' else ''.join(
                part.get('text', '') for block in output for part in block.get('content', []))
        if turn == 1:
            return {'passed': not calls and 'CAN_READY_73' in text, 'turns': 2, 'traces': traces}
        if len(calls) != 1:
            return {'passed': False, 'reason': 'expected exactly one tool call', 'traces': traces}
        call = calls[0]
        name = call.get('function', call).get('name')
        arguments = call.get('input') if protocol == 'messages' else json.loads(call.get('function', call)['arguments'])
        if name != 'read_status' or arguments != {'bus': 2} or type(arguments['bus']) is not int:
            return {'passed': False, 'reason': 'invalid tool schema/arguments', 'traces': traces}
        if protocol == 'responses':
            history += output + [{'type': 'function_call_output', 'call_id': call['call_id'], 'output': TOOL_RESULT}]
        elif protocol == 'messages':
            history += [{'role': 'assistant', 'content': output}, {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call['id'], 'content': TOOL_RESULT}]}]
        else:
            history += [output, {'role': 'tool', 'tool_call_id': call['id'], 'content': TOOL_RESULT}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default=declared.gateway_url())
    parser.add_argument('--model', required=True)
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--key-file', type=Path, default=Path('secrets/gateway.env'))
    parser.add_argument('--root', type=Path, default=Path('artifacts/protocols'))
    parser.add_argument('--wait-unit')
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    url = urllib.parse.urlparse(args.base)
    if url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.scheme != 'http':
        parser.error('only local HTTP endpoints are permitted')
    if args.wait_unit:
        while True:
            status = subprocess.check_output(['systemctl', 'show', args.wait_unit, '-p', 'ActiveState', '--value'], text=True).strip()
            if status not in ('active', 'activating'):
                break
            time.sleep(30)
    os.umask(0o077); args.root.mkdir(parents=True, exist_ok=True)
    lock = (args.root / 'controller.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    key = read_key(args.key_file)
    deployment = json.loads(args.deployment.read_text())
    for field in ('collected_at', 'container_id'):
        deployment.pop(field, None)
    failed = False
    for protocol in ('chat', 'responses', 'messages'):
        for stream in (False, True):
            spec = {'model': args.model, 'deployment': deployment, 'protocol': protocol, 'stream': stream,
                    'script_sha256': SCRIPT_SHA256}
            identity = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
            folder = args.root / identity; folder.mkdir(exist_ok=True)
            completed = sorted(folder.glob('attempt-*.json'))
            if completed:
                previous = json.loads(completed[-1].read_text())['result']
                if previous['passed'] or not args.retry_failed:
                    print(json.dumps({'protocol': protocol, 'stream': stream, 'skipped': True, 'passed': previous['passed']}), flush=True)
                    failed |= not previous['passed']; continue
            started = time.monotonic()
            try:
                result = probe(args.base.rstrip('/'), key, args.model, protocol, stream)
            except Exception as exc:
                result = {'passed': False, 'error_type': type(exc).__name__, 'detail': redact(str(exc), [key])}
            result['wall_s'] = time.monotonic() - started
            target = folder / f'attempt-{len(completed)+1:04d}.json'
            temporary = target.with_suffix('.tmp'); temporary.write_text(json.dumps({'spec': spec, 'result': result}, indent=2) + '\n'); temporary.replace(target)
            print(json.dumps({'id': identity, 'protocol': protocol, 'stream': stream, 'passed': result['passed'],
                              'wall_s': result['wall_s'], 'error_type': result.get('error_type')}), flush=True)
            failed |= not result['passed']
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
