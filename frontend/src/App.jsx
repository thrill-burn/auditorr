import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import Sidebar      from './components/Sidebar'
import Dashboard    from './components/Dashboard'
import FileExplorer from './components/FileExplorer'
import Config       from './components/Config'
import Trackers     from './components/Trackers'
import ErrorBanner  from './components/ErrorBanner'
import ChangesPanel from './components/ChangesPanel'
import { ToastProvider, useToast } from './components/Toast'
import { api } from './api'

// ── Script Modal ──────────────────────────────────────────────────────────────
function _btnStyle(bg, color) {
  return { padding: '7px 14px', borderRadius: 6, border: 'none', background: bg, color, fontSize: 12, fontWeight: 500, cursor: 'pointer' }
}

function ScriptModal({ scriptType, title, subtitle, onClose }) {
  const [script, setScript] = useState(null)
  const [loading, setLoading] = useState(true)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    api.actionScript(scriptType)
      .then(text => { setScript(text); setLoading(false) })
      .catch(e => { setScript(`# Error loading script: ${e.message}`); setLoading(false) })
  }, [scriptType])

  const handleCopy = () => {
    const ta = document.createElement('textarea')
    ta.value = script
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    try { document.execCommand('copy') } catch (_) {}
    document.body.removeChild(ta)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleDownload = () => {
    const blob = new Blob([script], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = scriptType + '.sh'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0,
        background: 'rgba(0,0,0,0.7)',
        zIndex: 500,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: '100%',
          maxWidth: 700,
          maxHeight: '85vh',
          background: 'var(--surface)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--rl)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12, flexShrink: 0 }}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--text)' }}>{title}</div>
            {subtitle && <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 2 }}>{subtitle}</div>}
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-dim)', fontSize: 20, lineHeight: 1, padding: 0, flexShrink: 0 }}>×</button>
        </div>
        <div style={{ padding: '10px 16px', background: 'rgba(234,179,8,0.13)', borderLeft: '3px solid var(--yellow)', margin: '12px 16px 0', fontSize: 11, color: 'var(--text-dim)', flexShrink: 0 }}>
          ⚠ Review this script carefully before running. auditorr does not execute scripts — you run this manually in your terminal.
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
          {loading ? (
            <div style={{ height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-dim)', fontSize: 13 }}>Loading…</div>
          ) : (
            <pre style={{ margin: 0, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{script}</pre>
          )}
        </div>
        <div style={{ padding: '14px 16px', borderTop: '1px solid var(--border)', display: 'flex', gap: 10, justifyContent: 'flex-end', flexShrink: 0 }}>
          <button onClick={onClose} style={_btnStyle('var(--surface2)', 'var(--text-dim)')}>Close</button>
          {!loading && script && (
            <>
              <button onClick={handleDownload} style={_btnStyle('var(--surface2)', 'var(--text)')}>Download .sh</button>
              <button onClick={handleCopy} style={_btnStyle('var(--accent)', 'var(--bg)')}>{copied ? '✓ Copied!' : 'Copy to clipboard'}</button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// Hash-based routing helpers
function getHashTab() {
  const hash = window.location.hash.replace('#', '') || 'dashboard'
  const valid = ['dashboard', 'media', 'torrents', 'trackers', 'config']
  return valid.includes(hash) ? hash : 'dashboard'
}
function setHashTab(tab) {
  window.location.hash = tab
}

function AppInner() {
  const [tab,        setTab]        = useState(getHashTab)
  const [results,    setResults]    = useState(null)
  const [changes,    setChanges]    = useState(null)
  const [scanState,  setScanState]  = useState({
    is_scanning: false, progress: 0, last_audit_time: 'Never',
    trigger: 'idle', next_scan_in: null, status_message: '',
    last_scan_status: 'never',
  })
  const [pendingNav,         setPendingNav]         = useState(null)
  const [isRefreshing,       setIsRefreshing]       = useState(false)
  const [theme,              setTheme]              = useState(() => localStorage.getItem('auditorr_theme') || 'dark')
  const [scriptModal,        setScriptModal]        = useState(null)
  const [timeRange,          setTimeRange]          = useState(30)
  const [selectedTrackers,   setSelectedTrackers]   = useState(null)
  const [revealPath,         setRevealPath]         = useState(null)
  const prevScanRef = useRef(false)

  useEffect(() => {
    if (tab !== 'media' && tab !== 'torrents') setRevealPath(null)
  }, [tab])

  const allTrackers = useMemo(() => {
    if (!results) return []
    const set = new Set()
    for (const f of results.media_files || []) {
      for (const t of f.trackers || []) { if (t !== 'None') set.add(t) }
    }
    return [...set].sort()
  }, [results])
  const toast       = useToast()

  // Apply theme to document root
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : '')
    localStorage.setItem('auditorr_theme', theme)
  }, [theme])

  // Sync tab to hash
  useEffect(() => {
    const onHashChange = () => setTab(getHashTab())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const fetchResults = useCallback(async () => {
    setIsRefreshing(true)
    try {
      const data = await api.results()
      setResults(data)
      // Also fetch changes diff
      try {
        const changesData = await api.changes()
        setChanges(changesData)
      } catch (_) {}
    } catch (e) {
      console.error('Failed to fetch results:', e)
    } finally {
      setIsRefreshing(false)
    }
  }, [])

  useEffect(() => {
    if (Notification.permission === 'default') Notification.requestPermission()
    fetchResults()
    const id = setInterval(async () => {
      try {
        const state = await api.progress()
        setScanState(state)
        if (prevScanRef.current && !state.is_scanning) {
          await fetchResults()
          const msg = state.status_message?.startsWith('Audit error') ||
                      state.status_message?.startsWith('qBittorrent')
            ? state.status_message : 'Audit complete'
          const isError = msg !== 'Audit complete'
          toast(msg, isError ? 'error' : 'success')
          if (!isError && Notification.permission === 'granted')
            new Notification('auditorr', { body: 'Library audit complete.', icon: '/favicon.ico' })
        }
        prevScanRef.current = state.is_scanning
      } catch (e) { console.error('Poll error:', e) }
    }, 5000)
    return () => clearInterval(id)
  }, [fetchResults, toast])

  const handleScan = async () => {
    await api.startScan()
    setScanState(s => ({ ...s, is_scanning: true, progress: 0 }))
    prevScanRef.current = true
    toast('Manual audit started', 'info')
  }

  const handleTabChange = t => {
    setHashTab(t)
    setTab(t)
    setPendingNav(null)
  }

  const handleNavigate = (action) => {
    const nav = {
      status: action.status || null,
      importFilter: action.importFilter || null,
      tracker: action.tracker || null,
      seedCount: action.seedCount != null ? action.seedCount : null,
    }
    setPendingNav(nav)
    setHashTab(action.tab)
    setTab(action.tab)
  }

  // Cross-seed multiplier computed from media_files
  const crossSeedMultiplier = results?.media_files ? (() => {
    let ws = 0, ts = 0
    for (const f of results.media_files) {
      const n = (f.trackers||[]).filter(t => t !== 'None').length
      ws += f.size * n; ts += f.size
    }
    return ts > 0 ? ws / ts : null
  })() : null

  const navKey = tab +
    (pendingNav?.status || '') +
    (pendingNav?.importFilter || '') +
    (pendingNav?.tracker || '') +
    (pendingNav?.seedCount != null ? String(pendingNav.seedCount) : '')

  return (
    <div style={{ display: 'flex', minHeight: '100vh', background: 'var(--bg)' }}>
      <Sidebar
        active={tab}
        onChange={handleTabChange}
        isScanning={scanState.is_scanning}
        progress={scanState.progress}
        lastAuditTime={scanState.last_audit_time}
        lastScanStatus={scanState.last_scan_status}
        trigger={scanState.trigger}
        nextScanIn={scanState.next_scan_in}
        statusMessage={scanState.status_message}
        score={results?.dashboard?.score}
        crossSeedMultiplier={crossSeedMultiplier}
      />
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <ErrorBanner message={results?.status} />
        <div style={{ flex: 1, position: 'relative' }}>
          {/* Refresh shimmer overlay */}
          {isRefreshing && (
            <div style={{
              position: 'absolute', top: 0, left: 0, right: 0, height: 2, zIndex: 200,
              background: 'var(--accent)',
              animation: 'slideIn 0.6s ease',
            }} />
          )}
          {tab === 'dashboard' && (
            <Dashboard
              data={results?.dashboard ? { ...results.dashboard, media_files: results.media_files, torrent_files: results.torrent_files } : null}
              changes={changes}
              onNavigate={handleNavigate}
              isRefreshing={isRefreshing}
              onScript={setScriptModal}
              timeRange={timeRange}
              setTimeRange={setTimeRange}
              selectedTrackers={selectedTrackers}
              setSelectedTrackers={setSelectedTrackers}
              allTrackers={allTrackers}
              onReveal={(path, revealTab) => { setRevealPath(path); setHashTab(revealTab); setTab(revealTab) }}
            />
          )}
          {(tab === 'media' || tab === 'torrents') && (
            <FileExplorer
              key={navKey}
              files={results ? (tab === 'media' ? results.media_files : results.torrent_files) : []}
              trackers={results?.trackers || []}
              tab={tab}
              initialStatus={pendingNav?.status}
              initialImportFilter={pendingNav?.importFilter}
              initialTracker={pendingNav?.tracker}
              initialSeedCount={pendingNav?.seedCount}
              revealPath={revealPath}
            />
          )}
          {tab === 'trackers' && (
            <Trackers
              torrentFiles={results?.torrent_files || []}
              onNavigate={handleNavigate}
              timeRange={timeRange}
              onTimeRangeChange={setTimeRange}
              selectedTrackers={selectedTrackers}
              allTrackers={allTrackers}
              onTrackersChange={setSelectedTrackers}
            />
          )}
          {tab === 'config' && (
            <Config
              lastAuditTime={scanState.last_audit_time}
              onScan={handleScan}
              isScanning={scanState.is_scanning}
              onConfigSaved={fetchResults}
              theme={theme}
              onThemeChange={setTheme}
            />
          )}
        </div>
      </div>
      {scriptModal && (
        <ScriptModal
          scriptType={scriptModal.scriptType}
          title={scriptModal.title || scriptModal.label}
          subtitle={scriptModal.subtitle}
          onClose={() => setScriptModal(null)}
        />
      )}
    </div>
  )
}

export default function App() {
  return (
    <ToastProvider>
      <AppInner />
    </ToastProvider>
  )
}
