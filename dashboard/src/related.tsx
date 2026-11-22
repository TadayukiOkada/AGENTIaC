import { useEffect, useState } from 'react'
import { api } from './api'
import type { Related as RelatedReport } from './types'
import { Badge } from './view'

export type ResourceKind = 'nodes' | 'models' | 'engines' | 'agents' | 'configuration'

// A resource's address in the URL hash, e.g. #/nodes/<node> or #/configuration/backend%2Fx.
export function link(kind: string, id: string): string {
  return `#/${kind}/${encodeURIComponent(id)}`
}

export function parseHash(hash: string): { view: string; id: string | null } {
  const path = hash.replace(/^#\/?/, '').split('?')[0]
  const slash = path.indexOf('/')
  if (slash < 0) return { view: path, id: null }
  try {
    return { view: path.slice(0, slash), id: decodeURIComponent(path.slice(slash + 1)) || null }
  } catch {
    return { view: path.slice(0, slash), id: null }
  }
}

// Route targets are identified by their route key; people read the node-first name (naming stage 1).
export function targetName(key: string | null | undefined, names?: Record<string, string> | null): string {
  if (!key) return 'unknown'
  return names?.[key] ?? key
}

export function anchor(kind: string, id: string): string {
  return `row-${kind}-${id}`
}

export function Related({ kind, id, refresh }: { kind: ResourceKind; id: string; refresh: number }) {
  const [report, setReport] = useState<RelatedReport | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setReport(null); setError(null)
    api<RelatedReport>(`/related/${kind}/${id.split('/').map(encodeURIComponent).join('/')}`)
      .then(value => { if (active) setReport(value) })
      .catch(err => { if (active) setError(err instanceof Error ? err.message : String(err)) })
    return () => { active = false }
  }, [kind, id, refresh])

  return (
    <section className="related" aria-label={`related to ${id}`}>
      <h4>Related</h4>
      {error ? <span className="muted">not loaded: {error}</span>
        : !report ? <span className="muted">loading…</span>
        : !report.relations.length ? <span className="muted">none declared</span>
        : (
          <ul>
            {report.relations.map(item => (
              <li key={`${item.kind}:${item.id}:${item.relation}`}>
                <span className="muted">{item.relation}</span>{' '}
                {item.exists
                  ? <a className="mono" href={link(item.kind, item.id)}>{item.kind === 'configuration' ? item.id
                    : `${item.kind}/${item.kind === 'models' ? targetName(item.id, report.target_names) : item.id}`}</a>
                  : <><span className="mono">{item.kind}/{item.id}</span> <Badge tone="bad" title="referenced but not declared">not declared</Badge></>}
              </li>
            ))}
          </ul>
        )}
    </section>
  )
}
