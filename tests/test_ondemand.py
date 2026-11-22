"""On-demand front: load on first request, passive probes never load, idle unload, failed loads, auth."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'gateway'))
from agentiac import paths  # noqa: E402
SITE = paths.root()
import identity_policy  # noqa: E402

spec = importlib.util.spec_from_file_location('ondemand_proxy', ROOT / 'scripts/ondemand-proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
proxy.log = lambda event, **fields: None


class FakeBackend(proxy.OnDemand):
    """Start/stop an in-process HTTP backend instead of a systemd unit."""
    fail_start = False
    refuse = False

    def __init__(self, config):
        super().__init__(config)
        self.server, self.calls = None, []

    async def command(self, argv, timeout):
        self.calls.append(argv[0])
        if argv[0] == 'check':
            return (1, 'Traceback (most recent call last):\n  ...\nGPU in use by another workload (2900 MiB)') if self.refuse else (0, '')
        if argv[0] == 'start':
            if self.fail_start:
                return 1, 'host busy'
            self.server = await asyncio.start_server(self.backend, '127.0.0.1', self.config['backend_port'])
        elif self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        return 0, ''

    async def unit_state(self):
        return 'active' if self.server else 'inactive'

    async def backend(self, reader, writer):
        head = await reader.readuntil(b'\r\n\r\n')
        body = json.dumps({'path': head.split(b' ')[1].decode()}).encode()
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s' % (len(body), body))
        await writer.drain()
        writer.close()


def free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class OnDemandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        key_file = Path(self.directory.name) / 'api-key.env'
        key_file.write_text('LITELLM_MASTER_KEY=secret\n')
        self.ondemand = FakeBackend({'listen_port': 0, 'backend_port': free_port(), 'key_file': str(key_file),
                                     'unit': 'fake', 'start': ['start'], 'stop': ['stop'], 'idle_s': 0.4,
                                     'start_timeout_s': 5, 'poll_s': 0.05, 'monitor_s': 0.05})
        self.server = await self.ondemand.start()
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.ondemand.monitor_task.cancel()
        self.server.close()
        await self.ondemand.command(['stop'], 1)
        self.directory.cleanup()

    async def get(self, path, method='GET', key='secret', load=False):
        def call():
            headers = {'Authorization': 'Bearer ' + key} if key else {}
            if load:
                headers['X-Ondemand-Load'] = '1'
            request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)
        return await asyncio.to_thread(call)

    async def test_first_request_loads_and_is_forwarded_but_passive_probes_never_load(self):
        for path in ('/health', '/v1/models', '/models', '/props', '/slots', '/metrics'):
            self.assertEqual(await self.get(path), (503, {'status': 'unloaded', 'ondemand': True}))
        self.assertEqual(self.ondemand.calls, [])
        self.assertEqual(await self.get('/v1/models', load=True), (200, {'path': '/v1/models'}))
        self.assertEqual((self.ondemand.state, self.ondemand.calls), ('ready', ['start']))
        self.assertEqual(await self.get('/health'), (200, {'path': '/health'}))
        self.assertEqual(await self.get('/v1/models'), (200, {'path': '/v1/models'}))

    async def test_idle_backend_is_unloaded_and_loaded_again_on_demand(self):
        await self.get('/v1/models', load=True)
        for _ in range(3):  # probes and the gateway's model polls do not count as use
            await asyncio.sleep(0.15)
            await self.get('/health')
            await self.get('/v1/models')
        await asyncio.sleep(0.3)
        self.assertEqual((self.ondemand.state, self.ondemand.calls), ('unloaded', ['start', 'stop']))
        self.assertEqual((await self.get('/v1/chat/completions', 'POST'))[0], 200)
        self.assertEqual((self.ondemand.loads, self.ondemand.unloads), (2, 1))

    async def test_failed_load_returns_503_cleans_up_and_does_not_retry_at_once(self):
        self.ondemand.fail_start = True
        code, body = await self.get('/v1/models', load=True)
        self.assertEqual(code, 503)
        self.assertIn('host busy', body['error']['message'])
        self.assertEqual(await self.get('/v1/models', load=True), (code, body))
        self.assertEqual(self.ondemand.calls, ['start', 'stop'])

    async def test_admission_refusal_answers_with_its_reason_and_never_starts(self):
        self.ondemand.config['check'] = ['check']
        self.ondemand.refuse = True
        code, body = await self.get('/v1/chat/completions', 'POST')
        self.assertEqual((code, body['error']['type']), (503, 'ondemand_unavailable'))
        self.assertEqual(body['error']['message'], 'on-demand load failed: admission refused: GPU in use by another workload (2900 MiB)')
        self.assertEqual(self.ondemand.calls, ['check', 'stop'])
        self.ondemand.refuse, self.ondemand.failed_at = False, None  # the owner's job ended; retry period over
        self.assertEqual((await self.get('/v1/chat/completions', 'POST'))[0], 200)
        self.assertEqual(self.ondemand.calls, ['check', 'stop', 'check', 'start'])

    async def test_backend_stopped_outside_the_front_is_loaded_again(self):
        await self.get('/v1/chat/completions', 'POST')
        await self.ondemand.command(['stop'], 1)
        self.assertEqual(await self.get('/v1/chat/completions', 'POST'), (200, {'path': '/v1/chat/completions'}))
        self.assertEqual(self.ondemand.loads, 2)

    async def test_control_endpoints_need_the_key(self):
        self.assertEqual((await self.get('/ondemand/status', key=None))[0], 401)
        self.assertEqual((await self.get('/ondemand/status', key='wrong'))[0], 401)
        code, status = await self.get('/ondemand/warm', 'POST', key='secret')
        self.assertEqual((code, status['state']), (200, 'ready'))
        code, status = await self.get('/ondemand/unload', 'POST', key='secret')
        self.assertEqual((code, status['state'], status['unloads']), (200, 'unloaded', 1))

    async def test_unauthorized_requests_cannot_load_or_keep_a_worker_alive(self):
        for path, method, load in (('/v1/chat/completions', 'POST', False), ('/v1/models', 'GET', True)):
            for key in (None, 'wrong'):
                self.assertEqual((await self.get(path, method, key=key, load=load))[0], 401)
        self.assertEqual(self.ondemand.calls, [])
        await self.get('/v1/models', load=True)
        last_activity = self.ondemand.last_activity
        self.assertEqual((await self.get('/v1/chat/completions', 'POST', key=None))[0], 401)
        self.assertEqual(self.ondemand.last_activity, last_activity)
        self.assertEqual(self.ondemand.calls, ['start'])


class OneRequestPerConnectionTests(unittest.IsolatedAsyncioTestCase):
    """A connection carries one request: a second one would bypass classification and authentication (F04)."""

    async def asyncSetUp(self):
        self.recorded = []

        async def backend(reader, writer):
            # unlike FakeBackend, this one keeps the connection open unless the head says close
            try:
                while True:
                    head = await reader.readuntil(b'\r\n\r\n')
                    length = proxy.OnDemand.header(head, b'content-length')
                    if length:
                        await reader.readexactly(int(length))
                    self.recorded.append((head.split(b'\r\n', 1)[0], head.lower()))
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
                    await writer.drain()
                    if b'connection: close' in head.lower():
                        break
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            writer.close()

        self.backend = await asyncio.start_server(backend, '127.0.0.1', 0)
        self.addCleanup(self.backend.close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        key_file = Path(directory.name) / 'api-key.env'
        key_file.write_text('LITELLM_MASTER_KEY=secret\n')
        self.front = proxy.OnDemand({'listen_port': 0, 'backend_port': self.backend.sockets[0].getsockname()[1],
                                     'key_file': str(key_file), 'unit': 'fake', 'start': ['start'],
                                     'stop': ['stop'], 'idle_s': 0.4, 'start_timeout_s': 5, 'poll_s': 0.05,
                                     'monitor_s': 0.05})
        self.front.state, self.front.lock = 'ready', asyncio.Lock()
        self.server = await asyncio.start_server(self.front.handle, '127.0.0.1', 0)
        self.addCleanup(self.server.close)
        self.reader, self.writer = await asyncio.open_connection('127.0.0.1', self.server.sockets[0].getsockname()[1])
        self.addCleanup(self.writer.close)

    async def response(self):
        head = await asyncio.wait_for(self.reader.readuntil(b'\r\n\r\n'), 5)
        return head + await asyncio.wait_for(self.reader.readexactly(int(proxy.OnDemand.header(head, b'content-length'))), 5)

    POST = (b'POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer secret\r\n'
            b'Connection: keep-alive\r\nContent-Length: 2\r\n\r\n{}')

    async def test_the_backend_is_told_to_close(self):
        self.writer.write(self.POST)
        await self.response()
        self.assertEqual(len(self.recorded), 1)
        self.assertIn(b'connection: close', self.recorded[0][1])
        self.assertNotIn(b'keep-alive', self.recorded[0][1])

    async def test_a_second_request_on_the_connection_is_not_forwarded(self):
        self.writer.write(b'GET /health HTTP/1.1\r\nHost: x\r\n\r\n')
        await self.response()
        self.writer.write(b'POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}')
        self.assertEqual(await asyncio.wait_for(self.reader.read(), 5), b'')
        self.assertEqual(len(self.recorded), 1)

    async def test_a_passive_request_after_an_active_one_does_not_count_as_use(self):
        self.writer.write(self.POST)
        await self.response()
        self.front.last_activity -= 1000
        self.writer.write(b'GET /health HTTP/1.1\r\nHost: x\r\n\r\n')
        await asyncio.wait_for(self.reader.read(), 5)
        self.assertGreater(time.monotonic() - self.front.last_activity, 900)

    def test_close_after_response_replaces_existing_headers(self):
        head = (b'POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nConnection: keep-alive, Upgrade\r\n'
                b'Keep-Alive: timeout=5\r\nContent-Length: 2\r\n\r\n')
        result = proxy.close_after_response(head)
        lines = result[:-4].split(b'\r\n')
        self.assertEqual([line for line in lines if line.lower().startswith(b'connection')], [b'Connection: close'])
        self.assertEqual(lines[:3], [b'POST /v1/chat/completions HTTP/1.1', b'Host: x', b'Content-Length: 2'])
        self.assertNotIn(b'keep-alive', result.lower())
        self.assertTrue(result.endswith(b'\r\n\r\n'))


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    """What the front reports about the model it serves is what the gateway compares (IP-02, T4.1)."""

    MANIFEST = {'id': 'strata-qwen38-gyro-m-0.2.8', 'context': 131072, 'quantization': 'gyo-m'}

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'api-key.env').write_text('LITELLM_MASTER_KEY=secret\n')
        self.manifest = self.root / 'backends/strata-0.2.8-qfn-gyro-m-tq2_0.json'
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps(self.MANIFEST) + '\n')
        self.ondemand = FakeBackend({'listen_port': 0, 'backend_port': free_port(), 'key_file': str(self.root / 'api-key.env'),
                                     'unit': 'fake', 'start': ['start'], 'stop': ['stop'], 'idle_s': 0,
                                     'start_timeout_s': 5, 'manifest_file': str(self.manifest)})
        self.server = await self.ondemand.start()
        self.addCleanup(self.server.close)
        self.addCleanup(self.ondemand.monitor_task.cancel)
        self.port = self.server.sockets[0].getsockname()[1]

    async def status(self):
        def call():
            request = urllib.request.Request(f'http://127.0.0.1:{self.port}/ondemand/status',
                                             headers={'Authorization': 'Bearer secret'})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        return await asyncio.to_thread(call)

    async def test_the_status_names_the_backend_its_bytes_and_its_context(self):
        identity = (await self.status())['identity']
        self.assertEqual(identity['backend_id'], 'strata-qwen38-gyro-m-0.2.8')
        self.assertEqual(identity['manifest_hash'], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        self.assertEqual(identity['context'], 131072)

    async def test_a_manifest_replaced_under_the_front_is_reported_as_it_is_now(self):
        # the report is read at status time, so a swapped manifest cannot be hidden by a stale claim
        self.manifest.write_text(json.dumps(self.MANIFEST | {'id': 'strata-qwen38-gyro-m'}) + '\n')
        identity = (await self.status())['identity']
        self.assertEqual(identity['backend_id'], 'strata-qwen38-gyro-m')
        self.assertNotEqual(identity['manifest_hash'], hashlib.sha256(json.dumps(self.MANIFEST).encode() + b'\n').hexdigest())

    async def test_a_manifest_that_cannot_be_read_is_reported_as_an_error_never_as_nothing(self):
        self.manifest.unlink()
        self.assertIn('error', (await self.status())['identity'])
        self.manifest.write_text('{not json\n')
        self.assertIn('error', (await self.status())['identity'])
        self.manifest.write_text('["valid json", "not a manifest"]\n')
        self.assertIn('error', (await self.status())['identity'])

    async def test_a_front_without_a_manifest_reports_no_identity(self):
        self.ondemand.config.pop('manifest_file')
        self.assertNotIn('identity', await self.status())

    async def test_the_served_context_of_the_claim_wins_over_the_manifest(self):
        # a claim may serve a narrower window than the manifest allows; the report says what is served
        self.ondemand.config['context'] = 65536
        self.assertEqual((await self.status())['identity']['context'], 65536)

    async def test_the_report_is_what_the_gateway_expects_for_a_declared_backend(self):
        # the two sides of the comparison are built independently: this is where they must agree
        stems = [p.stem for p in sorted((SITE / 'backends').glob('*.json'))
                 if p.name != 'schema.json' and not p.name.endswith('.launch.json')]
        self.assertTrue(stems)
        for stem in stems:
            self.ondemand.config['manifest_file'] = str(SITE / 'backends' / f'{stem}.json')
            reported = (await self.status())['identity']
            expected = identity_policy.expected_identity(SITE, stem)
            with self.subTest(stem=stem):
                self.assertEqual(reported, expected)
                self.assertIsNone(identity_policy.identity_problem(expected, reported))


if __name__ == '__main__':
    unittest.main()
