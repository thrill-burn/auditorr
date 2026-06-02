import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api'
import { formatBytes } from '../utils'

// ── Multi-select filter pill ──────────────────────────────────────────────────
function MultiSelect({ label, options, value, onChange }) {
  const [open, setOpen] = useState(false)
  const toggle = (opt) => {
    onChange(value.includes(opt) ? value.filter(v => v !== opt) : [...value, opt])
  }
  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'flex', alignItems: 'center', gap: 6,
          padding: '6px 10px', borderRadius: 7,
          border: '1px solid var(--border2)', background: 'var(--surface2)',
          color: value.length ? 'var(--text)' : 'var(--text-dim)',
          fontSize: 12, cursor: 'pointer', whiteSpace: 'nowrap',
        }}
      >
        {label}{value.length > 0 && <span style={{ color: 'var(--accent)', fontWeight: 600 }}> ({value.length})</span>}
        <span style={{ fontSize: 10, color: 'var(--text-dim)', marginLeft: 2 }}>▾</span>
      </button>
      {open && (
        <>
          <div onClick={() => setOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 49 }} />
          <div style={{
            position: 'absolute', top: '100%', left: 0, zIndex: 50, marginTop: 4,
            background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8,
            padding: '6px 0', minWidth: 180, maxHeight: 240, overflowY: 'auto',
          }}>
            {options.length === 0 && (
              <div style={{ padding: '8px 14px', fontSize: 12, color: 'var(--text-dim)' }}>No indexers found</div>
            )}
            {options.map(opt => (
              <label key={opt} style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '6px 14px', cursor: 'pointer', fontSize: 12,
              }}
              onMouseEnter={e => e.currentTarget.style.background = 'var(--surface2)'}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
              >
                <input type="checkbox" checked={value.includes(opt)} onChange={() => toggle(opt)} style={{ accentColor: 'var(--accent)' }} />
                <span style={{ color: 'var(--text)' }}>{opt}</span>
              </label>
            ))}
            {value.length > 0 && (
              <div style={{ borderTop: '1px solid var(--border)', marginTop: 4, padding: '4px 14px 2px' }}>
                <button onClick={() => { onChange([]); setOpen(false) }} style={{ background: 'none', border: 'none', padding: 0, fontSize: 11, color: 'var(--text-dim)', cursor: 'pointer' }}>
                  Clear all
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

// ── Release matrix row ────────────────────────────────────────────────────────
function ReleaseRow({ r }) {
  return (
    <div style={{
      display: 'grid', gridTemplateColumns: '1fr 140px 60px 90px',
      gap: 8, padding: '7px 12px', fontSize: 12, alignItems: 'center',
      borderBottom: '1px solid var(--border)',
    }}>
      <span style={{ color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.title}>{r.title}</span>
      <span style={{ color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.indexer}>{r.indexer}</span>
      <span style={{ color: r.seeders > 0 ? 'var(--green)' : 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 11, textAlign: 'right' }}>{r.seeders}S</span>
      <span style={{ color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 11, textAlign: 'right' }}>{formatBytes(r.size)}</span>
    </div>
  )
}

// ── Candidate row ─────────────────────────────────────────────────────────────
function CandidateRow({ c, downloadFrom, seedingOn }) {
  const [expanded, setExpanded] = useState(false)
  const [releases, setReleases] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const handleExpand = useCallback(async () => {
    if (!c.resolved) return
    setExpanded(e => !e)
    if (releases !== null) return
    setLoading(true)
    try {
      const params = {
        service: c.arr_service,
        connection_id: c.arr_connection_id,
        arr_id: c.arr_id,
        path: c.path,
      }
      if (c.episode_id) params.episode_id = c.episode_id
      const data = await api.acquireReleases(params)
      setReleases(data.releases || [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [c, releases])

  const filename = (c.path || '').split('/').pop()
  const title = c.arr_title || filename

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 9,
      overflow: 'hidden',
      opacity: c.resolved ? 1 : 0.65,
    }}>
      <div
        onClick={c.resolved ? handleExpand : undefined}
        style={{
          display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px',
          cursor: c.resolved ? 'pointer' : 'default',
        }}
      >
        {/* Expand chevron */}
        <span style={{ fontSize: 10, color: 'var(--text-dim)', flexShrink: 0, width: 12, textAlign: 'center' }}>
          {c.resolved ? (expanded ? '▾' : '▸') : '—'}
        </span>

        {/* Title */}
        <span style={{ flex: 1, fontSize: 13, fontWeight: 500, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c.path}>
          {title}
        </span>

        {/* Size */}
        <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
          {formatBytes(c.size || 0)}
        </span>

        {/* Service badge */}
        {c.arr_service && (
          <span style={{
            fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4,
            background: c.arr_service === 'radarr' ? 'var(--yellow)18' : 'var(--blue)18',
            color: c.arr_service === 'radarr' ? 'var(--yellow)' : 'var(--blue)',
            border: `1px solid ${c.arr_service === 'radarr' ? 'var(--yellow)' : 'var(--blue)'}40`,
            flexShrink: 0,
          }}>
            {c.arr_service}
          </span>
        )}

        {/* Open in Arr link */}
        {c.arr_url && (
          <a
            href={c.arr_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            style={{
              fontSize: 11, color: 'var(--accent)', textDecoration: 'none', flexShrink: 0,
              padding: '2px 8px', borderRadius: 5, border: '1px solid var(--accent)40',
              background: 'var(--accent)10',
            }}
          >
            {c.resolved ? 'Open ↗' : 'Search ↗'}
          </a>
        )}
      </div>

      {expanded && (
        <div style={{ borderTop: '1px solid var(--border)' }}>
          {loading && (
            <div style={{ padding: '20px 14px', color: 'var(--text-dim)', fontSize: 12 }}>Loading releases…</div>
          )}
          {error && (
            <div style={{ padding: '12px 14px', color: 'var(--red)', fontSize: 12 }}>{error}</div>
          )}
          {!loading && !error && releases !== null && releases.length === 0 && (
            <div style={{ padding: '16px 14px', color: 'var(--text-dim)', fontSize: 12 }}>
              No releases found — try opening directly in {c.arr_service}.
            </div>
          )}
          {!loading && !error && releases !== null && releases.length > 0 && (
            <>
              <div style={{
                display: 'grid', gridTemplateColumns: '1fr 140px 60px 90px',
                gap: 8, padding: '6px 12px', fontSize: 10,
                color: 'var(--text-dim)', fontFamily: 'var(--mono)', letterSpacing: 1,
                textTransform: 'uppercase', background: 'var(--surface2)',
              }}>
                <span>Release</span>
                <span>Indexer</span>
                <span style={{ textAlign: 'right' }}>Seeds</span>
                <span style={{ textAlign: 'right' }}>Size</span>
              </div>
              {releases.map((r, i) => <ReleaseRow key={i} r={r} />)}
              <div style={{ padding: '10px 14px', fontSize: 11, color: 'var(--text-dim)', background: 'var(--surface2)' }}>
                Grab releases inside {c.arr_service} — {' '}
                <a href={c.arr_url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)' }}>
                  Open {c.arr_title} ↗
                </a>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main Workflows component ──────────────────────────────────────────────────
export default function Workflows({ onNavigate }) {
  const [candidates, setCandidates] = useState(null)
  const [resolvedCount, setResolvedCount] = useState(0)
  const [unresolvedCount, setUnresolvedCount] = useState(0)
  const [indexers, setIndexers] = useState([])
  const [downloadFrom, setDownloadFrom] = useState([])
  const [seedingOn, setSeedingOn] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

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

  const saveFilters = useCallback(async (nextDownloadFrom, nextSeedingOn) => {
    setSaving(true)
    try {
      await api.saveConfig({ ACQUIRE_DOWNLOAD_FROM: nextDownloadFrom, ACQUIRE_SEEDING_ON: nextSeedingOn })
    } catch (_) {}
    setSaving(false)
  }, [])

  const handleDownloadFromChange = (val) => {
    setDownloadFrom(val)
    saveFilters(val, seedingOn)
  }

  const handleSeedingOnChange = (val) => {
    setSeedingOn(val)
    saveFilters(downloadFrom, val)
  }

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 16 }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: 2.5, textTransform: 'uppercase', marginBottom: 4 }}>
            Workflows
          </div>
          <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--text)', lineHeight: 1.2 }}>Acquire Candidates</div>
          <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.6, maxWidth: 560 }}>
            Media files with no active tracker — fully unseeded. Expand a resolved row to see available releases, then grab inside Sonarr or Radarr.
          </p>
        </div>

        {/* Filter bar */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {saving && <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Saving…</span>}
          <MultiSelect
            label="Download from"
            options={indexers}
            value={downloadFrom}
            onChange={handleDownloadFromChange}
          />
          <MultiSelect
            label="Seeding on"
            options={indexers}
            value={seedingOn}
            onChange={handleSeedingOnChange}
          />
        </div>
      </div>

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
          {downloadFrom.length > 0 && (
            <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
              · releases filtered to: {downloadFrom.join(', ')}
            </span>
          )}
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
      {!loading && !error && candidates !== null && candidates.length === 0 && (
        <div style={{ padding: '60px 0', textAlign: 'center', color: 'var(--text-dim)', fontSize: 13 }}>
          No unseeded files found — everything is actively seeding.
        </div>
      )}
      {!loading && !error && candidates !== null && candidates.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {candidates.map((c, i) => (
            <CandidateRow
              key={i}
              c={c}
              downloadFrom={downloadFrom}
              seedingOn={seedingOn}
            />
          ))}
        </div>
      )}
    </div>
  )
}
