# AGENTIaC (formerly agent-infra)
Operator-controlled local coding-agent inference and evaluation on heterogeneous Ubuntu workers.
The control host runs the CLI, a private loopback gateway, agent frontends and evaluation history;
worker GPUs serve pinned models over SSH forwards. Dedicated workers are the default; shared workers
are claimed for a stated purpose and released without disturbing their owner's workloads.

Start with the [documentation index](docs/README.md), [daily use](docs/usage.md),
[operations](docs/operations.md), [architecture](docs/architecture.md),
the [development guide](docs/development.md), the [agent frontend reference](docs/agents.md),
the [Japanese quick guide](docs/manual-ja.md), and the [example site](examples/site/README.md).

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
See [development](docs/development.md) for tests, CI and publication checks.

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

## Create your own site

An installation keeps its machine declarations (`infra/inventory.yml`, `infra/components.yml`,
`infra/retention.yml`), model and route declarations (`gateway/config.yaml`, `gateway/local-coder.json`,
`backends/`, `profiles/`, `agents/`), and ignored runtime state (`secrets/`, `artifacts/`, `private/`)
in a separate **site repository** that pins `AGENTIaC` as a submodule at `core/`.

To bootstrap a new site from [`examples/site/`](examples/site/README.md):

```sh
mkdir ~/src/my-site && cd ~/src/my-site
git init -b main
git submodule add https://github.com/TadayukiOkada/AGENTIaC.git core
cp -R core/examples/site/{infra,gateway,backends,profiles,agents} .
mkdir -p bin
cat > bin/my-site <<'EOF'
#!/bin/sh
AGENTIAC_ROOT=$(dirname "$(dirname "$(readlink -f "$0")")")
AGENTIAC_COMMAND=$(basename "$0")
export AGENTIAC_ROOT AGENTIAC_COMMAND
exec "$AGENTIAC_ROOT/core/bin/agentiac" "$@"
EOF
chmod +x bin/my-site
```

Build the virtual environments inside `core/` (`core/.venv`, `core/.venv-gateway`, `core/.venv-lint`),
edit `infra/inventory.yml`, `gateway/config.yaml`, `gateway/local-coder.json`, `backends/` and `profiles/`
for your hosts, commit the site repository, and verify with `./bin/my-site ops deploy --check`.
