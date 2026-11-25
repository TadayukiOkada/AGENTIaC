"""What the generic container worker decides before it launches, and what it does when asked to stop.

The worker is the declared way's engine: the container name, the port, the readiness route and the launch command come
from `<root>/service.json`, `<root>/launch-args.json` and `<root>/backend-manifest.json`, all written by
`borrow.prepare_backend`. These tests run it as a subprocess against a `docker`/`nvidia-smi` shim on `PATH` and a stub
HTTP server, so no engine, no container and no host is touched — and no launch happens unless the refusal tests prove
it did not.
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = Path(__file__).resolve().parents[1]
PIN = 'sha256:00000000000000000000000000000000000000000000000000000000000000aa'
DOCKER = '''#!/bin/sh
echo "$@" >> "$CALLS"
running=$CONTAINER_RUNNING; exists=$CONTAINER_EXISTS; image=$CONTAINER_IMAGE
if [ -f "$STARTED" ]; then running=1; exists=1; [ -n "$CONTAINER_LAUNCH_IMAGE" ] && image=$CONTAINER_LAUNCH_IMAGE; fi
case "$1" in
  inspect)
    [ "$exists" = 1 ] || exit 1
    if [ "$running" = 1 ]; then state=true; else state=false; fi
    if [ "$2" = --format ]; then echo "$state"; else
      echo "[{\\"State\\":{\\"Running\\":$state},\\"Config\\":{\\"Labels\\":{\\"io.agent-infra.owner\\":\\"$CONTAINER_LABEL\\"}},\\"Image\\":\\"$image\\"}]"
    fi ;;
  top)
    tops=$(grep -c '^top' "$CALLS")
    echo PID
    if [ "$TOP_FIRST_EMPTY" = 1 ] && [ "$tops" = 1 ]; then exit 0; fi
    for pid in $CONTAINER_PIDS; do echo $pid; done ;;
  rm) exit 0 ;;
  stop) exit "${STOP_STATUS:-0}" ;;
  run) if [ -n "$SECRET_EXPECT" ]; then if [ -f "$SECRET_EXPECT" ]; then echo present; else echo absent; fi > "$SECRET_SEEN"; fi
       : > "$STARTED"; exit "${RUN_STATUS:-0}" ;;
esac
'''
NVIDIA = '''#!/bin/sh
if [ -f "$STARTED" ]; then echo "$GPU_PIDS_AFTER"; else echo "$GPU_PIDS"; fi
'''


class Handler(BaseHTTPRequestHandler):
    paths = []
    authorizations = []
    body = b'{"status":"ok"}'

    def do_GET(self):
        Handler.paths.append(self.path)
        Handler.authorizations.append(self.headers.get('Authorization'))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(Handler.body)

    def log_message(self, *args):
        pass


class WorkerFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'worker.py').write_text((ROOT / 'scripts/container-service-worker.py').read_text())
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.service = {'unit': 'agentiac-infernix-inference', 'port': self.port, 'root': str(self.root),
                        'container': 'infernix-serving', 'status_path': '/health', 'start_timeout_s': None}
        self.manifest = {'id': 'infernix-fixture', 'image': {'tag': 'agentiac-infernix:fixture', 'id': PIN}}
        self.launch = ['docker', 'run', '--name', 'infernix-serving', '--label', 'io.agent-infra.owner=agent-infra',
                       f'--publish=127.0.0.1:{self.port}:8080', PIN, 'infernix-serve', '--health-port', str(self.port)]
        self.write()
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        for name, text in (('docker', DOCKER), ('nvidia-smi', NVIDIA)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.env = {**os.environ, 'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
                    'CALLS': str(self.root / 'calls'), 'STARTED': str(self.root / 'started'),
                    'CONTAINER_EXISTS': '0', 'CONTAINER_RUNNING': '0', 'CONTAINER_LABEL': 'agent-infra',
                    'CONTAINER_IMAGE': PIN, 'CONTAINER_LAUNCH_IMAGE': '', 'CONTAINER_PIDS': '1', 'GPU_PIDS': '',
                    'GPU_PIDS_AFTER': ''}

    def write(self, **service):
        (self.root / 'service.json').write_text(json.dumps({**self.service, **service}))
        (self.root / 'backend-manifest.json').write_text(json.dumps(self.manifest))
        (self.root / 'launch-args.json').write_text(json.dumps(self.launch))

    def start(self, umask=None, **env):
        self.process = subprocess.Popen([sys.executable, str(self.root / 'worker.py')],
                                        env={**self.env, **env}, stderr=subprocess.PIPE, text=True,
                                        **({'umask': umask} if umask is not None else {}))
        self.addCleanup(self.process.kill)
        return self.process

    def status(self):
        path = self.root / 'status.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def wait_for_phase(self, phase, deadline=25):
        stop = time.monotonic() + deadline
        while self.status().get('phase') != phase:
            self.assertLess(time.monotonic(), stop, f'worker never reached {phase}: {self.status()}')
            time.sleep(0.1)
        return self.status()

    def calls(self):
        return (self.root / 'calls').read_text().splitlines() if (self.root / 'calls').exists() else []

    def stop(self):
        self.process.send_signal(signal.SIGTERM)
        _, stderr = self.process.communicate(timeout=25)
        return self.process.returncode, stderr

    def serve(self):
        Handler.paths = []
        Handler.authorizations = []
        Handler.body = b'{"status":"ok"}'
        server = HTTPServer(('127.0.0.1', self.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server


class LaunchGuardTests(WorkerFixture):
    def test_a_name_held_by_a_running_container_is_refused_without_launching(self):
        self.start(CONTAINER_EXISTS='1', CONTAINER_RUNNING='1')
        self.assertEqual(self.wait_for_phase('failed')['reason'], 'container name is in use by a running or unowned container')
        self.assertEqual([call for call in self.calls() if call.startswith('run')], [])

    def test_a_name_held_by_an_unowned_stopped_container_is_refused_and_not_removed(self):
        self.start(CONTAINER_EXISTS='1', CONTAINER_RUNNING='0', CONTAINER_LABEL='someone-else')
        self.assertIn('unowned container', self.wait_for_phase('failed')['reason'])
        self.assertEqual([call for call in self.calls() if call.startswith(('run', 'rm'))], [])

    def test_the_pinned_image_is_compared_before_a_stopped_container_of_ours_is_removed(self):
        # Removing a stopped container we own is fine, but not one that runs a different engine than the manifest pins.
        self.start(CONTAINER_EXISTS='1', CONTAINER_RUNNING='0', CONTAINER_IMAGE='sha256:ffffffff')
        reason = self.wait_for_phase('failed')['reason']
        self.assertIn('sha256:ffffffff', reason)
        self.assertIn(PIN, reason)
        self.assertEqual([call for call in self.calls() if call.startswith(('run', 'rm'))], [])

    def test_a_foreign_gpu_process_refuses_the_launch(self):
        self.start(GPU_PIDS='4242')
        self.assertEqual(self.wait_for_phase('failed')['reason'], 'foreign GPU workload or GPU check failure')
        self.assertEqual([call for call in self.calls() if call.startswith('run')], [])

    def test_a_port_already_in_use_is_refused_when_the_container_name_is_free(self):
        with socket.socket() as occupier:
            occupier.bind(('127.0.0.1', self.port))
            occupier.listen(1)
            self.start()
            self.assertEqual(self.wait_for_phase('failed')['reason'], 'port occupied')
        self.assertEqual([call for call in self.calls() if call.startswith('run')], [])

    def test_a_launch_that_fails_is_a_failure_and_not_a_silent_retry(self):
        self.start(RUN_STATUS='1')
        self.assertIn('docker launch failed', self.wait_for_phase('failed')['reason'])

    def test_an_occupied_port_is_still_refused_when_a_stopped_owned_container_exists(self):
        with socket.socket() as occupier:
            occupier.bind(('127.0.0.1', self.port))
            occupier.listen(1)
            self.start(CONTAINER_EXISTS='1', CONTAINER_RUNNING='0')
            self.assertEqual(self.wait_for_phase('failed')['reason'], 'port occupied')
        self.assertEqual([call for call in self.calls() if call.startswith(('run', 'rm'))], [])

    def test_a_launched_container_with_the_wrong_image_is_stopped_and_refused(self):
        process = self.start(CONTAINER_LAUNCH_IMAGE='sha256:bad')
        reason = self.wait_for_phase('failed')['reason']
        self.assertIn('sha256:bad', reason)
        self.assertIn(PIN, reason)
        process.wait(timeout=25)
        self.assertTrue(self.status()['container_stopped'])
        self.assertIn('stop -t 10 infernix-serving', '\n'.join(self.calls()))


class ServingTests(WorkerFixture):
    def test_the_launch_command_is_the_one_this_site_declared(self):
        self.start()
        self.wait_for_phase('loading')
        launched = [call for call in self.calls() if call.startswith('run')]
        self.assertEqual(len(launched), 1)
        self.assertIn(PIN, launched[0])
        self.assertIn('infernix-serving', launched[0])

    def test_readiness_is_the_declared_route_answering_on_the_declared_port(self):
        self.start()
        self.wait_for_phase('loading')
        self.serve()
        self.assertEqual(self.wait_for_phase('ready')['phase'], 'ready')
        self.assertIn('/health', Handler.paths)
        code, _ = self.stop()
        self.assertEqual(code, 0)
        self.assertEqual(self.status()['phase'], 'stopped')

    def test_readiness_times_out_on_the_declared_wait_and_names_the_route_it_asked(self):
        self.write(start_timeout_s=4)
        self.start()
        self.wait_for_phase('loading')
        reason = self.wait_for_phase('failed')['reason']
        self.assertIn('readiness timeout', reason)
        self.assertIn('/health', reason)

    def test_memory_is_sampled_while_the_engine_is_held(self):
        self.start()
        self.wait_for_phase('loading')
        self.serve()
        self.wait_for_phase('ready')
        rows = [json.loads(line) for line in (self.root / 'memory.jsonl').read_text().splitlines()]
        self.assertTrue(rows)
        self.assertTrue(all('MemAvailable' in row for row in rows))
        self.stop()

    def test_readiness_waits_while_health_reports_loaded_false(self):
        self.start()
        self.wait_for_phase('loading')
        self.serve()
        Handler.body = b'{"loaded": false}'
        time.sleep(1)
        self.assertEqual(self.status()['phase'], 'loading')
        Handler.body = b'{"loaded": true}'
        self.assertEqual(self.wait_for_phase('ready')['phase'], 'ready')
        self.stop()

    def test_the_guard_looks_at_the_container_again_before_calling_a_gpu_process_foreign(self):
        # the engine's GPU process can appear between docker top and nvidia-smi; it is ours, not another tenant's
        self.start(TOP_FIRST_EMPTY='1', CONTAINER_PIDS='555', GPU_PIDS_AFTER='555')
        self.wait_for_phase('loading')
        self.serve()
        self.assertEqual(self.wait_for_phase('ready')['phase'], 'ready')
        self.stop()

    def test_a_requested_stop_is_a_clean_exit_that_stops_the_container(self):
        self.start()
        self.wait_for_phase('loading')
        self.serve()
        self.wait_for_phase('ready')
        code, stderr = self.stop()
        self.assertEqual(code, 0, stderr)
        self.assertIn('service termination requested', stderr)
        status = self.status()
        self.assertEqual(status['phase'], 'stopped')
        self.assertTrue(status['container_stopped'])
        self.assertIn('stop -t 10 infernix-serving', '\n'.join(self.calls()))

    def test_a_container_that_refuses_to_stop_is_not_reported_as_a_clean_stop(self):
        # The unit's own exit is a requested stop; a container that would not stop is the failure an operator has to
        # see, so the exit code and the recorded fields say so rather than the reason pretending otherwise.
        self.start(STOP_STATUS='1')
        self.wait_for_phase('loading')
        code, _ = self.stop()
        status = self.status()
        self.assertNotEqual(code, 0)
        self.assertEqual(status['phase'], 'stopped')
        self.assertFalse(status['container_stopped'])
        self.assertEqual(status['stop_returncode'], 1)


class EngineAgnosticTests(unittest.TestCase):
    def test_the_worker_carries_no_engine_facts_of_its_own(self):
        """The comments may name the engines this pattern came from; the code must not carry any of their facts.

        A container name, a port, an image id and a memory threshold are the four facts Spark TensorFold's worker has
        hardcoded, and this file is meant to be what a *declaration* supplies them from.
        """
        source = (ROOT / 'scripts/container-service-worker.py').read_text()
        for hardcoded in ('spark-tensorfold', 'infernix-serve', '18885', '3442529299de2bed', '100*(1<<30)', '--gpus'):
            self.assertNotIn(hardcoded, source, hardcoded)



class SecretTests(WorkerFixture):
    KEY = 'k3y-0123456789abcdef'
    SECRET = [{'path': 'engine-secrets.toml', 'text': '[serve]\napi-key = "{api_key}"\n'}]

    def ready_with(self, key_text):
        (self.root / 'api.key').write_text(key_text)
        self.start()
        self.wait_for_phase('loading')
        self.serve()
        self.wait_for_phase('ready')
        self.stop()
        return set(Handler.authorizations)

    def test_the_readiness_route_is_asked_with_the_key_as_a_bearer_token(self):
        self.assertEqual(self.ready_with(self.KEY + '\n'), {f'Bearer {self.KEY}'})

    def test_a_name_equals_value_key_file_is_read_as_its_value(self):
        self.assertEqual(self.ready_with(f'  # written by the claim\nLITELLM_MASTER_KEY={self.KEY}\n'),
                         {f'Bearer {self.KEY}'})

    def test_a_bare_key_with_base64_padding_stays_whole(self):
        self.assertEqual(self.ready_with('abcdefghijklmnop==\n'), {'Bearer abcdefghijklmnop=='})

    def test_declared_secret_files_exist_at_the_launch_written_0600_whatever_the_umask(self):
        (self.root / 'api.key').write_text(self.KEY + '\n')
        stale = self.root / 'engine-secrets.toml'
        stale.write_text('old')
        stale.chmod(0o644)                                  # replaced, not reused with its looser mode
        self.write(secret_files=self.SECRET)
        seen = self.root / 'secret-seen'
        self.start(umask=0o022, SECRET_EXPECT=str(stale), SECRET_SEEN=str(seen))
        self.wait_for_phase('loading')
        self.assertEqual(seen.read_text().strip(), 'present')          # it was there when `docker run` ran
        self.assertEqual(stale.read_text(), f'[serve]\napi-key = "{self.KEY}"\n')
        self.assertEqual(stale.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.root.glob('.*.tmp')), [])
        self.stop()

    def refused(self, reason, **write):
        self.write(**write)
        process = self.start()
        self.assertIn(reason, self.wait_for_phase('failed')['reason'])
        process.wait(timeout=25)
        # refused before Docker was asked anything: no inspect, no rm, no run
        self.assertEqual(self.calls(), [])

    def test_secret_files_without_a_usable_key_refuse_before_docker_is_touched(self):
        for key in (None, 'short', 'has "quote" and spaces 0123456789'):
            with self.subTest(key=key):
                for name in ('api.key', 'calls', 'status.json'):
                    (self.root / name).unlink(missing_ok=True)
                if key is not None:
                    (self.root / 'api.key').write_text(key + '\n')
                self.refused('no usable key', secret_files=self.SECRET)
                self.assertFalse((self.root / 'engine-secrets.toml').exists())

    def test_a_symlinked_key_file_is_not_followed(self):
        outside = self.root / 'outside.key'
        outside.write_text(self.KEY + '\n')
        (self.root / 'api.key').symlink_to(outside)
        self.refused('no usable key', secret_files=self.SECRET)

    def test_a_secret_file_may_not_replace_a_file_the_claim_wrote(self):
        (self.root / 'api.key').write_text(self.KEY + '\n')
        for name in ('launch-args.json', 'ondemand', 'x' * 101):
            with self.subTest(name=name):
                (self.root / 'calls').unlink(missing_ok=True)
                self.refused('secret_files must be', secret_files=[{'path': name, 'text': '{api_key}'}])
        self.assertEqual(json.loads((self.root / 'launch-args.json').read_text()), self.launch)

    def test_one_bad_entry_means_no_entry_is_written(self):
        (self.root / 'api.key').write_text(self.KEY + '\n')
        self.refused('secret_files must be', secret_files=[*self.SECRET, {'path': 'b.toml', 'text': 'no key'}])
        self.assertFalse((self.root / 'engine-secrets.toml').exists())

    def test_a_write_that_fails_leaves_no_copy_of_the_key(self):
        (self.root / 'api.key').write_text(self.KEY + '\n')
        (self.root / 'engine-secrets.toml').mkdir()        # os.replace onto a directory fails
        self.write(secret_files=self.SECRET)
        process = self.start()
        self.wait_for_phase('failed')
        process.wait(timeout=25)
        self.assertEqual(sorted(p.name for p in self.root.glob('.*.tmp')), [])
        self.assertEqual([call for call in self.calls() if call.startswith(('run', 'rm'))], [])

    def test_the_worker_and_the_claim_reserve_the_same_names(self):
        import ast

        def literal(path, name):
            tree = ast.parse(path.read_text())
            return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                        and any(getattr(target, 'id', None) == name for target in node.targets))
        self.assertEqual(literal(ROOT / 'scripts/container-service-worker.py', 'RESERVED'),
                         literal(ROOT / 'scripts/borrow.py', 'SERVICE_FILES'))

if __name__ == '__main__':
    unittest.main()
