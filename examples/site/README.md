# The example site

The smallest complete, valid site (docs/implementation-plan/site-separation.md D8.3). The core's CI runs its class A
and B tests against it (`make example-check`, `tests/example-site.txt`), and a new site can start from a copy of it.

Everything here is synthetic. The hosts are `control` (the local one), `example-alpha` (a dedicated `orin`) and
`example-beta` (a borrowed `rtx3090`). The account is `example-operator`, the addresses are under `.example` (RFC 2606)
and the paths are under `/srv/agentiac`. The model, engine and revisions do not exist, and nothing here is ever
downloaded or served. No real site's names may appear in these files, and these names appear nowhere in the core
(`tests/test_example_site.py`).

| File | What it declares |
|---|---|
| `infra/inventory.yml` | the hosts, and the site-wide settings in `all.vars` (D8.2) |
| `infra/components.yml` | the engine pin a dedicated host's `strata_backend` needs |
| `backends/<name>.json` | one Strata manifest, named by the backend naming rule |
| `profiles/tiny-coder-q4.json` | one llama.cpp profile |
| `gateway/config.yaml` | a worker alias per host and one subscription alias |
| `gateway/local-coder.json` | one route target per host, named node-first |
| `agents/pi/models.json`, `agents/pi/router.json` | what the declared graph reads for the pi front end |

There is no `secrets/`, `artifacts/`, retention ledger or evidence: the loaders accept a site without them.
