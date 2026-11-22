"""One Strata worker root per engine version, so a 0.2.8 trial never reads an older engine's root.

The selection rules are tested on hosts and manifests the test writes - `scripts/borrow.py`'s `ROOT` is
pointed at the fixture, so no test here reads a shipped manifest, and a rule cannot pass by accident
because the checkout happens to hold a file. What the shipped inventory declares is tested against the
shipped files, which is where a missing root would show up.
"""
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import declared, paths  # noqa: E402

SITE = paths.root()
STRATA_UNIT = declared.resource('strata-inference', SITE)
TENSORFOLD_UNIT = declared.resource('spark-tensorfold', SITE)
INFERENCE_UNIT = declared.resource('inference', SITE)


def borrow_module():
    spec = importlib.util.spec_from_file_location('borrow_roots', ROOT / 'scripts/borrow.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StrataRootSelectionTests(unittest.TestCase):
    def setUp(self):
        self.borrow = borrow_module()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = Path(directory.name)
        (self.fixture / 'backends').mkdir()
        self.borrow.ROOT = self.fixture        # the checkout the module reads manifests from

    def manifest(self, name, payload):
        (self.fixture / f'backends/{name}.json').write_text(json.dumps(payload))
        return name

    def host(self, **roots):
        return {'strata_trial_roots': {'0.2.8': '/prepared/028', **roots}}

    def test_a_manifest_can_name_its_own_root_when_one_engine_holds_two_weight_sets(self):
        # A host builds an engine once but can need two roots for it, one per weight set.
        name = self.manifest('fixture-028-gyro', {'source_version': '0.2.8'})
        plain = self.manifest('fixture-028', {'source_version': '0.2.8'})
        host = self.host(**{name: '/prepared/028-gyro'})
        self.assertEqual(self.borrow.strata_root(host, name), '/prepared/028-gyro')
        self.assertEqual(self.borrow.strata_root(host, plain), '/prepared/028')

    def test_the_front_follows_the_manifest_named_root(self):
        name = self.manifest('fixture-028-gyro', {'source_version': '0.2.8'})
        host = {'backend': 'strata', 'ondemand_backend_port': 18090, 'strata_api_port': 18085,
                'strata_trial_roots': {'0.2.8': '/prepared/028', name: '/prepared/028-gyro'}}
        root, config = self.borrow.ondemand_config('worker-a', host, 30, name, 131072)
        self.assertEqual(root, '/prepared/028-gyro')
        self.assertIn('/prepared/028-gyro/backends/fixture-028-gyro.json', config['start'])

    def test_a_claim_with_no_backend_is_refused_when_the_host_names_no_default(self):
        # `claim <host>` takes its backend from inventory `strata_default_backend`. A host that
        # names none is refused: there is no host-wide directory left to guess at (F3).
        with self.assertRaisesRegex(SystemExit, 'needs a backend'):
            self.borrow.strata_root(self.host(), None)

    def test_versioned_manifest_selects_its_own_root(self):
        name = self.manifest('fixture-028', {'source_version': '0.2.8'})
        self.assertEqual(self.borrow.strata_root(self.host(), name), '/prepared/028')

    def test_an_engine_with_no_root_on_the_host_is_refused_by_name(self):
        # Legacy engines held no versioned root and lived in `strata_root`; the retirement removed that fallback,
        # so the engine is now named back to the operator instead of served from wherever the host keeps a root.
        name = self.manifest('fixture-legacy-engine', {'source_version': '0.1.45'})
        with self.assertRaisesRegex(SystemExit, 'no root for'):
            self.borrow.strata_root(self.host(), name)

    def test_a_retired_manifest_is_refused_even_where_its_engine_version_has_a_root(self):
        # A manifest kept as the record of a retired path must not become a way to start a root
        # that does not hold its weights, even when another manifest of that engine has a root.
        name = self.manifest('fixture-retired', {'source_version': '0.2.8',
                                                 'status': 'retired: kept as the record of the pins'})
        with self.assertRaisesRegex(SystemExit, 'is retired'):
            self.borrow.strata_root(self.host(), name)

    def test_a_host_with_no_root_for_the_engine_is_refused(self):
        # Nothing serves a version it has no root for, and there is no legacy root to fall back to.
        name = self.manifest('fixture-unprepared', {'source_version': '0.3.0'})
        for host in ({'strata_trial_roots': {'0.2.8': '/prepared/028'}},   # a versioned root of another engine
                     {}):                                                  # no root of any kind
            with self.subTest(roots=sorted(host), manifest=name):
                with self.assertRaisesRegex(SystemExit, 'no root for'):
                    self.borrow.strata_root(host, name)

    def test_inventory_gives_every_strata_host_a_root_per_engine_version(self):
        shipped = borrow_module()          # unpatched: this one reads the checkout's inventory
        for name, host in shipped.hosts().items():
            if host.get('backend') != 'strata':
                continue
            roots = host.get('strata_trial_roots', {})
            self.assertTrue(roots, name)
            self.assertNotIn('strata_trial_root', host, f'{name}: the singular trial root is gone')
            self.assertNotIn('strata_root', host, f'{name}: the host-wide root is gone')
            default = host.get('strata_default_backend')
            self.assertTrue(default, f'{name}: a Strata host names the backend a claim without --backend serves')
            self.assertIn(shipped.strata_root(host, default), roots.values(),
                          f'{name}: {default} resolves to one of this host\'s roots')
            for key, path in roots.items():
                with self.subTest(host=name, key=key):
                    engine = key if re.fullmatch(r'[0-9.]+', key) else self.engine_of_manifest(key)
                    self.assertIn(f'-{engine}', path, f'{path}: a root names the engine it holds')

    @staticmethod
    def engine_of_manifest(key):
        """A `strata_trial_roots` key that is not a version must be a manifest this checkout declares."""
        manifest = SITE / 'backends' / f'{key}.json'
        if not manifest.exists():
            raise AssertionError(f'{key}: neither an engine version nor a manifest this checkout declares')
        return json.loads(manifest.read_text())['source_version']

    def test_every_strata_manifest_names_its_engine_version_in_its_stem(self):
        manifests = sorted((SITE / 'backends').glob('strata-*.json'))
        self.assertTrue(manifests)
        for path in manifests:
            manifest = json.loads(path.read_text())
            self.assertIn(f"strata-{manifest['source_version']}-", path.stem, path.name)

    def test_the_on_demand_front_starts_the_028_root_with_the_028_manifest(self):
        self.manifest('fixture-028', {'source_version': '0.2.8'})
        host = {'backend': 'strata', 'ondemand_backend_port': 18090, 'strata_api_port': 18085,
                'strata_root': '/prepared/legacy', 'strata_trial_roots': {'0.2.8': '/prepared/028'}}
        root, config = self.borrow.ondemand_config('worker-a', host, 30, 'fixture-028', 131072)
        self.assertEqual(root, '/prepared/028')
        self.assertEqual(config['unit'], STRATA_UNIT)
        self.assertEqual(config['start'], ['/prepared/028/.venv/bin/python', '/prepared/028/start-worker.py',
                                           '--node', 'worker-a', '--root', '/prepared/028', '--attempt', 'claim',
                                           '--port', '18090', '--unit', STRATA_UNIT, '--backend',
                                           '/prepared/028/backends/fixture-028.json',
                                           '--context', '131072'])
        self.assertEqual(config['key_file'], '/prepared/028/api-key-claim.env')
        self.assertEqual((config['backend'], config['context'], config['idle_s']),
                         ('fixture-028', 131072, 1800))

    def test_an_older_engine_front_is_refused_rather_than_served_from_another_root(self):
        host = {'backend': 'strata', 'ondemand_backend_port': 18090, 'strata_api_port': 18085,
                'strata_trial_roots': {'0.2.8': '/prepared/028'}}
        name = self.manifest('fixture-legacy-engine', {'source_version': '0.1.45'})
        with self.assertRaisesRegex(SystemExit, 'no root for'):
            self.borrow.ondemand_config('worker-a', host, 30, name, 131072)


class TensorfoldStrataClaimTests(unittest.TestCase):
    """A TensorFold worker answers a Strata manifest without becoming a Strata host by default.

    The facts this pins are the ones an operator would otherwise have to remember by reading scripts/borrow.py: what a
    bare `claim <host>` serves did not change, what an explicit `--backend` resolves to, and which unit has to be
    stopped because a host has one API port.
    """

    WORKER = {'spark_tensorfold': True, 'project_root': '/srv/worker/agentiac', 'backend_port': 18080,
              'ondemand_backend_port': 18090, 'inference_unit': TENSORFOLD_UNIT,
              'tunnel_port': 18103,
              'strata_trial_roots': {'0.2.13': '/srv/worker/agentiac/strata-dsv41-coder'}}

    def setUp(self):
        self.borrow = borrow_module()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = Path(directory.name)
        (self.fixture / 'backends').mkdir()
        self.borrow.ROOT = self.fixture
        # the host's own TensorFold manifest: borrow finds it by engine and workers (T8.6), not by a constant
        self.manifest('tf-test', {'id': 'tf-test', 'engine': {'repository': 'https://example.invalid/TensorFold.git',
                                                              'version': '1.0'}, 'workers': {'worker-d': {}}})

    def manifest(self, name, payload):
        (self.fixture / f'backends/{name}.json').write_text(json.dumps(payload))
        return name

    def test_a_bare_claim_still_serves_the_pinned_tensorfold_service(self):
        root, config = self.borrow.ondemand_config('worker-d', self.WORKER, 30)
        self.assertEqual(config['unit'], TENSORFOLD_UNIT)
        self.assertIn('evaluations/spark-flash-next/service-tensorfold', root)

    def test_an_explicit_backend_resolves_to_the_root_the_inventory_declares(self):
        name = self.manifest('fixture-dsv41', {'source_version': '0.2.13'})
        root, config = self.borrow.ondemand_config('worker-d', self.WORKER, 30, name, 131072)
        self.assertEqual(root, '/srv/worker/agentiac/strata-dsv41-coder')
        self.assertEqual(config['unit'], STRATA_UNIT)
        # the same listen port its tunnel and its gateway entry already use: two services, one port, never both up
        self.assertEqual(config['listen_port'], 18080)
        self.assertIn(f'/srv/worker/agentiac/strata-dsv41-coder/backends/{name}.json', config['start'])
        self.assertEqual((config['backend'], config['context']), (name, 131072))

    def test_the_service_this_claim_does_not_start_is_the_one_stopped_first(self):
        self.assertEqual(self.borrow.other_serving_units(self.WORKER, STRATA_UNIT), [TENSORFOLD_UNIT])
        self.assertEqual(self.borrow.other_serving_units(self.WORKER, TENSORFOLD_UNIT), [STRATA_UNIT])

    def test_a_dedicated_worker_still_switches_between_its_llama_cpp_default_and_a_manifest(self):
        worker_c = {'default_profile': 'qwen38-27b-q6', 'strata_backend': 'fixture', 'backend_port': 18080,
                    'ondemand_backend_port': 18090, 'strata_api_port': 18081}
        self.assertEqual(self.borrow.other_serving_units(worker_c, STRATA_UNIT), [INFERENCE_UNIT])
        self.assertEqual(self.borrow.other_serving_units(worker_c, INFERENCE_UNIT), [STRATA_UNIT])
        # A Strata-only host has one serving unit, named by inference_unit, so there is nothing beside it to stop.
        one = {'backend': 'strata', 'inference_unit': STRATA_UNIT, 'ondemand_backend_port': 18090,
               'strata_api_port': 18085}
        self.assertEqual(self.borrow.other_serving_units(one, STRATA_UNIT), [])

    def test_trial_roots_alone_make_a_host_answerable_and_not_defaultable(self):
        self.assertTrue(self.borrow.serves_strata(self.WORKER))
        with self.assertRaisesRegex(SystemExit, 'needs a backend'):
            self.borrow.strata_root(self.WORKER, None)


if __name__ == '__main__':
    unittest.main()
