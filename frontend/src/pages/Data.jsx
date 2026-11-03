import React, { useEffect, useState } from 'react'
import { api, downloadUrl, fmtInt } from '../api.js'
import DataTable from '../components/DataTable.jsx'
import { useLoad } from '../hooks.js'

const ROLES = ['measure', 'dimension', 'id', 'time', 'text', 'ignore']
const AGGS = ['sum', 'mean', 'ratio', 'last', 'min', 'max', 'median']
const AGG_HELP = { sum: 'add up', mean: 'average of rows', ratio: 'total / total (weighted)', last: 'latest value (stock, balances)', min: 'smallest', max: 'largest', median: 'middle value' }

function TableDetail({ tkey, toast, onChanged, askInChat }) {
  const { data: t, err, reload } = useLoad(() => api.table(tkey), [tkey])
  const [edits, setEdits] = useState({})
  const [label, setLabel] = useState('')
  const [prev, setPrev] = useState(null)
  const [metric, setMetric] = useState({ name: '', label: '', expr: '', unit: '' })
  useEffect(() => { setEdits({}); setPrev(null); setLabel('') }, [tkey])
  if (err) return <p className="err">{err}</p>
  if (!t) return <p className="notes">Loading…</p>
  const cols = Object.entries(t.columns || {})
  const val = (c, k) => (edits[c] && edits[c][k] !== undefined ? edits[c][k] : t.columns[c][k])
  const setE = (c, k, v) => setEdits({ ...edits, [c]: { ...(edits[c] || {}), [k]: v } })
  const dirty = Object.keys(edits).length > 0 || label
  const save = async () => {
    const columns = {}
    for (const [c, e] of Object.entries(edits)) {
      const x = { ...e }
      if (typeof x.synonyms === 'string') x.synonyms = x.synonyms.split(',').map((s) => s.trim()).filter(Boolean)
      columns[c] = x
    }
    try { await api.editTable(tkey, { label: label || undefined, columns }); toast('Data dictionary saved. Questions now use your changes.'); setEdits({}); setLabel(''); reload(); onChanged?.() } catch (e) { toast(e.message) }
  }
  const act = async (fn, msg) => { try { await fn(); toast(msg); reload(); onChanged?.() } catch (e) { toast(e.message) } }
  const timeCols = cols.filter(([, e]) => e.role === 'time').map(([c]) => c)
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div>
          <label className="field">Table name<input value={label || t.label} onChange={(e) => setLabel(e.target.value)} style={{ minWidth: 260 }} /></label>
          <p className="notes" style={{ paddingLeft: 0 }}><code>{t.key}</code> · {fmtInt(t.rows)} rows · {t.time ? `time column ${t.time} (${t.grain})` : 'no time column'}
            {t.time_range ? ` · ${String(t.time_range[0]).slice(0, 10)} to ${String(t.time_range[1]).slice(0, 10)}` : ''} · quality {Math.round(t.quality || 0)}% · from {(t.sources || []).join(', ')}</p>
        </div>
        <div className="acts">
          <button className="btn ghost small" onClick={async () => setPrev(prev ? null : await api.preview(tkey))}>{prev ? 'Hide rows' : 'Show rows'}</button>
          <a className="btn ghost small" href={downloadUrl(tkey)} download>Clean CSV</a>
          {(t.versions || []).length > 1 ? <button className="btn ghost small" onClick={() => act(() => api.rollback(tkey), 'Rolled back to the previous version')}>Roll back last load</button> : null}
          <button className="btn ghost small danger" onClick={() => act(() => api.deleteTable(tkey), `${t.label} removed`)}>Remove table</button>
        </div>
      </div>
      {prev?.columns ? <DataTable t={{ columns: prev.columns, rows: prev.rows, caption: `First ${prev.rows.length} of ${fmtInt(prev.total)} rows (after cleaning)`, total_rows: prev.rows.length }} max={30} /> : null}
      <h3 style={{ marginTop: 14 }}>Columns</h3>
      <p className="notes" style={{ paddingLeft: 0 }}>This is what the app understood. Rename columns, add words people use for them (synonyms), change how a number adds up, or hide a column. Saved changes apply to every question, report and alert.</p>
      <div className="tbl-wrap">
        <table className="tbl dict">
          <thead><tr><th>Column</th><th>Label</th><th>Type</th><th>Role</th><th>Adds up by</th><th>Unit</th><th>Also called (comma separated)</th><th>Hide</th><th>Values</th></tr></thead>
          <tbody>
            {cols.map(([c, e]) => (
              <tr key={c} className={e.hidden ? 'muted' : ''}>
                <td><code>{c}</code></td>
                <td><input aria-label={`Label of ${c}`} value={val(c, 'label') || ''} onChange={(ev) => setE(c, 'label', ev.target.value)} /></td>
                <td>{e.type}</td>
                <td><select aria-label={`Role of ${c}`} value={val(c, 'role')} onChange={(ev) => setE(c, 'role', ev.target.value)}>{ROLES.map((r) => <option key={r}>{r}</option>)}</select></td>
                <td>{val(c, 'role') === 'measure' ? (
                  <select aria-label={`Aggregation of ${c}`} value={val(c, 'agg') || 'sum'} title={AGG_HELP[val(c, 'agg') || 'sum']} onChange={(ev) => setE(c, 'agg', ev.target.value)}>
                    {AGGS.filter((a) => a !== 'ratio' || e.ratio).map((a) => <option key={a} value={a}>{a} - {AGG_HELP[a]}</option>)}
                  </select>) : '-'}</td>
                <td><input aria-label={`Unit of ${c}`} style={{ width: 80 }} value={val(c, 'unit') || ''} onChange={(ev) => setE(c, 'unit', ev.target.value)} /></td>
                <td><input aria-label={`Synonyms of ${c}`} value={typeof val(c, 'synonyms') === 'string' ? val(c, 'synonyms') : (val(c, 'synonyms') || []).join(', ')} onChange={(ev) => setE(c, 'synonyms', ev.target.value)} /></td>
                <td><input type="checkbox" aria-label={`Hide ${c}`} checked={!!val(c, 'hidden')} onChange={(ev) => setE(c, 'hidden', ev.target.checked)} /></td>
                <td className="ex">{e.type === 'number' ? `${e.min ?? ''} to ${e.max ?? ''}` : e.top ? e.top.slice(0, 4).map((x) => x[0]).join(' · ') + (e.card > 4 ? ` (+${e.card - 4})` : '') : e.min ? `${String(e.min).slice(0, 10)} to ${String(e.max).slice(0, 10)}` : ''}
                  {e.ratio ? <div><small>= total {e.ratio.num} / total {e.ratio.den}{e.ratio.factor !== 1 ? ` x ${e.ratio.factor}` : ''}</small></div> : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <button className="btn" disabled={!dirty} onClick={save}>Save changes</button>
        {timeCols.length > 1 ? <label className="field" style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>Main time column
          <select value={t.time || ''} onChange={(ev) => act(() => api.editTable(tkey, { time: ev.target.value, columns: {} }), 'Main time column changed')}>{timeCols.map((c) => <option key={c}>{c}</option>)}</select></label> : null}
        <button className="btn ghost" onClick={() => askInChat(`What data is in ${t.label}?`)}>Ask about this table</button>
      </div>
      {(t.relations || []).length ? <><h3 style={{ marginTop: 16 }}>Formulas found in the data</h3><ul className="notes">{t.relations.map((r, i) => <li key={i}>{r.formula}{r.violations ? ` - ${r.violations} row(s) do not add up` : ' - holds for every row'}</li>)}</ul></> : null}
      <h3 style={{ marginTop: 16 }}>Your own formulas</h3>
      <p className="notes" style={{ paddingLeft: 0 }}>Define a number that is not a column, e.g. <code>sum(paid) / sum(billed) * 100</code>. Allowed: sum, avg, count, count_distinct, min, max, median, last, + - * / and numbers. It can then be asked like any column.</p>
      {Object.entries(t.metrics || {}).length ? <ul className="notes">{Object.entries(t.metrics).map(([k, m]) => <li key={k}><b>{m.label}</b> = <code>{m.expr}</code> {m.unit} <button className="btn ghost small danger" onClick={() => act(() => api.delMetric(tkey, k), 'Formula removed')}>Remove</button></li>)}</ul> : null}
      <div className="row">
        <label className="field">Short name<input value={metric.name} onChange={(e) => setMetric({ ...metric, name: e.target.value })} style={{ width: 130 }} /></label>
        <label className="field">Label<input value={metric.label} onChange={(e) => setMetric({ ...metric, label: e.target.value })} style={{ width: 180 }} /></label>
        <label className="field">Formula<input value={metric.expr} onChange={(e) => setMetric({ ...metric, expr: e.target.value })} style={{ minWidth: 280 }} /></label>
        <label className="field">Unit<input value={metric.unit} onChange={(e) => setMetric({ ...metric, unit: e.target.value })} style={{ width: 70 }} /></label>
        <button className="btn ghost" disabled={!metric.name || !metric.expr} onClick={() => act(() => api.addMetric(tkey, { ...metric, label: metric.label || metric.name, unit_kind: metric.unit === '%' ? 'percent' : '' }), 'Formula added').then(() => setMetric({ name: '', label: '', expr: '', unit: '' }))}>Add formula</button>
      </div>
      <h3 style={{ marginTop: 16 }}>Checks</h3>
      <ul className="notes">{(t.validation || []).map((c, i) => <li key={i}><span className={`sev ${c.status === 'pass' ? 'ok' : c.status === 'warn' ? 'warn' : 'Red'}`}>{c.status}</span> {c.check}: {c.detail}</li>)}</ul>
      {(t.versions || []).length ? <p className="notes">Load history: {t.versions.map((v) => `v${v.v} ${String(v.ts || '').replace('T', ' ').slice(0, 16)} ${v.source || ''} (${v.mode || ''}, ${fmtInt(v.rows_after)} rows)`).join(' | ')}</p> : null}
    </div>
  )
}

export default function DataPage({ toast, onChanged, go, version, askInChat }) {
  const { data, err, reload } = useLoad(api.tables, [version])
  const [sel, setSel] = useState(null)
  const [confirm, setConfirm] = useState('')
  useEffect(() => { if (data?.tables?.length && !data.tables.some((t) => t.key === sel)) setSel(data.tables[0].key) }, [data]) // eslint-disable-line
  const act = async (fn, msg) => { try { await fn(); toast(msg); reload(); onChanged?.() } catch (e) { toast(e.message) } }
  return (
    <div className="page wide">
      <h1>Data dictionary</h1>
      <p className="lede">Every table in the warehouse, what each column means, how numbers add up, links between tables, quality checks and load history. Everything else in the app reads from here.</p>
      {err ? <p className="err">{err}</p> : null}
      {data?.empty ? (
        <div className="panel"><h3>No data loaded yet</h3><p className="notes" style={{ paddingLeft: 0 }}>Upload files on the Pipeline page; the dictionary fills in automatically.</p><button className="btn" onClick={() => go('pipeline')}>Go to Pipeline</button></div>
      ) : null}
      {data && !data.empty ? (
        <div className="dict-layout">
          <aside className="dict-list">
            {data.tables.map((t) => (
              <button key={t.key} className={`dict-item ${sel === t.key ? 'on' : ''}`} onClick={() => setSel(t.key)}>
                <b>{t.label}</b><span>{fmtInt(t.rows)} rows · {t.measures.length} numbers · {t.dims.length} groupings</span>
                {t.status !== 'ok' ? <span className={`sev ${t.status}`}>{t.status === 'warn' ? 'warnings' : t.status}</span> : null}
              </button>
            ))}
            {data.links?.length ? <div className="notes"><b>Links</b><ul>{data.links.map((l, i) => <li key={i}>{l.text}</li>)}</ul></div> : null}
            <div className="notes"><b>Documents</b>{data.documents.length ? <ul>{data.documents.map((d) => <li key={d.name}>{d.name} <button className="linkish danger" onClick={() => act(() => api.deleteStored('documents', d.name), 'Removed')}>remove</button></li>)}</ul> : <p>None</p>}</div>
            <div className="notes"><b>Slide templates</b>{data.templates.length ? <ul>{data.templates.map((d) => <li key={d}>{d} <button className="linkish danger" onClick={() => act(() => api.deleteStored('templates', d), 'Removed')}>remove</button></li>)}</ul> : <p>None (upload a .pptx/.potx on Pipeline)</p>}</div>
          </aside>
          <div className="dict-main">{sel ? <TableDetail tkey={sel} toast={toast} onChanged={() => { reload(); onChanged?.() }} askInChat={askInChat} /> : null}</div>
        </div>
      ) : null}
      {data && !data.empty ? (
        <div className="panel danger-zone">
          <h3>Delete all data</h3>
          <p className="notes" style={{ paddingLeft: 0 }}>Removes every table, document, run, alert and generated file. Settings are kept. Type <b>DELETE ALL DATA</b> to confirm.</p>
          <div className="row">
            <input className="inp" value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-label="Type DELETE ALL DATA to confirm" />
            <button className="btn danger" disabled={confirm !== 'DELETE ALL DATA'} onClick={() => act(() => api.reset(confirm), 'All data removed. The app is empty again.').then(() => setConfirm(''))}>Delete everything</button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
