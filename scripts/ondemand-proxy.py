#!/usr/bin/env python3
"""On-demand front for one inference backend: load on the first request, unload when idle.

Runs on the worker and listens on the API port the control tunnel forwards to;
the backend serves on an internal loopback port. A request that arrives while
the backend is unloaded runs the optional admission check (host memory, GPU in
use; a refusal is answered 503 with its reason), the start command, waits for
the backend's /health and is then forwarded. When no byte has passed in either direction for idle_s
seconds (0 = never) the stop command unloads it. A backend stopped from outside
(release, RuntimeMaxSec, crash) is noticed and loaded again on the next request.

Passive requests neither load the model nor keep it loaded: GET /health and the
metadata GETs (/v1/models, /models, /props, /slots, /metrics), which the gateway
polls every 5-10 min, answer 503 unless the backend is ready (then the
backend answers). A metadata GET with X-Ondemand-Load: 1 (the gateway's
pre-call check) counts as use. Local endpoints: GET /ondemand/status, POST
/ondemand/warm (load now, wait until ready) and POST /ondemand/unload need the
backend's API key. With an identity config (manifest_file) the status also reports
what this front serves — backend id, manifest sha256, context, read from the
manifest file on this worker — so the gateway can refuse an alias whose declaration
no longer matches the loaded backend (IP-02, T4.1). A connection carries one request:
the head is forwarded with `Connection: close`, so the backend closes after its response and a second
request on the same connection is never forwarded uninspected. Bodies are forwarded as bytes
and never logged. Standard
library only (worker python3 >= 3.10).
"""
import argparse
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import time
import urllib.request

LIMIT = 2**20
METADATA = {'/v1/models', '/models', '/props', '/slots', '/metrics'}
REASONS = {200: 'OK', 400: 'Bad Request', 401: 'Unauthorized', 404: 'Not Found', 503: 'Service Unavailable'}


def log(event, **fields):
    print(json.dumps({'event': event, **fields}), flush=True)


def read_key(path):
    """Raw key file (llama.cpp api.key) or LITELLM_MASTER_KEY=... (Strata)."""
    return Path(path).read_text().strip().split('=', 1)[-1].strip()


def last_line(output):
    """The reason a start script gives is its last line (a traceback ends with the error)."""
    lines = output.strip().splitlines()
    return lines[-1] if lines else ''


def close_after_response(head):
    """HEAD (bytes, ending in CRLF CRLF) with every Connection/Keep-Alive header replaced by `Connection: close`."""
    lines = head[:-4].split(b'\r\n')
    kept = [lines[0]] + [line for line in lines[1:]
                         if line.split(b':', 1)[0].strip().lower() not in (b'connection', b'keep-alive')]
    return b'\r\n'.join(kept + [b'Connection: close']) + b'\r\n\r\n'


class LoadError(Exception):
    pass


class OnDemand:
    def __init__(self, config):
        self.config = config
        self.key = read_key(config['key_file'])
        self.state = 'unloaded'
        self.last_activity = time.monotonic()
        self.last_error = self.failed_at = None
        self.loads = self.unloads = 0
        self.connections = set()
        self.lock = None

    # Backend control; tests replace these three.
    async def command(self, argv, timeout):
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return 124, 'timeout'
        return process.returncode, output.decode(errors='replace').strip()[-300:]

    async def unit_state(self):
        _, output = await self.command(['systemctl', 'show', self.config['unit'], '-p', 'ActiveState', '--value'], 30)
        return output

    async def backend_ready(self):
        def probe():
            request = urllib.request.Request(f"http://127.0.0.1:{self.config['backend_port']}/health",
                                             headers={'Authorization': 'Bearer ' + self.key})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    body = json.loads(response.read() or b'{}')
                    return response.status == 200 and body.get('loaded', True) is not False
            except (OSError, ValueError):
                return False
        return await asyncio.to_thread(probe)

    def touch(self):
        self.last_activity = time.monotonic()

    def status(self):
        idle, idle_for = self.config['idle_s'], time.monotonic() - self.last_activity
        report = {'state': self.state, 'idle_limit_s': idle, 'idle_for_s': round(idle_for),
                  'unload_in_s': max(0, round(idle - idle_for)) if idle and self.state == 'ready' else None,
                  'loads': self.loads, 'unloads': self.unloads, 'last_error': self.last_error}
        identity = self.identity()
        if identity is not None:
            report['identity'] = identity
        return report

    def identity(self):
        """What this front serves, from the manifest file on this worker; None when nothing is configured.

        Read at status time, so a manifest replaced under the front is reported as it
        is now, not as it was at claim. A file that cannot be read or parsed is
        reported as an error: the gateway's comparison treats that as a mismatch, so
        a broken identity is never a silent pass.
        """
        path = Path(self.config['manifest_file']) if self.config.get('manifest_file') else None
        if path is None:
            return None
        try:
            raw = path.read_bytes()
            manifest = json.loads(raw)
            digest = hashlib.sha256(raw).hexdigest()
            if not isinstance(manifest, dict):
                raise ValueError('not an object')
        except (OSError, ValueError) as error:
            return {'error': f'{type(error).__name__}: {path.name}'}
        return {'backend_id': manifest.get('id', path.stem), 'manifest_hash': digest,
                'context': self.config.get('context') or manifest.get('context')}

    async def ensure_ready(self):
        if self.state == 'ready':
            return
        async with self.lock:
            if self.state == 'ready':
                return
            if self.failed_at is not None and time.monotonic() - self.failed_at < self.config.get('retry_after_failure_s', 60):
                raise LoadError(self.last_error)
            await self.load()

    async def load(self):
        self.state, started = 'loading', time.monotonic()
        log('loading')
        deadline = started + self.config['start_timeout_s']
        try:
            if self.config.get('check'):
                code, output = await self.command(self.config['check'], 120)
                if code:
                    raise LoadError(f'admission refused: {last_line(output)}')
            code, output = await self.command(self.config['start'], self.config['start_timeout_s'])
            if code:
                raise LoadError(f'start command exited {code}: {last_line(output)}')
            while not await self.backend_ready():
                if await self.unit_state() in ('failed', 'inactive'):
                    raise LoadError(f"{self.config['unit']} stopped while loading")
                if time.monotonic() > deadline:
                    raise LoadError('backend not ready before start timeout')
                await asyncio.sleep(self.config.get('poll_s', 2))
        except LoadError as error:
            self.state, self.last_error, self.failed_at = 'unloaded', str(error), time.monotonic()
            log('load_failed', error=str(error), seconds=round(time.monotonic() - started, 1))
            await self.command(self.config['stop'], 180)  # leave nothing half-started
            raise
        self.state, self.last_error, self.failed_at = 'ready', None, None
        self.loads += 1
        self.touch()
        log('ready', seconds=round(time.monotonic() - started, 1))

    async def unload(self, reason):
        async with self.lock:
            if reason == 'idle' and (self.state != 'ready' or time.monotonic() - self.last_activity < self.config['idle_s']):
                return
            self.state = 'unloading'
            for writer in list(self.connections):
                writer.close()
            code, _ = await self.command(self.config['stop'], 180)
            self.state = 'unloaded'
            self.unloads += 1
            log('unloaded', reason=reason, stop_returncode=code)

    async def monitor(self):
        while True:
            await asyncio.sleep(self.config.get('monitor_s', 15))
            if self.state != 'ready':
                continue
            if await self.unit_state() not in ('active', 'reloading'):
                self.state = 'unloaded'
                log('backend_gone')
            elif self.config['idle_s'] and time.monotonic() - self.last_activity >= self.config['idle_s']:
                await self.unload('idle')

    async def reply(self, writer, code, body):
        data = json.dumps(body).encode()
        writer.write(f'HTTP/1.1 {code} {REASONS[code]}\r\nContent-Type: application/json\r\n'
                     f'Content-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode() + data)
        try:
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()

    @staticmethod
    def header(head, wanted):
        for line in head.split(b'\r\n')[1:]:
            name, _, value = line.partition(b':')
            if name.strip().lower() == wanted:
                return value.strip()
        return None

    def authorized(self, head):
        value = self.header(head, b'authorization')
        return value is not None and hmac.compare_digest(value, b'Bearer ' + self.key.encode())

    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            method, target = head.split(b'\r\n', 1)[0].decode('latin-1').split(' ')[:2]
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ValueError):
            writer.close()
            return
        path = target.split('?', 1)[0]
        if path.startswith('/ondemand/'):
            if not self.authorized(head):
                return await self.reply(writer, 401, {'error': 'unauthorized'})
            if (method, path) == ('GET', '/ondemand/status'):
                return await self.reply(writer, 200, self.status())
            if (method, path) == ('POST', '/ondemand/warm'):
                self.touch()
                try:
                    await self.ensure_ready()
                except LoadError:
                    return await self.reply(writer, 503, self.status())
                return await self.reply(writer, 200, self.status())
            if (method, path) == ('POST', '/ondemand/unload'):
                await self.unload('request')
                return await self.reply(writer, 200, self.status())
            return await self.reply(writer, 404, {'error': 'unknown on-demand endpoint'})
        active = not (path == '/health' or method == 'GET' and path in METADATA
                      and self.header(head, b'x-ondemand-load') != b'1')
        if active and not self.authorized(head):
            return await self.reply(writer, 401, {'error': 'unauthorized'})
        if not active and self.state != 'ready':
            return await self.reply(writer, 503, {'status': self.state, 'ondemand': True})
        upstream = None
        for attempt in range(2):
            if active:
                self.touch()
                try:
                    await self.ensure_ready()
                except LoadError as error:
                    return await self.reply(writer, 503, {'error': {
                        'message': f'on-demand load failed: {error}', 'type': 'ondemand_unavailable'}})
            try:
                upstream = await asyncio.open_connection('127.0.0.1', self.config['backend_port'], limit=LIMIT)
                break
            except OSError:
                if self.state == 'ready':
                    self.state = 'unloaded'
                    log('backend_gone')
                if not active:
                    break
        if upstream is None:
            return await self.reply(writer, 503, {'error': {'message': 'backend unavailable', 'type': 'ondemand_unavailable'}})
        upstream_reader, upstream_writer = upstream
        self.connections.update((writer, upstream_writer))
        upstream_writer.write(close_after_response(head))
        try:
            await asyncio.gather(self.pipe(reader, upstream_writer, active), self.pipe(upstream_reader, writer, active))
        finally:
            for each in (writer, upstream_writer):
                self.connections.discard(each)
                each.close()

    async def pipe(self, source, sink, active):
        try:
            while data := await source.read(65536):
                sink.write(data)
                await sink.drain()
                if active:
                    self.touch()
            if sink.can_write_eof():
                sink.write_eof()
        except (ConnectionError, OSError):
            sink.close()

    async def start(self, host='127.0.0.1'):
        """Adopt an already loaded backend, start listening and the idle monitor; return the server."""
        self.lock = asyncio.Lock()
        if await self.unit_state() == 'active' and await self.backend_ready():
            self.state = 'ready'
            log('adopted')
        server = await asyncio.start_server(self.handle, host, self.config['listen_port'], limit=LIMIT)
        self.monitor_task = asyncio.create_task(self.monitor())
        log('listening', port=self.config['listen_port'], backend_port=self.config['backend_port'],
            idle_s=self.config['idle_s'], state=self.state)
        return server


async def serve(config):
    server = await OnDemand(config).start()
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('config', type=Path)
    asyncio.run(serve(json.loads(parser.parse_args().config.read_text())))


if __name__ == '__main__':
    main()
