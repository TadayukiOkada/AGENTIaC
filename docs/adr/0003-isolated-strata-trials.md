# ADR 0003: Isolate Strata trials from default model serving

Status: accepted for experimental evaluation; production promotion remains capability-gated.

## Context

Experimental inference engines such as Strata have their own engine binaries, model pack
preparation, and API server. Existing dedicated-worker profiles describe llama.cpp, so
starting Strata through that lifecycle would misattribute backend provenance. Borrowed
workers may also run unrelated desktop or owner workloads alongside CUDA toolkits.

## Decision

Keep Strata in a separate backend manifest (`backends/*.json`), per-engine-revision worker
root (`strata_trial_root`), dedicated inference unit, authenticated loopback port, and
transient control SSH tunnel:

- Pin the engine source, its ggml dependency, the model revision, and shard SHA256 digests.
  Build for each worker's CPU/GPU with the existing CUDA toolkit; do not invoke installer
  paths that replace system drivers or reuse an unpinned release engine.
- Validate mocked API and setup behavior first, then verify real downloaded weights, check
  live resource admission, and run bounded evaluation suites. Save environment failures
  separately from model scores, keep detailed traces on control, and stop the trial's
  serving and tunnel processes afterward to release resources.
- Claim and release through the same borrowed-host lifecycle: `agentiac claim <host>` pushes
  a control-held key and starts the transient inference/on-demand unit and SSH tunnel (with
  a bounded lifetime); `agentiac release <host>` stops both.
- For long-context manifest backends where resident headroom must breathe, omit a hard RAM
  cgroup cap and instead set `OOMScoreAdjust=1000`, disable swap usage for the trial, enforce
  admission at the manifest's free-RAM need plus 4 GiB headroom, and run a worker watch that
  stops the front and engine below 2 GiB `MemAvailable` or when a foreign GPU process
  appears. Manifests may set per-host engine environment variables (`workers.<host>.env`,
  such as `STRATA_RESIDENT_HEADROOM_GIB`).
- Leave default models, aliases, and gateway routes unchanged until promoted under
  [ADR 0002](0002-evaluation-before-routing.md).

## Consequences

Experimental backend manifests are distinct from llama.cpp profiles and must not be started
through llama.cpp-only paths. A successful mock API test establishes neither inference
quality nor hardware compatibility. A trial that changes both model and backend cannot
isolate backend speed or justify promotion by itself; repeatability, longer-context runs,
and frontend tests remain separate evidence before promotion.
