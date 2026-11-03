// Pure filtering behind `ListFilterBar` (ListFilters.jsx). No React here, so
// the rules can be read, and run under plain node, apart from the controls.
//
// A page describes what it can filter on as a list of fields:
//
//   { key, kind: 'search',   text: item => string }
//   { key, kind: 'trackers', get:  item => [host, …] }
//   { key, kind: 'range',    get:  (item, nowSecs) => number | null, units?: [...] }
//
// and the state is one plain object, so it persists to sessionStorage as is:
//
//   { q: '', inc: [], exc: [], ranges: { [key]: { min: '', max: '', unit } } }
//
// **A value the row does not have fails an active range.** Seed time, age and
// ratio arrive on Triage only with live verification, and a row that cannot
// show it is above 30 days is not shown as if it were. Those rows are counted
// apart (`unknownHidden`), so the page can say why they are missing rather
// than leaving them to look like they were filtered on a known value.

export const DURATION_UNITS = {
  h:  { label: 'h',  mult: 3600 },
  d:  { label: 'd',  mult: 86400 },
  w:  { label: 'w',  mult: 7 * 86400 },
  mo: { label: 'mo', mult: 30 * 86400 },
  y:  { label: 'y',  mult: 365 * 86400 },
}

// Powers of 1024, as `formatBytes` prints them, so "≥ 10 GB" and a row
// reading "10.0 GB" agree.
export const SIZE_UNITS = {
  MB: { label: 'MB', mult: 1024 ** 2 },
  GB: { label: 'GB', mult: 1024 ** 3 },
  TB: { label: 'TB', mult: 1024 ** 4 },
}

export function emptyFilterState(fields) {
  const ranges = {}
  for (const f of fields) {
    if (f.kind === 'range') ranges[f.key] = { min: '', max: '', unit: f.defaultUnit || null }
  }
  return { q: '', inc: [], exc: [], ranges }
}

// A stored state is trusted only as far as it still fits the page's fields: a
// field added since is filled in empty, one removed is dropped, and a unit the
// field no longer offers falls back to its default.
export function normalizeFilterState(raw, fields) {
  const base = emptyFilterState(fields)
  if (!raw || typeof raw !== 'object') return base
  const strs = v => (Array.isArray(v) ? v.filter(x => typeof x === 'string') : [])
  const out = { q: typeof raw.q === 'string' ? raw.q : '', inc: strs(raw.inc), exc: strs(raw.exc), ranges: base.ranges }
  for (const f of fields) {
    if (f.kind !== 'range') continue
    const r = raw.ranges?.[f.key]
    if (!r) continue
    const units = f.units || null
    out.ranges[f.key] = {
      min:  typeof r.min === 'string' ? r.min : '',
      max:  typeof r.max === 'string' ? r.max : '',
      unit: units && r.unit in units ? r.unit : base.ranges[f.key].unit,
    }
  }
  return out
}

const num = s => {
  if (s === '' || s == null) return null
  const n = Number(s)
  return Number.isFinite(n) && n >= 0 ? n : null
}

// The bounds a range's inputs mean, in the field's base unit (seconds, bytes),
// or null when neither bound is a usable number.
export function rangeBounds(field, r) {
  if (!r) return null
  const mult = field.units && r.unit ? (field.units[r.unit]?.mult ?? 1) : 1
  const lo = num(r.min), hi = num(r.max)
  if (lo == null && hi == null) return null
  return { lo: lo == null ? null : lo * mult, hi: hi == null ? null : hi * mult }
}

export function activeFilterCount(fields, state) {
  let n = 0
  for (const f of fields) {
    if (f.kind === 'search' && state.q.trim()) n++
    else if (f.kind === 'trackers') n += state.inc.length + state.exc.length
    else if (f.kind === 'range' && rangeBounds(f, state.ranges[f.key])) n++
  }
  return n
}

export function applyListFilters(items, fields, state, nowSecs = Date.now() / 1000) {
  const q = state.q.trim().toLowerCase()
  const inc = new Set(state.inc), exc = new Set(state.exc)
  const checks = []
  for (const f of fields) {
    if (f.kind === 'search' && q) {
      checks.push(item => ((f.text(item) || '').toLowerCase().includes(q) ? 'ok' : 'no'))
    } else if (f.kind === 'trackers' && (inc.size || exc.size)) {
      checks.push(item => {
        const ts = f.get(item) || []
        if (inc.size && !ts.some(t => inc.has(t))) return 'no'
        if (exc.size && ts.some(t => exc.has(t))) return 'no'
        return 'ok'
      })
    } else if (f.kind === 'range') {
      const b = rangeBounds(f, state.ranges[f.key])
      if (!b) continue
      checks.push(item => {
        const v = f.get(item, nowSecs)
        if (v == null || !Number.isFinite(v)) return 'unknown'
        if (b.lo != null && v < b.lo) return 'no'
        if (b.hi != null && v > b.hi) return 'no'
        return 'ok'
      })
    }
  }
  if (checks.length === 0) return { visible: items, unknownHidden: 0 }
  const visible = []
  let unknownHidden = 0
  for (const item of items) {
    let known = false, unknown = false
    for (const c of checks) {
      const r = c(item)
      if (r === 'no') { known = true; break }
      if (r === 'unknown') unknown = true
    }
    if (!known && !unknown) visible.push(item)
    else if (!known) unknownHidden++
  }
  return { visible, unknownHidden }
}

// Every tracker the items name, with how many rows each is on, most first.
export function trackerCounts(items, field) {
  const m = new Map()
  for (const item of items) for (const t of new Set(field.get(item) || [])) m.set(t, (m.get(t) || 0) + 1)
  return [...m].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([name, count]) => ({ name, count }))
}
