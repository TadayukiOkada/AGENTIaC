"""Display-only lifecycle metadata, separate from identity-bearing backend manifests.

Missing metadata means an older declaration root; an existing malformed file is an error.
Semantic references are checked separately so the read projection can expose contradictions.
"""
from functools import lru_cache
import json
from pathlib import Path

import jsonschema
import yaml

from agentiac import backend_names, paths

FILE = 'backends/lifecycle.yml'
SCHEMA = paths.CODE / 'infra/backend-lifecycle.schema.json'


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate keys instead of silently losing an archival decision."""


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError(f'{FILE}: mapping keys must be unique strings')
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@lru_cache(maxsize=1)
def _validator():
    return jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))


def validate(data):
    errors = sorted(_validator().iter_errors(data), key=lambda e: str(list(e.absolute_path)))
    if errors:
        where = '/'.join(map(str, errors[0].absolute_path)) or 'root'
        raise ValueError(f'{FILE} {where}: invalid lifecycle metadata')
    return data


def load(root):
    path = Path(root) / FILE
    if not path.exists():
        return None
    try:
        return validate(yaml.load(path.read_text(), Loader=UniqueLoader))
    except yaml.YAMLError as error:
        raise ValueError(f'{FILE}: invalid YAML') from error


def defaults(node, facts, backends):
    """Explicit backend defaults of NODE, not a running-process observation.

    A `spark_tensorfold` node's default is the TensorFold manifest that names it (backend_names.tensorfold_backend);
    BACKENDS is the declared graph's {stem: manifest}.
    """
    values = [(key, facts[key]) for key in ('strata_backend', 'strata_default_backend') if facts.get(key)]
    if facts.get('spark_tensorfold'):
        backend = backend_names.tensorfold_backend(backends, node)
        if backend is not None:
            values.append(('spark_tensorfold', backend))
    return values


def problems(graph):
    """Strict declaration validation; incomplete classifications remain warnings in reads."""
    from agentiac.services.engine_catalog import project
    if getattr(graph, 'backend_lifecycle', None) is None:
        return []
    found = [f'{issue["source_resource_id"]}: {issue["message"]}'
             for issue in project(graph)['issues'] if issue['severity'] == 'error']
    for stem, decision in graph.backend_lifecycle['backends'].items():
        captured = getattr(graph, 'backend_lifecycle_evidence', {}).get(decision['evidence'])
        if captured is not False and not (Path(graph.root) / decision['evidence']).is_file():
            found.append(f'{FILE} {stem}: evidence {decision["evidence"]} does not exist')
    return found
