import React, { useState, useEffect } from 'react'
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { formatBytes, scoreColor } from '../utils'
import ChangesPanel from './ChangesPanel'
import FilterBar from './FilterBar'
import { api } from '../api'
import { useToast } from './Toast'

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
function HealthDial({ score, status, smartTrend, color }) {
  const SIZE = 220
  const CX = SIZE / 2, CY = SIZE / 2
  const R_OUTER = 90, R_INNER = 68
  const GAP_DEG = 50
  const START_DEG = 90 + GAP_DEG / 2
  const END_DEG   = 90 + 360 - GAP_DEG / 2
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

  const pct     = Math.min(Math.max(score, 0), 100) / 100
  const fillEnd = START_DEG + SWEEP_DEG * pct
  const trackPath = arcPath(CX, CY, R_OUTER, R_INNER, START_DEG, END_DEG)
  const ticks = [0, 25, 50, 75, 100].map(v => {
    const deg = START_DEG + SWEEP_DEG * (v / 100)
    return { inner: polarToXY(CX, CY, R_INNER - 4, deg), outer: polarToXY(CX, CY, R_OUTER + 4, deg) }
  })

  const delta = smartTrend?.delta
  const trendLabel = smartTrend?.label
  const up = delta != null && delta >= 0

  // Build arc segments that sweep the color along the arc path.
  // We draw N thin segments, each colored by interpolating red->yellow->green
  // based on its position along the arc. Only segments within the filled
  // portion (0..pct) are rendered — this gives a true conic gradient effect.
  const SEGMENTS = 60  // enough for smooth appearance
  const arcSegments = []
  for (let i = 0; i < SEGMENTS; i++) {
    const t0 = i / SEGMENTS
    const t1 = (i + 1) / SEGMENTS
    if (t0 >= pct) break  // only draw up to the score
    const cappedT1 = Math.min(t1, pct)
    const segStart = START_DEG + SWEEP_DEG * t0
    const segEnd   = START_DEG + SWEEP_DEG * cappedT1
    const segPath  = arcPath(CX, CY, R_OUTER, R_INNER, segStart, segEnd)
    // Color: 0=red, 0.5=yellow, 1=green interpolated in HSL space
    // red=0deg, yellow=60deg, green=120deg in HSL
    const hue = t0 * 120  // 0 -> 120
    const segColor = `hsl(${hue}, 90%, 52%)`
    arcSegments.push({ path: segPath, color: segColor })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
      <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`}>
        {/* Track */}
        <path d={trackPath} fill="var(--surface3)" />
        {/* Color-swept filled segments */}
        {arcSegments.map((seg, i) => (
          <path key={i} d={seg.path} fill={seg.color}
            style={{ filter: i === arcSegments.length - 1 ? 'drop-shadow(0 0 5px ' + color + '66)' : 'none' }} />
        ))}
        {/* Tick marks on top */}
        {ticks.map((t, i) => (
          <line key={i} x1={t.inner.x} y1={t.inner.y} x2={t.outer.x} y2={t.outer.y}
            stroke="var(--bg)" strokeWidth="2.5" />
        ))}
        <text x={CX} y={CY - 6} textAnchor="middle" fontFamily="var(--mono)" fontSize="38" fontWeight="700" fill={color}>{score}</text>
        <text x={CX} y={CY + 12} textAnchor="middle" fontFamily="var(--mono)" fontSize="10" fill="var(--text-dim)">/ 100</text>
        <text x={CX} y={CY + 28} textAnchor="middle" fontFamily="var(--mono)" fontSize="9" fontWeight="600" fill={color} letterSpacing="1.5">{status?.toUpperCase()}</text>
      </svg>
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
      <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', marginBottom: 6 }}>{label}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
        <div style={{ width: 8, height: 8, borderRadius: 2, background: color, flexShrink: 0 }} />
        <span style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 700, color: '#ebebeb' }}>{payload[0].value}</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>/ 100</span>
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
      <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', marginBottom: 6 }}>{label}</div>
      {items.map(p => (
        <div key={p.name} style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 3 }}>
          <div style={{ width: 8, height: 8, borderRadius: 2, background: p.fill, flexShrink: 0 }} />
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: '#ebebeb', flex: 1 }}>{p.name}</span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>{formatBytes(p.value)}</span>
        </div>
      ))}
      {items.length > 1 && (
        <div style={{ marginTop: 6, paddingTop: 6, borderTop: '1px solid #2a2a2a', display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>Total</span>
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

// ── Metric card ───────────────────────────────────────────────────────────────
function MetricCard({ label, value, sub, pts, desc, color, actionRows, onNavigate, onScript, toast }) {
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
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginTop: 4 }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 9, fontWeight: 600, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase' }}>{label}</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color, background: color + '18', border: '1px solid ' + color + '35', borderRadius: 4, padding: '1px 6px' }}>{pts}</span>
      </div>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 34, fontWeight: 700, color, lineHeight: 1, marginTop: 10 }}>{value}</span>
      <span style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>{sub}</span>
      <p style={{ fontSize: 11.5, color: 'var(--text-dim)', marginTop: 10, lineHeight: 1.6, flexGrow: 1 }}>{desc}</p>
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

      {/* Tooltip */}
      {hovered !== null && segments[hovered] && segments[hovered].size > 0 && (() => {
        const seg = segments[hovered]
        const color = seedColor(seg.count)
        const pct = ((seg.size / totalSize) * 100).toFixed(1)
        return (
          <div style={{ marginTop: 8, padding: '8px 12px', background: 'var(--surface2)', border: '1px solid var(--border2)', borderRadius: 6, display: 'inline-flex', gap: 10, alignItems: 'center' }}>
            <div style={{ width: 10, height: 10, borderRadius: 3, background: color, flexShrink: 0 }} />
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>
              {seg.count === 0 ? 'Orphaned (0×)' : `${seg.count}× seeded`}
            </span>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
              {formatBytes(seg.size)} · {pct}%
            </span>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--accent)' }}>click to filter →</span>
          </div>
        )
      })()}

      {/* Legend */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 14px', marginTop: hovered !== null ? 8 : 12 }}>
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
              <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>
                {seg.count === 0 ? '0× (orphaned)' : `${seg.count}×`} — {pct}%
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
                <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', flexShrink: 0 }}>{t.count} files · {formatBytes(t.size)}</span>
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

// ── Compute cross-seed stats from media_files ─────────────────────────────────
function computeCrossSeedStats(mediaFiles) {
  if (!mediaFiles || mediaFiles.length === 0) return null

  // Group by seed count (number of real trackers, excluding "None")
  const buckets = {}   // count → total_size
  let weightedSum = 0
  let totalSize = 0

  for (const f of mediaFiles) {
    const realTrackers = (f.trackers || []).filter(t => t !== 'None')
    const n = realTrackers.length  // 0 = orphaned/not seeded
    buckets[n] = (buckets[n] || 0) + f.size
    weightedSum += f.size * n
    totalSize += f.size
  }

  // Cross-seed score: weightedSum / totalSize (e.g. 1.8 = avg 1.8x seeded)
  const crossSeedMultiplier = totalSize > 0 ? weightedSum / totalSize : 0

  // Build sorted segments array for the bar
  const maxCount = Math.max(...Object.keys(buckets).map(Number))
  const segments = []
  for (let i = 0; i <= maxCount; i++) {
    segments.push({ count: i, size: buckets[i] || 0 })
  }

  // Tracker leaderboard from media files
  const trackerMap = {}
  for (const f of mediaFiles) {
    for (const t of (f.trackers || [])) {
      if (t === 'None') continue
      if (!trackerMap[t]) trackerMap[t] = { name: t, size: 0, count: 0 }
      trackerMap[t].size += f.size
      trackerMap[t].count++
    }
  }
  const trackerStats = Object.values(trackerMap).sort((a, b) => b.size - a.size)

  return { crossSeedMultiplier, segments, totalSize, trackerStats }
}

// ── Tracker card (shared between modal and Trackers page) ─────────────────────
export function TrackerCard({ trackerName, torrentFiles, uploadStats, onNavigate, onClose }) {
  const trackerTorrents = torrentFiles.filter(f => (f.trackers || []).includes(trackerName))
  const seeding     = trackerTorrents.filter(f => f.status === 'Seeding')
  const orphaned    = trackerTorrents.filter(f => f.status === 'Orphaned')
  const notImported = trackerTorrents.filter(f => !f.imported && f.status !== 'Orphaned')

  const seedingSize     = seeding.reduce((a, f) => a + f.size, 0)
  const orphanedSize    = orphaned.reduce((a, f) => a + f.size, 0)
  const notImportedSize = notImported.reduce((a, f) => a + f.size, 0)

  const yieldData = uploadStats?.tracker_yields?.find(t => t.tracker === trackerName)
  const yieldPct  = yieldData?.yield != null ? (yieldData.yield * 100).toFixed(2) + '%' : '—'
  const uploadedBytes = yieldData?.uploaded ?? null

  const uploadTrendData = uploadStats?.daily_uploads?.map(day => ({
    date: day.date.slice(5),
    uploaded: day.by_tracker?.[trackerName] || 0,
  }))
  const hasUploadData = uploadTrendData?.some(d => d.uploaded > 0)
  const gradId = `tug-${trackerName.replace(/[^a-zA-Z0-9]/g, '')}`

  const statBoxes = [
    { label: 'Seeding',      value: formatBytes(seedingSize),                                          sub: `${seeding.length} files`,                          color: 'var(--green)'  },
    { label: 'Uploaded',     value: uploadedBytes !== null ? formatBytes(uploadedBytes) : '—',         sub: uploadStats ? `last ${uploadStats.period_days}d` : 'no data yet', color: 'var(--blue)'   },
    { label: 'Yield',        value: yieldPct,                                                          sub: 'uploaded / seeding',                               color: 'var(--accent)' },
    { label: 'Orphaned',     value: formatBytes(orphanedSize),                                         sub: `${orphaned.length} files`,                         color: 'var(--yellow)' },
    { label: 'Not Imported', value: formatBytes(notImportedSize),                                      sub: `${notImported.length} files`,                      color: 'var(--red)'    },
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

        {/* Stats row */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10 }}>
          {statBoxes.map(s => (
            <div key={s.label} style={{ background: 'var(--surface2)', border: '1px solid var(--border)', borderRadius: 'var(--r)', padding: '10px 14px' }}>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase', marginBottom: 6 }}>{s.label}</div>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 22, fontWeight: 700, color: s.color, lineHeight: 1 }}>{s.value}</div>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>{s.sub}</div>
            </div>
          ))}
        </div>

        {/* Upload trend */}
        {uploadStats && (
          hasUploadData ? (
            <div>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase', marginBottom: 10 }}>Upload Trend</div>
              <div style={{ height: 160 }}>
                <ResponsiveContainer width="100%" height={160}>
                  <AreaChart data={uploadTrendData} margin={{ top: 4, right: 4, left: -10, bottom: 0 }}>
                    <defs>
                      <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.25} />
                        <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="2 4" stroke="var(--border)" strokeOpacity={0.6} vertical={false} />
                    <XAxis dataKey="date" tick={{ fontFamily: 'var(--mono)', fontSize: 9, fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} />
                    <YAxis
                      tick={{ fontFamily: 'var(--mono)', fontSize: 9, fill: 'var(--text-dim)' }}
                      tickLine={false} axisLine={false}
                      tickFormatter={v => v >= 1e12 ? (v/1e12).toFixed(1)+'T' : v >= 1e9 ? (v/1e9).toFixed(1)+'G' : v >= 1e6 ? (v/1e6).toFixed(0)+'M' : v}
                    />
                    <Tooltip
                      content={({ active, payload, label }) => {
                        if (!active || !payload?.length) return null
                        return (
                          <div style={{ background: '#151515', border: '1px solid #2a2a2a', borderRadius: 6, padding: '10px 14px', boxShadow: '0 8px 24px rgba(0,0,0,0.5)' }}>
                            <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', marginBottom: 4 }}>{label}</div>
                            <div style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 700, color: '#ebebeb' }}>{formatBytes(payload[0].value)}</div>
                          </div>
                        )
                      }}
                      cursor={{ stroke: 'var(--accent)', strokeWidth: 1, strokeOpacity: 0.4, strokeDasharray: '3 3' }}
                    />
                    <Area type="linear" dataKey="uploaded" stroke="var(--accent)" strokeWidth={1.5} fill={`url(#${gradId})`} dot={false} activeDot={{ r: 4, fill: 'var(--accent)', stroke: 'var(--bg)', strokeWidth: 2 }} />
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
          {seeding.length > 0 && (
            <button style={btnStyle} onMouseEnter={btnHover} onMouseLeave={btnLeave}
              onClick={() => onNavigate({ tab: 'torrents', tracker: trackerName, status: 'Seeding' })}>
              View Seeding Files <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>({seeding.length} files · {formatBytes(seedingSize)})</span>
            </button>
          )}
          {orphaned.length > 0 && (
            <button style={btnStyle} onMouseEnter={btnHover} onMouseLeave={btnLeave}
              onClick={() => onNavigate({ tab: 'torrents', tracker: trackerName, status: 'Orphaned' })}>
              View Orphaned Files <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>({orphaned.length} files · {formatBytes(orphanedSize)})</span>
            </button>
          )}
          {notImported.length > 0 && (
            <button style={btnStyle} onMouseEnter={btnHover} onMouseLeave={btnLeave}
              onClick={() => onNavigate({ tab: 'torrents', tracker: trackerName, importFilter: 'notImported' })}>
              View Not Imported <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>({notImported.length} files · {formatBytes(notImportedSize)})</span>
            </button>
          )}
        </div>

      </div>
    </div>
  )
}

// ── Tracker detail modal (thin wrapper around TrackerCard) ────────────────────
function TrackerDetailModal({ trackerName, torrentFiles, uploadStats, onNavigate, onClose }) {
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
          torrentFiles={torrentFiles}
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
  const [sonarrConfigured, setSonarrConfigured] = useState(false)
  const [radarrConfigured, setRadarrConfigured] = useState(false)
  const [uploadStats, setUploadStats] = useState(null)
  const [trackerDetail, setTrackerDetail] = useState(null)
  const [yieldPanelTab, setYieldPanelTab] = useState('upload')

  useEffect(() => {
    api.getConfig().then(cfg => {
      setSonarrConfigured(!!cfg.SONARR_URL)
      setRadarrConfigured(!!cfg.RADARR_URL)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    api.uploadStats(timeRange).then(d => {
      if (!d.status) setUploadStats(d)
    }).catch(() => {})
  }, [timeRange])

  if (!data) return <DashboardSkeleton />

  const { score, status, trend, current, history_chart } = data
  const det = current.details
  const hlPct = det.total_media_size > 0
    ? Math.round((det.hardlinked_media_size / det.total_media_size) * 100) : 100
  const c = scoreColor(score)

  // Cross-seed calculations use media_files passed via data
  const cs = computeCrossSeedStats(data.media_files)

  const notImportedPaths = (data.torrent_files || [])
    .filter(f => !f.imported && f.status !== 'Orphaned')
    .map(f => f.path)

  const metrics = [
    {
      label: 'Hardlinked Media', value: hlPct + '%',
      sub: `${formatBytes(det.hardlinked_media_size)} of ${formatBytes(det.total_media_size)}`,
      pts: `${det.hl_score} / 70 pts`,
      desc: 'Percentage of your media library that is hardlinked back to a torrent file. 100% means everything is connected.',
      color: 'var(--blue)',
      actionRows: [
        [{ type: 'navigate', label: 'View Orphaned Media', tab: 'media', status: 'Orphaned' }],
        [],
        [],
      ],
    },
    {
      label: 'Orphaned Torrents', value: formatBytes(det.orphaned_torrent_size),
      sub: `${det.orphaned_torrent_count} file${det.orphaned_torrent_count !== 1 ? 's' : ''} · threshold ${formatBytes(det.or_limit)}`,
      pts: `${det.or_score} / 10 pts`,
      desc: 'Files in your torrent folder that qBittorrent has no knowledge of. Safe to delete unless you added them manually.',
      color: 'var(--yellow)',
      actionRows: [
        [{ type: 'navigate', label: 'View Orphaned Torrents', tab: 'torrents', status: 'Orphaned' }],
        [{ type: 'script', label: 'Generate Delete Script', scriptType: 'orphaned_torrents_delete', title: 'Orphaned Torrent Delete Script' }],
        [],
      ],
    },
    {
      label: 'Not Imported', value: formatBytes(det.not_imported_size),
      sub: `${det.not_imported_count} file${det.not_imported_count !== 1 ? 's' : ''} · threshold ${formatBytes(det.ni_limit)}`,
      pts: `${det.ni_score} / 10 pts`,
      desc: 'Seeding torrents with no matching file in your media folder. Sonarr/Radarr may have skipped or failed to import these.',
      color: 'var(--red)',
      actionRows: [
        [{ type: 'navigate', label: 'View Not Imported', tab: 'torrents', importFilter: 'notImported' }],
        [{ type: 'api', label: 'Trigger Radarr Rescan', loadingLabel: 'Rescanning…',
            apiCall: () => api.radarrRescan(notImportedPaths),
            successToast: 'Radarr rescan triggered — check Radarr for import results',
            errorToast: true, hidden: !radarrConfigured }],
        [{ type: 'api', label: 'Trigger Sonarr Rescan', loadingLabel: 'Rescanning…',
            apiCall: () => api.sonarrRescan(notImportedPaths),
            successToast: 'Sonarr rescan triggered — check Sonarr for import results',
            errorToast: true, hidden: !sonarrConfigured }],
      ],
    },
    {
      label: 'Duplicate Files', value: formatBytes(det.duplicate_size),
      sub: `${det.duplicate_count} file${det.duplicate_count !== 1 ? 's' : ''} · threshold ${formatBytes(det.dup_limit)}`,
      pts: `${det.dup_score} / 10 pts`,
      desc: 'Bit-for-bit identical files that share no inode — true copies wasting disk space.',
      color: 'var(--purple)',
      actionRows: [
        [{ type: 'navigate', label: 'View Media Dupes', tab: 'media', status: 'Duplicate' }],
        [{ type: 'navigate', label: 'View Torrent Dupes', tab: 'torrents', status: 'Duplicate' }],
        [{ type: 'script', label: 'Generate Dedupe Script', scriptType: 'dedupe', title: 'Dedupe Script' }],
      ],
    },
  ]

  const filteredHistory = (() => {
    if (!history_chart) return []
    const cutoff = new Date()
    cutoff.setDate(cutoff.getDate() - timeRange)
    return history_chart.filter(d => new Date(d.date) >= cutoff)
  })()

  const scores = filteredHistory.map(d => d.avg_score).filter(Boolean)
  const minScore = scores.length ? Math.max(0, Math.min(...scores) - 5) : 0
  const maxScore = scores.length ? Math.min(100, Math.max(...scores) + 5) : 100

  // Smart trend: delta vs timeRange days ago (fallback to oldest entry)
  const smartTrend = (() => {
    if (!history_chart || history_chart.length < 2) return null
    const today = history_chart[history_chart.length - 1]
    const todayDate = new Date(today.date)
    const targetDate = new Date(todayDate)
    targetDate.setDate(targetDate.getDate() - timeRange)

    let best = null
    let bestDiff = Infinity
    for (const entry of history_chart) {
      const d = Math.abs(new Date(entry.date) - targetDate)
      if (d < bestDiff) { bestDiff = d; best = entry }
    }

    if (!best || best.date === today.date) return null

    const delta = Math.round((today.avg_score - best.avg_score) * 10) / 10
    const actualDays = Math.round(Math.abs(new Date(today.date) - new Date(best.date)) / (1000 * 60 * 60 * 24))
    return { delta, label: `vs ${actualDays}d ago` }
  })()

  const csMultDisplay = cs ? cs.crossSeedMultiplier.toFixed(2) : null

  // Upload chart: derive active trackers and reshape daily data for Recharts
  const effectiveTrackers = selectedTrackers !== null ? selectedTrackers : allTrackers
  const filteredTrackerStats = cs ? cs.trackerStats.filter(t => effectiveTrackers.includes(t.name)) : []
  const uploadChartData = uploadStats ? (() => {
    const activeTrackers = Object.keys(
      (uploadStats.daily_uploads || []).reduce((acc, day) => {
        Object.entries(day.by_tracker || {}).forEach(([h, v]) => { if (v > 0) acc[h] = true })
        return acc
      }, {})
    ).filter(h => effectiveTrackers.includes(h))
    const chartData = (uploadStats.daily_uploads || []).map(day => {
      const row = { date: day.date.slice(5) }  // MM-DD
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
                  <span style={{ color: a.color, fontSize: 14 }}>⚠</span>
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
          <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 2.5, textTransform: 'uppercase', alignSelf: 'flex-start' }}>Library Health</span>
          <HealthDial score={score} status={status} smartTrend={smartTrend} color={c} />
        </div>

        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: '20px 20px 14px', display: 'flex', flexDirection: 'column' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 18 }}>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 2.5, textTransform: 'uppercase' }}>Score History</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <div style={{ width: 8, height: 8, borderRadius: 2, background: c }} />
              <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>avg_score</span>
            </div>
          </div>
          <div style={{ flex: 1, minHeight: 0 }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={filteredHistory} margin={{ top: 4, right: 4, left: -18, bottom: 0 }}>
              <defs>
                <linearGradient id="grafanaGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={c} stopOpacity={0.25} />
                  <stop offset="100%" stopColor={c} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="2 4" stroke="var(--border)" strokeOpacity={0.6} vertical={false} />
              <XAxis dataKey="date" tick={{ fontFamily: 'var(--mono)', fontSize: 9, fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} tickFormatter={v => v.slice(5)} />
              <YAxis domain={[minScore, maxScore]} tick={{ fontFamily: 'var(--mono)', fontSize: 9, fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} tickCount={5} tickFormatter={v => Math.round(v)} />
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
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
              <div>
                <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase', marginBottom: 8 }}>Cross-Seed Effectiveness</div>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                  <span style={{ fontFamily: 'var(--mono)', fontSize: 40, fontWeight: 700, color: 'var(--blue)', lineHeight: 1 }}>{csMultDisplay}×</span>
                  <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>avg seed multiplier</span>
                </div>
                <p style={{ fontSize: 11.5, color: 'var(--text-dim)', marginTop: 8, lineHeight: 1.6, maxWidth: 340 }}>
                  Weighted average of how many trackers each byte of media is seeded on. 1.0× = all files seeded once. Higher is better.
                </p>
              </div>

              {/* Rating badge instead of misleading /100 gauge */}
              <div style={{ flexShrink: 0, textAlign: 'center' }}>
                {(() => {
                  const mult = cs.crossSeedMultiplier
                  const { label, color } = mult >= 2.5 ? { label: 'Excellent', color: 'var(--green)' }
                    : mult >= 1.8 ? { label: 'Good', color: 'var(--blue)' }
                    : mult >= 1.2 ? { label: 'Fair', color: 'var(--yellow)' }
                    : { label: 'Low', color: 'var(--red)' }
                  return (
                    <div style={{
                      padding: '10px 14px', borderRadius: 10,
                      background: color + '15', border: `1px solid ${color}35`,
                      display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
                    }}>
                      <span style={{ fontFamily: 'var(--mono)', fontSize: 22, fontWeight: 700, color, lineHeight: 1 }}>{label}</span>
                      <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 1, textTransform: 'uppercase' }}>effectiveness</span>
                    </div>
                  )
                })()}
              </div>
            </div>

            {/* Distribution bar */}
            <div>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 1.5, textTransform: 'uppercase', marginBottom: 8 }}>Disk Space by Seed Count</div>
              <CrossSeedBar segments={cs.segments} totalSize={cs.totalSize} onNavigate={onNavigate} />
            </div>
          </div>

          {/* Tracker leaderboard */}
          <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase', marginBottom: 4 }}>Top Trackers by Disk Space</div>
              <p style={{ fontSize: 11.5, color: 'var(--text-dim)', lineHeight: 1.5 }}>Click a tracker for detailed stats and navigation.</p>
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
                      fontFamily: 'var(--mono)', fontSize: 10, padding: '3px 8px',
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
              <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase', marginBottom: 4 }}>Upload Activity</div>
            </div>
            <div style={{ height: 220 }}>
              {effectiveTrackers.length === 0
                ? <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>No trackers selected</div>
                : (
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart data={uploadChartData.data} margin={{ top: 4, right: 4, left: -10, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="2 4" stroke="var(--border)" strokeOpacity={0.6} vertical={false} />
                      <XAxis dataKey="date" tick={{ fontFamily: 'var(--mono)', fontSize: 9, fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} />
                      <YAxis
                        tick={{ fontFamily: 'var(--mono)', fontSize: 9, fill: 'var(--text-dim)' }}
                        tickLine={false} axisLine={false}
                        tickFormatter={v => v >= 1e12 ? (v/1e12).toFixed(1)+'T' : v >= 1e9 ? (v/1e9).toFixed(1)+'G' : v >= 1e6 ? (v/1e6).toFixed(0)+'M' : v}
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
                <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase' }}>
                  {yieldPanelTab === 'upload' ? 'Upload by Tracker' : 'Library Yield'}
                </div>
                <div style={{ display: 'flex', gap: 10 }}>
                  {['upload', 'yield'].map(tab => (
                    <button key={tab} onClick={() => setYieldPanelTab(tab)} style={{
                      background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                      fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 1, textTransform: 'uppercase',
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
                  <p style={{ fontSize: 11.5, color: 'var(--text-dim)', marginTop: 8, lineHeight: 1.6 }}>
                    total uploaded · {uploadStats.period_days} day window
                  </p>
                </>
              ) : (
                <>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 40, fontWeight: 700, color: 'var(--green)', lineHeight: 1 }}>
                      {uploadStats.library_yield !== null ? (uploadStats.library_yield * 100).toFixed(2) + '%' : '—'}
                    </span>
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
                      over {uploadStats.period_days} day{uploadStats.period_days !== 1 ? 's' : ''}
                    </span>
                  </div>
                  <p style={{ fontSize: 11.5, color: 'var(--text-dim)', marginTop: 8, lineHeight: 1.6 }}>
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
                                letterSpacing: 1, fontSize: 9, textTransform: 'uppercase',
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
                                  style={{ fontFamily: 'var(--mono)', fontSize: 10, background: 'none', border: 'none', cursor: 'pointer', color: 'var(--accent)', padding: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}
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
                                letterSpacing: 1, fontSize: 9, textTransform: 'uppercase',
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
                                  style={{ fontFamily: 'var(--mono)', fontSize: 10, background: 'none', border: 'none', cursor: 'pointer', color: 'var(--accent)', padding: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}
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
          torrentFiles={data.torrent_files || []}
          mediaFiles={data.media_files || []}
          uploadStats={uploadStats}
          onNavigate={(action) => { setTrackerDetail(null); onNavigate(action) }}
          onClose={() => setTrackerDetail(null)}
        />
      )}
    </div>
    </>
  )
}
