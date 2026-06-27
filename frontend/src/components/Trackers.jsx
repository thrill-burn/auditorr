import React, { useState, useEffect, useMemo } from 'react'
import DatePicker from './DatePicker'
import MultiLineChart from './MultiLineChart'
import CompareTable from './CompareTable'
import { api } from '../api'

// Categorical series palette — one stable hue per tracker. Color IS the data
// here: this is the one place auditorr spends hue freely (a chart series).
const TRACKER_COLORS = ['#f57c00', '#5b9bd9', '#46b56a', '#a78bd0', '#c99a2e', '#e5604d', '#9aa0a8']

// Metric = a chart series spec + a table column. `nav` maps a metric to the
// matching filtered Torrents view (null = no file view for that metric, so its
// value cells aren't drill-downs).
const METRICS = [
  { key: 'seeding',      label: 'Seeding',      color: 'var(--green)',  unit: 'bytes', baseline: false, nav: { status: 'Seeding' } },
  { key: 'uploaded',     label: 'Uploaded',     color: 'var(--blue)',   unit: 'bytes', baseline: true,  nav: null },
  { key: 'yield',        label: 'Yield %',      color: 'var(--accent)', unit: 'pct',   baseline: false, nav: null },
  { key: 'orphaned',     label: 'Orphaned',     color: 'var(--yellow)', unit: 'bytes', baseline: true,  nav: { status: 'Orphaned' } },
  { key: 'not_imported', label: 'Not Imported', color: 'var(--red)',    unit: 'bytes', baseline: true,  nav: { importFilter: 'notImported' } },
]
const metricBy = (k) => METRICS.find(m => m.key === k)

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const fmtDate = (ymd) => { const p = String(ymd).split('-'); return p.length === 3 ? `${MONTHS[+p[1] - 1]} ${+p[2]}` : ymd }

function initialDateFrom(timeRange) {
  if (!timeRange) return ''
  const d = new Date()
  d.setDate(d.getDate() - timeRange)
  return d.toISOString().slice(0, 10)
}

// Build per-tracker daily history for every metric over the loaded snapshots.
// Replaces the prototype's synthesized buildSeries() with the real data layer:
//   daily_uploads        → per-tracker uploaded bytes / day
//   daily_tracker_stats  → per-tracker seeding/orphaned/not_imported point-in-time
//   yield                → daily uploaded ÷ daily seeding
// Point-in-time metrics carry forward across gap days; uploaded gaps are 0.
function buildHistory(uploadStats, trackers) {
  if (!uploadStats) return { dates: [], labels: [], seriesByMetric: {} }
  const dailyUploads = uploadStats.daily_uploads || []
  const dailyStats   = uploadStats.daily_tracker_stats || []

  const dateSet = new Set()
  dailyUploads.forEach(d => dateSet.add(d.date))
  dailyStats.forEach(d => dateSet.add(d.date))
  const dates = [...dateSet].sort()

  const upByDate = {}; dailyUploads.forEach(d => { upByDate[d.date] = d.by_tracker || {} })
  const ptByDate = {}; dailyStats.forEach(d => { ptByDate[d.date] = d.by_tracker || {} })

  const seriesByMetric = { seeding: {}, uploaded: {}, yield: {}, orphaned: {}, not_imported: {} }
  for (const t of trackers) {
    let lastSeed = 0, lastOrph = 0, lastNI = 0
    const seeding = [], uploaded = [], yld = [], orphaned = [], notImported = []
    for (const date of dates) {
      const up = upByDate[date]?.[t] || 0
      const pt = ptByDate[date]?.[t]
      if (pt) { lastSeed = pt.seeding_size || 0; lastOrph = pt.orphaned_size || 0; lastNI = pt.not_imported_size || 0 }
      uploaded.push(up)
      seeding.push(lastSeed)
      orphaned.push(lastOrph)
      notImported.push(lastNI)
      yld.push(lastSeed > 0 ? (up / lastSeed) * 100 : 0)
    }
    seriesByMetric.seeding[t]      = seeding
    seriesByMetric.uploaded[t]     = uploaded
    seriesByMetric.yield[t]        = yld
    seriesByMetric.orphaned[t]     = orphaned
    seriesByMetric.not_imported[t] = notImported
  }
  return { dates, labels: dates.map(fmtDate), seriesByMetric }
}

// ── Metric switcher (segmented, mutually exclusive) ──────────────────────────
function MetricSwitch({ value, onChange }) {
  return (
    <div style={{ display: 'inline-flex', background: 'var(--surface3)', border: '1px solid var(--border2)', borderRadius: 7, padding: 2, gap: 2, flexWrap: 'wrap' }}>
      {METRICS.map(m => {
        const active = value === m.key
        return (
          <button key={m.key} onClick={() => onChange(m.key)} style={{
            padding: '6px 14px', fontSize: 13, fontWeight: active ? 600 : 500, borderRadius: 5, border: 'none',
            background: active ? 'var(--surface)' : 'transparent', boxShadow: active ? 'var(--elev-1)' : 'none',
            color: active ? 'var(--text)' : 'var(--text-dim)',
            display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap', transition: 'all 0.12s',
          }}>
            <span style={{ width: 7, height: 7, borderRadius: 2, background: m.color }} />
            {m.label}
          </button>
        )
      })}
    </div>
  )
}

export default function Trackers({ trackerFileStats, allTrackers, timeRange, onNavigate }) {
  const [uploadStats, setUploadStats] = useState(null)
  const [dateFrom,    setDateFrom]    = useState(() => initialDateFrom(timeRange))
  const [dateTo,      setDateTo]      = useState('')
  const [metricKey,   setMetricKey]   = useState('uploaded')
  const [visible,     setVisible]     = useState(() => allTrackers.slice())
  const [hovered,     setHovered]     = useState(null)
  const [sortKey,     setSortKey]     = useState('seeding')
  const [sortDir,     setSortDir]     = useState('desc')

  const trackerKey = allTrackers.join('|')

  useEffect(() => {
    const params = (dateFrom || dateTo) ? { from: dateFrom || undefined, to: dateTo || undefined } : 0
    api.uploadStats(params).then(d => { if (!d.status) setUploadStats(d) }).catch(() => {})
  }, [dateFrom, dateTo])

  // Reset line visibility to "all" only when the tracker set genuinely changes
  // (poll refreshes produce the same trackerKey, so user toggles are preserved).
  useEffect(() => { setVisible(allTrackers.slice()) }, [trackerKey])

  const { labels, seriesByMetric } = useMemo(
    () => buildHistory(uploadStats, allTrackers),
    [uploadStats, trackerKey],
  )

  // Per-tracker scalars for the table: file stats + period uploaded/yield.
  const statsMap = useMemo(() => {
    const yields = {}
    for (const ty of (uploadStats?.tracker_yields || [])) yields[ty.tracker] = ty
    const m = {}
    for (const t of allTrackers) {
      const fs = trackerFileStats?.[t] || {}
      const ty = yields[t] || {}
      m[t] = {
        seeding_size: fs.seeding_size || 0,
        orphaned_size: fs.orphaned_size || 0,
        not_imported_size: fs.not_imported_size || 0,
        uploaded: ty.uploaded || 0,
        yield: ty.yield != null ? ty.yield : null,
      }
    }
    return m
  }, [trackerFileStats, uploadStats, trackerKey])

  const sortValue = (t, key) => {
    const s = statsMap[t] || {}
    if (key === 'seeding')      return s.seeding_size || 0
    if (key === 'uploaded')     return s.uploaded || 0
    if (key === 'yield')        return s.yield ?? -1
    if (key === 'orphaned')     return s.orphaned_size || 0
    if (key === 'not_imported') return s.not_imported_size || 0
    return 0
  }

  const sortedRows = useMemo(() => allTrackers.slice().sort((a, b) => {
    const va = sortValue(a, sortKey), vb = sortValue(b, sortKey)
    return sortDir === 'asc' ? va - vb : vb - va
  }), [trackerKey, sortKey, sortDir, statsMap])

  const colorFor = (t) => TRACKER_COLORS[Math.max(0, allTrackers.indexOf(t)) % TRACKER_COLORS.length]

  const metric = metricBy(metricKey)
  const chartSeries = sortedRows.map(t => ({
    name: t, color: colorFor(t), visible: visible.includes(t),
    values: seriesByMetric[metricKey]?.[t] || [],
  }))

  const toggle    = (t) => setVisible(v => v.includes(t) ? v.filter(x => x !== t) : [...v, t])
  const solo      = (t) => setVisible(v => (v.length === 1 && v[0] === t) ? allTrackers.slice() : [t])
  const toggleAll = () => setVisible(v => v.length === allTrackers.length ? [] : allTrackers.slice())
  const sortBy    = (k) => { if (sortKey === k) setSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setSortKey(k); setSortDir('desc') } }

  const visCount = visible.length
  const hasHistory = labels.length > 0

  const pill = { padding: '4px 10px', borderRadius: 6, border: '1px solid var(--border2)', background: 'var(--surface2)' }

  return (
    <div className="fade-in" style={{ padding: '24px 32px 64px', display: 'flex', flexDirection: 'column', gap: 18 }}>
      {/* Date range — scopes both the chart and the table's Uploaded/Yield columns */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8, fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-dim)' }}>
        <DatePicker value={dateFrom} onChange={setDateFrom} placeholder="From" />
        <span>—</span>
        <DatePicker value={dateTo} onChange={setDateTo} placeholder="To" />
      </div>

      {/* Chart card */}
      <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', padding: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, marginBottom: 18, flexWrap: 'wrap' }}>
          <MetricSwitch value={metricKey} onChange={setMetricKey} />
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--text-dim)' }}>
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: visCount ? 'var(--green)' : 'var(--text-faint)' }} />
            <span><span style={{ color: 'var(--text)', fontWeight: 600 }}>{visCount}</span> of {allTrackers.length} trackers on chart</span>
          </div>
        </div>
        <div style={{ position: 'relative' }}>
          <MultiLineChart series={chartSeries} dates={labels} unit={metric.unit} baseline={metric.baseline} hovered={hovered} />
          {!hasHistory && (
            <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-dim)' }}>Trend data will appear after a few audits</span>
            </div>
          )}
        </div>
      </div>

      {/* Comparison table (legend + selector) */}
      <CompareTable
        rows={sortedRows} statsMap={statsMap} metrics={METRICS} metricKey={metricKey} colorFor={colorFor}
        visible={visible} onToggle={toggle} onSolo={solo} onToggleAll={toggleAll}
        hovered={hovered} onHover={setHovered} sortKey={sortKey} sortDir={sortDir} onSort={sortBy}
        onNavigate={onNavigate}
      />
    </div>
  )
}
