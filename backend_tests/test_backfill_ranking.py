"""Backfill ranks releases by closeness to the file already on disk (B3).

The old order was the arr's interactive-search order — custom format score,
quality weight, seeders — which answers "what is the best copy of this?". That
is the upgrade question. Backfill asks "which of these is the file I already
have?", and the page already rendered every input needed to answer it (the
library file's size, quality and HDR) while the ranker read none of them. So
the default pick was routinely a different, larger release: a bigger download,
a library file replaced by another encode, and often a release the user's own
quality profile had already passed over.

The upgrade order is still there, as a choice.
"""
import time
from unittest.mock import patch

import app

_ENV = {'REMOTE_ADDR': '127.0.0.1'}
_LOCAL = {'total_size': 8_000_000_000, 'file_quality': 'Bluray-1080p', 'file_hdr': ''}


def _row(title, size, quality='Bluray-1080p', hdr='', seeders=5, cf=0, weight=10):
    """A release row as `fetch_release_matrix` builds it."""
    return {'title': title, 'size': size, 'indexer': 'ix', 'indexer_id': 1, 'guid': title,
            'quality_name': quality, 'hdr': hdr, 'seeders': seeders,
            'custom_format_score': cf, 'quality_weight': weight}


def _rank(rows, rank='closest', local=_LOCAL):
    return app._rank_releases(app._apply_release_filters(rows, [], []), rank, local)


def _titles(rows):
    return [r['title'] for r in rows]


def test_ranking_prefers_the_local_file():
    """A size-identical release beats a higher-scoring larger one by default."""
    rows = [_row('Film.2160p.Remux', 60_000_000_000, quality='Remux-2160p', cf=100, weight=30),
            _row('Film.1080p.BluRay-OLD', 8_000_000_000, cf=0, weight=10)]

    assert _titles(_rank(rows)) == ['Film.1080p.BluRay-OLD', 'Film.2160p.Remux']


def test_the_upgrade_order_is_still_there_when_asked_for():
    rows = [_row('Film.1080p.BluRay-OLD', 8_000_000_000, cf=0, weight=10),
            _row('Film.2160p.Remux', 60_000_000_000, quality='Remux-2160p', cf=100, weight=30)]

    assert _titles(_rank(rows, rank='upgrade')) == ['Film.2160p.Remux', 'Film.1080p.BluRay-OLD']


def test_a_scene_sidecar_does_not_cost_the_size_match():
    """An .nfo and a sample beside the video put a torrent a little over the one
    library file — still the same payload, and still ahead of a different encode
    at the same quality."""
    rows = [_row('Film.1080p.BluRay-OTHER', 9_400_000_000, seeders=90),
            _row('Film.1080p.BluRay-SCENE', 8_040_000_000, seeders=2)]

    ranked = _rank(rows)
    assert _titles(ranked) == ['Film.1080p.BluRay-SCENE', 'Film.1080p.BluRay-OTHER']
    assert ranked[0]['match']['size'] == 'partial'


def test_quality_then_hdr_then_seeders_break_ties_between_size_misses():
    rows = [_row('a.webdl', 12_000_000_000, quality='WEBDL-1080p', seeders=99),
            _row('b.bluray.hdr', 12_000_000_000, hdr='HDR10', seeders=50),
            _row('c.bluray.few', 12_000_000_000, seeders=3),
            _row('d.bluray.many', 12_000_000_000, seeders=40)]

    assert _titles(_rank(rows)) == ['d.bluray.many', 'c.bluray.few', 'b.bluray.hdr', 'a.webdl']


def test_a_null_seeder_count_does_not_crash_either_order():
    """Both arrs declare Seeders as `int?` — present and null for usenet."""
    rows = [_row('a', 8_000_000_000, seeders=None), _row('b', 8_000_000_000, seeders=None)]

    assert len(_rank(rows)) == 2
    assert len(_rank(rows, rank='upgrade')) == 2


def test_each_row_carries_its_evidence():
    ranked = _rank([_row('Film.2160p.WEB-DL.DV', 9_200_000_000, quality='WEBDL-2160p', hdr='DV')])

    assert ranked[0]['size_delta'] == 1_200_000_000
    assert ranked[0]['match'] == {'size': 'diff', 'quality': 'diff', 'hdr': 'diff'}


def test_partial_quality_is_named_partial():
    ranked = _rank([_row('Film.1080p.WEB-DL', 8_000_000_000, quality='WEBDL-1080p')])

    assert ranked[0]['match'] == {'size': 'same', 'quality': 'partial', 'hdr': ''}


def test_without_a_local_file_nothing_is_reordered():
    rows = [_row('a', 1, cf=1), _row('b', 2, cf=5)]

    assert _titles(_rank(rows, local=None)) == ['b', 'a']


# ── the run honours the user's choice ────────────────────────────────────────

def _generate(release_rank=None):
    releases = [
        {'title': 'Film.2020.2160p.UHD.BluRay.Remux-GRP', 'indexer': 'ix', 'indexerId': 1,
         'guid': 'remux', 'size': 60_000_000_000, 'seeders': 50, 'customFormatScore': 100,
         'qualityWeight': 30, 'quality': {'quality': {'name': 'Remux-2160p', 'resolution': 2160}}},
        {'title': 'Film.2020.1080p.BluRay.x264-OLD', 'indexer': 'ix', 'indexerId': 1,
         'guid': 'old', 'size': 8_000_000_000, 'seeders': 3, 'customFormatScore': 0,
         'qualityWeight': 10, 'quality': {'quality': {'name': 'Bluray-1080p', 'resolution': 1080}}},
    ]
    rel = 'movies/Film (2020)/Film.2020.1080p.BluRay.x264-OLD.mkv'
    media = [{'path': rel, 'size': 8_000_000_000, 'trackers': ['None']}]
    arr_media = [{'service': 'radarr', 'connection_id': 'radarr-mv', 'arr_id': 7, 'title': 'Film',
                  'path': f'/data/media/{rel}', 'title_slug': 'film', 'file_id': 70,
                  'file_quality_name': 'Bluray-1080p', 'file_hdr': ''}]
    cfg = {'MEDIA_PATH': '/data/media', 'ARR_CONNECTIONS': [
        {'id': 'radarr-mv', 'service': 'radarr', 'name': 'Movies',
         'base_url': 'http://mv:7878', 'api_key': 'b'}]}
    body = {'count': 5}
    if release_rank:
        body['release_rank'] = release_rank

    client = app.app.test_client()
    with patch.object(app, 'AUDITORR_SECRET', ''), \
         patch.object(app, 'AUDITORR_REQUIRE_AUTH', False), \
         patch.object(app, 'db_load_config', return_value=cfg), \
         patch.object(app, 'db_load_file_results', return_value=media), \
         patch.object(app, 'fetch_arr_media_index', return_value=arr_media), \
         patch.object(app, 'arr_media_index_errors', return_value=[]), \
         patch('arr._arr_get', return_value=releases):
        job_id = client.post('/api/workflows/generate', json=body, environ_base=_ENV).get_json()['job_id']
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = client.get(f'/api/workflows/generate/status?job_id={job_id}',
                                environ_base=_ENV).get_json()
            if status['status'] != 'running':
                return status['results'][0]
            time.sleep(0.01)
    raise AssertionError('the generate job never finished')


def test_a_run_picks_the_closest_release_by_default():
    result = _generate()

    assert result['best_release']['guid'] == 'old'
    assert result['best_release']['match']['size'] == 'same'


def test_a_run_can_ask_for_the_best_available_instead():
    assert _generate('upgrade')['best_release']['guid'] == 'remux'
