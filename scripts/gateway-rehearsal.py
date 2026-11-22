#!/usr/bin/env python3
"""Rehearse a gateway change against a throwaway proxy before it goes near the live one.

The live gateway (loopback, the declared `gateway_port`) is the only place the real behaviour can be seen, and it is
also the thing a mistake would take down. This starts a fake worker and a second litellm proxy
that loads `gateway/gateway_callbacks.py` exactly as `agentiac-gateway.service` does - same
PYTHONPATH, same `AGENTIAC_ROUTES` mechanism, same callbacks wiring - and drives the cases
that matter through it: a normal call, a streamed call, and the three identity outcomes of T4.1
(matching, replaced, unreadable). Nothing here binds a live port or talks to a worker.

    .venv-gateway/bin/python scripts/gateway-rehearsal.py

Exits non-zero when a case is wrong, so it can gate a deploy. `make check` does not run it: it
starts a proxy and needs the gateway venv. The unit tests in tests/test_gateway_log.py and
tests/test_identity.py cover the same rules without a process; this covers the wiring around them.
"""
import argparse
import json
import re
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
sys.path.insert(0, str(CODE / 'scripts'))
import route  # noqa: E402  # the node tables the deployed gateway loads (nodes.json, T3.3)
import yaml  # noqa: E402
SERVED = 'probe-served-model'
# a port NODE_KEY knows, so the callbacks find a key for it; 18106 is control-small, which is
# normally stopped, and the tunnels own every other one
DEFAULT_WORKER_PORT, DEFAULT_PROXY_PORT = 18106, 14199
STATE = {}


class Worker(BaseHTTPRequestHandler):
    """The three endpoints a front owes the gateway: models, status, completions."""

    def log_message(self, *a):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/v1/models'):
            return self._json({'data': [{'id': SERVED}]})
        if self.path.startswith('/ondemand/status'):
            payload = {'state': 'loaded', 'idle_s': 3}
            if STATE['report_identity']:
                payload['identity'] = STATE['identity']
            return self._json(payload)
        self._json({'error': 'not found'}, 404)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get('content-length', 0))) or b'{}')
        if request.get('stream'):
            self.send_response(200)
            self.send_header('content-type', 'text/event-stream')
            self.end_headers()
            # As OpenAI-compatible servers (llama.cpp and its forks) do: content chunks, then a final chunk
            # with an empty delta and finish_reason. LiteLLM 1.108+ treats a clean EOF without any
            # finish_reason as a truncated stream and returns an error instead of a short success.
            for delta, finish in (({'content': 'hel'}, None), ({'content': 'lo'}, None), ({}, 'stop')):
                chunk = {'id': '1', 'object': 'chat.completion.chunk', 'model': SERVED,
                         'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                self.wfile.write((f'data: {json.dumps(chunk)}\n\n').encode())
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
            return
        self._json({'id': '1', 'object': 'chat.completion', 'model': SERVED,
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'hello there'},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14}})


CONFIG = """
model_list:
- model_name: {alias}
  litellm_params:
    model: openai/{served}
    api_base: {base}/v1
    api_key: os.environ/{key}
litellm_settings:
  telemetry: false
  set_verbose: false
  turn_off_message_logging: true
  callbacks: gateway_callbacks.metadata_logger
router_settings:
  num_retries: 0
  timeout: 60
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  custom_auth: gateway_callbacks.custom_auth
  custom_auth_run_common_checks: true
"""


def free(port):
    with socket.socket() as probe:
        return probe.connect_ex(('127.0.0.1', port)) != 0


def start_proxy(alias, base, key, declared, lines, port, workdir):
    (workdir / 'config.yaml').write_text(CONFIG.format(alias=alias, served=SERVED, base=base, key=key))
    (workdir / 'routes.json').write_text(json.dumps({alias: [base, SERVED, declared]}))
    # the proxy checks with the generated node tables, as the deployed gateway does since M3
    nodes = route.node_tables(yaml.safe_load((ROOT / 'gateway/config.yaml').read_text()), route.inventory_hosts())
    (workdir / 'nodes.json').write_text(json.dumps(nodes, indent=2, sort_keys=True) + '\n')
    env = dict(os.environ, PYTHONPATH=str(CODE / 'gateway'), AGENTIAC_ROUTES=str(workdir / 'routes.json'),
               AGENTIAC_NODES=str(workdir / 'nodes.json'),
               LITELLM_MASTER_KEY='rehearsal-master', LITELLM_TASK_KEY='rehearsal-task', DO_NOT_TRACK='1', LITELLM_TELEMETRY='False',
               LITELLM_CODE_KEY='rehearsal-code', LITELLM_CODE_CLOUD_KEY='rehearsal-code-cloud',
               LITELLM_LOCAL_MODEL_COST_MAP='True',
               **{name: 'rehearsal' for name in ('SPARK_API_KEY', 'CONTROL_API_KEY', 'ORIN1_API_KEY',
                                                  'ORIN2_API_KEY', 'STRATA_FRANK_API_KEY', 'STRATA_GENIE_API_KEY')})
    proc = subprocess.Popen([str(CODE / '.venv-gateway/bin/litellm'), '--config', str(workdir / 'config.yaml'),
                             '--host', '127.0.0.1', '--port', str(port), '--num_workers', '1'],
                            cwd=str(CODE / 'gateway'), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    threading.Thread(target=lambda: [lines.append(line.rstrip()) for line in proc.stdout], daemon=True).start()
    import httpx
    for _ in range(120):
        try:
            if httpx.get(f'http://127.0.0.1:{port}/health/liveliness', timeout=2).status_code == 200:
                return proc
        except Exception:
            time.sleep(1)
    raise RuntimeError('the throwaway proxy did not come up:\n' + '\n'.join(lines[-40:]))


def call(port, alias, key, stream):
    import httpx
    body = {'model': alias, 'messages': [{'role': 'user', 'content': 'the secret prompt text'}], 'stream': stream}
    response = httpx.post(f'http://127.0.0.1:{port}/v1/chat/completions', json=body,
                          headers={'Authorization': f'Bearer {key}'}, timeout=90)
    if stream and response.status_code == 200:
        text = ''.join(json.loads(line[6:])['choices'][0]['delta'].get('content', '')
                       for line in response.text.splitlines()
                       if line.startswith('data: ') and line != 'data: [DONE]')
        return response.status_code, text
    if response.status_code == 200:
        return response.status_code, response.json()['choices'][0]['message']['content']
    return response.status_code, response.text[:200]


def inference_lines(lines, since):
    return [json.loads(match.group(0)) for line in lines[since:]
            for match in [re.search(r'\{"event": "inference".*\}', line)] if match]


CASES = (
    ('a normal call is served and logged with host, alias and tokens',
     'match', False, 200, 'hello there'),
    ('a streamed call reports a time to first token',
     'match', True, 200, 'hello'),
    ('a front serving another manifest is refused',
     'replaced', False, 503, 'manifest_hash'),
    ('a front that reports no identity is compared nothing (a front older than T4.1)',
     'absent', False, 200, 'hello there'),
    ('a front that cannot read its manifest is refused, never passed',
     'unreadable', False, 503, 'manifest_hash'),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--alias', default='rehearsal-control')
    parser.add_argument('--task-key', action='store_true', help='exercise the restricted task credential and denials')
    parser.add_argument('--code-key', action='store_true', help="exercise the daily agent's code credentials and denials")
    parser.add_argument('--worker-port', type=int, default=DEFAULT_WORKER_PORT)
    parser.add_argument('--proxy-port', type=int, default=DEFAULT_PROXY_PORT)
    args = parser.parse_args()
    for port in (args.worker_port, args.proxy_port):
        if not free(port):
            print(f'FAIL  port {port} is already in use; refusing to sit on a live service')
            return 2

    alias, base, key = args.alias, f'http://127.0.0.1:{args.worker_port}', 'CONTROL_API_KEY'
    declared = {'backend_id': 'rehearsal-v1', 'manifest_hash': 'a' * 64, 'context': 131072}
    STATE.update(identity=dict(declared), report_identity=True)
    workdir = ROOT / 'artifacts/gateway-rehearsal'
    workdir.mkdir(parents=True, exist_ok=True)

    server = HTTPServer(('127.0.0.1', args.worker_port), Worker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    lines, failures = [], []
    proc = start_proxy(alias, base, key, declared, lines, args.proxy_port, workdir)
    try:
        for name, identity, stream, want_status, want_text in CASES:
            STATE['report_identity'] = identity != 'absent'
            STATE['identity'] = ({'error': 'manifest unreadable: Permission denied'} if identity == 'unreadable'
                                 else dict(declared, manifest_hash='b' * 64) if identity == 'replaced'
                                 else dict(declared))
            mark = len(lines)
            status, text = call(args.proxy_port, alias, 'rehearsal-task' if args.task_key else 'rehearsal-code' if args.code_key else 'rehearsal-master', stream)
            time.sleep(1.0)  # the log line is written by the proxy process, not by this one
            logged = inference_lines(lines, mark)
            ok = status == want_status and want_text in text
            if stream and ok:
                ok = bool(logged) and logged[0].get('ttft_s') is not None
            if identity == 'match' and ok:
                ok = bool(logged) and logged[0].get('node') == 'control' and logged[0].get('alias') == alias
            print(f'{"ok  " if ok else "FAIL"} {name}')
            print(f'       http {status} (want {want_status}), reply {text[:48]!r}')
            for record in logged:
                print(f'       log {json.dumps(record, sort_keys=True)}')
            if not ok:
                failures.append(name)
        if args.task_key:
            import httpx
            denied = httpx.get(f'http://127.0.0.1:{args.proxy_port}/v1/models',
                               headers={'Authorization': 'Bearer rehearsal-task'}, timeout=20)
            ok = denied.status_code == 403
            print(f'{"ok  " if ok else "FAIL"} task key refuses metadata/admin access ({denied.status_code})')
            if not ok:
                failures.append('task key metadata refusal')
            status, _ = call(args.proxy_port, 'cloud-gpt-sol', 'rehearsal-task', False)
            ok = status == 403
            print(f'{"ok  " if ok else "FAIL"} task key refuses cloud alias ({status})')
            if not ok:
                failures.append('task key cloud refusal')
        if args.code_key:
            import httpx
            denied = httpx.get(f'http://127.0.0.1:{args.proxy_port}/v1/models',
                               headers={'Authorization': 'Bearer rehearsal-code'}, timeout=20)
            ok = denied.status_code == 403
            print(f'{"ok  " if ok else "FAIL"} code key refuses metadata/admin access ({denied.status_code})')
            if not ok:
                failures.append('code key metadata refusal')
            status, _ = call(args.proxy_port, 'cloud-gpt-sol', 'rehearsal-code', False)
            ok = status == 403
            print(f'{"ok  " if ok else "FAIL"} code key refuses cloud alias ({status})')
            if not ok:
                failures.append('code key cloud refusal')
            # the throwaway config has no cloud model, so any status but 403 means the key policy let it through
            status, _ = call(args.proxy_port, 'cloud-gpt-sol', 'rehearsal-code-cloud', False)
            ok = status != 403
            print(f'{"ok  " if ok else "FAIL"} code cloud key passes the key policy for the cloud alias ({status})')
            if not ok:
                failures.append('code cloud key cloud pass')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        server.shutdown()
        server.server_close()
    total = len(CASES) + (2 if args.task_key else 0) + (3 if args.code_key else 0)
    print(f'\n{total - len(failures)}/{total} cases passed'
          + (f'; failed: {", ".join(failures)}' if failures else ''))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
