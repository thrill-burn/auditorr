import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { api } from '../api'
import { formatBytes } from '../utils'

// ── Helpers ───────────────────────────────────────────────────────────────────
function parseSeason(path) {
  const m = (path || '').match(/[Ss](\d{1,2})[Ee]/i)
  return m ? parseInt(m[1], 10) : null
}

function getRootFolder(path) {
  if (!path) return 'Other'
  const normalized = path.replace(/\\/g, '/').replace(/^\//, '')
  const first = normalized.split('/')[0]
  return first || 'Other'
}

function groupCandidates(candidates) {
  const groups = []
  const sonarrMap = {}
  for (const c of candidates) {
    if (c.resolved && c.arr_service === 'sonarr') {
      const season = parseSeason(c.path)
      const key = `${c.arr_id}_S${season ?? 'x'}`
      if (key in sonarrMap) {
        const g = groups[sonarrMap[key]]
        g.file_count++
        g.total_size += c.size || 0
        if (!g.episode_id && c.episode_id) g.episode_id = c.episode_id
        if (!g.rep_path) g.rep_path = c.path
      } else {
        sonarrMap[key] = groups.length
        groups.push({ ...c, season, season_number: season, file_count: 1, total_size: c.size || 0, rep_path: c.path })
      }
    } else {
      groups.push({ ...c, season: null, season_number: null, file_count: 1, total_size: c.size || 0, rep_path: c.path })
    }
  }
  return groups
}

function groupByFolder(candidates) {
  const map = {}
  for (const c of candidates) {
    const folder = getRootFolder(c.rep_path || c.path)
    if (!map[folder]) map[folder] = []
    map[folder].push(c)
  }
  return Object.entries(map).sort(([a], [b]) => a.localeCompare(b))
}

// ── Shared chip ───────────────────────────────────────────────────────────────
function Chip({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '3px 10px', borderRadius: 99, fontSize: 11, cursor: 'pointer',
        border: `1px solid ${active ? 'var(--accent)' : 'var(--border2)'}`,
        background: active ? 'var(--accent)18' : 'transparent',
        color: active ? 'var(--accent)' : 'var(--text-dim)',
        fontWeight: active ? 600 : 400,
      }}
    >
      {children}
    </button>
  )
}

// ── Labeled chips (options with separate display labels) ─────────────────────
const QUALITY_RES_OPTIONS = [
  { value: '2160p', label: '2160p / 4K' },
  { value: '1080p', label: '1080p'      },
  { value: '720p',  label: '720p'       },
]
const QUALITY_SOURCE_OPTIONS = [
  { value: 'remux',  label: 'Remux'  },
  { value: 'bluray', label: 'Bluray' },
  { value: 'webdl',  label: 'WEB-DL' },
  { value: 'webrip', label: 'WEBRip' },
  { value: 'hdtv',   label: 'HDTV'   },
]
const HDR_OPTIONS = [
  { value: 'DV',    label: 'Dolby Vision' },
  { value: 'HDR10+', label: 'HDR10+' },
  { value: 'HDR10', label: 'HDR10'   },
  { value: 'HDR',   label: 'HDR'     },
  { value: 'HLG',   label: 'HLG'     },
  { value: 'SDR',   label: 'SDR'     },
]
const HDR_STYLE = {
  'DV':     { bg: '#7c3aed20', color: '#a78bfa' },
  'HDR10+': { bg: '#1d4ed820', color: '#60a5fa' },
  'HDR10':  { bg: '#0e749820', color: '#38bdf8' },
  'HDR':    { bg: '#05966920', color: '#34d399' },
  'HLG':    { bg: '#0f766e20', color: '#2dd4bf' },
}

function LabeledChips({ options, value, onChange, allLabel = 'Any' }) {
  const noneSelected = value.length === 0
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      <Chip active={noneSelected} onClick={() => onChange([])}>{allLabel}</Chip>
      {options.map(opt => {
        const active = value.includes(opt.value)
        return (
          <Chip key={opt.value} active={active}
            onClick={() => onChange(active ? value.filter(v => v !== opt.value) : [...value, opt.value])}>
            {opt.label}
          </Chip>
        )
      })}
    </div>
  )
}

// ── Indexer chips ─────────────────────────────────────────────────────────────
function IndexerChips({ options, value, onChange, allLabel = 'All' }) {
  const noneSelected = value.length === 0
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      <Chip active={noneSelected} onClick={() => onChange([])}>{allLabel}</Chip>
      {options.map(opt => {
        const active = value.includes(opt)
        return (
          <Chip key={opt} active={active}
            onClick={() => onChange(active ? value.filter(v => v !== opt) : [...value, opt])}>
            {opt}
          </Chip>
        )
      })}
    </div>
  )
}

// ── Folder chips ──────────────────────────────────────────────────────────────
function FolderChips({ folders, selected, onChange }) {
  const noneSelected = selected.length === 0
  const toggle = name => onChange(selected.includes(name) ? selected.filter(f => f !== name) : [...selected, name])
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      <Chip active={noneSelected} onClick={() => onChange([])}>All</Chip>
      {folders.map(({ name, count }) => (
        <Chip key={name} active={selected.includes(name)} onClick={() => toggle(name)}>
          {name} <span style={{ opacity: 0.55 }}>({count})</span>
        </Chip>
      ))}
    </div>
  )
}

// ── Sort picker ───────────────────────────────────────────────────────────────
const SORT_OPTIONS = [
  { value: 'largest',  label: 'Largest',    sub: 'biggest files first' },
  { value: 'smallest', label: 'Smallest',   sub: 'quickest wins first' },
  { value: 'random',   label: 'Random',     sub: 'shuffle the queue'   },
  { value: 'alpha',    label: 'A → Z',      sub: 'alphabetical'        },
]

function SortPicker({ value, onChange }) {
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      {SORT_OPTIONS.map(opt => {
        const active = value === opt.value
        return (
          <button
            key={opt.value}
            onClick={() => onChange(opt.value)}
            style={{
              display: 'flex', flexDirection: 'column', alignItems: 'flex-start',
              padding: '8px 14px', borderRadius: 8, cursor: 'pointer', minWidth: 110,
              border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
              background: active ? 'var(--accent)12' : 'var(--surface)',
              color: active ? 'var(--accent)' : 'var(--text)',
            }}
          >
            <span style={{ fontSize: 12, fontWeight: 600 }}>{opt.label}</span>
            <span style={{ fontSize: 10, marginTop: 2, opacity: 0.6, fontFamily: 'var(--mono)' }}>{opt.sub}</span>
          </button>
        )
      })}
    </div>
  )
}

// ── Count picker ──────────────────────────────────────────────────────────────
// null means "all available"
const FIXED_COUNT_OPTIONS = [
  { value: 5,  est: '~5 min'  },
  { value: 15, est: '~15 min' },
]

function CountPicker({ value, onChange, max }) {
  const options = [
    ...FIXED_COUNT_OPTIONS,
    { value: null, est: max > 0 ? `~${max} min` : '—' },
  ]
  return (
    <div style={{ display: 'flex', gap: 10 }}>
      {options.map(opt => {
        const active   = value === opt.value
        const disabled = opt.value !== null && opt.value > max
        const display  = opt.value ?? max
        return (
          <button
            key={String(opt.value)}
            onClick={() => !disabled && onChange(opt.value)}
            disabled={disabled}
            style={{
              display: 'flex', flexDirection: 'column', alignItems: 'center',
              padding: '10px 22px', borderRadius: 8, cursor: disabled ? 'not-allowed' : 'pointer',
              border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
              background: active ? 'var(--accent)12' : 'var(--surface)',
              color: disabled ? 'var(--text-dim)' : active ? 'var(--accent)' : 'var(--text)',
              opacity: disabled ? 0.4 : 1, minWidth: 80,
            }}
          >
            <span style={{ fontSize: 22, fontWeight: 700, fontFamily: 'var(--mono)', lineHeight: 1.1 }}>{display}</span>
            <span style={{ fontSize: 10, marginTop: 3, fontFamily: 'var(--mono)', opacity: 0.7 }}>
              {opt.value === null ? 'all' : opt.est}
            </span>
          </button>
        )
      })}
    </div>
  )
}

// ── Section label ─────────────────────────────────────────────────────────────
function SectionLabel({ children }) {
  return (
    <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: 2, textTransform: 'uppercase', color: 'var(--text-dim)', marginBottom: 8 }}>
      {children}
    </div>
  )
}

// ── Grab button (shared) ──────────────────────────────────────────────────────
function GrabButton({ state, onGrab, onReset, errorMsg }) {
  if (state === 'idle')     return <button onClick={onGrab} style={{ fontSize: 10, fontFamily: 'var(--mono)', padding: '2px 8px', borderRadius: 5, cursor: 'pointer', border: '1px solid var(--accent)50', background: 'var(--accent)10', color: 'var(--accent)' }}>Grab</button>
  if (state === 'grabbing') return <span style={{ fontSize: 10, color: 'var(--text-dim)', fontFamily: 'var(--mono)' }}>Grabbing…</span>
  if (state === 'grabbed')  return <span style={{ fontSize: 10, color: 'var(--green)', fontFamily: 'var(--mono)', padding: '2px 8px' }}>✓ Grabbed</span>
  return <button onClick={onReset} title={errorMsg || 'Grab failed — click to retry'} style={{ fontSize: 10, fontFamily: 'var(--mono)', padding: '2px 8px', borderRadius: 5, cursor: 'pointer', border: '1px solid var(--red)50', background: 'var(--red)10', color: 'var(--red)' }}>Failed ↺</button>
}

// ── Result item ───────────────────────────────────────────────────────────────
function ResultItem({ item }) {
  // grabStates: { [guid]: 'idle' | 'grabbing' | 'grabbed' | 'error' }
  const [grabStates,    setGrabStates]    = useState({})
  const [grabErrors,    setGrabErrors]    = useState({})
  const [importStatus,  setImportStatus]  = useState(null)   // null | watching | importing | done | error
  const [importMessage, setImportMessage] = useState(null)
  const importPollRef    = useRef(null)
  const importStartedRef = useRef(false)
  const mountedRef       = useRef(true)

  useEffect(() => () => {
    mountedRef.current = false
    clearTimeout(importPollRef.current)
  }, [])

  const startImportWatch = useCallback(async () => {
    if (importStartedRef.current || !item.arr_id) return
    importStartedRef.current = true
    try {
      const resp = await api.watchImport({
        service:       item.arr_service,
        connection_id: item.arr_connection_id,
        arr_id:        item.arr_id,
        title:         item.arr_title || '',
      })
      window.dispatchEvent(new CustomEvent('auditorr:import_started'))
      if (!mountedRef.current) return
      setImportStatus('queued')
      const poll = async () => {
        try {
          const data = await api.watchImportStatus(resp.job_id)
          if (!mountedRef.current) return
          setImportStatus(data.status)
          setImportMessage(data.message)
          if (['queued', 'downloading', 'importing'].includes(data.status)) {
            importPollRef.current = setTimeout(poll, 3000)
          }
        } catch (_) {}
      }
      poll()
    } catch (err) {
      if (mountedRef.current) {
        setImportStatus('error')
        setImportMessage(err.message)
      }
    }
  }, [item])

  const doGrab = useCallback(async (release, e) => {
    e.stopPropagation()
    const key = release.guid || release.title
    if (grabStates[key] && grabStates[key] !== 'error') return
    setGrabStates(s => ({ ...s, [key]: 'grabbing' }))
    try {
      await api.grabRelease({
        service:       item.arr_service,
        connection_id: item.arr_connection_id,
        guid:          release.guid,
        indexer_id:    release.indexer_id,
      })
      setGrabStates(s => ({ ...s, [key]: 'grabbed' }))
      startImportWatch()
    } catch (err) {
      setGrabErrors(s => ({ ...s, [key]: err.message }))
      setGrabStates(s => ({ ...s, [key]: 'error' }))
    }
  }, [grabStates, item, startImportWatch])

  const resetGrab = useCallback((key, e) => {
    e.stopPropagation()
    setGrabStates(s => ({ ...s, [key]: 'idle' }))
  }, [])

  const searching = item.status === 'searching'
  const found     = item.status === 'found'
  const notFound  = item.status === 'not_found'
  const errored   = item.status === 'error'

  const releases  = (found && item.releases) || (item.best_release ? [item.best_release] : [])
  const multi     = releases.length > 1

  const icon = searching ? (
    <span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: '50%', border: '2px solid var(--accent)', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />
  ) : found ? (
    <span style={{ color: 'var(--green)', fontSize: 11, fontWeight: 700 }}>✓</span>
  ) : notFound ? (
    <span style={{ color: 'var(--text-dim)', fontSize: 12 }}>—</span>
  ) : (
    <span style={{ color: 'var(--red)', fontSize: 11, fontWeight: 700 }}>✗</span>
  )

  const seasonSuffix = item.arr_service === 'sonarr' && item.file_count > 1
    ? ` · S${String(item.season_number ?? '?').padStart(2, '0')} · ${item.file_count} ep`
    : ''

  const filename = (item.path || '').replace(/\\/g, '/').split('/').pop()

  // Single-release grab key
  const singleKey = item.best_release?.guid || item.best_release?.title

  return (
    <div style={{ borderBottom: '1px solid var(--border)' }}>
      {/* Header row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: multi ? '10px 16px 6px' : '10px 16px' }}>
        <span style={{ width: 14, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          {icon}
        </span>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{
            fontSize: 13, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            color: notFound || errored ? 'var(--text-dim)' : 'var(--text)',
          }}>
            {item.arr_title}{seasonSuffix}
          </div>
          {/* Single-release info line */}
          {!multi && (
            <div style={{ fontSize: 11, color: 'var(--text-dim)', fontFamily: 'var(--mono)', marginTop: 2 }}>
              {searching && 'Querying indexers…'}
              {found && item.best_release && (() => {
                const r = item.best_release
                const parts = [r.indexer, formatBytes(r.size), `${r.seeders}S`, r.quality_name, r.hdr || null].filter(Boolean)
                if (r.custom_format_score) parts.push(`CF:${r.custom_format_score}`)
                return parts.join(' · ')
              })()}
              {notFound  && 'No releases found'}
              {errored   && item.error}
            </div>
          )}
          {multi && (
            <div style={{ fontSize: 11, color: 'var(--text-dim)', fontFamily: 'var(--mono)', marginTop: 2 }}>
              {releases.length} releases found
            </div>
          )}
          {filename && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2, minWidth: 0 }}>
              <span style={{ fontSize: 10, color: 'var(--text-dim)', fontFamily: 'var(--mono)', opacity: 0.55, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: '1 1 0', minWidth: 0 }}
                title={filename}>
                {filename}
              </span>
              {item.file_quality && (
                <span style={{ fontSize: 10, color: 'var(--text-dim)', fontFamily: 'var(--mono)', opacity: 0.65, whiteSpace: 'nowrap', flexShrink: 0 }}>
                  {item.file_quality}
                </span>
              )}
              {item.file_hdr && HDR_STYLE[item.file_hdr] && (
                <span style={{ fontSize: 9, fontFamily: 'var(--mono)', fontWeight: 700, padding: '1px 4px', borderRadius: 3, whiteSpace: 'nowrap', flexShrink: 0, background: HDR_STYLE[item.file_hdr].bg, color: HDR_STYLE[item.file_hdr].color }}>
                  {item.file_hdr}
                </span>
              )}
              {item.total_size > 0 && (
                <span style={{ fontSize: 10, color: 'var(--text-dim)', fontFamily: 'var(--mono)', opacity: 0.65, whiteSpace: 'nowrap', flexShrink: 0 }}>
                  {formatBytes(item.total_size)}
                </span>
              )}
            </div>
          )}
        </div>

        {/* Service badge link */}
        {item.arr_service && (
          <a href={item.arr_url || undefined} target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()} title={`Open in ${item.arr_service}`}
            style={{
              fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4, flexShrink: 0,
              textDecoration: 'none', cursor: item.arr_url ? 'pointer' : 'default',
              background: item.arr_service === 'radarr' ? 'var(--yellow)18' : 'var(--blue)18',
              color:      item.arr_service === 'radarr' ? 'var(--yellow)'   : 'var(--blue)',
              border:     `1px solid ${item.arr_service === 'radarr' ? 'var(--yellow)' : 'var(--blue)'}40`,
            }}>
            {item.arr_service} ↗
          </a>
        )}

        {/* Import status (auto-starts after any grab) */}
        {importStatus && (
          <span style={{ fontSize: 10, fontFamily: 'var(--mono)', flexShrink: 0, display: 'flex', alignItems: 'center', gap: 4 }}>
            {['queued', 'downloading', 'importing'].includes(importStatus) && (
              <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', border: '1.5px solid var(--accent)', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />
            )}
            <span style={{
              color: importStatus === 'done' ? 'var(--green)' : importStatus === 'error' ? 'var(--red)' : 'var(--text-dim)',
            }} title={importMessage}>
              {importStatus === 'queued'      && 'Queued…'}
              {importStatus === 'downloading' && 'Downloading…'}
              {importStatus === 'importing'   && 'Importing…'}
              {importStatus === 'done'        && '✓ Imported'}
              {importStatus === 'error'       && 'Import failed'}
            </span>
          </span>
        )}

        {/* Single-release grab button */}
        {found && !multi && item.best_release && (
          <div style={{ flexShrink: 0 }}>
            <GrabButton
              state={grabStates[singleKey] || 'idle'}
              errorMsg={grabErrors[singleKey]}
              onGrab={e => doGrab(item.best_release, e)}
              onReset={e => resetGrab(singleKey, e)}
            />
          </div>
        )}
      </div>

      {/* Multi-release picker */}
      {found && multi && (
        <div style={{ paddingLeft: 42, paddingRight: 16, paddingBottom: 10, display: 'flex', flexDirection: 'column', gap: 3 }}>
          {/* Column headers */}
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 110px 65px 44px 90px 46px 46px 72px', gap: 8, padding: '2px 10px' }}>
            {['Title', 'Tracker', 'Size', 'Peers', 'Quality', 'Score', 'HDR', ''].map((h, i) => (
              <span key={i} style={{
                fontSize: 9, fontFamily: 'var(--mono)', letterSpacing: 1.5, textTransform: 'uppercase',
                color: 'var(--text-dim)', opacity: 0.6,
                textAlign: i >= 2 && i <= 5 ? 'right' : 'left',
              }}>{h}</span>
            ))}
          </div>
          {releases.map((r, i) => {
            const key     = r.guid || r.title
            const gs      = grabStates[key] || 'idle'
            const isBest  = i === 0
            const hdrInfo = HDR_STYLE[r.hdr]
            return (
              <div key={key} style={{
                display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 110px 65px 44px 90px 46px 46px 72px',
                alignItems: 'center', gap: 8, padding: '5px 10px', borderRadius: 6,
                background: isBest ? 'var(--accent)08' : 'transparent',
                border: `1px solid ${isBest ? 'var(--accent)25' : 'transparent'}`,
              }}>
                <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                  title={r.title}>
                  {r.title}
                </span>
                <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'right' }}
                  title={r.indexer}>
                  {r.indexer}
                </span>
                <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', textAlign: 'right' }}>
                  {formatBytes(r.size)}
                </span>
                <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: r.seeders > 0 ? 'var(--green)' : 'var(--text-dim)', textAlign: 'right' }}>
                  {r.seeders}S
                </span>
                <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                  title={r.quality_name}>
                  {r.quality_name || '—'}
                </span>
                <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', textAlign: 'right' }}>
                  {r.custom_format_score ?? 0}
                </span>
                <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
                  {hdrInfo ? (
                    <span style={{
                      fontSize: 9, fontFamily: 'var(--mono)', fontWeight: 700,
                      padding: '2px 5px', borderRadius: 4,
                      background: hdrInfo.bg, color: hdrInfo.color,
                      whiteSpace: 'nowrap',
                    }}>
                      {r.hdr}
                    </span>
                  ) : null}
                </div>
                <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <GrabButton
                    state={gs}
                    errorMsg={grabErrors[key]}
                    onGrab={e => doGrab(r, e)}
                    onReset={e => resetGrab(key, e)}
                  />
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── Prefs persistence ─────────────────────────────────────────────────────────
const PREFS_KEY = 'auditorr_backfill_prefs'
function loadPref(key, fallback) {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY) || '{}')[key] ?? fallback }
  catch { return fallback }
}

// ── Main component ────────────────────────────────────────────────────────────
export default function Workflows() {
  const [phase, setPhase] = useState('config')  // config | running | done | stopped

  // Data loaded on mount
  const [indexers,      setIndexers]      = useState([])
  const [allGroups,     setAllGroups]     = useState([])   // resolved+grouped candidates, raw
  const [totalUnseeded, setTotalUnseeded] = useState(0)
  const [loading,       setLoading]       = useState(true)
  const [loadError,     setLoadError]     = useState(null)

  // Config — indexer strategy (persisted server-side via saveAcquirePrefs)
  const [downloadFrom,    setDownloadFrom]    = useState([])
  const [seedingOn,       setSeedingOn]       = useState([])
  const [saving,          setSaving]          = useState(false)
  // Config — quality / sort / depth (persisted in localStorage)
  const [selectedFolders, setSelectedFolders] = useState(() => loadPref('selectedFolders', []))
  const [titleSearch,     setTitleSearch]     = useState('')  // not persisted — ephemeral per-session
  const [searchCount,     setSearchCount]     = useState(() => loadPref('searchCount', 5))
  const [sort,            setSort]            = useState(() => loadPref('sort', 'largest'))
  const [resFilter,       setResFilter]       = useState(() => loadPref('resFilter', []))
  const [sourceFilter,    setSourceFilter]    = useState(() => loadPref('sourceFilter', []))
  const [hdrFilter,       setHdrFilter]       = useState(() => loadPref('hdrFilter', []))

  // Job
  const [jobId,    setJobId]    = useState(null)
  const [jobData,  setJobData]  = useState(null)
  const pollRef    = useRef(null)
  const mountedRef = useRef(true)

  useEffect(() => () => {
    mountedRef.current = false
    clearTimeout(pollRef.current)
  }, [])

  // Persist filter prefs to localStorage whenever they change
  useEffect(() => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        sort, resFilter, sourceFilter, hdrFilter, searchCount, selectedFolders,
      }))
    } catch {}
  }, [sort, resFilter, sourceFilter, hdrFilter, searchCount, selectedFolders])

  useEffect(() => {
    Promise.all([api.acquireCandidates(), api.workflowIndexers(), api.getConfig()])
      .then(([cdata, idata, cfg]) => {
        const grouped  = groupCandidates(cdata.candidates || [])
        const resolved = grouped.filter(c => c.resolved)
        setAllGroups(resolved)
        setTotalUnseeded((cdata.resolved_count || 0) + (cdata.unresolved_count || 0))
        setIndexers(idata.indexers || [])
        setDownloadFrom(cfg.ACQUIRE_DOWNLOAD_FROM || [])
        setSeedingOn(cfg.ACQUIRE_SEEDING_ON || [])
      })
      .catch(e => setLoadError(e.message))
      .finally(() => setLoading(false))
  }, [])

  const saveFilters = useCallback(async (df, so) => {
    setSaving(true)
    try { await api.saveAcquirePrefs({ ACQUIRE_DOWNLOAD_FROM: df, ACQUIRE_SEEDING_ON: so }) } catch (_) {}
    setSaving(false)
  }, [])

  const handleDownloadFromChange = v => { setDownloadFrom(v); saveFilters(v, seedingOn) }
  const handleSeedingOnChange    = v => { setSeedingOn(v);    saveFilters(downloadFrom, v) }

  // Filter raw groups by title search, then derive folder chips + count reactively
  const filteredGroups = useMemo(() => {
    if (!titleSearch.trim()) return allGroups
    const term = titleSearch.trim().toLowerCase()
    return allGroups.filter(g => (g.arr_title || '').toLowerCase().includes(term))
  }, [allGroups, titleSearch])

  const folders = useMemo(() =>
    groupByFolder(filteredGroups).map(([name, items]) => ({ name, count: items.length }))
  , [filteredGroups])

  const availableCount = useMemo(() => {
    if (selectedFolders.length === 0) return folders.reduce((s, f) => s + f.count, 0)
    return folders.filter(f => selectedFolders.includes(f.name)).reduce((s, f) => s + f.count, 0)
  }, [folders, selectedFolders])

  const willSearch = searchCount === null ? availableCount : Math.min(availableCount, searchCount)

  const startPoll = useCallback((id) => {
    const poll = async () => {
      try {
        const data = await api.generateStatus(id)
        if (!mountedRef.current) return
        setJobData(data)
        if (data.status === 'running') {
          pollRef.current = setTimeout(poll, 2000)
        } else {
          setPhase(data.status)
        }
      } catch (_) {
        if (mountedRef.current) setPhase('config')
      }
    }
    poll()
  }, [])

  async function handleGenerate() {
    setPhase('running')
    setJobData(null)
    try {
      const resp = await api.startGenerate({
        folders:       selectedFolders,
        count:         searchCount ?? availableCount,
        sort,
        res_filter:    resFilter,
        source_filter: sourceFilter,
        hdr_filter:    hdrFilter,
        title_search:  titleSearch.trim() || undefined,
        download_from: downloadFrom,
        seeding_on:    seedingOn,
      })
      setJobId(resp.job_id)
      startPoll(resp.job_id)
    } catch (e) {
      setPhase('config')
      setLoadError(e.message)
    }
  }

  const handleStop = useCallback(async () => {
    if (jobId) { try { await api.stopGenerate(jobId) } catch (_) {} }
  }, [jobId])

  const handleReset = useCallback(() => {
    clearTimeout(pollRef.current)
    setPhase('config')
    setJobId(null)
    setJobData(null)
    setLoadError(null)
  }, [])

  // ── Config phase ──────────────────────────────────────────────────────────────
  if (phase === 'config') {
    return (
      <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 28, maxWidth: 740 }}>
        <div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: 2.5, textTransform: 'uppercase', marginBottom: 4 }}>Workflows</div>
          <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--text)', lineHeight: 1.2 }}>Backfill</div>
          <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.6, maxWidth: 520 }}>
            Convert media files with no seeding torrent file partner into a hardlinked seeding pair.
          </p>
        </div>

        {loadError && (
          <div style={{ padding: '10px 14px', background: 'var(--red)10', border: '1px solid var(--red)30', borderRadius: 8, color: 'var(--red)', fontSize: 12 }}>
            {loadError}
          </div>
        )}

        {loading ? (
          <div style={{ color: 'var(--text-dim)', fontSize: 13 }}>Loading…</div>
        ) : (
          <>
            {indexers.length > 0 && (
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 14 }}>
                  <SectionLabel>Indexer Strategy</SectionLabel>
                  {saving && <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>Saving…</span>}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
                  <div>
                    <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Download from</div>
                    <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8, lineHeight: 1.5 }}>Restrict to these indexers. <em>All</em> = no restriction.</div>
                    <IndexerChips options={indexers} value={downloadFrom} onChange={handleDownloadFromChange} />
                  </div>
                  <div>
                    <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Must also be seeding on</div>
                    <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8, lineHeight: 1.5 }}>Release must also appear on these. <em>Any</em> = no restriction.</div>
                    <IndexerChips options={indexers} value={seedingOn} onChange={handleSeedingOnChange} allLabel="Any" />
                  </div>
                </div>
              </div>
            )}

            {folders.length > 0 && (
              <div>
                <SectionLabel>Root Folders</SectionLabel>
                <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 10, lineHeight: 1.5 }}>
                  Limit search to specific folders. <em>All</em> = search everything.
                </div>
                <FolderChips folders={folders} selected={selectedFolders} onChange={setSelectedFolders} />
              </div>
            )}

            <div>
              <SectionLabel>Quality Filter</SectionLabel>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 12, lineHeight: 1.5 }}>
                Restrict results by resolution and/or source, exactly like a Sonarr/Radarr quality profile. <em>Any</em> = no restriction.
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div>
                  <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text)', marginBottom: 6 }}>Resolution</div>
                  <LabeledChips options={QUALITY_RES_OPTIONS} value={resFilter} onChange={setResFilter} />
                </div>
                <div>
                  <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text)', marginBottom: 6 }}>Source</div>
                  <LabeledChips options={QUALITY_SOURCE_OPTIONS} value={sourceFilter} onChange={setSourceFilter} />
                </div>
                <div>
                  <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text)', marginBottom: 6 }}>HDR</div>
                  <LabeledChips options={HDR_OPTIONS} value={hdrFilter} onChange={setHdrFilter} />
                </div>
              </div>
            </div>

            <div>
              <SectionLabel>Priority</SectionLabel>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 10, lineHeight: 1.5 }}>
                How to order candidates when there are more than the search limit.
              </div>
              <SortPicker value={sort} onChange={setSort} />
              <div style={{ marginTop: 12 }}>
                <input
                  type="text"
                  value={titleSearch}
                  onChange={e => setTitleSearch(e.target.value)}
                  placeholder="Filter by title…"
                  style={{
                    padding: '6px 10px', borderRadius: 6, fontSize: 12,
                    border: '1px solid var(--border)',
                    background: 'var(--surface2)',
                    color: 'var(--text)',
                    fontFamily: 'inherit',
                    width: 220, outline: 'none',
                    opacity: titleSearch ? 1 : 0.7,
                  }}
                />
              </div>
            </div>

            <div>
              <SectionLabel>Search Depth</SectionLabel>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 12, lineHeight: 1.5 }}>
                Each release search takes 30–90 s while Radarr/Sonarr queries your indexers.
                {availableCount > 0 && ` ${availableCount} searchable candidate${availableCount !== 1 ? 's' : ''}${totalUnseeded > availableCount ? ` (${totalUnseeded} total unseeded)` : ''}.`}
              </div>
              <CountPicker value={searchCount} onChange={setSearchCount} max={availableCount || 999} />
            </div>

            <div>
              <button
                onClick={handleGenerate}
                disabled={availableCount === 0}
                style={{
                  fontSize: 13, fontWeight: 600, padding: '10px 24px', borderRadius: 8,
                  cursor: availableCount > 0 ? 'pointer' : 'not-allowed',
                  background: availableCount > 0 ? 'var(--accent)' : 'var(--surface2)',
                  color: availableCount > 0 ? '#fff' : 'var(--text-dim)',
                  border: 'none', opacity: availableCount > 0 ? 1 : 0.5,
                }}
              >
                Generate {willSearch > 0 ? `${willSearch} ` : ''}Releases →
              </button>
              {availableCount === 0 && (
                <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-dim)' }}>
                  No resolved candidates — check that Radarr/Sonarr is configured.
                </div>
              )}
            </div>
          </>
        )}

        <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      </div>
    )
  }

  // ── Results phase (running | done | stopped) ──────────────────────────────────
  const results    = jobData?.results || []
  const total      = jobData?.total ?? willSearch
  const completed  = jobData?.completed ?? 0
  const foundCount = results.filter(r => r.status === 'found').length
  const progress   = total > 0 ? (completed / total) * 100 : 0

  const phaseLabel =
    phase === 'running' ? `Searching ${completed} of ${total}…` :
    phase === 'done'    ? `Done — ${foundCount} release${foundCount !== 1 ? 's' : ''} found` :
                          `Stopped — ${foundCount} release${foundCount !== 1 ? 's' : ''} found`

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 14 }}>
      {/* Header row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: 2.5, textTransform: 'uppercase', marginBottom: 2 }}>Workflows</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--text)' }}>{phaseLabel}</div>
        </div>
        <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
          {phase === 'running' && (
            <button onClick={handleStop} style={{
              fontSize: 12, padding: '6px 16px', borderRadius: 7, cursor: 'pointer',
              border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)',
            }}>Stop</button>
          )}
          {phase !== 'running' && (
            <button onClick={handleReset} style={{
              fontSize: 12, padding: '6px 16px', borderRadius: 7, cursor: 'pointer',
              border: '1px solid var(--accent)40', background: 'var(--accent)10', color: 'var(--accent)',
            }}>← New Search</button>
          )}
        </div>
      </div>

      {/* Progress bar */}
      <div style={{ height: 3, background: 'var(--border)', borderRadius: 2, overflow: 'hidden' }}>
        <div style={{
          height: '100%', borderRadius: 2, transition: 'width 0.5s ease',
          background: phase === 'done' ? 'var(--green)' : 'var(--accent)',
          width: `${progress}%`,
        }} />
      </div>

      {/* Results list */}
      {results.length > 0 && (
        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 9, overflow: 'hidden' }}>
          {results.map((item, i) => <ResultItem key={i} item={item} />)}
        </div>
      )}

      {results.length === 0 && phase === 'running' && (
        <div style={{ padding: '48px 0', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, color: 'var(--text-dim)', fontSize: 13 }}>
          <span style={{ display: 'inline-block', width: 12, height: 12, borderRadius: '50%', border: '2px solid var(--accent)', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />
          Starting search…
        </div>
      )}

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}
