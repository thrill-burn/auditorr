import React, { useState, useEffect, useMemo } from 'react'
import { Button, Segmented, SearchInput, FlagToggle, Dot, tint } from './shared'
import {
  emptyFilterState, normalizeFilterState, activeFilterCount, applyListFilters, trackerCounts, rangeBounds, rangeErrors,
} from './listFilterRules'

export { DURATION_UNITS, SIZE_UNITS, applyListFilters } from './listFilterRules'

// ── Workflow list filters ─────────────────────────────────────────────────────
// One filter bar for the workflow pages that list rows: a search, an include /
// exclude tracker panel (File Explorer's), and min–max ranges such as seed time,
// age and size (File Explorer's size range, in any unit). The rules are in
// listFilterRules.js; this file is only the controls and their state.
//
// The state lives in sessionStorage per page, so moving to another tab and back
// keeps it, while a fresh visit starts unfiltered: these pages lead to deletes,
// and a filter left over from last week would hide rows without saying so.

const DIVIDER = { width: 1, height: 18, background: 'var(--border2)', margin: '0 3px', flexShrink: 0 }
// The bar's labels, suffixes, hint and count line: all words, so Geist. What's
// typed into its boxes is data and stays mono.
const DIM = { fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', whiteSpace: 'nowrap' }

function readStored(storageKey, fields) {
  try {
    const raw = storageKey && sessionStorage.getItem(storageKey)
    return normalizeFilterState(raw ? JSON.parse(raw) : null, fields)
  } catch {
    return emptyFilterState(fields)
  }
}

// [state, setState, result] — `result` is `applyListFilters` for `items`,
// recomputed when either changes. `initialQuery` (a tile on the Dashboard's
// library map, 2026-11-09) starts the page on an empty filter with just that
// search, rather than the stored one: a tracker or range left from earlier
// could hide the very row the tile was for.
export function useListFilters(storageKey, fields, items, initialQuery) {
  const [state, setState] = useState(() => (initialQuery
    ? { ...emptyFilterState(fields), q: String(initialQuery) }
    : readStored(storageKey, fields)))
  useEffect(() => {
    try { sessionStorage.setItem(storageKey, JSON.stringify(state)) } catch { /* private mode */ }
  }, [storageKey, state])
  const result = useMemo(() => applyListFilters(items, fields, state), [items, fields, state])
  return [state, setState, result]
}

// A selection must never act on a row the filter hides: the action bar names
// what it touches, and a hidden row is one nobody is looking at. So narrowing
// the filter drops hidden rows from the selection, rather than letting a
// delete carry them along unseen.
export function usePruneSelection(visibleKeys, setSelected) {
  useEffect(() => {
    const keep = new Set(visibleKeys)
    setSelected(prev => {
      let changed = false
      const next = new Set()
      for (const k of prev) keep.has(k) ? next.add(k) : (changed = true)
      return changed ? next : prev
    })
  }, [visibleKeys, setSelected])
}

// `problem` is why the box's value isn't used, said on hover and by a red edge:
// a range the rules ignore, or one that matches nothing, otherwise looks set.
function RangeInput({ value, onChange, placeholder, label, problem }) {
  const [focused, setFocused] = useState(false)
  return (
    <input
      type="number" min="0" step="any"
      className="ph-words"
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={`${label} ${placeholder}`}
      aria-invalid={!!problem}
      title={problem || undefined}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      style={{
        width: 64, height: 'var(--control-h)', padding: '0 8px',
        borderRadius: 'var(--r)', fontSize: 'var(--font-base)',
        border: `1px solid ${problem ? tint('var(--red)', 70) : focused ? 'var(--accent)' : value ? tint('var(--accent)', 40) : 'var(--border2)'}`,
        background: focused || value ? 'var(--surface2)' : 'transparent',
        color: 'var(--text)', fontFamily: 'var(--mono)',
        outline: 'none', transition: 'all 0.12s',
      }}
    />
  )
}

function RangeFilter({ field, value, onChange }) {
  const set = patch => onChange({ ...value, ...patch })
  const active = !!rangeBounds(field, value)
  const errors = rangeErrors(value)
  const units = field.units ? Object.entries(field.units).map(([v, u]) => ({ value: v, label: u.label })) : null
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }} title={field.hint}>
      <span style={{ ...DIM, color: active ? 'var(--accent)' : 'var(--text-dim)' }}>{field.label}:</span>
      <RangeInput label={field.label} value={value.min} onChange={min => set({ min })} placeholder="min" problem={errors.min} />
      <span style={DIM}>–</span>
      <RangeInput label={field.label} value={value.max} onChange={max => set({ max })} placeholder="max" problem={errors.max} />
      {units && (
        <Segmented mono label={`${field.label} unit`} options={units} value={value.unit} onChange={unit => set({ unit })} />
      )}
      {field.suffix && <span style={DIM}>{field.suffix}</span>}
      {/* On whatever a box holds, as File Explorer's, so a value the rules
          ignore can still be cleared in one click. */}
      {(value.min || value.max) && (
        <Button size="sm" variant="ghost" square onClick={() => set({ min: '', max: '' })}
          title={`Clear ${field.label.toLowerCase()}`} ariaLabel={`Clear ${field.label.toLowerCase()}`}>✕</Button>
      )}
    </div>
  )
}

// `items` is the unfiltered list (the tracker panel counts from it), `shown`
// how many rows pass, `noun` what a row is ("torrent", "file").
// `unknownNote` says why a row can lack a ranged value, for the line that
// counts the rows hidden for that reason. `unloaded` is how many rows the page
// never loaded (Triage's cap): no filter reaches them, so a filtered count
// says so rather than reading as all there is.
export function ListFilterBar({ fields, state, onChange, items, shown, unknownHidden = 0, noun = 'row', unknownNote, unloaded = 0 }) {
  const [trackersOpen, setTrackersOpen] = useState(false)
  const search = fields.find(f => f.kind === 'search')
  const trackerField = fields.find(f => f.kind === 'trackers')
  const ranges = fields.filter(f => f.kind === 'range')
  const trackers = useMemo(() => (trackerField ? trackerCounts(items, trackerField) : []), [items, trackerField])
  const active = activeFilterCount(fields, state)
  const trackerActive = state.inc.length + state.exc.length
  const plural = n => `${n.toLocaleString()} ${noun}${n !== 1 ? 's' : ''}`

  const setTracker = (t, v) => onChange(s => ({
    ...s,
    inc: v === 'only' ? [...s.inc.filter(x => x !== t), t] : s.inc.filter(x => x !== t),
    exc: v === 'hide' ? [...s.exc.filter(x => x !== t), t] : s.exc.filter(x => x !== t),
  }))

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--r)',
      boxShadow: 'var(--elev-1)', padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 8,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px 12px', flexWrap: 'wrap' }}>
        <span className="ui-field-label" style={{ color: 'var(--text-dim)' }}>Filter</span>
        {search && (
          <SearchInput value={state.q} onChange={q => onChange(s => ({ ...s, q }))}
            placeholder={search.placeholder || 'Search…'} width={200} />
        )}
        {trackerField && trackers.length > 0 && (
          <Button size="sm" variant={trackersOpen ? 'secondary' : 'ghost'} pressed={trackersOpen}
            onClick={() => setTrackersOpen(o => !o)}>
            <Dot color="var(--blue)" size={6} />
            {'Trackers' + (trackerActive > 0 ? ` (${trackerActive})` : '')}
          </Button>
        )}
        {ranges.map(f => (
          <React.Fragment key={f.key}>
            <div style={DIVIDER} />
            <RangeFilter field={f} value={state.ranges[f.key]}
              onChange={r => onChange(s => ({ ...s, ranges: { ...s.ranges, [f.key]: r } }))} />
          </React.Fragment>
        ))}
      </div>

      {trackersOpen && trackers.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center', paddingTop: 8, borderTop: '1px solid var(--border)' }}>
          <span style={{ ...DIM, marginRight: 4 }}>+ include / − exclude</span>
          {trackers.map(({ name, count }) => (
            <FlagToggle key={name} label={`${name} (${count})`}
              value={state.inc.includes(name) ? 'only' : state.exc.includes(name) ? 'hide' : 'any'}
              onChange={v => setTracker(name, v)}
              onlyTitle={`Show only ${noun}s on ${name}`} hideTitle={`Hide ${noun}s on ${name}`} />
          ))}
          {trackerActive > 0 && (
            <Button size="sm" variant="ghost" onClick={() => onChange(s => ({ ...s, inc: [], exc: [] }))}>Clear</Button>
          )}
        </div>
      )}

      {active > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ ...DIM, color: 'var(--accent)' }}>
            Showing {shown.toLocaleString()} of {plural(items.length)}
          </span>
          {unknownHidden > 0 && (
            <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
              · {plural(unknownHidden)} hidden for lacking a filtered value{unknownNote ? ` — ${unknownNote}` : ''}
            </span>
          )}
          {unloaded > 0 && (
            <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
              · {unloaded.toLocaleString()} more {noun}{unloaded !== 1 ? 's aren’t' : ' isn’t'} loaded, so the
              filters can’t reach {unloaded !== 1 ? 'them' : 'it'}
            </span>
          )}
          <div style={{ flex: 1 }} />
          <Button size="sm" variant="ghost" onClick={() => onChange(emptyFilterState(fields))}>Clear all filters</Button>
        </div>
      )}
    </div>
  )
}
