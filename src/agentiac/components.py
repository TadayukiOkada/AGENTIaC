"""Rules for infra/components.yml (T9.1, PA §7.2, §8.2). Nothing here reads the network or applies an update.

A pin repeats a value that lives in its lock file. `ops update check` compares the upstream with the pin, so a pin that
has drifted from its lock would report the wrong version with no error anywhere; `problems` is what stops that.
"""
import json
from pathlib import Path

from agentiac import paths

SCHEMA = paths.CODE / 'infra/components.schema.json'
OBSERVED_ONLY = ('user', 'vendor')  # §8.2


def shape(components):
    """The file's form, as 'where: what' strings."""
    import jsonschema  # requirements-control.lock; only `make check` and its tests need it
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    return [f'infra/components.yml {"/".join(map(str, error.absolute_path)) or "root"}: {error.message}'
            for error in sorted(validator.iter_errors(components), key=lambda e: list(map(str, e.absolute_path)))]


def _in_lock(component, pin, text, field):
    """Whether the lock really holds the value. A bare search would let `1.3.0` match some other package."""
    value, source = pin[field], component.get('source') or {}
    if field != 'version':
        return value in text
    if source.get('type') == 'pypi':
        return f"{source.get('package')}=={value}" in text.splitlines()
    if source.get('type') == 'npm':
        packages = json.loads(text).get('packages', {})
        return packages.get(f"node_modules/{source.get('package')}", {}).get('version') == value
    return value in text


def pin_problems(component, root):
    """What is wrong between a component's pin and the file it names."""
    pin = component.get('pin')
    if not pin:
        return []
    lock = paths.locate(str(pin.get('lock')), root)
    if not lock.is_file():
        return [f'its lock {pin.get("lock")} does not exist']
    text = lock.read_text()
    return [f'{field} {pin[field]} is not what {pin["lock"]} holds' for field in ('version', 'revision', 'digest')
            if field in pin and not _in_lock(component, pin, text, field)]


def problems(components, hosts, root):
    """Every rule the file must keep, with the form first: an empty list means it is sound."""
    found = shape(components)
    seen = set()
    for component in components:
        name = component.get('id')
        where = f'infra/components.yml component {name}'
        if name in seen:
            found.append(f'{where}: declared twice')
        seen.add(name)
        found += [f'{where}: {host} is not declared' for host in component.get('hosts', []) if host not in hosts]
        owner = component.get('owner')
        if owner in OBSERVED_ONLY and component.get('policy') != 'frozen':
            found.append(f'{where}: owner {owner} is observed only, so its policy must be frozen')
        if owner in OBSERVED_ONLY and 'pin' in component:
            found.append(f'{where}: owner {owner} is observed only, so it has no pin')
        if owner == 'project' and component.get('policy') == 'frozen' and not component.get('gate'):
            found.append(f'{where}: a frozen project component says what holds it in `gate`')
        if owner == 'project' and 'pin' not in component:
            found.append(f'{where}: a project component is pinned')
        found += [f'{where}: {problem}' for problem in pin_problems(component, root)]
    return found
