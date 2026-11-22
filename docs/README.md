# Documentation

Read this index before following an old plan. Current guidance, accepted decisions, proposed work and
historical measurements have different scopes; the newest timestamp alone does not establish authority.

## Start here

| Reader / question | Read |
|---|---|
| New developer: what is this project? | [Goals, requirements and terminology](project-overview.md), then [architecture](architecture.md) |
| Operator: how do I use it? | [Daily use](usage.md), [日本語ガイド](manual-ja.md) |
| Maintainer: what is deployed, and what is next? | [Recorded current status](progress.md), [roadmap](roadmap.md), [task and approval register](execution-plan.md) |
| Developer: how do I install and verify a change? | [Development guide](development.md), [evaluation guide](testing.md) |
| Dashboard prototype: local UI, minimal probes and recovery | [Dashboard guide](dashboard.md), [design gates](dashboard-design.md) |
| Operator: deploy, recover or change a worker? | [Operations](operations.md), [troubleshooting](troubleshooting.md), [TensorFold recipe](tensorfold-recipe.md) |
| Which model / agent is this name? | [Naming contract](model-naming.md), [generated bindings](model-names.md), [agents](agents.md) |
| Architect: why this design? | [ADR index](adr/README.md), [accepted platform design](platform-architecture.md), [orchestration design](orchestration-architecture.md) |
| Reviewer: where is the evidence? | [Results](results/README.md), [review reports](reviews/README.md), [audit](documentation-audit.md) |

## Authority map

| Category | Canonical scope / maintainer role |
|---|---|
| Goals and existing constraints | [Project baseline](project-overview.md); project owner accepts requirement changes |
| Accepted rationale and policy | [ADRs](adr/README.md); D1–D7 in [storage policy](storage-lifecycle-plan.md), U1–U8 in [platform design](platform-architecture.md#18-ユーザー判断), OA §15 choices in [orchestration design](orchestration-architecture.md#15-決定事項ユーザー判断) |
| Implementation architecture | [Architecture](architecture.md); component maintainers verify against code/tests |
| Operator procedures | [Operations](operations.md); daily entry points in [usage](usage.md); Japanese guide links to the same procedures |
| Declared machine/model/component facts | [Inventory reference](inventory.md) identifies the files; [compatibility](compatibility.md) and [model bindings](model-names.md) are generated |
| Saved observations, not live health | [Progress](progress.md); handoff is a short resume pointer, not a second status ledger |
| Active priorities | [Roadmap](roadmap.md); detailed legacy task IDs/dependencies and approval vocabulary in [execution register](execution-plan.md) |
| Proposals | [Dashboard investigation and design](dashboard-design.md), [improvement proposals](improvement-proposals.md) and review follow-ups; a proposal is not accepted because code resembles it |
| Historical evidence | [Results](results/README.md), [archive](archive/README.md), dated reviews and Git history; results retain their own measurement scope |

No document overrides explicit owner decisions or repository safety instructions. Source code proves what is
implemented, not that an unmet requirement has been waived. A declared pin is not proof of a running process.
See [maintenance rules](documentation-guide.md), [full inventory](documentation-inventory.md), and
[migration map](documentation-migration.md) for how this collection is maintained.
