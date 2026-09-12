"""Exclusion *construction* — the half R4 says is uncovered.

`test_exclusions_and_relink.py` covers matching: given a pattern, does the right
file match. Nothing covered the other direction — given a file the user clicked
Exclude on, what pattern gets written — which is where C7, C8 and T6 all live.

The invariant these tests establish, and the reason the last class exists: **no
string any construction path can emit is rejected by `validate_config`.**
`/api/workflows/exclude` writes through `db_save_config`, which validates
nothing, so an unenforced cap here becomes an unsaveable Config page later.
"""

import unittest
from unittest.mock import patch

import app
from audit import _assemble_records, _mark_whole_torrents
from db import (EXCLUSION_PATTERNS_MAX, EXCLUSION_PATTERN_MAX_CHARS,
                validate_config)
from exclusions import compile_exclusions, is_excluded


# ── C7 — a category dir must never become a folder pattern ────────────────────

def _orphan(path, size=100):
    return {'path': path, 'size': size, 'status': 'Orphaned', 'imported': False,
            'excluded': False, 'linked_paths': []}


def _cleanup(records):
    with patch.object(app, 'db_load_config', return_value={'LOCAL_PATH': ''}), \
         patch.object(app, 'db_load_file_results', return_value=records):
        resp = app.app.test_client().get('/api/workflows/cleanup')
    return resp.get_json()


class CategoryDirGroupingTests(unittest.TestCase):
    """C7: one click could write `movies/`, which matches the media library too."""

    def test_a_single_file_orphan_in_a_category_dir_yields_no_folder_pattern(self):
        report = _cleanup([
            _orphan('movies/Film.2020.1080p.mkv'),
            _orphan('movies/Other.2019.1080p.mkv'),
        ])
        groups = report['groups']
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g['folder'], 'movies')
        # The flag is the answer, shipped by the server so the client never
        # re-derives the depth rule.
        self.assertTrue(g['loose'])
        self.assertEqual(len(g['files']), 2)

    def test_root_level_orphans_are_loose_too(self):
        report = _cleanup([_orphan('Stray.mkv')])
        g = report['groups'][0]
        self.assertEqual(g['folder'], '(root)')
        self.assertTrue(g['loose'])

    def test_a_release_folder_group_is_not_loose(self):
        report = _cleanup([
            _orphan('movies/Sicario.2015.1080p.BluRay.x264-GRP/Sicario.mkv'),
            _orphan('movies/Sicario.2015.1080p.BluRay.x264-GRP/Sicario.nfo'),
        ])
        g = report['groups'][0]
        self.assertEqual(g['folder'], 'movies/Sicario.2015.1080p.BluRay.x264-GRP')
        self.assertFalse(g['loose'])

    def test_the_category_pattern_it_used_to_emit_really_does_hide_the_library(self):
        """Why the flag matters — the shape C7 reported, still reproducible."""
        matcher = compile_exclusions(['movies/'])
        self.assertTrue(matcher.match(
            '/data/media/movies/Film (2020)/Film.mkv',
            'movies/Film (2020)/Film.mkv', 'Film.mkv'))
        # ...and the release-folder pattern does not, because the arr renamed it.
        matcher = compile_exclusions(
            ['literal:movies/Sicario.2015.1080p.BluRay.x264-GRP/'])
        self.assertFalse(matcher.match(
            '/data/media/movies/Sicario (2015)/Sicario.mkv',
            'movies/Sicario (2015)/Sicario.mkv', 'Sicario.mkv'))
        self.assertTrue(matcher.match(
            '/data/torrents/movies/Sicario.2015.1080p.BluRay.x264-GRP/Sicario.mkv',
            'movies/Sicario.2015.1080p.BluRay.x264-GRP/Sicario.mkv', 'Sicario.mkv'))


# ── T6 — folder granularity, derived from the whole torrent ───────────────────

class WholeTorrentMarkingTests(unittest.TestCase):
    """The audit's positive evidence about what a Triage row may exclude."""

    def _records(self, *specs, media=()):
        recs = [{'path': p, 'size': 1, 'status': 'Seeding', 'excluded': False,
                 'imported': imported, 'hash': h, 'tracker_health': 'unknown'}
                for p, h, imported in specs]
        _mark_whole_torrents(recs, [{'path': p} for p in media])
        return recs

    def test_a_wholly_unimported_torrent_is_marked(self):
        recs = self._records(
            ('tv/Show.S01/e01.mkv', 'AAA', False),
            ('tv/Show.S01/e02.mkv', 'AAA', False))
        self.assertTrue(all(r.get('whole_torrent') for r in recs))
        self.assertTrue(all(r.get('excl_folder') == 'tv/Show.S01' for r in recs))

    def test_a_partially_imported_torrent_is_not_marked(self):
        recs = self._records(
            ('tv/Show.S01/e01.mkv', 'AAA', False),
            ('tv/Show.S01/e02.mkv', 'AAA', True))
        self.assertIsNone(recs[0].get('whole_torrent'))
        self.assertIsNone(recs[0].get('excl_folder'))

    def test_the_flag_stays_off_records_triage_never_reads(self):
        """Sparse by design — a field on every record grows files_json."""
        recs = self._records(
            ('tv/Show.S01/e01.mkv', 'AAA', True),      # imported, healthy
            ('tv/Show.S01/e02.mkv', 'AAA', True))
        self.assertTrue(all('whole_torrent' not in r for r in recs))
        self.assertTrue(all('excl_folder' not in r for r in recs))

    # -- what replaced the "at least two segments" rule --------------------

    def test_a_release_folder_one_segment_deep_is_offered(self):
        """The regression the probe found on the reference box.

        A torrent saved with no category directory has its release folder one
        segment down. The old depth rule refused it and emitted a per-file rule
        for every episode — each long enough for the 200-character config cap to
        refuse it in turn, while the dialog advised selecting the release folder
        the rule had just declined to use.
        """
        recs = self._records(
            ('Dark Matter (2024) S01 (2160p WEBRip)[cTurtle]/S01E03.mkv', 'AAA', False),
            ('Dark Matter (2024) S01 (2160p WEBRip)[cTurtle]/S01E06.mkv', 'AAA', False),
            media=['tv/Dark Matter (2024)/Season 01/ep.mkv'])
        self.assertEqual(recs[0]['excl_folder'],
                         'Dark Matter (2024) S01 (2160p WEBRip)[cTurtle]')

    def test_a_folder_holding_another_torrent_is_refused(self):
        """Exclusivity — what actually stopped `movies/` being offered."""
        recs = self._records(
            ('movies/Film.2020.mkv',  'AAA', False),
            ('movies/Other.2019.mkv', 'BBB', False))
        self.assertTrue(all(r.get('whole_torrent') for r in recs))
        self.assertTrue(all('excl_folder' not in r for r in recs))

    def test_a_folder_holding_a_file_triage_never_sees_is_refused(self):
        """The case only the exclusivity *walk* can catch.

        The other torrent here is imported and healthy, so it is not on the
        Triage pile and never becomes a candidate folder of its own — nothing
        but a pass over every record notices that its file sits inside the
        folder about to be excluded. Excluding it would hide a library file.
        """
        recs = self._records(
            ('movies/Rel.2020/a.mkv',         'AAA', False),
            ('movies/Rel.2020/b.mkv',         'AAA', False),
            ('movies/Rel.2020/extra/c.mkv',   'BBB', True),
            media=['movies/Rel (2020)/Rel.mkv'])
        self.assertTrue(recs[0].get('whole_torrent'))
        self.assertTrue(all('excl_folder' not in r for r in recs))

    def test_an_orphan_in_the_folder_also_refuses_it(self):
        """A record with no hash is a file no torrent claims — still a file."""
        recs = self._records(
            ('movies/Rel.2020/a.mkv',     'AAA', False),
            ('movies/Rel.2020/b.mkv',     'AAA', False),
            ('movies/Rel.2020/stray.nfo', '',    False))
        self.assertTrue(all('excl_folder' not in r for r in recs))

    def test_a_category_dir_shared_with_the_media_tree_is_refused(self):
        """C7 — a one-segment folder naming a media root matches both walks."""
        recs = self._records(
            ('movies/Film.2020/a.mkv', 'AAA', False),
            ('movies/Film.2020/b.mkv', 'AAA', False),
            media=['movies/Film (2020)/Film.mkv'])
        self.assertEqual(recs[0]['excl_folder'], 'movies/Film.2020')

        # ...but the same torrent saved loose IN that category dir gets nothing,
        # even though it is the only torrent there.
        loose = self._records(
            ('movies/a.mkv', 'AAA', False),
            ('movies/b.mkv', 'AAA', False),
            media=['movies/Film (2020)/Film.mkv'])
        self.assertTrue(all(r.get('whole_torrent') for r in loose))
        self.assertTrue(all('excl_folder' not in r for r in loose))

    def test_the_media_root_test_only_applies_at_one_segment(self):
        """A deeper folder cannot collide, so `movies/X` is fine under `movies`."""
        recs = self._records(
            ('movies/Rel.2020/a.mkv', 'AAA', False),
            ('movies/Rel.2020/b.mkv', 'AAA', False),
            media=['movies/Rel (2020)/Rel.mkv'])
        self.assertEqual(recs[0]['excl_folder'], 'movies/Rel.2020')

    def test_a_file_at_the_torrent_root_gets_no_folder(self):
        recs = self._records(('stray.mkv', 'AAA', False),
                             ('other.mkv', 'AAA', False))
        self.assertTrue(all('excl_folder' not in r for r in recs))

    def test_assemble_records_stamps_both(self):
        """It has to survive the real record builder, not just the helper."""
        inode_map = {
            (1, 10): {'torrent_rel_path': 'tv/Show.S01/e01.mkv', 'size': 5,
                      'status': 'Seeding', 'media_paths': [], 'trackers': set(),
                      'torrent_excluded': False, 'hash': 'AAA', 'category': 'tv',
                      'tracker_health': 'working'},
            (1, 11): {'torrent_rel_path': 'tv/Show.S01/e02.mkv', 'size': 5,
                      'status': 'Seeding', 'media_paths': [], 'trackers': set(),
                      'torrent_excluded': False, 'hash': 'AAA', 'category': 'tv',
                      'tracker_health': 'working'},
        }
        torrent_files, _ = _assemble_records([(1, 10), (1, 11)], [], inode_map, {})
        self.assertTrue(all(r.get('whole_torrent') for r in torrent_files))
        self.assertTrue(all(r.get('excl_folder') == 'tv/Show.S01' for r in torrent_files))


class TriageExclusionPatternTests(unittest.TestCase):
    def test_a_single_file_torrent_gets_an_exact_literal_rule(self):
        self.assertEqual(
            app._triage_exclusion_patterns(['movies/Film [2020].mkv'], 'movies'),
            ['literal:movies/Film [2020].mkv'])

    def test_a_stamped_folder_becomes_one_subtree_rule(self):
        self.assertEqual(
            app._triage_exclusion_patterns(
                ['tv/Show.S01.1080p/e01.mkv', 'tv/Show.S01.1080p/e02.mkv'],
                'tv/Show.S01.1080p'),
            ['literal:tv/Show.S01.1080p/'])

    def test_no_stamp_falls_back_to_per_file_rules(self):
        """The audit declined to name a safe folder — so the endpoint may not
        invent one. This is also what a pre-upgrade database looks like."""
        self.assertEqual(
            app._triage_exclusion_patterns(
                ['tv/Show.S01.1080p/e01.mkv', 'tv/Show.S01.1080p/e02.mkv'], ''),
            ['literal:tv/Show.S01.1080p/e01.mkv', 'literal:tv/Show.S01.1080p/e02.mkv'])

    def test_records_disagreeing_on_the_stamp_resolve_to_nothing(self):
        self.assertEqual(app._excl_folder([{'excl_folder': 'a/b'},
                                           {'excl_folder': 'a/c'}]), '')
        self.assertEqual(app._excl_folder([{'excl_folder': 'a/b'}, {}]), '')
        self.assertEqual(app._excl_folder([{'excl_folder': 'a/b'},
                                           {'excl_folder': 'a/b'}]), 'a/b')

    def test_the_folder_rule_it_emits_actually_matches_the_files(self):
        paths = ['tv/[Group] Show (2020)/e01.mkv', 'tv/[Group] Show (2020)/e02.mkv']
        pattern = app._triage_exclusion_patterns(paths, 'tv/[Group] Show (2020)')
        matcher = compile_exclusions(pattern)
        for p in paths:
            self.assertTrue(matcher.match(f'/data/torrents/{p}', p, p.split('/')[-1]))
            self.assertTrue(is_excluded(f'/data/torrents/{p}', p, p.split('/')[-1], pattern))

    def test_a_one_segment_release_folder_stays_under_the_cap(self):
        """The arithmetic behind the fix, as a test rather than a claim.

        The reference box's row: a ~95-character release folder holding episodes
        with ~95-character names. Per file that is ~205 characters and the config
        cap refuses it; as one folder rule it is ~104 and fits.
        """
        folder = ('Dark Matter (2024) S01 Season 1 '
                  '(2160p WEBRip DV+HDR10P HYB x265 q14 12M V94 DDPA 5.1)[cTurtle]')
        paths = [f'{folder}/Dark Matter (2024) S01E0{n} The Box '
                 f'(2160p WEBRip DV+HDR10P HYB x265 q14 12M V94 DDPA 5.1)[cTurtle].mkv'
                 for n in (3, 6)]
        self.assertTrue(all(len(f'literal:{p}') > EXCLUSION_PATTERN_MAX_CHARS
                            for p in paths), 'fixture no longer exceeds the cap')
        folder_rule = app._triage_exclusion_patterns(paths, folder)
        self.assertEqual(len(folder_rule), 1)
        self.assertLessEqual(len(folder_rule[0]), EXCLUSION_PATTERN_MAX_CHARS)
        self.assertEqual(validate_config({'EXCLUSION_PATTERNS': folder_rule}), [])


def _rec(**over):
    base = {'path': 'radarr/Movie.2020/Movie.2020.mkv', 'size': 100,
            'status': 'Seeding', 'imported': False, 'excluded': False,
            'hash': 'AAAA', 'instance_id': 1, 'trackers': ['t.example'],
            'tracker_health': 'unknown', 'tracker_msg': ''}
    base.update(over)
    return base


def _triage(records):
    with patch.object(app, 'db_load_config', return_value={}), \
         patch.object(app, 'db_has_file_results', return_value=True), \
         patch.object(app, 'db_load_file_results', return_value=records), \
         patch.object(app, 'fetch_arr_media_index', return_value=[]), \
         patch.object(app, 'fetch_arr_all_titles', return_value=[]), \
         patch.object(app, 'normalize_arr_connections', return_value=[]), \
         patch.object(app, 'arr_media_index_errors', return_value=[]), \
         patch.object(app, 'arr_titles_errors', return_value=[]):
        return app.app.test_client().get('/api/workflows/triage').get_json()


class TriageEndpointExclusionTests(unittest.TestCase):
    def test_the_endpoint_ships_the_patterns(self):
        report = _triage([
            _rec(path='tv/Show.S01/e01.mkv', whole_torrent=True,
                 excl_folder='tv/Show.S01'),
            _rec(path='tv/Show.S01/e02.mkv', whole_torrent=True,
                 excl_folder='tv/Show.S01'),
        ])
        self.assertEqual(report['items'][0]['exclusion_patterns'],
                         ['literal:tv/Show.S01/'])

    def test_an_unmarked_torrent_gets_per_file_rules(self):
        """Absence of the flag is 'not established', never 'safe'.

        A database whose last audit predates the field has exactly that
        absence, and it must degrade to more patterns, not to the T6 bug.
        """
        report = _triage([
            _rec(path='tv/Show.S01/e01.mkv'),
            _rec(path='tv/Show.S01/e02.mkv'),
        ])
        self.assertEqual(report['items'][0]['exclusion_patterns'],
                         ['literal:tv/Show.S01/e01.mkv', 'literal:tv/Show.S01/e02.mkv'])

    def test_dead_registration_rows_offer_no_exclusion_at_all(self):
        """Those paths belong to the healthy carrier — a live cross-seed's file."""
        report = _triage([
            _rec(path='tv/Carrier/e01.mkv', hash='LIVE', imported=True,
                 tracker_health='working',
                 dead_siblings=[{'hash': 'DEAD', 'instance_id': 1,
                                 'tracker_msg': 'Unregistered torrent'}]),
        ])
        rows = [i for i in report['items'] if i['verdict'] == 'dead_registration']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['exclusion_patterns'], [])


# ── The endpoint caps ─────────────────────────────────────────────────────────

def _exclude(patterns, existing=None):
    saved = {}

    def _save(cfg):
        saved.update(cfg)

    with patch.object(app, 'db_load_config',
                      return_value={'EXCLUSION_PATTERNS': list(existing or [])}), \
         patch.object(app, 'db_save_config', side_effect=_save), \
         patch.object(app, 'nudge_watchdog'):
        resp = app.app.test_client().post('/api/workflows/exclude',
                                          json={'patterns': patterns})
    return resp, saved


class ExcludeEndpointCapTests(unittest.TestCase):
    """`workflows_exclude` bypasses `validate_config` entirely — so it has to
    enforce the same caps itself, or it writes a list the Config page refuses
    to save and blocks every unrelated setting on that page."""

    def test_an_over_long_pattern_is_refused_and_said_so(self):
        long_one = 'literal:tv/' + ('x' * EXCLUSION_PATTERN_MAX_CHARS) + '.mkv'
        resp, saved = _exclude(['literal:tv/ok.mkv', long_one])
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body['added'], 1)
        self.assertEqual(body['too_long'], 1)
        self.assertIn('Added 1 of 2', body['message'])
        self.assertIn('too long', body['message'])
        self.assertNotIn(long_one, saved['EXCLUSION_PATTERNS'])
        # The actual invariant: what was written is still saveable.
        self.assertEqual(validate_config(saved), [])

    def test_the_pattern_count_cap_is_enforced(self):
        existing = [f'literal:tv/existing-{i}.mkv'
                    for i in range(EXCLUSION_PATTERNS_MAX - 1)]
        resp, saved = _exclude(['literal:tv/a.mkv', 'literal:tv/b.mkv'], existing)
        body = resp.get_json()
        self.assertEqual(body['added'], 1)
        self.assertEqual(body['no_room'], 1)
        self.assertIn(f'{EXCLUSION_PATTERNS_MAX}-pattern limit', body['message'])
        self.assertEqual(len(saved['EXCLUSION_PATTERNS']), EXCLUSION_PATTERNS_MAX)
        self.assertEqual(validate_config(saved), [])

    def test_a_fully_refused_request_writes_nothing(self):
        long_one = 'literal:' + ('x' * 400)
        resp, saved = _exclude([long_one])
        self.assertEqual(resp.get_json()['added'], 0)
        self.assertEqual(saved, {})   # db_save_config never called

    def test_duplicates_are_reported_separately_from_refusals(self):
        resp, _ = _exclude(['literal:tv/a.mkv'], ['literal:tv/a.mkv'])
        body = resp.get_json()
        self.assertEqual((body['added'], body['duplicates'], body['refused']), (0, 1, 0))


class ConstructedPatternsAreAlwaysSaveableTests(unittest.TestCase):
    """The invariant this phase establishes, as one assertion.

    Every construction path emits `literal:` strings built from real paths. A
    pattern that is short enough must always survive `validate_config` — no
    character in a release name may make it invalid.
    """

    def test_no_constructed_pattern_shape_is_rejected(self):
        paths = [
            'anime/[SubsPlease] Show - 01 [1080p].mkv',
            "movies/Ocean's 11 (1960)/Ocean's 11.mkv",
            'movies/Film*.mkv',
            'tv/Show (2020)/ep?.mkv',
            'tv/Show — Dash/S01E01.mkv',
            'movies/Sicario.2015.1080p.BluRay.x264-GRP/Sicario.mkv',
        ]
        constructed = []
        # Triage's server-side builder, both branches.
        constructed += app._triage_exclusion_patterns(paths, 'movies/Some Release')
        constructed += app._triage_exclusion_patterns(paths, '')
        constructed += app._triage_exclusion_patterns(paths[:1], '')
        # Cleanup's two shapes, spelled the way Cleanup.jsx spells them.
        constructed.append(app._literal_pattern(
            'movies/Sicario.2015.1080p.BluRay.x264-GRP', subtree=True))
        constructed += [app._literal_pattern(p) for p in paths]

        self.assertEqual(validate_config({'EXCLUSION_PATTERNS': constructed}), [])
        for pattern in constructed:
            self.assertTrue(pattern.startswith('literal:'), pattern)
            self.assertLessEqual(len(pattern), EXCLUSION_PATTERN_MAX_CHARS, pattern)


if __name__ == '__main__':
    unittest.main()
