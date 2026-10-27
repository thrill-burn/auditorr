"""Triage T25: *Find the season pack*.

At the end of a season a good tracker kills its single episodes and posts a
pack. The path from Triage's dead seeds (or Backfill's unseeded episodes) to
that pack in the library: look for it in the client, else search Sonarr, grab
it, follow the download, and hand T22's replace the torrent once it's in. The
watch never force-imports anything; the replace is the user's to confirm.

Library files are real files in `tmp_path`, so `linked` is a real `st_nlink`.
"""
import os
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

import app

CONN = {'id': 'tv', 'service': 'sonarr', 'name': 'TV', 'base_url': 'http://sonarr:8989',
        'api_key': 'k', 'remote_path': '', 'media_path': '', 'local_media_path': ''}
SERIES = {'service': 'sonarr', 'connection_id': 'tv', 'arr_id': 143, 'title': 'Small Prophets',
          'year': 2025, 'title_slug': 'small-prophets', 'has_file': True, 'alt_titles': ['Kleine Propheten']}


def _row(name, h, progress=1.0, instance_id=1, tracker='aither.cc'):
    return {'reg': f'{instance_id}:{h}', 'hash': h, 'name': name, 'size': 5_000_000_000,
            'save_path': '/data/torrents/tv', 'content_path': f'/data/torrents/tv/{name}',
            'progress': progress, 'completion_on': 1 if progress >= 1.0 else 0,
            'tracker': tracker, 'instance_id': instance_id, 'instance_name': 'main'}


@pytest.fixture
def season(tmp_path):
    """The box's Small Prophets S01. E01 and E02 2160p and hardlinked to their
    own torrents; E03–E06 BluRay-1080p with nothing else linking them."""
    media = tmp_path / 'media' / 'Small Prophets' / 'Season 1'
    media.mkdir(parents=True)
    single = tmp_path / 'torrents'
    single.mkdir()
    index = []
    for n in range(1, 7):
        lib = media / f'Small.Prophets.S01E{n:02d}.mkv'
        lib.write_bytes(b'x')
        if n <= 2:
            os.link(lib, single / lib.name)
        index.append({'connection_id': 'tv', 'service': 'sonarr', 'arr_id': 143, 'file_id': 7110 + n,
                      'path': str(lib), 'file_quality_name': 'WEBDL-2160p' if n <= 2 else 'Bluray-1080p'})
    # E07 is announced and has no file yet.
    episodes = [{'id': 500 + n, 'season': 1, 'episode': n, 'file_id': 7110 + n if n <= 6 else 0}
                for n in range(1, 8)]
    episodes.append({'id': 600, 'season': 2, 'episode': 1, 'file_id': 0})
    return {'index': index, 'episodes': episodes}


def _patched(season, **over):
    s = {'rows': [], 'report': {'instances_failed': []}, 'titles': [SERIES], 'title_errors': [],
         **season, **over}
    return [
        patch.object(app, 'db_load_config', return_value={'TORRENT_SOURCE': 'qui'}),
        patch.object(app, 'normalize_arr_connections', side_effect=lambda cfg, **kw: [CONN]),
        patch.object(app, 'sonarr_series_episodes', return_value=s['episodes']),
        patch.object(app, 'fetch_arr_media_index_result', return_value=(s['index'], [])),
        patch.object(app, 'fetch_arr_all_titles_result', return_value=(s['titles'], s['title_errors'])),
        patch.object(app.sources, 'list_torrents_detailed', return_value=(s['rows'], s['report'])),
    ]


def _post(url, season, body=None, extra=(), **over):
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in [*_patched(season, **over), *extra]:
            stack.enter_context(p)
        payload = {'connection_id': 'tv', 'arr_id': 143, 'season': 1, **(body or {})}
        resp = app.app.test_client().post(url, json=payload)
        return resp.status_code, resp.get_json()


# ═════════════════════════════════════════════════════════════════════════════
# Lookup — the season in the library, and a pack already in the client
# ═════════════════════════════════════════════════════════════════════════════

class TestLookup:

    def test_the_library_says_which_episodes_are_seeded(self, season):
        status, body = _post('/api/workflows/season_pack/lookup', season)
        assert status == 200
        lib = {e['episode']: e for e in body['library']}
        assert sorted(lib) == [1, 2, 3, 4, 5, 6, 7]
        assert [lib[n]['linked'] for n in range(1, 7)] == [True, True, False, False, False, False]
        assert (lib[7]['has_file'], lib[7]['linked']) == (False, None)
        assert lib[3]['quality'] == 'Bluray-1080p'
        assert (body['title'], body['client_name']) == ('Small Prophets', 'qui')

    def test_a_pack_of_the_season_in_the_client_is_found_and_singles_are_not(self, season):
        rows = [
            _row('Small.Prophets.S01.1080p.iP.WEB-DL.AAC2.0.H.264-Kitsune', 'k1'),
            _row('Small.Prophets.S01E03.1080p.BluRay.DD+5.1.x264-SbR', 's3'),
            _row('Small.Prophets.S02.1080p.WEB-DL-GRP', 's2'),
            _row('Other.Show.S01.1080p.WEB-DL-GRP', 'o1'),
            _row('Kleine.Propheten.S01.German.1080p.WEB-DL-DE', 'de'),
            _row('Small.Prophets.S01.2160p.WEB-DL-NEW', 'dl', progress=0.4),
        ]
        status, body = _post('/api/workflows/season_pack/lookup', season, rows=rows)
        assert status == 200
        packs = body['packs']
        assert [p['hash'] for p in packs] == ['k1', 'de', 'dl']
        kitsune = packs[0]
        assert (kitsune['group'], kitsune['quality'], kitsune['complete'], kitsune['reg']) == \
            ('Kitsune', '1080p WEB-DL', True, '1:k1')
        assert packs[-1]['complete'] is False

    def test_a_pack_whose_year_is_before_the_series_began_is_another_show(self, season):
        rows = [_row('Small.Prophets.1990.S01.1080p.WEB-DL-OLD', 'old')]
        _, body = _post('/api/workflows/season_pack/lookup', season, rows=rows)
        assert body['packs'] == []

    def test_a_series_sonarr_names_with_its_year_matches_the_releases_year_token(self, season):
        series = {**SERIES, 'title': 'Doctor Who (2005)', 'year': 2005, 'alt_titles': []}
        rows = [_row('Doctor.Who.2005.S01.1080p.BluRay-GRP', 'dw')]
        _, body = _post('/api/workflows/season_pack/lookup', season, rows=rows, titles=[series])
        assert [p['hash'] for p in body['packs']] == ['dw']

    def test_a_client_that_did_not_answer_in_full_says_so(self, season):
        rows = [_row('Small.Prophets.S01.1080p.iP.WEB-DL.AAC2.0.H.264-Kitsune', 'k1')]
        _, body = _post('/api/workflows/season_pack/lookup', season, rows=rows,
                        report={'instances_failed': [{'name': 'second'}]})
        assert body['client_checked'] is False
        assert len(body['packs']) == 1

    @pytest.mark.parametrize('over, status, code', [
        ({'episodes': None}, 502, 'sonarr_unavailable'),
        ({'titles': []}, 404, 'not_in_sonarr'),
        ({'titles': [], 'title_errors': [{'connection_id': 'tv'}]}, 502, 'sonarr_unavailable'),
    ])
    def test_what_sonarr_could_not_say_refuses(self, season, over, status, code):
        got, body = _post('/api/workflows/season_pack/lookup', season, **over)
        assert (got, body['code']) == (status, code)

    def test_an_unknown_connection_refuses(self, season):
        got, body = _post('/api/workflows/season_pack/lookup', season, body={'connection_id': 'gone'})
        assert (got, body['code']) == (404, 'unknown_connection')


# ═════════════════════════════════════════════════════════════════════════════
# Releases — full-season packs of this season, the singles' tracker first
# ═════════════════════════════════════════════════════════════════════════════

def _release(title, indexer, quality, full_season=True, mapped_season=1, **kw):
    return {'title': title, 'indexer': indexer, 'indexer_id': 1, 'guid': title, 'seeders': 5,
            'size': 1, 'quality_name': quality, 'full_season': full_season,
            'mapped_season': mapped_season, 'info_hash': '', **kw}


class TestReleases:

    def _search(self, season, releases, trackers=None):
        search = patch.object(app, 'fetch_release_matrix', return_value=releases)
        with search as fetch:
            status, body = _post('/api/workflows/season_pack/releases', season,
                                 body={'trackers': trackers or []}, extra=[])
        return status, body, fetch

    def test_only_packs_of_this_season_and_the_singles_tracker_leads(self, season):
        releases = [
            _release('Small.Prophets.S01.1080p.WEB-DL-OTHER', 'SeedPool', 'WEBDL-1080p'),
            _release('Small.Prophets.S01E03.1080p.WEB-DL-EP', 'Aither', 'WEBDL-1080p', full_season=False),
            _release('Small.Prophets.S02.1080p.WEB-DL-NEXT', 'Aither', 'WEBDL-1080p', mapped_season=2),
            _release('Small.Prophets.S01.1080p.iP.WEB-DL-Kitsune', 'Aither', 'WEBDL-1080p'),
        ]
        with patch.object(app, 'fetch_release_matrix', return_value=releases) as fetch:
            status, body = _post('/api/workflows/season_pack/releases', season, body={'trackers': ['aither.cc']})
        assert status == 200
        assert fetch.call_args.kwargs['season_number'] == 1
        assert [r['title'] for r in body['releases']] == [
            'Small.Prophets.S01.1080p.iP.WEB-DL-Kitsune', 'Small.Prophets.S01.1080p.WEB-DL-OTHER']
        assert [r['preferred'] for r in body['releases']] == [True, False]
        assert body['searched'] == 4

    def test_each_release_says_how_it_compares_with_the_library(self, season):
        releases = [_release('Small.Prophets.S01.1080p.BluRay-SbR', 'Aither', 'Bluray-1080p')]
        with patch.object(app, 'fetch_release_matrix', return_value=releases):
            _, body = _post('/api/workflows/season_pack/releases', season)
        assert body['releases'][0]['vs_library'] == {'higher': 0, 'same': 4, 'lower': 2, 'unknown': 0}

    def test_a_failed_search_refuses(self, season):
        with patch.object(app, 'fetch_release_matrix', side_effect=OSError('indexers down')):
            status, body = _post('/api/workflows/season_pack/releases', season)
        assert (status, body['code']) == (502, 'search_failed')


# ═════════════════════════════════════════════════════════════════════════════
# Grab — B12's queue check, then a watch that never force-imports
# ═════════════════════════════════════════════════════════════════════════════

class TestGrab:

    def _grab(self, season, queue=None, grab=None, body=None):
        extra = [patch.object(app, 'queue_records_for_item', return_value=[] if queue is None else queue),
                 patch.object(app, 'grab_release', side_effect=grab),
                 patch.object(app, '_start_pack_watch', return_value='job1')]
        from contextlib import ExitStack
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in [*_patched(season), *extra]]
            payload = {'connection_id': 'tv', 'arr_id': 143, 'season': 1, 'guid': 'g', 'indexer_id': 3,
                       'series_title': 'Small Prophets', 'info_hash': 'ABCDEF', **(body or {})}
            resp = app.app.test_client().post('/api/workflows/season_pack/grab', json=payload)
        queue_mock, grab_mock, watch_mock = mocks[-3:]
        return resp.status_code, resp.get_json(), queue_mock, grab_mock, watch_mock

    def test_a_grab_starts_a_watch_scoped_to_the_episodes_sonarr_holds(self, season):
        status, body, queue, grab, watch = self._grab(season)
        assert (status, body['job_id']) == (200, 'job1')
        assert queue.call_args.kwargs['episode_ids'] == [501, 502, 503, 504, 505, 506, 507]
        grab.assert_called_once()
        args, kw = watch.call_args
        assert args[1:5] == ('tv', 143, 1, 'Small Prophets · S01 pack')
        assert args[5] == [501, 502, 503, 504, 505, 506]
        assert (kw['info_hash'], kw['series_title']) == ('ABCDEF', 'Small Prophets')

    def test_a_season_already_in_the_queue_is_not_grabbed_twice(self, season):
        status, body, _, grab, watch = self._grab(season, queue=[{'title': 'Small.Prophets.S01-X'}])
        assert (status, body['code']) == (409, 'already_queued')
        grab.assert_not_called()
        watch.assert_not_called()

    def test_grab_anyway_skips_the_queue_check(self, season):
        status, _, queue, grab, _ = self._grab(season, queue=[{'title': 'x'}], body={'force': True})
        assert status == 200
        queue.assert_not_called()
        grab.assert_called_once()

    def test_a_release_gone_from_the_cache_says_so(self, season):
        err = urllib.error.HTTPError('http://sonarr', 404, 'Not Found', {}, BytesIO(b'not json'))
        status, body, _, _, watch = self._grab(season, grab=err)
        assert (status, body['code']) == (400, 'stale_release')
        watch.assert_not_called()

    def test_a_season_sonarr_could_not_list_grabs_nothing(self, season):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in _patched(season, episodes=None):
                stack.enter_context(p)
            grab = stack.enter_context(patch.object(app, 'grab_release'))
            resp = app.app.test_client().post('/api/workflows/season_pack/grab', json={
                'connection_id': 'tv', 'arr_id': 143, 'season': 1, 'guid': 'g', 'indexer_id': 3})
        assert (resp.status_code, resp.get_json()['code']) == (502, 'sonarr_unavailable')
        grab.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# The watch — `ready` hands the replace its torrent, and nothing is forced
# ═════════════════════════════════════════════════════════════════════════════

class _Inline:
    """`threading.Thread` that runs its target at `start()`."""
    def __init__(self, target, daemon=None):
        self._target = target

    def start(self):
        self._target()


def _watch(outcome, records=(), info_hash=None, landed=False):
    res = {'outcome': outcome, 'records': list(records), 'messages': ['blocked']}
    with patch.object(app.threading, 'Thread', _Inline), \
         patch.object(app.time, 'sleep'), \
         patch.object(app, '_read_target_files', return_value=[[501, 7111]]), \
         patch.object(app, '_await_landing', return_value=landed), \
         patch.object(app, 'poll_queue_until_clear', return_value=res) as poll, \
         patch.object(app, 'force_manual_import_by_id') as forced, \
         patch.dict(app._import_watches, {}, clear=True):
        job = app._start_pack_watch({}, 'tv', 143, 1, 'Small Prophets · S01 pack', [501], info_hash=info_hash,
                                    series_title='Small Prophets')
        watch = dict(app._import_watches[job])
    forced.assert_not_called()
    return watch, poll


class TestWatch:

    def test_a_pack_sonarr_holds_back_is_ready_with_its_torrent(self, season):
        watch, poll = _watch('import_pending', [{'downloadId': 'ABC123'}], info_hash='ABC123')
        assert watch['status'] == 'ready'
        assert watch['replace'] == {'hash': 'abc123', 'instance_id': None, 'connection_id': 'tv',
                                    'arr_id': 143, 'season': 1, 'title': 'Small Prophets'}
        assert poll.call_args.kwargs['download_id'] == 'ABC123'

    def test_with_no_info_hash_the_one_download_in_the_queue_names_it(self, season):
        watch, poll = _watch('import_pending', [{'downloadId': 'DEF456'}, {'downloadId': 'DEF456'}])
        assert (watch['status'], watch['replace']['hash']) == ('ready', 'def456')
        assert poll.call_args.kwargs['episode_ids'] == [501]

    def test_two_downloads_in_the_queue_name_neither(self, season):
        watch, _ = _watch('import_pending', [{'downloadId': 'AAA'}, {'downloadId': 'BBB'}])
        assert (watch['status'], watch['replace']) == ('error', None)

    def test_a_pack_sonarr_imported_itself_is_done(self, season):
        watch, _ = _watch('cleared', landed=True)
        assert (watch['status'], watch['replace']) == ('done', None)

    def test_a_failed_download_is_failed(self, season):
        watch, _ = _watch('failed')
        assert watch['status'] == 'failed'
        assert 'blocked' in watch['message']

    def test_a_ready_watch_stays_in_the_panel_past_a_minute(self):
        now = app.time.time()
        watches = {
            'ready': {'status': 'ready', 'source': 'season_pack', 'completed_at': now - 600, 'replace': {}},
            'old_ready': {'status': 'ready', 'source': 'season_pack', 'completed_at': now - 4000},
            'done': {'status': 'done', 'source': 'backfill', 'completed_at': now - 120},
        }
        with patch.dict(app._import_watches, watches, clear=True):
            jobs = app.app.test_client().get('/api/workflows/watch_import/active').get_json()['jobs']
            left = set(app._import_watches)
        assert [j['job_id'] for j in jobs] == ['ready']
        assert left == {'ready', 'done'}
