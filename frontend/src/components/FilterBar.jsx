import React, { useState, useRef, useEffect } from 'react'
import { Button, Segmented } from './workflows/shared'

export const RANGE_PRESETS = [
  { value: 7,  label: '7d'  },
  { value: 30, label: '30d' },
  { value: 90, label: '90d' },
  { value: 0,  label: 'All' },
]

// Range-preset selector (7d/30d/90d/All), shared by the Dashboard filter row
// and the Trackers page. It was the inset track every filter row in the app now
// uses, which is why `Segmented` is built from it; mono because the labels are
// readouts. `isActive` is a predicate because Trackers derives the active
// preset from a free date range, where none may match.
export function RangePresets({ options = RANGE_PRESETS, isActive, onSelect }) {
  const active = options.find(o => isActive(o.value))
  return (
    <Segmented size="lg" mono label="Date range" value={active ? active.value : undefined}
      onChange={onSelect} options={options} />
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
      <Button variant="ghost" pressed={dropdownOpen} onClick={() => setDropdownOpen(o => !o)}>
        Trackers ({effectiveTrackers.length}/{allTrackers.length})
      </Button>
      {dropdownOpen && (
        <div style={{
          position: 'absolute', right: 0, top: '100%', marginTop: 6,
          background: 'var(--surface2)', border: '1px solid var(--border)',
          borderRadius: 8, padding: '8px 0', minWidth: 180, zIndex: 100, boxShadow: 'var(--shadow-pop)',
        }}>
          <div style={{ display: 'flex', gap: 6, padding: '0 10px 8px', borderBottom: '1px solid var(--border)' }}>
            <Button size="sm" onClick={() => onTrackersChange(allTrackers)}>Select all</Button>
            <Button size="sm" variant="ghost" onClick={() => onTrackersChange([])}>Clear</Button>
          </div>
          {allTrackers.map(tracker => (
            <label key={tracker} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 10px', cursor: 'pointer', fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', color: 'var(--text)' }}>
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
