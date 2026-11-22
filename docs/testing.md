# Evaluation and validation

Local development checks are documented in [Development](development.md). Shared-machine
evaluations retain explicit operator approval gates; this guide documents the existing
commands and verification contracts.

## Validation layers

| Layer | What it establishes | Limits |
|---|---|---|
| Unit/regression and static checks | Deterministic behavior with fixtures, Python parsing, Ansible syntax and lint | No CUDA, cold-start or live gateway guarantee |
| Optional Docker integration | Actual sandbox lifecycle and protected grader behavior | Needs built image and explicit opt-in; not covered by skipped tests |
| Disposable gateway rehearsal | Protocol rendering, task-key restrictions, identity refusals against fake worker | Does not validate GPU engines |
| Hardware protocol smoke | The selected live target and tested protocol paths at that time | Does not prove held-out quality, long-session reliability or all undeployed manifests |
| Scored Suite / controlled comparison | Bounded task success, regressions, time and recorded provenance | Preserve failures; report attempts/cohorts; recommendations are not promotion |

Keep seeds/settings, task definitions, frontend/grader revisions, and retry policies
comparable across candidates.

## Evaluate targets on the task suite

```sh
agentiac eval suite run --alias <target-a> --alias <target-b> --k 3
agentiac eval suite report
```

Tasks live in `benchmarks/suite/tasks/` in the site repository: the coding agent receives the
issue on the parent commit and is graded offline inside the suite container image by the
commit's own tests plus the existing suite. Finished cases are never rerun unless
`--retry-failed` is passed. Traces remain in `artifacts/suite/runs/`.

`--k N` selects trials `0` through `N-1` (default: `3`). To add a bounded repeat without
re-selecting trial `0`, pass `--trial 1` (repeatable, e.g., `--trial 1 --trial 2`). Trial
numbers are non-negative; duplicate flags run once in the order given. `--trial` and `--k`
are mutually exclusive. Keep the same alias, provider configuration, tasks, and settings when
resuming; selection flags do not change case identity. Unselected failed attempts stay
untouched even with `--retry-failed`, and a new alias creates distinct cases rather than an
implicit continuation.

## What failed, without reading the trace

```sh
agentiac eval report --failures
agentiac eval report --failures --alias <target> --task <task-id>
agentiac eval report --failures --alias <candidate> --against <incumbent>
agentiac eval suite report --failures --json
```

Each failed attempt prints one line carrying the field that determined the verdict (for
example process exit code, broken test runner exit, or token-cap truncation) and points to
`trace.jsonl` if deeper inspection is needed. Failure classes are evaluated in priority order
so a crashed process is not misclassified as `no-change`. `infrastructure` failures are
counted in the header and excluded from the model-failure denominator; `unexplained` stays
explicit rather than being redistributed, and unreported adapter fields are not treated as
symptoms.

`--against <incumbent>` requires `--alias` and compares each task with the same agent at the
same thinking and loop-guard settings. Any test name failing on the candidate that passed on
the incumbent is reported as a new regression class; tasks the incumbent never ran (or ran
only with infrastructure errors) are listed under `never ran for the incumbent` and produce
`insufficient-evidence`. Screen output truncates test lists at 20 names grouped by file, while
`--json` emits the complete list.

## Evidence: what has been measured, under which keys

```sh
agentiac evidence import [--dry-run]
agentiac evidence show --metric pass_rate [--usable]
agentiac evidence show --node <host> --json
```

`artifacts/evidence/evidence.sqlite` is a derived view over the suite database and
hand-authored site evidence tables, so deleting it loses nothing and `import` rebuilds it.
Each row records one configuration (`class_id`, `config_id`, `hw_fp`, agent and version,
workload, metric, value, sample count, and interval), keeping quality and speed in separate
rows.

Two site files are validated strictly before rows are written:

- `evidence/alias-history.yml` maps historical aliases to configurations when earlier suite
  runs lacked full provenance. Entries require all keys and a human-citable `why`, and are
  refused if their `class_id` hashes an unmanifested target that could pool distinct machines
  together.
- `evidence/reports.yml` records transcribed report or vendor values, each citing an existing
  file. `published` vendor figures are retained for reference only and never rank.

Rows whose configuration is unestablished are displayed with the refusal reason and excluded
from ranking. Rows whose declaration keys moved retain their measurements with a
`stale_reason` instead of being deleted or silently re-keyed.

## Suite overrides and provenance

For a private evaluation endpoint, `eval suite run --provider-config PATH` loads a pi provider
object (`baseUrl`, `api`, `apiKey`, `models`) for that run only; model IDs must match
`--alias`. It is recorded in case provenance and does not modify gateway or daily-use
configuration. Use a dummy key for private unauthenticated endpoints and never store real
credentials in this file.

`eval suite run --deadline-epoch EPOCH` avoids starting a case unless its 1800-second agent
timeout plus 900-second grading allowance fits. Pending cases remain resumable; this option
does not replace an outer batch supervisor.

Every attempt result carries a `provenance` block: `target`, `node`, `class_id`, `config_id`,
`hw_fp` (with inventory record age), agent name and version, and git revision with
`revision_dirty`. It is read from the declared graph, `artifacts/inventory/<host>.json`, and
git—never passed by the caller—and stored on the result rather than the case spec so suite
case IDs stay stable. Unreadable parts appear in `unknowns` without blocking the rest. The
same provenance block is emitted as one JSON line per alias before its first attempt so an
early-failing run still records its identity.
