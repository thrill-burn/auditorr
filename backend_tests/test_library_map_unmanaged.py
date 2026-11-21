"""The library map's Unmanaged state (2026-11-14).

A library file that isn't seeded and that no arr holds. It was drawn Not
seeded, and a click opened Backfill on nothing: a second copy of a movie
beside Radarr's own folder, a show folder whose episodes Sonarr tracks none
of, a film's extras. The scan stamps it (`unmanaged`), the
row carries it as an eighth sum (version 3), and the stamp is Backfill's own
path test, so a video drawn Not seeded is always one Backfill can search.
"""
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app
import arr
import audit

GB = 1024 ** 3
ROOT = '/data/media'


def _media(path, size=GB, linked=False, dups=(), excluded=False):
    return {'path': path, 'size': size, 'linked_paths': ['/t/x'] if linked else [],
            'duplicate_paths': list(dups), 'excluded': excluded, 'trackers': ['arvel'] if linked else ['None'],
            'status': 'Seeding' if linked else 'Orphaned', 'imported': linked}


def _managed(*rels):
    return arr.ManagedPaths(arr.media_path_key(os.path.join(ROOT, r)) for r in rels)


# ── The stamp ────────────────────────────────────────────────────────────────

def test_an_unseeded_video_no_arr_holds_is_stamped():
    media = [_media("family_movies/The Owl's Promise (2011)/Owl.1080p.mkv"),
             _media('family_movies/The.Owls.Promise.720p.HDTV.x264-BiA/the.owls.promise.720p.mkv')]
    n = audit.stamp_unmanaged(media, ROOT, _managed("family_movies/The Owl's Promise (2011)/Owl.1080p.mkv"))
    assert n == 1
    assert 'unmanaged' not in media[0]
    assert media[1]['unmanaged'] is True


def test_linked_copied_and_excluded_files_are_never_stamped():
    """Unseeded as the map reads it: a linked file is Linked and a copy is Copies,
    whoever holds them, and an excluded file is on no tile."""
    media = [_media('movies/A/a.mkv', linked=True), _media('movies/B/b.mkv', dups=['/t/b.mkv']),
             _media('movies/C/c.mkv', excluded=True)]
    assert audit.stamp_unmanaged(media, ROOT, arr.ManagedPaths([])) == 0
    assert not any('unmanaged' in f for f in media)


def test_a_sidecar_follows_the_folder_it_sits_in():
    """No arr lists a subtitle or a poster. Matched alone, every sidecar of a
    Radarr movie would have read as in no arr; it's unmanaged only beside no
    file an arr holds."""
    media = [_media('movies/Wren (2009)/Wren.en.srt', size=1000), _media('movies/Wren (2009)/poster.jpg', size=1000),
             _media('movies/Stray/Stray.en.srt', size=1000), _media('movies/Stray/Stray.mkv')]
    audit.stamp_unmanaged(media, ROOT, _managed('movies/Wren (2009)/Wren.mkv'))
    assert [bool(f.get('unmanaged')) for f in media] == [False, False, True, True]


def test_an_extra_video_in_an_arr_folder_is_unmanaged():
    """Radarr holds the film, not the featurettes beside it."""
    media = [_media('movies/Comet Run (1977)/Comet.Run.mkv'),
             _media('movies/Comet Run (1977)/Featurettes/Making.Of.mkv')]
    audit.stamp_unmanaged(media, ROOT, _managed('movies/Comet Run (1977)/Comet.Run.mkv'))
    assert [bool(f.get('unmanaged')) for f in media] == [False, True]


# ── The arrs' answer ─────────────────────────────────────────────────────────

_RADARR = {'id': 'radarr-mv', 'service': 'radarr', 'name': 'Movies', 'base_url': 'http://mv:7878', 'api_key': 'c'}


def test_no_arr_configured_stamps_nothing():
    assert arr.fetch_arr_managed_paths({}) == (None, 'none')


_SONARR = {'id': 'sonarr-tv', 'service': 'sonarr', 'name': 'TV', 'base_url': 'http://tv:8989', 'api_key': 'a'}


def _no_cache():
    return patch.dict(arr._arr_media_index_cache, {}, clear=True)


def test_an_arr_that_failed_or_answered_in_part_stamps_nothing():
    """R1: a file of an instance that didn't answer would read as one no arr holds."""
    cfg = {'ARR_CONNECTIONS': [_RADARR, _SONARR]}
    with _no_cache(), patch('arr._fetch_radarr_media', return_value=([], None)), \
            patch('arr._fetch_sonarr_media', return_value=([], {'failed': 1, 'total': 9})):
        assert arr.fetch_arr_managed_paths(cfg) == (None, 'unreachable')
    with _no_cache(), patch('arr._fetch_radarr_media', side_effect=OSError('down')):
        assert arr.fetch_arr_managed_paths(cfg) == (None, 'unreachable')
    # A fresh snapshot a page left, with an instance that failed, is the same answer.
    snap = (arr.time.monotonic(), [{'path': '/data/media/m/a.mkv'}], ({'connection_id': 'sonarr-tv'},), {})
    with patch.dict(arr._arr_media_index_cache, {'snapshot': snap}, clear=True):
        assert arr.fetch_arr_managed_paths(cfg) == (None, 'unreachable')


def test_its_paths_are_the_media_index_s_own_mapped_and_keyed_as_backfill_keys_them():
    """The same arr answer through the index (`_apply_arr_media_path_mapping`,
    what Backfill matches) and through the scan's paths-only fetch."""
    conn = dict(_RADARR, media_path='/movies', local_media_path='/data/media/movies')
    cfg = {'MEDIA_PATH': ROOT, 'ARR_CONNECTIONS': [conn]}
    movies = [{'id': 1, 'title': 'A', 'movieFile': {'id': 1, 'path': '/movies/A (2001)/A.mkv'}},
              {'id': 2, 'title': 'B', 'movieFile': {'id': 2, 'path': '/movies//B (2002)/./B.mkv'}},
              {'id': 3, 'title': 'C', 'movieFile': {}}]
    with _no_cache(), patch('arr._arr_get', return_value=movies):
        paths, check = arr.fetch_arr_managed_paths(cfg)
        rows, _ = arr._fetch_radarr_media(arr.normalize_arr_connections(cfg)[0])
        index = arr._apply_arr_media_path_mapping(rows, arr.normalize_arr_connections(cfg)[0], cfg)
        # The scan's fetch never fills the cache the pages read: a row is about
        # a kilobyte, and the cache keeps the last index.
        assert 'snapshot' not in arr._arr_media_index_cache
    assert check == 'ok' and len(paths) == 2
    for r in index:
        assert arr.media_path_key(r['path']) in paths
    assert '/data/media/movies/A (2001)/A.mkv' in paths
    assert paths.has_dir('/data/media/movies/B (2002)')
    assert '/movies/A (2001)/A.mkv' not in paths


def test_a_fresh_snapshot_a_page_left_is_read_without_asking():
    cfg = {'ARR_CONNECTIONS': [_RADARR]}
    snap = (arr.time.monotonic(), [{'path': '/data/media/movies/A/a.mkv'}, {'path': ''}], (), {})
    with patch.dict(arr._arr_media_index_cache, {'snapshot': snap}, clear=True), \
            patch('arr._arr_get', side_effect=AssertionError('asked')):
        paths, check = arr.fetch_arr_managed_paths(cfg)
    assert check == 'ok' and len(paths) == 1 and '/data/media/movies/A/a.mkv' in paths


def test_a_sonarr_answers_a_series_at_a_time():
    series = [{'id': 1, 'title': 'S'}, {'id': 2, 'title': 'T'}]

    def get(_base, _key, url, timeout=None):
        if url == '/api/v3/series':
            return series
        sid = url.rsplit('=', 1)[1]
        return [{'id': 10, 'path': f'/data/media/tv/{sid}/e1.mkv'}, {'id': 11, 'path': ''}]
    with _no_cache(), patch('arr._arr_get', side_effect=get):
        paths, check = arr.fetch_arr_managed_paths({'MEDIA_PATH': ROOT, 'ARR_CONNECTIONS': [_SONARR]})
    assert check == 'ok' and len(paths) == 2
    assert '/data/media/tv/1/e1.mkv' in paths and '/data/media/tv/2/e1.mkv' in paths


def test_the_scan_asks_meanwhile_and_never_fails_on_the_answer():
    with patch('audit.fetch_arr_managed_paths', return_value=({'/x'}, 'ok')):
        answer, _ = audit._ask_arrs_meanwhile({})
        assert answer() == ({'/x'}, 'ok')
    with patch('audit.fetch_arr_managed_paths', side_effect=RuntimeError('boom')):
        answer, _ = audit._ask_arrs_meanwhile({})
        assert answer() == (None, 'unreachable')


def test_an_arr_slower_than_the_wait_is_skipped():
    release = threading.Event()

    def slow(_cfg):
        release.wait(5)
        return {'/x'}, 'ok'
    with patch('audit.fetch_arr_managed_paths', side_effect=slow), patch.object(audit, '_ARR_PATHS_WAIT', 0.05):
        answer, asking = audit._ask_arrs_meanwhile({})
        assert asking()
        assert answer() == (None, 'unreachable')
    release.set()


def test_the_check_rides_a_config_save_recompute():
    assert {'unmanaged_check', 'unmanaged_count'} <= set(audit.SCAN_ONLY_DETAILS)


# ── Backfill's own test ──────────────────────────────────────────────────────

def test_an_unseeded_video_is_unmanaged_exactly_when_backfill_cannot_search_it():
    """The point of the state: a video drawn Not seeded is one Backfill can
    search, and one drawn Unmanaged is one it can't. Same records, same arr
    rows, through both."""
    cfg = {'MEDIA_PATH': ROOT, 'ARR_CONNECTIONS': [_RADARR]}
    media = [_media('movies/A (2001)/A.mkv'), _media('movies/A.720p-GRP/A.720p.mkv'),
             _media('movies/B (2002)/B.mkv'), _media('movies/B (2002)/Extras/Trailer.mp4'),
             _media('movies/C (2003)/C.mkv', linked=True), _media('loose.mkv')]
    rows = [{'service': 'radarr', 'connection_id': 'radarr-mv', 'arr_id': i, 'title': t,
             'path': f'{ROOT}/{p}', 'arr_path': f'/movies/{p}', 'title_slug': t.lower(),
             'file_id': i, 'file_quality_name': 'WEBDL-1080p', 'file_hdr': ''}
            for i, (t, p) in enumerate([('A', 'movies/A (2001)/A.mkv'), ('B', 'movies/B (2002)/B.mkv'),
                                        ('C', 'movies/C (2003)/C.mkv')], 1)]
    with patch.object(app, 'db_load_file_results', return_value=[dict(m) for m in media]), \
         patch.object(app, 'fetch_arr_media_index_with_roots', return_value=(rows, [], {'radarr-mv': ['/movies']})):
        searchable = {g['path'] for g in app._resolve_backfill(cfg)['groups']}
    audit.stamp_unmanaged(media, ROOT, arr.ManagedPaths(arr.media_path_key(r['path']) for r in rows))
    not_seeded = {f['path'] for f in media if not f['linked_paths'] and not f.get('unmanaged')}
    assert not_seeded == searchable == {'movies/A (2001)/A.mkv', 'movies/B (2002)/B.mkv'}


# ── The map row ──────────────────────────────────────────────────────────────

def test_the_row_carries_unmanaged_as_its_eighth_sum():
    media = [_media('movies/Comet Run (1977)/Comet.Run.mkv', size=30 * GB, linked=True),
             dict(_media('movies/Comet Run (1977)/Featurettes/Making.Of.mkv', size=2 * GB), unmanaged=True),
             _media('movies/Wren (2009)/Wren.mkv', size=3 * GB)]
    row = audit.library_map_row(media, [])
    assert row['version'] == 3
    by = {t[1]: t[2:] for t in row['titles']}
    assert by['Comet Run (1977)'] == [2, 30 * GB, 0, 0, 0, 0, 0, 0, 2 * GB]
    assert by['Wren (2009)'] == [1, 0, 3 * GB, 0, 0, 0, 0, 0, 0]


def test_a_stamp_never_outranks_a_link_or_a_copy():
    """`_map_state_media`'s order: a stamp is read only on an unseeded file."""
    row = audit.library_map_row([dict(_media('m/A/a.mkv', linked=True), unmanaged=True),
                                 dict(_media('m/B/b.mkv', dups=['/t/b']), unmanaged=True)], [])
    by = {t[1]: t[3:] for t in row['titles']}
    assert by['A'][audit.MAP_LINKED] == GB and by['A'][audit.MAP_UNMANAGED] == 0
    assert by['B'][audit.MAP_COPY] == GB and by['B'][audit.MAP_UNMANAGED] == 0
    assert audit._map_state_media(dict(_media('m/C/c.mkv'), unmanaged=True)) == audit.MAP_UNMANAGED


# ── Where the scan stamps: as it builds the records ──────────────────────────

def test_the_scan_stamps_as_it_builds_the_records(tmp_path):
    """The real walk and `_assemble_records`, as `run_audit_process` runs them,
    and `stamp_unmanaged` on the same records: one answer."""
    from exclusions import compile_exclusions
    from media_server_exclusions import expand_exclusion_patterns
    media, torrents = tmp_path / 'media', tmp_path / 'torrents'
    files = {'movies/A (2001)/A.mkv': b'a' * 64, 'movies/A (2001)/A.en.srt': b's' * 8,
             'movies/Stray.720p/Stray.mkv': b'b' * 64, 'movies/Stray.720p/Stray.nfo': b'n' * 8,
             'movies/C (2003)/C.mkv': b'c' * 64}
    for rel, data in files.items():
        (media / rel).parent.mkdir(parents=True, exist_ok=True)
        (media / rel).write_bytes(data)
    (torrents / 'radarr').mkdir(parents=True)
    os.link(media / 'movies/C (2003)/C.mkv', torrents / 'radarr/C.mkv')
    managed = arr.ManagedPaths(arr.media_path_key(os.path.join(str(media), r))
                               for r in ('movies/A (2001)/A.mkv', 'movies/C (2003)/C.mkv'))
    expanded = expand_exclusion_patterns({'EXCLUSION_PATTERNS': []})
    compiled = compile_exclusions(expanded)
    inode_map = {}
    tko, _, _, _ = audit._walk_directory(str(torrents), 'Torrent', inode_map, {}, 0, 0,
                                         exclusion_patterns=expanded, compiled_exclusions=compiled)
    mko, _, _, _ = audit._walk_directory(str(media), 'Media', inode_map, {}, 0, 0,
                                         exclusion_patterns=expanded, compiled_exclusions=compiled)
    counts = {'unmanaged': 0}
    _, records = audit._assemble_records(tko, mko, inode_map, {}, compiled_exclusions=compiled,
                                         unmanaged=audit.unmanaged_test(managed, str(media)), counts=counts)
    stamped = {f['path'].replace(chr(92), '/') for f in records if f.get('unmanaged')}
    assert stamped == {'movies/Stray.720p/Stray.mkv', 'movies/Stray.720p/Stray.nfo'}
    assert counts['unmanaged'] == 2
    fresh = [{k: v for k, v in f.items() if k != 'unmanaged'} for f in records]
    assert audit.stamp_unmanaged(fresh, str(media), managed) == 2
    assert {f['path'].replace(chr(92), '/') for f in fresh if f.get('unmanaged')} == stamped


def test_the_quick_key_is_backfills_key():
    """A walked path skips `normpath`; the key it makes must be the one
    `media_path_key` makes of the joined path, whatever the root's spelling."""
    keys = []
    for root in ('/data/media', '/data/media/', '/data//media', '/'):
        rel = 'movies/A (2001)/A.mkv'
        held = arr.ManagedPaths([arr.media_path_key(os.path.join(root, rel))])
        keys.append(audit.unmanaged_test(held, root)(rel))
    assert keys == [False, False, False, False]
