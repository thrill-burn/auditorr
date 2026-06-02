import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { api } from '../api'
import { formatBytes } from '../utils'

// ── Helpers ───────────────────────────────────────────────────────────────────
function parseSeason(path) {
  const m = (path || '').match(/[Ss](\d{1,2})[Ee]/i)
  return m ? parseInt(m[1], 10) : null
}

// Group flat candidate list: resolved Sonarr rows → one row per (arr_id, season)
// with season_number so we can use Sonarr's season pack search endpoint.
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
        groups.push({
          ...c,
          season,
          season_number: season,
          file_count: 1,
          total_size: c.size || 0,
          rep_path: c.path,
        })
      }
    } else {
      groups.push({ ...c, season: null, season_number: null, file_count: 1, total_size: c.size || 0, rep_path: c.path })
    }
  }
  return groups
}

function getRootFolder(path) {
  if (!path) return 'Other'
  const normalized = path.replace(/\\/g, '/').replace(/^\//, '')
  const first = normalized.split('/')[0]
  return first || 'Other'
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

// ── Folder group ──────────────────────────────────────────────────────────────
function FolderGroup({ name, candidates }) {
  const [expanded, setExpanded] = useState(true)
  const resolvedCount = candidates.filter(c => c.resolved).length
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div
        onClick={() => setExpanded(v => !v)}
        style={{
          display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer',
          padding: '6px 2px', userSelect: 'none',
        }}
      >
        <span style={{ fontSize: 10, color: 'var(--text-dim)', width: 10, textAlign: 'center', flexShrink: 0 }}>
          {expanded ? '▾' : '▸'}
        </span>
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', fontFamily: 'var(--mono)' }}>
          {name}
        </span>
        <span style={{
          fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 7px', borderRadius: 99,
          background: 'var(--surface2)', border: '1px solid var(--border)',
          color: 'var(--text-dim)',
        }}>
          {resolvedCount}/{candidates.length}
        </span>
        <div style={{ flex: 1, height: 1, background: 'var(--border)', marginLeft: 4 }} />
      </div>
      {expanded && candidates.map(c => <CandidateRow key={c.rep_path || c.path} c={c} />)}
    </div>
  )
}

// ── Indexer chip filter ───────────────────────────────────────────────────────
function IndexerChips({ options, value, onChange }) {
  const allSelected = value.length === 0
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      <button
        onClick={() => onChange([])}
        style={{
          padding: '3px 10px', borderRadius: 99, fontSize: 11, cursor: 'pointer',
          border: `1px solid ${allSelected ? 'var(--accent)' : 'var(--border2)'}`,
          background: allSelected ? 'var(--accent)18' : 'transparent',
          color: allSelected ? 'var(--accent)' : 'var(--text-dim)',
          fontWeight: allSelected ? 600 : 400,
        }}
      >
        All
      </button>
      {options.map(opt => {
        const active = value.includes(opt)
        return (
          <button
            key={opt}
            onClick={() => onChange(active ? value.filter(v => v !== opt) : [...value, opt])}
            style={{
              padding: '3px 10px', borderRadius: 99, fontSize: 11, cursor: 'pointer',
              border: `1px solid ${active ? 'var(--accent)' : 'var(--border2)'}`,
              background: active ? 'var(--accent)18' : 'transparent',
              color: active ? 'var(--accent)' : 'var(--text-dim)',
              fontWeight: active ? 600 : 400,
            }}
          >
            {opt}
          </button>
        )
      })}
    </div>
  )
}

// ── Release matrix row ────────────────────────────────────────────────────────
function ReleaseRow({ r, service, connectionId }) {
  const [grabStatus, setGrabStatus] = useState('idle')  // idle | grabbing | grabbed | error
  const [grabError, setGrabError]   = useState(null)

  const handleGrab = useCallback(async (e) => {
    e.stopPropagation()
    if (grabStatus !== 'idle') return
    setGrabStatus('grabbing')
    try {
      await api.grabRelease({ service, connection_id: connectionId, guid: r.guid, indexer_id: r.indexer_id })
      setGrabStatus('grabbed')
    } catch (err) {
      setGrabError(err.message)
      setGrabStatus('error')
    }
  }, [grabStatus, service, connectionId, r.guid, r.indexer_id])

  return (
    <div style={{
      display: 'grid', gridTemplateColumns: '1fr 140px 60px 90px 72px',
      gap: 8, padding: '7px 12px', fontSize: 12, alignItems: 'center',
      borderBottom: '1px solid var(--border)',
    }}>
      <span style={{ color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.title}>{r.title}</span>
      <span style={{ color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.indexer}>{r.indexer}</span>
      <span style={{ color: r.seeders > 0 ? 'var(--green)' : 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 11, textAlign: 'right' }}>{r.seeders}S</span>
      <span style={{ color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 11, textAlign: 'right' }}>{formatBytes(r.size)}</span>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        {grabStatus === 'idle' && (
          <button
            onClick={handleGrab}
            title={r.guid ? undefined : 'No GUID — cannot grab'}
            disabled={!r.guid}
            style={{
              fontSize: 10, fontFamily: 'var(--mono)', padding: '2px 8px', borderRadius: 5, cursor: r.guid ? 'pointer' : 'not-allowed',
              border: '1px solid var(--accent)50', background: 'var(--accent)10', color: 'var(--accent)',
              opacity: r.guid ? 1 : 0.4,
            }}
          >
            Grab
          </button>
        )}
        {grabStatus === 'grabbing' && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10, color: 'var(--text-dim)', fontFamily: 'var(--mono)' }}>
            <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', border: '1.5px solid var(--accent)', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />
            Grabbing
          </span>
        )}
        {grabStatus === 'grabbed' && (
          <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--green)', padding: '2px 8px' }}>✓ Grabbed</span>
        )}
        {grabStatus === 'error' && (
          <button
            onClick={() => { setGrabStatus('idle'); setGrabError(null) }}
            title={grabError || 'Grab failed — click to retry'}
            style={{
              fontSize: 10, fontFamily: 'var(--mono)', padding: '2px 8px', borderRadius: 5, cursor: 'pointer',
              border: '1px solid var(--red)50', background: 'var(--red)10', color: 'var(--red)',
            }}
          >
            Failed ↺
          </button>
        )}
      </div>
    </div>
  )
}

// ── Candidate row (supports polling) ─────────────────────────────────────────
function CandidateRow({ c }) {
  const [expanded, setExpanded]   = useState(false)
  const [searchStatus, setStatus] = useState('idle')   // idle | searching | done | error
  const [releases, setReleases]   = useState(null)
  const [errorMsg, setErrorMsg]   = useState(null)
  const [elapsed, setElapsed]     = useState(0)
  const pollRef    = useRef(null)
  const startRef   = useRef(null)
  const mountedRef = useRef(true)

  useEffect(() => () => { mountedRef.current = false; clearTimeout(pollRef.current) }, [])

  const startSearch = useCallback(() => {
    if (searchStatus !== 'idle') return
    setStatus('searching')
    startRef.current = Date.now()

    const params = {
      service:       c.arr_service,
      connection_id: c.arr_connection_id,
      arr_id:        c.arr_id,
      path:          c.rep_path || c.path,
    }
    // Use season pack search for grouped Sonarr rows; fall back to episodeId for single files
    if (c.arr_service === 'sonarr' && c.season_number != null) {
      params.season_number = c.season_number
    } else if (c.episode_id) {
      params.episode_id = c.episode_id
    }

    const poll = async () => {
      try {
        const data = await api.acquireReleases(params)
        if (!mountedRef.current) return
        if (data.status === 'searching') {
          setElapsed(Math.round((Date.now() - startRef.current) / 1000))
          pollRef.current = setTimeout(poll, 3000)
          return
        }
        if (data.status === 'error') {
          setErrorMsg(data.message || 'Search failed')
          setStatus('error')
        } else {
          setReleases(data.releases || [])
          setStatus('done')
        }
      } catch (e) {
        if (mountedRef.current) { setErrorMsg(e.message); setStatus('error') }
      }
    }
    poll()
  }, [c, searchStatus])

  const handleExpand = useCallback(() => {
    if (!c.resolved) return
    const next = !expanded
    setExpanded(next)
    if (next && searchStatus === 'idle') startSearch()
  }, [c.resolved, expanded, searchStatus, startSearch])

  const filename = (c.path || '').split('/').pop()
  const title    = c.arr_title || filename
  const subtitle = c.arr_service === 'sonarr' && c.file_count > 1
    ? `Season ${c.season_number ?? '?'} · ${c.file_count} episodes · ${formatBytes(c.total_size)}`
    : formatBytes(c.total_size || c.size || 0)

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 9,
      overflow: 'hidden', opacity: c.resolved ? 1 : 0.65,
    }}>
      <div
        onClick={handleExpand}
        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', cursor: c.resolved ? 'pointer' : 'default' }}
      >
        <span style={{ fontSize: 10, color: 'var(--text-dim)', flexShrink: 0, width: 12, textAlign: 'center' }}>
          {c.resolved ? (expanded ? '▾' : '▸') : '—'}
        </span>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c.path}>
            {title}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', fontFamily: 'var(--mono)', marginTop: 1 }}>
            {subtitle}
          </div>
        </div>

        {c.arr_service && (
          <span style={{
            fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4, flexShrink: 0,
            background: c.arr_service === 'radarr' ? 'var(--yellow)18' : 'var(--blue)18',
            color:      c.arr_service === 'radarr' ? 'var(--yellow)'   : 'var(--blue)',
            border:     `1px solid ${c.arr_service === 'radarr' ? 'var(--yellow)' : 'var(--blue)'}40`,
          }}>
            {c.arr_service === 'sonarr' && c.file_count > 1 ? `sonarr S${String(c.season_number ?? '?').padStart(2,'0')}` : c.arr_service}
          </span>
        )}

        {c.arr_url && (
          <a
            href={c.arr_url} target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            style={{
              fontSize: 11, color: 'var(--accent)', textDecoration: 'none', flexShrink: 0,
              padding: '2px 8px', borderRadius: 5,
              border: '1px solid var(--accent)40', background: 'var(--accent)10',
            }}
          >
            {c.resolved ? 'Open ↗' : 'Search ↗'}
          </a>
        )}
      </div>

      {expanded && (
        <div style={{ borderTop: '1px solid var(--border)' }}>
          {searchStatus === 'searching' && (
            <div style={{ padding: '16px 14px', color: 'var(--text-dim)', fontSize: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: '50%', border: '2px solid var(--accent)', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />
              Querying indexers… {elapsed > 0 && `(${elapsed}s)`}
            </div>
          )}
          {searchStatus === 'error' && (
            <div style={{ padding: '12px 14px', color: 'var(--red)', fontSize: 12 }}>{errorMsg}</div>
          )}
          {searchStatus === 'done' && releases !== null && releases.length === 0 && (
            <div style={{ padding: '16px 14px', color: 'var(--text-dim)', fontSize: 12 }}>
              No releases found — try opening directly in {c.arr_service}.
            </div>
          )}
          {searchStatus === 'done' && releases !== null && releases.length > 0 && (
            <>
              <div style={{
                display: 'grid', gridTemplateColumns: '1fr 140px 60px 90px 72px',
                gap: 8, padding: '6px 12px', fontSize: 10,
                color: 'var(--text-dim)', fontFamily: 'var(--mono)', letterSpacing: 1,
                textTransform: 'uppercase', background: 'var(--surface2)',
              }}>
                <span>Release</span><span>Indexer</span>
                <span style={{ textAlign: 'right' }}>Seeds</span>
                <span style={{ textAlign: 'right' }}>Size</span>
                <span />
              </div>
              {releases.map((r, i) => (
                <ReleaseRow key={i} r={r} service={c.arr_service} connectionId={c.arr_connection_id} />
              ))}
              {c.arr_url && (
                <div style={{ padding: '8px 14px', fontSize: 11, color: 'var(--text-dim)', background: 'var(--surface2)', borderTop: '1px solid var(--border)' }}>
                  <a href={c.arr_url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)' }}>
                    Open {c.arr_title} in {c.arr_service} ↗
                  </a>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export default function Workflows() {
  const [candidates, setCandidates] = useState(null)
  const [resolvedCount, setResolvedCount]     = useState(0)
  const [unresolvedCount, setUnresolvedCount] = useState(0)
  const [indexers, setIndexers]     = useState([])
  const [downloadFrom, setDownloadFrom] = useState([])
  const [seedingOn, setSeedingOn]       = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState(null)
  const [saving, setSaving]   = useState(false)

  useEffect(() => {
    Promise.all([
      api.acquireCandidates(),
      api.workflowIndexers(),
      api.getConfig(),
    ]).then(([cdata, idata, cfg]) => {
      setCandidates(cdata.candidates || [])
      setResolvedCount(cdata.resolved_count || 0)
      setUnresolvedCount(cdata.unresolved_count || 0)
      setIndexers(idata.indexers || [])
      setDownloadFrom(cfg.ACQUIRE_DOWNLOAD_FROM || [])
      setSeedingOn(cfg.ACQUIRE_SEEDING_ON || [])
    }).catch(e => setError(e.message)).finally(() => setLoading(false))
  }, [])

  const saveFilters = useCallback(async (df, so) => {
    setSaving(true)
    try { await api.saveAcquirePrefs({ ACQUIRE_DOWNLOAD_FROM: df, ACQUIRE_SEEDING_ON: so }) } catch (_) {}
    setSaving(false)
  }, [])

  const handleDownloadFromChange = v => { setDownloadFrom(v); saveFilters(v, seedingOn) }
  const handleSeedingOnChange    = v => { setSeedingOn(v);    saveFilters(downloadFrom, v) }

  const grouped       = useMemo(() => candidates ? groupCandidates(candidates) : [], [candidates])
  const folderGroups  = useMemo(() => groupByFolder(grouped), [grouped])

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 16 }}>

      {/* Header */}
      <div>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: 2.5, textTransform: 'uppercase', marginBottom: 4 }}>
          Workflows
        </div>
        <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--text)', lineHeight: 1.2 }}>Acquire Candidates</div>
        <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.6, maxWidth: 560 }}>
          Media files with no active tracker — fully unseeded. Expand a resolved row to search for releases, then grab inside Sonarr or Radarr.
          Season rows use Sonarr's season pack search. Release searches run in the background and may take 30–90 seconds.
        </p>
      </div>

      {/* Indexer strategy card */}
      {!loading && !error && indexers.length > 0 && (
        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 9, padding: '14px 18px', display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase' }}>
              Indexer Strategy
            </span>
            {saving && <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Saving…</span>}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 18 }}>
            <div>
              <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 3 }}>Download from</div>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8, lineHeight: 1.5 }}>
                Only show releases from these indexers. <em>All</em> = no restriction.
              </div>
              <IndexerChips options={indexers} value={downloadFrom} onChange={handleDownloadFromChange} />
            </div>
            <div>
              <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 3 }}>Must also be seeding on</div>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8, lineHeight: 1.5 }}>
                Only show release groups that appear on ALL of these indexers too. <em>All</em> = no restriction.
              </div>
              <IndexerChips options={indexers} value={seedingOn} onChange={handleSeedingOnChange} />
            </div>
          </div>
        </div>
      )}

      {/* Summary chips */}
      {!loading && !error && candidates !== null && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{
            fontSize: 11, fontFamily: 'var(--mono)', padding: '3px 10px', borderRadius: 99,
            background: 'var(--green)18', border: '1px solid var(--green)40', color: 'var(--green)',
          }}>
            {resolvedCount} resolved
          </span>
          <span style={{
            fontSize: 11, fontFamily: 'var(--mono)', padding: '3px 10px', borderRadius: 99,
            background: 'var(--surface2)', border: '1px solid var(--border)', color: 'var(--text-dim)',
          }}>
            {unresolvedCount} unresolved
          </span>
          <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>· {grouped.length} rows in {folderGroups.length} folder{folderGroups.length !== 1 ? 's' : ''}</span>
        </div>
      )}

      {/* Content */}
      {loading && (
        <div style={{ padding: '60px 0', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-dim)', fontSize: 13 }}>
          Loading candidates…
        </div>
      )}
      {!loading && error && (
        <div style={{ padding: '20px', background: 'var(--red)10', border: '1px solid var(--red)30', borderRadius: 9, color: 'var(--red)', fontSize: 13 }}>
          {error}
        </div>
      )}
      {!loading && !error && grouped.length === 0 && (
        <div style={{ padding: '60px 0', textAlign: 'center', color: 'var(--text-dim)', fontSize: 13 }}>
          No unseeded files found — everything is actively seeding.
        </div>
      )}
      {!loading && !error && folderGroups.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
          {folderGroups.map(([folder, candidates]) => (
            <FolderGroup key={folder} name={folder} candidates={candidates} />
          ))}
        </div>
      )}

      <style>{`
        @keyframes spin { to { transform: rotate(360deg); } }
      `}</style>
    </div>
  )
}
