import React, { useState } from 'react'
import { formatBytes } from '../utils'
import { CHANGE_CATEGORIES } from './changeCategories'
import { Segmented, IconButton, CloseButton, Dot } from './workflows/shared'

const ROW_HEIGHT = 36

export default function ChangesPanel({ changes, prevRanAt, currRanAt, onReveal }) {
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

  const allRows = []
  for (const cat of CHANGE_CATEGORIES) {
    const items = (changes[cat.diffKey] || []).filter(item =>
      cat.tab == null || item.tab === cat.tab
    )
    for (const item of items) allRows.push({ ...item, cat })
  }

  const hasItems = allRows.length > 0
  if (!hasItems && changes.score_delta == null) return null

  const rows = activeFilter ? allRows.filter(r => r.cat.key === activeFilter) : allRows
  const counts = {}
  for (const cat of CHANGE_CATEGORIES) {
    counts[cat.key] = (changes[cat.diffKey] || []).filter(item =>
      cat.tab == null || item.tab === cat.tab
    ).length
  }

  const scoreDelta = changes.score_delta
  const fmtDate = dt => dt ? new Date(dt).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''
  const showBody = hasItems && !collapsed

  return (
    <div className="ui-card" style={{ margin: '0 0 16px', overflow: 'hidden' }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '12px 16px',
        borderBottom: showBody ? '1px solid var(--border)' : 'none',
        gap: 12,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', minWidth: 0 }}>
          <span className="ui-section-title" style={{ textAlign: 'left' }}>
            Changes since last scan
          </span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
            {fmtDate(prevRanAt)} - {fmtDate(currRanAt)}
          </span>
          {scoreDelta != null && (
            <span style={{
              fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', fontWeight: 600,
              color: 'var(--text-dim)',
              border: '1px solid var(--border2)',
              borderRadius: 6, padding: '2px 8px',
              display: 'inline-flex', alignItems: 'center', gap: 6,
            }}>
              <span className="ui-status-dot" style={{ width: 6, height: 6, background: scoreDelta >= 0 ? 'var(--green)' : 'var(--red)' }} />
              {scoreDelta >= 0 ? '+' : ''}{scoreDelta} pts
            </span>
          )}
          {hasItems && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
              {allRows.length} {allRows.length === 1 ? 'change' : 'changes'}
            </span>
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          <IconButton onClick={handleCollapse} title={collapsed ? 'Expand changes' : 'Collapse changes'} pressed={!collapsed}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
              strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
              style={{ transform: collapsed ? 'none' : 'rotate(180deg)', transition: 'transform 0.15s' }}>
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </IconButton>
          <CloseButton onClick={handleDismiss} title="Dismiss changes" />
        </div>
      </div>

      {showBody && (
        <>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap',
            padding: '9px 16px', borderBottom: '1px solid var(--border)',
            background: 'var(--surface2)',
          }}>
            <Segmented label="Change category" value={activeFilter} onChange={setActiveFilter}
              options={[
                { value: null, label: `All (${allRows.length})` },
                ...CHANGE_CATEGORIES.filter(cat => counts[cat.key]).map(cat => ({
                  value: cat.key,
                  icon: <Dot color={cat.color} size={6} />,
                  label: `${cat.label} (${counts[cat.key]})`,
                })),
              ]} />
          </div>

          <div style={{
            display: 'grid',
            gridTemplateColumns: '140px 1fr 84px',
            padding: '5px 16px',
            borderBottom: '1px solid var(--border)',
            background: 'var(--surface2)',
          }}>
            <span className="ui-table-header">Type</span>
            <span className="ui-table-header">Path</span>
            <span className="ui-table-header" style={{ textAlign: 'right' }}>Size</span>
          </div>

          <div style={{ maxHeight: 320, overflowY: 'auto' }}>
            {rows.map((row, i) => (
              <div key={`${row.cat.key}-${row.path}-${i}`} style={{
                display: 'grid',
                gridTemplateColumns: '140px 1fr 84px',
                alignItems: 'center',
                height: ROW_HEIGHT,
                padding: '0 16px',
                borderBottom: i < rows.length - 1 ? '1px solid var(--border)' : 'none',
                background: 'var(--surface)',
                boxSizing: 'border-box',
                overflow: 'hidden',
              }}>
                <div>
                  <span style={{
                    fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', fontWeight: 600,
                    color: 'var(--text)',
                    border: '1px solid var(--border2)',
                    borderRadius: 6, padding: '1px 7px',
                    whiteSpace: 'nowrap',
                    display: 'inline-flex', alignItems: 'center', gap: 6,
                  }}>
                    <span className="ui-status-dot" style={{ width: 6, height: 6, background: row.cat.color }} />
                    {row.cat.label}
                  </span>
                </div>

                <div style={{ overflow: 'hidden', paddingRight: 8 }}>
                  {row.cat.navigable ? (
                    <button
                      onClick={() => { if (onReveal && row.tab) onReveal(row.path, row.tab) }}
                      title="Click to reveal in file explorer"
                      style={{
                        background: 'none', border: 'none', cursor: 'pointer',
                        fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text)',
                        textAlign: 'left', width: '100%', overflow: 'hidden',
                        textOverflow: 'ellipsis', whiteSpace: 'nowrap', padding: 0,
                        display: 'block',
                      }}
                      onMouseEnter={e => { e.currentTarget.style.color = 'var(--blue)' }}
                      onMouseLeave={e => { e.currentTarget.style.color = 'var(--text)' }}
                    >
                      {row.path}
                    </button>
                  ) : (
                    <span style={{
                      fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)',
                      display: 'block', overflow: 'hidden',
                      textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>
                      {row.path}
                    </span>
                  )}
                </div>

                <div style={{ textAlign: 'right' }}>
                  {row.size != null && (
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
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
