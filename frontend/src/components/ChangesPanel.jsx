import React, { useState } from 'react'
import { formatBytes } from '../utils'

const CATEGORIES = [
  { key: 'newly_orphaned',      label: 'Became Orphaned',      color: 'var(--yellow)', icon: '⚠', navigable: true  },
  { key: 'new_duplicates',      label: 'New Duplicates',        color: 'var(--purple)', icon: '⊕', navigable: true  },
  { key: 'newly_imported',      label: 'Newly Imported',        color: 'var(--green)',  icon: '✓', navigable: true  },
  { key: 'resolved_duplicates', label: 'Duplicates Resolved',   color: 'var(--blue)',   icon: '✓', navigable: true  },
  { key: 'new_files',           label: 'New Files',             color: 'var(--text-dim)', icon: '+', navigable: true  },
  { key: 'removed_files',       label: 'Removed Files',         color: 'var(--text-dim)', icon: '−', navigable: false },
]

export default function ChangesPanel({ changes, prevRanAt, currRanAt, onNavigate, onReveal }) {
  const [expanded, setExpanded] = useState(null)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('auditorr_changes_collapsed') === '1')
  // Persist dismissed state keyed to the scan timestamp so it reappears after a new scan
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

  const hasItems = CATEGORIES.some(c => changes[c.key]?.length > 0)
  if (!hasItems && changes.score_delta == null) return null

  const scoreDelta = changes.score_delta
  const fmtDate = dt => dt ? new Date(dt).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''

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
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 16px', borderBottom: (hasItems && !collapsed) ? '1px solid var(--border)' : 'none' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase' }}>
            Changes since last scan
          </span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)' }}>
            {fmtDate(prevRanAt)} → {fmtDate(currRanAt)}
          </span>
          {scoreDelta != null && (
            <span style={{
              fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
              color: scoreDelta >= 0 ? 'var(--green)' : 'var(--red)',
              background: (scoreDelta >= 0 ? 'var(--green)' : 'var(--red)') + '15',
              border: `1px solid ${(scoreDelta >= 0 ? 'var(--green)' : 'var(--red)')}30`,
              borderRadius: 99, padding: '2px 8px',
            }}>
              {scoreDelta >= 0 ? '+' : ''}{scoreDelta} pts
            </span>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <button onClick={handleCollapse} style={{
            background: 'none', border: 'none', color: 'var(--text-dim)',
            fontFamily: 'var(--mono)', fontSize: 10, cursor: 'pointer', lineHeight: 1, padding: '2px 5px',
          }}>{collapsed ? '▶' : '▼'}</button>
          <button onClick={handleDismiss} style={{
            background: 'none', border: 'none', color: 'var(--text-dim)',
            fontSize: 16, cursor: 'pointer', lineHeight: 1, padding: '0 4px',
          }}>×</button>
        </div>
      </div>

      {/* Category rows */}
      {hasItems && !collapsed && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 0 }}>
          {CATEGORIES.map(cat => {
            const items = changes[cat.key] || []
            if (!items.length) return null
            const isOpen = expanded === cat.key
            return (
              <div key={cat.key} style={{ width: '100%', borderBottom: '1px solid var(--border)' }}>
                <button
                  onClick={() => setExpanded(isOpen ? null : cat.key)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10, width: '100%',
                    padding: '9px 16px', background: isOpen ? 'var(--surface2)' : 'transparent',
                    border: 'none', cursor: 'pointer', textAlign: 'left',
                    transition: 'background 0.12s',
                  }}
                >
                  <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: cat.color, width: 14 }}>{cat.icon}</span>
                  <span style={{ fontSize: 12, color: 'var(--text)', flex: 1 }}>{cat.label}</span>
                  <span style={{
                    fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
                    color: cat.color, background: cat.color + '18',
                    border: `1px solid ${cat.color}30`, borderRadius: 4, padding: '1px 6px',
                  }}>{items.length}</span>
                  <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>
                    {isOpen ? '▲' : '▼'}
                  </span>
                </button>

                {isOpen && (
                  <div style={{ background: 'var(--surface2)', padding: '8px 16px 12px 40px', maxHeight: 220, overflowY: 'auto' }}>
                    {items.map((item, i) => (
                      <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '4px 0', borderBottom: i < items.length - 1 ? '1px solid var(--border)' : 'none' }}>
                        {cat.navigable ? (
                          <button
                            onClick={() => { if (onReveal && item.tab) onReveal(item.path, item.tab) }}
                            title="Click to reveal in file explorer"
                            style={{
                              background: 'none', border: 'none', cursor: 'pointer',
                              fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--accent)',
                              textAlign: 'left', maxWidth: '80%', overflow: 'hidden',
                              textOverflow: 'ellipsis', whiteSpace: 'nowrap', padding: 0,
                            }}
                          >
                            {item.path}
                          </button>
                        ) : (
                          <span style={{
                            fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)',
                            display: 'block', maxWidth: '80%', overflow: 'hidden',
                            textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                          }}>
                            {item.path}
                          </span>
                        )}
                        {item.size != null && (
                          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', flexShrink: 0, marginLeft: 8 }}>
                            {formatBytes(item.size)}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
