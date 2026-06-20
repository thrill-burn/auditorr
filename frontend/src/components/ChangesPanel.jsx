import React, { useState } from 'react'
import { formatBytes } from '../utils'

const CATEGORIES = [
  { key: 'newly_orphaned',      diffKey: 'newly_orphaned',      tab: null,       label: 'Orphaned',       color: 'var(--yellow)',   navigable: true  },
  { key: 'new_duplicates',      diffKey: 'new_duplicates',      tab: null,       label: 'New Dupe',        color: 'var(--purple)',   navigable: true  },
  { key: 'newly_imported',      diffKey: 'newly_imported',      tab: null,       label: 'Imported',        color: 'var(--green)',    navigable: true  },
  { key: 'resolved_duplicates', diffKey: 'resolved_duplicates', tab: null,       label: 'Dupe Resolved',   color: 'var(--blue)',     navigable: true  },
  { key: 'new_torrent',         diffKey: 'new_files',           tab: 'torrents', label: 'New Torrent',     color: 'var(--text-dim)', navigable: true  },
  { key: 'new_media',           diffKey: 'new_files',           tab: 'media',    label: 'New Media',       color: 'var(--text-dim)', navigable: true  },
  { key: 'removed_torrent',     diffKey: 'removed_files',       tab: 'torrents', label: 'Removed Torrent', color: 'var(--text-dim)', navigable: false },
  { key: 'removed_media',       diffKey: 'removed_files',       tab: 'media',    label: 'Removed Media',   color: 'var(--text-dim)', navigable: false },
]

const ROW_HEIGHT = 36

export default function ChangesPanel({ changes, prevRanAt, currRanAt, onNavigate, onReveal }) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('auditorr_changes_collapsed') === '1')
  const [activeFilter, setActiveFilter] = useState(null)
  const dismissKey = currRanAt ? 'auditorr_changes_dismissed_' + currRanAt : null
  const [dismissed, setDismissed] = useState(() =>
    dismissKey ? sessionStorage.getItem(dismissKey) === '1' : false
  )

  const handleDismiss = () => {
    setDismissed(true)
    if (dismissKey) sessionStorage.setItem(dismissKey, '1')
  }

  const handleCollapse = () => {
    const next = !collapsed
    setCollapsed(next)
    localStorage.setItem('auditorr_changes_collapsed', next ? '1' : '0')
  }

  if (!changes || dismissed) return null

  // Flatten all items across categories into a single table rows list
  const allRows = []
  for (const cat of CATEGORIES) {
    const items = (changes[cat.diffKey] || []).filter(item =>
      cat.tab == null || item.tab === cat.tab
    )
    for (const item of items) {
      allRows.push({ ...item, cat })
    }
  }

  const hasItems = allRows.length > 0
  if (!hasItems && changes.score_delta == null) return null

  const rows = activeFilter ? allRows.filter(r => r.cat.key === activeFilter) : allRows

  // Counts per category for filter chips
  const counts = {}
  for (const cat of CATEGORIES) {
    counts[cat.key] = (changes[cat.diffKey] || []).filter(item =>
      cat.tab == null || item.tab === cat.tab
    ).length
  }

  const scoreDelta = changes.score_delta
  const fmtDate = dt => dt ? new Date(dt).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''

  const showBody = hasItems && !collapsed

  return (
    <div style={{
      margin: '0 0 16px 0',
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderLeft: '3px solid var(--accent)',
      borderRadius: 10,
      overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '10px 16px',
        borderBottom: showBody ? '1px solid var(--border)' : 'none',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontFamily: 'var(--sans)', fontSize: 13, fontWeight: 600, color: 'var(--text)', letterSpacing: 0, textTransform: 'none' }}>
            Changes since last scan
          </span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
            {fmtDate(prevRanAt)} → {fmtDate(currRanAt)}
          </span>
          {scoreDelta != null && (
            <span style={{
              fontFamily: 'var(--sans)', fontSize: 11, fontWeight: 600,
              color: 'var(--text-dim)',
              border: '1px solid var(--border2)',
              borderRadius: 99, padding: '2px 8px',
              display: 'inline-flex', alignItems: 'center', gap: 6,
            }}>
              <span className="ui-status-dot" style={{ width: 6, height: 6, background: scoreDelta >= 0 ? 'var(--green)' : 'var(--red)' }} />
              {scoreDelta >= 0 ? '+' : ''}{scoreDelta} pts
            </span>
          )}
          {hasItems && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
              {allRows.length} {allRows.length === 1 ? 'change' : 'changes'}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <button onClick={handleCollapse} style={{
            background: 'none', border: 'none', color: 'var(--text-dim)',
            fontFamily: 'var(--sans)', fontSize: 11, cursor: 'pointer', lineHeight: 1, padding: '2px 5px',
          }}>{collapsed ? '▶' : '▼'}</button>
          <button onClick={handleDismiss} style={{
            background: 'none', border: 'none', color: 'var(--text-dim)',
            fontSize: 16, cursor: 'pointer', lineHeight: 1, padding: '0 4px',
          }}>×</button>
        </div>
      </div>

      {showBody && (
        <>
          {/* Filter chips */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap',
            padding: '8px 16px', borderBottom: '1px solid var(--border)',
            background: 'var(--surface2)',
          }}>
            <button
              onClick={() => setActiveFilter(null)}
              style={{
                padding: '2px 10px', borderRadius: 99, fontSize: 11, fontWeight: 500,
                border: '1px solid var(--border2)',
                background: activeFilter === null ? 'var(--surface2)' : 'transparent',
                color: activeFilter === null ? 'var(--text)' : 'var(--text-dim)',
                cursor: 'pointer', fontFamily: 'var(--sans)',
              }}
            >
              All ({allRows.length})
            </button>
            {CATEGORIES.map(cat => {
              const n = counts[cat.key]
              if (!n) return null
              const active = activeFilter === cat.key
              return (
                <button key={cat.key}
                  onClick={() => setActiveFilter(active ? null : cat.key)}
                  style={{
                    padding: '2px 10px', borderRadius: 99, fontSize: 11, fontWeight: 500,
                    border: '1px solid var(--border2)',
                    background: active ? 'var(--surface2)' : 'transparent',
                    color: active ? 'var(--text)' : 'var(--text-dim)',
                    cursor: 'pointer', fontFamily: 'var(--sans)',
                    display: 'inline-flex', alignItems: 'center', gap: 6,
                  }}
                >
                  <span className="ui-status-dot" style={{ width: 6, height: 6, background: cat.color }} />
                  {cat.label} ({n})
                </button>
              )
            })}
          </div>

          {/* Column headers */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: '130px 1fr 80px',
            padding: '5px 16px',
            borderBottom: '1px solid var(--border)',
            background: 'var(--surface2)',
          }}>
            <span className="ui-table-header">Type</span>
            <span className="ui-table-header">Path</span>
            <span className="ui-table-header" style={{ textAlign: 'right' }}>Size</span>
          </div>

          {/* Rows */}
          <div style={{ maxHeight: 320, overflowY: 'auto' }}>
            {rows.map((row, i) => (
              <div key={i} style={{
                display: 'grid',
                gridTemplateColumns: '130px 1fr 80px',
                alignItems: 'center',
                height: ROW_HEIGHT,
                padding: '0 16px',
                borderBottom: i < rows.length - 1 ? '1px solid var(--border)' : 'none',
                background: 'var(--surface)',
                boxSizing: 'border-box',
                overflow: 'hidden',
              }}>
                {/* Type tag */}
                <div>
                  <span style={{
                    fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 600,
                    color: 'var(--text-dim)',
                    border: '1px solid var(--border2)',
                    borderRadius: 4, padding: '1px 6px',
                    whiteSpace: 'nowrap',
                    display: 'inline-flex', alignItems: 'center', gap: 6,
                  }}>
                    <span className="ui-status-dot" style={{ width: 6, height: 6, background: row.cat.color }} />
                    {row.cat.label}
                  </span>
                </div>
                {/* Path */}
                <div style={{ overflow: 'hidden', paddingRight: 8 }}>
                  {row.cat.navigable ? (
                    <button
                      onClick={() => { if (onReveal && row.tab) onReveal(row.path, row.tab) }}
                      title="Click to reveal in file explorer"
                      style={{
                        background: 'none', border: 'none', cursor: 'pointer',
                        fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--accent)',
                        textAlign: 'left', width: '100%', overflow: 'hidden',
                        textOverflow: 'ellipsis', whiteSpace: 'nowrap', padding: 0,
                        display: 'block',
                      }}
                    >
                      {row.path}
                    </button>
                  ) : (
                    <span style={{
                      fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)',
                      display: 'block', overflow: 'hidden',
                      textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>
                      {row.path}
                    </span>
                  )}
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
            ))}
          </div>
        </>
      )}
    </div>
  )
}
