"""Backfill against several Sonarr/Radarr instances.

Three faults that only appear once a second instance of the same service exists,
all of which end the same way — candidates and their folder chips silently
missing from the page:

  * series ids are per-instance, so grouping episodes by a bare id merged
    unrelated shows and searched the loser's episodes against the winner's
    series, on the winner's instance;
  * Sonarr needs one HTTP call per series where Radarr needs one in total, and
    re-raising the first failure discarded every other series' files, reporting
    a whole working instance as managing nothing;
  * an instance whose index failed is indistinguishable from one managing
    nothing, so its absence had to be inferred rather than read.
"""
import arr
import app as app_module
from arr import fetch_arr_media_index, arr_media_index_errors
from unittest.mock import patch


def _two_sonarrs():
    return {
        'MEDIA_PATH': '/data/media',
        'ARR_CONNECTIONS': [
            {'id': 'sonarr-tv', 'service': 'sonarr', 'name': 'TV',
             'base_url': 'http://tv:8989', 'api_key': 'a',
             'media_path': '/tv', 'local_media_path': '/data/media/tv'},
            {'id': 'sonarr-anime', 'service': 'sonarr', 'name': 'Anime',
             'base_url': 'http://anime:8989', 'api_key': 'b',
             'media_path': '/anime', 'local_media_path': '/data/media/anime'},
        ],
    }


def _clear_index_cache():
    arr._arr_media_index_cache.update({'data': None, 'ts': 0, 'errors': []})


# ── Series-id collision ───────────────────────────────────────────────────────

def test_same_series_id_on_two_sonarrs_stays_two_candidates():
    """Both instances number their first series 1. A bare id merges them."""
    media_files = [
        {'path': 'tv/Show/Show.S01E01.mkv',     'size': 10, 'trackers': ['None']},
        {'path': 'anime/Anime/Anime.S01E01.mkv', 'size': 20, 'trackers': ['None']},
    ]
    arr_media = [
        {'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1,
         'title': 'Show', 'path': '/data/media/tv/Show/Show.S01E01.mkv',
         'title_slug': 'show', 'episode_ids': [11]},
        {'service': 'sonarr', 'connection_id': 'sonarr-anime', 'arr_id': 1,
         'title': 'Anime', 'path': '/data/media/anime/Anime/Anime.S01E01.mkv',
         'title_slug': 'anime', 'episode_ids': [21]},
    ]
    with patch.object(app_module, 'db_load_file_results', return_value=media_files), \
         patch.object(app_module, 'fetch_arr_media_index', return_value=arr_media):
        groups = app_module._build_generate_candidates(_two_sonarrs(), limit=None)

    assert len(groups) == 2, 'the anime series was absorbed into the TV series'
    assert {g['arr_connection_id'] for g in groups} == {'sonarr-tv', 'sonarr-anime'}
    # Each group must still point at its own instance — a merged group keeps the
    # winner's connection and searches the loser's episodes on the wrong Sonarr.
    by_conn = {g['arr_connection_id']: g for g in groups}
    assert by_conn['sonarr-anime']['arr_title'] == 'Anime'
    assert by_conn['sonarr-anime']['episode_id'] == 21


def test_both_instances_contribute_a_root_folder():
    """The reported symptom: folder chips are derived from resolved candidates,
    so a merged group takes its folder off the page with it."""
    media_files = [
        {'path': 'tv/Show/Show.S01E01.mkv',      'size': 10, 'trackers': ['None']},
        {'path': 'anime/Anime/Anime.S01E01.mkv', 'size': 20, 'trackers': ['None']},
    ]
    arr_media = [
        {'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1,
         'title': 'Show', 'path': '/data/media/tv/Show/Show.S01E01.mkv'},
        {'service': 'sonarr', 'connection_id': 'sonarr-anime', 'arr_id': 1,
         'title': 'Anime', 'path': '/data/media/anime/Anime/Anime.S01E01.mkv'},
    ]
    with patch.object(app_module, 'db_load_file_results', return_value=media_files), \
         patch.object(app_module, 'fetch_arr_media_index', return_value=arr_media):
        groups = app_module._build_generate_candidates(_two_sonarrs(), limit=None)

    folders = {app_module._gen_root_folder(g['rep_path']) for g in groups}
    assert folders == {'tv', 'anime'}


# ── One bad series must not discard the instance ──────────────────────────────

def test_one_unreachable_series_does_not_discard_the_others():
    series = [{'id': n, 'title': f'Show {n}', 'year': 2024} for n in (1, 2, 3)]

    def fake_get(_base, _key, path, **_kw):
        if path == '/api/v3/series':
            return series
        if path == '/api/v3/episodefile?seriesId=2':
            raise OSError('timed out')
        sid = path.rsplit('=', 1)[-1]
        return [{'id': int(sid) * 10, 'path': f'/tv/Show {sid}/S01E01.mkv'}]

    conn = {'id': 'sonarr-tv', 'name': 'TV', 'service': 'sonarr',
            'base_url': 'http://tv:8989', 'api_key': 'a'}
    with patch('arr._arr_get', side_effect=fake_get):
        rows = arr._fetch_sonarr_media(conn)

    assert sorted(r['arr_id'] for r in rows) == [1, 3]


# ── A failed instance is reported, not inferred ──────────────────────────────

def test_failed_instance_is_named_in_the_index_errors():
    _clear_index_cache()

    def fake_get(base_url, _key, path, **_kw):
        if 'anime' in base_url:
            raise OSError('timed out')
        if path == '/api/v3/series':
            return [{'id': 1, 'title': 'Show', 'year': 2024}]
        return [{'id': 11, 'path': '/tv/Show/S01E01.mkv'}]

    with patch('arr._arr_get', side_effect=fake_get):
        media = fetch_arr_media_index(_two_sonarrs(), force=True)

    assert [m['connection_id'] for m in media] == ['sonarr-tv']
    errors = arr_media_index_errors()
    assert [e['connection_id'] for e in errors] == ['sonarr-anime']
    assert errors[0]['name'] == 'Anime'
    assert 'timed out' in errors[0]['message']
    _clear_index_cache()


def test_a_healthy_fetch_clears_a_previous_failure():
    _clear_index_cache()

    def failing(_base, _key, _path, **_kw):
        raise OSError('down')

    def healthy(_base, _key, path, **_kw):
        if path == '/api/v3/series':
            return [{'id': 1, 'title': 'Show', 'year': 2024}]
        return [{'id': 11, 'path': '/tv/Show/S01E01.mkv'}]

    with patch('arr._arr_get', side_effect=failing):
        fetch_arr_media_index(_two_sonarrs(), force=True)
    assert len(arr_media_index_errors()) == 2

    with patch('arr._arr_get', side_effect=healthy):
        fetch_arr_media_index(_two_sonarrs(), force=True)
    assert arr_media_index_errors() == []
    _clear_index_cache()
