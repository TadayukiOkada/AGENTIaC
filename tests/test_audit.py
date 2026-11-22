"""T9.7: the lock audit reports, keeps going, and sends only names and versions. No test reaches the network."""
import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

from agentiac import audit, doctor  # noqa: E402

NOW = '2026-11-02T10:00:00+00:00'
LOCKFILE = {'packages': {'': {'name': 'root', 'version': '0.0.0'},
                         'node_modules/undici': {'version': '8.10.2'},
                         'node_modules/a/node_modules/undici': {'version': '8.10.2'},
                         'node_modules/alias': {'name': 'real-name', 'version': '1.0.0'},
                         'node_modules/@scope/pkg': {'version': '2.0.0'},
                         'node_modules/linked': {'link': True}}}


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Osv:
    """OSV with HITS {(name, version): [ids]} and DETAILS {id: record}. It keeps what each request sent."""

    def __init__(self, hits=None, details=None, failing=()):
        self.hits, self.details, self.failing, self.sent, self.requests = hits or {}, details or {}, failing, [], []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if any(text in request.full_url for text in self.failing):
            raise urllib.error.URLError('down')
        if request.full_url.endswith('/querybatch'):
            body = json.loads(request.data)
            self.sent += body['queries']
            return Answer(json.dumps({'results': [self.answer(query) for query in body['queries']]}).encode())
        identifier = request.full_url.rsplit('/', 1)[1]
        return Answer(json.dumps(self.details.get(identifier, {'database_specific': {'severity': 'HIGH'}})).encode())

    def answer(self, query):
        ids = self.hits.get((query['package']['name'], query['version']), [])
        return {'vulns': [{'id': identifier} for identifier in ids]} if ids else {}


class Checkout(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'agents/pi').mkdir(parents=True)
        (self.root / 'agents/pi/package-lock.json').write_text(json.dumps(LOCKFILE))
        (self.root / 'requirements-control.lock').write_text('# a comment==1\nansible-core==2.21.5\n')
        (self.root / 'requirements-gateway.lock').write_text('filelock==3.32.7\nlitellm==1.106.6\n')
        (self.root / 'requirements-lint.lock').write_text('yamllint==1.0\n')

    def run_audit(self, opener=None, previous=None):
        return audit.run(self.root, previous, opener or Osv(), NOW)

    def lock(self, report, name):
        return next(entry for entry in report['locks'] if entry['lock'] == name)


class PackageTests(Checkout):
    def test_an_npm_lock_gives_each_installed_package_once(self):
        self.assertEqual(audit.packages(self.root, 'agents/pi/package-lock.json', 'npm'),
                         [('@scope/pkg', '2.0.0'), ('real-name', '1.0.0'), ('undici', '8.10.2')])

    def test_a_python_lock_gives_its_pins_and_not_its_comments(self):
        self.assertEqual(audit.packages(self.root, 'requirements-control.lock', 'PyPI'), [('ansible-core', '2.21.5')])

    def test_an_empty_lock_is_an_error_and_not_a_clean_result(self):
        (self.root / 'requirements-lint.lock').write_text('')
        with self.assertRaisesRegex(ValueError, 'names no package'):
            audit.packages(self.root, 'requirements-lint.lock', 'PyPI')

    def test_every_shipped_lock_has_packages(self):
        for lock, ecosystem in audit.LOCKS:
            self.assertTrue(audit.packages(ROOT, lock, ecosystem), lock)


class ResultTests(Checkout):
    def test_an_advisory_is_kept_with_its_package_and_severity(self):
        report = self.run_audit(Osv({('undici', '8.10.2'): ['GHSA-1']}, {'GHSA-1': {'database_specific': {'severity': 'MODERATE'}}}))
        self.assertEqual(self.lock(report, 'agents/pi/package-lock.json')['advisories'],
                         [{'id': 'GHSA-1', 'package': 'undici', 'version': '8.10.2', 'severity': 'moderate'}])

    def test_a_clean_lock_counts_its_packages(self):
        locked = self.lock(self.run_audit(), 'requirements-gateway.lock')
        self.assertEqual((locked['packages'], locked['advisories'], locked['stale'], locked['fetched_at']), (2, [], False, NOW))

    def test_each_ecosystem_is_asked_by_its_own_name(self):
        opener = Osv()
        self.run_audit(opener)
        kinds = {query['package']['name']: query['package']['ecosystem'] for query in opener.sent}
        self.assertEqual((kinds['undici'], kinds['litellm']), ('npm', 'PyPI'))

    def test_a_withdrawn_advisory_is_left_out(self):
        report = self.run_audit(Osv({('litellm', '1.106.6'): ['GHSA-1']}, {'GHSA-1': {'withdrawn': '2026-01-01T00:00:00Z'}}))
        self.assertEqual(self.lock(report, 'requirements-gateway.lock')['advisories'], [])

    def test_an_advisory_whose_detail_cannot_be_read_still_counts(self):
        report = self.run_audit(Osv({('litellm', '1.106.6'): ['GHSA-1']}, failing=('/vulns/',)))
        self.assertEqual(self.lock(report, 'requirements-gateway.lock')['advisories'][0]['severity'], 'unknown')

    def test_a_severity_nobody_declared_is_unknown(self):
        for record in ({}, {'database_specific': {}}, {'database_specific': {'severity': 'URGENT'}}):
            report = self.run_audit(Osv({('litellm', '1.106.6'): ['GHSA-1']}, {'GHSA-1': record}))
            self.assertEqual(self.lock(report, 'requirements-gateway.lock')['advisories'][0]['severity'], 'unknown')

    def test_a_second_page_is_followed(self):
        class Paged(Osv):
            def answer(self, query):
                if query.get('page_token'):
                    return {'vulns': [{'id': 'GHSA-2'}]}
                return {'vulns': [{'id': 'GHSA-1'}], 'next_page_token': 'more'} if query['package']['name'] == 'litellm' else {}
        ids = [item['id'] for item in self.lock(self.run_audit(Paged()), 'requirements-gateway.lock')['advisories']]
        self.assertEqual(ids, ['GHSA-1', 'GHSA-2'])

    def test_a_large_lock_is_split_into_batches(self):
        (self.root / 'requirements-gateway.lock').write_text(''.join(f'p{index}==1\n' for index in range(audit.BATCH + 1)))
        opener = Osv()
        self.run_audit(opener)
        sizes = [len(json.loads(request.data)['queries']) for request in opener.requests
                 if b'p0' in (request.data or b'') or b'"p' in (request.data or b'')]
        self.assertEqual(max(sizes), audit.BATCH)
        self.assertEqual(self.lock(self.run_audit(), 'requirements-gateway.lock')['packages'], audit.BATCH + 1)

    def test_too_few_answers_is_a_failure_and_not_a_clean_result(self):
        class Short(Osv):
            def __call__(self, request, timeout=None):
                return Answer(b'{"results": []}')
        locked = self.lock(self.run_audit(Short()), 'requirements-lint.lock')
        self.assertIsNone(locked['packages'])
        self.assertIn('answers', locked['reason'])


class FailureTests(Checkout):
    def test_a_lock_that_cannot_be_read_costs_that_lock(self):
        (self.root / 'requirements-lint.lock').unlink()
        report = self.run_audit()
        self.assertIsNone(self.lock(report, 'requirements-lint.lock')['packages'])
        self.assertEqual(self.lock(report, 'requirements-gateway.lock')['packages'], 2)

    def test_a_failed_read_keeps_the_last_answer_marked_stale(self):
        first = self.run_audit(Osv({('litellm', '1.106.6'): ['GHSA-1']}))
        locked = self.lock(self.run_audit(Osv(failing=('querybatch',)), first), 'requirements-gateway.lock')
        self.assertEqual((locked['stale'], len(locked['advisories']), locked['fetched_at']), (True, 1, NOW))
        self.assertIn('URLError', locked['reason'])

    def test_no_last_answer_is_unread_and_not_clean(self):
        locked = self.lock(self.run_audit(Osv(failing=('querybatch',))), 'requirements-control.lock')
        self.assertEqual((locked['packages'], locked['stale']), (None, False))

    def test_a_record_that_cannot_be_used_costs_the_stale_answers_only(self):
        for text in ('not json', '{}', '{"schema_version": 2, "locks": []}', '{"schema_version": 1, "locks": 5}'):
            path = self.root / 'audit.json'
            path.write_text(text)
            self.assertIsNone(audit.load(path), text)
        self.assertIsNone(audit.load(self.root / 'missing.json'))


class CountTests(Checkout):
    def report(self):
        return self.run_audit(Osv({('undici', '8.10.2'): ['GHSA-1', 'GHSA-2'], ('litellm', '1.106.6'): ['GHSA-3']},
                                  {'GHSA-2': {'database_specific': {'severity': 'LOW'}}}))

    def test_counts(self):
        numbers = audit.counts(self.report())
        self.assertEqual((numbers['advisories'], numbers['packages'], numbers['severity']['high'], numbers['severity']['low']),
                         (3, 2, 2, 1))

    def test_a_package_in_two_locks_is_one_finding(self):
        (self.root / 'requirements-control.lock').write_text('litellm==1.106.6\n')
        self.assertEqual(audit.counts(self.report())['advisories'], 3)

    def test_the_line_gives_counts_and_names_no_package(self):
        text = audit.line(self.report())
        self.assertIn('3 advisories in 2 packages (2 high, 1 low)', text)
        self.assertNotIn('undici', text)

    def test_nothing_read_is_not_written_as_a_clean_zero(self):
        text = audit.line(self.run_audit(Osv(failing=('querybatch',))))
        self.assertIn('4 locks not read', text)

    def test_a_stale_lock_is_said(self):
        first = self.run_audit()
        self.assertIn('4 locks stale', audit.line(self.run_audit(Osv(failing=('querybatch',)), first)))


class PrivacyTests(Checkout):
    def test_only_a_name_a_version_and_an_ecosystem_leave(self):
        opener = Osv({('litellm', '1.106.6'): ['GHSA-1']})
        self.run_audit(opener)
        for query in opener.sent:
            self.assertEqual(set(query), {'package', 'version'})
            self.assertEqual(set(query['package']), {'name', 'ecosystem'})

    def test_no_key_is_sent(self):
        opener = Osv({('litellm', '1.106.6'): ['GHSA-1']})
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'x', 'LITELLM_MASTER_KEY': 'y'}):
            self.run_audit(opener)
        for request in opener.requests:
            self.assertEqual({name.lower() for name in request.headers}, {'content-type'} if request.data else set())

    def test_it_talks_to_one_host(self):
        opener = Osv({('litellm', '1.106.6'): ['GHSA-1']})
        self.run_audit(opener)
        self.assertEqual({request.host for request in opener.requests}, {'api.osv.dev'})

    def test_the_lock_contents_are_not_changed(self):
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.run_audit()
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)

    def test_it_runs_no_process(self):
        with mock.patch('subprocess.Popen', side_effect=AssertionError('an audit must not run anything')), \
                mock.patch('socket.socket', side_effect=AssertionError('a test opened a connection')):
            self.run_audit()


class DoctorTests(Checkout):
    def save(self, report):
        path = self.root / 'artifacts/components/audit.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report))

    def check(self, now='2026-11-03T10:00:00+00:00'):
        return doctor.audit_check(self.root, datetime.datetime.fromisoformat(now))

    def test_no_audit_is_a_note_that_says_what_to_run(self):
        found = self.check()
        self.assertEqual((found['status'], found['next']), ('note', f'{doctor.cli_name()} ops update check'))

    def test_a_clean_audit_is_ok(self):
        self.save(self.run_audit())
        self.assertEqual(self.check()['status'], 'ok')

    def test_a_finding_is_a_note_and_not_a_problem(self):
        self.save(self.run_audit(Osv({('litellm', '1.106.6'): ['GHSA-1']})))
        found = self.check()
        self.assertEqual(found['status'], 'note')
        self.assertIn('1 advisories in 1 packages', found['evidence'])

    def test_a_lock_nobody_read_is_a_note(self):
        self.save(self.run_audit(Osv(failing=('querybatch',))))
        self.assertEqual(self.check()['status'], 'note')

    def test_an_old_audit_says_the_timer_has_stopped(self):
        self.save(self.run_audit())
        found = self.check('2026-11-06T11:00:00+00:00')
        self.assertEqual(found['status'], 'note')
        self.assertIn('the daily check has stopped', found['evidence'])
        self.assertIn('update-check.timer', found['next'])

    def test_three_days_is_not_yet_old(self):
        self.save(self.run_audit())
        self.assertEqual(self.check('2026-11-05T10:00:00+00:00')['status'], 'ok')

    def test_doctor_opens_no_connection_and_runs_no_process(self):
        self.save(self.run_audit())
        blocked = AssertionError('doctor must only read the saved audit')
        with mock.patch('urllib.request.urlopen', side_effect=blocked), mock.patch('socket.socket', side_effect=blocked), \
                mock.patch('subprocess.Popen', side_effect=blocked):
            self.assertEqual(self.check()['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
