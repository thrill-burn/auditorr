"""Triage's library-matching block.

Phase 9 item 1 opens with this file properly — T2's instance ranking, T5's
per-file verdict spread and the rest of the 40 lines every review independently
flagged as the least-tested, highest-consequence code in the repo. What is here
now is the one assertion Phase 4a adds, written with the change it belongs to:

**an arr that did not answer must never produce `not_in_library`.**

That verdict means "no arr has ever heard of this", and its UI copy ends
"junk can be deleted". A not-imported torrent has no hardlink anywhere by
definition, so its files are the only copy — this is the verdict that cost a
field reporter 33 GB when title matching missed (T15), and an unreachable arr
reaches it by a shorter road: `fetch_arr_media_index` returns `[]` on failure,
`arr_configured` is derived from the config rather than the fetch so it stays
true, and nothing else on the response said anything had gone wrong.
"""
import unittest
from unittest.mock import patch

import app


def _rec(**over):
    base = {
        'path': 'radarr/Movie.2020.1080p.WEB-DL/Movie.2020.1080p.WEB-DL.mkv',
        'size': 100, 'status': 'Seeding', 'imported': False, 'excluded': False,
        'hash': 'AAAA', 'instance_id': 1, 'trackers': ['tracker.example'],
        'tracker_health': 'unknown', 'tracker_msg': '',
    }
    base.update(over)
    return base


def _triage(records, *, index_errors=(), title_errors=(), media=(), titles=()):
    """GET /api/workflows/triage with the arr layer's health stated explicitly."""
    with patch.object(app, 'db_load_config', return_value={}), \
         patch.object(app, 'db_has_file_results', return_value=True), \
         patch.object(app, 'db_load_file_results', return_value=records), \
         patch.object(app, 'fetch_arr_media_index', return_value=list(media)), \
         patch.object(app, 'fetch_arr_all_titles', return_value=list(titles)), \
         patch.object(app, 'arr_media_index_errors', return_value=list(index_errors)), \
         patch.object(app, 'arr_titles_errors', return_value=list(title_errors)), \
         patch.object(app, 'normalize_arr_connections', return_value=[]), \
         patch.object(app.sources, 'fetch_torrent_details',
                      side_effect=AssertionError('phase 1 must not call the client')):
        return app.app.test_client().get('/api/workflows/triage').get_json()


def _down(name='Main Sonarr', cid='s1'):
    return {'connection_id': cid, 'name': name, 'service': 'sonarr',
            'partial': False, 'message': 'timed out'}


class ArrUnreachableVerdictTests(unittest.TestCase):

    def test_an_unreachable_arr_never_produces_not_in_library(self):
        report = _triage([_rec()], index_errors=[_down()], title_errors=[_down()])
        self.assertEqual([i['verdict'] for i in report['items']], ['library_unknown'])

    def test_the_alternatives_carry_the_gate_too(self):
        """The client re-resolves every verdict from these after live tracker
        verification, so a gate applied only to the phase-1 verdict would be
        undone the moment the batch came back."""
        report = _triage([_rec()], index_errors=[_down()])
        alts = report['items'][0]['verdict_alternatives']
        self.assertEqual(alts['working'], 'library_unknown')
        self.assertEqual(alts['other'], 'library_unknown')
        self.assertEqual(alts['unregistered'], 'unregistered')

    def test_a_partial_read_counts_as_unreachable(self):
        """Sonarr needs one call per series, so the likely failure is that some
        of the library came back. The missing series are exactly the ones whose
        episodes would read as "no arr has heard of this"."""
        partial = {'connection_id': 's1', 'name': 'TV', 'service': 'sonarr',
                   'partial': True, 'failed': 12, 'total': 400,
                   'message': '12 of 400 series could not be read'}
        report = _triage([_rec()], index_errors=[partial])
        self.assertEqual(report['items'][0]['verdict'], 'library_unknown')

    def test_the_failure_is_named_on_the_response(self):
        report = _triage([_rec()], index_errors=[_down(name='TV')], title_errors=[_down(name='TV')])
        self.assertEqual(len(report['arr_errors']), 2)
        self.assertEqual(report['arr_errors'][0]['name'], 'TV')

    def test_a_healthy_arr_still_says_not_in_library(self):
        """The gate must not cost the working path — an arr that answered and
        has never heard of the title is a real verdict, and it is the one the
        exclusion suggestions and the delete button are for."""
        report = _triage([_rec()])
        self.assertEqual([i['verdict'] for i in report['items']], ['not_in_library'])
        self.assertEqual(report['arr_errors'], [])

    def test_a_positive_match_is_not_suppressed(self):
        """Only the verdict derived purely from silence is gated. An instance
        that *did* answer and holds the title still reports `superseded`, even
        while a different instance is down — suppressing that would throw away
        evidence auditorr actually has."""
        media = [{'connection_id': 'r1', 'connection_name': 'main', 'service': 'radarr',
                  'title': 'Movie', 'year': 2020, 'arr_id': 7, 'file_id': 1,
                  'path': '/media/movies/Movie (2020)/Movie.2020.1080p.WEB-DL.mkv',
                  'relative_path': 'Movie.2020.1080p.WEB-DL.mkv',
                  'title_slug': 'movie', 'file_quality_name': 'WEBDL-1080p', 'file_hdr': ''}]
        report = _triage([_rec()], media=media, index_errors=[_down()])
        self.assertEqual(report['items'][0]['verdict'], 'superseded')
        self.assertEqual(report['items'][0]['library']['arr_id'], 7)


if __name__ == '__main__':
    unittest.main()
