#!/usr/bin/env python3
"""Prepare Spark's TensorFold image and checkpoint from the pinned recipe (docs/tensorfold-recipe.md).

Runs on the worker from backends/tf-1.1.4-qfn-mxfp8-nvfp4.json. The recipe's files are read as data and
checked against the manifest; its scripts are never run. Its Dockerfile is built only when the
image is missing, and the checkpoint is downloaded only when it is missing or incomplete. A
rebuilt image gets a new ID, which the manifest, launch arguments and worker pin, so a rebuild
ends by saying what to update.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

BUILD_FREE_GIB = 40    # as the recipe's IMAGE_FREE_GB
MODEL_FREE_GIB = 110   # as the recipe's MIN_FREE_GB for this checkpoint
HEREDOC = b"<<'DOCKERFILE'"
SNAPSHOT = ('import sys; from huggingface_hub import snapshot_download; '
            'snapshot_download(sys.argv[1], revision=sys.argv[2], local_dir="/target")')


def heredoc_lines(recipe):
    """The lines of scripts/prepare.sh from the one opening the Dockerfile to the one closing it."""
    lines = (recipe / 'scripts/prepare.sh').read_bytes().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if HEREDOC in line)
    end = next(i for i, line in enumerate(lines) if i > start and line == b'DOCKERFILE\n')
    return lines[start:end + 1]


def dockerfile(recipe):
    return b''.join(heredoc_lines(recipe)[1:-1])


def recipe_hash(recipe, args, base_image):
    """The recipe's own image_hash, which its image carries as the tf.patches label."""
    digest = hashlib.sha256()
    for patch in sorted((recipe / 'patches').glob('*.patch')):
        digest.update(patch.read_bytes())
    digest.update(b''.join(heredoc_lines(recipe)))
    digest.update((f"{args['TF_REPO']} {args['TF_REF']} zig-{args['ZIG_VERSION']} {args['ZIG_SHA256']} {base_image} "
                   f"jpeg-{args['LIBJPEG_TURBO_VERSION']}-{args['LIBJPEG_TURBO_SHA256']} "
                   f"webp-{args['LIBWEBP_VERSION']}-{args['LIBWEBP_SHA256']} "
                   f"grammar-{args['TF_RELEASE_VERSION']}-{args['TF_RELEASE_AARCH64_SHA256']}\n").encode())
    return digest.hexdigest()[:12]


def check_recipe(recipe, manifest):
    """Refuse a recipe that is not the reviewed one; return its Dockerfile."""
    spec = manifest['recipe']
    count = len(list((recipe / 'patches').glob('*.patch')))
    if count != spec['patches']:
        raise SystemExit(f"recipe has {count} patches, manifest {spec['patches']}")
    found = recipe_hash(recipe, manifest['build_args'], spec['hash_base_image'])
    if found != spec['hash'] or found != manifest['build_args']['PATCHES_HASH']:
        raise SystemExit(f"recipe hash {found}, manifest {spec['hash']}")
    text = dockerfile(recipe)
    if hashlib.sha256(text).hexdigest() != spec['dockerfile_sha256']:
        raise SystemExit('recipe Dockerfile differs from the manifest')
    return text


def git(directory, *argv):
    return subprocess.run(['git', '-C', str(directory), *argv], check=True, capture_output=True, text=True).stdout.strip()


def fetch_recipe(spec, directory):
    if not (directory / '.git').is_dir():
        directory.mkdir(parents=True)
        git(directory, 'init', '-q')
        git(directory, 'remote', 'add', 'origin', spec['repository'])
    if not head_is(directory, spec['revision']):
        git(directory, 'fetch', '-q', '--depth', '1', 'origin', spec['revision'])
        git(directory, 'checkout', '-q', '--detach', 'FETCH_HEAD')
    if not head_is(directory, spec['revision']) or git(directory, 'status', '--porcelain'):
        raise SystemExit('recipe checkout is not the pinned revision')


def head_is(directory, revision):
    done = subprocess.run(['git', '-C', str(directory), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    return done.returncode == 0 and done.stdout.strip() == revision


def image_labels(reference):
    done = subprocess.run(['docker', 'image', 'inspect', reference], capture_output=True, text=True)
    if done.returncode:
        return None
    image = json.loads(done.stdout)[0]
    return {'id': image['Id'], **(image['Config'].get('Labels') or {})}


def require_free(path, gib, what):
    free = shutil.disk_usage(path).free / 2 ** 30
    if free < gib:
        raise SystemExit(f'{what} needs {gib} GiB free under {path}; {free:.0f} GiB is free')


def build(manifest, recipe, work, text):
    require_free(work, BUILD_FREE_GIB, 'the image build')
    context = work / 'build-context'
    shutil.rmtree(context, ignore_errors=True)
    (context / 'patches').mkdir(parents=True)
    (context / 'Dockerfile').write_bytes(text)
    for patch in (recipe / 'patches').glob('*.patch'):
        shutil.copy(patch, context / 'patches')
    argv = ['docker', 'build', '--progress', 'plain', '--iidfile', str(work / 'image-id.txt'), '-t', manifest['image']['tag']]
    for key, value in manifest['build_args'].items():
        argv += ['--build-arg', f'{key}={value}']
    subprocess.run(argv + [str(context)], check=True)
    return (work / 'image-id.txt').read_text().strip()


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def model_problems(spec, directory, deep):
    """Files that are missing or wrong; deep also compares the SHA256 of the weights."""
    problems = []
    for entry in spec['files']:
        path = directory / entry['path']
        if not path.is_file() or path.stat().st_size != entry['size']:
            problems.append(entry['path'])
        elif deep and 'sha256' in entry and sha256(path) != entry['sha256']:
            problems.append(entry['path'])
    return problems


def download(spec, directory, image):
    require_free(directory.parent, MODEL_FREE_GIB, 'the checkpoint')
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(['docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}', '--network', 'host',
                    '--entrypoint', 'python', '--env', 'HOME=/tmp', '--mount', f'type=bind,source={directory},target=/target',
                    image, '-c', SNAPSHOT, spec['repository'], spec['revision']], check=True)


def prepare_model(manifest, root, image):
    spec = manifest['model']
    directory, marker = root / spec['directory'], root / spec['directory'] / 'verified.json'
    if marker.exists() and json.loads(marker.read_text()).get('revision') == spec['revision'] and not model_problems(spec, directory, False):
        return 'verified'
    if model_problems(spec, directory, False):
        download(spec, directory, image)
    problems = model_problems(spec, directory, True)
    if problems:
        raise SystemExit('checkpoint does not match the manifest: ' + ', '.join(problems[:5]))
    marker.write_text(json.dumps({'revision': spec['revision'], 'files': len(spec['files']),
                                  'verified_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}) + '\n')
    return 'verified now'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('root', type=Path, help='project root on the worker')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    work = args.root / 'tensorfold-prepare' / manifest['id']
    work.mkdir(parents=True, exist_ok=True)
    with (work / 'prepare.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recipe = work / 'recipe'
        fetch_recipe(manifest['recipe'], recipe)
        text = check_recipe(recipe, manifest)
        print(f"recipe {manifest['recipe']['revision'][:8]}: hash {manifest['recipe']['hash']}, Dockerfile and patches match", flush=True)
        labels = image_labels(manifest['image']['tag'])
        if labels and labels.get('tf.patches') == manifest['recipe']['hash'] and labels.get('tf.kernels') == 'embedded':
            image = labels['id']
        else:
            image = build(manifest, recipe, work, text)
            labels = image_labels(image)
            if not labels or labels.get('tf.patches') != manifest['recipe']['hash']:
                raise SystemExit('built image does not carry the recipe hash')
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'test', image,
                        '-x', '/opt/tensorfold/native/bin/tensorfold-native'], check=True)
        print(f'image {image}', flush=True)
        print('checkpoint', prepare_model(manifest, args.root, image), flush=True)
        if image != manifest['image']['id']:
            sys.exit(f"new image ID {image}: update image.id in backends/tf-1.1.4-qfn-mxfp8-nvfp4.json, "
                     'backends/tf-1.1.4-qfn-mxfp8-nvfp4.launch.json and scripts/spark-tensorfold-worker.py')


if __name__ == '__main__':
    main()
