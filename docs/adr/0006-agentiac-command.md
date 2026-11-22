# ADR 0006: One `agentiac` command instead of make targets

Status: accepted.

## Context

Exposing daily use, host lifecycle, operations, and evaluation as ~35 flat `make` targets
with `NODE=`, `ARGS=`, `REPO=`, and `ALIAS=` variables caused nested quoting issues, silently
ignored misspelled variables, risked default-host accidents, required running from the
repository root, and offered no per-command help or shell completion.

## Decision

Provide one CLI `bin/agentiac` (`src/agentiac/command.py`, `argparse`, stdlib), linked into
`~/.local/bin` by `make install` with bash completion generated from the parser, inventory,
and gateway/agent configuration. Each site repository wraps it with a small
`bin/<site-command>` script that sets `AGENTIAC_ROOT` and `AGENTIAC_COMMAND`
(see [Create your own site](../../README.md#create-your-own-site)).

- **Daily commands at the top level**: `code [REPO]` (defaults to the current directory;
  `-m ALIAS`, `-p PROMPT`, `--resume`, agent arguments after `--`), `worktrees` (`list`,
  `review`, `apply`, `drop`, `prune` — refusing uncommitted, unmerged, or divergent
  mutations), `models`, `model`, `status` (gateway, hosts, active route target, ChatGPT
  sign-in), `warm`, `unload`, `claim`, `release`, `reclaim`, `route [TARGET]`,
  `chatgpt login|status|logout`, `check`, and `doctor`.
- **Operations under `ops`**: `build`, `runtime`, `download`, `start`, `stop`, `deploy`
  (with `--check`, `--only`, `--allow-dirty`), `drift`, `preflight`, `prepare`, `storage`,
  `retire`, `update`, `backup`, `restore`, `netwatch`, `netwatch-report`, `backend-probe`,
  `logs`, `hosts`, `inventory`, `metrics`, `pipeline`, `dashboard`, and `eval-image`.
- **Evaluation under `eval`**: `suite`, `bench`, and `report`, passing evaluation arguments
  through cleanly.
- **Explicit host safety**: hosts are positional and required wherever an action mutates a
  host (no default host, enforced by both the CLI and Ansible playbooks). `release` accepts
  only borrowed hosts; `ops start` and `ops download` default to the host's declared
  `default_profile` and refuse a host without one.
- **Dispatch**: the CLI maps arguments and executes the existing scripts and playbooks with
  the pinned virtual environments (`.venv`, `.venv-gateway`, `.venv-dashboard`) while keeping
  relative path arguments predictable.
- **`Makefile`** retains development, test, lint, publication-scan, and install targets only.

## Consequences

Operator messages, completion, and `--json` contracts use the unified CLI while core logic
stays in focused modules and scripts rather than growing `command.py`. See
[ADR 0007](0007-declared-and-runtime-state.md) and
[ADR 0009](0009-detect-and-propose-apply-by-approval.md) for state boundaries and approval
rules.
