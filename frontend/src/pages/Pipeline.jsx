import React, { useEffect, useRef, useState } from 'react'
import { STATUS_LABEL, TERMINAL, api, fmtInt } from '../api.js'
import DataTable from '../components/DataTable.jsx'

const STAGE_HELP = {
  ingest: 'Opens the file: every sheet and table, the real header row (even split over two rows), stacked tables; notes and readme sheets are kept as notes.',
  profile: 'Works out what every column is (number, date, month, category, ID, text), its unit, how it adds up, and whether the file is a new table or more rows for an existing one. Anything uncertain waits for you.',
  clean: 'Fixes formats: dates (day/month order from the evidence), months, numbers with commas, %, Rs, mixed units per row, spelling variants, total rows, blank rows, duplicates, wide layouts. Every change is logged.',
  validate: 'Checks readability, gaps in time, future dates, duplicates, signs and outliers, and finds formulas between columns (like amount = quantity x rate) to check every row. Bad tables are quarantined.',
  load: 'Writes the clean table to the warehouse. Rows with the same key are updated, new ones added, so the same file twice changes nothing. Old versions are kept for rollback.',
  dictionary: 'Updates the data dictionary: labels, units, how each number adds up, links between tables (e.g. customer ID), and the questions you can now ask.',
  ready: 'Live for questions, dashboards, insights, alerts, reports and decks.',
}
const TYPES = ['number', 'date', 'datetime', 'month', 'year', 'category', 'id', 'text', 'boolean', 'fy_label', 'ignore']

export function StageTrack({ stages, compact = false }) {
  return (
    <ol className={`track ${compact ? 'compact' : ''}`} aria-label="Pipeline stages">
      {stages.map((s, i) => (
        <li key={s.key} className={`st s-${s.status}`} title={`${s.name}: ${s.status}${s.ms != null ? ` (${s.ms} ms)` : ''}`}>
          <span className="pipe-seg" aria-hidden="true" />
          <span className="st-node" aria-hidden="true">{s.status === 'done' ? '✓' : s.status === 'failed' ? '!' : s.status === 'review' ? '?' : s.status === 'warn' ? '!' : i + 1}</span>
          {compact ? null : <span className="st-name">{s.name}</span>}
          <span className="sr-only">{s.name}: {s.status}</span>
        </li>
      ))}
    </ol>
  )
}

const Level = ({ l }) => <span className={`lvl ${l}`}>{l === 'info' ? 'info' : l === 'warn' ? 'warning' : 'error'}</span>

function TableReview({ e, tables, value, onChange }) {
  const set = (patch) => onChange({ ...value, ...patch })
  const sug = e.target?.suggest
  const [tcols, setTcols] = useState([])
  useEffect(() => {
    if (!value.target || value.target === 'new') { setTcols([]); return }
    api.table(value.target).then((t) => setTcols(Object.keys(t.columns || {}))).catch(() => setTcols([]))
  }, [value.target])
  const mapped = (c) => (value.mapping[c] !== undefined ? value.mapping[c] : ((sug && sug.key === value.target ? sug.mapping : e.target?.mapping) || {})[c] || '')
  return (
    <div className="review">
      <h4>{e.origin} <small>{fmtInt(e.rows_raw)} rows, {e.columns.length} columns</small></h4>
      <ul className="log">{(e.review || []).map((r, i) => <li key={i}><Level l="warn" /> {r}</li>)}</ul>
      <div className="row" style={{ margin: '8px 0 12px' }}>
        <label className="field">What to do
          <select value={value.action} onChange={(ev) => set({ action: ev.target.value })}>
            <option value="load">Load it</option><option value="skip">Skip this table</option>
          </select>
        </label>
        {value.action === 'load' ? (
          <label className="field">Where
            <select value={value.target} onChange={(ev) => set({ target: ev.target.value })}>
              <option value="new">As a new table</option>
              {tables.map((t) => <option key={t.key} value={t.key}>Add rows to: {t.label}{sug && sug.key === t.key ? ' (suggested)' : ''}</option>)}
            </select>
          </label>
        ) : null}
        {value.action === 'load' && value.target === 'new' ? (
          <label className="field">Table name<input value={value.name} onChange={(ev) => set({ name: ev.target.value })} /></label>
        ) : null}
      </div>
      {value.action === 'load' ? (
        <div className="tbl-wrap">
          <table className="tbl map-tbl">
            <thead><tr><th>Column in your file</th><th>Read as</th>{tcols.length ? <th>Same as existing column</th> : null}<th>Examples</th><th>Confidence</th></tr></thead>
            <tbody>
              {e.columns.map((c) => {
                const tp = value.types[c.name] || c.type
                return (
                  <tr key={c.name} className={c.confidence < 0.85 || c.date_ambiguous ? 'flag' : ''}>
                    <td><b>{c.original}</b><br /><small><code>{c.name}</code></small></td>
                    <td>
                      <select aria-label={`Type of ${c.original}`} value={tp} onChange={(ev) => set({ types: { ...value.types, [c.name]: ev.target.value } })}>
                        {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                      </select>
                      {(tp === 'date' || tp === 'datetime') ? (
                        <select aria-label={`Date order of ${c.original}`} value={value.date_order[c.name] || c.date_order || 'dayfirst'} style={{ marginLeft: 6 }}
                          onChange={(ev) => set({ date_order: { ...value.date_order, [c.name]: ev.target.value } })}>
                          <option value="dayfirst">day/month/year</option><option value="monthfirst">month/day/year</option>
                        </select>
                      ) : null}
                    </td>
                    {tcols.length ? (
                      <td><select aria-label={`Existing column for ${c.original}`} value={mapped(c.name)} onChange={(ev) => set({ mapping: { ...value.mapping, [c.name]: ev.target.value } })}>
                        <option value="">(new column)</option>{tcols.map((x) => <option key={x} value={x}>{x}</option>)}
                      </select></td>) : null}
                    <td className="ex">{(c.sample || []).slice(0, 4).join(' · ')}</td>
                    <td className="n">{c.confidence != null ? `${Math.round(c.confidence * 100)}%` : ''}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : null}
      {e.preview?.columns ? <DataTable t={{ columns: e.preview.columns, rows: e.preview.rows, caption: 'First rows as found in the file', total_rows: e.preview.rows.length }} max={6} /> : null}
    </div>
  )
}

function ReviewPanel({ run, tables, onDone, toast }) {
  const need = run.tables.filter((e) => e.status === 'review')
  const [dec, setDec] = useState(() => Object.fromEntries(need.map((e) => [e.origin, {
    action: 'load', target: e.target?.suggest?.key || (e.target?.action === 'merge' ? e.target.key : 'new'), name: e.target?.label || e.name, types: {}, date_order: {}, mapping: {},
  }])))
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    setBusy(true)
    try {
      await api.resolve(run.id, need.map((e) => {
        const d = dec[e.origin]
        const sug = e.target?.suggest
        const base = (sug && sug.key === d.target ? sug.mapping : {}) || {}
        return { origin: e.origin, ...d, mapping: d.target === 'new' ? {} : { ...base, ...d.mapping } }
      }))
      toast('Confirmed. The pipeline is continuing.')
      onDone()
    } catch (er) { toast(er.message) } finally { setBusy(false) }
  }
  return (
    <div className="panel review-panel">
      <h3>Please confirm before loading</h3>
      <p className="notes" style={{ paddingLeft: 0 }}>The pipeline would rather ask than guess. Check the highlighted items, change anything that is wrong, then confirm. The same layout is recognised automatically next time.</p>
      {need.map((e) => <TableReview key={e.origin} e={e} tables={tables} value={dec[e.origin]} onChange={(v) => setDec({ ...dec, [e.origin]: v })} />)}
      <button className="btn" disabled={busy} onClick={submit}>Confirm and continue</button>
    </div>
  )
}

function TableDetail({ e }) {
  return (
    <div className="tdetail">
      <h4>{e.origin} <span className="arrow">→</span> {e.load ? e.load.label : e.target?.action === 'merge' ? `added to ${e.target.label}` : e.target?.label}
        {e.target?.action === 'merge' && e.target.similarity ? <small> ({Math.round(e.target.similarity * 100)}% column match)</small> : null}
        {e.validation?.quarantined ? <span className="sev quarantined" style={{ marginLeft: 8 }}>Quarantined</span> : null}</h4>
      <p className="maps">{e.columns.filter((c) => c.role !== 'ignore').map((c) => (
        <span key={c.name} className="map-chip"><span>{c.original}</span> → <code>{c.type}</code>{c.role === 'measure' ? <em>{c.agg}{c.unit ? `, ${c.unit}` : ''}</em> : <em>{c.role}</em>}</span>))}</p>
      {e.clean ? (
        <>
          <p className="notes" style={{ paddingLeft: 0 }}>Cleaning: {fmtInt(e.clean.rows_in)} rows in → {fmtInt(e.clean.rows_out)} rows out{e.clean.keys?.length ? `; one row per ${e.clean.keys.join(' + ')}` : ''}{e.clean.time ? `; time column ${e.clean.time}` : ''}</p>
          {e.clean.log?.length ? <ul className="log">{e.clean.log.map((x, i) => <li key={i}><Level l={x.level} /> {x.detail}</li>)}</ul> : null}
        </>
      ) : null}
      {e.validation ? (
        <>
          <ul className="log">{e.validation.checks.map((c, i) => <li key={i}><span className={`lvl ${c.status === 'pass' ? 'info' : c.status === 'warn' ? 'warn' : 'error'}`}>{c.status}</span> {c.check}: {c.detail}</li>)}</ul>
          {e.validation.relations?.length ? <p className="notes" style={{ paddingLeft: 0 }}>Formulas found: {e.validation.relations.join('; ')}</p> : null}
        </>
      ) : null}
      {e.load?.version ? <p className="notes" style={{ paddingLeft: 0 }}>Warehouse: +{fmtInt(e.load.inserted)} new, {fmtInt(e.load.updated)} already present/updated → {fmtInt(e.load.rows_after)} rows (version {e.load.version})</p> : null}
      {e.preview_clean?.columns ? <DataTable t={{ columns: e.preview_clean.columns, rows: e.preview_clean.rows, caption: 'After cleaning (first rows)', total_rows: e.preview_clean.rows.length }} max={6} /> : null}
    </div>
  )
}

function RunCard({ summary, tables, open, onToggle, toast, onChanged }) {
  const [run, setRun] = useState(null)
  const live = !TERMINAL.includes(summary.status)
  useEffect(() => {
    if (!open) return undefined
    let stop = false
    const get = () => api.run(summary.id).then((r) => { if (!stop) setRun(r) }).catch(() => {})
    get()
    const t = live ? setInterval(get, 800) : null
    return () => { stop = true; if (t) clearInterval(t) }
  }, [open, summary.id, summary.status]) // eslint-disable-line
  const stages = (open && run ? run.stages : summary.stages) || []
  const total = stages.reduce((a, s) => a + (s.ms || 0), 0)
  return (
    <div className={`run ${summary.status}`}>
      <button className="run-head" onClick={onToggle} aria-expanded={open}>
        <span className="run-file">{summary.file}</span>
        <span className={`rs ${summary.status}`}>{STATUS_LABEL[summary.status] || summary.status}</span>
        <StageTrack stages={stages} compact />
        <span className="run-time">{summary.created?.replace('T', ' ').slice(0, 16)}{total ? ` · ${(total / 1000).toFixed(1)} s` : ''}</span>
      </button>
      {open ? (
        <div className="run-body">
          <StageTrack stages={stages} />
          {run?.status === 'needs_review' ? <ReviewPanel run={run} tables={tables} toast={toast} onDone={() => { onChanged(); api.run(run.id).then(setRun) }} /> : null}
          {run ? (
            <>
              {run.message && run.message !== 'Done' ? <p className={run.status === 'failed' || run.status === 'quarantined' ? 'err' : 'notes'} style={{ paddingLeft: 0 }}>{run.message}</p> : null}
              <div className="stages">
                {run.stages.map((s, i) => (
                  <details key={s.key} className={`stage ${s.status}`} open={s.status === 'failed' || s.status === 'review'}>
                    <summary><b>{i + 1}. {s.name}</b> <span className={`rs ${s.status}`}>{s.status}</span> <span className="stage-sum">{s.summary}</span>{s.ms != null ? <span className="ms">{s.ms} ms</span> : null}</summary>
                    <p className="notes" style={{ paddingLeft: 0 }}>{STAGE_HELP[s.key]}</p>
                    {s.details?.length ? <ul className="log">{s.details.slice(0, 120).map((d, j) => <li key={j}><Level l={d.level} /> <span style={{ whiteSpace: 'pre-wrap' }}>{d.detail}</span></li>)}</ul> : null}
                  </details>
                ))}
              </div>
              {run.tables?.length && run.status !== 'needs_review' ? <><h3 style={{ marginTop: 14 }}>Tables in this file</h3>{run.tables.map((e) => <TableDetail key={e.origin} e={e} />)}</> : null}
            </>
          ) : <p className="notes">Loading…</p>}
        </div>
      ) : null}
    </div>
  )
}

export default function PipelinePage({ toast, onChanged }) {
  const [runs, setRuns] = useState(null)
  const [schema, setSchema] = useState(null)
  const [mode, setMode] = useState('merge')
  const [over, setOver] = useState(false)
  const [open, setOpen] = useState(null)
  const [err, setErr] = useState('')
  const input = useRef(null)
  const prevLive = useRef(0)
  const load = () => api.runs().then((r) => { setRuns(r.runs); setErr('') }).catch((e) => setErr(e.message))
  const loadSchema = () => api.schema().then(setSchema).catch(() => {})
  useEffect(() => { load(); loadSchema() }, [])
  const live = (runs || []).filter((r) => r.status === 'running' || r.status === 'queued').length
  useEffect(() => {
    if (prevLive.current && !live) { onChanged?.(); loadSchema() }
    prevLive.current = live
    if (!live) return undefined
    const t = setInterval(load, 900)
    return () => clearInterval(t)
  }, [live]) // eslint-disable-line
  async function up(files) {
    if (!files?.length) return
    try {
      const r = await api.upload(files, mode)
      toast(`${r.runs.length} file(s) sent to the pipeline`)
      if (r.runs.length === 1) setOpen(r.runs[0].id)
      load()
    } catch (e) { toast(`Upload failed: ${e.message}`) }
  }
  const review = (runs || []).filter((r) => r.status === 'needs_review')
  return (
    <div className="page">
      <h1>Data pipeline</h1>
      <p className="lede">Upload any export from any system. Each file goes through seven checked stages before a single number reaches an answer, dashboard or report. Nothing is guessed silently: every fix is logged and anything uncertain waits for you.</p>
      <div className="pipe-map" aria-label="How the pipeline works">
        {(schema?.stages || []).map((s, i) => <div className="pm" key={s.key}><span className="pm-n">{i + 1}</span><b>{s.name}</b><p>{STAGE_HELP[s.key]}</p></div>)}
      </div>
      <div className={`drop ${over ? 'over' : ''}`} onClick={() => input.current?.click()} onDragOver={(e) => { e.preventDefault(); setOver(true) }}
        onDragLeave={() => setOver(false)} onDrop={(e) => { e.preventDefault(); setOver(false); up(e.dataTransfer.files) }} role="button" tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter') input.current?.click() }} data-testid="dropzone">
        <b>Drop files here or click to choose</b>
        <span>CSV / TSV, Excel (.xlsx, .xls, all sheets), SQLite database, JSON / JSON lines, Parquet, PDF, Word, PowerPoint template. Several files at once is fine.</span>
        <input ref={input} type="file" multiple hidden data-testid="file-input" onChange={(e) => { up(e.target.files); e.target.value = '' }} />
      </div>
      <div className="row" style={{ margin: '10px 0 4px' }}>
        <div className="seg" role="group" aria-label="Load mode">
          <button aria-pressed={mode === 'merge'} onClick={() => setMode('merge')}>Add / update rows</button>
          <button aria-pressed={mode === 'replace'} onClick={() => setMode('replace')}>Replace the whole table</button>
        </div>
        <span className="notes" style={{ margin: 0 }}>{mode === 'merge' ? 'Rows with the same key (e.g. month + area, or an ID) are updated; new ones are added. The same file twice changes nothing.' : 'The matching table is replaced by this file. The previous version is kept for rollback.'}</span>
      </div>
      {err ? <p className="err">{err}</p> : null}
      {review.length ? <p className="callout">{review.length} file(s) need you to confirm something before loading. Open {review.length === 1 ? 'it' : 'them'} below.</p> : null}
      <h2>Runs</h2>
      {runs && !runs.length ? <p className="notes" style={{ paddingLeft: 0 }}>No files processed yet.</p> : null}
      {(runs || []).map((r) => (
        <RunCard key={r.id} summary={r} tables={schema?.tables || []} open={open === r.id} onToggle={() => setOpen(open === r.id ? null : r.id)} toast={toast} onChanged={() => { load(); onChanged?.() }} />
      ))}
    </div>
  )
}
