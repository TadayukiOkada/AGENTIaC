#!/usr/bin/env python3
"""Prepare a container-served engine from the manifest that declares its build (`build` + `image`).

Runs on the worker as a persistent job started by `ops prepare HOST --backend ID` (infra/tasks/prepare-container.yml).
It is TensorFold's preparation (scripts/spark-tensorfold-prepare.py) with the engine's facts moved into the manifest:

- the source is fetched at the pinned revision (`build.revision`, else `engine.revision`) and must check out clean,
  and the Dockerfile and build context must resolve inside that checkout (a tracked symlink may not lead out of it);
- the Dockerfile is read as data and refused unless its SHA-256 is `build.dockerfile_sha256`. That hash is what makes
  the build reviewable: whatever its `RUN` steps fetch or execute is what the reviewed text says, and an inherited
  `ONBUILD` comes from a base pinned by digest. This script itself fetches no weights;
- every image the build resolves must be pinned by digest: each `FROM`, each `COPY --from=` / `RUN --mount=from=`
  that names an image rather than a stage, and the `# syntax=` frontend. A base is pinned either in its own reference
  (`@sha256:`) or in `build.base_images`, and reaches the build as
  `--build-context <reference>=docker-image://<repository>@<digest>` so BuildKit resolves it to that digest. The
  frontend is pinned by `build.args.BUILDKIT_SYNTAX` (`<image>@sha256:…`), which BuildKit uses in place of the directive
  (checked the same day: a zero digest there failed the build by that digest, the real one built).
  No local tag is read, pulled, created or overwritten for a base: on a borrowed host a tag such as `vendor/cuda:13`
  may be its owner's, and what it points at says nothing about the pin (checked 2026-11-24 with Docker 29.10 /
  buildx 0.39: with a local `alpine:3.20` present, `FROM alpine:3.20` built from the 3.19 digest it was given).
  A remote `ADD` without `--checksum` is refused for the same reason;
- the image is built without a tag and tagged `image.tag` only afterwards, only when it is the declared `image.id` (or
  none is declared yet), and only when that tag is free or already held by an image this site labelled for this
  backend - a tag held by anything else is left where it is. The tag must start with this site's `resource_prefix`
  so `ops storage` and the retention ledger can see it;
- the model directory the manifest declares is **checked, never downloaded**: weights arrive through `ops download`
  and its license gate (scripts/download-model.py), which a preparation must not route around.

It never starts a model and never touches a running container. One preparation runs at a time on a host. A first build
(no `image.id` declared) ends with the id to declare; a build whose id differs from a declared `image.id` is left
untagged and ends non-zero naming what to update.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

LABEL = 'io.agent-infra'          # the owner label the container workers read is f'{LABEL}.owner'
DEFAULT_FREE_GIB = 40             # as TensorFold's recipe asks for an image build
REVISION = re.compile(r'^[0-9a-f]{40}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
PINNED = re.compile(r'^[^@\s]+@sha256:[0-9a-f]{64}$')
NAME = re.compile(r'^[a-z0-9][a-z0-9._-]*$')
VARIABLE = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?')
REMOTE = re.compile(r'^(https?://|git@|git://|ssh://)')
DIRECTIVE = re.compile(r'^#\s*([A-Za-z]+)\s*=\s*(\S+)\s*$')


def run(argv, **kw):
    return subprocess.run(argv, **kw)


def spec_of(manifest, prefix):
    """The build this manifest declares, with defaults filled and every value a shell or a path would read checked.

    Raises SystemExit naming the key, so `ops prepare` refuses before anything is fetched or built.
    """
    build, image, engine = manifest.get('build'), manifest.get('image'), manifest.get('engine') or {}
    if not isinstance(manifest.get('id'), str) or not NAME.match(manifest['id']):
        raise SystemExit('the manifest `id` must be a plain name: it names the preparation directory')
    if not isinstance(build, dict):
        raise SystemExit('the manifest declares no `build`: this preparation builds a container from a declared source')
    if not isinstance(image, dict) or not isinstance(image.get('tag'), str):
        raise SystemExit('the manifest declares no `image.tag`: the image needs a name before it is built')
    spec = {'repository': build.get('repository') or engine.get('repository'),
            'revision': build.get('revision') or engine.get('revision'),
            'dockerfile': build.get('dockerfile', 'Dockerfile'), 'context': build.get('context', '.'),
            'dockerfile_sha256': build.get('dockerfile_sha256'), 'args': build.get('args') or {},
            'target': build.get('target'), 'base_images': build.get('base_images') or {},
            'patches': manifest.get('patches') if 'patches' in manifest else [],
            'free_gib': build.get('free_gib', DEFAULT_FREE_GIB), 'tag': image['tag'], 'id': image.get('id')}
    if not isinstance(spec['repository'], str) or not spec['repository'].startswith('https://'):
        raise SystemExit('`build.repository` (or `engine.repository`) must be an https URL')
    if not isinstance(spec['revision'], str) or not REVISION.match(spec['revision']):
        raise SystemExit('`build.revision` (or `engine.revision`) must be a full 40-character commit')
    if not isinstance(spec['dockerfile_sha256'], str) or not SHA256.match(spec['dockerfile_sha256']):
        raise SystemExit('`build.dockerfile_sha256` must be the SHA-256 of the Dockerfile at that revision')
    for key in ('dockerfile', 'context'):
        value = spec[key]
        if not isinstance(value, str) or not value or value.startswith('/') or '..' in Path(value).parts:
            raise SystemExit(f'`build.{key}` must be a path inside the repository')
    if not spec['tag'].startswith(f'{prefix}-') or not re.match(r'^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_][A-Za-z0-9._-]*$',
                                                                   spec['tag']):
        raise SystemExit(f'`image.tag` must be `{prefix}-<name>:<tag>`: an image this site does not name is invisible '
                         'to `ops storage` and to the retention ledger')
    if spec['id'] is not None and (not isinstance(spec['id'], str) or not DIGEST.match(spec['id'])):
        raise SystemExit('`image.id` must be `sha256:<64 hex>` when it is declared')
    if not isinstance(spec['args'], dict) or not all(isinstance(k, str) and re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', k)
                                                     and isinstance(v, (str, int)) and not isinstance(v, bool)
                                                     for k, v in spec['args'].items()):
        raise SystemExit('`build.args` must map argument names to strings')
    if not isinstance(spec['base_images'], dict) or not all(isinstance(v, str) and DIGEST.match(v)
                                                            for v in spec['base_images'].values()):
        raise SystemExit('`build.base_images` must map each base reference to `sha256:<64 hex>`')
    if spec['target'] is not None and (not isinstance(spec['target'], str) or not NAME.match(spec['target'])):
        raise SystemExit('`build.target` must be a stage name')
    if not isinstance(spec['free_gib'], int) or isinstance(spec['free_gib'], bool) or spec['free_gib'] < 1:
        raise SystemExit('`build.free_gib` must be a whole number of GiB')
    patches = spec['patches']
    if not isinstance(patches, list):
        raise SystemExit('`patches` must be a list of {path, sha256} entries')
    normalized_patches = []
    for entry in patches:
        if not isinstance(entry, dict):
            raise SystemExit('each `patches` entry must be a mapping with `path` and `sha256`')
        patch_path = entry.get('path')
        patch_sha = entry.get('sha256')
        if (not isinstance(patch_path, str) or not patch_path or patch_path.startswith('/')
                or '..' in Path(patch_path).parts):
            raise SystemExit('`patches` entries must declare a relative `path` with no `..`')
        if not isinstance(patch_sha, str) or not SHA256.match(patch_sha):
            raise SystemExit(f'`patches` entry {patch_path} must declare a 64-hex lowercase `sha256`')
        normalized_patches.append({'path': patch_path, 'sha256': patch_sha})
    spec['patches'] = normalized_patches
    return spec


def git(directory, *argv):
    return run(['git', '-C', str(directory), *argv], check=True, capture_output=True, text=True).stdout.strip()


def head_is(directory, revision):
    done = run(['git', '-C', str(directory), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    return done.returncode == 0 and done.stdout.strip() == revision


def fetch_source(spec, directory):
    """The pinned revision, checked out detached and clean - or a refusal."""
    if not (directory / '.git').is_dir():
        directory.mkdir(parents=True)
        git(directory, 'init', '-q')
        git(directory, 'remote', 'add', 'origin', spec['repository'])
    if git(directory, 'remote', 'get-url', 'origin') != spec['repository']:
        raise SystemExit(f'{directory} fetches from another repository than {spec["repository"]}')
    if spec.get('patches'):
        if not head_is(directory, spec['revision']):
            git(directory, 'fetch', '-q', '--depth', '1', 'origin', spec['revision'])
        # Patched builds re-apply pinned diffs onto a clean detached tree at spec['revision'].
        git(directory, 'checkout', '-q', '-f', '--detach', spec['revision'])
        git(directory, 'clean', '-q', '-fdx')
        if not head_is(directory, spec['revision']) or git(directory, 'status', '--porcelain', '--ignored'):
            raise SystemExit('the source checkout could not be reset to the pinned revision before patching')
        return
    if not head_is(directory, spec['revision']):
        git(directory, 'fetch', '-q', '--depth', '1', 'origin', spec['revision'])
        git(directory, 'checkout', '-q', '--detach', 'FETCH_HEAD')
    if not head_is(directory, spec['revision']) or git(directory, 'status', '--porcelain', '--ignored'):
        raise SystemExit('the source checkout is not the pinned revision, or it is not clean')


def apply_patches(spec, source, patches_root):
    """Verify every pinned patch by SHA-256 and apply it cleanly onto SOURCE."""
    patches = spec.get('patches') or []
    if not patches:
        return
    if patches_root is None:
        raise SystemExit('the manifest declares `patches` but no --patches-root was passed')
    root_resolved = Path(patches_root).resolve()
    for entry in patches:
        patch_file = (root_resolved / entry['path']).resolve()
        if not patch_file.is_relative_to(root_resolved) or not patch_file.is_file():
            raise SystemExit(f'`patches` file {entry["path"]} is missing under {root_resolved}')
        digest = hashlib.sha256(patch_file.read_bytes()).hexdigest()
        if digest != entry['sha256']:
            raise SystemExit(f'`patches` file {entry["path"]} has SHA-256 {digest}, manifest declares {entry["sha256"]}')
        for argv in (['git', '-C', str(source), 'apply', '--check', str(patch_file)],
                     ['git', '-C', str(source), 'apply', str(patch_file)]):
            done = run(argv, capture_output=True, text=True)
            if done.returncode != 0:
                detail = (done.stderr or done.stdout or '').strip()
                raise SystemExit(f'could not apply `patches` entry {entry["path"]} at {spec["revision"][:8]}: {detail}')


def inside(source, relative, what):
    """SOURCE/RELATIVE, refused when it resolves outside SOURCE (a tracked symlink may point anywhere)."""
    path = (source / relative).resolve()
    if not path.is_relative_to(source.resolve()):
        raise SystemExit(f'`build.{what}` {relative} resolves to {path}, outside the pinned checkout')
    return path


def check_dockerfile(spec, source):
    path = inside(source, spec['dockerfile'], 'dockerfile')
    if not path.is_file():
        raise SystemExit(f'{spec["dockerfile"]} is not a file at {spec["revision"][:8]}')
    found = hashlib.sha256(path.read_bytes()).hexdigest()
    if found != spec['dockerfile_sha256']:
        raise SystemExit(f'{spec["dockerfile"]} at {spec["revision"][:8]} has SHA-256 {found}, the manifest declares '
                         f'{spec["dockerfile_sha256"]}: review the new text before declaring it')
    return path.read_text(encoding='utf-8-sig')


HEREDOC = re.compile(r'<<-?(["\']?)([A-Za-z_][A-Za-z0-9_]*)\1')


def instructions(text):
    """(directives, [(KEYWORD, rest)]) of a Dockerfile, read the way the parser reads it.

    Parser directives (`# syntax=`, `# escape=`) are only the leading comment lines. A line ending with the escape
    character continues on the next, joined without inserting anything, and comment lines inside a continuation are
    dropped. A heredoc (`<<EOF` ... `EOF`) body is skipped, so its lines are never read as instructions.
    """
    lines = text.lstrip('\ufeff').splitlines()
    directives, index = {}, 0
    while index < len(lines) and (match := DIRECTIVE.match(lines[index].strip())):
        directives[match.group(1).lower()] = match.group(2)
        index += 1
    escape = directives.get('escape', '\\')
    found, pending, closing = [], '', []
    for line in lines[index:]:
        if closing:
            if line.strip() == closing[0]:
                closing.pop(0)
            continue
        stripped = line.strip()
        if (stripped.startswith('#') and pending) or (not pending and (not stripped or stripped.startswith('#'))):
            continue
        body = line.rstrip()
        if body.endswith(escape):
            pending += body[:-len(escape)]
            continue
        whole = (pending + body).strip()
        pending = ''
        parts = whole.split(None, 1)
        found.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ''))
        closing = [match.group(2) for match in HEREDOC.finditer(whole)] if parts[0].upper() in ('RUN', 'COPY', 'ADD') else []
    if pending.strip():
        parts = pending.strip().split(None, 1)
        found.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ''))
    return directives, found


def resolve(word, args):
    reference = VARIABLE.sub(lambda m: str(args.get(m.group(1), m.group(0))), word)
    if '$' in reference:
        raise SystemExit(f'{word} uses a variable `build.args` does not set: pin it there')
    return reference


FLAG = re.compile(r'^--([A-Za-z][A-Za-z-]*)(?:=("[^"]*"|\S*))?\s*')


def flags_of(rest):
    """The leading `--name=value` flags of an instruction and the words after them.

    Only the flags are parsed: the rest of a RUN is shell text, which this does not try to tokenize. A JSON-form
    argument list (`ADD ["src", "dest"]`) is read as JSON.
    """
    flags = {}
    while match := FLAG.match(rest):
        flags.setdefault(match.group(1).lower(), []).append((match.group(2) or '').strip('"'))
        rest = rest[match.end():]
    if rest.startswith('['):
        try:
            words = [str(word) for word in json.loads(rest)]
        except ValueError:
            words = rest.split()
    else:
        words = rest.split()
    return flags, words


def build_images(text, args):
    """(frontend, [image references]) the build will resolve: FROM bases, `--from=` images and the syntax frontend.

    A stage named earlier, a stage index and `scratch` are not images. A variable is resolved from `build.args` only:
    a default hidden in the Dockerfile is exactly the kind of value a pin must not depend on.
    """
    directives, steps = instructions(text)
    stages, found = set(), []

    def add(reference):
        if reference.lower() not in stages and reference != 'scratch' and not reference.isdigit() and reference not in found:
            found.append(reference)

    for keyword, rest in steps:
        flags, words = flags_of(rest)
        if keyword == 'FROM':
            if not words:
                raise SystemExit(f'cannot read FROM {rest!r}')
            add(resolve(words[0], args))
            if len(words) >= 3 and words[1].upper() == 'AS':
                stages.add(words[2].lower())
        elif keyword in ('COPY', 'ADD'):
            for value in flags.get('from', []):
                add(resolve(value, args))
            if keyword == 'ADD' and 'checksum' not in flags:
                sources = [VARIABLE.sub(lambda m: str(args.get(m.group(1), m.group(0))), word) for word in words[:-1]]
                if any('$' in word for word in sources):
                    raise SystemExit(f'ADD {rest} names a source through a variable `build.args` does not set: it could '
                                     'be remote, and nothing would pin what it brings in')
                if any(REMOTE.match(word) for word in sources):
                    raise SystemExit(f'ADD {rest} fetches a remote source with no --checksum: nothing pins what it '
                                     'brings in')
        elif keyword == 'RUN':
            for mount in flags.get('mount', []):
                for part in mount.split(','):
                    key, _, value = part.partition('=')
                    if key == 'from':
                        add(resolve(value, args))
    return directives.get('syntax'), found


def repository_of(reference):
    """`name` of `name:tag` / `name@digest` (a registry port is not a tag)."""
    name = reference.split('@', 1)[0]
    head, _, tail = name.rpartition(':')
    return head if head and '/' not in tail else name


def normalized(reference):
    """The reference as BuildKit keys it: registry and `library/` filled in, `:latest` when no tag or digest."""
    name, at, digest = reference.partition('@')
    repository = repository_of(name)
    tag = name[len(repository) + 1:] if len(name) > len(repository) else ''
    first = repository.split('/', 1)[0]
    if '/' not in repository:
        repository = f'docker.io/library/{repository}'
    elif '.' not in first and ':' not in first and first != 'localhost':
        repository = f'docker.io/{repository}'
    registry, _, path = repository.partition('/')
    if registry in ('docker.io', 'index.docker.io', 'registry-1.docker.io'):
        repository = 'docker.io/' + (path if '/' in path else f'library/{path}')
    return repository + (f':{tag}' if tag else '' if at else ':latest') + (f'@{digest}' if at else '')


def pin_bases(spec, references, frontend):
    """{reference: digest} for every image the build resolves, and the build arguments that make it use them.

    Refuses an image pinned nowhere, a pin no instruction uses, two spellings of one reference, and a reference
    pinned twice. Reads nothing from the host: the digests are the manifest's.
    """
    pinned, contexts, keys = {}, [], {}
    for reference in references:
        key = normalized(reference)
        if key in keys:
            raise SystemExit(f'{reference} and {keys[key]} are the same image to BuildKit: spell it one way')
        keys[key] = reference
        if '@' in reference:
            if not PINNED.match(reference):
                raise SystemExit(f'{reference} is not `<image>@sha256:<64 hex>`')
            if reference in spec['base_images']:
                raise SystemExit(f'{reference} carries its digest and is pinned again in `build.base_images`')
            pinned[reference] = reference.split('@', 1)[1]
            continue
        digest = spec['base_images'].get(reference)
        if not digest:
            raise SystemExit(f'base image {reference} is not pinned: add it to `build.base_images` with its digest '
                             f'(read it without pulling: docker buildx imagetools inspect {reference})')
        pinned[reference] = digest
        contexts += ['--build-context', f'{reference}=docker-image://{repository_of(reference)}@{digest}']
    unused = sorted(set(spec['base_images']) - set(references))
    if unused:
        raise SystemExit(f'`build.base_images` pins {", ".join(unused)}, which no instruction in the Dockerfile names: '
                         'a pin that pins nothing hides a changed base')
    chosen = spec['args'].get('BUILDKIT_SYNTAX')
    if frontend is not None or chosen is not None:
        # BuildKit runs the frontend BUILDKIT_SYNTAX names whether or not the Dockerfile has a directive
        if not PINNED.match(str(chosen or '')):
            raise SystemExit(f'the build would run the frontend {chosen or frontend}, which BuildKit fetches and runs: '
                             'pin it as `build.args.BUILDKIT_SYNTAX` = `<image>@sha256:<64 hex>`')
        pinned['# syntax'] = str(chosen).split('@', 1)[1]
    return pinned, contexts


def image_inspect(reference):
    """The image REFERENCE names, None when there is no such image; any other failure is a refusal, not an absence."""
    done = run(['docker', 'image', 'inspect', reference], capture_output=True, text=True)
    if done.returncode == 0:
        return json.loads(done.stdout)[0]
    if 'no such image' in (done.stderr or '').lower() or 'not found' in (done.stderr or '').lower():
        return None
    raise SystemExit(f'docker image inspect {reference} failed: {(done.stderr or "").strip() or done.returncode}')


def build_input(spec, bases):
    """One hash of everything that decides the image's bytes besides the source revision and the Dockerfile."""
    declared = {'bases': bases, 'args': {k: str(v) for k, v in spec['args'].items()}, 'target': spec['target'],
                'dockerfile': spec['dockerfile'], 'context': spec['context']}
    if spec.get('patches'):
        declared['patches'] = [{'path': entry['path'], 'sha256': entry['sha256']} for entry in spec['patches']]
    return hashlib.sha256(json.dumps(declared, sort_keys=True).encode()).hexdigest()


def labels_for(manifest, spec, prefix, bases):
    """The labels a built image carries; an image is reused only when all of them match (see `carries`)."""
    return {f'{LABEL}.owner': prefix, f'{LABEL}.backend': manifest['id'],
            f'{LABEL}.source-revision': spec['revision'], f'{LABEL}.dockerfile-sha256': spec['dockerfile_sha256'],
            f'{LABEL}.build-input-sha256': build_input(spec, bases)}


def carries(image, labels):
    found = (image or {}).get('Config', {}).get('Labels') or {}
    return image is not None and all(found.get(key) == value for key, value in labels.items())


def tag_is_ours(spec, labels):
    """Whether `image.tag` is free, or held by an image this site labelled for this backend (so it may move)."""
    held = image_inspect(spec['tag'])
    owner = {key: labels[key] for key in (f'{LABEL}.owner', f'{LABEL}.backend')}
    return held is None or carries(held, owner)


def require_free(path, gib):
    free = shutil.disk_usage(path).free / 2 ** 30
    if free < gib:
        raise SystemExit(f'the image build needs {gib} GiB free under {path}; {free:.0f} GiB is free')


def build(spec, source, work, labels, contexts):
    """Build without a tag; the caller tags only an image it has checked."""
    require_free(work, spec['free_gib'])
    iid = work / 'image-id.txt'
    iid.unlink(missing_ok=True)
    argv = ['docker', 'build', '--progress', 'plain', '--pull=false', '--iidfile', str(iid),
            '-f', str(inside(source, spec['dockerfile'], 'dockerfile'))]
    for key, value in sorted(labels.items()):
        argv += ['--label', f'{key}={value}']
    for key, value in sorted(spec['args'].items()):
        argv += ['--build-arg', f'{key}={value}']
    if spec['target']:
        argv += ['--target', spec['target']]
    run(argv + contexts + [str(inside(source, spec['context'], 'context'))], check=True)
    return iid.read_text().strip()


def model_state(manifest, root):
    """What the declared model directory holds against the pins: sizes only, and the downloader's marker if any."""
    model = manifest.get('model') if isinstance(manifest.get('model'), dict) else {}
    directory, files = model.get('directory'), model.get('files') or []
    if not isinstance(directory, str) or not directory.strip('/') or '..' in Path(directory).parts:
        return {'state': 'none declared'}
    path = root / directory.strip('/')
    missing = [entry['path'] for entry in files
               if not (path / entry['path']).is_file() or (path / entry['path']).stat().st_size != entry.get('size')]
    marker = path / 'verified.json'
    return {'state': 'missing or incomplete' if missing else 'present', 'directory': str(path),
            'missing': missing[:5], 'verified_marker': marker.is_file()}


def prepare(manifest, root, prefix, patches_root=None):
    spec = spec_of(manifest, prefix)
    base = root / 'container-prepare'
    work = base / manifest['id']
    work.mkdir(parents=True, exist_ok=True)
    # one preparation at a time on this host (two backends may declare the same tag); a second one waits rather than
    # being lost, bounded by the job's RuntimeMaxSec. The leading dot keeps the name apart from every manifest id.
    with (base / '.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('waiting for another container preparation on this host', flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        source = work / 'source'
        fetch_source(spec, source)
        apply_patches(spec, source, patches_root)
        text = check_dockerfile(spec, source)
        print(f'source {spec["revision"][:8]}: {spec["dockerfile"]} matches the manifest', flush=True)
        frontend, references = build_images(text, spec['args'])
        bases, contexts = pin_bases(spec, references, frontend)
        for reference, digest in bases.items():
            print(f'pinned {reference} = {digest}', flush=True)
        labels = labels_for(manifest, spec, prefix, bases)
        if not tag_is_ours(spec, labels):
            raise SystemExit(f'{spec["tag"]} is held by an image this site did not label for {manifest["id"]}: it is '
                             'left where it is, and nothing is built under that name')
        existing = image_inspect(spec['tag'])
        if carries(existing, labels) and spec['id'] in (None, existing['Id']):
            image, how = existing['Id'], 'present'
        else:
            image, how = build(spec, source, work, labels, contexts), 'built'
            if not carries(image_inspect(image), labels):
                raise SystemExit('the built image does not carry the labels it was built with')
        mismatch = spec['id'] is not None and image != spec['id']
        if how == 'built' and not mismatch:
            if not tag_is_ours(spec, labels):
                raise SystemExit(f'{spec["tag"]} was taken by another image during the build; {image} stays untagged')
            run(['docker', 'tag', image, spec['tag']], check=True)
        print(f'image {image} ({how}' + (', untagged' if mismatch else f', {spec["tag"]}') + ')', flush=True)
        model = model_state(manifest, root)
        print(f'model: {model["state"]}' + (f' ({", ".join(model["missing"])})' if model.get('missing') else ''),
              flush=True)
        source_record = {'repository': spec['repository'], 'revision': spec['revision'],
                         'dockerfile': spec['dockerfile'], 'dockerfile_sha256': spec['dockerfile_sha256']}
        if spec['patches']:
            source_record['patches'] = spec['patches']
        result = {'backend': manifest['id'], 'image': {'tag': spec['tag'], 'id': image, 'how': how,
                                                       'tagged': not mismatch},
                  'source': source_record,
                  'pinned': bases, 'model': model,
                  'time': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        (work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        if spec['id'] is None:
            print(f'first build: declare "image": {{"tag": "{spec["tag"]}", "id": "{image}"}} in '
                  f'backends/{manifest["id"]}.json', flush=True)
            return 0
        if mismatch:
            print(f'image {image} is not the declared image.id {spec["id"]}; it is left untagged. Update image.id in '
                  f'backends/{manifest["id"]}.json (and anything that pins it) or find what changed', file=sys.stderr)
            return 1
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('root', type=Path, help='project root on the worker')
    parser.add_argument('--patches-root', type=Path, default=None, help='directory holding pinned patch files')
    parser.add_argument('--resource-prefix', required=True, help="this site's resource_prefix (infra/inventory.yml)")
    args = parser.parse_args()
    return prepare(json.loads(args.manifest.read_text()), args.root, args.resource_prefix,
                   patches_root=args.patches_root)


if __name__ == '__main__':
    sys.exit(main())
