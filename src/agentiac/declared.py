"""Declared state: one read of every declared file, and the cross-references between them.

docs/platform-architecture.md §7 (adopted by ADR 0009; execution plan T8.1). The files
keep their separate owners and meanings — node facts in `infra/inventory.yml`, model and
engine pins in `profiles/` and `backends/`, route candidates in `gateway/local-coder.json`,
`infra/components.yml` (T9.1), `infra/retention.yml` (T5.1) and `agents/agents.yml` (T12.2) — and this module
is the one place that reads all of them as one graph. `make check` fails on a dangling
reference (tests/test_declared.py), and T6.5 (IP-04) generates the gateway tables from
this graph rather than from a fourth copy of the facts.

Scripts that already read one of these files keep doing so: T8.1 changes nothing that
runs, only what can be checked. A reference is dangling when a file names something no
declared file defines — a route target on a host the inventory does not have, a
`default_profile` with no profile, a backend manifest served to a host that is not one.
"""
import hashlib
import json
from pathlib import Path
import re

import yaml

from agentiac import backend_lifecycle as lifecycle_rules
from agentiac import backend_names
from agentiac import components as components_rules
from agentiac import paths
from agentiac import retention as retention_rules
from agentiac.errors import explain

ROOT = paths.root()
INVENTORY = 'infra/inventory.yml'

# Site-wide settings the core reads from `all.vars` of the inventory (site-separation.md D8.2). They have no
# core default: a site declares them, and examples/site shows each one. resource_prefix is the one exception,
# with the default D6.8 fixed.
SETTINGS = {
    'ansible_user': 'the service account',
    'nodejs_binary': 'the Node.js binary the agent adapters run',
    'rclone_config_dir': "the service account's rclone configuration directory, which the backup units write",
    'backup_destination': 'the rclone destination of the control backup',
    'gateway_port': 'the loopback port of the gateway',
    'chatgpt_port': 'the loopback port of the subscription gateway',
}
DEFAULTS = {'resource_prefix': 'agentiac'}
GATEWAY_HOST = '127.0.0.1'      # invariant I1: the gateway listens on loopback only, so this is not a setting


class Declared:
    """The declared graph: the file groups of PA §7.1, keyed by their reference names."""

    def __init__(self, root, nodes, profiles, backends, route, gateway_models, components, retention, agents,
                 pi_models, router, group_vars=None, gateway_config=None, backend_lifecycle=None,
                 backend_legacy_names=None, backend_lifecycle_evidence=None):
        self.backend_lifecycle = backend_lifecycle  # display-only; None for older roots
        self.backend_lifecycle_evidence = backend_lifecycle_evidence or {}
        self.backend_legacy_names = backend_legacy_names or {}
        self.root = root
        self.nodes = nodes                      # inventory host name -> facts
        self.vars = group_vars or {}            # `all.vars`: what every worker inherits (runtime_image, ports)
        self.profiles = profiles                # profile stem -> spec
        self.backends = backends                # backend stem -> manifest (dicts with an id only)
        self.route = route                      # gateway/local-coder.json
        self.gateway_config = gateway_config or {}  # tracked source parameters, read once for descriptive names
        self.gateway_models = gateway_models    # model_name list gateway/config.yaml routes with
        self.components = components            # infra/components.yml entries
        self.retention = retention              # infra/retention.yml entries; {} until T5.1 lands
        self.agents = agents                    # agents/agents.yml entries; [] until T12.2
        self.pi_models = pi_models              # agents/pi/models.json ids
        self.router = router                    # agents/pi/router.json

    @property
    def node_classes(self):
        return {n['node_class'] for n in self.nodes.values() if n.get('node_class')}

    @property
    def rendered_aliases(self):
        """What scripts/route.py publishes (route_aliases.api_targets): the route alias plus each target's API names."""
        from agentiac import route_aliases
        try:
            return set(route_aliases.api_targets(self.route, self.route['default']))
        except (ValueError, KeyError, TypeError):
            alias = self.route['alias']
            short = self.route.get('short_aliases', {})
            return {alias} | {f'{alias}-{t}' for t in self.route['targets']} | (set(short) if isinstance(short, dict) else set())


def _group_vars(root=None):
    """`all.vars` of the inventory under ROOT, without loading the rest of the graph."""
    root = Path(root) if root is not None else ROOT
    return yaml.safe_load((root / INVENTORY).read_text())['all'].get('vars') or {}


def setting(name, root=None):
    """The declared value of a site-wide setting (D8.2), or an explained exit naming the key and the file."""
    value = _group_vars(root).get(name)
    if value not in (None, ''):
        return value
    if name in DEFAULTS:
        return DEFAULTS[name]
    raise SystemExit(explain(
        f'{name} is not declared in {INVENTORY}',
        [f'all.vars has no {name} ({SETTINGS.get(name, "a site-wide setting")})'],
        f'grep -n "{name}:" {INVENTORY}',
        f'add `{name}: <value>` under all.vars (docs/implementation-plan/site-separation.md D8.2)'))


def hosts(root=None):
    """The inventory's hosts under ROOT ({name: facts}, without `all.vars`); {} for a root without an inventory."""
    path = (Path(root) if root is not None else ROOT) / INVENTORY
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text())['all']['children']['workers']['hosts'] or {}


def former_names(nodes):
    """{former name: current host} for every host that declares `former_names` (newest first) (T8.10).

    A record or an argument written under a host's old name still means that host: claims, evidence, telemetry and
    the command line resolve it with `current_host`.
    """
    return {old: host for host, facts in nodes.items() for old in ((facts or {}).get('former_names') or [])}


def current_host(name, nodes):
    """NAME, or the host it names now when NAME is a former name."""
    return former_names(nodes).get(name, name)


def identity_name(host, nodes):
    """The name a host's target identities are taken over: its first name, else its name.

    `former_names` lists newest first, so the first name is the last one. A rename therefore leaves every
    `config_id` where it was (identities.compute_target): the configuration did not change, only its label.
    """
    olds = (nodes.get(host) or {}).get('former_names') or []
    return olds[-1] if olds else host


def former_name_problems(nodes):
    """Every former name that cannot resolve to exactly one host, as 'where: what' strings."""
    problems, seen = [], {}
    for host, facts in nodes.items():
        olds = (facts or {}).get('former_names')
        if olds is None:
            continue
        where = f'{INVENTORY} host {host} former_names'
        if not isinstance(olds, list) or not all(isinstance(old, str) and old for old in olds):
            problems.append(f'{where}: must be a list of host names')
            continue
        for old in olds:
            if old in nodes:
                problems.append(f'{where}: {old} is a current host')
            elif old in seen:
                problems.append(f'{where}: {old} is also a former name of {seen[old]}')
            seen.setdefault(old, host)
    return problems


def resource(role, root=None):
    """The deployed name of ROLE (a unit, timer, image or container): `<resource_prefix>-<role>` (D6.8).

    The prefix is the site's `resource_prefix`; a site that declares none gets the core's default. This site declares
    `agent-infra`, so nothing it has deployed is renamed.
    """
    return f'{setting("resource_prefix", root)}-{role}'


def account(host, root=None):
    """The service account on HOST (its inventory facts): the host's ansible_user, else the one in all.vars.

    The same order Ansible applies, so a per-host override means the same thing to a playbook and to this code.
    """
    return host.get('ansible_user') or setting('ansible_user', root)


def local_account(nodes, root=None):
    """The service account on the host this runs on: the inventory host with `ansible_connection: local`."""
    local = [host for host in nodes.values() if host.get('ansible_connection') == 'local']
    return account(local[0] if local else {}, root)


def gateway_url(root=None, path=''):
    """The gateway as a client reaches it: loopback (I1) and the declared port."""
    return f'http://{GATEWAY_HOST}:{setting("gateway_port", root)}{path}'


def setting_problems(group_vars):
    """Every missing or malformed site-wide setting in GROUP_VARS (`all.vars`), as 'where: what' strings."""
    where = f'{INVENTORY} all.vars'
    problems = []
    for key, meaning in SETTINGS.items():
        value = group_vars.get(key)
        if value in (None, ''):
            problems.append(f'{where}: {key} ({meaning}) is not declared')
        elif key.endswith('_port') and (isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536):
            problems.append(f'{where}: {key} is not a port number: {value!r}')
        elif key.endswith(('_binary', '_dir')) and not (isinstance(value, str) and value.startswith('/')):
            problems.append(f'{where}: {key} is not an absolute path: {value!r}')
        elif not isinstance(value, (str, int)) or isinstance(value, bool):
            problems.append(f'{where}: {key} is not a single value: {value!r}')
    ports = [group_vars.get(key) for key in ('gateway_port', 'chatgpt_port')]
    if ports[0] is not None and ports[0] == ports[1]:
        problems.append(f'{where}: gateway_port and chatgpt_port are both {ports[0]}')
    prefix = group_vars.get('resource_prefix')
    if prefix is not None and not (isinstance(prefix, str) and prefix[:1].isalpha() and prefix.islower()
                                   and all(c.isalnum() or c == '-' for c in prefix) and not prefix.endswith('-')):
        problems.append(f'{where}: resource_prefix is not a lower-case name with hyphens: {prefix!r}')
    return problems


def _yaml_list(path):
    """A declared list file that a test checkout may not hold (components.yml, agents.yml): [] then."""
    return yaml.safe_load(path.read_text()) if path.exists() else []


def _yaml_map(path):
    """The same for a declared mapping file (infra/retention.yml): {} when the checkout does not hold it."""
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def load(root=None):
    root = Path(root) if root is not None else ROOT
    inventory = yaml.safe_load((root / 'infra/inventory.yml').read_text())
    nodes = inventory['all']['children']['workers']['hosts']
    profiles = {p.stem: json.loads(p.read_text()) for p in sorted((root / 'profiles').glob('*.json')) if p.stem != 'schema'}
    backends = {}
    for p in sorted((root / 'backends').glob('*.json')):
        if p.stem == 'schema':
            continue
        spec = json.loads(p.read_text())
        if isinstance(spec, dict) and 'id' in spec:  # launch-argument lists are not referenceable manifests
            backends[p.stem] = spec
    route = json.loads((root / 'gateway/local-coder.json').read_text())
    gateway_config = yaml.safe_load((root / 'gateway/config.yaml').read_text())
    gateway_models = [m['model_name'] for m in gateway_config['model_list']]
    pi_models = [m['id'] for m in json.loads((root / 'agents/pi/models.json').read_text())['providers']['local-infra']['models']]
    router = json.loads((root / 'agents/pi/router.json').read_text())
    lifecycle = lifecycle_rules.load(root)
    evidence_files = {entry['evidence']: (root / entry['evidence']).is_file()
                      for entry in (lifecycle or {}).get('backends', {}).values()}
    return Declared(root, nodes, profiles, backends, route, gateway_models,
                    _yaml_list(root / 'infra/components.yml'), _yaml_map(root / 'infra/retention.yml'),
                    _yaml_list(root / 'agents/agents.yml'), pi_models, router,
                    (inventory['all'].get('vars') or {}), gateway_config=gateway_config,
                    backend_lifecycle=lifecycle, backend_legacy_names=backend_names.legacy(root),
                    backend_lifecycle_evidence=evidence_files)


def references(declared):
    """Every dangling cross-file reference as 'where: what' strings; empty means the graph is sound."""
    problems = setting_problems(declared.vars) + former_name_problems(declared.nodes)

    def dangling(where, what, known):
        if what not in known:
            problems.append(f'{where}: {what} is not declared')

    route = declared.route
    dangling('gateway/local-coder.json default', route['default'], set(route['targets']))
    for name, spec in route['targets'].items():
        where = f'gateway/local-coder.json target {name}'
        dangling(where + ' node', spec['node'], set(declared.nodes))
        dangling(where + ' source_alias', spec['source_alias'], set(declared.gateway_models))
        if 'backend' in spec:
            dangling(where + ' backend', spec['backend'], set(declared.backends))

    for host, facts in declared.nodes.items():
        where = f'infra/inventory.yml host {host}'
        if 'default_backend' in facts:
            default_backend = facts['default_backend']
            dangling(where + ' default_backend', default_backend, set(declared.backends))
            if default_backend in declared.backends and host not in (declared.backends[default_backend].get('workers') or {}):
                problems.append(f'{where} default_backend: backends/{default_backend}.json does not name {host} in workers')
        if 'default_profile' in facts:
            dangling(where + ' default_profile', facts['default_profile'], set(declared.profiles))
        if 'gateway_alias' in facts:
            dangling(where + ' gateway_alias', facts['gateway_alias'], set(declared.gateway_models))
        if 'strata_backend' in facts:
            # A dedicated worker's default engine (scripts/borrow.py reads this). It has to be pinned by a
            # component that names this host, or `ops update check` cannot see what the worker is serving:
            # both Orins ran Strata 0.2.10 for two days while only 0.2.8 was declared.
            pinned = {stem for stem, spec in declared.backends.items()
                      for entry in declared.components
                      if Path(entry.get('pin', {}).get('lock', '')).stem == stem
                      and host in entry.get('hosts', []) and host in spec.get('workers', {})}
            dangling(where + ' strata_backend', facts['strata_backend'], pinned)

    for stem, spec in declared.profiles.items():
        for node_class in spec.get('nodes', []):
            dangling(f'profiles/{stem}.json node class', node_class, declared.node_classes)

    for stem, spec in declared.backends.items():
        for host in spec.get('workers', {}):
            dangling(f'backends/{stem}.json worker', host, set(declared.nodes))
        if 'patches' in spec:
            where_patches = f'backends/{stem}.json patches'
            patches = spec.get('patches')
            if not isinstance(patches, list):
                problems.append(f'{where_patches}: must be a list of {{path, sha256}} entries')
            else:
                root_resolved = Path(declared.root).resolve()
                for entry in patches:
                    if not isinstance(entry, dict):
                        problems.append(f'{where_patches}: each entry must be a mapping')
                        continue
                    patch_path = entry.get('path')
                    patch_sha = entry.get('sha256')
                    if (not isinstance(patch_path, str) or not patch_path or patch_path.startswith('/')
                            or '..' in Path(patch_path).parts):
                        problems.append(f'{where_patches}: {patch_path!r} must be a relative path with no `..`')
                        continue
                    if not isinstance(patch_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', patch_sha):
                        problems.append(f'{where_patches} {patch_path}: sha256 must be 64 lowercase hex characters')
                        continue
                    resolved = (root_resolved / patch_path).resolve()
                    if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
                        problems.append(f'{where_patches} {patch_path}: file is missing')
                    else:
                        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
                        if actual != patch_sha:
                            problems.append(f'{where_patches} {patch_path}: sha256 {actual} does not match {patch_sha}')

    callable_aliases = set(declared.gateway_models) | declared.rendered_aliases
    for model_id in declared.pi_models:
        dangling('agents/pi/models.json model', model_id, callable_aliases)
    for role in ('low', 'high'):
        dangling(f'agents/pi/router.json {role}', declared.router[role], callable_aliases)

    if declared.components:
        problems += components_rules.problems(declared.components, set(declared.nodes), declared.root)
    if declared.retention:
        # The ledger is not a list of references but a set of claims about what each host holds, so its rules
        # run here rather than through `dangling`: they are the ones that keep it equal to the inventory,
        # the manifests and the route table (T5.1's completion condition).
        problems += retention_rules.problems(declared.retention, declared.nodes, declared.backends, declared.route)
    for agent in declared.agents:
        for host in agent.get('hosts', []):
            dangling(f'agents/agents.yml agent {agent.get("id")}', host, set(declared.nodes))
    from agentiac import model_names
    problems += model_names.alias_problems(declared)
    if not problems:
        problems += model_names.short_alias_coverage(declared)
    problems += backend_names.problems(declared.backends, declared.backend_legacy_names)
    problems += lifecycle_rules.problems(declared)
    return problems
