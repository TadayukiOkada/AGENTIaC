"""Job ledger and executor scaffolding for P2c.

The ledger is explicit acceptance, not a GET side effect. A live read-only probe executor
is connected through the fixed existing `scripts/inventory.py` handler; fake/crash/residual
executors remain fixtures for crash, lease, residual and idempotency semantics.
"""
