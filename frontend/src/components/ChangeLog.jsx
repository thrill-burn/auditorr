import React, { useState, useEffect, useMemo, useCallback } from 'react'
import DatePicker from './DatePicker'
import { FixedSizeList } from 'react-window'
import AutoSizer from 'react-virtualized-auto-sizer'
import { api } from '../api'
import { formatBytes, tint } from '../utils'
import { CHANGE_CATEGORIES } from './changeCategories'
import { Button, Segmented, SearchInput, Dot } from './workflows/shared'

function useDebounce(value, delay) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(t)
  }, [value, delay])
  return debounced
}

const TRIGGER_LABELS = {
  watchdog:  'watchdog',
  scheduled: 'scheduled',
  manual:    'manual',
  startup:   'startup',
}

const ROW_HEIGHT = 36
const COL_HEADER_HEIGHT = 28

function fmtDate(iso) {
  const d = new Date(iso)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function fmtDuration(s) {
  if (s == null) return null
  if (s < 60) return `${Math.round(s)}s`
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
}

// ─── Virtual row ─────────────────────────────────────────────────────────────

function ChangeRow({ index, style, data }) {
  const row = data.rows[index]
  const { onNavigate } = data
  return (
    <div style={{
      ...style,
      display: 'grid',
      gridTemplateColumns: '200px 80px 120px 1fr 72px',
      alignItems: 'center',
      padding: '0 16px',
      borderBottom: '1px solid var(--border)',
      background: 'var(--surface)',
      boxSizing: 'border-box',
      overflow: 'hidden',
    }}>
      {/* Date */}
      <div style={{ overflow: 'hidden', paddingRight: 8 }}>
        <span style={{
          fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text)',
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', display: 'block',
        }}>
          {fmtDate(row.ran_at)}
          {fmtDuration(row.duration_seconds) && (
            <span style={{ color: 'var(--text-dim)', opacity: 0.6, fontSize: 'var(--font-sm)' }}>
              {' · '}{fmtDuration(row.duration_seconds)}
            </span>
          )}
        </span>
      </div>
      {/* Trigger and Type are text, not boxes (the user's pick from
          `.internal/preview/kitchoices.html`, 2026-09-23). They were outlined
          rectangles with bold text — the shape of a row's clickable chip — on
          cells nothing can click, beside a path that can be clicked and had no
          box. Type is the category filter's own dot and label; a box on every
          row of a long list is more hue than ration-colour allows, which is
          File Explorer's rule for its row tags too. */}
      <div>
        {row.trigger && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', whiteSpace: 'nowrap' }}>
            {TRIGGER_LABELS[row.trigger] ?? row.trigger}
          </span>
        )}
      </div>
      <div>
        <span style={{
          fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', fontWeight: 500, color: 'var(--text)',
          whiteSpace: 'nowrap', display: 'inline-flex', alignItems: 'center', gap: 7,
        }}>
          <span className="ui-status-dot" style={{ width: 6, height: 6, background: row.cat.color }} />
          {row.cat.label}
        </span>
      </div>
      {/* Path */}
      <div style={{ overflow: 'hidden', paddingRight: 8 }}>
        <span
          title={row.path}
          onClick={() => onNavigate && onNavigate(row.path, row.tab)}
          style={{
            fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)',
            display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            cursor: onNavigate ? 'pointer' : 'default',
            textDecoration: 'none',
          }}
          onMouseEnter={e => { if (onNavigate) e.currentTarget.style.color = 'var(--text)' }}
          onMouseLeave={e => { e.currentTarget.style.color = 'var(--text-dim)' }}
        >
          {row.path}
        </span>
      </div>
      {/* Size */}
      <div style={{ textAlign: 'right' }}>
        {row.size != null && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
            {formatBytes(row.size)}
          </span>
        )}
      </div>
    </div>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function ChangeLog({ onNavigate }) {
  const [entries,   setEntries]   = useState(null)
  const [error,     setError]     = useState(null)
  const [catFilter, setCatFilter] = useState([]) // selected category keys; empty = all
  const [dateFrom,  setDateFrom]  = useState('')
  const [dateTo,    setDateTo]    = useState('')
  const [pathQuery, setPathQuery] = useState('')
  const [copied,    setCopied]    = useState(false)
  const debouncedPath = useDebounce(pathQuery, 150)

  useEffect(() => {
    api.changeLog()
      .then(data => setEntries(data.entries))
      .catch(e => setError(e.message))
  }, [])

  // Flatten every entry × category × file into a single row list
  const allRows = useMemo(() => {
    if (!entries) return []
    const rows = []
    for (const entry of entries) {
      for (const cat of CHANGE_CATEGORIES) {
        const items = (entry.diff[cat.diffKey] || []).filter(item =>
          cat.tab == null || item.tab === cat.tab
        )
        for (const item of items) {
          rows.push({
            ran_at:           entry.ran_at,
            trigger:          entry.trigger,
            duration_seconds: entry.duration_seconds ?? null,
            cat,
            path: item.path,
            tab:  item.tab,
            size: item.size ?? null,
          })
        }
      }
    }
    return rows
  }, [entries])

  const rows = useMemo(() => {
    let r = allRows
    if (dateFrom) {
      const from = new Date(dateFrom).getTime()
      r = r.filter(row => new Date(row.ran_at).getTime() >= from)
    }
    if (dateTo) {
      // include the full selected day
      const to = new Date(dateTo).getTime() + 86400_000
      r = r.filter(row => new Date(row.ran_at).getTime() < to)
    }
    if (catFilter.length) r = r.filter(row => catFilter.includes(row.cat.key))
    if (debouncedPath.trim()) {
      const q = debouncedPath.trim().toLowerCase()
      r = r.filter(row => row.path.toLowerCase().includes(q))
    }
    return r
  }, [allRows, dateFrom, dateTo, catFilter, debouncedPath])

  const exportCSV = useCallback(() => {
    const lines = [
      'Date,Trigger,Type,Path,Size',
      ...rows.map(r =>
        `"${fmtDate(r.ran_at)}","${r.trigger ?? ''}","${r.cat.label}","${r.path}","${r.size ?? ''}"`
      ),
    ]
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/csv' }))
    a.download = 'auditorr_changes.csv'
    a.click()
  }, [rows])

  const counts = useMemo(() => {
    const c = {}
    for (const cat of CHANGE_CATEGORIES) c[cat.key] = 0
    for (const r of allRows) c[r.cat.key] = (c[r.cat.key] ?? 0) + 1
    return c
  }, [allRows])

  // Summary-card stats over the *filtered* rows — mirrors the file browsers'
  // boxes, so they respond to the date/category/search filters below.
  const boxStats = useMemo(() => {
    const s = { total: 0, totalSize: 0, imported: 0, importedSize: 0, orphaned: 0, orphanedSize: 0, removed: 0, removedSize: 0 }
    for (const r of rows) {
      const sz = r.size || 0
      s.total++; s.totalSize += sz
      const k = r.cat.key
      if (k === 'newly_imported')                            { s.imported++; s.importedSize += sz }
      else if (k === 'newly_orphaned')                       { s.orphaned++; s.orphanedSize += sz }
      else if (k === 'removed_torrent' || k === 'removed_media') { s.removed++;  s.removedSize  += sz }
    }
    return s
  }, [rows])

  const itemData = useMemo(() => ({ rows, onNavigate }), [rows, onNavigate])

  return (
    <div className="fade-in" style={{ padding: '0 24px 24px', height: '100%', display: 'flex', flexDirection: 'column', boxSizing: 'border-box' }}>

      {/* Summary cards — mirror the file-browser stat boxes */}
      <div style={{ padding: '16px 0 14px', display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 10, flexShrink: 0 }}>
        {[
          { label: 'Changes',  val: boxStats.total,    size: boxStats.totalSize,    color: 'var(--text)' },
          { label: 'Imported', val: boxStats.imported, size: boxStats.importedSize, color: 'var(--green)' },
          { label: 'Orphaned', val: boxStats.orphaned, size: boxStats.orphanedSize, color: 'var(--yellow)' },
          { label: 'Removed',  val: boxStats.removed,  size: boxStats.removedSize,  color: 'var(--red)' },
        ].map(c => (
          <div key={c.label} style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--r)', boxShadow: 'var(--elev-1)', padding: '10px 14px' }}>
            <div style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', fontWeight: 600, color: 'var(--text)', textTransform: 'none', letterSpacing: 0, display: 'flex', alignItems: 'center', gap: 7 }}>
              {c.color !== 'var(--text)' && <span className="ui-status-dot" style={{ background: c.color }} />}
              {c.label}
            </div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-xl)', fontWeight: 700, color: 'var(--text)' }}>{entries == null ? '—' : c.val.toLocaleString()}</div>
            <div style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>{entries == null ? '' : formatBytes(c.size)}</div>
          </div>
        ))}
      </div>

      {/* Filter bar */}
      <div style={{
        background: 'var(--bg)',
        borderBottom: '1px solid var(--border)',
        marginBottom: 14,
        flexShrink: 0,
      }}>
        {/* A page filter bar: every control is var(--control-h-lg), the height
            the date pickers already had. The categories are many-of-N, one
            track with All as its reset — the same shape as every filter row. */}
        {/* Row 1: categories */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', padding: '8px 0 6px' }}>
          <Segmented multiple size="lg" allLabel="All" label="Change categories"
            value={catFilter} onChange={setCatFilter}
            options={CHANGE_CATEGORIES
              .filter(cat => (counts[cat.key] ?? 0) || entries == null)
              .map(cat => {
                const n = counts[cat.key] ?? 0
                return {
                  value: cat.key,
                  icon: <Dot color={cat.color} size={6} />,
                  label: `${cat.label}${n ? ` (${n.toLocaleString()})` : ''}`,
                }
              })} />
        </div>
        {/* Row 2: date range + search + export */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', padding: '0 0 8px' }}>
          <span style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', color: 'var(--text-dim)' }}>Date:</span>
          <DatePicker value={dateFrom} onChange={setDateFrom} placeholder="From" />
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>—</span>
          <DatePicker value={dateTo} onChange={setDateTo} placeholder="To" />
          {(dateFrom || dateTo) && (
            <Button variant="ghost" square onClick={() => { setDateFrom(''); setDateTo('') }}
              title="Clear dates" ariaLabel="Clear dates">✕</Button>
          )}

          <div style={{ width: 1, height: 18, background: 'var(--border2)', margin: '0 3px' }} />

          {/* Path search */}
          <SearchInput value={pathQuery} onChange={setPathQuery} placeholder="Search path…" width={200} size="lg" mono />
          {pathQuery && (
            <Button variant="ghost" square onClick={() => setPathQuery('')} title="Clear search" ariaLabel="Clear search">✕</Button>
          )}

          <div style={{ flex: 1 }} />

          <Button variant="subtle" onClick={exportCSV}>Export CSV</Button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div style={{ padding: '12px 16px', borderRadius: 'var(--r)', background: tint('var(--red)', 7), border: `1px solid ${tint('var(--red)', 19)}`, fontFamily: 'var(--mono)', fontSize: 'var(--font-base)', color: 'var(--red)' }}>
          Failed to load change log: {error}
        </div>
      )}

      {/* Table */}
      {!error && (
        <div style={{
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', overflow: 'hidden',
          flex: 1, minHeight: 0,
        }}>
          {entries == null ? (
            <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-dim)', fontFamily: 'var(--sans)', fontSize: 'var(--font-base)' }}>
              Loading…
            </div>
          ) : rows.length === 0 ? (
            <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-dim)', fontFamily: 'var(--sans)', fontSize: 'var(--font-base)' }}>
              {allRows.length === 0
                ? 'No changes recorded yet. Changes appear after two or more successful audits.'
                : 'No entries match the current filters.'}
            </div>
          ) : (
            <>
              {/* Column headers */}
              <div style={{
                display: 'grid',
                gridTemplateColumns: '200px 80px 120px 1fr 72px',
                padding: '5px 16px',
                height: COL_HEADER_HEIGHT,
                borderBottom: '1px solid var(--border)',
                background: 'var(--surface2)',
                boxSizing: 'border-box',
                alignItems: 'center',
              }}>
                {['Date', 'Trigger', 'Type', 'Path', 'Size'].map((col, i) => (
                  <span key={col} style={{
                    fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', fontWeight: 600, color: 'var(--text-dim)',
                    letterSpacing: 0, textTransform: 'none',
                    textAlign: i === 4 ? 'right' : 'left',
                  }}>{col}</span>
                ))}
              </div>
              {/* Virtual rows */}
              <div style={{ height: `calc(100% - ${COL_HEADER_HEIGHT}px)` }}>
                <AutoSizer>
                  {({ height, width }) => (
                    <FixedSizeList
                      height={height}
                      width={width}
                      itemCount={rows.length}
                      itemSize={ROW_HEIGHT}
                      itemData={itemData}
                      overscanCount={10}
                    >
                      {ChangeRow}
                    </FixedSizeList>
                  )}
                </AutoSizer>
              </div>
            </>
          )}
        </div>
      )}

      {!error && entries != null && rows.length > 0 && (
        <div style={{ marginTop: 8, fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', textAlign: 'right' }}>
          {rows.length.toLocaleString()} {rows.length === 1 ? 'change' : 'changes'}
          {entries.length > 0 && ` across ${entries.length.toLocaleString()} ${entries.length === 1 ? 'audit' : 'audits'}`}
        </div>
      )}
    </div>
  )
}
