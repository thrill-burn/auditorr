import React, { useState } from 'react'
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { formatBytes, formatBytesCompact } from '../utils'
import { Button, CloseButton, Segmented } from './workflows/shared'
import { SECTION_LABEL, CARD, fmtChartDate, splitValueUnit, HeroNumber, Hero } from './dashKit'

// The tracker panels the Dashboard used to end with: the leaderboard, upload
// activity, and upload by tracker / library yield, with the detail popup they
// open. They moved to the Trackers page as they were (2026-11-09), where they
// follow that page's date range and the trackers its chart shows, and the
// Dashboard's tracker dropdown, which filtered nothing else, went.

// ── Upload activity tooltip ───────────────────────────────────────────────────
export function UploadActivityTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const items = payload.filter(p => p.value > 0)
  const total = payload.reduce((s, p) => s + (p.value || 0), 0)
  return (
    <div style={{ background: '#151515', border: '1px solid #2a2a2a', borderRadius: 6, padding: '10px 14px', boxShadow: '0 8px 24px rgba(0,0,0,0.5)', minWidth: 160 }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginBottom: 6 }}>{fmtChartDate(label)}</div>
      {items.map(p => (
        <div key={p.name} style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 3 }}>
          <div style={{ width: 8, height: 8, borderRadius: 2, background: p.fill, flexShrink: 0 }} />
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: '#ebebeb', flex: 1 }}>{p.name}</span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>{formatBytes(p.value)}</span>
        </div>
      ))}
      {items.length > 1 && (
        <div style={{ marginTop: 6, paddingTop: 6, borderTop: '1px solid #2a2a2a', display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>Total</span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fontWeight: 700, color: '#ebebeb' }}>{formatBytes(total)}</span>
        </div>
      )}
    </div>
  )
}

export function RoundedStackedBar({ x, y, width, height, fill, allTrackers, host, payload }) {
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


// ── Tracker leaderboard ───────────────────────────────────────────────────────
export function TrackerLeaderboard({ trackerStats, onTrackerDetail }) {
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
                <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fontWeight: 600, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '60%' }}>{t.name}</span>
                <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', flexShrink: 0 }}>{t.count} files · {formatBytes(t.size)}</span>
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
            <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', textAlign: 'center', padding: '16px 0' }}>
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

// ── The three panels ──────────────────────────────────────────────────────────
// `trackers` is the trackers to show (the Trackers page's chart picks) and
// `colorFor` the page's own hue for each, so a bar here is its chart line's
// colour. Each panel says so when the picks leave it nothing to draw.

function NoneSelected() {
  return (
    <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', textAlign: 'center', padding: '16px 0' }}>
      No trackers selected
    </div>
  )
}

const TABLE_HEAD = {
  padding: '4px 8px', color: 'var(--text-dim)', fontWeight: 600,
  fontFamily: 'var(--sans)', letterSpacing: 0, fontSize: 'var(--font-sm)', textTransform: 'none',
  borderBottom: '1px solid var(--border)',
}

function TrackerLink({ name, onClick }) {
  return (
    <button
      onClick={onClick}
      onMouseEnter={e => { e.currentTarget.style.color = 'var(--accent)' }}
      onMouseLeave={e => { e.currentTarget.style.color = 'var(--text)' }}
      style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text)', padding: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%', transition: 'color 0.15s' }}
    >{name}</button>
  )
}

export function TopTrackersCard({ trackerStats, trackers, onTrackerDetail }) {
  const rows = (trackerStats || []).filter(t => trackers.includes(t.name))
  return (
    <div style={{ ...CARD, padding: '18px 20px', gap: 14 }}>
      <div style={SECTION_LABEL}>Top trackers by disk space</div>
      {rows.length === 0 ? <NoneSelected /> : <TrackerLeaderboard trackerStats={rows} onTrackerDetail={onTrackerDetail} />}
      {rows.length > 3 && (
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {rows.slice(3).map(t => (
            <Button key={t.name} size="chip" variant="ghost" onClick={() => onTrackerDetail(t.name)}>
              {t.name}
            </Button>
          ))}
        </div>
      )}
    </div>
  )
}

export function UploadActivityCard({ uploadStats, trackers, colorFor }) {
  const active = Object.keys(
    (uploadStats?.daily_uploads || []).reduce((acc, day) => {
      Object.entries(day.by_tracker || {}).forEach(([h, v]) => { if (v > 0) acc[h] = true })
      return acc
    }, {})
  ).filter(h => trackers.includes(h))
  const data = (uploadStats?.daily_uploads || []).map(day => {
    const row = { date: day.date }
    for (const h of active) row[h] = day.by_tracker[h] || 0
    return row
  })
  return (
    <div style={{ ...CARD, padding: '18px 20px', gap: 14 }}>
      <div style={SECTION_LABEL}>Upload activity</div>
      {trackers.length === 0 ? <NoneSelected /> : (
        <div style={{ flex: 1, minHeight: 240 }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 4, right: 20, left: 4, bottom: 0 }}>
              <CartesianGrid strokeDasharray="2 4" stroke="var(--border)" strokeOpacity={0.6} vertical={false} />
              <XAxis dataKey="date" tick={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fill: 'var(--text-dim)' }} tickLine={false} axisLine={false} minTickGap={36} tickFormatter={fmtChartDate} />
              <YAxis
                width={44}
                tick={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fill: 'var(--text-dim)' }}
                tickLine={false} axisLine={false}
                tickFormatter={formatBytesCompact}
              />
              <Tooltip content={<UploadActivityTooltip />} cursor={{ fill: 'var(--surface2)' }} />
              {active.map(host => (
                <Bar
                  key={host} dataKey={host} stackId="uploads"
                  fill={colorFor(host)}
                  maxBarSize={80}
                  shape={props => <RoundedStackedBar {...props} allTrackers={active} host={host} />}
                />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  )
}

export function UploadByTrackerCard({ uploadStats, trackers, onTrackerDetail }) {
  const [tab, setTab] = useState('upload')
  const rows = (uploadStats?.tracker_yields || [])
    .filter(t => !(t.uploaded === 0 && (t.yield === null || t.yield === 0)))
    .filter(t => trackers.includes(t.tracker))
  const uploaded = rows.reduce((s, t) => s + t.uploaded, 0)
  const seeding  = rows.reduce((s, t) => s + t.seeding_size, 0)
  const days = uploadStats?.period_days
  return (
    <div style={{ ...CARD, padding: '18px 20px', gap: 14 }}>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
          <div style={SECTION_LABEL}>{tab === 'upload' ? 'Upload by tracker' : 'Library yield'}</div>
          <Segmented label="Panel" value={tab} onChange={setTab}
            options={[{ value: 'upload', label: 'Upload' }, { value: 'yield', label: 'Yield' }]} />
        </div>
        {tab === 'upload' ? (
          <>
            <Hero text={formatBytes(uploaded)} />
            <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginTop: 8, lineHeight: 1.6 }}>
              total uploaded · {days} day window
            </p>
          </>
        ) : (
          <>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <Hero text={seeding > 0 ? (uploaded / seeding * 100).toFixed(1) + '%' : '—'} />
              <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
                over {days} day{days !== 1 ? 's' : ''}
              </span>
            </div>
            <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginTop: 8, lineHeight: 1.6 }}>
              Upload volume relative to seeding size. Higher yield = your disk space is earning more.
            </p>
          </>
        )}
      </div>
      {trackers.length === 0 ? <NoneSelected /> : rows.length > 0 && (
        <div style={{ flex: 1, overflow: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)' }}>
            <thead>
              <tr>
                {(tab === 'yield' ? ['Tracker', 'Uploaded', 'Seeding', 'Yield'] : ['Tracker', 'Total Uploaded']).map(h => (
                  <th key={h} style={{ ...TABLE_HEAD, textAlign: h === 'Tracker' ? 'left' : 'right' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(tab === 'yield' ? rows : [...rows].sort((a, b) => b.uploaded - a.uploaded)).map((t, i) => (
                <tr key={t.tracker} style={{ background: i % 2 === 0 ? 'var(--surface2)' : 'transparent' }}>
                  <td style={{ padding: '5px 8px', maxWidth: 120 }}>
                    <TrackerLink name={t.tracker} onClick={() => onTrackerDetail(t.tracker)} />
                  </td>
                  <td style={{ padding: '5px 8px', color: 'var(--text-dim)', textAlign: 'right' }}>{formatBytes(t.uploaded)}</td>
                  {tab === 'yield' && <>
                    <td style={{ padding: '5px 8px', color: 'var(--text-dim)', textAlign: 'right' }}>{formatBytes(t.seeding_size)}</td>
                    <td style={{ padding: '5px 8px', textAlign: 'right', fontWeight: t.yield > 0 ? 600 : 400, color: t.yield > 0 ? 'var(--green)' : 'var(--text-dim)' }}>
                      {t.yield !== null ? (t.yield * 100).toFixed(1) + '%' : '—'}
                    </td>
                  </>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
