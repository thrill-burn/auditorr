import React, { useState, useEffect } from 'react'
import { api } from '../api'
import { formatBytes } from '../utils'

const CATEGORIES = [
  { key: 'newly_orphaned',      label: 'Became Orphaned',    color: 'var(--yellow)', icon: '⚠' },
  { key: 'new_duplicates',      label: 'New Duplicates',      color: 'var(--purple)', icon: '⊕' },
  { key: 'newly_imported',      label: 'Newly Imported',      color: 'var(--green)',  icon: '✓' },
  { key: 'resolved_duplicates', label: 'Duplicates Resolved', color: 'var(--blue)',   icon: '✓' },
  { key: 'new_files',           label: 'New Files',           color: 'var(--text-dim)', icon: '+' },
  { key: 'removed_files',       label: 'Removed Files',       color: 'var(--text-dim)', icon: '−' },
]

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
  const [openCat, setOpenCat] = useState(null)

  const activeCats = CATEGORIES.filter(c => entry.diff[c.key]?.length > 0)

  const toggleCat = (key) => setOpenCat(prev => prev === key ? null : key)

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderLeft: '3px solid var(--accent)', borderRadius: 10, overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', flexWrap: 'wrap' }}>
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
      </div>

      {/* Category chips */}
      {activeCats.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, padding: '0 16px 12px' }}>
          {activeCats.map(cat => {
            const items = entry.diff[cat.key]
            const isOpen = openCat === cat.key
            return (
              <button
                key={cat.key}
                onClick={() => toggleCat(cat.key)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 5,
                  padding: '4px 10px', borderRadius: 99, cursor: 'pointer',
                  border: `1px solid ${isOpen ? cat.color : cat.color + '40'}`,
                  background: isOpen ? cat.color + '18' : cat.color + '0a',
                  color: cat.color, fontFamily: 'var(--mono)', fontSize: 11,
                  fontWeight: isOpen ? 600 : 400, transition: 'all 0.12s',
                }}
              >
                <span>{cat.icon}</span>
                <span>{items.length} {cat.label}</span>
                <span style={{ opacity: 0.6, fontSize: 9 }}>{isOpen ? '▲' : '▼'}</span>
              </button>
            )
          })}
        </div>
      )}

      {/* Expanded file list */}
      {openCat && entry.diff[openCat]?.length > 0 && (
        <div style={{
          borderTop: '1px solid var(--border)',
          background: 'var(--surface2)',
          maxHeight: 260, overflowY: 'auto',
          padding: '8px 16px 12px 16px',
        }}>
          {entry.diff[openCat].map((item, i) => (
            <div key={i} style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '4px 0',
              borderBottom: i < entry.diff[openCat].length - 1 ? '1px solid var(--border)' : 'none',
            }}>
              <span style={{
                fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1,
              }}>
                {item.path}
              </span>
              {item.size != null && (
                <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-faint)', flexShrink: 0, marginLeft: 12 }}>
                  {formatBytes(item.size)}
                </span>
              )}
            </div>
          ))}
        </div>
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
          File-level changes detected between consecutive audits, kept for 90 days.
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
            {cat.icon} {cat.label}
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
