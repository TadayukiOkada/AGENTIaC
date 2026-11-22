"""Probe executor fixtures and a fail-closed live entry pending bounded SSH recovery."""
from .probe import LiveProbeExecutor

class FakeExecutor:
    def run(self, job):
        if job['operation'] != 'probe-node':
            return {'status': 'failed', 'result': {'reason': 'unknown operation'}, 'logs': [], 'events': []}
        if job.get('status') == 'cancel_requested':
            return {'status': 'cancelled', 'result': {'reason': 'cancel requested'}, 'logs': [], 'events': []}
        return {
            'status': 'succeeded',
            'result': {'node': job['target'], 'verified': True, 'residual': False, 'fake': True},
            'logs': [f'fake probe {job["target"]}: read-only'],
            'events': [{'kind': 'probe', 'message': 'fake probe completed'}],
        }


class ResidualExecutor:
    def run(self, job):
        return {
            'status': 'needs_attention',
            'result': {'node': job['target'], 'verified': False, 'residual': True},
            'logs': ['remote residual remains'],
            'events': [{'kind': 'probe', 'message': 'remote residual'}],
        }


class CrashExecutor:
    def run(self, job):
        raise RuntimeError('worker interrupted')
