"""The library map's attention rule, run under node (2026-11-10).

The Dashboard's library map draws what needs a workflow larger than its bytes,
as the health score weighs it (`libraryMapRules.js`, "Attention"). It exists
because by bytes alone a well-linked library's torrent folder was a fraction of
a percent of the map: on the box three not-imported episodes, 23 GB beside
18.8 TB, were a 1-3px strip the layout dropped, so Triage had rows the map
never drew. The user's rule is that **attention only ever makes a gap bigger**:
the score weights up and never below a linked byte, a raise never shrinks and
keeps parts in order, and every figure stays bytes.

The header's *Actual size* switch (2026-11-11) draws with null weights, which
must be the bytes and nothing else: no weight, no raise, no widened root.

The rules are React-free JavaScript, so this runs them under node on a made-up
library shaped like the box's and checks the rule over several scoring configs
and canvas sizes. It is the fourth test that reads frontend files, beside
test_type_scale.py, test_tint_idiom.py and test_module_names.py. Node is on
GitHub's runners; a machine without it skips, and CI without it fails.
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

// A library like the box's: 700 titles in three folders, most linked, one in
// nine with a not-seeded part, a few with copies; the torrent folder holds
// three small not-imported releases and an orphan.
let seed = 7
const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647
const GB = 1e9
const folders = [['library', 'movies'], ['library', 'television'], ['library', 'kids'],
                 ['torrents', 'tv-sonarr'], ['torrents', 'radarr']]
const titles = []
for (let i = 0; i < 700; i++) {
  const by = [0, 0, 0, 0, 0, 0], size = Math.exp(rnd() * 6) * GB / 4
  if (i % 9 === 0) { const u = size * (i % 2 ? 1 : 0.02 + rnd() * 0.5); by[1] = u; by[0] = size - u }
  else by[0] = size
  if (i % 97 === 0) by[4] = size * 0.3
  titles.push([i % 3, `Title ${i}`, 10, ...by])
}
titles.push([3, 'Show.S01E03.2160p-GRP.mkv', 1, 0, 0, 0, 11.3 * GB, 0, 0])
titles.push([3, 'Show.S01E07.2160p-GRP.mkv', 1, 0, 0, 0, 6.6 * GB, 0, 0])
titles.push([3, 'Show.S01E08.2160p-GRP', 1, 0, 0, 0, 5.5 * GB, 0, 0])
titles.push([4, 'Movie.2020.1080p-GRP', 3, 0, 0, 0.8 * GB, 0, 0, 0.01 * GB])
const model = R.modelFromRow({ version: 1, folders, titles, folded: [] })
const by = [0, 0, 0, 0, 0, 0]
for (const r of model.roots) r.by.forEach((b, s) => { by[s] += b })
const media = model.roots.find(r => r.name === 'library').v

// Scoring configs: the defaults, a category far past its limit (the cap would
// weight it below a linked byte), a category weighted 0, Hardlinked Media
// weighted 0, a zero limit, and a scan from before the score had limits.
const base = { total_media_size: media, total_torrents_size: media, hl_max: 70, or_max: 10, ni_max: 10, dup_max: 10,
               or_limit: media * 0.01, ni_limit: media * 0.01, dup_limit: media * 0.01,
               orphaned_torrent_size: by[2], not_imported_size: by[3], duplicate_size: by[4] }
const configs = {
  defaults: base,
  saturated: { ...base, not_imported_size: media * 3 },
  'weighted 0': { ...base, ni_max: 0 },
  'hardlinks weighted 0': { ...base, hl_max: 0 },
  'zero limit': { ...base, ni_limit: 0 },
  'no limits': { total_media_size: media },
  none: null,
}
const W8 = {}
for (const [name, d] of Object.entries(configs)) {
  const w = R.attentionWeights(d)
  W8[name] = w
  ok(w.length === 6 && w.every(x => Number.isFinite(x) && x >= 1), `${name}: a weight below 1x: ${w}`)
  ok(w[R.LINKED] === 1 && w[R.UNSEEDED] === 1 && w[R.OTHER] === 1, `${name}: linked, not seeded or other not at 1x: ${w}`)
}
ok(W8.defaults[R.NOT_IMPORTED] > 5, `defaults: the score should weigh not imported well above a library byte: ${W8.defaults}`)

const views = { whole: model, torrents: R.zoomModel(model, { root: 'torrents' }) }
for (const [cname, w] of Object.entries(W8)) {
  for (const [vname, m] of Object.entries(views)) {
    for (const [W, H] of [[1110, 611], [2230, 820], [400, 300]]) {
      const at = `${cname}, ${vname}, ${W}x${H}`
      const A = R.attentionSizes(m, W, H, w)
      const parts = [[], [], [], [], [], []]
      for (const r of m.roots) for (const f of r.folders) for (const t of f.titles) {
        const e = A.eby.get(t)
        t.by.forEach((b, s) => {
          ok(e[s] >= b * w[s] * (1 - 1e-12), `${at}: ${t.name} state ${s} drawn below its weight`)
          if (!R.PROBLEM.includes(s)) ok(close(e[s], b * w[s]), `${at}: ${t.name} state ${s} raised though it needs nothing`)
          if (b > 0) { parts[s].push([b, e[s]]); if (e[s] > b * w[s] * 1.0001) raised++ }
        })
      }
      // A raise keeps a state's parts in order of bytes, strictly: raised all
      // to one size, a 300 MB gap and a 2 GB one were the same tile.
      for (const ps of parts) {
        ps.sort((p, q) => p[0] - q[0])
        for (let i = 1; i < ps.length; i++) {
          if (ps[i][0] > ps[i - 1][0] * (1 + 1e-6)) ok(ps[i][1] > ps[i - 1][1], `${at}: a raise drew a bigger part no bigger than a smaller one`)
        }
      }
      for (const r of m.roots) ok(A.root.get(r) >= A.inner.get(r) * (1 - 1e-12), `${at}: root ${r.name} drawn smaller than its contents`)

      // Every figure stays bytes, and nothing is drawn smaller than it is.
      const L = R.layoutMap(m, W, H, 'titles', w)
      for (const t of L.titles) ok(close(t.v, t.by.reduce((a, b) => a + b, 0)), `${at}: title ${t.name}'s figure isn't its bytes`)
      for (const l of L.leaves) {
        ok(close(l.bytes, l.T.by[l.s]), `${at}: a leaf's figure isn't its bytes`)
        ok(l.boost >= 1 - 1e-9, `${at}: a leaf drawn at ${l.boost}x its size`)
        // The hover strip reads it to say whether the score enlarged a tile.
        ok(l.weight === w[l.s], `${at}: a leaf's weight is ${l.weight}, not its state's ${w[l.s]}`)
      }
    }
  }
}
ok(raised > 0, 'no part was ever raised: rule 2 went unexercised')

// Actual size (null weights) is the bytes and nothing else: no state weighted,
// no part raised, no root widened, and every leaf drawn at 1x.
for (const [vname, m] of Object.entries(views)) {
  for (const [W, H] of [[1110, 611], [2230, 820], [400, 300]]) {
    const at = `actual size, ${vname}, ${W}x${H}`
    const A = R.attentionSizes(m, W, H, null)
    for (const r of m.roots) {
      ok(close(A.root.get(r), r.v), `${at}: root ${r.name} drawn at ${A.root.get(r)}, not its ${r.v} bytes`)
      for (const f of r.folders) for (const t of f.titles) {
        const e = A.eby.get(t)
        t.by.forEach((b, s) => ok(e[s] === b, `${at}: ${t.name} state ${s} drawn at ${e[s]}, not its ${b} bytes`))
      }
    }
    const L = R.layoutMap(m, W, H, 'titles', null)
    ok(L.leaves.length > 0, `${at}: nothing drawn`)
    for (const l of L.leaves) {
      ok(close(l.boost, 1) && l.weight === 1, `${at}: a leaf drawn at ${l.boost}x its size`)
      ok(close(l.bytes, l.T.by[l.s]), `${at}: a leaf's figure isn't its bytes`)
    }
  }
}

// The bug this exists for: at the user's two screens the not-imported
// releases are drawn, each big enough to see, whatever the config.
for (const [cname, w] of Object.entries(W8)) {
  for (const [W, H] of [[1110, 611], [2230, 820]]) {
    const L = R.layoutMap(model, W, H, 'titles', w)
    ok(!L.tiny.some(t => t.name === 'Torrent folder'), `${cname}, ${W}x${H}: the torrent folder was dropped`)
    const red = L.leaves.filter(l => l.s === R.NOT_IMPORTED)
    ok(red.length === 3, `${cname}, ${W}x${H}: ${red.length} of 3 not-imported releases drawn`)
    for (const l of red) ok(l.w * l.h >= 400, `${cname}, ${W}x${H}: a not-imported release drawn at ${Math.round(l.w * l.h)}px²`)
    // And every gap of any workflow is big enough to see and point at.
    for (const l of L.leaves) {
      if (R.PROBLEM.includes(l.s)) ok(l.w * l.h >= 200, `${cname}, ${W}x${H}: ${l.T.name}'s ${R.STATES[l.s].name} drawn at ${Math.round(l.w * l.h)}px²`)
    }
  }
}

// The Files view: one leaf per file, each at least its size and its figure its bytes.
const fm = [], ft = []
for (let i = 0; i < 60; i++) fm.push({ path: `movies/M${i}/m${i}.mkv`, size: (1 + i) * GB, linked_paths: i % 6 ? ['x'] : [] })
ft.push({ path: 'tv-sonarr/A/a.mkv', size: 2 * GB, imported: false, status: 'Seeding' })
ft.push({ path: 'tv-sonarr/A/a.nfo', size: 4000, imported: false, status: 'Seeding' })
ft.push({ path: 'radarr/B/b.mkv', size: 0.5 * GB, imported: false, status: 'Orphaned' })
const files = R.modelFromFiles(fm, ft)
for (const [W, H] of [[1110, 611], [400, 300]]) {
  const L = R.layoutMap(files, W, H, 'files', W8.defaults)
  ok(L.leaves.length > 0, `files, ${W}x${H}: nothing drawn`)
  for (const l of L.leaves) {
    ok(l.boost >= 1 - 1e-9, `files, ${W}x${H}: ${l.file} drawn at ${l.boost}x its size`)
    ok(l.T.list.some(q => q.name === l.file && q.size === l.bytes), `files, ${W}x${H}: ${l.file}'s figure isn't its size`)
  }
  for (const l of R.layoutMap(files, W, H, 'files', null).leaves) {
    ok(close(l.boost, 1), `files at actual size, ${W}x${H}: ${l.file} drawn at ${l.boost}x its size`)
  }
}

console.log(JSON.stringify({ problems: problems.slice(0, 20), count: problems.length, checked }))
"""


def test_attention_only_ever_makes_a_gap_bigger():
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
