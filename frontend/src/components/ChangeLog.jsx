import React, { useState, useEffect } from 'react'
import { api } from '../api'
import { formatBytes } from '../utils'

const CATEGORIES = [
  { key: 'newly_orphaned',      label: 'Orphaned',       color: 'var(--yellow)' },
  { key: 'new_duplicates',      label: 'New Dupe',        color: 'var(--purple)' },
  { key: 'newly_imported',      label: 'Imported',        color: 'var(--green)'  },
  { key: 'resolved_duplicates', label: 'Dupe Resolved',   color: 'var(--blue)'   },
  { key: 'new_files',           label: 'New File',        color: 'var(--text-dim)' },
  { key: 'removed_files',       label: 'Removed',         color: 'var(--text-dim)' },
]

// Labels used in the page-level filter bar (slightly more descriptive)
const FILTER_LABELS = {
  newly_orphaned:      'Became Orphaned',
  new_duplicates:      'New Duplicates',
  newly_imported:      'Newly Imported',
  resolved_duplicates: 'Duplicates Resolved',
  new_files:           'New Files',
  removed_files:       'Removed Files',
}

const TRIGGER_LABELS = {
  watchdog:  'watchdog',
  scheduled: 'scheduled',
  manual:    'manual',
  startup:   'startup',
}

function fmtDate(iso) {
  const d = new Date(iso)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function ScoreDelta({ delta }) {
  if (delta == null) return null
  const pos = delta >= 0
  return (
    <span style={{
      fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
      color: pos ? 'var(--green)' : 'var(--red)',
      background: (pos ? 'var(--green)' : 'var(--red)') + '15',
      border: `1px solid ${(pos ? 'var(--green)' : 'var(--red)')}30`,
      borderRadius: 99, padding: '2px 8px',
    }}>
      {pos ? '+' : ''}{delta} pts
    </span>
  )
}

function EntryCard({ entry }) {
  const [expanded, setExpanded] = useState(false)
  const [activeFilter, setActiveFilter] = useState(null)

  const activeCats = CATEGORIES.filter(c => entry.diff[c.key]?.length > 0)

  const allRows = []
  for (const cat of CATEGORIES) {
    for (const item of (entry.diff[cat.key] || [])) allRows.push({ ...item, cat })
  }

  const rows = activeFilter ? allRows.filter(r => r.cat.key === activeFilter) : allRows

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderLeft: '3px solid var(--accent)', borderRadius: 10, overflow: 'hidden',
    }}>
      {/* Header — click to expand */}
      <button
        onClick={() => setExpanded(e => !e)}
        style={{
          display: 'flex', alignItems: 'center', gap: 10, width: '100%',
          padding: '12px 16px', background: 'transparent', border: 'none',
          cursor: allRows.length > 0 ? 'pointer' : 'default',
          textAlign: 'left', flexWrap: 'wrap',
        }}
      >
        <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text)', fontWeight: 600 }}>
          {fmtDate(entry.ran_at)}
        </span>
        {entry.trigger && (
          <span style={{
            fontFamily: 'var(--mono)', fontSize: 10,
            color: 'var(--text-dim)', background: 'var(--surface2)',
            border: '1px solid var(--border2)', borderRadius: 4, padding: '1px 7px',
          }}>
            {TRIGGER_LABELS[entry.trigger] ?? entry.trigger}
          </span>
        )}
        {entry.health_score != null && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>
            health {entry.health_score.toFixed(1)}
          </span>
        )}
        <ScoreDelta delta={entry.diff.score_delta} />
        {/* Category count badges + chevron pushed to the right */}
        {activeCats.length > 0 && (
          <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', marginLeft: 'auto', alignItems: 'center' }}>
            {activeCats.map(cat => (
              <span key={cat.key} style={{
                fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
                color: cat.color, background: cat.color + '18',
                border: `1px solid ${cat.color}30`,
                borderRadius: 4, padding: '1px 6px', whiteSpace: 'nowrap',
              }}>
                {entry.diff[cat.key].length} {cat.label}
              </span>
            ))}
            <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', marginLeft: 2 }}>
              {expanded ? '▲' : '▼'}
            </span>
          </div>
        )}
      </button>

      {/* Expanded table */}
      {expanded && allRows.length > 0 && (
        <>
          {/* Filter chips */}
          <div style={{
            display: 'flex', gap: 5, flexWrap: 'wrap',
            padding: '7px 16px', borderTop: '1px solid var(--border)',
            background: 'var(--surface2)',
          }}>
            <button
              onClick={e => { e.stopPropagation(); setActiveFilter(null) }}
              style={{
                padding: '2px 10px', borderRadius: 99, fontSize: 11,
                border: `1px solid ${activeFilter === null ? 'var(--accent)' : 'var(--border2)'}`,
                background: activeFilter === null ? 'var(--accent)22' : 'transparent',
                color: activeFilter === null ? 'var(--accent)' : 'var(--text-dim)',
                cursor: 'pointer', fontFamily: 'var(--mono)',
              }}
            >All ({allRows.length})</button>
            {activeCats.map(cat => {
              const active = activeFilter === cat.key
              return (
                <button key={cat.key}
                  onClick={e => { e.stopPropagation(); setActiveFilter(active ? null : cat.key) }}
                  style={{
                    padding: '2px 10px', borderRadius: 99, fontSize: 11,
                    border: `1px solid ${active ? cat.color : 'var(--border2)'}`,
                    background: active ? cat.color + '22' : 'transparent',
                    color: active ? cat.color : 'var(--text-dim)',
                    cursor: 'pointer', fontFamily: 'var(--mono)',
                  }}
                >
                  {cat.label} ({entry.diff[cat.key].length})
                </button>
              )
            })}
          </div>

          {/* Column headers */}
          <div style={{
            display: 'grid', gridTemplateColumns: '130px 1fr 80px',
            padding: '5px 16px', borderTop: '1px solid var(--border)',
            background: 'var(--surface2)',
          }}>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 1.5, textTransform: 'uppercase' }}>Type</span>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 1.5, textTransform: 'uppercase' }}>Path</span>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 1.5, textTransform: 'uppercase', textAlign: 'right' }}>Size</span>
          </div>

          {/* Rows */}
          <div style={{ maxHeight: 320, overflowY: 'auto' }}>
            {rows.map((row, i) => (
              <div key={i} style={{
                display: 'grid', gridTemplateColumns: '130px 1fr 80px',
                alignItems: 'center', height: 36,
                padding: '0 16px',
                borderTop: '1px solid var(--border)',
                background: 'var(--surface)', boxSizing: 'border-box', overflow: 'hidden',
              }}>
                <div>
                  <span style={{
                    fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
                    color: row.cat.color, background: row.cat.color + '18',
                    border: `1px solid ${row.cat.color}30`,
                    borderRadius: 4, padding: '1px 6px', whiteSpace: 'nowrap',
                  }}>{row.cat.label}</span>
                </div>
                <div style={{ overflow: 'hidden', paddingRight: 8 }}>
                  <span style={{
                    fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)',
                    display: 'block', overflow: 'hidden',
                    textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {row.path}
                  </span>
                </div>
                <div style={{ textAlign: 'right' }}>
                  {row.size != null && (
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>
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

export default function ChangeLog() {
  const [entries, setEntries] = useState(null)
  const [error,   setError]   = useState(null)
  const [filter,  setFilter]  = useState('all')

  useEffect(() => {
    api.changeLog()
      .then(data => setEntries(data.entries))
      .catch(e => setError(e.message))
  }, [])

  const filtered = entries
    ? (filter === 'all' ? entries : entries.filter(e => e.diff[filter]?.length > 0))
    : null

  return (
    <div className="fade-in" style={{ padding: 24, maxWidth: 860 }}>

      {/* Page header */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 2, textTransform: 'uppercase', color: 'var(--text-dim)', marginBottom: 6 }}>
          Change Log
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span style={{ fontSize: 20, fontWeight: 700, color: 'var(--text)' }}>Audit History</span>
          {filtered != null && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
              {filtered.length} {filter === 'all' ? 'entries' : 'matching'}
            </span>
          )}
        </div>
        <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.55 }}>
          File-level changes detected between consecutive audits, kept indefinitely.
        </p>
      </div>

      {/* Filter bar */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 20 }}>
        <button
          onClick={() => setFilter('all')}
          style={{
            padding: '5px 14px', borderRadius: 99, fontSize: 11, cursor: 'pointer',
            border: `1px solid ${filter === 'all' ? 'var(--accent)' : 'var(--border2)'}`,
            background: filter === 'all' ? 'var(--accent)18' : 'transparent',
            color: filter === 'all' ? 'var(--accent)' : 'var(--text-dim)',
            transition: 'all 0.12s',
          }}
        >All</button>
        {CATEGORIES.map(cat => (
          <button
            key={cat.key}
            onClick={() => setFilter(cat.key)}
            style={{
              padding: '5px 14px', borderRadius: 99, fontSize: 11, cursor: 'pointer',
              border: `1px solid ${filter === cat.key ? cat.color : cat.color + '40'}`,
              background: filter === cat.key ? cat.color + '18' : 'transparent',
              color: filter === cat.key ? cat.color : 'var(--text-dim)',
              fontFamily: 'var(--mono)', transition: 'all 0.12s',
            }}
          >
            {FILTER_LABELS[cat.key]}
          </button>
        ))}
      </div>

      {/* Content */}
      {error && (
        <div style={{ padding: '12px 16px', borderRadius: 8, background: 'var(--red)12', border: '1px solid var(--red)30', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--red)' }}>
          Failed to load change log: {error}
        </div>
      )}

      {!error && filtered == null && (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-dim)', padding: '40px 0', textAlign: 'center' }}>
          Loading…
        </div>
      )}

      {!error && filtered != null && filtered.length === 0 && (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-dim)', padding: '40px 0', textAlign: 'center' }}>
          {filter === 'all'
            ? 'No changes recorded yet. Changes appear after two or more successful audits.'
            : 'No entries match this filter.'}
        </div>
      )}

      {filtered != null && filtered.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {filtered.map(entry => (
            <EntryCard key={entry.id} entry={entry} />
          ))}
        </div>
      )}
    </div>
  )
}
