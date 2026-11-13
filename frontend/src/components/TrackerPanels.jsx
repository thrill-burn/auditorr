import React, { useState } from 'react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { formatBytes, formatBytesCompact, tint } from '../utils'
import { Button, CloseButton } from './workflows/shared'
import { SECTION_LABEL, CARD, CAPTION, READOUT, fmtChartDate, splitValueUnit, HeroNumber } from './dashKit'

// The Trackers page's cards and the tracker detail popup.
//
// The Dashboard's upload panels moved here on 2026-11-09 and Cross-seed
// effectiveness on 2026-11-10. On 2026-11-13 three of them went into the
// page's chart and table: Upload activity's stacked bars are the chart's
// Uploaded > Daily view, and Upload by tracker and Top trackers by disk space
// repeated the table's Uploaded, Yield and Seeding columns. Seeded only here
// and the health card took their places.

// ── Cross-seed effectiveness ──────────────────────────────────────────────────
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
                // The one place the alpha suffix worked: SEED_COLORS are hex, so
                // `#ef4444cc` is a valid 8-digit colour. Said as tint() anyway,
                // so the guard needs no exception and a var() can't creep in.
                background: hovered === i ? color : tint(color, 80),
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
              <span style={{ ...READOUT, color: 'var(--text)' }}>
                {seg.count === 0 ? 'Not Seeded (0×)' : `${seg.count}× seeded`}
              </span>
              <span style={READOUT}>
                {formatBytes(seg.size)} · {pct}%
              </span>
              <span style={{ ...CAPTION, color: 'var(--accent)' }}>click to filter →</span>
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
              <span style={READOUT}>
                {seg.count === 0 ? '0× (not seeded)' : `${seg.count}×`} — {pct}%
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// Library-wide, so it doesn't follow the chart's tracker picks: a file's seed
// count is every tracker seeding it, whichever of them are on the chart.
export function CrossSeedCard({ stats, onNavigate }) {
  if (!stats) return null
  return (
    <div style={{ ...CARD, padding: '18px 20px', gap: 14 }}>
      <div>
        <div style={{ ...SECTION_LABEL, marginBottom: 8 }}>Cross-seed effectiveness</div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
          <HeroNumber value={stats.multiplier.toFixed(2)} unit="×" />
          <span style={CAPTION}>avg seed multiplier</span>
        </div>
      </div>
      <div>
        <div style={{ ...SECTION_LABEL, marginBottom: 8 }}>Disk space by seed count</div>
        <CrossSeedBar segments={stats.segments} totalSize={stats.total_size} onNavigate={onNavigate} />
      </div>
      <SharedPairs pairs={stats.pairs} totalSize={stats.total_size} />
    </div>
  )
}

// The tracker pairs that seed the most of the same library (2026-11-13).
function SharedPairs({ pairs, totalSize }) {
  const rows = (pairs || []).slice(0, 5)
  if (!rows.length) return null
  return (
    <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12 }}>
      <div style={{ ...SECTION_LABEL, marginBottom: 8 }}>Seeded together most</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {rows.map(p => (
          <div key={p.trackers.join('+')} style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, minWidth: 0 }}>
            <span style={{ ...READOUT, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
              {p.trackers[0]}<span style={{ color: 'var(--text-dim)' }}> + </span>{p.trackers[1]}
            </span>
            <span style={{ ...READOUT, flexShrink: 0 }}>
              {formatBytes(p.size)}{totalSize > 0 ? ` · ${((p.size / totalSize) * 100).toFixed(0)}%` : ''}
            </span>
          </div>
        ))}
      </div>
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
  const yieldPct      = yieldData?.yield != null ? (yieldData.yield * 100).toFixed(1) + '%' : '—'
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
    { key: 'yield',        label: 'Yield',        dataKey: 'yield_pct',         color: 'var(--accent)', fmt: v => v != null ? v.toFixed(1) + '%' : '—' },
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

  // The drill-downs to File Explorer: quiet full-width link rows, the same
  // ghost-with-no-hairline the dashboard cards' "just look" actions use.
  const NAV = { width: '100%', justifyContent: 'flex-start', fontWeight: 500, '--btn-border': 'transparent' }

  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', overflow: 'hidden', display: 'flex', flexDirection: 'column', flex: 1 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '16px 20px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-lg)', fontWeight: 700, color: 'var(--text)' }}>{trackerName}</span>
        {onClose && (
          <CloseButton onClick={onClose} />
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
                  background: active ? 'var(--surface3)' : 'var(--surface2)',
                  border: `1px solid ${active ? 'var(--border2)' : 'var(--border)'}`,
                  borderRadius: 'var(--r)', padding: '10px 14px',
                  cursor: 'pointer', transition: 'border-color 0.12s, background 0.12s',
                }}
              >
                <div style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', fontWeight: 600, color: 'var(--text-dim)', letterSpacing: 0, textTransform: 'none', marginBottom: 6, display: 'flex', alignItems: 'center', gap: 7 }}>
                  <span style={{ width: 6, height: 6, borderRadius: 99, background: s.color, flexShrink: 0 }} />
                  {s.label}
                </div>
                <div>{(() => {
                  const { num, unit } = splitValueUnit(s.value)
                  return <HeroNumber value={num} unit={unit} size={22} />
                })()}</div>
                <div style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginTop: 4 }}>{s.sub}</div>
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
                    <XAxis dataKey="date" tick={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} minTickGap={36} tickFormatter={fmtChartDate} />
                    <YAxis
                      width={44}
                      tick={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fill: 'var(--text-dim)' }}
                      tickLine={false} axisLine={false}
                      domain={chartTab === 'seeding' ? ['auto', 'auto'] : [0, 'auto']}
                      tickFormatter={chartTab === 'yield'
                        ? v => v != null ? v.toFixed(1) + '%' : ''
                        : formatBytesCompact}
                    />
                    <Tooltip
                      content={({ active, payload, label }) => {
                        if (!active || !payload?.length) return null
                        const val = payload[0].value
                        return (
                          <div style={{ background: '#151515', border: '1px solid #2a2a2a', borderRadius: 6, padding: '10px 14px', boxShadow: '0 8px 24px rgba(0,0,0,0.5)' }}>
                            <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginBottom: 4 }}>{fmtChartDate(label)}</div>
                            <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-md)', fontWeight: 700, color: '#ebebeb' }}>
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
            <div style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', textAlign: 'center', padding: '16px 0' }}>
              Upload data will appear after a few audits
            </div>
          )
        )}

        {/* Navigation buttons */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {seedingCount > 0 && (
            <Button variant="ghost" style={NAV}
              onClick={() => onNavigate({ tab: 'torrents', tracker: trackerName, status: 'Seeding' })}>
              View seeding files <span style={{ color: 'var(--text-dim)', fontSize: 'var(--font-sm)' }}>({seedingCount} files · {formatBytes(seedingSize)})</span>
            </Button>
          )}
          {orphanedCount > 0 && (
            <Button variant="ghost" style={NAV}
              onClick={() => onNavigate({ tab: 'torrents', tracker: trackerName, status: 'Orphaned' })}>
              View orphaned files <span style={{ color: 'var(--text-dim)', fontSize: 'var(--font-sm)' }}>({orphanedCount} files · {formatBytes(orphanedSize)})</span>
            </Button>
          )}
          {notImportedCount > 0 && (
            <Button variant="ghost" style={NAV}
              onClick={() => onNavigate({ tab: 'torrents', tracker: trackerName, importFilter: 'notImported' })}>
              View not imported <span style={{ color: 'var(--text-dim)', fontSize: 'var(--font-sm)' }}>({notImportedCount} files · {formatBytes(notImportedSize)})</span>
            </Button>
          )}
        </div>

      </div>
    </div>
  )
}

// ── Tracker detail modal (thin wrapper around TrackerCard) ────────────────────
export function TrackerDetailModal({ trackerName, trackerStats, uploadStats, onNavigate, onClose }) {
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

// ── Seeded only here ──────────────────────────────────────────────────────────
// Per tracker, the library it seeds, split by whether any other tracker seeds it
// too. The solid part is what would lose its last seed if the tracker went; on a
// hardlinked library that is the tracker's own contribution, not disk it costs,
// because the files are library either way. Follows the chart's tracker picks,
// and each bar is its tracker's chart colour. Solid against striped, so the two
// parts read apart without the colour.

const sharedFill = c => ({
  background: `repeating-linear-gradient(135deg, ${tint(c, 75)} 0 2px, transparent 2px 4.5px), ${tint(c, 22)}`,
})

function NoneSelected() {
  return (
    <div style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', textAlign: 'center', padding: '16px 0' }}>
      No trackers selected
    </div>
  )
}

function TrackerLink({ name, onClick }) {
  return (
    <button
      onClick={onClick} title={`${name}: details`}
      onMouseEnter={e => { e.currentTarget.style.color = 'var(--accent)' }}
      onMouseLeave={e => { e.currentTarget.style.color = 'var(--text)' }}
      style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-base)', fontWeight: 600, background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text)', padding: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0, textAlign: 'left', transition: 'color 0.15s' }}
    >{name}</button>
  )
}

function Figure({ value, word, onClick, title }) {
  const [hov, setHov] = useState(false)
  return (
    <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 4, whiteSpace: 'nowrap' }}>
      {onClick ? (
        <button onClick={onClick} title={title}
          onMouseEnter={() => setHov(true)} onMouseLeave={() => setHov(false)}
          style={{ ...READOUT, color: 'var(--text)', fontWeight: 600, background: 'none', border: 'none', padding: 0, cursor: 'pointer', textDecoration: hov ? 'underline' : 'none', textUnderlineOffset: 3 }}>
          {value}
        </button>
      ) : <span style={{ ...READOUT, color: 'var(--text)', fontWeight: 600 }}>{value}</span>}
      <span style={CAPTION}>{word}</span>
    </span>
  )
}

function PartSwatch({ striped }) {
  const c = 'var(--text-dim)'
  return <span style={{ width: 14, height: 8, borderRadius: 2, flexShrink: 0, display: 'inline-block', ...(striped ? sharedFill(c) : { background: c }) }} />
}

export function SeededOnlyHereCard({ trackerStats, trackers, colorFor, onTrackerDetail, onNavigate }) {
  const rows = (trackerStats || [])
    .filter(t => trackers.includes(t.name))
    .sort((a, b) => (b.only_size || 0) - (a.only_size || 0) || b.size - a.size)
  const max = Math.max(1, ...rows.map(r => r.size))
  return (
    <div style={{ ...CARD, padding: '18px 20px', gap: 14 }}>
      <div>
        <div style={{ ...SECTION_LABEL, marginBottom: 6 }}>Seeded only here</div>
        <div style={{ ...CAPTION, lineHeight: 1.5 }}>
          The library each tracker seeds, and how much of it no other tracker does: what would lose its last seed if the tracker went.
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px', marginTop: 10 }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, ...CAPTION }}><PartSwatch /> Only this tracker</span>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, ...CAPTION }}><PartSwatch striped /> Other trackers too</span>
        </div>
      </div>
      {rows.length === 0 ? <NoneSelected /> : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {rows.map(t => {
            const c = colorFor(t.name)
            const only = t.only_size || 0
            const shared = Math.max(0, t.size - only)
            return (
              <div key={t.name} style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, minWidth: 0 }}>
                  <TrackerLink name={t.name} onClick={() => onTrackerDetail(t.name)} />
                  <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 10, flexShrink: 0 }}>
                    <Figure value={formatBytes(only)} word="only here"
                      onClick={only > 0 && onNavigate ? () => onNavigate({ tab: 'media', tracker: t.name, seedCount: 1 }) : undefined}
                      title={`Media seeded only by ${t.name}`} />
                    <Figure value={formatBytes(shared)} word="shared" />
                  </span>
                </div>
                <div style={{ height: 8, borderRadius: 3, background: 'var(--surface3)', overflow: 'hidden', display: 'flex' }}>
                  <div style={{ width: `${(only / max) * 100}%`, background: c }} />
                  <div style={{ width: `${(shared / max) * 100}%`, ...sharedFill(c) }} />
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── Trackers reporting a problem ──────────────────────────────────────────────
// Only when one is: a tracker refusing its torrents (the client's "not working",
// with the tracker's own message) or ones it no longer lists (unregistered, the
// dead seeds Triage clears). Every tracker, whatever the chart shows. The
// verdict is per torrent, so a cross-seeded file's working sibling can't hide
// it, as it does on the file records.
export function TrackerHealthCard({ trackerFileStats, onNavigate }) {
  const count = s => (s.not_working_count || 0) + (s.unregistered_count || 0)
  const rows = Object.entries(trackerFileStats || {})
    .filter(([t, s]) => t !== 'Unknown' && count(s) > 0)
    .sort(([, a], [, b]) => count(b) - count(a))
  if (!rows.length) return null
  return (
    <div style={{ ...CARD, padding: '16px 20px', gap: 12 }}>
      <div style={SECTION_LABEL}>Trackers reporting a problem</div>
      {rows.map(([t, s]) => {
        const nw = s.not_working_count || 0, un = s.unregistered_count || 0, of = s.torrent_count || 0
        return (
          <div key={t} style={{ display: 'flex', alignItems: 'flex-start', gap: 12, paddingTop: 10, borderTop: '1px solid var(--border)' }}>
            <span style={{ width: 7, height: 7, borderRadius: '50%', marginTop: 6, flexShrink: 0, background: un ? 'var(--red)' : 'var(--yellow)' }} />
            <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ display: 'flex', alignItems: 'baseline', flexWrap: 'wrap', gap: '2px 12px' }}>
                <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-base)', fontWeight: 600, color: 'var(--text)' }}>{t}</span>
                {nw > 0 && (
                  <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 4 }}>
                    <span style={{ ...READOUT, color: 'var(--text)' }}>{nw} of {of}</span>
                    <span style={CAPTION}>torrents: the tracker isn't working</span>
                  </span>
                )}
                {un > 0 && (
                  <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 4 }}>
                    <span style={{ ...READOUT, color: 'var(--text)' }}>{un}</span>
                    <span style={CAPTION}>{un === 1 ? 'torrent' : 'torrents'} unregistered</span>
                  </span>
                )}
              </div>
              {s.health_msg && (
                <div title="The tracker's message, as the client reports it"
                  style={{ ...READOUT, lineHeight: 1.5, overflowWrap: 'anywhere' }}>{s.health_msg}</div>
              )}
            </div>
            <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
              {un > 0 && <Button variant="ghost" onClick={() => onNavigate({ tab: 'triage' })}>Open Triage</Button>}
              <Button variant="ghost" onClick={() => onNavigate({ tab: 'torrents', tracker: t })}>View torrents</Button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
