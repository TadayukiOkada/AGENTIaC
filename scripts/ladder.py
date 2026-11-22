#!/usr/bin/env python3
"""Context ladder: prefill, TTFT and decode speed at growing prompt depths for one alias.

run     For each depth and repetition: a cold request (real llama.cpp source with
        three answer anchors at about 10/50/90% depth, the 28K probe's question,
        a per-repetition first line so no prefix cache applies) and a warm
        follow-up on the same prefix asking for a long walkthrough (decode speed
        over --max-tokens). The alias resolves to its backend's base URL, model
        and key from the gateway config; requests go straight to the backend.
        Results are appended to artifacts/ladder/<label>/results.jsonl; a rerun
        with identical options resumes, failed cases need --retry-failed.
        Before any request the run compares what the alias declares with what the
        front answers at /ondemand/status, and refuses a mismatch (T4.1) — the
        ladder talks to the backend directly, so the gateway's own refusal does not
        cover it. What it read is kept in artifacts/ladder/<label>/identity.json.
report  Medians per depth from a label's results.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

import yaml

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'gateway'))  # identity_policy lives with the gateway that enforces it
sys.path.insert(0, str(CODE / 'src'))      # pathwatch holds the one passive read of a declared front
from agentiac import paths  # noqa: E402
ROOT = paths.root()
import identity_policy  # noqa: E402
from agentiac import pathwatch  # noqa: E402
from agentiac.errors import command as cli_name  # noqa: E402
OUT = ROOT / 'artifacts/ladder'
CORPUS = ROOT / 'artifacts/corpora/llama.cpp'
REVISION = '620d2225cc436ab0b001e3200e590e3b286ef9ab'
spec = importlib.util.spec_from_file_location('context_grade', CODE / 'benchmarks/context-probe.py')
grader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grader)
ANCHORS = [('common/arg.cpp', 'params.sampling.seed = std::stoul(value);'),
           ('include/llama.h', '#define LLAMA_DEFAULT_SEED'),
           ('tools/server/server-context.cpp', 'slot.spec_synth_rng.seed')]
ANCHOR_WIDTH = 3000          # characters of source around each anchor
POSITIONS = (0.1, 0.5, 0.9)  # anchor depths within the padding
FOLLOW_UP = ('Now write a detailed, section-by-section technical walkthrough of every file in the supplied source: '
             'what each part does and how the parts interact. Write at least 3000 words and do not stop early.')
SYSTEM = 'Answer from the supplied source code. Follow the requested format.'


def padding_files(repository):
    """Real source used as filler: src/*.cpp and src/*.h without seed handling or generated tables, by path."""
    files = sorted(p for p in (repository / 'src').iterdir() if p.suffix in ('.cpp', '.h') and p.name != 'unicode-data.cpp')
    return [p for p in files if 'seed' not in p.read_text(errors='replace').lower()]


def anchor_windows(repository):
    windows = []
    for name, needle in ANCHORS:
        source = (repository / name).read_text()
        if source.count(needle) != 1:
            raise ValueError('expected exactly one anchor in ' + name)
        middle = source.index(needle)
        start = max(0, min(middle - ANCHOR_WIDTH // 2, len(source) - ANCHOR_WIDTH))
        windows.append(f'\nFILE: {name} (characters {start}:{start + ANCHOR_WIDTH})\n' + source[start:start + ANCHOR_WIDTH])
    return windows


def build_prompt(repository, padding_chars, nonce):
    """Nonce line, padding with the anchor windows inserted at POSITIONS, then the question."""
    pool = ''.join(f'\nFILE: {p.relative_to(repository)}\n' + p.read_text() for p in padding_files(repository))
    if padding_chars > len(pool):
        raise ValueError('padding pool too small for this depth')
    padding = pool[:padding_chars]
    cuts = [0] + [padding.rfind('\n', 0, int(padding_chars * f)) + 1 for f in POSITIONS] + [padding_chars]
    parts = [f'Run {nonce}.']
    for index, window in enumerate(anchor_windows(repository)):
        parts += [padding[cuts[index]:cuts[index + 1]], window]
    parts += [padding[cuts[-2]:], '\n\n' + grader.QUESTION]
    return '\n'.join(parts)


def default_depths(context):
    """4K / 32K / 64K / ~100K for a 128K window; the steps that fit a smaller one, the last near 92% of it."""
    steps = [d for d in (4096, 32768, 65536, 100000) if d + 4096 <= context * 0.8]
    return steps + [int(context * 0.92) // 1000 * 1000] if context < 131072 else steps


def gateway_route(alias, routes_file=None):
    """What the rendered gateway says about ALIAS: [api base, model], or [api base, model, expected identity].

    `artifacts/gateway/routes.json` is what `scripts/route.py` renders for the gateway's pre-call (IP-02, T4.1), and
    its third field is the identity the gateway itself compares. It is present only for a target whose backend is
    manifested; a llama.cpp profile target has no third field, so there is nothing declared to compare with.
    """
    path = Path(routes_file) if routes_file else ROOT / 'artifacts/gateway/routes.json'
    if not path.exists():
        raise SystemExit(f'{path} is not rendered; run {cli_name()} ops deploy before measuring through an alias')
    routes = json.loads(path.read_text())
    if alias not in routes:
        raise SystemExit(f'{alias} is not a rendered gateway alias; {cli_name()} status names the aliases that are')
    return routes[alias]


def served_identity(base, key):
    """What the front at BASE reports it serves (GET /ondemand/status), or None when it reports nothing.

    `scripts/ondemand-proxy.py` answers /ondemand/status before it judges a request as use, so this read does not
    load a model, touch an idle timer or add a request the measurement would have to pay for.
    """
    report = pathwatch.front_status(base, key, timeout=60)   # passive: it never asks the front to load anything
    return report.get('identity') if report else None


def named(identity):
    """An identity as one line, for a message that has to say which backend was expected and which was found."""
    if not isinstance(identity, dict):
        return 'nothing (the front declares no manifest)'
    if 'error' in identity:
        return "a front that cannot read its manifest"
    return (f'{identity.get("backend_id")} at manifest {str(identity.get("manifest_hash"))[:12]} '
            f'context {identity.get("context")}')


def identity_gate(alias, base, key, expected, baseline=None):
    """Refuse when the front behind BASE is not what ALIAS declares; return what it reported.

    The ladder resolves an alias from the gateway config and then speaks to that base URL through the worker's
    tunnel, so the gateway's own refusal never sees the request; without this check, a case can be recorded under
    an alias that does not match the backend the worker is serving.
    A front that reports nothing is not a contradiction (a llama.cpp profile declares no manifest), but a saved
    baseline that no longer matches is, and one label never measures two backends.
    """
    served = served_identity(base, key)
    problem = identity_policy.identity_problem(expected, served)
    if problem:
        raise SystemExit(f'{alias}: the front at {base} does not serve what the alias declares ({problem} differs; '
                         f'declared {named(expected)}, the front reports {named(served)}). The ladder measures the '
                         'base URL directly, past the gateway refusal: claim the declared backend '
                         f'({cli_name()} claim HOST --backend ID) or measure the alias of what is served.')
    if baseline is not None and any((served or {}).get(field) != (baseline or {}).get(field)
                                    for field in identity_policy.FIELDS):
        raise SystemExit(f'{alias}: this label began against {named(baseline)} and the front now reports '
                         f'{named(served)}; start a new --label instead of measuring two backends as one run')
    return served


def record_identity(folder, entry):
    """Append one identity reading to the label's identity.json: what was declared, what the front said, where."""
    path = folder / 'identity.json'
    checks = json.loads(path.read_text()) if path.exists() else []
    checks.append(entry)
    path.write_text(json.dumps(checks, indent=2) + '\n')
    return checks


def resolve(alias):
    """(base URL without /v1, model id, key) of the alias from the rendered or tracked gateway config."""
    rendered = ROOT / 'artifacts/gateway/config.yaml'
    for path in (rendered, ROOT / 'gateway/config.yaml'):
        if path.exists():
            for entry in yaml.safe_load(path.read_text())['model_list']:
                if entry['model_name'] == alias:
                    params = entry['litellm_params']
                    variable = params['api_key'].removeprefix('os.environ/')
                    env = dict(line.split('=', 1) for line in (ROOT / 'secrets/gateway.env').read_text().splitlines() if '=' in line)
                    return params['api_base'].removesuffix('/v1'), params['model'].split('/', 1)[1], env[variable].strip()
    raise SystemExit(f'unknown alias {alias}')


def post(url, key, body, timeout):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key, 'x-api-key': key,
        'anthropic-version': '2023-06-01'})
    return urllib.request.urlopen(request, timeout=timeout)


def count_tokens(base, key, model, text):
    """Served tokenizer count: Strata's Messages count_tokens, else llama.cpp's /tokenize."""
    try:
        with post(base + '/v1/messages/count_tokens', key, {'model': model, 'system': SYSTEM,
                  'messages': [{'role': 'user', 'content': text}]}, 300) as response:
            return json.load(response)['input_tokens']
    except urllib.error.HTTPError as error:
        if error.code not in (404, 405):
            raise
    with post(base + '/tokenize', key, {'content': text}, 300) as response:
        return len(json.load(response)['tokens'])


def fit_padding(count, target, low=0, high=2_000_000):
    """Largest padding length whose prompt counts at most TARGET tokens (within 2%); returns (chars, tokens)."""
    best = None
    for _ in range(24):
        middle = (low + high) // 2
        tokens = count(middle)
        if tokens <= target:
            best = (middle, tokens)
            if tokens >= target * 0.98:
                return best
            low = middle + 1
        else:
            high = middle - 1
    if best is None:
        raise ValueError('no padding fits the target depth')
    return best


def stream_chat(base, key, model, messages, max_tokens):
    """One streamed chat completion: text, finish reason, usage, TTFT and total time."""
    body = {'model': model, 'messages': [{'role': 'system', 'content': SYSTEM}] + messages, 'stream': True,
            'stream_options': {'include_usage': True}, 'max_tokens': max_tokens, 'temperature': 0, 'seed': 42}
    started = time.monotonic()
    first, text, reason, usage = None, [], None, {}
    with post(base + '/v1/chat/completions', key, body, 3600) as response:
        for line in response:
            if not line.startswith(b'data:') or line[5:].strip() == b'[DONE]':
                continue
            chunk = json.loads(line[5:])
            usage = chunk.get('usage') or usage
            for choice in chunk.get('choices', []):
                delta = choice.get('delta', {})
                piece = delta.get('content') or delta.get('reasoning_content')
                if piece:
                    first = first if first is not None else time.monotonic() - started
                    text.append(delta.get('content') or '')
                reason = choice.get('finish_reason') or reason
    return {'text': ''.join(text), 'finish_reason': reason, 'usage': usage, 'ttft_s': first,
            'elapsed_s': time.monotonic() - started}


def measure(cold, warm):
    """Speeds from a cold answer and its warm follow-up."""
    prompt = cold['usage'].get('prompt_tokens')
    decoded = warm['usage'].get('completion_tokens') or 0
    decode_s = warm['elapsed_s'] - (warm['ttft_s'] or 0)
    return {'prompt_tokens': prompt, 'cold_ttft_s': cold['ttft_s'],
            'prefill_tok_s': prompt / cold['ttft_s'] if prompt and cold['ttft_s'] else None,
            'warm_prompt_tokens': warm['usage'].get('prompt_tokens'), 'warm_ttft_s': warm['ttft_s'],
            'decode_tokens': decoded, 'decode_tok_s': (decoded - 1) / decode_s if decoded > 1 and decode_s > 0 else None,
            'warm_finish_reason': warm['finish_reason']}


def load_results(folder):
    path = folder / 'results.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def check_identity(folder, alias, base, key, expected, baseline, position):
    """One recorded reading of what the front serves: it must be what the alias declares, and the same one the
    run began with. `position` says where in the run the reading was taken."""
    served = identity_gate(alias, base, key, expected, baseline)
    record_identity(folder, {'position': position, 'alias': alias, 'declared': expected, 'served': served,
                            'at': datetime.datetime.now(datetime.timezone.utc).isoformat()})
    return served


def run(args):
    base, model, key = resolve(args.alias)
    folder = OUT / args.label
    folder.mkdir(parents=True, exist_ok=True)
    declared = gateway_route(args.alias)
    expected = declared[2] if len(declared) > 2 else None
    started = folder / 'identity.json'
    baseline = json.loads(started.read_text())[0]['served'] if started.exists() else None
    served = check_identity(folder, args.alias, base, key, expected, baseline, 'before')
    depths = args.depth or default_depths(args.context)
    options = {'alias': args.alias, 'model': model, 'context': args.context, 'depths': depths, 'repeat': args.repeat,
               'max_tokens': args.max_tokens, 'corpus_revision': REVISION,
               'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    manifest = folder / 'manifest.json'
    if manifest.exists():
        saved = json.loads(manifest.read_text())
        if {k: v for k, v in saved.items() if k != 'runner_sha256'} != {k: v for k, v in options.items() if k != 'runner_sha256'}:
            raise SystemExit('options differ from the saved run; use a new --label')
    else:
        manifest.write_text(json.dumps(options, indent=2) + '\n')
    if subprocess.check_output(['git', '-C', str(CORPUS), 'rev-parse', 'HEAD'], text=True).strip() != REVISION or \
            subprocess.check_output(['git', '-C', str(CORPUS), 'status', '--porcelain'], text=True):
        raise SystemExit('llama.cpp corpus must be the clean pinned checkout')
    done = {r['case']: r for r in load_results(folder)}
    for depth in depths:
        if depth + args.max_tokens * 2 > args.context:
            raise SystemExit(f'depth {depth} does not fit context {args.context}')
        for rep in range(1, args.repeat + 1):
            case = f'{depth}-{rep}'
            if case in done and (done[case]['status'] == 'ok' or not args.retry_failed):
                continue
            row = {'case': case, 'depth': depth, 'rep': rep, 'started': datetime.datetime.now(datetime.timezone.utc).isoformat()}
            try:
                nonce = f'{args.label}-{case}-{time.time_ns()}'
                chars, counted = fit_padding(lambda n: count_tokens(base, key, model, build_prompt(CORPUS, n, nonce)), depth)
                prompt = build_prompt(CORPUS, chars, nonce)
                cold = stream_chat(base, key, model, [{'role': 'user', 'content': prompt}], args.max_tokens)
                graded = grader.grade(cold['text'].strip())
                warm = stream_chat(base, key, model, [{'role': 'user', 'content': prompt},
                                                      {'role': 'assistant', 'content': cold['text']},
                                                      {'role': 'user', 'content': FOLLOW_UP}], args.max_tokens)
                row.update(status='ok', counted_tokens=counted, padding_chars=chars, answer_passed=graded['passed'],
                           answer_content_passed=graded['content_passed'], cold_finish_reason=cold['finish_reason'],
                           **measure(cold, warm))
                (folder / f'answer-{case}.txt').write_text(cold['text'])
            except Exception as error:  # recorded, never retried silently
                row.update(status='failed', error_type=type(error).__name__, error=str(error)[:300])
                (folder / f'error-{case}.txt').write_text(traceback.format_exc())
            row['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            with (folder / 'results.jsonl').open('a') as log:
                log.write(json.dumps(row) + '\n')
            print(json.dumps(row), flush=True)
        served = check_identity(folder, args.alias, base, key, expected, served, f'after-depth-{depth}')
    check_identity(folder, args.alias, base, key, expected, served, 'after')


def summarize(rows):
    latest = {}
    for row in rows:
        latest[row['case']] = row
    table = {}
    for row in latest.values():
        table.setdefault(row['depth'], []).append(row)
    def median(cases, key):
        values = [c[key] for c in cases if c.get(key) is not None]
        return round(statistics.median(values), 2) if values else None
    return [{'depth': depth, 'ok': sum(c['status'] == 'ok' for c in cases), 'cases': len(cases),
             'answers_passed': sum(bool(c.get('answer_passed')) for c in cases),
             **{key: median(cases, key) for key in ('prompt_tokens', 'cold_ttft_s', 'prefill_tok_s', 'warm_ttft_s',
                                                    'decode_tok_s', 'decode_tokens')}}
            for depth, cases in sorted(table.items())]


def _latest_by_depth(rows):
    latest = {}
    for row in rows:
        latest[row['case']] = row
    table = {}
    for row in latest.values():
        table.setdefault(row['depth'], []).append(row)
    return table


def _stats(cases, key):
    values = [c[key] for c in cases if c.get('status') == 'ok' and c.get(key) is not None]
    if not values:
        return None
    return {'median': round(statistics.median(values), 2), 'min': round(min(values), 2), 'max': round(max(values), 2)}


def _delta_pct(base_val, cand_val):
    if base_val in (None, 0) or cand_val is None:
        return None
    return round((cand_val - base_val) / base_val * 100.0, 2)


def compare_runs(base_label, base_rows, base_manifest, cand_label, cand_rows, cand_manifest,
                 base_identity=None, cand_identity=None):
    """Compare two ladder runs across shared depths: medians, min-max ranges, % deltas, and pooled decode ratio."""
    base_table, cand_table = _latest_by_depth(base_rows), _latest_by_depth(cand_rows)
    depths = sorted(set(base_table) | set(cand_table))
    rows = []
    base_decodes, cand_decodes = [], []
    for depth in depths:
        bc, cc = base_table.get(depth, []), cand_table.get(depth, [])
        bd, cd = _stats(bc, 'decode_tok_s'), _stats(cc, 'decode_tok_s')
        bp, cp = _stats(bc, 'prefill_tok_s'), _stats(cc, 'prefill_tok_s')
        bt, ct = _stats(bc, 'cold_ttft_s'), _stats(cc, 'cold_ttft_s')
        base_decodes.extend(c['decode_tok_s'] for c in bc if c.get('status') == 'ok' and c.get('decode_tok_s') is not None)
        cand_decodes.extend(c['decode_tok_s'] for c in cc if c.get('status') == 'ok' and c.get('decode_tok_s') is not None)
        rows.append({
            'depth': depth,
            'base_ok': sum(c.get('status') == 'ok' for c in bc),
            'base_cases': len(bc),
            'base_answers': sum(bool(c.get('answer_passed')) for c in bc),
            'cand_ok': sum(c.get('status') == 'ok' for c in cc),
            'cand_cases': len(cc),
            'cand_answers': sum(bool(c.get('answer_passed')) for c in cc),
            'decode': {'base': bd, 'cand': cd, 'delta_pct': _delta_pct(bd and bd['median'], cd and cd['median'])},
            'prefill': {'base': bp, 'cand': cp, 'delta_pct': _delta_pct(bp and bp['median'], cp and cp['median'])},
            'cold_ttft': {'base': bt, 'cand': ct, 'delta_pct': _delta_pct(bt and bt['median'], ct and ct['median'])},
        })
    pooled_ratio = (round(statistics.mean(cand_decodes) / statistics.mean(base_decodes), 4)
                    if base_decodes and cand_decodes and statistics.mean(base_decodes) else None)
    return {
        'baseline': base_label,
        'candidate': cand_label,
        'corpus_match': base_manifest.get('corpus_revision') == cand_manifest.get('corpus_revision'),
        'base_corpus': base_manifest.get('corpus_revision'),
        'cand_corpus': cand_manifest.get('corpus_revision'),
        'base_identity': (base_identity[0].get('served') if base_identity else None),
        'cand_identity': (cand_identity[0].get('served') if cand_identity else None),
        'pooled_decode_ratio': pooled_ratio,
        'depths': rows,
    }


def _fmt_stat(stat):
    if stat is None:
        return '-'
    return f"{stat['median']} ({stat['min']}–{stat['max']})"


def _fmt_delta(val):
    if val is None:
        return '-'
    return f"{val:+.2f}%"


def format_comparison(report):
    lines = [
        f"Ladder comparison: {report['candidate']} vs {report['baseline']}",
        f"Corpus match: {'yes' if report['corpus_match'] else 'NO'} | Pooled decode ratio: {report['pooled_decode_ratio']}",
        '',
        '| depth | decode base | decode cand | decode Δ | prefill Δ | cold TTFT Δ | answers (base / cand) |',
        '|---:|---|---|---:|---:|---:|---|',
    ]
    for row in report['depths']:
        lines.append(
            f"| {row['depth']} | {_fmt_stat(row['decode']['base'])} | {_fmt_stat(row['decode']['cand'])} | "
            f"{_fmt_delta(row['decode']['delta_pct'])} | {_fmt_delta(row['prefill']['delta_pct'])} | "
            f"{_fmt_delta(row['cold_ttft']['delta_pct'])} | "
            f"{row['base_answers']}/{row['base_cases']} / {row['cand_answers']}/{row['cand_cases']} |"
        )
    return '\n'.join(lines)


def _load_json_if_exists(path):
    return json.loads(path.read_text()) if path.exists() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    r = sub.add_parser('run')
    r.add_argument('--alias', required=True, help='gateway alias, e.g. <host>-worker-<model>')
    r.add_argument('--context', type=int, required=True, help='context the backend was started with')
    r.add_argument('--label', required=True, help='results folder under artifacts/ladder/')
    r.add_argument('--depth', type=int, action='append', help='prompt depth in tokens (repeatable); default by --context')
    r.add_argument('--repeat', type=int, default=3)
    r.add_argument('--max-tokens', type=int, default=1024)
    r.add_argument('--retry-failed', action='store_true')
    p = sub.add_parser('report')
    p.add_argument('label')
    c = sub.add_parser('compare', help='compare two ladder labels under artifacts/ladder/')
    c.add_argument('baseline')
    c.add_argument('candidate')
    c.add_argument('--json', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'run':
        run(args)
    elif args.command == 'compare':
        base_dir, cand_dir = OUT / args.baseline, OUT / args.candidate
        report = compare_runs(
            args.baseline, load_results(base_dir), _load_json_if_exists(base_dir / 'manifest.json') or {},
            args.candidate, load_results(cand_dir), _load_json_if_exists(cand_dir / 'manifest.json') or {},
            _load_json_if_exists(base_dir / 'identity.json'), _load_json_if_exists(cand_dir / 'identity.json'),
        )
        print(json.dumps(report, indent=2) if args.json else format_comparison(report))
    else:
        print(json.dumps(summarize(load_results(OUT / args.label)), indent=2))


if __name__ == '__main__':
    main()
