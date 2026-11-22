export class ApiError extends Error {
  status: number
  code: string
  retryable: boolean

  constructor(status: number, code: string, message: string, retryable = false) {
    super(message)
    this.status = status
    this.code = code
    this.retryable = retryable
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers || {})
  headers.set('Accept', 'application/json')
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    credentials: 'same-origin',
    signal: options.signal ?? AbortSignal.timeout(5000),
    headers,
  })
  const text = await response.text()
  let body: unknown
  try {
    body = JSON.parse(text)
  } catch {
    body = { message: text.slice(0, 300) }
  }
  if (!response.ok) {
    const record = body as Record<string, unknown>
    const error = new ApiError(response.status,
      String(record.code ?? 'http_error'),
      String(record.message ?? 'request failed'),
      Boolean(record.retryable))
    throw error
  }
  return body as T
}

const MAX_PAGES = 50

// Every page of a cursor list, so a list longer than one page is never silently truncated.
// The cursor is opaque; it is passed back exactly as the server returned it.
export async function pages<P extends { next_cursor?: string | null }>(path: string): Promise<P[]> {
  const out: P[] = []
  let cursor: string | null | undefined = null
  do {
    const separator = path.includes('?') ? '&' : '?'
    const page: P = await api<P>(cursor ? `${path}${separator}cursor=${encodeURIComponent(cursor)}` : path)
    out.push(page)
    cursor = page.next_cursor
    if (cursor && out.length >= MAX_PAGES) {
      throw new ApiError(0, 'too_many_pages', `${path} returned more than ${MAX_PAGES} pages`)
    }
  } while (cursor)
  return out
}
