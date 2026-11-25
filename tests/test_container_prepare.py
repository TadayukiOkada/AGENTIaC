"""A container build is prepared from what its manifest declares, and refused when a pin does not hold.

Nothing here runs docker or reaches a network: docker is a fake that records its argv (and so cannot show which base
BuildKit really resolved - that was checked once by hand, see the script's docstring), and the git tests fetch from a
repository in a temporary directory.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('container_prepare', ROOT / 'scripts/container-prepare.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)

PREFIX = 'agentiac'
REVISION = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
OTHER = 'sha256:' + 'c' * 64
IMAGE_ID = 'sha256:' + 'd' * 64
DOCKERFILE = ('# two stages, as an engine image usually has\n'
              'FROM vendor/cuda:13-devel AS build\n'
              'RUN make\n'
              'FROM --platform=linux/arm64 vendor/cuda:13-runtime\n'
              'COPY --from=build /out /opt/engine\n')
BASES = {'vendor/cuda:13-devel': DIGEST, 'vendor/cuda:13-runtime': OTHER}


def manifest(**overrides):
    found = {'id': 'engine-1.0-model-q4',
             'workers': {'workerx': {'slots': 1}},
             'engine': {'repository': 'https://example.invalid/engine.git', 'revision': REVISION, 'version': '1.0'},
             'build': {'dockerfile_sha256': hashlib.sha256(DOCKERFILE.encode()).hexdigest(), 'base_images': BASES},
             'image': {'tag': f'{PREFIX}-engine:1.0'},
             'model': {'directory': 'models/m/rev', 'files': [{'path': 'w.bin', 'size': 4, 'sha256': 'e' * 64}]}}
    for key, value in overrides.items():
        if value is None:
            found.pop(key, None)
        else:
            found[key] = value
    return found


class FakeDocker:
    """Images by reference; records every argv it was asked to run. `build` yields BUILT_ID untagged."""

    def __init__(self, images=None, built_id=IMAGE_ID):
        self.images, self.built_id, self.calls = dict(images or {}), built_id, []

    def __call__(self, argv, check=False, capture_output=False, text=False, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ['docker', 'image', 'inspect']:
            image = self.images.get(argv[3])
            return types.SimpleNamespace(returncode=0 if image else 1, stdout=json.dumps([image]) if image else '[]',
                                         stderr='' if image else f'Error response from daemon: No such image: {argv[3]}')
        if argv[:2] == ['docker', 'tag']:
            self.images[argv[3]] = self.images[argv[2]]
        elif argv[:2] == ['docker', 'build']:
            labels = dict(argv[i + 1].split('=', 1) for i, word in enumerate(argv) if word == '--label')
            self.images[self.built_id] = {'Id': self.built_id, 'Config': {'Labels': labels}}
            Path(argv[argv.index('--iidfile') + 1]).write_text(self.built_id + '\n')
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    def named(self, verb):
        return [call for call in self.calls if call[:2] == ['docker', verb]]


class SpecTests(unittest.TestCase):
    def test_a_sound_declaration_is_read_with_its_defaults(self):
        found = prepare.spec_of(manifest(), PREFIX)
        self.assertEqual(found['revision'], REVISION)           # from engine.revision when build names none
        self.assertEqual(found['repository'], 'https://example.invalid/engine.git')
        self.assertEqual((found['dockerfile'], found['context'], found['free_gib']), ('Dockerfile', '.', 40))
        self.assertIsNone(found['id'])

    def test_each_unsound_value_is_refused_by_its_key(self):
        base = manifest()
        cases = [({'id': '../../etc'}, '`id`'),
                 ({'id': None}, '`id`'),
                 ({'build': None}, '`build`'),
                 ({'image': {}}, '`image.tag`'),
                 ({'image': {'tag': 'someone-engine:1.0'}}, f'`{PREFIX}-'),
                 ({'image': {'tag': f'{PREFIX}-engine:1.0', 'id': 'abc'}}, '`image.id`'),
                 ({'engine': {**base['engine'], 'revision': 'main'}}, 'commit'),
                 ({'engine': {**base['engine'], 'repository': 'git@example.invalid:engine.git'}}, 'https'),
                 ({'build': {**base['build'], 'dockerfile_sha256': 'x'}}, 'dockerfile_sha256'),
                 ({'build': {**base['build'], 'dockerfile': '../Dockerfile'}}, '`build.dockerfile`'),
                 ({'build': {**base['build'], 'context': '/etc'}}, '`build.context`'),
                 ({'build': {**base['build'], 'base_images': {'vendor/cuda:13-devel': 'latest'}}}, 'base_images'),
                 ({'build': {**base['build'], 'args': {'BAD-NAME': 'x'}}}, '`build.args`'),
                 ({'build': {**base['build'], 'target': 'Bad Stage'}}, '`build.target`'),
                 ({'build': {**base['build'], 'free_gib': True}}, '`build.free_gib`')]
        for overrides, words in cases:
            with self.subTest(overrides=overrides), self.assertRaises(SystemExit) as caught:
                prepare.spec_of(manifest(**overrides), PREFIX)
            self.assertIn(words, str(caught.exception))


class DockerfileTests(unittest.TestCase):
    def images(self, text, args=None):
        return prepare.build_images(text, args or {})

    def test_the_images_are_the_external_ones_and_not_the_stages(self):
        self.assertEqual(self.images(DOCKERFILE), (None, ['vendor/cuda:13-devel', 'vendor/cuda:13-runtime']))
        text = 'FROM base:1 AS one\nFROM one AS two\nFROM two\nFROM scratch\nCOPY --from=0 /a /b\n'
        self.assertEqual(self.images(text), (None, ['base:1']))

    def test_an_image_named_by_copy_from_or_a_run_mount_is_an_image_to_pin(self):
        text = ('FROM base:1 AS one\nCOPY --from=one /a /a\nCOPY --from=tools/helper:2 /b /b\n'
                'RUN --mount=type=bind,from=tools/data:3,target=/d true\n')
        self.assertEqual(self.images(text), (None, ['base:1', 'tools/helper:2', 'tools/data:3']))

    def test_a_from_behind_a_bom_or_split_across_lines_is_still_read(self):
        self.assertEqual(self.images('﻿FROM base:1\n'), (None, ['base:1']))
        self.assertEqual(self.images('FROM \\\n  base:1\n'), (None, ['base:1']))
        self.assertEqual(self.images('FROM \\\n# a comment inside\n  base:1 AS x\n'), (None, ['base:1']))
        self.assertEqual(self.images('# escape=`\nFROM `\n  base:1\n'), (None, ['base:1']))

    def test_a_tab_a_split_keyword_and_a_heredoc_are_read_as_the_parser_reads_them(self):
        self.assertEqual(self.images('FROM\tbase:1\n'), (None, ['base:1']))
        self.assertEqual(self.images('FR\\\nOM base:1\n'), (None, ['base:1']))
        # a heredoc line that looks like an instruction is body text: it neither adds an image nor names a stage
        text = 'FROM base:1\nRUN <<EOF\nFROM evil:1 AS helper\nEOF\nCOPY --from=helper /a /a\n'
        self.assertEqual(self.images(text), (None, ['base:1', 'helper']))
        self.assertEqual(self.images('FROM base:1\nCOPY <<-"END" /etc/x\nADD https://x.invalid/w /w\nEND\n'),
                         (None, ['base:1']))

    def test_the_syntax_frontend_is_reported_and_only_from_the_leading_directives(self):
        self.assertEqual(self.images('# syntax=docker/dockerfile:1\nFROM base:1\n'),
                         ('docker/dockerfile:1', ['base:1']))
        self.assertEqual(self.images('FROM base:1\n# syntax=docker/dockerfile:1\n'), (None, ['base:1']))

    def test_a_variable_resolves_from_the_declared_args_only(self):
        self.assertEqual(self.images('ARG BASE=x:1\nFROM ${BASE}\n', {'BASE': 'vendor/x@' + DIGEST}),
                         (None, ['vendor/x@' + DIGEST]))
        with self.assertRaisesRegex(SystemExit, 'variable'):
            self.images('ARG BASE=x:1\nFROM $BASE\n')

    def test_a_remote_add_without_a_checksum_is_refused(self):
        with self.assertRaisesRegex(SystemExit, 'no --checksum'):
            self.images('FROM base:1\nADD https://example.invalid/weights.bin /w\n')
        with self.assertRaisesRegex(SystemExit, 'no --checksum'):
            self.images('FROM base:1\nADD ["https://example.invalid/w", "/w"]\n')
        self.assertEqual(self.images('FROM base:1\nADD --checksum=sha256:' + 'f' * 64 + ' https://x.invalid/w /w\n'
                                     'ADD local.tar /x\n'), (None, ['base:1']))
        with self.assertRaisesRegex(SystemExit, 'through a variable'):
            self.images('FROM base:1\nARG SRC=https://x.invalid/w\nADD $SRC /w\n')
        with self.assertRaisesRegex(SystemExit, 'no --checksum'):
            self.images('FROM base:1\nADD ${SRC} /w\n', {'SRC': 'https://x.invalid/w'})

    def test_shell_text_in_a_run_is_not_tokenized(self):
        self.assertEqual(self.images('FROM base:1\nRUN echo "it\'s" && printf \'%s\' "unbalanced\n'), (None, ['base:1']))

    def test_the_dockerfile_is_refused_unless_its_hash_is_declared(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'Dockerfile').write_text(DOCKERFILE)
            found = prepare.spec_of(manifest(), PREFIX)
            self.assertEqual(prepare.check_dockerfile(found, source), DOCKERFILE)
            (source / 'Dockerfile').write_text(DOCKERFILE + 'RUN curl evil\n')
            with self.assertRaisesRegex(SystemExit, 'review the new text'):
                prepare.check_dockerfile(found, source)

    def test_a_dockerfile_or_context_that_leads_out_of_the_checkout_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            source, outside = Path(directory) / 'source', Path(directory) / 'outside'
            source.mkdir()
            outside.mkdir()
            (outside / 'Dockerfile').write_text(DOCKERFILE)
            os.symlink(outside / 'Dockerfile', source / 'Dockerfile')
            os.symlink(outside, source / 'ctx')
            found = prepare.spec_of(manifest(), PREFIX)
            with self.assertRaisesRegex(SystemExit, 'outside the pinned checkout'):
                prepare.check_dockerfile(found, source)
            with self.assertRaisesRegex(SystemExit, 'outside the pinned checkout'):
                prepare.inside(source, 'ctx', 'context')


class PinTests(unittest.TestCase):
    def pin(self, references, base_images=None, frontend=None, args=None):
        found = prepare.spec_of(manifest(), PREFIX)
        if base_images is not None:
            found['base_images'] = base_images
        found['args'] = args or {}
        docker = FakeDocker()
        with mock.patch.object(prepare, 'run', docker):
            answer = prepare.pin_bases(found, references, frontend)
        self.assertEqual(docker.calls, [])       # the digests are the manifest's: nothing is read from the host
        return answer

    def test_a_reference_pinned_in_itself_needs_no_declaration_and_no_context(self):
        self.assertEqual(self.pin(['vendor/x@' + DIGEST], {}), ({'vendor/x@' + DIGEST: DIGEST}, []))

    def test_a_declared_base_reaches_the_build_by_digest(self):
        pinned, contexts = self.pin(list(BASES))
        self.assertEqual(pinned, BASES)
        self.assertEqual(contexts, ['--build-context', f'vendor/cuda:13-devel=docker-image://vendor/cuda@{DIGEST}',
                                    '--build-context', f'vendor/cuda:13-runtime=docker-image://vendor/cuda@{OTHER}'])

    def test_an_unpinned_base_is_refused_with_the_read_only_way_to_find_its_digest(self):
        with self.assertRaisesRegex(SystemExit, 'imagetools inspect vendor/other:1'):
            self.pin([*BASES, 'vendor/other:1'])

    def test_a_pin_no_instruction_uses_is_refused(self):
        with self.assertRaisesRegex(SystemExit, 'vendor/cuda:13-runtime, which no instruction'):
            self.pin(['vendor/cuda:13-devel'])

    def test_two_spellings_of_one_image_are_refused(self):
        # BuildKit keys contexts by the normalized name, so the second pin would silently replace the first
        for other in ('docker.io/library/alpine:latest', 'docker.io/alpine', 'index.docker.io/alpine:latest'):
            with self.subTest(other=other), self.assertRaisesRegex(SystemExit, 'same image to BuildKit'):
                self.pin(['alpine', other], {'alpine': DIGEST, other: OTHER})

    def test_an_inline_digest_must_be_whole_and_not_pinned_again(self):
        with self.assertRaisesRegex(SystemExit, 'is not `<image>@sha256'):
            self.pin(['vendor/x@sha256:short'], {})
        with self.assertRaisesRegex(SystemExit, 'pinned again'):
            self.pin(['vendor/x@' + DIGEST], {'vendor/x@' + DIGEST: OTHER})

    def test_a_syntax_frontend_is_refused_unless_pinned_by_buildkit_syntax(self):
        with self.assertRaisesRegex(SystemExit, 'BUILDKIT_SYNTAX'):
            self.pin(list(BASES), frontend='docker/dockerfile:1')
        with self.assertRaisesRegex(SystemExit, 'BUILDKIT_SYNTAX'):
            self.pin(list(BASES), frontend='docker/dockerfile:1', args={'BUILDKIT_SYNTAX': 'docker/dockerfile:1'})
        pinned, _ = self.pin(list(BASES), frontend='docker/dockerfile:1',
                             args={'BUILDKIT_SYNTAX': 'docker/dockerfile:1@' + DIGEST})
        self.assertEqual(pinned['# syntax'], DIGEST)

    def test_a_buildkit_syntax_arg_is_a_frontend_to_pin_even_without_a_directive(self):
        with self.assertRaisesRegex(SystemExit, 'frontend docker/dockerfile:1'):
            self.pin(list(BASES), args={'BUILDKIT_SYNTAX': 'docker/dockerfile:1'})

    def test_normalization_follows_the_docker_reference_rules(self):
        self.assertEqual(prepare.normalized('alpine'), 'docker.io/library/alpine:latest')
        self.assertEqual(prepare.normalized('vendor/x:1'), 'docker.io/vendor/x:1')
        self.assertEqual(prepare.normalized('registry:5000/vendor/x'), 'registry:5000/vendor/x:latest')
        self.assertEqual(prepare.normalized('nvcr.io/nvidia/pytorch@' + DIGEST), 'nvcr.io/nvidia/pytorch@' + DIGEST)
        self.assertEqual(prepare.repository_of('registry:5000/vendor/x:1'), 'registry:5000/vendor/x')


class PrepareTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

        def fetched(found, source):
            source.mkdir(parents=True, exist_ok=True)
            (source / 'Dockerfile').write_text(DOCKERFILE)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(prepare, 'fetch_source', fetched).start()
        mock.patch.object(prepare, 'require_free', lambda path, gib: None).start()
        self.docker = FakeDocker()
        mock.patch.object(prepare, 'run', self.docker).start()

    def prepared(self, declared):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prepare.prepare(declared, self.root, PREFIX)
        return code, out.getvalue(), err.getvalue()

    def labels(self, declared, bases=BASES):
        return prepare.labels_for(declared, prepare.spec_of(declared, PREFIX), PREFIX, bases)

    def test_a_first_build_is_built_untagged_then_tagged_and_ends_with_the_id_to_declare(self):
        declared = manifest(build={**manifest()['build'], 'args': {'ARCH': '121a', 'A': 1}})
        code, out, _ = self.prepared(declared)
        self.assertEqual(code, 0)
        self.assertIn(f'"id": "{IMAGE_ID}"', out)
        [argv] = self.docker.named('build')
        self.assertNotIn('-t', argv)                 # the tag moves only after the image is checked
        self.assertIn('--pull=false', argv)
        for label in (f'io.agent-infra.owner={PREFIX}', f'io.agent-infra.backend={declared["id"]}',
                      f'io.agent-infra.source-revision={REVISION}'):
            self.assertIn(label, argv)
        self.assertEqual([argv[i + 1] for i, word in enumerate(argv) if word == '--build-arg'], ['A=1', 'ARCH=121a'])
        self.assertEqual([argv[i + 1] for i, word in enumerate(argv) if word == '--build-context'],
                         [f'vendor/cuda:13-devel=docker-image://vendor/cuda@{DIGEST}',
                          f'vendor/cuda:13-runtime=docker-image://vendor/cuda@{OTHER}'])
        self.assertEqual(self.docker.named('tag'), [['docker', 'tag', IMAGE_ID, f'{PREFIX}-engine:1.0']])
        self.assertEqual(self.docker.named('pull'), [])
        result = json.loads((self.root / 'container-prepare' / declared['id'] / 'result.json').read_text())
        self.assertEqual((result['image']['id'], result['image']['tagged']), (IMAGE_ID, True))
        self.assertEqual(result['pinned'], BASES)

    def test_the_image_of_ours_that_is_already_there_is_not_rebuilt(self):
        declared = manifest(image={'tag': f'{PREFIX}-engine:1.0', 'id': IMAGE_ID})
        self.docker.images[f'{PREFIX}-engine:1.0'] = {'Id': IMAGE_ID, 'Config': {'Labels': self.labels(declared)}}
        code, out, _ = self.prepared(declared)
        self.assertEqual((code, self.docker.named('build'), self.docker.named('tag')), (0, [], []))
        self.assertIn('(present', out)

    def test_our_image_built_from_other_base_pins_is_rebuilt_and_the_tag_moves(self):
        declared = manifest()
        old = self.labels(declared, {'vendor/cuda:13-devel': OTHER, 'vendor/cuda:13-runtime': OTHER})
        self.docker.images[f'{PREFIX}-engine:1.0'] = {'Id': OTHER, 'Config': {'Labels': old}}
        self.assertEqual(self.prepared(declared)[0], 0)
        self.assertEqual(len(self.docker.named('build')), 1)
        self.assertEqual(self.docker.images[f'{PREFIX}-engine:1.0']['Id'], IMAGE_ID)

    def test_a_tag_held_by_an_image_not_labelled_for_this_backend_is_left_and_nothing_is_built(self):
        for labels in ({}, {'io.agent-infra.owner': PREFIX, 'io.agent-infra.backend': 'another-backend'}):
            with self.subTest(labels=labels):
                self.docker.images[f'{PREFIX}-engine:1.0'] = {'Id': OTHER, 'Config': {'Labels': labels}}
                with self.assertRaisesRegex(SystemExit, 'left where it is'):
                    self.prepared(manifest())
                self.assertEqual(self.docker.named('build') + self.docker.named('tag'), [])
                self.assertEqual(self.docker.images[f'{PREFIX}-engine:1.0']['Id'], OTHER)

    def test_a_failing_docker_is_a_refusal_and_not_a_free_tag(self):
        def broken(argv, **kw):
            if argv[:3] == ['docker', 'image', 'inspect']:
                return types.SimpleNamespace(returncode=1, stdout='', stderr='Cannot connect to the Docker daemon')
            return self.docker(argv, **kw)
        with mock.patch.object(prepare, 'run', broken), self.assertRaisesRegex(SystemExit, 'Cannot connect'):
            self.prepared(manifest())
        self.assertEqual(self.docker.named('build') + self.docker.named('tag'), [])

    def test_the_host_lock_is_a_name_no_backend_can_have(self):
        self.prepared(manifest())
        self.assertTrue((self.root / 'container-prepare/.lock').is_file())
        with self.assertRaises(SystemExit):
            prepare.spec_of(manifest(id='.lock'), PREFIX)

    def test_a_build_that_is_not_the_declared_image_stays_untagged_and_fails(self):
        declared = manifest(image={'tag': f'{PREFIX}-engine:1.0', 'id': OTHER})
        code, out, err = self.prepared(declared)
        self.assertEqual(code, 1)
        self.assertIn(f'backends/{declared["id"]}.json', err)
        self.assertEqual(self.docker.named('tag'), [])
        self.assertIn('untagged', out)

    def test_patches_are_verified_applied_and_included_in_build_input(self):
        patch_text = '--- a/Dockerfile\n+++ b/Dockerfile\n@@ -1,2 +1,3 @@\n # two stages, as an engine image usually has\n+# patched\n FROM vendor/cuda:13-devel AS build\n'
        patch_sha = hashlib.sha256(patch_text.encode()).hexdigest()
        patched_df_sha = hashlib.sha256((DOCKERFILE.replace('FROM vendor/cuda:13-devel AS build',
                                                            '# patched\nFROM vendor/cuda:13-devel AS build')).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upstream = tmp_path / 'upstream'
            upstream.mkdir()
            for cmd in (['init', '-q'], ['config', 'uploadpack.allowAnySHA1InWant', 'true']):
                subprocess.run(['git', '-C', str(upstream), *cmd], check=True)
            (upstream / 'Dockerfile').write_text(DOCKERFILE)
            subprocess.run(['git', '-C', str(upstream), 'add', 'Dockerfile'], check=True)
            subprocess.run(['git', '-C', str(upstream), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                            'commit', '-q', '-m', 'init'], check=True)
            rev = subprocess.run(['git', '-C', str(upstream), 'rev-parse', 'HEAD'], check=True,
                                 capture_output=True, text=True).stdout.strip()
            patches_root = tmp_path / 'patches'
            (patches_root / 'backends/patches').mkdir(parents=True)
            patch_rel = 'backends/patches/fix.patch'
            (patches_root / patch_rel).write_text(patch_text)

            base_spec = prepare.spec_of(manifest(), PREFIX)
            patched_manifest = manifest(
                engine={'repository': 'https://example.invalid/engine.git', 'revision': rev, 'version': '1.0'},
                build={'dockerfile_sha256': patched_df_sha, 'base_images': BASES},
                patches=[{'path': patch_rel, 'sha256': patch_sha}],
            )
            patched_spec = prepare.spec_of(patched_manifest, PREFIX)
            self.assertNotEqual(prepare.build_input(base_spec, BASES), prepare.build_input(patched_spec, BASES))

            work_root = tmp_path / 'work'
            real_run = subprocess.run
            real_fetch = prepare.fetch_source
            with mock.patch.object(prepare, 'fetch_source',
                                   side_effect=lambda s, d: real_fetch({**s, 'repository': upstream.as_uri()}, d)), \
                 mock.patch.object(prepare, 'run', side_effect=lambda argv, **kw: (
                     self.docker(argv, **kw) if argv[:1] == ['docker'] else real_run(argv, **kw))):
                code = prepare.prepare(patched_manifest, work_root, PREFIX, patches_root=patches_root)
                self.assertEqual(code, 0)
                # Re-running with the same clean upstream revision + patch is idempotent.
                code2 = prepare.prepare(patched_manifest, work_root, PREFIX, patches_root=patches_root)
                self.assertEqual(code2, 0)
            result = json.loads((work_root / 'container-prepare' / patched_manifest['id'] / 'result.json').read_text())
            self.assertEqual(result['source']['patches'], [{'path': patch_rel, 'sha256': patch_sha}])

            with self.assertRaisesRegex(SystemExit, 'no --patches-root'):
                prepare.apply_patches(patched_spec, work_root / 'container-prepare' / patched_manifest['id'] / 'source', None)

            (patches_root / patch_rel).write_text(patch_text + '# tampered\n')
            with self.assertRaisesRegex(SystemExit, 'SHA-256'):
                prepare.apply_patches(patched_spec, work_root / 'container-prepare' / patched_manifest['id'] / 'source', patches_root)

            bad_patch = '--- a/Dockerfile\n+++ b/Dockerfile\n@@ -1,1 +1,1 @@\n-MISSING LINE\n+OTHER\n'
            (patches_root / patch_rel).write_text(bad_patch)
            conflict_spec = {**patched_spec, 'patches': [{'path': patch_rel, 'sha256': hashlib.sha256(bad_patch.encode()).hexdigest()}]}
            with self.assertRaisesRegex(SystemExit, 'could not apply'), \
                 mock.patch.object(prepare, 'run', side_effect=real_run):
                prepare.apply_patches(conflict_spec, work_root / 'container-prepare' / patched_manifest['id'] / 'source', patches_root)

        with self.assertRaisesRegex(SystemExit, 'relative `path`'):
            prepare.spec_of(manifest(patches=[{'path': '../escape.patch', 'sha256': 'a' * 64}]), PREFIX)
        with self.assertRaisesRegex(SystemExit, '64-hex lowercase'):
            prepare.spec_of(manifest(patches=[{'path': 'backends/patches/x.patch', 'sha256': 'bad'}]), PREFIX)

    def test_the_weights_are_only_looked_at(self):
        directory = self.root / 'models/m/rev'
        directory.mkdir(parents=True)
        (directory / 'w.bin').write_bytes(b'1234')
        (directory / 'verified.json').write_text('{}')
        code, out, _ = self.prepared(manifest())
        self.assertEqual(code, 0)
        self.assertIn('model: present', out)
        # the only docker verbs are reading images, building and tagging: nothing runs a container or pulls
        self.assertEqual({tuple(call[:2]) for call in self.docker.calls},
                         {('docker', 'image'), ('docker', 'build'), ('docker', 'tag')})
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ['verified.json', 'w.bin'])


class FetchTests(unittest.TestCase):
    def git(self, directory, *argv):
        return subprocess.run(['git', '-C', str(directory), *argv], check=True, capture_output=True,
                              text=True).stdout.strip()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.upstream = Path(directory.name) / 'upstream'
        self.upstream.mkdir()
        self.git(self.upstream, 'init', '-q')
        self.git(self.upstream, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        (self.upstream / 'Dockerfile').write_text(DOCKERFILE)
        self.git(self.upstream, 'add', 'Dockerfile')
        self.git(self.upstream, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-m', 'one')
        self.pinned = self.git(self.upstream, 'rev-parse', 'HEAD')
        self.spec = {'repository': self.upstream.as_uri(), 'revision': self.pinned}
        self.source = Path(directory.name) / 'source'

    def test_the_pinned_revision_is_checked_out_and_a_second_run_reuses_it(self):
        prepare.fetch_source(self.spec, self.source)
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), self.pinned)
        prepare.fetch_source(self.spec, self.source)

    def test_a_dirty_checkout_is_refused(self):
        prepare.fetch_source(self.spec, self.source)
        (self.source / 'Dockerfile').write_text('FROM other\n')
        with self.assertRaisesRegex(SystemExit, 'not clean'):
            prepare.fetch_source(self.spec, self.source)

    def test_a_checkout_of_another_repository_is_refused(self):
        prepare.fetch_source(self.spec, self.source)
        with self.assertRaisesRegex(SystemExit, 'another repository'):
            prepare.fetch_source({**self.spec, 'repository': 'https://example.invalid/other.git'}, self.source)


class GateTests(unittest.TestCase):
    def test_a_declared_build_is_not_asked_the_strata_weight_question(self):
        from agentiac import upgrade
        # a manifest with flat `files` would otherwise be asked for a `strata_trial_roots` entry it never uses
        for build in (manifest()['build'], {}):    # an empty `build` goes where infra/prepare.yml sends it, too
            declared = manifest(files=[{'path': 'w.bin', 'size': 4, 'sha256': 'e' * 64}], build=build)
            graph = types.SimpleNamespace(backends={declared['id']: declared}, root=ROOT, nodes={})
            self.assertEqual(upgrade.prepare_gate(graph, 'workerx', declared['id'])[3], ('skip', None))


class PlaybookTests(unittest.TestCase):
    """The YAML's shape; the assert itself was evaluated against these cases with ansible-playbook when written."""

    def test_a_manifest_that_declares_build_goes_to_the_container_preparation(self):
        import yaml
        tasks = yaml.safe_load((ROOT / 'infra/prepare.yml').read_text())[0]['tasks']
        by_name = {task['name']: task for task in tasks}
        self.assertIn('manifest.build is defined',
                      by_name['A declared container build is prepared from its declaration']['ansible.builtin.set_fact']
                      ['prepare_container'])
        self.assertEqual(by_name['Prepare a declared container build']['ansible.builtin.include_tasks'],
                         'tasks/prepare-container.yml')
        # Strata is no longer "everything that is not TensorFold": a declared build is not asked for a trial root
        self.assertIn('not (prepare_container | bool)', by_name['Prepare Strata']['when'])

    def test_the_container_preparation_refuses_before_it_copies_and_starts_one_job(self):
        import yaml
        tasks = yaml.safe_load((ROOT / 'infra/tasks/prepare-container.yml').read_text())
        gate = ' '.join(tasks[0]['ansible.builtin.assert']['that'])
        for words in ("'^' + resource_prefix + '-", 'dockerfile_sha256', 'inventory_hostname in',
                      "manifest.id | default('') == backend_id", 'manifest.build.context'):
            self.assertIn(words, gate)
        started = [task for task in tasks if task['name'] == 'Start the persistent preparation'][0]
        argv = started['ansible.builtin.command']['argv']
        self.assertIn('--unit={{ resource_prefix }}-prepare-{{ backend_id }}.service', argv)
        self.assertIn('{{ project_root }}/scripts/container-prepare.py', argv)
        self.assertIn('--patches-root', argv)
        self.assertEqual(argv[-2:], ['--resource-prefix', '{{ resource_prefix }}'])
        self.assertEqual(started['when'], 'preparing.stdout != "active"')


if __name__ == '__main__':
    unittest.main()
