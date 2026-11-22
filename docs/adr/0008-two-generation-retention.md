# ADR 0008: Keep two generations, delete by ledger and approval

Status: accepted. Deleting any artifact or model root always requires explicit operator
approval.

## Context

Large quantized model roots consume substantial worker disk capacity when retaining a full
previous generation as a rollback path. At the same time, hard-linking shared weight shards
between roots risks corrupting both generations if an in-place repair tool writes to a
shared inode.

## Decision

- **Two generations only: `N` and `N-1`.** `N-1` is kept in full while `N` is being
  confirmed, then thinned to files not identical to `N`. Older generations are retirement
  candidates, never a third retained copy. Thinning requires enough free headroom beforehand
  to restore the shared weights plus the free-space floor; otherwise the host reports that
  it is not rollback-capable.
- **Retention is declared state in Git** ([ADR 0007](0007-declared-and-runtime-state.md)):
  `infra/retention.yml` declares what each host serves, what it can roll back to (`full` or
  `thin`), and what is retired. Observation windows, soak records, and `N-1` start-test
  evidence live in `artifacts/lifecycle/` as runtime state.
- **Detection is automatic; deletion is approved and two-stage**
  ([ADR 0009](0009-detect-and-propose-apply-by-approval.md)): `agentiac ops storage` and
  `agentiac ops drift` report free space, headroom for another root, ledger declarations, and
  undeclared leftovers; neither deletes anything, and no check command takes a deletion
  flag. Approved removals (`agentiac ops retire`) first move objects to a named staging
  location before final deletion so an interrupted removal is visible and recoverable.
- **No inode sharing across versions.** A reused weight is a verified copy on its own inode
  (`nlink == 1`, a distinct inode, hashed before and after). Restoring a thin `N-1` uses a
  verified copy rather than re-linking to the running root, so in-place repairs cannot damage
  both generations.

## Consequences

Workers can run consecutive updates when thinning `N-1` precedes the next preparation.
Preparations that would breach the free-space floor are refused before copying. Unregistered
leftovers and unowned images are reported notes rather than silent deletions; whole-cache
pruning (`docker system prune`) remains out of scope because it cannot distinguish managed
objects from owner workloads. `ops drift` notices when the retention ledger is out of date.
