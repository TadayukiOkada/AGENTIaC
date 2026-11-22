# ADR 0004: On-demand model serving with idle unload

Status: accepted.

## Context

Shared and borrowed workers run alongside their owners' workloads, and agent work is bursty.
A claimed model should not hold GPU memory all day when idle; operators want models to load
on demand, support explicit warm-up, and unload automatically after a configurable idle
period.

## Decision

Run a small Python standard-library on-demand proxy front on each worker (`<prefix>-ondemand`,
`scripts/ondemand-proxy.py`). It listens on the API port forwarded by the control SSH tunnel,
while the inference backend moves to an internal loopback port (`18090`):

- A request arriving while the backend is unloaded runs admission checks first
  (`serve-profile.py --check` for llama.cpp hosts—checking host memory, load, and optional
  `max_gpu_used_mib`—or the Strata launcher's RAM/GPU admission) before starting the backend
  unit, holds the request until `/health` is ready, and returns 503 with the refusal reason
  if admission fails.
- When no request byte has passed for the configured idle period, the front stops the backend
  unit.
- `/health` and metadata GETs (`/v1/models`, `/models`, `/props`, `/slots`, `/metrics`) are
  passive: they answer 503 `{"ondemand": true}` while unloaded and never load or keep a model
  loaded, so gateway polling never wakes workers. A metadata GET with `X-Ondemand-Load: 1`
  counts as use (sent by the gateway's pre-call check). `/ondemand/{status,warm,unload}`
  require the backend key.
- Dedicated workers with `ondemand_persistent: true` in `infra/inventory.yml` run the front
  as an enabled systemd unit that survives a reboot while the backend unit stays disabled.
  Borrowed hosts keep a transient front that ends with `agentiac release` or reboot.

## Alternatives rejected

- **systemd socket activation (`systemd-socket-proxyd --exit-idle-time`)**: a refused start
  leaves the connection queued and re-triggers until the socket fails, counts open
  keep-alive connections as activity, and cannot answer with a structured refusal reason.
- **Control-side loading from the gateway**: the worker would keep the model loaded if the
  control host or tunnel dropped; idle unload belongs next to the GPU.

## Consequences

- Tunnels, gateway routes, aliases, and the loaded-model check remain unchanged; the
  gateway's pre-call check waits for a cold load while still failing fast when a tunnel or
  front is absent.
- The first request after an unload waits for the model load; `agentiac warm <host>` loads
  ahead so frontends with short header timeouts do not time out.
- Owner workloads win: while unloaded, the model holds no GPU memory, and a load is refused
  (503 with the reason) when admission checks fail, with no silent fallback
  ([ADR 0002](0002-evaluation-before-routing.md)).
- `agentiac release <host>` stops the front before the backend so nothing reloads.
