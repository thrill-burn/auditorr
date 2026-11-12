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

// **A dead seed is a library state** (2026-11-11): a library file linked to a
// torrent its tracker has deleted. Its bytes are the library file's, drawn
// once, there. Triage removes the torrent and its torrent-side link, and the
// file turns Not seeded, Backfill's. It was drawn Linked, which looked
// healthy while Triage listed it. Index 6, appended so a version 1 row's six
// sums keep their places; LEGEND and PROBLEM put it in the cards' order.
// Triage's red, so it differs from Not imported by pattern (horizontal rows)
// and by name, never by colour alone.
export const LINKED = 0, UNSEEDED = 1, ORPHANED = 2, NOT_IMPORTED = 3, COPY = 4, OTHER = 5, DEAD_SEED = 6

export const STATES = [
  { name: 'Linked',       workflow: null,       tab: null,       color: 'linked', pattern: null },
  { name: 'Not seeded',   workflow: 'Backfill', tab: 'backfill', color: 'blue',   pattern: 'slash' },
  { name: 'Orphaned',     workflow: 'Cleanup',  tab: 'cleanup',  color: 'yellow', pattern: 'dots' },
  { name: 'Not imported', workflow: 'Triage',   tab: 'triage',   color: 'red',    pattern: 'back' },
  { name: 'Copies',       workflow: 'Dedupe',   tab: 'dedupe',   color: 'purple', pattern: 'cross' },
  { name: 'Other',        workflow: null,       tab: null,       color: 'other',  pattern: null },
  { name: 'Dead seed',    workflow: 'Triage',   tab: 'triage',   color: 'red',    pattern: 'rows' },
]
export const PROBLEM = [UNSEEDED, ORPHANED, NOT_IMPORTED, DEAD_SEED, COPY]
// The legend's entries, in the cards' order (Other has none).
export const LEGEND = [LINKED, UNSEEDED, ORPHANED, NOT_IMPORTED, DEAD_SEED, COPY]
const N_STATES = STATES.length
// The torrent root's blocks, in the cards' order.
const TORRENT_GROUPS = [ORPHANED, NOT_IMPORTED, COPY, OTHER]

// `dead`: the file_ids of dead seeds' torrent records (`deadSeeds`), as
// `audit.library_map_row` joins them, by device and inode.
export function stateOfMedia(f, dead) {
  if (f.linked_paths?.length) return dead && dead.has(f.file_id) ? DEAD_SEED : LINKED
  if (f.duplicate_paths?.length) return COPY
  return UNSEEDED
}

// `audit._is_dead_seed_torrent`: imported, not orphaned, and its tracker has
// deleted it. Change one and change the other.
export function isDeadSeed(f) {
  return !f.excluded && !!f.imported && f.status !== 'Orphaned' && f.tracker_health === 'unregistered'
}

// file_id → the dead torrent's own folder (`category/release`), which is what
// Triage's search matches: its rows are torrents, named as the client names them.
function deadSeeds(torrents) {
  const dead = new Map()
  for (const f of torrents || []) {
    if (f.linked_paths?.length && f.file_id && isDeadSeed(f)) {
      const [folder, title] = titleOf(f.path)
      dead.set(f.file_id, relOf(folder, title))
    }
  }
  return dead
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

const zeros = () => [0, 0, 0, 0, 0, 0, 0]
const sum = a => a.reduce((x, y) => x + y, 0)

// ── The model ────────────────────────────────────────────────────────────────
// { roots: [{ name, folders: [{ name, state?, titles: [title] }] }] }
// title: { name, folder, root, rel, by: [7 sums], v, files: n, list?: [{name, size, s}], folded?, search? }
// `rel` is the root-relative folder a click scopes a workflow to; `search` a
// library title's dead torrent's folder, which Triage's search matches.

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
// Version 1 has six sums; version 2 (2026-11-11) seven, and a dead seed's
// search after them.
export function modelFromRow(row) {
  const lib = {}, tor = []
  const n = (row.version || 1) >= 2 ? N_STATES : 6
  const sums = rest => { const by = zeros(); for (let s = 0; s < n; s++) by[s] = rest[s] || 0; return by }
  const libFolder = name => (lib[name] ||= { name, titles: [] })
  for (const [fi, name, files, ...rest] of row.titles || []) {
    const [root, folder] = row.folders[fi]
    const t = { name, folder, root, rel: relOf(folder, name), by: sums(rest), files }
    if (typeof rest[n] === 'string') t.search = rest[n]
    if (root === 'library') libFolder(folder).titles.push(t)
    else tor.push({ ...t, name: relOf(folder, name) })
  }
  for (const [fi, count, files, ...rest] of row.folded || []) {
    const [root, folder] = row.folders[fi]
    const t = { name: `${count.toLocaleString()} smaller titles`, folder, root, rel: folder, by: sums(rest), files, folded: count }
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
  const dead = deadSeeds(torrents)
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
    return t
  }
  for (const f of media || []) {
    if (f.excluded) continue
    const s = stateOfMedia(f, dead)
    const t = add('library', f, s)
    if (s === DEAD_SEED && !t.search) t.search = dead.get(f.file_id)
  }
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
      // A dead seed is still linked: what Fully/Partly/Not linked count.
      if (!(t.by[UNSEEDED] > 0) && !(t.by[COPY] > 0)) full += 1
      else if (t.by[LINKED] > 0 || t.by[DEAD_SEED] > 0) partly += 1
      else none += 1
    }
  }
  return { by, files, full, partly, none, titles, problemTitles: problemKeys.size,
           problem: PROBLEM.reduce((x, s) => x + by[s], 0) }
}

// The titles with the most bytes for a workflow — "Biggest gaps". The biggest
// of each workflow leads, then the rest by size (2026-11-10): by size alone a
// library's Backfill titles filled the list, and on the box the first Triage
// row was 43rd.
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
  const all = [...seen.values()].sort((a, b) => b.bytes - a.bytes)
  const lead = []
  for (const g of all) if (!lead.some(x => x.state === g.state)) lead.push(g)
  return [...lead, ...all.filter(g => !lead.includes(g))].slice(0, n)
}

export function worstState(by) {
  let best = LINKED, most = 0
  for (const s of PROBLEM) if (by[s] > most) { most = by[s]; best = s }
  return best
}

// Where a click on a tile in this state goes: the workflow, scoped to the title.
// Backfill takes a library folder (BACKFILL B15); Cleanup and Triage a search
// for the release's path; Dedupe has no filter, so it opens whole. A dead seed
// is a library tile whose Triage row is the torrent, so it searches for the
// torrent's folder (`search`), and opens Triage whole when a row didn't carry
// one (a folded tile, a version 1 row).
export function navFor(state, title) {
  const tab = STATES[state]?.tab
  if (!tab) return null
  if (tab === 'backfill') return title.root === 'library' ? { tab, under: title.rel } : null
  if (state === DEAD_SEED) return title.search ? { tab, search: title.search } : { tab }
  if (tab === 'cleanup' || tab === 'triage') return { tab, search: title.rel }
  return { tab }
}

// ── Layout ───────────────────────────────────────────────────────────────────

// Squarified (Bruls, Huizing & van Wijk). Each item carries its size under
// `key` (`v`, bytes, or `a`, its drawn size: see Sizing below); the result is
// the items with x, y, w, h added, largest first.
export function squarify(items, x, y, w, h, key = 'v', ordered = false) {
  const out = []
  const total = items.reduce((t, i) => t + (i[key] > 0 ? i[key] : 0), 0)
  if (!total || w <= 0 || h <= 0) return out
  const live = items.filter(i => i[key] > 0)
  const rest = (ordered ? live : live.sort((p, q) => q[key] - p[key])).map(i => ({ i, a: i[key] * w * h / total }))
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

// Titles are laid out wide, not square (2026-11-10). A name is one line of
// text, so a tile 2:1 wide holds one at half the area a square needs: at the
// default size on the user's portrait screen, the box's map named 18 of its
// 729 titles (22% of the bytes) square, and 108 (49%) wide, with the label
// rule in LibraryMap.jsx. Squarified in a space twice as tall, then squashed:
// its squares there are 2:1 here. Roots, folders and the leaves inside a
// title stay square; only titles carry names.
export const TITLE_ASPECT = 2
function squarifyWide(items, x, y, w, h, key = 'v', k = TITLE_ASPECT) {
  return squarify(items, x, 0, w, h * k, key).map(t => ({ ...t, y: y + t.y / k, h: t.h / k }))
}

export const ROOT_GAP = 6, GAP = 3, ROOT_HEAD = 22, HEAD = 18

// ── Sizing (2026-11-11) ──────────────────────────────────────────────────────
// Every tile's area is its bytes, with one exception: a gap (a title's bytes in
// a state a workflow acts on, PROBLEM) that would be too small to see is drawn
// at the smallest size that shows. By bytes alone a well-linked library's
// torrent folder is a sliver: on the box, three not-imported episodes (23 GB
// beside 18.8 TB) were a strip 1–3px wide, which the layout dropped, so Triage
// had rows the map never drew.
//
// It replaced Attention (2026-11-10), which drew each state as much larger as
// the health score weighs it (a not-imported byte 16× a linked one), raised
// every gap toward the median title, and widened a root holding one to 140px,
// which on the portrait default gave 23 GB of torrents an eighth of the map.
// The user (2026-11-11): "we're changing the size of things too crazily". What
// needs nothing keeps its size, a gap too small to draw gets a minimum, and
// that's all, so the header's Attention / Actual size switch went with it.
//
// 1. A gap is at least FLOOR_PX on its short side. A title that's one gap and
//    nothing else (every torrent release, a library title with nothing linked)
//    is at least TITLE_ASPECT·FLOOR_PX × FLOOR_PX, the shape titles are laid
//    out in. A gap beside other bytes is a stripe down its title's short side,
//    so it's at least STRIPE_PX by that side: a square floor there would be a
//    hairline down a big tile's edge.
// 2. A small root is a box, not a strip. Squarify gives a small root a strip
//    the full height of the map: nothing inside a 2px one is drawn, and widened
//    to 27px for its gaps (the first cut of this) it was a column 605px tall of
//    releases standing on end (the user: "the torrent folder is too tall?").
//    So a root that would be a strip under NEST_PX across is laid out as one
//    more box among the other root's folders, and one holding a gap is at
//    least ROOT_BOX_PX square, room for its heading and its blocks'. The room
//    is its gaps', shared equally, never what else the root holds.
// 3. Floors take at most FLOOR_BUDGET of the canvas. Past that they shrink
//    together, never below their bytes, and a zoom gives them room again.
//
// Only a gap is raised, so every tile that needs nothing is drawn at one scale,
// its bytes'. What the raises take comes from all of them in proportion, so
// the rest of the map keeps its proportions. The pixels a byte
// gets are measured, not assumed: the gaps and headings between folders take a
// share of the canvas, so floors are set against a layout drawn by bytes. Every
// figure (headings, hover, legend, the header's stats) stays bytes.
export const FLOOR_PX = 12, STRIPE_PX = 8, FLOOR_BUDGET = 0.1, NEST_PX = 80, ROOT_BOX_PX = 120

// The drawn size of every title, folder and root, from its titles' parts.
function sums(model, eby) {
  const title = new Map(), folder = new Map(), root = new Map()
  let total = 0
  for (const r of model.roots) {
    let ra = r.free ? r.v : 0
    for (const f of r.folders) {
      let fa = 0
      for (const t of f.titles) { const a = sum(eby.get(t)); title.set(t, a); fa += a }
      folder.set(f, fa); ra += fa
    }
    root.set(r, ra); total += ra
  }
  return { title, folder, root, total }
}

// The share of the canvas a byte's tile really gets, the rest going to the
// roots' and folders' gaps and headings, measured on the layout by bytes. It's
// all lost at those two levels (titles fill their folder, and leaves their
// title), so the layout stops at folders: one by titles made Files on 200,000
// files a third slower to draw.
function drawnShare(model, W, H, A) {
  let px = 0, bytes = 0
  for (const f of place(model, W, H, null, A).folders) { px += f.w * (f.h - (f.head ? HEAD : 0)); bytes += f.a }
  return bytes > 0 ? Math.min(1, px / bytes / (W * H / A.total)) : 1
}

// Each title's size by state as drawn (`eby`), with the rules above applied for
// a W × H canvas, and the sums its folders and roots are laid out by.
// `floored` lists each [title, state, size] rule 1 raised, `widened` each root
// rule 2 did, and `nested` the root drawn as a box inside `host`. Exported for
// backend_tests' guard, which holds "only a gap moves, and only up"
// (test_library_map_sizing.py).
export function mapSizes(model, W, H) {
  const eby = new Map(), gaps = []
  let calm = 0
  for (const r of model.roots) {
    if (r.free) { calm += r.v; continue }
    for (const f of r.folders) for (const t of f.titles) {
      eby.set(t, [...t.by])
      const alone = t.by.filter(b => b > 0).length === 1
      for (let s = 0; s < N_STATES; s++) {
        if (PROBLEM.includes(s) && t.by[s] > 0) gaps.push({ t, s, alone })
        else calm += t.by[s]
      }
    }
  }
  const floored = []
  let S = sums(model, eby)
  if (gaps.length && S.total > 0 && W > 0 && H > 0) {
    // Rule 1, in pixels: P is what the tiles get of the canvas.
    const P = W * H * drawnShare(model, W, H, { eby, ...S })
    const perByte = P / S.total
    for (const g of gaps) {
      const short = Math.sqrt(g.t.v * perByte / TITLE_ASPECT)
      g.f = g.alone ? TITLE_ASPECT * FLOOR_PX * FLOOR_PX : Math.max(FLOOR_PX * FLOOR_PX, STRIPE_PX * short)
    }
    // A floor in bytes is its share of everything drawn once the floors are
    // in: T = calm + Σ max(bytes, f·cut·T/P), which contracts while the floors
    // are a small share of the canvas.
    const drawnTotal = cut => {
      let T = S.total
      for (let i = 0; i < 100; i++) {
        let next = calm
        for (const g of gaps) next += Math.max(g.t.by[g.s], g.f * cut * T / P)
        if (Math.abs(next - T) <= next * 1e-9) return next
        T = next
      }
      return T
    }
    // Rule 3: only a gap that ends up raised costs anything. Cutting the floors
    // only lowers T, so no gap the cut leaves raised was not raised before it,
    // and the raised floors' total stays within the budget.
    let T = drawnTotal(1), cut = 1
    const need = gaps.reduce((a, g) => a + (g.f * T / P > g.t.by[g.s] ? g.f : 0), 0)
    if (need > FLOOR_BUDGET * P) { cut = FLOOR_BUDGET * P / need; T = drawnTotal(cut) }
    for (const g of gaps) {
      const f = g.f * cut * T / P
      if (f > g.t.by[g.s]) { eby.get(g.t)[g.s] = f; floored.push([g.t, g.s, f]) }
    }
    S = sums(model, eby)
  }
  // Rule 2: a root that would be a strip under NEST_PX across is drawn as a
  // box among the other root's folders (`nested`, inside `host`), and one
  // holding a gap is at least ROOT_BOX_PX square. The room comes from its gaps
  // alone, shared equally, so they keep their order and the smallest gains
  // most: shared by size, a 2 GB copy beside 23 GB of episodes came out a
  // tile 5px wide. Widened as a whole, the root stretched what needs nothing
  // with it, and an unfinished download beside three red episodes was drawn
  // 11× its size. Free space isn't a root here, so its switch never nests the
  // library or raises it.
  const widened = []
  let nested = null, host = null
  const solid = model.roots.filter(r => !r.free)
  if (solid.length === 2 && S.total > 0) {
    const [big, small] = [...solid].sort((p, q) => S.root.get(q) - S.root.get(p))
    if (S.root.get(small) / S.total * Math.max(W, H) < NEST_PX) {
      nested = small; host = big
      const k = ROOT_BOX_PX * ROOT_BOX_PX / (W * H)
      const a = S.root.get(small), need = k * (S.total - a) / (1 - k)
      if (k < 0.5 && a < need && PROBLEM.some(s => small.by[s] > 0)) {
        let n = 0
        for (const f of small.folders) for (const t of f.titles) for (const s of PROBLEM) if (t.by[s] > 0) n++
        const add = (need - a) / n
        for (const f of small.folders) for (const t of f.titles) { const e = eby.get(t); for (const s of PROBLEM) if (t.by[s] > 0) e[s] += add }
        widened.push(small)
        S = sums(model, eby)
      }
    }
  }
  return { eby, title: S.title, folder: S.folder, root: S.root, floored, widened, nested, host }
}

// A title's name as its tile and the Biggest gaps row show it: a torrent
// release without its category folder (`tv-sonarr/…` took the room the release
// name needed). The hover strip shows the folder as its prefix.
export function shortName(t) {
  if (t.root !== 'torrents' || t.folded || !t.folder) return t.name
  return t.name.startsWith(t.folder + '/') ? t.name.slice(t.folder.length + 1) : t.name
}

// Rectangles for every root, folder, title and leaf. A leaf is a title's share
// in one state (`detail` 'titles') or one file ('files'). `tiny` lists what was
// too small to draw, so the card can name it rather than drop it, and `raised`
// counts the gaps drawn at a floor (Sizing above), so it can say so.
// Each leaf's `boost` is how many times larger than its bytes it's drawn: 1
// for everything but a raised gap.
export function layoutMap(model, W, H, detail) {
  const L = place(model, W, H, detail, mapSizes(model, W, H))
  L.raised = 0
  for (const t of L.titles) for (const s of PROBLEM) if (t.eby[s] > t.by[s]) L.raised++
  return L
}

// The host's folders with the nested root in a corner. Squarify alone can't
// make a small item square among a few big folders: it gets whatever is left
// once they're placed, which on the box's landscape map was a 37×264 column
// (its folders are 41%, 41%, 12%, 5% and two slivers). So the box shares a
// column down the long side's end with the host's k smallest folders, the box
// at its foot, and the rest are squarified beside it: every area exact. k is
// the one that draws the box nearest the titles' 2:1, a folder beside it
// allowed twice as long a shape before it counts against it.
const aspect = o => (o && o.w > 0 && o.h > 0 ? Math.max(o.w / o.h, o.h / o.w) : Infinity)
// How far a box is from the titles' 2:1 wide shape: a wide box holds release
// names, a tall one doesn't.
const wideness = o => (o && o.w > 0 && o.h > 0 ? Math.max(o.w / o.h / TITLE_ASPECT, TITLE_ASPECT * o.h / o.w) : Infinity)
function nestedLayout(items, nest, x, y, w, h) {
  const sorted = items.filter(i => i.a > 0).sort((p, q) => q.a - p.a)
  const total = sorted.reduce((s, i) => s + i.a, 0) + nest.a
  const wide = w >= h
  let best = null, score = Infinity
  for (let k = 0; k <= sorted.length; k++) {
    const main = sorted.slice(0, sorted.length - k), side = sorted.slice(sorted.length - k)
    const c = (side.reduce((s, i) => s + i.a, 0) + nest.a) / total * (wide ? w : h)
    const col = wide ? [x + w - c, y, c, h] : [x, y + h - c, w, c]
    const out = [...squarify(main, x, y, wide ? w - c : w, wide ? h : h - c, 'a'),
                 ...squarify([...side, nest], ...col, 'a', true)]
    // Folders smaller than the box are slivers wherever they go.
    const beside = new Set(side.map(i => i.name))
    let s = wideness(out.find(o => o.nest))
    for (const o of out) if (!o.nest && beside.has(o.name) && o.a >= nest.a) s = Math.max(s, aspect(o) / 2)
    if (s < score) { best = out; score = s }
  }
  return best
}

// The geometry, for drawn sizes `A` (mapSizes, or the bytes alone). A `detail`
// of null stops at folders. A nested root (Sizing's rule 2) is laid out as one
// more item among its host's folders, and drawn in that box as a root.
function place(model, W, H, detail, A) {
  const roots = [], folders = [], titles = [], leaves = [], tiny = []
  const nest = A.nested?.name, host = A.host?.name
  const placeRoot = (r, X, Y, RW, RH) => {
    const rx = X + ROOT_GAP / 2, ry = Y + ROOT_GAP / 2, rw = RW - ROOT_GAP, rh = RH - ROOT_GAP
    if (rw < 4 || rh < 4) { tiny.push({ name: r.free ? r.label : r.name === 'library' ? 'Library folder' : 'Torrent folder', v: r.v }); return }
    if (r.free) { roots.push({ ...r, x: rx, y: ry, w: rw, h: rh, head: false }); return }
    const head = rw >= 56 && rh >= 60
    roots.push({ ...r, x: rx, y: ry, w: rw, h: rh, head, nested: r.name === nest })
    const fy = head ? ry + ROOT_HEAD : ry, fh = head ? rh - ROOT_HEAD : rh
    const items = r.folders.map(x => ({ ...x, a: A.folder.get(x) }))
    for (const f of r.name === host ? nestedLayout(items, { nest: A.nested, a: A.root.get(A.nested) }, rx, fy, rw, fh)
                                    : squarify(items, rx, fy, rw, fh, 'a')) {
      if (f.nest) { placeRoot(f.nest, f.x, f.y, f.w, f.h); continue }
      const x = f.x + GAP / 2, y = f.y + GAP / 2, w = f.w - GAP, h = f.h - GAP
      if (w < 3 || h < 3) { tiny.push(f); continue }
      const fhead = w >= 56 && h >= 44
      folders.push({ ...f, root: r.name, x, y, w, h, head: fhead })
      if (detail == null) continue
      const iy = fhead ? y + HEAD : y, ih = fhead ? h - HEAD : h
      const ts = f.titles.map(t => ({ ...t, a: A.title.get(t), eby: A.eby.get(t) }))
      for (const t of squarifyWide(ts, x, iy, w, ih, 'a')) {
        titles.push(t)
        if (detail === 'files' && t.list) {
          // A file is drawn at its state's scale within the title.
          const scale = t.by.map((b, s) => (b > 0 ? t.eby[s] / b : 0))
          for (const l of squarify(t.list.map(q => ({ ...q, a: q.size * scale[q.s] })), t.x, t.y, t.w, t.h, 'a')) {
            leaves.push({ x: l.x, y: l.y, w: l.w, h: l.h, s: l.s, T: t, file: l.name, bytes: l.size, boost: scale[l.s] })
          }
        } else {
          for (const p of squarify(t.by.map((v, s) => ({ v, s, a: t.eby[s] })), t.x, t.y, t.w, t.h, 'a')) {
            leaves.push({ x: p.x, y: p.y, w: p.w, h: p.h, s: p.s, T: t, bytes: p.v, boost: t.eby[p.s] / p.v })
          }
        }
      }
    }
  }
  const top = model.roots.filter(r => r.name !== nest)
  const size = x => A.root.get(x) + (x.name === host ? A.root.get(A.nested) : 0)
  for (const r of squarify(top.map(x => ({ ...x, a: size(x) })), 0, 0, W, H, 'a')) placeRoot(r, r.x, r.y, r.w, r.h)
  return { roots, folders, titles, leaves, tiny }
}

// The leaf under a point, or null.
export function leafAt(layout, x, y) {
  if (!layout) return null
  for (const l of layout.leaves) if (x >= l.x && x < l.x + l.w && y >= l.y && y < l.y + l.h) return l
  return null
}

// ── Zoom (2026-11-10) ────────────────────────────────────────────────────────
// A heading zooms: a folder's fills the map with that folder, and a root's with
// that root. However the map is laid out, an overview of a big library can only
// name its biggest titles (729 titles on the box at the portrait default are
// ~930px² each); one folder on the whole map is how the rest get room. A zoom
// is `{root}` or `{root, folder}`, and `null` is the whole map. It holds keys,
// not objects, so it survives a rescan and the Titles/Files switch.

// A folder's key: its name in the library, its state in the torrent root.
export const folderKey = f => (f.state != null ? `state:${f.state}` : `name:${f.name}`)
export const sameZoom = (a, b) => !!a && !!b && a.root === b.root && (a.folder ?? null) === (b.folder ?? null)

// The model a zoom draws, or null when it no longer resolves (a rescan took
// the folder away). A zoomed root keeps its size, so its heading still says
// how big the whole root is.
export function zoomModel(model, zoom) {
  if (!zoom) return model
  const r = model.roots.find(x => x.name === zoom.root)
  if (!r) return null
  if (zoom.folder == null) return { roots: [r] }
  const f = r.folders.find(x => folderKey(x) === zoom.folder)
  return f ? { roots: [{ ...r, folders: [f] }] } : null
}

// One level out. A folder's parent is its root, unless that root is the whole
// map (the torrent root is often empty: everything linked), when it's the map.
export function zoomOut(model, zoom) {
  if (!zoom || zoom.folder == null) return null
  return model.roots.length > 1 ? { root: zoom.root } : null
}

// Where a click on a heading goes, or undefined for a heading that doesn't
// zoom (the only root's, on the whole map). Clicking the heading of what's
// already filling the map zooms out.
export function headingTarget(model, zoom, h) {
  if (h.kind === 'folder') {
    const to = { root: h.f.root, folder: folderKey(h.f) }
    return sameZoom(to, zoom) ? zoomOut(model, zoom) : to
  }
  if (model.roots.length <= 1) return zoom ? null : undefined
  const to = { root: h.r.name }
  return sameZoom(to, zoom) ? null : to
}

// The heading under a point: a folder's band or a root's line. A root or a
// folder too small to show its heading is its own heading, all of it: a torrent
// folder beside a big library is a sliver with no room to name it, which is
// where a zoom is wanted most, and tiles that small are no target for a click.
// **A tile at least POINT_PX each way is a target, and wins** (2026-11-11):
// since the floor (Sizing above), a torrent folder too narrow for its heading
// holds tiles you can see, and hovering a red one described the whole folder
// and a click zoomed rather than opening Triage.
export const POINT_PX = 6
export function headingAt(layout, x, y) {
  if (!layout) return null
  const inside = (b, h = b.h) => x >= b.x && x < b.x + b.w && y >= b.y && y < b.y + h
  for (const f of layout.folders) if (f.head && inside(f, HEAD)) return { kind: 'folder', f }
  for (const r of layout.roots) if (!r.free && r.head && inside(r, ROOT_HEAD)) return { kind: 'root', r }
  const leaf = leafAt(layout, x, y)
  if (leaf && leaf.w >= POINT_PX && leaf.h >= POINT_PX) return null
  for (const r of layout.roots) if (!r.free && !r.head && inside(r)) return { kind: 'root', r, whole: true }
  for (const f of layout.folders) if (!f.head && inside(f)) return { kind: 'folder', f, whole: true }
  return null
}

// The free-space tile under a point, or null.
export function freeAt(layout, x, y) {
  if (!layout) return null
  for (const r of layout.roots) if (r.free && x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h) return r
  return null
}

// ── Free space (2026-11-10) ──────────────────────────────────────────────────
// WinDirStat's <Free Space>, behind a switch: the drive's free bytes as a tile
// beside the roots, one per filesystem (one when both roots share it, which
// hardlinks need). Never part of the model the totals, the zoom and the
// headings read: the card adds it to what's drawn, on the whole map only.
export function freeRoots(ds) {
  if (!ds) return []
  const one = (x, label, name) => (x && x.free > 0
    ? [{ name, label, free: true, v: x.free, total: x.total, by: zeros(), folders: [] }] : [])
  if (ds.same || !ds.media || !ds.torrents) return one(ds.torrents || ds.media, 'Free space', 'free')
  return [...one(ds.torrents, 'Free for torrents', 'free-torrents'), ...one(ds.media, 'Free for the library', 'free-media')]
}
