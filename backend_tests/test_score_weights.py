import unittest
from unittest.mock import patch

from audit import process_health_metrics
from db import DEFAULT_CONFIG, SCORE_WEIGHT_KEYS, score_weight_points, validate_config

GB = 1024 ** 3

# One of three media files is hardlinked, so Hardlinked Media earns a third of
# whatever it is weighted. There is an orphan large enough to zero the Orphaned
# component, and nothing not-imported or duplicated — so those two earn full
# marks. That spread means every component contributes something distinguishable.
MEDIA = [
    {"size": 10 * GB, "file_id": "a", "linked_paths": ["/t/a"], "duplicate_paths": [],
     "status": "Seeding",  "imported": True},
    {"size": 10 * GB, "file_id": "b", "linked_paths": [], "duplicate_paths": [],
     "status": "Orphaned", "imported": True},
    {"size": 10 * GB, "file_id": "c", "linked_paths": [], "duplicate_paths": [],
     "status": "Orphaned", "imported": True},
]
TORRENTS = [
    {"size": 10 * GB, "file_id": "a", "linked_paths": ["/m/a"], "duplicate_paths": [],
     "status": "Seeding",  "imported": True},
    {"size":  1 * GB, "file_id": "d", "linked_paths": [], "duplicate_paths": [],
     "status": "Orphaned", "imported": False},
]
RATIOS = {"OR_RATIO": 0.01, "NI_RATIO": 0.01, "DUP_RATIO": 0.01}


def _score(cfg):
    with patch("audit.db_load_history", return_value={"hourly_stats": [], "daily_stats": []}):
        return process_health_metrics(MEDIA, TORRENTS, cfg, update_history=False)


class WeightNormalizationTests(unittest.TestCase):
    def test_defaults_are_the_original_fixed_allocation(self):
        self.assertEqual(
            {k: round(v, 4) for k, v in score_weight_points({}).items()},
            {"WEIGHT_HARDLINKED": 70.0, "WEIGHT_ORPHANED": 10.0,
             "WEIGHT_NOT_IMPORTED": 10.0, "WEIGHT_DUPLICATES": 10.0},
        )

    def test_points_always_total_100(self):
        for weights in (
            {k: 1 for k in SCORE_WEIGHT_KEYS},
            {"WEIGHT_HARDLINKED": 3, "WEIGHT_ORPHANED": 1,
             "WEIGHT_NOT_IMPORTED": 1, "WEIGHT_DUPLICATES": 1},
            {"WEIGHT_HARDLINKED": 0, "WEIGHT_ORPHANED": 7,
             "WEIGHT_NOT_IMPORTED": 0, "WEIGHT_DUPLICATES": 3},
            {"WEIGHT_HARDLINKED": 999, "WEIGHT_ORPHANED": 1,
             "WEIGHT_NOT_IMPORTED": 1, "WEIGHT_DUPLICATES": 1},
        ):
            self.assertAlmostEqual(sum(score_weight_points(weights).values()), 100.0, places=6)

    def test_zero_weight_earns_zero_points(self):
        pts = score_weight_points({"WEIGHT_HARDLINKED": 0})
        self.assertEqual(pts["WEIGHT_HARDLINKED"], 0.0)
        self.assertAlmostEqual(pts["WEIGHT_ORPHANED"], 100 / 3, places=6)

    def test_all_zero_and_garbage_fall_back_to_defaults(self):
        # Reachable from a hand-edited DB row even though the API rejects it —
        # must not divide by zero or produce a NaN score.
        for weights in ({k: 0 for k in SCORE_WEIGHT_KEYS},
                        {"WEIGHT_HARDLINKED": "abc"},
                        {"WEIGHT_ORPHANED": None}):
            pts = score_weight_points(weights)
            self.assertAlmostEqual(sum(pts.values()), 100.0, places=6)

    def test_negative_weights_are_clamped_not_subtracted(self):
        pts = score_weight_points({"WEIGHT_HARDLINKED": -50, "WEIGHT_ORPHANED": 10,
                                   "WEIGHT_NOT_IMPORTED": 10, "WEIGHT_DUPLICATES": 10})
        self.assertEqual(pts["WEIGHT_HARDLINKED"], 0.0)
        self.assertAlmostEqual(sum(pts.values()), 100.0, places=6)


class WeightedScoringTests(unittest.TestCase):
    def test_default_config_reproduces_the_pre_weighting_score(self):
        # hl 1/3 of 70 = 23.3; orphan exceeds its 1% allowance so or = 0;
        # nothing not-imported or duplicated so those earn 10 each.
        result = _score(dict(RATIOS))
        details = result["current"]["details"]
        self.assertEqual(result["score"], 43.3)
        self.assertEqual(details["hl_score"], 23.3)
        self.assertEqual(details["or_score"], 0)
        self.assertEqual(details["ni_score"], 10.0)
        self.assertEqual(details["dup_score"], 10.0)

    def test_zeroing_hardlinks_removes_its_penalty_entirely(self):
        result = _score(dict(RATIOS, WEIGHT_HARDLINKED=0))
        details = result["current"]["details"]
        self.assertEqual(details["hl_max"], 0.0)
        self.assertEqual(details["hl_score"], 0.0)
        # The other three now split 100; only the orphan still costs anything.
        self.assertEqual(result["score"], 66.7)

    def test_details_carry_the_point_maxima_for_the_dashboard(self):
        details = _score(dict(RATIOS, WEIGHT_HARDLINKED=1, WEIGHT_ORPHANED=1,
                              WEIGHT_NOT_IMPORTED=1, WEIGHT_DUPLICATES=1))["current"]["details"]
        for key in ("hl_max", "or_max", "ni_max", "dup_max"):
            self.assertEqual(details[key], 25.0)

    def test_no_component_can_exceed_its_weighted_maximum(self):
        cfg = dict(RATIOS, WEIGHT_HARDLINKED=40, WEIGHT_ORPHANED=30,
                   WEIGHT_NOT_IMPORTED=20, WEIGHT_DUPLICATES=10)
        details = _score(cfg)["current"]["details"]
        for score_key, max_key in (("hl_score", "hl_max"), ("or_score", "or_max"),
                                   ("ni_score", "ni_max"), ("dup_score", "dup_max")):
            self.assertLessEqual(details[score_key], details[max_key])
            self.assertGreaterEqual(details[score_key], 0)

    def test_score_stays_within_bounds_across_weightings(self):
        for weights in ({"WEIGHT_HARDLINKED": 100}, {"WEIGHT_ORPHANED": 100},
                        {"WEIGHT_DUPLICATES": 1, "WEIGHT_HARDLINKED": 0,
                         "WEIGHT_ORPHANED": 0, "WEIGHT_NOT_IMPORTED": 0}):
            score = _score(dict(RATIOS, **weights))["score"]
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)


class WeightValidationTests(unittest.TestCase):
    def test_all_four_zero_is_rejected(self):
        errors = validate_config({k: 0 for k in SCORE_WEIGHT_KEYS})
        self.assertEqual(len(errors), 1)
        self.assertIn("above zero", errors[0])

    def test_single_zero_is_allowed(self):
        self.assertEqual(validate_config({"WEIGHT_HARDLINKED": 0}), [])

    def test_partial_payload_is_not_treated_as_all_zero(self):
        # Missing keys fall back to their non-zero defaults on save, so three
        # zeros in a partial payload must not trip the all-zero guard.
        self.assertEqual(validate_config({"WEIGHT_HARDLINKED": 0, "WEIGHT_ORPHANED": 0,
                                          "WEIGHT_NOT_IMPORTED": 0}), [])

    def test_negative_and_non_numeric_are_rejected(self):
        self.assertTrue(validate_config({"WEIGHT_ORPHANED": -1}))
        self.assertTrue(validate_config({"WEIGHT_ORPHANED": "lots"}))
        self.assertTrue(validate_config({"WEIGHT_ORPHANED": 100000}))

    def test_normal_weightings_pass(self):
        self.assertEqual(validate_config({
            "WEIGHT_HARDLINKED": 40, "WEIGHT_ORPHANED": 20,
            "WEIGHT_NOT_IMPORTED": 20, "WEIGHT_DUPLICATES": 20}), [])

    def test_defaults_are_present_and_sum_to_100(self):
        self.assertEqual(sum(DEFAULT_CONFIG[k] for k in SCORE_WEIGHT_KEYS), 100)


if __name__ == "__main__":
    unittest.main()
