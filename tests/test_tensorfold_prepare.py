"""The TensorFold recipe is pinned by content, read as data and refused when it differs."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tf_prepare', ROOT / 'scripts/spark-tensorfold-prepare.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)

DOCKERFILE = b'FROM ${BASE_IMAGE}\nRUN true\n'
PREPARE = b"#!/bin/sh\necho before\n  docker build -f - <<'DOCKERFILE'\n" + DOCKERFILE + b'DOCKERFILE\necho after\n'
ARGS = {key: key.lower() for key in ('TF_REPO', 'TF_REF', 'ZIG_VERSION', 'ZIG_SHA256', 'LIBJPEG_TURBO_VERSION', 'LIBJPEG_TURBO_SHA256',
                                     'LIBWEBP_VERSION', 'LIBWEBP_SHA256', 'TF_RELEASE_VERSION', 'TF_RELEASE_AARCH64_SHA256')}


class Recipe(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.recipe = Path(directory.name)
        (self.recipe / 'scripts').mkdir()
        (self.recipe / 'patches').mkdir()
        (self.recipe / 'scripts/prepare.sh').write_bytes(PREPARE)
        (self.recipe / 'patches/0002-b.patch').write_bytes(b'b\n')
        (self.recipe / 'patches/0001-a.patch').write_bytes(b'a\n')
        self.manifest = {'build_args': {**ARGS, 'PATCHES_HASH': ''},
                         'recipe': {'patches': 2, 'hash': '', 'hash_base_image': 'base:tag',
                                    'dockerfile_sha256': hashlib.sha256(DOCKERFILE).hexdigest()}}
        found = prepare.recipe_hash(self.recipe, ARGS, 'base:tag')
        self.manifest['recipe']['hash'] = self.manifest['build_args']['PATCHES_HASH'] = found


class RecipeTests(Recipe):
    def test_the_hash_follows_the_recipes_own_formula(self):
        # image_hash: the patches in order, the heredoc with its fences, then one line of pins.
        pins = ('tf_repo tf_ref zig-zig_version zig_sha256 base:tag jpeg-libjpeg_turbo_version-libjpeg_turbo_sha256 '
                'webp-libwebp_version-libwebp_sha256 grammar-tf_release_version-tf_release_aarch64_sha256\n')
        text = b"a\nb\n  docker build -f - <<'DOCKERFILE'\n" + DOCKERFILE + b'DOCKERFILE\n' + pins.encode()
        self.assertEqual(prepare.recipe_hash(self.recipe, ARGS, 'base:tag'), hashlib.sha256(text).hexdigest()[:12])

    def test_the_dockerfile_excludes_its_fences(self):
        self.assertEqual(prepare.dockerfile(self.recipe), DOCKERFILE)

    def test_a_matching_recipe_returns_its_dockerfile(self):
        self.assertEqual(prepare.check_recipe(self.recipe, self.manifest), DOCKERFILE)

    def test_a_changed_patch_is_refused(self):
        (self.recipe / 'patches/0001-a.patch').write_bytes(b'changed\n')
        with self.assertRaisesRegex(SystemExit, 'recipe hash'):
            prepare.check_recipe(self.recipe, self.manifest)

    def test_a_missing_patch_is_refused(self):
        (self.recipe / 'patches/0002-b.patch').unlink()
        with self.assertRaisesRegex(SystemExit, '1 patches'):
            prepare.check_recipe(self.recipe, self.manifest)

    def test_a_changed_dockerfile_is_refused(self):
        (self.recipe / 'scripts/prepare.sh').write_bytes(PREPARE.replace(b'RUN true', b'RUN false'))
        with self.assertRaises(SystemExit):
            prepare.check_recipe(self.recipe, self.manifest)


class ModelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.directory = self.root / 'model'
        self.directory.mkdir()
        (self.directory / 'config.json').write_text('{}')
        (self.directory / 'weights').write_bytes(b'weights')
        self.manifest = {'model': {'revision': 'r', 'directory': 'model', 'repository': 'x/y', 'files': [
            {'path': 'config.json', 'size': 2},
            {'path': 'weights', 'size': 7, 'sha256': hashlib.sha256(b'weights').hexdigest()}]}}

    def test_a_complete_checkpoint_is_hashed_once_and_marked(self):
        self.assertEqual(prepare.prepare_model(self.manifest, self.root, 'image'), 'verified now')
        with mock.patch.object(prepare, 'sha256', side_effect=AssertionError('hashed twice')):
            self.assertEqual(prepare.prepare_model(self.manifest, self.root, 'image'), 'verified')

    def test_a_marker_never_hides_a_missing_file(self):
        prepare.prepare_model(self.manifest, self.root, 'image')
        (self.directory / 'weights').unlink()
        with mock.patch.object(prepare, 'download') as download, self.assertRaises(SystemExit):
            prepare.prepare_model(self.manifest, self.root, 'image')
        download.assert_called_once()

    def test_corrupt_weights_of_the_right_size_are_refused(self):
        (self.directory / 'weights').write_bytes(b'WEIGHTS')
        with self.assertRaisesRegex(SystemExit, 'does not match'):
            prepare.prepare_model(self.manifest, self.root, 'image')
        self.assertFalse((self.directory / 'verified.json').exists())


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source, self.target = Path(directory.name) / 'source', Path(directory.name) / 'recipe'
        self.source.mkdir()
        self.git('init', '-q')
        (self.source / 'file').write_text('x')
        self.git('add', 'file')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', 'x')
        self.revision = self.git('rev-parse', 'HEAD')
        subprocess.run(['git', 'clone', '-q', str(self.source), str(self.target)], check=True)

    def git(self, *argv):
        return subprocess.run(['git', '-C', str(self.source), *argv], check=True, capture_output=True, text=True).stdout.strip()

    def test_the_pinned_revision_is_accepted_without_a_fetch(self):
        prepare.fetch_recipe({'revision': self.revision, 'repository': 'unused'}, self.target)

    def test_local_edits_in_the_recipe_are_refused(self):
        (self.target / 'file').write_text('edited')
        with self.assertRaisesRegex(SystemExit, 'not the pinned revision'):
            prepare.fetch_recipe({'revision': self.revision, 'repository': 'unused'}, self.target)


if __name__ == '__main__':
    unittest.main()
