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
    """The audit's positive evidence that a Triage row covers its whole torrent."""

    def _records(self, *specs):
        recs = [{'path': p, 'size': 1, 'status': 'Seeding', 'excluded': False,
                 'imported': imported, 'hash': h, 'tracker_health': 'unknown'}
                for p, h, imported in specs]
        _mark_whole_torrents(recs)
        return recs

    def test_a_wholly_unimported_torrent_is_marked(self):
        recs = self._records(
            ('tv/Show.S01/e01.mkv', 'AAA', False),
            ('tv/Show.S01/e02.mkv', 'AAA', False))
        self.assertTrue(all(r.get('whole_torrent') for r in recs))

    def test_a_partially_imported_torrent_is_not_marked(self):
        recs = self._records(
            ('tv/Show.S01/e01.mkv', 'AAA', False),
            ('tv/Show.S01/e02.mkv', 'AAA', True))
        self.assertIsNone(recs[0].get('whole_torrent'))

    def test_the_flag_stays_off_records_triage_never_reads(self):
        """Sparse by design — a field on every record grows files_json."""
        recs = self._records(
            ('tv/Show.S01/e01.mkv', 'AAA', True),      # imported, healthy
            ('tv/Show.S01/e02.mkv', 'AAA', True))
        self.assertTrue(all('whole_torrent' not in r for r in recs))

    def test_assemble_records_stamps_it(self):
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


class TriageExclusionPatternTests(unittest.TestCase):
    def test_a_single_file_torrent_gets_an_exact_literal_rule(self):
        self.assertEqual(
            app._triage_exclusion_patterns(['movies/Film [2020].mkv'], True),
            ['literal:movies/Film [2020].mkv'])

    def test_a_whole_multi_file_torrent_gets_its_release_folder(self):
        self.assertEqual(
            app._triage_exclusion_patterns(
                ['tv/Show.S01.1080p/e01.mkv', 'tv/Show.S01.1080p/e02.mkv'], True),
            ['literal:tv/Show.S01.1080p/'])

    def test_a_partially_imported_torrent_falls_back_to_per_file_rules(self):
        """T6's first half: the common folder also holds the imported files."""
        self.assertEqual(
            app._triage_exclusion_patterns(
                ['tv/Show.S01.1080p/e01.mkv', 'tv/Show.S01.1080p/e02.mkv'], False),
            ['literal:tv/Show.S01.1080p/e01.mkv', 'literal:tv/Show.S01.1080p/e02.mkv'])

    def test_a_common_folder_one_segment_deep_is_refused(self):
        """A category dir is never a folder pattern — the C7 rule, here too."""
        self.assertEqual(
            app._triage_exclusion_patterns(['tv/a.mkv', 'tv/b.mkv'], True),
            ['literal:tv/a.mkv', 'literal:tv/b.mkv'])

    def test_a_nested_pack_uses_its_deepest_common_folder(self):
        self.assertEqual(
            app._triage_exclusion_patterns(
                ['tv/Show.S01/Season 1/e01.mkv', 'tv/Show.S01/Season 1/e02.mkv'], True),
            ['literal:tv/Show.S01/Season 1/'])

    def test_the_folder_rule_it_emits_actually_matches_the_files(self):
        paths = ['tv/[Group] Show (2020)/e01.mkv', 'tv/[Group] Show (2020)/e02.mkv']
        pattern = app._triage_exclusion_patterns(paths, True)
        matcher = compile_exclusions(pattern)
        for p in paths:
            self.assertTrue(matcher.match(f'/data/torrents/{p}', p, p.split('/')[-1]))
            self.assertTrue(is_excluded(f'/data/torrents/{p}', p, p.split('/')[-1], pattern))


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
            _rec(path='tv/Show.S01/e01.mkv', whole_torrent=True),
            _rec(path='tv/Show.S01/e02.mkv', whole_torrent=True),
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
        constructed += app._triage_exclusion_patterns(paths, True)
        constructed += app._triage_exclusion_patterns(paths, False)
        constructed += app._triage_exclusion_patterns(paths[:1], True)
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
