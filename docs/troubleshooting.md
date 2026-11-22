# Troubleshooting

## Missing GPU or container runtime mismatch

Before changing Docker configuration on a worker, inspect host `nvidia-smi`, device nodes
(such as `/dev/nvhost-gpu` on Jetson), and `sudo journalctl -k -b` for early driver or
firmware errors. Preserve logs and inspect running owner workloads before coordinating a
clean reboot. Never upgrade JetPack or host NVIDIA drivers to troubleshoot.

Jetson CSV-mode workers require `--runtime=nvidia` with `NVIDIA_VISIBLE_DEVICES` and
`NVIDIA_DRIVER_CAPABILITIES`; `--gpus all` alone may fail or invoke an unsupported hook.
Standard CUDA or GB10 workers use `--gpus all` where no `nvidia` runtime is registered. Do
not change global Docker defaults to force heterogeneous workers onto one runtime setting.

Agent sandboxes can hide the local GPU device on the control host; verify outside the
sandbox before diagnosing a driver failure.

## Jetson CUDA shared-library paths

On JetPack 7.x SBSA layouts, dynamic CUDA runtime libraries live under
`targets/sbsa-linux/lib` rather than `aarch64-linux/lib`. Verify with `ldd` on
`libggml-cuda.so`: a statically linked CUDA probe can pass while a dynamically linked
backend fails to locate `libcudart`. Keep `cuda_lib_dir` in `infra/inventory.yml` aligned
with the measured host layout.

## Worker Wi-Fi drops and netwatch

Inspect evidence before rebooting an unreachable wireless worker:

```sh
agentiac ops netwatch <host...>         # install persistent netwatch unit and Wi-Fi power-save config
agentiac ops netwatch-report <host>     # summarize outages, diagnostics, and reconnect attempts
```

The watch (`<prefix>-netwatch`, `/var/log/<prefix>/netwatch.jsonl`) records link state,
`wpa_supplicant` / NetworkManager diagnostics, and recovery actions (`nmcli device connect`
and radio cycling). Persistent `rsyslog` / `journald` records retain outage context across
reboots. If repeated reconnect attempts fail, inspect the access point or router before
rebooting workers.

## Unified-memory kernel `NV_ERR_NO_MEMORY` warnings

On unified-memory GPUs, kernel log lines such as `NVRM: ... [NV_ERR_NO_MEMORY]` during
model loading often track scarce non-reclaimable `MemFree` pages while `MemAvailable` page
cache remains plentiful. Distinguish benign load-time driver warnings from a true host OOM
(`Out of memory: Killed process`):

```sh
sudo -n journalctl -k | grep NV_ERR_NO_MEMORY
journalctl -u <prefix>-ondemand -o short-iso | grep -E 'loading|ready|unloaded'
```

On borrowed hosts, avoid changing kernel `vm.*` knobs or worker memory floors without
explicit owner approval.

## Backend container cannot find `libgomp` or `libstdc++`

Plain Ubuntu containers lack `libgomp`/`libstdc++` runtime libraries. Run
`agentiac ops runtime <host>` to build the project runtime image (mounting the host CUDA
toolkit read-only and injecting GPU libraries through NVIDIA container integration). Record
image identity and compiler/library metadata.

## Build or download unfinished

Inspect worker status and journals with `agentiac ops hosts` and the worker's
`<prefix>-build` / `<prefix>-download-<profile>` systemd units. Active jobs are skipped
safely on repeat invocations; CMake builds resume in revision-specific directories, and
downloads keep `.part` files and verify SHA256 before moving into place. Never delete an
existing verified weight on checksum mismatch—inspect it first.

When an upstream model repository provides only an SPDX license identifier in metadata, the
downloader preserves that declaration alongside a checksum-pinned SPDX reference without
misrepresenting it as a publisher-supplied `LICENSE` file. Unknown or custom licenses require
separate review.

## API, tool-call, and journal diagnostics

1. Check worker health through the local SSH tunnel first, then gateway authentication and
   model alias routing. Keep prompt logging disabled on workers (`--log-disable`).
2. Tool text emitted inside `content` with empty `tool_calls` indicates a parser or chat
   template mismatch, not a passing tool call; test the active parser directly.
3. `journalctl --since` parses naive timestamps in the host's local timezone even when
   `--utc` is passed (`--utc` formats output only). Always include explicit `UTC` in query
   bounds:

```sh
journalctl -u <prefix>-gateway --since '<timestamp> UTC' --until '<timestamp> UTC' --no-pager
```

4. Before assuming an agent loop resent a user prompt, inspect the saved session `.jsonl`
   transcript and gateway `inference` journal counts.

## Benchmark interruption

Re-run the exact benchmark command. A controller file lock prevents concurrent runners on
the same SQLite database, marks abandoned attempts as interrupted while preserving artifacts,
skips completed cases, and retries failures only with `--retry-failed`. Configuration or
source changes produce new identities; never merge incomparable scores or export private
traces outside control.
