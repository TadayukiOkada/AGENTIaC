# Development only. Daily use, operations and evaluation are `agentiac` (ADR 0006,
# docs/usage.md); `make install` puts it on PATH with bash completion.
# The interpreter is the control venv the bootstrap creates (docs/operations.md) - the same one `bin/agentiac`
# runs and the ansible line below uses. The suite and the control code use APIs newer than a 2022 system
# python3 (contextlib.chdir, argparse accepting an empty `nargs='*'` under `choices`, fromisoformat reading
# Docker's nanosecond timestamps), so an older interpreter fails for that reason alone; the guard below says
# so instead of leaving four misleading failures. Override with PYTHON=... only on a 3.12 or newer.
PYTHON ?= .venv/bin/python
ANSIBLE = ANSIBLE_CONFIG=infra/ansible.cfg .venv/bin/ansible-playbook -i infra/inventory.yml
COMPLETIONS = $(HOME)/.local/share/bash-completion/completions
# infra/retention.yml is declared state, not a playbook (PA §7.1): the Makefile and tests/test_iac.py leave
# both it and infra/components.yml out, and src/agentiac/retention.py is what reads it.
PLAYBOOKS = $(filter-out infra/inventory.yml infra/components.yml infra/retention.yml,$(wildcard infra/*.yml))
.PHONY: check install lint site-lint split-check example-check publish-scan rename-check
check:
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else "agentiac check needs Python 3.12 or newer; this is %d.%d. The bootstrap makes .venv with it (docs/operations.md) and it is this Makefile default." % sys.version_info[:2])'
	@# The lock, the journal and the claim records go to a temporary directory, never this checkout's artifacts/ (D2.3).
	state=$$(mktemp -d) && { AGENTIAC_STATE_ROOT=$$state PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v; code=$$?; rm -rf "$$state"; exit $$code; }
	$(PYTHON) -c "import ast,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('scripts').glob('*.py')]"
	for playbook in $(PLAYBOOKS); do $(ANSIBLE) $$playbook --syntax-check || exit 1; done
	git diff --check
# Needs .venv-lint: uv venv --python 3.12 .venv-lint && uv pip install --python .venv-lint/bin/python -r requirements-lint.lock
lint:
	.venv-lint/bin/yamllint -c .yamllint.yml infra
	ANSIBLE_CONFIG=infra/ansible.cfg .venv-lint/bin/ansible-lint $(PLAYBOOKS)
	.venv-lint/bin/shellcheck scripts/*.sh
site-lint:
	.venv-lint/bin/yamllint -c .yamllint.yml profiles gateway agents evidence
# The suite with code and instance root physically apart, in the layout D6.1 gives a site checkout (core at core/).
# A failure that names a missing file is a code path reached through the wrong root (paths.locate, D7.1).
split-check:
	$(PYTHON) scripts/split-rehearsal.py --check
# The class A and B tests against the example site (D8.3): tests/example-site.txt lists them, with the interpreter a
# module needs when it is not $(PYTHON). This is what the core's CI runs once the core is published (M9).
example-check:
	@state=$$(mktemp -d) && site=$$(pwd)/examples/site && code=0 && \
	while read -r module python; do \
	  case "$$module" in ''|'#'*) continue;; esac; \
	  AGENTIAC_ROOT=$$site AGENTIAC_STATE_ROOT=$$state PYTHONPATH=src $${python:-$(PYTHON)} -m unittest discover -s tests -p "$$module.py" || code=1; \
	done < tests/example-site.txt; \
	rm -rf "$$state"; exit $$code
# Completion is generated on first <Tab>, so it follows the current commands and inventory.
# Is the core tree fit to publish (D6.7 item 2)? A split rehearsal, then on its core tree: the site-literal test against
# the site, the dated-line grep (only m9-dated-lines.md's data lines may remain) and gitleaks. Needs the pinned gitleaks:
# scripts/gitleaks-install.sh [PATH] (default .venv-lint/bin/gitleaks; `make publish-scan GITLEAKS=PATH` to use another).
GITLEAKS ?= .venv-lint/bin/gitleaks
REHEARSAL ?= $(if $(wildcard ../infra/inventory.yml),$(abspath ../..))
publish-scan:
	@test -x $(GITLEAKS) || { echo "gitleaks is not installed at $(GITLEAKS): run scripts/gitleaks-install.sh $(GITLEAKS)" >&2; exit 2; }
	$(PYTHON) scripts/publish-scan.py --gitleaks $(GITLEAKS) $(if $(REHEARSAL),--rehearsal $(REHEARSAL))
# Rehearse the M9 extraction rename (D6.8, T9.9): build a split rehearsal, run rename-core.py on its core tree, check
# that only the T9.1 section 6 survivors remain, run example-check against examples/site and the full suite against the
# rehearsal's site with the D6.1 wrapper written into the rehearsal only.
rename-check:
	$(PYTHON) scripts/rename-core.py --check
install:
	mkdir -p $(HOME)/.local/bin $(COMPLETIONS)
	ln -sfn $(CURDIR)/bin/agentiac $(HOME)/.local/bin/agentiac
	echo '. <("$(HOME)/.local/bin/agentiac" completion)' > $(COMPLETIONS)/agentiac

# Former targets: say where they went instead of failing with "No rule".
moved.agent = agentiac code [REPO] [-m ALIAS] [-- PI_ARGS]
moved.smoke = agentiac check [-m ALIAS] [--no-pi]
moved.route = agentiac route [TARGET]
moved.claim = agentiac claim HOST [--route] [--idle MIN] [--no-warm]
moved.warm = agentiac warm [HOST]
moved.unload = agentiac unload HOST
moved.release = agentiac release HOST
moved.ondemand-status = agentiac status
moved.health = agentiac status
moved.status = agentiac ops hosts
moved.chatgpt-login = agentiac chatgpt login|status|logout
moved.suite = agentiac eval suite ...
moved.inventory = agentiac ops inventory
moved.build = agentiac ops build HOST
moved.runtime = agentiac ops runtime HOST
moved.deploy = agentiac ops deploy
moved.download = agentiac ops download HOST [--model PROFILE]
moved.start = agentiac ops start HOST [--model PROFILE]
moved.stop = agentiac ops stop HOST
moved.logs = agentiac ops logs [--chatgpt]
moved.netwatch = agentiac ops netwatch HOST...
moved.netwatch-report = agentiac ops netwatch-report HOST
moved.metrics = agentiac ops metrics HOST [...]
moved.pipeline = agentiac ops pipeline HOST
moved.backend-probe = agentiac ops backend-probe HOST BACKEND
moved.benchmark = agentiac eval bench run --model ALIAS --profile profiles/PROFILE.json [...]
moved.benchmark-resume = agentiac eval bench run (same options as the first run)
moved.benchmark-status = agentiac eval bench status
moved.benchmark-export = agentiac eval bench export
moved.benchmark-report = agentiac eval report DB
moved.update-models = add a reviewed revision/SHA256 profile, then agentiac ops download HOST --model PROFILE
MOVED = $(patsubst moved.%,%,$(filter moved.%,$(.VARIABLES)))
.PHONY: $(MOVED)
$(MOVED):
	@echo 'make $@ is now: $(moved.$@)  (agentiac --help; make install puts it on PATH)' >&2; exit 2
