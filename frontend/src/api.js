// Thin API client. All calls go to /api (proxied to the FastAPI backend by Vite).
async function req(path, opts = {}) {
  const r = await fetch(`/api${path}`, opts)
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`
    try { const j = await r.json(); msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail) } catch (_) { /* not json */ }
    throw new Error(msg)
  }
  return r.json()
}
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
const enc = encodeURIComponent

export const api = {
  health: () => req('/health'),
  settings: () => req('/settings'),
  saveSettings: (b) => req('/settings', json('POST', b)),
  llmTest: () => req('/llm/test', { method: 'POST' }),
  overview: () => req('/overview'),
  catalog: () => req('/catalog'),
  examples: () => req('/examples'),
  pin: (question, title) => req('/pins', json('POST', { question, title })),
  unpin: (question) => req(`/pins?question=${enc(question)}`, { method: 'DELETE' }),
  // pipeline
  schema: () => req('/pipeline/schema'),
  upload: (files, mode = 'merge') => {
    const fd = new FormData(); [...files].forEach((f) => fd.append('files', f)); fd.append('mode', mode)
    return req('/pipeline/upload', { method: 'POST', body: fd })
  },
  runs: () => req('/pipeline/runs'),
  run: (id) => req(`/pipeline/runs/${enc(id)}`),
  resolve: (id, decisions) => req(`/pipeline/runs/${enc(id)}/resolve`, json('POST', { decisions })),
  // data dictionary / warehouse
  tables: () => req('/tables'),
  table: (key) => req(`/tables/${enc(key)}`),
  preview: (key) => req(`/tables/${enc(key)}/preview?n=30`),
  editTable: (key, body) => req(`/tables/${enc(key)}/edit`, json('POST', body)),
  addMetric: (key, body) => req(`/tables/${enc(key)}/metrics`, json('POST', body)),
  delMetric: (key, name) => req(`/tables/${enc(key)}/metrics/${enc(name)}`, { method: 'DELETE' }),
  rollback: (key) => req(`/tables/${enc(key)}/rollback`, { method: 'POST' }),
  deleteTable: (key) => req(`/tables/${enc(key)}`, { method: 'DELETE' }),
  deleteStored: (kind, name) => req(`/files-in-store/${kind}/${enc(name)}`, { method: 'DELETE' }),
  reset: (confirm) => req(`/reset?confirm=${enc(confirm)}`, { method: 'POST' }),
  // analysis
  ask: (question, session_id) => req('/ask', json('POST', { question, session_id })),
  feedback: (body) => req('/feedback', json('POST', body)),
  insights: (scope) => req(`/insights?scope=${scope}`),
  actions: () => req('/actions'),
  alerts: () => req('/alerts'),
  addRule: (b) => req('/alerts/rules', json('POST', b)),
  delRule: (id) => req(`/alerts/rules/${id}`, { method: 'DELETE' }),
  readNotes: () => req('/notifications/read', { method: 'POST' }),
  // outputs
  report: (b) => req('/reports', json('POST', b)),
  deck: (b) => req('/decks', json('POST', b)),
  audit: (file) => { const fd = new FormData(); fd.append('file', file); return req('/audit', { method: 'POST', body: fd }) },
  files: () => req('/files'),
}

export const fileUrl = (name, inline = false) => `/api/files/${enc(name)}${inline ? '?inline=true' : ''}`
export const downloadUrl = (key) => `/api/tables/${enc(key)}/download`
export const TERMINAL = ['succeeded', 'succeeded_with_warnings', 'quarantined', 'failed', 'needs_review']
export const STATUS_LABEL = { queued: 'Queued', running: 'Running', succeeded: 'Loaded', succeeded_with_warnings: 'Loaded with warnings', quarantined: 'Quarantined', failed: 'Failed', needs_review: 'Needs your review' }
export const fmtInt = (n) => (n ?? 0).toLocaleString('en-IN')

export function sessionId() {
  try {
    let s = localStorage.getItem('sahayak_session')
    if (!s) { s = Math.random().toString(36).slice(2, 12); localStorage.setItem('sahayak_session', s) }
    return s
  } catch (_) { return 'browser-' + Math.random().toString(36).slice(2, 8) }
}
