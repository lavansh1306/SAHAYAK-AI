import React, { useState } from 'react'
import { api, fileUrl } from '../api.js'
import Chart from './Chart.jsx'
import DataTable from './DataTable.jsx'
import Md from './Md.jsx'

function Stamp({ r }) {
  if (r.type === 'refusal') return <span className="stamp warn">Read-only, request declined</span>
  if (r.type === 'out_of_scope') return <span className="stamp warn">Outside the loaded data</span>
  if (r.type === 'clarify') return <span className="stamp warn">Needs one more detail</span>
  if (r.unverified) return <span className="stamp no">Unverified: generated query</span>
  const checks = r.checks || []
  const ok = checks.filter((c) => c.match).length
  if (checks.length && ok < checks.length) return <span className="stamp no">Cross-check mismatch ({ok} of {checks.length})</span>
  if (checks.length) return <span className="stamp ok">Verified: {ok} of {checks.length} cross-checks match</span>
  if (r.type === 'answer') return <span className="stamp ok">Computed from source data</span>
  return null
}

const ROUTE = { rules: 'understood by the rule engine', 'rules (low confidence)': 'best guess by the rule engine', llm: 'understood by the LLM', documents: 'answered from documents', insights: 'automatic checks', actions: 'decision support', 'what-if': 'what-if scenario', 'plan-vs-actual': 'plan vs actual', report: 'report builder', deck: 'deck builder', 'deck-edit': 'deck edit', 'report-edit': 'report edit', alert: 'alert set', audit: 'manual report check', 'fact-check': 'document check', catalogue: 'data catalogue', help: 'help', guardrail: 'stopped by a guardrail', nl2sql: 'answered with a generated query',
  'follow-up': 'follow-up to the last answer', clarify: 'needs clarification' }

export default function AnswerCard({ r, onAsk, onPin }) {
  const [kind, setKind] = useState(null)
  const [vote, setVote] = useState(0)
  const lines = r.answer || []
  const kpis = (r.kpis || []).filter((k) => k.display !== undefined).slice(0, 5)
  const tbl = r.table
  const smallTable = tbl && tbl.rows && tbl.rows.length <= 12
  const cls = r.type === 'refusal' || r.type === 'out_of_scope' ? 'refuse' : r.unverified ? 'unverified' : ''
  const send = (v) => { setVote(v); api.feedback({ id: r.id, rating: v, question: r.question }).catch(() => {}) }
  const extraTables = (r.tables || []).filter(Boolean)
  const hasMore = extraTables.length || (tbl && !smallTable) || (r.checks || []).length || (r.assumptions || []).length || r.sql || (r.citations || []).length || (r.plan_text || []).length

  return (
    <div className={`answer ${cls}`}>
      <span className="node" style={{ top: 6 }} aria-hidden="true" />
      <div className="answer-head">
        <h3>{r.title}</h3>
        <Stamp r={r} />
      </div>
      <div className="answer-body">
        {r.summary ? <p className="summary">{r.summary}<small>In simple words, written by the LLM; every number in it was checked against the computed result.</small></p> : null}
        {kpis.length ? (
          <div className="readouts">
            {kpis.map((k, i) => (
              <div key={i} className={`readout ${k.tone || ''}`}><div className="v">{k.display}</div><div className="l">{k.label}</div></div>
            ))}
          </div>
        ) : null}
        <ul className="lines">
          {lines.map((l, i) => {
            const plain = /^(\d+\.|-)\s/.test(l) || i === 0 && lines.length > 1 && /:$/.test(l)
            return <li key={i} className={plain ? 'plain' : ''}><Md text={l.replace(/^-\s/, '')} /></li>
          })}
        </ul>
        {r.chart ? (
          <>
            {r.chart.type !== 'pie' || (r.chart.series || []).length === 1 ? (
              <div className="chart-switch" role="group" aria-label="Chart type">
                {['bar', 'line', 'pie'].filter((k) => k !== 'pie' || (r.chart.series || []).length === 1).map((k) => (
                  <button key={k} aria-pressed={(kind || r.chart.type) === k} onClick={() => setKind(k)}>{k}</button>))}
              </div>
            ) : null}
            <Chart spec={{ ...r.chart, type: kind || r.chart.type }} />
          </>
        ) : null}
        {(r.files || []).length ? (
          <div className="files">
            {r.files.map((f) => <a key={f.name} className="file-link" href={fileUrl(f.name)} download>Download {f.label}</a>)}
          </div>
        ) : null}
        {r.preview_image ? <img className="preview-img" src={fileUrl(r.preview_image, true)} alt="Infographic preview" /> : null}
        {r.outline ? <ul className="notes">{r.outline.map((o) => <li key={o}>{o}</li>)}</ul> : null}
        {smallTable ? <DataTable t={tbl} /> : null}
        {r.clarify ? (
          <div className="clarify">
            <p style={{ margin: '0 0 8px' }}>{r.clarify.question}</p>
            <div className="chips">{r.clarify.options.map((o) => <button key={o} className="chip" onClick={() => onAsk(o)}>{o}</button>)}</div>
          </div>
        ) : null}
        {hasMore ? (
          <details className="more">
            <summary>Details: how it was computed, checks and full table</summary>
            {(r.plan_text || []).length ? <ul className="notes plan">{r.plan_text.map((p, i) => <li key={i}>{p}</li>)}</ul> : null}
            {tbl && !smallTable ? <DataTable t={tbl} /> : null}
            {extraTables.map((t, i) => <DataTable key={i} t={t} />)}
            {r.sql ? <><p style={{ margin: '10px 0 4px', fontSize: 13.5 }}>Generated SQL (read-only, validated before running):</p><pre className="sql">{r.sql}</pre></> : null}
            {(r.citations || []).map((c, i) => <div className="cite" key={i}>"{c.text}" <br /><small>{c.doc}, {c.locator}</small></div>)}
            {(r.checks || []).length ? (
              <ul className="notes">{r.checks.map((c, i) => <li key={i}>{c.match ? 'Match' : 'MISMATCH'}: {c.label} - {c.matched} of {c.n} value(s) identical in pandas and in an independent SQL query{c.error ? ` (${c.error})` : ''}</li>)}</ul>
            ) : null}
              </details>
        ) : null}
        {(r.assumptions || []).length ? <ul className="notes">{r.assumptions.map((a, i) => <li key={i}>Assumption: {a}</li>)}</ul> : null}
        {(r.notes || []).length ? <ul className="notes">{r.notes.map((n, i) => <li key={i}>{n}</li>)}</ul> : null}
        {(r.followups || []).length ? <div className="chips" style={{ marginTop: 12 }}>{r.followups.map((f) => <button key={f} className="chip" onClick={() => onAsk(f)}>{f}</button>)}</div> : null}
      </div>
      {(r.sources || []).length ? (
        <div className="trace">
          <span className="node" aria-hidden="true" />
          {r.sources.slice(0, 4).map((s, i) => (
            <div className="trace-row" key={i}>
              <b>Source</b> {s.file}{s.origin ? ` [${s.origin}]` : ''}{s.filter ? `, ${s.filter}` : ''}{s.rows_used != null ? `, ${Number(s.rows_used).toLocaleString('en-IN')} rows` : ''}
              {s.formula ? <>, computed as <code>{s.formula}</code></> : null}
            </div>
          ))}
        </div>
      ) : null}
      <div className="foot">
        <span>{ROUTE[r.route?.chosen_by] || ''}</span>
        {r.elapsed_ms != null ? <span>{r.elapsed_ms} ms</span> : null}
        {r.narration ? <span>{r.narration}</span> : null}
        {r.type === 'answer' ? (
          <>
            <button aria-pressed={vote === 1} onClick={() => send(1)} title="Correct and useful">Helpful</button>
            <button aria-pressed={vote === -1} onClick={() => send(-1)} title="Wrong or not useful">Not right</button>
            {onPin && r.intent === 'query' ? <button onClick={() => onPin(r.question, r.title)} title="Keep this question on the Overview page">Pin to Overview</button> : null}
          </>
        ) : null}
      </div>
    </div>
  )
}
