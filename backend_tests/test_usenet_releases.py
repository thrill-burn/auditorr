"""Nothing auditorr searches for comes from Usenet (B14, issue #25).

The report was that Backfill's *Must also be seeding on* picker listed Usenet
indexers. Behind the picker, nothing filtered the releases either. With
*Download from: All*, the default, a Usenet release could top a Backfill run and
be grabbed, which produces no seed and still earned a Rounds point. Trumped's
step 4 read the same search, and a scene release carries the same name on Usenet
(its test is in `test_trump_resolution.py`, beside the fixture it needs).

Both arrs' `ReleaseResource` and `IndexerResource` carry `Protocol`, serialized
as "usenet" / "torrent". A row is dropped only where it says Usenet. A missing
field is not evidence, and reading it as Usenet would empty every search (R1).

These drive the real endpoints with only the arr's HTTP layer mocked.
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
     'base_url': 'http://mv:7878', 'api_key': 'b'}]}


def _release(guid, indexer, protocol='torrent', title='Film.2020.1080p.BluRay.x264-GRP',
             size=8_000_000_000, cf=0):
    """A raw `/api/v3/release` row as Radarr returns it. `protocol=None` omits the field."""
    row = {'title': title, 'indexer': indexer, 'indexerId': 1, 'guid': guid, 'size': size,
           'seeders': None if protocol == 'usenet' else 5, 'customFormatScore': cf,
           'qualityWeight': 10, 'quality': {'quality': {'name': 'Bluray-1080p', 'resolution': 1080}}}
    if protocol is not None:
        row['protocol'] = protocol
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


# ── Backfill's search ────────────────────────────────────────────────────────

def test_backfill_never_offers_a_usenet_release():
    """The Usenet copy outranks the torrent by custom format score, so before
    the fix it was the run's pick, the one the row's Grab button sends."""
    result = _generate([_release('nzb', 'NZBgeek', protocol='usenet', cf=100),
                        _release('tor', 'Aither')])

    assert _guids(result) == ['tor']
    assert result['best_release']['guid'] == 'tor'


def test_a_film_only_on_usenet_is_not_found():
    result = _generate([_release('nzb', 'NZBgeek', protocol='usenet')])

    assert result['status'] == 'not_found'
    assert result['best_release'] is None


def test_a_release_that_does_not_say_its_protocol_is_still_offered():
    """Absence is not evidence: an arr that left the field out keeps its releases."""
    result = _generate([_release('tor', 'Aither', protocol=None)])

    assert _guids(result) == ['tor']


def test_protocol_is_read_case_insensitively():
    result = _generate([_release('nzb', 'NZBgeek', protocol='Usenet'), _release('tor', 'Aither')])

    assert _guids(result) == ['tor']


def test_a_usenet_listing_never_satisfies_must_also_be_seeding_on():
    """*Must also be seeding on* means a tracker. The same release on a Usenet
    indexer is not a seed anywhere."""
    result = _generate([_release('tor', 'Aither'), _release('nzb', 'NZBgeek', protocol='usenet')],
                       seeding_on=['NZBgeek'])

    assert result['status'] == 'not_found'


# ── the indexer pickers ──────────────────────────────────────────────────────

_TWO_ARRS = {'ARR_CONNECTIONS': [
    {'id': 'radarr-mv', 'service': 'radarr', 'name': 'Movies', 'base_url': 'http://mv:7878', 'api_key': 'a'},
    {'id': 'sonarr-tv', 'service': 'sonarr', 'name': 'TV', 'base_url': 'http://tv:8989', 'api_key': 'b'},
]}


def _indexers(by_base_url):
    def fake_get(base, _key, path, **_kw):
        assert path == '/api/v3/indexer'
        answer = by_base_url[base]
        if isinstance(answer, Exception):
            raise answer
        return answer
    return fake_get


def test_usenet_indexers_are_left_out_of_the_pickers_and_named():
    answers = {
        'http://mv:7878': [{'name': 'Aither', 'protocol': 'torrent'},
                           {'name': 'NZBgeek', 'protocol': 'usenet'},
                           {'name': 'Shared', 'protocol': 'torrent'},
                           {'name': 'Older', 'implementation': 'Torznab'}],
        'http://tv:8989': [{'name': 'NZBgeek', 'protocol': 'usenet'},
                           {'name': 'Shared', 'protocol': 'usenet'},
                           {'name': 'Aither', 'protocol': 'torrent'}],
    }
    with patch('arr._arr_get', side_effect=_indexers(answers)):
        names, usenet = arr.fetch_arr_indexers(_TWO_ARRS)

    # De-duplicated across both arrs; a missing protocol stays listed.
    assert names == ['Aither', 'Shared', 'Older']
    # A name some arr has as a torrent indexer is never cleared from a choice.
    assert usenet == ['NZBgeek']


def test_an_arr_that_did_not_answer_names_no_usenet_indexers():
    """The page clears only names in `usenet`. An arr that did not answer leaves
    its torrent indexers out of `names` too, and those choices must survive."""
    answers = {'http://mv:7878': [{'name': 'Aither', 'protocol': 'torrent'}],
               'http://tv:8989': OSError('timed out')}
    with patch('arr._arr_get', side_effect=_indexers(answers)):
        assert arr.fetch_arr_indexers(_TWO_ARRS) == (['Aither'], [])


def test_the_indexers_endpoint_ships_both_lists():
    answers = {'http://mv:7878': [{'name': 'Aither', 'protocol': 'torrent'},
                                  {'name': 'NZBgeek', 'protocol': 'usenet'}],
               'http://tv:8989': []}
    with patch.object(app, 'AUDITORR_SECRET', ''), \
         patch.object(app, 'AUDITORR_REQUIRE_AUTH', False), \
         patch.object(app, 'db_load_config', return_value=_TWO_ARRS), \
         patch('arr._arr_get', side_effect=_indexers(answers)):
        body = app.app.test_client().get('/api/workflows/indexers', environ_base=_ENV).get_json()

    assert body == {'status': 'success', 'indexers': ['Aither'], 'usenet': ['NZBgeek']}
