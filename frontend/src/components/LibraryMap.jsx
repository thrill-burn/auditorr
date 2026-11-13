import React, { useState, useEffect, useLayoutEffect, useMemo, useRef, useCallback } from 'react'
import { api } from '../api'
import { formatBytes } from '../utils'
import { Button, Segmented, Spinner, useAuditComplete, ITEM_TITLE, MONO_TITLE } from './workflows/shared'
import { SECTION_LABEL, CARD, CAPTION, READOUT, Hero, TrendPill } from './dashKit'
import {
  STATES, PROBLEM, LEGEND, LINKED, ORPHANED, NOT_IMPORTED, COPY, OTHER, DEAD_SEED, ROOT_HEAD, HEAD, worstState,
  modelFromFiles, modelTotals, biggestGaps, navFor, layoutMap, leafAt,
  zoomModel, zoomOut, headingTarget, headingAt, freeAt, freeRoots, shortName,
} from './libraryMapRules'

// The library map (2026-11-09): WinDirStat's idea applied to hardlinks. Every
// rectangle's area is its size on disk, grouped by top-level folder, and its
// colour is the workflow that acts on it — the four cards' colours above it.
// The one exception: a gap a workflow acts on that would be too small to see
// is drawn at the smallest size that shows (libraryMapRules.js, "Sizing"); the
// figures stay bytes.
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
  [LINKED]: '', 1: 'not seeded', 2: 'orphaned', 3: 'not imported', 4: 'copies', 5: 'other', [DEAD_SEED]: 'dead seed',
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

// How each theme shades its tiles (2026-11-10): `lift` and `shade` are the
// white and black mixed into the cushion's lit and shaded corners, `ink` the
// pattern's white. Dark shades toward its black card, which reads as gloss.
// Light did the same and pushed every grey tile's far corner to L* 50, as dark
// as the blue (53) it's meant to set off, so light lifts instead. Its stripes
// were 55% white, twice dark's contrast on the deeper light colours; 30%
// matches it. The user's pick from .internal/preview/maptheme.mjs's shots.
const LOOK = {
  dark:  { lift: 0.2, shade: 0.35, ink: 0.34 },
  light: { lift: 0.4, shade: 0.1, ink: 0.3 },
}

function palette() {
  const dim = toRgb(cssVar('--text-dim'))
  const light = document.documentElement.getAttribute('data-theme') === 'light'
  const k = LOOK[light ? 'light' : 'dark']
  return {
    surface: cssVar('--surface'), band: cssVar('--surface2'), text: cssVar('--text'), dim: cssVar('--text-dim'),
    // Linked is the neutral bulk; Other quieter still (index.css).
    c: [toRgb(cssVar('--map-linked')), toRgb(cssVar('--blue')), toRgb(cssVar('--yellow')), toRgb(cssVar('--red')),
        toRgb(cssVar('--purple')), toRgb(cssVar('--map-other')), toRgb(cssVar('--red'))],
    lift: k.lift, shade: k.shade,
    ink: `rgba(255,255,255,${k.ink})`,
    // Free space's sparse hatch: the dim text colour, faint.
    hatch: `rgba(${dim.map(Math.round).join(',')},${light ? 0.28 : 0.22})`,
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
    case 'rows':  return { background: `repeating-linear-gradient(0deg, ${c} 0 2px, ${w} 2px 3.5px), ${c}` }
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
// A title's name is drawn whole, or cut where at least this many characters
// fit (2026-11-10; it was any tile 64px wide). Fewer reads as noise. A cut
// drops an unclosed bracket and what follows it, so "Halloween (19…" reads
// "Halloween…": half a year says nothing, and the tile is one of several.
const MIN_NAME_CHARS = 6
function nameFit(ctx, s, max) {
  const f = fit(ctx, s, max)
  if (f === s) return f
  if (f.length - 1 < MIN_NAME_CHARS) return ''
  let cut = f.slice(0, -1)
  const open = Math.max(cut.lastIndexOf('('), cut.lastIndexOf('['))
  if (open > 0 && !/[)\]]/.test(cut.slice(open))) cut = cut.slice(0, open)
  cut = cut.replace(/[\s\-–—:,.]+$/, '')
  return cut.length >= 3 ? cut + '…' : f
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
  // A dead seed's rows: level, so no diagonal reads as one.
  if (kind === 'rows') for (let y = l.y + 3; y < l.y + l.h; y += 5) { ctx.moveTo(l.x, y); ctx.lineTo(l.x + l.w, y) }
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
    g.addColorStop(0, rgb(mix([255, 255, 255], base, P.lift)))
    g.addColorStop(0.5, rgb(base))
    g.addColorStop(1, rgb(mix([0, 0, 0], base, P.shade)))
    ctx.fillStyle = g
  } else ctx.fillStyle = rgb(base)
  ctx.fillRect(l.x, l.y, l.w, l.h)
  const kind = STATES[l.s].pattern
  if (kind && l.w >= 4 && l.h >= 4) pattern(ctx, l, kind, P.ink)
}

function pctLinked(by) {
  const v = by.reduce((a, b) => a + b, 0)
  // A dead seed is linked; only its torrent is gone.
  const p = v ? (by[LINKED] + (by[DEAD_SEED] || 0)) / v * 100 : 0
  return p >= 99.95 ? '100' : p.toFixed(p < 10 ? 1 : 0)
}
// What a library folder or title holds, worst first, in a few words.
function breakdown(by) {
  const parts = []
  if (by[LINKED] || by[DEAD_SEED]) parts.push(`${pctLinked(by)}% linked`)
  for (const s of PROBLEM) if (by[s]) parts.push(`${formatBytes(by[s])} ${LABEL[s]}`)
  return parts
}

const ROOT_LABEL = { library: 'Library folder', torrents: "Torrent folder · what isn't linked" }
const ROOT_SHORT = { library: 'Library', torrents: 'Torrents' }
const NESTED_HEAD = { library: ['library folder', ''], torrents: ['torrent folder', 'unlinked'] }

// Free space (the switch in the header): the card's band colour with a sparse
// hatch, unlike any state's fill or pattern, and named like a root.
function freeTile(ctx, r, P) {
  ctx.save()
  ctx.fillStyle = P.band; ctx.fillRect(r.x, r.y, r.w, r.h)
  ctx.beginPath(); ctx.rect(r.x, r.y, r.w, r.h); ctx.clip()
  ctx.strokeStyle = P.hatch; ctx.lineWidth = 1
  ctx.beginPath()
  for (let i = -r.h; i < r.w; i += 11) { ctx.moveTo(r.x + i, r.y + r.h); ctx.lineTo(r.x + i + r.h, r.y) }
  ctx.stroke()
  if (r.w >= 56 && r.h >= ROOT_HEAD) {
    ctx.textBaseline = 'middle'
    ctx.font = `600 ${P.md} ${P.sans}`; ctx.fillStyle = P.text
    const label = longest(ctx, [r.label, 'Free'], r.w - 12)
    const lw = ctx.measureText(label).width
    ctx.fillText(label, r.x + 6, r.y + ROOT_HEAD / 2)
    ctx.font = `500 ${P.sm} ${P.mono}`; ctx.fillStyle = P.dim
    const size = longest(ctx, [`  ${formatBytes(r.v)} of ${formatBytes(r.total)}`, `  ${formatBytes(r.v)}`], r.w - 12 - lw)
    ctx.fillText(size, r.x + 6 + lw, r.y + ROOT_HEAD / 2)
  }
  ctx.restore()
}

// A highlighted state (a legend entry clicked) fades every other leaf to the
// card, as WinDirStat lights up one extension's files. It doesn't depend on
// telling two colours apart, which the user can't always.
const FADE = 0.78

function draw(canvas, model, W, H, detail, highlight = null) {
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
    // A root drawn as a box among the other root's folders (Sizing's rule 2)
    // is headed like them (the user, 2026-11-11): its name lower case in the
    // folders' mono, then one word, dimmed, as their figures are.
    if (r.nested) {
      const [nm, word] = NESTED_HEAD[r.name]
      ctx.font = monoSm
      const name = fit(ctx, nm, r.w - 9)
      const nameW = ctx.measureText(name).width
      ctx.fillStyle = P.text; ctx.fillText(name, r.x + 4.5, r.y + ROOT_HEAD / 2)
      ctx.fillStyle = P.dim; ctx.fillText(longest(ctx, [`  ${word}`], r.w - 9 - nameW), r.x + 4.5 + nameW, r.y + ROOT_HEAD / 2)
      continue
    }
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
  for (const r of L.roots) if (r.free) freeTile(ctx, r, P)
  if (highlight != null) {
    ctx.save(); ctx.globalAlpha = FADE; ctx.fillStyle = P.surface
    for (const l of L.leaves) if (l.s !== highlight) ctx.fillRect(l.x, l.y, l.w, l.h)
    ctx.restore()
  }
  // Titles part with a 1px line of the card, so a title reads as one block.
  ctx.strokeStyle = P.surface; ctx.lineWidth = 1
  for (const t of L.titles) if (t.w > 3 && t.h > 3) ctx.strokeRect(t.x + 0.5, t.y + 0.5, t.w - 1, t.h - 1)
  ctx.textBaseline = 'top'
  const faded = labelOn(mix(toRgb(P.surface), [128, 128, 128], 0.85))
  // Under a highlight, a label over a faded leaf is set against the card, not
  // the leaf's colour.
  const inkOf = s => (highlight != null && s !== highlight ? faded : labelOn(P.c[s]))
  const leavesOf = new Map()
  for (const l of L.leaves) { const a = leavesOf.get(l.T); if (a) a.push(l); else leavesOf.set(l.T, [l]) }
  for (const t of L.titles) {
    if (t.w < 24 || t.h < 18) continue
    // A library title is one auditorr knows (Geist); a torrent's is a release name (mono).
    const font = t.root === 'torrents' ? monoSm : sansSm
    ctx.font = font
    const name = nameFit(ctx, shortName(t), t.w - 10)
    if (!name) continue
    let width = ctx.measureText(name).width, size = null
    if (t.h >= 38 && t.w >= 60) {
      ctx.font = monoSm
      const b = t.root === 'library' ? breakdown(t.by).slice(0, 1) : []
      size = longest(ctx, [[formatBytes(t.v), ...b].join(' · '), formatBytes(t.v)], t.w - 10)
      width = Math.max(width, ctx.measureText(size).width)
    }
    // A title without the highlighted state fades with its tiles.
    const lit = highlight == null || t.by[highlight] > 0
    const text = ink => {
      ctx.fillStyle = ink
      ctx.globalAlpha = lit ? 1 : 0.4; ctx.font = font
      ctx.fillText(name, t.x + 5, t.y + 4)
      if (size) { ctx.globalAlpha = lit ? 0.78 : 0.3; ctx.font = monoSm; ctx.fillText(size, t.x + 5, t.y + 19) }
      ctx.globalAlpha = 1
    }
    // A label is inked for the leaf it's on. One that runs across two leaves
    // wanting different inks (black on the dark theme's blue, white on its
    // grey) takes each one's ink, clipped to it: inked for the first leaf
    // alone, "The Handmaid's Tale" ran black onto dark grey (2026-11-10).
    const x0 = t.x + 5, x1 = x0 + width, y0 = t.y + 4, y1 = t.y + (size ? 32 : 17)
    const byInk = new Map()
    for (const l of leavesOf.get(t) || []) {
      if (l.w <= 0 || l.h <= 0 || l.x >= x1 || l.x + l.w <= x0 || l.y >= y1 || l.y + l.h <= y0) continue
      const ink = inkOf(l.s), a = byInk.get(ink)
      if (a) a.push(l); else byInk.set(ink, [l])
    }
    if (byInk.size <= 1) { text(byInk.keys().next().value ?? inkOf(LINKED)); continue }
    for (const [ink, ls] of byInk) {
      ctx.save(); ctx.beginPath()
      for (const l of ls) ctx.rect(l.x, l.y, l.w, l.h)
      ctx.clip(); text(ink); ctx.restore()
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
// The free-space switch is per browser too, written only when flipped.
const FREE_KEY = 'auditorr_map_free'
function storedFree() { try { return localStorage.getItem(FREE_KEY) === '1' } catch { return false } }
function storeFree(on) { try { if (on) localStorage.setItem(FREE_KEY, '1'); else localStorage.removeItem(FREE_KEY) } catch { /* private window */ } }
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

// ── The detail strip (2026-11-10) ────────────────────────────────────────────
// Under the map, always the same height: a thumbnail, then up to five lines.
//   name     the title, set by kind like a row title on the workflow pages: a
//            library title is one auditorr knows (Geist, ITEM_TITLE), a release
//            or file name is off the disk (mono, MONO_TITLE); `prefix` is its folder
//   context  a library title's arr ("Sonarr · 2025 · Continuing…", fetched on a
//            pause), else what the state means, in words
//   facts    figures, mono
//   by       the bytes by state, drawn as a bar with the legend's patterns
//   action   what a click does (accent), or `note` when it does nothing
// It replaced a 30px hover line whose name was the legend's 11px.

// What a torrent tile's state means, said where the arr line would be.
const STATE_NOTE = {
  [ORPHANED]:     'In the torrent folder, but no torrent in your client claims it',
  [NOT_IMPORTED]: 'Seeding, but not in your library',
  [COPY]:         'A copy of a library file rather than a hardlink to it',
  [OTHER]:        'Nothing to do: a sidecar, a sample or an unfinished download',
}

// Initials for a title with no poster (yet), as other apps do: the first two
// words' letters, past a leading article and the small words after it.
const ARTICLES = new Set(['the', 'a', 'an'])
const MINOR = new Set(['of', 'and', 'the', 'a', 'an', 'in', 'on', 'to', 'for', '&'])
export function initials(name) {
  const words = String(name || '').replace(/\(\d{4}\)|\[[^\]]*\]/g, ' ')
    .split(/[\s._:–—-]+/).filter(w => /[\p{L}\p{N}]/u.test(w))
  if (words.length > 1 && ARTICLES.has(words[0].toLowerCase())) words.shift()
  const keep = words.filter((w, i) => i === 0 || !MINOR.has(w.toLowerCase()))
  return keep.slice(0, 2).map(w => (w.match(/[\p{L}\p{N}]/u) || [''])[0].toUpperCase()).join('') || '?'
}

// What a click on a tile opens, said as a sentence. Backfill's is a search,
// which starts by itself (BACKFILL B17), so the sentence says so.
function actionFor(nav, t) {
  if (!nav) return null
  const wf = STATES.find(x => x.tab === nav.tab)?.workflow || nav.tab
  if (nav.tab === 'dedupe') return `Click to open ${wf} →`
  if (nav.tab === 'backfill') return 'Click to search for this title in Backfill →'
  return `Click to open ${wf} on this ${t.root === 'library' ? 'title' : 'release'} →`
}

// How much larger than its bytes a gap is drawn ("Sizing" in the rules), said
// beside its figures, and nothing for a tile drawn to scale, which is every
// tile that needs nothing.
function drawnNote(leaf, part) {
  const b = leaf.boost
  if (!(b >= 1.5)) return null
  const n = b < 10 ? b.toFixed(1).replace(/\.0$/, '') : Math.round(b).toLocaleString()
  const state = STATES[leaf.s].name.toLowerCase()
  return `${part ? `Its ${state} part is drawn` : 'Drawn'} ${n}× its size so it shows`
}

function hoverText(leaf, mode) {
  const t = leaf.T
  const lib = t.root === 'library'
  // A library title's linked part opens the title's own problem, so a click
  // anywhere on a title that needs something goes to the workflow for it.
  const nav = navFor(leaf.s, t) || (lib && !t.folded ? navFor(worstState(t.by), t) : null)
  const base = { s: leaf.s, nav, action: actionFor(nav, t),
                 drawn: drawnNote(leaf, mode !== 'files' && lib && t.by.filter(Boolean).length > 1),
                 note: nav ? null : (lib ? 'Every file here is linked: nothing to do' : null) }
  // A torrent title's name is its root-relative folder; its category is the prefix.
  const cut = !lib && t.folder && t.name.startsWith(t.folder + '/') ? t.folder.length + 1 : 0
  const own = lib ? t.name : t.name.slice(cut)
  const rel = lib && !t.folded ? t.rel : null
  const thumb = t.folded ? { icon: 'folder' } : { initials: initials(own) }
  if (mode === 'files' && leaf.file) {
    const st = STATES[leaf.s]
    return { ...base, thumb, rel, prefix: lib && t.folder ? `${t.folder} / ${t.name}` : t.name,
             name: leaf.file, kind: 'mono', context: lib ? null : STATE_NOTE[leaf.s] || null,
             facts: `${formatBytes(leaf.bytes)} · ${st.name.toLowerCase()}${st.workflow ? ` (${st.workflow})` : ''}`,
             by: null }
  }
  const files = `${(t.files || 0).toLocaleString()} file${t.files !== 1 ? 's' : ''}`
  if (lib) {
    const facts = [formatBytes(t.v), files, ...(t.by[LINKED] || t.by[DEAD_SEED] ? [`${pctLinked(t.by)}% linked`] : []),
      ...PROBLEM.filter(s => t.by[s]).map(s => `${formatBytes(t.by[s])} ${LABEL[s]} (${STATES[s].workflow})`)].join(' · ')
    return { ...base, thumb, rel, prefix: t.folded ? '' : t.folder || '', name: t.name,
             kind: t.folded ? 'words' : 'title', context: t.folded ? 'Titles too small to draw, as one tile' : null,
             facts, by: t.by }
  }
  return { ...base, thumb, rel: null, prefix: t.folded ? '' : cut ? t.folder : '', name: t.folded ? t.name : own,
           kind: t.folded ? 'words' : 'mono', context: STATE_NOTE[leaf.s] || null,
           facts: `${formatBytes(t.v)} · ${files}`, by: null }
}

// A heading under the pointer: what it is, its figures, and where a click
// takes the map. `target` is the zoom it sets (null is the whole map).
function headingText(h, target, zoomingOut) {
  const common = { thumb: { icon: 'folder' }, target, zoom: zoomingOut ? 'out' : 'in',
                   action: zoomingOut ? 'Click to zoom out' : 'Click to zoom in' }
  if (h.kind === 'root') {
    return { ...common, s: null, prefix: '', name: ROOT_LABEL[h.r.name], kind: 'words',
             context: h.r.name === 'library' ? 'Everything in your library folder' : "What's in your torrent folder and not linked into the library",
             facts: [formatBytes(h.r.v), ...breakdown(h.r.by)].join(' · '), by: h.r.by }
  }
  const f = h.f, n = f.titles.length
  if (f.state != null) {
    const st = STATES[f.state]
    return { ...common, s: f.state, prefix: '', name: `${st.name}${st.workflow ? ' · ' + st.workflow : ''}`, kind: 'words',
             context: STATE_NOTE[f.state] || null,
             facts: `${formatBytes(f.v)} · ${n.toLocaleString()} release${n !== 1 ? 's' : ''}`, by: null }
  }
  return { ...common, s: null, prefix: '', name: f.name || '(top level)', kind: 'mono',
           context: 'A folder in your library',
           facts: [formatBytes(f.v), `${n.toLocaleString()} titles`, ...breakdown(f.by)].join(' · '), by: f.by }
}

// The arr line for a library title: which series or movie it is, from the
// title's folder (`/api/library_map/title`). Blank until it answers; a
// "couldn't ask" says so and is never "not there" (R1).
const STATUS_WORD = { continuing: 'Continuing', ended: 'Ended', upcoming: 'Upcoming', deleted: 'Deleted',
                      released: 'Released', announced: 'Announced', inCinemas: 'In cinemas', tba: 'TBA' }
const squash = s => String(s || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '')
// 121 → "2h 1m", 120 → "2h" (it read "2h 0m"), 45 → "45m".
export const runtimeText = m => [Math.floor(m / 60) && `${Math.floor(m / 60)}h`, m % 60 && `${m % 60}m`].filter(Boolean).join(' ')
function arrLine(info, name) {
  if (!info) return ''
  if (info.found === null) return `Couldn't ask ${(info.unreachable || []).join(' or ') || 'Sonarr or Radarr'}`
  if (!info.found) return info.configured === false ? '' : 'Not in Sonarr or Radarr'
  const parts = [info.service === 'radarr' ? 'Radarr' : 'Sonarr']
  if (info.title && squash(info.title) !== squash(name.replace(/\s*\(\d{4}\)\s*$/, ''))) parts.push(`as ${info.title}`)
  if (info.year) parts.push(String(info.year))
  if (info.status) parts.push(STATUS_WORD[info.status] || info.status)
  if (info.service === 'sonarr' && info.seasons != null) parts.push(`${info.seasons} season${info.seasons !== 1 ? 's' : ''}`)
  if (info.service === 'radarr' && info.runtime > 0) parts.push(runtimeText(info.runtime))
  if (info.network) parts.push(info.network)
  return parts.join(' · ')
}

// A title's arr answer and poster, looked up once a pointer rests on it: a
// sweep across the map asks for nothing. Both are kept for the page's life
// (posters as object URLs, the oldest let go past POSTER_KEEP), so a second
// look is instant; an answer that couldn't ask isn't kept, so a later look
// asks again.
const ARR_DELAY = 300, POSTER_KEEP = 150
const arrInfo = new Map(), posters = new Map()
function infoFor(rel) {
  if (!arrInfo.has(rel)) {
    arrInfo.set(rel, api.libraryMapTitle(rel).then(r => {
      if (!r || r.found === null) arrInfo.delete(rel)
      return r
    }))
  }
  return arrInfo.get(rel)
}
function posterFor(rel) {
  if (!posters.has(rel)) {
    if (posters.size >= POSTER_KEEP) {
      const [old, url] = posters.entries().next().value
      posters.delete(old)
      url.then(u => u && URL.revokeObjectURL(u))
    }
    posters.set(rel, api.libraryMapPoster(rel).then(b => (b ? URL.createObjectURL(b) : null)))
  }
  return posters.get(rel)
}
function useArrDetail(rel) {
  const [got, setGot] = useState({ rel: null, info: null, poster: null })
  useEffect(() => {
    if (!rel) return undefined
    let live = true
    const t = setTimeout(async () => {
      const info = await infoFor(rel)
      if (!live) return
      setGot({ rel, info, poster: null })
      if (info?.found && info.poster) {
        const poster = await posterFor(rel)
        if (live) setGot({ rel, info, poster })
      }
    }, arrInfo.has(rel) ? 0 : ARR_DELAY)
    return () => { live = false; clearTimeout(t) }
  }, [rel])
  return got.rel === rel ? got : { rel, info: null, poster: null }
}

// The thumbnail: the poster, else initials, else a line icon for what isn't a title.
const ICONS = {
  folder: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />,
  drive:  <><rect x="3" y="6" width="18" height="12" rx="2" /><path d="M7 14h.01M11 14h6" /></>,
  map:    <><rect x="3" y="4" width="18" height="16" rx="1.5" /><path d="M11 4v16M11 12h10M16 12v8" /></>,
}
function Thumb({ thumb, poster }) {
  return (
    <div style={{ width: 56, height: 84, flexShrink: 0, borderRadius: 4, overflow: 'hidden', background: 'var(--surface2)',
                  border: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-dim)' }}>
      {poster
        ? <img src={poster} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
        : thumb?.initials
          ? <span style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-lg)', fontWeight: 600, letterSpacing: '0.02em' }}>{thumb.initials}</span>
          : <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
              strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{ICONS[thumb?.icon || 'map']}</svg>}
    </div>
  )
}

// The bytes by state as one bar, in the legend's colours and patterns; only
// where there's more than one state to tell apart.
function StateBar({ by }) {
  const total = by ? by.reduce((a, b) => a + b, 0) : 0
  if (!total || by.filter(Boolean).length < 2) return null
  return (
    <div style={{ display: 'flex', gap: 1, width: 240, maxWidth: '100%', height: 6, borderRadius: 2, overflow: 'hidden' }} aria-hidden="true">
      {by.map((b, s) => (b > 0 ? <span key={s} style={{ flex: `${b} 0 0`, minWidth: 2, ...swatchStyle(s) }} /> : null))}
    </div>
  )
}

// What the header names while zoomed: the root's word, or the folder's name
// (off the disk, mono) or state (words).
function zoomLabel(model, zoom) {
  if (zoom.folder == null) return { text: ROOT_SHORT[zoom.root] || zoom.root, mono: false }
  const r = model.roots.find(x => x.name === zoom.root)
  const f = r?.folders[0]
  if (f?.state != null) return { text: STATES[f.state].name, mono: false }
  return { text: f?.name || '(top level)', mono: true }
}

const HOVER_NAME = { title: ITEM_TITLE, words: ITEM_TITLE, mono: MONO_TITLE }

// The idle strip's word on scale, beside its figures: the map is to scale
// except for the gaps it raised ("Sizing" in the rules), and says how many,
// since those are the only tiles that aren't their size. Nothing raised,
// nothing said. It shared one line with two other notes, which a laptop cut off.
function scaleNote(raised) {
  if (!(raised > 0)) return null
  return `To scale, except ${raised.toLocaleString()} small gap${raised !== 1 ? 's' : ''} drawn larger`
}
// The folders too small to draw, the first few by name.
function tinyNote(tiny) {
  if (!tiny.length) return null
  const names = tiny.map(t => t.name || '(top level)')
  return `Too small to draw: ${names.slice(0, 3).join(', ')}${names.length > 3 ? ` and ${names.length - 3} more` : ''}.`
}
const sameTiny = (a, b) => a.length === b.length && a.every((t, i) => t.name === b[i].name)

export default function LibraryMap({ details, onNavigate, row, rowModel, focus, mediaFiles, torrentFiles, ensureFilesLoaded }) {
  const [detail, setDetail] = useState('titles')     // Titles by default, every visit (the user's call)
  const [width, setWidth] = useState(0)
  const [themeTick, setThemeTick] = useState(0)
  const [hover, setHover] = useState(null)           // { box, text }
  const [userH, setUserH] = useState(storedHeight)   // null = the width's default
  const [dragging, setDragging] = useState(false)
  const [zoom, setZoom] = useState(null)             // null = the whole map; ephemeral, like the detail switch
  const [highlight, setHighlight] = useState(null)   // a state lit from the legend; ephemeral
  const [showFree, setShowFree] = useState(storedFree)
  // What the drawn layout raised and dropped, for the strip. Kept as state,
  // set when the map draws: read off layoutRef during the render, it was the
  // layout before, so a zoom said "41 gaps drawn larger" from the whole map.
  const [drawnInfo, setDrawnInfo] = useState({ raised: 0, tiny: [] })
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
  // The header's stats and the legend stay the whole library's; only the
  // canvas zooms, and a folder's own figures are on its heading.
  const totals = useMemo(() => (model ? modelTotals(model) : null), [model])
  const zoomed = useMemo(() => (model && zoom ? zoomModel(model, zoom) : null), [model, zoom])
  const activeZoom = zoomed ? zoom : null             // a zoom a rescan took away draws the whole map
  // Free space joins what's drawn on the whole map only; a zoom is one folder's room.
  const free = useMemo(() => freeRoots(details?.disk_space), [details?.disk_space])
  const shown = useMemo(() => zoomed || (model && showFree && free.length ? { roots: [...model.roots, ...free] } : model),
    [zoomed, model, showFree, free])
  const toggleFree = () => { const on = !showFree; setShowFree(on); storeFree(on) }
  // What the highlighted state holds on the map as drawn (zoomed or not).
  const lit = useMemo(() => {
    if (highlight == null || !shown) return null
    let bytes = 0, titles = 0
    for (const r of shown.roots) for (const f of r.folders) for (const t of f.titles) {
      if (t.by[highlight] > 0) { bytes += t.by[highlight]; titles += 1 }
    }
    return { bytes, titles }
  }, [highlight, shown])
  const zoomCrumb = activeZoom ? zoomLabel(shown, activeZoom) : null
  const shownTitles = zoomed ? zoomed.roots.reduce((a, r) => a + r.folders.reduce((b, f) => b + f.titles.length, 0), 0) : 0

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
  }, [shown != null])
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
    if (!shown || !width || !canvasRef.current) return
    const t0 = performance.now()
    const L = draw(canvasRef.current, shown, width, height, drawn, highlight)
    layoutRef.current = L
    drawMs.current = performance.now() - t0
    setDrawnInfo(d => (d.raised === L.raised && sameTiny(d.tiny, L.tiny) ? d : { raised: L.raised, tiny: L.tiny }))
    setHover(null)
  }, [shown, width, height, drawn, themeTick, highlight])

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

  // What's under a pointer event: a heading (which zooms; its outline is the
  // whole folder or root it would fill the map with) or a tile. A click works
  // it out from its own coordinates, never from the hover state, which a click
  // straight after a move (a tap on a touchscreen) arrives ahead of.
  const hitAt = e => {
    const r = e.currentTarget.getBoundingClientRect()
    const x = e.clientX - r.left, y = e.clientY - r.top
    const fr = freeAt(layoutRef.current, x, y)
    if (fr) {
      return { box: fr, outline: false, text: {
        thumb: { icon: 'drive' }, s: null, prefix: '', name: fr.label, kind: 'words',
        context: fr.name === 'free' ? 'On the drive your library and torrent folders share'
          : fr.name === 'free-torrents' ? "On your torrent folder's drive" : "On your library folder's drive",
        facts: fr.total ? `${formatBytes(fr.v)} of ${formatBytes(fr.total)} · ${Math.round(fr.v / fr.total * 100)}% free` : formatBytes(fr.v),
        by: null, action: null, note: null } }
    }
    const h = headingAt(layoutRef.current, x, y)
    const target = h && model ? headingTarget(model, activeZoom, h) : undefined
    if (h && target !== undefined) {
      const out = target === null || (activeZoom?.folder != null && target.folder == null)
      return { box: h.kind === 'folder' ? h.f : h.r, heading: true, text: headingText(h, target, out) }
    }
    const leaf = leafAt(layoutRef.current, x, y)
    return leaf ? { box: drawn === 'files' ? leaf : leaf.T, leaf, text: hoverText(leaf, drawn) } : null
  }
  const onMove = e => {
    const hit = hitAt(e)
    if (!hit) { if (hover) setHover(null); return }
    if (hover && hover.box === hit.box && hover.leaf?.s === hit.leaf?.s) return
    setHover(hit)
  }
  const onClick = e => {
    const hit = hitAt(e)
    if (hit?.heading) setZoom(hit.text.target)
    else if (hit?.text?.nav) onNavigate(hit.text.nav)
  }
  const cursor = hover?.heading ? (hover.text.zoom === 'out' ? 'zoom-out' : 'zoom-in')
    : hover?.text?.nav ? 'pointer' : 'default'
  const arr = useArrDetail(hover?.text?.rel || null)

  // A gap hovered on the Biggest gaps card: outline its tile and name it here.
  // A torrent release has a tile in each state's block, so it's the gap's state's.
  useEffect(() => {
    if (!focus) { setHover(null); return }
    const t = focus.t, s = focus.state
    const box = layoutRef.current?.titles.find(x => x.root === t.root && x.rel === t.rel && x.name === t.name
      && (t.root !== 'torrents' || x.by[s] > 0))
    const lf = box && layoutRef.current.leaves.find(l => l.T === box && l.s === s)
    const leaf = { s, T: t, bytes: t.v, boost: lf?.boost }
    const text = hoverText(leaf, 'titles')
    // A gap outside the zoom has no tile to outline, so the strip says why.
    setHover({ box: box || null, leaf, text: !box && activeZoom ? { ...text, drawn: 'Outside this zoom' } : text })
  }, [focus])

  const stats = totals ? [
    ...diskStats(details || {}),
    ['Fully linked', `${totals.full.toLocaleString()} titles`],
    ['Partly linked', `${totals.partly.toLocaleString()} titles`],
    ['Not linked', `${totals.none.toLocaleString()} titles`],
    ['Files', totals.files.toLocaleString()],
  ] : []
  return (
    <div style={{ ...CARD, padding: '18px 20px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px 12px', marginBottom: 10 }}>
        {/* While zoomed, the header names what fills the map and offers the way back. */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, minHeight: 'var(--control-h)' }}>
          <span style={{ ...SECTION_LABEL, flexShrink: 0 }}>Library map</span>
          {zoomCrumb && (
            <>
              <span style={{ fontSize: 'var(--font-md)', color: 'var(--text-dim)' }}>/</span>
              <span style={{ ...(zoomCrumb.mono ? MONO_TITLE : ITEM_TITLE), whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}>{zoomCrumb.text}</span>
              <Button size="sm" variant="ghost" onClick={() => setZoom(zoomOut(model, activeZoom))}>Zoom out</Button>
            </>
          )}
        </div>
        {/* Wraps on a narrow window rather than running off the card. */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', flexWrap: 'wrap', gap: '6px 10px', minWidth: 0, marginLeft: 'auto' }}>
          {detail === 'files' && !filesReady && (
            <span style={{ ...CAPTION, display: 'inline-flex', alignItems: 'center', gap: 6 }}><Spinner size={10} /> Loading file lists…</span>
          )}
          {/* No switches before the first scan that writes a map: there's nothing to switch. */}
          {row !== null && free.length > 0 && (
            // WinDirStat's <Free Space>, off unless asked for: it takes the
            // library's room, so fewer titles carry a name.
            <Button size="sm" variant={showFree ? 'secondary' : 'ghost'} pressed={showFree} onClick={toggleFree}
              title={activeZoom ? 'Free space shows on the whole map' : undefined}>
              Free space
            </Button>
          )}
          {row !== null && (
            <Segmented label="Map detail" value={detail} onChange={setDetail}
              options={[{ value: 'titles', label: 'Titles' }, { value: 'files', label: 'Files' }]} />
          )}
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
            aria-label={`Library map${zoomCrumb ? `, zoomed into ${zoomCrumb.text}` : ''}: ${formatBytes(totals?.by[LINKED] || 0)} linked, ${formatBytes(totals?.problem || 0)} for the workflows`}
            onMouseMove={onMove}
            onClick={onClick}
            style={{ display: 'block', borderRadius: 4, cursor }}
          />
          {hover?.box && hover.outline !== false && (
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

      {/* The detail strip (2026-11-10): one height whatever it shows, so the
          card never moves under the pointer. See "The detail strip" above. */}
      {model && model.roots.length > 0 && (() => {
        const tooSmall = tinyNote(drawnInfo.tiny)
        const scale = scaleNote(drawnInfo.raised)
        // What's drawn, from its folders: a zoomed root keeps the whole root's
        // size for its heading, so its `v` said 17.2 TB over 7.1 TB of movies.
        const shownBytes = shown ? shown.roots.filter(r => !r.free).reduce((a, r) => a + r.folders.reduce((b, f) => b + f.v, 0), 0) : 0
        const d = hover?.text || (lit ? {
          thumb: { icon: 'map' }, s: highlight, prefix: '', name: `Highlighting ${STATES[highlight].name.toLowerCase()}`, kind: 'words',
          context: zoomCrumb ? `In ${zoomCrumb.text}` : null,
          facts: `${formatBytes(lit.bytes)} in ${lit.titles.toLocaleString()} title${lit.titles !== 1 ? 's' : ''}`,
          note: 'Click it again in the legend to show everything.',
        } : zoomCrumb ? {
          thumb: { icon: 'folder' }, s: null, prefix: '', name: `${shownTitles.toLocaleString()} title${shownTitles !== 1 ? 's' : ''} in ${zoomCrumb.text}`, kind: 'words',
          context: 'Click a tile to open its workflow, or the heading to zoom out.',
          facts: formatBytes(shownBytes), drawn: scale, note: tooSmall,
        } : {
          thumb: { icon: 'map' }, s: null, prefix: '', name: 'Hover a tile for its details', kind: 'words',
          context: "Click a tile to open its workflow, or a folder's name to zoom in.",
          facts: `${totals.titles.toLocaleString()} titles · ${formatBytes(shownBytes)}`, drawn: scale,
          note: [!model.roots.some(r => r.name === 'torrents') && "Your torrent folder is all linked, so it's drawn in the library.", tooSmall].filter(Boolean).join(' ') || null,
        })
        const context = d.rel ? arrLine(arr.info, d.name) : d.context
        const ellipsis = { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }
        return (
          <div data-map-detail="" style={{ display: 'flex', alignItems: 'center', gap: 14, height: 92, marginTop: 4, minWidth: 0 }}>
            <Thumb thumb={d.thumb} poster={d.rel ? arr.poster : null} />
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0, flex: 1 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                {d.s != null && <Swatch s={d.s} />}
                <span style={{ display: 'flex', alignItems: 'baseline', gap: 6, minWidth: 0 }}>
                  {d.prefix && <span style={{ ...READOUT, ...ellipsis, maxWidth: 240, flexShrink: 0 }}>{d.prefix} /</span>}
                  <span style={{ ...HOVER_NAME[d.kind], ...ellipsis }}>{d.name}</span>
                </span>
              </div>
              {/* A title's arr line keeps its room while it's asked for, so the lines don't jump. */}
              {(context || d.rel) && <span style={{ ...CAPTION, ...ellipsis, minHeight: 14 }}>{context}</span>}
              {d.facts && (
                // The figures, and beside them in words how much larger the tile is drawn.
                <span style={{ display: 'flex', alignItems: 'baseline', gap: 10, minWidth: 0 }}>
                  <span style={{ ...READOUT, ...ellipsis, flex: '0 1 auto' }}>{d.facts}</span>
                  {d.drawn && <span style={{ ...CAPTION, ...ellipsis, flex: '0 10 auto' }} title={d.drawn}>{d.drawn}</span>}
                </span>
              )}
              <StateBar by={d.by} />
              {d.action
                ? <span style={{ ...CAPTION, ...ellipsis, color: 'var(--accent)' }}>{d.action}</span>
                : d.note && <span style={{ ...CAPTION, ...ellipsis }} title={d.note}>{d.note}</span>}
            </div>
          </div>
        )
      })()}

      {/* The legend: click a state to fade everything else on the map
          (index.css's .map-key, raised like a selected segment while lit). */}
      {totals && (
        <div role="group" aria-label="Highlight a state on the map"
          style={{ display: 'flex', flexWrap: 'wrap', gap: '2px 6px', marginTop: 4, marginLeft: -7 }}>
          {LEGEND.map(s => [STATES[s], s]).map(([st, s]) => (
            <button key={st.name} type="button" className="map-key" aria-pressed={highlight === s}
              disabled={!totals.by[s]} onClick={() => setHighlight(highlight === s ? null : s)}
              title={totals.by[s] ? (highlight === s ? 'Show everything' : `Highlight ${st.name.toLowerCase()} on the map`) : undefined}>
              <Swatch s={s} />
              {st.name}{st.workflow && <span>· {st.workflow}</span>}
              <span style={FIGURE}>{formatBytes(totals.by[s])}</span>
            </button>
          ))}
        </div>
      )}

    </div>
  )
}

// ── Biggest gaps ─────────────────────────────────────────────────────────────
// The titles with the most bytes for a workflow, each workflow's biggest always
// among them (2026-11-10), shown by size (2026-11-12), from the stored scan whatever the map's Titles/Files
// switch says. A card of its own since 2026-11-10, left
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
                      title={!nav ? undefined : nav.tab === 'backfill'
                        ? 'Search for this title in Backfill'
                        : `Open ${STATES[g.state].workflow} on this title`}
                      style={{ '--btn-border': 'transparent', width: '100%', justifyContent: 'flex-start', gap: 10, padding: '4px 8px', fontWeight: 400 }}>
                      <Swatch s={g.state} />
                      <span style={{ ...(g.t.root === 'torrents' ? MONO_TITLE : ITEM_TITLE), flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', textAlign: 'left' }}>{shortName(g.t)}</span>
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
