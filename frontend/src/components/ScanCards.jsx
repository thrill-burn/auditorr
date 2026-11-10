import React from 'react'
import { tint } from '../utils'
import { Button } from './workflows/shared'
import { SECTION_LABEL, CARD, fmtChartDate, HeroNumber } from './dashKit'

// Two of the Dashboard's disk-and-links cards (2026-11-09).
//
// **Last scan** is R1 on the page. Every scan has stored what it could see
// since S03 and C2 — instances that answered, file lists read, folders walked,
// what couldn't be listed — and only /api/debug/report read it, so a refused
// scan left the Dashboard showing the scan before it with nothing on it saying
// so. A refused or failed scan also puts a line in the alert row at the top.
//
// **New in the library** is the leading indicator the Hardlinked % can't be:
// that number moves slowly, so an import setup that stopped linking (an arr
// whose two folders became separate mounts) shows only as a slow drift. This
// counts each scan's new library files by how they arrived, and a copy of a
// torrent file says the link wasn't made. It never says who made the copy.

const META = { fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
export function fmtWhen(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// `audit._ACCEPTABLE_BY_HAND`: the refusals that describe a change in what the
// client or the disk holds, which a manual scan may accept. Every other one is
// a read that failed, and a manual scan refuses it too.
const ACCEPTABLE_BY_HAND = new Set(['torrent_count_collapse', 'file_map_collapse', 'client_blackout', 'disk_collapse'])

export function scanState(sr) {
  const run = sr?.last_run
  if (!run) return null
  if (run.status === 'anomaly') return { label: 'Refused', color: 'var(--red)', bad: true }
  if (run.status === 'error')   return { label: 'Failed',  color: 'var(--red)', bad: true }
  if (run.status === 'aborted') return { label: 'Stopped', color: 'var(--red)', bad: true }
  if (sr.report?.partial)       return { label: 'Partial', color: 'var(--yellow)' }
  return { label: 'Complete', color: 'var(--green)' }
}

// The alert-row line for a scan that didn't save, or saved without reading
// everything. Null when the last scan was complete.
export function scanAlert(sr, savedAt) {
  const st = scanState(sr)
  if (!st) return null
  const from = savedAt ? `, so these numbers are from ${fmtWhen(savedAt)}` : ''
  if (sr.last_run.status === 'anomaly')
    return { color: 'var(--red)', msg: `The last scan was refused${from}. ${sr.anomaly?.message || ''}`.trim() }
  if (sr.last_run.status === 'error')
    return { color: 'var(--red)', msg: `The last scan failed${from}.` }
  if (sr.last_run.status === 'aborted')
    return { color: 'var(--red)', msg: `The last scan was stopped partway${from}.` }
  if (sr.report?.listing_unresolved > 0)
    return { color: 'var(--yellow)', msg: `The last scan couldn't read ${sr.report.listing_unresolved.toLocaleString()} torrent file list${sr.report.listing_unresolved !== 1 ? 's' : ''}, so their folders weren't checked.` }
  return null
}

function Warn({ children }) {
  return <span style={{ color: 'var(--yellow)' }}>{children}</span>
}

function plural(n, one, many = one + 's') { return `${n.toLocaleString()} ${n === 1 ? one : many}` }

function walkLine(block) {
  if (!block) return null
  if (block.configured === false) return 'not set'
  if (block.exists === false) return <Warn>not found</Warn>
  const parts = [`${(block.files || 0).toLocaleString()} files`]
  return (
    <>
      {parts.join(' · ')} · {block.unlistable
        ? <Warn>{plural(block.unlistable, 'folder')} unreadable</Warn>
        : 'nothing unreadable'}
      {block.stat_errors > 0 && <> · <Warn>{plural(block.stat_errors, 'file')} unreadable</Warn></>}
    </>
  )
}

function runsLine(counts) {
  const c = counts || {}
  const total = Object.values(c).reduce((a, b) => a + b, 0)
  if (!total) return null
  const parts = [
    c.anomaly && `${c.anomaly} refused`, c.error && `${c.error} failed`, c.aborted && `${c.aborted} stopped`,
  ].filter(Boolean)
  return `${plural(total, 'scan')} in the last 30 days · ${parts.length ? parts.join(', ') : 'all saved'}`
}

export function LastScanCard({ id, scanReport, details, savedAt, onScan }) {
  const sr = scanReport
  const st = scanState(sr)
  const run = sr?.last_run
  const rep = sr?.report
  const d = details || {}
  const bad = st?.bad
  const fs = rep?.filesystem || {}
  const rows = rep ? [
    ['Torrent client', `${rep.source === 'qbit' ? 'qBittorrent' : rep.source || '—'} · ${rep.instances_ok ?? 0} of ${plural(rep.instances_total ?? 0, 'instance')} answered`],
    ['Torrents', <>{(rep.torrent_count || 0).toLocaleString()} · {rep.listing_unresolved
      ? <Warn>{rep.listing_recovered || 0} read from disk, {rep.listing_unresolved} couldn't be read</Warn>
      : rep.listing_recovered ? `${plural(rep.listing_recovered, 'file list')} read from disk` : 'every file list read'}</>],
    ['Torrent folder', walkLine(fs.torrents)],
    ['Library folder', walkLine(fs.media)],
    ['Tracker stats', rep.tracker_failures
      ? <Warn>{plural(rep.tracker_failures, 'torrent')}' trackers didn't answer</Warn> : 'every tracker answered'],
    ...('empty_folders_unknown' in d ? [['Empty folders', d.empty_folders_unknown
      ? <span title="The client's save folders didn't load, so Cleanup offers no empty folder rather than guess at one."><Warn>not looked for</Warn></span>
      : `${(d.empty_folder_count || 0).toLocaleString()} found`]] : []),
    ...(d.cleanup_unchecked_count > 0 ? [['Unchecked', <Warn>{plural(d.cleanup_unchecked_count, 'folder')} whose file list didn't load</Warn>]] : []),
  ] : []
  const runs = runsLine(sr?.runs_30d)

  return (
    <div id={id} style={{ ...CARD, padding: '18px 20px 16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, marginBottom: 4 }}>
        <span style={SECTION_LABEL}>Last scan</span>
        {st && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fontWeight: 600, color: st.color, display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <span className="ui-status-dot" style={{ background: st.color }} />{st.label}
          </span>
        )}
      </div>
      {!sr || !run ? (
        <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: '8px 0 0' }}>
          No scan has been recorded yet.
        </p>
      ) : (
        <>
          {bad ? (
            <div style={{ padding: '10px 12px', borderRadius: 8, background: tint('var(--red)', 6), border: `1px solid ${tint('var(--red)', 19)}`, margin: '8px 0 12px' }}>
              <p style={{ margin: 0, fontSize: 'var(--font-base)', color: 'var(--text)', lineHeight: 1.5 }}>
                {run.status === 'anomaly' ? (sr.anomaly?.message || 'The scan was refused.') : (run.error_message || 'The scan did not finish.')}
                {' '}Nothing from this scan was saved.
                {run.status === 'anomaly' && ACCEPTABLE_BY_HAND.has(sr.anomaly?.code) && ' If the drop is real, a manual scan accepts it.'}
              </p>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginTop: 6 }}>
                <span style={META}>{fmtWhen(run.ran_at)} · {run.trigger}</span>
                {onScan && run.status === 'anomaly' && ACCEPTABLE_BY_HAND.has(sr.anomaly?.code) && (
                  <Button size="sm" onClick={onScan}>Scan now</Button>
                )}
              </div>
            </div>
          ) : null}
          <div style={{ ...META, marginBottom: 10 }}>
            {bad
              ? `The numbers on this page · ${fmtWhen(savedAt)}`
              : `${fmtWhen(run.ran_at)} · ${run.trigger}${run.duration_seconds != null ? ` · ${run.duration_seconds} s` : ''}${run.peak_rss_mb != null ? ` · ${run.peak_rss_mb} MB` : ''}`}
          </div>
          {rows.length > 0 ? (
            <dl style={{ display: 'grid', gridTemplateColumns: '100px minmax(0, 1fr)', gap: '5px 12px', alignItems: 'baseline', margin: 0 }}>
              {rows.map(([k, v]) => (
                <React.Fragment key={k}>
                  <dt style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)' }}>{k}</dt>
                  <dd style={{ margin: 0, fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text)' }}>{v}</dd>
                </React.Fragment>
              ))}
            </dl>
          ) : (
            <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: 0 }}>
              The next scan records what it could see here.
            </p>
          )}
          {runs && (
            <div style={{ ...META, borderTop: '1px solid var(--border)', marginTop: 'auto', paddingTop: 12 }}>{runs}</div>
          )}
        </>
      )}
    </div>
  )
}

// ── New in the library ───────────────────────────────────────────────────────
const KINDS = [
  { key: 'linked',   label: 'linked',                   color: 'var(--map-linked)' },
  { key: 'unseeded', label: 'not seeded',               color: 'var(--blue)' },
  { key: 'copies',   label: 'copies of a torrent file', color: 'var(--purple)' },
]

function isoDay(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export function NewInLibraryCard({ uploadStats, timeRange, onNavigate }) {
  const series = uploadStats?.daily_new_media || []
  const sum = { linked: 0, copies: 0, unseeded: 0 }
  for (const d of series) for (const k of Object.keys(sum)) sum[k] += d[k] || 0
  const total = sum.linked + sum.copies + sum.unseeded
  const firstCopy = series.find(d => d.copies > 0)

  // One column per calendar day from the first counted day in range: a day
  // before counting began is absent, never drawn as zero.
  const cols = []
  if (series.length) {
    const byDay = Object.fromEntries(series.map(d => [d.date, d]))
    const start = new Date(series[0].date + 'T12:00:00')
    const end = new Date()
    for (let d = new Date(start); isoDay(d) <= isoDay(end); d.setDate(d.getDate() + 1)) {
      const k = isoDay(d)
      cols.push(byDay[k] || { date: k, linked: 0, copies: 0, unseeded: 0 })
    }
  }
  const max = Math.max(1, ...cols.map(c => c.linked + c.copies + c.unseeded))
  const rangeStart = timeRange ? (() => { const d = new Date(); d.setDate(d.getDate() - timeRange); return isoDay(d) })() : null
  const lateStart = series.length && rangeStart && series[0].date > rangeStart

  return (
    <div style={{ ...CARD, padding: '18px 20px 16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, marginBottom: 8 }}>
        <span style={SECTION_LABEL}>New in the library</span>
        <span style={META}>{timeRange ? `last ${timeRange} days` : 'all time'}</span>
      </div>
      {!series.length ? (
        <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: 0, lineHeight: 1.6 }}>
          Counting starts with the next scan: each one counts the library files that are new since the scan before it, and whether they arrived hardlinked.
        </p>
      ) : (
        <>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
            <HeroNumber value={sum.linked.toLocaleString()} />
            <span style={META}>of {total.toLocaleString()} new file{total !== 1 ? 's' : ''} hardlinked</span>
          </div>
          <div style={{ minHeight: 21, marginTop: 10, display: 'flex' }}>
            {sum.copies > 0 ? (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 'var(--font-base)', fontWeight: 600, color: 'var(--red)', lineHeight: 1.35 }}>
                <span style={{ fontSize: 'var(--font-sm)', lineHeight: 1 }}>▼</span>
                {plural(sum.copies, 'copy', 'copies')} since {fmtChartDate(firstCopy.date)}: new files aren't all being linked
              </span>
            ) : total > 0 ? (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 'var(--font-base)', fontWeight: 600, color: 'var(--green)', lineHeight: 1.35 }}>
                <span style={{ fontSize: 'var(--font-sm)', lineHeight: 1 }}>✓</span>
                No copies: new files are being linked
              </span>
            ) : (
              <span style={META}>Nothing new in this range</span>
            )}
          </div>
          <svg viewBox={`0 0 ${Math.max(cols.length, 1)} 50`} preserveAspectRatio="none" width="100%" height="50"
            style={{ display: 'block', marginTop: 12, borderBottom: '1px solid var(--border)' }} aria-hidden="true">
            {cols.map((c, i) => {
              let y = 50
              return KINDS.map(k => {
                const v = c[k.key] || 0
                if (!v) return null
                const h = v / max * 48
                y -= h
                return (
                  <rect key={k.key + c.date} x={i + 0.15} y={y} width={0.7} height={h} fill={k.color}>
                    <title>{`${fmtChartDate(c.date)}: ${c.linked} linked, ${c.unseeded} not seeded, ${c.copies} copies`}</title>
                  </rect>
                )
              })
            })}
          </svg>
          <div style={{ ...META, display: 'flex', justifyContent: 'space-between', marginTop: 2 }}>
            <span>{cols.length ? fmtChartDate(cols[0].date) : ''}</span>
            <span>{cols.length ? fmtChartDate(cols[cols.length - 1].date) : ''}</span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 14px', marginTop: 8 }}>
            {KINDS.map(k => (
              <span key={k.key} style={{ ...META, display: 'inline-flex', alignItems: 'center', gap: 6, opacity: sum[k.key] ? 1 : 0.55 }}
                title={k.key === 'copies' ? 'A library file with the same bytes as a torrent file but its own inode, so it takes the space twice. Who made the copy isn\'t known.' : undefined}>
                <span style={{ width: 9, height: 9, borderRadius: 2, background: k.color, flexShrink: 0 }} />
                <span style={{ color: 'var(--text)', fontWeight: 500 }}>{sum[k.key].toLocaleString()}</span> {k.label}
              </span>
            ))}
          </div>
          {lateStart && (
            <div style={{ ...META, marginTop: 6 }}>Counted since {fmtChartDate(series[0].date)}.</div>
          )}
          {sum.copies > 0 && onNavigate && (
            <div style={{ marginTop: 'auto', paddingTop: 14 }}>
              <Button onClick={() => onNavigate({ tab: 'dedupe' })} style={{ gap: 7 }}>
                <span style={{ display: 'inline-flex', color: 'var(--purple)' }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                </span>
                Dedupe
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
