"""SCAN SC8, the diff half — one tree's previous signatures at a time.

The change-log diff used to load both trees' previous `{path: bitmask}` maps
together and diff them in one call. Each map is a string per file of the last
scan, held beside this scan's records, so the scan now loads the torrent tree's
(the shovel count reads it too), diffs it, lets it go, then does the same for
the library, and `audit._merge_tab_diffs` joins the two answers.

What's stored must not move. The change-log entry is `json.dumps(diff)`, so
both its key order and every list's order reach the database: each list is the
library's entries, then the torrent tree's, 50 in all; `score_delta` is the
same; and there is no entry when neither tree changed. A tree with no previous
scan lists every one of its files as new, as one call over both did. *New in
the library* is counted only when the library had a previous scan.
"""

import json
import random
import sqlite3
import weakref
from contextlib import contextmanager
from unittest.mock import patch

import audit
from audit import _merge_tab_diffs, compute_diff_from_signatures, file_signatures
from backend_tests.test_publish_atomicity import real_db, run_scan


# ---------------------------------------------------------------------------
# The merge against one call, on made-up trees
# ---------------------------------------------------------------------------

def _records(rng, tab, n, prefix='p'):
    out = []
    for i in range(n):
        dups = [f'/data/torrents/x/{prefix}{i}.mkv'] if rng.random() < 0.3 else []
        out.append({
            'path': f'/data/{tab}/{prefix}{i}.mkv', 'size': 100 + i,
            'status': rng.choice(['Seeding', 'Orphaned', 'Seeding']),
            'imported': rng.random() < 0.5, 'duplicate_paths': dups,
            'excluded': rng.random() < 0.1,
            'linked_paths': [f'/data/torrents/l/{i}.mkv'] if rng.random() < 0.4 else [],
        })
    return out


def _mutate(rng, records, tab):
    """The same tree a scan later: some files gone, some new, flags flipped."""
    out = [dict(r) for r in records if rng.random() > 0.15]
    for r in out:
        if rng.random() < 0.3:
            r['status'] = 'Orphaned' if r['status'] == 'Seeding' else 'Seeding'
        if rng.random() < 0.3:
            r['imported'] = not r['imported']
        if rng.random() < 0.3:
            r['duplicate_paths'] = [] if r['duplicate_paths'] else ['/data/torrents/y.mkv']
    out += _records(rng, tab, rng.choice([0, 5, 70]), prefix='n')
    return out


def _two_calls(prev_m, prev_t, curr, prev_score, torrent_root, load=None):
    """The scan's sequence: the torrent tree, then the library, then the merge."""
    new_media = {'linked': 0, 'copies': 0, 'unseeded': 0}
    if not prev_m and not prev_t:
        return None, new_media
    diff_t = None
    newly_linked = set() if prev_t else None
    if prev_t:
        diff_t = compute_diff_from_signatures(
            {'torrents': prev_t},
            {'torrent_files': curr['torrent_files'], 'dashboard': curr['dashboard']},
            newly_linked=newly_linked)
    diff_m = compute_diff_from_signatures(
        {'media': prev_m}, {'media_files': curr['media_files'], 'dashboard': curr['dashboard']},
        prev_score=prev_score, new_media=new_media, torrent_root=torrent_root,
        prev_library_keys=load, newly_linked=newly_linked)
    if not prev_t:
        diff_t = compute_diff_from_signatures(
            {'torrents': {}},
            {'torrent_files': curr['torrent_files'], 'dashboard': curr['dashboard']})
    return _merge_tab_diffs(diff_m, diff_t, prev_score, curr['dashboard']), new_media


def _one_call(prev_m, prev_t, curr, prev_score, torrent_root, load=None):
    new_media = {'linked': 0, 'copies': 0, 'unseeded': 0}
    if not prev_m and not prev_t:
        return None, new_media
    diff = compute_diff_from_signatures(
        {'media': prev_m, 'torrents': prev_t}, curr, prev_score=prev_score,
        new_media=new_media, torrent_root=torrent_root, prev_library_keys=load,
        newly_linked=set())
    return diff, new_media


def test_the_merge_is_one_call_over_both_trees():
    rng = random.Random(8)
    cases = 0
    for sizes in [(0, 0), (3, 0), (0, 3), (3, 3), (40, 40), (120, 4), (4, 120), (120, 120)]:
        for had in [(True, True), (True, False), (False, True), (False, False)]:
            for scores in [(None, 70.0), (64.25, None), (64.25, 70.0), (70.0, 70.0)]:
                for _ in range(3):
                    prev_media = _records(rng, 'media', sizes[0])
                    prev_tor = _records(rng, 'torrents', sizes[1])
                    curr = {'media_files': _mutate(rng, prev_media, 'media'),
                            'torrent_files': _mutate(rng, prev_tor, 'torrents'),
                            'dashboard': {'score': scores[1]}}
                    prev_m = file_signatures(prev_media) if had[0] else {}
                    prev_t = file_signatures(prev_tor) if had[1] else {}
                    args = (prev_m, prev_t, curr, scores[0], '/data/torrents')
                    want, want_new = _one_call(*args)
                    got, got_new = _two_calls(*args)
                    assert json.dumps(got) == json.dumps(want), (sizes, had, scores)
                    assert got_new == want_new
                    cases += 1
    assert cases == 8 * 4 * 4 * 3


def _with_inodes(records, start):
    return [dict(r, inode=start + i) for i, r in enumerate(records)]


def test_the_merge_is_one_call_over_both_trees_with_moves():
    """2026-11-20: the library's moves (a new path on a file the previous scan
    held) are tagged and counted the same way in the two calls as in one."""
    rng = random.Random(9)
    moved_any = 0
    for sizes in [(3, 3), (40, 40), (120, 4), (4, 120), (120, 120)]:
        for had in [(True, True), (True, False)]:
            for _ in range(4):
                prev_media = _with_inodes(_records(rng, 'media', sizes[0]), 1)
                prev_tor = _records(rng, 'torrents', sizes[1])
                curr_media = _mutate(rng, prev_media, 'media')
                curr_media = [dict(r, path=r['path'].replace('/p', '/moved/p'))
                              if 'inode' in r and rng.random() < 0.3 else r for r in curr_media]
                curr_media = [r if 'inode' in r else dict(r, inode=10_000 + i)
                              for i, r in enumerate(curr_media)]
                curr = {'media_files': curr_media,
                        'torrent_files': _mutate(rng, prev_tor, 'torrents'),
                        'dashboard': {'score': 70.0}}
                keys = audit.library_keys(prev_media)
                prev_m = file_signatures(prev_media)
                prev_t = file_signatures(prev_tor) if had[1] else {}
                args = (prev_m, prev_t, curr, 60.0, '/data/torrents')
                want, want_new = _one_call(*args, load=lambda: keys)
                got, got_new = _two_calls(*args, load=lambda: keys)
                assert json.dumps(got) == json.dumps(want), (sizes, had)
                assert got_new == want_new
                moved_any += want_new.get('moved', 0)
    assert moved_any, 'the cases moved something'


def test_more_than_fifty_entries_keep_the_librarys_first():
    """The cap is shared: a library with 60 new files fills `new_files`, and the
    torrent tree's entries are what's cut, as in one call."""
    rng = random.Random(1)
    curr = {'media_files': _records(rng, 'media', 60), 'torrent_files': _records(rng, 'torrents', 60),
            'dashboard': {'score': 50.0}}
    prev_m = {'/data/media/gone.mkv': 0}
    prev_t = {'/data/torrents/gone.mkv': 0}
    got, _ = _two_calls(prev_m, prev_t, curr, 49.0, None)
    want, _ = _one_call(prev_m, prev_t, curr, 49.0, None)
    assert json.dumps(got) == json.dumps(want)
    assert [e['tab'] for e in got['new_files']] == ['media'] * 50
    assert got['removed_files'] == [{'path': '/data/media/gone.mkv', 'tab': 'media'},
                                    {'path': '/data/torrents/gone.mkv', 'tab': 'torrents'}]
    assert got['score_delta'] == 1.0
    # What the cap left out is still counted, for Audit History to say so.
    assert got['totals']['new_files'] == {'media': 60, 'torrents': 60}
    assert got['totals']['removed_files'] == {'media': 1, 'torrents': 1}


def test_split_lists_take_the_torrent_trees_entries_after_the_librarys():
    rng = random.Random(2)
    curr = {'media_files': _records(rng, 'media', 30), 'torrent_files': _records(rng, 'torrents', 40),
            'dashboard': {}}
    got, _ = _two_calls({'/m': 0}, {'/t': 0}, curr, None, None)
    assert [e['tab'] for e in got['new_files']] == ['media'] * 30 + ['torrents'] * 20
    assert got['score_delta'] is None
    assert list(got) == list(audit._DIFF_LISTS) + ['score_delta', 'totals']


def test_nothing_changed_in_either_tree_is_no_entry():
    rng = random.Random(3)
    media, tor = _records(rng, 'media', 5), _records(rng, 'torrents', 5)
    curr = {'media_files': media, 'torrent_files': tor, 'dashboard': {'score': 80.0}}
    got, _ = _two_calls(file_signatures(media), file_signatures(tor), curr, 70.0, None)
    assert got is None
    assert _merge_tab_diffs(None, None, 70.0, {'score': 80.0}) is None


def test_a_change_in_one_tree_is_an_entry_with_the_other_empty():
    rng = random.Random(4)
    media, tor = _records(rng, 'media', 5), _records(rng, 'torrents', 5)
    curr = {'media_files': media, 'torrent_files': tor + _records(rng, 'torrents', 1, prefix='z'),
            'dashboard': {'score': 80.0}}
    got, _ = _two_calls(file_signatures(media), file_signatures(tor), curr, 70.0, None)
    assert [e['path'] for e in got['new_files']] == ['/data/torrents/z0.mkv']
    assert all(got[k] == [] for k in audit._DIFF_LISTS if k != 'new_files')
    assert got['score_delta'] == 10.0


# ---------------------------------------------------------------------------
# The scan itself: what it stores, and what it holds
# ---------------------------------------------------------------------------

class _Sigs(dict):
    """A dict a weak reference can watch."""


@contextmanager
def _watching():
    """Record each previous map the scan loads, the one-call answer for those
    maps and this scan's records (worked out when the merge is reached, before
    anything later could touch the records), and the maps alive at each load."""
    seen = {'loads': [], 'maps': {}, 'curr': {}, 'want': None, 'refs': []}
    real_load, real_diff, real_merge = (audit.db_load_file_signatures,
                                        audit.compute_diff_from_signatures,
                                        audit._merge_tab_diffs)

    def load(tab, *a, **kw):
        alive = [t for t, r in seen['refs'] if r() is not None]
        out = _Sigs(real_load(tab, *a, **kw))
        seen['loads'].append((tab, alive))
        seen['refs'].append((tab, weakref.ref(out)))
        seen['maps'][tab] = dict(out)
        return out

    def diff(prev, curr, *a, **kw):
        for key in ('media_files', 'torrent_files'):
            if key in curr:
                seen['curr'][key] = curr[key]
        seen['curr']['dashboard'] = curr.get('dashboard')
        if 'torrent_root' in kw:
            seen['root'] = kw['torrent_root']
        if 'prev_library_keys' in kw:
            seen['load'] = kw['prev_library_keys']
            # The library's call says whether the torrent tree could tell new
            # links from old (a set) or not (None); one call over both trees
            # starts its own set.
            seen['links'] = kw.get('newly_linked') is not None
        return real_diff(prev, curr, *a, **kw)

    def merge(media, torrents, prev_score, dashboard):
        new_media = {'linked': 0, 'copies': 0, 'unseeded': 0, 'moved': 0}
        seen['want'] = real_diff(
            {'media': seen['maps'].get('media') or {}, 'torrents': seen['maps'].get('torrents') or {}},
            {'media_files': seen['curr']['media_files'],
             'torrent_files': seen['curr']['torrent_files'],
             'dashboard': dashboard},
            prev_score=prev_score, new_media=new_media, torrent_root=seen.get('root'),
            prev_library_keys=seen.get('load'), newly_linked=set() if seen.get('links') else None)
        seen['want_new'] = new_media
        return real_merge(media, torrents, prev_score, dashboard)

    with patch('audit.db_load_file_signatures', new=load), \
         patch('audit.compute_diff_from_signatures', new=diff), \
         patch('audit._merge_tab_diffs', new=merge):
        yield seen


def _change_log(path):
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute('SELECT diff_json FROM change_log ORDER BY id')]
    finally:
        conn.close()


def _last_snapshot(path):
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute('SELECT snapshot FROM upload_snapshots ORDER BY id DESC LIMIT 1').fetchone()
        return json.loads(row[0])
    finally:
        conn.close()


def _forget(path, tab):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute('DELETE FROM file_results WHERE tab = ?', (f'{tab}_sigs',))
        conn.commit()
    finally:
        conn.close()


def _second_scan(tmp_path, path, forget=None):
    """A first scan, then a second where the library and the torrent tree both
    grew past 50 files, three torrent files lost their claim and one library
    file is gone. Returns what the second scan saw."""
    # `run_scan` writes f0…fN on every call, so the file that goes is another.
    gone = tmp_path / 'media' / 'Movies' / 'Rel' / 'old.mkv'
    gone.parent.mkdir(parents=True)
    gone.write_bytes(b'old')
    run_scan(tmp_path, torrents=5, media=5)
    assert _change_log(path) == []          # a first scan has nothing to compare
    if forget:
        _forget(path, forget)
    gone.unlink()
    with _watching() as seen:
        run_scan(tmp_path, torrents=70, media=70, claimed=2)
    assert seen['curr']['media_files'], 'the library was diffed'
    return seen


def test_the_stored_entry_is_one_calls_over_both_trees(tmp_path):
    with real_db(tmp_path) as path:
        seen = _second_scan(tmp_path, path)
        log = _change_log(path)
        snap = _last_snapshot(path)

    assert seen['want'] is not None
    assert log == [json.dumps(seen['want'])]
    entry = json.loads(log[0])
    assert len(entry['new_files']) == 50 and {e['tab'] for e in entry['new_files']} == {'media'}
    # In walk order, which is the filesystem's.
    assert sorted(e['path'].replace('\\', '/').rsplit('/', 1)[-1]
                  for e in entry['newly_orphaned']) == ['f2.mkv', 'f3.mkv', 'f4.mkv']
    assert [e['path'].replace('\\', '/').rsplit('/', 1)[-1] for e in entry['removed_files']] == ['old.mkv']
    assert snap['_library']['new_media'] == seen['want_new']
    assert sum(seen['want_new'].values()) == 65


def test_the_previous_maps_are_held_one_at_a_time(tmp_path):
    """The saving: when the library's previous map is loaded, the torrent
    tree's is already gone. One call over both trees held them together."""
    with real_db(tmp_path) as path:
        seen = _second_scan(tmp_path, path)
    assert [tab for tab, _ in seen['loads']] == ['torrents', 'media']
    assert seen['loads'][1] == ('media', [])


def test_a_torrent_tree_with_no_previous_scan_lists_its_files_as_new(tmp_path):
    with real_db(tmp_path) as path:
        seen = _second_scan(tmp_path, path, forget='torrents')
        log = _change_log(path)
        snap = _last_snapshot(path)
    assert seen['maps']['torrents'] == {}
    assert log == [json.dumps(seen['want'])]
    # The library's own entries come first and fill the shared cap.
    assert {e['tab'] for e in json.loads(log[0])['new_files']} == {'media'}
    assert json.loads(log[0])['newly_orphaned'] == []
    assert snap['_library']['new_media'] == seen['want_new']


def test_a_library_with_no_previous_scan_lists_its_files_as_new_and_counts_none(tmp_path):
    with real_db(tmp_path) as path:
        seen = _second_scan(tmp_path, path, forget='media')
        log = _change_log(path)
        snap = _last_snapshot(path)
    assert seen['maps']['media'] == {}
    assert log == [json.dumps(seen['want'])]
    assert json.loads(log[0])['removed_files'] == []
    # *New in the library* reads "not counted", never zero.
    assert 'new_media' not in (snap.get('_library') or {})
    assert seen['want_new'] == {'linked': 0, 'copies': 0, 'unseeded': 0, 'moved': 0}


def test_neither_tree_with_a_previous_scan_stores_no_entry(tmp_path):
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=3, media=3)
        _forget(path, 'torrents')
        _forget(path, 'media')
        with _watching() as seen:
            run_scan(tmp_path, torrents=4, media=4)
        log = _change_log(path)
        snap = _last_snapshot(path)
    assert log == []
    assert seen['want'] is None
    assert 'new_media' not in (snap.get('_library') or {})
