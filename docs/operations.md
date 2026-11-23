# Operations

Control installation uses the [locked bootstrap](../README.md#control-environment); keep
control, gateway, dashboard, and lint virtual environments isolated and do not upgrade
packages implicitly. Workers require existing Docker/NVIDIA runtime, CUDA compiler, `git`,
`g++`, `cmake`, `curl`, Python 3, and passwordless `sudo`. Never upgrade host drivers or
JetPack to troubleshoot.

## Worker builds, downloads, inventory, and benchmarks

```sh
agentiac ops inventory             # collect hardware facts into artifacts/inventory/<host>.json
agentiac ops hosts                 # inspect worker status and active build/download jobs
agentiac ops build <host>          # persistent source build on worker NVMe
agentiac ops runtime <host>        # build content-tagged runtime image (libgomp/libstdc++)
agentiac ops download <host> [--model <profile>]
agentiac ops start <host> [--model <profile>]   # always-on llama.cpp unit (disables on-demand front)
agentiac ops stop <host>           # stop on-demand front and inference unit
agentiac ops metrics <host> --seconds 600 --interval 10
agentiac ops memory <host>         # newest stored memory reading (0 ok, 1 below floor, 3 stale)
```

`ops inventory` records each hardware fact with `value`, `source`, and `confidence`
(`detected`, `manual`, `unknown`), plus `collected_at` and `hw_fp` (RAM rounded to whole
GiB). Re-run after hardware/driver changes and at least every 30 days, because expired
records strip `hw_fp` from new suite results.

`ops download` resumes `.part` files and verifies SHA256 before moving weights into place
alongside upstream license/notice metadata. `ops start` serves `default_profile` (llama.cpp)
always on and is separate from `agentiac claim <host>` (on-demand serving,
[ADR 0004](adr/0004-on-demand-serving.md)).

```sh
agentiac ops eval-image            # inspect suite container label against benchmarks/Dockerfile
agentiac ops eval-image build      # rebuild suite sandbox image (--pull=false)
agentiac ops eval-image pin-suite  # update benchmarks/suite/image.lock.json after deliberate rebuild
agentiac eval bench run --model <alias> --profile profiles/<name>.json
agentiac eval bench status
agentiac eval bench export
agentiac ops pipeline <host>
```

Evaluation containers disable networking, drop capabilities, bound memory/CPU/PIDs, and
mount task workspaces plus read-only graders. `collect-deployment.py` captures container
args/image, compiled backend metadata, and active profile; `prepare-context.py` builds real
tracked-source corpora without padding. Every worker API uses a distinct generated key in
`secrets/nodes/<host>.key` (mode `0600`).

## Deploying changes

Declared state is in Git and runtime state is in `artifacts/`
([ADR 0007](adr/0007-declared-and-runtime-state.md)).

```sh
agentiac ops deploy --check            # show planned diffs without applying
agentiac ops deploy                    # refuses dirty site or dirty/unpinned core/; --allow-dirty overrides
agentiac ops deploy --only gateway     # or tunnels, chatgpt, watch
agentiac ops drift                     # read-only comparison of live state vs checkout
agentiac ops preflight <host...>       # check prerequisites before running on hosts
```

`ops deploy` does not sync Python locks: run `uv pip sync` on `.venv-gateway` first when
`requirements-gateway.lock` changes. When the site pins `core/`, `ops deploy` records both
`site` and `core` revisions in `artifacts/deployed.json` and `ops drift` checks both. Route
switches whose gateway fails to restart restore the previous selection.

Before editing `gateway/gateway_callbacks.py`, run the isolated rehearsal away from the live
gateway:

```sh
.venv-gateway/bin/python scripts/gateway-rehearsal.py
```

If `backends/*.json` changes and the gateway restarts, re-claim any active host so its
on-demand front serves the matching `manifest_hash` instead of returning 503:

```sh
agentiac claim <host> --backend <backend-id> --context <context>
```

`make lint` runs `yamllint`, `ansible-lint`, and `shellcheck` from `.venv-lint`. Host
playbooks refuse to run without `--limit`; `agentiac` always passes it.

### Reinstalling pi

`agents/pi/package.json` and `agents/pi/package-lock.json` pin the `pi` frontend:

```sh
mkdir -p /tmp/pi-check && cp agents/pi/package.json agents/pi/package-lock.json /tmp/pi-check/
(cd /tmp/pi-check && npm ci --ignore-scripts)
diff -rq --exclude=.package-lock.json /tmp/pi-check/node_modules artifacts/frontends/pi/node_modules
```

Do not replace `artifacts/frontends/pi` while daily sessions or Suite runs are active.

### After a reboot

A control or borrowed-host reboot clears transient fronts, watches, and tunnels. Run
`agentiac reclaim` to re-claim the active `local-coder` host with its target's backend and
context without restarting the gateway. On borrowed hosts, confirm first that the owner has
not reclaimed the machine.

Targets whose ledger entry in `infra/retention.yml` is `form: thin` cannot be routed to,
claimed, or reclaimed because a thin root holds no weights; restore weights first with
`agentiac ops restore <host> --backend <id>` or rebuild with `ops prepare`.

### Dedicated and borrowed workers

Dedicated workers have no `borrowed: true` key in `infra/inventory.yml` and declare
`ondemand_persistent: true` and `auto_reclaim: true`; the `default` fallback target in
`gateway/local-coder.json` must remain on a dedicated worker. Borrowed hosts (`borrowed:
true`) are claimed for a stated purpose and returned with `agentiac release <host>`, which
stops project workloads there and switches `local-coder` back to `default`.

### Recovery-only automation

`<prefix>-recovery.timer` runs `agentiac ops recover run` periodically on control. It reads
`ops drift`'s route line and re-claims only the already-selected dedicated host (never moving
routes or restarting the gateway). Six gates stand it down (`ops recover status`,
`doctor`'s `recovery` line):

| Gate | What it refuses |
|---|---|
| `route` | The route answers or cannot be parsed (`control` serves its own target) |
| `armed` | The host lacks `auto_reclaim: true` in `infra/inventory.yml` |
| `credential` | Control holds no key under `secrets/nodes/` for that host |
| `pause` | `agentiac ops recover pause MIN --reason "..."` is active |
| `cooldown` | A reclaim was attempted within `recovery.COOLDOWN_S` (20 min) |
| `breaker` | 3 consecutive reclaims failed (`recovery.MAX_FAILURES`; clear with `ops recover reset`) |

Cleanly stopped or unreleased hosts (`ops stop`, `release`) are left stopped rather than
re-claimed; unreachable SSH hosts stand down rather than accepting changed host keys.

```sh
agentiac ops recover run --check
agentiac ops recover status
agentiac ops recover pause 60 --reason 'ops stop'
agentiac ops recover resume
agentiac ops recover reset
```

### Mutation lock and claim records

`claim`, `release`, `reclaim`, `route TARGET`, `ops deploy` (without `--check`), and the
recovery timer's reclaim serialize on `artifacts/locks/mutation.lock` (`src/agentiac/mutation.py`)
and append start/end entries to `artifacts/locks/journal.jsonl`. Read-only commands
(`status`, `doctor`, `ops drift`, `route`, `warm`, `unload`) take no lock. `claims.py`
writes active claim records to `artifacts/claims/<host>.json` for `ops drift`.

### Rotating credentials

1. Run `.venv/bin/python scripts/bootstrap-secrets.py --rotate` to regenerate keys in `secrets/`.
2. Run `agentiac ops deploy --only gateway` and `agentiac ops deploy --only chatgpt`.
3. Re-claim active hosts (`agentiac claim <host> ...`) or run `ops start` so workers receive
   their updated `api.key`.

### Backups and restoring control

The control host holds the gateway, tunnels, ChatGPT refresher, evaluation store, and
`artifacts/` records (single control point of failure).

| Asset | Recovery path |
|---|---|
| Git checkouts (`site` + `core/`) | Clone site with `--recurse-submodules`; verify via `ops drift` |
| `secrets/gateway.env`, `secrets/nodes/*.key` | `scripts/bootstrap-secrets.py --rotate`, deploy gateway/chatgpt, re-claim workers |
| `secrets/chatgpt/auth.json` | Operator runs `agentiac chatgpt login` |
| `.venv*` environments | Rebuild from `requirements-*.lock` and run `make install` |
| Suite container image | `agentiac ops eval-image build` |
| Units, gateway config, active route | `agentiac ops deploy`, then `agentiac route TARGET` |
| `artifacts/` evaluation & task history | Control set-B backup (`artifacts/suite/state.sqlite` via `sqlite3` backup API) |
| `artifacts/evidence/evidence.sqlite` | Rebuilt from primary records via `agentiac evidence import` |
| `private/management/` | Separate offline dashboard snapshots ([Offline snapshots and credentials](dashboard.md#offline-snapshots-and-credentials)) |

Set-B backups exclude reproducible caches (`frontends/`, `uv-cache/`, `corpora/`,
`backends/`, `tmp/`, staging), `evidence/evidence.sqlite`, legacy management state, and live
WAL/SHM files (`REPRODUCIBLE` and `PRIVATE` in `src/agentiac/backup.py`). Verify archives
with `agentiac ops backup verify`.

**Restore.** `agentiac ops restore --into PATH` downloads the recorded archive (or
`--archive NAME --sha256 HEX`), verifies SHA256 before extraction, refuses existing records
or the live checkout, extracts safely into `artifacts/` with `tarfile.data_filter`, and
rebuilds `evidence.sqlite`. Then complete the remaining operator steps in order:

1. `make install` - the unit files, the wrapper and the venv on the new control host.
2. `tailscale up` - enrolment, and the host-key approval at https://login.tailscale.com/admin/machines.
3. `scripts/bootstrap-secrets.py --rotate` - secrets are not in the archive, so a rebuilt control has none until this runs.
4. `agentiac ops deploy` - re-render the units and the gateway; it reaches the workers over the tailnet and reads the secrets, so it comes after the two steps above.
5. `agentiac chatgpt login` - the ChatGPT sign-in for cloud-gpt-sol, then ops deploy --only chatgpt (a browser and a human).
6. `agentiac claim HOST` - the hosts the route needs, then `agentiac route TARGET`.
7. `agentiac check` - last, and the proof the restored host serves.

### Strata preparation, upgrades, and two-generation retention

See [Lifecycle](lifecycle.md) for the end-to-end promotion, cutover, thinning, and retirement workflow.
Strata loads gate on free host RAM (`min_available_gib + 4 GiB` headroom in
`scripts/strata-start-worker.py`). On unified-memory Jetson nodes, the unit's
`ExecStopPost=+` and pre-start retry flush idle `nvmap` page pools via
`/sys/module/nvmap/parameters/shrink_page_pools` without touching running GPU processes.

Before preparing or upgrading a root, inspect storage, upgrade plan, and reusable weights:

```sh
agentiac ops storage <host>
agentiac ops storage --candidates
agentiac ops upgrade plan <host> --backend <id>
agentiac ops upgrade reuse <host> --backend <id>
agentiac ops prepare <host> --backend <id> [--reuse-root PATH | --reuse-declared | --download] [--isolated] [--check]
```

`ops prepare` refuses to overwrite a root owned by another ledger entry, refuses `form: thin`
sources, and copies reused weights onto independent inodes (`nlink == 1`, verified by
SHA256; [ADR 0008](adr/0008-two-generation-retention.md)).

Root retirement and thinning follow a two-stage, reference-gated flow:

```sh
agentiac ops retire <host> --backend <id> --check
agentiac ops retire <host> --backend <id> --quarantine
agentiac ops retire <host> --backend <id> --purge        # or --name <dir> --purge
agentiac ops thin <host> --backend <id> [--check]
agentiac ops restore <host> --backend <id> [--check]
```

`--quarantine` checks declared references and the running `<prefix>-ondemand` unit first,
archives preparation metadata to `artifacts/retired/<host>/`, scans for credential-shaped
strings before moving, and moves the root to staging on the same filesystem. `--purge`
requires the quarantine record, ledger entry, and a 24-hour wait (overridable only with
`--shorten-wait REASON`). `ops thin` moves identical weights to staging after verifying
separate copies and headroom; `ops restore` copies verified weights back onto independent
inodes.

### Aggregate reports, probes, ladder guard, and watchdogs

- **Aggregate reports**: `agentiac eval report artifacts/benchmarks/<node>/<model>/state.sqlite`
  exports allowlisted metadata only (run outside restrictive sandboxes when WAL/SHM sidecars
  are needed).
- **Protocol probes**: `PYTHONPATH=src .venv/bin/python scripts/protocol-smoke.py ...` tests
  Chat Completions, Responses, and Anthropic Messages over loopback.
- **Backend probes**: `agentiac ops backend-probe <host> <probe-id>` runs bounded container
  compatibility checks without modifying services or routes.
- **Ladder guard**: `scripts/ladder.py run --alias <alias> ...` checks `GET /ondemand/status`
  against `artifacts/gateway/routes.json` before, between, and after depths so measurements
  never attribute to the wrong backend.
- **Evaluation watchdogs**: `agentiac eval watch check|status|ack` inspects
  `<prefix>-evaluation-watch.timer` and `<prefix>-evaluation-watch-health.timer`;
  `<prefix>-update-check.timer` runs `ops update check --notify` daily. For concurrency
  sweeps, set `benchmark.concurrency` to the tested list so expected request counts match.
- **Container cleanup**: after capturing exit status, logs, and artifacts, remove only
  specific stopped evaluation containers you own; never run blanket `docker system prune`
  on shared workers.
