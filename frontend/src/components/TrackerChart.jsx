import React, { useState, useRef, useEffect } from 'react'
import { formatBytes } from '../utils'

// The Trackers page's chart (2026-11-13; it was MultiLineChart). Hand-rolled
// SVG, as the app's other charts are, in two forms:
//   'stack'  stacked bars, one per day: Upload activity's chart, which moved up
//            here as Uploaded's Daily view when its card went. A day's total
//            means something, so the bars add up to it.
//   'line'   one stroked line per tracker, each named at its end, so a line can
//            be told by its label and not only by its colour.
// Series: { name, color, values: number[], visible }. `dates` are formatted
// labels, one per point. `unit` is 'bytes' | 'pct'; `signed` lets values go
// below zero (Seeding's Change view) and prints a sign; `baseline` pins the
// axis to 0. The axis steps are round in the unit the labels print: binary
// for bytes (50G, 100G), because formatBytes is binary and decimal steps
// printed as 46.6G and 93.1G.

const K = 1024
const UNITS = ['B', 'K', 'M', 'G', 'T', 'P']

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)))
  const f = raw / p
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p
}

// Ticks from lo to hi (both included, both on a step), about `count` intervals.
function niceTicks(lo, hi, unit, count = 4) {
  if (hi <= lo) hi = lo + (unit === 'pct' ? 1 : K)
  let scale = 1
  if (unit === 'bytes') {
    const m = Math.max(Math.abs(lo), Math.abs(hi))
    const i = Math.max(0, Math.min(UNITS.length - 1, Math.floor(Math.log(m) / Math.log(K))))
    scale = Math.pow(K, i)
  }
  const step = niceStep((hi - lo) / scale / count) * scale
  const first = Math.floor(lo / step + 1e-9) * step
  const last = Math.ceil(hi / step - 1e-9) * step
  const ticks = []
  for (let v = first; v <= last + step * 1e-6; v += step) ticks.push(Math.abs(v) < step * 1e-6 ? 0 : v)
  return { ticks, step }
}

const sign = (v, signed) => (signed && v > 0 ? '+' : v < 0 ? '−' : '')

function fmtAxis(v, unit, step, signed) {
  if (unit === 'pct') {
    const d = step < 1 ? (step < 0.1 ? 2 : 1) : 0
    return sign(v, signed) + Math.abs(v).toFixed(d) + '%'
  }
  if (v === 0) return '0'
  const a = Math.abs(v)
  const i = Math.max(0, Math.min(UNITS.length - 1, Math.floor(Math.log(Math.max(a, step)) / Math.log(K))))
  const n = a / Math.pow(K, i)
  const s = n >= 100 || Number.isInteger(n) ? String(Math.round(n)) : n.toFixed(n >= 10 ? 1 : 2).replace(/\.?0+$/, '')
  return sign(v, signed) + s + UNITS[i]
}

export function fmtValue(v, unit, signed) {
  if (unit === 'pct') return sign(v, signed) + Math.abs(v).toFixed(1) + '%'
  return sign(v, signed) + formatBytes(Math.abs(v))
}

// The end-of-line labels' value: short, so the column stays narrow.
function fmtShort(v, unit, signed) {
  if (unit === 'pct') return sign(v, signed) + Math.abs(v).toFixed(1) + '%'
  if (!v) return '0'
  const a = Math.abs(v)
  const i = Math.max(0, Math.min(UNITS.length - 1, Math.floor(Math.log(a) / Math.log(K))))
  const n = a / Math.pow(K, i)
  return sign(v, signed) + (n >= 100 ? Math.round(n) : n.toFixed(1)) + UNITS[i]
}

const LABEL_GAP = 14

// Spread labels apart vertically, keeping each as near its line as it can.
function spread(items, top, bottom) {
  const out = items.slice().sort((a, b) => a.y - b.y)
  for (let i = 0; i < out.length; i++) {
    out[i].ly = Math.max(out[i].y, i ? out[i - 1].ly + LABEL_GAP : top)
  }
  for (let i = out.length - 1; i >= 0; i--) {
    const cap = i === out.length - 1 ? bottom : out[i + 1].ly - LABEL_GAP
    if (out[i].ly > cap) out[i].ly = cap
  }
  return out
}

export default function TrackerChart({
  series, dates, kind = 'line', unit, signed = false, baseline = false, height = 320, hovered,
}) {
  const ref = useRef(null)
  const [w, setW] = useState(900)
  const [cursor, setCursor] = useState(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => setW(el.clientWidth)
    measure()
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    ro?.observe(el)
    window.addEventListener('resize', measure)
    return () => { ro?.disconnect(); window.removeEventListener('resize', measure) }
  }, [])

  const N = dates.length
  const vis = series.filter(s => s.visible && s.values.length)
  const stack = kind === 'stack'
  const labelled = !stack && vis.length > 0 && N > 0
  const gL = 52, gT = 14, gB = 28
  // The label column fits the longest name it shows (value ~56px + name at
  // ~6.7px a character), within bounds, so a full host stays readable.
  const longest = labelled ? Math.max(...vis.map(s => s.name.length)) : 0
  const gR = labelled ? Math.round(Math.min(250, Math.max(120, 66 + longest * 6.7, w * 0.08))) : 24
  const plotW = Math.max(10, w - gL - gR), plotH = height - gT - gB

  // Day totals for the bars; the domain over what's drawn.
  const totals = stack ? dates.map((_, i) => vis.reduce((s, x) => s + (x.values[i] || 0), 0)) : null
  let lo = Infinity, hi = -Infinity
  if (stack) totals.forEach(v => { if (v > hi) hi = v; if (v < lo) lo = v })
  else vis.forEach(s => s.values.forEach(v => { if (v > hi) hi = v; if (v < lo) lo = v }))
  if (!isFinite(lo)) { lo = 0; hi = 1 }
  if (stack || baseline) lo = Math.min(0, lo)
  if (signed) { lo = Math.min(0, lo); hi = Math.max(0, hi) }
  if (!stack && !baseline && !signed) {
    const pad = (hi - lo) * 0.1
    lo = Math.max(0, lo - pad); hi = hi + pad
  }
  if (lo === hi) hi = lo + (unit === 'pct' ? 1 : K * K * K)
  const { ticks, step } = niceTicks(lo, hi, unit)
  const yMin = ticks[0], yMax = ticks[ticks.length - 1]

  const band = N ? plotW / N : plotW
  const barW = Math.max(1, Math.min(80, band * 0.72))
  const x = stack
    ? (i) => gL + band * (i + 0.5)
    : (i) => (N <= 1 ? gL + plotW / 2 : gL + (i / (N - 1)) * plotW)
  const y = (v) => gT + (1 - (v - yMin) / (yMax - yMin)) * plotH

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
    let i = stack ? Math.floor((mx - gL) / band) : Math.round((mx - gL) / plotW * Math.max(1, N - 1))
    setCursor(Math.max(0, Math.min(N - 1, i)))
  }

  const dimOf = (name) => hovered && hovered !== name

  // Tooltip rows: a bar's segments top-down (as drawn), a line's by value.
  const tipRows = cursor == null ? [] : (stack
    ? vis.slice().reverse().map(s => ({ name: s.name, color: s.color, v: s.values[cursor] || 0 })).filter(r => r.v > 0)
    : vis.map(s => ({ name: s.name, color: s.color, v: s.values[cursor] }))
        .filter(r => Number.isFinite(r.v)).sort((a, b) => b.v - a.v))
  const tipX = cursor != null ? x(cursor) : 0
  const tipLeft = tipX > plotW * 0.62 + gL

  const labels = labelled
    ? spread(vis.map(s => {
        const v = s.values[N - 1]
        return { s, v, y: y(Number.isFinite(v) ? v : 0) }
      }), gT + 4, gT + plotH)
    : []
  const nameChars = Math.max(6, Math.floor((gR - 62) / 6.7))
  const clip = (n) => (n.length > nameChars ? n.slice(0, nameChars - 1) + '…' : n)

  return (
    <div ref={ref} style={{ position: 'relative', width: '100%', height }}>
      <svg width={w} height={height} style={{ display: 'block' }}>
        {/* gridlines + y labels; zero is the solid line */}
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={gL} y1={y(t)} x2={gL + plotW} y2={y(t)} stroke="var(--border)"
              strokeOpacity={t === 0 ? 0.9 : 0.45} strokeDasharray={t === 0 ? '0' : '2 4'} />
            <text x={gL - 8} y={y(t) + 3.5} textAnchor="end" fontFamily="var(--mono)" style={{ fontSize: 'var(--font-sm)' }} fill="var(--text-dim)">{fmtAxis(t, unit, step, signed)}</text>
          </g>
        ))}
        {xTickIdx.map(i => (
          <text key={i} x={x(i)} y={height - 9} textAnchor="middle" fontFamily="var(--mono)" style={{ fontSize: 'var(--font-sm)' }} fill="var(--text-dim)">{dates[i]}</text>
        ))}

        {stack ? (
          <>
            {cursor != null && <rect x={gL + band * cursor} y={gT} width={band} height={plotH} fill="var(--surface2)" />}
            {dates.map((_, i) => {
              let acc = 0
              const top = (() => { for (let k = vis.length - 1; k >= 0; k--) if ((vis[k].values[i] || 0) > 0) return k; return -1 })()
              return vis.map((s, k) => {
                const v = s.values[i] || 0
                if (v <= 0) return null
                const y1 = y(acc + v), y0 = y(acc)
                acc += v
                const h = Math.max(0, y0 - y1)
                if (h <= 0) return null
                const bx = x(i) - barW / 2
                const op = dimOf(s.name) ? 0.22 : 1
                if (k === top && h > 3) {
                  const r = Math.min(3, barW / 2)
                  return <path key={s.name + i} opacity={op} fill={s.color}
                    d={`M${bx},${y0} V${y1 + r} a${r},${r} 0 0 1 ${r},-${r} h${barW - 2 * r} a${r},${r} 0 0 1 ${r},${r} V${y0} Z`} />
                }
                return <rect key={s.name + i} x={bx} y={y1} width={barW} height={h} fill={s.color} opacity={op} />
              })
            })}
          </>
        ) : (
          <>
            {cursor != null && <line x1={x(cursor)} y1={gT} x2={x(cursor)} y2={gT + plotH} stroke="var(--border2)" strokeWidth="1" />}
            {vis.map(s => (
              <path key={s.name} d={linePath(s.values)} fill="none" stroke={s.color}
                strokeWidth={hovered === s.name ? 2.4 : 1.6} strokeLinejoin="round" strokeLinecap="round"
                opacity={dimOf(s.name) ? 0.16 : 0.95} style={{ transition: 'opacity 0.12s' }} />
            ))}
            {cursor != null && vis.map(s => {
              const v = s.values[cursor]
              if (!Number.isFinite(v)) return null
              return <circle key={s.name} cx={x(cursor)} cy={y(v)} r={hovered === s.name ? 3.6 : 2.8}
                fill="var(--bg)" stroke={s.color} strokeWidth="1.8" opacity={dimOf(s.name) ? 0.2 : 1} />
            })}
            {/* Each line's name and last value at its end: the cue that isn't colour. */}
            {labels.map(l => {
              const x0 = gL + plotW, lx = x0 + 10
              const op = dimOf(l.s.name) ? 0.3 : 1
              return (
                <g key={l.s.name} opacity={op}>
                  <path d={`M${x0},${l.y} L${x0 + 5},${l.y} L${lx - 3},${l.ly}`} fill="none" stroke={l.s.color} strokeWidth="1.2" />
                  <text x={lx} y={l.ly + 3.5} fontFamily="var(--mono)" style={{ fontSize: 'var(--font-sm)' }}>
                    <tspan fill="var(--text)" fontWeight="600">{fmtShort(Number.isFinite(l.v) ? l.v : 0, unit, signed)}</tspan>
                    <tspan fill="var(--text-dim)" dx="7">{clip(l.s.name)}</tspan>
                  </text>
                </g>
              )
            })}
          </>
        )}
        <rect x={gL} y={gT} width={plotW} height={plotH} fill="transparent"
          onMouseMove={onMove} onMouseLeave={() => setCursor(null)} />
      </svg>

      {cursor != null && tipRows.length > 0 && (
        <div style={{
          position: 'absolute', top: gT + 4,
          left: tipLeft ? undefined : tipX + 14, right: tipLeft ? (w - tipX + 14) : undefined,
          background: 'var(--surface)', border: '1px solid var(--border2)', borderRadius: 8,
          boxShadow: 'var(--shadow-pop)', padding: '8px 10px', pointerEvents: 'none', zIndex: 5, minWidth: 170,
        }}>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginBottom: 6 }}>{dates[cursor]}</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {tipRows.map(r => (
              <div key={r.name} style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', opacity: dimOf(r.name) ? 0.45 : 1 }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: r.color, flexShrink: 0 }} />
                <span style={{ color: 'var(--text-dim)', flex: 1 }}>{r.name}</span>
                <span style={{ color: 'var(--text)', fontWeight: 600 }}>{fmtValue(r.v, unit, signed)}</span>
              </div>
            ))}
          </div>
          {stack && tipRows.length > 1 && (
            <div style={{ marginTop: 6, paddingTop: 6, borderTop: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', gap: 12 }}>
              <span style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>Total</span>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fontWeight: 700, color: 'var(--text)' }}>{fmtValue(totals[cursor], unit, false)}</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
