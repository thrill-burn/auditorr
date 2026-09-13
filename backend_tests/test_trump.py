import unittest
from unittest.mock import patch

import app
from arr import (
    parse_trump_pm, match_trump_release, match_trumped_torrent, _norm_release_name,
    rank_release_matches, score_release_match, _audio_codec, title_soft_match,
    indexer_key, tracker_matches_indexer, _release_group_tag, _release_match_features,
    rank_trump_replacements,
)


class TrumpReplacementRankingTests(unittest.TestCase):
    """Step 4: the replacement on the tracker that sent the PM is the one to grab
    — first, and pre-selected — with another tracker's copy the cross-seed edge
    case. Asked for by the user, 2026-09-13. Fixtures from a real PM (QA-5)."""

    NEW = "Apocalypse Now 1979 Theatrical REPACK 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-ATELiER"
    OLD = "Apocalypse Now 1979 Theatrical 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-ATELiER"
    PM  = "Aither (API) (Prowlarr)"

    def test_the_exact_release_on_the_pm_tracker_leads_whatever_the_seeders(self):
        rels = [{'guid': 'b', 'title': self.NEW, 'indexer': 'Blutopia', 'seeders': 90},
                {'guid': 'a', 'title': self.NEW.replace(' ', '.'), 'indexer': 'Aither', 'seeders': 3}]
        release, cands = rank_trump_replacements(rels, self.NEW, self.PM)
        self.assertEqual(release['guid'], 'a')
        self.assertEqual([c['guid'] for c in cands], ['a', 'b'])
        self.assertEqual([c['pm_tracker'] for c in cands], [True, False])

    def test_the_trumped_original_never_outranks_its_replacement(self):
        """The fuzzy score cannot tell a REPACK from the release it trumped, so
        tracker preference over it would pre-select the trumped copy."""
        self.assertEqual(score_release_match(self.NEW, self.OLD)[0],
                         score_release_match(self.NEW, self.NEW)[0])
        rels = [{'guid': 'old', 'title': self.OLD, 'indexer': 'Aither', 'seeders': 200},
                {'guid': 'new', 'title': self.NEW, 'indexer': 'Blutopia', 'seeders': 5}]
        release, cands = rank_trump_replacements(rels, self.NEW, self.PM)
        self.assertEqual(release['guid'], 'new')
        self.assertEqual(cands[0]['guid'], 'new')
        self.assertFalse(cands[0]['pm_tracker'])

    def test_with_no_pm_tracker_the_exact_release_still_leads(self):
        rels = [{'guid': 'old', 'title': self.OLD, 'indexer': 'X', 'seeders': 200},
                {'guid': 'new', 'title': self.NEW, 'indexer': 'Y', 'seeders': 5}]
        release, cands = rank_trump_replacements(rels, self.NEW)
        self.assertEqual((release['guid'], cands[0]['guid']), ('new', 'new'))
        self.assertFalse(any(c['pm_tracker'] for c in cands))

    def test_no_exact_release_means_nothing_is_preselected(self):
        rels = [{'guid': 'old', 'title': self.OLD, 'indexer': 'Aither', 'seeders': 1}]
        release, cands = rank_trump_replacements(rels, self.NEW, self.PM)
        self.assertIsNone(release)
        self.assertEqual([c['guid'] for c in cands], ['old'])


class ReleaseMatchCacheTests(unittest.TestCase):
    """TR11 — features are cached by name, so what is cached must not be editable.

    A cached dict or set that any caller mutated would corrupt every later match
    in the process, silently. No caller mutates today; this keeps it that way.
    """

    NAME = "Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos HDR x265-HQMUX"

    def test_the_same_name_is_parsed_once(self):
        self.assertIs(_release_match_features(self.NAME), _release_match_features(self.NAME))

    def test_cached_features_cannot_be_edited(self):
        f = _release_match_features(self.NAME)
        self.assertIsInstance(f['core'], frozenset)
        with self.assertRaises(TypeError):
            f['core'] = set()


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


# Real trump PMs, pasted from the field for QA-5 (2026-09-13). All three are one
# tracker's automated template: indented old title, three blank lines, the
# delimiter on its own line, the new title with a sentence period, a "Reason:"
# block, then boilerplate. Kept verbatim, whitespace included — the layout is
# the fixture. The other delimiter phrases below are NOT yet backed by a real PM.
_FIELD_TAIL = (
    "Our system shows that you were either the uploader, a seeder or a leecher on said "
    "trumped torrent. We just wanted to let you know you can safely remove it from your client,\n"
    "and please consider seeding the replacement. It has been granted 100% FreeLeech for 7 days!\n\n"
    "THIS IS AN AUTOMATED SYSTEM MESSAGE, PLEASE DO NOT REPLY!"
)


def _field_pm(old, new, reason):
    return ("The following torrent(s) have been trumped\n\n"
            f"    {old}\n\n\n\n"
            "and will be replaced by\n"
            f"{new}.\n\n"
            f"{reason}"
            f"{_FIELD_TAIL}")


FIELD_PMS = [
    ("Alone S12 1080p AMZN WEB-DL DD+ 5.1 H.264-RAWR",
     "Alone S12 1080p AMZN WEB-DL DD+ 5.1 H.264-Kitsune",
     "Reason:\nInternal\n\n"),
    ("Apocalypse Now 1979 Theatrical 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-ATELiER",
     "Apocalypse Now 1979 Theatrical REPACK 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-ATELiER",
     "Reason:\nRepack: Removed Commentary which is intended for Redux Cut\n\n"),
    ("Finding Nemo 2003 2160p UHD BluRay DD+ 5.1 HDR AV1-TiZU",
     "Finding Nemo 2003 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-W4NK3R",
     "Reason:\nNo slot for AV1\n\n"),
]


class TrumpPMFieldTests(unittest.TestCase):
    """TR6 — written against real PMs (QA-5), not only the TRUMP.md example."""

    def test_real_pms_parse(self):
        for old, new, reason in FIELD_PMS:
            with self.subTest(old=old):
                self.assertEqual(parse_trump_pm(_field_pm(old, new, reason)), ([old], new))

    def test_a_pm_without_a_reason_block_stops_at_the_blank_line(self):
        """The only terminator was a literal `Reason:` line, so everything after
        the delimiter — here the tracker's own boilerplate — joined the title."""
        for old, new, _reason in FIELD_PMS:
            with self.subTest(old=old):
                self.assertEqual(parse_trump_pm(_field_pm(old, new, '')), ([old], new))

    def test_a_sign_off_on_the_next_line_is_not_part_of_the_title(self):
        pm = ("Trumped\nOld.Release-A\nwill be replaced by\nNew.Release-B.\n"
              "Please remove the old torrent within 48 hours. Thanks for seeding!")
        self.assertEqual(parse_trump_pm(pm), (["Old.Release-A"], "New.Release-B"))

    def test_a_greeting_above_the_delimiter_is_not_a_trumped_release(self):
        """With no header line every line above the delimiter became a title,
        and phase 1 ranked every torrent in the client against `Hi there,`."""
        pm = "Hi there,\nShow.S01E01.1080p-OLD\nwill be replaced by\nShow.S01E01.1080p-NEW"
        self.assertEqual(parse_trump_pm(pm), (["Show.S01E01.1080p-OLD"], "Show.S01E01.1080p-NEW"))

    def test_the_delimiter_may_carry_a_colon(self):
        pm = "Trumped\nOld.Release-A\nwill be replaced by: New.Release-B"
        self.assertEqual(parse_trump_pm(pm), (["Old.Release-A"], "New.Release-B"))

    def test_a_missing_new_title_is_not_the_reason_line(self):
        pm = "Trumped\nOld.Release-A\nwill be replaced by\n\nReason: dupe"
        self.assertEqual(parse_trump_pm(pm), (["Old.Release-A"], ""))

    def test_the_other_delimiter_phrases_in_circulation(self):
        """Not yet backed by a real PM — QA-5 stays open for these."""
        for phrase in ("has been trumped by", "has been superseded by", "superseded by",
                       "replaced with", "has been replaced by"):
            with self.subTest(phrase=phrase):
                pm = f"Your torrent\nOld.Release-A\n{phrase}\nNew.Release-B."
                self.assertEqual(parse_trump_pm(pm), (["Old.Release-A"], "New.Release-B"))


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

    def test_never_crosses_to_a_different_title(self):
        # Field report: two unrelated movies, same group and identical quality
        # tags. Quality tokens alone cleared the 0.6 overlap bar, so the wizard
        # offered a stranger's film for deletion. The title must gate.
        rows = self._rows("Weapons.2025.2160p.UHD.BluRay.HDR10+.DoVi.TrueHD 7.1.Atmos.x265-SPHD.mkv")
        m = match_trumped_torrent(
            rows, "The Drama 2026 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR10+ x265-SPHD")
        self.assertIsNone(m)

    def test_never_crosses_to_a_remake_from_the_same_group(self):
        # Same title and group, years far apart — a different film.
        rows = self._rows("Dune.1984.2160p.UHD.BluRay.TrueHD.7.1.Atmos.x265-GRP")
        self.assertIsNone(match_trumped_torrent(
            rows, "Dune 2021 2160p UHD BluRay TrueHD 7.1 Atmos x265-GRP"))

    def test_year_drift_of_one_still_matches(self):
        # Premiere vs wide-release rendering — same film, must still resolve.
        rows = self._rows("Snow.White.1937.2160p.UHD.BluRay.TrueHD.7.1.Atmos.x265-GRP")
        self.assertIsNotNone(match_trumped_torrent(
            rows, "Snow White 1938 2160p UHD BluRay TrueHD 7.1 Atmos x265-GRP"))

    def test_a_named_group_does_not_disqualify_a_groupless_copy(self):
        # The PM names its group; the client's copy of the same payload is named
        # without one and ends in WEB-DL. That used to parse as group 'dl', and
        # a mismatched group is a hard `continue` in the overlap tier — so the
        # correct torrent was disqualified outright and the pre-selection lost.
        rows = self._rows("FROM.S04E01.The.Arrival.2160p.AMZN.WEB-DL")
        m = match_trumped_torrent(
            rows, "FROM.S04E01.The.Arrival.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune")
        self.assertIsNotNone(m)

    def test_two_groupless_names_are_not_a_group_agreement(self):
        # The quieter inverse: both sides reduce to 'ray', which scores as a
        # group *match* neither release ever declared. Different titles, so the
        # title gate must still be what decides — not a manufactured agreement.
        rows = self._rows("Weapons 2025 2160p Blu-Ray")
        self.assertIsNone(match_trumped_torrent(rows, "The Drama 2026 2160p Blu-Ray"))


class ReleaseGroupTagTests(unittest.TestCase):
    """A trailing hyphen is not proof of a release group."""

    def test_quality_tokens_are_not_groups(self):
        self.assertEqual(_release_group_tag("Show.S01E01.1080p.AMZN.WEB-DL"), "")
        self.assertEqual(_release_group_tag("Movie 2020 2160p Blu-Ray"), "")

    def test_real_groups_survive(self):
        self.assertEqual(_release_group_tag("A.Movie.2020-GRP"), "grp")
        self.assertEqual(
            _release_group_tag("FROM.S04E01.2160p.AMZN.WEB-DL.DDP5.1.H.265-Kitsune"),
            "kitsune")


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

    def test_indexer_filter_reconciles_names_like_the_rest_of_the_flow(self):
        """Indexer names are pooled across every arr, so the same tracker can
        arrive as "Aither (API) (Prowlarr)" and "Aither". Exact lowercase
        equality fell through to the any-indexer retry."""
        m = match_trump_release(
            self.releases, "Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-RandomBytes",
            indexer="Aither (API) (Prowlarr)")
        self.assertIsNotNone(m)

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


class TrackerIndexerMatchTests(unittest.TestCase):
    def test_arr_indexer_name_matches_tracker_host(self):
        self.assertTrue(tracker_matches_indexer("aither.cc", "Aither (API) (Prowlarr)"))
        self.assertTrue(tracker_matches_indexer("tracker.beyond-hd.me", "Beyond-HD"))
        self.assertTrue(tracker_matches_indexer("https://hawke.uno/announce", "Hawke.uno (API)"))

    def test_other_trackers_do_not_match(self):
        self.assertFalse(tracker_matches_indexer("hawke.uno", "Aither (API) (Prowlarr)"))
        self.assertFalse(tracker_matches_indexer("", "Aither"))
        self.assertFalse(tracker_matches_indexer("aither.cc", ""))

    def test_short_names_need_an_exact_key(self):
        # 3-char keys can't match by containment, or 'HD' swallows everything.
        self.assertFalse(tracker_matches_indexer("hdt.org", "HDTorrents"))
        self.assertTrue(tracker_matches_indexer("hdt.org", "HDT (API)"))

    def test_key_normalization(self):
        self.assertEqual(indexer_key("Aither (API) (Prowlarr)"), "aither")
        self.assertEqual(indexer_key("aither.cc"), "aither")
        self.assertEqual(indexer_key("https://tracker.beyond-hd.me:2053/announce"), "beyondhd")


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

    def test_symbol_quality_token_is_not_a_title_word(self):
        # 'HDR10+' kept its '+', missed the quality-noise set, and became a
        # shared *title* token — one junk word was enough to clear the gate.
        score, brk = score_release_match(
            "The Drama 2026 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR10+ x265-SPHD",
            "Weapons.2025.2160p.UHD.BluRay.HDR10+.DoVi.TrueHD 7.1.Atmos.x265-SPHD.mkv")
        self.assertEqual(score, 0.0)
        self.assertEqual(brk["title"], "diff")

    def test_unrelated_title_is_dropped_from_the_candidate_list(self):
        rows = self._rows("Weapons.2025.2160p.UHD.BluRay.HDR10+.DoVi.TrueHD 7.1.Atmos.x265-SPHD.mkv")
        ranked = rank_release_matches(
            rows, "The Drama 2026 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR10+ x265-SPHD", "name")
        self.assertEqual(ranked, [])

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

    def test_year_off_by_one_is_the_same_film(self):
        # Real trump: the PM says 1938, Aither's API renders the same release as
        # 1937 (premiere vs wide-release year) plus a .mkv file-name suffix.
        # Must rank first — flagged 'partial' on year, not disqualified.
        rows = self._rows(
            "Snow.White.and.the.Seven.Dwarfs.1937.Hybrid.2160p.BluRay.DD.1.0.DV.HDR10.x265-Softboat.mkv",
            "Snow White and the Seven Dwarfs 1938 2160p BluRay HDR10 DDP 7 1 x265-edge2020",
        )
        ranked = rank_release_matches(
            rows, "Snow White and the Seven Dwarfs 1938 Hybrid 2160p UHD BluRay DD 1.0 DV HDR x265-Softboat",
            "name")
        self.assertTrue(ranked[0]["name"].endswith("Softboat.mkv"))
        self.assertEqual(ranked[0]["match"]["year"], "partial")
        # .mkv is not a title word — the cores are identical despite the suffix
        self.assertEqual(ranked[0]["match"]["title"], "same")

    def test_exact_year_outranks_off_by_one_twin(self):
        rows = self._rows(
            "A.Movie.2019.1080p.WEB-DL.DDP5.1.H.264-GRP",
            "A.Movie.2020.1080p.WEB-DL.DDP5.1.H.264-GRP",
        )
        ranked = rank_release_matches(
            rows, "A Movie 2020 1080p WEB-DL DD+ 5.1 H.264-GRP", "name")
        self.assertEqual(len(ranked), 2)
        self.assertIn("2020", ranked[0]["name"])
        self.assertEqual(ranked[0]["match"]["year"], "same")
        self.assertEqual(ranked[1]["match"]["year"], "partial")


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


class TrumpSeedFileListRuleTests(unittest.TestCase):
    """Phase 2 must refuse a seed whose file list could not be read.

    `fetch_torrent_file_paths` now answers `None` for "could not ask" and `[]`
    for "the client says there are none" (it returned `[]` for both until the
    R1 pass, documented as deliberate). Either way the seed has no usable paths,
    and `_cross_seed_group` tests siblings against the seed's paths — so an
    unusable one short-circuits every test and the group collapses to the seed
    alone. `execute` then deletes that torrent's files while its cross-seed
    siblings stay registered and keep seeding on top of the hole. A smaller
    group is not a degraded answer here, it is a wrong one. Both spellings are
    exercised below.
    """

    ROWS = [
        {'hash': 'aaa', 'name': 'Rel.1080p.WEB-DL-GRP', 'size': 100,
         'tracker': 't1', 'instance_id': 1, 'instance_name': 'main'},
        {'hash': 'bbb', 'name': 'Rel.1080p.WEB-DL-GRP', 'size': 100,
         'tracker': 't2', 'instance_id': 1, 'instance_name': 'main'},
        {'hash': 'ccc', 'name': 'Rel.1080p.WEB-DL-GRP', 'size': 100,
         'tracker': 't3', 'instance_id': 1, 'instance_name': 'main'},
    ]

    def _resolve(self, paths_map):
        with patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app.sources, 'list_torrents', return_value=list(self.ROWS)), \
             patch.object(app.sources, 'fetch_torrent_file_paths', return_value=paths_map), \
             patch.object(app.sources, 'fetch_torrent_details', return_value={}):
            return app.app.test_client().post('/api/workflows/trump/resolve_group', json={
                'old_titles': ['Rel.1080p.WEB-DL-GRP'], 'seed_hashes': ['aaa']})

    def test_healthy_lookup_resolves_the_whole_group(self):
        resp = self._resolve({'aaa': ['j.mkv'], 'bbb': ['j.mkv'], 'ccc': ['j.mkv']})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['status'], 'success')
        self.assertEqual(sorted(t['hash'] for t in body['torrents']), ['aaa', 'bbb', 'ccc'])

    def test_an_unreadable_seed_refuses_instead_of_returning_one_torrent(self):
        resp = self._resolve({'aaa': [], 'bbb': ['j.mkv'], 'ccc': ['j.mkv']})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json()['status'], 'error')

    def test_a_seed_whose_listing_failed_refuses(self):
        """The `None` spelling — the source layer could not ask at all."""
        resp = self._resolve({'aaa': None, 'bbb': ['j.mkv'], 'ccc': ['j.mkv']})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json()['status'], 'error')

    def test_an_unreadable_candidate_does_not_refuse_but_is_reported(self):
        """A candidate whose list is unknown only narrows the group, so it must
        not fail the request — and since Phase 7 it must not pass as a confident
        answer either (TR1c). The two spellings resolve differently, on purpose:
        `None` ("could not ask") marks the group `partial`; `[]` ("the client
        says there are none") does not, because a torrent holding no files has
        nothing on disk to share or to lose."""
        resp = self._resolve({'aaa': ['j.mkv'], 'bbb': ['j.mkv'], 'ccc': None})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(sorted(t['hash'] for t in body['torrents']), ['aaa', 'bbb'])
        self.assertTrue(body['partial'])
        self.assertEqual(body['unknown_listings'], 1)

        resp = self._resolve({'aaa': ['j.mkv'], 'bbb': ['j.mkv'], 'ccc': []})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(sorted(t['hash'] for t in body['torrents']), ['aaa', 'bbb'])
        self.assertFalse(body['partial'])
        self.assertEqual(body['unknown_listings'], 0)


if __name__ == "__main__":
    unittest.main()
