# Model, configuration, and alias naming

AGENTIaC separates physical nodes, upstream model weights, declared backend manifests,
deployment configurations, route target keys, gateway API aliases, and backend served IDs.
Full descriptive names (`full_name`) remain catalogue labels rather than gateway API IDs.

## Entities and terminology

| Kind | Meaning | Shape / location |
|---|---|---|
| Node | Physical worker or local host | `<host>` in `infra/inventory.yml` |
| Upstream model | Weight repository and pinned revision | `<org>/<model>` + commit SHA |
| Backend | Engine, weights, and launch configuration | `backends/<name>.json` |
| Deployment configuration | Node + backend + context + max output + thinking | Target of `full_name` |
| Target key | Route selector key in `gateway/local-coder.json` | `<node>-<backend>-<context>` |
| API alias | Model name accepted by the LiteLLM gateway | `<target>` or legacy alias |
| Runtime selection alias | Dynamic alias routed to the active target | `local-coder` |
| Backend served ID | Model ID returned by the inference engine | Manifest/profile served ID (not a full config ID) |

A target key or API alias that includes a node name identifies a **deployment configuration**,
not the upstream model itself. One node can declare multiple configurations, so node names
alone never imply which engine or model is loaded.

## Inspecting declared names

```sh
agentiac model                       # primary display name, aliases, and class/config IDs
agentiac model names                 # target key -> descriptive full_name -> API aliases
agentiac model names --json          # pins, engine packages, quantization source, missing IDs
agentiac model names --write         # regenerate site docs/model-names.md
agentiac model list                  # list targets with descriptive display names
agentiac model show <target> --json  # inspect declared, observed, and evidence state separately
agentiac models                      # list aliases including the active local-coder target
```

## Declaration display name

The catalogue display name uses `--` between levels and `-` within a segment:

```text
<node>--<engine>-<version>--<publisher>-<model[-variant]>--<quantization>--ctx<N>--out<N>--decl<digest>
```

- Explicit `manifest.quantization` takes precedence; otherwise the label comes from the pinned
  weight filename and records its provenance without claiming tensor inspection. Variants
  (such as `gyro-m`) stay distinct from quantization formats (such as `tq2_0`).
- `decl<digest>` hashes the full backend manifest contents, target configuration parameters,
  and effective gateway inference parameters—excluding alias names, backend filename,
  transport `source_alias`, API keys, and API base URLs. Identical configurations across
  aliases keep the same digest while leaving historical `class_id` and `config_id` keys
  unchanged.
- `full_name` is a human-readable catalogue identifier, not an API model parameter.

## Node-first naming rules

```text
backend   = <engine>-<engine-version>-<model>-<quantization>[-<variant>]
target    = <node>-<backend>-<context>
transport = <node>-worker[-<model>]
```

- Engine and model abbreviations come from `src/agentiac/backend_names.py`. The engine
  release version follows the engine token; any separate engine package version stays inside
  the manifest. Quantization uses the lowercase main-weight label, keeping `_` and omitting
  `mixed-`.
- Backend names omit node names (constraints live in `workers`) and context lengths (held by
  targets) so one backend manifest can serve multiple nodes or contexts. If four elements
  collide for different launch settings, append a short disambiguation suffix.
- The backend file stem is the declared backend name referenced by inventory, retention,
  components, route targets, and `--backend`; manifest `id` remains the runtime identity
  reported by the worker front and Strata preparation state. `strata_trial_roots` resolves by
  filename, then manifest `id`, then engine version.
- Context sizes divisible by 1024 render as `<N>k` (for example `128k`, `256k`). If two
  targets share the same base name with different output limits, thinking settings, or launch
  options, append `-d<declaration_id>`.

## Compatibility, aliases, and completion

Five name families can coexist in a site:

| Family | Meaning |
|---|---|
| `local-coder` | Runtime-selected target set by `agentiac route` |
| `local-coder-<legacy-target>` | Legacy fixed API alias wrapping an older target key |
| `<node>-<backend>-<context>` | Node-first target key and primary API alias |
| `<node>-worker[-<model>]` / `cloud-gpt-sol` | Node transport or explicit cloud alias |
| `<node>--...--decl<digest>` | Display-only catalogue name (never sent to the API) |

In `gateway/local-coder.json` version 2, `targets` are keyed by node-first target names
while `legacy_targets` and `legacy_sources` (plus `backends/legacy-names.yml`) keep stored
`route.json`, route logs, evaluation history, and legacy CLI selectors resolving without
rewriting historical databases.

Shell completion (`code -m`, `check -m`, `model`, `route`) surfaces node-first names and
hides retired equivalents while still accepting legacy names. `make check` enforces
`model_names.short_alias_coverage` and `declared.references` against `backends/legacy-names.yml`.
Adding a new target updates declarations, verifies `recommended_short_alias`, regenerates
`docs/model-names.md` via `agentiac model names --write`, and creates fresh evaluation case
IDs without mutating past SQLite records.
