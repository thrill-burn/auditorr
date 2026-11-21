"""New in the library: a moved file isn't a new one (2026-11-20).

The diff knows a library file by its path, so a file moved or renamed read as a
new arrival: renaming a release-named folder to the arr's name put every
episode in it on the card as "not seeded", though they'd been on disk for
months. A file keeps its inode through a move, and the scan tells a move two
ways, neither of them a pass of its own:

- a linked file by its torrent file: the torrent diff collects the files linked
  since the last scan (`newly_linked`), and a new linked library path on any
  other was linked before;
- an unlinked file by `hash((inode, size))`, which `_assemble_records` keeps for
  every unlinked library record (`audit.library_keys` is the same rule) and the
  publish stores as `media_keys`. The next diff reads them only when its library
  both gained and lost a path.

Each library `new_files` entry the change log keeps says how its file arrived
(`arrived`), so a day on the card can be listed on the Changes page, and
`totals` counts every list uncapped, so the page can say what the 50-per-scan
cap left out.
"""

import json
import os
import sqlite3
import unittest
import zlib
from array import array
from unittest.mock import MagicMock, patch

import audit
from backend_tests.test_publish_atomicity import real_db, run_scan

ROOT = '/data/torrents'
GB = 1 << 30


def _media(path, inode, size=GB, linked=False, dups=(), excluded=False):
    return {'path': path, 'inode': inode, 'size': size,
            'linked_paths': ['/t/x'] if linked else [], 'duplicate_paths': list(dups),
            'excluded': excluded, 'status': 'Seeding' if linked else 'Orphaned', 'imported': linked}


def _torrent(path, inode, size=GB, imported=True):
    return {'path': path, 'inode': inode, 'size': size, 'imported': imported,
            'status': 'Seeding', 'linked_paths': ['/m/x'] if imported else [],
            'duplicate_paths': [], 'excluded': False}


def _diff(prev_media, curr_media, prev_torrents=(), curr_torrents=(), load=None, torrents_known=True):
    """Both trees in one call, as the scan's two give it: the previous scan's
    records give the signatures and, unless `load` says otherwise, the stored
    keys. Returns the counts, the entry's library half and the loader."""
    counts = {'linked': 0, 'copies': 0, 'unseeded': 0}
    if load is None:
        load = MagicMock(return_value=audit.library_keys(prev_media))
    out = audit.compute_diff_from_signatures(
        {'media': audit.file_signatures(prev_media), 'torrents': audit.file_signatures(list(prev_torrents))},
        {'media_files': curr_media, 'torrent_files': list(curr_torrents), 'dashboard': {}},
        new_media=counts, torrent_root=ROOT, prev_library_keys=load,
        newly_linked=set() if torrents_known else None)
    lib = [e for e in (out or {}).get('new_files', []) if e['tab'] == 'media']
    return counts, out, lib, load


class UnlinkedMoveTests(unittest.TestCase):
    def test_a_renamed_folder_is_moved_not_new(self):
        """A release-named folder renamed to the arr's name."""
        before = [_media(f'television/Some.Show.1990.S01.1080p-GRP/S01E{e:02d}.mp4', inode=100 + e,
                         size=433_000_000 + e) for e in range(1, 37)]
        after = [dict(f, path=f['path'].replace('Some.Show.1990.S01.1080p-GRP', 'Some Show (1990)'))
                 for f in before]
        counts, out, lib, load = _diff(before, after)
        self.assertEqual(counts, {'linked': 0, 'copies': 0, 'unseeded': 0, 'moved': 36})
        load.assert_called_once()
        self.assertEqual({e['arrived'] for e in lib}, {'moved'})
        self.assertEqual(out['totals'], {'new_files': {'media': 36}, 'removed_files': {'media': 36}})

    def test_a_reused_inode_is_new_when_the_size_differs(self):
        """An upgrade by copy frees the old file's inode, and the filesystem can
        hand it straight to the copy: the size is what tells them apart."""
        before = [_media('movies/Heat (1995)/Heat.1995.1080p.mkv', inode=5, size=12 * GB)]
        after = [_media('movies/Heat (1995)/Heat.1995.2160p.mkv', inode=5, size=60 * GB,
                        dups=['/data/torrents/radarr/Heat.1995.2160p.mkv'])]
        counts, _, _, _ = _diff(before, after)
        self.assertEqual(counts, {'linked': 0, 'copies': 1, 'unseeded': 0})

    def test_the_keys_are_read_only_when_the_library_lost_a_path(self):
        """A move leaves a new path and a removed one. A scan that only gained
        files, which is most of them, reads nothing."""
        before = [_media('movies/A/a.mkv', inode=1)]
        after = before + [_media('movies/B/b.mkv', inode=2)]
        load = MagicMock(return_value=audit.library_keys(before))
        counts, _, _, _ = _diff(before, after, load=load)
        load.assert_not_called()
        self.assertEqual(counts, {'linked': 0, 'copies': 0, 'unseeded': 1})

    def test_the_keys_are_not_read_with_nothing_unlinked_to_check(self):
        """Only removals, new files that don't count, or new linked files (the
        torrent tree's to decide) read nothing."""
        before = [_media('movies/A/a.mkv', inode=1), _media('movies/A/a.en.srt', inode=2)]
        after = [_media('movies/A/a.en.forced.srt', inode=3),
                 _media('movies/B/b.mkv', inode=4, linked=True)]
        load = MagicMock(return_value=audit.library_keys(before))
        _diff(before, after, curr_torrents=[_torrent('radarr/b.mkv', inode=4)], load=load)
        load.assert_not_called()

    def test_no_stored_keys_counts_as_it_did_before(self):
        """The first scan after upgrading has no keys to read: could not ask,
        so every new unlinked path counts as an arrival, as it always did."""
        before = [_media('movies/Old/a.mkv', inode=1)]
        after = [_media('movies/New/a.mkv', inode=1)]
        counts, _, lib, load = _diff(before, after, load=MagicMock(return_value=None))
        load.assert_called_once()
        self.assertEqual(counts, {'linked': 0, 'copies': 0, 'unseeded': 1})
        self.assertEqual(lib[0]['arrived'], 'unseeded')

    def test_a_record_without_an_inode_is_never_a_move(self):
        before = [_media('movies/Old/a.mkv', inode=1)]
        after = [dict(_media('movies/New/a.mkv', inode=1), inode=None)]
        counts, _, _, _ = _diff(before, after)
        self.assertEqual(counts, {'linked': 0, 'copies': 0, 'unseeded': 1})

    def test_more_new_paths_than_one_chunk(self):
        """A whole library renamed is checked a chunk at a time, so it never
        holds one set the size of the library."""
        before = [_media(f'tv/Old/{i}.mkv', inode=i, size=GB + i) for i in range(10)]
        after = ([dict(f, path=f['path'].replace('Old', 'New')) for f in before]
                 + [_media(f'tv/New/new{i}.mkv', inode=1000 + i) for i in range(4)])
        with patch.object(audit, '_MOVE_CHUNK', 3):
            counts, _, lib, _ = _diff(before, after)
        self.assertEqual(counts, {'linked': 0, 'copies': 0, 'unseeded': 4, 'moved': 10})
        self.assertEqual([e['arrived'] for e in lib], ['moved'] * 10 + ['unseeded'] * 4)


class LinkedMoveTests(unittest.TestCase):
    """A linked library file is judged by its torrent file: linked before, the
    library path is new but the bytes aren't."""

    def test_a_renamed_linked_file_is_moved(self):
        """A Sonarr rename of a linked season used to read as that many new
        hardlinks: the right colour, the wrong number."""
        tor = [_torrent('sonarr/Show.S01/Show.S01E01.mkv', inode=7)]
        before = [_media('tv/Show/Season 1/Show.S01E01.mkv', inode=7, linked=True)]
        after = [_media('tv/Show/Season 01/Show - S01E01 - Pilot.mkv', inode=7, linked=True)]
        counts, _, lib, load = _diff(before, after, tor, tor)
        self.assertEqual(counts, {'linked': 0, 'copies': 0, 'unseeded': 0, 'moved': 1})
        self.assertEqual(lib[0]['arrived'], 'moved')
        load.assert_not_called()          # the torrent tree said so; no keys read

    def test_an_upgrade_is_new(self):
        """The arr deletes the old file and links the new torrent's: a new
        torrent file linked this scan, so it arrived."""
        before_t = [_torrent('radarr/Heat.1995.1080p.mkv', inode=1, size=12 * GB)]
        after_t = [_torrent('radarr/Heat.1995.1080p.mkv', inode=1, size=12 * GB, imported=False),
                   _torrent('radarr/Heat.1995.2160p.mkv', inode=2, size=60 * GB)]
        before = [_media('movies/Heat (1995)/Heat.1995.1080p.mkv', inode=1, size=12 * GB, linked=True)]
        after = [_media('movies/Heat (1995)/Heat.1995.2160p.mkv', inode=2, size=60 * GB, linked=True)]
        counts, _, lib, _ = _diff(before, after, before_t, after_t)
        self.assertEqual(counts, {'linked': 1, 'copies': 0, 'unseeded': 0})
        self.assertEqual(lib[0]['arrived'], 'linked')

    def test_a_torrent_linked_for_the_first_time_is_new(self):
        """A force import of a torrent that sat not imported: its path is old,
        its link is new."""
        before_t = [_torrent('radarr/Kestrel.2026.mkv', inode=3, imported=False)]
        after_t = [_torrent('radarr/Kestrel.2026.mkv', inode=3)]
        counts, _, lib, _ = _diff([_media('movies/Other/o.mkv', inode=9)],
                                  [_media('movies/Other/o.mkv', inode=9),
                                   _media('movies/Kestrel (2026)/Kestrel.mkv', inode=3, linked=True)],
                                  before_t, after_t)
        self.assertEqual(counts, {'linked': 1, 'copies': 0, 'unseeded': 0})

    def test_with_no_previous_torrent_scan_every_link_is_new(self):
        """No previous torrent signatures: could not ask, so a link counts."""
        tor = [_torrent('sonarr/e1.mkv', inode=7)]
        before = [_media('tv/Show/e1.mkv', inode=7, linked=True)]
        after = [_media('tv/Show/renamed e1.mkv', inode=7, linked=True)]
        counts, _, _, _ = _diff(before, after, tor, tor, torrents_known=False)
        self.assertEqual(counts, {'linked': 1, 'copies': 0, 'unseeded': 0})
        # And a torrent tree with no previous scan reads every torrent file as
        # newly linked, which is the same answer.
        counts, _, _, _ = _diff(before, after, (), tor)
        self.assertEqual(counts, {'linked': 1, 'copies': 0, 'unseeded': 0})

    def test_moves_and_arrivals_in_one_scan_are_told_apart(self):
        old_t = [_torrent(f'radarr/{i}.mkv', inode=50 + i) for i in range(2)]
        new_t = old_t + [_torrent('radarr/fresh.mkv', inode=99)]
        before = ([_media(f'movies/Old/{i}.mkv', inode=i, size=GB + i) for i in range(3)]
                  + [_media(f'movies/OldL/{i}.mkv', inode=50 + i, linked=True) for i in range(2)])
        after = ([dict(f, path=f['path'].replace('/Old', '/New')) for f in before]
                 + [_media('movies/Fresh/x.mkv', inode=99, linked=True),
                    _media('movies/Rip/rip.mkv', inode=200)])
        counts, out, lib, _ = _diff(before, after, old_t, new_t)
        self.assertEqual(counts, {'linked': 1, 'copies': 0, 'unseeded': 1, 'moved': 5})
        self.assertEqual([e['arrived'] for e in lib], ['moved'] * 5 + ['linked', 'unseeded'])
        self.assertEqual(out['totals']['removed_files'], {'media': 5})

    def test_the_torrent_tree_in_its_own_call_says_the_same(self):
        """The scan diffs the torrent tree first, in its own call, and hands
        the library's call what it collected."""
        old_t = [_torrent('radarr/a.mkv', inode=1), _torrent('radarr/b.mkv', inode=2, imported=False)]
        new_t = [_torrent('radarr/a.mkv', inode=1), _torrent('radarr/b.mkv', inode=2),
                 _torrent('radarr/c.mkv', inode=3)]
        before = [_media('movies/A/a.mkv', inode=1, linked=True)]
        after = [_media('movies/A2/a.mkv', inode=1, linked=True), _media('movies/B/b.mkv', inode=2, linked=True),
                 _media('movies/C/c.mkv', inode=3, linked=True)]
        nl = set()
        audit.compute_diff_from_signatures({'torrents': audit.file_signatures(old_t)},
                                           {'torrent_files': new_t, 'dashboard': {}}, newly_linked=nl)
        self.assertEqual(nl, {hash((2, GB)), hash((3, GB))})
        counts = {'linked': 0, 'copies': 0, 'unseeded': 0}
        audit.compute_diff_from_signatures({'media': audit.file_signatures(before)},
                                           {'media_files': after, 'dashboard': {}}, new_media=counts,
                                           torrent_root=ROOT, prev_library_keys=MagicMock(), newly_linked=nl)
        self.assertEqual(counts, {'linked': 2, 'copies': 0, 'unseeded': 0, 'moved': 1})
        self.assertEqual(counts, _diff(before, after, old_t, new_t)[0])


class OnlyVideosTests(unittest.TestCase):
    def test_nothing_but_a_video_is_counted_however_it_arrived(self):
        """The user's call (2026-11-20): the card is about the library, so a
        hardlinked .nfo or subtitle no longer counts beside its episode."""
        before = [_media('tv/Show/old.mkv', inode=1)]
        tor = [_torrent('sonarr/e1.mkv', inode=2), _torrent('sonarr/e1.nfo', inode=3)]
        after = before + [
            _media('tv/Show/e1.mkv', inode=2, linked=True),
            _media('tv/Show/e1.nfo', inode=3, linked=True),
            _media('tv/Show/e1.en.srt', inode=4, dups=['/data/torrents/sonarr/Show/e1.en.srt']),
            _media('tv/Show/poster.jpg', inode=5),
        ]
        counts, _, lib, _ = _diff(before, after, (), tor)
        self.assertEqual(counts, {'linked': 1, 'copies': 0, 'unseeded': 0})
        # Listed in the change log all the same; only the counted one is tagged.
        self.assertEqual([(e['path'].rsplit('/', 1)[1], e.get('arrived')) for e in lib],
                         [('e1.mkv', 'linked'), ('e1.nfo', None), ('e1.en.srt', None), ('poster.jpg', None)])


class EntryTests(unittest.TestCase):
    def test_only_the_entries_the_cap_kept_are_tagged_and_totals_count_them_all(self):
        before = [_media('tv/gone.mkv', inode=1)]
        after = [_media(f'tv/New/{i:03d}.mkv', inode=10 + i) for i in range(70)]
        counts, out, lib, _ = _diff(before, after)
        self.assertEqual(counts['unseeded'], 70)
        self.assertEqual(len(lib), 50)
        self.assertTrue(all(e['arrived'] == 'unseeded' for e in lib))
        self.assertEqual(out['totals'], {'new_files': {'media': 70}, 'removed_files': {'media': 1}})
        self.assertEqual(list(out), list(audit._DIFF_LISTS) + ['score_delta', 'totals'])

    def test_totals_are_in_one_order_whatever_order_the_changes_came_in(self):
        self.assertEqual(
            audit._ordered_totals({'removed_files': {'torrents': 2, 'media': 1}, 'new_files': {'media': 3}}),
            {'new_files': {'media': 3}, 'removed_files': {'media': 1, 'torrents': 2}})
        self.assertEqual(list(audit._ordered_totals({'removed_files': {'media': 1}, 'new_files': {'media': 3}})),
                         ['new_files', 'removed_files'])


class LibraryKeysTests(unittest.TestCase):
    def test_only_unlinked_records_are_kept(self):
        files = [_media('a/1.mkv', inode=1), _media('a/2.mkv', inode=2, linked=True),
                 _media('a/3.mkv', inode=3, size=5), {'path': 'a/4.mkv', 'size': 5}]
        keys = audit.library_keys(files)
        self.assertEqual(keys.typecode, 'q')
        self.assertEqual(list(keys), [hash((1, GB)), hash((3, 5))])

    def test_no_key_is_the_sentinel(self):
        """CPython never hashes a tuple to -1, which is what makes -1 safe to
        mean "no key"."""
        self.assertNotIn(audit._NO_KEY, audit.library_keys(
            [_media('a.mkv', inode=i, size=s) for i in range(-3, 300) for s in (-2, -1, 0, 1)]))

    def test_the_assemble_keeps_the_same_keys(self):
        """`_assemble_records` keeps them in the branches that already run only
        for unlinked records, unlinked copies included, in record order."""
        def inode(media_rel, torrent_paths=()):
            return {'size': 100, 'status': 'Orphaned', 'trackers': set(), 'media_rel_path': media_rel,
                    'torrent_paths': list(torrent_paths), 'media_paths': [f'/m/{media_rel}'],
                    'media_excluded': False, 'torrent_rel_path': None}
        inode_map = {(1, 1): inode('a.mkv'), (1, 2): inode('b.mkv', ['/t/b.mkv']),
                     (1, 3): inode('c.mkv'), (1, 4): inode('d.mkv')}
        dup = {(1, 3): ['/t/copy.mkv']}
        keys = array('q')
        _, media = audit._assemble_records([], [(1, 1), (1, 2), (1, 3), (1, 4)], inode_map, dup,
                                           unlinked_keys=keys)
        self.assertEqual(list(keys), [hash((1, 100)), hash((3, 100)), hash((4, 100))])
        self.assertEqual(keys, audit.library_keys(media))


class UploadStatsTests(unittest.TestCase):
    def test_a_day_sums_its_moves_apart_from_its_arrivals(self):
        def row(at, nm):
            return {'taken_at': at, 'source': 'qui', 'snapshot': {
                '_instance_count': 1, 'a.cc': {'uploaded': 0, 'seeding_size': 1},
                '_library': {'total_media_size': 1, 'new_media': nm}}}
        rows = [row('2026-03-02T10:00:00', {'linked': 0, 'copies': 0, 'unseeded': 0, 'moved': 40}),
                row('2026-03-02T10:05:00', {'linked': 0, 'copies': 0, 'unseeded': 0, 'moved': 40}),
                row('2026-03-02T11:00:00', {'linked': 1, 'copies': 0, 'unseeded': 1})]   # before moves
        with patch.object(audit, 'db_get_upload_snapshots', return_value=rows):
            stats = audit.compute_upload_stats(30)
        self.assertEqual(stats['daily_new_media'],
                         [{'date': '2026-03-02', 'linked': 1, 'copies': 0, 'unseeded': 1, 'moved': 80}])


# ---------------------------------------------------------------------------
# Real scans: folders renamed between two scans
# ---------------------------------------------------------------------------

def _read(path):
    conn = sqlite3.connect(str(path))
    try:
        keys = conn.execute("SELECT files_json FROM file_results WHERE tab = 'media_keys'").fetchone()
        media = conn.execute("SELECT files_json FROM file_results WHERE tab = 'media'").fetchone()
        snap = conn.execute('SELECT snapshot FROM upload_snapshots ORDER BY id DESC LIMIT 1').fetchone()
        log = conn.execute('SELECT diff_json FROM change_log ORDER BY id DESC LIMIT 1').fetchone()
        return (keys[0] if keys else None, json.loads(zlib.decompress(media[0]).decode()),
                json.loads(snap[0]), json.loads(log[0]) if log else None)
    finally:
        conn.close()


def _folder(entry_path):
    return entry_path.replace('\\', '/').split('/')[-2]


def test_folders_renamed_between_scans_are_moved_not_new(tmp_path):
    """One unlinked folder and one linked folder renamed, beside one new file:
    the unlinked by its key, the linked by its torrent file."""
    lib = tmp_path / 'media' / 'Movies'
    linked = lib / 'Linked'
    linked.mkdir(parents=True)
    tor = tmp_path / 'torrents' / 'movies' / 'Rel'
    tor.mkdir(parents=True)
    (tor / 'f1.mkv').write_bytes(b'x' * 11)          # what run_scan writes there
    os.link(tor / 'f1.mkv', linked / 'a.mkv')
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=2, media=6)
        keys, media, _, _ = _read(path)
        assert keys == audit.library_keys(media).tobytes(), 'the assemble keeps the rule'
        assert len(keys) == 6 * 8, 'one 8-byte key per unlinked library file, none for the linked one'

        os.rename(lib / 'Rel', lib / 'The Film (2001)')
        os.rename(linked, lib / 'Linked Renamed')
        (lib / 'Fresh').mkdir()
        (lib / 'Fresh' / 'new.mkv').write_bytes(b'n' * 999)
        # `run_scan` makes `Rel` again, empty: media=0 writes no files into it.
        run_scan(tmp_path, torrents=2, media=0)
        _, _, snap, entry = _read(path)

    assert snap['_library']['new_media'] == {'linked': 0, 'copies': 0, 'unseeded': 1, 'moved': 7}
    arrived = {_folder(e['path']): e['arrived'] for e in entry['new_files'] if e['tab'] == 'media'}
    assert arrived == {'The Film (2001)': 'moved', 'Linked Renamed': 'moved', 'Fresh': 'unseeded'}
    assert entry['totals']['new_files'] == {'media': 8}
    assert entry['totals']['removed_files'] == {'media': 7}


def test_the_keys_are_part_of_the_publish(tmp_path):
    """Written in the publish with the signatures (S04): a publish that fails
    leaves the last scan's keys, so the next diff reads one generation."""
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=1, media=2)
        good = _read(path)[0]
        boom = patch('audit.db_update_meta', side_effect=RuntimeError('disk full'))
        run_scan(tmp_path, torrents=1, media=5, extra=[boom])
        after = _read(path)[0]
    assert len(good) == 2 * 8
    assert after == good


def test_a_scan_with_no_stored_keys_still_saves_them(tmp_path):
    """A database from before the keys: the scan counts as it used to and
    stores them, so the next one can tell."""
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=1, media=3)
        conn = sqlite3.connect(str(path))
        conn.execute("DELETE FROM file_results WHERE tab = 'media_keys'")
        conn.commit()
        conn.close()
        lib = tmp_path / 'media' / 'Movies'
        os.rename(lib / 'Rel', lib / 'Renamed')
        run_scan(tmp_path, torrents=1, media=0)
        keys, _, snap, _ = _read(path)
    # (`run_scan` writes the same bytes as f0 in both trees, so one is a copy.)
    nm = snap['_library']['new_media']
    assert nm['moved'] == 0 and nm['linked'] + nm['copies'] + nm['unseeded'] == 3
    assert len(keys) == 3 * 8
