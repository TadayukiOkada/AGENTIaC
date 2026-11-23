"""`ops storage`: what each node declares, what one more update needs, and what nobody names."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import declared, paths  # noqa: E402
import borrow  # noqa: E402
import storage  # noqa: E402

SITE = paths.root()
STRATA_PREFIX = declared.resource('strata', SITE)
RUNTIME_PREFIX = declared.resource('runtime', SITE)

GIB = 1024**3

# A tag nothing records, assembled at run time on purpose so it never appears as one literal in this file.
UNRECORDED = 'agentiac-tensorfold:' + 'flash-next-' + 'v999-' + 'dead' + 'beef'


def rows(roots, entries=(), images=(), cache=None):
    measured = {'roots': {}, 'entries': list(entries), 'images': list(images), 'cache': cache}
    for path, (free, size) in roots.items():
        measured['roots'][path] = {'filesystem': path, 'avail_bytes': free, 'size_bytes': size}
    return measured


class DeclaredTests(unittest.TestCase):
    def test_a_strata_node_declares_its_roots_and_their_parent(self):
        host = {'backend': 'strata', 'strata_root': '/mnt/data/agentiac-strata',
                'strata_trial_roots': {'0.2.8': '/mnt/data/agentiac-strata-0.2.8',
                                       '0.2.10': '/mnt/data/agentiac-strata-0.2.10'}}
        root_paths, parents = storage.declared_roots('worker-a', host)
        self.assertEqual(root_paths, ['/mnt/data/agentiac-strata', '/mnt/data/agentiac-strata-0.2.8',
                                      '/mnt/data/agentiac-strata-0.2.10'])
        self.assertEqual(parents, ['/mnt/data'])

    def test_a_node_with_nothing_ours_on_it_is_left_out_of_the_report(self):
        """T5.10: every node is asked now, and one with no declared root, no image of ours and no build cache
        answers nothing — it is left out rather than printed as a zero."""
        host = {'backend': 'strata'}
        self.assertEqual(storage.declared_roots('worker-b', host), ([], []))
        with mock.patch.object(storage, 'measure', return_value=rows({})):
            self.assertIsNone(storage.summary('worker-b', host))


class SummaryTests(unittest.TestCase):
    HOST = {'backend': 'strata', 'ansible_host': 'worker-a.example',
            'strata_root': f'/mnt/data/{STRATA_PREFIX}',
            'strata_trial_roots': {'0.2.8': f'/mnt/data/{STRATA_PREFIX}-0.2.8'}}

    def measured(self):
        return rows({f'/mnt/data/{STRATA_PREFIX}': (285 * GIB, 106 * GIB),
                     f'/mnt/data/{STRATA_PREFIX}-0.2.8': (285 * GIB, 125 * GIB)},
                    entries=[('/mnt/data', f'{STRATA_PREFIX}-0.2.9-candidate'),
                             ('/mnt/data', f'{STRATA_PREFIX}-0.2.1.3'),
                             ('/mnt/data', 'other-user-project')])

    def node(self, measured=None):
        with mock.patch.object(storage, 'measure', return_value=measured if measured is not None else self.measured()), \
                mock.patch.object(storage, 'manifests_for', return_value=[{'workers': {'worker-a': {'scratch_gib': 45}}}]):
            return storage.summary('worker-a', self.HOST)

    def test_the_largest_root_and_the_hosts_scratch_make_the_ask(self):
        node = self.node()
        self.assertEqual(node['free_gib'], 285.0)
        self.assertEqual(node['scratch_gib'], 45)
        self.assertEqual(node['update_needs_gib'], round((125 + 40 + 45), 1))
        self.assertEqual(node['spare_needs_gib'], round(storage.SPARE_WEIGHTS_GIB + storage.FLOOR_GIB, 1))
        self.assertEqual([root['gib'] for root in node['roots']], [106.0, 125.0])
        self.assertEqual(node['missing'], [])

    def test_a_strata_directory_nobody_declares_is_unregistered_and_another_projects_is_not(self):
        self.assertEqual(self.node()['unregistered'],
                         [f'{STRATA_PREFIX}-0.2.1.3', f'{STRATA_PREFIX}-0.2.9-candidate'])

    def test_a_declared_root_that_is_absent_is_reported_as_missing(self):
        measured = self.measured()
        del measured['roots'][f'/mnt/data/{STRATA_PREFIX}-0.2.8']
        self.assertEqual(self.node(measured)['missing'], [f'/mnt/data/{STRATA_PREFIX}-0.2.8'])

    def test_a_host_that_does_not_answer_is_a_note_not_a_zero(self):
        with mock.patch.object(storage, 'measure', return_value=None):
            self.assertEqual(storage.summary('worker-a', self.HOST), {'host': 'worker-a', 'reachable': False})

    def test_free_space_below_one_more_root_is_a_note(self):
        roomy = dict(self.node(), unregistered=[])
        self.assertEqual(storage.notes(roomy)[0], 'ok')
        tight = dict(roomy, free_gib=100.0)
        state, subject, detail = storage.notes(tight)
        self.assertEqual((state, subject), ('note', 'storage worker-a'))
        self.assertIn('100.0 GiB free', detail)
        self.assertIn('one more root needs 210.0 GiB', detail)

    def test_the_report_names_the_free_file_system_and_every_root(self):
        text = '\n'.join(storage.lines(self.node()))
        self.assertIn('/mnt/data', text)
        self.assertIn('285.0 GiB free', text)
        self.assertIn(f'/mnt/data/{STRATA_PREFIX}-0.2.8', text)
        self.assertIn(f'unregistered: {STRATA_PREFIX}-0.2.1.3, {STRATA_PREFIX}-0.2.9-candidate', text)
        self.assertIn('one more full root needs about 210.0 GiB', text)

    def test_the_build_cache_is_read_on_every_node_and_is_not_a_problem_line(self):
        """T5.10: `docker system df` is asked of every node. A build cache is regenerable layers — worth naming,
        not worth reporting as a leftover, because deleting it costs rebuild time rather than revealing a decision."""
        measured = dict(self.measured(), cache={'size': '72.18GB', 'reclaimable': '35.21GB'})
        node = dict(self.node(measured), unregistered=[])
        self.assertEqual(node['cache'], {'size': '72.18GB', 'reclaimable': '35.21GB'})
        self.assertIn('build cache 72.18GB (35.21GB reclaimable)', '\n'.join(storage.lines(node)))
        state, _, detail = storage.notes(node)
        self.assertEqual(state, 'ok')
        self.assertIn('build cache 72.18GB (35.21GB reclaimable)', detail)

    def test_the_image_and_build_cache_reads_are_asked_of_every_node(self):
        with mock.patch.object(storage.borrow, 'on_host',
                               return_value=mock.Mock(returncode=0, stdout='')) as on_host:
            storage.measure(self.HOST, ['/mnt/data/a'], ['/mnt/data'])
        script = on_host.call_args.kwargs['input']
        self.assertIn('docker images', script)
        self.assertIn('docker system df', script)
        self.assertIn('$5, $6', script)
        for verb in ('rm ', 'rmi', 'mv ', 'truncate', '>>', 'dd '):
            self.assertNotIn(verb, script)

    def test_the_remote_command_is_read_only_and_covers_every_declared_path(self):
        with mock.patch.object(storage.borrow, 'on_host',
                               return_value=mock.Mock(returncode=0, stdout='')) as on_host:
            storage.measure(self.HOST, ['/mnt/data/a', '/mnt/data/b'], ['/mnt/data'], False)
        argv = on_host.call_args.args[1]
        script = on_host.call_args.kwargs['input']
        self.assertEqual(argv[:2], ['bash', '-s'])
        self.assertIn('/mnt/data/a', script)
        self.assertIn('/mnt/data/b', script)
        for verb in ('rm ', 'mv ', 'truncate', '>>', 'dd ', 'docker rmi'):
            self.assertNotIn(verb, script)
        self.assertNotIn('docker', script)
        self.assertIn('du -sx', script)
        self.assertIn('df -B1 --output=avail', script)


class ImageOnlyTests(unittest.TestCase):
    """T5.10: the image reading runs on every node including control (no declared root, images and build cache)."""
    HOST = {'ansible_connection': 'local'}

    def node(self, images, cache=None):
        with mock.patch.object(storage, 'measure', return_value=rows({}, images=images, cache=cache)):
            return storage.summary('control', self.HOST)

    def test_an_unnamed_image_on_a_node_with_no_declared_root_is_a_note(self):
        stale = f'{RUNTIME_PREFIX}:' + 'cafe' + 'babe' + '0000'
        node = self.node([{'tag': stale, 'size': '79.4MB'}],
                         cache={'size': '83.33GB', 'reclaimable': '60.17GB'})
        self.assertIsNone(node['free_gib'])
        self.assertIsNone(node['update_needs_gib'])
        self.assertEqual([image['tag'] for image in node['images']], [stale])
        state, subject, detail = storage.notes(node)
        self.assertEqual((state, subject), ('note', 'storage control'))
        self.assertIn(f'unnamed image: {stale}', detail)
        self.assertIn('no declared root', '\n'.join(storage.lines(node)))

    def test_the_image_the_declaration_names_is_not_a_leftover_on_any_node(self):
        graph = storage.declared.load()
        runtime = graph.vars['runtime_image']
        self.assertIn(runtime, storage.declared_images('control', self.HOST, graph))
        self.assertEqual(self.node([{'tag': runtime, 'size': '79.4MB'}])['images'], [])


class TensorfoldStorageTests(unittest.TestCase):
    HOST = {'spark_tensorfold': True, 'ansible_host': 'worker-d.example',
            'project_root': '/srv/worker/agentiac', 'runtime_image': 'agentiac-runtime:81034e5916a3'}
    WEIGHTS_REL = 'evaluations/spark-flash-next/nvidia-fp4local-b9ce18a6887e'
    WEIGHTS = f'/srv/worker/agentiac/{WEIGHTS_REL}'
    WEIGHTS_BYTES = 102_262_310_123
    SERVING_TAG = 'agentiac-tensorfold:flash-next-v114-20e15e4'
    ROLLBACK_TAG = 'agentiac-tensorfold:flash-next-v114-fd3f43e1'
    BASE_IMAGE = 'nvcr.io/nvidia/pytorch@sha256:' + 'a' * 64

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = Path(directory.name)
        (self.fixture / 'infra').mkdir()
        (self.fixture / 'infra/inventory.yml').write_text(yaml.safe_dump({
            'all': {
                'vars': {'ansible_user': 'operator', 'runtime_image': 'agentiac-runtime:declared0000'},
                'children': {'workers': {'hosts': {
                    'worker-d': {'ansible_host': 'worker-d.example', 'spark_tensorfold': True,
                                 'project_root': '/srv/worker/agentiac'},
                }}},
            }
        }))
        (self.fixture / 'gateway').mkdir()
        (self.fixture / 'gateway/local-coder.json').write_text(json.dumps({
            'version': 2, 'alias': 'local-coder', 'default': 't1', 'targets': {},
        }))
        (self.fixture / 'gateway/config.yaml').write_text(yaml.safe_dump({'model_list': []}))
        (self.fixture / 'agents/pi').mkdir(parents=True)
        (self.fixture / 'agents/pi/models.json').write_text(json.dumps({'providers': {'local-infra': {'models': []}}}))
        (self.fixture / 'agents/pi/router.json').write_text('{}')
        (self.fixture / 'profiles').mkdir()
        (self.fixture / 'backends').mkdir()
        (self.fixture / 'backends/tf-1.1.4-fixture.json').write_text(json.dumps({
            'id': 'tf-1.1.4-fixture',
            'image': {'tag': self.SERVING_TAG},
            'build_args': {'BASE_IMAGE': self.BASE_IMAGE},
            'model': {'directory': self.WEIGHTS_REL},
            'workers': {'worker-d': {}},
        }))
        (self.fixture / 'backends/strata-0.2.13-fixture.json').write_text(json.dumps({
            'id': 'strata-0.2.13-fixture',
            'workers': {'worker-d': {'scratch_gib': 40}},
        }))
        (self.fixture / 'docs').mkdir()
        (self.fixture / 'docs/record.md').write_text(f'{self.ROLLBACK_TAG}\n{self.SERVING_TAG}\n')
        subprocess.run(['git', '-C', str(self.fixture), 'init', '-q'], check=True)
        subprocess.run(['git', '-C', str(self.fixture), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.fixture), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-q', '-m', 'init'], check=True)
        orig_load = declared.load
        for patch in (mock.patch.object(storage, 'ROOT', self.fixture),
                      mock.patch.object(storage.declared, 'load', side_effect=lambda root=None: orig_load(self.fixture))):
            patch.start()
            self.addCleanup(patch.stop)

    def declared(self):
        manifest = json.loads((self.fixture / 'backends/tf-1.1.4-fixture.json').read_text())
        return self.HOST['project_root'] + '/' + manifest['model']['directory']

    def node(self):
        measured = rows({'/srv/worker/agentiac/evaluations/spark-flash-next/service-tensorfold':
                         (707 * GIB, 4 * GIB),
                         self.WEIGHTS: (707 * GIB, self.WEIGHTS_BYTES)},
                        images=[{'tag': self.SERVING_TAG, 'size': '24.5GB'},
                                {'tag': self.ROLLBACK_TAG, 'size': '24.5GB'},
                                {'tag': UNRECORDED, 'size': '24.5GB'},
                                {'tag': self.HOST['runtime_image'], 'size': '2GB'},
                                {'tag': self.BASE_IMAGE.split('@')[0] + ':24.10-py3', 'size': '16.9GB'}])
        with mock.patch.object(storage, 'measure', return_value=measured):
            return storage.summary('worker-d', self.HOST)

    def test_only_an_image_no_record_names_is_reported(self):
        node = self.node()
        self.assertEqual([image['tag'] for image in node['images']], [UNRECORDED])
        state, subject, detail = storage.notes(node)
        self.assertEqual((state, subject), ('note', 'storage worker-d'))
        self.assertIn('flash-next-v999', detail)
        self.assertNotIn('fd3f43e1', detail)

    def test_an_image_the_records_name_is_not_a_leftover(self):
        self.assertTrue(storage.tracked_names(self.ROLLBACK_TAG))
        self.assertTrue(storage.tracked_names(self.SERVING_TAG))
        self.assertFalse(storage.tracked_names(UNRECORDED))
        self.assertNotIn(UNRECORDED, Path(__file__).read_text())

    def test_a_manifest_with_a_model_directory_declares_loose_weights(self):
        root_paths, parents = storage.declared_roots('worker-d', self.HOST)
        self.assertEqual(self.declared(), self.WEIGHTS)
        self.assertIn(self.WEIGHTS, root_paths)
        self.assertIn(borrow.tensorfold_root({'project_root': self.HOST['project_root']}), root_paths)
        self.assertNotIn('/srv/worker/agentiac/evaluations/spark-flash-next', parents)
        self.assertEqual(storage.loose_weights('worker-b', {'project_root': '/srv/agentiac'}), [])

    def test_loose_weights_are_reported_as_what_they_are(self):
        node = self.node()
        kinds = {root['path']: root['kind'] for root in node['roots']}
        self.assertEqual(kinds[self.WEIGHTS], 'weights')
        text = '\n'.join(storage.lines(node))
        self.assertIn('nvidia-fp4local-b9ce18a6887e  (loose weights: not inside an image)', text)

    def test_next_update_has_to_fit_the_weights_not_the_image(self):
        node = self.node()
        self.assertEqual(node['basis'], 'weights')
        self.assertEqual(node['update_needs_gib'], 175.2)
        self.assertIn('one more full weight set needs about 175.2 GiB', '\n'.join(storage.lines(node)))

    def test_a_declared_root_that_is_not_on_disk_is_listed_as_missing(self):
        host = dict(self.HOST, strata_trial_roots={'0.2.13': '/srv/worker/agentiac/strata-dsv41-coder'})
        measured = rows({'/srv/worker/agentiac/evaluations/spark-flash-next/service-tensorfold': (707 * GIB, 4 * GIB),
                         self.WEIGHTS: (707 * GIB, self.WEIGHTS_BYTES)},
                        images=[{'tag': self.SERVING_TAG, 'size': '24.5GB'},
                                {'tag': self.HOST['runtime_image'], 'size': '2GB'}])
        with mock.patch.object(storage, 'measure', return_value=measured):
            node = storage.summary('worker-d', host)
        self.assertEqual(node['missing'], ['/srv/worker/agentiac/strata-dsv41-coder'])

    def test_docker_sizes_are_read_as_bytes(self):
        for text, expected in [('24.5GB', 24500000000), ('2GB', 2000000000), ('16.9GB', 16900000000),
                               ('512MB', 512000000), ('3GiB', 3 * 1024**3), ('', 0), ('n/a', 0)]:
            self.assertEqual(storage.human_bytes(text), expected)


class DeclaredServiceTests(unittest.TestCase):
    """An engine that declares its `service` has a root and a weight directory this site has to see (PA §11, §3.4)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = Path(directory.name)
        (self.fixture / 'backends').mkdir()
        (self.fixture / 'backends/infernix-fixture.json').write_text(json.dumps({
            'id': 'infernix-fixture',
            'engine': {'repository': 'https://example.invalid/Infernix-dgx-spark-gb10.git', 'version': '1.0.0'},
            'model': {'repository': 'example/Model', 'directory': 'evaluations/infernix/hc4-w4dense',
                      'files': [{'path': 'model-00001-of-00003.safetensors', 'size': 7}]},
            'workers': {'workere': {'service': {'unit': 'agentiac-infernix-inference', 'port': 18090,
                                                'root': '/srv/agentiac/evaluations/infernix/service'}}}}))
        previous = storage.ROOT
        storage.ROOT = self.fixture
        self.addCleanup(setattr, storage, 'ROOT', previous)

    def test_the_root_the_engine_declares_is_measured_as_ours(self):
        paths, parents = storage.declared_roots('workere', {'project_root': '/srv/agentiac'})
        self.assertIn('/srv/agentiac/evaluations/infernix/service', paths)
        self.assertEqual(parents, [])  # its parent is the owner's evaluation tree, so the scan does not claim it

    def test_the_weight_directory_outside_the_root_is_listed_as_held_not_leftover(self):
        self.assertEqual(storage.loose_weights('workere', {'project_root': '/srv/agentiac'}),
                         ['/srv/agentiac/evaluations/infernix/hc4-w4dense'])

    def test_a_host_the_manifest_does_not_name_is_told_nothing_about_this_engine(self):
        self.assertEqual(storage.declared_roots('workerz', {'project_root': '/srv/agentiac'}), ([], []))
        self.assertEqual(storage.loose_weights('workerz', {'project_root': '/srv/agentiac'}), [])

    def test_corrupt_and_retired_manifests_do_not_break_or_pollute_storage_reads(self):
        (self.fixture / 'backends/broken.json').write_text('{not valid json')
        (self.fixture / 'backends/infernix-retired.json').write_text(json.dumps({
            'id': 'infernix-retired', 'status': 'retired 2026-11-23',
            'model': {'directory': 'evaluations/infernix/old-weights'},
            'workers': {'workere': {'service': {'unit': 'agentiac-infernix-old', 'port': 18090,
                                                'root': '/srv/agentiac/evaluations/infernix/old-service'}}}}))
        self.assertEqual(storage.loose_weights('workere', {'project_root': '/srv/agentiac'}),
                         ['/srv/agentiac/evaluations/infernix/hc4-w4dense'])
        self.assertEqual(storage.service_roots('workere'), ['/srv/agentiac/evaluations/infernix/service'])


class ImageFilterTests(unittest.TestCase):
    """Which `docker images` rows are ours: this site's own prefix, not the core's default one (D6.8)."""

    def test_the_prefix_this_site_deploys_is_the_prefix_the_reading_asks_for(self):
        prefix = declared.setting('resource_prefix', SITE)
        pattern = storage.image_pattern()
        self.assertIn(re.escape(prefix), pattern)
        self.assertTrue(re.search(pattern, f'{prefix}-infernix:1.0.0-dev'))
        self.assertTrue(re.search(pattern, 'nvcr.io/nvidia/pytorch:26.07-py3'))
        self.assertIsNone(re.search(pattern, 'ubuntu:24.04'))

    def test_an_image_this_site_built_is_asked_of_the_host_and_an_upstream_one_is_not(self):
        """The rule, applied to the manifests as they are: `<resource_prefix>-...` is ours, anything else a manifest
        pins (an upstream image it builds from or probes against) is not ours to name (§11)."""
        prefix = declared.setting('resource_prefix', SITE)
        pattern = storage.image_pattern()
        tags = []
        for path in sorted((SITE / 'backends').glob('*.json')):
            manifest = json.loads(path.read_text())
            if not isinstance(manifest, dict):
                continue
            image = manifest.get('image')
            tag = image.get('tag') if isinstance(image, dict) else image
            if isinstance(tag, str):
                tags.append(tag)
        for tag in tags + [f'{prefix}-infernix:1.0.0-dev', 'docker.io/vllm/vllm-openai@sha256:' + '0' * 64]:
            with self.subTest(tag=tag):
                if tag.startswith(prefix + '-'):
                    self.assertIsNotNone(re.search(pattern, tag))
                else:
                    self.assertIsNone(re.search(pattern, tag))


if __name__ == '__main__':
    unittest.main()
