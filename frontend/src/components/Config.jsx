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
const DISC_RIP_PRESETS = [
  { id: 'bluray', label: 'Blu-ray Disc' },
  { id: 'dvd', label: 'DVD Disc' },
]
const ARR_SERVICES = [
  { id: 'sonarr', label: 'Sonarr', port: '8989' },
  { id: 'radarr', label: 'Radarr', port: '7878' },
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

// Collapsed disclosure for the browser-facing link addresses.
//
// Named for the symptom, not the category: someone arrives here because their
// links open the wrong address, and nobody expands a section called "Advanced"
// looking for that. Once values are set the collapsed row says so — a setting
// that is silently active is one people forget they turned on.
//
// There is no Test button, deliberately. The only meaningful test of a link
// address is "does my browser reach it", which the server cannot answer and
// must not try — it never fetches these. So the affordance is an open ↗ link,
// which asks the one thing that can actually answer the question.
function ExternalUrlSection({ fields }) {
  const [open, setOpen] = useState(false)
  const setCount = fields.filter(f => (f.value || '').trim()).length
  return (
    <div style={{ marginTop: 16, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
      <button onClick={() => setOpen(o => !o)} style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
        {open ? '▼' : '▶'} External URLs (reverse proxy){setCount > 0 ? ` — ${setCount} set` : ''}
      </button>
      {open && (
        <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 14 }}>
          <p style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.55, margin: 0 }}>
            Only needed if you reach these apps at a different address than auditorr does —
            a reverse proxy or Tailscale domain. auditorr keeps using the host above for its
            own API calls; this is only what the “open ↗” buttons link to.
            <strong style={{ color: 'var(--text)' }}> If your links already open correctly, leave these blank.</strong>
          </p>
          {fields.map(f => (
            <div key={f.key}>
              <Field
                label={f.label}
                placeholder={f.placeholder}
                value={f.value}
                onChange={f.onChange}
              />
              {(f.value || '').trim() && (
                <a href={f.value} target="_blank" rel="noopener noreferrer"
                  style={{ display: 'inline-block', marginTop: 5, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--accent)', textDecoration: 'none' }}>
                  open ↗
                </a>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// The four score categories, in dashboard-card order and carrying the same
// accent each card uses — the donut is meant to read as "those four cards, as
// proportions", not as a new set of colors to learn.
const SCORE_CATEGORIES = [
  { key: 'WEIGHT_HARDLINKED',   label: 'Hardlinked Media',   color: 'var(--blue)',
    hint: 'Share of your library hardlinked back to a torrent.' },
  { key: 'WEIGHT_ORPHANED',     label: 'Orphaned Torrents',  color: 'var(--yellow)',
    hint: 'Torrent-folder files your client has no knowledge of.' },
  { key: 'WEIGHT_NOT_IMPORTED', label: 'Not Imported',       color: 'var(--red)',
    hint: 'Seeding torrents with no matching library file.' },
  { key: 'WEIGHT_DUPLICATES',   label: 'Duplicate Files',    color: 'var(--purple)',
    hint: 'Identical files that share no inode.' },
]

// Mirrors the backend defaults in db.py — they sum to 100, so an untouched
// install reads its weights and its points as the same four numbers.
const DEFAULT_WEIGHTS = {
  WEIGHT_HARDLINKED: 70, WEIGHT_ORPHANED: 10,
  WEIGHT_NOT_IMPORTED: 10, WEIGHT_DUPLICATES: 10,
}

// Normalize raw weights to points out of 100. Mirrors score_weight_points()
// in db.py so the preview always matches what the next audit will compute.
function weightPoints(weights) {
  const raw = SCORE_CATEGORIES.map(cat => {
    const v = parseFloat(weights[cat.key])
    return isNaN(v) || v < 0 ? 0 : v
  })
  const total = raw.reduce((a, b) => a + b, 0)
  return Object.fromEntries(SCORE_CATEGORIES.map((cat, i) =>
    [cat.key, total > 0 ? (raw[i] / total) * 100 : 0]))
}

// Donut readout for the score weighting. Deliberately not draggable: the
// numbers are entered in the fields beside it, and this shows what they mean.
function WeightDonut({ points, hovered }) {
  const SIZE = 150
  const CX = SIZE / 2, CY = SIZE / 2
  const R_OUTER = 62, R_INNER = 42
  const GAP_DEG = 2.5   // breathing room between segments

  function polarToXY(cx, cy, r, angleDeg) {
    const rad = (angleDeg - 90) * Math.PI / 180
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) }
  }
  function arcPath(cx, cy, rO, rI, s, e) {
    const s1 = polarToXY(cx, cy, rO, s), e1 = polarToXY(cx, cy, rO, e)
    const s2 = polarToXY(cx, cy, rI, e), e2 = polarToXY(cx, cy, rI, s)
    const large = (e - s) > 180 ? 1 : 0
    return `M ${s1.x} ${s1.y} A ${rO} ${rO} 0 ${large} 1 ${e1.x} ${e1.y} L ${s2.x} ${s2.y} A ${rI} ${rI} 0 ${large} 0 ${e2.x} ${e2.y} Z`
  }

  const visible = SCORE_CATEGORIES.filter(c => (points[c.key] || 0) > 0.05)
  const segments = []
  let cursor = 0
  for (const cat of visible) {
    const sweep = (points[cat.key] / 100) * 360
    // A lone category owns the full ring — a gap there would render as a
    // hairline slit in an otherwise solid circle, which reads as a bug.
    const gap = visible.length > 1 ? GAP_DEG : 0
    const isHovered = hovered === cat.key
    const grow = isHovered ? 3 : 0
    segments.push({
      key: cat.key, color: cat.color, dim: hovered && !isHovered,
      // An SVG arc whose start and end coincide draws nothing, so a category
      // holding every point is rendered as a ring rather than a 360° wedge.
      full: sweep >= 359.9,
      rOuter: R_OUTER + grow, rInner: R_INNER - (isHovered ? 1 : 0),
      path: arcPath(CX, CY, R_OUTER + grow, R_INNER - (isHovered ? 1 : 0),
                    cursor + gap / 2, cursor + sweep - gap / 2),
    })
    cursor += sweep
  }

  return (
    <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} style={{ flexShrink: 0 }}>
      <circle cx={CX} cy={CY} r={(R_OUTER + R_INNER) / 2} fill="none"
        stroke="var(--border)" strokeWidth={R_OUTER - R_INNER} opacity={0.35} />
      {segments.map(seg => (seg.full ? (
        <circle key={seg.key} cx={CX} cy={CY} r={(seg.rOuter + seg.rInner) / 2}
          fill="none" stroke={seg.color} strokeWidth={seg.rOuter - seg.rInner}
          opacity={seg.dim ? 0.3 : 0.85} style={{ transition: 'opacity 0.12s' }} />
      ) : (
        <path key={seg.key} d={seg.path} fill={seg.color}
          opacity={seg.dim ? 0.3 : 0.85}
          style={{ transition: 'opacity 0.12s' }} />
      )))}
      <text x={CX} y={CY - 3} textAnchor="middle" dominantBaseline="middle"
        style={{ fontFamily: 'var(--mono)', fontSize: 22, fontWeight: 700, fill: 'var(--text)' }}>
        100
      </text>
      <text x={CX} y={CY + 16} textAnchor="middle" dominantBaseline="middle"
        style={{ fontFamily: 'var(--mono)', fontSize: 10, fill: 'var(--text-dim)' }}>
        pts
      </text>
    </svg>
  )
}

function Card({ title, children }) {
  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', padding: 24, marginBottom: 16 }}>
      <div style={{ fontFamily: 'var(--sans)', fontSize: 13, fontWeight: 600, letterSpacing: 0, textTransform: 'none', textAlign: 'left', color: 'var(--text)', paddingBottom: 14, marginBottom: 18, borderBottom: '1px solid var(--border)' }}>{title}</div>
      {children}
    </div>
  )
}

// Segmented selector — separate pill buttons, matching the app's standard
// toggle style (Workflow torrent deletion, Theme, etc.). `value` is compared
// to each option's `value` with ===; pass a normalized value for booleans.
function SegToggle({ options, value, onChange, size = 'md' }) {
  const pad = size === 'sm' ? '5px 12px' : '7px 18px'
  const fontSize = size === 'sm' ? 11 : 12
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      {options.map(opt => {
        const active = value === opt.value
        return (
          <button
            key={String(opt.value)}
            onClick={() => onChange(opt.value)}
            style={{
              padding: pad, borderRadius: 'var(--r)', fontSize, fontWeight: 500,
              border: `1px solid ${active ? 'var(--accent)' : 'var(--border2)'}`,
              background: active ? 'var(--accent)18' : 'transparent',
              color: active ? 'var(--accent)' : 'var(--text-dim)',
              cursor: 'pointer', transition: 'all 0.12s',
            }}
          >
            {opt.label}
          </button>
        )
      })}
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
  // Score weights, held as strings so the fields can be cleared while typing.
  const [weights,      setWeights]      = useState({})
  const [hoveredWeight, setHoveredWeight] = useState(null)
  const [exclusionPatterns,        setExclusionPatterns]        = useState('')
  const [exclusionFocused,         setExclusionFocused]         = useState(false)
  const [discRipPresets,           setDiscRipPresets]           = useState([])
  const [mediaServerPresets,       setMediaServerPresets]       = useState([])
  const [exclusionHideFromExplorer, setExclusionHideFromExplorer] = useState(false)
  const [arrConnections,           setArrConnections]           = useState([])
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
      setWeights(Object.fromEntries(SCORE_CATEGORIES.map(cat =>
        [cat.key, String(c[cat.key] ?? DEFAULT_WEIGHTS[cat.key])])))
      setExclusionPatterns((c.EXCLUSION_PATTERNS || []).join('\n'))
      setDiscRipPresets(Array.isArray(c.DISC_RIP_EXCLUSION_PRESETS) ? c.DISC_RIP_EXCLUSION_PRESETS : [])
      setMediaServerPresets(Array.isArray(c.MEDIA_SERVER_EXCLUSION_PRESETS) ? c.MEDIA_SERVER_EXCLUSION_PRESETS : [])
      setExclusionHideFromExplorer(!!c.EXCLUSION_HIDE_FROM_EXPLORER)
      setArrConnections((c.ARR_CONNECTIONS || []).map(conn => ({
        id: conn.id || '',
        _original_id: conn.id || '',
        _stored_api_key: conn.api_key === '__stored__',
        service: conn.service || 'sonarr',
        name: conn.name || '',
        base_url: conn.base_url || conn.url || '',
        external_url: conn.external_url || '',
        api_key: conn.api_key === '__stored__' ? '' : (conn.api_key || ''),
        media_path: conn.media_path || '',
        local_media_path: conn.local_media_path || '',
      })))
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
      if (onConfigSaved) onConfigSaved()
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

  const cleanArrConnections = () => {
    const seen = new Set()
    return arrConnections
      .map((conn, i) => {
        const service = (conn.service || 'sonarr').toLowerCase()
        const label = service === 'radarr' ? 'radarr' : 'sonarr'
        const name = (conn.name || '').trim()
        const id = (conn.id || name || `${label}-${i + 1}`)
          .trim()
          .toLowerCase()
          .replace(/[^a-z0-9_-]+/g, '-')
          .replace(/^-+|-+$/g, '') || `${label}-${i + 1}`
        const uniqueId = seen.has(id) ? `${id}-${i + 1}` : id
        seen.add(uniqueId)
        return {
          id: uniqueId,
          service: label,
          name,
          base_url: (conn.base_url || '').trim(),
          external_url: (conn.external_url || '').trim(),
          api_key: conn.api_key || '',
          media_path: (conn.media_path || '').trim(),
          local_media_path: (conn.local_media_path || '').trim(),
        }
      })
      .filter(conn => conn.base_url || conn.api_key || conn.name || conn.media_path || conn.local_media_path)
  }

  const handleTestArrConnections = async () => {
    setArrTestStatus({ loading: true })
    try {
      const result = await api.testArrConnections({ ...conf, ARR_CONNECTIONS: cleanArrConnections() })
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
    if (Object.values(weightPoints(weights)).every(p => p <= 0)) {
      setSaveStatus({ ok: false, msg: 'At least one score category must have a weight above zero' })
      return
    }
    const sourceChanged = conf.TORRENT_SOURCE !== savedSource
    setPersistentWarnings([])
    const payload = {
      ...conf,
      OR_RATIO:  parseFloat(orPct)  / 100 || 0.01,
      NI_RATIO:  parseFloat(niPct)  / 100 || 0.01,
      DUP_RATIO: parseFloat(dupPct) / 100 || 0.01,
      // A blank field means zero here, not "use the default" — clearing a box
      // is how you switch a category off.
      ...Object.fromEntries(SCORE_CATEGORIES.map(cat => {
        const v = parseFloat(weights[cat.key])
        return [cat.key, isNaN(v) || v < 0 ? 0 : v]
      })),
      EXCLUSION_PATTERNS:           exclusionPatterns.split('\n').map(p => p.trim()).filter(Boolean),
      DISC_RIP_EXCLUSION_PRESETS:   discRipPresets,
      MEDIA_SERVER_EXCLUSION_PRESETS: mediaServerPresets,
      EXCLUSION_HIDE_FROM_EXPLORER: exclusionHideFromExplorer,
      ARR_CONNECTIONS:              cleanArrConnections(),
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

  // Every category at zero leaves nothing to score — the backend rejects it,
  // so block the save here and say why rather than surface a generic error.
  const allWeightsZero = Object.values(weightPoints(weights)).every(p => p <= 0)
  const weightsAreDefault = SCORE_CATEGORIES.every(cat =>
    parseFloat(weights[cat.key]) === DEFAULT_WEIGHTS[cat.key])
  const resetWeights = () => {
    setWeights(Object.fromEntries(SCORE_CATEGORIES.map(cat =>
      [cat.key, String(DEFAULT_WEIGHTS[cat.key])])))
    setIsDirty(true)
  }

  const formGrid = { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 14 }
  const compactGrid = { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 10 }
  const ghostButton = {
    padding: '6px 12px',
    borderRadius: 'var(--r)',
    border: '1px solid var(--border2)',
    background: 'var(--surface2)',
    color: 'var(--text)',
    fontSize: 12,
    fontWeight: 600,
    cursor: 'pointer',
  }
  const smallMonoButton = {
    ...ghostButton,
    fontFamily: 'var(--sans)',
    fontSize: 12,
  }

  const toggleMediaPreset = id => {
    setMediaServerPresets(prev => prev.includes(id) ? prev.filter(p => p !== id) : [...prev, id])
    setIsDirty(true)
  }

  const toggleDiscPreset = id => {
    setDiscRipPresets(prev => prev.includes(id) ? prev.filter(p => p !== id) : [...prev, id])
    setIsDirty(true)
  }

  const setArrConnection = (index, key, value) => {
    setArrConnections(prev => prev.map((conn, i) => i === index ? { ...conn, [key]: value } : conn))
    setArrTestStatus(null)
    setIsDirty(true)
  }

  const addArrConnection = (service = 'sonarr') => {
    const base = ARR_SERVICES.find(s => s.id === service) || ARR_SERVICES[0]
    setArrConnections(prev => [
      ...prev,
      {
        id: `${base.id}-${prev.filter(c => c.service === base.id).length + 1}`,
        service: base.id,
        name: '',
        base_url: '',
        api_key: '',
        media_path: '',
        local_media_path: '',
      },
    ])
    setArrTestStatus(null)
    setIsDirty(true)
  }

  const removeArrConnection = index => {
    setArrConnections(prev => prev.filter((_, i) => i !== index))
    setArrTestStatus(null)
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

  // The point figure follows the weighting above, so the hint stays truthful
  // when a category is reweighted — and says so plainly when it is switched off.
  const thresholdHint = (label, key) => {
    const max = weightPoints(weights)[key]
    if (!(max > 0.05)) return `${label} data is not scored — this threshold has no effect.`
    return `All ${+max.toFixed(1)} pts lost when ${label} data reaches this % of your library. Points lost proportionally below that.`
  }

  return (
    <div className="fade-in" style={{ padding: 24, maxWidth: 800 }}>

      <Card title="Torrent Source">
        {/* Source toggle */}
        <div style={{ marginBottom: 18 }}>
          <SegToggle
            options={[{ value: 'qbit', label: 'qBittorrent' }, { value: 'qui', label: 'qui' }]}
            value={conf.TORRENT_SOURCE}
            onChange={src => { set('TORRENT_SOURCE')(src); setTestStatus(null); setSourceInfo(null); setIsDirty(true) }}
          />
        </div>

        {!isQui ? (
          <>
            <Field label="Host URL" placeholder="http://192.168.1.x:8080" value={conf.QB_HOST} onChange={v => { set('QB_HOST')(v); setSourceInfo(null) }} style={{ marginBottom: 14 }} />
            <div style={formGrid}>
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
        <ExternalUrlSection fields={isQui
          ? [{ key: 'QUI_EXTERNAL_URL', label: 'qui External URL', placeholder: 'https://qui.example.com',
               value: conf.QUI_EXTERNAL_URL, onChange: set('QUI_EXTERNAL_URL') }]
          : [{ key: 'QB_EXTERNAL_URL', label: 'qBittorrent External URL', placeholder: 'https://qbit.example.com',
               value: conf.QB_EXTERNAL_URL, onChange: set('QB_EXTERNAL_URL') }]} />
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
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 16, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 3 }}>Workflow torrent deletion</div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
              Allow workflow pages to delete torrents and their files directly via {isQui ? 'qui' : 'qBittorrent'}. Off keeps auditorr read-only against your client.
            </div>
          </div>
          <div style={{ flexShrink: 0, marginLeft: 24 }}>
            <SegToggle
              options={[{ value: false, label: 'Disallowed' }, { value: true, label: 'Allowed' }]}
              value={!!conf.ALLOW_CLIENT_DELETE}
              onChange={v => set('ALLOW_CLIENT_DELETE')(v)}
            />
          </div>
        </div>
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
        <div style={formGrid}>
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
        <div style={formGrid}>
          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', display: 'block', marginBottom: 5 }}>Filesystem Watchdog</label>
            <span style={{ fontSize: 11, color: 'var(--text-dim)', display: 'block', marginBottom: 10, lineHeight: 1.45 }}>
              Re-audit automatically when files change. Disable to avoid constant rescans on large libraries with frequent downloads.
            </span>
            <div style={{ marginBottom: 14 }}>
              <SegToggle
                options={[{ value: true, label: 'Enabled' }, { value: false, label: 'Disabled' }]}
                value={watchdogEnabled}
                onChange={v => { setWatchdogEnabled(v); setIsDirty(true) }}
              />
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
        <div style={formGrid}>
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
              <button onClick={handleTestSonarr} style={ghostButton}>
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
              <button onClick={handleTestRadarr} style={ghostButton}>
                Test Radarr
              </button>
            </div>
          </div>
        </div>
        <ExternalUrlSection fields={[
          { key: 'SONARR_EXTERNAL_URL', label: 'Sonarr External URL', placeholder: 'https://sonarr.example.com',
            value: conf.SONARR_EXTERNAL_URL, onChange: set('SONARR_EXTERNAL_URL') },
          { key: 'RADARR_EXTERNAL_URL', label: 'Radarr External URL', placeholder: 'https://radarr.example.com',
            value: conf.RADARR_EXTERNAL_URL, onChange: set('RADARR_EXTERNAL_URL') },
        ]} />
        <div style={{ marginTop: 18, borderTop: '1px solid var(--border)', paddingTop: 14 }}>
          <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', display: 'block', marginBottom: 5 }}>Additional Sonarr/Radarr instances</label>
          <span style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45, display: 'block', marginBottom: 10 }}>
            Add extra Sonarr or Radarr servers for split libraries such as 4K, anime, or kids. These are used <em>alongside</em> the primary Sonarr/Radarr above, not instead of them.
          </span>
          {arrConnections.length === 0 ? (
            <div style={{ padding: '10px 12px', border: '1px dashed var(--border2)', borderRadius: 'var(--r)', color: 'var(--text-dim)', fontSize: 11, marginBottom: 10 }}>
              No additional Arr instances configured.
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 10 }}>
              {arrConnections.map((conn, index) => {
                const service = conn.service === 'radarr' ? 'radarr' : 'sonarr'
                const idChangedWithStoredKey = conn._stored_api_key && conn._original_id && conn.id !== conn._original_id && !conn.api_key
                return (
                  <div key={index} style={{ border: '1px solid var(--border)', borderRadius: 'var(--r)', padding: 12, background: 'var(--surface2)' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0, flexWrap: 'wrap' }}>
                        <SegToggle
                          size="sm"
                          options={ARR_SERVICES.map(s => ({ value: s.id, label: s.label }))}
                          value={service}
                          onChange={id => setArrConnection(index, 'service', id)}
                        />
                        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {conn.id || `${service}-${index + 1}`}
                        </span>
                        {conn._stored_api_key && !conn.api_key && (
                          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--green)', border: '1px solid var(--green)35', borderRadius: 'var(--r)', padding: '1px 6px' }}>stored key</span>
                        )}
                      </div>
                      <button onClick={() => removeArrConnection(index)} style={smallMonoButton}>
                        Remove
                      </button>
                    </div>
                    <div style={{ ...compactGrid, marginBottom: 10 }}>
                      <Field label="Label" placeholder={service === 'radarr' ? '4K Radarr' : 'Anime Sonarr'} value={conn.name} onChange={v => setArrConnection(index, 'name', v)} />
                      <Field label="ID" placeholder={`${service}-4k`} value={conn.id} onChange={v => setArrConnection(index, 'id', v)} />
                    </div>
                    <div style={{ ...compactGrid, marginBottom: 10 }}>
                      <Field label="URL" placeholder={`http://192.168.1.x:${ARR_SERVICES.find(s => s.id === service)?.port}`} value={conn.base_url} onChange={v => setArrConnection(index, 'base_url', v)} />
                      <Field label="API Key" type="password" placeholder={conn._stored_api_key ? '(stored - leave blank to keep current)' : 'paste API key...'} value={conn.api_key} onChange={v => setArrConnection(index, 'api_key', v)} />
                    </div>
                    {idChangedWithStoredKey && (
                      <div style={{ fontSize: 11, color: 'var(--yellow)', marginBottom: 10 }}>
                        Changing the ID requires re-entering this instance's API key before saving.
                      </div>
                    )}
                    <div style={{ ...compactGrid, marginBottom: 10 }}>
                      <Field label="External URL" placeholder="https://sonarr-4k.example.com"
                        hint="Optional. Where your browser reaches this instance, if that differs from the URL above."
                        value={conn.external_url} onChange={v => setArrConnection(index, 'external_url', v)} />
                    </div>
                    <div style={compactGrid}>
                      <Field label="Arr Media Path" placeholder="/movies or /tv" hint="Path as this Arr instance sees its library." value={conn.media_path} onChange={v => setArrConnection(index, 'media_path', v)} />
                      <Field label="Auditorr Media Path" placeholder="/data/media/movies" hint="Matching path inside auditorr. Leave blank when paths already match." value={conn.local_media_path} onChange={v => setArrConnection(index, 'local_media_path', v)} />
                    </div>
                  </div>
                )
              })}
            </div>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10, flexWrap: 'wrap' }}>
            <button onClick={() => addArrConnection('sonarr')} style={smallMonoButton}>
              Add Sonarr
            </button>
            <button onClick={() => addArrConnection('radarr')} style={smallMonoButton}>
              Add Radarr
            </button>
            <button onClick={handleTestArrConnections} style={smallMonoButton}>
              {arrTestStatus?.loading ? 'Testing...' : 'Test Arr Connections'}
            </button>
            {arrTestStatus && !arrTestStatus.loading && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: arrTestStatus.ok ? 'var(--green)' : 'var(--red)' }}>
                {arrTestStatus.ok ? 'Connected' : `${arrTestStatus.msg || 'Connection check failed'}`}
              </span>
            )}
          </div>
          {arrTestStatus?.connections?.length > 0 && (
            <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 4 }}>
              {arrTestStatus.connections.map(conn => (
                <div key={conn.id} style={{ fontFamily: 'var(--mono)', fontSize: 11, color: conn.ok ? 'var(--text-dim)' : 'var(--red)' }}>
                  <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {conn.ok ? 'ok' : 'error'} · {conn.name || conn.id} · {conn.service} · {conn.managed_file_count || 0} managed file{conn.managed_file_count === 1 ? '' : 's'}
                    {conn.message ? ` · ${conn.message}` : ''}
                  </div>
                  {/* The path this instance reports, after path mapping — the only
                      place you can see what auditorr will actually try to match
                      against your library, and the first thing to check when an
                      instance connects fine but resolves nothing. */}
                  {conn.sample_paths?.[0] && (
                    <div style={{ opacity: 0.7, paddingLeft: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      ↳ {conn.sample_paths[0]}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </Card>

      <Card title="Health Score">
        <p style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.55, marginBottom: 18 }}>
          How much each category counts toward your score out of 100. The numbers are relative —
          only their proportions matter, so you can type whatever expresses your priorities.
          Set a category to <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>0</span> to
          stop scoring it entirely; it still appears on the dashboard, just unscored.
        </p>

        <div style={{ display: 'flex', alignItems: 'center', gap: 28, flexWrap: 'wrap', marginBottom: 8 }}>
          <WeightDonut points={weightPoints(weights)} hovered={hoveredWeight} />
          <div style={{ flex: '1 1 320px', minWidth: 260, maxWidth: 400, display: 'flex', flexDirection: 'column', gap: 2 }}>
            {SCORE_CATEGORIES.map(cat => {
              const pts = weightPoints(weights)[cat.key]
              const off = !(pts > 0.05)
              return (
                <div key={cat.key}
                  onMouseEnter={() => setHoveredWeight(cat.key)}
                  onMouseLeave={() => setHoveredWeight(null)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: '6px 8px',
                    borderRadius: 'var(--r)',
                    background: hoveredWeight === cat.key ? 'var(--surface2)' : 'transparent',
                    transition: 'background 0.12s',
                  }}>
                  <span style={{
                    width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
                    background: cat.color, opacity: off ? 0.3 : 1,
                  }} />
                  <span style={{
                    flex: 1, fontSize: 12, minWidth: 0,
                    color: off ? 'var(--text-dim)' : 'var(--text)',
                  }} title={cat.hint}>{cat.label}</span>
                  <input
                    type="number" min="0" max="1000"
                    value={weights[cat.key] ?? ''}
                    onChange={e => { setWeights(w => ({ ...w, [cat.key]: e.target.value })); setIsDirty(true) }}
                    style={{
                      width: 62, padding: '5px 8px', borderRadius: 'var(--r)',
                      border: '1px solid var(--border2)', background: 'var(--surface2)',
                      color: 'var(--text)', fontFamily: 'var(--mono)', fontSize: 12,
                      outline: 'none', textAlign: 'right',
                    }}
                  />
                  <span style={{
                    width: 74, textAlign: 'right', fontFamily: 'var(--mono)', fontSize: 11,
                    color: off ? 'var(--text-dim)' : cat.color,
                  }}>
                    {off ? 'not scored' : `${+pts.toFixed(1)} pts`}
                  </span>
                </div>
              )
            })}
            {/* Sits under the rows it resets, so its scope is unambiguous —
                weights only, not the thresholds further down the card. */}
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 8 }}>
              <button
                onClick={resetWeights}
                disabled={weightsAreDefault}
                title={weightsAreDefault ? 'Already at the default 70/10/10/10'
                                         : 'Restore the default 70/10/10/10 split'}
                style={{
                  padding: '5px 10px', borderRadius: 'var(--r)',
                  border: '1px solid var(--border2)',
                  background: 'transparent',
                  color: weightsAreDefault ? 'var(--text-dim)' : 'var(--text)',
                  fontSize: 11, fontFamily: 'var(--sans)',
                  cursor: weightsAreDefault ? 'default' : 'pointer',
                  opacity: weightsAreDefault ? 0.45 : 1,
                  transition: 'opacity 0.12s, color 0.12s',
                }}
              >
                Reset to defaults
              </button>
            </div>
          </div>
        </div>

        {allWeightsZero && (
          <div style={{
            fontSize: 11, color: 'var(--red)', fontFamily: 'var(--mono)',
            background: 'var(--red)14', border: '1px solid var(--red)33',
            borderRadius: 'var(--r)', padding: '7px 10px', marginBottom: 18,
          }}>
            At least one category needs a weight above zero.
          </div>
        )}

        <div style={{
          fontSize: 12, fontWeight: 600, color: 'var(--text)',
          paddingTop: 18, marginTop: 10, marginBottom: 10,
          borderTop: '1px solid var(--border)',
        }}>Thresholds</div>
        <p style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.55, marginBottom: 18 }}>
          Each threshold defines the size limit for that category relative to your total torrent library.
          Points are lost <em>linearly</em> as you approach the threshold — at exactly the threshold value that
          category's points are all gone.
          For example, a <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>1%</span> threshold means
          you start losing points immediately if any problem data exists, and lose them all once it reaches 1% of your library.
          Lower = stricter. Hardlinked Media has no threshold — it scores in direct proportion to how much of your library is hardlinked.
        </p>
        <div style={formGrid}>
          <Field label="Orphaned Torrent Threshold" type="number"
            suffix="%"
            hint={thresholdHint('orphaned torrent', 'WEIGHT_ORPHANED')}
            placeholder="1" value={orPct} onChange={v => { setOrPct(v); setIsDirty(true) }} />
          <Field label="Not Imported Threshold" type="number"
            suffix="%"
            hint={thresholdHint('unlinked seeding', 'WEIGHT_NOT_IMPORTED')}
            placeholder="1" value={niPct} onChange={v => { setNiPct(v); setIsDirty(true) }} />
          <Field label="Duplicate Files Threshold" type="number"
            suffix="%"
            hint={thresholdHint('duplicate file', 'WEIGHT_DUPLICATES')}
            placeholder="1" value={dupPct} onChange={v => { setDupPct(v); setIsDirty(true) }} />
        </div>

        {/* Live score preview — points come from the weighting above, so this
            stays truthful when a category is reweighted or switched off. */}
        <div style={{ marginTop: 14, ...formGrid }}>
          {[
            { label: 'Orphaned',     pct: orPct,  key: 'WEIGHT_ORPHANED' },
            { label: 'Not Imported', pct: niPct,  key: 'WEIGHT_NOT_IMPORTED' },
            { label: 'Duplicates',   pct: dupPct, key: 'WEIGHT_DUPLICATES' },
          ].map(({ label, pct, key }) => {
            const v   = parseFloat(pct)
            const max = weightPoints(weights)[key]
            if (!v || isNaN(v) || !(max > 0.05)) return null
            return (
              <div key={label} style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', background: 'var(--surface2)', borderRadius: 'var(--r)', padding: '6px 10px' }}>
                {label}: all {+max.toFixed(1)} pts lost at <span style={{ color: 'var(--accent)' }}>{v}%</span> of library
              </div>
            )
          })}
        </div>
      </Card>

      <Card title="Excluded Files & Folders">
        <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', display: 'block', marginBottom: 5 }}>Paths or patterns to ignore</label>
        <span style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45, display: 'block', marginBottom: 8 }}>
          One rule per line. Use this for torrent categories you do not manage with Sonarr/Radarr, such as books, music, games, or other non-library downloads. Excluded items are ignored for scoring and Not Imported reporting.
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45, display: 'block', marginBottom: 8 }}>
          Folder paths can be written however you recognize them: <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>torrents/books</span>, <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>/data/torrents/books</span>, or <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>/mnt/user/data/torrents/books</span>. File names, directory names, extensions, and glob patterns work too.
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
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Full disc rip presets</div>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45, marginBottom: 9 }}>
            Ignore full-disc Blu-ray and DVD folder structures such as <span style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>BDMV</span>, <span style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>CERTIFICATE</span>, <span style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>VIDEO_TS</span>, and <span style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>AUDIO_TS</span>. Standalone video files remain visible.
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {DISC_RIP_PRESETS.map(preset => {
              const active = discRipPresets.includes(preset.id)
              return (
                <button
                  key={preset.id}
                  onClick={() => toggleDiscPreset(preset.id)}
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
        <div style={{ marginTop: 14, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Media server sidecar presets</div>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45, marginBottom: 9 }}>
            Ignore metadata and artwork sidecars commonly written or read by media servers, such as <span style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>.plexmatch</span>, <span style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>.nfo</span>, <span style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>poster.jpg</span>, <span style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>fanart.jpg</span>, and <span style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>folder.jpg</span>.
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
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 14, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 3 }}>File explorer visibility</div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>Control whether excluded files appear in the file explorer.</div>
          </div>
          <div style={{ flexShrink: 0, marginLeft: 24 }}>
            <SegToggle
              options={[{ value: false, label: 'Visible' }, { value: true, label: 'Hidden' }]}
              value={exclusionHideFromExplorer}
              onChange={v => { setExclusionHideFromExplorer(v); setIsDirty(true) }}
            />
          </div>
        </div>
      </Card>

      <Card title="Appearance">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Theme</div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>Choose between dark and light mode. Dark is the default.</div>
          </div>
          <div style={{ flexShrink: 0, marginLeft: 24 }}>
            <SegToggle
              options={[{ value: 'dark', label: '🌙 Dark' }, { value: 'light', label: '☀️ Light' }]}
              value={theme}
              onChange={t => onThemeChange && onThemeChange(t)}
            />
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
                <div key={h} style={{ padding: '8px 12px' }} className="ui-table-header">{h}</div>
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
