import React, { useEffect, useRef, useState } from 'react'
import { STATUS_LABEL, TERMINAL, api, fmtInt, sessionId } from '../api.js'
import AnswerCard from '../components/AnswerCard.jsx'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

export default function Ask({ pending, clearPending, version, empty, go, onChanged, toast }) {
  const [items, setItems] = useState([])
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(false)
  const [examples, setExamples] = useState([])
  const sid = useRef(sessionId())
  const end = useRef(null)
  const fileInput = useRef(null)

  useEffect(() => { api.examples().then((e) => setExamples(e.groups || [])).catch(() => {}) }, [version])
  useEffect(() => { end.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [items, busy])
  useEffect(() => { if (pending) { ask(pending); clearPending() } }, [pending]) // eslint-disable-line

  async function ask(text) {
    const question = (text ?? q).trim()
    if (!question || busy) return
    setQ('')
    setItems((x) => [...x, { kind: 'q', text: question }])
    setBusy(true)
    try {
      const r = await api.ask(question, sid.current)
      setItems((x) => [...x, { kind: 'a', r }])
    } catch (e) {
      setItems((x) => [...x, { kind: 'a', r: { type: 'clarify', title: 'Could not reach the server', answer: [`${e.message}. Check that the backend is running (python start.py).`] } }])
    } finally { setBusy(false) }
  }

  // Paperclip: files go through the same 7-stage pipeline as the Pipeline page.
  async function attach(files) {
    if (!files?.length) return
    setItems((x) => [...x, { kind: 'q', text: `Uploaded ${[...files].map((f) => f.name).join(', ')}` }])
    setBusy(true)
    try {
      const up = await api.upload(files)
      const done = []
      for (const r0 of up.runs) {
        let r = await api.run(r0.id)
        const t0 = Date.now()
        while (!TERMINAL.includes(r.status) && Date.now() - t0 < 180000) { await sleep(700); r = await api.run(r0.id) }
        done.push(r)
      }
      const lines = done.map((r) => {
        const tabs = (r.tables || []).filter((e) => e.load?.version).map((e) => `${e.load.label} ${fmtInt(e.load.rows_after)} rows`)
        return `**${r.file}**: ${STATUS_LABEL[r.status] || r.status}${tabs.length ? ` (${tabs.join('; ')})` : ''}. ${r.stages.find((s) => s.key === 'ready')?.summary || r.message || ''}`
      })
      const review = done.some((r) => r.status === 'needs_review')
      const draft = false
      if (review) lines.push('Some columns need your confirmation. Open **Pipeline** to check the mapping, then it loads automatically.')
      setItems((x) => [...x, { kind: 'a', r: { type: 'answer', title: 'Files processed by the pipeline', answer: lines, sources: [],
        followups: draft ? ['Check this MIS draft against the source data and list every error.'] : ['What data is loaded?'] } }])
      onChanged?.()
    } catch (e) {
      setItems((x) => [...x, { kind: 'a', r: { type: 'clarify', title: 'Upload failed', answer: [e.message] } }])
    } finally { setBusy(false) }
  }

  return (
    <div className="ask">
      <div className="thread" aria-live="polite">
        {items.length === 0 ? (
          <div className="empty">
            <h1>Ask anything about your data.</h1>
            <p>Type in plain English or Hinglish. Every number is computed from your loaded data, cross-checked a second way, and shown with its source and the exact steps used. You can also ask for reports, infographics, decks, alerts, what-if checks and what to act on first.</p>
            {empty ? (
              <div className="panel">
                <h3>No data loaded yet</h3>
                <p className="notes" style={{ paddingLeft: 0 }}>Upload your files first. Suggested questions appear here once there is data to answer them from.</p>
                <button className="btn" onClick={() => go('pipeline')}>Go to Pipeline</button>
              </div>
            ) : examples.map((g) => (
              <div className="ex-group" key={g.name}>
                <h3>{g.name}</h3>
                <div className="chips">{g.items.map((e) => <button className="chip" key={e} onClick={() => ask(e)}>{e}</button>)}</div>
              </div>
            ))}
          </div>
        ) : null}
        {items.map((it, i) => it.kind === 'q'
          ? <div className="q-bubble" key={i}>{it.text}</div>
          : <AnswerCard key={i} r={it.r} onAsk={ask} onPin={async (q, t) => { try { await api.pin(q, t); toast?.('Pinned to Overview'); onChanged?.() } catch (e) { toast?.(e.message) } }} />)}
        {busy ? <div className="thinking"><span className="flow" aria-hidden="true" />Working on it</div> : null}
        <div ref={end} />
      </div>
      <div className="composer-wrap">
        <form className="composer" onSubmit={(e) => { e.preventDefault(); ask() }}>
          <button type="button" className="icon-btn" title="Upload files through the pipeline" aria-label="Upload files" onClick={() => fileInput.current?.click()}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5" /></svg>
          </button>
          <input ref={fileInput} type="file" multiple hidden onChange={(e) => { attach(e.target.files); e.target.value = '' }} />
          <textarea rows={1} value={q} placeholder={empty ? 'Load data on the Pipeline page first' : 'Ask a question about your data'} aria-label="Your question"
            onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask() } }} />
          <button className="btn" type="submit" disabled={busy || !q.trim()}>Ask</button>
        </form>
        <div className="composer-hint">Read-only: SAHAYAK-AI never changes your data. Press Enter to ask, Shift+Enter for a new line.</div>
      </div>
    </div>
  )
}
