"""Backfill says what actually happened (B9, B10, B12, B13).

* **B9** — "Of 2,612 unseeded files, 15 matched" could not tell a pile of
  subtitles (fine) from two hundred videos the arr does not know (a real,
  fixable path-mapping problem). The unmatched *videos* are counted apart.
* **B10** — the "Root Folders" chips were the first path segment below
  MEDIA_PATH; auditorr had never asked an arr for its root folders. They
  collapsed when the real roots sat deeper and were named after a directory
  rather than the thing configured in Sonarr/Radarr.
* **B12** — any grab failure re-searched and grabbed again, including a timeout
  on a grab the arr had in fact processed, which downloaded the release twice.
* **B13** — the smaller items: release searches only ever swept by their own
  endpoint, a cached search that dropped the quality filters, and a dead
  `limit` default waiting for a second caller.
"""
import inspect
import io
import json
import time
import urllib.error
from unittest.mock import MagicMock, patch

import app
import arr

_ENV = {'REMOTE_ADDR': '127.0.0.1'}

_TV = {'id': 'sonarr-tv', 'service': 'sonarr', 'name': 'TV',
       'base_url': 'http://tv:8989', 'api_key': 'a'}
_ANIME = {'id': 'sonarr-anime', 'service': 'sonarr', 'name': 'Anime',
          'base_url': 'http://anime:8989', 'api_key': 'b'}
_MOVIES = {'id': 'radarr-mv', 'service': 'radarr', 'name': 'Movies',
           'base_url': 'http://mv:7878', 'api_key': 'c'}


def _cfg(*conns):
    return {'MEDIA_PATH': '/data/media', 'ARR_CONNECTIONS': list(conns)}


def _file(conn_id, service, n, rel, arr_path):
    """One unseeded library file and its arr index row (local and arr-side paths)."""
    media = {'path': rel, 'size': 1000 + n, 'trackers': ['None']}
    row = {'service': service, 'connection_id': conn_id, 'arr_id': n, 'title': f'Title {n}',
           'path': f'/data/media/{rel}', 'arr_path': arr_path, 'title_slug': f't{n}',
           'file_id': n, 'season_number': 1 if service == 'sonarr' else None,
           'file_quality_name': 'WEBDL-1080p', 'file_hdr': ''}
    return media, row


def _patched(cfg, files, roots, extra_media=()):
    media = [m for m, _r in files] + list(extra_media)
    rows = [r for _m, r in files]
    return (patch.object(app, 'AUDITORR_SECRET', ''),
            patch.object(app, 'AUDITORR_REQUIRE_AUTH', False),
            patch.object(app, 'db_load_config', return_value=cfg),
            patch.object(app, 'db_load_file_results', return_value=media),
            # Rows, errors and roots from one snapshot, as `_resolve_backfill`
            # reads them since Phase 14 (S11). Repointed from three separate
            # patches of the fetch and its two accessors; fixtures unchanged.
            patch.object(app, 'fetch_arr_media_index_with_roots', return_value=(rows, [], roots)))


def _acquire(cfg, files, roots, extra_media=()):
    patches = _patched(cfg, files, roots, extra_media)
    for p in patches:
        p.start()
    try:
        return app.app.test_client().get('/api/workflows/acquire_candidates',
                                         environ_base=_ENV).get_json()
    finally:
        for p in reversed(patches):
            p.stop()


def _folders(body):
    return sorted(c['folder'] for c in body['candidates'])


# ── B9: unmatched videos are the number worth acting on ──────────────────────

def test_unmatched_videos_are_counted_apart_from_sidecars():
    files = [_file('radarr-mv', 'radarr', 1, 'movies/A/A.mkv', '/movies/A/A.mkv')]
    extra = [{'path': p, 'size': 1, 'trackers': ['None']} for p in (
        'movies/A/A.en.srt', 'movies/A/poster.jpg', 'movies/A/movie.nfo', 'movies/A/fanart.jpg',
        'movies/B/B.mkv', 'movies/C/C.M4V', 'tv/Show/Show.S01E01.mp4')]

    body = _acquire(_cfg(_MOVIES), files, {'radarr-mv': ['/movies']}, extra_media=extra)

    assert body['resolved_count'] == 1
    assert body['unresolved_count'] == 7
    assert body['unresolved_video_count'] == 3


# ── B10: the chips are the arrs' own root folders ────────────────────────────

def test_folder_chips_are_the_arrs_own_root_folders():
    files = [_file('sonarr-tv', 'sonarr', 1, 'tv/Show/S01E01.mkv', '/tv/Show/S01E01.mkv'),
             _file('radarr-mv', 'radarr', 2, 'movies/Film/Film.mkv', '/movies/Film/Film.mkv')]

    body = _acquire(_cfg(_TV, _MOVIES), files, {'sonarr-tv': ['/tv'], 'radarr-mv': ['/movies']})

    assert _folders(body) == ['/movies', '/tv']


def test_roots_deeper_than_one_level_are_not_collapsed():
    """The first-segment heuristic called both of these `tv`."""
    files = [
        _file('sonarr-tv', 'sonarr', 1, 'tv/anime/A/S01E01.mkv', '/data/media/tv/anime/A/S01E01.mkv'),
        _file('sonarr-tv', 'sonarr', 2, 'tv/western/B/S01E01.mkv', '/data/media/tv/western/B/S01E01.mkv'),
    ]

    body = _acquire(_cfg(_TV), files,
                    {'sonarr-tv': ['/data/media/tv/western', '/data/media/tv/anime']})

    assert _folders(body) == ['/data/media/tv/anime', '/data/media/tv/western']


def test_the_longest_matching_root_wins():
    files = [_file('sonarr-tv', 'sonarr', 1, 'tv/anime/A/S01E01.mkv',
                   '/data/media/tv/anime/A/S01E01.mkv')]

    body = _acquire(_cfg(_TV), files, {'sonarr-tv': ['/data/media/tv', '/data/media/tv/anime']})

    assert _folders(body) == ['/data/media/tv/anime']


def test_a_root_matches_whole_path_segments_only():
    files = [_file('sonarr-tv', 'sonarr', 1, 'tvshows/A/S01E01.mkv', '/tvshows/A/S01E01.mkv')]

    body = _acquire(_cfg(_TV), files, {'sonarr-tv': ['/tv']})

    assert _folders(body) == ['Other']


def test_two_instances_configuring_the_same_path_are_told_apart():
    files = [_file('sonarr-tv', 'sonarr', 1, 'tv/A/S01E01.mkv', '/tv/A/S01E01.mkv'),
             _file('sonarr-anime', 'sonarr', 2, 'anime/B/S01E01.mkv', '/tv/B/S01E01.mkv')]

    body = _acquire(_cfg(_TV, _ANIME), files, {'sonarr-tv': ['/tv'], 'sonarr-anime': ['/tv']})

    assert _folders(body) == ['/tv (Anime)', '/tv (TV)']


def test_a_file_under_no_known_root_is_other_not_a_guessed_root():
    files = [_file('radarr-mv', 'radarr', 1, 'movies/Film/Film.mkv', '/elsewhere/Film/Film.mkv')]

    body = _acquire(_cfg(_MOVIES), files, {'radarr-mv': ['/movies']})

    assert _folders(body) == ['Other']


def test_unreadable_root_folders_are_other_too():
    files = [_file('radarr-mv', 'radarr', 1, 'movies/Film/Film.mkv', '/movies/Film/Film.mkv')]

    body = _acquire(_cfg(_MOVIES), files, {'radarr-mv': None})

    assert _folders(body) == ['Other']


def test_a_run_filters_on_the_labels_the_page_shows():
    files = [_file('sonarr-tv', 'sonarr', 1, 'tv/Show/S01E01.mkv', '/tv/Show/S01E01.mkv'),
             _file('radarr-mv', 'radarr', 2, 'movies/Film/Film.mkv', '/movies/Film/Film.mkv')]
    cfg = _cfg(_TV, _MOVIES)
    patches = _patched(cfg, files, {'sonarr-tv': ['/tv'], 'radarr-mv': ['/movies']})
    for p in patches:
        p.start()
    try:
        groups = app._build_generate_candidates(cfg, folders=['/tv'])
    finally:
        for p in reversed(patches):
            p.stop()

    assert [g['arr_service'] for g in groups] == ['sonarr']


def _clear_index_cache():
    arr._arr_media_index_cache.update({'data': None, 'ts': 0, 'errors': [], 'roots': {}, 'snapshot': None})


def test_root_folders_ride_the_index_fetch_and_their_failure_does_not_fail_it():
    _clear_index_cache()

    def fake_get(_base, _key, path, **_kw):
        if path == '/api/v3/rootfolder':
            raise OSError('timed out')
        return [{'id': 1, 'title': 'Film', 'movieFile': {'id': 9, 'path': '/movies/Film/Film.mkv'}}]

    with patch('arr._arr_get', side_effect=fake_get):
        media, errors, roots = arr.fetch_arr_media_index_with_roots(_cfg(_MOVIES), force=True)

    assert len(media) == 1
    assert errors == []
    assert roots == {'radarr-mv': None}
    _clear_index_cache()


def test_root_folder_paths_are_normalised_longest_first():
    _clear_index_cache()

    def fake_get(_base, _key, path, **_kw):
        if path == '/api/v3/rootfolder':
            return [{'path': '/tv/'}, {'path': 'D:\\Media\\Anime\\'}, {'path': '/tv/anime/'}]
        return []

    with patch('arr._arr_get', side_effect=fake_get):
        _media, _errors, roots = arr.fetch_arr_media_index_with_roots(_cfg(_MOVIES), force=True)

    assert roots == {'radarr-mv': ['D:/Media/Anime', '/tv/anime', '/tv']}
    _clear_index_cache()


# ── B12: a grab is retried only when a retry is what the failure asks for ────

_GRAB = {'service': 'sonarr', 'connection_id': 'sonarr-tv', 'guid': 'g', 'indexer_id': 3}


def _http_error(code, message):
    return urllib.error.HTTPError('http://tv:8989/api/v3/release', code, 'err', {},
                                  io.BytesIO(json.dumps({'message': message}).encode()))


def _grab(body, grab=None, queue=None):
    grab = grab or MagicMock(return_value={})
    with patch.object(app, 'AUDITORR_SECRET', ''), \
         patch.object(app, 'AUDITORR_REQUIRE_AUTH', False), \
         patch.object(app, 'db_load_config', return_value=_cfg(_TV)), \
         patch.object(app, 'grab_release', grab), \
         patch.object(app, 'queue_records_for_item', return_value=queue) as check:
        res = app.app.test_client().post('/api/workflows/grab_release', json=body,
                                         environ_base=_ENV)
    return res, grab, check


def test_a_stale_release_is_named_so_the_page_can_search_again():
    """Both arrs answer a guid that fell out of their release cache with a 404
    and this message. That — and only that — is what a re-search fixes."""
    err = _http_error(404, "Couldn't find requested release in cache, try searching again")

    res, _grab_mock, _check = _grab(_GRAB, grab=MagicMock(side_effect=err))

    assert res.get_json()['code'] == 'stale_release'


def test_any_other_failure_is_surfaced_without_a_retry_code():
    """A timeout is the worst one to retry: the arr may have processed the grab,
    and an automatic second grab downloads the release twice."""
    for exc in (_http_error(500, 'Indexer returned an error'), OSError('timed out')):
        res, _grab_mock, _check = _grab(_GRAB, grab=MagicMock(side_effect=exc))
        assert res.status_code == 400
        assert not res.get_json().get('code')


def test_a_candidate_already_in_the_queue_is_not_grabbed_twice():
    res, grab, check = _grab({**_GRAB, 'arr_id': 1, 'episode_ids': [101], 'season_number': 1},
                             queue=[{'title': 'Show.S01E01.1080p-GRP'}])

    assert res.status_code == 409
    assert res.get_json()['code'] == 'already_queued'
    grab.assert_not_called()
    assert check.call_args.kwargs['episode_ids'] == [101]


def test_grab_anyway_skips_the_queue_check():
    res, grab, check = _grab({**_GRAB, 'arr_id': 1, 'force': True}, queue=[{'title': 'x'}])

    assert res.status_code == 200
    grab.assert_called_once()
    check.assert_not_called()


def test_an_unreadable_queue_does_not_block_the_grab_and_says_so():
    res, grab, _check = _grab({**_GRAB, 'arr_id': 1}, queue=None)

    assert res.status_code == 200
    grab.assert_called_once()
    assert res.get_json()['queue_checked'] is False


def _queue_records(records, fn):
    with patch('arr._arr_get', return_value={'records': records}):
        return fn()


def test_the_queue_check_matches_episodes_not_the_whole_series():
    recs = [{'seriesId': 1, 'episodeId': 101, 'seasonNumber': 1, 'title': 'mine'},
            {'seriesId': 1, 'episodeId': 205, 'seasonNumber': 2, 'title': 'next season'},
            {'seriesId': 2, 'episodeId': 101, 'seasonNumber': 1, 'title': 'other series'}]

    by_episode = _queue_records(recs, lambda: arr.queue_records_for_item(
        _cfg(_TV), 'sonarr', 'sonarr-tv', 1, episode_ids=[101]))
    by_season = _queue_records(recs, lambda: arr.queue_records_for_item(
        _cfg(_TV), 'sonarr', 'sonarr-tv', 1, season_number=2))

    assert [r['title'] for r in by_episode] == ['mine']
    assert [r['title'] for r in by_season] == ['next season']


def test_the_queue_check_for_a_movie_matches_the_movie():
    recs = [{'movieId': 7, 'title': 'mine'}, {'movieId': 8, 'title': 'other'}]

    found = _queue_records(recs, lambda: arr.queue_records_for_item(
        _cfg(_MOVIES), 'radarr', 'radarr-mv', 7))

    assert [r['title'] for r in found] == ['mine']


def test_a_failed_queue_entry_is_not_a_queued_download():
    recs = [{'movieId': 7, 'status': 'failed', 'title': 'dead'}]

    assert _queue_records(recs, lambda: arr.queue_records_for_item(
        _cfg(_MOVIES), 'radarr', 'radarr-mv', 7)) == []


def test_an_unreadable_queue_is_none_not_empty():
    with patch('arr._arr_get', side_effect=OSError('down')):
        assert arr.queue_records_for_item(_cfg(_MOVIES), 'radarr', 'radarr-mv', 7) is None


# ── B13 ──────────────────────────────────────────────────────────────────────

def test_expired_jobs_are_swept_by_the_poll_every_page_makes():
    """`_release_jobs` used to be swept only from inside its own endpoint, so an
    entry lived until the next release search — which may never come."""
    now = time.time()
    app._release_jobs['stale'] = {'status': 'done', 'releases': [], 'ts': now - 10_000}
    app._gen_jobs['old'] = {'id': 'old', 'status': 'done', 'finished_at': now - 10_000}

    with patch.object(app, 'AUDITORR_SECRET', ''), patch.object(app, 'AUDITORR_REQUIRE_AUTH', False):
        app.app.test_client().get('/api/workflows/watch_import/active', environ_base=_ENV)

    assert 'stale' not in app._release_jobs
    assert 'old' not in app._gen_jobs


def test_a_cached_release_search_applies_the_quality_filters_too():
    rows = [{'title': 'a.2160p', 'size': 1, 'indexer': 'ix', 'resolution': 2160,
             'quality_name': 'WEBDL-2160p', 'hdr': '', 'seeders': 1},
            {'title': 'b.1080p', 'size': 2, 'indexer': 'ix', 'resolution': 1080,
             'quality_name': 'WEBDL-1080p', 'hdr': '', 'seeders': 1}]
    key = app._release_job_key('radarr', 'radarr-mv', 7, None, None, None)
    app._release_jobs[key] = {'status': 'done', 'releases': rows, 'ts': time.time()}
    try:
        with patch.object(app, 'AUDITORR_SECRET', ''), \
             patch.object(app, 'AUDITORR_REQUIRE_AUTH', False), \
             patch.object(app, 'db_load_config', return_value={}):
            body = app.app.test_client().get(
                '/api/workflows/acquire_releases?service=radarr&connection_id=radarr-mv'
                '&arr_id=7&res_filter=2160p', environ_base=_ENV).get_json()
    finally:
        app._release_jobs.pop(key, None)

    assert [r['title'] for r in body['releases']] == ['a.2160p']


def test_the_candidate_builder_has_no_dead_limit():
    assert 'limit' not in inspect.signature(app._build_generate_candidates).parameters


# ── S11: rows, errors and root folders from one snapshot (Phase 14) ──────────
#
# Phase 9 closed S11 for Triage and Trumped; Backfill kept reading the accessors
# straight after its fetch. Under gunicorn's eight threads another request's
# refresh can land between the two, and the accessors then describe a library
# this request never received. The pattern is Triage's own test
# (`ArrResultPairingTests.test_arr_errors_describe_the_rows_they_came_with`):
# the accessor itself triggers the interleaving refresh.

def test_backfill_reads_rows_errors_and_roots_from_one_snapshot():
    media = [{'path': 'movies/Film/Film.mkv', 'size': 1000, 'trackers': ['None']}]
    film = [{'id': 7, 'title': 'Film', 'titleSlug': 'film',
             'movieFile': {'id': 70, 'path': '/data/media/movies/Film/Film.mkv',
                           'quality': {'quality': {'name': 'WEBDL-1080p'}}}}]

    def arr_answering(roots, fail=False):
        def fake_get(_base, _key, path, **_kw):
            if path == '/api/v3/rootfolder':
                return [{'path': r} for r in roots]
            if fail:
                raise OSError('timed out')
            return film
        return fake_get

    real = {name: getattr(arr, name) for name in ('arr_media_index_errors', 'arr_root_folders')}

    def refresh_then(accessor):
        def read():
            # Another request's successful refresh, landing mid-request.
            with patch('arr._arr_get', side_effect=arr_answering(['/elsewhere'])):
                arr.fetch_arr_media_index(_cfg(_MOVIES), force=True)
            return real[accessor]()
        return read

    def acquire(fail):
        _clear_index_cache()
        # The interleaving fires on an accessor read by either spelling — the name
        # `app` imported, or the `arr` module's own attribute. Patching only the
        # first let a guard mutation that read `arr.arr_media_index_errors`
        # directly pass this test.
        with patch.object(app, 'AUDITORR_SECRET', ''), \
             patch.object(app, 'AUDITORR_REQUIRE_AUTH', False), \
             patch.object(app, 'db_load_config', return_value=_cfg(_MOVIES)), \
             patch.object(app, 'db_load_file_results', return_value=media), \
             patch('arr._arr_get', side_effect=arr_answering(['/data/media/movies'], fail=fail)), \
             patch('app.arr_media_index_errors', side_effect=refresh_then('arr_media_index_errors'),
                   create=True), \
             patch('app.arr_root_folders', side_effect=refresh_then('arr_root_folders'), create=True), \
             patch('arr.arr_media_index_errors', side_effect=refresh_then('arr_media_index_errors')), \
             patch('arr.arr_root_folders', side_effect=refresh_then('arr_root_folders')):
            return app.app.test_client().get('/api/workflows/acquire_candidates',
                                             environ_base=_ENV).get_json()

    try:
        failed = acquire(fail=True)
        answered = acquire(fail=False)
    finally:
        _clear_index_cache()

    # The request's own fetch failed: its errors say so, whatever landed since.
    assert [e['connection_id'] for e in failed['arr_errors']] == ['radarr-mv']
    # The request's own fetch read `/data/media/movies` as the root, and its
    # candidate is labelled with it — not with a root another request fetched.
    assert [c['folder'] for c in answered['candidates']] == ['/data/media/movies']
    assert answered['arr_errors'] == []


# ── B6: a candidate the server accepted a grab for is not offered again ──────

def test_generate_skips_the_candidates_the_page_already_grabbed():
    """The page keeps the keys of candidates whose grab the server accepted until
    the audit lands. It can hide them from its own list, but a run builds its
    candidates server-side — so the keys ride the request, or the grabbed
    candidate is searched and offered again on the very next run."""
    files = [_file('radarr-mv', 'radarr', 1, 'movies/A/A.mkv', '/movies/A/A.mkv'),
             _file('radarr-mv', 'radarr', 2, 'movies/B/B.mkv', '/movies/B/B.mkv')]
    patches = _patched(_cfg(_MOVIES), files, {'radarr-mv': ['/movies']})
    for p in patches:
        p.start()
    try:
        with patch.object(app, 'fetch_release_matrix', return_value=[]):
            client = app.app.test_client()
            res = client.post('/api/workflows/generate', environ_base=_ENV,
                              json={'count': 10, 'exclude_keys': ['radarr-mv_1']})
            assert res.status_code == 200, res.get_json()
            job_id = res.get_json()['job_id']
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                body = client.get(f'/api/workflows/generate/status?job_id={job_id}',
                                  environ_base=_ENV).get_json()
                if body['status'] != 'running':
                    break
                time.sleep(0.01)
    finally:
        for p in reversed(patches):
            p.stop()

    assert body['total'] == 1
    assert [r['key'] for r in body['results']] == ['radarr-mv_2']


# ── B9's optional half: one unmatched video beside the arr's own path ────────

def test_an_unmatched_video_is_shown_beside_the_arrs_file_of_the_same_name():
    """The mismatch is usually obvious the moment the two paths are on screen
    together (BACKFILL B9). The arr's path rides the media index this request
    already fetched: a file of the same name it holds somewhere else."""
    files = [_file('radarr-mv', 'radarr', 1, 'movies/A/A.mkv', '/movies/A/A.mkv')]
    # The arr holds B.mkv, reported at a path that maps nowhere under MEDIA_PATH.
    stray = dict(_file('radarr-mv', 'radarr', 2, 'x', '/movies/B/B.mkv')[1],
                 path='/mnt/other/movies/B/B.mkv')
    extra = [{'path': 'movies/B/B.mkv', 'size': 1, 'trackers': ['None']},
             {'path': 'movies/A/A.en.srt', 'size': 1, 'trackers': ['None']}]
    patches = _patched(_cfg(_MOVIES), files + [({'path': 'unused', 'trackers': ['t']}, stray)],
                       {'radarr-mv': ['/movies']}, extra_media=extra)
    for p in patches:
        p.start()
    try:
        body = app.app.test_client().get('/api/workflows/acquire_candidates',
                                         environ_base=_ENV).get_json()
    finally:
        for p in reversed(patches):
            p.stop()

    assert body['unresolved_video_count'] == 1
    assert body['unmatched_example'] == {
        'path': 'movies/B/B.mkv', 'arr_path': '/movies/B/B.mkv',
        'service': 'radarr', 'connection_name': 'Movies'}


def test_no_example_is_shown_where_no_arr_file_shares_the_name():
    """A video the arr does not track at all has no counterpart to show, and a
    guess from its title would be a claim the page cannot back."""
    files = [_file('radarr-mv', 'radarr', 1, 'movies/A/A.mkv', '/movies/A/A.mkv')]
    extra = [{'path': 'movies/Untracked/Untracked.mkv', 'size': 1, 'trackers': ['None']}]

    body = _acquire(_cfg(_MOVIES), files, {'radarr-mv': ['/movies']}, extra_media=extra)

    assert body['unresolved_video_count'] == 1
    assert body.get('unmatched_example') is None
