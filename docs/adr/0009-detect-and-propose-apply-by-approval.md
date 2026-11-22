# ADR 0009: Detect and propose automatically, apply by approval

Status: accepted (user decision: U1 to U6 of the platform architecture design, section 18, as recommended).
Partly implemented: declared graph, component/update reporting, configuration identities, evidence,
recommendations and agent adapters exist. See the site's execution plan for individual task status.

Context: an external proposal asked for a unified platform with a single manifest, a reconciler, automatic
pruning, automatic model selection and validated automatic updates. Its diagnosis matched the repository: pi
sat behind a published release with nothing reporting it, Claude Code and Codex updated outside the plan, and
evidence for model choices was spread over Markdown and SQLite under an alias that does not identify a
configuration. Several of its remedies contradict ADR 0001, 0002, 0007 and 0008.

Decision:

- **Automation stops at the proposal.** Commands detect, compare, explain and plan. Applying stays with the
  existing paths (`ops prepare`, `ops deploy`, `route`, the promotion rule, `ops retire`) and their approvals.
  This extends ADR 0008's "detection is automatic, deletion is approved" to every kind of change.
- **Declared files stay separate and are read as one graph.** No single manifest and no reconciler.
  `infra/components.yml` and `agents/agents.yml` are added; `declared.py` reads everything and tests references.
- **Update policies are `frozen`, `manual` and `notify`.** There is no automatic apply. Drivers and JetPack are
  `frozen`, and a daily read of upstream metadata is allowed.
- **Components have owners.** `project` components are pinned and applied. `user` and `vendor` components are
  observed only. Evaluation uses pinned copies of Claude Code and Codex, separate from the personal install.
- **One identity.** `class_id`, `config_id` and `hw_fp` key replicas, evidence and recommendations. Alias-only
  history is not converted in bulk, and rows that cannot be mapped are never used for ranking.
- **Recommendations do not act.** `model recommend` answers `incumbent`, `candidate` or
  `insufficient-evidence`. It downloads nothing and changes no route.
- **`cloud-gpt-sol` stays a human choice.** Agent adapters, scheduled jobs and fallbacks never use it (ADR 0005).
  Adapters other than pi talk to the local gateway only.
- **Hermes is out of scope** until there is a decision that needs it.

Rejected: a single manifest, reconciliation, `prune --apply` and automatic reclamation, content-addressed
sharing, `validated-auto`, automatic model discovery and placement, a new workflow engine, renaming the CLI, an
interactive wizard, ACP or MCP as an integration layer, automatic installation of external skills and offline
bundles. Reasons are in the design's sections 2 and 3.

Consequences: `agentiac` gains commands and a `--json` contract; existing commands, files and aliases do not
change. Suite case ids stay stable because new provenance goes in results, not in the hashed spec. Updates need
a person, so a component can stay behind, and `ops update check` is what keeps that visible.
