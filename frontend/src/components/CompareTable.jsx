import React, { useState } from 'react'
import { Checkbox, Button } from './workflows/shared'
import { tint } from '../utils'

// Comparison table — doubles as the chart's legend and tracker selector.
// Whole-row click toggles a tracker's line; the colored checkbox mirrors the
// series color; column headers are sort buttons; a column with `nav` makes its
// value cells drill-downs into File Explorer — same UI, no extra buttons.
//
// Columns are { key, label, title?, color?, metric?, value(s) → number|null,
// display(s) → string, nav?(tracker) → action }. `metric` ties a column to the
// chart metric of the same key, which tints it while that metric is drawn.
// Two are table-only (2026-11-13): Seeded only here and Torrents.

function ValueCell({ stats, col, isActive, on, tracker, onNavigate }) {
  const [hov, setHov] = useState(false)
  const raw = col.value(stats)
  const display = col.display(stats)
  const zero = raw == null || raw === 0
  const navigable = !!col.nav && raw > 0 && onNavigate
  const click = navigable ? (e) => { e.stopPropagation(); onNavigate(col.nav(tracker)) } : undefined
  const hue = col.color || 'var(--accent)'
  return (
    <div
      onClick={click}
      onMouseEnter={navigable ? () => setHov(true) : undefined}
      onMouseLeave={navigable ? () => setHov(false) : undefined}
      title={navigable ? col.navTitle(tracker) : undefined}
      style={{
        display: 'flex', alignItems: 'center', justifyContent: 'flex-end', padding: '0 12px', height: '100%',
        background: isActive ? tint(hue, 5) : 'transparent', opacity: on ? 1 : 0.4,
        cursor: navigable ? 'pointer' : 'default',
      }}>
      <span style={{
        fontFamily: 'var(--mono)', fontSize: 'var(--font-base)', fontWeight: isActive ? 700 : 500,
        color: navigable && hov ? hue : zero ? 'var(--text-faint)' : isActive ? 'var(--text)' : 'var(--text-dim)',
        textDecoration: navigable && hov ? 'underline' : 'none', textUnderlineOffset: 3,
        transition: 'color 0.1s',
      }}>{display}</span>
    </div>
  )
}

// A tracker whose torrents the tracker is refusing, or has dropped: a status
// dot beside its name, spelled out in the dot's title and in the health card.
function healthOf(s) {
  if (s.unregistered_count > 0) return { color: 'var(--red)', text: `${s.unregistered_count} torrent${s.unregistered_count === 1 ? '' : 's'} unregistered` }
  if (s.not_working_count > 0) return { color: 'var(--yellow)', text: `${s.not_working_count} of ${s.torrent_count} torrents: tracker not working` + (s.health_msg ? ` — ${s.health_msg}` : '') }
  return null
}

export default function CompareTable({
  rows, statsMap, columns, metricKey, colorFor,
  visible, onToggle, onSolo, onToggleAll,
  hovered, onHover, sortKey, sortDir, onSort, onNavigate,
}) {
  const grid = `230px repeat(${columns.length}, minmax(0, 1fr))`
  const allOn = rows.length > 0 && visible.length === rows.length
  const someOn = visible.length > 0 && !allOn

  const Th = ({ col }) => {
    const active = sortKey === col.key
    return (
      <button onClick={() => onSort(col.key)} title={col.title || `Sort by ${col.label}`} style={{
        display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 5, width: '100%',
        background: col.metric && metricKey === col.metric ? tint(col.color, 8) : 'none', border: 'none', cursor: 'pointer',
        padding: '0 12px', height: '100%', fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)',
        color: active ? 'var(--text)' : 'var(--text-dim)', fontWeight: active ? 700 : 500, whiteSpace: 'nowrap',
      }}>
        {col.label}{active ? (sortDir === 'asc' ? ' ↑' : ' ↓') : ''}
      </button>
    )
  }

  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', overflow: 'hidden' }}>
      {/* header */}
      <div style={{ display: 'grid', gridTemplateColumns: grid, background: 'var(--surface2)', borderBottom: '1px solid var(--border)', height: 34, alignItems: 'stretch' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '0 14px' }}>
          <Checkbox checked={allOn} indeterminate={someOn} onChange={onToggleAll} />
          <span style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', fontWeight: 500, color: 'var(--text-dim)' }}>Tracker</span>
        </div>
        {columns.map(c => <Th key={c.key} col={c} />)}
      </div>

      {/* rows */}
      {rows.map(t => {
        const stats = statsMap[t] || {}
        const on = visible.includes(t)
        const emph = hovered === t
        const c = colorFor(t)
        const health = healthOf(stats)
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
              <span title={t} style={{
                fontFamily: 'var(--mono)', fontSize: 'var(--font-md)', fontWeight: 600, color: on ? 'var(--text)' : 'var(--text-dim)',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', opacity: on ? 1 : 0.55, minWidth: 0,
              }}>{t}</span>
              {health && (
                <span title={health.text} style={{ width: 7, height: 7, borderRadius: '50%', background: health.color, flexShrink: 0 }} />
              )}
              {emph && (
                <Button size="chip" variant="subtle" title="Show only this tracker" style={{ marginLeft: 'auto' }}
                  onClick={(e) => { e.stopPropagation(); onSolo(t) }}>only</Button>
              )}
            </div>
            {columns.map(col => (
              <ValueCell key={col.key} stats={stats} col={col} isActive={!!col.metric && metricKey === col.metric} on={on} tracker={t} onNavigate={onNavigate} />
            ))}
          </div>
        )
      })}
    </div>
  )
}
