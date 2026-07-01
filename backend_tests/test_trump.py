import unittest

from arr import (
    parse_trump_pm, match_trump_release, match_trumped_torrent, _norm_release_name,
    rank_release_matches, score_release_match, _audio_codec, title_soft_match,
)


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


class AudioCodecTests(unittest.TestCase):
    def test_renderings_normalize_to_one_family(self):
        for name in ("Show DD+ 5.1", "Show DDP5.1", "Show.E-AC3.5.1", "Show EAC3"):
            self.assertEqual(_audio_codec(name), 'ddp', name)
        self.assertEqual(_audio_codec("Show TrueHD 7.1 Atmos"), 'truehd')
        self.assertEqual(_audio_codec("Show DTS-HD MA 5.1"), 'dtshd')
        self.assertEqual(_audio_codec("Show DTS 5.1"), 'dts')
        self.assertEqual(_audio_codec("Show x265 no audio"), '')


class RankReleaseMatchesTests(unittest.TestCase):
    def _rows(self, *names):
        return [{"name": n, "hash": n, "size": 1, "seeders": 0} for n in names]

    def test_exact_match_scores_top(self):
        rows = self._rows(
            "FROM.S04E01.The.Arrival.REPACK.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune",
            "Completely.Different.Show.S01E01.1080p-XYZ",
        )
        ranked = rank_release_matches(
            rows, "FROM S04E01 The Arrival REPACK 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune", "name")
        self.assertTrue(ranked[0]["name"].startswith("FROM.S04E01"))
        self.assertEqual(ranked[0]["match_score"], 1.0)

    def test_audio_rendering_does_not_break_the_top_match(self):
        # PM prints "DD+ 5.1"; torrent says "DDP5.1" — still the #1 candidate.
        rows = self._rows("FROM.S04E01.The.Arrival.REPACK.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune")
        ranked = rank_release_matches(
            rows, "FROM S04E01 The Arrival REPACK 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune", "name")
        self.assertEqual(ranked[0]["match"]["audio"], "same")
        self.assertEqual(ranked[0]["match"]["anchor"], "same")

    def test_codec_notation_difference_does_not_penalize_title(self):
        # PM writes "H.265", torrent writes "x265" — title core must still match.
        score, brk = score_release_match(
            "Movie 2020 2160p BluRay H.265-GRP", "Movie 2020 2160p BluRay x265-GRP")
        self.assertEqual(brk["title"], "same")

    def test_different_episode_is_kept_out_of_the_top(self):
        rows = self._rows(
            "FROM.S04E01.The.Arrival.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune",
            "FROM.S04E02.Fray.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune",
        )
        ranked = rank_release_matches(
            rows, "FROM S04E01 The Arrival 2160p AMZN WEB-DL DD+ 5.1 H.265-Kitsune",
            "name", min_score=0.2)
        self.assertTrue(ranked[0]["name"].startswith("FROM.S04E01"))
        self.assertTrue(all("S04E02" not in r["name"] for r in ranked))

    def test_same_title_other_quality_still_surfaces(self):
        # Same movie/year, different encode — a genuine title match, so it shows
        # (ranked lower via the quality diffs), letting the user vet it.
        rows = self._rows("Jumanji.1995.1080p.BluRay.DTS.x264-GRP")
        ranked = rank_release_matches(
            rows, "Jumanji 1995 2160p UHD BluRay TrueHD Atmos DV HDR x265-RandomBytes", "name")
        self.assertEqual(len(ranked), 1)

    def test_unrelated_titles_drop_out(self):
        rows = self._rows("Totally.Unrelated.Movie.2001.1080p-ABC")
        ranked = rank_release_matches(rows, "Jumanji 1995 2160p BluRay x265-XYZ", "name")
        self.assertEqual(ranked, [])

    def test_unrelated_title_with_matching_quality_is_excluded(self):
        # The screenshot bug: quality (1080p WEB-DL) agrees but the titles have
        # nothing in common — must NOT be offered as a match.
        rows = self._rows(
            "Flow.2019.1080p.WEB-DL.ARTE.AAC.H264.AYAKO.mkv",
            "Superworm.2021.1080p.iP.WEB-DL.H264.AAC2.0.SNAKE.mkv",
        )
        ranked = rank_release_matches(
            rows, "Obsession 2026 Director's Cut 1080p AMZN WEB-DL DD+ 5.1 H.264-KyoGo", "name")
        self.assertEqual(ranked, [])

    def test_real_title_matches_despite_edition_and_audio_rendering(self):
        # Same title/year; PM has "Director's Cut" + "DD+", torrent has neither
        # spelled the same — still the match.
        rows = self._rows("Obsession.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-KyoGo")
        ranked = rank_release_matches(
            rows, "Obsession 2026 Director's Cut 1080p AMZN WEB-DL DD+ 5.1 H.264-KyoGo", "name")
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["match"]["title"], "same")

    def test_year_gate_blocks_remakes(self):
        rows = self._rows("Obsession.1976.1080p.BluRay.x264-OLD")
        ranked = rank_release_matches(
            rows, "Obsession 2026 1080p WEB-DL DD+ 5.1 H.264-KyoGo", "name")
        self.assertEqual(ranked, [])


class TitleSoftMatchTests(unittest.TestCase):
    def test_stray_season_token_still_matches_series(self):
        # The step-4 bug: parsed new title keeps "S01"; the series has none.
        self.assertEqual(
            title_soft_match("The Magic School Bus Rides Again S01",
                             "The Magic School Bus Rides Again"), 1.0)

    def test_unrelated_titles_score_zero(self):
        self.assertEqual(title_soft_match("Obsession", "The Magic School Bus"), 0.0)

    def test_empty_is_safe(self):
        self.assertEqual(title_soft_match("", "Anything"), 0.0)
        self.assertEqual(title_soft_match("2160p 1080p", "Anything"), 0.0)

    def test_hdr_difference_is_reported(self):
        # The classic trump: same encode chain, HDR → DV. Breakdown must flag it.
        score, brk = score_release_match(
            "Jumanji 1995 2160p UHD BluRay TrueHD Atmos HDR x265-HQMUX",
            "Jumanji 1995 2160p UHD BluRay TrueHD Atmos DV HDR x265-RandomBytes")
        self.assertEqual(brk["hdr"], "diff")
        self.assertEqual(brk["group"], "diff")
        self.assertEqual(brk["title"], "same")


class NormalizeReleaseNameTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(_norm_release_name("Show.S01E01.WEB-DL"), "show s01e01 web-dl")
        self.assertEqual(_norm_release_name("A__B  C"), "a b c")


if __name__ == "__main__":
    unittest.main()
