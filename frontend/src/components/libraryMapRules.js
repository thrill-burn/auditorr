// The library map's rules (LibraryMap.jsx draws them). No React here, so they
// can be read, and run under plain node, apart from the canvas.
//
// The map is the disk as root → top-level folder → title, each title's bytes
// split by the workflow that acts on them, in the four dashboard cards' order
// and colours. **Linked bytes are drawn once, in the library**: a torrent file
// with a library link is the same inode, so the torrent root holds only what
// isn't linked. Titles come from the scan's stored row (`audit.library_map_row`);
// the Files view builds the same thing from both file lists, with
// `stateOfMedia` / `stateOfTorrent`, which are that function's rule in
// JavaScript. Change one and change the other.
//
// **The torrent root is grouped by state, not by folder** (2026-11-09, the user,
// who is colorblind, couldn't tell the orphaned tiles from the not-imported ones
// when they sat mixed in one category). Each state gets its own block with its
// name written on it, so where a tile sits says what it is. The library keeps
// its folders, because there the folder is the point, and its three states
// differ in pattern as well as colour.

export const LINKED = 0, UNSEEDED = 1, ORPHANED = 2, NOT_IMPORTED = 3, COPY = 4, OTHER = 5

export const STATES = [
  { name: 'Linked',       workflow: null,       tab: null,       color: 'linked', pattern: null },
  { name: 'Not seeded',   workflow: 'Backfill', tab: 'backfill', color: 'blue',   pattern: 'slash' },
  { name: 'Orphaned',     workflow: 'Cleanup',  tab: 'cleanup',  color: 'yellow', pattern: 'dots' },
  { name: 'Not imported', workflow: 'Triage',   tab: 'triage',   color: 'red',    pattern: 'back' },
  { name: 'Copies',       workflow: 'Dedupe',   tab: 'dedupe',   color: 'purple', pattern: 'cross' },
  { name: 'Other',        workflow: null,       tab: null,       color: 'other',  pattern: null },
]
export const PROBLEM = [UNSEEDED, ORPHANED, NOT_IMPORTED, COPY]
// The torrent root's blocks, in the cards' order.
const TORRENT_GROUPS = [ORPHANED, NOT_IMPORTED, COPY, OTHER]

export function stateOfMedia(f) {
  if (f.linked_paths?.length) return LINKED
  if (f.duplicate_paths?.length) return COPY
  return UNSEEDED
}

// null for a linked torrent file: the library side already drew it.
export function stateOfTorrent(f) {
  if (f.linked_paths?.length) return null
  if (f.status === 'Orphaned') return ORPHANED
  if (!f.imported && !f.sidecar && !f.incomplete) return NOT_IMPORTED
  if (f.duplicate_paths?.length) return COPY
  return OTHER
}

// [top-level folder, title] of a root-relative path, as `audit._map_title`.
export function titleOf(path) {
  const parts = String(path || '').replace(/\\/g, '/').split('/')
  if (parts.length === 1) return ['', parts[0]]
  return [parts[0], parts[1]]
}

const zeros = () => [0, 0, 0, 0, 0, 0]
const sum = a => a.reduce((x, y) => x + y, 0)

// ── The model ────────────────────────────────────────────────────────────────
// { roots: [{ name, folders: [{ name, state?, titles: [title] }] }] }
// title: { name, folder, root, rel, by: [6 sums], v, files: n, list?: [{name, size, s}], folded? }
// `rel` is the root-relative folder a click scopes a workflow to.

function finish(roots) {
  for (const r of roots) {
    r.by = zeros()
    for (const f of r.folders) {
      f.by = zeros()
      for (const t of f.titles) {
        t.v = sum(t.by)
        t.by.forEach((b, i) => { f.by[i] += b })
      }
      f.v = sum(f.by)
      f.titles = f.titles.filter(t => t.v > 0)
      f.by.forEach((b, i) => { r.by[i] += b })
    }
    r.folders = r.folders.filter(f => f.v > 0)
    r.v = sum(r.by)
  }
  return { roots: roots.filter(r => r.v > 0) }
}

// Torrent-side titles regrouped into one block per state (see the header).
function torrentBlocks(titles) {
  return TORRENT_GROUPS.map(s => ({
    name: STATES[s].name, state: s,
    titles: titles.filter(t => t.by[s] > 0).map(t => {
      const by = zeros(); by[s] = t.by[s]
      return { ...t, by, list: t.list && t.list.filter(x => x.s === s) }
    }),
  }))
}

const relOf = (folder, title) => (folder ? `${folder}/${title}` : title)

// From the scan's stored row: {version, folders: [[root, name]], titles: [...], folded: [...]}.
export function modelFromRow(row) {
  const lib = {}, tor = []
  const libFolder = name => (lib[name] ||= { name, titles: [] })
  for (const [fi, name, files, ...by] of row.titles || []) {
    const [root, folder] = row.folders[fi]
    const t = { name, folder, root, rel: relOf(folder, name), by: by.slice(0, 6), files }
    if (root === 'library') libFolder(folder).titles.push(t)
    else tor.push({ ...t, name: relOf(folder, name) })
  }
  for (const [fi, count, files, ...by] of row.folded || []) {
    const [root, folder] = row.folders[fi]
    const t = { name: `${count.toLocaleString()} smaller titles`, folder, root, rel: folder, by: by.slice(0, 6), files, folded: count }
    if (root === 'library') libFolder(folder).titles.push(t)
    else tor.push({ ...t, name: `${folder ? folder + ': ' : ''}${t.name}` })
  }
  return finish([
    { name: 'library', folders: Object.values(lib) },
    { name: 'torrents', folders: torrentBlocks(tor) },
  ])
}

// From both file lists (the Files view). Excluded files are left out, as the
// stored row leaves them out.
export function modelFromFiles(media, torrents) {
  const titles = new Map()
  const add = (root, f, s) => {
    const [folder, title] = titleOf(f.path)
    const key = `${root}\u0000${folder}\u0000${title}`
    let t = titles.get(key)
    if (!t) {
      t = { name: title, folder, root, rel: relOf(folder, title), by: zeros(), files: 0, list: [] }
      titles.set(key, t)
    }
    t.by[s] += f.size || 0
    t.files += 1
    const p = String(f.path || '')
    t.list.push({ name: p.slice(p.replace(/\\/g, '/').lastIndexOf('/') + 1), size: f.size || 0, s })
  }
  for (const f of media || []) if (!f.excluded) add('library', f, stateOfMedia(f))
  for (const f of torrents || []) {
    if (f.excluded) continue
    const s = stateOfTorrent(f)
    if (s != null) add('torrents', f, s)
  }
  const lib = {}, tor = []
  for (const t of titles.values()) {
    if (t.root === 'library') (lib[t.folder] ||= { name: t.folder, titles: [] }).titles.push(t)
    else tor.push({ ...t, name: t.rel })
  }
  return finish([
    { name: 'library', folders: Object.values(lib) },
    { name: 'torrents', folders: torrentBlocks(tor) },
  ])
}

// Totals for the map's header and the Biggest gaps card: bytes by state, the
// library's titles by how much of each is linked, and how many titles in
// either root have bytes for a workflow. A torrent release sits in one block
// per state, so it's counted once by its folder; a folded tile is many titles
// of unknown problems, so it isn't counted.
export function modelTotals(model) {
  const by = zeros()
  let files = 0, full = 0, partly = 0, none = 0, titles = 0
  const problemKeys = new Set()
  for (const r of model.roots) {
    r.by.forEach((b, i) => { by[i] += b })
    for (const f of r.folders) for (const t of f.titles) {
      files += t.files || 0
      if (t.folded) continue
      const bad = PROBLEM.some(s => t.by[s] > 0)
      if (bad) problemKeys.add(`${r.name}\u0000${t.rel}`)
      if (r.name !== 'library') continue
      titles += 1
      if (!bad) full += 1
      else if (t.by[LINKED] > 0) partly += 1
      else none += 1
    }
  }
  return { by, files, full, partly, none, titles, problemTitles: problemKeys.size,
           problem: PROBLEM.reduce((x, s) => x + by[s], 0) }
}

// The titles with the most bytes for a workflow, worst first — "Biggest gaps".
export function biggestGaps(model, n = 6) {
  const out = []
  for (const r of model.roots) for (const f of r.folders) for (const t of f.titles) {
    if (t.folded) continue
    const p = PROBLEM.reduce((x, s) => x + t.by[s], 0)
    if (p > 0) out.push({ t, bytes: p, state: worstState(t.by) })
  }
  // A torrent title appears once per state block; keep its biggest.
  const seen = new Map()
  for (const g of out) {
    const k = `${g.t.root}\u0000${g.t.rel}`
    if (!seen.has(k) || seen.get(k).bytes < g.bytes) seen.set(k, g)
  }
  return [...seen.values()].sort((a, b) => b.bytes - a.bytes).slice(0, n)
}

export function worstState(by) {
  let best = LINKED, most = 0
  for (const s of PROBLEM) if (by[s] > most) { most = by[s]; best = s }
  return best
}

// Where a click on a tile in this state goes: the workflow, scoped to the title.
// Backfill takes a library folder (BACKFILL B15); Cleanup and Triage a search
// for the release's path; Dedupe has no filter, so it opens whole.
export function navFor(state, title) {
  const tab = STATES[state]?.tab
  if (!tab) return null
  if (tab === 'backfill') return title.root === 'library' ? { tab, under: title.rel } : null
  if (tab === 'cleanup' || tab === 'triage') return { tab, search: title.rel }
  return { tab }
}

// ── Layout ───────────────────────────────────────────────────────────────────

// Squarified (Bruls, Huizing & van Wijk). Each item carries `v`; the result is
// the items with x, y, w, h added, largest first.
export function squarify(items, x, y, w, h) {
  const out = []
  const total = items.reduce((t, i) => t + (i.v > 0 ? i.v : 0), 0)
  if (!total || w <= 0 || h <= 0) return out
  const rest = items.filter(i => i.v > 0).sort((p, q) => q.v - p.v).map(i => ({ i, a: i.v * w * h / total }))
  while (rest.length) {
    const side = Math.min(w, h)
    let s = rest[0].a, mx = s, mn = s, k = 1
    let best = Math.max(side * side * mx / (s * s), (s * s) / (side * side * mn))
    while (k < rest.length) {
      const a = rest[k].a, s2 = s + a, mx2 = Math.max(mx, a), mn2 = Math.min(mn, a)
      const r = Math.max(side * side * mx2 / (s2 * s2), (s2 * s2) / (side * side * mn2))
      if (r > best) break
      s = s2; mx = mx2; mn = mn2; best = r; k++
    }
    const row = rest.splice(0, k)
    if (w >= h) {
      const cw = s / h
      let cy = y
      for (const r of row) { const ch = r.a / cw; out.push({ ...r.i, x, y: cy, w: cw, h: ch }); cy += ch }
      x += cw; w -= cw
    } else {
      const ch = s / w
      let cx = x
      for (const r of row) { const cw = r.a / ch; out.push({ ...r.i, x: cx, y, w: cw, h: ch }); cx += cw }
      y += ch; h -= ch
    }
  }
  return out
}

export const ROOT_GAP = 6, GAP = 3, ROOT_HEAD = 22, HEAD = 18

// Rectangles for every root, folder, title and leaf. A leaf is a title's share
// in one state (`detail` 'titles') or one file ('files'). `tiny` lists what was
// too small to draw, so the card can name it rather than drop it.
export function layoutMap(model, W, H, detail) {
  const roots = [], folders = [], titles = [], leaves = [], tiny = []
  for (const r of squarify(model.roots, 0, 0, W, H)) {
    const rx = r.x + ROOT_GAP / 2, ry = r.y + ROOT_GAP / 2, rw = r.w - ROOT_GAP, rh = r.h - ROOT_GAP
    if (rw < 4 || rh < 4) { tiny.push({ name: r.name === 'library' ? 'Library folder' : 'Torrent folder', v: r.v }); continue }
    const head = rw >= 56 && rh >= 60
    roots.push({ ...r, x: rx, y: ry, w: rw, h: rh, head })
    const fy = head ? ry + ROOT_HEAD : ry, fh = head ? rh - ROOT_HEAD : rh
    for (const f of squarify(r.folders, rx, fy, rw, fh)) {
      const x = f.x + GAP / 2, y = f.y + GAP / 2, w = f.w - GAP, h = f.h - GAP
      if (w < 3 || h < 3) { tiny.push(f); continue }
      const fhead = w >= 56 && h >= 44
      folders.push({ ...f, root: r.name, x, y, w, h, head: fhead })
      const iy = fhead ? y + HEAD : y, ih = fhead ? h - HEAD : h
      for (const t of squarify(f.titles, x, iy, w, ih)) {
        titles.push(t)
        if (detail === 'files' && t.list) {
          for (const l of squarify(t.list.map(q => ({ ...q, v: q.size })), t.x, t.y, t.w, t.h)) {
            leaves.push({ x: l.x, y: l.y, w: l.w, h: l.h, s: l.s, T: t, file: l.name, bytes: l.size })
          }
        } else {
          for (const p of squarify(t.by.map((v, s) => ({ v, s })), t.x, t.y, t.w, t.h)) {
            leaves.push({ x: p.x, y: p.y, w: p.w, h: p.h, s: p.s, T: t, bytes: p.v })
          }
        }
      }
    }
  }
  return { roots, folders, titles, leaves, tiny }
}

// The leaf under a point, or null.
export function leafAt(layout, x, y) {
  if (!layout) return null
  for (const l of layout.leaves) if (x >= l.x && x < l.x + l.w && y >= l.y && y < l.y + l.h) return l
  return null
}
