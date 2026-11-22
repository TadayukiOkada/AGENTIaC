"""Configuration service: the declared resources and their cross-references, one read.

The API may show these resources, but it must not replace the CLI or the Ansible/systemd
surface. Declared files are facts; runtime readings are separate status/catalog services.
"""
import datetime

from agentiac import declared


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load(root=None, now=None):
    graph = declared.load(root)
    resources = []
    for kind, block in [('profile', graph.profiles), ('backend', graph.backends),
                       ('component', graph.components), ('retention', graph.retention),
                       ('backend_lifecycle', (graph.backend_lifecycle or {}).get('backends', {})),
                       ('agent', graph.agents)]:
        if isinstance(block, dict):
            for stem, spec in block.items():
                resources.append({'id': f'{kind}/{stem}', 'kind': kind, 'declared': spec})
        else:
            for spec in block:
                resources.append({'id': f'{kind}/{spec.get("id", spec.get("name"))}',
                                 'kind': kind, 'declared': spec})
    return {'schema_version': 1, 'as_of': now or _now(),
            'resources': resources, 'route': graph.route,
            'references': declared.references(graph)}
