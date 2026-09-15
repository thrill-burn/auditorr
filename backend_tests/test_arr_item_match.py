"""Choosing *which* arr item a parsed release belongs to.

`rank_arr_candidates` and `arr_year_ok` replace the two shapes auditorr used
everywhere a title lookup returned more than one row:

  * a **service preference expressed as a sort key** — `sort(preferred first)`
    then `[0]`, which silently falls back to the wrong content type whenever the
    right one has no rows (TRIAGE T1's bug at its second call site, TRUMPED TR7);
  * **`rows[0]`** — rows from every connection pooled under one title key, so
    the answer was whichever instance `normalize_arr_connections` emitted first.

Both are pure functions over candidate rows, which is why they are testable on
an install that has exactly one Sonarr and one Radarr to offer them (M4). The
*UI* half of TRIAGE T2 — saying on the row that two instances both hold a title
— is deferred, because that is the part no fixture can exercise.
"""
import unittest
from unittest.mock import patch

import app
from arr import arr_year_ok, rank_arr_candidates, parse_release_info


def _title(**over):
    base = {'service': 'sonarr', 'connection_id': 'c1', 'arr_id': 1,
            'title': 'Show', 'title_slug': 'show', 'year': 2019,
            'has_file': True, 'alt_titles': []}
    base.update(over)
    return base


class ArrYearGateTests(unittest.TestCase):

    def test_radarr_tolerates_one_year_of_drift(self):
        parsed = {'title': 'Snow White', 'year': 1938}
        self.assertTrue(arr_year_ok(parsed, {'service': 'radarr', 'year': 1937}))
        self.assertFalse(arr_year_ok(parsed, {'service': 'radarr', 'year': 1997}))

    def test_sonarr_rejects_a_release_predating_the_series(self):
        """The remake case the gate exists for: a 1990 release name against the
        2019 reboot of the same title."""
        parsed = {'title': 'The Show', 'year': 1990}
        self.assertFalse(arr_year_ok(parsed, {'service': 'sonarr', 'year': 2019}))

    def test_sonarr_never_rejects_a_later_air_date(self):
        """Sonarr stores a series' *first air* year while a TV release name
        carries the episode's air date, so ±1 would disqualify every daily
        series and everything past season two."""
        parsed = {'title': 'The Show', 'year': 2024}
        self.assertTrue(arr_year_ok(parsed, {'service': 'sonarr', 'year': 2015}))

    def test_a_title_that_is_a_year_is_not_a_release_year(self):
        """'1923' parses its own name as the year; the arr stores 2022."""
        parsed = parse_release_info('1923.S01E01.1080p.WEB-DL.mkv')
        self.assertEqual(parsed['year'], 1923)
        self.assertTrue(arr_year_ok(parsed, {'service': 'sonarr', 'year': 2022}))
        self.assertTrue(arr_year_ok(parsed, {'service': 'radarr', 'year': 2022}))

    def test_unknown_on_either_side_passes(self):
        self.assertTrue(arr_year_ok({'title': 'X', 'year': None}, {'service': 'radarr', 'year': 2020}))
        self.assertTrue(arr_year_ok({'title': 'X', 'year': 2020}, {'service': 'radarr', 'year': None}))


class RankArrCandidatesTests(unittest.TestCase):

    def test_service_is_a_gate_not_a_preference(self):
        parsed = parse_release_info('Fargo.S05E01.1080p.WEB-DL.mkv')
        rows = [_title(service='radarr', title='Fargo', year=1996, arr_id=42)]
        self.assertEqual(rank_arr_candidates(rows, parsed, service='sonarr'), [])

    def test_ties_keep_input_order(self):
        """A single-instance install must get byte-identical answers to the old
        `[0]`: the sort is stable and nothing distinguishes these two."""
        parsed = parse_release_info('Show.S01E01.1080p.mkv')
        a, b = _title(connection_id='c1'), _title(connection_id='c2')
        ranked = rank_arr_candidates([a, b], parsed, service='sonarr')
        self.assertEqual([r['connection_id'] for r in ranked], ['c1', 'c2'])

    def test_the_season_anchor_outranks_emission_order(self):
        parsed = parse_release_info('Show.S03E04.1080p.mkv')
        wrong = {'service': 'sonarr', 'connection_id': 'c1', 'arr_id': 1,
                 'title': 'Show', 'season_number': 1, 'episode_numbers': [4],
                 'path': '/tv/Show/Season 01/Show.S01E04.mkv'}
        right = {'service': 'sonarr', 'connection_id': 'c2', 'arr_id': 1,
                 'title': 'Show', 'season_number': 3, 'episode_numbers': [4],
                 'path': '/tv/Show/Season 03/Show.S03E04.mkv'}
        ranked = rank_arr_candidates([wrong, right], parsed, service='sonarr')
        self.assertEqual(ranked[0]['connection_id'], 'c2')

    def test_the_season_anchor_falls_back_to_the_filename(self):
        """Rows written before Sonarr's own numbers were carried, and title-list
        rows, which have no per-file season at all."""
        parsed = parse_release_info('Show.S03E04.1080p.mkv')
        wrong = {'service': 'sonarr', 'connection_id': 'c1', 'arr_id': 1,
                 'title': 'Show', 'relative_path': 'Season 01/Show.S01E04.mkv'}
        right = {'service': 'sonarr', 'connection_id': 'c2', 'arr_id': 1,
                 'title': 'Show', 'relative_path': 'Season 03/Show.S03E04.mkv'}
        ranked = rank_arr_candidates([wrong, right], parsed, service='sonarr')
        self.assertEqual(ranked[0]['connection_id'], 'c2')

    def test_an_exact_year_outranks_a_tolerated_one(self):
        parsed = parse_release_info('Movie.2020.1080p.BluRay.mkv')
        near  = _title(service='radarr', connection_id='c1', title='Movie', year=2019)
        exact = _title(service='radarr', connection_id='c2', title='Movie', year=2020)
        ranked = rank_arr_candidates([near, exact], parsed, service='radarr')
        self.assertEqual(ranked[0]['connection_id'], 'c2')

    def test_the_year_gate_still_drops_a_remake(self):
        parsed = parse_release_info('Movie.2020.1080p.BluRay.mkv')
        rows = [_title(service='radarr', title='Movie', year=1974)]
        self.assertEqual(rank_arr_candidates(rows, parsed, service='radarr'), [])


class TrumpFindArrItemTests(unittest.TestCase):
    """TR7 — `_trump_find_arr_item` is 4b's real call site."""

    @staticmethod
    def _find(name, titles):
        parsed = app.parse_release_info_for_path(name)
        return app._trump_find_arr_item({}, parsed, titles=titles, name=name)

    def test_a_same_titled_movie_never_answers_for_an_episode(self):
        item = self._find('Fargo.S05E01.1080p.WEB-DL-GRP.mkv',
                          [_title(service='radarr', title='Fargo', year=1996, arr_id=42)])
        self.assertIsNone(item)

    def test_a_same_titled_series_never_answers_for_a_movie(self):
        item = self._find('Dune.2021.2160p.UHD.BluRay-GRP.mkv',
                          [_title(service='sonarr', title='Dune', year=2021, arr_id=9)])
        self.assertIsNone(item)

    def test_the_right_type_still_resolves(self):
        item = self._find('Show.S01E02.1080p.WEB-DL-GRP.mkv',
                          [_title(service='sonarr', title='Show', year=2019, arr_id=5)])
        self.assertEqual(item['arr_id'], 5)

    def test_a_sonarr_remake_is_rejected_on_year(self):
        """Two series sharing a title used to resolve on the title alone —
        `_trump_year_ok` was explicitly radarr-only."""
        item = self._find('The Show 1990 S01E01 1080p WEB-DL-GRP.mkv',
                          [_title(service='sonarr', title='The Show', year=2019, arr_id=5)])
        self.assertIsNone(item)

    def test_a_long_running_series_is_not_rejected_on_year(self):
        item = self._find('The Show 2024 03 11 1080p WEB-DL-GRP.mkv',
                          [_title(service='sonarr', title='The Show', year=2015, arr_id=5)])
        self.assertEqual(item['arr_id'], 5)

    def test_a_daily_series_resolves_to_sonarr_despite_having_no_season_token(self):
        """The service is a gate now, so guessing radarr for an air-dated
        release would 404 rather than merely mis-rank."""
        item = self._find('The Show 2024 03 11 1080p WEB-DL-GRP.mkv',
                          [_title(service='sonarr', title='The Show', year=2015, arr_id=5)])
        self.assertIsNotNone(item)
        self.assertEqual(item['service'], 'sonarr')

    def test_an_alternate_title_rescues_a_release_named_in_its_own_language(self):
        """T15's primitives at TR7's call site — the arr could only have matched
        the grab because it consults this same field."""
        titles = [_title(service='sonarr', title="I'm Not Afraid", year=2018, arr_id=5,
                         alt_titles=['No tengo miedo'])]
        item = self._find('No.tengo.miedo.S01E01.1080p.WEB-DL-GRP.mkv', titles)
        self.assertEqual(item['arr_id'], 5)

    def test_an_exact_title_still_beats_an_alias(self):
        titles = [
            _title(service='sonarr', title='Other Show', year=2018, arr_id=9,
                   alt_titles=['Show']),
            _title(service='sonarr', title='Show', year=2019, arr_id=5),
        ]
        item = self._find('Show.S01E01.1080p.WEB-DL-GRP.mkv', titles)
        self.assertEqual(item['arr_id'], 5)

    def test_the_soft_fallback_is_gated_on_service_too(self):
        item = self._find('The Magic School Bus Rides Again S01 1080p WEB-DL-GRP',
                          [_title(service='sonarr', title='The Magic School Bus Rides Again',
                                  year=2017, arr_id=5)])
        self.assertEqual(item['arr_id'], 5)


class TrumpSearchReleaseArrErrorTests(unittest.TestCase):
    """A failed arr and an absent title are the same empty list and are not the
    same answer — telling a user to add a title their arr is already managing is
    how a trump ends in the wrong grab."""

    def _post(self, titles, errors):
        with patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app, 'fetch_arr_all_titles_result', return_value=(titles, errors)), \
             patch.object(app, 'normalize_arr_connections', return_value=[]):
            return app.app.test_client().post(
                '/api/workflows/trump/search_release',
                json={'new_title': 'Nothing.Matches.This.2020.1080p.BluRay-GRP'})

    def test_an_unreachable_arr_is_a_502_not_a_404(self):
        resp = self._post([], [{'connection_id': 'r1', 'name': 'Radarr',
                                'service': 'radarr', 'partial': False,
                                'message': 'timed out'}])
        self.assertEqual(resp.status_code, 502)
        body = resp.get_json()
        self.assertIn('Could not check', body['message'])
        self.assertIn('Radarr', body['message'])
        self.assertEqual(len(body['arr_errors']), 1)

    def test_a_healthy_arr_that_has_never_heard_of_it_is_still_a_404(self):
        resp = self._post([], [])
        self.assertEqual(resp.status_code, 404)
        self.assertIn('add the title to an arr first', resp.get_json()['message'])


if __name__ == '__main__':
    unittest.main()
