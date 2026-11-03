import React, { useEffect, useState } from 'react'
import { STATUS_LABEL, api, fmtInt } from '../api.js'
import Chart from '../components/Chart.jsx'

export default function Overview({ go, askInChat, version, toast }) {
  const [d, setD] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api.overview().then((x) => { setD(x); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [version])
  if (err) return <div className="page"><h1>Overview</h1><p className="err">{err}</p></div>
  if (!d) return <div className="page"><h1>Overview</h1><p className="notes">Loading…</p></div>

  if (d.empty) {
    return (
      <div className="page">
        <div className="empty-hero">
          <h1>No data yet</h1>
          <p>SAHAYAK-AI starts empty and works with any data you give it. Nothing is shown until your own files go through the pipeline, so every number on screen comes from your data.</p>
          <ol className="steps">
            <li><b>Upload</b> any export: CSV, Excel (all sheets), database file, JSON, Parquet, PDF or Word.</li>
            <li><b>Watch the 7 stages</b>: ingest, profile and match, clean, validate, load, update dictionary, ready. Confirm anything the pipeline is unsure about.</li>
            <li><b>Check the data dictionary</b>: rename columns, add synonyms or your own formulas.</li>
            <li><b>Ask</b> in plain English or Hinglish, and build reports, decks and alerts.</li>
          </ol>
          <button className="btn big" onClick={() => go('pipeline')}>Upload your first files</button>
          {d.runs?.length ? <p className="notes" style={{ paddingLeft: 0, marginTop: 16 }}>Recent runs: {d.runs.map((r) => `${r.file} (${STATUS_LABEL[r.status] || r.status})`).join(', ')}</p> : null}
        </div>
      </div>
    )
  }
  return (
    <div className="page wide">
      <h1>Overview</h1>
      <p className="lede">An automatic dashboard built from the data you loaded: the main figures of each table, their trend and their biggest breakdown. Click a figure to ask about it.</p>
      <div className="split" style={{ marginBottom: 18 }}>
        <div className="panel">
          <h3>Alerts now</h3>
          <p className="alert-count"><span className="sev Red">{d.alerts.red} red</span> <span className="sev Amber">{d.alerts.amber} amber</span></p>
          {d.alerts.top.length ? <ul className="notes">{d.alerts.top.map((a, i) => <li key={i}><span className={`sev ${a.severity}`}>{a.severity}</span> {a.alert}{a.entity && a.entity !== 'All' ? ` - ${a.entity}` : ''} ({a.period}){a.value ? `: ${a.value}` : ''}</li>)}</ul>
            : <p className="notes">Nothing is breached.</p>}
          <div className="row" style={{ marginTop: 10 }}>
            <button className="btn ghost small" onClick={() => go('alerts')}>All alerts</button>
            <button className="btn ghost small" onClick={() => askInChat('What should we act on first?')}>What to act on first</button>
          </div>
        </div>
        <div className="panel">
          <h3>Pinned questions</h3>
          {d.pins?.length ? <div className="chips">{d.pins.map((p) => (
            <span key={p.question} className="chip pin"><button className="linkish" onClick={() => askInChat(p.question)}>{p.question}</button>
              <button className="x" aria-label={`Unpin ${p.question}`} onClick={async () => { await api.unpin(p.question); load(); toast('Unpinned') }}>×</button></span>))}</div>
            : <p className="notes" style={{ paddingLeft: 0 }}>Pin any answer from Ask (the pin button under an answer) to keep the question here.</p>}
          <p className="notes" style={{ paddingLeft: 0 }}>{d.tables.length} table(s){d.documents.length ? `, ${d.documents.length} document(s)` : ''} loaded. <button className="linkish" onClick={() => go('data')}>Open the data dictionary</button></p>
        </div>
      </div>
      {d.cards.map((c) => (
        <section className="dash-card" key={c.table}>
          <h2>{c.label}</h2>
          {c.tiles.length ? (
            <div className="tiles">
              {c.tiles.map((t) => (
                <button key={t.label} className={`tile ${t.tone || ''}`} onClick={() => askInChat(`${t.label} by month`)} title={`Ask: ${t.label} by month`}>
                  <span className="l">{t.label}</span><span className="v">{t.value}</span>
                  <span className="s">{t.period}{t.change ? ` · ${t.change} vs a year earlier` : ''}</span>
                </button>
              ))}
            </div>
          ) : null}
          <div className="split">
            {c.trend ? <div className="panel tight"><Chart spec={c.trend} height={240} /></div> : null}
            {c.breakdown ? <div className="panel tight"><Chart spec={c.breakdown} height={240} /></div> : null}
          </div>
        </section>
      ))}
      <p className="notes" style={{ paddingLeft: 0 }}>Recent pipeline runs: {d.runs.map((r) => `${r.file} (${STATUS_LABEL[r.status] || r.status})`).join(', ') || 'none'}. <button className="linkish" onClick={() => go('pipeline')}>Upload more</button></p>
    </div>
  )
}

export { fmtInt }
