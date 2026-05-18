import React, { useState, useEffect, useMemo } from 'react'
import { FixedSizeList } from 'react-window'
import AutoSizer from 'react-virtualized-auto-sizer'
import { api } from '../api'
import { formatBytes } from '../utils'

const CATEGORIES = [
  { key: 'newly_orphaned',      label: 'Orphaned',      color: 'var(--yellow)'   },
  { key: 'new_duplicates',      label: 'New Dupe',       color: 'var(--purple)'   },
  { key: 'newly_imported',      label: 'Imported',       color: 'var(--green)'    },
  { key: 'resolved_duplicates', label: 'Dupe Resolved',  color: 'var(--blue)'     },
  { key: 'new_files',           label: 'New File',       color: 'var(--text-dim)' },
  { key: 'removed_files',       label: 'Removed',        color: 'var(--text-dim)' },
]

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

// ─── Virtual row ─────────────────────────────────────────────────────────────

function ChangeRow({ index, style, data }) {
  const row = data.rows[index]
  return (
    <div style={{
      ...style,
      display: 'grid',
      gridTemplateColumns: '200px 80px 70px 120px 1fr 72px',
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
        </span>
      </div>
      {/* Trigger */}
      <div>
        {row.trigger && (
          <span style={{
            fontFamily: 'var(--mono)', fontSize: 10,
            color: 'var(--text-dim)', background: 'var(--surface2)',
            border: '1px solid var(--border2)', borderRadius: 4, padding: '1px 6px',
            whiteSpace: 'nowrap',
          }}>
            {TRIGGER_LABELS[row.trigger] ?? row.trigger}
          </span>
        )}
      </div>
      {/* Score delta */}
      <div>
        {row.score_delta != null && (
          <span style={{
            fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
            color: row.score_delta >= 0 ? 'var(--green)' : 'var(--red)',
            background: (row.score_delta >= 0 ? 'var(--green)' : 'var(--red)') + '15',
            border: `1px solid ${(row.score_delta >= 0 ? 'var(--green)' : 'var(--red)')}30`,
            borderRadius: 99, padding: '1px 6px', whiteSpace: 'nowrap',
          }}>
            {row.score_delta >= 0 ? '+' : ''}{row.score_delta}
          </span>
        )}
      </div>
      {/* Type */}
      <div>
        <span style={{
          fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
          color: row.cat.color, background: row.cat.color + '18',
          border: `1px solid ${row.cat.color}30`,
          borderRadius: 4, padding: '1px 6px', whiteSpace: 'nowrap',
        }}>{row.cat.label}</span>
      </div>
      {/* Path */}
      <div style={{ overflow: 'hidden', paddingRight: 8 }}>
        <span style={{
          fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)',
          display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {row.path}
        </span>
      </div>
      {/* Size */}
      <div style={{ textAlign: 'right' }}>
        {row.size != null && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>
            {formatBytes(row.size)}
          </span>
        )}
      </div>
    </div>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function ChangeLog() {
  const [entries, setEntries] = useState(null)
  const [error,   setError]   = useState(null)
  const [catFilter, setCatFilter] = useState(null)

  useEffect(() => {
    api.changeLog()
      .then(data => setEntries(data.entries))
      .catch(e => setError(e.message))
  }, [])

  // Flatten every entry × category × file into a single sorted row list
  const allRows = useMemo(() => {
    if (!entries) return []
    const rows = []
    for (const entry of entries) {
      for (const cat of CATEGORIES) {
        for (const item of (entry.diff[cat.key] || [])) {
          rows.push({
            ran_at:      entry.ran_at,
            trigger:     entry.trigger,
            score_delta: entry.diff.score_delta,
            cat,
            path: item.path,
            size: item.size ?? null,
          })
        }
      }
    }
    return rows
  }, [entries])

  const rows = useMemo(
    () => catFilter ? allRows.filter(r => r.cat.key === catFilter) : allRows,
    [allRows, catFilter]
  )

  const counts = useMemo(() => {
    const c = {}
    for (const cat of CATEGORIES) c[cat.key] = 0
    for (const r of allRows) c[r.cat.key]++
    return c
  }, [allRows])

  const itemData = useMemo(() => ({ rows }), [rows])

  return (
    <div className="fade-in" style={{ padding: '0 24px 24px' }}>

      {/* Page header */}
      <div style={{ padding: '16px 0 14px' }}>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 2, textTransform: 'uppercase', color: 'var(--text-dim)', marginBottom: 6 }}>
          Change Log
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span style={{ fontSize: 20, fontWeight: 700, color: 'var(--text)' }}>Audit History</span>
          {entries != null && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
              {rows.length.toLocaleString()} {catFilter ? 'matching' : 'changes'}
            </span>
          )}
        </div>
        <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.55 }}>
          File-level changes detected between consecutive audits, kept indefinitely.
        </p>
      </div>

      {/* Sticky filter bar */}
      <div style={{
        position: 'sticky', top: 0, zIndex: 90,
        background: 'var(--bg)',
        borderBottom: '1px solid var(--border)',
        marginBottom: 14,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', padding: '8px 0 6px' }}>
          <button
            onClick={() => setCatFilter(null)}
            style={{
              padding: '4px 12px', borderRadius: 99, fontSize: 12, cursor: 'pointer',
              border: `1px solid ${catFilter === null ? 'var(--accent)' : 'var(--border2)'}`,
              background: catFilter === null ? 'var(--accent)22' : 'transparent',
              color: catFilter === null ? 'var(--accent)' : 'var(--text-dim)',
              transition: 'all 0.12s',
            }}
          >All</button>
          {CATEGORIES.map(cat => {
            const n = counts[cat.key] ?? 0
            if (!n && entries != null) return null
            const active = catFilter === cat.key
            return (
              <button key={cat.key}
                onClick={() => setCatFilter(active ? null : cat.key)}
                style={{
                  padding: '4px 12px', borderRadius: 99, fontSize: 11, cursor: 'pointer',
                  border: `1px solid ${active ? cat.color : cat.color + '40'}`,
                  background: active ? cat.color + '18' : 'transparent',
                  color: active ? cat.color : 'var(--text-dim)',
                  fontFamily: 'var(--mono)', transition: 'all 0.12s',
                }}
              >
                {cat.label}{n ? ` (${n.toLocaleString()})` : ''}
              </button>
            )
          })}
        </div>
      </div>

      {/* Error */}
      {error && (
        <div style={{ padding: '12px 16px', borderRadius: 8, background: 'var(--red)12', border: '1px solid var(--red)30', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--red)' }}>
          Failed to load change log: {error}
        </div>
      )}

      {/* Table */}
      {!error && (
        <div style={{
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 'var(--rl)', overflow: 'hidden',
          height: 'calc(100vh - 320px)',
        }}>
          {entries == null ? (
            <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 12 }}>
              Loading…
            </div>
          ) : rows.length === 0 ? (
            <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 12 }}>
              {allRows.length === 0
                ? 'No changes recorded yet. Changes appear after two or more successful audits.'
                : 'No entries match this filter.'}
            </div>
          ) : (
            <>
              {/* Column headers */}
              <div style={{
                display: 'grid',
                gridTemplateColumns: '200px 80px 70px 120px 1fr 72px',
                padding: '5px 16px',
                height: COL_HEADER_HEIGHT,
                borderBottom: '1px solid var(--border)',
                background: 'var(--surface2)',
                boxSizing: 'border-box',
                alignItems: 'center',
              }}>
                {['Date', 'Trigger', 'Score', 'Type', 'Path', 'Size'].map((col, i) => (
                  <span key={col} style={{
                    fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)',
                    letterSpacing: 1.5, textTransform: 'uppercase',
                    textAlign: i === 5 ? 'right' : 'left',
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
        <div style={{ marginTop: 8, fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', textAlign: 'right' }}>
          {rows.length.toLocaleString()} {rows.length === 1 ? 'change' : 'changes'}
          {entries.length > 0 && ` across ${entries.length.toLocaleString()} ${entries.length === 1 ? 'audit' : 'audits'}`}
        </div>
      )}
    </div>
  )
}
