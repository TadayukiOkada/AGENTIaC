"""IP-02 (T4.1): an alias is served only by the backend its declaration describes.

`_verify_loaded_model` checked only the served model id, so an alias could deliver a different backend
version or context than it names whenever two manifests declare the same served model id (for example,
two builds that pin the same weight). The on-demand front now
reports what it actually serves — the backend id, the sha256 of its manifest and the
served context, read from the manifest file on the worker — and the pre-call hook
compares that report with the expected identity rendered for the alias from
`backends/<stem>.json` (the same file the claim pushed to the worker). This module is
the comparison: pure stdlib, so the control venv tests it directly, the way
`task_policy.py` is tested for the task key.

A report is absent while a front predates this change or runs without a manifest
(the llama.cpp profiles): `expected` is then None and `identity_problem` returns None,
so those aliases keep their current served-id-only check. Once a front reports, every
field must match, and a field it left out does not; a manifest the front cannot read
is a problem, never a pass.
"""
import hashlib
import json

FIELDS = ('backend_id', 'manifest_hash', 'context')


def manifest_sha256(path):
    """The manifest hash both sides compute: the sha256 of the file's exact bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expected_identity(root, stem, context=None):
    """What the alias must be served by, from backends/<stem>.json; None when nothing is manifested.

    context is the route target's context_window: two targets of one manifest (64K and
    128K) differ in it and nothing else, so it is part of the expected identity.
    """
    path = root / 'backends' / f'{stem}.json'
    if not path.exists():
        return None
    manifest = json.loads(path.read_text())
    return {'backend_id': manifest.get('id', stem),
            'manifest_hash': manifest_sha256(path),
            'context': context if context is not None else manifest.get('context')}


def identity_problem(expected, reported):
    """A field name when the front's report contradicts the declaration; None when it matches or says nothing."""
    if expected is None or reported is None:
        return None
    if not isinstance(reported, dict) or 'error' in reported:
        return 'manifest_hash'  # a manifest the front cannot read, or a report that is not one
    for field in FIELDS:
        if reported.get(field) != expected[field]:
            return field
    return None
