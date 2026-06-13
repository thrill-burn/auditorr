import unittest

from arr import parse_trump_pm, match_trump_release, _norm_release_name


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
        self.assertEqual(old, "Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos HDR x265-HQMUX")
        self.assertEqual(new, "Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-RandomBytes")

    def test_dotted_names_without_and_or_reason(self):
        pm = (
            "Your torrent has been trumped\n"
            "Show.S01E01.1080p.WEB-DL.x264-OLD\n"
            "will be replaced by Show.S01E01.1080p.WEB-DL.x265-NEW"
        )
        old, new = parse_trump_pm(pm)
        self.assertEqual(old, "Show.S01E01.1080p.WEB-DL.x264-OLD")
        self.assertEqual(new, "Show.S01E01.1080p.WEB-DL.x265-NEW")

    def test_no_delimiter_returns_empty(self):
        self.assertEqual(parse_trump_pm("just some random text"), ("", ""))
        self.assertEqual(parse_trump_pm(""), ("", ""))

    def test_crlf_normalized(self):
        pm = "Trumped\r\nOld.Release-A\r\nwill be replaced by\r\nNew.Release-B."
        old, new = parse_trump_pm(pm)
        self.assertEqual(old, "Old.Release-A")
        self.assertEqual(new, "New.Release-B")


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
