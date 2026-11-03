import React, { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import Ask from './pages/Ask.jsx'
import DataPage from './pages/Data.jsx'
import Overview from './pages/Overview.jsx'
import PipelinePage from './pages/Pipeline.jsx'
import { AlertsPage, InsightsPage, OutputsPage, SettingsPage } from './pages/Pages.jsx'

const I = {
  overview: <><rect x="3" y="3" width="7" height="8" rx="1" /><rect x="14" y="3" width="7" height="5" rx="1" /><rect x="14" y="12" width="7" height="9" rx="1" /><rect x="3" y="15" width="7" height="6" rx="1" /></>,
  pipeline: <><path d="M2 8h6v8H2zM16 8h6v8h-6z" /><path d="M8 12h8" /><circle cx="12" cy="12" r="2" /></>,
  ask: <path d="M4 5h16v11H8l-4 4z" />,
  data: <><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3" /></>,
  insights: <path d="M3 17l5-6 4 3 6-8 3 4" />,
  alerts: <><path d="M12 3l9 16H3z" /><path d="M12 10v4M12 17h.01" /></>,
  outputs: <><path d="M6 3h9l4 4v14H6z" /><path d="M14 3v5h5M9 13h7M9 17h5" /></>,
  status: <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2" /></>,
}
const NAV = [['overview', 'Overview'], ['pipeline', 'Pipeline'], ['ask', 'Ask'], ['data', 'Data dictionary'], ['insights', 'Insights'], ['alerts', 'Alerts'],
  ['outputs', 'Reports and decks'], ['status', 'Settings']]

export default function App() {
  const [page, setPage] = useState('overview')
  const [health, setHealth] = useState(null)
  const [unread, setUnread] = useState(0)
  const [pipeAttn, setPipeAttn] = useState(0)
  const [toastMsg, setToastMsg] = useState('')
  const [pending, setPending] = useState(null)
  const [version, setVersion] = useState(0)
  const toastT = useRef(null)
  const refresh = () => {
    api.health().then(setHealth).catch(() => setHealth(null))
    api.alerts().then((a) => setUnread((a.notifications || []).filter((n) => !n.read).length)).catch(() => {})
    api.runs().then((r) => setPipeAttn(r.runs.filter((x) => ['needs_review', 'running', 'queued'].includes(x.status)).length)).catch(() => {})
  }
  const dataChanged = () => { setVersion((v) => v + 1); refresh() }
  useEffect(() => { refresh(); const t = setInterval(refresh, 30000); return () => clearInterval(t) }, [])
  const toast = (m) => { setToastMsg(m); clearTimeout(toastT.current); toastT.current = setTimeout(() => setToastMsg(''), 4500) }
  const askInChat = (q) => { setPending(q); setPage('ask') }
  const llmOn = health?.llm?.enabled && !health?.llm?.circuit_open

  return (
    <div className="shell">
      <nav className="rail" aria-label="Main">
        <div className="brand">
          <svg width="30" height="30" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="6" fill="#174b55" /><path d="M16 6c4 5 6 8 6 11a6 6 0 0 1-12 0c0-2 1-4 3-6 0 2 1 3 2 3 0-3 0-5 1-8z" fill="#F2B705" /></svg>
          <div><b>SAHAYAK-AI</b><span>{health?.company || 'Decision support'}</span></div>
        </div>
        {NAV.map(([k, label]) => (
          <button key={k} className="nav-btn" aria-current={page === k ? 'page' : undefined} onClick={() => setPage(k)}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{I[k]}</svg>
            {label}
            {k === 'alerts' && unread ? <span className="badge" aria-label={`${unread} unread notifications`}>{unread}</span> : null}
            {k === 'pipeline' && pipeAttn ? <span className="badge" aria-label={`${pipeAttn} runs active or waiting`}>{pipeAttn}</span> : null}
          </button>
        ))}
        <div className="rail-foot">
          {health ? (
            <>
              <div><span className="dot" style={{ background: health.empty ? '#f2b705' : '#5fd39a' }} />{health.empty ? 'No data loaded yet' : `${health.tables} tables, ${health.documents} documents`}</div>
              <div><span className="dot" style={{ background: llmOn ? '#5fd39a' : '#f2b705' }} />{llmOn ? `LLM: ${health.llm.model}` : 'LLM off: rule-based mode'}</div>
            </>
          ) : <div><span className="dot" style={{ background: '#e5484d' }} />Backend not reachable</div>}
        </div>
      </nav>
      <main className="main">
        {page === 'overview' ? <Overview go={setPage} askInChat={askInChat} version={version} toast={toast} /> : null}
        {page === 'pipeline' ? <PipelinePage toast={toast} onChanged={dataChanged} /> : null}
        <div style={{ display: page === 'ask' ? 'block' : 'none', height: '100%' }}>
          <Ask pending={pending} clearPending={() => setPending(null)} version={version} empty={health?.empty} go={setPage} onChanged={dataChanged} toast={toast} />
        </div>
        {page === 'data' ? <DataPage toast={toast} onChanged={dataChanged} go={setPage} version={version} askInChat={askInChat} /> : null}
        {page === 'insights' ? <InsightsPage askInChat={askInChat} go={setPage} version={version} /> : null}
        {page === 'alerts' ? <AlertsPage toast={toast} onChange={refresh} go={setPage} version={version} /> : null}
        {page === 'outputs' ? <OutputsPage toast={toast} go={setPage} version={version} /> : null}
        {page === 'status' ? <SettingsPage health={health} refresh={refresh} toast={toast} onChanged={dataChanged} /> : null}
      </main>
      {toastMsg ? <div className="toast" role="status">{toastMsg}</div> : null}
    </div>
  )
}
