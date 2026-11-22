import { Fragment } from 'react'
import type { ReactNode } from 'react'

// Presentation helpers. Tables show a few scannable summary columns; full records stay one click away
// in a key/value detail row instead of being squeezed into a cell as one-line JSON.

export type Rec = Record<string, unknown>

export function isRecord(value: unknown): value is Rec {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function rec(value: unknown): Rec {
  return isRecord(value) ? value : {}
}

export function text(value: unknown): string {
  if (value === null || value === undefined) return 'unknown'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export function opt(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

export function tokens(value: unknown): string {
  if (typeof value !== 'number') return 'unknown'
  return value >= 1024 && value % 1024 === 0 ? `${value / 1024}k` : String(value)
}

export function gib(bytes: unknown): string | null {
  return typeof bytes === 'number' ? `${(bytes / 2 ** 30).toFixed(1)} GiB` : null
}

export function age(seconds: unknown): string | null {
  if (typeof seconds !== 'number') return null
  if (seconds < 120) return `${Math.round(seconds)}s ago`
  if (seconds < 7200) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

export function short(value: unknown, length = 12): string | null {
  const shown = opt(value)
  return shown && shown.length > length ? shown.slice(0, length) : shown
}

export function join(parts: Array<string | null | undefined | false>, separator = ' · '): string {
  return parts.filter(Boolean).join(separator)
}

export type Tone = 'ok' | 'warn' | 'bad' | 'accent' | 'muted'

export function Badge({ tone = 'muted', title, children }: { tone?: Tone; title?: string; children: ReactNode }) {
  return <span className={`badge ${tone}`} title={title}>{children}</span>
}

export function statusTone(status: string): Tone {
  if (['succeeded', 'serving', 'fresh', 'native', 'current'].includes(status)) return 'ok'
  if (['failed', 'interrupted', 'unsupported'].includes(status)) return 'bad'
  if (['queued', 'running', 'cancel_requested'].includes(status)) return 'accent'
  if (['stale', 'emulated', 'cancelled', 'emergency stop'].includes(status)) return 'warn'
  return 'muted'
}

function Scalar({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="muted">—</span>
  if (typeof value === 'boolean') return <>{value ? 'yes' : 'no'}</>
  return <>{String(value)}</>
}

function isLeaf(value: unknown): boolean {
  if (Array.isArray(value)) return value.every(item => !isRecord(item) && !Array.isArray(item))
  return !isRecord(value) || Object.keys(value).length === 0
}

// Nested records become dotted paths, so a deep declaration stays a single two-column list
// instead of ever-narrower nested columns.
export function flatten(value: unknown, prefix = ''): Array<[string, unknown]> {
  if (isLeaf(value)) return [[prefix || 'value', value]]
  const entries: Array<[string, unknown]> = Array.isArray(value)
    ? value.map((item, index) => [`${prefix}[${index}]`, item])
    : Object.entries(value as Rec).map(([key, item]) => [prefix ? `${prefix}.${key}` : key, item])
  return entries.flatMap(([path, item]) => flatten(item, path))
}

function Leaf({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    if (!value.length) return <span className="muted">none</span>
    return <span className="chips">{value.map((item, index) => <code key={index}>{String(item)}</code>)}</span>
  }
  if (isRecord(value)) return <span className="muted">empty</span>
  return <Scalar value={value} />
}

export function Value({ value }: { value: unknown }) {
  return isLeaf(value) ? <Leaf value={value} /> : <Fields data={value as Rec} />
}

export function Fields({ data, omit = [] }: { data: Rec; omit?: string[] }) {
  const entries = flatten(Object.fromEntries(Object.entries(data).filter(([key]) => !omit.includes(key))))
  if (!entries.length || (entries.length === 1 && entries[0][0] === 'value')) return <span className="muted">empty</span>
  return (
    <dl className="kv">
      {entries.map(([path, value]) => (
        <Fragment key={path}>
          <dt>{path}</dt>
          <dd><Leaf value={value} /></dd>
        </Fragment>
      ))}
    </dl>
  )
}

export function Sections({ sections }: { sections: Array<[string, unknown]> }) {
  return (
    <div className="detail-grid">
      {sections.map(([title, value]) => (
        <section key={title}>
          <h4>{title}</h4>
          <Value value={value} />
        </section>
      ))}
    </div>
  )
}

export function Toggle({ open, onClick }: { open: boolean; onClick: () => void }) {
  return (
    <button type="button" className="toggle" aria-expanded={open} onClick={onClick}>
      {open ? 'hide' : 'details'}
    </button>
  )
}
