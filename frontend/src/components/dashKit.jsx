import React from 'react'

// The Dashboard's pieces that the Trackers page shares since the upload panels
// moved there (2026-11-09): the card frame, the section label, the two small
// text styles, the trend pill, the chart date, and the display numeral. They
// lived in Dashboard.jsx, and a second copy is how two pages' numbers drift apart.

// Shared section-header label style — keeps every dashboard card title uniform.
export const SECTION_LABEL = {
  fontFamily: 'var(--sans)',
  fontSize: 'var(--font-md)',
  fontWeight: 600,
  color: 'var(--text)',
  letterSpacing: 0,
  textTransform: 'none',
  textAlign: 'left',
}

// A dashboard card's frame. Padding is each card's own.
export const CARD = {
  background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12,
  boxShadow: 'var(--elev-1)', display: 'flex', flexDirection: 'column', minWidth: 0,
}

// ── Typeface by kind ─────────────────────────────────────────────────────────
// Geist Mono is for data: a figure (a number with its unit or the noun it
// counts, a date, a time), a line made only of figures joined by `·`, and a
// name that comes from the disk or the client (a folder, path, release name,
// hostname). Geist is for words: a label, a legend or state name, a caption
// beside or under a figure, a verdict, a hint, a sentence, and any line with a
// phrase in it, numbers and all (the metric cards' "0 files · threshold 157.0 GB").
//
// The 2026-11-09 cards set nearly every line in mono through one local META
// style, so beside the four metric cards they read as a terminal (fixed
// 2026-11-10). Small text on a dashboard card is one of these two, picked by
// what it is, never by what the line above it used.
export const CAPTION = { fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }
export const READOUT = { fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }

// ── Trend pill ───────────────────────────────────────────────────────────────
// Sentiment-based, NOT directional: it reports whether the metric's *health* is
// improving/worsening, decoupled from whether the raw number went up or down.
// The metric cards' word, or a caller's own `label` and `icon` (New in the
// library's line), in one style, so a second copy can't drift from it.
const TREND_META = {
  good:    { label: 'Improving', color: 'var(--green)',    icon: '▲' },
  neutral: { label: 'Steady',    color: 'var(--text-dim)', icon: '—' },
  bad:     { label: 'Worsening', color: 'var(--red)',      icon: '▼' },
}

export function TrendPill({ trend, label, icon }) {
  if (!trend) return null
  const m = TREND_META[trend.sentiment]
  return (
    <div title={trend.detail} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, alignSelf: 'flex-start', fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', fontWeight: 600, color: m.color, lineHeight: 1.35 }}>
      <span style={{ fontSize: 'var(--font-sm)', lineHeight: 1, flexShrink: 0 }}>{icon || m.icon}</span>
      {label || m.label}
    </div>
  )
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
// Accepts a full ISO date (YYYY-MM-DD) and renders e.g. "Jun 12"; passes
// through anything that isn't a date so it's safe as a tooltip formatter too.
export const fmtChartDate = v => {
  if (typeof v !== 'string') return v
  const p = v.slice(0, 10).split('-')
  return p.length === 3 ? `${MONTHS[+p[1] - 1]} ${+p[2]}` : v
}

// Split a formatted metric value ("15.8 GB", "82%", "0 B") into its numeric
// lead and trailing unit, so the unit can render small + dim per the design
// system (value leads near-white; unit is a quiet caption).
export function splitValueUnit(s) {
  const str = String(s ?? '')
  if (str.endsWith('%')) return { num: str.slice(0, -1), unit: '%' }
  const sp = str.lastIndexOf(' ')
  if (sp !== -1) return { num: str.slice(0, sp), unit: str.slice(sp + 1) }
  return { num: str, unit: '' }
}

// A dashboard hero stat: big near-white numeral + small, dim unit caption
// (×, %, GB…). Weight 500, tabular figures, tight tracking — the "Instrument"
// number treatment, shared across every panel so units read consistently.
// A display figure, which the type scale deliberately stops short of: the two
// sizes below are named exemptions in backend_tests/test_type_scale.py.
export function HeroNumber({ value, unit, size = 33 }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 3, whiteSpace: 'nowrap' }}>
      <span style={{ fontFamily: 'var(--mono)', fontSize: size, fontWeight: 500, color: 'var(--text)', lineHeight: 1, letterSpacing: '-0.5px', fontVariantNumeric: 'tabular-nums' }}>{value}</span>
      {unit && <span style={{ fontFamily: 'var(--mono)', fontSize: Math.round(size * 0.55), fontWeight: 500, color: 'var(--text-dim)' }}>{unit}</span>}
    </span>
  )
}

// A formatted value ("15.3 TB") as a HeroNumber.
export function Hero({ text, size }) {
  const { num, unit } = splitValueUnit(text)
  return <HeroNumber value={num} unit={unit} size={size} />
}
