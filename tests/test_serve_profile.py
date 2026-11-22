"""Launcher admission: a discrete GPU shared with its owner refuses a start while the owner's work holds it."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('serve_profile', Path(__file__).resolve().parents[1] / 'scripts/serve-profile.py')
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)

PROFILE = {'id': 'tiny', 'revision': 'r1', 'nodes': ['rtx3050-laptop'], 'entrypoint': 'tiny.gguf',
           'backend': {'revision': 'b1', 'context': 4096, 'slots': 1, 'batch': 512, 'ubatch': 512,
                       'cache_ram_mib': 0, 'extra_args': []}}


class GpuAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        weights = self.root / 'models/tiny/r1'
        weights.mkdir(parents=True)
        (weights / 'verified.json').write_text(json.dumps({'profile': PROFILE}))
        backend = self.root / 'backends/llama-b1/runtime-sm86'
        backend.mkdir(parents=True)
        (backend / 'build-metadata.txt').write_text('ok')
        self.node = {'cuda_arch': '86', 'node_class': 'rtx3050-laptop', 'memory_limit': '1G', 'min_available_gib': 0,
                     'max_load': 1000, 'cpus': 1, 'port': 18090, 'cuda_lib': '/cuda', 'max_gpu_used_mib': 512}

    def tearDown(self):
        self.directory.cleanup()

    def launch(self, used_mib):
        gpu = mock.Mock(return_value=used_mib) if used_mib is not None else mock.Mock(side_effect=AssertionError('queried'))
        image = mock.Mock(return_value=json.dumps([{'Id': 'sha256:image'}]))
        with mock.patch.object(serve, 'gpu_used_mib', gpu), mock.patch.object(serve.subprocess, 'check_output', image):
            return serve.command(self.root, PROFILE, self.node, 'image')

    def test_owner_gpu_work_refuses_the_start(self):
        with self.assertRaisesRegex(RuntimeError, r'GPU in use by another workload \(2900 MiB\)'):
            self.launch(2900)

    def test_idle_gpu_and_hosts_without_the_limit_start(self):
        self.assertEqual(self.launch(15)[1], 'sha256:image')
        del self.node['max_gpu_used_mib']  # Orin, Spark: unified memory, host memory admission only
        self.assertEqual(self.launch(None)[1], 'sha256:image')


if __name__ == '__main__':
    unittest.main()
