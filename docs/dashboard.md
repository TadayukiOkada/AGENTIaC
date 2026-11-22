# Local management dashboard

Read-only management API, React UI, foreground minimal-probe worker, offline state
snapshots/credentials, and local user-service release/plan tooling. See
[Architecture](architecture.md#local-management-dashboard) for module boundaries.

## Install and start locally

Run from the core or site checkout using Python 3.12 and Node 24.22.0. Dashboard and audit
dependencies live in `.venv-dashboard`, separate from control and gateway environments:

```sh
uv venv --python 3.12 .venv-dashboard
uv pip sync --python .venv-dashboard/bin/python requirements-dashboard.lock
cd dashboard
npm ci
npm run check
npm run build
cd ..
PYTHONPATH=src .venv-dashboard/bin/python scripts/dashboard-schema.py --check
agentiac dashboard owner init
agentiac dashboard serve --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/dashboard/` locally or over an SSH port forward. UI and API
must share the same browser origin; the production bundle is served by the API. Owner
initialization prompts interactively, requires 12..256 characters, and refuses credential
overwrite; never reuse an inference key. Five failed logins within ten minutes trigger a
temporary throttle, cleared by a successful login.

If `session.sqlite` or `operations.sqlite` disappears, the running API fails closed with
`503 state_missing` rather than recreating an empty database; restore offline and restart.
Management state lives in `private/management/` (excluded from set-B exports). Never mount
management state in task containers, commit it, or bind the API to a public interface.

## Opt in to minimal probes

API startup alone leaves probe capability unavailable and returns 503 for new probe
requests. In a separate terminal, start the foreground worker naming each allowed inventory
target explicitly (no wildcard or default-to-all):

```sh
agentiac dashboard worker --serve --target <node>
```

Starting the probe worker does not claim a GPU, start a model, restart a service, or change
a route. Only an authenticated operator/admin session with a valid CSRF token may enqueue a
job. The API validates the worker's PID, start ticks, boot ID, implementation receipt,
10-second heartbeat TTL, and target allowlist. Repeated idempotency keys return the original
job; new requests are rate-limited to one per node every five seconds and run serially.

The minimal probe reads Linux OS/kernel/architecture, CPU count, and `MemTotal`/`MemAvailable`
only via `/usr/bin/python3` stdlib over SSH (`BatchMode`, `StrictHostKeyChecking=yes`, no
user SSH config, multiplexing, or forwarding). It writes nothing on the remote host, runs no
GPU/service commands, and records a snapshot with a 60-second freshness window separate from
`hw_fp`. Missing `known_hosts` entries fail without auto-trusting host keys.

Each job has a 1..30 s execution deadline, bounded cleanup grace, and a 35 s remote handshake
alarm. Before executing, the worker durably records prelaunch intent, endpoint, local
process, and remote handle, then records authorization before sending `GO`. Raw stderr, SSH
command text, and execution handles are never exposed by the job API.

## Cancel and recover

Queued cancellation completes immediately. Running cancellation stops the owned probe and
verifies local and remote absence before marking the job cancelled. Pressing Ctrl-C stops
the foreground worker and withdraws readiness; any interrupted active attempt must be
reconciled before the worker accepts new jobs:

```sh
agentiac dashboard worker --reconcile JOB_ID
agentiac dashboard worker --reconcile JOB_ID --stop-probe
```

Reconciliation acquires the control-host OS lock and uses the saved endpoint and nonce
rather than edited inventory files. For SSH targets it stops only a proven owned local
transport (and with `--stop-probe`, signals only the matching remote probe process). For
`connection: local` targets it inspects without signalling unless `--stop-probe` is passed.
Ambiguous identity or unreachable hosts leave the attempt unresolved; manual status edits,
PID-wide kills, and automatic retries are unsupported.

## Offline snapshots and credentials

Use `agentiac dashboard ...` (or the site wrapper command). Supply `--root`/`--state-dir`
before the subcommand, or configure `AGENTIAC_MANAGEMENT_ROOT` /
`AGENTIAC_MANAGEMENT_STATE` for services. Stop both API and worker before offline
maintenance so process-lifetime locks are released:

```sh
agentiac dashboard backup /ABSOLUTE/PRIVATE/backups/snapshot-001
agentiac dashboard verify /ABSOLUTE/PRIVATE/backups/snapshot-001
agentiac dashboard restore /ABSOLUTE/PRIVATE/backups/snapshot-001 /ABSOLUTE/PRIVATE/restored-state
agentiac dashboard --state-dir /ABSOLUTE/PRIVATE/restored-state owner recover \
  --backup /ABSOLUTE/PRIVATE/backups/before-owner-reset --confirm-owner owner
agentiac dashboard owner rotate --backup /ABSOLUTE/PRIVATE/backups/before-rotation
```

Destinations must be new private directories outside `artifacts/`. Snapshots use the
[SQLite backup API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup),
verify integrity/schema and SHA256 manifests, preserve operational jobs/attempts/receipts,
and strip sessions, login throttles, and stale readiness. Keep snapshots local and
owner-only; interrupted operations leave a `.incomplete` marker and are refused.

`dashboard restore` creates an inactive candidate requiring offline `owner rotate` or
`owner recover --confirm-owner owner` before startup so old passwords cannot reactivate
silently. Never run original and restored state queues side by side, and never pass
passwords in chat, shell arguments, or API URLs.

## Dedicated releases and user-service plans

Code, built UI assets, lockfiles, and JSON schemas (`releases.CODE_DATA`) can be staged into
an immutable release directory outside the checkout with its own `.venv-dashboard`, while
declarations stay in the site checkout and state stays in `private/management/`:

```sh
mkdir -p ~/agentiac-dashboard/releases
agentiac dashboard release prepare ~/agentiac-dashboard/releases/RELEASE_ID --source "$PWD"
agentiac dashboard release provision ~/agentiac-dashboard/releases/RELEASE_ID
agentiac dashboard release verify ~/agentiac-dashboard/releases/RELEASE_ID --environment
agentiac dashboard plan --release ~/agentiac-dashboard/releases/RELEASE_ID \
  --history "$PWD/private/dashboard-deployment" --output "$PWD/private/activate-RELEASE_ID.json" \
  --port 8043 --ssh-tunnel
```

Replace `--ssh-tunnel` with `--tls-cert /ABSOLUTE/cert.pem --tls-key /ABSOLUTE/key.pem --tls-ca /ABSOLUTE/ca.pem`
for native loopback HTTPS using Python's
[SSL context](https://docs.python.org/3.12/library/ssl.html#ssl.SSLContext.load_cert_chain)
(no LAN bind, no host trust-store mutation, owner-only keys).

Plans manage only `<prefix>-dashboard.service` and optional
`<prefix>-dashboard-probes.service` in the operator's user systemd manager. Probe `--target`
flags must match current declared inventory node names (`former_names` do not resolve in the
probe worker; `agentiac doctor` flags retired or undeclared targets). Custom paths with
whitespace or systemd specifiers, foreign/symlinked units, and plan drift are refused. Each
plan is single-use and applied only with its exact SHA256:

```sh
agentiac dashboard apply /ABSOLUTE/PRIVATE/activate-RELEASE_ID.json --approve-plan PLAN_SHA256
```

`apply` checks DB schema compatibility before downtime, stops the two owned units, takes an
offline snapshot, installs unit files, reloads systemd, starts the units, and verifies
authentication and compiled UI assets (waiting up to 30 s for API startup). Failures
compensate unit files/enablement while preserving DBs; unresolved failures retain a private
intent blocking further activation until inspected and archived to `failed-PLAN_ID.json`.
Emergency stop and code-only rollback use the same boundary:

```sh
agentiac dashboard plan --action stop --history "$PWD/private/dashboard-deployment" \
  --output "$PWD/private/stop-RELEASE_ID.json"
agentiac dashboard apply "$PWD/private/stop-RELEASE_ID.json" --approve-plan STOP_PLAN_SHA256
agentiac dashboard plan --action rollback --history "$PWD/private/dashboard-deployment" \
  --output "$PWD/private/rollback-RELEASE_ID.json"
agentiac dashboard apply "$PWD/private/rollback-RELEASE_ID.json" --approve-plan ROLLBACK_PLAN_SHA256
```

## Validation, caching, views, and quality gates

```sh
PYTHONPATH=src .venv-dashboard/bin/python -m unittest discover -s tests -p test_services.py -v
PYTHONPATH=src .venv-dashboard/bin/python -m unittest discover -s tests -p 'test_management*.py' -v
PYTHONPATH=src .venv-dashboard/bin/python -m unittest discover -s tests -p test_jobs.py -v
PYTHONPATH=src .venv-dashboard/bin/python -m unittest discover -s tests -p test_probe_worker.py -v
PYTHONPATH=src .venv-dashboard/bin/python -m unittest discover -s tests -p 'test_dashboard_*.py' -v
PYTHONPATH=src .venv-dashboard/bin/python scripts/dashboard-schema.py --check
cd dashboard && npm run check && npm run build && npm run test:smoke && npm audit
```

The browser smoke uses disposable credentials/DBs, its own loopback API/Chrome profile, and
a local child probe fixture (`CHROME_BIN`, `DASHBOARD_PYTHON`, and
`DASHBOARD_SMOKE_SCREENSHOTS` are optional overrides). Regenerate the OpenAPI/TypeScript
contract with `scripts/dashboard-schema.py` after changing DTOs or routes.

- **Declaration caching & cursors**: list/detail endpoints share a 30 s per-process cached
  declaration snapshot (`generated_at`, `age_s`, `ttl_s`, `stale`, `error`) and opaque
  `next_cursor` pagination; failed rebuilds log and serve the previous snapshot as `stale: true`
  (or `503 snapshot_unavailable` with no prior snapshot).
- **Related resources (`GET /api/v1/related/{nodes|models|engines|agents|configuration}/{id}`)**:
  links nodes, targets, backends/profiles, agents, retention policies, and newest jobs,
  preserving missing references as `exists: false`.
- **Engines (`GET /api/v1/engines[/{id}]`)**: combines backend pins, route targets,
  retention, and `backends/lifecycle.yml` metadata (`current` reflects the active `local-coder`
  target, not a live worker probe).
- **Deployments (`GET /api/v1/deployments`)**: reads local `artifacts/deployed.json`, route
  state/logs, and dashboard deployment history (`AGENTIAC_DASHBOARD_HISTORY`, default
  `private/dashboard-deployment`), returning allowlisted metadata only.
- **Display names**: every view shows node-first display names alongside route keys
  ([Model naming](model-naming.md)).
- **Quality gates**: `test_management_contract.py` enforces permissions, CSRF, same-origin
  `Origin`, `Cache-Control: no-store`, shared error shapes, and secret/traceback redaction on
  every `/api/v1` route; load and structural browser accessibility gates run in tests/smoke.
