import React, { useState, useRef, useEffect } from 'react'
import DatePicker from './DatePicker'

export default function FilterBar({ timeRange, onTimeRangeChange, dateFrom, dateTo, onDateFromChange, onDateToChange, selectedTrackers, allTrackers, onTrackersChange, sortControls }) {
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
      {/* Left: time range (presets) or date range picker */}
      {onDateFromChange ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <DatePicker value={dateFrom || ''} onChange={onDateFromChange} placeholder="From" />
          <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>—</span>
          <DatePicker value={dateTo || ''} onChange={onDateToChange} placeholder="To" />
          {(dateFrom || dateTo) && (
            <button
              onClick={() => { onDateFromChange(''); onDateToChange('') }}
              style={{
                background: 'none', border: 'none', cursor: 'pointer', padding: '0 2px',
                fontFamily: 'var(--sans)', fontSize: 11, color: 'var(--text-dim)',
              }}
            >✕</button>
          )}
        </div>
      ) : (
        <div style={{
          display: 'inline-flex', alignItems: 'center', gap: 3, padding: 3,
          background: 'var(--surface3)', borderRadius: 'var(--r)',
        }}>
          {[7, 30, 90, 0].map(d => (
            <button key={d} onClick={() => onTimeRangeChange(d)} style={{
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
              minHeight: 28, padding: '4px 10px', borderRadius: 'var(--r)',
              border: 'none', cursor: 'pointer',
              fontFamily: 'var(--mono)', fontSize: 12, letterSpacing: 0, textTransform: 'none',
              background: timeRange === d ? 'var(--surface)' : 'transparent',
              boxShadow: timeRange === d ? 'var(--elev-1)' : 'none',
              color: timeRange === d ? 'var(--text)' : 'var(--text-dim)',
              fontWeight: timeRange === d ? 700 : 500,
            }}>{d === 0 ? 'All' : d + 'd'}</button>
          ))}
        </div>
      )}

      {/* Centre: sort controls */}
      <div style={{ flex: 1, display: 'flex', justifyContent: 'center' }}>
        {sortControls}
      </div>

      {/* Right: tracker dropdown */}
      <div ref={dropdownRef} style={{ position: 'relative' }}>
        <button onClick={() => setDropdownOpen(o => !o)} style={{
          display: 'inline-flex', alignItems: 'center', minHeight: 34,
          background: 'transparent', border: '1px solid var(--border2)', cursor: 'pointer',
          padding: '4px 12px', borderRadius: 'var(--r)',
          fontFamily: 'var(--sans)', fontSize: 12, fontWeight: 600, letterSpacing: 0, textTransform: 'none',
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
            <div style={{ display: 'flex', gap: 8, padding: '0 10px 8px', borderBottom: '1px solid var(--border)' }}>
              <button onClick={() => onTrackersChange(allTrackers)} style={{ background: 'transparent', border: '1px solid var(--border2)', cursor: 'pointer', padding: '6px 12px', borderRadius: 'var(--r)', fontFamily: 'var(--sans)', fontSize: 12, color: 'var(--text)' }}>Select all</button>
              <button onClick={() => onTrackersChange([])} style={{ background: 'transparent', border: '1px solid var(--border2)', cursor: 'pointer', padding: '6px 12px', borderRadius: 'var(--r)', fontFamily: 'var(--sans)', fontSize: 12, color: 'var(--text-dim)' }}>Clear</button>
            </div>
            {allTrackers.map(tracker => (
              <label key={tracker} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 10px', cursor: 'pointer', fontFamily: 'var(--sans)', fontSize: 12, color: 'var(--text)' }}>
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
