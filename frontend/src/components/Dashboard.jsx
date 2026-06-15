import React, { useState, useEffect, useRef } from 'react'
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { formatBytes, formatBytesCompact, scoreColor } from '../utils'
import ChangesPanel from './ChangesPanel'
import FilterBar from './FilterBar'
import { api } from '../api'
import { useToast } from './Toast'

// Shared section-header label style — keeps every dashboard card title uniform.
const SECTION_LABEL = { fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase' }

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
// Accepts a full ISO date (YYYY-MM-DD) and renders e.g. "Jun 12"; passes
// through anything that isn't a date so it's safe as a tooltip formatter too.
const fmtChartDate = v => {
  if (typeof v !== 'string') return v
  const p = v.split('-')
  return p.length === 3 ? `${MONTHS[+p[1] - 1]} ${+p[2]}` : v
}

// ── Skeleton ──────────────────────────────────────────────────────────────────
function Skeleton({ w = '100%', h = 16, style = {} }) {
  return <div className="skeleton" style={{ width: w, height: h, borderRadius: 5, ...style }} />
}
function DashboardSkeleton() {
  return (
    <div style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: 28, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14 }}>
          <Skeleton w={90} h={10} />
          <Skeleton w={180} h={180} style={{ borderRadius: '50%' }} />
          <Skeleton w={80} h={22} style={{ borderRadius: 99 }} />
        </div>
        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: 28 }}>
          <Skeleton w={130} h={10} style={{ marginBottom: 20 }} />
          <Skeleton w="100%" h={160} />
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 16 }}>
        {[0,1,2,3].map(i => (
          <div key={i} style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: 22, display: 'flex', flexDirection: 'column', gap: 12 }}>
            <Skeleton w={80} h={9} /><Skeleton w={60} h={36} /><Skeleton w="100%" h={9} /><Skeleton w="100%" h={48} style={{ marginTop: 'auto' }} />
          </div>
        ))}
      </div>
    </div>
  )
}

// ── SVG Arc Dial ──────────────────────────────────────────────────────────────
// Gradient hue stops aligned with the scoreColor() status thresholds:
// red below 75, yellow 75–89, green 90+. The arc tip color therefore always
// agrees with the status text and the score number.
const DIAL_HUE_STOPS = [[0, 0], [0.75, 55], [0.9, 100], [1, 125]]
const DIAL_ZONES = [
  { from: 0,    to: 0.75, color: 'hsl(0, 80%, 50%)'   },   // Poor/Fair
  { from: 0.75, to: 0.9,  color: 'hsl(55, 85%, 50%)'  },   // Good
  { from: 0.9,  to: 1,    color: 'hsl(115, 70%, 45%)' },   // Excellent
]

function dialHue(t) {
  for (let i = 1; i < DIAL_HUE_STOPS.length; i++) {
    const [t1, h1] = DIAL_HUE_STOPS[i - 1]
    const [t2, h2] = DIAL_HUE_STOPS[i]
    if (t <= t2) return h1 + (h2 - h1) * ((t - t1) / (t2 - t1))
  }
  return DIAL_HUE_STOPS[DIAL_HUE_STOPS.length - 1][1]
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

  // Zone bands on the unfilled track, with labeled ticks at the thresholds
  const zonePaths = DIAL_ZONES.map(z => ({
    path: arcPath(CX, CY, R_OUTER, R_INNER,
      START_DEG + SWEEP_DEG * z.from, START_DEG + SWEEP_DEG * z.to),
    color: z.color,
  }))
  const ticks = [75, 90].map(v => {
    const deg = START_DEG + SWEEP_DEG * (v / 100)
    return {
      value: v,
      inner: polarToXY(CX, CY, R_INNER - 4, deg),
      outer: polarToXY(CX, CY, R_OUTER + 4, deg),
      label: polarToXY(CX, CY, R_OUTER + 13, deg),
    }
  })
  // Scale anchors at the open ends of the dial.
  const ends = [0, 100].map(v => ({
    value: v,
    label: polarToXY(CX, CY, R_OUTER + 13, START_DEG + SWEEP_DEG * (v / 100)),
  }))

  const delta = smartTrend?.delta
  const trendLabel = smartTrend?.label
  const up = delta != null && delta >= 0

  // Thin segments sweeping the threshold-aligned gradient up to the
  // (animated) fill position — a conic gradient the dial actually earns.
  // Each segment overlaps into the next so antialiased edges blend against
  // same-colored paint instead of the track (butted edges leave a faint
  // radial seam at every junction — the "grainy" look).
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
      color: `hsl(${dialHue(Math.min((t0 + t1) / 2, animPct))}, 90%, 52%)`,
    })
  }
  const tipColor = `hsl(${dialHue(animPct)}, 90%, 52%)`
  const startColor = `hsl(${dialHue(0)}, 90%, 52%)`
  // Rounded end caps: a circle the width of the ring rounds each butt end of
  // the wedge fill. The tip cap also carries the (soft, contained) glow.
  const fillEndDeg = START_DEG + SWEEP_DEG * animPct
  const startCap = polarToXY(CX, CY, R_MID, START_DEG)
  const tipCap = polarToXY(CX, CY, R_MID, fillEndDeg)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
      <div style={{ position: 'relative', width: SIZE, height: SIZE }}>
        <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} style={{ position: 'relative' }}>
          {/* Recessed track with rounded ends */}
          <path d={arcLine(CX, CY, R_MID, START_DEG, START_DEG + SWEEP_DEG)}
            fill="none" stroke="var(--surface3)" strokeWidth={R_OUTER - R_INNER} strokeLinecap="round" />
          {/* Faint zone bands — where Poor/Fair, Good, and Excellent live */}
          {zonePaths.map((z, i) => (
            <path key={i} d={z.path} fill={z.color} fillOpacity="0.10" />
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
          {/* Threshold ticks + labels */}
          {ticks.map((t, i) => (
            <g key={i}>
              <line x1={t.inner.x} y1={t.inner.y} x2={t.outer.x} y2={t.outer.y}
                stroke="var(--bg)" strokeWidth="2.5" />
              <text x={t.label.x} y={t.label.y + 3} textAnchor="middle"
                fontFamily="var(--mono)" fontSize="10" fill="var(--text-dim)">{t.value}</text>
            </g>
          ))}
          {/* Scale anchors at the open ends */}
          {ends.map((e, i) => (
            <text key={i} x={e.label.x} y={e.label.y + 3} textAnchor="middle"
              fontFamily="var(--mono)" fontSize="9" fill="var(--text-dim)" opacity="0.65">{e.value}</text>
          ))}
        </svg>
        {/* Center readout — HTML overlay for crisp text + status chip */}
        <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 42, fontWeight: 700, color, lineHeight: 1 }}>{displayScore}</span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', marginTop: 2 }}>/ 100</span>
          <span style={{ marginTop: 6, fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 700, letterSpacing: 1.5, color, background: color + '1a', border: `1px solid ${color}40`, borderRadius: 99, padding: '2px 10px' }}>{status?.toUpperCase()}</span>
        </div>
      </div>
      {delta != null && (
        <div style={{
          display: 'inline-flex', alignItems: 'center', gap: 4,
          fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 600,
          color: up ? 'var(--green)' : 'var(--red)',
          background: up ? 'var(--green)12' : 'var(--red)12',
          border: `1px solid ${up ? 'var(--green)' : 'var(--red)'}30`,
          borderRadius: 99, padding: '3px 10px',
        }}>
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
      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', marginBottom: 6 }}>{fmtChartDate(label)}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
        <div style={{ width: 8, height: 8, borderRadius: 2, background: color, flexShrink: 0 }} />
        <span style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 700, color: '#ebebeb' }}>{payload[0].value}</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>/ 100</span>
      </div>
    </div>
  )
}

// ── Upload activity tooltip ───────────────────────────────────────────────────
const TRACKER_COLORS = [
  '#38bdf8', '#a78bfa', '#22c55e', '#f59e0b',
  '#ef4444', '#ec4899', '#14b8a6', '#f97316',
]

function UploadActivityTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const items = payload.filter(p => p.value > 0)
  const total = payload.reduce((s, p) => s + (p.value || 0), 0)
  return (
    <div style={{ background: '#151515', border: '1px solid #2a2a2a', borderRadius: 6, padding: '10px 14px', boxShadow: '0 8px 24px rgba(0,0,0,0.5)', minWidth: 160 }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', marginBottom: 6 }}>{fmtChartDate(label)}</div>
      {items.map(p => (
        <div key={p.name} style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 3 }}>
          <div style={{ width: 8, height: 8, borderRadius: 2, background: p.fill, flexShrink: 0 }} />
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: '#ebebeb', flex: 1 }}>{p.name}</span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>{formatBytes(p.value)}</span>
        </div>
      ))}
      {items.length > 1 && (
        <div style={{ marginTop: 6, paddingTop: 6, borderTop: '1px solid #2a2a2a', display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>Total</span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 700, color: '#ebebeb' }}>{formatBytes(total)}</span>
        </div>
      )}
    </div>
  )
}

function RoundedStackedBar({ x, y, width, height, fill, allTrackers, host, payload }) {
  if (!height || height <= 0) return null
  const r = 3
  // Determine if this segment is the topmost non-zero segment for this day
  const idx = allTrackers.indexOf(host)
  const isTop = allTrackers.slice(idx + 1).every(t => !(payload[t] > 0))
  if (isTop) {
    return (
      <path
        d={`M${x},${y + r} a${r},${r} 0 0 1 ${r},-${r} h${width - 2 * r} a${r},${r} 0 0 1 ${r},${r} v${height - r} h-${width} Z`}
        fill={fill}
      />
    )
  }
  return <rect x={x} y={y} width={width} height={height} fill={fill} />
}

// ── Trend pill ────────────────────────────────────────────────────────────────
// Sentiment-based, NOT directional: it reports whether the metric's *health* is
// improving/worsening, decoupled from whether the raw number went up or down.
// `lowerIsBetter` flips the mapping (orphaned/not-imported/dupes shrink = good;
// hardlinked % grows = good).
const TREND_META = {
  good:    { label: 'Improving', color: 'var(--green)' },
  neutral: { label: 'Steady',    color: 'var(--text-dim)' },
  bad:     { label: 'Worsening', color: 'var(--red)' },
}

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

function TrendIcon({ sentiment }) {
  const c = 'currentColor'
  if (sentiment === 'good') return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M3 8.5l3 3 7-7.5" stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
  if (sentiment === 'bad') return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 2l7 12H1L8 2z" stroke={c} strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M8 6.5v3" stroke={c} strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="8" cy="11.6" r="0.9" fill={c} />
    </svg>
  )
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M3 8h10" stroke={c} strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

function TrendPill({ trend }) {
  if (!trend) return null
  const m = TREND_META[trend.sentiment]
  return (
    <div title={trend.detail} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, alignSelf: 'flex-start', fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 600, color: m.color, background: m.color + '14', border: '1px solid ' + m.color + '30', borderRadius: 5, padding: '2px 8px' }}>
      <TrendIcon sentiment={trend.sentiment} />{m.label}
    </div>
  )
}

// ── Metric card ───────────────────────────────────────────────────────────────
function MetricCard({ label, value, sub, pts, desc, color, trend, actionRows, onNavigate, onScript, toast }) {
  const [loadingKeys, setLoadingKeys] = useState({})

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
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: '18px 18px 16px', display: 'flex', flexDirection: 'column', position: 'relative', overflow: 'hidden' }}>
      <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 3, background: color, borderRadius: '12px 12px 0 0' }} />
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8, marginTop: 4 }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 600, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase', lineHeight: 1.35, minHeight: '2.7em', display: 'flex', alignItems: 'flex-start' }}>{label}</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color, background: color + '18', border: '1px solid ' + color + '35', borderRadius: 4, padding: '1px 6px', whiteSpace: 'nowrap' }}>{pts}</span>
      </div>
      <div style={{ marginTop: 10 }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 34, fontWeight: 700, color, lineHeight: 1, whiteSpace: 'nowrap' }}>{value}</span>
      </div>
      <span style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>{sub}</span>
      <div style={{ minHeight: 21, marginTop: 10, display: 'flex' }}>
        <TrendPill trend={trend} />
      </div>
      <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 10, lineHeight: 1.6 }}>{desc}</p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 'auto', paddingTop: 14 }}>
        {enrichedRows.map((row, rowIdx) => {
          const visibleActions = row.filter(a => !a.hidden)
          if (visibleActions.length === 0) return <div key={rowIdx} style={{ height: 31 }} />
          return (
            <div key={rowIdx} style={{ display: 'flex', gap: 6 }}>
              {visibleActions.map((a, i) => (
                <button
                  key={i}
                  onClick={() => handleAction(a)}
                  disabled={a.loading}
                  style={{
                    flex: 1, padding: '7px 10px', borderRadius: 7,
                    border: `1px solid ${color}35`,
                    background: a.loading ? `${color}08` : `${color}12`,
                    color: a.loading ? `${color}88` : color,
                    fontSize: 12, fontWeight: 500, cursor: a.loading ? 'default' : 'pointer',
                    transition: 'background 0.15s',
                  }}
                  onMouseEnter={e => { if (!a.loading) e.currentTarget.style.background = `${color}22` }}
                  onMouseLeave={e => { if (!a.loading) e.currentTarget.style.background = `${color}12` }}
                >
                  {a.loading ? (a.loadingLabel || '…') : a.label}
                </button>
              ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Cross-seed bar ────────────────────────────────────────────────────────────
// SEED_COLORS[0] = orphaned (0 trackers), [1] = 1 tracker, [2] = 2, etc.
const SEED_COLORS = [
  '#ef4444',   // 0x — red (orphaned/dead weight)
  '#777777',   // 1x — grey (baseline)
  '#38bdf8',   // 2x — blue
  '#a78bfa',   // 3x — purple
  '#22c55e',   // 4x — green
  '#f59e0b',   // 5x+
]

function seedColor(n) {
  return SEED_COLORS[Math.min(n, SEED_COLORS.length - 1)]
}

function CrossSeedBar({ segments, totalSize, onNavigate }) {
  const [hovered, setHovered] = useState(null)

  if (!segments || totalSize === 0) return null

  return (
    <div>
      {/* The bar — each segment clickable */}
      <div style={{ display: 'flex', height: 36, borderRadius: 8, overflow: 'hidden', gap: 1 }}>
        {segments.map((seg, i) => {
          if (seg.size === 0) return null
          const pct = (seg.size / totalSize) * 100
          const color = seedColor(seg.count)
          return (
            <div
              key={i}
              title={`Click to filter media by ${seg.count}× seeded`}
              style={{
                flex: `0 0 ${pct}%`,
                background: hovered === i ? color : color + 'cc',
                transition: 'background 0.15s',
                cursor: 'pointer',
                position: 'relative',
              }}
              onMouseEnter={() => setHovered(i)}
              onMouseLeave={() => setHovered(null)}
              onClick={() => onNavigate && onNavigate({ tab: 'media', seedCount: seg.count })}
            />
          )
        })}
      </div>

      {/* Hover info strip — fixed height so legend never shifts */}
      <div style={{ height: 28, marginTop: 8, display: 'flex', alignItems: 'center' }}>
        {hovered !== null && segments[hovered]?.size > 0 && (() => {
          const seg = segments[hovered]
          const color = seedColor(seg.count)
          const pct = ((seg.size / totalSize) * 100).toFixed(1)
          return (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ width: 9, height: 9, borderRadius: 2, background: color, flexShrink: 0 }} />
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>
                {seg.count === 0 ? 'Not Seeded (0×)' : `${seg.count}× seeded`}
              </span>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
                {formatBytes(seg.size)} · {pct}%
              </span>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--accent)' }}>click to filter →</span>
            </div>
          )
        })()}
      </div>

      {/* Legend */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 14px' }}>
        {segments.map((seg, i) => {
          if (seg.size === 0) return null
          const color = seedColor(seg.count)
          const pct = ((seg.size / totalSize) * 100).toFixed(1)
          return (
            <button
              key={i}
              onClick={() => onNavigate && onNavigate({ tab: 'media', seedCount: seg.count })}
              style={{ display: 'flex', alignItems: 'center', gap: 5, background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
            >
              <div style={{ width: 9, height: 9, borderRadius: 2, background: color, flexShrink: 0 }} />
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
                {seg.count === 0 ? '0× (not seeded)' : `${seg.count}×`} — {pct}%
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── Tracker leaderboard ───────────────────────────────────────────────────────
function TrackerLeaderboard({ trackerStats, onTrackerDetail }) {
  if (!trackerStats || trackerStats.length === 0) return null

  const top3 = trackerStats.slice(0, 3)
  const maxSize = top3[0]?.size || 1

  const medals = ['🥇', '🥈', '🥉']
  const colors = ['var(--yellow)', 'var(--text-dim)', '#cd7f32']

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {top3.map((t, i) => {
        const barPct = (t.size / maxSize) * 100
        return (
          <button
            key={t.name}
            onClick={() => onTrackerDetail(t.name)}
            style={{
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '10px 12px', borderRadius: 8,
              background: 'var(--surface2)', border: '1px solid var(--border)',
              cursor: 'pointer', textAlign: 'left', width: '100%',
              transition: 'border-color 0.15s',
            }}
            onMouseEnter={e => e.currentTarget.style.borderColor = colors[i]}
            onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}
          >
            <span style={{ fontSize: 16, flexShrink: 0 }}>{medals[i]}</span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 5 }}>
                <span style={{ fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 600, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '60%' }}>{t.name}</span>
                <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', flexShrink: 0 }}>{t.count} files · {formatBytes(t.size)}</span>
              </div>
              <div style={{ height: 4, background: 'var(--surface3)', borderRadius: 99, overflow: 'hidden' }}>
                <div style={{ width: barPct + '%', height: '100%', background: colors[i], borderRadius: 99 }} />
              </div>
            </div>
          </button>
        )
      })}
    </div>
  )
}

// ── Tracker card (shared between modal and Trackers page) ─────────────────────
// trackerStats = { seeding_count, seeding_size, orphaned_count, orphaned_size,
//                  not_imported_count, not_imported_size } — pre-computed by backend
export function TrackerCard({ trackerName, trackerStats, uploadStats, onNavigate, onClose }) {
  const [chartTab, setChartTab] = useState('upload')

  const seedingSize      = trackerStats?.seeding_size       ?? 0
  const seedingCount     = trackerStats?.seeding_count      ?? 0
  const orphanedSize     = trackerStats?.orphaned_size      ?? 0
  const orphanedCount    = trackerStats?.orphaned_count     ?? 0
  const notImportedSize  = trackerStats?.not_imported_size  ?? 0
  const notImportedCount = trackerStats?.not_imported_count ?? 0

  const yieldData     = uploadStats?.tracker_yields?.find(t => t.tracker === trackerName)
  const yieldPct      = yieldData?.yield != null ? (yieldData.yield * 100).toFixed(2) + '%' : '—'
  const uploadedBytes = yieldData?.uploaded ?? null

  // Merge daily_uploads (delta bytes) + daily_tracker_stats (point-in-time) by date
  const trendData = (() => {
    if (!uploadStats) return []
    const byDate = {}
    for (const day of (uploadStats.daily_uploads || [])) {
      const d = day.date
      if (!byDate[d]) byDate[d] = { date: d }
      byDate[d].uploaded = day.by_tracker?.[trackerName] || 0
    }
    for (const day of (uploadStats.daily_tracker_stats || [])) {
      const s = day.by_tracker?.[trackerName]
      if (!s) continue
      const d = day.date
      if (!byDate[d]) byDate[d] = { date: d }
      byDate[d].seeding_size      = s.seeding_size || 0
      byDate[d].orphaned_size     = s.orphaned_size || 0
      byDate[d].not_imported_size = s.not_imported_size || 0
      const uploaded = byDate[d].uploaded || 0
      const seeding  = s.seeding_size || 0
      byDate[d].yield_pct = seeding > 0 ? (uploaded / seeding) * 100 : null
    }
    return Object.entries(byDate)
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([, v]) => v)
  })()
  const hasTrendData = trendData.some(d => (d.uploaded || 0) > 0 || (d.seeding_size || 0) > 0)
  const gradId = `tug-${trackerName.replace(/[^a-zA-Z0-9]/g, '')}-${chartTab}`

  const CHART_TABS = [
    { key: 'seeding',      label: 'Seeding',      dataKey: 'seeding_size',      color: 'var(--green)',  fmt: formatBytes },
    { key: 'upload',       label: 'Uploaded',     dataKey: 'uploaded',          color: 'var(--blue)',   fmt: formatBytes },
    { key: 'yield',        label: 'Yield',        dataKey: 'yield_pct',         color: 'var(--accent)', fmt: v => v != null ? v.toFixed(3) + '%' : '—' },
    { key: 'orphaned',     label: 'Orphaned',     dataKey: 'orphaned_size',     color: 'var(--yellow)', fmt: formatBytes },
    { key: 'not_imported', label: 'Not Imported', dataKey: 'not_imported_size', color: 'var(--red)',    fmt: formatBytes },
  ]
  const activeTab = CHART_TABS.find(t => t.key === chartTab) || CHART_TABS[0]

  const statBoxes = [
    { label: 'Seeding',      tabKey: 'seeding',      value: formatBytes(seedingSize),                                  sub: `${seedingCount} files`,                                                   color: 'var(--green)'  },
    { label: 'Uploaded',     tabKey: 'upload',        value: uploadedBytes !== null ? formatBytes(uploadedBytes) : '—', sub: uploadStats ? `last ${uploadStats.period_days}d` : 'no data yet',         color: 'var(--blue)'   },
    { label: 'Yield',        tabKey: 'yield',         value: yieldPct,                                                  sub: uploadStats ? `last ${uploadStats.period_days}d` : 'no data yet',         color: 'var(--accent)' },
    { label: 'Orphaned',     tabKey: 'orphaned',      value: formatBytes(orphanedSize),                                 sub: `${orphanedCount} files`,                                                  color: 'var(--yellow)' },
    { label: 'Not Imported', tabKey: 'not_imported',  value: formatBytes(notImportedSize),                              sub: `${notImportedCount} files`,                                               color: 'var(--red)'    },
  ]

  const btnStyle = {
    padding: '9px 14px', borderRadius: 8, border: '1px solid var(--accent)35',
    background: 'var(--accent)12', color: 'var(--accent)',
    fontSize: 13, fontWeight: 500, cursor: 'pointer', textAlign: 'left',
    transition: 'background 0.15s', width: '100%',
  }
  const btnHover = e => e.currentTarget.style.background = 'var(--accent)22'
  const btnLeave = e => e.currentTarget.style.background = 'var(--accent)12'

  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', overflow: 'hidden', display: 'flex', flexDirection: 'column', flex: 1 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '16px 20px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 15, fontWeight: 700, color: 'var(--text)' }}>{trackerName}</span>
        {onClose && (
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-dim)', fontSize: 20, padding: '2px 6px', lineHeight: 1 }}
            onMouseEnter={e => e.currentTarget.style.color = 'var(--text)'}
            onMouseLeave={e => e.currentTarget.style.color = 'var(--text-dim)'}
          >×</button>
        )}
      </div>

      {/* Content */}
      <div style={{ overflowY: 'auto', flex: 1, padding: '20px', display: 'flex', flexDirection: 'column', gap: 20 }}>

        {/* Stats row — click to select chart */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10 }}>
          {statBoxes.map(s => {
            const active = chartTab === s.tabKey
            return (
              <div
                key={s.label}
                onClick={() => setChartTab(s.tabKey)}
                style={{
                  background: active ? s.color + '10' : 'var(--surface2)',
                  border: `1px solid ${active ? s.color : 'var(--border)'}`,
                  borderRadius: 'var(--r)', padding: '10px 14px',
                  cursor: 'pointer', transition: 'border-color 0.12s, background 0.12s',
                }}
              >
                <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: active ? s.color : 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase', marginBottom: 6 }}>{s.label}</div>
                <div style={{ fontFamily: 'var(--mono)', fontSize: 22, fontWeight: 700, color: s.color, lineHeight: 1 }}>{s.value}</div>
                <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>{s.sub}</div>
              </div>
            )
          })}
        </div>

        {/* Trend charts */}
        {uploadStats && (
          hasTrendData ? (
            <div>
              <div style={{ ...SECTION_LABEL, marginBottom: 10 }}>
                {activeTab.label} Trend
              </div>
              <div style={{ height: 160 }}>
                <ResponsiveContainer width="100%" height={160}>
                  <AreaChart data={trendData} margin={{ top: 4, right: 20, left: 4, bottom: 0 }}>
                    <defs>
                      <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={activeTab.color} stopOpacity={0.25} />
                        <stop offset="100%" stopColor={activeTab.color} stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="2 4" stroke="var(--border)" strokeOpacity={0.6} vertical={false} />
                    <XAxis dataKey="date" tick={{ fontFamily: 'var(--mono)', fontSize: 11, fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} minTickGap={36} tickFormatter={fmtChartDate} />
                    <YAxis
                      width={44}
                      tick={{ fontFamily: 'var(--mono)', fontSize: 11, fill: 'var(--text-dim)' }}
                      tickLine={false} axisLine={false}
                      domain={chartTab === 'seeding' ? ['auto', 'auto'] : [0, 'auto']}
                      tickFormatter={chartTab === 'yield'
                        ? v => v != null ? v.toFixed(2) + '%' : ''
                        : formatBytesCompact}
                    />
                    <Tooltip
                      content={({ active, payload, label }) => {
                        if (!active || !payload?.length) return null
                        const val = payload[0].value
                        return (
                          <div style={{ background: '#151515', border: '1px solid #2a2a2a', borderRadius: 6, padding: '10px 14px', boxShadow: '0 8px 24px rgba(0,0,0,0.5)' }}>
                            <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', marginBottom: 4 }}>{fmtChartDate(label)}</div>
                            <div style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 700, color: '#ebebeb' }}>
                              {val != null ? activeTab.fmt(val) : '—'}
                            </div>
                          </div>
                        )
                      }}
                      cursor={{ stroke: activeTab.color, strokeWidth: 1, strokeOpacity: 0.4, strokeDasharray: '3 3' }}
                    />
                    <Area
                      type="linear"
                      dataKey={activeTab.dataKey}
                      stroke={activeTab.color}
                      strokeWidth={1.5}
                      fill={`url(#${gradId})`}
                      dot={false}
                      connectNulls={false}
                      activeDot={{ r: 4, fill: activeTab.color, stroke: 'var(--bg)', strokeWidth: 2 }}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </div>
          ) : (
            <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', textAlign: 'center', padding: '16px 0' }}>
              Upload data will appear after a few audits
            </div>
          )
        )}

        {/* Navigation buttons */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {seedingCount > 0 && (
            <button style={btnStyle} onMouseEnter={btnHover} onMouseLeave={btnLeave}
              onClick={() => onNavigate({ tab: 'torrents', tracker: trackerName, status: 'Seeding' })}>
              View Seeding Files <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>({seedingCount} files · {formatBytes(seedingSize)})</span>
            </button>
          )}
          {orphanedCount > 0 && (
            <button style={btnStyle} onMouseEnter={btnHover} onMouseLeave={btnLeave}
              onClick={() => onNavigate({ tab: 'torrents', tracker: trackerName, status: 'Orphaned' })}>
              View Orphaned Files <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>({orphanedCount} files · {formatBytes(orphanedSize)})</span>
            </button>
          )}
          {notImportedCount > 0 && (
            <button style={btnStyle} onMouseEnter={btnHover} onMouseLeave={btnLeave}
              onClick={() => onNavigate({ tab: 'torrents', tracker: trackerName, importFilter: 'notImported' })}>
              View Not Imported <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>({notImportedCount} files · {formatBytes(notImportedSize)})</span>
            </button>
          )}
        </div>

      </div>
    </div>
  )
}

// ── Tracker detail modal (thin wrapper around TrackerCard) ────────────────────
function TrackerDetailModal({ trackerName, trackerStats, uploadStats, onNavigate, onClose }) {
  return (
    <div
      style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', zIndex: 500, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}
      onClick={onClose}
    >
      <div
        style={{ maxWidth: 860, width: '100%', maxHeight: '85vh', display: 'flex', flexDirection: 'column' }}
        onClick={e => e.stopPropagation()}
      >
        <TrackerCard
          trackerName={trackerName}
          trackerStats={trackerStats}
          uploadStats={uploadStats}
          onNavigate={onNavigate}
          onClose={onClose}
        />
      </div>
    </div>
  )
}

// ── Main Dashboard ────────────────────────────────────────────────────────────
export default function Dashboard({ data, changes, onNavigate, isRefreshing, onScript, timeRange, setTimeRange, selectedTrackers, setSelectedTrackers, allTrackers, onReveal }) {
  const toast = useToast()
  const [uploadStats, setUploadStats] = useState(null)
  const [trackerDetail, setTrackerDetail] = useState(null)
  const [yieldPanelTab, setYieldPanelTab] = useState('upload')

  useEffect(() => {
    api.uploadStats(timeRange).then(d => {
      if (!d.status) setUploadStats(d)
    }).catch(() => {})
  }, [timeRange])

  // Cross-seed stats are pre-computed by the backend audit process
  const cs = data?.cross_seed_stats ?? null

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

  const metrics = [
    {
      label: 'Hardlinked Media', value: hlPct + '%',
      sub: `${formatBytes(det.hardlinked_media_size)} of ${formatBytes(det.total_media_size)}`,
      pts: `${det.hl_score} / 70 pts`,
      desc: 'Percentage of your media library that is hardlinked back to a torrent file. 100% means everything is connected.',
      color: 'var(--blue)',
      trend: makeTrend(trendSeries.hardlinked, false, 'pct'),
      actionRows: [
        [{ type: 'navigate', label: 'View Orphaned Media', tab: 'media', status: 'Orphaned' }],
        [{ type: 'navigate', label: 'Open Backfill Workflow', tab: 'backfill' }],
      ],
    },
    {
      label: 'Orphaned Torrents', value: formatBytes(det.orphaned_torrent_size),
      sub: `${det.orphaned_torrent_count} file${det.orphaned_torrent_count !== 1 ? 's' : ''} · threshold ${formatBytes(det.or_limit)}`,
      pts: `${det.or_score} / 10 pts`,
      desc: 'Files in your torrent folder that qBittorrent has no knowledge of. Safe to delete unless you added them manually.',
      color: 'var(--yellow)',
      trend: makeTrend(trendSeries.orphaned, true, 'bytes'),
      actionRows: [
        [{ type: 'navigate', label: 'View Orphaned Torrents', tab: 'torrents', status: 'Orphaned' }],
        [{ type: 'navigate', label: 'Open Cleanup Workflow', tab: 'cleanup' }],
      ],
    },
    {
      label: 'Not Imported', value: formatBytes(det.not_imported_size),
      sub: `${det.not_imported_count} file${det.not_imported_count !== 1 ? 's' : ''} · threshold ${formatBytes(det.ni_limit)}`,
      pts: `${det.ni_score} / 10 pts`,
      desc: 'Seeding torrents with no matching file in your media folder. Sonarr/Radarr may have skipped or failed to import these.',
      color: 'var(--red)',
      trend: makeTrend(trendSeries.notImported, true, 'bytes'),
      actionRows: [
        [{ type: 'navigate', label: 'View Not Imported', tab: 'torrents', importFilter: 'notImported' }],
        [{ type: 'navigate', label: 'Open Triage Workflow', tab: 'triage' }],
      ],
    },
    {
      label: 'Duplicate Files', value: formatBytes(det.duplicate_size),
      sub: `${det.duplicate_count} file${det.duplicate_count !== 1 ? 's' : ''} · threshold ${formatBytes(det.dup_limit)}`,
      pts: `${det.dup_score} / 10 pts`,
      desc: 'Bit-for-bit identical files that share no inode — true copies wasting disk space.',
      color: 'var(--purple)',
      trend: makeTrend(trendSeries.duplicates, true, 'bytes'),
      actionRows: [
        [{ type: 'navigate', label: 'View Media Dupes', tab: 'media', status: 'Duplicate' },
         { type: 'navigate', label: 'View Torrent Dupes', tab: 'torrents', status: 'Duplicate' }],
        [{ type: 'navigate', label: 'Open Dedupe Workflow', tab: 'dedupe' }],
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

  const csMultDisplay = cs ? cs.multiplier.toFixed(2) : null

  // Upload chart: derive active trackers and reshape daily data for Recharts
  const effectiveTrackers = selectedTrackers !== null ? selectedTrackers : allTrackers
  const filteredTrackerStats = cs ? cs.tracker_stats.filter(t => effectiveTrackers.includes(t.name)) : []
  const uploadChartData = uploadStats ? (() => {
    const activeTrackers = Object.keys(
      (uploadStats.daily_uploads || []).reduce((acc, day) => {
        Object.entries(day.by_tracker || {}).forEach(([h, v]) => { if (v > 0) acc[h] = true })
        return acc
      }, {})
    ).filter(h => effectiveTrackers.includes(h))
    const chartData = (uploadStats.daily_uploads || []).map(day => {
      const row = { date: day.date }
      for (const h of activeTrackers) row[h] = day.by_tracker[h] || 0
      return row
    })
    return { activeTrackers, data: chartData }
  })() : null
  const yieldRows = (uploadStats?.tracker_yields || [])
    .filter(t => !(t.uploaded === 0 && (t.yield === null || t.yield === 0)))
    .filter(t => effectiveTrackers.includes(t.tracker))

  return (
    <>
    <FilterBar
      timeRange={timeRange}
      onTimeRangeChange={setTimeRange}
      selectedTrackers={selectedTrackers}
      allTrackers={allTrackers}
      onTrackersChange={setSelectedTrackers}
    />
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 16 }}>

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

      {/* Threshold alerts */}
      {(() => {
        const alerts = []
        const d = det
        if (d.orphaned_torrent_size > 0 && d.or_limit > 0 && d.orphaned_torrent_size > d.or_limit * 2) {
          alerts.push({ msg: `Orphaned torrent data (${formatBytes(d.orphaned_torrent_size)}) is significantly above your threshold`, color: 'var(--yellow)', action: { label: 'View', tab: 'torrents', status: 'Orphaned' } })
        }
        if (d.not_imported_size > 0 && d.ni_limit > 0 && d.not_imported_size > d.ni_limit * 2) {
          alerts.push({ msg: `Not-imported data (${formatBytes(d.not_imported_size)}) is significantly above your threshold`, color: 'var(--red)', action: { label: 'View', tab: 'torrents', status: 'NotImported', importFilter: 'notImported' } })
        }
        if (d.duplicate_size > 0 && d.dup_limit > 0 && d.duplicate_size > d.dup_limit * 2) {
          alerts.push({ msg: `Duplicate data (${formatBytes(d.duplicate_size)}) is significantly above your threshold`, color: 'var(--purple)', action: { label: 'View', tab: 'media', status: 'Duplicate' } })
        }
        if (!alerts.length) return null
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {alerts.map((a, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 14px', borderRadius: 8, background: a.color + '10', border: `1px solid ${a.color}30` }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }} aria-hidden="true">
                    <path d="M8 2l7 12H1L8 2z" stroke={a.color} strokeWidth="1.5" strokeLinejoin="round" />
                    <path d="M8 6.5v3" stroke={a.color} strokeWidth="1.5" strokeLinecap="round" />
                    <circle cx="8" cy="11.6" r="0.9" fill={a.color} />
                  </svg>
                  <span style={{ fontSize: 12, color: 'var(--text)' }}>{a.msg}</span>
                </div>
                <button onClick={() => onNavigate(a.action)} style={{ padding: '4px 12px', borderRadius: 6, border: `1px solid ${a.color}40`, background: a.color + '15', color: a.color, fontSize: 11, fontWeight: 500, cursor: 'pointer', flexShrink: 0, marginLeft: 12 }}>
                  {a.action.label} →
                </button>
              </div>
            ))}
          </div>
        )
      })()}

      {/* Row 1: dial + chart */}
      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: '20px 20px 16px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
          <span style={{ ...SECTION_LABEL, alignSelf: 'flex-start' }}>Library Health</span>
          <HealthDial score={score} status={status} smartTrend={smartTrend} color={c} />
        </div>

        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: '20px 20px 14px', display: 'flex', flexDirection: 'column' }}>
          <div style={{ marginBottom: 18 }}>
            <span style={SECTION_LABEL}>Score History</span>
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
              <XAxis dataKey="date" tick={{ fontFamily: 'var(--mono)', fontSize: 11, fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} minTickGap={36} tickFormatter={fmtChartDate} />
              <YAxis domain={[minScore, maxScore]} ticks={yTicks} allowDecimals={false} width={30} tick={{ fontFamily: 'var(--mono)', fontSize: 11, fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} />
              <Tooltip content={<GrafanaTooltip color={c} />} cursor={{ stroke: c + '40', strokeWidth: 1, strokeDasharray: '3 3' }} />
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

      {/* Row 3: cross-seed panels */}
      {cs && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>

          {/* Cross-seed effectiveness */}
          <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div style={{ display: 'flex', alignItems: 'flex-start' }}>
              <div>
                <div style={{ ...SECTION_LABEL, marginBottom: 8 }}>Cross-Seed Effectiveness</div>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                  <span style={{ fontFamily: 'var(--mono)', fontSize: 40, fontWeight: 700, color: 'var(--blue)', lineHeight: 1 }}>{csMultDisplay}×</span>
                  <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>avg seed multiplier</span>
                </div>
                <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 8, lineHeight: 1.6, maxWidth: 340 }}>
                  Weighted average of how many trackers each byte of media is seeded on. 1.0× = all files seeded once. Higher is better.
                </p>
              </div>
            </div>

            {/* Distribution bar */}
            <div>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: 1.5, textTransform: 'uppercase', marginBottom: 8 }}>Disk Space by Seed Count</div>
              <CrossSeedBar segments={cs.segments} totalSize={cs.total_size} onNavigate={onNavigate} />
            </div>
          </div>

          {/* Tracker leaderboard */}
          <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <div style={{ ...SECTION_LABEL, marginBottom: 4 }}>Top Trackers by Disk Space</div>
              <p style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.5 }}>Click a tracker for detailed stats and navigation.</p>
            </div>
            <TrackerLeaderboard trackerStats={filteredTrackerStats} onTrackerDetail={setTrackerDetail} />

            {/* All trackers summary */}
            {filteredTrackerStats.length > 3 && (
              <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {filteredTrackerStats.slice(3).map(t => (
                  <button
                    key={t.name}
                    onClick={() => setTrackerDetail(t.name)}
                    style={{
                      fontFamily: 'var(--mono)', fontSize: 11, padding: '3px 8px',
                      borderRadius: 99, border: '1px solid var(--border2)',
                      background: 'transparent', color: 'var(--text-dim)',
                      cursor: 'pointer', transition: 'all 0.12s',
                    }}
                    onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--accent)'; e.currentTarget.style.color = 'var(--accent)' }}
                    onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border2)'; e.currentTarget.style.color = 'var(--text-dim)' }}
                  >
                    {t.name}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      )}


      {/* Row 4: upload activity + library yield */}
      {uploadStats && uploadChartData && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>

          {/* Upload Activity */}
          <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <div style={{ ...SECTION_LABEL, marginBottom: 4 }}>Upload Activity</div>
            </div>
            <div style={{ flex: 1, minHeight: 0 }}>
              {effectiveTrackers.length === 0
                ? <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>No trackers selected</div>
                : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={uploadChartData.data} margin={{ top: 4, right: 20, left: 4, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="2 4" stroke="var(--border)" strokeOpacity={0.6} vertical={false} />
                      <XAxis dataKey="date" tick={{ fontFamily: 'var(--mono)', fontSize: 11, fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} minTickGap={36} tickFormatter={fmtChartDate} />
                      <YAxis
                        width={44}
                        tick={{ fontFamily: 'var(--mono)', fontSize: 11, fill: 'var(--text-dim)' }}
                        tickLine={false} axisLine={false}
                        tickFormatter={formatBytesCompact}
                      />
                      <Tooltip content={<UploadActivityTooltip />} cursor={{ fill: 'var(--surface2)' }} />
                      {uploadChartData.activeTrackers.map((host, i) => (
                        <Bar
                          key={host} dataKey={host} stackId="uploads"
                          fill={TRACKER_COLORS[i % TRACKER_COLORS.length]}
                          maxBarSize={80}
                          shape={props => <RoundedStackedBar {...props} allTrackers={uploadChartData.activeTrackers} host={host} />}
                        />
                      ))}
                    </BarChart>
                  </ResponsiveContainer>
                )
              }
            </div>
          </div>

          {/* Library Yield */}
          <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <div style={SECTION_LABEL}>
                  {yieldPanelTab === 'upload' ? 'Upload by Tracker' : 'Library Yield'}
                </div>
                <div style={{ display: 'flex', gap: 10 }}>
                  {['upload', 'yield'].map(tab => (
                    <button key={tab} onClick={() => setYieldPanelTab(tab)} style={{
                      background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                      fontFamily: 'var(--mono)', fontSize: 11, letterSpacing: 1, textTransform: 'uppercase',
                      color: yieldPanelTab === tab ? 'var(--accent)' : 'var(--text-dim)',
                      fontWeight: yieldPanelTab === tab ? 700 : 400,
                    }}>{tab}</button>
                  ))}
                </div>
              </div>
              {yieldPanelTab === 'upload' ? (
                <>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 40, fontWeight: 700, color: 'var(--green)', lineHeight: 1 }}>
                      {formatBytes(yieldRows.reduce((s, t) => s + t.uploaded, 0))}
                    </span>
                  </div>
                  <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 8, lineHeight: 1.6 }}>
                    total uploaded · {uploadStats.period_days} day window
                  </p>
                </>
              ) : (
                <>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 40, fontWeight: 700, color: 'var(--green)', lineHeight: 1 }}>
                      {(() => {
                        const filteredUploaded = yieldRows.reduce((s, t) => s + t.uploaded, 0)
                        const filteredSeeding  = yieldRows.reduce((s, t) => s + t.seeding_size, 0)
                        const filteredYield    = filteredSeeding > 0 ? filteredUploaded / filteredSeeding : null
                        return filteredYield !== null ? (filteredYield * 100).toFixed(2) + '%' : '—'
                      })()}
                    </span>
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
                      over {uploadStats.period_days} day{uploadStats.period_days !== 1 ? 's' : ''}
                    </span>
                  </div>
                  <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 8, lineHeight: 1.6 }}>
                    Upload volume relative to seeding size. Higher yield = your disk space is earning more.
                  </p>
                </>
              )}
            </div>
            {effectiveTrackers.length === 0
              ? <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', textAlign: 'center', padding: '16px 0' }}>No trackers selected</div>
              : yieldPanelTab === 'yield'
                ? yieldRows.length > 0 && (
                    <div style={{ flex: 1, overflow: 'auto' }}>
                      <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'var(--mono)', fontSize: 10 }}>
                        <thead>
                          <tr>
                            {['Tracker', 'Uploaded', 'Seeding', 'Yield'].map(h => (
                              <th key={h} style={{
                                textAlign: h === 'Tracker' ? 'left' : 'right',
                                padding: '4px 8px', color: 'var(--text-dim)', fontWeight: 600,
                                letterSpacing: 1, fontSize: 11, textTransform: 'uppercase',
                                borderBottom: '1px solid var(--border)',
                              }}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {yieldRows.map((t, i) => (
                            <tr key={t.tracker} style={{ background: i % 2 === 0 ? 'var(--surface2)' : 'transparent' }}>
                              <td style={{ padding: '5px 8px', maxWidth: 120 }}>
                                <button
                                  onClick={() => setTrackerDetail(t.tracker)}
                                  style={{ fontFamily: 'var(--mono)', fontSize: 11, background: 'none', border: 'none', cursor: 'pointer', color: 'var(--accent)', padding: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}
                                >{t.tracker}</button>
                              </td>
                              <td style={{ padding: '5px 8px', color: 'var(--text-dim)', textAlign: 'right' }}>{formatBytes(t.uploaded)}</td>
                              <td style={{ padding: '5px 8px', color: 'var(--text-dim)', textAlign: 'right' }}>{formatBytes(t.seeding_size)}</td>
                              <td style={{ padding: '5px 8px', textAlign: 'right', fontWeight: t.yield > 0 ? 600 : 400, color: t.yield > 0 ? 'var(--green)' : 'var(--text-dim)' }}>
                                {t.yield !== null ? (t.yield * 100).toFixed(2) + '%' : '—'}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )
                : yieldRows.length > 0 && (
                    <div style={{ flex: 1, overflow: 'auto' }}>
                      <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'var(--mono)', fontSize: 10 }}>
                        <thead>
                          <tr>
                            {['Tracker', 'Total Uploaded'].map(h => (
                              <th key={h} style={{
                                textAlign: h === 'Tracker' ? 'left' : 'right',
                                padding: '4px 8px', color: 'var(--text-dim)', fontWeight: 600,
                                letterSpacing: 1, fontSize: 11, textTransform: 'uppercase',
                                borderBottom: '1px solid var(--border)',
                              }}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {[...yieldRows].sort((a, b) => b.uploaded - a.uploaded).map((t, i) => (
                            <tr key={t.tracker} style={{ background: i % 2 === 0 ? 'var(--surface2)' : 'transparent' }}>
                              <td style={{ padding: '5px 8px', maxWidth: 120 }}>
                                <button
                                  onClick={() => setTrackerDetail(t.tracker)}
                                  style={{ fontFamily: 'var(--mono)', fontSize: 11, background: 'none', border: 'none', cursor: 'pointer', color: 'var(--accent)', padding: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}
                                >{t.tracker}</button>
                              </td>
                              <td style={{ padding: '5px 8px', color: 'var(--text-dim)', textAlign: 'right' }}>{formatBytes(t.uploaded)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )
            }
          </div>

        </div>
      )}


      {trackerDetail && (
        <TrackerDetailModal
          trackerName={trackerDetail}
          trackerStats={data.tracker_file_stats?.[trackerDetail]}
          uploadStats={uploadStats}
          onNavigate={(action) => { setTrackerDetail(null); onNavigate(action) }}
          onClose={() => setTrackerDetail(null)}
        />
      )}
    </div>
    </>
  )
}
