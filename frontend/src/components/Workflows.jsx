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
const COUNT_OPTIONS = [
  { value: 5,  est: '~5 min' },
  { value: 10, est: '~10 min' },
  { value: 20, est: '~20 min' },
]

function CountPicker({ value, onChange, max }) {
  return (
    <div style={{ display: 'flex', gap: 10 }}>
      {COUNT_OPTIONS.map(opt => {
        const active = value === opt.value
        const disabled = opt.value > max
        return (
          <button
            key={opt.value}
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
            <span style={{ fontSize: 22, fontWeight: 700, fontFamily: 'var(--mono)', lineHeight: 1.1 }}>{opt.value}</span>
            <span style={{ fontSize: 10, marginTop: 3, fontFamily: 'var(--mono)', opacity: 0.7 }}>{opt.est}</span>
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

// ── Result item ───────────────────────────────────────────────────────────────
function ResultItem({ item }) {
  const [grabStatus, setGrabStatus] = useState('idle')
  const [grabError,  setGrabError]  = useState(null)

  const handleGrab = useCallback(async e => {
    e.stopPropagation()
    if (grabStatus !== 'idle' || !item.best_release) return
    setGrabStatus('grabbing')
    try {
      await api.grabRelease({
        service:       item.arr_service,
        connection_id: item.arr_connection_id,
        guid:          item.best_release.guid,
        indexer_id:    item.best_release.indexer_id,
      })
      setGrabStatus('grabbed')
    } catch (err) {
      setGrabError(err.message)
      setGrabStatus('error')
    }
  }, [grabStatus, item])

  const searching  = item.status === 'searching'
  const found      = item.status === 'found'
  const notFound   = item.status === 'not_found'
  const errored    = item.status === 'error'

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

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12,
      padding: '10px 16px', borderBottom: '1px solid var(--border)',
    }}>
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
        <div style={{ fontSize: 11, color: 'var(--text-dim)', fontFamily: 'var(--mono)', marginTop: 2 }}>
          {searching  && 'Querying indexers…'}
          {found && item.best_release && `${item.best_release.indexer} · ${item.best_release.seeders}S · ${formatBytes(item.best_release.size)}`}
          {notFound   && 'No releases found'}
          {errored    && item.error}
        </div>
        {filename && (
          <div style={{ fontSize: 10, color: 'var(--text-dim)', fontFamily: 'var(--mono)', marginTop: 2, opacity: 0.6, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
            title={filename}>
            {filename}
          </div>
        )}
      </div>

      {item.arr_service && (
        <a
          href={item.arr_url || undefined}
          target="_blank" rel="noopener noreferrer"
          onClick={e => e.stopPropagation()}
          title={`Open in ${item.arr_service}`}
          style={{
            fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4, flexShrink: 0,
            textDecoration: 'none', cursor: item.arr_url ? 'pointer' : 'default',
            background: item.arr_service === 'radarr' ? 'var(--yellow)18' : 'var(--blue)18',
            color:      item.arr_service === 'radarr' ? 'var(--yellow)'   : 'var(--blue)',
            border:     `1px solid ${item.arr_service === 'radarr' ? 'var(--yellow)' : 'var(--blue)'}40`,
          }}
        >
          {item.arr_service} ↗
        </a>
      )}

      {found && item.best_release && (
        <div style={{ flexShrink: 0 }}>
          {grabStatus === 'idle' && (
            <button onClick={handleGrab} style={{
              fontSize: 10, fontFamily: 'var(--mono)', padding: '2px 8px', borderRadius: 5, cursor: 'pointer',
              border: '1px solid var(--accent)50', background: 'var(--accent)10', color: 'var(--accent)',
            }}>Grab</button>
          )}
          {grabStatus === 'grabbing' && (
            <span style={{ fontSize: 10, color: 'var(--text-dim)', fontFamily: 'var(--mono)' }}>Grabbing…</span>
          )}
          {grabStatus === 'grabbed' && (
            <span style={{ fontSize: 10, color: 'var(--green)', fontFamily: 'var(--mono)', padding: '2px 8px' }}>✓ Grabbed</span>
          )}
          {grabStatus === 'error' && (
            <button onClick={() => { setGrabStatus('idle'); setGrabError(null) }}
              title={grabError || 'Grab failed — click to retry'}
              style={{
                fontSize: 10, fontFamily: 'var(--mono)', padding: '2px 8px', borderRadius: 5, cursor: 'pointer',
                border: '1px solid var(--red)50', background: 'var(--red)10', color: 'var(--red)',
              }}>Failed ↺</button>
          )}
        </div>
      )}

    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export default function Workflows() {
  const [phase, setPhase] = useState('config')  // config | running | done | stopped

  // Data loaded on mount
  const [indexers,   setIndexers]   = useState([])
  const [folders,    setFolders]    = useState([])   // [{name, count}]
  const [loading,    setLoading]    = useState(true)
  const [loadError,  setLoadError]  = useState(null)

  // Config
  const [downloadFrom,    setDownloadFrom]    = useState([])
  const [seedingOn,       setSeedingOn]       = useState([])
  const [selectedFolders, setSelectedFolders] = useState([])
  const [searchCount,     setSearchCount]     = useState(10)
  const [sort,            setSort]            = useState('largest')
  const [saving,          setSaving]          = useState(false)

  // Job
  const [jobId,    setJobId]    = useState(null)
  const [jobData,  setJobData]  = useState(null)
  const pollRef    = useRef(null)
  const mountedRef = useRef(true)

  useEffect(() => () => {
    mountedRef.current = false
    clearTimeout(pollRef.current)
  }, [])

  useEffect(() => {
    Promise.all([api.acquireCandidates(), api.workflowIndexers(), api.getConfig()])
      .then(([cdata, idata, cfg]) => {
        const grouped  = groupCandidates(cdata.candidates || [])
        const resolved = grouped.filter(c => c.resolved)
        setFolders(groupByFolder(resolved).map(([name, items]) => ({ name, count: items.length })))
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

  const availableCount = useMemo(() => {
    if (selectedFolders.length === 0) return folders.reduce((s, f) => s + f.count, 0)
    return folders.filter(f => selectedFolders.includes(f.name)).reduce((s, f) => s + f.count, 0)
  }, [folders, selectedFolders])

  const willSearch = Math.min(availableCount, searchCount)

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

  const handleGenerate = useCallback(async () => {
    setPhase('running')
    setJobData(null)
    try {
      const resp = await api.startGenerate({
        folders:       selectedFolders,
        count:         searchCount,
        sort,
        download_from: downloadFrom,
        seeding_on:    seedingOn,
      })
      setJobId(resp.job_id)
      startPoll(resp.job_id)
    } catch (e) {
      setPhase('config')
      setLoadError(e.message)
    }
  }, [selectedFolders, searchCount, downloadFrom, seedingOn, startPoll])

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
          <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--text)', lineHeight: 1.2 }}>Acquire Candidates</div>
          <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.6, maxWidth: 520 }}>
            Unseeded files in your library. Configure your strategy, pick folders, choose how deep to search, then generate a grab list one by one.
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
              <SectionLabel>Priority</SectionLabel>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 10, lineHeight: 1.5 }}>
                How to order candidates when there are more than the search limit.
              </div>
              <SortPicker value={sort} onChange={setSort} />
            </div>

            <div>
              <SectionLabel>Search Depth</SectionLabel>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 12, lineHeight: 1.5 }}>
                Each release search takes 30–90 s while Radarr/Sonarr queries your indexers.
                {availableCount > 0 && ` ${availableCount} candidate${availableCount !== 1 ? 's' : ''} available.`}
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
