import React, { useState, useRef, useEffect } from 'react'

export const RANGE_PRESETS = [
  { value: 7,  label: '7d'  },
  { value: 30, label: '30d' },
  { value: 90, label: '90d' },
  { value: 0,  label: 'All' },
]

// Segmented range-preset selector (7d/30d/90d/All). The single source of truth
// for this control, shared by the Dashboard filter row and the Trackers page so
// the two can never drift in size or style. 34px tall (28 + 3px track padding),
// matching the DatePicker and Trackers-dropdown heights.
export function RangePresets({ options = RANGE_PRESETS, isActive, onSelect }) {
  return (
    <div style={{ display: 'inline-flex', alignItems: 'center', gap: 3, padding: 3, background: 'var(--surface3)', borderRadius: 'var(--r)' }}>
      {options.map(o => {
        const active = isActive(o.value)
        return (
          <button key={o.value} onClick={() => onSelect(o.value)} style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            minHeight: 28, padding: '4px 12px', borderRadius: 'var(--r)',
            border: 'none', cursor: 'pointer',
            fontFamily: 'var(--mono)', fontSize: 12, letterSpacing: 0, textTransform: 'none',
            background: active ? 'var(--surface)' : 'transparent',
            boxShadow: active ? 'var(--elev-1)' : 'none',
            color: active ? 'var(--text)' : 'var(--text-dim)',
            fontWeight: active ? 700 : 500, transition: 'all 0.12s',
          }}>{o.label}</button>
        )
      })}
    </div>
  )
}

// Tracker multi-select dropdown — the right-hand filter on the Dashboard's top
// row. Owns its own open / outside-click state. `selectedTrackers === null`
// means "all" (the untouched default).
export function TrackerDropdown({ selectedTrackers, allTrackers, onTrackersChange }) {
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
  )
}
