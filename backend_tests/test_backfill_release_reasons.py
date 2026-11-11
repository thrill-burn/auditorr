"""Backfill says why a row is empty, can ask for "your file or better", and never
offers another title's release (BACKFILL B16, 2026-11-11).

The report: searching *Miss Fisher's Murder Mysteries* gave nothing, though
Aither had both 1080p packs of every season. Measured on the box:

* **The Resolution chip was left on 2160p** from an earlier run, so both packs
  were filtered out, and the row said "No releases found", exactly what an empty
  tracker says. A run from the library map used those stored chips too.
* **Sonarr listed 100 releases of other shows above them.** seedpool answers a
  season search with its latest uploads, and Sonarr maps each one it can to a
  series in the library (*Stick S02E03* for a Miss Fisher S02 search). A grab
  posts only the guid, so grabbing that row downloads Stick.

These drive the real endpoints with only the arr's HTTP layer mocked, except
where a helper is the thing under test.
"""
import time
from unittest.mock import patch

import app
import arr

_ENV = {'REMOTE_ADDR': '127.0.0.1'}

_REL = 'movies/Film (2020)/Film.2020.1080p.BluRay.x264-OLD.mkv'
_MEDIA = [{'path': _REL, 'size': 8_000_000_000, 'trackers': ['None']}]
_ARR_MEDIA = [{'service': 'radarr', 'connection_id': 'radarr-mv', 'arr_id': 7, 'title': 'Film',
               'path': f'/data/media/{_REL}', 'title_slug': 'film', 'file_id': 70,
               'file_quality_name': 'Bluray-1080p', 'file_hdr': ''}]
_CFG = {'MEDIA_PATH': '/data/media', 'ARR_CONNECTIONS': [
    {'id': 'radarr-mv', 'service': 'radarr', 'name': 'Movies',
     'base_url': 'http://mv:7878', 'api_key': 'b'},
    {'id': 'sonarr-tv', 'service': 'sonarr', 'name': 'TV',
     'base_url': 'http://tv:8989', 'api_key': 'c'}]}

_RES = {'Bluray-2160p': 2160, 'Bluray-1080p': 1080, 'Bluray-1080p Remux': 1080,
        'WEBDL-1080p': 1080, 'WEBDL-720p': 720, 'Unknown': 0}


def _release(guid, quality='Bluray-1080p', indexer='Aither', title=None, **fields):
    """A raw `/api/v3/release` row. Extra keyword fields are the arr's own, verbatim."""
    row = {'title': title or f'Film.2020.{guid}.x264-GRP', 'indexer': indexer, 'indexerId': 1,
           'guid': guid, 'size': 8_000_000_000, 'seeders': 5, 'customFormatScore': 0,
           'qualityWeight': 10, 'protocol': 'torrent',
           'quality': {'quality': {'name': quality, 'resolution': _RES.get(quality, 0)}}}
    row.update(fields)
    return row


def _generate(releases, **body):
    """One real generate run over one Radarr film. Returns its result row."""
    client = app.app.test_client()
    with patch.object(app, 'AUDITORR_SECRET', ''), \
         patch.object(app, 'AUDITORR_REQUIRE_AUTH', False), \
         patch.object(app, 'db_load_config', return_value=_CFG), \
         patch.object(app, 'db_load_file_results', return_value=_MEDIA), \
         patch.object(app, 'fetch_arr_media_index_with_roots', return_value=(_ARR_MEDIA, [], {})), \
         patch('arr._arr_get', return_value=releases):
        job_id = client.post('/api/workflows/generate', json={'count': 5, **body},
                             environ_base=_ENV).get_json()['job_id']
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = client.get(f'/api/workflows/generate/status?job_id={job_id}',
                                environ_base=_ENV).get_json()
            if status['status'] != 'running':
                return status['results'][0]
            time.sleep(0.01)
    raise AssertionError('the generate job never finished')


def _guids(result):
    return [r['guid'] for r in result['releases'] or []]


def _matrix(service, arr_id, releases, **kw):
    report = {}
    with patch('arr._arr_get', return_value=releases):
        rows = arr.fetch_release_matrix(_CFG, service, f'{service}-{"mv" if service == "radarr" else "tv"}',
                                        arr_id, season_number=1 if service == 'sonarr' else None,
                                        report=report, **kw)
    return [r['guid'] for r in rows], report


# ── another title's release is never offered ─────────────────────────────────

def test_a_release_the_arr_mapped_to_another_movie_is_dropped():
    """Radarr grabs a release as the movie it mapped it to, whatever row it was clicked on."""
    guids, report = _matrix('radarr', 7, [
        _release('mine', mappedMovieId=7, downloadAllowed=True),
        _release('remake', mappedMovieId=8, downloadAllowed=True),
    ])
    assert guids == ['mine']
    assert report['other_title'] == 1 and report['returned'] == 2


def test_a_release_the_arr_mapped_to_another_series_is_dropped():
    """The box's case: *Stick S02E03* in a Miss Fisher season search."""
    guids, report = _matrix('sonarr', 6, [
        _release('fisher', title="Miss Fisher's Murder Mysteries S01 1080p BluRay", mappedSeriesId=6,
                 downloadAllowed=True, fullSeason=True, mappedSeasonNumber=1),
        _release('stick', title='Stick.S02E03.2160p.ATVP.WEB-DL', mappedSeriesId=41,
                 downloadAllowed=True, mappedSeasonNumber=1),
    ])
    assert guids == ['fisher']
    assert report['other_title'] == 1


def test_a_release_the_arr_could_not_identify_is_dropped():
    """No mapped id and `downloadAllowed: false`: the grab can't work from here
    (Sonarr: "Unable to find matching series and episodes, will need to be
    manually provided"), and it's a release of a show nobody has."""
    guids, report = _matrix('sonarr', 6, [
        _release('fisher', mappedSeriesId=6, downloadAllowed=True),
        _release('the-body', title='The.Body.2026.S01E08.2160p', downloadAllowed=False),
    ])
    assert guids == ['fisher']
    assert report['unidentified'] == 1 and report['other_title'] == 0


def test_this_series_with_episodes_sonarr_could_not_place_is_kept():
    """Mapped to this series, episodes unknown: the grab looks them up again."""
    guids, _ = _matrix('sonarr', 6, [_release('fisher', mappedSeriesId=6, downloadAllowed=False)])
    assert guids == ['fisher']


def test_a_release_without_either_field_is_kept():
    """An arr that doesn't send them is not evidence (R1)."""
    guids, report = _matrix('radarr', 7, [_release('plain')])
    assert guids == ['plain']
    assert report == {'returned': 1, 'usenet': 0, 'other_title': 0, 'unidentified': 0}


def test_ids_compare_whatever_type_the_caller_passed():
    """`acquire_releases` parses arr_id from a query string; the arr's is an int."""
    guids, _ = _matrix('radarr', '7', [_release('mine', mappedMovieId=7)])
    assert guids == ['mine']


def test_report_is_optional_and_the_return_value_unchanged():
    with patch('arr._arr_get', return_value=[_release('a', mappedMovieId=7)]):
        rows = arr.fetch_release_matrix(_CFG, 'radarr', 'radarr-mv', 7)
    assert [r['guid'] for r in rows] == ['a']


def test_a_run_never_offers_another_titles_release():
    result = _generate([_release('remake', mappedMovieId=8, customFormatScore=500),
                        _release('mine', mappedMovieId=7)])
    assert _guids(result) == ['mine']


# ── an empty row says why ────────────────────────────────────────────────────

def test_the_users_case_a_2160p_chip_says_it_hid_the_1080p_releases():
    result = _generate([_release('remux', 'Bluray-1080p Remux'), _release('x264', 'Bluray-1080p')],
                       res_filter=['2160p'])

    assert result['status'] == 'not_found'
    empty = result['empty']
    assert empty['quality'] == 2
    assert empty['quality_names'] == ['Bluray-1080p Remux', 'Bluray-1080p']
    assert empty['indexers'] == 0 and empty['below_floor'] == 0


def test_an_empty_row_names_the_indexer_strategy():
    result = _generate([_release('tl', indexer='TorrentLeech')], seeding_on=['Aither'])
    empty = result['empty']
    assert result['status'] == 'not_found'
    assert empty['indexers'] == 1 and empty['quality'] == 0


def test_an_empty_row_counts_the_other_titles_and_usenet():
    result = _generate([_release('remake', mappedMovieId=8),
                        _release('unknown', downloadAllowed=False),
                        _release('nzb', protocol='usenet')])
    empty = result['empty']
    assert empty['returned'] == 3
    assert empty['other_title'] == 2 and empty['usenet'] == 1


def test_a_hidden_quality_names_its_hdr():
    result = _generate([_release('dv', 'Bluray-2160p', title='Film.2020.2160p.BluRay.DV.x265-GRP')],
                       hdr_filter=['SDR'])
    assert result['empty']['quality_names'] == ['Bluray-2160p DV']


def test_a_row_with_releases_carries_no_explanation():
    result = _generate([_release('x264')])
    assert result['status'] == 'found'
    assert result['empty'] is None


def test_the_in_flight_row_has_every_key_from_creation():
    """The status endpoint serialises the row being searched as `current` while
    the job thread writes to it, and a key appearing mid-iteration raises there."""
    client = app.app.test_client()
    seen = {}

    def slow_get(*_a, **_kw):
        job = next(iter(app._gen_jobs.values()))
        seen['keys'] = set(job['results'][0])
        return [_release('x264')]

    with patch.object(app, 'AUDITORR_SECRET', ''), \
         patch.object(app, 'AUDITORR_REQUIRE_AUTH', False), \
         patch.object(app, 'db_load_config', return_value=_CFG), \
         patch.object(app, 'db_load_file_results', return_value=_MEDIA), \
         patch.object(app, 'fetch_arr_media_index_with_roots', return_value=(_ARR_MEDIA, [], {})), \
         patch('arr._arr_get', side_effect=slow_get):
        app._gen_jobs.clear()
        job_id = client.post('/api/workflows/generate', json={'count': 1},
                             environ_base=_ENV).get_json()['job_id']
        deadline = time.monotonic() + 10
        while app._gen_jobs[job_id]['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.01)
    final = set(app._gen_jobs[job_id]['results'][0])
    assert {'floor_quality', 'empty'} <= seen['keys']
    assert final == seen['keys']


# ── "your file or better" ────────────────────────────────────────────────────

def test_your_file_or_better_hides_only_a_lower_release():
    """The file is Bluray-1080p. Resolution decides, then source, as Triage compares."""
    result = _generate([_release('uhd', 'Bluray-2160p'), _release('remux', 'Bluray-1080p Remux'),
                        _release('same', 'Bluray-1080p'), _release('web', 'WEBDL-1080p'),
                        _release('sd', 'WEBDL-720p'), _release('unknown', 'Unknown')],
                       min_quality='file')

    assert set(_guids(result)) == {'uhd', 'remux', 'same', 'unknown'}
    assert result['floor_quality'] == 'Bluray-1080p'


def test_with_no_minimum_a_lower_release_is_offered():
    result = _generate([_release('sd', 'WEBDL-720p')])
    assert _guids(result) == ['sd']
    assert result['floor_quality'] is None


def test_an_empty_row_says_what_was_below_your_file():
    result = _generate([_release('sd', 'WEBDL-720p'), _release('web', 'WEBDL-1080p')],
                       min_quality='file')
    empty = result['empty']
    assert result['status'] == 'not_found'
    assert empty['below_floor'] == 2 and empty['quality'] == 0
    assert empty['below_floor_names'] == ['WEBDL-720p', 'WEBDL-1080p']
    assert empty['floor'] == 'Bluray-1080p'


def test_a_file_whose_quality_cant_be_read_hides_nothing():
    media = [dict(_ARR_MEDIA[0], file_quality_name='')]
    client = app.app.test_client()
    with patch.object(app, 'AUDITORR_SECRET', ''), \
         patch.object(app, 'AUDITORR_REQUIRE_AUTH', False), \
         patch.object(app, 'db_load_config', return_value=_CFG), \
         patch.object(app, 'db_load_file_results', return_value=_MEDIA), \
         patch.object(app, 'fetch_arr_media_index_with_roots', return_value=(media, [], {})), \
         patch('arr._arr_get', return_value=[_release('sd', 'WEBDL-720p')]):
        job_id = client.post('/api/workflows/generate', json={'count': 1, 'min_quality': 'file'},
                             environ_base=_ENV).get_json()['job_id']
        deadline = time.monotonic() + 10
        while app._gen_jobs[job_id]['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.01)
    result = app._gen_jobs[job_id]['results'][0]
    assert _guids(result) == ['sd']
    assert result['floor_quality'] is None


def test_a_season_floor_is_its_lowest_file():
    """A season whose profile was lowered part-way: the floor must not hide the
    quality half the season is already at."""
    assert app._lowest_quality(['Bluray-2160p', 'WEBDL-1080p', 'Bluray-1080p']) == 'WEBDL-1080p'
    assert app._lowest_quality(['', 'Unknown', 'HDTV-720p']) == 'HDTV-720p'
    assert app._lowest_quality(['', None]) == ''


def test_a_season_candidate_carries_its_floor():
    files = [{'arr_service': 'sonarr', 'arr_connection_id': 'sonarr-tv', 'arr_id': 6,
              'arr_title': 'Show', 'arr_url': '', 'file_quality': q, 'file_hdr': '',
              'path': f'tv/Show/Season 1/Show.S01E0{i}.mkv', 'size': 1, 'file_id': i,
              'episode_ids': [], 'episode_numbers': []}
             for i, q in enumerate(['Bluray-2160p', 'WEBDL-1080p'], start=1)]
    group = app._backfill_group('season', files, season=1, held=2, unseeded=2)
    assert group['floor_quality'] == 'WEBDL-1080p'
