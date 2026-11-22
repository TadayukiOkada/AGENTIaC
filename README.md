# AGENTIaC (formerly agent-infra)
Operator-controlled local coding-agent inference and evaluation on heterogeneous Ubuntu workers.
The control host runs the CLI, a private loopback gateway, agent frontends and evaluation history;
worker GPUs serve pinned models over SSH forwards. Dedicated Orins are the default; shared workers
are claimed for a stated purpose and released without disturbing their owner's workloads.

Start with the [documentation index](docs/README.md), [project baseline](docs/project-overview.md),
[daily guide](docs/usage.md) or [日本語ガイド](docs/manual-ja.md).
[Recorded status](docs/progress.md) and the [active roadmap](docs/roadmap.md) distinguish shipped work,
validation, proposals and owner decisions. Small selected task results do not establish general coding quality.

## Control environment

From `~/src/AGENTIaC`, with uv available:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-control.lock
uv venv --python 3.12 .venv-gateway
uv pip install --python .venv-gateway/bin/python -r requirements-gateway.lock
uv venv --python 3.12 .venv-lint
uv pip install --python .venv-lint/bin/python -r requirements-lint.lock
make check PYTHON=.venv/bin/python
make lint
make install
```

This installs control dependencies and the CLI; it does not provision or start workers.
See [development](docs/development.md) for tests/CI and [operations](docs/operations.md) for
reviewed deployment, build, download, start and recovery procedures.

```sh
agentiac --help
agentiac model names
agentiac status
agentiac code ~/repo
agentiac code --resume
agentiac worktrees review --diff
agentiac worktrees apply
```

Daily code runs in a worktree by default. Review and test its committed changes before applying.
Inference APIs stay private. Credentials, weights and detailed prompt/response traces never enter Git.
