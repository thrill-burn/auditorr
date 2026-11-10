import React, { useState, useEffect, useLayoutEffect, useMemo, useRef, useCallback } from 'react'
import { api } from '../api'
import { formatBytes } from '../utils'
import { Button, Segmented, Spinner, useAuditComplete, ITEM_TITLE, MONO_TITLE } from './workflows/shared'
import { SECTION_LABEL, CARD, CAPTION, READOUT, Hero, TrendPill } from './dashKit'
import {
  STATES, PROBLEM, LINKED, ROOT_HEAD, HEAD,
  modelFromFiles, modelTotals, biggestGaps, navFor, layoutMap, leafAt,
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
//
// The Dashboard fetches the row (useLibraryMapRow) and hands it to both this
// card and Biggest gaps, which has been a card of its own since 2026-11-10, so
// one request feeds both. Hovering a gap outlines its tile here (`focus`).

// The stored row: undefined while loading, null before the first scan that
// writes one. Reloaded when a scan completes.
export function useLibraryMapRow() {
  const [row, setRow] = useState(undefined)
  const load = useCallback(() => {
    api.libraryMap().then(r => setRow(r && r.version ? r : null)).catch(() => setRow(null))
  }, [])
  useEffect(() => { load() }, [load])
  useAuditComplete(load)
  return row
}

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
    // A torrent block is named for its state and the workflow that acts on it,
    // which are words (Geist); a library folder's name is off the disk (mono).
    // What follows either is figures, in mono.
    ctx.font = f.state != null ? sansSm : monoSm
    const name = fit(ctx, f.state != null
      ? `${STATES[f.state].name}${STATES[f.state].workflow ? ' · ' + STATES[f.state].workflow : ''}`
      : (f.name || '(top level)'), f.w - 12)
    const nameW = ctx.measureText(name).width
    const room = f.w - 12 - nameW
    ctx.fillStyle = P.text; ctx.fillText(name, f.x + 6, f.y + HEAD / 2 + 0.5)
    ctx.font = monoSm
    const n = f.titles.length
    const rest = f.state != null
      ? longest(ctx, [`  ${formatBytes(f.v)} · ${n.toLocaleString()} release${n !== 1 ? 's' : ''}`, `  ${formatBytes(f.v)}`], room)
      : (() => { const b = breakdown(f.by); return longest(ctx, [
          `  ${[...b, `${n.toLocaleString()} titles`].join(' · ')}`, `  ${b.join(' · ')}`,
          `  ${b.slice(0, 2).join(' · ')}`, `  ${b[0] || ''}`], room) })()
    ctx.fillStyle = P.dim; ctx.fillText(rest, f.x + 6 + nameW, f.y + HEAD / 2 + 0.5)
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
    // A library title is one auditorr knows (Geist); a torrent's is a release name (mono).
    ctx.font = t.root === 'torrents' ? monoSm : sansSm
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

// The map's height: its width's share by default, or what this browser was
// dragged to (2026-11-10, the user: "it's pretty information dense… people have
// some pretty big libraries"). The default rose from 420–640 to 480–820, a third
// taller at both of the user's screens. The dragged height is per browser, like
// the page width, because it belongs to the screen, and it's written only when
// someone drags, never on load, so a stored value means someone picked it.
const mapHeight = W => Math.round(Math.min(820, Math.max(480, W * 0.55)))
const HEIGHT_KEY = 'auditorr_map_height'
const MAP_MIN = 320, MAP_MAX = 2000
const clampHeight = h => Math.round(Math.min(MAP_MAX, Math.max(MAP_MIN, h)))
function storedHeight() {
  try { const v = Number(localStorage.getItem(HEIGHT_KEY)); return v > 0 ? clampHeight(v) : null } catch { return null }
}
function storeHeight(h) {
  try { if (h == null) localStorage.removeItem(HEIGHT_KEY); else localStorage.setItem(HEIGHT_KEY, String(h)) } catch { /* private window */ }
}
// A drag redraws live while a draw is this quick (Titles always is, the stored
// row being at most 2,000 titles); a slower one (Files on a big library) is
// stretched while dragging and drawn once on release.
const LIVE_DRAW_MS = 30

// ── The card ─────────────────────────────────────────────────────────────────
// Its text is dashKit's two kinds: words in Geist (CAPTION), figures and names
// off the disk in Geist Mono. A stat's figure is mono at --font-base, MONO_TITLE's
// step: at --font-md, Geist Mono read larger and heavier than the card's title.
const FIGURE = { fontFamily: 'var(--mono)', color: 'var(--text)', fontWeight: 500 }

function Stat({ label, value }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span style={CAPTION}>{label}</span>
      <span style={{ ...FIGURE, fontSize: 'var(--font-base)', whiteSpace: 'nowrap' }}>{value}</span>
    </div>
  )
}

// The drive's numbers, as stats beside the title counts (2026-11-10). What the
// header's large "saved by hardlinks" figure said is the legend's Linked figure
// and the Hardlinked Media card's, so it went; what's on disk and what's free
// are said nowhere else. One free figure when both roots are one filesystem,
// which hardlinks need; two when they aren't. A scan before disk_space has none.
function diskStats(d) {
  const shared = d.shared_size ?? d.hardlinked_media_size ?? 0
  const both = (d.total_media_size || 0) + (d.total_torrents_size || 0)
  const out = [['On disk', formatBytes(both - shared)]]
  const ds = d.disk_space
  const free = x => (x.total ? `${formatBytes(x.free)} of ${formatBytes(x.total)}` : formatBytes(x.free))
  if (ds && (ds.same || !ds.media || !ds.torrents)) {
    const one = ds.torrents || ds.media
    if (one) out.push(['Free', free(one)])
  } else if (ds) {
    out.push(['Free for torrents', free(ds.torrents)], ['Free for the library', free(ds.media)])
  }
  return out
}

// The hover line's parts. `name` is the line's title, set by kind like a row
// title on the workflow pages: a library title is one auditorr knows (Geist,
// ITEM_TITLE), a release or file name is off the disk (mono, MONO_TITLE), a
// folded tile's "N smaller titles" is words. `prefix` is the folder it sits in.
function hoverText(leaf, detail) {
  const t = leaf.T, nav = navFor(leaf.s, t)
  if (detail === 'files' && leaf.file) {
    const st = STATES[leaf.s]
    return { s: leaf.s, prefix: t.root === 'library' && t.folder ? `${t.folder} / ${t.name}` : t.name,
             name: leaf.file, kind: 'mono',
             facts: `${formatBytes(leaf.bytes)} · ${st.name.toLowerCase()}${st.workflow ? ` (${st.workflow})` : ''}`, nav }
  }
  const facts = t.root === 'library'
    ? [formatBytes(t.v), ...(t.by[LINKED] ? [`${pctLinked(t.by)}% linked`] : []),
       ...PROBLEM.filter(s => t.by[s]).map(s => `${formatBytes(t.by[s])} ${LABEL[s]} (${STATES[s].workflow})`)].join(' · ')
    : `${formatBytes(t.v)} ${STATES[leaf.s].name.toLowerCase()}`
  const files = t.folded ? ` (${t.files.toLocaleString()} files)` : ''
  if (t.folded) return { s: leaf.s, prefix: '', name: `${t.name}${files}`, kind: 'words', facts, nav }
  if (t.root === 'library') return { s: leaf.s, prefix: t.folder || '', name: t.name, kind: 'title', facts, nav }
  // A torrent title's name is its root-relative folder; its category is the prefix.
  const cut = t.folder && t.name.startsWith(t.folder + '/') ? t.folder.length + 1 : 0
  return { s: leaf.s, prefix: cut ? t.folder : '', name: t.name.slice(cut), kind: 'mono', facts, nav }
}

const HOVER_NAME = { title: ITEM_TITLE, words: ITEM_TITLE, mono: MONO_TITLE }

export default function LibraryMap({ details, onNavigate, row, rowModel, focus, mediaFiles, torrentFiles, ensureFilesLoaded }) {
  const [detail, setDetail] = useState('titles')     // Titles by default, every visit (the user's call)
  const [width, setWidth] = useState(0)
  const [themeTick, setThemeTick] = useState(0)
  const [hover, setHover] = useState(null)           // { box, text }
  const [userH, setUserH] = useState(storedHeight)   // null = the width's default
  const [dragging, setDragging] = useState(false)
  const wrapRef = useRef(null)
  const canvasRef = useRef(null)
  const layoutRef = useRef(null)
  const drawMs = useRef(0)
  const dragRef = useRef(null)

  const filesReady = mediaFiles != null && torrentFiles != null
  useEffect(() => {
    if (detail !== 'files') return
    if (mediaFiles == null) ensureFilesLoaded?.('media')
    if (torrentFiles == null) ensureFilesLoaded?.('torrents')
  }, [detail, mediaFiles, torrentFiles, ensureFilesLoaded])

  const drawn = detail === 'files' && filesReady ? 'files' : 'titles'
  const model = useMemo(() => {
    if (drawn === 'files') return modelFromFiles(mediaFiles, torrentFiles)
    return rowModel || null
  }, [drawn, mediaFiles, torrentFiles, rowModel])
  const totals = useMemo(() => (model ? modelTotals(model) : null), [model])

  // Redraw on resize, on a theme change (an attribute on <html>), and when a
  // web font finishes loading: a canvas draws in whatever face is there and
  // never redraws by itself, so a map drawn before Geist arrived would keep
  // the fallback face for good.
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return undefined
    const ro = new ResizeObserver(([e]) => setWidth(Math.floor(e.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [model != null])
  useEffect(() => {
    const redraw = () => setThemeTick(t => t + 1)
    const mo = new MutationObserver(redraw)
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    const fonts = document.fonts
    fonts?.addEventListener?.('loadingdone', redraw)
    return () => { mo.disconnect(); fonts?.removeEventListener?.('loadingdone', redraw) }
  }, [])
  const height = userH ?? mapHeight(width)
  useLayoutEffect(() => {
    if (!model || !width || !canvasRef.current) return
    const t0 = performance.now()
    layoutRef.current = draw(canvasRef.current, model, width, height, drawn)
    drawMs.current = performance.now() - t0
    setHover(null)
  }, [model, width, height, drawn, themeTick])

  // ── The height grip ──
  const commitHeight = h => { const c = clampHeight(h); setUserH(c); storeHeight(c) }
  const onGripDown = e => {
    if (e.button !== 0) return
    e.preventDefault()
    e.currentTarget.setPointerCapture(e.pointerId)
    dragRef.current = { y: e.clientY, h: height, last: height }
    setDragging(true)
    setHover(null)
  }
  const onGripMove = e => {
    const d = dragRef.current
    if (!d) return
    const h = clampHeight(d.h + e.clientY - d.y)
    if (h === d.last) return
    d.last = h
    if (drawMs.current <= LIVE_DRAW_MS) setUserH(h)
    else if (canvasRef.current) canvasRef.current.style.height = h + 'px'
  }
  const onGripUp = () => {
    const d = dragRef.current
    if (!d) return
    dragRef.current = null
    setDragging(false)
    if (d.last !== d.h) commitHeight(d.last)    // a click without a drag stores nothing
  }
  const resetHeight = () => { setUserH(null); storeHeight(null) }
  const onGripKey = e => {
    const step = e.shiftKey ? 160 : 40
    if (e.key === 'ArrowUp') { e.preventDefault(); commitHeight(height - step) }
    else if (e.key === 'ArrowDown') { e.preventDefault(); commitHeight(height + step) }
  }

  const onMove = e => {
    const r = e.currentTarget.getBoundingClientRect()
    const leaf = leafAt(layoutRef.current, e.clientX - r.left, e.clientY - r.top)
    if (!leaf) { if (hover) setHover(null); return }
    const box = drawn === 'files' ? leaf : leaf.T
    if (hover && hover.box === box && hover.leaf?.s === leaf.s) return
    setHover({ box, leaf, text: hoverText(leaf, drawn) })
  }
  const onClick = () => { if (hover?.text?.nav) onNavigate(hover.text.nav) }

  // A gap hovered on the Biggest gaps card: outline its tile and name it here.
  // A torrent release has a tile in each state's block, so it's the gap's state's.
  useEffect(() => {
    if (!focus) { setHover(null); return }
    const t = focus.t, s = focus.state
    const box = layoutRef.current?.titles.find(x => x.root === t.root && x.rel === t.rel && x.name === t.name
      && (t.root !== 'torrents' || x.by[s] > 0))
    const leaf = { s, T: t, bytes: t.v }
    setHover({ box: box || null, leaf, text: hoverText(leaf, 'titles') })
  }, [focus])

  const stats = totals ? [
    ...diskStats(details || {}),
    ['Fully linked', `${totals.full.toLocaleString()} titles`],
    ['Partly linked', `${totals.partly.toLocaleString()} titles`],
    ['Not linked', `${totals.none.toLocaleString()} titles`],
    ['Files', totals.files.toLocaleString()],
  ] : []
  const tiny = layoutRef.current?.tiny || []

  return (
    <div style={{ ...CARD, padding: '18px 20px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 10 }}>
        <span style={SECTION_LABEL}>Library map</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {detail === 'files' && !filesReady && (
            <span style={{ ...CAPTION, display: 'inline-flex', alignItems: 'center', gap: 6 }}><Spinner size={10} /> Loading file lists…</span>
          )}
          <Segmented label="Map detail" value={detail} onChange={setDetail}
            options={[{ value: 'titles', label: 'Titles' }, { value: 'files', label: 'Files' }]} />
        </div>
      </div>

      {stats.length > 0 && (
        <div style={{ display: 'flex', gap: '12px 28px', flexWrap: 'wrap', marginBottom: 14 }}>
          {stats.map(([label, value]) => <Stat key={label} label={label} value={value} />)}
        </div>
      )}

      {row === undefined && drawn === 'titles' ? (
        <div className="skeleton" style={{ height, borderRadius: 4 }} />
      ) : !model || model.roots.length === 0 ? (
        <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center', textAlign: 'center' }}>
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
      {model && model.roots.length > 0 && (
        // Drag for a taller or shorter map; double-click for the default.
        <div className="map-grip" role="separator" aria-orientation="horizontal" aria-label="Map height"
          aria-valuenow={height} aria-valuemin={MAP_MIN} aria-valuemax={MAP_MAX} tabIndex={0}
          data-dragging={dragging} title="Drag to change the map's height · double-click for the default"
          onPointerDown={onGripDown} onPointerMove={onGripMove} onPointerUp={onGripUp} onPointerCancel={onGripUp}
          onDoubleClick={resetHeight} onKeyDown={onGripKey}>
          <svg width="24" height="6" viewBox="0 0 24 6" aria-hidden="true">
            <path d="M2 1.5h20M2 4.5h20" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </div>
      )}

      {/* The hovered tile: its name is the line's title, a row title's size
          and face (2026-11-10, it was the legend's 11px); the folder before it
          and the figures after it stay small. The hint is words. */}
      <div style={{ ...CAPTION, height: 30, marginTop: 2, display: 'flex', alignItems: 'center', minWidth: 0 }}>
        {hover?.text ? (
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, minWidth: 0 }}>
            <span style={{ alignSelf: 'center', display: 'inline-flex' }}><Swatch s={hover.text.s} /></span>
            <span style={{ display: 'flex', alignItems: 'baseline', gap: 6, minWidth: 0 }}>
              {hover.text.prefix && (
                <span style={{ ...READOUT, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 220, flexShrink: 0 }}>{hover.text.prefix} /</span>
              )}
              <span style={{ ...HOVER_NAME[hover.text.kind], overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>{hover.text.name}</span>
            </span>
            <span style={{ ...READOUT, whiteSpace: 'nowrap' }}>{hover.text.facts}</span>
            {hover.text.nav && <span style={{ color: 'var(--accent)', whiteSpace: 'nowrap' }}>{STATES[hover.leaf.s].workflow} →</span>}
          </div>
        ) : model ? (
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
            {totals.titles.toLocaleString()} titles · hover a tile, click one to open its workflow
            {!model.roots.some(r => r.name === 'torrents') && ' · torrent folder: all linked, drawn once in the library'}
            {tiny.length > 0 && ` · too small to draw: ${tiny.map(t => t.name || '(top level)').join(', ')}`}
          </span>
        ) : null}
      </div>

      {totals && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px', marginTop: 6 }}>
          {STATES.slice(0, 5).map((st, s) => (
            <span key={st.name} style={{ ...CAPTION, display: 'inline-flex', alignItems: 'center', gap: 6, opacity: totals.by[s] ? 1 : 0.55 }}>
              <Swatch s={s} />
              {st.name}{st.workflow && <span>· {st.workflow}</span>}
              <span style={FIGURE}>{formatBytes(totals.by[s])}</span>
            </span>
          ))}
        </div>
      )}

    </div>
  )
}

// ── Biggest gaps ─────────────────────────────────────────────────────────────
// The titles with the most bytes for a workflow, from the stored scan whatever
// the map's Titles/Files switch says. A card of its own since 2026-11-10, left
// of Last scan; it was a wrapped row of buttons under the map. Its headline is
// what the map's "To act on" stat said, since the list is the top of that total.
//
// Each row is the kit's quiet button, full width: the swatch, the name (a
// library title in Geist, a release name in mono, as ITEM_TITLE and MONO_TITLE
// set a row title), the bytes, and the workflow a click opens, in words,
// because at 9px a swatch's pattern is too small to tell states apart by.
const GAP_ROWS = 6

export function BiggestGapsCard({ row, model, onNavigate, onHighlight }) {
  const totals = useMemo(() => (model ? modelTotals(model) : null), [model])
  const gaps = useMemo(() => (model ? biggestGaps(model, GAP_ROWS) : []), [model])
  useEffect(() => () => onHighlight?.(null), [])     // never leave a tile outlined behind
  const clear = () => onHighlight?.(null)

  return (
    <div style={{ ...CARD, padding: '18px 20px 16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, marginBottom: 8 }}>
        <span style={SECTION_LABEL}>Biggest gaps</span>
      </div>
      {row === undefined ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="skeleton" style={{ width: 140, height: 30, borderRadius: 5 }} />
          {[0, 1, 2, 3].map(i => <div key={i} className="skeleton" style={{ height: 14, borderRadius: 4 }} />)}
        </div>
      ) : !totals ? (
        <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: 0, lineHeight: 1.6 }}>
          The list appears after the next scan.
        </p>
      ) : (
        <>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
            <Hero text={formatBytes(totals.problem)} />
            <span style={CAPTION}>
              to act on{totals.problemTitles ? ` in ${totals.problemTitles.toLocaleString()} title${totals.problemTitles !== 1 ? 's' : ''}` : ''}
            </span>
          </div>
          {totals.problem === 0 ? (
            <div style={{ minHeight: 21, marginTop: 10, display: 'flex' }}>
              <TrendPill trend={{ sentiment: 'good' }} icon="✓" label="Nothing to act on: every title is linked" />
            </div>
          ) : gaps.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 12, marginLeft: -8, marginRight: -8 }}
              onMouseLeave={clear} onBlur={clear}>
              {gaps.map(g => {
                const nav = navFor(g.state, g.t)
                const show = () => onHighlight?.({ t: g.t, state: g.state })
                return (
                  <span key={`${g.t.root}/${g.t.rel}`} style={{ display: 'flex' }} onMouseEnter={show} onFocus={show}>
                    <Button size="sm" variant="ghost" disabled={!nav} onClick={() => nav && onNavigate(nav)}
                      title={nav ? `Open ${STATES[g.state].workflow} on this title` : undefined}
                      style={{ '--btn-border': 'transparent', width: '100%', justifyContent: 'flex-start', gap: 10, padding: '4px 8px', fontWeight: 400 }}>
                      <Swatch s={g.state} />
                      <span style={{ ...(g.t.root === 'torrents' ? MONO_TITLE : ITEM_TITLE), flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', textAlign: 'left' }}>{g.t.name}</span>
                      <span style={{ ...READOUT, color: 'var(--text)', fontVariantNumeric: 'tabular-nums' }}>{formatBytes(g.bytes)}</span>
                      <span style={{ ...CAPTION, width: 52, textAlign: 'left' }}>{STATES[g.state].workflow}</span>
                    </Button>
                  </span>
                )
              })}
            </div>
          )}
        </>
      )}
    </div>
  )
}
