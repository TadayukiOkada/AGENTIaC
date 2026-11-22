"""The worker's exit status: a requested stop is clean, so only a real failure marks the unit failed."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCKER = '''#!/bin/sh
echo "$@" >> "$CALLS"
case "$1" in
  inspect) [ "$2" = --format ] && echo true || exit 1 ;;
  top) echo PID; echo 1 ;;
  stop) exit "${STOP_STATUS:-0}" ;;
  run) exit "${RUN_STATUS:-0}" ;;
esac
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        worker = (ROOT / 'scripts/spark-tensorfold-worker.py').read_text()
        self.assertIn('100*(1<<30)', worker)
        (self.root / 'worker.py').write_text(worker.replace('100*(1<<30)', '0'))  # this host need not have a spark worker's memory
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        (self.root / 'manifest.json').write_text(json.dumps({'container': 'test', 'port': port}))
        (self.root / 'launch-args.json').write_text(json.dumps(['docker', 'run']))
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        for name, text in (('docker', DOCKER), ('nvidia-smi', '#!/bin/sh\n')):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.env = {**os.environ, 'PATH': f"{bin_dir}{os.pathsep}{os.environ['PATH']}", 'CALLS': str(self.root / 'calls')}

    def status(self):
        path = self.root / 'status.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def run_until_loading_then_stop(self, **env):
        process = subprocess.Popen([sys.executable, str(self.root / 'worker.py')], env={**self.env, **env}, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 20
        while self.status().get('phase') != 'loading':
            self.assertLess(time.monotonic(), deadline, 'worker never reached loading')
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=20)
        return process.returncode, stderr

    def test_a_requested_stop_is_a_clean_exit_and_stops_the_container(self):
        code, stderr = self.run_until_loading_then_stop()
        self.assertEqual(code, 0)
        self.assertIn('service termination requested', stderr)
        self.assertEqual(self.status()['phase'], 'stopped')
        self.assertEqual(self.status()['reason'], 'service termination requested')
        self.assertIn('stop -t 10 test', (self.root / 'calls').read_text())

    def test_a_container_that_will_not_stop_is_a_failure(self):
        code, _ = self.run_until_loading_then_stop(STOP_STATUS='1')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.status()['stop_returncode'], 1)

    def test_a_failed_launch_is_a_failure(self):
        done = subprocess.run([sys.executable, str(self.root / 'worker.py')], env={**self.env, 'RUN_STATUS': '1'}, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.status()['phase'], 'failed')


if __name__ == '__main__':
    unittest.main()
