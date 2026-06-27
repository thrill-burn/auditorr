import React, { useState } from 'react'
import { Checkbox } from './workflows/shared'
import { formatBytes } from '../utils'

// Comparison table — doubles as the chart's legend and tracker selector.
// Whole-row click toggles a tracker's line; the colored checkbox mirrors the
// series color; metric columns are sort buttons; value cells in the navigable
// metrics (Seeding / Orphaned / Not Imported) deep-link into a filtered
// Torrents view — same UI, no extra buttons.

// Resolve a metric's numeric value + display for a tracker's scalar stats.
function cellData(s, m) {
  if (m.key === 'yield') {
    const frac = s.yield
    const pct = frac == null ? null : frac * 100
    return { raw: pct, display: frac == null ? '—' : pct.toFixed(2) + '%', zero: frac == null || frac === 0 }
  }
  const field = m.key === 'seeding' ? 'seeding_size'
              : m.key === 'uploaded' ? 'uploaded'
              : m.key === 'orphaned' ? 'orphaned_size'
              : 'not_imported_size'
  const raw = s[field] || 0
  return { raw, display: formatBytes(raw), zero: raw === 0 }
}

function ValueCell({ stats, m, isActive, on, tracker, onNavigate }) {
  const [hov, setHov] = useState(false)
  const { raw, display, zero } = cellData(stats, m)
  const navigable = !!m.nav && raw > 0 && onNavigate
  const click = navigable ? (e) => { e.stopPropagation(); onNavigate({ tab: 'torrents', tracker, ...m.nav }) } : undefined
  return (
    <div
      onClick={click}
      onMouseEnter={navigable ? () => setHov(true) : undefined}
      onMouseLeave={navigable ? () => setHov(false) : undefined}
      title={navigable ? `View ${tracker} ${m.label.toLowerCase()} in Torrents` : undefined}
      style={{
        display: 'flex', alignItems: 'center', justifyContent: 'flex-end', padding: '0 12px', height: '100%',
        background: isActive ? m.color + '0d' : 'transparent', opacity: on ? 1 : 0.4,
        cursor: navigable ? 'pointer' : 'default',
      }}>
      <span style={{
        fontFamily: 'var(--mono)', fontSize: 12.5, fontWeight: isActive ? 700 : 500,
        color: navigable && hov ? m.color : zero ? 'var(--text-faint)' : isActive ? 'var(--text)' : 'var(--text-dim)',
        textDecoration: navigable && hov ? 'underline' : 'none', textUnderlineOffset: 3,
        transition: 'color 0.1s',
      }}>{display}</span>
    </div>
  )
}

export default function CompareTable({
  rows, statsMap, metrics, metricKey, colorFor,
  visible, onToggle, onSolo, onToggleAll,
  hovered, onHover, sortKey, sortDir, onSort, onNavigate,
}) {
  const grid = `230px repeat(${metrics.length}, 1fr)`
  const allOn = rows.length > 0 && visible.length === rows.length
  const someOn = visible.length > 0 && !allOn

  const Th = ({ m }) => {
    const active = sortKey === m.key
    return (
      <button onClick={() => onSort(m.key)} title={`Sort by ${m.label}`} style={{
        display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 5, width: '100%',
        background: metricKey === m.key ? m.color + '14' : 'none', border: 'none', cursor: 'pointer',
        padding: '0 12px', height: '100%', fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: 1,
        textTransform: 'uppercase', color: active ? 'var(--text)' : 'var(--text-dim)', fontWeight: active ? 700 : 500,
      }}>
        {m.label}{active ? (sortDir === 'asc' ? ' ↑' : ' ↓') : ''}
      </button>
    )
  }

  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', overflow: 'hidden' }}>
      {/* header */}
      <div style={{ display: 'grid', gridTemplateColumns: grid, background: 'var(--surface2)', borderBottom: '1px solid var(--border)', height: 34, alignItems: 'stretch' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '0 14px' }}>
          <Checkbox checked={allOn} indeterminate={someOn} onChange={onToggleAll} />
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--text-dim)' }}>Tracker</span>
        </div>
        {metrics.map(m => <Th key={m.key} m={m} />)}
      </div>

      {/* rows */}
      {rows.map(t => {
        const stats = statsMap[t] || {}
        const on = visible.includes(t)
        const emph = hovered === t
        const c = colorFor(t)
        return (
          <div key={t}
            onMouseEnter={() => onHover(t)} onMouseLeave={() => onHover(null)}
            onClick={() => onToggle(t)}
            style={{
              display: 'grid', gridTemplateColumns: grid, height: 44, alignItems: 'center', cursor: 'pointer',
              borderBottom: '1px solid var(--border)', background: emph ? 'var(--surface2)' : 'transparent',
              transition: 'background 0.12s',
            }}>
            {/* name cell */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '0 14px', minWidth: 0 }}>
              <span title={on ? 'Hide from chart' : 'Show on chart'} style={{
                width: 16, height: 16, borderRadius: 4, flexShrink: 0,
                border: `1.5px solid ${on ? c : 'var(--border2)'}`, background: on ? c : 'transparent',
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.1s',
              }}>
                {on && <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#0b0b0c" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>}
              </span>
              <span style={{
                fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 600, color: on ? 'var(--text)' : 'var(--text-dim)',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', opacity: on ? 1 : 0.55,
              }}>{t}</span>
              {emph && <button onClick={(e) => { e.stopPropagation(); onSolo(t) }} title="Show only this tracker" style={{
                marginLeft: 'auto', flexShrink: 0, background: 'var(--surface3)', border: '1px solid var(--border2)', borderRadius: 5, cursor: 'pointer',
                padding: '2px 8px', fontFamily: 'var(--mono)', fontSize: 10.5, letterSpacing: 0.5, color: 'var(--text-dim)',
              }}>only</button>}
            </div>
            {/* value cells */}
            {metrics.map(m => (
              <ValueCell key={m.key} stats={stats} m={m} isActive={metricKey === m.key} on={on} tracker={t} onNavigate={onNavigate} />
            ))}
          </div>
        )
      })}
    </div>
  )
}
