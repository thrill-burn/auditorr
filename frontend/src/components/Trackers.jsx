import React, { useState, useEffect, useMemo } from 'react'
import DatePicker from './DatePicker'
import { RangePresets } from './FilterBar'
import TrackerChart from './TrackerChart'
import CompareTable from './CompareTable'
import { api } from '../api'
import { formatBytes } from '../utils'
import { Segmented } from './workflows/shared'
import { CAPTION, TrendPill, Hero } from './dashKit'
import { CrossSeedCard, SeededOnlyHereCard, TrackerHealthCard, TrackerDetailModal } from './TrackerPanels'

// Categorical series palette — one stable hue per tracker. Color IS the data
// here: this is the one place auditorr spends hue freely (a chart series).
// The first seven are the ones the page has always used; the last three were
// added 2026-11-13, because an eighth tracker took the first one's orange.
// Assigned in name order, so a tracker keeps its colour as sizes change.
const TRACKER_COLORS = ['#f57c00', '#5b9bd9', '#46b56a', '#a78bd0', '#c99a2e', '#e5604d', '#9aa0a8', '#3fb8af', '#d46aa8', '#b3c24a']

// A chart metric, and the views it can be drawn in. `optional` metrics leave
// the switch and the table while every tracker reads zero over the range: on a
// healthy library Orphaned and Not Imported were two flat lines and two
// columns of "0 B".
const METRICS = [
  { key: 'seeding',      label: 'Seeding size', color: 'var(--green)',
    views: [{ value: 'size', label: 'Size' }, { value: 'change', label: 'Change' }] },
  { key: 'uploaded',     label: 'Uploaded',     color: 'var(--blue)',
    views: [{ value: 'daily', label: 'Daily' }, { value: 'total', label: 'Running total' }] },
  { key: 'yield',        label: 'Yield %',      color: 'var(--accent)' },
  { key: 'orphaned',     label: 'Orphaned',     color: 'var(--yellow)', optional: true },
  { key: 'not_imported', label: 'Not Imported', color: 'var(--red)',    optional: true },
]
const metricBy = (k) => METRICS.find(m => m.key === k)

// Rolling yield's window, in points (a point is a day with a scan).
const YIELD_WINDOW = 7

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const fmtDate = (ymd) => { const p = String(ymd).split('-'); return p.length === 3 ? `${MONTHS[+p[1] - 1]} ${+p[2]}` : ymd }

function initialDateFrom(timeRange) {
  if (!timeRange) return ''
  const d = new Date()
  d.setDate(d.getDate() - timeRange)
  return d.toISOString().slice(0, 10)
}

// Per-tracker daily history for every metric and view over the loaded snapshots:
//   daily_uploads        → uploaded bytes / day (Daily), and its running sum
//   daily_tracker_stats  → seeding / orphaned / not_imported, point-in-time;
//                          Seeding's Change is each day less the range's first
//   yield                → the last YIELD_WINDOW days' upload ÷ that day's seeding
// Point-in-time metrics carry forward across gap days; uploaded gaps are 0.
function buildHistory(uploadStats, trackers) {
  if (!uploadStats) return { dates: [], labels: [], series: {} }
  const dailyUploads = uploadStats.daily_uploads || []
  const dailyStats   = uploadStats.daily_tracker_stats || []

  const dateSet = new Set()
  dailyUploads.forEach(d => dateSet.add(d.date))
  dailyStats.forEach(d => dateSet.add(d.date))
  const dates = [...dateSet].sort()

  const upByDate = {}; dailyUploads.forEach(d => { upByDate[d.date] = d.by_tracker || {} })
  const ptByDate = {}; dailyStats.forEach(d => { ptByDate[d.date] = d.by_tracker || {} })

  const series = { 'seeding:size': {}, 'seeding:change': {}, 'uploaded:daily': {}, 'uploaded:total': {},
                   yield: {}, orphaned: {}, not_imported: {} }
  for (const t of trackers) {
    let lastSeed = 0, lastOrph = 0, lastNI = 0, run = 0
    const seeding = [], uploaded = [], total = [], yld = [], orphaned = [], notImported = []
    dates.forEach((date, i) => {
      const up = upByDate[date]?.[t] || 0
      const pt = ptByDate[date]?.[t]
      if (pt) { lastSeed = pt.seeding_size || 0; lastOrph = pt.orphaned_size || 0; lastNI = pt.not_imported_size || 0 }
      run += up
      uploaded.push(up); total.push(run)
      seeding.push(lastSeed); orphaned.push(lastOrph); notImported.push(lastNI)
      let win = 0
      for (let k = Math.max(0, i - YIELD_WINDOW + 1); k <= i; k++) win += uploaded[k]
      yld.push(lastSeed > 0 ? (win / lastSeed) * 100 : 0)
    })
    const first = seeding.length ? seeding[0] : 0
    series['seeding:size'][t]   = seeding
    series['seeding:change'][t] = seeding.map(v => v - first)
    series['uploaded:daily'][t] = uploaded
    series['uploaded:total'][t] = total
    series.yield[t]             = yld
    series.orphaned[t]          = orphaned
    series.not_imported[t]      = notImported
  }
  return { dates, labels: dates.map(fmtDate), series }
}

// How each metric and view is drawn.
function drawSpec(metric, view) {
  if (metric === 'uploaded') return view === 'total'
    ? { key: 'uploaded:total', kind: 'line', unit: 'bytes', baseline: true }
    : { key: 'uploaded:daily', kind: 'stack', unit: 'bytes', baseline: true }
  if (metric === 'seeding') return view === 'size'
    ? { key: 'seeding:size', kind: 'line', unit: 'bytes' }
    : { key: 'seeding:change', kind: 'line', unit: 'bytes', signed: true }
  if (metric === 'yield') return { key: 'yield', kind: 'line', unit: 'pct', baseline: true }
  return { key: metric, kind: 'line', unit: 'bytes', baseline: true }
}

// What a line view's end labels are, over their column: the table's figure,
// or what this view shows instead of it.
function labelTitle(key, first) {
  if (key === 'seeding:size') return 'Seeding size now'
  if (key === 'seeding:change') return first ? `Change since ${first}` : 'Change'
  if (key === 'uploaded:total') return 'Uploaded in range'
  if (key === 'yield') return `Yield, last ${YIELD_WINDOW} days`
  return 'Now'
}

const pct = v => (v == null ? '—' : (v * 100).toFixed(1) + '%')
const count = v => (v ? v.toLocaleString() : '0')

// The table's columns. `metric` ties one to the chart metric it shades.
const COLUMNS = [
  { key: 'seeding', label: 'Seeding size', metric: 'seeding', color: 'var(--green)',
    value: s => s.seeding_size || 0, display: s => formatBytes(s.seeding_size || 0),
    nav: t => ({ tab: 'torrents', tracker: t, status: 'Seeding' }), navTitle: t => `View ${t} seeding in Torrents` },
  { key: 'only', label: 'Seeded only here', title: 'Library this tracker seeds and no other tracker does',
    value: s => s.only_size ?? null, display: s => (s.only_size == null ? '—' : formatBytes(s.only_size)),
    nav: t => ({ tab: 'media', tracker: t, seedCount: 1 }), navTitle: t => `View media seeded only by ${t}` },
  { key: 'uploaded', label: 'Uploaded', metric: 'uploaded', color: 'var(--blue)',
    value: s => s.uploaded || 0, display: s => formatBytes(s.uploaded || 0) },
  { key: 'yield', label: 'Yield %', metric: 'yield', color: 'var(--accent)', title: 'Uploaded in the range ÷ seeding now',
    value: s => (s.yield == null ? null : s.yield), display: s => pct(s.yield) },
  { key: 'torrents', label: 'Torrents', title: 'Torrents the client has on this tracker',
    value: s => s.torrent_count ?? null, display: s => (s.torrent_count == null ? '—' : count(s.torrent_count)) },
  { key: 'orphaned', label: 'Orphaned', metric: 'orphaned', color: 'var(--yellow)', optional: true,
    value: s => s.orphaned_size || 0, display: s => formatBytes(s.orphaned_size || 0),
    nav: t => ({ tab: 'torrents', tracker: t, status: 'Orphaned' }), navTitle: t => `View ${t} orphaned in Torrents` },
  { key: 'not_imported', label: 'Not Imported', metric: 'not_imported', color: 'var(--red)', optional: true,
    value: s => s.not_imported_size || 0, display: s => formatBytes(s.not_imported_size || 0),
    nav: t => ({ tab: 'torrents', tracker: t, importFilter: 'notImported' }), navTitle: t => `View ${t} not imported in Torrents` },
]

// ── The chart's readout ──────────────────────────────────────────────────────
// The figure the chart is about, over the trackers on it. Uploaded's carries
// the comparison with the window before (Upload by tracker's total, moved here
// when the card went).
function Readout({ metric, view, uploadStats, statsMap, visible, history }) {
  const days = uploadStats?.period_days
  const sum = f => visible.reduce((s, t) => s + (f(statsMap[t] || {}) || 0), 0)
  let figure = null, caption = null, extra = null
  if (metric === 'uploaded') {
    const up = sum(s => s.uploaded)
    figure = formatBytes(up)
    caption = days ? `uploaded in ${days} day${days === 1 ? '' : 's'}` : 'uploaded'
    const prev = uploadStats?.previous
    if (prev) {
      const before = visible.reduce((s, t) => s + (prev.by_tracker?.[t] || 0), 0)
      if (before > 0) {
        const d = (up - before) / before
        const n = prev.days
        const detail = `${formatBytes(before)} on ${prev.from} to ${prev.to}`
        extra = Math.abs(d) < 0.01
          ? <TrendPill trend={{ sentiment: 'neutral', detail }} label={`about the same as the ${n} days before`} />
          : <TrendPill trend={{ sentiment: d > 0 ? 'good' : 'bad', detail }} icon={d > 0 ? '▲' : '▼'}
              label={`${Math.abs(d * 100).toFixed(0)}% ${d > 0 ? 'more' : 'less'} than the ${n} days before`} />
      }
    }
  } else if (metric === 'seeding') {
    figure = formatBytes(sum(s => s.seeding_size))
    caption = 'of torrents seeding now, counted once for each tracker'
    const ch = history.series['seeding:change']
    if (ch && history.dates.length > 1) {
      const delta = visible.reduce((s, t) => s + ((ch[t] || []).slice(-1)[0] || 0), 0)
      extra = <span style={CAPTION}>{delta === 0 ? 'no change' : `${delta > 0 ? '+' : '−'}${formatBytes(Math.abs(delta))}`} since {history.labels[0]}: torrents added, less those removed or replaced</span>
    }
  } else if (metric === 'yield') {
    const seed = sum(s => s.seeding_size), up = sum(s => s.uploaded)
    figure = seed > 0 ? (up / seed * 100).toFixed(1) + '%' : '—'
    caption = `yield over ${days || 0} day${days === 1 ? '' : 's'}: uploaded ÷ seeding`
    extra = <span style={CAPTION}>Each point is the yield of the {YIELD_WINDOW} days up to it</span>
  } else {
    figure = formatBytes(sum(s => (metric === 'orphaned' ? s.orphaned_size : s.not_imported_size)))
    caption = metric === 'orphaned' ? 'orphaned now' : 'not imported now'
  }
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', flexWrap: 'wrap', gap: '6px 14px', marginBottom: 14 }}>
      <Hero text={figure} />
      <span style={{ ...CAPTION, fontSize: 'var(--font-base)' }}>{caption}</span>
      {/* TrendPill pins itself to the top of a flex row; this wrapper is the
          row's item, so the pill sits on the caption's baseline. */}
      {extra && <span style={{ display: 'inline-flex', alignSelf: 'baseline' }}>{extra}</span>}
    </div>
  )
}

// Opens on the last 30 days. It used to open on the Dashboard's range, which is
// now Score history's own control.
export default function Trackers({ trackerFileStats, crossSeedStats, allTrackers, onNavigate }) {
  const [uploadStats, setUploadStats] = useState(null)
  const [trackerDetail, setTrackerDetail] = useState(null)
  const [dateFrom,    setDateFrom]    = useState(() => initialDateFrom(30))
  const [dateTo,      setDateTo]      = useState('')
  const [metricKey,   setMetricKey]   = useState('uploaded')
  // Seeding size opens on Size, the table's figure. It opened on Change, which
  // starts at 0 and climbs in GB, and read as a running total of upload: then
  // a torrent removed or replaced looked like upload going backwards (2026-11-13).
  const [views,       setViews]       = useState({ uploaded: 'daily', seeding: 'size' })
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

  const history = useMemo(() => buildHistory(uploadStats, allTrackers), [uploadStats, trackerKey])

  // Per-tracker scalars for the table: file stats, the source's torrent counts
  // and health, the library each seeds alone, and the period's upload/yield.
  const statsMap = useMemo(() => {
    const yields = {}
    for (const ty of (uploadStats?.tracker_yields || [])) yields[ty.tracker] = ty
    const lib = {}
    for (const ts of (crossSeedStats?.tracker_stats || [])) lib[ts.name] = ts
    const m = {}
    for (const t of allTrackers) {
      const fs = trackerFileStats?.[t] || {}
      const ty = yields[t] || {}
      const ls = lib[t]
      m[t] = {
        ...fs,
        seeding_size: fs.seeding_size || 0,
        orphaned_size: fs.orphaned_size || 0,
        not_imported_size: fs.not_imported_size || 0,
        // Absent before the scan that first counts it: a dash, not a zero.
        only_size: ls && ls.only_size != null ? ls.only_size : (ls ? null : 0),
        uploaded: ty.uploaded || 0,
        yield: ty.yield != null ? ty.yield : null,
      }
    }
    return m
  }, [trackerFileStats, crossSeedStats, uploadStats, trackerKey])

  // An optional metric shows while any tracker has any of it, now or in the range.
  const shown = useMemo(() => {
    const has = k => allTrackers.some(t => {
      const s = statsMap[t] || {}
      if ((k === 'orphaned' ? s.orphaned_size : s.not_imported_size) > 0) return true
      return (history.series[k]?.[t] || []).some(v => v > 0)
    })
    return Object.fromEntries(METRICS.map(m => [m.key, !m.optional || has(m.key)]))
  }, [statsMap, history, trackerKey])
  const metrics = METRICS.filter(m => shown[m.key])
  const columns = COLUMNS.filter(c => !c.optional || shown[c.key])
  const activeMetric = shown[metricKey] ? metricKey : 'uploaded'
  const activeSort = columns.some(c => c.key === sortKey) ? sortKey : 'seeding'

  const sortedRows = useMemo(() => {
    const col = COLUMNS.find(c => c.key === activeSort)
    const val = t => { const v = col.value(statsMap[t] || {}); return v == null ? -1 : v }
    return allTrackers.slice().sort((a, b) => sortDir === 'asc' ? val(a) - val(b) : val(b) - val(a))
  }, [trackerKey, activeSort, sortDir, statsMap])

  const colorFor = (t) => TRACKER_COLORS[Math.max(0, allTrackers.indexOf(t)) % TRACKER_COLORS.length]

  const metric = metricBy(activeMetric)
  const view = views[activeMetric] || metric.views?.[0]?.value
  const spec = drawSpec(activeMetric, view)
  const chartSeries = sortedRows.map(t => ({
    name: t, color: colorFor(t), visible: visible.includes(t),
    values: history.series[spec.key]?.[t] || [],
  }))

  const toggle    = (t) => setVisible(v => v.includes(t) ? v.filter(x => x !== t) : [...v, t])
  const solo      = (t) => setVisible(v => (v.length === 1 && v[0] === t) ? allTrackers.slice() : [t])
  const toggleAll = () => setVisible(v => v.length === allTrackers.length ? [] : allTrackers.slice())
  const sortBy    = (k) => { if (activeSort === k) setSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setSortKey(k); setSortDir('desc') } }

  const visCount = visible.length
  const hasHistory = history.labels.length > 0

  // Presets drive the same dateFrom/dateTo state as the pickers, so the two
  // controls stay in sync. A preset sets From = today−Nd (To cleared); "All"
  // clears both. The active preset is derived back from the dates — so editing
  // a picker into a custom range naturally deselects every preset.
  const activePreset = useMemo(() => {
    if (!dateFrom && !dateTo) return 0
    if (dateTo) return null
    for (const d of [7, 30, 90]) if (dateFrom === initialDateFrom(d)) return d
    return null
  }, [dateFrom, dateTo])
  const applyPreset = (d) => { setDateTo(''); setDateFrom(d === 0 ? '' : initialDateFrom(d)) }

  return (
    <div className="fade-in page-frame" style={{ padding: '24px 32px 64px', display: 'flex', flexDirection: 'column', gap: 18 }}>
      {/* Date range — quick presets (easy default) + precise From/To for drill-down.
          Both scope the chart and the table's Uploaded/Yield columns. */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <RangePresets isActive={v => activePreset === v} onSelect={applyPreset} />
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'var(--mono)', fontSize: 'var(--font-base)', color: 'var(--text-dim)' }}>
          <DatePicker value={dateFrom} onChange={setDateFrom} placeholder="From" align="right" />
          <span>—</span>
          <DatePicker value={dateTo} onChange={setDateTo} placeholder="To" align="right" />
        </div>
      </div>

      {/* Chart card */}
      <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', padding: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, marginBottom: 16, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            {/* The square swatch is the metric's colour, kept square so it
                reads as a series key rather than a status dot. */}
            <Segmented size="lg" label="Metric" value={activeMetric} onChange={setMetricKey}
              options={metrics.map(m => ({
                value: m.key, label: m.label,
                icon: <span style={{ width: 7, height: 7, borderRadius: 2, background: m.color, flexShrink: 0 }} />,
              }))} />
            {metric.views && (
              <Segmented size="lg" label="View" value={view}
                onChange={v => setViews(s => ({ ...s, [activeMetric]: v }))} options={metric.views} />
            )}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: visCount ? 'var(--green)' : 'var(--text-faint)' }} />
            <span><span style={{ fontFamily: 'var(--mono)', color: 'var(--text)', fontWeight: 600 }}>{visCount}</span> of <span style={{ fontFamily: 'var(--mono)' }}>{allTrackers.length}</span> trackers on chart</span>
          </div>
        </div>
        {hasHistory && (
          <Readout metric={activeMetric} view={view} uploadStats={uploadStats} statsMap={statsMap} visible={visible} history={history} />
        )}
        <div style={{ position: 'relative' }}>
          <TrackerChart series={chartSeries} dates={history.labels} kind={spec.kind} unit={spec.unit}
            signed={!!spec.signed} baseline={!!spec.baseline} hovered={hovered}
            labelTitle={labelTitle(spec.key, history.labels[0])} />
          {!hasHistory && (
            <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
              <span style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', color: 'var(--text-dim)' }}>Trend data will appear after a few audits</span>
            </div>
          )}
        </div>
      </div>

      {/* Comparison table (legend + selector) */}
      <CompareTable
        rows={sortedRows} statsMap={statsMap} columns={columns} metricKey={activeMetric} colorFor={colorFor}
        visible={visible} onToggle={toggle} onSolo={solo} onToggleAll={toggleAll}
        hovered={hovered} onHover={setHovered} sortKey={activeSort} sortDir={sortDir} onSort={sortBy}
        onNavigate={onNavigate}
      />

      <TrackerHealthCard trackerFileStats={trackerFileStats} onNavigate={onNavigate} />

      {/* Seeded only here follows the chart's tracker picks and takes each
          tracker's chart colour; Cross-seed effectiveness is library-wide. */}
      {crossSeedStats && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 520px), 1fr))', gap: 16 }}>
          <SeededOnlyHereCard trackerStats={crossSeedStats.tracker_stats} trackers={visible} colorFor={colorFor}
            onTrackerDetail={setTrackerDetail} onNavigate={onNavigate} />
          <CrossSeedCard stats={crossSeedStats} onNavigate={onNavigate} />
        </div>
      )}

      {trackerDetail && (
        <TrackerDetailModal
          trackerName={trackerDetail}
          trackerStats={trackerFileStats?.[trackerDetail]}
          uploadStats={uploadStats}
          onNavigate={(action) => { setTrackerDetail(null); onNavigate(action) }}
          onClose={() => setTrackerDetail(null)}
        />
      )}
    </div>
  )
}
