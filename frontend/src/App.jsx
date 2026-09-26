import React, { useState, useEffect, useCallback, useRef } from 'react'
import SetupWizard  from './components/SetupWizard'
import ChangeLog    from './components/ChangeLog'
import Sidebar      from './components/Sidebar'
import Dashboard    from './components/Dashboard'
import Rounds       from './components/Rounds'
import FileExplorer from './components/FileExplorer'
import Config       from './components/Config'
import Trackers     from './components/Trackers'
import Backfill     from './components/workflows/Backfill'
import Triage       from './components/workflows/Triage'
import Cleanup      from './components/workflows/Cleanup'
import Dedupe       from './components/workflows/Dedupe'
import Trumped      from './components/workflows/Trumped'
import ScanProgress    from './components/ScanProgress'
import ImportProgress, { WATCH_ACTIVE } from './components/ImportProgress'
import ErrorBanner  from './components/ErrorBanner'
import ErrorBoundary from './components/ErrorBoundary'
import ChangesPanel from './components/ChangesPanel'
import { ToastProvider, useToast } from './components/Toast'
import { api } from './api'
import { formatBytes } from './utils'
import { Button, CloseButton, tint } from './components/workflows/shared'


// ── Script Modal ──────────────────────────────────────────────────────────────

// What the server says about a script it just built. Cleanup's delete script
// sends a verification time (it re-checks the selection against the torrent
// client immediately before building it); Dedupe's sends the groups and files it
// actually scripted, which leave out any group that changed since the scan.
function scriptMeta(headers) {
  const num = k => Number(headers.get(k) || 0)
  if (headers?.get?.('X-Auditorr-Groups')) {
    return {
      kind:      'dedupe',
      groups:    num('X-Auditorr-Groups'),
      files:     num('X-Auditorr-Files'),
      freesUpTo: num('X-Auditorr-Frees-Up-To'),
    }
  }
  const at = headers?.get?.('X-Auditorr-Verified-At')
  if (!at) return null
  return {
    verifiedAt: Number(at),
    dropped:    num('X-Auditorr-Dropped'),
    files:      num('X-Auditorr-Files'),
    freeable:   num('X-Auditorr-Freeable'),
  }
}

function ScriptModal({ scriptType, title, subtitle, body, onClose }) {
  const [script, setScript] = useState(null)
  const [meta, setMeta] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [copied, setCopied] = useState(false)
  // One request per modal open. StrictMode runs effects twice in development,
  // and for Cleanup every request is a fresh round of torrent-client listings.
  const requested = useRef(null)

  useEffect(() => {
    if (requested.current === body) return
    requested.current = body
    api.actionScript(scriptType, body)
      .then(({ text, headers }) => { setScript(text); setMeta(scriptMeta(headers)); setLoading(false) })
      // A refusal is not a script. It used to render inside the code box as
      // `# Error loading script: …` — copyable, downloadable, and shaped like
      // something to run.
      .catch(e => { setError(e.message || 'Could not build the script'); setLoading(false) })
  }, [scriptType, body])

  // The page computed its subtitle from the selection; the server's count is the
  // one that holds after files a torrent now claims were dropped (Cleanup), or
  // after groups that changed since the scan were left out (Dedupe).
  const plural = (n, w) => `${n} ${w}${n !== 1 ? 's' : ''}`
  const shownSubtitle = !meta ? subtitle
    : meta.kind === 'dedupe'
      ? `${plural(meta.groups, 'group')} · ${plural(meta.files, 'file')} · frees up to ${formatBytes(meta.freesUpTo)}`
      : `${plural(meta.files, 'file')} · up to ${formatBytes(meta.freeable)} freed`

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
    // Local timestamp postfix (with seconds) so each generated script gets a
    // unique, sortable filename and re-runs never overwrite an earlier one.
    const d = new Date()
    const p = n => String(n).padStart(2, '0')
    const stamp = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
    a.download = `${scriptType}_${stamp}.sh`
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
          boxShadow: 'var(--shadow-pop)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12, flexShrink: 0 }}>
          <div>
            <div style={{ fontSize: 'var(--font-lg)', fontWeight: 700, color: 'var(--text)' }}>{title}</div>
            {shownSubtitle && !error && <div style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginTop: 2 }}>{shownSubtitle}</div>}
          </div>
          <CloseButton onClick={onClose} />
        </div>
        {!error && (
          <div style={{ padding: '10px 16px', background: 'var(--surface2)', border: '1px solid var(--border2)', borderRadius: 'var(--r)', margin: '12px 16px 0', fontSize: 'var(--font-base)', color: 'var(--text-dim)', flexShrink: 0 }}>
            ⚠ Review this script carefully before running. auditorr does not execute scripts — you run this manually in your terminal.
            {meta && (
              <div style={{ marginTop: 6 }}>
                Checked against your torrent client at {new Date(meta.verifiedAt * 1000).toLocaleTimeString()} — run it soon; it warns if it is more than a day old.
                {meta.dropped > 0 && (
                  <span style={{ color: 'var(--yellow)' }}>
                    {' '}{meta.dropped} selected file{meta.dropped !== 1 ? 's are' : ' is'} in use by a torrent now and {meta.dropped !== 1 ? 'were' : 'was'} left out.
                  </span>
                )}
              </div>
            )}
          </div>
        )}
        <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
          {loading ? (
            <div style={{ height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-dim)', fontSize: 'var(--font-base)' }}>Checking…</div>
          ) : error ? (
            <div style={{ padding: '12px 14px', background: tint('var(--red)', 6), border: `1px solid ${tint('var(--red)', 19)}`, borderRadius: 'var(--r)', fontSize: 'var(--font-base)', lineHeight: 1.6 }}>
              <div style={{ color: 'var(--red)', fontWeight: 600, marginBottom: 4 }}>No script was built</div>
              <div style={{ color: 'var(--text)' }}>{error}</div>
            </div>
          ) : (
            <pre style={{ margin: 0, fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{script}</pre>
          )}
        </div>
        <div style={{ padding: '14px 16px', borderTop: '1px solid var(--border)', display: 'flex', gap: 8, justifyContent: 'flex-end', flexShrink: 0 }}>
          <Button variant="ghost" onClick={onClose}>Close</Button>
          {!loading && !error && script && (
            <>
              <Button onClick={handleDownload}>Download .sh</Button>
              <Button variant="primary" onClick={handleCopy}>{copied ? '✓ Copied!' : 'Copy to clipboard'}</Button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// Hash-based routing helpers
function getHashTab() {
  let hash = window.location.hash.replace('#', '') || 'dashboard'
  if (hash === 'workflows') hash = 'backfill'  // legacy alias from before per-workflow pages
  const valid = ['dashboard', 'next-steps', 'media', 'torrents', 'trackers', 'changes', 'config', 'backfill', 'triage', 'cleanup', 'dedupe', 'trumped']
  return valid.includes(hash) ? hash : 'dashboard'
}
function setHashTab(tab) {
  window.location.hash = tab
}

// How many rows the Triage page will list, per torrent — the sidebar badge and
// the Cleanup page's cross-link both show this, and they must agree with the
// page. `triage_counts` is computed by the audit (audit.count_triage_items);
// the fallback is the old per-file sum, which undercounts (it cannot see dead
// registrations at all) and only covers results rows written before the
// upgrade. Deliberately one function: this was duplicated at two call sites and
// fixing one of them left the other quietly wrong.
function triageRowCount(details) {
  if (!details) return 0
  if (details.triage_counts) return details.triage_counts.total
  return (details.not_imported_count || 0) + (details.dead_seed_count || 0)
}

// Triage's dead seeds, per torrent — the rows a missed trump PM ends up as.
function deadSeedCount(details) {
  if (!details) return 0
  if (details.triage_counts) return details.triage_counts.dead_seeds || 0
  return details.dead_seed_count || 0
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
  const [authBlocked,        setAuthBlocked]        = useState(false)  // server refuses: AUDITORR_SECRET unset
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
      if (e.code === 'auth_not_configured') setAuthBlocked(true)
      console.error('Failed to fetch results:', e)
    } finally {
      setIsRefreshing(false)
      if (fromScan) setIsLoadingResults(false)
    }
  }, [])

  useEffect(() => {
    api.getConfig().then(cfg => {
      if (localStorage.getItem('auditorr_setup_dismissed')) return
      const isQui = cfg.TORRENT_SOURCE === 'qui'
      const unconfigured = isQui ? !cfg.QUI_HOST : !cfg.QB_HOST
      if (unconfigured) setShowWizard(true)
    }).catch(() => {})
  }, [])

  const pollOnce = useCallback(async () => {
    try {
      const state = await api.progress()
      setAuthBlocked(false)  // server answered — secret configured (or opt-out set)
      setScanState(state)
      if (prevScanRef.current && !state.is_scanning) {
        await fetchResults(true)
        // Tell the workflow pages too. They each own their own fetch (the
        // reports are not in `results`), so without this a page left open
        // across an audit keeps rendering pre-scan data indefinitely — the
        // same window idiom as `auditorr:import_started` below.
        window.dispatchEvent(new CustomEvent('auditorr:audit_complete'))
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
    } catch (e) {
      if (e.code === 'auth_not_configured') setAuthBlocked(true)
      console.error('Poll error:', e)
    }
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

    const onImportStarted = () => {
      setImportPanelOpen(true)
      pollImports()
    }
    window.addEventListener('auditorr:import_started', onImportStarted)

    return () => {
      clearInterval(importIntervalRef.current)
      window.removeEventListener('auditorr:import_started', onImportStarted)
    }
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
    // Land on Rounds, not the dashboard — a fresh install has no numbers
    // to read yet, but Rounds can always answer "what should I be doing".
    setHashTab('next-steps'); setTab('next-steps')
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
      // Triage's "Trumped?" chip pre-fills the trumped release name.
      oldTitle: action.oldTitle || null,
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
    (pendingNav?.seedCount != null ? String(pendingNav.seedCount) : '') +
    (pendingNav?.oldTitle || '')

  // Server is fail-closed: AUDITORR_REQUIRE_AUTH is set but no AUDITORR_SECRET
  // is configured. Nothing in the app can work, so take over the page with
  // instructions. pollOnce keeps running and clears this as soon as the server
  // answers again (secret set + restart).
  if (authBlocked) {
    const envChip = {
      fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', background: 'var(--surface2)',
      border: '1px solid var(--border)', borderRadius: 4, padding: '1px 5px',
    }
    return (
      <div style={{ minHeight: '100vh', background: 'var(--bg)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
        <div style={{
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 12, boxShadow: 'var(--elev-1)', padding: '28px 32px', maxWidth: 540,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
            <span style={{ width: 8, height: 8, borderRadius: 99, background: 'var(--red)', flexShrink: 0 }} />
            <span style={{ fontSize: 'var(--font-lg)', fontWeight: 600 }}>Access key required</span>
          </div>
          <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', lineHeight: 1.6, margin: 0 }}>
            <code style={envChip}>AUDITORR_REQUIRE_AUTH</code> is set, but no access key is
            configured. Set <code style={envChip}>AUDITORR_SECRET</code> in the container
            environment and restart — this page will pick it up automatically and ask for
            the key.
          </p>
        </div>
      </div>
    )
  }

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
        activeImportCount={activeImports.filter(j => WATCH_ACTIVE.includes(j.status)).length}
        onOpenImportPanel={() => setImportPanelOpen(true)}
        workflowCounts={(() => {
          const det = results?.dashboard?.current?.details
          if (!det) return null
          return {
            triage:  triageRowCount(det),
            cleanup: det.orphaned_torrent_count || 0,
            // Groups, as the page lists them (Phase 14). `duplicate_count` counts
            // files and is only the fallback, for details a scan wrote before
            // the group count existed.
            dedupe:  det.dedupe_group_count ?? det.duplicate_count ?? 0,
          }
        })()}
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
          {/* A page that throws while rendering shows its error here and leaves
              the sidebar working. Keyed by tab so switching page clears it; each
              page already unmounts when its tab closes, so the key remounts
              nothing that would have stayed mounted. */}
          <ErrorBoundary key={tab} scope="page" page={tab}>
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
            {/* Tab id stays `next-steps` — it is in users' bookmarks. Only the
                component was renamed to match what the page calls itself. */}
            {tab === 'next-steps' && (
              <Rounds onNavigate={handleTabChange} />
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
                allTrackers={allTrackers}
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
            {tab === 'backfill' && (
              <Backfill onNavigate={handleNavigate} />
            )}
            {tab === 'triage' && (
              <Triage onNavigate={handleNavigate}
                cleanupCount={results?.dashboard?.current?.details?.orphaned_torrent_count || 0}
                trumpedCount={deadSeedCount(results?.dashboard?.current?.details)} />
            )}
            {tab === 'cleanup' && (
              <Cleanup onNavigate={handleNavigate} onScript={setScriptModal}
                triageCount={triageRowCount(results?.dashboard?.current?.details)} />
            )}
            {tab === 'dedupe' && (
              <Dedupe onNavigate={handleNavigate} onScript={setScriptModal} />
            )}
            {tab === 'trumped' && (
              <Trumped key={navKey} onNavigate={handleNavigate}
                initialOldTitle={pendingNav?.oldTitle}
                triageDeadSeeds={deadSeedCount(results?.dashboard?.current?.details)} />
            )}
          </ErrorBoundary>
        </div>
      </div>
      {scriptModal && (
        <ScriptModal
          scriptType={scriptModal.scriptType}
          title={scriptModal.title || scriptModal.label}
          subtitle={scriptModal.subtitle}
          body={scriptModal.body}
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
        open={importPanelOpen}
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
