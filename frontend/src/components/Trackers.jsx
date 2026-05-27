import React, { useState, useEffect } from 'react'
import { TrackerCard } from './Dashboard'
import FilterBar from './FilterBar'
import { api } from '../api'

function initialDateFrom(timeRange) {
  if (!timeRange) return ''
  const d = new Date()
  d.setDate(d.getDate() - timeRange)
  return d.toISOString().slice(0, 10)
}

export default function Trackers({ trackerFileStats, onNavigate, timeRange, selectedTrackers, allTrackers, onTrackersChange }) {
  const [uploadStats, setUploadStats] = useState(null)
  const [sortKey,     setSortKey]     = useState('seedingSize')
  const [sortDir,     setSortDir]     = useState('desc')
  const [dateFrom,    setDateFrom]    = useState(() => initialDateFrom(timeRange))
  const [dateTo,      setDateTo]      = useState('')

  useEffect(() => {
    const params = (dateFrom || dateTo) ? { from: dateFrom || undefined, to: dateTo || undefined } : 0
    api.uploadStats(params).then(d => { if (!d.status) setUploadStats(d) }).catch(() => {})
  }, [dateFrom, dateTo])

  // Build stats map for sorting — seeding sizes come from pre-computed backend summary
  const statsMap = {}
  for (const [t, s] of Object.entries(trackerFileStats || {})) {
    statsMap[t] = { seeding_size: s.seeding_size || 0, uploaded: 0, yield_val: null }
  }
  for (const ty of (uploadStats?.tracker_yields || [])) {
    if (!statsMap[ty.tracker]) statsMap[ty.tracker] = { seeding_size: 0, uploaded: 0, yield_val: null }
    statsMap[ty.tracker].uploaded = ty.uploaded
    statsMap[ty.tracker].yield_val = ty.yield
  }

  const handleSortClick = key => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortKey(key); setSortDir('desc') }
  }

  const effectiveTrackers = selectedTrackers ?? allTrackers

  const sortedTrackers = allTrackers
    .filter(t => effectiveTrackers.includes(t))
    .sort((a, b) => {
      const sa = statsMap[a] || {}
      const sb = statsMap[b] || {}
      if (sortKey === 'name')
        return sortDir === 'asc' ? a.localeCompare(b) : b.localeCompare(a)
      const va = sortKey === 'seedingSize' ? (sa.seeding_size || 0)
               : sortKey === 'uploaded'    ? (sa.uploaded || 0)
               :                             (sa.yield_val ?? -1)
      const vb = sortKey === 'seedingSize' ? (sb.seeding_size || 0)
               : sortKey === 'uploaded'    ? (sb.uploaded || 0)
               :                             (sb.yield_val ?? -1)
      return sortDir === 'asc' ? va - vb : vb - va
    })

  const sortControls = (
    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
      {[
        { key: 'name',        label: 'Name'     },
        { key: 'seedingSize', label: 'Seeding'  },
        { key: 'uploaded',    label: 'Uploaded' },
        { key: 'yield',       label: 'Yield %'  },
      ].map(({ key, label }) => (
        <button key={key} onClick={() => handleSortClick(key)} style={{
          background: 'none', border: 'none', cursor: 'pointer', padding: '2px 6px',
          fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 1, textTransform: 'uppercase',
          color: sortKey === key ? 'var(--accent)' : 'var(--text-dim)',
          fontWeight: sortKey === key ? 700 : 400,
        }}>
          {label}{sortKey === key ? (sortDir === 'asc' ? ' ↑' : ' ↓') : ''}
        </button>
      ))}
    </div>
  )

  return (
    <>
    <FilterBar
      dateFrom={dateFrom}
      dateTo={dateTo}
      onDateFromChange={setDateFrom}
      onDateToChange={setDateTo}
      selectedTrackers={selectedTrackers}
      allTrackers={allTrackers}
      onTrackersChange={onTrackersChange}
      sortControls={sortControls}
    />
      {/* Cards */}
      {sortedTrackers.length === 0 ? (
        <div className="fade-in" style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', textAlign: 'center', padding: '48px 28px' }}>
          No trackers selected
        </div>
      ) : (
        <div className="fade-in" style={{ display: 'flex', flexWrap: 'wrap', gap: '16px', padding: '20px 28px 48px' }}>
          {sortedTrackers.map(tracker => (
            <div key={tracker} style={{ width: '860px', flexShrink: 0 }}>
              <TrackerCard
                trackerName={tracker}
                trackerStats={trackerFileStats?.[tracker]}
                uploadStats={uploadStats}
                onNavigate={onNavigate}
              />
            </div>
          ))}
        </div>
      )}
    </>
  )
}
