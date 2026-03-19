import React, { useState, useEffect, useCallback, useRef } from 'react'
import Sidebar      from './components/Sidebar'
import Dashboard    from './components/Dashboard'
import FileExplorer from './components/FileExplorer'
import Config       from './components/Config'
import ErrorBanner  from './components/ErrorBanner'
import ChangesPanel from './components/ChangesPanel'
import { ToastProvider, useToast } from './components/Toast'
import { api } from './api'

// Hash-based routing helpers
function getHashTab() {
  const hash = window.location.hash.replace('#', '') || 'dashboard'
  const valid = ['dashboard', 'media', 'torrents', 'config']
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
  const [pendingNav,    setPendingNav]    = useState(null)
  const [isRefreshing,  setIsRefreshing]  = useState(false)
  const [theme, setTheme] = useState(() => localStorage.getItem('auditorr_theme') || 'dark')
  const pollRef     = useRef(null)
  const prevScanRef = useRef(false)
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

  const startPolling = useCallback(() => {
    if (pollRef.current) return
    pollRef.current = setInterval(async () => {
      try {
        const state = await api.progress()
        setScanState(state)
        if (prevScanRef.current && !state.is_scanning) {
          clearInterval(pollRef.current)
          pollRef.current = null
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
    }, 1000)
  }, [fetchResults, toast])

  useEffect(() => {
    if (Notification.permission === 'default') Notification.requestPermission()
    fetchResults()
    api.progress().then(state => {
      setScanState(state)
      prevScanRef.current = state.is_scanning
      if (state.is_scanning) startPolling()
    })
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [fetchResults, startPolling])

  const handleScan = async () => {
    await api.startScan()
    setScanState(s => ({ ...s, is_scanning: true, progress: 0 }))
    prevScanRef.current = true
    startPolling()
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
              data={results?.dashboard ? { ...results.dashboard, media_files: results.media_files } : null}
              changes={changes}
              onNavigate={handleNavigate}
              isRefreshing={isRefreshing}
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
