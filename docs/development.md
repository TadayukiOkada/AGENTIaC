# Development and control setup

Status: source-checkout workflow, verified against pyproject.toml, Makefile and CI.
Run commands from `~/src/AGENTIaC` (or `core/` inside a site checkout). Follow the [root bootstrap](../README.md#control-environment)
using locked control, gateway and lint dependencies and Python 3.12; pyproject requires Python >=3.12.
`make install` links the CLI and installs bash completion. An installed wheel alone does not provision this checkout.

## Repository map

| Location | Responsibility |
|---|---|
| `src/agentiac/`, `bin/agentiac` | CLI, declarations, lifecycle clients, Suite, SQLite state, identities, evidence and updates |
| `tests/` | unittest fixtures, regressions, opt-in Docker checks |
| `infra/` | Inventory, Ansible playbooks, component/retention declarations, systemd templates |
| `profiles/`, `backends/` | Model/schema declarations and pinned alternative inference engines |
| `gateway/`, `agents/` | Gateway hooks/policy/rendering, routing, frontend pins and skills |
| `runtime/`, `benchmarks/` | Container build definitions, task fixtures and protected graders |
| `scripts/`, `scripts/archive/` | Current operational helpers; historical experiment reproduction scripts |
| `evidence/` | Versioned alias history and historical evidence import declarations |
| `artifacts/` | Ignored runtime state, SQLite, measurements, private detailed traces and frontend installs |
| `docs/` | [Documentation authority map](README.md#authority-map) |

## Required local checks and CI

```sh
make check PYTHON=.venv/bin/python
make lint
```

`check` runs unittest discovery, parses top-level Python scripts, syntax-checks Ansible playbooks and runs
`git diff --check`. Component and retention YAML are declarations, not playbooks. `lint` runs yamllint,
ansible-lint and shellcheck from the locked lint environment. CI in `.github/workflows/check.yml` runs these
on Ubuntu 24.04 / Python 3.12 with full Git history so historical evidence identities can be recomputed.
CI has no deployment credentials and does not deploy.

`make publish-scan` asks whether the core is fit to publish. It builds a split rehearsal in a temporary directory and,
on the core tree it holds, runs the site-literal test against the site, the dated-line grep (a dated line must be
listed as data in the site's `docs/implementation-plan/m9-dated-lines.md`) and gitleaks with the core's
`.gitleaks.toml`. It exits 0 only when all three pass. Install the pinned gitleaks first with
`scripts/gitleaks-install.sh`; it refuses an archive that does not match the pinned sha256.

Docker tests need the evaluation image (`agentiac ops eval-image build`) and
`AGENTIAC_SANDBOX_TEST=1`; without opt-in their skips are not Docker validation. Keep unit, Docker,
fake-gateway and live-worker results separate in reports. Name meaningful regressions `test_*.py` and
`test_*`, following unittest conventions. No numeric coverage threshold or dedicated formatter is configured.

## Branches and worktrees

Work happens on an `agent/<name>` branch in its own worktree (`git worktree add ~/src/AGENTIaC-<name> -b
agent/<name> origin/main`), never by switching the branch of `~/src/AGENTIaC`: the timers run that checkout.
Pull requests are squash-merged, so Git itself never calls such a branch merged, and leftovers pile up.

- `agentiac worktrees` (`list`) shows every session worktree in `~/agent-worktrees` (of any repository) and every
  other `agent/` worktree in Git's worktree list, with its path, branch, commits ahead of `HEAD`, uncommitted files,
  and whether it is merged as `prune` judges it (in `origin/main`, or the head of a merged pull request), marking
  worktrees outside `~/agent-worktrees` as made by hand. `review`, `apply` and `drop` still accept only session
  worktrees under `~/agent-worktrees`.
- After a pull request merges, run `agentiac worktrees prune`. It lists every `agent/` branch that `origin/main`
  contains or whose tip is the head of a merged pull request (it asks GitHub through `gh`), with its worktree and
  the ignored files removing it would discard. It keeps unmerged branches, any worktree with uncommitted or
  untracked files, and the checkout it runs in. Nothing is removed until `agentiac worktrees prune --apply`.
- `agentiac doctor` shows a `hygiene` note while merged leftovers remain, so a session start sees them.
- An Agent runs only the listing; the owner runs `--apply` (deletions are owner-run, see the workspace rules).
- Branches that are not `agent/` are never considered. GitHub deletes a merged pull request's remote branch when
  the repository setting "Automatically delete head branches" is on.

## Changing declarations and documentation

Use the existing declarative files rather than duplicating machine facts in prose. Model/license data in
`docs/license-review.json` and discovery metadata in `docs/model-discovery.json` are consumed by code;
they are not ordinary disposable notes. Discovery metadata is not a deployment lock.

Generated reference updates, when their inputs change:

```sh
agentiac model names --write
agentiac model compatibility --write
```

These generate `docs/model-names.md` and `docs/compatibility.md` in the site repository; contract tests compare their bytes.
Changing short aliases must preserve the node-first naming contract, routing/identity gates,
restricted task policy and historical target/case IDs. A manifest/declaration edit does not attest a loaded process.

Update commands parse PA §8.1 and execution-plan approval table rows directly. Preserve their headings,
step labels and row IDs, and run update tests when editing them. See [documentation maintenance](documentation-guide.md)
for link checks, archival rules and the minimal update set. No package upgrade is justified by a newer version alone.

## See what is out of date

```sh
agentiac ops update check          # upstream against every pin in infra/components.yml
agentiac ops update check --json   # the same as one document
agentiac ops update observe        # what the machines really run; read-only, and it loads no model
agentiac ops update plan pi        # what updating one component would take; prints, changes nothing
```

It reads upstream metadata and applies nothing. Only package and repository names leave this host. `behind` means a
newer version exists, `differs` means a revision or digest changed where only equality means anything, `observed`
means there is no upstream to read, and `frozen` is not read at all. A failed read keeps the last answer, marked
`stale`. It exits 0 when anything could be read and 3 when nothing could. The result stays in
`artifacts/components/latest.json`.

`observe` uses ssh on the machines, so it is separate. An engine version counts as observed only if the front's
manifest matches this checkout. A host that cannot be reached is `unknown`, which is not a failure. A `note` means a
personal CLI, a driver or JetPack changed since the last run, or a pin differs from what runs. The result stays in
`artifacts/components/observed.json`, and `agentiac model show` reads it.

`check` also audits `agents/pi` and the control, gateway and lint locks, and keeps the result in
`artifacts/components/audit.json`. It only reports, and `doctor` shows the counts without using the network. The name and
version of each locked package go to `api.osv.dev`, and no key does. **0 advisories does not mean safe:** an advisory
published only on a project's own repository is not there.

`--notify` tells the desktop once about a new version of a `notify` component. `agentiac-update-check.timer` runs
`check --notify` once a day at 06:17 local time, and catches up after a shutdown.

`plan ID` prints the pin, what runs, the candidate, any gate, the capacity, the approvals, and the steps, which it reads
from PA §8.1 each time. It uses only the saved records, so run `check` and `observe` first for a current answer: without
them it still prints the steps and says so. It uses no network and no subprocess, writes nothing and applies nothing. A
`frozen` component is refused with exit 2, and a refused `strata` shows its gate. An undeclared id is also exit 2, and a
row that left the documents is exit 3.

## What the Agent does repeatedly

```sh
agentiac sessions
agentiac sessions --days 14 --top 20
agentiac sessions --json
```

The inventory of routine work (L3, T11.1), counted out of pi's own transcripts at
`artifacts/agents/pi-daily/sessions`: the top ten tool calls, the top ten programs, and the baseline
T11.5 compares a Skill against — calls per question, errors per call, seconds per turn, tokens per
question, compactions per session.

The transcripts are full conversations and none of it is printed. The only strings the screen can show
are tool names, program names and subcommands, each checked against a list declared in the code: a tool
name shaped like a sentence becomes `unnamed`, a program nobody declared becomes `other`, and the two
lines after `python - <<'PY'` are the body of one python call and are never looked at. A `-` in the
baseline is a missing denominator and not a zero — a session that never asked a question has no rate
per question, and printing 0.0 there would be a claim.

The working directory is shown, because which repository the routine work happens in is half the
answer and a directory is metadata and not a body.


## Dashboard prototype (local only)

`dashboard/` and `src/agentiac/management/` implement the local management dashboard and minimal-probe
worker using the isolated `.venv-dashboard` environment (`requirements-dashboard.lock`), owner session
initialization, explicit worker target allowlist and process recovery (`agentiac dashboard --help`).
Control/gateway locks are unchanged; optional dashboard tests must run with the dashboard interpreter.
Full `make check` in an isolated worktree still needs the existing control Ansible executable.
