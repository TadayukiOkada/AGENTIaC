import { Fragment, useEffect, useState } from 'react'
import { api, pages } from './api'
import { ApiError } from './api'
import type {
  Session, Hardware, NodeRow, ModelRow, AgentRow, ConfigurationResource,
  Overview, Job, JobAccepted, JobList, Events, LogChunk, ConfigReport,
  NodeList, ModelList, AgentList, Snapshot, ConfigurationResources, EngineRow, EngineList, Deployments,
} from './types'
import { Badge, Fields, Sections, Toggle, Value, age, gib, join, opt, rec, short, statusTone, text, tokens } from './view'
import { Related, anchor, link, parseHash, targetName } from './related'
import { Engines } from './Engines'
import type { LifecycleIssueDTO } from './generated/api'

type View = 'overview' | 'nodes' | 'models' | 'engines' | 'agents' | 'deployments' | 'jobs' | 'monitoring' | 'configuration'

const NAV: Array<{ id: View; label: string }> = [
  { id: 'overview', label: 'Overview' },
  { id: 'nodes', label: 'Nodes' },
  { id: 'models', label: 'Models' },
  { id: 'engines', label: 'Engines' },
  { id: 'agents', label: 'Agents' },
  { id: 'deployments', label: 'Deployments' },
  { id: 'jobs', label: 'Jobs' },
  { id: 'monitoring', label: 'Monitoring' },
  { id: 'configuration', label: 'Configuration' },
]

const ROW_KEY: Record<string, string> = {
  nodes: 'node', models: 'model', engines: 'engine', agents: 'agent', configuration: 'config', deployments: 'deploy',
}

function routeView(hash: string): View {
  const { view } = parseHash(hash)
  return NAV.some(item => item.id === view) ? view as View : 'overview'
}

type DashboardData = {
  overview: Overview
  nodes: NodeRow[]
  models: ModelRow[]
  engines: EngineRow[]
  engineIssues: LifecycleIssueDTO[]
  agents: AgentRow[]
  config: ConfigReport
  jobs: JobList
  deployments: Deployments | null
  // When each loaded list was read from the declarations (its first page's snapshot).
  snapshots: Record<string, Snapshot>
}

function matches(row: unknown, query: string): boolean {
  if (!query) return true
  return JSON.stringify(row).toLowerCase().includes(query.toLowerCase())
}

function isMissingSession(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}

const JOB_PAGE = 100

async function consistentEnginePages(): Promise<EngineList[]> {
  for (let attempt = 0; attempt < 2; attempt++) {
    const result = await pages<EngineList>('/engines?limit=200')
    const generations = new Set(result.map(page => page.snapshot?.generated_at ?? null))
    if (generations.size <= 1) return result
  }
  throw new ApiError(0, 'snapshot_changed', 'Engine declarations changed while loading; please refresh', true)
}

async function loadData(view: View): Promise<DashboardData> {
  const shows = (...views: View[]) => views.includes(view)
  // Independent reads run concurrently; the slowest one bounds the load time.
  const snapshots: Record<string, Snapshot> = {}
  const note = <P extends { snapshot?: Snapshot | null }>(name: string, list: P[]): P[] => {
    if (list[0]?.snapshot) snapshots[name] = list[0].snapshot
    return list
  }
  const [overview, nodes, models, enginePages, agents, config, jobs, deployments] = await Promise.all([
    api<Overview>('/overview'),
    shows('nodes', 'overview', 'monitoring')
      ? pages<NodeList>('/nodes?limit=200').then(r => note('nodes', r).flatMap(p => p.nodes)) : [],
    shows('models', 'overview')
      ? pages<ModelList>('/models?limit=200').then(r => note('models', r).flatMap(p => p.targets)) : [],
    shows('engines', 'overview')
      ? consistentEnginePages().then(r => note('engines', r)) : [],
    shows('agents', 'overview', 'monitoring')
      ? pages<AgentList>('/agents?limit=200').then(r => note('agents', r).flatMap(p => p.agents)) : [],
    shows('configuration', 'overview')
      ? pages<ConfigurationResources>('/configuration/resources?limit=200').then(r => {
        note('configuration', r)
        return { resources: r.flatMap(p => p.resources), route: r[0].route, references: r[0].references,
          target_names: r[0].target_names }
      })
      : { resources: [], route: {}, references: [], target_names: {} },
    // Newest first, so recent and failed jobs stay visible however long the ledger grows.
    shows('jobs', 'overview', 'monitoring') ? api<JobList>(`/jobs?limit=${JOB_PAGE}&order=desc`) : null,
    shows('deployments') ? api<Deployments>('/deployments') : null,
  ])
  return { overview, nodes, models, engines: enginePages.flatMap(p => p.engines),
    engineIssues: enginePages[0]?.lifecycle_issues ?? [], agents, config, snapshots, deployments,
    jobs: jobs ?? { schema_version: 1, as_of: overview.as_of, next_cursor: null, jobs: [] } }
}

async function loadJob(jobId: string): Promise<{ job: Job; logs: LogChunk; events: Events }> {
  const job = await api<Job>(`/jobs/${jobId}`)
  const logs = await api<LogChunk>(`/jobs/${jobId}/logs?limit=100`)
  const events = await api<Events>(`/jobs/${jobId}/events?limit=100`)
  return { job, logs, events }
}

async function issueSession(password: string): Promise<Session> {
  return api<Session>('/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
}

async function acceptProbe(session: Session, target: string, key: string): Promise<JobAccepted> {
  return api<JobAccepted>('/jobs', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': session.csrf_token,
    },
    body: JSON.stringify({
      idempotency_key: key,
      operation: 'probe-node',
      target,
      args: { probe: true },
      timeout_s: 15,
    }),
  })
}

async function mutateJob(session: Session, path: string): Promise<Record<string, unknown>> {
  return api<Record<string, unknown>>(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': session.csrf_token,
    },
    body: JSON.stringify({ confirm: true }),
  })
}


export function Dashboard() {
  const [view, setView] = useState<View>(() => routeView(window.location.hash))
  const [focus, setFocus] = useState<string | null>(null)
  const [refresh, setRefresh] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  const [session, setSession] = useState<Session | null>(null)
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [theme, setTheme] = useState<'light' | 'dark'>('light')
  const [jobId, setJobId] = useState<string | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [logs, setLogs] = useState<LogChunk | null>(null)
  const [events, setEvents] = useState<Events | null>(null)
  const [accepted, setAccepted] = useState<JobAccepted | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const toggle = (key: string) => setExpanded(previous => {
    const next = new Set(previous)
    if (next.has(key)) next.delete(key); else next.add(key)
    return next
  })

  useEffect(() => {
    let active = true
    api<Session>('/session').then(value => { if (active) setSession(value) }).catch(err => {
      if (active && !isMissingSession(err)) setError(err.message)
    })
    return () => { active = false }
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
  }, [theme])

  // The URL hash addresses a view and optionally one resource (#/nodes/<node>, #/jobs/<id>), so a
  // detail can be linked, reloaded and reached with the browser's back button.
  useEffect(() => {
    const follow = () => {
      const { view: target, id } = parseHash(window.location.hash)
      const next = routeView(window.location.hash)
      setView(next)
      if (!id || next !== target) return
      if (next !== 'engines') setQuery('')
      if (next === 'jobs') { setJobId(id); return }
      const prefix = ROW_KEY[next]
      if (!prefix) return
      setExpanded(previous => new Set(previous).add(`${prefix}:${id}`))
      setFocus(anchor(next, id))
    }
    follow()
    window.addEventListener('hashchange', follow)
    return () => window.removeEventListener('hashchange', follow)
  }, [])

  useEffect(() => {
    if (!data || !focus) return
    document.getElementById(focus)?.scrollIntoView({ block: 'center' })
    setFocus(null)
  }, [data, focus])

  // A view change clears the previous view's rows; a refresh keeps them (and any form input)
  // on screen until the new data arrives, and keeps them if the reload fails.
  useEffect(() => { setData(null) }, [session, view])

  useEffect(() => {
    if (!session) return
    let active = true
    setRefreshing(true)
    loadData(view).then(loaded => {
      if (active) { setData(loaded); setError(null); setRefreshing(false) }
    }).catch(err => {
      if (!active) return
      setRefreshing(false)
      if (isMissingSession(err)) setSession(null)
      setError(err.message)
    })
    return () => { active = false }
  }, [session, view, refresh])

  useEffect(() => {
    setJob(null); setLogs(null); setEvents(null)
    if (!session || !jobId) return
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    let sawActive = false
    const poll = async () => {
      if (!active) return
      if (document.hidden) { timer = setTimeout(poll, 2000); return }
      try {
        const loaded = await loadJob(jobId)
        if (!active) return
        setJob(loaded.job); setLogs(loaded.logs); setEvents(loaded.events)
        if (['queued', 'running', 'cancel_requested'].includes(loaded.job.status)) {
          sawActive = true
          timer = setTimeout(poll, 1000)
        } else if (sawActive) {
          // The job finished while selected: reload the list once so its row and snapshots match.
          setRefresh(value => value + 1)
        }
      } catch (err) {
        if (!active) return
        if (isMissingSession(err)) setSession(null)
        setError(err instanceof Error ? err.message : String(err))
        // Retry GETs only, at a bounded rate; POST acceptance/retry is always explicit.
        if (!isMissingSession(err)) timer = setTimeout(poll, 2000)
      }
    }
    void poll()
    return () => { active = false; clearTimeout(timer) }
  }, [session, jobId, refresh])

  const nav = NAV.map((item) => (
    <button
      key={item.id}
      type="button"
      className={item.id === view ? 'active' : ''}
      onClick={() => { window.location.hash = `#/${item.id}`; setView(item.id) }}
    >
      {item.label}
    </button>
  ))

  const current = data
  const filteredNodes = current ? current.nodes.filter((row) => matches(row, query)) : []
  const filteredModels = current ? current.models.filter((row) => matches(row, query)) : []
  const filteredAgents = current ? current.agents.filter((row) => matches(row, query)) : []
  const filteredConfig = current ? current.config.resources.filter((row) => matches(row, query)) : []
  const filteredJobs = current ? current.jobs.jobs.filter((row) => matches(row, query)) : []

  return (
    <div className="dashboard">
      <header className="topbar">
        <div className="brand">agentiac management</div>
        <div className="meta">
          <span>{current ? text(current.overview.as_of) : 'not loaded'}</span>
          <span>connection: {session ? 'authenticated' : 'login required'}</span>
        </div>
        <div className="controls">
          <button type="button" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>
            theme: {theme}
          </button>
          {view !== 'engines' && <input name="q" aria-label="search" value={query} placeholder="search" onChange={(event) => setQuery(event.currentTarget.value)} />}
          {view !== 'engines' && <button type="button" onClick={() => setQuery('')}>clear search</button>}
        </div>
      </header>

      <nav className="nav">
        {nav}
        <button type="button" onClick={() => setRefresh(value => value + 1)}>refresh</button>
      </nav>

      {session && <button type="button" onClick={() => {
        api('/session', { method: 'DELETE', headers: { 'X-CSRF-Token': session.csrf_token } })
          .then(() => { setSession(null); setData(null); setJobId(null); setNotice(null) })
          .catch(err => { if (isMissingSession(err)) setSession(null); setError(err.message) })
      }}>logout</button>}
      {error && <div role="alert" className="error">{error}</div>}
      {session && !data && <p role="status">loading…</p>}
      {!session && (
        <section className="login">
          <h1>login</h1>
          <p>Localhost-only session. Password is sent once to the server; it is not placed in the URL or storage.</p>
          <form onSubmit={(event) => {
            event.preventDefault()
            const input = event.currentTarget.querySelector('input[name="password"]') as HTMLInputElement | null
            const password = input ? input.value : ''
            if (input) input.value = ''
            if (!password) {
              setError('password is required')
              return
            }
            issueSession(password).then((issued) => {
              setSession(issued)
              setJobId(null)
              setError(null)
              setNotice('authenticated')
              setView('overview')
            }).catch((err) => {
              setError(err.message)
            })
          }}>
            <label>
              password
              <input name="password" type="password" autoComplete="off" />
            </label>
            <button type="submit">login</button>
          </form>
        </section>
      )}

      {session && current && (
        <section className="content">
          {view === 'monitoring' &&
            <p role="status">This view currently shows related saved records. Dedicated APIs and live monitoring are pending.</p>}
          <div className="status">
            {refreshing && <div role="status">Updating…</div>}
            <div>snapshot: {text(current.overview.as_of)}</div>
            {view === 'engines' ? <details><summary>Other snapshot freshness</summary><div>{join(Object.entries(current.overview.freshness).map(([name, value]) => `${name} ${value}`))}</div></details>
              : <div>freshness: {join(Object.entries(current.overview.freshness).map(([name, value]) => `${name} ${value}`))}</div>}
            {Object.keys(current.snapshots).length > 0 && (
              <div>declarations read: {join(Object.entries(current.snapshots).map(([name, snap]) =>
                `${name} ${age(snap.age_s)}${snap.stale ? ` (stale: rebuild failed, ${text(snap.error)})` : ''}`))}</div>
            )}
            {/* Counts only for the lists this view loaded; an unloaded list is not "0". */}
            <div>{join([
              ...Object.keys(current.snapshots).map(name => `${name}: ${({
                nodes: current.nodes.length, models: current.models.length, engines: current.engines.length,
                agents: current.agents.length, configuration: current.config.resources.length,
              } as Record<string, number>)[name] ?? 'unknown'}`),
              ['jobs', 'overview', 'monitoring'].includes(view)
                && `jobs: ${current.jobs.next_cursor ? `latest ${current.jobs.jobs.length}` : current.jobs.jobs.length}`,
            ], ' / ')}</div>
          </div>

          {view === 'overview' || view === 'monitoring' || view === 'nodes' ? (
            <section className="panel">
              <h2>Nodes</h2>
              <div className="table">
                <table>
                  <thead>
                    <tr>
                      <th>id</th><th>host</th><th>class</th><th>backend / profile</th>
                      <th>hardware</th><th>minimal probe</th><th>hw_fp</th><th>freshness</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredNodes.map((row) => {
                      const key = `node:${row.id}`
                      const hw = row.hardware
                      const probe = row.probe
                      const os = probe?.data
                      return (
                        <Fragment key={row.id}>
                          <tr id={anchor('nodes', row.id)}>
                            <td className="id">
                              <a className="mono" href={link('nodes', row.id)}>{row.id}</a>
                              {row.borrowed ? <Badge tone="warn" title="borrowed worker">borrowed</Badge> : null}
                              <Toggle open={expanded.has(key)} onClick={() => toggle(key)} />
                            </td>
                            <td className="nowrap">
                              <span className="mono">{join([row.user, row.host], '@')}</span>
                              <div className="sub">worker port {text(row.port)}</div>
                            </td>
                            <td className="nowrap">{text(row.node_class)}</td>
                            <td className="mono">
                              {opt(row.strata_default_backend) ?? <span className="muted">no Strata default</span>}
                              {row.default_profile ? <div className="sub">profile {row.default_profile}</div> : null}
                            </td>
                            <td>
                              {hw ? join([
                                hw.gpu && (hw.gpu_count && hw.gpu_count > 1 ? `${hw.gpu_count}× ${hw.gpu}` : hw.gpu),
                                typeof hw.memory_total_gib === 'number' ? `${hw.memory_total_gib} GiB` : null,
                                hw.driver && `driver ${hw.driver}`,
                              ]) || 'unknown' : 'unknown'}
                              {hw?.collected_at ? <div className="sub">collected {age(hw.age_s) ?? hw.collected_at}</div> : null}
                            </td>
                            <td>
                              {probe && os ? (
                                <>
                                  {join([os.system, os.release, os.machine])}
                                  <div className="sub">
                                    {join([
                                      typeof os.cpu_count === 'number' ? `${os.cpu_count} CPU` : null,
                                      gib(os.memory_total_bytes) && `${gib(os.memory_total_bytes)} total`,
                                      gib(os.memory_available_bytes) && `${gib(os.memory_available_bytes)} available`,
                                    ])}
                                  </div>
                                  <div className="sub">
                                    <Badge tone={statusTone(probe.quality)}>{probe.quality}</Badge> {os.source} · {probe.recorded_at}
                                  </div>
                                </>
                              ) : <span className="muted">{text(probe)}</span>}
                            </td>
                            <td className="mono">{text(hw?.hw_fp)}</td>
                            <td>{hw?.stale == null ? 'unknown' : hw.stale ? 'stale' : 'fresh'}</td>
                          </tr>
                          {expanded.has(key) ? (
                            <tr className="detail-row">
                              <td colSpan={8}>
                                <Sections sections={[
                                  ['Declaration', {
                                    host: row.host, user: row.user, worker_port: row.port, node_class: row.node_class,
                                    borrowed: row.borrowed, default_profile: row.default_profile,
                                    strata_default_backend: row.strata_default_backend,
                                    spark_tensorfold: row.spark_tensorfold, runtime_image: row.runtime_image,
                                  }],
                                  ['Hardware record', hw ?? null],
                                  ['Minimal probe', probe ?? null],
                                ]} />
                                <Related kind="nodes" id={row.id} refresh={refresh} />
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}

          {(view === 'engines' || view === 'overview') && <Engines rows={current.engines} issues={current.engineIssues}
            snapshot={current.snapshots.engines} overview={view === 'overview'} overviewQuery={query} refresh={refresh} expanded={expanded} toggle={toggle} />}

          {view === 'deployments' && current.deployments ? (() => {
            const deploy = current.deployments
            const control = deploy.control
            return (
              <section className="panel">
                <h2>Deployments</h2>
                <div className="detail-grid">
                  <section>
                    <h4>Control (ops deploy)</h4>
                    <dl className="kv">
                      <dt>deployed</dt>
                      <dd>
                        <span className="mono">{short(control.revision, 8) ?? 'unknown'}</span>
                        {control.deployed_at ? <span className="muted"> · {control.deployed_at}</span> : null}
                        {control.dirty ? <> <Badge tone="warn">with uncommitted changes</Badge></> : null}
                      </dd>
                      <dt>checkout HEAD</dt>
                      <dd><span className="mono">{short(control.head, 8) ?? 'unknown'}</span></dd>
                      <dt>state</dt>
                      <dd>
                        {control.up_to_date == null ? <Badge>unknown</Badge>
                          : control.up_to_date ? <Badge tone="ok">HEAD deployed</Badge>
                          : <Badge tone="warn" title="run agentiac ops deploy --check">HEAD not deployed</Badge>}
                      </dd>
                    </dl>
                  </section>
                  <section>
                    <h4>Route (local-coder)</h4>
                    <dl className="kv">
                      <dt>current</dt>
                      <dd>{deploy.route.current ? <a className="mono" href={link('models', deploy.route.current)}>{targetName(deploy.route.current, deploy.target_names)}</a> : 'unknown'}</dd>
                      <dt>declared default</dt>
                      <dd>{deploy.route.declared_default ? <a className="mono" href={link('models', deploy.route.declared_default)}>{targetName(deploy.route.declared_default, deploy.target_names)}</a> : 'unknown'}</dd>
                    </dl>
                    {deploy.route.changes?.length ? (
                      <div className="table">
                        <table>
                          <thead><tr><th>time</th><th>from</th><th>to</th></tr></thead>
                          <tbody>
                            {deploy.route.changes.map((change, index) => (
                              <tr key={`${change.time}:${index}`}>
                                <td className="nowrap">{text(change.time)}</td>
                                <td className="mono">{change.from_target ? targetName(change.from_target, deploy.target_names) : 'unknown'}</td>
                                <td className="mono">{targetName(change.to_target, deploy.target_names)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : <p className="muted">no recorded route change</p>}
                  </section>
                </div>
                <h3>Dashboard releases</h3>
                {deploy.dashboard?.length ? (
                <div className="table">
                  <table>
                    <thead>
                      <tr><th>applied</th><th>status</th><th>release</th><th>revision</th><th>scope</th></tr>
                    </thead>
                    <tbody>
                      {(deploy.dashboard ?? []).filter(row => matches(row, query)).map(row => {
                        const key = `deploy:${row.id}`
                        return (
                          <Fragment key={row.id}>
                            <tr id={anchor('deployments', row.id)}>
                              <td className="id">
                                <a className="mono" href={link('deployments', row.id)}>{text(row.applied_at)}</a>
                                <Toggle open={expanded.has(key)} onClick={() => toggle(key)} />
                              </td>
                              <td className="nowrap"><Badge tone={statusTone(row.status)}>{row.status}</Badge> <span className="muted">{text(row.action)}</span></td>
                              <td className="mono nowrap">{text(row.release)}{row.previous_release ? <div className="sub">from {row.previous_release}</div> : null}</td>
                              <td className="mono">{short(row.revision, 8) ?? <span className="muted">release removed</span>}</td>
                              <td>{join([row.transport, row.port != null && `port ${row.port}`,
                                row.probe_targets?.length ? `${row.probe_targets.length} probe targets` : 'no probes',
                                row.enabled_at_login && 'enabled at login'])}</td>
                            </tr>
                            {expanded.has(key) ? (
                              <tr className="detail-row">
                                <td colSpan={5}><Fields data={row as unknown as Record<string, unknown>} /></td>
                              </tr>
                            ) : null}
                          </Fragment>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
                ) : <p className="muted">no dashboard deployment recorded in the plan/apply history</p>}
              </section>
            )
          })() : null}

          {view === 'models' || view === 'overview' ? (
            <section className="panel">
              <h2>Models</h2>
              <div className="table">
                <table>
                  <thead>
                    <tr>
                      <th>name</th><th>node</th><th>route</th><th>backend</th><th>engine</th>
                      <th>quantization</th><th>context / out</th><th>weights</th><th>evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredModels.map((row) => {
                      const key = `model:${row.target}`
                      const declared = rec(row.declared)
                      const engine = rec(declared.engine)
                      const weights = rec(declared.weights)
                      const state = rec(row.state)
                      const evidence = rec(row.evidence)
                      const route = opt(state.route) ?? 'unknown'
                      return (
                        <Fragment key={row.target}>
                          <tr id={anchor('models', row.target)}>
                            <td className="id">
                              <a className="mono" href={link('models', row.target)}>{row.name ?? row.target}</a>
                              {row.current ? <Badge tone="accent" title="current local-coder selection">current</Badge> : null}
                              <Toggle open={expanded.has(key)} onClick={() => toggle(key)} />
                              {row.name && row.name !== row.target ? <div className="sub">route key {row.target}</div> : null}
                            </td>
                            <td className="mono nowrap">{text(row.node)}</td>
                            <td className="nowrap"><Badge tone={route.startsWith('serving') ? 'ok' : 'muted'}>{route}</Badge></td>
                            <td className="mono">{text(declared.backend)}</td>
                            <td className="nowrap">
                              {join([opt(engine.version), short(engine.revision, 8)], ' @ ') || 'unknown'}
                              {opt(engine.repository) ? <div className="sub">{String(engine.repository).replace(/^https:\/\/github\.com\//, '').replace(/\.git$/, '')}</div> : null}
                            </td>
                            <td className="nowrap">{text(declared.quantization)}</td>
                            <td className="nowrap">{tokens(declared.context_window)} / {tokens(declared.max_tokens)}</td>
                            <td>
                              <span className="mono">{text(weights.repository)}</span>
                              {typeof weights.files === 'number' ? <div className="sub">{join([`${weights.files} files`, gib(weights.bytes)])}</div> : null}
                            </td>
                            <td className="nowrap">{typeof evidence.rows === 'number' ? `${text(evidence.usable)} usable / ${evidence.rows}` : text(evidence)}</td>
                          </tr>
                          {expanded.has(key) ? (
                            <tr className="detail-row">
                              <td colSpan={9}>
                                <Sections sections={[
                                  ['Declared', row.declared], ['Identity', row.identity],
                                  ['Observed state', row.state], ['Evidence', row.evidence],
                                ]} />
                                <Related kind="models" id={row.target} refresh={refresh} />
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}

          {view === 'agents' || view === 'overview' ? (
            <section className="panel">
              <h2>Agents</h2>
              <div className="table">
                <table>
                  <thead>
                    <tr>
                      <th>id</th><th>version</th><th>install</th><th>endpoint</th>
                      <th>gateway only</th><th>capabilities</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredAgents.map((row) => {
                      const key = `agent:${row.id}`
                      return (
                        <Fragment key={row.id}>
                          <tr id={anchor('agents', row.id)}>
                            <td className="id">
                              <a className="mono" href={link('agents', row.id)}>{row.id}</a>
                              <Toggle open={expanded.has(key)} onClick={() => toggle(key)} />
                            </td>
                            <td className="nowrap">
                              {opt(row.version) ?? <span className="muted">not detected</span>}
                              <div className="sub">declared {text(row.declared_version)}</div>
                            </td>
                            <td className="mono nowrap">{text(row.install)}</td>
                            <td className="mono nowrap">{text(row.endpoint)}</td>
                            <td>{row.gateway_only ? 'yes' : 'no'}</td>
                            <td>
                              <span className="chips">
                                {Object.values(row.capabilities ?? {}).map((cap) => (
                                  <Badge key={cap.feature} tone={statusTone(cap.value)} title={cap.reason}>
                                    {cap.feature}: {cap.value}
                                  </Badge>
                                ))}
                              </span>
                            </td>
                          </tr>
                          {expanded.has(key) ? (
                            <tr className="detail-row">
                              <td colSpan={6}>
                                <Sections sections={[
                                  ['Installation', {
                                    install: row.install, entrypoint: row.entrypoint, endpoint: row.endpoint,
                                    allowed_paths: row.allowed_paths, pin: row.pin,
                                  }],
                                  ['Capabilities', row.capabilities ?? {}],
                                ]} />
                                <Related kind="agents" id={row.id} refresh={refresh} />
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}

          {view === 'configuration' || view === 'overview' ? (
            <section className="panel">
              <h2>Configuration</h2>
              <div className="table">
                <table>
                  <thead><tr><th>id</th><th>kind</th><th>summary</th></tr></thead>
                  <tbody>
                    {filteredConfig.map((row) => {
                      const key = `config:${row.id}`
                      const summary = Object.entries(row.declared)
                        .filter(([name, value]) => !['id', 'schema_version'].includes(name) && value !== null && typeof value !== 'object')
                        .slice(0, 5)
                      return (
                        <Fragment key={row.id}>
                          <tr id={anchor('configuration', row.id)}>
                            <td className="id">
                              <a className="mono" href={link('configuration', row.id)}>{row.id}</a>
                              <Toggle open={expanded.has(key)} onClick={() => toggle(key)} />
                            </td>
                            <td className="nowrap">{row.kind}</td>
                            <td>
                              {summary.length ? (
                                <span className="chips">
                                  {summary.map(([name, value]) => <span key={name} className="pair"><span className="muted">{name}</span> {String(value)}</span>)}
                                </span>
                              ) : <span className="muted">{Object.keys(row.declared).length ? 'nested fields only' : 'empty'}</span>}
                            </td>
                          </tr>
                          {expanded.has(key) ? (
                            <tr className="detail-row">
                              <td colSpan={3}>
                                <Fields data={row.declared} />
                                <Related kind="configuration" id={row.id} refresh={refresh} />
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              <div className="detail-grid">
                <section>
                  <h4>Route</h4>
                  <dl className="kv">
                    <dt>alias</dt><dd className="mono">{text(rec(current.config.route).alias)}</dd>
                    <dt>declared default</dt>
                    <dd>
                      <a className="mono" href={link('models', String(rec(current.config.route).default ?? ''))}>
                        {targetName(opt(rec(current.config.route).default), current.config.target_names)}
                      </a>
                      <div className="sub">route key {text(rec(current.config.route).default)}</div>
                    </dd>
                  </dl>
                </section>
                <section>
                  <h4>References</h4>
                  <Value value={current.config.references ?? []} />
                </section>
              </div>
              {Object.keys(rec(rec(current.config.route).targets)).length ? (
                <>
                  <h4>Route targets</h4>
                  <div className="table">
                    <table>
                      <thead><tr><th>name</th><th>node</th><th>backend</th><th>context / out</th></tr></thead>
                      <tbody>
                        {Object.entries(rec(rec(current.config.route).targets)).map(([name, value]) => {
                          const target = rec(value)
                          return (
                            <tr key={name}>
                              <td className="mono">
                                <a href={link('models', name)}>{targetName(name, current.config.target_names)}</a>
                                {targetName(name, current.config.target_names) !== name ? <div className="sub">route key {name}</div> : null}
                              </td>
                              <td className="mono nowrap">{text(target.node)}</td>
                              <td className="mono">{text(target.backend)}</td>
                              <td className="nowrap">{tokens(target.context_window)} / {tokens(target.max_tokens)}</td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : null}
            </section>
          ) : null}

          {view === 'jobs' || view === 'overview' || view === 'monitoring' ? (
            <section className="panel">
              <h2>Jobs</h2>
              <div className="table">
                <table>
                  <thead>
                    <tr>
                      <th>id</th><th>operation</th><th>target</th><th>status</th>
                      <th>attempt</th><th>lease</th><th>result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredJobs.map((row) => {
                      const result = rec(row.result)
                      const probe = rec(result.probe)
                      return (
                        <tr key={row.id} className={row.id === jobId ? 'selected' : undefined}>
                          <td><a className="mono" href={link('jobs', row.id)} onClick={() => setJobId(row.id)}>{row.id}</a></td>
                          <td className="nowrap">{row.operation}</td>
                          <td className="mono nowrap">{row.target}</td>
                          <td className="nowrap"><Badge tone={statusTone(row.status)}>{row.status}</Badge></td>
                          <td>{row.attempt}</td>
                          <td className="nowrap">{text(row.lease_until)}</td>
                          <td>
                            {row.result == null ? <span className="muted">{opt(row.reason) ?? 'none'}</span>
                              : join([
                                opt(result.scope), opt(result.reason),
                                opt(probe.system) && join([opt(probe.system), opt(probe.machine), typeof probe.cpu_count === 'number' ? `${probe.cpu_count} CPU` : null, gib(probe.memory_total_bytes)], ' '),
                              ]) || `${Object.keys(result).length} fields`}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>

              <form onSubmit={(event) => {
                event.preventDefault()
                const target = event.currentTarget.querySelector('input[name="target"]') as HTMLInputElement | null
                const key = event.currentTarget.querySelector('input[name="key"]') as HTMLInputElement | null
                if (!target?.value || !key?.value) {
                  setError('target and idempotency key are required')
                  return
                }
                acceptProbe(session, target.value, key.value).then((issued) => {
                  setAccepted(issued)
                  setNotice(`job ${issued.job_id} accepted`)
                  setJobId(issued.job_id)
                  setRefresh(value => value + 1)
                }).catch((err) => {
                  setError(err.message)
                })
              }}>
                <label>target <input name="target" type="text" /></label>
                <label>idempotency key <input name="key" type="text" /></label>
                <button type="submit" disabled={!current.overview.operations.some(op => op.id === 'probe-node' && op.implemented)}>accept probe job</button>
                <p>{current.overview.operations.find(op => op.id === 'probe-node')?.reason}. Scope: OS, CPU count and memory only.</p>
              </form>

              <div className="controls">
                <button type="button" onClick={() => {
                  if (!jobId) return
                  mutateJob(session, `/jobs/${jobId}/cancel`).then(() => {
                    setNotice('cancel requested')
                    setRefresh(value => value + 1)
                  }).catch((err) => setError(err.message))
                }} disabled={!job || !['queued', 'running', 'cancel_requested'].includes(job.status)}>cancel selected job</button>
                <button type="button" onClick={() => {
                  if (!jobId) return
                  mutateJob(session, `/jobs/${jobId}/retry`).then(() => {
                    setNotice('retry requested')
                    setRefresh(value => value + 1)
                  }).catch((err) => setError(err.message))
                }} disabled={!job || !['failed', 'interrupted'].includes(job.status)}>retry selected job</button>
                <button type="button" onClick={() => {
                  if (!jobId) return
                  loadJob(jobId).then((loaded) => {
                    setJob(loaded.job)
                    setLogs(loaded.logs)
                    setEvents(loaded.events)
                  }).catch((err) => setError(err.message))
                }}>read logs / events</button>
              </div>

              {job && (
                <div className="detail">
                  <h3>selected job {job.id}</h3>
                  <div>status: {job.status}; attempt: {job.attempt}; lease until: {text(job.lease_until)}</div>
                  <h4>result</h4>
                  <Value value={job.result ?? null} />
                  <h4>logs</h4>
                  {logs?.lines?.length ? logs.lines.map((line, index) => <div key={index}>{line}</div>) : <div>no logs</div>}
                  <h4>events</h4>
                  {events?.events.length ? events.events.map((event) => <div key={event.event_id}>{event.at} {event.kind}: {event.message}</div>) : <div>no events</div>}
                </div>
              )}
            </section>
          ) : null}

          <section className="status">
            {accepted ? <div>accepted: {accepted.job_id} ({accepted.status})</div> : null}
            {notice ? <div>{notice}</div> : null}

          </section>
        </section>
      )}
    </div>
  )
}
