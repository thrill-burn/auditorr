"""Backfill never searches a season pack for a season it does not cover (B1).

Every Sonarr candidate used to be grouped by season and searched as a **pack**,
however many of that season's files were already seeded. One unseeded episode
of ten issued `/api/v3/release?seriesId=…&seasonNumber=…`, the page offered the
pack, and the import watch then force-imported it over the whole season: nine
hardlinked library files replaced, nine torrents orphaned, the health score
moved the wrong way on both axes — by the workflow whose card promises the
opposite, with nothing having gone wrong.

These tests drive the real endpoints with only the HTTP layer to the arr
mocked, and they assert on the **search that is issued**, not on a helper's
return value: the search path is what decides what gets grabbed.
"""
import time
from unittest.mock import patch

import app as app_module

_ENV = {'REMOTE_ADDR': '127.0.0.1'}


def _cfg():
    return {
        'MEDIA_PATH': '/data/media',
        'ARR_CONNECTIONS': [
            {'id': 'sonarr-tv', 'service': 'sonarr', 'name': 'TV',
             'base_url': 'http://tv:8989', 'api_key': 'a'},
        ],
    }


def _season(n_files, unseeded, name='Show.S{season:02d}E{ep:02d}.1080p.WEB-DL.mkv', season=1):
    """One season of `n_files` episode files; the first `unseeded` have no torrent.

    Index rows are shaped like a real Sonarr's: a season number and a file id,
    and **no episode ids or numbers** — `/api/v3/episodefile` does not carry
    them (Sonarr's EpisodeFileResource has no such field), so the episode has to
    be joined in from the series' episode list by `episodeFileId`. Fixtures
    that populated `episode_ids` exercised a code path no real install reaches.

    Episode ids are `season * 100 + ep` (S01E01 is 101).
    """
    media, arr_media, episodes = [], [], []
    for ep in range(1, n_files + 1):
        rel = f'tv/Show/Season {season:02d}/{name.format(ep=ep, season=season)}'
        file_id = 5000 + season * 100 + ep
        media.append({'path': rel, 'size': 1_000_000_000 + ep,
                      'trackers': ['None'] if ep <= unseeded else ['tracker.example']})
        arr_media.append({'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1,
                          'title': 'Show', 'path': f'/data/media/{rel}',
                          'title_slug': 'show', 'file_id': file_id, 'season_number': season,
                          'episode_ids': [], 'episode_numbers': [],
                          'file_quality_name': 'WEBDL-1080p', 'file_hdr': ''})
        episodes.append({'id': season * 100 + ep, 'seriesId': 1, 'seasonNumber': season,
                         'episodeNumber': ep, 'episodeFileId': file_id})
    return media, arr_media, episodes


def _movie():
    rel = 'movies/Film (2020)/Film.2020.1080p.BluRay.mkv'
    media = [{'path': rel, 'size': 8_000_000_000, 'trackers': ['None']}]
    arr_media = [{'service': 'radarr', 'connection_id': 'radarr-mv', 'arr_id': 7,
                  'title': 'Film', 'path': f'/data/media/{rel}', 'title_slug': 'film',
                  'file_id': 70, 'file_quality_name': 'Bluray-1080p', 'file_hdr': ''}]
    return media, arr_media


def _release(title, full_season=False, episodes=(), size=1_000_000_001):
    """A raw `/api/v3/release` row as Sonarr returns it."""
    return {'title': title, 'indexer': 'ix', 'indexerId': 1, 'seeders': 5, 'size': size,
            'guid': title, 'quality': {'quality': {'name': 'WEBDL-1080p', 'resolution': 1080}},
            'fullSeason': full_season, 'episodeNumbers': list(episodes)}


def _run_generate(media, arr_media, episodes=(), releases=(), episodes_fail=False):
    """Run a real generate job. Returns (release searches sent, final result rows)."""
    issued = []

    def fake_get(_base, _key, path, **_kw):
        if path.startswith('/api/v3/episode?'):
            if episodes_fail:
                raise OSError('timed out')
            return list(episodes)
        issued.append(path)
        return list(releases)

    client = app_module.app.test_client()
    with patch.object(app_module, 'AUDITORR_SECRET', ''), \
         patch.object(app_module, 'AUDITORR_REQUIRE_AUTH', False), \
         patch.object(app_module, 'db_load_config', return_value=_cfg()), \
         patch.object(app_module, 'db_load_file_results', return_value=media), \
         patch.object(app_module, 'fetch_arr_media_index', return_value=arr_media), \
         patch.object(app_module, 'arr_media_index_errors', return_value=[]), \
         patch('arr._arr_get', side_effect=fake_get):
        res = client.post('/api/workflows/generate', json={'count': 50}, environ_base=_ENV)
        assert res.status_code == 200, res.get_json()
        job_id = res.get_json()['job_id']
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            body = client.get(f'/api/workflows/generate/status?job_id={job_id}',
                              environ_base=_ENV).get_json()
            if body['status'] != 'running':
                break
            time.sleep(0.01)
        else:
            raise AssertionError('the generate job never finished')
    return [p for p in issued if p.startswith('/api/v3/release')], body['results']


def _searches_issued(media, arr_media, episodes=()):
    return _run_generate(media, arr_media, episodes)[0]


def _acquire(media, arr_media):
    with patch.object(app_module, 'AUDITORR_SECRET', ''), \
         patch.object(app_module, 'AUDITORR_REQUIRE_AUTH', False), \
         patch.object(app_module, 'db_load_config', return_value=_cfg()), \
         patch.object(app_module, 'db_load_file_results', return_value=media), \
         patch.object(app_module, 'fetch_arr_media_index', return_value=arr_media), \
         patch.object(app_module, 'arr_media_index_errors', return_value=[]):
        return app_module.app.test_client().get('/api/workflows/acquire_candidates',
                                                environ_base=_ENV).get_json()


def test_partial_season_never_searches_a_pack():
    media, arr_media, episodes = _season(10, unseeded=1)

    searches = _searches_issued(media, arr_media, episodes)

    packs = [p for p in searches if 'seasonNumber=' in p]
    assert not packs, (
        f'one unseeded episode of ten searched a season pack ({packs}) — grabbing '
        'it force-imports over the nine hardlinked library files beside it')
    assert searches == ['/api/v3/release?episodeId=101']


# ── The rule's other half: coverage still earns a pack ───────────────────────

def test_a_fully_unseeded_season_still_searches_one_pack():
    """The case the pack search was written for, and there it is strictly right:
    nothing in the season was seeded, so nothing is orphaned by replacing it."""
    media, arr_media, episodes = _season(10, unseeded=10)

    searches = _searches_issued(media, arr_media, episodes)

    assert searches == ['/api/v3/release?seriesId=1&seasonNumber=1']


def test_a_partly_seeded_season_searches_each_unseeded_episode():
    media, arr_media, episodes = _season(10, unseeded=3)

    searches = _searches_issued(media, arr_media, episodes)

    assert sorted(searches) == [f'/api/v3/release?episodeId={n}' for n in (101, 102, 103)]


def test_a_season_the_arr_did_not_number_everywhere_is_never_a_pack():
    """Absence is not coverage. One of the series' records carries no season, so
    it could sit in this season, seeded — and a pack would replace it."""
    media, arr_media, episodes = _season(3, unseeded=3)
    arr_media.append({'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1,
                      'title': 'Show', 'path': '/data/media/tv/Show/Specials/odd-name.mkv',
                      'file_id': 999, 'season_number': None,
                      'episode_ids': [], 'episode_numbers': []})

    searches = _searches_issued(media, arr_media, episodes)

    assert not [p for p in searches if 'seasonNumber=' in p]
    assert len(searches) == 3


# ── Which episode an episode row searches ────────────────────────────────────

def test_the_episode_is_joined_by_file_id_not_parsed_from_the_name():
    """A daily series has no SxxExx for the filename parse to find, and the index
    carries no episode ids. Sonarr's episode list joins on `episodeFileId`."""
    media, arr_media, episodes = _season(2, unseeded=1, name='Show - 2024-01-0{ep}.mkv')

    searches = _searches_issued(media, arr_media, episodes)

    assert searches == ['/api/v3/release?episodeId=101']


def test_an_unanswerable_episode_list_never_becomes_a_pack():
    """Could not ask which episode this is: the row fails honestly, and nothing
    about the failure widens the search to the season."""
    media, arr_media, episodes = _season(2, unseeded=1, name='Show - 2024-01-0{ep}.mkv')

    searches, results = _run_generate(media, arr_media, episodes, episodes_fail=True)

    assert searches == []
    assert results[0]['status'] == 'error'


# ── What an episode row may be offered ───────────────────────────────────────

def test_an_episode_row_is_never_offered_a_pack_or_a_wider_file():
    """Interactive search lists rejected releases too, and a grab through
    /api/v3/release bypasses the rejection — so a pack in an episode search is
    one click from B1 by another route."""
    media, arr_media, episodes = _season(10, unseeded=1)
    releases = [
        _release('Show.S01.1080p.WEB-DL-GRP', full_season=True, episodes=[]),
        _release('Show.S01E01E02.1080p.WEB-DL-GRP', episodes=[1, 2]),
        _release('Show.S01E01.1080p.WEB-DL-GRP', episodes=[1]),
        _release('Show.2024.01.05.1080p.WEB-DL-GRP', episodes=[]),
    ]

    _searches, results = _run_generate(media, arr_media, episodes, releases=releases)

    offered = sorted(r['title'] for r in results[0]['releases'])
    assert offered == ['Show.2024.01.05.1080p.WEB-DL-GRP', 'Show.S01E01.1080p.WEB-DL-GRP']


def test_a_season_row_keeps_its_pack():
    media, arr_media, episodes = _season(2, unseeded=2)
    releases = [_release('Show.S01.1080p.WEB-DL-GRP', full_season=True)]

    _searches, results = _run_generate(media, arr_media, episodes, releases=releases)

    assert [r['title'] for r in results[0]['releases']] == ['Show.S01.1080p.WEB-DL-GRP']


def test_a_retry_searches_what_the_row_searched():
    """A failed grab re-asks `acquire_releases` with the row's own `search`. It
    used to rebuild the query from `season_number`, which an episode row still
    carries as a label — so the retry of an episode grab searched the pack."""
    media, arr_media, episodes = _season(10, unseeded=1)

    _searches, results = _run_generate(media, arr_media, episodes)

    assert results[0]['scope'] == 'episode'
    assert results[0]['season_number'] == 1
    assert results[0]['search'] == {'service': 'sonarr', 'connection_id': 'sonarr-tv',
                                    'arr_id': 1, 'episode_id': 101,
                                    'path': 'tv/Show/Season 01/Show.S01E01.1080p.WEB-DL.mkv'}


# ── One grouping, server-side (B7 / B7b) ─────────────────────────────────────

def test_the_page_is_served_the_candidates_the_search_runs():
    """The count on the button and the searches that run are one computation.
    A client-side copy of the grouping cannot know how many files the arr holds
    in a season, and after B1 that is what decides pack or episodes."""
    m1, a1, _ = _season(10, unseeded=3)
    m2, a2, _ = _season(4, unseeded=4, season=2)
    mm, am = _movie()
    media, arr_media = m1 + m2 + mm, a1 + a2 + am

    body = _acquire(media, arr_media)
    with patch.object(app_module, 'db_load_file_results', return_value=media), \
         patch.object(app_module, 'fetch_arr_media_index', return_value=arr_media):
        searched = app_module._build_generate_candidates(_cfg())

    assert ([(c['key'], c['scope']) for c in body['candidates']]
            == [(g['key'], g['scope']) for g in searched])
    assert body['counts'] == {'candidates': 5, 'season': 1, 'episode': 3, 'movie': 1}


def test_acquire_candidates_ships_candidates_not_every_unseeded_file():
    """It used to send a row per unseeded file, resolved or not, so the browser
    could throw most of them away and compute two integers already present."""
    media, arr_media, _ = _season(10, unseeded=3)
    media += [{'path': f'tv/Show/Season 01/extra{n}.srt', 'size': 1, 'trackers': ['None']}
              for n in range(5)]

    body = _acquire(media, arr_media)

    assert len(body['candidates']) == 3
    assert all(c['arr_id'] == 1 for c in body['candidates'])
    assert (body['resolved_count'], body['unresolved_count']) == (3, 5)


def test_a_pack_row_and_an_episode_row_are_told_apart_in_the_payload():
    """`S01 · 10 ep` meant "ten unseeded episodes" and read as "a season pack".
    They are now two different actions, and the row carries which one it is and
    the numbers the decision was made on."""
    m1, a1, _ = _season(10, unseeded=10)
    m2, a2, _ = _season(10, unseeded=1, season=2)

    pack, episode = _acquire(m1 + m2, a1 + a2)['candidates']

    assert (pack['scope'], pack['file_count'], pack['season_files_held']) == ('season', 10, 10)
    assert (episode['scope'], episode['file_count'], episode['season_number'],
            episode['episode_numbers']) == ('episode', 1, 2, [1])
    assert (episode['season_files_held'], episode['season_files_unseeded']) == (10, 1)
