import unittest

from arr import parse_trump_pm, match_trump_release, match_trumped_torrent, _norm_release_name


class TrumpPMParseTests(unittest.TestCase):
    def test_standard_pm(self):
        pm = (
            "The following torrent(s) have been trumped\n\n"
            "    Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos HDR x265-HQMUX\n\n"
            "and will be replaced by\n"
            "Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-RandomBytes.\n\n"
            "Reason: DV/HDR replacing HDR"
        )
        old, new = parse_trump_pm(pm)
        self.assertEqual(old, ["Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos HDR x265-HQMUX"])
        self.assertEqual(new, "Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-RandomBytes")

    def test_dotted_names_without_and_or_reason(self):
        pm = (
            "Your torrent has been trumped\n"
            "Show.S01E01.1080p.WEB-DL.x264-OLD\n"
            "will be replaced by Show.S01E01.1080p.WEB-DL.x265-NEW"
        )
        old, new = parse_trump_pm(pm)
        self.assertEqual(old, ["Show.S01E01.1080p.WEB-DL.x264-OLD"])
        self.assertEqual(new, "Show.S01E01.1080p.WEB-DL.x265-NEW")

    def test_season_pack_lists_every_trumped_episode(self):
        # N episodes trumped by one season pack — all N old titles must be kept.
        pm = (
            "The following torrent(s) have been trumped\n\n"
            "    FROM S04E01 The Arrival REPACK 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune\n"
            "    FROM S04E02 Fray 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune\n"
            "    FROM S04E03 Episode 3 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune\n\n\n"
            "and will be replaced by\n"
            "FROM S04 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune.\n\n"
            "Reason:\nSeason Pack"
        )
        old, new = parse_trump_pm(pm)
        self.assertEqual(old, [
            "FROM S04E01 The Arrival REPACK 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune",
            "FROM S04E02 Fray 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune",
            "FROM S04E03 Episode 3 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune",
        ])
        self.assertEqual(new, "FROM S04 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune")

    def test_no_delimiter_returns_empty(self):
        self.assertEqual(parse_trump_pm("just some random text"), ([], ""))
        self.assertEqual(parse_trump_pm(""), ([], ""))

    def test_crlf_normalized(self):
        pm = "Trumped\r\nOld.Release-A\r\nwill be replaced by\r\nNew.Release-B."
        old, new = parse_trump_pm(pm)
        self.assertEqual(old, ["Old.Release-A"])
        self.assertEqual(new, "New.Release-B")


class TrumpedTorrentMatchTests(unittest.TestCase):
    def _rows(self, *names):
        return [{"name": n, "hash": n, "size": 1} for n in names]

    def test_exact_normalized_match(self):
        rows = self._rows("FROM.S04E01.The.Arrival.REPACK.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune")
        m = match_trumped_torrent(rows, "FROM.S04E01.The.Arrival.REPACK.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune")
        self.assertIsNotNone(m)

    def test_strong_overlap_tolerates_audio_rendering(self):
        # PM prints "DD+ 5.1"; the actual torrent says "DDP5.1" — neither exact
        # nor subset, but the overlap fallback finds it.
        rows = self._rows("FROM.S04E01.The.Arrival.REPACK.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune")
        m = match_trumped_torrent(rows, "FROM S04E01 The Arrival REPACK 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune")
        self.assertIsNotNone(m)

    def test_never_crosses_to_a_different_episode(self):
        rows = self._rows("FROM.S04E02.Fray.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune")
        m = match_trumped_torrent(rows, "FROM S04E01 The Arrival REPACK 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune")
        self.assertIsNone(m)

    def test_never_crosses_to_a_different_group(self):
        # Same episode, different encode group — distinct payload, must not match.
        rows = self._rows("FROM.S04E01.The.Arrival.2160p.AMZN.WEB-DL.DDP5.1.H.265-NTb")
        m = match_trumped_torrent(rows, "FROM S04E01 The Arrival REPACK 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune")
        self.assertIsNone(m)

    def test_episode_does_not_match_season_pack(self):
        rows = self._rows("FROM.S04.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune")
        m = match_trumped_torrent(rows, "FROM S04E01 The Arrival REPACK 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune")
        self.assertIsNone(m)

    def test_no_match_returns_none(self):
        rows = self._rows("Completely.Different.Show.S01E01.1080p-XYZ")
        self.assertIsNone(match_trumped_torrent(rows, "FROM S04E01 The Arrival 2160p-Kitsune"))
        self.assertIsNone(match_trumped_torrent(rows, ""))


class TrumpReleaseMatchTests(unittest.TestCase):
    def setUp(self):
        self.releases = [
            {"title": "Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-RandomBytes",
             "indexer": "Aither", "seeders": 12},
            {"title": "Jumanji.1995.2160p.UHD.BluRay.x265-OTHER",
             "indexer": "Aither", "seeders": 50},
        ]

    def test_exact_title_wins_over_seeders(self):
        # The near-match has far more seeders but the exact normalized title wins
        m = match_trump_release(
            self.releases, "Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-RandomBytes")
        self.assertIsNotNone(m)
        self.assertTrue(m["title"].endswith("RandomBytes"))

    def test_dots_vs_spaces_equivalent(self):
        m = match_trump_release(
            self.releases, "Jumanji.1995.2160p.UHD.BluRay.TrueHD.7.1.Atmos.DV.HDR.x265-RandomBytes")
        self.assertIsNotNone(m)
        self.assertTrue(m["title"].endswith("RandomBytes"))

    def test_indexer_filter(self):
        m = match_trump_release(
            self.releases, "Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-RandomBytes",
            indexer="OtherTracker")
        self.assertIsNone(m)

    def test_no_match_returns_none(self):
        self.assertIsNone(match_trump_release(self.releases, "Completely Different Release-XYZ"))
        self.assertIsNone(match_trump_release(self.releases, ""))

    def test_multiple_exact_picks_highest_seeders(self):
        rels = [
            {"title": "A.Movie.2020.1080p-GRP", "indexer": "X", "seeders": 3},
            {"title": "A.Movie.2020.1080p-GRP", "indexer": "Y", "seeders": 99},
        ]
        m = match_trump_release(rels, "A Movie 2020 1080p-GRP")
        self.assertEqual(m["seeders"], 99)


class NormalizeReleaseNameTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(_norm_release_name("Show.S01E01.WEB-DL"), "show s01e01 web-dl")
        self.assertEqual(_norm_release_name("A__B  C"), "a b c")


if __name__ == "__main__":
    unittest.main()
