"""Which tracked files belong to the publishable core and which to this site (M6, T6.1).

docs/implementation-plan/site-separation.md D6.1 splits this repository into a public core and a private site
repository that pins it; D6.2 gives code and the instance root (declarations and state) separate roots. Both need
one answer per path: `classify` is it. `review` marks the paths the owner has not decided yet. The lists are the
planner's proposal and the owner reviews them; a tracked path that none of them covers fails tests/test_layout.py.
Standard library only.
"""

STATE_PREFIXES = ('secrets/', 'artifacts/', 'private/')
SITE_FILES = frozenset({
    'infra/inventory.yml', 'infra/components.yml', 'infra/retention.yml',
    'gateway/config.yaml', 'gateway/chatgpt.yaml', 'gateway/local-coder.json',
    'agents/pi/models.json', 'agents/pi/router.json', 'agents/agents.yml', 'AGENTS.md', 'agents/jetson-skills.json',
    'docs/adr/README.md'})
# benchmarks/: this site's suite, graders and result records; its generic tools are CORE_FILES (D6.7 item 3, revised)
SITE_PREFIXES = ('backends/', 'evidence/', 'scripts/archive/',  # the archive: provenance of this site's results (D8.1)
                 'benchmarks/', 'agents/skills/',
                 # ADRs that record this site's hosts and history, by number: some of their file names name a host.
                 # M10 ports them back as rewrites (M9 review, decision 5); the index goes with them.
                 *(f'docs/adr/{number:04d}-' for number in (3, 4, 6, 7, 8, 10, 11, 12)))
CORE_FILES = frozenset({
    'Makefile', 'pyproject.toml', 'LICENSE', 'NOTICE', '.gitleaks.toml', 'README.md', '.gitignore', '.ansible-lint', '.yamllint.yml',
    'profiles/schema.json', 'backends/schema.json', 'agents/pi/package.json', 'agents/pi/package-lock.json',
    'benchmarks/Dockerfile', 'benchmarks/context-probe.py', 'benchmarks/bfcl-rescore.py',
    'benchmarks/repository-task.example.json'})
CORE_PREFIXES = ('src/', 'scripts/', 'tests/', 'bin/', 'runtime/', 'dashboard/', '.github/',
                 'infra/', 'gateway/', 'agents/pi/extensions/', 'docs/adr/', 'examples/')   # examples/site: D8.3
# Documents that name no site. The others (operations, usage, architecture, ...) stay with this site until M10
# rewrites them with synthetic names (M9 review, decision 5).
CORE_DOCS = frozenset({'README.md', 'development.md', 'agents.md', 'documentation-guide.md'})
# Class C tests (D6.6): every test module that does not run against examples/site (tests/example-site.txt), and the
# fixtures and harnesses only they use. They use this site as their fixture, so they stay with it until M10 ports them;
# tests/test_layout.py keeps this list the exact complement of tests/example-site.txt.
SITE_TEST_MODULES = frozenset({
    'test_adapters', 'test_archived_task_keys', 'test_backend_names', 'test_bfcl_target',
    'test_catalog', 'test_claude_repository', 'test_codex_frontend',
    'test_codex_repository', 'test_components',
    'test_dashboard_release_layout', 'test_declared',
    'test_eval_image', 'test_evidence',
    'test_glm_monitor',
    'test_health_scope', 'test_iac', 'test_identities',
    'test_local_coder', 'test_model_names',
    'test_pi_repository',
    'test_retention', 'test_restore_document',
    'test_short_aliases', 'test_skills', 'test_storage',
    'test_strata_context', 'test_strata_prepare', 'test_strata_scope', 'test_strata_start',
    'test_tool_loop_audit', 'test_tool_loop_reduction', 'test_updates', 'test_upgrade'})
SITE_TESTS = frozenset({f'tests/{name}.py' for name in SITE_TEST_MODULES} | {
    'tests/records.py', 'tests/golden/pi-invocation.json', 'tests/skills_harness.mjs', 'dashboard/tests/smoke.mjs'})
REVIEW_PREFIXES = ()                 # M9 settled the last ones (agents/skills/, agents/jetson-skills.json): site
REVIEW_FILES = frozenset()


def classify(path):
    """'core', 'site' or 'review' for a repository-relative POSIX path."""
    if path in CORE_FILES and path.startswith(SITE_PREFIXES):
        return 'core'                       # a schema or tool inside a site directory (backends/schema.json)
    if path in SITE_FILES or path in SITE_TESTS or path.startswith(STATE_PREFIXES + SITE_PREFIXES):
        return 'site'
    if path in REVIEW_FILES or path.startswith(REVIEW_PREFIXES):
        return 'review'
    parts = path.split('/')
    if parts[0] == 'agents' and parts[-1] == 'pin.json':
        return 'site'
    if parts[0] == 'profiles':
        return 'core' if path in CORE_FILES else 'site'
    if parts[0] == 'docs' and not path.startswith('docs/adr/'):
        return 'core' if len(parts) == 2 and parts[1] in CORE_DOCS else 'site'
    if path in CORE_FILES or path.startswith(CORE_PREFIXES) or path.startswith('requirements-'):
        return 'core'
    return 'review'


def side(path):
    """Which tree holds PATH once core and site are apart: 'site', or 'core' for code and for undecided paths.

    `review` paths go with the core until the owner decides them (M9), as the split rehearsal copies them.
    """
    return 'site' if classify(path) == 'site' else 'core'
