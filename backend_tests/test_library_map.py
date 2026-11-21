"""The dashboard's disk-and-links cards (2026-11-09), and what they read.

* The library map's row (`audit.library_map_row`): every title's bytes by the
  workflow that acts on them, linked bytes counted once, in the library.
* `shared_size`: the bytes both trees hold, which `hardlinked_media_size`
  over-counts when a linked file is excluded on the torrent side only.
* New in the library: the change-log diff counts how each new library file
  arrived, and the upload stats sum them per day.
* Last scan: `/api/results` carries the last scan's report, counts only.
* BACKFILL B15: the map opens Backfill on one title, by folder path.
* CLEANUP C28: qui's preference view is a list of fields, which read as
  "couldn't ask" and switched empty-folder detection off without a word.
"""

import json
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app
import audit
import db
from sources import _qui

GB = 1024 ** 3


def _media(path, size=GB, linked=False, dups=(), excluded=False):
    return {'path': path, 'size': size, 'linked_paths': ['/t/x'] if linked else [],
            'duplicate_paths': list(dups), 'excluded': excluded,
            'status': 'Seeding' if linked else 'Orphaned', 'imported': linked}


def _torrent(path, size=GB, status='Seeding', imported=False, linked=False, **extra):
    return {'path': path, 'size': size, 'status': status, 'imported': imported,
            'linked_paths': ['/m/x'] if linked else [], 'duplicate_paths': [], 'excluded': False,
            **extra}


def _titles(row):
    """`{(root, folder, title): [files, b0 … b7]}` from a stored row (a dead
    seed's Triage search, when a title carries one, is left off)."""
    folders = row['folders']
    return {(folders[t[0]][0], folders[t[0]][1], t[1]): t[2:11] for t in row['titles']}


class LibraryMapRowTests(unittest.TestCase):
    def test_each_state_is_the_workflow_that_acts_on_it(self):
        row = audit.library_map_row(
            [_media('television/Show/e1.mkv', linked=True),
             _media('television/Show/e2.mkv'),
             _media('movies/Film (2020)/f.mkv', dups=['/data/torrents/f.mkv'])],
            [_torrent('radarr/Gone/g.mkv', status='Orphaned'),
             _torrent('radarr/Waiting/w.mkv'),
             _torrent('radarr/Waiting/w.nfo', sidecar=True),
             _torrent('radarr/Busy/b.mkv', incomplete=True)])
        t = _titles(row)
        self.assertEqual(t[('library', 'television', 'Show')], [2, GB, GB, 0, 0, 0, 0, 0, 0])
        self.assertEqual(t[('library', 'movies', 'Film (2020)')], [1, 0, 0, 0, 0, GB, 0, 0, 0])
        self.assertEqual(t[('torrents', 'radarr', 'Gone')], [1, 0, 0, GB, 0, 0, 0, 0, 0])
        # A sidecar of an imported torrent isn't "not imported" (T19), and a
        # download in progress is nobody's yet (T4): both Other.
        self.assertEqual(t[('torrents', 'radarr', 'Waiting')], [2, 0, 0, 0, GB, 0, GB, 0, 0])
        self.assertEqual(t[('torrents', 'radarr', 'Busy')], [1, 0, 0, 0, 0, 0, GB, 0, 0])

    def test_linked_bytes_count_once_in_the_library(self):
        """The torrent-tree side of a linked file is the same inode the library
        side already counted; drawing it twice is the double count the map exists
        to show isn't on disk."""
        row = audit.library_map_row([_media('tv/Show/e1.mkv', linked=True)],
                                    [_torrent('sonarr/Show.S01/e1.mkv', imported=True, linked=True)])
        self.assertEqual(list(_titles(row)), [('library', 'tv', 'Show')])

    def test_a_dead_seed_is_drawn_on_its_library_file_with_its_torrent_to_search(self):
        """The box's Jackie Brown: a library file linked to a torrent Arvel has
        deleted. Its bytes are the library file's, so the dead seed is a library
        state, joined by inode; the title carries the dead torrent's own folder,
        which is what Triage's search matches."""
        lib = dict(_media('movies/Jackie Brown (1997)/Jackie.Brown.mkv', size=21 * GB, linked=True), file_id='1:7')
        tor = _torrent('radarr/Jackie.Brown.1997.1080p.BluRay-SA89/Jackie.Brown.mkv', size=21 * GB,
                       imported=True, linked=True, file_id='1:7', tracker_health='unregistered',
                       tracker_msg='Torrent has been deleted.')
        row = audit.library_map_row([lib], [tor])
        self.assertEqual(row['version'], 3)
        [entry] = row['titles']
        self.assertEqual(entry[2:11], [1, 0, 0, 0, 0, 0, 0, 21 * GB, 0])
        self.assertEqual(entry[11], 'radarr/Jackie.Brown.1997.1080p.BluRay-SA89')
        # Its torrent side is still the same inode, drawn once, in the library.
        self.assertEqual(list(_titles(row)), [('library', 'movies', 'Jackie Brown (1997)')])

    def test_only_a_dead_seed_is_one(self):
        """A working torrent, an excluded dead one and a dead orphan aren't
        dead seeds (`_is_dead_seed_torrent`), and a title with none carries no
        search."""
        media = [dict(_media(f'movies/F{i}/f.mkv', linked=True), file_id=f'1:{i}') for i in range(3)]
        torrents = [
            _torrent('radarr/F0/f.mkv', imported=True, linked=True, file_id='1:0', tracker_health='working'),
            _torrent('radarr/F1/f.mkv', imported=True, linked=True, file_id='1:1', tracker_health='unregistered',
                     excluded=True),
            _torrent('radarr/F2/f.mkv', imported=True, linked=True, file_id='1:2', tracker_health='unregistered',
                     status='Orphaned'),
        ]
        row = audit.library_map_row(media, torrents)
        for entry in row['titles']:
            self.assertEqual(len(entry), 11, entry)
            self.assertEqual(entry[2:11], [1, GB, 0, 0, 0, 0, 0, 0, 0])

    def test_excluded_files_are_left_out(self):
        row = audit.library_map_row([_media('tv/Show/e1.nfo', excluded=True)], [])
        self.assertEqual(row['titles'], [])

    def test_a_library_sidecar_is_other_never_not_seeded(self):
        """2026-11-20. A Bazarr subtitle beside a linked episode was Not seeded,
        a gap the sizing rules raised to a stripe on every title that had one,
        and its click opened Backfill on nothing. It's the Unmanaged stamp's own
        test: a video by its extension, anything else Other unless the stamp
        (by folder) says no arr is there."""
        row = audit.library_map_row([
            _media('tv/Show/e1.mkv', linked=True),
            _media('tv/Show/e1.en.srt', size=50_000),
            _media('tv/Show/tvshow.nfo', size=4_000),
            _media('tv/Show/.plexmatch', size=100),
            _media('tv/Show/e2.MKV', size=2 * GB),                     # a video, any case
            dict(_media('home/Clips/clip.srt', size=7_000), unmanaged=True),
        ], [])
        t = _titles(row)
        self.assertEqual(t[('library', 'tv', 'Show')], [5, GB, 2 * GB, 0, 0, 0, 54_100, 0, 0])
        self.assertEqual(t[('library', 'home', 'Clips')], [1, 0, 0, 0, 0, 0, 0, 0, 7_000])
        for f in [_media('a/b.srt'), _media('a/b.mkv'), _media('a/.mkv'), dict(_media('a/c.nfo'), unmanaged=True)]:
            self.assertEqual(audit._map_state_media(f),
                             {'a/b.srt': audit.MAP_OTHER, 'a/b.mkv': audit.MAP_UNSEEDED,
                              'a/.mkv': audit.MAP_OTHER, 'a/c.nfo': audit.MAP_UNMANAGED}[f['path']])

    def test_the_row_is_the_per_file_rule_in_any_order(self):
        """`library_map_row` tries the last title's key first (2026-11-20), which
        is faster in walk order and must give the same row in any order: here,
        each file's state by `_map_state_media` / `_map_state_torrent`, summed
        by `_map_key`, against the row over the walk order and shuffled."""
        import random
        rnd = random.Random(3)
        media, torrents = [], []
        for t in range(60):
            folder = rnd.choice(['tv', 'movies', 'kids'])
            for e in range(rnd.randrange(1, 6)):
                ext = rnd.choice(['mkv', 'MKV', 'srt', 'nfo', 'jpg', 'm2ts'])
                f = _media(f'{folder}/T{t}/Season 1/e{e}.{ext}', size=rnd.randrange(1, 10 ** 6),
                           linked=rnd.random() < 0.6, excluded=rnd.random() < 0.05,
                           dups=['/data/torrents/c'] if rnd.random() < 0.05 else ())
                if rnd.random() < 0.1:
                    f['unmanaged'] = True
                media.append(f)
            media.append(_media(f'{folder}/T{t}.mkv'))                     # a file at the title's depth
            torrents.append(_torrent(f'{folder}/Rel{t}/{t}.mkv', status=rnd.choice(['Seeding', 'Orphaned']),
                                     linked=rnd.random() < 0.5))
        media.append(_media('loose.mkv'))
        media.append(_media('tv\\Win\\e1.srt'))

        def reference():
            want = {}
            for root, files, state in (('library', media, audit._map_state_media),
                                       ('torrents', torrents, audit._map_state_torrent)):
                for f in files:
                    if f.get('excluded') or (root == 'torrents' and f.get('linked_paths')):
                        continue
                    key = audit._map_key(f['path'])
                    row = want.setdefault((root, *audit._map_title(key)), [0] * 9)
                    row[0] += 1
                    row[1 + state(f)] += f['size']
            return want

        want = reference()
        self.assertEqual(_titles(audit.library_map_row(media, torrents)), want)
        for _ in range(5):
            rnd.shuffle(media)
            rnd.shuffle(torrents)
            self.assertEqual(_titles(audit.library_map_row(media, torrents)), want)

    def test_disc_rips_are_counted_once_a_disc_off_unlinked_files(self):
        """For the exclusion suggestions: one per disc folder (BDMV/BACKUP holds
        a second index.bdmv, which isn't a second disc), off files that aren't
        linked or excluded, in either tree."""
        discs = {}
        audit.library_map_row(
            [_media('movies/Rip (2001)/BDMV/index.bdmv', size=200),
             _media('movies/Rip (2001)/BDMV/BACKUP/index.bdmv', size=200),
             _media('movies/Rip (2001)/BDMV/MovieObject.bdmv', size=200),
             _media('movies/Kept (2002)/BDMV/index.bdmv', size=200, linked=True),
             _media('movies/Hidden (2003)/BDMV/index.bdmv', size=200, excluded=True)],
            [_torrent('radarr/Old.DVD/VIDEO_TS/VIDEO_TS.IFO', size=10_000),
             _torrent('radarr/Old.DVD/VIDEO_TS/VTS_01_0.IFO', size=10_000),
             _torrent('radarr/Two.Disc/D1/BDMV/INDEX.BDMV', size=200),
             _torrent('radarr/Loose/VIDEO_TS.IFO', size=10_000)],          # not in a VIDEO_TS folder
            discs=discs)
        self.assertEqual(discs, {'bluray': {'library': 1, 'torrents': 1}, 'dvd': {'library': 0, 'torrents': 1}})

    def test_a_file_directly_in_a_folder_is_its_own_title(self):
        row = audit.library_map_row([_media('movies/Film.2020.mkv'), _media('loose.mkv')], [])
        self.assertEqual(set(_titles(row)), {('library', 'movies', 'Film.2020.mkv'),
                                             ('library', '', 'loose.mkv')})

    def test_titles_past_the_cap_fold_into_one_per_folder(self):
        media = [_media(f'movies/T{i}/f.mkv', size=(10 - i) * GB) for i in range(5)]
        row = audit.library_map_row(media, [], titles_max=2)
        self.assertEqual([t[1] for t in row['titles']], ['T0', 'T1'])     # largest kept
        [[fi, n_titles, n_files, *by]] = row['folded']
        self.assertEqual((row['folders'][fi], n_titles, n_files), (['library', 'movies'], 3, 3))
        self.assertEqual(by, [0, (8 + 7 + 6) * GB, 0, 0, 0, 0, 0, 0])

    def test_the_row_is_stored_and_served_as_written(self):
        stored = audit.library_map_row([_media('tv/Show/e1.mkv')], [])
        db.db_save_file_results('library_map', stored)
        client = app.app.test_client()
        with patch('app._is_local_client', return_value=True):
            res = client.get('/api/library_map')
        self.assertEqual(json.loads(res.data), stored)


class StateRuleParityTests(unittest.TestCase):
    """The map's state rule exists twice: `audit.library_map_row` for the stored
    row (Titles) and `libraryMapRules.js`' `modelFromFiles` for the Files view.
    This runs the same records through both, under node, and holds them to one
    answer per title, the dead seed (2026-11-11) included: its state, the
    torrent folder a click searches Triage for, and Triage as where it goes.
    A machine without node skips; CI without it fails, as the sizing guard does."""
    RULES = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'frontend', 'src', 'components', 'libraryMapRules.js')
    SCRIPT = r"""
const R = await import(process.argv[1])
let s = ''
for await (const c of process.stdin) s += c
const { media, torrents, row } = JSON.parse(s)
const dump = m => {
  const out = {}
  for (const r of m.roots) for (const f of r.folders) for (const t of f.titles) {
    const k = `${t.root}|${t.root === 'library' ? t.rel : t.name}|${f.state ?? ''}`
    out[k] = { by: t.by, search: t.search || null, nav: R.navFor(R.worstState(t.by), t), unmanaged_nav: R.navFor(R.UNMANAGED, t) }
  }
  return out
}
const fm = R.modelFromFiles(media, torrents), rm = R.modelFromRow(row)
console.log(JSON.stringify({ files: dump(fm), row: dump(rm), totals: R.modelTotals(rm),
  videos: [...R.VIDEO_EXTENSIONS].sort(),
  isVideo: ['a/b.MKV', 'a/.mkv', 'a/b.srt', 'a/b', 'a.b/c', 'a\\b.ts'].map(R.isVideo),
  legend: R.LEGEND.map(x => R.STATES[x].name), v1: dump(R.modelFromRow({ version: 1, folders: [['library', 'm']],
  titles: [[0, 'Old', 1, 5, 0, 0, 0, 0, 0]], folded: [] })),
  v2: dump(R.modelFromRow({ version: 2, folders: [['library', 'm']],
  titles: [[0, 'Old', 1, 5, 0, 0, 0, 0, 0, 3, 'radarr/Old-GRP']], folded: [] })) }))
"""

    def test_both_languages_draw_a_dead_seed_the_same(self):
        import shutil, subprocess
        from pathlib import Path
        node = shutil.which('node')
        if not node:
            if os.environ.get('CI'):
                self.fail('node is needed to run the library map rules and is on GitHub runners')
            self.skipTest('node is not installed')
        media = [dict(_media('movies/Jackie Brown (1997)/Jackie.Brown.mkv', size=21 * GB, linked=True), file_id='1:7'),
                 dict(_media('movies/Up (2009)/up.mkv', size=3 * GB, linked=True), file_id='1:8'),
                 dict(_media('movies/Up (2009)/up.en.srt', size=1000), file_id='1:9'),
                 dict(_media('tv/Show/e1.mkv', size=2 * GB, linked=True), file_id='1:10'),
                 dict(_media('kids_movies/The.Gruffalos.Child.720p.HDTV.x264-BiA/g.mkv', size=682 * 10 ** 6),
                      file_id='1:11', unmanaged=True),
                 # An unmanaged folder's subtitle stays Unmanaged (the stamp
                 # judges a sidecar by its folder), and a folder of artwork
                 # alone is Other, linking nothing.
                 dict(_media('kids_movies/The.Gruffalos.Child.720p.HDTV.x264-BiA/g.srt', size=2000),
                      file_id='1:12', unmanaged=True),
                 dict(_media('tv/Coming Soon/poster.jpg', size=300_000), file_id='1:13'),
                 dict(_media('tv/Coming Soon/.plexmatch', size=100), file_id='1:14')]
        torrents = [
            _torrent('radarr/Jackie.Brown.1997.1080p-SA89/Jackie.Brown.mkv', size=21 * GB, imported=True, linked=True,
                     file_id='1:7', tracker_health='unregistered', tracker_msg='Torrent has been deleted.'),
            _torrent('radarr/Up.2009.1080p-GRP/up.mkv', size=3 * GB, imported=True, linked=True,
                     file_id='1:8', tracker_health='working'),
            _torrent('sonarr/Show.S01-GRP/e1.mkv', size=2 * GB, imported=True, linked=True,
                     file_id='1:10', tracker_health='unregistered', excluded=True),
            _torrent('radarr/Waiting/w.mkv', size=GB)]
        row = audit.library_map_row(media, torrents)
        out = subprocess.run([node, '--input-type=module', '-e', self.SCRIPT, Path(self.RULES).as_uri()],
                             input=json.dumps({'media': media, 'torrents': torrents, 'row': row}),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got['files'], got['row'])
        jackie = got['row']['library|movies/Jackie Brown (1997)|']
        self.assertEqual(jackie['by'], [0, 0, 0, 0, 0, 0, 21 * GB, 0])
        self.assertEqual(jackie['search'], 'radarr/Jackie.Brown.1997.1080p-SA89')
        self.assertEqual(jackie['nav'], {'tab': 'triage', 'search': 'radarr/Jackie.Brown.1997.1080p-SA89'})
        # An excluded dead torrent isn't a dead seed: its library file is Linked.
        self.assertEqual(got['row']['library|tv/Show|']['by'][0], 2 * GB)
        # A dead seed is still linked: Jackie Brown counts as fully linked. So
        # is Up: its subtitle is Other (2026-11-20), which has no say.
        self.assertEqual((got['totals']['full'], got['totals']['partly']), (3, 0))
        up = got['row']['library|movies/Up (2009)|']
        self.assertEqual(up['by'], [3 * GB, 0, 0, 0, 0, 1000, 0, 0])
        self.assertEqual(up['nav'], None)
        self.assertEqual(got['row']['library|tv/Coming Soon|']['by'], [0, 0, 0, 0, 0, 300_100, 0, 0])
        # The two languages' video lists and tests are one.
        self.assertEqual(got['videos'], sorted(audit.VIDEO_EXTENSIONS))
        self.assertEqual(got['isVideo'], [audit._map_video(p) for p in
                                          ['a/b.MKV', 'a/.mkv', 'a/b.srt', 'a/b', 'a.b/c', 'a\\b.ts']])
        self.assertEqual(got['isVideo'], [True, False, False, False, False, True])
        self.assertEqual(got['legend'], ['Linked', 'Not seeded', 'Orphaned', 'Not imported', 'Dead seed', 'Copies',
                                         'Unmanaged'])
        # A version 1 row still reads, its six sums in place and the rest 0.
        self.assertEqual(got['v1']['library|m/Old|']['by'], [5, 0, 0, 0, 0, 0, 0, 0])
        # An unmanaged file: its own state, and a click opens Media on its folder.
        stray = got['row']['library|kids_movies/The.Gruffalos.Child.720p.HDTV.x264-BiA|']
        self.assertEqual(stray['by'], [0, 0, 0, 0, 0, 0, 0, 682 * 10 ** 6 + 2000])
        self.assertEqual(stray['unmanaged_nav'], {'tab': 'media', 'search': 'kids_movies/The.Gruffalos.Child.720p.HDTV.x264-BiA'})
        # It isn't linked, so its title isn't fully linked, and neither is the
        # artwork folder; neither is a workflow's gap, so the titles with one
        # are Jackie Brown and the waiting torrent. Up's subtitle was the third
        # until 2026-11-20, a 1,000-byte gap raised to a visible stripe.
        self.assertEqual(got['totals']['none'], 2)
        self.assertEqual(got['totals']['problemTitles'], 2)
        self.assertEqual(got['totals']['problem'], 21 * GB + GB)
        # A version 2 row still reads, its seven sums in place and Unmanaged 0.
        self.assertEqual(got['v2']['library|m/Old|']['by'], [5, 0, 0, 0, 0, 0, 3, 0])


class BiggestGapsTests(unittest.TestCase):
    """The Biggest gaps card: every workflow's biggest is on it, which by size
    alone a library's Backfill titles crowded out (2026-11-10), and it reads
    by size, which leading with each workflow's biggest didn't (2026-11-12:
    132.9, 48.4, 46.4, 9.3, 7.2, then 108.8 GB)."""
    SCRIPT = r"""
const R = await import(process.argv[1])
const GB = 1e9
const by = (s, b) => { const a = [0, 0, 0, 0, 0, 0, 0]; a[s] = b; return a }
const titles = [[0, 'Big show', 10, ...by(1, 133 * GB)], [0, 'Pottery', 10, ...by(1, 109 * GB)],
  [0, 'Another', 10, ...by(1, 100 * GB)], [0, 'Oldboy', 1, ...by(6, 48 * GB)], [1, 'GoodFellas', 1, ...by(4, 46 * GB)],
  [2, 'The.Conversation', 3, ...by(2, 9 * GB)], [2, 'Slow.Horses.S04E01', 1, ...by(3, 7 * GB)]]
const m = R.modelFromRow({ version: 2, folders: [['library', 'television'], ['library', 'movies'], ['torrents', 'radarr']], titles, folded: [] })
console.log(JSON.stringify(R.biggestGaps(m, 6).map(g => [R.shortName(g.t), g.state, g.bytes])))
"""

    def test_every_workflow_is_on_it_and_it_reads_by_size(self):
        import shutil, subprocess
        from pathlib import Path
        node = shutil.which('node')
        if not node:
            if os.environ.get('CI'):
                self.fail('node is needed to run the library map rules and is on GitHub runners')
            self.skipTest('node is not installed')
        out = subprocess.run([node, '--input-type=module', '-e', self.SCRIPT, Path(StateRuleParityTests.RULES).as_uri()],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        sizes = [b for _, _, b in got]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        # Every state with a gap is on it (not seeded, orphaned, not imported,
        # dead seed, copies); the third Backfill title isn't, though it's bigger
        # than four of them.
        self.assertEqual({s for _, s, _ in got}, {1, 2, 3, 4, 6})
        self.assertEqual([n for n, _, _ in got], ['Big show', 'Pottery', 'Oldboy', 'GoodFellas', 'The.Conversation', 'Slow.Horses.S04E01'])


class SharedSizeTests(unittest.TestCase):
    def test_a_library_file_whose_torrent_twin_is_excluded_is_not_held_twice(self):
        """The box: one disc rip linked into the library and excluded on the
        torrent side made `hardlinked_media_size` 23 GB larger than the torrent
        folder, so "on disk" would have come out smaller than the library."""
        media = [_media('m/A/a.mkv', size=10 * GB, linked=True), _media('m/B/b.m2ts', size=3 * GB, linked=True)]
        torrents = [_torrent('t/a.mkv', size=10 * GB, imported=True, linked=True),
                    dict(_torrent('t/b.m2ts', size=3 * GB, imported=True, linked=True), excluded=True)]
        stats = audit.process_health_metrics(media, torrents, {}, update_history=False)
        det = stats['current']['details']
        self.assertEqual(det['hardlinked_media_size'], 13 * GB)
        self.assertEqual(det['shared_size'], 10 * GB)

    def test_disk_space_is_carried_through_a_recompute(self):
        """A config save rebuilds the dashboard from the lists, which can't measure
        free space; it carries the scan's figure forward like the other scan-only
        details."""
        self.assertIn('disk_space', audit.SCAN_ONLY_DETAILS)


class NewMediaTests(unittest.TestCase):
    ROOT = '/data/torrents'

    def _count(self, files, prev=None):
        counts = {'linked': 0, 'copies': 0, 'unseeded': 0}
        audit.compute_diff_from_signatures(
            {'media': prev if prev is not None else {'old/x.mkv': 0}, 'torrents': {}},
            {'media_files': files, 'torrent_files': [], 'dashboard': {}},
            new_media=counts, torrent_root=self.ROOT)
        return counts

    def test_each_new_library_file_is_classed_by_how_it_arrived(self):
        counts = self._count([
            _media('tv/Show/e1.mkv', linked=True),
            _media('tv/Show/e2.mkv', dups=['/data/torrents/sonarr/Show/e2.mkv']),
            _media('tv/Show/e3.mkv', dups=['/data/media/tv/Show/e3 copy.mkv']),
            _media('tv/Show/e4.mkv'),
            _media('old/x.mkv'),                         # not new
            _media('tv/Show/e5.nfo', excluded=True),     # excluded
        ])
        # A copy of another *library* file isn't a failed link; it's unseeded.
        self.assertEqual(counts, {'linked': 1, 'copies': 1, 'unseeded': 2})

    def test_a_sidecar_counts_only_when_it_was_linked_or_copied(self):
        """2026-11-20, the map's rule: Bazarr writes a subtitle beside every new
        episode, so a healthy library read as half its new files not seeded. A
        sidecar an arr hardlinked (Import Extra Files) still says the link was
        made, and a copy that it wasn't."""
        counts = self._count([
            _media('tv/Show/e1.mkv', linked=True),
            _media('tv/Show/e1.en.srt'),
            _media('tv/Show/e1.nfo', linked=True),
            _media('tv/Show/e2.nfo', dups=['/data/torrents/sonarr/Show/e2.nfo']),
        ])
        self.assertEqual(counts, {'linked': 2, 'copies': 1, 'unseeded': 0})

    def test_counted_even_when_nothing_else_changed(self):
        """The diff returns None when nothing changed; the counters are an
        out-parameter, so a quiet scan still says "0 new" rather than nothing."""
        counts = {'linked': 0, 'copies': 0, 'unseeded': 0}
        out = audit.compute_diff_from_signatures(
            {'media': {'tv/a.mkv': audit.SIG_IMPORTED}, 'torrents': {}},
            {'media_files': [_media('tv/a.mkv', linked=True)], 'torrent_files': [], 'dashboard': {}},
            new_media=counts, torrent_root=self.ROOT)
        self.assertIsNone(out)
        self.assertEqual(counts, {'linked': 0, 'copies': 0, 'unseeded': 0})

    def test_a_first_scan_of_the_library_counts_nothing(self):
        """With no previous signatures every path is new and none of them arrived."""
        self.assertEqual(self._count([_media('tv/a.mkv')], prev={}),
                         {'linked': 0, 'copies': 0, 'unseeded': 0})

    def test_a_day_sums_its_scans_and_the_library_stats_keep_the_last(self):
        def row(at, nm=None, lib=None):
            snap = {'_instance_count': 1, 'a.cc': {'uploaded': 0, 'seeding_size': 1}}
            library = dict(lib or {'total_media_size': 1})
            if nm is not None:
                library['new_media'] = nm
            snap['_library'] = library
            return {'taken_at': at, 'snapshot': snap, 'source': 'qui'}
        rows = [row('2026-11-08T09:00:00'),                                    # before counting
                row('2026-11-09T09:00:00', {'linked': 3, 'copies': 0, 'unseeded': 1}),
                row('2026-11-09T15:00:00', {'linked': 2, 'copies': 1, 'unseeded': 0},
                    lib={'total_media_size': 2})]
        with patch.object(audit, 'db_get_upload_snapshots', return_value=rows):
            stats = audit.compute_upload_stats(30)
        self.assertEqual(stats['daily_new_media'],
                         [{'date': '2026-11-09', 'linked': 5, 'copies': 1, 'unseeded': 1}])
        self.assertEqual(stats['daily_library_stats'][-1], {'date': '2026-11-09', 'total_media_size': 2})


class ScanReportTests(unittest.TestCase):
    REPORT = {
        'source': 'qui', 'torrent_count': 1444, 'file_map_size': 3583, 'instances_ok': 1,
        'instances_total': 2, 'listing_failures': 0, 'listing_recovered': 3, 'listing_unresolved': 1,
        'tracker_failures': 0, 'partial': True,
        'instances_failed': [{'instance': 'nas', 'reason': "Could not reach qui at 'http://10.0.0.5:7476'"}],
        'notes': ['listing failed for abcdef0123456789abcdef0123 at /data/torrents/secret'],
        'filesystem': {'torrents': {'configured': True, 'exists': True, 'files': 3583, 'stat_errors': 0,
                                    'unlistable': 0, 'unlistable_shallow': 0}},
    }

    def _get(self, runs, meta):
        client = app.app.test_client()
        with patch('app._is_local_client', return_value=True), \
             patch('app.db_load_results', return_value={'dashboard': None}), \
             patch('app.db_get_recent_runs', return_value=runs), \
             patch('app.db_count_runs_since', return_value={'ok': 250, 'anomaly': 4}), \
             patch('app.db_get_meta', side_effect=lambda k, d=None: meta.get(k, d)):
            return client.get('/api/results').get_json()['scan_report']

    def test_counts_only_never_a_note_or_a_reason(self):
        rep = self._get([{'ran_at': '2026-11-09T19:31:42', 'trigger': 'watchdog', 'status': 'ok',
                          'duration_seconds': 2.9, 'peak_rss_mb': 106, 'error_message': None}],
                        {'last_source_report': self.REPORT})
        self.assertEqual(rep['report']['instances_failed'], 1)
        self.assertEqual(rep['report']['listing_unresolved'], 1)
        self.assertEqual(rep['report']['filesystem']['torrents']['files'], 3583)
        text = json.dumps(rep)
        self.assertNotIn('10.0.0.5', text)
        self.assertNotIn('abcdef0123456789', text)
        self.assertNotIn('notes', rep['report'])
        self.assertIsNone(rep['anomaly'])
        self.assertEqual(rep['runs_30d'], {'ok': 250, 'anomaly': 4})

    def test_a_refused_latest_run_carries_why(self):
        rep = self._get([{'ran_at': '2026-11-09T08:12:40', 'trigger': 'scheduled', 'status': 'anomaly',
                          'duration_seconds': 1.0, 'peak_rss_mb': 90,
                          'error_message': 'Source anomaly: …'}],
                        {'last_source_report': self.REPORT,
                         'last_source_anomaly': {'at': '2026-11-09T08:12:40', 'trigger': 'scheduled',
                                                 'code': 'torrent_count_collapse', 'message': 'down from 1,503',
                                                 'detail': {'torrent_count': 612}}})
        self.assertEqual(rep['last_run']['status'], 'anomaly')
        self.assertEqual(rep['anomaly'], {'at': '2026-11-09T08:12:40', 'trigger': 'scheduled',
                                          'code': 'torrent_count_collapse', 'message': 'down from 1,503'})


class BackfillUnderTests(unittest.TestCase):
    """B15: the map's tiles are folders, and a folder's name isn't the arr's title."""
    GROUPS = [
        {'key': 'a', 'folder': '/data/media/television', 'arr_title': 'PLUR1BUS',
         'path': 'television/Pluribus/Season 1/Pluribus.S01E01.mkv'},
        {'key': 'b', 'folder': '/data/media/television', 'arr_title': 'The Pitt',
         'path': 'television/The Pitt/Season 2/The.Pitt.S02E01.mkv'},
        {'key': 'c', 'folder': '/data/media/television', 'arr_title': 'The Pittsburgh Diaries',
         'path': 'television/The Pittsburgh Diaries/S01E01.mkv'},
        {'key': 'd', 'folder': '/data/media/movies', 'arr_title': 'Ran',
         'path': 'movies/Ran.1985.mkv'},
    ]

    def _keys(self, under):
        return [g['key'] for g in app._narrow_backfill(list(self.GROUPS), under=under)]

    def test_a_folder_finds_its_title_whatever_the_arr_calls_it(self):
        self.assertEqual(self._keys('television/Pluribus'), ['a'])

    def test_a_folder_never_matches_a_sibling_that_starts_the_same(self):
        self.assertEqual(self._keys('television/The Pitt'), ['b'])

    def test_a_file_directly_in_a_folder_is_its_own_scope(self):
        self.assertEqual(self._keys('movies/Ran.1985.mkv'), ['d'])

    def test_no_scope_narrows_nothing(self):
        self.assertEqual(self._keys(None), ['a', 'b', 'c', 'd'])
        self.assertEqual(self._keys(''), ['a', 'b', 'c', 'd'])


class QuiPreferenceViewTests(unittest.TestCase):
    """C28: qui answers `/preferences` with a list of fields since its preference
    catalog. `client_save_dirs` read the list as "couldn't ask", silently, so the
    box offered no empty folder for a month."""
    VIEW = [{'key': 'save_path', 'value': '/data/torrents', 'tab': 'downloads', 'control': 'path'},
            {'key': 'use_subcategories', 'value': False, 'tab': 'downloads', 'control': 'switch'},
            {'key': 'temp_path_enabled', 'value': True, 'tab': 'downloads', 'control': 'switch'},
            {'key': 'temp_path', 'value': '/data/torrents/incomplete', 'tab': 'downloads', 'control': 'path'}]

    def _scope(self, prefs, cats):
        sess = MagicMock()
        replies = {'preferences': prefs, 'categories': cats}
        sess.get.side_effect = lambda url, timeout: MagicMock(
            raise_for_status=lambda: None, json=lambda: replies[url.rsplit('/', 1)[1]])
        scope = {'dirs': set(), 'unknown': False}
        _qui._read_save_scope(sess, 'http://qui', {'id': 1, 'name': 'main'},
                              '/data/torrents', '/data/torrents', scope)
        return scope

    def test_the_field_list_is_read(self):
        scope = self._scope(self.VIEW, {'radarr': {'name': 'radarr', 'savePath': ''}})
        self.assertFalse(scope['unknown'])
        self.assertEqual(scope['dirs'], {'/data/torrents', '/data/torrents/radarr',
                                         '/data/torrents/incomplete'})

    def test_the_old_dict_still_reads(self):
        scope = self._scope({f['key']: f['value'] for f in self.VIEW}, {})
        self.assertFalse(scope['unknown'])
        self.assertIn('/data/torrents/incomplete', scope['dirs'])

    def test_a_shape_it_cannot_read_is_unknown_and_says_so(self):
        with self.assertLogs('sources._qui', logging.INFO) as logs:
            scope = self._scope('not a dict', {})
        self.assertTrue(scope['unknown'])
        self.assertIn("shape auditorr doesn't read", logs.output[0])
        self.assertIn('str', logs.output[0])


class DiskSpaceTests(unittest.TestCase):
    def test_both_roots_on_one_filesystem_say_so(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, 'a'), os.path.join(d, 'b')
            os.mkdir(a); os.mkdir(b)
            out = audit.disk_space(a, b)
        self.assertTrue(out['same'])
        self.assertGreater(out['torrents']['total'], 0)

    def test_a_root_that_is_not_there_is_left_out(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = audit.disk_space(d, os.path.join(d, 'missing'))
        self.assertNotIn('media', out)
        self.assertFalse(out['same'])
        self.assertIsNone(audit.disk_space('', ''))


if __name__ == '__main__':
    unittest.main()
