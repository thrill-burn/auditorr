"""The library map's sizing rule, run under node (2026-11-11).

The Dashboard's library map draws every tile at its bytes, with one exception
(`libraryMapRules.js`, "Sizing"): a gap, a title's bytes in a state a workflow
acts on, that would be too small to see is drawn at the smallest size that
shows. It exists because by bytes alone a well-linked library's torrent folder
was a sliver: on the box three not-imported episodes, 23 GB beside 18.8 TB,
were a 1-3px strip the layout dropped, so Triage had rows the map never drew.

It replaced Attention (2026-11-10), which weighted each state by the health
score, raised every gap toward the median title and widened a root holding one
to 140px. The user: "we're changing the size of things too crazily". The rule
now, and what this holds: **only a gap moves, and only up**. Everything else is
exactly its bytes; a gap is raised only when it's under its floor, never
weighted; the floors stay within their budget; a root is widened only to hold a
gap, by its gaps alone, so an unfinished download beside them isn't stretched;
a small root is a box among the other's folders, not a column the map's
height; and at the user's two screens every gap is drawn big enough to see and
click.

Rule 4 (2026-11-12) is the shape, which an area floor can't hold: beside 300
big orphans the not-imported block was a 2px strip and dropped, and zoomed
into the torrent folder a 47 MB orphan that was 56x19 on the whole map became
a 201x2 line. So this also holds, zoomed in as well as whole and beside a big
pile: no folder holding a gap is dropped or a sliver, no title holding one is,
every gap is drawn and clickable, and what rule 4 adds goes to gaps alone.

The rules are React-free JavaScript, so this runs them under node on a made-up
library shaped like the box's. It is one of five tests that read frontend files,
beside test_type_scale.py, test_tint_idiom.py, test_module_names.py and
test_library_map.py's state-rule parity test. Node is on GitHub's runners; a
machine without it skips, and CI without it fails.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

RULES = Path(__file__).resolve().parents[1] / 'frontend' / 'src' / 'components' / 'libraryMapRules.js'

SCRIPT = r"""
const R = await import(process.argv[1])
const problems = []
let checked = 0, raised = 0
const ok = (cond, msg) => { checked++; if (!cond) problems.push(msg) }
const close = (a, b) => Math.abs(a - b) <= Math.max(1, Math.abs(b)) * 1e-9
const sum = a => a.reduce((x, y) => x + y, 0)
const isGap = s => R.PROBLEM.includes(s)

// A library like the box's: 700 titles in four folders split as the box's are
// (41%, 41%, 12%, 5% of titles: a box among folders that coarse is what
// squarify alone made a strip of), most linked, one in
// nine with a not-seeded part (some a sliver, like a subtitle), a few with
// copies; the torrent folder holds three not-imported episodes, an orphaned
// 10 GB film, a small orphan, a copy and an unfinished download.
let seed = 7
const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647
const GB = 1e9, MB = 1e6
const folders = [['library', 'movies'], ['library', 'television'], ['library', 'kids_movies'],
                 ['library', 'kids_television'], ['torrents', 'tv-sonarr'], ['torrents', 'radarr']]
const folderOf = i => (i % 100 < 41 ? 0 : i % 100 < 82 ? 1 : i % 100 < 94 ? 2 : 3)
const library = []
for (let i = 0; i < 700; i++) {
  const by = [0, 0, 0, 0, 0, 0], size = Math.exp(rnd() * 6) * GB / 4
  if (i % 9 === 0) { const u = size * (i % 2 ? 1 : i % 4 ? 0.0005 : 0.02 + rnd() * 0.5); by[1] = u; by[0] = size - u }
  else by[0] = size
  if (i % 97 === 0) by[4] = size * 0.3
  library.push([folderOf(i), `Title ${i}`, 10, ...by])
}
const t = (fi, name, s, size) => { const by = [0, 0, 0, 0, 0, 0]; by[s] = size; return [fi, name, 1, ...by] }
const torrents = [
  t(4, 'Show.S01E03.2160p-GRP.mkv', 3, 11.3 * GB), t(4, 'Show.S01E07.2160p-GRP.mkv', 3, 6.6 * GB),
  t(4, 'Show.S01E08.2160p-GRP', 3, 5.5 * GB), t(5, 'Movie.2019.1080p-GRP', 2, 10 * GB),
  t(5, 'Tiny.2001.720p-GRP', 2, 50 * MB), t(5, 'Copy.2020.2160p-GRP', 4, 2 * GB),
  t(4, 'Downloading.S02.1080p-GRP', 5, 30 * GB),
]
const model = R.modelFromRow({ version: 1, folders, titles: [...library, ...torrents], folded: [] })
// The budget's case: 300 small orphans, more floors than the budget holds.
const flood = [...torrents]
for (let i = 0; i < 300; i++) flood.push(t(5, `Orphan.${i}.1080p-GRP`, 2, (50 + rnd() * 450) * MB))
const flooded = R.modelFromRow({ version: 1, folders, titles: [...library, ...flood], folded: [] })
// Nothing too small: every gap is a big one, so the map is exactly to scale.
const big = R.modelFromRow({ version: 1, folders, folded: [], titles: [
  ...library.map(([fi, name, n, ...by]) => [fi, name, n, ...by.map((b, s) => (s === 1 || s === 4 ? 0 : b))]),
  [0, 'Big gap', 10, 2000 * GB, 1500 * GB, 0, 0, 0, 0], t(5, 'Big.Orphan', 2, 2500 * GB)] })

// Rule 4's case: 300 big orphans (50 MB to 60 GB) beside two not-imported
// episodes, whose block squarify made a 2px strip, and the small orphan in
// `model` beside a 10 GB one, a line when zoomed into the torrent folder.
// Nothing else in the torrent folder: a download or a copy beside the
// episodes' block shares its strip and hides the bug.
const pile = []
for (let i = 0; i < 300; i++) pile.push(t(5, `Big.Orphan.${i}.1080p-GRP`, 2, Math.exp(Math.log(50 * MB) + rnd() * Math.log(1200)) ))
pile.push(t(4, 'Other.S02E05.1080p-GRP', 3, 4.2 * GB), t(4, 'Other.S02E06.1080p-GRP', 3, 4.2 * GB))
const piled = R.modelFromRow({ version: 1, folders, titles: [...library, ...pile], folded: [] })

const views = {
  whole: model, torrents: R.zoomModel(model, { root: 'torrents' }),
  orphaned: R.zoomModel(model, { root: 'torrents', folder: `state:${R.ORPHANED}` }),
  flooded, 'flooded torrents': R.zoomModel(flooded, { root: 'torrents' }),
  piled, 'piled torrents': R.zoomModel(piled, { root: 'torrents' }),
}
const screens = [[1110, 611], [2230, 820], [400, 300]]
for (const [vname, m] of Object.entries(views)) {
  for (const [W, H] of screens) {
    const at = `${vname}, ${W}x${H}`
    const A = R.mapSizes(m, W, H)
    const k = (t, s) => `${t.root}\u0000${t.name}\u0000${s}`
    const floor = new Map(A.floored.map(([t, s, f]) => [k(t, s), f]))
    const widened = new Set(A.widened.map(r => r.name))
    let T1 = 0, floors = 0, total = 0
    const alone = [[], [], [], [], [], []]
    for (const r of m.roots) {
      if (r.free) { T1 += r.v; total += r.v; continue }
      const added = []
      for (const f of r.folders) for (const t of f.titles) {
        const e = A.eby.get(t)
        total += sum(e)
        t.by.forEach((b, s) => {
          const f1 = floor.get(k(t, s))
          T1 += f1 ?? b
          // Only a gap moves, and only up; a gap not raised isn't weighted either.
          if (!isGap(s)) ok(e[s] === b, `${at}: ${t.name}'s ${R.STATES[s].name} drawn at ${e[s]}, not its ${b} bytes`)
          else if (f1 == null && !widened.has(r.name)) ok(e[s] === b, `${at}: ${t.name}'s ${R.STATES[s].name} gap moved without being raised`)
          if (f1 != null) { ok(f1 > b, `${at}: ${t.name}'s ${R.STATES[s].name} floored below its bytes`); floors += f1; raised++ }
          if (isGap(s) && b > 0) {
            ok(e[s] >= b, `${at}: ${t.name}'s ${R.STATES[s].name} drawn below its bytes`)
            if (widened.has(r.name)) added.push(e[s] - (f1 ?? b))
          }
          if (b > 0 && isGap(s) && t.by.filter(x => x > 0).length === 1) alone[s].push([b, e[s]])
        })
      }
      // A widened root's room is its gaps', shared equally among them.
      if (widened.has(r.name)) {
        ok(R.PROBLEM.some(s => r.by[s] > 0), `${at}: root ${r.name} widened with no gap in it`)
        ok(added.length > 0 && added.every(x => x > 0 && close(x, added[0])), `${at}: root ${r.name}'s room shared unevenly`)
      }
    }
    // Rule 1's floors stay within their budget of everything drawn.
    ok(floors / T1 <= R.FLOOR_BUDGET * (1 + 1e-9), `${at}: floors take ${(floors / T1 * 100).toFixed(1)}% of the map`)
    // A title that's one gap keeps its order: a bigger one is never drawn smaller.
    for (const ps of alone) {
      ps.sort((p, q) => p[0] - q[0])
      for (let i = 1; i < ps.length; i++) ok(ps[i][1] >= ps[i - 1][1] * (1 - 1e-9), `${at}: a bigger gap drawn smaller than a smaller one`)
    }
    // A root is widened only when it's nested, and only to ROOT_BOX_PX square.
    for (const r of A.widened) {
      ok(A.nested === r, `${at}: root ${r.name} widened but not nested`)
      ok(close(A.root.get(r) / total, R.ROOT_BOX_PX * R.ROOT_BOX_PX / (W * H)), `${at}: root ${r.name} widened past ROOT_BOX_PX`)
    }

    // Every figure stays bytes; nothing is drawn smaller than it is; a tile
    // with nothing to do is drawn at exactly its size, in either root.
    const L = R.layoutMap(m, W, H, 'titles')
    for (const t of L.titles) ok(close(t.v, sum(t.by)), `${at}: title ${t.name}'s figure isn't its bytes`)
    for (const l of L.leaves) {
      ok(close(l.bytes, l.T.by[l.s]), `${at}: a leaf's figure isn't its bytes`)
      ok(l.boost >= 1 - 1e-9, `${at}: a leaf drawn at ${l.boost}x its size`)
      if (!isGap(l.s)) ok(close(l.boost, 1), `${at}: ${l.T.name}'s ${R.STATES[l.s].name} drawn at ${l.boost}x`)
    }
    let count = 0
    for (const t of L.titles) for (const s of R.PROBLEM) if (t.eby[s] > t.by[s]) count++
    ok(L.raised === count, `${at}: raised says ${L.raised}, the layout raised ${count}`)

    // Rule 4: what it adds goes to gaps alone, on top of rules 1-3, and no
    // folder or title holding a gap is left a sliver, or dropped.
    const added = new Map()
    for (const [t, s, x] of L.thickened) {
      ok(isGap(s) && t.by[s] > 0 && x > 0, `${at}: rule 4 added to ${t.name}'s ${R.STATES[s].name}`)
      added.set(k(t, s), (added.get(k(t, s)) || 0) + x)
    }
    const before = new Map()
    for (const r of m.roots) for (const f of r.folders) for (const t of f.titles) before.set(`${t.root}\u0000${t.name}`, A.eby.get(t))
    let drawn = 0, thick = 0
    for (const t of L.titles) {
      const e0 = before.get(`${t.root}\u0000${t.name}`)
      t.by.forEach((b, s) => ok(close(t.eby[s], e0[s] + (added.get(k(t, s)) || 0)), `${at}: ${t.name}'s ${R.STATES[s].name} moved past rules 1-4`))
      drawn += sum(t.eby)
    }
    for (const x of added.values()) thick += x
    const holds = x => R.PROBLEM.some(s => x.by[s] > 0)
    ok(!L.tiny.some(x => x.titles && holds(x)), `${at}: a folder holding a gap was dropped`)
    for (const f of L.folders) if (holds(f)) ok(Math.min(f.w, f.h) >= R.STRIPE_PX * 0.999, `${at}: folder ${f.name}, holding a gap, is ${f.w.toFixed(1)}x${f.h.toFixed(1)}`)
    for (const t of L.titles) if (holds(t)) ok(Math.min(t.w, t.h) >= R.POINT_PX * Math.sqrt(A.cut) * 0.999, `${at}: ${t.name}, holding a gap, is ${t.w.toFixed(1)}x${t.h.toFixed(1)}`)
    // A few slivers' room, never a weighting.
    if (W > 1000) ok(thick <= 0.03 * drawn, `${at}: rule 4 took ${(thick / drawn * 100).toFixed(1)}% of the map`)
  }
}
ok(raised > 0, 'no gap was ever raised: the floor went unexercised')

// The bug this exists for: at the user's two screens every gap is drawn, big
// enough to see its pattern and point at, the torrent folder included, on the
// whole map and zoomed in, and beside a big pile of another workflow's (rule 4).
for (const [W, H] of [[1110, 611], [2230, 820]]) {
  for (const vname of ['whole', 'torrents', 'orphaned', 'piled', 'piled torrents']) {
    const m = views[vname], at = `${vname}, ${W}x${H}`
    const L = R.layoutMap(m, W, H, 'titles')
    // A pile past the budget shrinks every floor together (rule 3), the
    // stripes too; a sliver is still no sliver at that scale.
    const scale = Math.sqrt(R.mapSizes(m, W, H).cut)
    const gaps = new Set()
    for (const l of L.leaves) {
      if (!isGap(l.s)) continue
      gaps.add(`${l.T.root}\u0000${l.T.name}\u0000${l.s}`)
      ok(Math.min(l.w, l.h) >= R.STRIPE_PX * 0.75 * scale, `${at}: ${l.T.name}'s ${R.STATES[l.s].name} drawn ${l.w.toFixed(1)}x${l.h.toFixed(1)}`)
      // A gap you can see is a target of its own: a click on it opens its
      // workflow, never zooms into a folder too narrow for its heading.
      ok(R.headingAt(L, l.x + l.w / 2, l.y + l.h / 2) === null, `${at}: ${l.T.name}'s ${R.STATES[l.s].name} is taken by a heading`)
    }
    let want = 0
    for (const r of m.roots) for (const f of r.folders) for (const t of f.titles) want += R.PROBLEM.filter(s => t.by[s] > 0).length
    ok(gaps.size === want, `${at}: ${gaps.size} of ${want} gaps drawn`)
    // The pile's case by name: Triage's block beside it is drawn whole.
    let red = 0
    for (const r of m.roots) for (const f of r.folders) for (const t of f.titles) if (t.by[R.NOT_IMPORTED] > 0) red++
    ok(L.leaves.filter(l => l.s === R.NOT_IMPORTED).length === red, `${at}: not all ${red} not-imported releases drawn`)
    ok(red === (vname === 'orphaned' ? 0 : vname.startsWith('piled') ? 2 : 3), `${at}: ${red} not-imported releases in the case`)
  }
  const L = R.layoutMap(model, W, H, 'titles')
  ok(!L.tiny.some(t => t.name === 'Torrent folder'), `${W}x${H}: the torrent folder was dropped`)
  // And it costs the rest little: the torrent folder is a box among the
  // library's folders, not a column the map's height (the user: "the torrent
  // folder is too tall?") nor an eighth of the map as Attention drew it.
  const tor = L.roots.find(r => r.name === 'torrents')
  ok(tor && tor.nested, `${W}x${H}: the torrent folder isn't nested`)
  ok(tor && tor.h <= tor.w && tor.h < H / 3, `${W}x${H}: the torrent folder is ${tor ? Math.round(tor.w) + 'x' + Math.round(tor.h) : 'gone'}`)
  ok(tor && tor.w * tor.h <= 0.04 * W * H, `${W}x${H}: the torrent folder takes ${tor ? (tor.w * tor.h / W / H * 100).toFixed(1) : 0}% of the map`)
}

// Nothing too small: the map is exactly to scale, and at the user's screens a
// torrent folder that big is a column of its own, not nested.
for (const [W, H] of screens) {
  const A = R.mapSizes(big, W, H)
  ok(A.floored.length === 0, `big gaps, ${W}x${H}: ${A.floored.length} raised`)
  if (W > 1000) ok(A.nested === null, `big gaps, ${W}x${H}: the torrent folder was nested`)
  const L = R.layoutMap(big, W, H, 'titles')
  ok(L.raised === 0 && L.leaves.every(l => close(l.boost, 1)), `big gaps, ${W}x${H}: a tile not drawn at its size`)
}

// The Files view: one leaf per file, each at least its size and its figure its bytes.
const fm = [], ft = []
for (let i = 0; i < 60; i++) fm.push({ path: `movies/M${i}/m${i}.mkv`, size: (1 + i) * GB, linked_paths: i % 6 ? ['x'] : [] })
fm.push({ path: 'movies/M1/m1.en.srt', size: 60000 })
ft.push({ path: 'tv-sonarr/A/a.mkv', size: 2 * GB, imported: false, status: 'Seeding' })
ft.push({ path: 'tv-sonarr/A/a.nfo', size: 4000, imported: false, status: 'Seeding' })
ft.push({ path: 'radarr/B/b.mkv', size: 0.5 * GB, imported: false, status: 'Orphaned' })
const files = R.modelFromFiles(fm, ft)
for (const [W, H] of [[1110, 611], [400, 300]]) {
  const L = R.layoutMap(files, W, H, 'files')
  ok(L.leaves.length > 0, `files, ${W}x${H}: nothing drawn`)
  for (const l of L.leaves) {
    ok(l.boost >= 1 - 1e-9, `files, ${W}x${H}: ${l.file} drawn at ${l.boost}x its size`)
    ok(l.T.list.some(q => q.name === l.file && q.size === l.bytes), `files, ${W}x${H}: ${l.file}'s figure isn't its size`)
    if (!isGap(l.s)) ok(close(l.boost, 1), `files, ${W}x${H}: ${l.file} drawn at ${l.boost}x`)
  }
}

console.log(JSON.stringify({ problems: problems.slice(0, 20), count: problems.length, checked }))
"""


def test_only_a_gap_moves_and_only_up():
    node = shutil.which('node')
    if not node:
        if os.environ.get('CI'):
            pytest.fail('node is needed to run the library map rules and is on GitHub runners')
        pytest.skip('node is not installed')
    out = subprocess.run([node, '--input-type=module', '-e', SCRIPT, RULES.as_uri()],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result['count'] == 0, '\n'.join(result['problems'])
    assert result['checked'] > 10000, result
