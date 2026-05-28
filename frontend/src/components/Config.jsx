import React, { useState, useEffect, useMemo } from 'react'
import { FixedSizeList } from 'react-window'
import { api } from '../api'

const AUDIT_ROW_H = 36
const AUDIT_COLS = '2fr 1fr 1fr 0.8fr 0.8fr 1fr'
const MEDIA_SERVER_PRESETS = [
  { id: 'plex', label: 'Plex' },
  { id: 'jellyfin', label: 'Jellyfin' },
  { id: 'emby', label: 'Emby' },
  { id: 'kodi', label: 'Kodi' },
  { id: 'ums', label: 'Universal Media Server' },
]

function fmtDuration(s) {
  if (s == null) return '—'
  if (s < 60) return `${Math.round(s)}s`
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
}

const AuditRunRow = ({ index, style, data }) => {
  const run = data[index]
  const isOk = run.status === 'ok'
  const timeStr = new Date(run.ran_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  return (
    <div style={{
      ...style,
      display: 'grid', gridTemplateColumns: AUDIT_COLS, alignItems: 'center',
      borderBottom: '1px solid var(--border)',
      background: index % 2 === 0 ? 'transparent' : 'var(--surface2)',
      fontFamily: 'var(--mono)',
    }}>
      <div style={{ padding: '0 12px', fontSize: 11, color: 'var(--text-dim)', overflow: 'hidden', whiteSpace: 'nowrap' }}>{timeStr}</div>
      <div style={{ padding: '0 12px', fontSize: 11, color: 'var(--text-dim)' }}>{run.trigger}</div>
      <div style={{ padding: '0 12px', fontSize: 11, color: 'var(--text-dim)' }}>{run.source || 'qbit'}</div>
      <div style={{ padding: '0 12px', fontSize: 11, color: isOk ? 'var(--text)' : 'var(--text-dim)', fontWeight: isOk ? 600 : 400 }}>
        {isOk && run.health_score != null ? run.health_score : '—'}
      </div>
      <div style={{ padding: '0 12px', fontSize: 11, color: 'var(--text-dim)' }}>
        {fmtDuration(run.duration_seconds)}
      </div>
      <div style={{ padding: '0 12px' }}>
        <span style={{
          padding: '2px 8px', borderRadius: 99, fontSize: 11,
          background: isOk ? 'var(--green)18' : 'var(--red)18',
          color: isOk ? 'var(--green)' : 'var(--red)',
          border: `1px solid ${isOk ? 'var(--green)' : 'var(--red)'}35`,
        }}>
          {isOk ? 'ok' : run.error_message?.split(':')[0] || 'error'}
        </span>
      </div>
    </div>
  )
}

function DataBrowser({ onSelectMedia, onSelectTorrents }) {
  const [result, setResult] = useState(null)

  useEffect(() => {
    api.browseData().then(setResult).catch(() => setResult({ dirs: [], missing: true }))
  }, [])

  const btnStyle = {
    padding: '2px 8px', borderRadius: 'var(--r)', border: '1px solid var(--border2)',
    background: 'transparent', color: 'var(--text-dim)', fontFamily: 'var(--mono)',
    fontSize: 11, cursor: 'pointer', whiteSpace: 'nowrap',
  }

  if (!result) return (
    <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', marginTop: 10 }}>Browsing /data…</div>
  )

  if (result.missing || result.dirs.length === 0) return (
    <div style={{ marginTop: 10, fontFamily: 'var(--mono)', fontSize: 11, lineHeight: 1.55 }}>
      {result.missing
        ? <span style={{ color: '#f59e0b' }}>⚠ /data is not mounted or is empty. Check your Docker volume configuration — auditorr expects your data to be mounted at /data.</span>
        : <span style={{ color: 'var(--text-dim)' }}>No subdirectories found in /data.</span>
      }
    </div>
  )

  return (
    <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 4 }}>
      {result.dirs.map(dir => (
        <div key={dir} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, padding: '4px 0' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--text-dim)" strokeWidth="2" style={{ flexShrink: 0 }}>
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
            </svg>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>/data/{dir}</span>
          </div>
          <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
            <button style={btnStyle} onClick={() => onSelectMedia('/data/' + dir)}>→ Media</button>
            <button style={btnStyle} onClick={() => onSelectTorrents('/data/' + dir)}>→ Torrents</button>
          </div>
        </div>
      ))}
    </div>
  )
}

function Field({ label, hint, type = 'text', value, onChange, placeholder, style = {}, prefix, suffix }) {
  const [focused, setFocused] = useState(false)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5, ...style }}>
      <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)' }}>{label}</label>
      {hint && <span style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45 }}>{hint}</span>}
      <div style={{ display: 'flex', alignItems: 'center', position: 'relative' }}>
        {prefix && (
          <span style={{
            position: 'absolute', left: 10, fontFamily: 'var(--mono)', fontSize: 13,
            color: 'var(--text-dim)', pointerEvents: 'none',
          }}>{prefix}</span>
        )}
        <input
          type={type} value={value ?? ''} placeholder={placeholder}
          onChange={e => onChange(e.target.value)}
          onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
          style={{
            padding: `8px ${suffix ? '32px' : '11px'} 8px ${prefix ? '24px' : '11px'}`,
            borderRadius: 'var(--r)',
            border: `1px solid ${focused ? 'var(--accent)' : 'var(--border2)'}`,
            background: 'var(--surface2)', color: 'var(--text)',
            fontFamily: 'var(--mono)', fontSize: 13, outline: 'none',
            transition: 'border 0.12s', width: '100%',
          }}
        />
        {suffix && (
          <span style={{
            position: 'absolute', right: 10, fontFamily: 'var(--mono)', fontSize: 13,
            color: 'var(--text-dim)', pointerEvents: 'none',
          }}>{suffix}</span>
        )}
      </div>
    </div>
  )
}

function Card({ title, children }) {
  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', padding: 24, marginBottom: 16 }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, letterSpacing: 2, textTransform: 'uppercase', color: 'var(--text-dim)', paddingBottom: 14, marginBottom: 18, borderBottom: '1px solid var(--border)' }}>{title}</div>
      {children}
    </div>
  )
}

export default function Config({ lastAuditTime, isScanning, onConfigSaved, theme, onThemeChange, onScan }) {
  const [conf,        setConf]        = useState(null)
  const [testStatus,        setTestStatus]        = useState(null)
  const [sonarrTestStatus,  setSonarrTestStatus]  = useState(null)
  const [radarrTestStatus,  setRadarrTestStatus]  = useState(null)
  const [saveStatus,        setSaveStatus]        = useState(null)
  const [saveWarnings,      setSaveWarnings]      = useState(() => JSON.parse(localStorage.getItem('auditorr_path_warnings') || '[]'))
  const [passChanged,    setPassChanged]    = useState(false)
  const [apiKeyChanged,  setApiKeyChanged]  = useState(false)
  const [auditRuns,   setAuditRuns]   = useState(null)
  const [clearStatus, setClearStatus] = useState(null)
  const [pathTestStatus, setPathTestStatus] = useState(null)
  const [browserOpen, setBrowserOpen] = useState(false)
  const [sourceInfo, setSourceInfo] = useState(null)
  const [savePathStatus, setSavePathStatus] = useState(null)
  const [isDirty, setIsDirty] = useState(false)
  const [quiSkippedOpen, setQuiSkippedOpen] = useState(false)
  const [savedSource, setSavedSource] = useState('qbit')

  // We display ratios as percentages in the UI (0.01 → "1")
  // and convert back on save
  const [orPct,  setOrPct]  = useState('')
  const [niPct,  setNiPct]  = useState('')
  const [dupPct, setDupPct] = useState('')
  const [exclusionPatterns,        setExclusionPatterns]        = useState('')
  const [exclusionFocused,         setExclusionFocused]         = useState(false)
  const [mediaServerPresets,       setMediaServerPresets]       = useState([])
  const [exclusionHideFromExplorer, setExclusionHideFromExplorer] = useState(false)
  const [arrConnectionsText,       setArrConnectionsText]       = useState('')
  const [arrConnectionsFocused,    setArrConnectionsFocused]    = useState(false)
  const [arrTestStatus,            setArrTestStatus]            = useState(null)
  const [watchdogEnabled, setWatchdogEnabled] = useState(true)

  const loadConfig = () => {
    api.getConfig().then(data => {
      const c = {
        ...data,
        QB_PASS:        data.QB_PASS        === '__stored__' ? '' : data.QB_PASS,
        QUI_API_KEY:    data.QUI_API_KEY    === '__stored__' ? '' : data.QUI_API_KEY,
        SONARR_API_KEY: data.SONARR_API_KEY === '__stored__' ? '' : data.SONARR_API_KEY,
        RADARR_API_KEY: data.RADARR_API_KEY === '__stored__' ? '' : data.RADARR_API_KEY,
      }
      setConf(c)
      setSavedSource(c.TORRENT_SOURCE || 'qbit')
      setOrPct( String(parseFloat((c.OR_RATIO  ?? 0.01) * 100)))
      setNiPct( String(parseFloat((c.NI_RATIO  ?? 0.01) * 100)))
      setDupPct(String(parseFloat((c.DUP_RATIO ?? 0.01) * 100)))
      setExclusionPatterns((c.EXCLUSION_PATTERNS || []).join('\n'))
      setMediaServerPresets(Array.isArray(c.MEDIA_SERVER_EXCLUSION_PRESETS) ? c.MEDIA_SERVER_EXCLUSION_PRESETS : [])
      setExclusionHideFromExplorer(!!c.EXCLUSION_HIDE_FROM_EXPLORER)
      setArrConnectionsText(JSON.stringify(c.ARR_CONNECTIONS || [], null, 2))
      setArrTestStatus(null)
      setWatchdogEnabled(c.WATCHDOG_ENABLED !== false)
      setPassChanged(false)
      setApiKeyChanged(false)
      setIsDirty(false)
    })
  }

  useEffect(() => {
    loadConfig()
    api.auditHistory().then(data => setAuditRuns(data.runs || [])).catch(() => setAuditRuns([]))
  }, [])

  const dedupedRuns = useMemo(() => {
    if (!auditRuns) return []
    const seen = new Set()
    return auditRuns.filter(run => {
      const key = run.ran_at.slice(0, 16) + run.trigger + run.status
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
  }, [auditRuns])

  if (!conf) return <div style={{ padding: 40, color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 12 }}>Loading…</div>

  const set = key => val => { setConf(c => ({ ...c, [key]: val })); setIsDirty(true) }

  const setPersistentWarnings = (warnings) => {
    setSaveWarnings(warnings)
    if (warnings.length) localStorage.setItem('auditorr_path_warnings', JSON.stringify(warnings))
    else localStorage.removeItem('auditorr_path_warnings')
  }

  const isQui = conf?.TORRENT_SOURCE === 'qui'

  const handleTest = async () => {
    setTestStatus({ loading: true })
    setSourceInfo(null)
    setQuiSkippedOpen(false)
    try {
      const payload = isQui
        ? { TORRENT_SOURCE: 'qui', QUI_HOST: conf.QUI_HOST, QUI_API_KEY: conf.QUI_API_KEY }
        : { TORRENT_SOURCE: 'qbit', QB_HOST: conf.QB_HOST, QB_USER: conf.QB_USER, QB_PASS: conf.QB_PASS }
      const r = await api.testConnection(payload)
      setTestStatus({ ok: true })
      if (isQui) {
        const n = (r.instances || []).length
        const e = r.eligible_count ?? 0
        const s = (r.skipped || []).length
        setSourceInfo({
          version: r.version,
          summary: `${n} instance${n !== 1 ? 's' : ''} (${e} scannable, ${s} skipped)`,
          skipped: r.skipped || [],
        })
      } else {
        setSourceInfo({ version: r.version })
        api.fetchSavePath({ TORRENT_SOURCE: 'qbit', QB_HOST: conf.QB_HOST, QB_USER: conf.QB_USER, QB_PASS: conf.QB_PASS })
          .then(r2 => setSourceInfo(prev => ({ ...prev, version: r2.version || prev?.version })))
          .catch(() => {})
      }
    } catch (e) { setTestStatus({ ok: false, msg: e.message }) }
  }

  const handleFetchSavePath = async () => {
    setSavePathStatus('loading')
    try {
      const payload = isQui
        ? { TORRENT_SOURCE: 'qui', QUI_HOST: conf.QUI_HOST, QUI_API_KEY: conf.QUI_API_KEY }
        : { TORRENT_SOURCE: 'qbit', QB_HOST: conf.QB_HOST, QB_USER: conf.QB_USER, QB_PASS: conf.QB_PASS }
      const res = await api.fetchSavePath(payload)
      if (res.save_path) { set('REMOTE_PATH')(res.save_path); setSavePathStatus('ok') }
      else setSavePathStatus('empty')
    } catch (e) { setSavePathStatus('error') }
  }

  const handleTestPaths = async () => {
    setPathTestStatus('loading')
    try {
      const result = await api.testPaths({ MEDIA_PATH: conf.MEDIA_PATH, LOCAL_PATH: conf.LOCAL_PATH })
      setPathTestStatus(result)
    } catch (e) { setPathTestStatus({ error: e.message }) }
  }

  const handleTestSonarr = async () => {
    setSonarrTestStatus({ loading: true })
    try {
      await api.testSonarr(conf.SONARR_URL, conf.SONARR_API_KEY)
      await handleSave()
      setSonarrTestStatus({ ok: true, msg: 'Connected and saved!' })
    } catch (e) { setSonarrTestStatus({ ok: false, msg: e.message }) }
  }

  const handleTestRadarr = async () => {
    setRadarrTestStatus({ loading: true })
    try {
      await api.testRadarr(conf.RADARR_URL, conf.RADARR_API_KEY)
      await handleSave()
      setRadarrTestStatus({ ok: true, msg: 'Connected and saved!' })
    } catch (e) { setRadarrTestStatus({ ok: false, msg: e.message }) }
  }

  const parseArrConnections = () => {
    let arrConnections = []
    arrConnections = arrConnectionsText.trim() ? JSON.parse(arrConnectionsText) : []
    if (!Array.isArray(arrConnections)) throw new Error('ARR connections must be a JSON array')
    return arrConnections
  }

  const handleTestArrConnections = async () => {
    setArrTestStatus({ loading: true })
    let arrConnections = []
    try {
      arrConnections = parseArrConnections()
    } catch (e) {
      setArrTestStatus({ ok: false, msg: `Invalid Arr connections JSON: ${e.message}` })
      return
    }
    try {
      const result = await api.testArrConnections({ ...conf, ARR_CONNECTIONS: arrConnections })
      setArrTestStatus({
        ok: !!result.ok,
        msg: result.message,
        connections: result.connections || [],
      })
    } catch (e) {
      setArrTestStatus({ ok: false, msg: e.message })
    }
  }

  const handleSave = async () => {
    const sourceChanged = conf.TORRENT_SOURCE !== savedSource
    setPersistentWarnings([])
    let arrConnections = []
    try {
      arrConnections = parseArrConnections()
    } catch (e) {
      setSaveStatus({ ok: false, msg: `Invalid Arr connections JSON: ${e.message}` })
      return
    }
    const payload = {
      ...conf,
      OR_RATIO:  parseFloat(orPct)  / 100 || 0.01,
      NI_RATIO:  parseFloat(niPct)  / 100 || 0.01,
      DUP_RATIO: parseFloat(dupPct) / 100 || 0.01,
      EXCLUSION_PATTERNS:           exclusionPatterns.split('\n').map(p => p.trim()).filter(Boolean),
      MEDIA_SERVER_EXCLUSION_PRESETS: mediaServerPresets,
      EXCLUSION_HIDE_FROM_EXPLORER: exclusionHideFromExplorer,
      ARR_CONNECTIONS:              arrConnections,
      WATCHDOG_ENABLED:             watchdogEnabled,
    }
    if (!passChanged)   delete payload.QB_PASS
    if (!apiKeyChanged) delete payload.QUI_API_KEY
    try {
      const result = await api.saveConfig(payload)
      if (result.warnings?.length) setPersistentWarnings(result.warnings)
      else setPersistentWarnings([])
      setSaveStatus({ ok: true, msg: sourceChanged ? 'Saved! Starting audit…' : 'Saved!' })
      setTimeout(() => setSaveStatus(null), 5000)
      // Re-fetch config so form shows server-confirmed values
      loadConfig()
      // Refresh dashboard so threshold changes are reflected immediately
      if (onConfigSaved) onConfigSaved()
      if (sourceChanged && onScan) onScan()
    } catch (e) { setSaveStatus({ ok: false, msg: e.message }) }
  }

  const g2 = { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }
  const g3 = { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 }

  const toggleMediaPreset = id => {
    setMediaServerPresets(prev => prev.includes(id) ? prev.filter(p => p !== id) : [...prev, id])
    setIsDirty(true)
  }

  const handleClearHistory = async () => {
    if (!window.confirm('Clear all audit history? This will reset the score chart and run log. Cannot be undone.')) return
    try {
      await api.clearHistory()
      setAuditRuns([])
      setClearStatus({ ok: true, msg: 'History cleared.' })
      if (onConfigSaved) onConfigSaved()
      setTimeout(() => setClearStatus(null), 3000)
    } catch (e) { setClearStatus({ ok: false, msg: e.message }) }
  }

  const fmtSize = b => {
    if (!b) return '0 B'
    if (b >= 1e12) return (b / 1e12).toFixed(1) + ' TB'
    if (b >= 1e9)  return (b / 1e9).toFixed(1)  + ' GB'
    return (b / 1e6).toFixed(0) + ' MB'
  }

  const thresholdHint = (label) =>
    `All 10 pts lost when ${label} data reaches this % of your library. Points lost proportionally below that.`

  return (
    <div className="fade-in" style={{ padding: 24, maxWidth: 800 }}>

      <Card title="Torrent Source">
        {/* Source toggle */}
        <div style={{ display: 'flex', marginBottom: 18 }}>
          {['qbit', 'qui'].map((src, i) => (
            <button key={src} onClick={() => { set('TORRENT_SOURCE')(src); setTestStatus(null); setSourceInfo(null); setIsDirty(true) }} style={{
              padding: '6px 18px',
              borderRadius: i === 0 ? '99px 0 0 99px' : '0 99px 99px 0',
              border: `1px solid ${conf.TORRENT_SOURCE === src ? 'var(--accent)' : 'var(--border2)'}`,
              borderRight: i === 0 ? 'none' : undefined,
              background: conf.TORRENT_SOURCE === src ? 'var(--accent)22' : 'transparent',
              color: conf.TORRENT_SOURCE === src ? 'var(--accent)' : 'var(--text-dim)',
              fontSize: 12, fontWeight: 500, cursor: 'pointer', transition: 'all 0.12s',
            }}>{src === 'qbit' ? 'qBittorrent' : 'qui'}</button>
          ))}
        </div>

        {!isQui ? (
          <>
            <Field label="Host URL" placeholder="http://192.168.1.x:8080" value={conf.QB_HOST} onChange={v => { set('QB_HOST')(v); setSourceInfo(null) }} style={{ marginBottom: 14 }} />
            <div style={g2}>
              <Field label="Username" placeholder="admin" value={conf.QB_USER} onChange={v => { set('QB_USER')(v); setSourceInfo(null) }} />
              <Field label="Password" type="password" placeholder="(unchanged — leave blank to keep current)"
                value={conf.QB_PASS} onChange={v => { setPassChanged(true); set('QB_PASS')(v); setSourceInfo(null) }} />
            </div>
          </>
        ) : (
          <>
            <p style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.55, marginBottom: 14 }}>
              qui aggregates multiple qBittorrent instances behind one API endpoint — ideal for multi-instance setups sharing a common filesystem (e.g. mergerfs).
            </p>
            <Field label="Host URL" placeholder="http://192.168.1.x:7476" value={conf.QUI_HOST} onChange={v => { set('QUI_HOST')(v); setSourceInfo(null) }} style={{ marginBottom: 14 }} />
            <Field label="API Key" type="password" placeholder="(unchanged — leave blank to keep current)"
              hint="Create a full-access key in qui under Settings → API Keys."
              value={conf.QUI_API_KEY} onChange={v => { setApiKeyChanged(true); set('QUI_API_KEY')(v); setSourceInfo(null) }} />
          </>
        )}

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14 }}>
          {testStatus && (testStatus.loading || !testStatus.ok) && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: testStatus.loading ? 'var(--text-dim)' : 'var(--red)' }}>
              {testStatus.loading ? 'Testing…' : '✗ ' + testStatus.msg}
            </span>
          )}
          <button onClick={handleTest} style={{ padding: '7px 14px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)', fontSize: 12, cursor: 'pointer' }}>
            Test Connection
          </button>
        </div>
        {sourceInfo && testStatus?.ok && (
          <div style={{ marginTop: 8 }}>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--green)' }}>
              {isQui
                ? `✓ Connected · qui${sourceInfo.version ? ` v${sourceInfo.version}` : ''} · ${sourceInfo.summary}`
                : `✓ Connected · qBittorrent ${sourceInfo.version || ''}`}
            </div>
            {isQui && sourceInfo.skipped?.length > 0 && (
              <div style={{ marginTop: 6 }}>
                <button onClick={() => setQuiSkippedOpen(o => !o)} style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
                  {quiSkippedOpen ? '▼' : '▶'} {sourceInfo.skipped.length} skipped instance{sourceInfo.skipped.length !== 1 ? 's' : ''}
                </button>
                {quiSkippedOpen && (
                  <div style={{ marginTop: 4, paddingLeft: 10, borderLeft: '2px solid var(--border2)' }}>
                    {sourceInfo.skipped.map((inst, i) => (
                      <div key={i} style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.6 }}>
                        {inst.name || inst.id}: {inst._skip_reason}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </Card>

      <Card title="Path Mappings">
        <Field label="qBit Save Path"
          hint="The path qBittorrent reports via its API. May differ if qBit runs in its own container."
          placeholder="/data/torrents" value={conf.REMOTE_PATH} onChange={set('REMOTE_PATH')} />
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 6, marginBottom: 14 }}>
          <button onClick={handleFetchSavePath} style={{ padding: '4px 10px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 11, cursor: 'pointer' }}>
            {savePathStatus === 'loading' ? 'Fetching…' : `Fetch from ${isQui ? 'qui' : 'qBittorrent'}`}
          </button>
          {savePathStatus === 'empty' && <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>No torrents found in qBittorrent</span>}
          {savePathStatus === 'error' && <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--red)' }}>✗ Could not connect</span>}
        </div>
        <div style={g2}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Field label="Media Path"
              hint="Where your final media library lives inside this container — e.g. /data/media"
              placeholder="/data/media" value={conf.MEDIA_PATH} onChange={v => { set('MEDIA_PATH')(v); setPersistentWarnings([]); setPathTestStatus(null) }} />
            {pathTestStatus && pathTestStatus !== 'loading' && !pathTestStatus.error && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: pathTestStatus.media_path?.ok ? 'var(--green)' : 'var(--red)' }}>
                {pathTestStatus.media_path?.ok ? '✓ Found' : '✗ Not found inside container'}
              </span>
            )}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Field label="Local Torrent Path"
              hint="The path qBittorrent saves downloads on disk from this container's perspective."
              placeholder="/data/torrents" value={conf.LOCAL_PATH} onChange={v => { set('LOCAL_PATH')(v); setPersistentWarnings([]); setPathTestStatus(null) }} />
            {pathTestStatus && pathTestStatus !== 'loading' && !pathTestStatus.error && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: pathTestStatus.local_path?.ok ? 'var(--green)' : 'var(--red)' }}>
                {pathTestStatus.local_path?.ok ? '✓ Found' : '✗ Not found inside container'}
              </span>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14 }}>
          {!pathTestStatus && (
            <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Click Test Paths to verify these are visible inside the container</span>
          )}
          {pathTestStatus === 'loading' && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>Testing…</span>
          )}
          {pathTestStatus?.error && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--red)' }}>✗ {pathTestStatus.error}</span>
          )}
          <button onClick={handleTestPaths} style={{ padding: '7px 14px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)', fontSize: 12, cursor: 'pointer' }}>
            Test Paths
          </button>
        </div>
        <div style={{ marginTop: 16, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <button onClick={() => setBrowserOpen(o => !o)} style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
            {browserOpen ? '▼' : '▶'} Browse container filesystem
          </button>
          {browserOpen && (
            <DataBrowser
              onSelectMedia={v => { set('MEDIA_PATH')(v); setPathTestStatus(null); setPersistentWarnings([]) }}
              onSelectTorrents={v => { set('LOCAL_PATH')(v); setPathTestStatus(null); setPersistentWarnings([]) }}
            />
          )}
        </div>
        <div style={{ marginTop: 18 }}>
          <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', display: 'block', marginBottom: 5 }}>Advanced: multiple Sonarr/Radarr instances</label>
          <span style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45, display: 'block', marginBottom: 8 }}>
            Optional JSON array. Leave empty to use the single Sonarr/Radarr fields above. Each entry needs <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>id</span>, <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>service</span>, <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>base_url</span>, and <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>api_key</span>. Add <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>media_path</span> and <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>local_media_path</span> when an Arr container sees a different library path than auditorr.
          </span>
          <textarea
            value={arrConnectionsText}
            onChange={e => { setArrConnectionsText(e.target.value); setIsDirty(true); setArrTestStatus(null) }}
            onFocus={() => setArrConnectionsFocused(true)}
            onBlur={() => setArrConnectionsFocused(false)}
            placeholder={'[\n  {\n    "id": "radarr-4k",\n    "service": "radarr",\n    "name": "4K Radarr",\n    "base_url": "http://192.168.1.x:7878",\n    "api_key": "paste key",\n    "media_path": "/movies-4k",\n    "local_media_path": "/data/media/movies-4k"\n  }\n]'}
            style={{
              width: '100%', height: 140, padding: '8px 11px',
              borderRadius: 'var(--r)',
              border: `1px solid ${arrConnectionsFocused ? 'var(--accent)' : 'var(--border2)'}`,
              background: 'var(--surface2)', color: 'var(--text)',
              fontFamily: 'var(--mono)', fontSize: 12,
              outline: 'none', resize: 'vertical',
              transition: 'border 0.12s', boxSizing: 'border-box',
            }}
          />
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10, flexWrap: 'wrap' }}>
            <button onClick={handleTestArrConnections} style={{ padding: '6px 12px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 11, cursor: 'pointer' }}>
              {arrTestStatus?.loading ? 'Testing…' : 'Test Arr Connections'}
            </button>
            {arrTestStatus && !arrTestStatus.loading && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: arrTestStatus.ok ? 'var(--green)' : 'var(--red)' }}>
                {arrTestStatus.ok ? '✓ Connected' : `✗ ${arrTestStatus.msg || 'Connection check failed'}`}
              </span>
            )}
          </div>
          {arrTestStatus?.connections?.length > 0 && (
            <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 4 }}>
              {arrTestStatus.connections.map(conn => (
                <div key={conn.id} style={{ fontFamily: 'var(--mono)', fontSize: 11, color: conn.ok ? 'var(--text-dim)' : 'var(--red)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {conn.ok ? '✓' : '✗'} {conn.name || conn.id} · {conn.service} · {conn.managed_file_count || 0} managed file{conn.managed_file_count === 1 ? '' : 's'}
                </div>
              ))}
            </div>
          )}
        </div>
      </Card>

      {saveWarnings.length > 0 && (
        <div style={{
          marginBottom: 16, padding: '12px 16px',
          borderRadius: 'var(--rl)', border: '1px solid #f59e0b',
          background: '#f59e0b12',
        }}>
          {saveWarnings.map((w, i) => (
            <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontFamily: 'var(--mono)', fontSize: 12, color: '#f59e0b', lineHeight: 1.5 }}>
              <span style={{ flexShrink: 0 }}>⚠</span>
              <span>Path warning — {w}</span>
            </div>
          ))}
        </div>
      )}

      <Card title="Watchdog & Scheduled Audits">
        <div style={{ ...g2 }}>
          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', display: 'block', marginBottom: 5 }}>Filesystem Watchdog</label>
            <span style={{ fontSize: 11, color: 'var(--text-dim)', display: 'block', marginBottom: 10, lineHeight: 1.45 }}>
              Re-audit automatically when files change. Disable to avoid constant rescans on large libraries with frequent downloads.
            </span>
            <div style={{ display: 'flex', marginBottom: 14 }}>
              {[{ label: 'Enabled', value: true }, { label: 'Disabled', value: false }].map((opt, i) => (
                <button key={String(opt.value)} onClick={() => { setWatchdogEnabled(opt.value); setIsDirty(true) }} style={{
                  padding: '7px 18px',
                  borderRadius: i === 0 ? '99px 0 0 99px' : '0 99px 99px 0',
                  border: `1px solid ${watchdogEnabled === opt.value ? 'var(--accent)' : 'var(--border2)'}`,
                  borderRight: i === 0 ? 'none' : undefined,
                  background: watchdogEnabled === opt.value ? 'var(--accent)18' : 'transparent',
                  color: watchdogEnabled === opt.value ? 'var(--accent)' : 'var(--text-dim)',
                  fontSize: 12, fontWeight: 500, cursor: 'pointer', transition: 'all 0.12s',
                }}>{opt.label}</button>
              ))}
            </div>
            <div style={{ opacity: watchdogEnabled ? 1 : 0.4, pointerEvents: watchdogEnabled ? undefined : 'none' }}>
              <Field label="Watchdog Cooldown (seconds)" type="number"
                hint="After a filesystem change is detected, wait this many seconds before triggering an audit. Default: 60."
                placeholder="60" value={conf.WATCHDOG_COOLDOWN} onChange={set('WATCHDOG_COOLDOWN')} />
            </div>
          </div>
          <div>
            <Field label="Scheduled Interval (minutes)" type="number"
              hint="Run an audit every N minutes regardless of watchdog activity. Catches missed changes on NFS/bind mounts. Default: 360 (6h)."
              placeholder="360" value={conf.SCHEDULED_INTERVAL} onChange={set('SCHEDULED_INTERVAL')} />
          </div>
        </div>
      </Card>

      <Card title="Integrations">
        <p style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.55, marginBottom: 18 }}>
          Required for interactive search in the Media explorer. API keys found in each app under Settings → General.
        </p>
        <div style={g2}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Field label="Sonarr URL" placeholder="http://192.168.1.x:8989" value={conf.SONARR_URL} onChange={set('SONARR_URL')} />
            <Field label="Sonarr API Key" type="password" placeholder="paste API key…" value={conf.SONARR_API_KEY} onChange={set('SONARR_API_KEY')} />
            <Field label="Sonarr Remote Path" type="text"
              hint="The path to your downloads folder as Sonarr sees it inside its container. Leave blank if Sonarr and auditorr share the same paths."
              placeholder="/downloads or /data/torrents"
              value={conf.SONARR_REMOTE_PATH} onChange={set('SONARR_REMOTE_PATH')} />
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              {sonarrTestStatus && (
                <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: sonarrTestStatus.ok ? 'var(--green)' : 'var(--red)' }}>
                  {sonarrTestStatus.loading ? 'Testing…' : (sonarrTestStatus.ok ? '✓ ' : '✗ ') + sonarrTestStatus.msg}
                </span>
              )}
              <button onClick={handleTestSonarr} style={{ padding: '7px 14px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)', fontSize: 12, cursor: 'pointer' }}>
                Test Sonarr
              </button>
            </div>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Field label="Radarr URL" placeholder="http://192.168.1.x:7878" value={conf.RADARR_URL} onChange={set('RADARR_URL')} />
            <Field label="Radarr API Key" type="password" placeholder="paste API key…" value={conf.RADARR_API_KEY} onChange={set('RADARR_API_KEY')} />
            <Field label="Radarr Remote Path" type="text"
              hint="The path to your downloads folder as Radarr sees it inside its container. Leave blank if Radarr and auditorr share the same paths."
              placeholder="/downloads or /data/torrents"
              value={conf.RADARR_REMOTE_PATH} onChange={set('RADARR_REMOTE_PATH')} />
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              {radarrTestStatus && (
                <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: radarrTestStatus.ok ? 'var(--green)' : 'var(--red)' }}>
                  {radarrTestStatus.loading ? 'Testing…' : (radarrTestStatus.ok ? '✓ ' : '✗ ') + radarrTestStatus.msg}
                </span>
              )}
              <button onClick={handleTestRadarr} style={{ padding: '7px 14px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)', fontSize: 12, cursor: 'pointer' }}>
                Test Radarr
              </button>
            </div>
          </div>
        </div>
      </Card>

      <Card title="Health Score Thresholds">
        <p style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.55, marginBottom: 18 }}>
          Each threshold defines the size limit for that category relative to your total torrent library.
          Points are lost <em>linearly</em> as you approach the threshold — at exactly the threshold value all 10 points are gone.
          For example, a <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>1%</span> threshold means
          you start losing points immediately if any problem data exists, and lose all 10 points once it reaches 1% of your library.
          Lower = stricter. Hardlinked Media accounts for 70 pts; each category below accounts for 10 pts.
        </p>
        <div style={g3}>
          <Field label="Orphaned Torrent Threshold" type="number"
            suffix="%"
            hint={thresholdHint('orphaned torrent')}
            placeholder="1" value={orPct} onChange={v => { setOrPct(v); setIsDirty(true) }} />
          <Field label="Not Imported Threshold" type="number"
            suffix="%"
            hint={thresholdHint('unlinked seeding')}
            placeholder="1" value={niPct} onChange={v => { setNiPct(v); setIsDirty(true) }} />
          <Field label="Duplicate Files Threshold" type="number"
            suffix="%"
            hint={thresholdHint('duplicate file')}
            placeholder="1" value={dupPct} onChange={v => { setDupPct(v); setIsDirty(true) }} />
        </div>

        {/* Live score preview */}
        <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
          {[
            { label: 'Orphaned', pct: orPct,  size: null },
            { label: 'Not Imported', pct: niPct,  size: null },
            { label: 'Duplicates', pct: dupPct, size: null },
          ].map(({ label, pct }) => {
            const v = parseFloat(pct)
            if (!v || isNaN(v)) return null
            return (
              <div key={label} style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', background: 'var(--surface2)', borderRadius: 'var(--r)', padding: '6px 10px' }}>
                {label}: all 10 pts lost at <span style={{ color: 'var(--accent)' }}>{v}%</span> of library
              </div>
            )
          })}
        </div>
      </Card>

      <Card title="Exclusion Folders & Patterns">
        <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', display: 'block', marginBottom: 5 }}>Ignored folders or files</label>
        <span style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45, display: 'block', marginBottom: 8 }}>
          One rule per line. Add unmanaged torrent folders here to keep books, music, games, or other categories out of Not Imported reporting. Plain folder paths work; <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>torrents/books</span>, <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>data/torrents/books</span>, <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>/data/torrents/books</span>, and <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>/mnt/user/data/torrents/books</span> all target the same TRaSH-style folder. File names, directory names, and legacy globs still work.
        </span>
        <textarea
          value={exclusionPatterns}
          onChange={e => { setExclusionPatterns(e.target.value); setIsDirty(true) }}
          onFocus={() => setExclusionFocused(true)}
          onBlur={() => setExclusionFocused(false)}
          placeholder={'torrents/games\ntorrents/lidarr\ntorrents/music\ntorrents/books\nmedia/music\n**/.plexmatch\n*.srt'}
          style={{
            width: '100%', height: 120, padding: '8px 11px',
            borderRadius: 'var(--r)',
            border: `1px solid ${exclusionFocused ? 'var(--accent)' : 'var(--border2)'}`,
            background: 'var(--surface2)', color: 'var(--text)',
            fontFamily: 'var(--mono)', fontSize: 12,
            outline: 'none', resize: 'vertical',
            transition: 'border 0.12s', boxSizing: 'border-box',
          }}
        />
        <div style={{ marginTop: 14, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Media server sidecar presets</div>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45, marginBottom: 9 }}>
            Ignore metadata and artwork sidecars commonly written or read by media servers, such as <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>.plexmatch</span>, <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>.nfo</span>, <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>poster.jpg</span>, <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>fanart.jpg</span>, and <span style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>folder.jpg</span>.
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {MEDIA_SERVER_PRESETS.map(preset => {
              const active = mediaServerPresets.includes(preset.id)
              return (
                <button
                  key={preset.id}
                  onClick={() => toggleMediaPreset(preset.id)}
                  style={{
                    padding: '6px 12px', borderRadius: 'var(--r)', fontSize: 12,
                    border: `1px solid ${active ? 'var(--accent)' : 'var(--border2)'}`,
                    background: active ? 'var(--accent)18' : 'transparent',
                    color: active ? 'var(--accent)' : 'var(--text-dim)',
                    cursor: 'pointer', transition: 'all 0.12s',
                  }}
                >
                  {preset.label}
                </button>
              )
            })}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 14 }}>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 3 }}>File explorer visibility</div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>Control whether excluded files appear in the file explorer.</div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexShrink: 0, marginLeft: 24 }}>
            {[
              { value: false, label: 'Visible' },
              { value: true,  label: 'Hidden'  },
            ].map(opt => (
              <button
                key={String(opt.value)}
                onClick={() => { setExclusionHideFromExplorer(opt.value); setIsDirty(true) }}
                style={{
                  padding: '7px 18px', borderRadius: 'var(--r)', fontSize: 12, fontWeight: 500,
                  border: `1px solid ${exclusionHideFromExplorer === opt.value ? 'var(--accent)' : 'var(--border2)'}`,
                  background: exclusionHideFromExplorer === opt.value ? 'var(--accent)18' : 'transparent',
                  color: exclusionHideFromExplorer === opt.value ? 'var(--accent)' : 'var(--text-dim)',
                  cursor: 'pointer', transition: 'all 0.12s',
                }}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      </Card>

      <Card title="Appearance">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Theme</div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>Choose between dark and light mode. Dark is the default.</div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexShrink: 0, marginLeft: 24 }}>
            {['dark', 'light'].map(t => (
              <button
                key={t}
                onClick={() => onThemeChange && onThemeChange(t)}
                style={{
                  padding: '7px 18px', borderRadius: 'var(--r)', fontSize: 12, fontWeight: 500,
                  border: `1px solid ${theme === t ? 'var(--accent)' : 'var(--border2)'}`,
                  background: theme === t ? 'var(--accent)18' : 'transparent',
                  color: theme === t ? 'var(--accent)' : 'var(--text-dim)',
                  cursor: 'pointer', transition: 'all 0.12s',
                }}
              >
                {t === 'dark' ? '🌙 Dark' : '☀️ Light'}
              </button>
            ))}
          </div>
        </div>
      </Card>

      <Card title="Audit History">
        <div style={{ marginBottom: 14 }}>
          <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>
            {dedupedRuns.length} audit runs. History is stored indefinitely in SQLite and survives restarts.
          </span>
        </div>

        {!auditRuns ? (
          <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', padding: '12px 0' }}>Loading…</div>
        ) : dedupedRuns.length === 0 ? (
          <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', padding: '12px 0' }}>No audit runs recorded yet.</div>
        ) : (
          <div style={{ border: '1px solid var(--border)', borderRadius: 'var(--r)', overflow: 'hidden' }}>
            <div style={{
              display: 'grid', gridTemplateColumns: AUDIT_COLS,
              background: 'var(--surface2)', borderBottom: '1px solid var(--border)',
            }}>
              {['Time', 'Trigger', 'Source', 'Score', 'Duration', 'Status'].map(h => (
                <div key={h} style={{ padding: '8px 12px', fontSize: 11, letterSpacing: 1.5, textTransform: 'uppercase', color: 'var(--text-dim)', fontWeight: 600, fontFamily: 'var(--mono)' }}>{h}</div>
              ))}
            </div>
            <FixedSizeList
              height={Math.min(dedupedRuns.length * AUDIT_ROW_H, 320)}
              itemCount={dedupedRuns.length}
              itemSize={AUDIT_ROW_H}
              itemData={dedupedRuns}
              width="100%"
            >
              {AuditRunRow}
            </FixedSizeList>
          </div>
        )}
      </Card>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingTop: 16, borderTop: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
            Last audit: {lastAuditTime}
          </span>
          <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            Watchdog (if enabled) re-audits on file changes. Scheduled interval runs independently regardless of watchdog state. Use the button below to trigger one manually.
          </span>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexShrink: 0, marginLeft: 20 }}>
          {saveStatus && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: saveStatus.ok ? 'var(--green)' : 'var(--red)' }}>
              {saveStatus.ok ? '✓ ' : '✗ '}{saveStatus.msg}
            </span>
          )}
          <button onClick={handleSave} style={{ position: 'relative', padding: '7px 18px', borderRadius: 'var(--r)', border: 'none', background: 'var(--accent)', color: '#000', fontWeight: 700, fontSize: 12, cursor: 'pointer' }}>
            {isDirty && <span style={{ position: 'absolute', top: 4, right: 4, width: 7, height: 7, borderRadius: '50%', background: '#f59e0b', display: 'block' }} />}
            Save Settings
          </button>
          {onScan && (
            <button
              onClick={onScan}
              disabled={isScanning}
              style={{
                padding: '7px 18px', borderRadius: 'var(--r)',
                border: '1px solid var(--border2)',
                background: 'transparent',
                color: isScanning ? 'var(--text-dim)' : 'var(--text)',
                fontSize: 12, fontWeight: 500, cursor: isScanning ? 'default' : 'pointer',
                opacity: isScanning ? 0.5 : 1,
              }}
            >
              {isScanning ? 'Scanning…' : '▶ Run Audit'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
