import React, { useState, useRef, useEffect } from 'react'
import { formatBytes, formatBytesCompact } from '../utils'

// Stroked multi-series line chart with crosshair tooltip and per-metric
// y-axis rescaling. Hand-rolled SVG (the app already hand-rolls its charts;
// recharts can't hit the emphasis/tooltip spec cleanly). Series shape:
//   { name, color, values: number[], visible }
// `dates` are pre-formatted axis labels (one per point), `unit` is
// 'bytes' | 'pct', `baseline` pins yMin to 0 for cumulative metrics.

const fmtVal  = (v, unit) => unit === 'pct' ? v.toFixed(1) + '%' : formatBytes(v)
const fmtAxis = (v, unit) => unit === 'pct' ? v.toFixed(1) + '%' : formatBytesCompact(v)

export default function MultiLineChart({ series, dates, unit, baseline, height = 320, hovered }) {
  const ref = useRef(null)
  const [w, setW] = useState(900)
  const [cursor, setCursor] = useState(null) // day index under pointer

  useEffect(() => {
    const measure = () => { if (ref.current) setW(ref.current.clientWidth) }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [])

  const gL = 48, gR = 16, gT = 14, gB = 28
  const plotW = Math.max(10, w - gL - gR), plotH = height - gT - gB
  const N = dates.length
  const denom = Math.max(1, N - 1)
  const vis = series.filter(s => s.visible && s.values.length)

  // y-domain across visible series only
  let max = 0, min = Infinity
  vis.forEach(s => s.values.forEach(v => { if (v > max) max = v; if (v < min) min = v }))
  if (!isFinite(min)) { min = 0; max = 1 }
  if (max === min) max = min + 1
  let yMin = baseline ? 0 : min - (max - min) * 0.12
  let yMax = max + (max - min) * 0.08
  if (yMin < 0) yMin = 0
  if (yMax === yMin) yMax = yMin + 1

  const x = (i) => gL + (i / denom) * plotW
  const y = (v) => gT + (1 - (v - yMin) / (yMax - yMin)) * plotH

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map(f => yMin + f * (yMax - yMin))
  const xTickIdx = (() => {
    if (N <= 1) return N === 1 ? [0] : []
    const count = Math.min(6, N)
    const out = []
    for (let k = 0; k < count; k++) out.push(Math.round(k * (N - 1) / (count - 1)))
    return [...new Set(out)]
  })()

  const linePath = (vals) => vals.map((v, i) => `${i === 0 ? 'M' : 'L'} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(' ')

  const onMove = (e) => {
    if (N === 0) return
    const rect = e.currentTarget.getBoundingClientRect()
    const mx = e.clientX - rect.left
    let i = Math.round((mx - gL) / plotW * denom)
    i = Math.max(0, Math.min(N - 1, i))
    setCursor(i)
  }

  // tooltip rows: visible series at the cursor, sorted by value desc
  const tipRows = cursor != null
    ? vis.map(s => ({ name: s.name, color: s.color, v: s.values[cursor] }))
         .filter(r => Number.isFinite(r.v))
         .sort((a, b) => b.v - a.v)
    : []
  const tipX = cursor != null ? x(cursor) : 0
  const tipLeft = tipX > plotW * 0.62 + gL

  return (
    <div ref={ref} style={{ position: 'relative', width: '100%', height }}>
      <svg width={w} height={height} style={{ display: 'block' }}>
        {/* gridlines + y labels */}
        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={gL} y1={y(t)} x2={w - gR} y2={y(t)} stroke="var(--border)" strokeOpacity={i === 0 ? 0.9 : 0.45} strokeDasharray={i === 0 ? '0' : '2 4'} />
            <text x={gL - 8} y={y(t) + 3.5} textAnchor="end" fontFamily="var(--mono)" fontSize="10" fill="var(--text-faint)">{fmtAxis(t, unit)}</text>
          </g>
        ))}
        {/* x labels */}
        {xTickIdx.map(i => (
          <text key={i} x={x(i)} y={height - 9} textAnchor="middle" fontFamily="var(--mono)" fontSize="10" fill="var(--text-faint)">{dates[i]}</text>
        ))}
        {/* crosshair */}
        {cursor != null && <line x1={x(cursor)} y1={gT} x2={x(cursor)} y2={gT + plotH} stroke="var(--border2)" strokeWidth="1" />}
        {/* series lines */}
        {vis.map(s => {
          const dim = hovered && hovered !== s.name
          const emph = hovered === s.name
          return <path key={s.name} d={linePath(s.values)} fill="none" stroke={s.color}
            strokeWidth={emph ? 2.4 : 1.6} strokeLinejoin="round" strokeLinecap="round"
            opacity={dim ? 0.16 : 0.95} style={{ transition: 'opacity 0.12s' }} />
        })}
        {/* cursor dots */}
        {cursor != null && vis.map(s => {
          const v = s.values[cursor]
          if (!Number.isFinite(v)) return null
          const dim = hovered && hovered !== s.name
          return <circle key={s.name} cx={x(cursor)} cy={y(v)} r={hovered === s.name ? 3.6 : 2.8}
            fill="var(--bg)" stroke={s.color} strokeWidth="1.8" opacity={dim ? 0.2 : 1} />
        })}
        {/* pointer capture */}
        <rect x={gL} y={gT} width={plotW} height={plotH} fill="transparent"
          onMouseMove={onMove} onMouseLeave={() => setCursor(null)} />
      </svg>

      {cursor != null && tipRows.length > 0 && (
        <div style={{
          position: 'absolute', top: gT + 4,
          left: tipLeft ? undefined : tipX + 12, right: tipLeft ? (w - tipX + 12) : undefined,
          background: 'var(--surface)', border: '1px solid var(--border2)', borderRadius: 8,
          boxShadow: 'var(--shadow-pop)', padding: '8px 10px', pointerEvents: 'none', zIndex: 5, minWidth: 150,
        }}>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--text-dim)', marginBottom: 6, letterSpacing: 0.5 }}>{dates[cursor]}</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {tipRows.map(r => (
              <div key={r.name} style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'var(--mono)', fontSize: 11.5 }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: r.color, flexShrink: 0 }} />
                <span style={{ color: 'var(--text-dim)', flex: 1 }}>{r.name}</span>
                <span style={{ color: 'var(--text)', fontWeight: 600 }}>{fmtVal(r.v, unit)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
