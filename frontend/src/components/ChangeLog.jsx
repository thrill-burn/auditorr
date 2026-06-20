import React, { useState, useEffect, useMemo, useCallback } from 'react'
import DatePicker from './DatePicker'
import { FixedSizeList } from 'react-window'
import AutoSizer from 'react-virtualized-auto-sizer'
import { api } from '../api'
import { formatBytes } from '../utils'
import { CHANGE_CATEGORIES } from './changeCategories'

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
          fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)',
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', display: 'block',
        }}>
          {fmtDate(row.ran_at)}
          {fmtDuration(row.duration_seconds) && (
            <span style={{ color: 'var(--text-dim)', opacity: 0.6, fontSize: 10 }}>
              {' · '}{fmtDuration(row.duration_seconds)}
            </span>
          )}
        </span>
      </div>
      {/* Trigger */}
      <div>
        {row.trigger && (
          <span style={{
            fontFamily: 'var(--mono)', fontSize: 11,
            color: 'var(--text-dim)', background: 'var(--surface2)',
            border: '1px solid var(--border2)', borderRadius: 'var(--r-sm)', padding: '1px 6px',
            whiteSpace: 'nowrap',
          }}>
            {TRIGGER_LABELS[row.trigger] ?? row.trigger}
          </span>
        )}
      </div>
      {/* Type */}
      <div>
        <span style={{
          fontFamily: 'var(--sans)', fontSize: 11, fontWeight: 600,
          color: 'var(--text)',
          border: '1px solid var(--border2)',
          borderRadius: 'var(--r)', padding: '1px 7px', whiteSpace: 'nowrap',
          display: 'inline-flex', alignItems: 'center', gap: 6,
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
            fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)',
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
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
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
  const [catFilter, setCatFilter] = useState(null)
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
    if (catFilter) r = r.filter(row => row.cat.key === catFilter)
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

  const itemData = useMemo(() => ({ rows, onNavigate }), [rows, onNavigate])

  return (
    <div className="fade-in" style={{ padding: '0 24px 24px', height: '100%', display: 'flex', flexDirection: 'column', boxSizing: 'border-box' }}>

      {/* Page header */}
      <div style={{ padding: '16px 0 14px' }}>
        <div className="ui-section-title" style={{ marginBottom: 6 }}>
          Change log
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span style={{ fontSize: 20, fontWeight: 700, color: 'var(--text)' }}>Audit History</span>
          {entries != null && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
              {rows.length.toLocaleString()} {(catFilter || dateFrom || dateTo || debouncedPath.trim()) ? 'matching' : 'changes'}
            </span>
          )}
        </div>
        <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.55 }}>
          File-level changes detected between consecutive audits, kept indefinitely.
        </p>
      </div>

      {/* Filter bar */}
      <div style={{
        background: 'var(--bg)',
        borderBottom: '1px solid var(--border)',
        marginBottom: 14,
        flexShrink: 0,
      }}>
        {/* Row 1: category chips */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', padding: '8px 0 4px' }}>
          <button
            onClick={() => setCatFilter(null)}
            style={{
              padding: '4px 12px', borderRadius: 'var(--r-pill)', fontSize: 12, cursor: 'pointer',
              border: catFilter === null ? '1px solid var(--accent)' : '1px solid var(--border2)',
              background: catFilter === null ? 'var(--accent)18' : 'transparent',
              color: catFilter === null ? 'var(--accent)' : 'var(--text-dim)',
              transition: 'all 0.12s',
            }}
          >All</button>
          {CHANGE_CATEGORIES.map(cat => {
            const n = counts[cat.key] ?? 0
            if (!n && entries != null) return null
            const active = catFilter === cat.key
            return (
              <button key={cat.key}
                onClick={() => setCatFilter(active ? null : cat.key)}
                style={{
                  padding: '4px 12px', borderRadius: 'var(--r-pill)', fontSize: 11, cursor: 'pointer',
                  border: active ? '1px solid var(--accent)' : '1px solid var(--border2)',
                  background: active ? 'var(--accent)18' : 'transparent',
                  color: active ? 'var(--accent)' : 'var(--text-dim)',
                  fontFamily: 'var(--sans)', transition: 'all 0.12s',
                  display: 'inline-flex', alignItems: 'center', gap: 6,
                }}
              >
                <span className="ui-status-dot" style={{ width: 6, height: 6, background: cat.color }} />
                {cat.label}{n ? ` (${n.toLocaleString()})` : ''}
              </button>
            )
          })}
        </div>
        {/* Row 2: date range + search + export */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', padding: '4px 0 8px' }}>
          <span style={{ fontFamily: 'var(--sans)', fontSize: 12, color: 'var(--text-dim)' }}>Date:</span>
          <DatePicker value={dateFrom} onChange={setDateFrom} placeholder="From" />
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>—</span>
          <DatePicker value={dateTo} onChange={setDateTo} placeholder="To" />
          {(dateFrom || dateTo) && (
            <button
              onClick={() => { setDateFrom(''); setDateTo('') }}
              style={{
                padding: '2px 7px', borderRadius: 'var(--r-pill)', fontSize: 11,
                border: '1px solid var(--border2)', background: 'transparent',
                color: 'var(--text-dim)', cursor: 'pointer',
              }}
            >✕</button>
          )}

          <div style={{ width: 1, height: 18, background: 'var(--border2)', margin: '0 2px' }} />

          {/* Path search */}
          <div style={{ position: 'relative', display: 'flex', alignItems: 'center' }}>
            <input
              type="text"
              value={pathQuery}
              onChange={e => setPathQuery(e.target.value)}
              placeholder="🔎 search path…"
              style={{
                width: 200, height: 28, padding: '0 10px',
                borderRadius: 'var(--r-pill)', fontSize: 12,
                border: `1px solid ${pathQuery ? 'var(--accent)66' : 'var(--border2)'}`,
                background: pathQuery ? 'var(--surface2)' : 'transparent',
                color: 'var(--text)', fontFamily: 'var(--mono)',
                outline: 'none', transition: 'all 0.12s',
              }}
              onFocus={e => e.target.style.borderColor = 'var(--accent)'}
              onBlur={e => e.target.style.borderColor = pathQuery ? 'var(--accent)66' : 'var(--border2)'}
            />
          </div>
          {pathQuery && (
            <button onClick={() => setPathQuery('')} style={{
              padding: '2px 7px', borderRadius: 'var(--r-pill)', fontSize: 11,
              border: '1px solid var(--border2)', background: 'transparent',
              color: 'var(--text-dim)', cursor: 'pointer',
            }}>✕</button>
          )}

          <div style={{ flex: 1 }} />

          <button onClick={exportCSV} style={{
            padding: '4px 12px', borderRadius: 'var(--r-pill)', fontSize: 12, flexShrink: 0,
            border: '1px solid var(--border2)', background: 'transparent',
            color: 'var(--text-dim)', cursor: 'pointer',
          }}>Export CSV</button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div style={{ padding: '12px 16px', borderRadius: 'var(--r)', background: 'var(--red)12', border: '1px solid var(--red)30', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--red)' }}>
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
            <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-dim)', fontFamily: 'var(--sans)', fontSize: 12 }}>
              Loading…
            </div>
          ) : rows.length === 0 ? (
            <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-dim)', fontFamily: 'var(--sans)', fontSize: 12 }}>
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
                    fontFamily: 'var(--sans)', fontSize: 11, fontWeight: 600, color: 'var(--text-dim)',
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
        <div style={{ marginTop: 8, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', textAlign: 'right' }}>
          {rows.length.toLocaleString()} {rows.length === 1 ? 'change' : 'changes'}
          {entries.length > 0 && ` across ${entries.length.toLocaleString()} ${entries.length === 1 ? 'audit' : 'audits'}`}
        </div>
      )}
    </div>
  )
}
