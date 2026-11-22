"""Static capability registry. A handler is exposed only when this table says it is implemented.

The probe entry is a placeholder: `operations_for` replaces its `implemented` and `reason` on every read
from the ledger's ready, explicitly allowlisted probe workers.
"""
from .dto import OperationDTO


OPERATIONS = [
    OperationDTO(id='read-nodes', phase='P2', permission='viewer', readonly=True,
                  implemented=True, reason='GET declared node snapshots; no SSH probe'),
    OperationDTO(id='read-models', phase='P2', permission='viewer', readonly=True,
                  implemented=True, reason='GET catalogue; no download or route change'),
    OperationDTO(id='read-agents', phase='P2', permission='viewer', readonly=True,
                  implemented=True, reason='GET pins and capability matrix'),
    OperationDTO(id='read-configuration', phase='P2', permission='viewer', readonly=True,
                  implemented=True, reason='GET allowlisted declaration resources'),
    OperationDTO(id='login', phase='P2', permission='viewer', readonly=False,
                  implemented=True, reason='rate-limited session issue'),
    OperationDTO(id='probe-node', phase='P2c', permission='operator', readonly=True,
                  implemented=False, reason='No ready, explicitly allowlisted minimal-probe worker'),
    OperationDTO(id='configure', phase='P4a', permission='operator', readonly=False,
                  implemented=False, reason='requires candidate and plan approval'),
    OperationDTO(id='apply', phase='P4b', permission='admin', readonly=False,
                  implemented=False, reason='requires common lock and deploy executor'),
    OperationDTO(id='destructive', phase='P5b', permission='admin', readonly=False,
                  implemented=False, reason='requires named target and approved retention plan'),
]


def operations_for(ledger):
    targets = ledger.available_targets()
    return [op.model_copy(update={'implemented': bool(targets),
            'reason': 'Ready minimal OS/CPU/memory worker: ' + ', '.join(targets)
                      if targets else 'No ready, explicitly allowlisted minimal-probe worker'})
            if op.id == 'probe-node' else op for op in OPERATIONS]
