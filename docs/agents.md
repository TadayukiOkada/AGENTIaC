# Agent frontend reference

Status: current frontend contract. `agentiac code` owns interactive pi
worktrees and sessions; this page owns Suite adapter selection, pin validation and capability reporting.
See [development](development.md) for control environment setup, test execution and component pin checks, and
[Lifecycle](lifecycle.md#part-b-coding-agent-frontend-lifecycle) for frontend evaluation and upgrade gates.
Earlier frontend research and pilots in a site archive retain their original conditions;
their success is not a current support matrix or a fair frontend speed ranking.

## Agents in the suite

The suite runs one Agent per run. `--agent` chooses it; without the flag the run is
the baseline (pi), whose command and case ids are unchanged from before adapters
existed.

```sh
agentiac agent list --capabilities      # what is pinned, and what was actually probed
agentiac agent capabilities claude-code
agentiac agent probe codex apply_patch --alias local-coder
scripts/pin-agent.py claude-code --check   # the same comparison a run makes
```

An Agent runs pinned bytes under `artifacts/frontends/`, never a personal CLI that
updates itself (U4): Claude Code is pinned by sha256, Codex by a digest over its whole
release tree, pi by its install lockfile. Replacing either means re-running
`scripts/pin-agent.py` and committing the new digest; an Agent whose bytes do not match
its pin refuses to start. Every Agent talks to the local gateway or not at all: a base
URL that is not a literal loopback address (or a name that merely starts like one) is
refused. The container sandbox — image, memory, CPUs, pids, read-only root, mounts of
`/work` — is identical for every Agent, because a comparison where one Agent had more
resources is not a comparison. Results of two Agents are never pooled: a second Agent
names itself in the spec, so its cases have different ids.

Capabilities come from a probe, not from a claim: `agent list` shows the value with the
Agent version and the time it was observed, and a value recorded at another version is
`unverified` again. `emulated` means the Agent did the thing without a tool for it —
Codex has no read tool, a read is a command.


## Sources and capability status

`agents/agents.yml` declares adapters and their pins; `agents/pi/` contains daily provider/router configuration.
`src/agentiac/adapters.py` implements the Suite boundary. Inspect `agentiac agent --help` for available
list/show/probe commands rather than copying an old pilot command. A capability at a different frontend
version is unverified; a source declaration alone is not a successful live probe. Personal subscription
sign-ins are separate from the gateway-only Suite adapters.
