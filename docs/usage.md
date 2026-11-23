# Daily use

Daily commands and interactive workflows run on the control host through `agentiac` (or the
site's wrapper script; [ADR 0006](adr/0006-agentiac-command.md), `agentiac --help`,
`agentiac <command> --help`). Install it once with `make install` to link
`~/.local/bin/agentiac` and enable bash completion for commands, hosts, and aliases. The
gateway listens on `127.0.0.1:14000` only.

```sh
agentiac status       # gateway, every host (loaded / unloaded / not claimed), local-coder target, ChatGPT
agentiac models       # aliases for code -m, and which ones send code externally
agentiac model names  # target key -> descriptive full_name -> API aliases
```

For setup, CI, and update checks see [Development](development.md); Suite, failure reports,
and evidence are in [Evaluation and validation](testing.md); deployment and recovery
procedures are in [Operations](operations.md); naming rules are in
[Model naming](model-naming.md).

## Work on a repository with pi

```sh
cd ~/repo
agentiac code                          # interactive pi on local-coder, REPO = this directory
agentiac code -p "summarize src/"      # one-shot (pi print mode)
agentiac code ~/repo -m control-small  # small model on the control host
agentiac code --resume                 # resume the latest session in its saved worktree
agentiac code --in-place               # no worktree (edits REPO directly)
agentiac code --loop-guard             # stop pi after 10/20 repeated bash commands
agentiac code --thinking high          # starting thinking level (targets with a thinking switch)
```

Targets that declare a verified thinking switch (`thinking` in `gateway/local-coder.json`)
start at their configured `thinking_start` level and allow `/thinking` inside `pi` (or
`--thinking off` at launch). Targets without a thinking switch run with thinking off and
refuse levels above `off`. `eval suite run` keeps thinking off unless `--thinking` is given.

Arguments after `--` pass through to `pi` unchanged. Relative `REPO` paths resolve from your
current directory. Daily sessions carry a restricted gateway key (`LITELLM_CODE_KEY` for
local aliases, or `LITELLM_CODE_CLOUD_KEY` when cloud routing is enabled), not the master
key.

Each run creates `~/agent-worktrees/<repo>-<timestamp>` on branch `agent/<timestamp>` from
`HEAD`; uncommitted changes in the source checkout are not included and never touched. Review
and test before merging:

```sh
agentiac worktrees                        # list worktrees and their state
agentiac worktrees review                 # summarize the latest session's worktree
agentiac worktrees review --diff          # show its complete committed diff
agentiac worktrees apply                  # fast-forward its original repository
agentiac worktrees drop <name>            # remove worktree and branch (refuses unmerged without --force)
agentiac worktrees prune [--apply]        # list (or remove) merged agent/ branches and worktrees
```

`review` and `apply` default to the latest saved `pi` session's worktree; pass a worktree
name from `agentiac worktrees` to select another. `apply` refuses uncommitted worktree
files, tracked changes in the original repository, and diverged histories, performing only a
fast-forward without committing or deleting the worktree.

`pi` runs on the control host with read/edit/write/bash tools and no permission prompts.
Daily `pi` state and sessions live in `artifacts/agents/pi-daily` (isolated from any
personal `pi` configuration).

### Long sessions and compaction

Long sessions compact by asking the model to summarize prior conversation:

- `agentiac code` computes `compaction.reserveTokens` in
  `artifacts/agents/pi-daily/settings.json` from every model in the session so smaller-window
  models never inherit an oversized reserve.
- `agents/pi/extensions/compaction-guard.ts` runs compaction with thinking off for models
  that have a chat-template thinking switch, preserving the answer budget for the summary
  while leaving agent turns thinking. After a failed compaction, automatic threshold checks
  back off exponentially (`2`, `4`, `8`, capped at `16`) until one succeeds; overflow recovery
  and explicit `/compact` always run. Outcomes log to
  `artifacts/agents/pi-daily/compaction-guard-log.jsonl`.

### Resume a saved pi session

`agentiac code --resume` resumes the most recently modified session under
`artifacts/agents/pi-daily/sessions` in its saved worktree and branch (refusing if that
worktree is missing). To resume an older session explicitly, use `--in-place` with `pi`'s
`--session` flag after `--`:

```sh
agentiac code /path/to/existing/worktree --in-place -- --session /path/to/session.jsonl
```

## Aliases

| Alias | Target | Use |
|---|---|---|
| `local-coder` | Explicitly selected route target (`agentiac route` shows it) | Default coding target |
| `<node>-worker[-<model>]` | Fixed node transport | Explicit routing and evaluation |
| `control-small` | Small local profile on the control host | Light tasks, summaries, smoke checks |
| `<node>-<backend>-<context>` | Specific declared target with identity guard | Evaluating or calling a fixed target |
| Legacy aliases (`local-coder-<legacy>`, retired names) | Resolved via `legacy_targets` / `legacy_sources` | Compatibility only; avoid in new work |
| `cloud-gpt-sol` | Explicit commercial alias via ChatGPT sign-in ([ADR 0005](adr/0005-chatgpt-subscription-alias.md)) | Explicit opt-in only; **code leaves control** |

## Automatic model choice (pi router)

```sh
agentiac code -m router                # asks before the first cloud turn
agentiac code -m router --allow-cloud  # no prompt (also supports print mode)
```

`router/auto` (`agents/pi/extensions/router.ts`) routes messages to `local-coder` unless
rules in `agents/pi/router.json` classify a turn as hard, in which case it routes to
`cloud-gpt-sol`—only in repositories listed under `cloud_repos` and after per-session
confirmation (unless `--allow-cloud` is set). Once a session switches to cloud, it stays
there; tool follow-ups and retries never switch models. Inside `pi`, use
`/route low|high|auto|status`. Metadata-only routing decisions log to
`artifacts/agents/pi-daily/router-log.jsonl`.

## Commercial model and routing

```sh
agentiac chatgpt login
agentiac chatgpt status
agentiac code ~/repo -m cloud-gpt-sol
agentiac chatgpt logout
```

`cloud-gpt-sol` is served by a separate `<prefix>-chatgpt` unit so its failure does not
affect local aliases. `agentiac route` prints the active `local-coder` target, and
`agentiac route <target>` switches it (restarting the gateway and cutting in-flight
requests). Claimed Strata targets must match the backend and context currently claimed on
that host (`claim ... --route` selects the matching target). There is no silent fallback: if
a target is down, requests fail with 503.

## On-demand hosts

Workers serve on demand ([ADR 0004](adr/0004-on-demand-serving.md)): the first active
request loads the model and waits for readiness, and the front unloads after
`idle_unload_minutes` in `infra/inventory.yml`. Health probes and gateway polling do not
wake or extend idle timers. Dedicated workers with `ondemand_persistent: true` keep their
front across reboots; borrowed workers (`borrowed: true`) use transient fronts.

```sh
agentiac claim <host> [--backend <id>] [--context <N>] [--idle <min>] [--no-warm] [--route]
agentiac warm [<host>]                 # load the current route host or named host now
agentiac status                        # state and idle countdown of every host
agentiac unload <host>                 # free the GPU now; next active request reloads
agentiac release <host>                # stop project workloads on a borrowed host and fall back to default
agentiac reclaim                       # re-claim the active local-coder target's host after reboot
agentiac ops start <host>              # switch a dedicated host back to always-on default_profile
```

`--idle 0` keeps a claimed model loaded until release or unload. Refused admission (host RAM
or foreign GPU workload) returns 503 with the reason and never falls back silently.
`release` reports anything left running and lists other users' GPU processes without
stopping them.

A claimed manifest is served one of two ways. A Strata host serves it from the root its engine
version has there (`strata_trial_roots` in `infra/inventory.yml`) under
`<prefix>-strata-inference`. An engine that serves from an image or a container has no such
root, so its own manifest says how it is served: `workers.<host>.service` names the `unit` this
project starts and stops (stopping it is what frees the GPU), the internal `port` the front
reaches, the `root` its launch files live in, the `container` name the trial watch reads the
engine's processes from, and the `worker` script - a `*.py` under `scripts/`, installed as the
`<root>/worker.py` that unit runs, beside the `service.json` holding the container, port and root
it launches with. A host that declares neither refuses the claim, and a declared service whose
recipe this checkout does not have is refused before the host is touched: the role in
`infra/unit.yml`'s `units:` map, the text `infra/templates/<role>.service.j2` and the declared
script, all three named by the refusal. `claim`'s unit playbook (`infra/unit.yml`) checks the text again on the
control side, so a role added without its template does not fail halfway through a claim.

After a reboot, `agentiac reclaim` restores the active `local-coder` host without moving
the route or restarting the gateway. Hosts armed with `auto_reclaim: true` in
`infra/inventory.yml` are also checked periodically by recovery-only automation (`ops recover
run --check|status|pause|resume|reset`; see [Operations](operations.md)). Run
`agentiac ops drift` to compare live state against the checkout, and
`agentiac ops eval-image` (or `ops eval-image build`) to verify the suite container image.

## Check that it works

```sh
agentiac check                     # local-coder: protocol checks + one pi repair
agentiac check -m control-small    # validate the path without touching a worker
agentiac check --no-pi             # protocol checks only
agentiac status                    # worker and gateway health
agentiac ops logs [--chatgpt]      # latest gateway log lines (metadata only)
```

## Start a session

```sh
agentiac doctor                    # summarize problems, unknowns, and notes with next steps
agentiac doctor --json             # single JSON document for tooling
agentiac doctor --snapshot         # also save snapshot under artifacts/closures/
```

`doctor` reads gateway, checkout, `ops drift`, `ops preflight`, expired suite leases, backup
status, and recovery status without loading any model (exit `0` clean, `1` problem, `3`
undetermined). After editing a `backends/*.json` manifest, re-claim active hosts before the
next gateway restart so the front reports the matching `manifest_hash` instead of 503. After
rebuilding the suite image, run `agentiac ops eval-image pin-suite`.

## Record a real task

```sh
agentiac log-task --repo <name> --type coding --outcome rejected --minutes 23 --failure output-capped
agentiac log-task --repo <name> --type review --outcome accepted --minutes 6 --dry-run
```

Appends one metadata-only JSONL record to `artifacts/tasks/tasks.jsonl` (never prompt text,
diffs, or paths). Outcomes are `accepted`, `edited`, `rejected`, and `abandoned`; failed
outcomes require one declared failure category (`wrong-code`, `incomplete`,
`context-overflow`, `output-capped`, `timeout`, `tool-misuse`, `hallucinated-api`,
`refusal`, `style`, `environment`, `other`). Refused calls leave the file untouched.

## See everything at once

```sh
agentiac observe
agentiac observe --source router --source loop-guard --json
agentiac observe --no-gateway --days 7
```

Aggregates five metadata-only sources (`suite`, `tasks`, `router`, `loop-guard` /
`compaction-guard`, and gateway `journald`) while reporting explicitly which fields are
excluded and distinguishing `absent`, `empty`, and `unavailable` sources.

## Models and recommendations

```sh
agentiac model list
agentiac model show <target>
agentiac model compatibility [--write]
agentiac model recommend --node <host> --task coding --explain [--no-storage]
agentiac model <target>
```

Outputs separate **declared** configuration (`gateway/local-coder.json`, `backends/*.json`),
**observed** state (`artifacts/inventory/<host>.json` and active route), and **evidence**
(`artifacts/evidence/evidence.sqlite`). `docs/compatibility.md` in the site repository is
generated from declared files only.

`model recommend` runs capability/headroom filtering (`license`, `node`, `context`,
`memory`, `disk`) and controlled comparison against the incumbent (`pass_rate` lower bound,
no new regression class, >=25% median wall reduction or capacity gain), returning
`incumbent`, `candidate`, or `insufficient-evidence` without downloading weights, loading
models, or changing routes.

## Limits

- Each worker serves one active request slot at a time; concurrent requests queue.
- `control-small` is intended for summaries, short edits, and harness checks rather than full
  repository coding.
- Updating pinned coding agents requires re-running `scripts/pin-agent.py` and committing
  the updated digest ([Agents](agents.md)).
- Suite overrides, deadlines, and provenance are documented in
  [Evaluation and validation](testing.md#suite-overrides-and-provenance).
