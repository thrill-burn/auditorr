"""The shared parse helpers in arr.py.

Three small pure functions that every workflow reads through, each of which
answered a question wrongly rather than declining to answer it — which is the
failure mode that survives review, because a confident wrong value looks exactly
like a right one everywhere downstream.
"""
import unittest
from unittest.mock import patch

import arr
from arr import parse_release_info, _detect_hdr, season_episodes_from_name


class ReleaseNameExtensionTests(unittest.TestCase):
    """`os.path.splitext` takes everything after the final dot.

    Release names are dot-separated by convention, so
    `splitext('Some.Movie.2020')` returns ('Some.Movie', '.2020') and the year
    is parsed off as an extension that does not exist. Touches Backfill and
    Triage, both of which parse torrent paths through here.
    """

    def test_a_dotted_release_name_keeps_its_year(self):
        self.assertEqual(parse_release_info('Some.Movie.2020')['year'], 2020)

    def test_a_real_extension_is_still_stripped(self):
        info = parse_release_info('Some.Movie.2020.1080p.BluRay.x264-GRP.mkv')
        self.assertEqual(info['year'], 2020)
        self.assertEqual(info['resolution'], '1080p')
        self.assertEqual(info['source'], 'bluray')

    def test_a_folder_name_with_a_dotted_year_survives(self):
        # Season-pack folders carry no extension at all, so splitext's guess was
        # never anything but a guess.
        self.assertEqual(parse_release_info('Show.S03.2019.1080p.WEB-DL')['year'], 2019)
        self.assertEqual(parse_release_info('Show.S03.2019.1080p.WEB-DL')['season'], 3)

    def test_release_group_tag_survives_a_sidecar_extension(self):
        self.assertEqual(arr._release_group_tag('Movie.2020.1080p.BluRay-GRP.nfo'), 'grp')


class DetectHdrTests(unittest.TestCase):
    """`_detect_hdr` against a full path labels a whole library off one segment.

    Only the two media-index call sites were wrong: they pass the arr's absolute
    file path. The release-title call sites are correct as they are and are left
    alone.
    """

    def test_a_path_segment_does_not_set_hdr(self):
        rows = self._radarr_rows('/data/media/HDR/Movie (2020)/Movie.2020.1080p.mkv')
        self.assertEqual(rows[0]['file_hdr'], '')

    def test_a_dv_segment_does_not_set_hdr(self):
        rows = self._radarr_rows('/data/media/DV/Movie (2020)/Movie.2020.1080p.mkv')
        self.assertEqual(rows[0]['file_hdr'], '')

    def test_the_filename_still_sets_hdr(self):
        rows = self._radarr_rows('/data/media/Movies/M (2020)/M.2020.2160p.DV.HDR10.mkv')
        self.assertEqual(rows[0]['file_hdr'], 'DV')

    def test_release_titles_are_untouched(self):
        self.assertEqual(_detect_hdr('Movie.2020.2160p.HDR10.WEB-DL'), 'HDR10')

    @staticmethod
    def _radarr_rows(path):
        conn = {'id': 'r', 'name': 'R', 'service': 'radarr',
                'base_url': 'http://r:7878', 'api_key': 'k'}
        movie = {'id': 1, 'title': 'M', 'year': 2020,
                 'movieFile': {'id': 2, 'path': path, 'relativePath': 'x.mkv',
                               'quality': {'quality': {'name': 'WEBDL-1080p'}}}}
        with patch('arr._arr_get', return_value=[movie]):
            rows, _partial = arr._fetch_radarr_media(conn)
        return rows


class SeasonEpisodeParseTests(unittest.TestCase):
    """The two-digit episode cap did not fail to match — it matched *wrongly*.

    `[Ee](\\d{1,2})` against S01E120 returns episode 12, which then resolves to
    a real but unrelated episode id and searches for it.
    """

    def test_a_three_digit_episode_parses_whole(self):
        self.assertEqual(season_episodes_from_name('Show.S01E120.1080p.mkv'), (1, [120]))

    def test_a_two_digit_episode_still_parses(self):
        self.assertEqual(season_episodes_from_name('Show.S04E07.1080p.WEB-DL.mkv'), (4, [7]))

    def test_a_multi_episode_file_reports_every_episode(self):
        self.assertEqual(season_episodes_from_name('Show.S01E01E02.1080p.mkv'), (1, [1, 2]))
        self.assertEqual(season_episodes_from_name('Show.S01E01-E02.1080p.mkv'), (1, [1, 2]))

    def test_a_quality_token_is_never_read_as_an_episode(self):
        # The bare "S01E01-02" form is deliberately unparsed: without the E the
        # trailing number is indistinguishable from a resolution.
        self.assertEqual(season_episodes_from_name('Show.S01E01-720p.mkv'), (1, [1]))

    def test_nothing_to_parse(self):
        self.assertEqual(season_episodes_from_name('Movie.2020.1080p.mkv'), (None, []))


class EpisodeIdLookupTests(unittest.TestCase):
    def _lookup(self, filename, episodes):
        conn = {'id': 's', 'base_url': 'http://s:8989', 'api_key': 'k'}
        with patch('arr._arr_get', return_value=episodes):
            return arr._episode_id_from_path(conn, 1, filename)

    def test_three_digit_episode_resolves_to_itself(self):
        episodes = [{'seasonNumber': 1, 'episodeNumber': 12,  'id': 900},
                    {'seasonNumber': 1, 'episodeNumber': 120, 'id': 901}]
        self.assertEqual(self._lookup('Show.S01E120.mkv', episodes), 901)

    def test_multi_episode_file_falls_through_to_a_known_episode(self):
        episodes = [{'seasonNumber': 1, 'episodeNumber': 2, 'id': 902}]
        self.assertEqual(self._lookup('Show.S01E01-E02.mkv', episodes), 902)

    def test_an_unknown_episode_stays_unknown(self):
        self.assertIsNone(self._lookup('Show.S01E99.mkv', [
            {'seasonNumber': 1, 'episodeNumber': 1, 'id': 900}]))


if __name__ == '__main__':
    unittest.main()
