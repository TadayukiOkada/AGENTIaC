#!/usr/bin/env python3
"""Bounded real-source cross-file retrieval at a measured native input length."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import yaml

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import declared, paths
ROOT = paths.root()   # the inventory, profiles, deployments, secrets and results are the site's (D6.2, D7.1)
from agentiac.client import request
from agentiac.secrets import redact

QUESTION = '''Read the supplied repository source. Trace RNG seed handling across the CLI parser, public header, and server speculative synthetic RNG initialization. Return only a JSON object with these keys: cli_option (long option), cli_target (assigned C++ field), cli_conversion (C++ expression used to parse the option), sentinel (hex literal defining LLAMA_DEFAULT_SEED), random_source (C++ expression used when the server sees the sentinel), rng_target (object whose seed method is called), sources (three source file paths supporting the answer). Do not use outside knowledge.'''
EXPECTED = {'cli_option': '--seed', 'cli_target': 'params.sampling.seed',
            'cli_conversion': 'std::stoul(value)', 'sentinel': '0xFFFFFFFF',
            'random_source': 'std::random_device{}()', 'rng_target': 'slot.spec_synth_rng',
            'sources': ['common/arg.cpp', 'include/llama.h', 'tools/server/server-context.cpp']}


def grade(text, expected=None):
    expected = EXPECTED if expected is None else expected
    strict_json = True
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        strict_json = False
        if isinstance(text, str) and text.strip().startswith('```json\n') and text.strip().endswith('\n```'):
            try:
                value = json.loads(text.strip()[8:-4])
            except ValueError:
                return {'passed': False, 'content_passed': False, 'strict_json': False}
        else:
            return {'passed': False, 'content_passed': False, 'strict_json': False}

    if not isinstance(value, dict):
        return {'passed': False, 'reason': 'response is not a JSON object'}
    checks = {}
    for key, reference in expected.items():
        actual = value.get(key)
        if key == 'sources':
            checks[key] = isinstance(actual, list) and all(isinstance(x, str) for x in actual) and sorted(actual) == sorted(reference)
        else:
            normalized = None
            if isinstance(actual, str):
                normalized = ''.join(actual.split()) if key in ('cli_target', 'cli_conversion', 'random_source', 'rng_target') else actual.strip()
                if key == 'sentinel':
                    normalized = normalized.lower()
            checks[key] = normalized == (reference.lower() if key == 'sentinel' else reference)
    return {'passed': strict_json and all(checks.values()), 'content_passed': all(checks.values()),
            'strict_json': strict_json, 'checks': checks}


def prepare_variant(content, sample):
    expected = dict(EXPECTED)
    edits = []
    if sample == 'edited':
        before = 'params.sampling.seed = std::stoul(value);'
        after = 'params.sampling.seed = std::stoull(value);'
        if content.count(before) != 1:
            raise ValueError('edited probe requires exactly one seed assignment')
        offset = content.index(before)
        content = content.replace(before, after, 1)
        expected['cli_conversion'] = 'std::stoull(value)'
        edits = [{'before': before, 'after': after, 'character_offset': offset}]
    return content, expected, edits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json-object', action='store_true', help='request native JSON-object constrained output')
    parser.add_argument('--history', action='store_true', help='append three source-tracing questions using actual prior answers')
    parser.add_argument('--cache-sample', choices=['baseline', 'warm', 'edited'], default='baseline')
    hosts = yaml.safe_load((ROOT / 'infra/inventory.yml').read_text())['all']['children']['workers']['hosts']
    # a host whose tunnel carries its own front (a Strata host's transport is its strata_tunnel_port instead)
    parser.add_argument('--node', choices=sorted(name for name, facts in hosts.items()
                                                 if (facts or {}).get('tunnel_port') and (facts or {}).get('backend') != 'strata'),
                        required=True, help='a declared host whose tunnel carries its own front')
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--profile', help='Explicit temporary profile ID; deployment must equal its pinned manifest')
    parser.add_argument('--deployment', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    node = hosts[args.node]
    model = args.profile or node['default_profile']
    deployment = json.loads((args.deployment or ROOT / 'artifacts/deployments' / (args.node + '-' + model + '.json')).read_text())
    for key in ('collected_at', 'container_id'):
        deployment.pop(key, None)
    profile = json.loads((ROOT / 'profiles' / (model + '.json')).read_text())
    if profile != deployment['profile']:
        parser.error('deployment differs from pinned profile')
    corpus = json.loads(args.corpus.read_text())
    if hashlib.sha256(corpus['content'].encode()).hexdigest() != corpus['content_sha256']:
        parser.error('corpus hash differs')
    content, expected, edits = prepare_variant(corpus['content'], args.cache_sample)
    evaluator_snapshot = Path(__file__).read_bytes()
    spec = {'effective_corpus_sha256': hashlib.sha256(content.encode()).hexdigest(),
            'history': args.history, 'source_edits': edits, 'cache_sample': args.cache_sample, 'node': args.node, 'deployment': deployment, 'corpus_sha256': corpus['content_sha256'],
            'repository_revision': corpus['repository_revision'], 'question': QUESTION,
            'source_sha256': hashlib.sha256(evaluator_snapshot).hexdigest(),
            'generation': {'max_tokens': 2048, 'temperature': 0, 'seed': 42},
            'scope': 'one real-source cross-file retrieval case; not repository repair or full long-context quality'}
    if args.json_object:
        spec['generation']['response_format'] = {'type': 'json_object'}
    identity = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    folder = ROOT / 'artifacts/context-runs' / identity
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (folder / 'result.json').exists():
            print(json.dumps({'identity': identity, 'skipped': True})); return
        (folder / 'manifest.json').write_text(json.dumps(spec, indent=2) + '\n')
        (folder / 'evaluator_snapshot.py').write_bytes(evaluator_snapshot)
        status = {'stage': 'preflight', 'identity': identity, 'started': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        def record():
            temp = folder / 'state.tmp'; temp.write_text(json.dumps(status, indent=2) + '\n'); temp.replace(folder / 'state.json')
        record()
        try:
            active = subprocess.check_output(['systemctl', 'show', 'agentiac-bfcl-' + args.node,
                                             '-p', 'ActiveState', '--value'], text=True).strip()
            if active in ('active', 'activating'):
                raise RuntimeError('BFCL evaluator is still active on this worker')
            probe = subprocess.run(['ssh', '-x', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                    declared.account(node, ROOT) + '@' + node['ansible_host'], 'cat', '/proc/meminfo'],
                                   capture_output=True, text=True, timeout=20, check=True)
            memory = {line.split(':')[0]: int(line.split()[1]) for line in probe.stdout.splitlines()
                      if line.startswith(('MemTotal:', 'MemAvailable:'))}
            if memory['MemAvailable'] < memory['MemTotal'] * 0.2:
                raise RuntimeError('host memory headroom is below 20 percent')
            status['preflight_mem_kib'] = memory
            key = (ROOT / 'secrets/nodes' / (args.node + '.key')).read_text().strip()
            base = 'http://127.0.0.1:' + str(node['tunnel_port'])
            def api(path, payload=None):
                req = urllib.request.Request(base + path,
                    data=json.dumps(payload).encode() if payload is not None else None,
                    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
                with urllib.request.urlopen(req, timeout=180) as response:
                    return json.load(response)
            ids = [m['id'] for m in api('/v1/models')['data']]
            if model not in ids:
                raise RuntimeError('native model ID differs from requested profile')
            if any(slot.get('is_processing') for slot in api('/slots')):
                raise RuntimeError('worker slot is busy')
            messages = [{'role': 'system', 'content': 'Answer from the supplied source code. Follow the requested JSON format.'},
                        {'role': 'user', 'content': content + '\n\n' + QUESTION}]
            payload = {'model': model, 'messages': messages, **spec['generation'],
                       'stream': True, 'stream_options': {'include_usage': True}}
            tokens = api('/v1/chat/completions/input_tokens', payload)['input_tokens']
            status['templated_input_tokens'] = tokens
            if not 128000 <= tokens or tokens + 2048 > profile['backend']['context']:
                raise RuntimeError('templated prompt is outside the reviewed context budget')
            status.update(stage='inference', templated_input_tokens=tokens); record()
            result = request(base + '/v1', key, payload, timeout=1800)
            (folder / 'response.json').write_text(json.dumps(result, indent=2) + '\n')
            scored = grade(result['message'].get('content'), expected)
            scored['input_length_verified'] = result.get('usage', {}).get('prompt_tokens') == tokens
            scored['passed'] = scored['passed'] and result.get('finish_reason') == 'stop' and scored['input_length_verified']
            summary = {**scored, 'finish_reason': result.get('finish_reason'),
                       'usage': result.get('usage'), 'ttft_s': result.get('ttft_s'),
                       'elapsed_s': result.get('elapsed_s'), 'generation_tokens_s': result.get('generation_tokens_s'),
                       'templated_input_tokens': tokens, 'identity': identity}
            if args.history:
                turns = [dict(summary)]
                followups = [
                    ('Now return only cli_option, cli_target and cli_conversion from the supplied source as JSON.', ['cli_option', 'cli_target', 'cli_conversion']),
                    ('Return a JSON object with exactly one key named sentinel. Its value must be the hexadecimal literal assigned to LLAMA_DEFAULT_SEED in the supplied header, not the macro name.', ['sentinel']),
                    ('Now return only random_source and rng_target used by the server initialization, as JSON.', ['random_source', 'rng_target']),
                ]
                for number, (question, fields) in enumerate(followups, 2):
                    if result.get('finish_reason') != 'stop':
                        raise RuntimeError('cannot extend an unfinished assistant response')
                    messages.extend([result['message'], {'role': 'user', 'content': question}])
                    payload['messages'] = messages
                    tokens = api('/v1/chat/completions/input_tokens', payload)['input_tokens']
                    if tokens + 2048 > profile['backend']['context']:
                        raise RuntimeError('growing history exceeds the reviewed context budget')
                    status.update(stage='history-turn-' + str(number), templated_input_tokens=tokens); record()
                    result = request(base + '/v1', key, payload, timeout=1800)
                    (folder / ('response-' + str(number) + '.json')).write_text(json.dumps(result, indent=2) + '\n')
                    scored = grade(result['message'].get('content'), {field: expected[field] for field in fields})
                    verified = result.get('usage', {}).get('prompt_tokens') == tokens
                    turn = {**scored, 'turn': number, 'input_length_verified': verified,
                            'finish_reason': result.get('finish_reason'), 'usage': result.get('usage'),
                            'templated_input_tokens': tokens, 'ttft_s': result.get('ttft_s'),
                            'elapsed_s': result.get('elapsed_s'), 'generation_tokens_s': result.get('generation_tokens_s')}
                    turn['passed'] = turn['passed'] and verified and result.get('finish_reason') == 'stop'
                    turns.append(turn)
                    progress = folder / 'history-progress.tmp'
                    progress.write_text(json.dumps(turns, indent=2) + '\n'); progress.replace(folder / 'history-progress.json')
                summary['history_turns'] = turns
                summary['history_content_passed'] = all(turn.get('content_passed') and turn.get('input_length_verified') and turn.get('finish_reason') == 'stop' for turn in turns)
                summary['passed'] = all(turn['passed'] for turn in turns)
            (folder / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
            status.update(stage='complete', passed=summary['passed']); record()
            print(json.dumps(summary), flush=True)
        except Exception as exc:
            status['failure_phase'] = status['stage']
            (folder / 'error.txt').write_text(redact(str(exc), [key] if 'key' in locals() else []))
            status.update(stage='failed', error_type=type(exc).__name__); record()
            print(json.dumps(status), flush=True)
            raise SystemExit(1) from None


if __name__ == '__main__':
    main()
