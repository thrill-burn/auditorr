import React from 'react'
import { Segmented } from './workflows/shared'

export const RANGE_PRESETS = [
  { value: 7,  label: '7d'  },
  { value: 30, label: '30d' },
  { value: 90, label: '90d' },
  { value: 0,  label: 'All' },
]

// Range-preset selector (7d/30d/90d/All), shared by the Trackers page's filter
// row and the Dashboard's Score history header (`size="sm"`, a card's control).
// It was the inset track every filter row in the app now uses, which is why
// `Segmented` is built from it; mono because the labels are readouts.
// `isActive` is a predicate because Trackers derives the active preset from a
// free date range, where none may match.
export function RangePresets({ options = RANGE_PRESETS, isActive, onSelect, size = 'lg' }) {
  const active = options.find(o => isActive(o.value))
  return (
    <Segmented size={size} mono label="Date range" value={active ? active.value : undefined}
      onChange={onSelect} options={options} />
  )
}
