import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api, fileUrl } from '../api.js'
import AnswerCard from '../components/AnswerCard.jsx'
import Chart from '../components/Chart.jsx'
import DataTable from '../components/DataTable.jsx'
import Md from '../components/Md.jsx'
import { useLoad } from '../hooks.js'

function EmptyData({ go, what }) {
  return (
    <div className="panel">
      <h3>No data loaded yet</h3>
      <p className="notes" style={{ paddingLeft: 0 }}>Upload your files on the Pipeline page. {what} fills in from your own data only.</p>
      <button className="btn" onClick={() => go('pipeline')}>Go to Pipeline</button>
    </div>
  )
}

// -------------------------------------------------------------------------------------- Insights
export function InsightsPage({ askInChat, go, version }) {
  const [scope, setScope] = useState('all')
  const [open, setOpen] = useState(null)
  const { data, err, busy } = useLoad(() => api.insights(scope), [scope, version])
  if (data?.empty) return <div className="page"><h1>Insights</h1><EmptyData go={go} what="Insights" /></div>
  return (
    <div className="page">
      <h1>Insights</h1>
      <p className="lede">Automatic checks on every table: trends, seasonal patterns, step changes, abnormal days or hours, items far from their peers, spikes in counts, gaps against plan, and data-quality problems. Each finding shows its numbers, its chart and its source. Nothing is written by the LLM.</p>
      <div className="row">
        <div className="seg" role="group" aria-label="Scope">
          {[['all', 'Everything'], ['anomalies', 'Anomalies'], ['trends', 'Trends'], ['quality', 'Data quality']].map(([k, l]) => <button key={k} aria-pressed={scope === k} onClick={() => setScope(k)}>{l}</button>)}
        </div>
        <button className="btn ghost" onClick={() => askInChat('What should we act on first?')}>Rank what to act on first</button>
      </div>
      {err ? <p className="err">{err}</p> : null}
      {busy && !data ? <p className="notes">Scanning all tables…</p> : null}
      {data ? (
        <div className="panel" style={{ marginTop: 16 }}>
          {!data.findings.length ? <p className="notes">Nothing stands out in this scope.</p> : null}
          {data.findings.map((f) => (
            <div className={`finding ${f.severity}`} key={f.id}>
              <h3>{f.title}</h3>
              {f.detail.map((d, i) => <div key={i}><Md text={d} /></div>)}
              <div className="meta"><span className={`sev ${f.severity}`}>{f.severity}</span> {f.category.replace(/_/g, ' ')} · {f.table_label}{f.period ? ` · ${f.period}` : ''}
                {f.chart ? <button className="linkish" onClick={() => setOpen(open === f.id ? null : f.id)}>{open === f.id ? 'hide chart' : 'show chart'}</button> : null}</div>
              {open === f.id && f.chart ? <Chart spec={{ ...f.chart, title: f.title }} height={260} /> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------------------- Alerts
export function AlertsPage({ toast, onChange, go, version }) {
  const { data, err, reload } = useLoad(api.alerts, [version])
  const cat = useLoad(api.catalog, [version])
  const det = useRef({})
  const [form, setForm] = useState({ table: '', measure: '', group: '', grain: 'month', op: '>', amber: '', red: '', owner: '', window: 'latest' })
  const [cols, setCols] = useState(null)
  const tables = cat.data?.tables || []
  useEffect(() => { if (!form.table && tables.length) setForm((f) => ({ ...f, table: tables[0].key })) }, [cat.data]) // eslint-disable-line
  useEffect(() => {
    if (!form.table) return
    if (det.current[form.table]) { setCols(det.current[form.table]); return }
    api.table(form.table).then((t) => { det.current[form.table] = t; setCols(t) }).catch(() => {})
  }, [form.table])
  const measures = cols ? Object.entries(cols.columns).filter(([, e]) => e.role === 'measure' && !e.hidden).map(([c, e]) => [c, e.label]).concat(Object.entries(cols.metrics || {}).map(([k, m]) => [k, m.label])) : []
  const dims = cols ? Object.entries(cols.columns).filter(([, e]) => e.role === 'dimension' && !e.hidden).map(([c, e]) => [c, e.label]) : []
  useEffect(() => { if (cols && !measures.some(([c]) => c === form.measure)) setForm((f) => ({ ...f, measure: measures[0]?.[0] || '*' })) }, [cols]) // eslint-disable-line
  async function add(e) {
    e.preventDefault()
    try {
      const r = await api.addRule({ ...form, measure: form.measure || '*', agg: form.measure === '*' ? 'count' : null, amber: form.amber === '' ? null : Number(form.amber),
        red: form.red === '' ? null : Number(form.red), grain: cols?.time ? form.grain : 'none' })
      toast(`Alert saved. Breached now: ${r.now.length}.`)
      reload(); onChange?.()
    } catch (er) { toast(er.message) }
  }
  if (cat.data?.empty) return <div className="page"><h1>Alerts</h1><EmptyData go={go} what="Alerts" /></div>
  return (
    <div className="page">
      <h1>Alerts</h1>
      <p className="lede">Set a limit on any number in your data, per item and per period. Alerts are checked after every upload and every 15 minutes; new breaches become notifications. Automatic alerts come from serious findings. You can also type "alert me when ... goes above ..." in Ask.</p>
      <form className="panel" onSubmit={add}>
        <h3>New alert</h3>
        <div className="row">
          <label className="field">Table<select value={form.table} onChange={(e) => setForm({ ...form, table: e.target.value, measure: '', group: '' })}>{tables.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}</select></label>
          <label className="field">Number<select value={form.measure} onChange={(e) => setForm({ ...form, measure: e.target.value })}>{measures.map(([c, l]) => <option key={c} value={c}>{l}</option>)}<option value="*">Number of records</option></select></label>
          <label className="field">For each<select value={form.group} onChange={(e) => setForm({ ...form, group: e.target.value })}><option value="">(whole table)</option>{dims.map(([c, l]) => <option key={c} value={c}>{l}</option>)}</select></label>
          {cols?.time ? <label className="field">Per<select value={form.grain} onChange={(e) => setForm({ ...form, grain: e.target.value })}>{['day', 'week', 'month', 'quarter', 'year'].map((g) => <option key={g}>{g}</option>)}</select></label> : null}
          <label className="field">When it is<select value={form.op} onChange={(e) => setForm({ ...form, op: e.target.value })}><option value=">">above</option><option value="<">below</option></select></label>
          <label className="field">Amber limit<input type="number" step="any" value={form.amber} onChange={(e) => setForm({ ...form, amber: e.target.value })} style={{ width: 100 }} /></label>
          <label className="field">Red limit<input type="number" step="any" value={form.red} onChange={(e) => setForm({ ...form, red: e.target.value })} style={{ width: 100 }} /></label>
          <label className="field">Owner<input value={form.owner} onChange={(e) => setForm({ ...form, owner: e.target.value })} style={{ width: 130 }} /></label>
          <button className="btn" type="submit" disabled={form.amber === '' && form.red === ''}>Save alert</button>
        </div>
      </form>
      {err ? <p className="err">{err}</p> : null}
      {data ? (
        <>
          <h2>Alerts now</h2>
          {data.alerts.length ? <DataTable t={{ columns: ['Severity', 'Alert', 'Item', 'Period', 'Value', 'Owner', 'Evidence'], rows: data.alerts.map((a) => [a.severity, a.alert, a.entity, String(a.period), a.value, a.owner, a.evidence]), caption: `${data.alerts.length} alerts`, total_rows: data.alerts.length }} max={100} />
            : <p className="notes" style={{ paddingLeft: 0 }}>Nothing is breached.</p>}
          <div className="split" style={{ marginTop: 16 }}>
            <div className="panel"><h3>Your alert rules</h3>{data.rules.length ? <ul className="notes">{data.rules.map((r) => (
              <li key={r.id}>{r.name} (amber {r.amber ?? '-'}, red {r.red ?? '-'}{r.owner ? `, owner ${r.owner}` : ''}) <button className="btn ghost small danger" onClick={async () => { await api.delRule(r.id); reload(); onChange?.() }}>Delete</button></li>))}</ul>
              : <p className="notes">No rules yet.</p>}</div>
            <div className="panel"><h3>Notifications</h3>{data.notifications.length ? <ul className="notes">{data.notifications.slice().reverse().slice(0, 30).map((n) => <li key={n.key} style={{ fontWeight: n.read ? 400 : 600 }}>{n.ts.replace('T', ' ')} - {n.message}</li>)}</ul>
              : <p className="notes">Nothing triggered yet.</p>}
              {data.notifications.some((n) => !n.read) ? <button className="btn ghost small" onClick={async () => { await api.readNotes(); reload(); onChange?.() }}>Mark all read</button> : null}</div>
          </div>
        </>
      ) : null}
    </div>
  )
}

// --------------------------------------------------------------------------------------- Outputs
function monthsOf(tables) {
  const out = new Set()
  for (const t of tables) {
    if (!t.time_range) continue
    const a = String(t.time_range[0]).slice(0, 7), b = String(t.time_range[1]).slice(0, 7)
    if (!/^\d{4}-\d{2}$/.test(a) || !/^\d{4}-\d{2}$/.test(b)) continue
    let [y, m] = a.split('-').map(Number)
    const [y2, m2] = b.split('-').map(Number)
    let guard = 0
    while ((y < y2 || (y === y2 && m <= m2)) && guard++ < 600) { out.add(`${y}-${String(m).padStart(2, '0')}`); m += 1; if (m > 12) { m = 1; y += 1 } }
  }
  return [...out].sort().reverse()
}

export function OutputsPage({ toast, go, version }) {
  const files = useLoad(api.files)
  const cat = useLoad(api.catalog, [version])
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState('')
  const [rep, setRep] = useState({ tables: [], period_end: '', unit: '', sections: '', formats: { pdf: true, docx: true } })
  const [deck, setDeck] = useState({ topic: '', slides: 8, use_template: false })
  const auditIn = useRef(null)
  const tables = cat.data?.tables || []
  const months = useMemo(() => monthsOf(rep.tables.length ? tables.filter((t) => rep.tables.includes(t.key)) : tables), [cat.data, rep.tables]) // eslint-disable-line
  async function run(label, fn) {
    setBusy(label); setRes(null)
    try { setRes(await fn()); files.reload() } catch (e) { toast(e.message) } finally { setBusy('') }
  }
  if (cat.data?.empty) return <div className="page"><h1>Reports and decks</h1><EmptyData go={go} what="Reports and decks" /></div>
  const tog = (k) => setRep({ ...rep, tables: rep.tables.includes(k) ? rep.tables.filter((x) => x !== k) : [...rep.tables, k] })
  return (
    <div className="page">
      <h1>Reports and decks</h1>
      <p className="lede">Build documents from the same computed, cross-checked numbers used in Ask. You can also type requests in Ask, e.g. "prepare the monthly MIS report", "redo it in crore and add a section on complaints by category", or "make 5 slides on ...".</p>
      <div className="split">
        <div className="panel">
          <h3>Report (PDF and Word)</h3>
          <p className="notes" style={{ paddingLeft: 0 }}>Tables to include (none ticked = the most important ones):</p>
          <div className="chips">{tables.map((t) => <label key={t.key} className={`chip ${rep.tables.includes(t.key) ? 'on' : ''}`}><input type="checkbox" checked={rep.tables.includes(t.key)} onChange={() => tog(t.key)} /> {t.label}</label>)}</div>
          <div className="row" style={{ marginTop: 10 }}>
            <label className="field">Reporting month<select value={rep.period_end} onChange={(e) => setRep({ ...rep, period_end: e.target.value })}><option value="">Latest in the data</option>{months.map((m) => <option key={m}>{m}</option>)}</select></label>
            <label className="field">Money in<select value={rep.unit} onChange={(e) => setRep({ ...rep, unit: e.target.value })}><option value="">as in the data</option><option value="lakh">Rs lakh</option><option value="crore">Rs crore</option></select></label>
            <label className="field" style={{ flexDirection: 'row', gap: 6, alignItems: 'center' }}><input type="checkbox" checked={rep.formats.pdf} onChange={(e) => setRep({ ...rep, formats: { ...rep.formats, pdf: e.target.checked } })} />PDF</label>
            <label className="field" style={{ flexDirection: 'row', gap: 6, alignItems: 'center' }}><input type="checkbox" checked={rep.formats.docx} onChange={(e) => setRep({ ...rep, formats: { ...rep.formats, docx: e.target.checked } })} />Word</label>
          </div>
          <label className="field" style={{ marginTop: 10 }}>Extra sections - one question per line (optional)
            <textarea rows={3} value={rep.sections} onChange={(e) => setRep({ ...rep, sections: e.target.value })} placeholder="e.g. complaints by category this year" />
          </label>
          <div className="row" style={{ marginTop: 10 }}>
            <button className="btn" disabled={!!busy} onClick={() => run('report', () => api.report({ tables: rep.tables, period_end: rep.period_end || null, unit: rep.unit || null,
              sections: rep.sections.split('\n').map((s) => s.trim()).filter(Boolean), formats: Object.entries(rep.formats).filter(([, v]) => v).map(([k]) => k) }))}>Build report</button>
            <button className="btn ghost" disabled={!!busy} onClick={() => run('info', () => api.report({ kind: 'infographic' }))}>One-page infographic</button>
          </div>
        </div>
        <div className="panel">
          <h3>PowerPoint deck</h3>
          <p className="notes" style={{ paddingLeft: 0 }}>Leave the topic empty for a business review of everything loaded, or name a topic (a number, an item, a period).</p>
          <label className="field">Topic<input value={deck.topic} onChange={(e) => setDeck({ ...deck, topic: e.target.value })} placeholder="e.g. downtime at a station in August" /></label>
          <div className="row" style={{ marginTop: 10 }}>
            <label className="field">Slides<input type="number" min={3} max={20} value={deck.slides} onChange={(e) => setDeck({ ...deck, slides: Number(e.target.value) })} style={{ width: 80 }} /></label>
            <label className="field" style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}><input type="checkbox" checked={deck.use_template} onChange={(e) => setDeck({ ...deck, use_template: e.target.checked })} />Use the uploaded template{cat.data?.templates?.length ? '' : ' (none uploaded)'}</label>
            <button className="btn" disabled={!!busy} onClick={() => run('deck', () => api.deck(deck))}>Build deck</button>
          </div>
          <h3 style={{ marginTop: 18 }}>Check a manual report for errors</h3>
          <p className="notes" style={{ paddingLeft: 0 }}>Upload an Excel summary someone prepared by hand. Every number is recomputed from the loaded data; wrong cells are explained and highlighted in a returned copy.</p>
          <button className="btn ghost" disabled={!!busy} onClick={() => auditIn.current?.click()}>Upload and check</button>
          <input ref={auditIn} type="file" accept=".xlsx,.xlsm" hidden onChange={(e) => { const f = e.target.files[0]; e.target.value = ''; if (f) run('audit', () => api.audit(f)) }} />
        </div>
      </div>
      {busy ? <div className="thinking"><span className="flow" aria-hidden="true" />Building</div> : null}
      {res ? <AnswerCard r={{ ...res, type: res.type || 'answer' }} onAsk={() => {}} /> : null}
      <h2>Generated files</h2>
      {files.data?.files?.length ? (
        <div className="tbl-wrap"><table className="tbl"><thead><tr><th>File</th><th>Created</th><th>Size</th><th /></tr></thead><tbody>
          {files.data.files.filter((f) => !f.name.startsWith('_')).slice(0, 60).map((f) => <tr key={f.name}><td>{f.name}</td><td>{f.modified.replace('T', ' ')}</td><td className="n">{(f.size / 1024).toFixed(0)} KB</td>
            <td><a className="file-link" href={fileUrl(f.name)} download>Download</a></td></tr>)}
        </tbody></table></div>
      ) : <p className="notes">Nothing generated yet.</p>}
    </div>
  )
}

// -------------------------------------------------------------------------------------- Settings
export function SettingsPage({ health, refresh, toast, onChanged }) {
  const s = useLoad(api.settings)
  const [form, setForm] = useState(null)
  const [test, setTest] = useState(null)
  useEffect(() => { if (s.data) setForm(s.data) }, [s.data])
  const l = health?.llm
  const save = async () => {
    try { await api.saveSettings(form); toast('Settings saved'); refresh(); onChanged?.() } catch (e) { toast(e.message) }
  }
  const P = (k, v) => setForm({ ...form, pipeline: { ...form.pipeline, [k]: Number(v) } })
  return (
    <div className="page">
      <h1>Settings</h1>
      {form ? (
        <div className="panel">
          <h3>Organisation and numbers</h3>
          <div className="row">
            <label className="field">Company name<input value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} style={{ minWidth: 240 }} /></label>
            <label className="field">Short name<input value={form.company_short} onChange={(e) => setForm({ ...form, company_short: e.target.value })} style={{ width: 120 }} /></label>
            <label className="field">Financial year starts in
              <select value={form.fiscal_year_start_month} onChange={(e) => setForm({ ...form, fiscal_year_start_month: Number(e.target.value) })}>
                {['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'].map((m, i) => <option key={m} value={i + 1}>{m}</option>)}
              </select></label>
            <label className="field">Number style<select value={form.number_style} onChange={(e) => setForm({ ...form, number_style: e.target.value })}><option value="indian">Indian (12,34,567)</option><option value="international">International (1,234,567)</option></select></label>
          </div>
          <h3 style={{ marginTop: 16 }}>Pipeline strictness</h3>
          <div className="row">
            <label className="field">Ask me when confidence is below<input type="number" step="0.05" min="0.5" max="1" value={form.pipeline.auto_accept_confidence} onChange={(e) => P('auto_accept_confidence', e.target.value)} style={{ width: 90 }} /></label>
            <label className="field">Join an existing table at column match of<input type="number" step="0.05" min="0.5" max="1" value={form.pipeline.same_table_similarity} onChange={(e) => P('same_table_similarity', e.target.value)} style={{ width: 90 }} /></label>
            <label className="field">Quarantine when unreadable time values exceed<input type="number" step="0.05" min="0" max="1" value={form.pipeline.max_invalid_share} onChange={(e) => P('max_invalid_share', e.target.value)} style={{ width: 90 }} /></label>
          </div>
          <button className="btn" style={{ marginTop: 12 }} onClick={save}>Save settings</button>
        </div>
      ) : <p className="notes">Loading…</p>}
      <div className="panel">
        <h3>Language model (OpenRouter)</h3>
        <p className="notes" style={{ paddingLeft: 0 }}>The model only helps understand questions and write a one-line summary. All numbers come from code, so the app keeps working (rule-based) when the free model is busy or no key is set.</p>
        {l ? (
          <ul className="notes" style={{ paddingLeft: 16 }}>
            <li>Model: {l.model}</li>
            <li>API key: {l.key_present ? 'set' : 'not set - add OPENROUTER_API_KEY to the .env file in the project folder and restart'}</li>
            <li>Status: {l.enabled ? (l.circuit_open ? 'paused after a rate limit; will retry shortly' : 'ready') : 'off (rule-based mode)'}</li>
            {l.last_model ? <li>Last model used by the free router: {l.last_model}</li> : null}
            <li>Calls so far: {l.calls}; answers served from cache: {l.cache_hits}</li>
            {l.last_error ? <li>Last error: {l.last_error}</li> : null}
          </ul>
        ) : <p className="notes">Loading…</p>}
        <button className="btn ghost" onClick={async () => { setTest('Testing…'); try { const r = await api.llmTest(); setTest(r.ok ? `Model replied: ${r.reply}` : `No reply. ${r.status.last_error || 'Check the key.'}`) } catch (e) { setTest(e.message) } refresh() }}>Test the model</button>
        {test ? <p>{test}</p> : null}
      </div>
    </div>
  )
}
