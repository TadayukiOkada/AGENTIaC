# Lifecycle: Engines, Models, and Coding Agents

This guide defines the end-to-end lifecycle for inference engines, quantized models, and coding-agent frontends, tying together [ADR 0002](adr/0002-evaluation-before-routing.md), [ADR 0003](adr/0003-isolated-strata-trials.md), [ADR 0004](adr/0004-on-demand-serving.md), [ADR 0007](adr/0007-declared-and-runtime-state.md), [ADR 0008](adr/0008-two-generation-retention.md), and [ADR 0009](adr/0009-detect-and-propose-apply-by-approval.md).

Core principle: **detection and planning are automatic; state transitions, promotions, and deletions require explicit operator approval**. Declared pins and retention ledgers live in Git, while active routes, claim records, and evaluation traces live in `artifacts/`.

---

## Part A: Engine and Model Lifecycle

```text
Stage 0: Detect & Scope   (ops update check / plan / storage)
   │
   ▼
Stage 1: Draft & Prepare  (ops upgrade draft / ops prepare in isolated root)
   │
   ▼
Stage 2: Offline Eval     (check --no-pi, eval ladder, eval suite, evidence)
   │
   ▼
Stage 3: Runtime Trial    (claim standby --route; primary stays untouched on N)
   │
   ▼
Stage 4: Acceptance &     (rolling cutover: standby -> primary -> default route;
         Default Cutover   declare pins, retention, gateway, generated docs)
   │
   ▼
Stage 5: Observation &    (6 confirmation gates -> optional ops thin on N-1)
         Optional Thinning
   │
   ▼
Stage 6: Two-Stage Retire (remove refs -> ops retire --quarantine -> 24h -> --purge)
```

### Stage 0: Detect and Scope

Inspect upstream updates and worker headroom without modifying any host:

```sh
agentiac ops update check
agentiac ops update plan <component-id>
agentiac ops storage <host>
```

Classify the change before drafting:
- **Same-weight engine upgrade:** new engine revision/version pinning the exact same weight files and SHA-256 checksums.
- **New model or quantization:** new weight files, quantization, context window, or architecture.

### Stage 1: Draft and Prepare (Isolated)

1. Draft candidate declarations (dry-run first, then `--apply` in a clean worktree):
   ```sh
   agentiac ops upgrade draft <host> --version <ver>
   agentiac ops upgrade draft <host> --version <ver> --apply
   agentiac ops upgrade plan <host> --backend <backend-id>
   agentiac ops upgrade reuse <host> --backend <backend-id>
   ```
2. Verify declarations locally (`make check && make site-lint`).
3. Prepare an isolated root on the candidate host (`--reuse-declared` copies identical verified bytes onto independent inodes `nlink == 1`; `--download` fetches new weights):
   ```sh
   agentiac ops prepare <host> --backend <backend-id> --reuse-declared   # or --download
   ```

### Stage 2: Offline Evaluation

Evaluate the candidate before moving the shared `local-coder` route:

1. **Claim without switching `local-coder`:**
   ```sh
   agentiac claim <standby-host> --backend <backend-id>
   ```
2. **Protocol smoke:**
   ```sh
   agentiac check --no-pi -m <candidate-target>
   ```
3. **Context ladder and comparison:**
   ```sh
   agentiac eval ladder run --alias <candidate-target> --depths 4096 32768 65536 102400 --reps 3
   agentiac eval ladder compare <baseline-run> <candidate-run>
   ```
   Verify `100%` recall anchors, `100%` identity-guard agreement, and inspect trial watch telemetry (`MemAvailable` floor, cgroup peak, swap, and post-unload memory/pool recovery).
4. **Controlled task Suite and evidence (when quality or model behavior may change):**
   ```sh
   agentiac eval suite run --alias <candidate-target> --k 3
   agentiac eval report --failures --alias <candidate-target> --against <incumbent-target>
   agentiac evidence import
   agentiac model recommend --node <host> --task coding --explain
   ```

### Stage 3: Runtime Trial (Dogfooding)

On a dedicated worker pair, test the candidate in daily interactive use on the **standby** worker while keeping the **primary** worker untouched on `N`:

```sh
agentiac claim <standby-host> --backend <backend-id> --route
```

- Leave `default` in `gateway/local-coder.json` pointing to the primary `N` target during the trial.
- Instant rollback at any point: `agentiac route <primary-N-target>`.
- Record real-use outcomes with `agentiac log-task` and monitor `agentiac status`, `agentiac doctor`, `agentiac ops drift`, and worker watch logs.

### Stage 4: Acceptance and Default Cutover

Apply the acceptance criteria matching the change class:

| Change class | Acceptance criteria |
|---|---|
| Same-weight engine upgrade | `0` protocol/infra failures; `100%` ladder recall anchors & identity guard; no new Suite regression class (if run); acceptable prefill/TTFT/decode and memory/recovery envelope; clean dogfooding; tested rollback. |
| New model or quantization | Pass rate not below incumbent lower 95% CI bound; no new regression class; `>= 25%` lower median wall time OR capacity gain; tested rollback. |

**Rolling cutover order for a dedicated worker pair:**
1. Promote the **standby** worker's declared serving generation (`N -> rollback`, `N+1 -> serving`, `N-1 -> candidate` with `retirement: candidate`).
2. Prepare and sanity-check the **primary** worker (`ops upgrade draft` / `ops prepare --reuse-declared` / ladder check).
3. Promote the **primary** worker and update `default` in `gateway/local-coder.json`.

**Declared-file checklist for default promotion:**
- `infra/inventory.yml`: update `strata_backend` (or `strata_default_backend`).
- `infra/components.yml`: update the component `pin` (`version`, `revision`, `lock`) and `gate`.
- `infra/retention.yml`: update `serving`, `rollback`, and `candidate` (`retirement: candidate` on `N-2`).
- `gateway/local-coder.json`: update `default` (and `legacy_targets` if used).
- Regenerate catalog docs:
  ```sh
  agentiac model compatibility --write
  agentiac model names --write
  ```
- Add the evaluation review record (and ADR if architecture/serving layout changed).
- Validate and deploy: `make check && make site-lint`, merge PR, then `git pull --ff-only && git submodule update --init && agentiac ops deploy`.

### Stage 5: Observation and Optional Thinning (`N-1`)

Keep `N-1` as `form: full` until all six confirmation gates are satisfied:
1. Matched quality (no new regression class).
2. Speedup or capacity gain (or accepted same-weight upgrade).
3. 2-hour soak and reboot/`reclaim` recovery verified.
4. 7 consecutive days on the route with `>= 8` hours of active inference load.
5. Zero `last_error` events, zero unexpected watch stops, zero failed project units.
6. Rollback to `N-1` tested live.

Once confirmed and space is needed (and free headroom covers restore + floor):

```sh
agentiac ops thin <host> --backend <N-1-backend> --check
agentiac ops thin <host> --backend <N-1-backend>
```

Verify the restore path check (`agentiac ops restore <host> --backend <N-1-backend> --check`).

### Stage 6: Two-Stage Retirement (`N-2` or Rejected Candidate)

1. Remove active route targets and inventory references (`strata_backend` / `strata_default_backend`) and mark `retirement: candidate` in `infra/retention.yml`; deploy and verify `agentiac ops drift`.
2. Run the reference-gated two-stage removal (see [Operations](operations.md#strata-preparation-upgrades-and-two-generation-retention)):
   ```sh
   agentiac ops retire <host> --backend <id> --check
   agentiac ops retire <host> --backend <id> --quarantine
   # wait 24 hours, then with explicit operator approval:
   agentiac ops retire <host> --backend <id> --purge
   ```
3. Retain the manifest with `status: retired` and the `artifacts/retired/<host>/` metadata archive as provenance; never reuse a retired root as a weight source.

---

## Part B: Coding-Agent Frontend Lifecycle

Applies to the npm-locked baseline agent (`pi`) and pinned binary/tree adapters (`claude-code`, `codex`) declared in `agents/agents.yml` and `infra/components.yml` (see [Agent frontend reference](agents.md)).

```text
Stage 0: Detect & Audit     (ops update check / plan; release notes & breaking changes)
   │
   ▼
Stage 1: Isolated Candidate (install into <agent>-candidate; lock/CLI/schema checks)
   │
   ▼
Stage 2: Isolated Tests &   (worktree symlink -> extension & e2e tests, 0 skipped;
         Container Smoke     sandboxed smoke.py 6/6 against live gateway)
   │
   ▼
Stage 3: Capability Probes  (agent probe; pre-registered Suite comparison if needed)
   │
   ▼
Stage 4: Atomic Pin & Swap  (commit lock + pin + compatibility.md; verify 0 active runs;
                             build from lock -> byte-match -> mv prev / mv new)
   │
   ▼
Stage 5: Verify & Retain    (agent list --capabilities, check, ops drift;
                             keep <prev> until confirmed, purge only by approval)
```

### Stage 0: Detect and Changelog Audit

```sh
agentiac ops update check
agentiac ops update plan pi
```

Audit upstream release notes across every intermediate patch/minor version for:
- Security advisories (e.g. HTTP client or dependency fixes).
- Breaking changes affecting custom extensions (`agents/pi/extensions/`).
- Changes to CLI flags (`--offline`, `--no-session`, `--no-tools`, `--no-extensions`, `--no-mcp`, `--no-skills`, `--no-context-files`, `--no-prompt-templates`, `--no-themes`, `--no-approve`, `--tools`, `--provider`, `--model`, `--mode`, `--print`), `models.schema.json`, `settings.schema.json`, or built-in tool behavior.

### Stage 1: Isolated Candidate Build and Static Compatibility

Never modify `artifacts/frontends/<agent>` during evaluation. Build into an isolated candidate directory (e.g. `artifacts/frontends/<agent>-<ver>-candidate`):
- **For `pi`:** copy/write `package.json` with the target version, run `npm install --package-lock-only --ignore-scripts` and `npm ci --ignore-scripts`, verify `node_modules/.bin/pi --version`.
- Diff lockfile dependencies, diff `--help` output against the baseline version, and validate `agents/pi/models.json` and daily `settings.json` against the candidate schemas.
- **For `claude-code` / `codex`:** stage the release in a separate directory and verify digest/pin handling via `scripts/pin-agent.py`.

### Stage 2: Isolated Extension Tests and Live Container Smoke

1. In a clean worktree, symlink `artifacts/frontends/<agent>` to the isolated candidate directory (do not commit the symlink).
2. Run extension and end-to-end tests (`test_router`, `test_loop_guard`, `test_compaction_guard`, `test_compaction_e2e`, `test_skills`, `test_pi_repository`) and verify **0 failures and 0 skipped**.
3. Run the sandboxed container repair smoke against a live target (`scripts/smoke.py --alias local-coder`) and verify `6/6` passes (`listed`, `chat`, `chat stream tool call`, `anthropic messages`, `responses`, `pi repair`).
4. Remove temporary worktree symlinks before running `make check`.

### Stage 3: Capability Probes and Controlled Suite Comparison

- Every frontend version change resets capability probes to `unverified`. Verify `local_alias_read_edit_bash`, `structured_noninteractive`, and `suite_grading` with `agentiac agent probe`.
- Pre-register Suite acceptance criteria before running (`0` infra failures, timeouts `<=` baseline, pass count `>=` baseline / no new regression class). Suite case IDs embed `spec['frontend']` (`<agent> <version>`), so versions never pool.

### Stage 4: Atomic Pin and Install Switch

1. Update lockfile (`agents/pi/package.json` + `package-lock.json` in `core`), component `pin` in `infra/components.yml`, and regenerate `docs/compatibility.md` (`agentiac model compatibility --write`) in lockstep so `make check` stays green.
2. Confirm zero active Suite containers and zero active daily sessions (`pgrep -x pi` + `/proc/<pid>/environ` `PI_CODING_AGENT_DIR`, plus `docker ps --filter label=io.agent-infra.owner=agent-infra`).
3. Rebuild from the committed lockfile into a staging directory in `/tmp` (`npm ci --ignore-scripts`), confirm byte-for-byte identity with the tested candidate (`diff -rq --exclude=.package-lock.json`), and swap atomically:
   ```sh
   mv artifacts/frontends/pi artifacts/frontends/pi-<prev>
   mv /tmp/pi-install artifacts/frontends/pi
   ```

### Stage 5: Post-Switch Verification, Rollback, and Cleanup

1. Verify live state:
   ```sh
   agentiac agent list --capabilities
   agentiac agent probe pi local_alias_read_edit_bash --alias local-coder
   agentiac check -m local-coder
   agentiac ops drift
   ```
2. **Rollback:** while `artifacts/frontends/pi-<prev>` is retained, swap the directories back (`mv pi pi-<rejected> && mv pi-<prev> pi`); otherwise revert the lock/pin commit and rebuild with `npm ci --ignore-scripts`.
3. **Cleanup:** delete `artifacts/frontends/pi-<prev>` only after confirmation and explicit operator approval.
