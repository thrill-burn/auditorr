import unittest

from app import _classify_triage_junk, _triage_exclusion_suggestions


class ClassifyTriageJunkTests(unittest.TestCase):
    def test_sidecar_extension(self):
        self.assertEqual(
            _classify_triage_junk("tv-sonarr/Show.S01E01/show.s01e01.sfv"), ("ext", ".sfv"))
        self.assertEqual(
            _classify_triage_junk("x/release.nfo"), ("ext", ".nfo"))

    def test_sample(self):
        self.assertEqual(
            _classify_triage_junk("movies/Film/Sample/film.sample.mkv"), ("sample", None))
        self.assertEqual(
            _classify_triage_junk("movies/Film/film-sample.mkv"), ("sample", None))

    def test_disc_structures(self):
        self.assertEqual(
            _classify_triage_junk("movies/Film 2020/BDMV/STREAM/00800.m2ts"), ("disc", "bluray"))
        self.assertEqual(
            _classify_triage_junk("tv/Show/VIDEO_TS/VTS_01_1.VOB"), ("disc", "dvd"))

    def test_real_video_is_not_junk(self):
        self.assertEqual(
            _classify_triage_junk("movies/Real.Movie.2020.1080p.BluRay.x265.mkv"), (None, None))
        # "sample" must be a whole token, not a substring of a real title
        self.assertEqual(
            _classify_triage_junk("movies/Free.Sampler.2019.1080p.mkv"), (None, None))


class TriageSuggestionAggregationTests(unittest.TestCase):
    def test_aggregates_and_sorts_by_count(self):
        items = [
            {"rep_path": "a.sfv", "total_size": 1000},
            {"rep_path": "b.sfv", "total_size": 2000},
            {"rep_path": "c/Sample/x.sample.mkv", "total_size": 5_000_000},
            {"rep_path": "real.mkv", "total_size": 9_000_000_000},
        ]
        sugg = _triage_exclusion_suggestions(items)
        ids = [s["id"] for s in sugg]
        self.assertEqual(ids[0], "ext:.sfv")          # highest count first
        self.assertIn("sample", ids)
        self.assertNotIn("real.mkv", str(sugg))        # real video produces no suggestion
        sfv = next(s for s in sugg if s["id"] == "ext:.sfv")
        self.assertEqual(sfv["count"], 2)
        self.assertEqual(sfv["size"], 3000)
        self.assertEqual(sfv["patterns"], ["ext:sfv"])

    def test_disc_uses_preset_patterns(self):
        sugg = _triage_exclusion_suggestions([
            {"rep_path": "m/Film/BDMV/STREAM/0.m2ts", "total_size": 1}])
        self.assertEqual(sugg[0]["patterns"], ["BDMV", "CERTIFICATE"])


if __name__ == "__main__":
    unittest.main()
