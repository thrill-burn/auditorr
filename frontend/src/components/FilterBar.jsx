import React, { useState, useRef, useEffect } from 'react'

export default function FilterBar({ timeRange, onTimeRangeChange, selectedTrackers, allTrackers, onTrackersChange, sortControls }) {
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const dropdownRef = useRef(null)
  const effectiveTrackers = selectedTrackers ?? allTrackers

  useEffect(() => {
    if (!dropdownOpen) return
    const handler = e => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target))
        setDropdownOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [dropdownOpen])

  return (
    <div style={{
      position: 'sticky', top: 0, zIndex: 50,
      background: 'var(--bg)', borderBottom: '1px solid var(--border)',
      padding: '8px 20px',
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    }}>
      {/* Left: time range */}
      <div style={{ display: 'flex', gap: 10 }}>
        {[7, 30, 90].map(d => (
          <button key={d} onClick={() => onTimeRangeChange(d)} style={{
            background: 'none', border: 'none', cursor: 'pointer', padding: 0,
            fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 1, textTransform: 'uppercase',
            color: timeRange === d ? 'var(--accent)' : 'var(--text-dim)',
            fontWeight: timeRange === d ? 700 : 400,
          }}>{d}d</button>
        ))}
      </div>

      {/* Centre: sort controls */}
      <div style={{ flex: 1, display: 'flex', justifyContent: 'center' }}>
        {sortControls}
      </div>

      {/* Right: tracker dropdown */}
      <div ref={dropdownRef} style={{ position: 'relative' }}>
        <button onClick={() => setDropdownOpen(o => !o)} style={{
          background: 'none', border: 'none', cursor: 'pointer', padding: 0,
          fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 1, textTransform: 'uppercase',
          color: 'var(--text-dim)',
        }}>
          Trackers ({effectiveTrackers.length}/{allTrackers.length})
        </button>
        {dropdownOpen && (
          <div style={{
            position: 'absolute', right: 0, top: '100%', marginTop: 6,
            background: 'var(--surface2)', border: '1px solid var(--border)',
            borderRadius: 8, padding: '8px 0', minWidth: 180, zIndex: 100,
          }}>
            <div style={{ display: 'flex', gap: 10, padding: '0 10px 6px', borderBottom: '1px solid var(--border)' }}>
              <button onClick={() => onTrackersChange(allTrackers)} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--accent)' }}>Select All</button>
              <button onClick={() => onTrackersChange([])} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)' }}>Clear</button>
            </div>
            {allTrackers.map(tracker => (
              <label key={tracker} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 10px', cursor: 'pointer', fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text)' }}>
                <input
                  type="checkbox"
                  checked={effectiveTrackers.includes(tracker)}
                  onChange={e => onTrackersChange(
                    e.target.checked
                      ? [...effectiveTrackers, tracker]
                      : effectiveTrackers.filter(t => t !== tracker)
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
  )
}
