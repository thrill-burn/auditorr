import React, { useState, useEffect, useRef, useMemo } from 'react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { formatBytes, scoreColor, tint } from '../utils'
import ChangesPanel from './ChangesPanel'
import { RangePresets } from './FilterBar'
import { api } from '../api'
import { useToast } from './Toast'
import { Button } from './workflows/shared'
import { SECTION_LABEL, fmtChartDate, splitValueUnit, HeroNumber, TrendPill } from './dashKit'
import LibraryMap, { BiggestGapsCard, useLibraryMapRow } from './LibraryMap'
import { modelFromRow } from './libraryMapRules'
import { LastScanCard, NewInLibraryCard, scanAlert } from './ScanCards'

// ── Skeleton ──────────────────────────────────────────────────────────────────
function Skeleton({ w = '100%', h = 16, style = {} }) {
  return <div className="skeleton" style={{ width: w, height: h, borderRadius: 5, ...style }} />
}
function DashboardSkeleton() {
  return (
    <div className="page-frame" style={{ padding: '24px 32px 64px', display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, boxShadow: 'var(--elev-1)', padding: 28, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14 }}>
          <Skeleton w={90} h={10} />
          <Skeleton w={180} h={180} style={{ borderRadius: '50%' }} />
          <Skeleton w={80} h={22} style={{ borderRadius: 99 }} />
        </div>
        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, boxShadow: 'var(--elev-1)', padding: 28 }}>
          <Skeleton w={130} h={10} style={{ marginBottom: 20 }} />
          <Skeleton w="100%" h={160} />
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 16 }}>
        {[0,1,2,3].map(i => (
          <div key={i} style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, boxShadow: 'var(--elev-1)', padding: 22, display: 'flex', flexDirection: 'column', gap: 12 }}>
            <Skeleton w={80} h={9} /><Skeleton w={60} h={36} /><Skeleton w="100%" h={9} /><Skeleton w="100%" h={48} style={{ marginTop: 'auto' }} />
          </div>
        ))}
      </div>
    </div>
  )
}

// ── SVG Arc Dial ──────────────────────────────────────────────────────────────
// Full-spectrum health palette: red (poor) → orange → yellow → green → cyan
// (perfect), interpolated in HSL. The hue progression is intentionally nonlinear
// — the warm bottom half holds (red→orange across 0–50%), then ramps faster
// through yellow→green→cyan across the top half so the "good" range carries more
// visual variety. The endpoints (red ↔ cyan) stay distinguishable for
// deuteranopia, and the brightness ramp adds a non-hue cue. Stops align with the
// scoreColor() thresholds: poor <50, fair 50-74, good 75-89, great 90+.
// Each stop is [t, hue, sat%, light%].
const DIAL_COLOR_STOPS = [
  [0.00,   8, 88, 47],   // red (Poor) — dim
  [0.50,  52, 95, 56],   // yellow (Fair begins)
  [0.75, 135, 58, 50],   // green (Good begins)
  [0.90, 188, 70, 57],   // cyan (Great begins)
  [1.00, 188, 70, 57],   // cyan (perfect) — bright
]
const DIAL_ZONES = [
  { from: 0,    to: 0.5,  color: 'hsl(8, 88%, 47%)'   },   // Poor
  { from: 0.5,  to: 0.75, color: 'hsl(52, 95%, 56%)'  },   // Fair
  { from: 0.75, to: 0.9,  color: 'hsl(135, 58%, 50%)' },   // Good
  { from: 0.9,  to: 1,    color: 'hsl(188, 70%, 57%)' },   // Great
]

function hslToHex(h, s, l) {
  s /= 100; l /= 100
  const k = n => (n + h / 30) % 12
  const a = s * Math.min(l, 1 - l)
  const f = n => {
    const c = l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1))
    return Math.round(255 * c).toString(16).padStart(2, '0')
  }
  return `#${f(0)}${f(8)}${f(4)}`
}

// Interpolates hue, sat AND light so the endpoints can be deep/rich rather than
// two equally-bright mid-tones. Returns hex (not hsl) so a caller can read the
// colour itself; tint it with `tint(color, pct)` like any other, never by
// gluing on a hex alpha — `scoreColor` returns a var(), where that is dropped.
function dialColor(t) {
  const stops = DIAL_COLOR_STOPS
  const last = stops[stops.length - 1]
  if (t <= stops[0][0]) return hslToHex(stops[0][1], stops[0][2], stops[0][3])
  if (t >= last[0])     return hslToHex(last[1], last[2], last[3])
  for (let i = 1; i < stops.length; i++) {
    const [t1, h1, s1, l1] = stops[i - 1]
    const [t2, h2, s2, l2] = stops[i]
    if (t <= t2) {
      const f = (t - t1) / (t2 - t1)
      return hslToHex(h1 + (h2 - h1) * f, s1 + (s2 - s1) * f, l1 + (l2 - l1) * f)
    }
  }
  return hslToHex(last[1], last[2], last[3])
}

function HealthDial({ score, status, smartTrend, color }) {
  const SIZE = 244
  const CX = SIZE / 2, CY = SIZE / 2
  const R_OUTER = 100, R_INNER = 70
  const R_MID = (R_OUTER + R_INNER) / 2
  const CAP_R = (R_OUTER - R_INNER) / 2
  const GAP_DEG = 56
  const START_DEG = 180 + GAP_DEG / 2   // gap centered at the bottom
  const SWEEP_DEG = 360 - GAP_DEG

  function polarToXY(cx, cy, r, angleDeg) {
    const rad = (angleDeg - 90) * Math.PI / 180
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) }
  }
  function arcPath(cx, cy, rO, rI, s, e) {
    const s1 = polarToXY(cx, cy, rO, s), e1 = polarToXY(cx, cy, rO, e)
    const s2 = polarToXY(cx, cy, rI, e), e2 = polarToXY(cx, cy, rI, s)
    const large = (e - s) > 180 ? 1 : 0
    return `M ${s1.x} ${s1.y} A ${rO} ${rO} 0 ${large} 1 ${e1.x} ${e1.y} L ${s2.x} ${s2.y} A ${rI} ${rI} 0 ${large} 0 ${e2.x} ${e2.y} Z`
  }
  // Centerline arc (for the stroked track — gives free rounded ends).
  function arcLine(cx, cy, r, s, e) {
    const p1 = polarToXY(cx, cy, r, s), p2 = polarToXY(cx, cy, r, e)
    const large = (e - s) > 180 ? 1 : 0
    return `M ${p1.x} ${p1.y} A ${r} ${r} 0 ${large} 1 ${p2.x} ${p2.y}`
  }

  const target = Math.min(Math.max(score, 0), 100) / 100

  // Animated fill: ease toward the score on mount and whenever it changes,
  // so the gradient is visible as a sweep rather than a static rainbow.
  const animRef = useRef(0)
  const [animPct, setAnimPct] = useState(0)
  useEffect(() => {
    const from = animRef.current
    const dur = 900
    const t0 = performance.now()
    let raf
    const step = (now) => {
      const t = Math.min(1, (now - t0) / dur)
      const eased = 1 - Math.pow(1 - t, 3)
      const v = from + (target - from) * eased
      animRef.current = v
      setAnimPct(v)
      if (t < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target])

  // Number counts up in lockstep with the arc sweep.
  const displayScore = Math.round(animPct * 100)
  const [showThresholdLabels, setShowThresholdLabels] = useState(false)

  // Zone bands on the unfilled track, with quiet threshold labels on hover.
  const zonePaths = DIAL_ZONES.map(z => ({
    path: arcPath(CX, CY, R_OUTER, R_INNER,
      START_DEG + SWEEP_DEG * z.from, START_DEG + SWEEP_DEG * z.to),
    color: z.color,
  }))
  const ticks = [50, 75, 90].map(v => {
    const deg = START_DEG + SWEEP_DEG * (v / 100)
    return {
      value: v,
      label: polarToXY(CX, CY, R_OUTER + 15, deg),
    }
  })

  const delta = smartTrend?.delta
  const trendLabel = smartTrend?.label
  const up = delta != null && delta >= 0

  // Thin segments sweeping a single status color up to the animated fill
  // position. Color stays a state marker instead of becoming decoration.
  const SEGMENTS = 120
  const OVERLAP  = 0.6 / SEGMENTS
  const arcSegments = []
  for (let i = 0; i < SEGMENTS; i++) {
    const t0 = i / SEGMENTS
    if (t0 >= animPct) break
    const t1 = Math.min((i + 1) / SEGMENTS + OVERLAP, animPct)
    const segStart = START_DEG + SWEEP_DEG * t0
    const segEnd   = START_DEG + SWEEP_DEG * t1
    arcSegments.push({
      path:  arcPath(CX, CY, R_OUTER, R_INNER, segStart, segEnd),
      color: color || scoreColor(score),
    })
  }
  const tipColor = color || scoreColor(score)
  const startColor = tipColor
  // Rounded end caps: a circle the width of the ring rounds each butt end of
  // the wedge fill. The tip cap also carries the (soft, contained) glow.
  const fillEndDeg = START_DEG + SWEEP_DEG * animPct
  const startCap = polarToXY(CX, CY, R_MID, START_DEG)
  const tipCap = polarToXY(CX, CY, R_MID, fillEndDeg)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
      <div
        onMouseEnter={() => setShowThresholdLabels(true)}
        onMouseLeave={() => setShowThresholdLabels(false)}
        style={{ position: 'relative', width: SIZE, height: SIZE }}
      >
        <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} style={{ position: 'relative' }}>
          {/* Recessed track with rounded ends */}
          <path d={arcLine(CX, CY, R_MID, START_DEG, START_DEG + SWEEP_DEG)}
            fill="none" stroke="var(--surface3)" strokeWidth={R_OUTER - R_INNER} strokeLinecap="round" />
          {/* Faint zone bands — where Poor, Fair, Good, and Great live */}
          {zonePaths.map((z, i) => (
            <path key={i} d={z.path} fill="var(--surface2)" fillOpacity="0.55" />
          ))}
          {/* Color-swept filled segments */}
          {arcSegments.map((seg, i) => (
            <path key={i} d={seg.path} fill={seg.color} />
          ))}
          {/* Rounded start cap */}
          {animPct > 0.01 && <circle cx={startCap.x} cy={startCap.y} r={CAP_R} fill={startColor} />}
          {/* Rounded tip cap with contained glow */}
          {animPct > 0.01 && (
            <circle cx={tipCap.x} cy={tipCap.y} r={CAP_R} fill={tipColor}
              style={{ filter: `drop-shadow(0 0 5px ${tipColor})` }} />
          )}
          {/* Threshold labels */}
          {showThresholdLabels && ticks.map((t, i) => (
            <g key={i} style={{ transition: 'opacity 0.12s ease' }}>
              <text x={t.label.x} y={t.label.y + 3} textAnchor="middle"
                fontFamily="var(--mono)" style={{ fontSize: 'var(--font-xs)' }} fill="var(--text-dim)" opacity="0.48">{t.value}</text>
            </g>
          ))}
        </svg>
        {/* Center readout — HTML overlay for crisp text + status chip */}
        <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 42, fontWeight: 500, color: 'var(--text)', lineHeight: 1, letterSpacing: '-0.5px', fontVariantNumeric: 'tabular-nums' }}>{displayScore}</span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginTop: 2 }}>/ 100</span>
          <span style={{ marginTop: 6, fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', fontWeight: 600, color: 'var(--text-dim)', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <span style={{ width: 7, height: 7, borderRadius: 99, background: tipColor }} />
            {status}
          </span>
        </div>
      </div>
      {delta != null && (
        <div style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', fontWeight: 600,
          color: 'var(--text-dim)',
          lineHeight: 1.35,
        }}>
          <span style={{ width: 6, height: 6, borderRadius: 99, background: up ? 'var(--green)' : 'var(--red)' }} />
          {up ? '↑' : '↓'} {Math.abs(delta)} pts {trendLabel}
        </div>
      )}
    </div>
  )
}
// ── Grafana tooltip ───────────────────────────────────────────────────────────
function GrafanaTooltip({ active, payload, label, color }) {
  if (!active || !payload?.length) return null
  return (
    <div style={{ background: '#151515', border: '1px solid #2a2a2a', borderRadius: 6, padding: '10px 14px', boxShadow: '0 8px 24px rgba(0,0,0,0.5)', minWidth: 130 }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginBottom: 6 }}>{fmtChartDate(label)}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
        <div style={{ width: 8, height: 8, borderRadius: 2, background: color, flexShrink: 0 }} />
        <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-md)', fontWeight: 700, color: '#ebebeb' }}>{payload[0].value}</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>/ 100</span>
      </div>
    </div>
  )
}

// ── Trend ─────────────────────────────────────────────────────────────────────
// The pill is dashKit's. `lowerIsBetter` flips the mapping (orphaned/not-
// imported/dupes shrink = good; hardlinked % grows = good).
function computeTrend(data, lowerIsBetter) {
  if (!data || data.length < 2) return null
  const first = data[0], last = data[data.length - 1]
  const delta = last - first
  const base = Math.max(Math.abs(first), Math.abs(last), 1)
  if (Math.abs(delta / base) < 0.02) return { sentiment: 'neutral', delta }
  const improving = lowerIsBetter ? delta < 0 : delta > 0
  return { sentiment: improving ? 'good' : 'bad', delta }
}

function makeTrend(data, lowerIsBetter, unit) {
  const t = computeTrend(data, lowerIsBetter)
  if (!t) return null
  const days = data.length
  const mag = unit === 'pct'
    ? Math.abs(t.delta).toFixed(1) + ' pts'
    : formatBytes(Math.abs(t.delta))
  const sign = t.delta < 0 ? '−' : '+'
  t.detail = t.sentiment === 'neutral'
    ? `Roughly flat over the last ${days} days`
    : `${sign}${mag} over the last ${days} days`
  return t
}

// ── Metric card ───────────────────────────────────────────────────────────────
// Leading glyphs for the primary workflow buttons (keyed by destination tab).
const WORKFLOW_ICONS = {
  backfill: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
    </svg>
  ),
  cleanup: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
    </svg>
  ),
  triage: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
    </svg>
  ),
  dedupe: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
    </svg>
  ),
}

function MetricCard({ label, value, sub, pts, desc, color, trend, actionRows, onNavigate, onScript, toast }) {
  const [loadingKeys, setLoadingKeys] = useState({})
  const scoreText = String(pts || '').replace(/\s*pts$/i, '').replace(/\s*\/\s*/g, '/')
  const { num: numPart, unit: unitPart } = splitValueUnit(value)

  const handleAction = async (a) => {
    if (a.type === 'script') {
      onScript(a)
    } else if (a.type === 'api') {
      setLoadingKeys(k => ({ ...k, [a.label]: true }))
      try {
        await a.apiCall()
        if (a.successToast) toast(a.successToast, 'success')
      } catch (e) {
        if (a.errorToast) toast(e.message || 'Request failed', 'error')
      } finally {
        setLoadingKeys(k => ({ ...k, [a.label]: false }))
      }
    } else {
      onNavigate(a)
    }
  }

  const enrichedRows = actionRows.map(row =>
    row.map(a => ({ ...a, loading: !!loadingKeys[a.label] }))
  )

  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, boxShadow: 'var(--elev-1)', padding: '18px 18px 16px', display: 'flex', flexDirection: 'column', position: 'relative', overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8, marginTop: 4 }}>
        <span style={{ display: 'flex', alignItems: 'flex-start', gap: 7, minHeight: 18 }}>
          <span className="ui-status-dot" style={{ background: color, marginTop: 5 }} />
          <span style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-md)', fontWeight: 500, color: 'var(--text)', letterSpacing: 0, textTransform: 'none', lineHeight: 1.25 }}>
            {label}
          </span>
        </span>
        <span style={{
          fontFamily: 'var(--mono)',
          fontSize: 'var(--font-sm)',
          color: 'var(--text-faint)',
          fontVariantNumeric: 'tabular-nums',
          whiteSpace: 'nowrap',
          flexShrink: 0,
          marginTop: 2,
        }}>{scoreText}</span>
      </div>
      <div style={{ marginTop: 14 }}>
        <HeroNumber value={numPart} unit={unitPart} />
      </div>
      <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginTop: 4 }}>{sub}</span>
      <div style={{ minHeight: 21, marginTop: 10, display: 'flex' }}>
        <TrendPill trend={trend} />
      </div>
      <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginTop: 10, lineHeight: 1.6 }}>{desc}</p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 'auto', paddingTop: 14 }}>
        {enrichedRows.map((row, rowIdx) => {
          const visibleActions = row.filter(a => !a.hidden)
          if (visibleActions.length === 0) return <div key={rowIdx} style={{ height: 'var(--control-h-lg)' }} />
          return (
            <div key={rowIdx} style={{ display: 'flex', gap: 6 }}>
              {visibleActions.map((a, i) => {
                // Primary = the lead workflow action: a quiet raised surface
                // with a near-white label; only the small leading icon carries
                // the card's workflow hue (color rationed to a pointed accent,
                // like the status dot — never a fill — per the design system).
                // Secondary = the "just look" view link: quiet until hover, so
                // its hairline is withheld. Both are the kit's Button (UI pass,
                // 2026-09-22), which is what gives them a real :hover.
                const isPrimary = a.variant === 'primary'
                const icon = isPrimary ? WORKFLOW_ICONS[a.tab] : null
                return (
                  <Button
                    key={i}
                    variant={isPrimary ? 'secondary' : 'ghost'}
                    onClick={() => handleAction(a)}
                    disabled={a.loading}
                    style={{
                      flex: 1, justifyContent: 'flex-start', gap: 7,
                      fontWeight: isPrimary ? 600 : 500,
                      ...(isPrimary ? null : { '--btn-border': 'transparent' }),
                    }}
                  >
                    {icon && <span style={{ display: 'inline-flex', flexShrink: 0, color: a.loading ? 'var(--text-dim)' : color }}>{icon}</span>}
                    {a.loading ? (a.loadingLabel || '…') : a.label}
                  </Button>
                )
              })}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Main Dashboard ────────────────────────────────────────────────────────────
// Health, the four workflow cards, then the disk and its links: the library
// map, the last scan and what's new in the library (2026-11-09). The upload
// panels and the tracker leaderboard moved to the Trackers page, and the
// tracker dropdown, which filtered only them, went. Cross-seed effectiveness
// followed them on 2026-11-10, beside the leaderboard it used to sit with, and
// Biggest gaps left the map for a card of its own, left of Last scan.
//
// The 7d/30d/90d/All range lives on the Score history card (2026-11-13) and
// drives that chart and the dial's "vs Nd ago" only. It used to be a row of its
// own above everything, also driving the four cards' trend pills, New in the
// library and the Trackers page's opening range, which a control on one card
// would change unseen; those now read TREND_DAYS.
const TREND_DAYS = 30

export default function Dashboard({ data, changes, onNavigate, isRefreshing, onScript, timeRange, setTimeRange, onReveal, onScan, mediaFiles, torrentFiles, ensureFilesLoaded }) {
  const toast = useToast()
  const [uploadStats, setUploadStats] = useState(null)
  // The map's stored row, one fetch for the map and Biggest gaps; a gap
  // hovered there outlines its tile on the map.
  const mapRow = useLibraryMapRow()
  const mapModel = useMemo(() => (mapRow ? modelFromRow(mapRow) : null), [mapRow])
  const [mapFocus, setMapFocus] = useState(null)

  // A fixed window, not the chart's range: the range selector sits on the
  // Score history card, so it owns only that card and the dial's delta beside it.
  useEffect(() => {
    api.uploadStats(TREND_DAYS).then(d => {
      if (!d.status) setUploadStats(d)
    }).catch(() => {})
  }, [])

  if (!data) return <DashboardSkeleton />

  const { score, status, trend, current, history_chart } = data
  const det = current.details
  const hlPct = det.total_media_size > 0
    ? Math.round((det.hardlinked_media_size / det.total_media_size) * 100) : 100
  const c = scoreColor(score)

  // Per-day series from upload snapshots — feeds the card trend pills.
  // Orphaned/Not Imported sum the per-tracker stats; Hardlinked %/Duplicates
  // come from the snapshot's library-wide '_library' block.
  const trendSeries = (() => {
    const series = {}
    const days = uploadStats?.daily_tracker_stats
    if (days && days.length >= 2) {
      const orphaned = [], notImported = []
      for (const day of days) {
        let o = 0, n = 0
        for (const s of Object.values(day.by_tracker || {})) {
          o += s.orphaned_size || 0
          n += s.not_imported_size || 0
        }
        orphaned.push(o)
        notImported.push(n)
      }
      if (orphaned.some(v => v > 0))    series.orphaned    = orphaned
      if (notImported.some(v => v > 0)) series.notImported = notImported
    }
    const libDays = uploadStats?.daily_library_stats
    if (libDays && libDays.length >= 2) {
      series.hardlinked = libDays.map(d =>
        d.total_media_size > 0 ? (d.hardlinked_media_size / d.total_media_size) * 100 : 100)
      const dupes = libDays.map(d => d.duplicate_size || 0)
      if (dupes.some(v => v > 0)) series.duplicates = dupes
    }
    return series
  })()

  // Point denominators come from the audit, which resolves them from the
  // configurable score weights. Older snapshots predate those fields, so fall
  // back to the original fixed allocation. A category weighted to zero is
  // reported as unscored rather than as a zero-out-of-zero failure.
  const ptsLabel = (score, max, fallback) => {
    const m = max ?? fallback
    return m > 0 ? `${score} / ${+m.toFixed(1)} pts` : 'Not scored'
  }

  const metrics = [
    {
      label: 'Hardlinked Media', value: hlPct + '%',
      sub: `${formatBytes(det.hardlinked_media_size)} of ${formatBytes(det.total_media_size)}`,
      pts: ptsLabel(det.hl_score, det.hl_max, 70),
      desc: 'Percentage of your media library hardlinked back to a torrent file.',
      color: 'var(--blue)',
      trend: makeTrend(trendSeries.hardlinked, false, 'pct'),
      actionRows: [
        [{ type: 'navigate', label: 'Backfill', tab: 'backfill', variant: 'primary' }],
        [{ type: 'navigate', label: 'View orphaned media', tab: 'media', status: 'Orphaned' }],
      ],
    },
    {
      label: 'Orphaned Torrents', value: formatBytes(det.orphaned_torrent_size),
      sub: `${det.orphaned_torrent_count} file${det.orphaned_torrent_count !== 1 ? 's' : ''} · threshold ${formatBytes(det.or_limit)}`,
      pts: ptsLabel(det.or_score, det.or_max, 10),
      desc: 'Files in your torrent folder that qBittorrent has no knowledge of.',
      color: 'var(--yellow)',
      trend: makeTrend(trendSeries.orphaned, true, 'bytes'),
      actionRows: [
        [{ type: 'navigate', label: 'Cleanup', tab: 'cleanup', variant: 'primary' }],
        [{ type: 'navigate', label: 'View orphaned torrents', tab: 'torrents', status: 'Orphaned' }],
      ],
    },
    {
      label: 'Not Imported', value: formatBytes(det.not_imported_size),
      sub: `${det.not_imported_count} file${det.not_imported_count !== 1 ? 's' : ''} · threshold ${formatBytes(det.ni_limit)}`,
      pts: ptsLabel(det.ni_score, det.ni_max, 10),
      desc: 'Seeding torrents with no matching file in your media folder.',
      color: 'var(--red)',
      trend: makeTrend(trendSeries.notImported, true, 'bytes'),
      actionRows: [
        [{ type: 'navigate', label: 'Triage', tab: 'triage', variant: 'primary' }],
        [{ type: 'navigate', label: 'View not imported', tab: 'torrents', importFilter: 'notImported' }],
      ],
    },
    {
      label: 'Duplicate Files', value: formatBytes(det.duplicate_size),
      sub: `${det.duplicate_count} file${det.duplicate_count !== 1 ? 's' : ''} · threshold ${formatBytes(det.dup_limit)}`,
      pts: ptsLabel(det.dup_score, det.dup_max, 10),
      desc: 'Bit-for-bit identical files that share no inode.',
      color: 'var(--purple)',
      trend: makeTrend(trendSeries.duplicates, true, 'bytes'),
      actionRows: [
        [{ type: 'navigate', label: 'Dedupe', tab: 'dedupe', variant: 'primary' }],
        [{ type: 'navigate', label: 'Media dupes', tab: 'media', status: 'Duplicate' },
         { type: 'navigate', label: 'Torrent dupes', tab: 'torrents', status: 'Duplicate' }],
      ],
    },
  ]

  const filteredHistory = (() => {
    if (!history_chart) return []
    if (timeRange === 0) return history_chart
    const cutoff = new Date()
    cutoff.setDate(cutoff.getDate() - timeRange)
    return history_chart.filter(d => new Date(d.date) >= cutoff)
  })()

  const scores = filteredHistory.map(d => d.avg_score).filter(Boolean)
  // Snap the y-axis to a round grid so ticks land on even multiples instead of
  // arbitrary rounded fractions (which produced uneven gaps like 75/79/83/86/90).
  const rawMin = scores.length ? Math.min(...scores) : 0
  const rawMax = scores.length ? Math.max(...scores) : 100
  const yStep = (rawMax - rawMin) <= 15 ? 5 : (rawMax - rawMin) <= 40 ? 10 : 20
  const minScore = Math.max(0, Math.floor((rawMin - yStep * 0.5) / yStep) * yStep)
  const maxScore = Math.min(100, Math.ceil((rawMax + yStep * 0.5) / yStep) * yStep)
  const yTicks = []
  for (let v = minScore; v <= maxScore + 0.001; v += yStep) yTicks.push(v)

  // Smart trend: delta vs timeRange days ago (fallback to oldest entry)
  const smartTrend = (() => {
    if (!history_chart || history_chart.length < 2) return null
    const today = history_chart[history_chart.length - 1]

    let best
    if (timeRange === 0) {
      best = history_chart[0]
    } else {
      const todayDate = new Date(today.date)
      const targetDate = new Date(todayDate)
      targetDate.setDate(targetDate.getDate() - timeRange)
      best = null
      let bestDiff = Infinity
      for (const entry of history_chart) {
        const d = Math.abs(new Date(entry.date) - targetDate)
        if (d < bestDiff) { bestDiff = d; best = entry }
      }
    }

    if (!best || best.date === today.date) return null

    const delta = Math.round((today.avg_score - best.avg_score) * 10) / 10
    const actualDays = Math.round(Math.abs(new Date(today.date) - new Date(best.date)) / (1000 * 60 * 60 * 24))
    return { delta, label: `vs ${actualDays}d ago` }
  })()

  const scanReport = data.scan_report
  const lastScanAlert = scanAlert(scanReport, current.timestamp)
  const toLastScan = () => document.getElementById('last-scan-card')?.scrollIntoView({ behavior: 'smooth', block: 'center' })

  return (
    <div className="fade-in page-frame" style={{ padding: '24px 32px 64px', display: 'flex', flexDirection: 'column', gap: 16 }}>

      {/* Changes since last scan */}
      {changes?.changes && (
        <ChangesPanel
          changes={changes.changes}
          prevRanAt={changes.prev_ran_at}
          currRanAt={changes.curr_ran_at}
          onNavigate={onNavigate}
          onReveal={onReveal}
        />
      )}

      {/* Alerts: a scan that didn't save comes first, then the thresholds. */}
      {(() => {
        const alerts = []
        const d = det
        if (lastScanAlert) {
          alerts.push({ ...lastScanAlert, action: { label: 'Last scan ↓', onClick: toLastScan } })
        }
        if (d.orphaned_torrent_size > 0 && d.or_limit > 0 && d.orphaned_torrent_size > d.or_limit * 2) {
          alerts.push({ msg: `Orphaned torrent data (${formatBytes(d.orphaned_torrent_size)}) is significantly above your threshold`, color: 'var(--yellow)', action: { label: 'View', tab: 'torrents', status: 'Orphaned' } })
        }
        if (d.not_imported_size > 0 && d.ni_limit > 0 && d.not_imported_size > d.ni_limit * 2) {
          alerts.push({ msg: `Not-imported data (${formatBytes(d.not_imported_size)}) is significantly above your threshold`, color: 'var(--red)', action: { label: 'View', tab: 'torrents', importFilter: 'notImported' } })
        }
        if (d.duplicate_size > 0 && d.dup_limit > 0 && d.duplicate_size > d.dup_limit * 2) {
          alerts.push({ msg: `Duplicate data (${formatBytes(d.duplicate_size)}) is significantly above your threshold`, color: 'var(--purple)', action: { label: 'View', tab: 'media', status: 'Duplicate' } })
        }
        if (!alerts.length) return null
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {alerts.map((a, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 14px', borderRadius: 8, background: tint(a.color, 6), border: `1px solid ${tint(a.color, 19)}` }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }} aria-hidden="true">
                    <path d="M8 2l7 12H1L8 2z" stroke={a.color} strokeWidth="1.5" strokeLinejoin="round" />
                    <path d="M8 6.5v3" stroke={a.color} strokeWidth="1.5" strokeLinecap="round" />
                    <circle cx="8" cy="11.6" r="0.9" fill={a.color} />
                  </svg>
                  <span style={{ fontSize: 'var(--font-base)', color: 'var(--text)' }}>{a.msg}</span>
                </div>
                <Button size="sm" tone={a.color} onClick={() => (a.action.onClick ? a.action.onClick() : onNavigate(a.action))} style={{ marginLeft: 12 }}>
                  {a.action.label}{a.action.onClick ? '' : ' →'}
                </Button>
              </div>
            ))}
          </div>
        )
      })()}

      {/* Row 1: dial + chart */}
      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, boxShadow: 'var(--elev-1)', padding: '20px 20px 16px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
          {/* As tall as the range control beside Score history's label, so the
              two cards' labels sit on one line. */}
          <span style={{ ...SECTION_LABEL, width: '100%', display: 'flex', alignItems: 'center', minHeight: 'var(--control-h)' }}>Library health</span>
          <HealthDial score={score} status={status} smartTrend={smartTrend} color={c} />
        </div>

        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, boxShadow: 'var(--elev-1)', padding: '20px 20px 14px', display: 'flex', flexDirection: 'column' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px 12px', minHeight: 'var(--control-h)', marginBottom: 12 }}>
            <span style={SECTION_LABEL}>Score history</span>
            <RangePresets size="sm" isActive={v => timeRange === v} onSelect={setTimeRange} />
          </div>
          <div style={{ flex: 1, minHeight: 0 }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={filteredHistory} margin={{ top: 4, right: 20, left: 4, bottom: 0 }}>
              <defs>
                <linearGradient id="grafanaGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={c} stopOpacity={0.25} />
                  <stop offset="100%" stopColor={c} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="2 4" stroke="var(--border)" strokeOpacity={0.6} vertical={false} />
              <XAxis dataKey="date" tick={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} minTickGap={36} tickFormatter={fmtChartDate} />
              <YAxis domain={[minScore, maxScore]} ticks={yTicks} allowDecimals={false} width={30} tick={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} />
              <Tooltip content={<GrafanaTooltip color={c} />} cursor={{ stroke: tint(c, 25), strokeWidth: 1, strokeDasharray: '3 3' }} />
              <Area type="linear" dataKey="avg_score" stroke={c} strokeWidth={1.5} fill="url(#grafanaGrad)" dot={false} activeDot={{ r: 4, fill: c, stroke: 'var(--bg)', strokeWidth: 2 }} />
            </AreaChart>
          </ResponsiveContainer>
          </div>
        </div>
      </div>

      {/* Row 2: metric cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 16 }}>
        {metrics.map(m => <MetricCard key={m.label} {...m} onNavigate={onNavigate} onScript={onScript} toast={toast} />)}
      </div>

      {/* Row 3: the library map, the page's width */}
      <LibraryMap details={det} onNavigate={onNavigate} row={mapRow} rowModel={mapModel} focus={mapFocus}
        mediaFiles={mediaFiles} torrentFiles={torrentFiles} ensureFilesLoaded={ensureFilesLoaded} />

      {/* Row 4: the biggest gaps, the last scan, and what's new in the library */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 16 }}>
        <BiggestGapsCard row={mapRow} model={mapModel} onNavigate={onNavigate} onHighlight={setMapFocus} />
        <LastScanCard id="last-scan-card" scanReport={scanReport} details={det} savedAt={current.timestamp} onScan={onScan} />
        <NewInLibraryCard uploadStats={uploadStats} timeRange={TREND_DAYS} onNavigate={onNavigate} />
      </div>
    </div>
  )
}
