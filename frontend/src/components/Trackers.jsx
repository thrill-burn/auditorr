import React, { useState, useEffect, useRef } from 'react'
import { TrackerCard } from './Dashboard'
import { api } from '../api'

export default function Trackers({ torrentFiles, onNavigate }) {
  const [uploadStats, setUploadStats] = useState(null)
  const [sortKey, setSortKey] = useState('seeding_size')
  const [sortDir, setSortDir] = useState('desc')
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const dropdownRef = useRef(null)

  useEffect(() => {
    api.uploadStats(30).then(d => { if (!d.status) setUploadStats(d) }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!dropdownOpen) return
    const handler = e => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target))
        setDropdownOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [dropdownOpen])

  const allTrackers = [...new Set(
    torrentFiles.flatMap(f => (f.trackers || []).filter(t => t !== 'None'))
  )]

  const [selectedTrackers, setSelectedTrackers] = useState(() => allTrackers)

  // Build stats map for sorting
  const statsMap = {}
  for (const f of torrentFiles) {
    for (const t of (f.trackers || [])) {
      if (t === 'None') continue
      if (!statsMap[t]) statsMap[t] = { seeding_size: 0, uploaded: 0, yield_val: null }
      if (f.status === 'Seeding') statsMap[t].seeding_size += f.size
    }
  }
  for (const ty of (uploadStats?.tracker_yields || [])) {
    if (!statsMap[ty.tracker]) statsMap[ty.tracker] = { seeding_size: 0, uploaded: 0, yield_val: null }
    statsMap[ty.tracker].uploaded = ty.uploaded
    statsMap[ty.tracker].yield_val = ty.yield
  }

  const handleSort = key => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortKey(key); setSortDir('desc') }
  }

  const sortedTrackers = allTrackers
    .filter(t => selectedTrackers.includes(t))
    .sort((a, b) => {
      const sa = statsMap[a] || {}
      const sb = statsMap[b] || {}
      if (sortKey === 'name')
        return sortDir === 'asc' ? a.localeCompare(b) : b.localeCompare(a)
      const va = sortKey === 'seeding_size' ? (sa.seeding_size || 0)
               : sortKey === 'uploaded'     ? (sa.uploaded || 0)
               :                              (sa.yield_val ?? -1)
      const vb = sortKey === 'seeding_size' ? (sb.seeding_size || 0)
               : sortKey === 'uploaded'     ? (sb.uploaded || 0)
               :                              (sb.yield_val ?? -1)
      return sortDir === 'asc' ? va - vb : vb - va
    })

  const sortBtnStyle = key => ({
    background: 'none', border: 'none', cursor: 'pointer', padding: 0,
    fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 1, textTransform: 'uppercase',
    color: sortKey === key ? 'var(--accent)' : 'var(--text-dim)',
    fontWeight: sortKey === key ? 700 : 400,
  })

  const arrow = key => sortKey === key ? (sortDir === 'asc' ? ' ↑' : ' ↓') : ''

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 16 }}>

      {/* Top bar */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', gap: 12 }}>
          {[['name', 'Name'], ['seeding_size', 'Seeding Size'], ['uploaded', 'Uploaded'], ['yield', 'Yield %']].map(([key, label]) => (
            <button key={key} onClick={() => handleSort(key)} style={sortBtnStyle(key)}>
              {label}{arrow(key)}
            </button>
          ))}
        </div>

        {/* Tracker filter dropdown */}
        <div ref={dropdownRef} style={{ position: 'relative' }}>
          <button onClick={() => setDropdownOpen(o => !o)} style={{
            background: 'none', border: 'none', cursor: 'pointer', padding: 0,
            fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 1, textTransform: 'uppercase',
            color: 'var(--text-dim)',
          }}>
            Trackers ({selectedTrackers.length}/{allTrackers.length})
          </button>
          {dropdownOpen && (
            <div style={{
              position: 'absolute', right: 0, top: '100%', marginTop: 6,
              background: 'var(--surface2)', border: '1px solid var(--border)',
              borderRadius: 8, padding: '8px 0', minWidth: 180, zIndex: 100,
            }}>
              <div style={{ display: 'flex', gap: 10, padding: '0 10px 6px', borderBottom: '1px solid var(--border)' }}>
                <button onClick={() => setSelectedTrackers(allTrackers)} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--accent)' }}>Select All</button>
                <button onClick={() => setSelectedTrackers([])} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)' }}>Clear</button>
              </div>
              {allTrackers.map(tracker => (
                <label key={tracker} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 10px', cursor: 'pointer', fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text)' }}>
                  <input
                    type="checkbox"
                    checked={selectedTrackers.includes(tracker)}
                    onChange={e => setSelectedTrackers(prev =>
                      e.target.checked ? [...prev, tracker] : prev.filter(t => t !== tracker)
                    )}
                    style={{ accentColor: 'var(--accent)' }}
                  />
                  {tracker}
                </label>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Grid */}
      {sortedTrackers.length === 0 ? (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', textAlign: 'center', padding: '48px 0' }}>
          No trackers selected
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, alignItems: 'start' }}>
          {sortedTrackers.map(tracker => (
            <TrackerCard
              key={tracker}
              trackerName={tracker}
              torrentFiles={torrentFiles}
              uploadStats={uploadStats}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      )}
    </div>
  )
}
