import React, { useState, useEffect, useLayoutEffect, useMemo, useRef, useCallback } from 'react'
import { api } from '../api'
import { formatBytes } from '../utils'
import { Button, Segmented, Spinner, useAuditComplete } from './workflows/shared'
import { SECTION_LABEL, CARD, Hero } from './dashKit'
import {
  STATES, PROBLEM, LINKED, ROOT_HEAD, HEAD,
  modelFromRow, modelFromFiles, modelTotals, biggestGaps, navFor, layoutMap, leafAt,
} from './libraryMapRules'

// The library map (2026-11-09): WinDirStat's idea applied to hardlinks. Every
// rectangle's area is its size on disk, grouped by top-level folder, and its
// colour is the workflow that acts on it — the four cards' colours above it.
// Each of those four also has its own pattern (stripes, dots, back-stripes,
// cross-hatch): the user is colorblind, and a colour alone isn't a difference.
// The rules, the layout and the click targets are in libraryMapRules.js.
//
// Titles by default, from the scan's stored row (`/api/library_map`, a few KB).
// Files is the same map at one tile per file, from the two file lists File
// Explorer loads, fetched the first time it's picked and never on a visit.

const LABEL = {
  [LINKED]: '', 1: 'not seeded', 2: 'orphaned', 3: 'not imported', 4: 'copies', 5: 'other',
}

// ── Colours, read from the live theme ────────────────────────────────────────
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}
function toRgb(c) {
  const s = String(c || '').trim()
  let m = s.match(/^#([0-9a-f]{3})$/i)
  if (m) return m[1].split('').map(h => parseInt(h + h, 16))
  m = s.match(/^#([0-9a-f]{6})$/i)
  if (m) return [0, 2, 4].map(i => parseInt(m[1].slice(i, i + 2), 16))
  m = s.match(/rgba?\(([^)]+)\)/i)
  if (m) return m[1].split(',').slice(0, 3).map(v => parseFloat(v))
  return [128, 128, 128]
}
const mix = (a, b, t) => a.map((v, i) => v * t + b[i] * (1 - t))
const rgb = c => `rgb(${c.map(Math.round).join(',')})`

function palette() {
  const dim = toRgb(cssVar('--text-dim')), s3 = toRgb(cssVar('--surface3'))
  const light = document.documentElement.getAttribute('data-theme') === 'light'
  return {
    surface: cssVar('--surface'), band: cssVar('--surface2'), text: cssVar('--text'), dim: cssVar('--text-dim'),
    // Linked is the neutral bulk; Other quieter still.
    c: [mix(dim, s3, 0.42), toRgb(cssVar('--blue')), toRgb(cssVar('--yellow')), toRgb(cssVar('--red')),
        toRgb(cssVar('--purple')), mix(dim, s3, 0.18)],
    ink: light ? 'rgba(255,255,255,0.55)' : 'rgba(255,255,255,0.34)',
    sm: cssVar('--font-sm') || '11px', md: cssVar('--font-base') || '12px',
    mono: cssVar('--mono'), sans: cssVar('--sans'),
  }
}

// The same patterns in CSS, for the legend's swatches.
const PATTERN_INK = 'color-mix(in srgb, white 45%, transparent)'
const SWATCH_VAR = { linked: 'var(--map-linked)', other: 'var(--map-other)' }
export function swatchStyle(s) {
  const c = SWATCH_VAR[STATES[s].color] || `var(--${STATES[s].color})`
  const w = PATTERN_INK
  switch (STATES[s].pattern) {
    case 'slash': return { background: `repeating-linear-gradient(135deg, ${c} 0 2px, ${w} 2px 3.5px), ${c}` }
    case 'back':  return { background: `repeating-linear-gradient(45deg, ${c} 0 2px, ${w} 2px 3.5px), ${c}` }
    case 'dots':  return { background: `radial-gradient(${w} 0.9px, transparent 1.2px) 0 0 / 4px 4px, ${c}` }
    case 'cross': return { background: `repeating-linear-gradient(45deg, transparent 0 2.5px, ${w} 2.5px 3.5px), repeating-linear-gradient(135deg, transparent 0 2.5px, ${w} 2.5px 3.5px), ${c}` }
    default:      return { background: c }
  }
}
function Swatch({ s }) {
  return <span style={{ width: 9, height: 9, borderRadius: 2, flexShrink: 0, display: 'inline-block', ...swatchStyle(s) }} />
}

// ── Drawing ──────────────────────────────────────────────────────────────────
const lum = c => {
  const l = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 })
  return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2]
}
// Whichever of near-black and near-white reads better on a tile.
const labelOn = c => { const L = lum(c); return (L + 0.05) / 0.05 > 1.05 / (L + 0.05) ? '#08090a' : '#f4f4f5' }

function fit(ctx, s, max) {
  if (ctx.measureText(s).width <= max) return s
  while (s.length > 1 && ctx.measureText(s + '…').width > max) s = s.slice(0, -1)
  return s.length > 1 ? s + '…' : ''
}
// The longest that fits whole, never a number cut in half.
const longest = (ctx, options, max) => options.find(t => ctx.measureText(t).width <= max) ?? ''

function pattern(ctx, l, kind, ink) {
  ctx.save()
  ctx.beginPath(); ctx.rect(l.x, l.y, l.w, l.h); ctx.clip()
  ctx.strokeStyle = ink; ctx.fillStyle = ink; ctx.lineWidth = 1.5
  ctx.beginPath()
  const step = kind === 'cross' ? 7 : 6
  if (kind === 'slash' || kind === 'cross') for (let i = -l.h; i < l.w; i += step) { ctx.moveTo(l.x + i, l.y + l.h); ctx.lineTo(l.x + i + l.h, l.y) }
  if (kind === 'back' || kind === 'cross') for (let i = -l.h; i < l.w; i += step) { ctx.moveTo(l.x + i, l.y); ctx.lineTo(l.x + i + l.h, l.y + l.h) }
  if (kind === 'dots') {
    for (let yy = l.y + 2, row = 0; yy < l.y + l.h; yy += 4.5, row++) {
      for (let xx = l.x + 2 + (row % 2 ? 2.25 : 0); xx < l.x + l.w; xx += 4.5) { ctx.moveTo(xx + 1.1, yy); ctx.arc(xx, yy, 1.1, 0, 7) }
    }
    ctx.fill()
  } else ctx.stroke()
  ctx.restore()
}

function tile(ctx, l, P) {
  const base = P.c[l.s]
  if (l.w >= 2 && l.h >= 2) {
    // A WinDirStat cushion: lit from the upper left, falling off to the far corner.
    const g = ctx.createRadialGradient(l.x + l.w * 0.3, l.y + l.h * 0.25, 0, l.x + l.w * 0.45, l.y + l.h * 0.45, Math.max(l.w, l.h) * 0.85)
    g.addColorStop(0, rgb(mix([255, 255, 255], base, 0.2)))
    g.addColorStop(0.5, rgb(base))
    g.addColorStop(1, rgb(mix([0, 0, 0], base, 0.35)))
    ctx.fillStyle = g
  } else ctx.fillStyle = rgb(base)
  ctx.fillRect(l.x, l.y, l.w, l.h)
  const kind = STATES[l.s].pattern
  if (kind && l.w >= 4 && l.h >= 4) pattern(ctx, l, kind, P.ink)
}

function pctLinked(by) {
  const v = by.reduce((a, b) => a + b, 0)
  const p = v ? by[LINKED] / v * 100 : 0
  return p >= 99.95 ? '100' : p.toFixed(p < 10 ? 1 : 0)
}
// What a library folder or title holds, worst first, in a few words.
function breakdown(by) {
  const parts = []
  if (by[LINKED]) parts.push(`${pctLinked(by)}% linked`)
  for (const s of PROBLEM) if (by[s]) parts.push(`${formatBytes(by[s])} ${LABEL[s]}`)
  return parts
}

const ROOT_LABEL = { library: 'Library folder', torrents: "Torrent folder · what isn't linked" }
const ROOT_SHORT = { library: 'Library', torrents: 'Torrents' }

function draw(canvas, model, W, H, detail) {
  const P = palette(), dpr = window.devicePixelRatio || 1
  canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr)
  canvas.style.width = W + 'px'; canvas.style.height = H + 'px'
  const ctx = canvas.getContext('2d')
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.fillStyle = P.surface; ctx.fillRect(0, 0, W, H)
  const L = layoutMap(model, W, H, detail)
  const sansSm = `500 ${P.sm} ${P.sans}`, monoSm = `500 ${P.sm} ${P.mono}`

  ctx.textBaseline = 'middle'
  for (const r of L.roots) {
    if (!r.head) continue
    ctx.font = `600 ${P.md} ${P.sans}`; ctx.fillStyle = P.text
    const label = longest(ctx, [ROOT_LABEL[r.name], ROOT_LABEL[r.name].split(' · ')[0], ROOT_SHORT[r.name]], r.w - 4)
    const lw = ctx.measureText(label).width
    ctx.fillText(label, r.x + 2, r.y + ROOT_HEAD / 2)
    ctx.font = monoSm; ctx.fillStyle = P.dim
    const size = `  ${formatBytes(r.v)}`
    if (ctx.measureText(size).width <= r.w - 4 - lw) ctx.fillText(size, r.x + 2 + lw, r.y + ROOT_HEAD / 2)
  }
  for (const f of L.folders) {
    ctx.fillStyle = P.band; ctx.fillRect(f.x, f.y, f.w, f.h)
    if (!f.head) continue
    ctx.font = monoSm
    // A torrent block is named for its state and the workflow that acts on it.
    const name = fit(ctx, f.state != null
      ? `${STATES[f.state].name}${STATES[f.state].workflow ? ' · ' + STATES[f.state].workflow : ''}`
      : (f.name || '(top level)'), f.w - 12)
    const room = f.w - 12 - ctx.measureText(name).width
    ctx.fillStyle = P.text; ctx.fillText(name, f.x + 6, f.y + HEAD / 2 + 0.5)
    const n = f.titles.length
    const rest = f.state != null
      ? longest(ctx, [`  ${formatBytes(f.v)} · ${n.toLocaleString()} release${n !== 1 ? 's' : ''}`, `  ${formatBytes(f.v)}`], room)
      : (() => { const b = breakdown(f.by); return longest(ctx, [
          `  ${[...b, `${n.toLocaleString()} titles`].join(' · ')}`, `  ${b.join(' · ')}`,
          `  ${b.slice(0, 2).join(' · ')}`, `  ${b[0] || ''}`], room) })()
    ctx.fillStyle = P.dim; ctx.fillText(rest, f.x + 6 + ctx.measureText(name).width, f.y + HEAD / 2 + 0.5)
  }
  for (const l of L.leaves) tile(ctx, l, P)
  // Titles part with a 1px line of the card, so a title reads as one block.
  ctx.strokeStyle = P.surface; ctx.lineWidth = 1
  for (const t of L.titles) if (t.w > 3 && t.h > 3) ctx.strokeRect(t.x + 0.5, t.y + 0.5, t.w - 1, t.h - 1)
  ctx.textBaseline = 'top'
  for (const t of L.titles) {
    if (t.w < 64 || t.h < 20) continue
    const under = leafAt(L, t.x + 6, t.y + 6)
    ctx.fillStyle = labelOn(P.c[under && under.T === t ? under.s : LINKED])
    ctx.font = sansSm
    ctx.fillText(fit(ctx, t.name, t.w - 10), t.x + 5, t.y + 4)
    if (t.h >= 38 && t.w >= 60) {
      ctx.globalAlpha = 0.78; ctx.font = monoSm
      const b = t.root === 'library' ? breakdown(t.by).slice(0, 1) : []
      ctx.fillText(longest(ctx, [[formatBytes(t.v), ...b].join(' · '), formatBytes(t.v)], t.w - 10), t.x + 5, t.y + 19)
      ctx.globalAlpha = 1
    }
  }
  return L
}

const mapHeight = W => Math.round(Math.min(640, Math.max(420, W * 0.42)))

// ── The card ─────────────────────────────────────────────────────────────────
const META = { fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }

function Stat({ label, value }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>{label}</span>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-md)', fontWeight: 500, color: 'var(--text)', whiteSpace: 'nowrap' }}>{value}</span>
    </div>
  )
}

function freeLine(ds) {
  if (!ds) return null
  if (ds.same || !ds.media || !ds.torrents) {
    const one = ds.torrents || ds.media
    return one ? `${formatBytes(one.free)} free` : null
  }
  return `${formatBytes(ds.torrents.free)} free for torrents · ${formatBytes(ds.media.free)} for the library`
}

function hoverText(leaf, detail) {
  const t = leaf.T, nav = navFor(leaf.s, t)
  const where = t.root === 'library' ? `${t.folder ? t.folder + ' / ' : ''}${t.name}` : t.name
  if (detail === 'files' && leaf.file) {
    const st = STATES[leaf.s]
    return { s: leaf.s, name: `${where} / ${leaf.file}`,
             facts: `${formatBytes(leaf.bytes)} · ${st.name.toLowerCase()}${st.workflow ? ` (${st.workflow})` : ''}`, nav }
  }
  const facts = t.root === 'library'
    ? [formatBytes(t.v), ...(t.by[LINKED] ? [`${pctLinked(t.by)}% linked`] : []),
       ...PROBLEM.filter(s => t.by[s]).map(s => `${formatBytes(t.by[s])} ${LABEL[s]} (${STATES[s].workflow})`)].join(' · ')
    : `${formatBytes(t.v)} ${STATES[leaf.s].name.toLowerCase()}`
  return { s: leaf.s, name: `${where}${t.folded ? ` (${t.files.toLocaleString()} files)` : ''}`, facts, nav }
}

export default function LibraryMap({ details, onNavigate, mediaFiles, torrentFiles, ensureFilesLoaded }) {
  const [row, setRow] = useState(undefined)          // undefined = loading, null = none stored yet
  const [detail, setDetail] = useState('titles')     // Titles by default, every visit (the user's call)
  const [width, setWidth] = useState(0)
  const [themeTick, setThemeTick] = useState(0)
  const [hover, setHover] = useState(null)           // { box, text }
  const wrapRef = useRef(null)
  const canvasRef = useRef(null)
  const layoutRef = useRef(null)

  const load = useCallback(() => {
    api.libraryMap().then(r => setRow(r && r.version ? r : null)).catch(() => setRow(null))
  }, [])
  useEffect(() => { load() }, [load])
  useAuditComplete(load)

  const filesReady = mediaFiles != null && torrentFiles != null
  useEffect(() => {
    if (detail !== 'files') return
    if (mediaFiles == null) ensureFilesLoaded?.('media')
    if (torrentFiles == null) ensureFilesLoaded?.('torrents')
  }, [detail, mediaFiles, torrentFiles, ensureFilesLoaded])

  const drawn = detail === 'files' && filesReady ? 'files' : 'titles'
  const model = useMemo(() => {
    if (drawn === 'files') return modelFromFiles(mediaFiles, torrentFiles)
    return row ? modelFromRow(row) : null
  }, [drawn, mediaFiles, torrentFiles, row])
  const totals = useMemo(() => (model ? modelTotals(model) : null), [model])
  const gaps = useMemo(() => (model ? biggestGaps(model) : []), [model])

  // Redraw on resize and on a theme change, which is an attribute on <html>.
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return undefined
    const ro = new ResizeObserver(([e]) => setWidth(Math.floor(e.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [model != null])
  useEffect(() => {
    const mo = new MutationObserver(() => setThemeTick(t => t + 1))
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => mo.disconnect()
  }, [])
  const height = mapHeight(width)
  useLayoutEffect(() => {
    if (!model || !width || !canvasRef.current) return
    layoutRef.current = draw(canvasRef.current, model, width, height, drawn)
    setHover(null)
  }, [model, width, height, drawn, themeTick])

  const onMove = e => {
    const r = e.currentTarget.getBoundingClientRect()
    const leaf = leafAt(layoutRef.current, e.clientX - r.left, e.clientY - r.top)
    if (!leaf) { if (hover) setHover(null); return }
    const box = drawn === 'files' ? leaf : leaf.T
    if (hover && hover.box === box && hover.leaf?.s === leaf.s) return
    setHover({ box, leaf, text: hoverText(leaf, drawn) })
  }
  const onClick = () => { if (hover?.text?.nav) onNavigate(hover.text.nav) }
  const showTitle = t => {
    const box = layoutRef.current?.titles.find(x => x.root === t.root && x.rel === t.rel && x.name === t.name)
    const leaf = { s: worstOf(t), T: t, bytes: t.v }
    setHover({ box: box || null, leaf, text: hoverText(leaf, 'titles') })
  }

  // ── Header numbers: the Where your disk goes card's, which the map replaced.
  const d = details || {}
  const shared = d.shared_size ?? d.hardlinked_media_size ?? 0
  const both = (d.total_media_size || 0) + (d.total_torrents_size || 0)
  const free = freeLine(d.disk_space)
  const tiny = layoutRef.current?.tiny || []

  return (
    <div style={{ ...CARD, padding: '18px 20px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 10 }}>
        <span style={SECTION_LABEL}>Library map</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {detail === 'files' && !filesReady && (
            <span style={{ ...META, display: 'inline-flex', alignItems: 'center', gap: 6 }}><Spinner size={10} /> Loading file lists…</span>
          )}
          <Segmented label="Map detail" value={detail} onChange={setDetail}
            options={[{ value: 'titles', label: 'Titles' }, { value: 'files', label: 'Files' }]} />
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: '16px 28px', flexWrap: 'wrap', marginBottom: 14 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
            <Hero text={formatBytes(shared)} />
            <span style={META}>saved by hardlinks</span>
          </div>
          <span style={META}>
            <span style={{ color: 'var(--text)' }}>{formatBytes(both - shared)}</span> on disk · {formatBytes(both)} if every link were a copy{free ? ` · ${free}` : ''}
          </span>
        </div>
        {totals && (
          <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap' }}>
            <Stat label="To act on" value={`${formatBytes(totals.problem)} · ${totals.problemTitles.toLocaleString()} titles`} />
            <Stat label="Fully linked" value={`${totals.full.toLocaleString()} titles`} />
            <Stat label="Partly linked" value={`${totals.partly.toLocaleString()} titles`} />
            <Stat label="Not linked" value={`${totals.none.toLocaleString()} titles`} />
            <Stat label="Files" value={totals.files.toLocaleString()} />
          </div>
        )}
      </div>

      {row === undefined && drawn === 'titles' ? (
        <div className="skeleton" style={{ height: 420, borderRadius: 4 }} />
      ) : !model || model.roots.length === 0 ? (
        <div style={{ ...META, height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center', textAlign: 'center' }}>
          {row === null ? 'The map appears after the next scan.' : 'Nothing to draw yet.'}
        </div>
      ) : (
        <div ref={wrapRef} style={{ position: 'relative' }} onMouseLeave={() => setHover(null)}>
          <canvas
            ref={canvasRef}
            role="img"
            aria-label={`Library map: ${formatBytes(totals?.by[LINKED] || 0)} linked, ${formatBytes(totals?.problem || 0)} for the workflows`}
            onMouseMove={onMove}
            onClick={onClick}
            style={{ display: 'block', borderRadius: 4, cursor: hover?.text?.nav ? 'pointer' : 'default' }}
          />
          {hover?.box && (
            <div style={{
              position: 'absolute', pointerEvents: 'none', left: hover.box.x, top: hover.box.y,
              width: hover.box.w, height: hover.box.h, border: '2px solid var(--text)', borderRadius: 2,
              boxShadow: '0 0 0 1px var(--surface)',
            }} />
          )}
        </div>
      )}

      <div style={{ ...META, height: 22, marginTop: 8, display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
        {hover?.text ? (
          <>
            <Swatch s={hover.text.s} />
            <span style={{ color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>{hover.text.name}</span>
            <span style={{ whiteSpace: 'nowrap' }}>{hover.text.facts}</span>
            {hover.text.nav && <span style={{ color: 'var(--accent)', whiteSpace: 'nowrap' }}>{STATES[hover.leaf.s].workflow} →</span>}
          </>
        ) : model ? (
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
            {totals.titles.toLocaleString()} titles · hover a tile, click one to open its workflow
            {!model.roots.some(r => r.name === 'torrents') && ' · torrent folder: all linked, drawn once in the library'}
            {tiny.length > 0 && ` · too small to draw: ${tiny.map(t => t.name || '(top level)').join(', ')}`}
          </span>
        ) : null}
      </div>

      {totals && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px', marginTop: 4 }}>
          {STATES.slice(0, 5).map((st, s) => (
            <span key={st.name} style={{ ...META, display: 'inline-flex', alignItems: 'center', gap: 6, opacity: totals.by[s] ? 1 : 0.55 }}>
              <Swatch s={s} />
              {st.name}{st.workflow && <span>· {st.workflow}</span>}
              <span style={{ color: 'var(--text)', fontWeight: 500 }}>{formatBytes(totals.by[s])}</span>
            </span>
          ))}
        </div>
      )}

      {gaps.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '2px 4px', marginTop: 10 }}>
          <span style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginRight: 6 }}>Biggest gaps</span>
          {gaps.map(g => {
            const nav = navFor(g.state, g.t)
            return (
              <span key={`${g.t.root}/${g.t.rel}`} onMouseEnter={() => showTitle(g.t)} onMouseLeave={() => setHover(null)}>
                <Button size="sm" variant="ghost" disabled={!nav} onClick={() => nav && onNavigate(nav)}
                  style={{ '--btn-border': 'transparent', gap: 6, fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)' }}>
                  <Swatch s={g.state} />
                  <span style={{ color: 'var(--text)' }}>{g.t.name}</span>
                  <span>{formatBytes(g.bytes)}</span>
                </Button>
              </span>
            )
          })}
        </div>
      )}
    </div>
  )
}

function worstOf(t) {
  let best = LINKED, most = 0
  for (const s of PROBLEM) if (t.by[s] > most) { most = t.by[s]; best = s }
  return best
}
