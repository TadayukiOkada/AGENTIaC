# Architecture decisions

This directory holds accepted architecture decisions for the **AGENTIaC** core (`0001`–`0009`).
Site-specific decisions—such as individual worker promotions, hardware-specific skill bridges,
and installation-level operational pins—live in each installation's site repository under
`docs/adr/`.

See the [Documentation index](../README.md) and [Documentation maintenance](../documentation-guide.md)
for how core and site documentation fit together. A historical decision does not authorize a
new disruptive operation; current pins are declared in the site repository.

- [ADR 0001: SSH, Ansible, systemd and Docker](0001-small-cluster-control.md)
- [ADR 0002: Capability-gated model routing](0002-evaluation-before-routing.md)
- [ADR 0003: Isolate Strata trials from default model serving](0003-isolated-strata-trials.md)
- [ADR 0004: On-demand model serving with idle unload](0004-on-demand-serving.md)
- [ADR 0005: One commercial alias through the user's ChatGPT subscription](0005-chatgpt-subscription-alias.md)
- [ADR 0006: One `agentiac` command instead of make targets](0006-agentiac-command.md)
- [ADR 0007: Declared state in git, runtime state in artifacts](0007-declared-and-runtime-state.md)
- [ADR 0008: Keep two generations, delete by ledger and approval](0008-two-generation-retention.md)
- [ADR 0009: Detect and propose automatically, apply by approval](0009-detect-and-propose-apply-by-approval.md)
