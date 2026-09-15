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
    arr._arr_media_index_cache.update({'data': None, 'ts': 0, 'errors': [], 'roots': {}, 'snapshot': None})


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
        groups = app_module._build_generate_candidates(_two_sonarrs())

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
    roots = {'sonarr-tv': ['/data/media/tv'], 'sonarr-anime': ['/data/media/anime']}
    with patch.object(app_module, 'db_load_file_results', return_value=media_files), \
         patch.object(app_module, 'fetch_arr_media_index', return_value=arr_media), \
         patch.object(app_module, 'arr_root_folders', return_value=roots):
        groups = app_module._build_generate_candidates(_two_sonarrs())

    # The arrs' own root folders since B10, not the first segment below MEDIA_PATH.
    assert {g['folder'] for g in groups} == {'/data/media/tv', '/data/media/anime'}


# ── Season grouping keys on Sonarr's number, not the filename (B8) ───────────

def _single_sonarr():
    return {
        'MEDIA_PATH': '/data/media',
        'ARR_CONNECTIONS': [
            {'id': 'sonarr-tv', 'service': 'sonarr', 'name': 'TV',
             'base_url': 'http://tv:8989', 'api_key': 'a'},
        ],
    }


def test_unparseable_episode_names_do_not_merge_seasons():
    """The regex failure is not a missing season, it is a **merge**.

    Daily series, anime absolute numbering, "S01.E02" and any non-standard
    renamer all leave `_gen_parse_season` with None, and every such episode of a
    series then collapses into one `{conn}_{id}_SNone` candidate — which falls
    to the episode_id branch and searches for **one** episode while the row
    reads "N ep". Sonarr reports `seasonNumber` on the same episode-file record
    the index is built from.
    """
    media_files = [
        {'path': 'tv/Show/Show - 2024-01-05.mkv', 'size': 10, 'trackers': ['None']},
        {'path': 'tv/Show/Show - 2025-02-09.mkv', 'size': 20, 'trackers': ['None']},
    ]
    arr_media = [
        {'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1, 'title': 'Show',
         'path': '/data/media/tv/Show/Show - 2024-01-05.mkv',
         'season_number': 1, 'episode_numbers': [5], 'episode_ids': [105]},
        {'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1, 'title': 'Show',
         'path': '/data/media/tv/Show/Show - 2025-02-09.mkv',
         'season_number': 2, 'episode_numbers': [9], 'episode_ids': [209]},
    ]
    with patch.object(app_module, 'db_load_file_results', return_value=media_files), \
         patch.object(app_module, 'fetch_arr_media_index', return_value=arr_media):
        groups = app_module._build_generate_candidates(_single_sonarr())

    assert len(groups) == 2, 'two seasons merged into one candidate'
    assert sorted(g['season_number'] for g in groups) == [1, 2]
    assert all(g['file_count'] == 1 for g in groups)


def test_the_filename_regex_is_still_the_fallback():
    """Rows the arr did not supply a season number on — an index written before
    this shipped, or any future row that lacks the key."""
    media_files = [
        {'path': 'tv/Show/Show.S01E01.mkv', 'size': 10, 'trackers': ['None']},
        {'path': 'tv/Show/Show.S02E01.mkv', 'size': 20, 'trackers': ['None']},
    ]
    arr_media = [
        {'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1, 'title': 'Show',
         'path': '/data/media/tv/Show/Show.S01E01.mkv'},
        {'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1, 'title': 'Show',
         'path': '/data/media/tv/Show/Show.S02E01.mkv'},
    ]
    with patch.object(app_module, 'db_load_file_results', return_value=media_files), \
         patch.object(app_module, 'fetch_arr_media_index', return_value=arr_media):
        groups = app_module._build_generate_candidates(_single_sonarr())

    assert sorted(g['season_number'] for g in groups) == [1, 2]


def test_a_whole_season_still_groups_into_one_candidate():
    """The grouping must not be lost to the fix — a season's episodes are one
    candidate, which is the unit the Search Depth readout counts."""
    media_files = [{'path': f'tv/Show/Show - 2024-01-0{n}.mkv', 'size': 10,
                    'trackers': ['None']} for n in (1, 2, 3)]
    arr_media = [{'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1,
                  'title': 'Show', 'path': f'/data/media/tv/Show/Show - 2024-01-0{n}.mkv',
                  'season_number': 1, 'episode_numbers': [n], 'episode_ids': [100 + n]}
                 for n in (1, 2, 3)]
    with patch.object(app_module, 'db_load_file_results', return_value=media_files), \
         patch.object(app_module, 'fetch_arr_media_index', return_value=arr_media):
        groups = app_module._build_generate_candidates(_single_sonarr())

    assert len(groups) == 1
    assert groups[0]['file_count'] == 3
    assert groups[0]['season_number'] == 1


def test_acquire_candidates_ships_the_season_so_the_client_can_agree():
    """The grouping key exists in three places (CLAUDE.md flags two of them).
    If the server keys on arr data while the client regexes the filename, the
    two disagree on exactly the library this change is for."""
    media_files = [{'path': 'tv/Show/Show - 2024-01-05.mkv', 'size': 10, 'trackers': ['None']}]
    arr_media = [{'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1,
                  'title': 'Show', 'path': '/data/media/tv/Show/Show - 2024-01-05.mkv',
                  'season_number': 4, 'episode_numbers': [5], 'episode_ids': [405]}]
    with patch.object(app_module, 'db_load_config', return_value=_single_sonarr()), \
         patch.object(app_module, 'db_load_file_results', return_value=media_files), \
         patch.object(app_module, 'fetch_arr_media_index', return_value=arr_media), \
         patch.object(app_module, 'arr_media_index_errors', return_value=[]):
        body = app_module.app.test_client().get('/api/workflows/acquire_candidates').get_json()

    assert body['candidates'][0]['season_number'] == 4


# ── One bad series must not discard the instance ──────────────────────────────

def _one_series_fails():
    series = [{'id': n, 'title': f'Show {n}', 'year': 2024} for n in (1, 2, 3)]

    def fake_get(_base, _key, path, **_kw):
        if path == '/api/v3/series':
            return series
        if path == '/api/v3/episodefile?seriesId=2':
            raise OSError('timed out')
        sid = path.rsplit('=', 1)[-1]
        return [{'id': int(sid) * 10, 'path': f'/tv/Show {sid}/S01E01.mkv',
                 'seasonNumber': 1, 'episodeNumbers': [1]}]

    return fake_get


def test_one_unreachable_series_does_not_discard_the_others():
    conn = {'id': 'sonarr-tv', 'name': 'TV', 'service': 'sonarr',
            'base_url': 'http://tv:8989', 'api_key': 'a'}
    with patch('arr._arr_get', side_effect=_one_series_fails()):
        rows, _partial = arr._fetch_sonarr_media(conn)

    assert sorted(r['arr_id'] for r in rows) == [1, 3]


def test_a_partial_series_failure_is_returned_not_only_logged():
    """The count was computed and thrown away at a log.error.

    Sonarr needs one call per series, so a partial failure is both likelier
    than a total one and — until it came back as a value — completely silent:
    the survivors are a flat list, indistinguishable from a library that simply
    has no files for those series.
    """
    conn = {'id': 'sonarr-tv', 'name': 'TV', 'service': 'sonarr',
            'base_url': 'http://tv:8989', 'api_key': 'a'}
    with patch('arr._arr_get', side_effect=_one_series_fails()):
        _rows, partial = arr._fetch_sonarr_media(conn)

    assert partial == {'failed': 1, 'total': 3}


def test_a_clean_fetch_reports_no_partial():
    def fake_get(_base, _key, path, **_kw):
        if path == '/api/v3/series':
            return [{'id': 1, 'title': 'Show', 'year': 2024}]
        return [{'id': 11, 'path': '/tv/Show/S01E01.mkv'}]

    conn = {'id': 'sonarr-tv', 'name': 'TV', 'service': 'sonarr',
            'base_url': 'http://tv:8989', 'api_key': 'a'}
    with patch('arr._arr_get', side_effect=fake_get):
        rows, partial = arr._fetch_sonarr_media(conn)

    assert len(rows) == 1 and partial is None


def test_a_partial_instance_reaches_the_index_errors_channel():
    """A half-read instance lands on the same channel a dead one does — the
    consequence is the same shape (an unexplained gap), and `partial` is what
    lets the UI say which happened."""
    _clear_index_cache()
    cfg = {'MEDIA_PATH': '/data/media',
           'ARR_CONNECTIONS': [{'id': 'sonarr-tv', 'service': 'sonarr', 'name': 'TV',
                                'base_url': 'http://tv:8989', 'api_key': 'a'}]}
    with patch('arr._arr_get', side_effect=_one_series_fails()):
        media = fetch_arr_media_index(cfg, force=True)

    assert len(media) == 2, 'the two readable series must still be indexed'
    errors = arr_media_index_errors()
    assert len(errors) == 1
    assert errors[0]['partial'] is True
    assert (errors[0]['failed'], errors[0]['total']) == (1, 3)
    assert '1 of 3' in errors[0]['message']
    _clear_index_cache()


def test_sonarr_season_and_episode_numbers_are_carried_off_the_record():
    """B8 — the episode-file record already says which season it is in."""
    _clear_index_cache()
    cfg = {'MEDIA_PATH': '/data/media',
           'ARR_CONNECTIONS': [{'id': 'sonarr-tv', 'service': 'sonarr', 'name': 'TV',
                                'base_url': 'http://tv:8989', 'api_key': 'a'}]}

    def fake_get(_base, _key, path, **_kw):
        if path == '/api/v3/series':
            return [{'id': 1, 'title': 'Show', 'year': 2024}]
        # Daily-series naming: nothing here for the SxxExx regex to find.
        return [{'id': 11, 'path': '/tv/Show/Show - 2024-01-05.mkv',
                 'seasonNumber': 3, 'episodeNumbers': [7], 'episodeIds': [70]}]

    with patch('arr._arr_get', side_effect=fake_get):
        media = fetch_arr_media_index(cfg, force=True)

    assert media[0]['season_number'] == 3
    assert media[0]['episode_numbers'] == [7]
    _clear_index_cache()


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
