import { Fragment, useEffect, useState } from 'react'
import type { EngineRow, Snapshot } from './types'
import type { LifecycleIssueDTO, LifecycleAssociationDTO } from './generated/api'
import { Badge, Sections, short } from './view'
import { Related, anchor, link, parseHash } from './related'

type Scope = 'retained' | 'history' | 'all'
type Filters = { scope: Scope; node: string; use: string; q: string }
const USES: Record<string, string> = { default: 'Default', rollback: 'Rollback', evaluation: 'Evaluation', quarantined: 'Quarantined', retired: 'Retired record', declared_only: 'Declared only', review: 'Needs review' }
const DEFAULTS: Filters = { scope: 'retained', node: '', use: '', q: '' }
function readFilters(): Filters {
  const query = new URLSearchParams(window.location.hash.split('?')[1] ?? '')
  const scope = query.get('scope'), use = query.get('use') ?? ''
  return { scope: scope === 'all' || scope === 'history' ? scope : 'retained', node: query.get('node') ?? '', use: use in USES ? use : '', q: query.get('q') ?? '' }
}
function filterHash(filters: Filters, id: string | null): string {
  const params = new URLSearchParams()
  if (filters.scope !== 'retained') params.set('scope', filters.scope)
  if (filters.node) params.set('node', filters.node)
  if (filters.use) params.set('use', filters.use)
  if (filters.q) params.set('q', filters.q)
  return `#/engines${id ? '/' + encodeURIComponent(id) : ''}${params.size ? '?' + params : ''}`
}
function matching(row: EngineRow, filters: Filters, ignoreScope = false): boolean {
  const life = row.lifecycle
  if (!ignoreScope && life && ((filters.scope === 'retained' && life.history_only) || (filters.scope === 'history' && !life.history_only))) return false
  const haystack = [row.id, row.name, row.version, row.revision, ...(row.backends ?? []), ...(row.targets ?? []), ...(row.nodes ?? []), ...(life?.nodes ?? [])].join(' ').toLowerCase()
  if (!filters.q.toLowerCase().trim().split(/\s+/).filter(Boolean).every(term => haystack.includes(term))) return false
  if (!life) return !filters.node && !filters.use
  const sameNode = life.associations.filter(a => !filters.node || a.node === filters.node)
  if (filters.node && !sameNode.length && !(filters.use === 'review' && life.issues.some(i => i.node === filters.node || !i.node))) return false
  if (filters.use === 'review') return life.issues.some(i => !filters.node || !i.node || i.node === filters.node)
  if (filters.use) return sameNode.some(a => a.role === filters.use)
  return true
}
function order(row: EngineRow): number {
  if (row.current) return 0
  const roles = row.lifecycle?.roles ?? []
  for (const [index, role] of ['default', 'rollback', 'evaluation', 'quarantined', 'declared_only'].entries()) if (roles.includes(role as typeof roles[number])) return index + 1
  return row.lifecycle?.history_only ? 7 : 6
}
function compare(a: EngineRow, b: EngineRow): number {
  const rank = order(a) - order(b), name = (a.name ?? '').localeCompare(b.name ?? '')
  if (rank || name) return rank || name
  const av = a.version ?? '', bv = b.version ?? '', numeric = /^\d+(\.\d+)*$/
  if (numeric.test(av) && numeric.test(bv)) {
    const aa = av.split('.').map(Number), bb = bv.split('.').map(Number)
    for (let i = 0; i < Math.max(aa.length, bb.length); i++) { const diff = (bb[i] ?? 0) - (aa[i] ?? 0); if (diff) return diff }
  } else if (av !== bv) return av.localeCompare(bv)
  return (a.repository ?? '').localeCompare(b.repository ?? '') || (a.revision ?? '').localeCompare(b.revision ?? '') || a.id.localeCompare(b.id)
}
function guidance(a: LifecycleAssociationDTO): string | null {
  if (a.retirement_intent !== 'candidate' || !a.node || a.source.handle !== a.backend) return null
  if (!/^[a-z0-9][a-z0-9-]*$/.test(a.node) || !/^[a-z0-9][a-z0-9._-]*$/.test(a.backend)) return null
  return `agentiac ops retire ${a.node} --backend ${a.backend} --check`
}
export function Engines({ rows, issues, snapshot, overview, overviewQuery, refresh, expanded, toggle }: {
  rows: EngineRow[]; issues: LifecycleIssueDTO[]; snapshot?: Snapshot; overview: boolean; overviewQuery: string; refresh: number;
  expanded: Set<string>; toggle: (key: string) => void
}) {
  const [filters, setFilters] = useState<Filters>(readFilters), [search, setSearch] = useState(filters.q)
  const [notice, setNotice] = useState(''), [selected, setSelected] = useState<string | null>(() => parseHash(window.location.hash).id)
  useEffect(() => {
    const follow = () => { const next = readFilters(); setFilters(next); setSearch(next.q); setSelected(parseHash(window.location.hash).id) }
    window.addEventListener('hashchange', follow)
    return () => window.removeEventListener('hashchange', follow)
  }, [])
  function update(next: Filters, replace = false) {
    setFilters(next)
    const hash = filterHash(next, selected)
    if (replace) history.replaceState(null, '', hash)
    else if (window.location.hash !== hash) window.location.hash = hash
  }
  useEffect(() => {
    if (overview || search === filters.q) return
    const timer = setTimeout(() => update({ ...filters, q: search }, true), 300)
    return () => clearTimeout(timer)
  }, [search, filters, overview])
  const supported = rows.every(row => !!row.lifecycle), applied = overview ? { ...DEFAULTS, q: overviewQuery } : filters
  const visible = rows.filter(row => matching(row, supported ? applied : { ...DEFAULTS, scope: 'all', q: applied.q })).sort(compare)
  const hidden = supported ? rows.filter(row => row.lifecycle?.history_only && matching(row, applied, true)).length : 0
  const selectedRow = !overview && selected ? rows.find(row => row.id === selected) : undefined
  const outside = selectedRow && !visible.some(row => row.id === selectedRow.id) ? selectedRow : null
  const displayed = outside ? [...visible, outside] : visible
  const nodes = [...new Set(rows.flatMap(row => row.lifecycle?.nodes ?? []))].sort()
  const reviewCount = rows.filter(row => row.lifecycle?.issues.length).length
  const reset = () => { setSearch(''); update(DEFAULTS) }
  return <section className="panel engines-panel">
    <h2>Engines</h2><p>Declared uses of each engine release. Installed and loaded state is not checked here.</p>
    {snapshot?.stale && <p role="status">Showing saved declarations from {snapshot.generated_at}; refresh failed.</p>}
    {!supported && <p role="status">Lifecycle information unavailable; showing all releases.</p>}
    {issues.length > 0 && <section aria-label="Unresolved lifecycle references"><h3>Needs review: {issues.length}</h3><ul>{issues.map((item, index) => <li key={`${item.code}-${index}`}>{item.message} <a href={link('configuration', item.source_resource_id)}>{item.source_resource_id}</a></li>)}</ul></section>}
    {supported && reviewCount > 0 && <p>{reviewCount} release{reviewCount === 1 ? '' : 's'} need review. {overview
      ? <a href="#/engines?use=review">Review releases</a>
      : <button type="button" onClick={() => update({ ...filters, scope: 'all', use: 'review' })}>Review releases</button>}</p>}
    {!overview && <div className="engine-filters">
      <label>Scope <select value={filters.scope} disabled={!supported} onChange={e => update({ ...filters, scope: e.target.value as Scope })}><option value="retained">Declared &amp; retained</option><option value="history">History only</option><option value="all">All releases</option></select></label>
      <label>Node <select value={filters.node} disabled={!supported} onChange={e => update({ ...filters, node: e.target.value })}><option value="">All nodes</option>{nodes.map(node => <option key={node} value={node}>{node}</option>)}</select></label>
      <label>Use <select value={filters.use} disabled={!supported} onChange={e => update({ ...filters, use: e.target.value })}><option value="">All uses</option>{Object.entries(USES).filter(([key]) => key !== 'retired').map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label>Search <input aria-label="Search engines" value={search} onChange={e => setSearch(e.target.value)} placeholder="Engine, version, node…" /></label>
      <button type="button" onClick={reset}>Reset filters</button>
    </div>}
    <p aria-live="polite">{visible.length} of {rows.length} releases{supported && applied.scope === 'retained' && hidden > 0 ? ` · ${hidden} history-only hidden` : ''}</p>
    {supported && applied.scope === 'retained' && hidden > 0 && (overview ? <a href="#/engines?scope=all">Show history in Engines</a> : <button type="button" onClick={() => update({ ...filters, scope: 'all' })}>Show history</button>)}
    {overview && <p><a href="#/engines">Open Engines</a></p>}
    {outside && <p role="status">This release is hidden by the current filters. <button type="button" onClick={() => { setSearch(''); update({ ...DEFAULTS, scope: 'all' }) }}>Show in list</button></p>}
    {!visible.length && !outside && <p>{!rows.length ? 'No engine declarations found' : applied.scope === 'history' && !applied.q && !applied.node && !applied.use ? 'No releases in this scope' : 'No matching releases'} {!overview && rows.length > 0 && (applied.scope === 'history' && !applied.q && !applied.node && !applied.use ? <button type="button" onClick={() => update({ ...filters, scope: 'all' })}>Show all releases</button> : <button type="button" onClick={reset}>Reset filters</button>)}</p>}
    <div className="table engine-table"><table><thead><tr><th>engine</th><th>version @ revision</th><th>declared uses</th><th>backends</th><th>nodes</th></tr></thead>
      <tbody>{displayed.map(row => {
        const key = `engine:${row.id}`, details = `engine-details-${row.id}`
        const relevant = applied.node ? row.lifecycle?.associations.filter(a => a.node === applied.node) : row.lifecycle?.associations
        const roles = applied.node ? [...new Set(relevant?.map(a => a.role) ?? [])] : row.lifecycle?.roles ?? []
        const others = applied.node ? new Set((row.lifecycle?.nodes ?? []).filter(n => n !== applied.node)).size : 0
        return <Fragment key={row.id}>
          <tr id={anchor('engines', row.id)}><td className="id"><a className="mono" href={filterHash(applied, row.id)}>{row.id}</a>
            {row.current && <Badge tone="accent">local-coder selected</Badge>}{row.lifecycle?.history_only && <Badge>History</Badge>}
            {row.lifecycle?.issues.length ? <Badge tone="warn">Needs review</Badge> : null}{!row.declared && <Badge tone="warn">engine not declared</Badge>}
            <button type="button" className="toggle" aria-label={`${expanded.has(key) ? 'Hide' : 'Show'} details for ${row.id}`} aria-expanded={expanded.has(key)} aria-controls={expanded.has(key) ? details : undefined} onClick={() => toggle(key)}>{expanded.has(key) ? 'hide' : 'details'}</button></td>
            <td>{[row.version, short(row.revision, 8)].filter(Boolean).join(' @ ') || 'unknown'}<div className="sub">{row.repository}</div></td>
            <td>{roles.map(role => <Badge key={role}>{USES[role]}</Badge>)}{others > 0 && <div className="sub">Other uses on {others} node{others === 1 ? '' : 's'}</div>}</td>
            <td>{row.backends?.length ?? 0} backend configurations</td><td>{(row.lifecycle?.nodes ?? row.nodes ?? []).join(', ') || 'none'}</td></tr>
          {expanded.has(key) && <tr className="detail-row" id={details}><td colSpan={5}>
            <h3>Uses by node</h3>
            {row.lifecycle && <p>Declared uses: {row.lifecycle.roles.map(role => {
              const useNodes = [...new Set(row.lifecycle?.associations.filter(a => a.role === role && a.node).map(a => a.node) ?? [])]
              return `${USES[role]}${useNodes.length ? ` on ${useNodes.join(', ')}` : ''}`
            }).join('; ') || 'catalog history'}.</p>}
            {row.lifecycle?.associations.length ? <div className="table"><table><thead><tr><th>node</th><th>backend</th><th>use</th><th>retirement intent</th><th>details</th></tr></thead><tbody>
              {row.lifecycle.associations.map((a, i) => { const command = guidance(a); return <tr key={`${a.node}-${a.backend}-${a.role}-${i}`}>
                <td>{a.node ?? 'Catalog only'}</td><td className="mono">{a.backend}</td><td>{USES[a.role]}{a.form === 'thin' && ' · Restore required'}</td>
                <td>{a.retirement_intent === 'candidate' ? 'Cleanup proposed' : a.retirement_intent === 'declined' ? 'Keep' : 'Not specified'}</td>
                <td><a href={a.source.resource_id.startsWith('nodes/') ? link('nodes', a.source.resource_id.slice(6)) : link('configuration', a.source.resource_id)}>{a.source.resource_id}</a>{a.reason && <details><summary>Why retained</summary><p>{a.reason}</p></details>}
                  {command && <><code>{command}</code> <button type="button" onClick={() => { navigator.clipboard?.writeText(command).then(() => setNotice('Command copied')).catch(() => setNotice('Copy failed; select the command text')) ?? setNotice('Copy unavailable; select the command text') }}>Copy check command</button><p>Run the check before planning cleanup.</p></>}</td></tr> })}
            </tbody></table></div> : <p>Catalog record only.</p>}
            {row.lifecycle?.issues.length ? <section><h3>Needs review</h3><ul>{row.lifecycle.issues.map((item, i) => <li key={`${item.code}-${i}`}>{item.message} ({item.source_resource_id})</li>)}</ul></section> : null}
            {row.lifecycle?.catalog_states.filter(c => c.status === 'archived').map(c => <p key={c.backend}><strong>History: {c.backend}</strong> — {c.reason} {c.evidence && <code>{c.evidence}</code>}</p>)}
            {row.lifecycle?.references.length ? <details><summary>Declared references</summary><ul>{row.lifecycle.references.map((ref, i) => <li key={`${ref.kind}-${ref.id}-${i}`}>{ref.kind}: {ref.id} ({ref.node ?? 'all nodes'})</li>)}</ul></details> : null}
            <Sections sections={[[ 'Release', { name: row.name, repository: row.repository, version: row.version, revision: row.revision, ggml_revisions: row.ggml_revisions ?? [], declared: row.declared } ]]} />
            <Related kind="engines" id={row.id} refresh={refresh} />
          </td></tr>}
        </Fragment>
      })}</tbody></table></div><span className="sr-only" aria-live="polite">{notice}</span>
  </section>
}
