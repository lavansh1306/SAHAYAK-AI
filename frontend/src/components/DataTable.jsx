import React from 'react'
import Md from './Md.jsx'

const isNum = (v) => typeof v === 'number' || (typeof v === 'string' && /^[-+]?[\d,]+(\.\d+)?%?$/.test(v.trim()))
const SEV = new Set(['Red', 'Amber', 'OK', 'high', 'medium', 'low', 'ok', 'warn', 'quarantined'])

export default function DataTable({ t, max = 60 }) {
  if (!t || !t.columns) return null
  const rows = (t.rows || []).slice(0, max)
  return (
    <div className="tbl-wrap">
      <table className="tbl">
        {t.caption ? <caption>{t.caption}</caption> : null}
        <thead><tr>{t.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>{r.map((v, j) => (
              <td key={j} className={isNum(v) ? 'n' : ''}>
                {SEV.has(v) ? <span className={`sev ${v}`}>{v}</span> : typeof v === 'number' ? v.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : <Md text={v == null ? '' : String(v)} />}
              </td>
            ))}</tr>
          ))}
        </tbody>
      </table>
      {t.total_rows > rows.length ? <div style={{ padding: '6px 10px', fontSize: 12.5, color: 'var(--muted)' }}>Showing {rows.length} of {t.total_rows} rows</div> : null}
    </div>
  )
}
