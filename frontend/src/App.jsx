import React, { useState, useEffect, useCallback, useRef } from 'react'
import SetupWizard  from './components/SetupWizard'
import ChangeLog    from './components/ChangeLog'
import Sidebar      from './components/Sidebar'
import Dashboard    from './components/Dashboard'
import FileExplorer from './components/FileExplorer'
import Config       from './components/Config'
import Trackers     from './components/Trackers'
import Workflows    from './components/Workflows'
import ScanProgress    from './components/ScanProgress'
import ImportProgress  from './components/ImportProgress'
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
        zIndex: 700,
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
  const valid = ['dashboard', 'media', 'torrents', 'trackers', 'changes', 'config', 'workflows']
  return valid.includes(hash) ? hash : 'dashboard'
}
function setHashTab(tab) {
  window.location.hash = tab
}

function AppInner() {
  const [tab,        setTab]        = useState(getHashTab)
  const [results,      setResults]      = useState(null)
  const [changes,      setChanges]      = useState(null)
  const [mediaFiles,   setMediaFiles]   = useState(null)   // null = not yet loaded
  const [torrentFiles, setTorrentFiles] = useState(null)
  const [scanState,  setScanState]  = useState({
    is_scanning: false, progress: 0, last_audit_time: 'Never',
    trigger: 'idle', next_scan_in: null, status_message: '',
    last_scan_status: 'never',
  })
  const [pendingNav,         setPendingNav]         = useState(null)
  const [isRefreshing,       setIsRefreshing]       = useState(false)
  const [theme,              setTheme]              = useState(() => localStorage.getItem('auditorr_theme') || 'dark')
  const [scriptModal,        setScriptModal]        = useState(null)
  const [timeRange,          setTimeRange]          = useState(() => {
    const stored = localStorage.getItem('auditorr_chart_days')
    return stored !== null ? parseInt(stored) : 30
  })
  const [selectedTrackers,   setSelectedTrackers]   = useState(null)
  const [revealPath,         setRevealPath]         = useState(null)
  const [showWizard,         setShowWizard]         = useState(false)
  const [isLoadingResults,   setIsLoadingResults]   = useState(false)
  const [activeImports,      setActiveImports]      = useState([])
  const [importPanelOpen,    setImportPanelOpen]    = useState(false)
  const prevScanRef        = useRef(false)
  const intervalRef        = useRef(null)
  const filesFetchingRef   = useRef({ media: false, torrents: false })
  const importIntervalRef  = useRef(null)

  useEffect(() => {
    if (tab !== 'media' && tab !== 'torrents') setRevealPath(null)
  }, [tab])

  // tracker names come pre-sorted from the backend
  const allTrackers = results?.trackers || []
  const toast       = useToast()

  // Apply theme to document root
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : '')
    localStorage.setItem('auditorr_theme', theme)
  }, [theme])

  useEffect(() => {
    localStorage.setItem('auditorr_chart_days', timeRange)
  }, [timeRange])

  // Sync tab to hash
  useEffect(() => {
    const onHashChange = () => setTab(getHashTab())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const ensureFilesLoaded = useCallback(async (fileTab) => {
    if (filesFetchingRef.current[fileTab]) return
    filesFetchingRef.current[fileTab] = true
    const setter = fileTab === 'media' ? setMediaFiles : setTorrentFiles
    try {
      setter(await api.files(fileTab))
    } catch (e) {
      console.error('Failed to load files for tab:', fileTab, e)
      setter([])
    } finally {
      filesFetchingRef.current[fileTab] = false
    }
  }, [])

  const fetchResults = useCallback(async (fromScan = false) => {
    setIsRefreshing(true)
    if (fromScan) {
      setIsLoadingResults(true)
      // Invalidate file caches so the next tab visit re-fetches fresh data
      setMediaFiles(null)
      setTorrentFiles(null)
    }
    try {
      const data = await api.results()
      setResults(data)
      try {
        const changesData = await api.changes()
        setChanges(changesData)
      } catch (_) {}
    } catch (e) {
      console.error('Failed to fetch results:', e)
    } finally {
      setIsRefreshing(false)
      if (fromScan) setIsLoadingResults(false)
    }
  }, [])

  useEffect(() => {
    if (localStorage.getItem('auditorr_setup_dismissed')) return
    api.getConfig().then(cfg => {
      const isQui = cfg.TORRENT_SOURCE === 'qui'
      const unconfigured = isQui ? !cfg.QUI_HOST : !cfg.QB_HOST
      if (unconfigured) setShowWizard(true)
    }).catch(() => {})
  }, [])

  const pollOnce = useCallback(async () => {
    try {
      const state = await api.progress()
      setScanState(state)
      if (prevScanRef.current && !state.is_scanning) {
        await fetchResults(true)
        const msg = state.status_message?.startsWith('Audit error') ||
                    state.status_message?.startsWith('qBittorrent') ||
                    state.status_message?.startsWith('qui')
          ? state.status_message : 'Audit complete'
        const isError = msg !== 'Audit complete'
        toast(msg, isError ? 'error' : 'success')
        if (!isError && 'Notification' in window && Notification.permission === 'granted')
          new Notification('auditorr', { body: 'Library audit complete.', icon: '/favicon.ico' })
      }
      prevScanRef.current = state.is_scanning
    } catch (e) { console.error('Poll error:', e) }
  }, [fetchResults, toast])

  // Lazy-load file lists only when a tab that needs them becomes active
  useEffect(() => {
    if (tab === 'media'    && mediaFiles   === null) ensureFilesLoaded('media')
    if (tab === 'torrents' && torrentFiles === null) ensureFilesLoaded('torrents')
    if (tab === 'trackers' && torrentFiles === null) ensureFilesLoaded('torrents')
  }, [tab, mediaFiles, torrentFiles, ensureFilesLoaded])

  useEffect(() => {
    if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission()
    fetchResults()
    intervalRef.current = setInterval(pollOnce, 5000)
    return () => clearInterval(intervalRef.current)
  }, [fetchResults, pollOnce])

  useEffect(() => {
    const pollImports = async () => {
      try {
        const data = await api.watchImportActive()
        setActiveImports(data.jobs || [])
      } catch (_) {}
    }
    importIntervalRef.current = setInterval(pollImports, 5000)
    return () => clearInterval(importIntervalRef.current)
  }, [])

  useEffect(() => {
    const rate = scanState.is_scanning ? 500 : 5000
    clearInterval(intervalRef.current)
    intervalRef.current = setInterval(pollOnce, rate)
    return () => clearInterval(intervalRef.current)
  }, [scanState.is_scanning, pollOnce])

  const handleScan = async () => {
    await api.startScan()
    setScanState(s => ({ ...s, is_scanning: true, progress: 0 }))
    prevScanRef.current = true
    toast('Manual audit started', 'info')
  }

  const handleWizardEarlyStart = async (partialConfig) => {
    try { await api.saveConfig(partialConfig) } catch (_) {}
    try { await api.startScan() } catch (_) {}
    setScanState(s => ({ ...s, is_scanning: true, progress: 0 }))
    prevScanRef.current = true
  }

  const handleWizardComplete = async (wizardData) => {
    try { await api.saveConfig(wizardData) } catch (_) {}
    localStorage.setItem('auditorr_setup_dismissed', '1')
    setShowWizard(false)
  }

  const handleWizardSkip = () => {
    localStorage.setItem('auditorr_setup_dismissed', '1')
    setShowWizard(false)
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

  // Pre-computed by the backend — no need to iterate file lists client-side
  const crossSeedMultiplier = results?.dashboard?.cross_seed_stats?.multiplier ?? null

  const navKey = tab +
    (pendingNav?.status || '') +
    (pendingNav?.importFilter || '') +
    (pendingNav?.tracker || '') +
    (pendingNav?.seedCount != null ? String(pendingNav.seedCount) : '')

  return (
    <div style={{ display: 'flex', minHeight: '100vh', background: 'var(--bg)' }}>
      {showWizard && <SetupWizard onComplete={handleWizardComplete} onSkip={handleWizardSkip} onEarlyStart={handleWizardEarlyStart} />}
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
        activeImportCount={activeImports.filter(j => !['done', 'error'].includes(j.status)).length}
        onOpenImportPanel={() => setImportPanelOpen(true)}
      />
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        {!showWizard && <ErrorBanner message={results?.status} />}
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
              data={results?.dashboard ? {
                ...results.dashboard,
                tracker_file_stats: results.tracker_file_stats,
                not_imported_paths: results.not_imported_paths,
              } : null}
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
              files={tab === 'media' ? mediaFiles : torrentFiles}
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
              trackerFileStats={results?.tracker_file_stats || {}}
              onNavigate={handleNavigate}
              timeRange={timeRange}
              selectedTrackers={selectedTrackers}
              allTrackers={allTrackers}
              onTrackersChange={setSelectedTrackers}
            />
          )}
          {tab === 'changes' && (
            <ChangeLog onNavigate={(path, revealTab) => { setRevealPath(path); setHashTab(revealTab); setTab(revealTab) }} />
          )}
          {tab === 'config' && (
            <Config
              lastAuditTime={scanState.last_audit_time}
              isScanning={scanState.is_scanning}
              onConfigSaved={fetchResults}
              onScan={handleScan}
              theme={theme}
              onThemeChange={setTheme}
            />
          )}
          {tab === 'workflows' && (
            <Workflows onNavigate={handleNavigate} />
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
      <ScanProgress
        isScanning={scanState.is_scanning}
        progress={scanState.progress}
        phase={scanState.phase}
        statusMessage={scanState.status_message}
        scannedFiles={scanState.scanned_files}
        totalFiles={scanState.total_files}
        isLoadingResults={isLoadingResults}
      />
      <ImportProgress
        open={importPanelOpen && activeImports.length > 0}
        jobs={activeImports}
        onClose={() => setImportPanelOpen(false)}
      />
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
