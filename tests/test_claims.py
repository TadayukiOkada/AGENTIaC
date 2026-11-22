"""F05: control keeps a record of its own claims (D2.2), and borrow.py writes and clears it (T2.3)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import paths  # noqa: E402
SITE = paths.root()
from agentiac import claims, mutation  # noqa: E402
import borrow  # noqa: E402


class ClaimRecordTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        environment = mock.patch.dict(os.environ)  # these tests write under their own root
        self.addCleanup(environment.stop)
        environment.start()
        os.environ.pop(mutation.STATE_ROOT, None)

    def test_a_record_reads_back_with_its_actor(self):
        claims.write(self.root, 'worker-a', 'strata-0.2.13-dsv41-coder-iq2_xxs', 131072, 30, 'claim')
        record = claims.read(self.root, 'worker-a')
        self.assertEqual({k: record[k] for k in ('host', 'backend', 'context', 'idle_unload_minutes', 'by', 'actor')},
                         {'host': 'worker-a', 'backend': 'strata-0.2.13-dsv41-coder-iq2_xxs', 'context': 131072,
                          'idle_unload_minutes': 30, 'by': 'claim', 'actor': 'operator'})
        self.assertIn('claimed_at', record)

    def test_a_second_claim_overwrites_and_leaves_one_file(self):
        claims.write(self.root, 'worker-b', 'a', 1, 30, 'claim')
        claims.write(self.root, 'worker-b', 'b', 2, 0, 'reclaim')
        self.assertEqual(os.listdir(Path(self.root) / claims.DIRECTORY), ['worker-b.json'])
        self.assertEqual((claims.read(self.root, 'worker-b')['backend'], claims.read(self.root, 'worker-b')['by']),
                         ('b', 'reclaim'))

    def test_clear_removes_and_is_silent_when_nothing_is_there(self):
        claims.clear(self.root, 'worker-e')
        claims.write(self.root, 'worker-e', 'g', 1, 30, 'claim')
        claims.clear(self.root, 'worker-e')
        self.assertIsNone(claims.read(self.root, 'worker-e'))
        claims.clear(self.root, 'worker-e')

    def test_all_returns_every_host(self):
        for host in ('worker-d', 'worker-e', 'worker-a'):
            claims.write(self.root, host, host + '-backend', 1, 30, 'claim')
        self.assertEqual(sorted(claims.all(self.root)), ['worker-a', 'worker-d', 'worker-e'])
        self.assertEqual(claims.all(self.root)['worker-e']['backend'], 'worker-e-backend')

    def test_a_host_name_that_is_not_a_file_name_is_refused(self):
        for host in ('../x', 'WorkerA', '', '-x', 'a/b', None):
            with self.subTest(host=host), self.assertRaises(ValueError):
                claims.write(self.root, host, 'b', 1, 30, 'claim')

    def test_a_corrupt_record_reads_as_none_and_does_not_break_all(self):
        claims.write(self.root, 'worker-a', 'b', 1, 30, 'claim')
        directory = Path(self.root) / claims.DIRECTORY
        (directory / 'worker-d.json').write_text('{not json')
        (directory / 'worker-e.json').write_text(json.dumps(['not', 'a', 'dict']))
        self.assertIsNone(claims.read(self.root, 'worker-d'))
        self.assertIsNone(claims.read(self.root, 'worker-e'))
        self.assertEqual(list(claims.all(self.root)), ['worker-a'])
        self.assertIsNone(claims.read(self.root, 'worker-c'))  # missing

    def test_a_redirected_record_lives_under_the_override_only(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            os.environ[mutation.STATE_ROOT] = elsewhere
            claims.write(self.root, 'worker-a', 'b', 1, 30, 'claim')
            self.assertTrue((Path(elsewhere) / claims.DIRECTORY / 'worker-a.json').is_file())
            self.assertEqual(claims.read(self.root, 'worker-a')['backend'], 'b')
            self.assertEqual(list(claims.all(self.root)), ['worker-a'])
            self.assertEqual(os.listdir(self.root), [])
            claims.clear(self.root, 'worker-a')
            self.assertIsNone(claims.read(self.root, 'worker-a'))
            self.assertEqual(os.listdir(Path(elsewhere) / claims.DIRECTORY), [])


class BorrowRecordTests(unittest.TestCase):
    """borrow.py writes the record after a claim succeeded and clears it after a release succeeded (T2.3)."""

    def setUp(self):
        for name in ('claims', 'mutation'):  # never the real artifacts/
            patcher = mock.patch.object(borrow, name)
            self.addCleanup(patcher.stop)
            patcher.start()
        for name in ('enable_ondemand', 'start_tunnel', 'warm', 'weight_guard'):
            patcher = mock.patch.object(borrow, name, mock.Mock())
            self.addCleanup(patcher.stop)
            patcher.start()
        quiet = mock.patch('builtins.print')  # claim and release print their JSON line
        self.addCleanup(quiet.stop)
        quiet.start()

    def test_a_claim_writes_the_record(self):
        hosts = borrow.hosts()
        dedicated = next(h for h, f in hosts.items() if f.get('strata_backend'))
        host = hosts[dedicated]
        backend = host['strata_backend']
        context = json.loads((SITE / f'backends/{backend}.json').read_text())['context']
        borrow.claim(dedicated, host, False, 30, False)
        borrow.claims.write.assert_called_once_with(borrow.ROOT, dedicated, backend, context, 30, 'claim')

    def test_a_failed_claim_writes_nothing(self):
        hosts = borrow.hosts()
        dedicated = next(h for h, f in hosts.items() if f.get('strata_backend'))
        borrow.start_tunnel.side_effect = subprocess.CalledProcessError(1, 'x')
        with self.assertRaises(subprocess.CalledProcessError):
            borrow.claim(dedicated, hosts[dedicated], False, 30, False)
        borrow.claims.write.assert_not_called()

    def test_a_reclaim_says_so(self):
        default = json.loads(borrow.route.ROUTES.read_text())['default']
        with mock.patch.object(borrow, 'claim') as claim, mock.patch.object(borrow.route, 'current', return_value=default):
            borrow.reclaim(borrow.hosts())
        self.assertEqual(claim.call_args.kwargs['by'], 'reclaim')

    def release(self, returncode):
        default = json.loads(borrow.route.ROUTES.read_text())['default']
        hosts = borrow.hosts()
        borrowed = next(h for h, f in hosts.items() if f.get('borrowed') and default not in borrow.targets_on(h))
        report = subprocess.CompletedProcess([], returncode, 'units=\nuser_units=\ncontainers=\ngpu_apps=\n', '')
        with mock.patch.object(borrow, 'on_host', return_value=report), \
             mock.patch.object(borrow.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
             mock.patch.object(borrow.route, 'current', return_value=default):
            borrow.release(borrowed, hosts[borrowed])
        return borrowed

    def test_a_release_clears_the_record(self):
        borrowed = self.release(0)
        borrow.claims.clear.assert_called_once_with(borrow.ROOT, borrowed)

    def test_a_failed_remote_release_keeps_the_record(self):
        with self.assertRaises(SystemExit):
            self.release(1)
        borrow.claims.clear.assert_not_called()


if __name__ == '__main__':
    unittest.main()
