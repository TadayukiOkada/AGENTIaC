# ADR 0007: Declared state in git, runtime state in artifacts

Status: accepted.

## Context

Without an explicit boundary between Git-tracked declarations and host-local runtime state,
switching an active route target or claiming a borrowed host could be mistaken for uncommitted
drift or a dirty working tree.

## Decision

- **Declared state is in Git** (in the site repository, pinning core at `core/`): the
  inventory, playbooks, unit templates, profiles, backend manifests, component pins,
  retention ledger, every route target, and the gateway base configuration.
  `agentiac ops deploy` refuses uncommitted site changes or a dirty/unpinned `core/`
  submodule unless given `--allow-dirty`, and records the deployed site and core revisions
  in `artifacts/deployed.json`.
- **Runtime state is in `artifacts/`** (or `AGENTIAC_STATE_ROOT`): which target
  `local-coder` selects, which borrowed hosts are claimed, telemetry, evaluation state, and
  lifecycle evidence. It changes through explicit operator commands and is not committed on
  each switch.
- **`default` in `gateway/local-coder.json` is the fallback, not the preference.**
  `agentiac release` falls back to `default` and refuses if `default` resides on the host
  being released. `default` must stay on a dedicated worker.
- **A route target declares what it serves**: its backend and context. `agentiac reclaim`
  needs no other record to restore the selected target after a reboot.
- **Borrowed hosts persist nothing.** Their units stay transient, defined as commands in
  `scripts/borrow.py` with their arguments tested; persistent units come from Ansible
  templates.
- **Differences are reported, not auto-repaired.** `agentiac ops drift` is read-only
  ([ADR 0009](0009-detect-and-propose-apply-by-approval.md)).

## Consequences

A clean clone routes to `default` until an operator selects a target with
`agentiac route TARGET`. Route switches and deploys record the same target signature, so
neither forces a needless restart. Losing `artifacts/` loses only runtime selection, not the
declared system.
