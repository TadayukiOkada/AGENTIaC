# Documentation maintenance

## Put each fact in one place

Use the [authority map](README.md#authority-map). Keep this small flat set of guides rather than adding a
folder or document per session. The project owner accepts requirement/policy changes; component maintainers
verify implementation/procedures; whoever completes a task updates its status/evidence. These are roles,
not a claim that a named team has accepted ownership.

- Goals/requirements: `project-overview.md`. Describe a code discrepancy as a gap until a decision resolves it.
- Accepted design/rationale: ADRs and linked PA/OA/storage decision sections. Add a dated amendment with a
  supersedes reference when policy changes. Do not rewrite a historical decision to look current.
- Implementation: `architecture.md`; commands/procedures: `usage.md`, `operations.md`, `testing.md`, `development.md`.
  Japanese quick guide links to the same procedures; avoid maintaining a second detailed command contract.
- Declared values: update the configuration/schema, then regenerate derived references. Do not copy changing
  pin tables or disk readings into several guides. JSON registries under docs may be application inputs.
- Current state: replace the concise `progress.md` snapshot with timestamp, revision and evidence. Separate
  implemented, validated, deployed and currently observed. Keep long narratives in dated records/archive.
- Plans: `roadmap.md` owns active priority and acceptance. `execution-plan.md` keeps legacy IDs, dependencies,
  approvals and task history. Mark completed/held work explicitly; proposals do not become approved tasks by repetition.
- Handoff: a resume pointer to status/roadmap, plus genuinely session-specific unresolved hazards. Do not duplicate
  status tables. Results/reviews record bounded evidence and are not a live source of defaults.

## Preserve history and stable references

Before replacing a unique narrative, move/preserve it under `archive/` with original date, original path,
new location, status and replacement. Record important moves in `documentation-migration.md`. Keep old paths
and inbound fragments working via a short pointer when code, tests, evidence or old reports refer to them.
Rebase relative Markdown links in archived content and record that transformation; do not regrade old results.
Never delete an unverified unique note because it sounds repetitive.

ADRs remain at stable paths. Version-specific ADRs describe their accepted event; add an amendment pointing to
the current configuration source when a later version changes the implementation. Accepted design is not
proof of completion. Keep unresolved owner decisions explicit and linked to the active plan.

## Validate a documentation change

1. Read affected code/config/tests and Git history. Identify intended requirement, implemented behavior and
   observations separately; label unsupported claims as unknown or proposal.
2. Check relative Markdown links, reference links and fragments in active **and archived** documents. Resolve
   targets relative to the document's new directory. Ignore fenced examples and external URLs for local checks;
   external technical claims need a separate source review if they are changed.
3. Check required topics and incoming links after consolidation. A new developer must find setup; an operator
   must find current state/recovery; an architect must find decisions/gaps; an agent must find the next gated task.
4. Run `git diff --check`. Run `make check PYTHON=.venv/bin/python` for structural changes and machine-consumed
   prose/table edits; run `make lint` if configuration or required review checks warrant it. Compare generated
   model/compatibility references with their generators. Report skips and limits; docs alone do not require a deploy.
5. Inspect `git diff --name-only` for accidental application/configuration changes and preserve unrelated work.

### Machine-consumed prose

`src/agentiac/updates.py` reads PA `### 8.1` through `### 8.2`, the update flow labels and table rows,
and approval rows in `execution-plan.md`. Tests also bind the roadmap promotion/log-task vocabulary,
usage pin instructions and reinstall/recipe headings. Preserve those interfaces or propose a separately
reviewed code migration. Generated `model-names.md` and `compatibility.md` must match their generators.
This coupling is a known limitation, not a reason to duplicate those tables in a new document.
