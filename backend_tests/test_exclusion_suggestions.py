"""Exclusion suggestions (2026-11-20): what the scan offers Settings to exclude.

A new install has the always-on rules and nothing else, and what a user would
exclude is their own: a torrent category nothing reaches the library from, a
library folder no arr manages, full-disc rips. The scan names them from what it
already has (`_mark_whole_torrents`' registrations, the library map's folder
sums and the disc rips it passes), and Settings shows each with its figures
and rule. Each kind is refused wherever its evidence could mean something else.
"""

import copy
import os
import random
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app
import audit
import db
from exclusions import compile_exclusions
from media_server_exclusions import expand_exclusion_patterns

GB = 1024 ** 3
CFG = {'LOCAL_PATH': '/data/torrents', 'MEDIA_PATH': '/data/media'}


def _t(path, h, imported=False, size=GB, **extra):
    r = {'path': path, 'size': size, 'status': 'Seeding', 'excluded': False, 'imported': imported,
         'hash': h, 'tracker_health': 'unknown', 'trackers': ['t.example'],
         'linked_paths': ['/data/media/x'] if imported else [], 'duplicate_paths': [],
         'category': ''}
    r.update(extra)
    return r


def _m(path, size=GB, linked=False, unmanaged=False):
    r = {'path': path, 'size': size, 'linked_paths': ['/data/torrents/x'] if linked else [],
         'duplicate_paths': [], 'excluded': False, 'imported': True}
    if unmanaged:
        r['unmanaged'] = True
    return r


def _suggest(torrents, media=(), cfg=CFG):
    """The scan's three steps over these records, as `run_audit_process` runs them."""
    folders, discs = {}, {}
    audit._mark_whole_torrents(torrents, list(media), folders=folders)
    row = audit.library_map_row(list(media), torrents, discs=discs)
    return audit.exclusion_suggestions(cfg, folders, row, discs)


def _library_movies():
    """Something linked, so the hardlink setup is shown to work."""
    return [_t('movies/Film.2020/f.mkv', 'M1', imported=True, category='movies'),
            _t('movies/Other.2021/o.mkv', 'M2', imported=True, category='movies')]


def _games(n=3, **extra):
    return [_t(f'games/Game{i}/setup.iso', f'G{i}', category='games', **extra) for i in range(n)]


class TorrentFolderTests(unittest.TestCase):
    def test_a_category_nothing_reaches_the_library_from_is_suggested(self):
        [s] = _suggest(_library_movies() + _games(3))
        self.assertEqual(s, {'id': 'torrents:games', 'kind': 'torrents', 'folder': 'games',
                             'rule': 'literal:/data/torrents/games/', 'category': 'games',
                             'torrents': 3, 'files': 3, 'size': 3 * GB, 'orphaned_size': 0})

    def test_one_torrent_is_no_evidence(self):
        """It could be an import that hasn't happened yet."""
        self.assertEqual(_suggest(_library_movies() + _games(1)), [])

    def test_a_download_still_running_is_no_evidence(self):
        """T4: nobody's to act on yet. Two finished ones are still needed."""
        games = _games(2)
        games[1]['incomplete'] = True
        self.assertEqual(_suggest(_library_movies() + games), [])
        games = _games(3)
        games[2]['completion_unknown'] = True
        [s] = _suggest(_library_movies() + games)
        self.assertEqual(s['torrents'], 3)

    def test_a_folder_with_anything_in_the_library_is_never_suggested(self):
        games = _games(3)
        games.append(_t('games/Game0/manual.pdf', 'G0', imported=True, category='games'))
        self.assertEqual(_suggest(_library_movies() + games), [])

    def test_nothing_linked_anywhere_suggests_nothing(self):
        """That's a wrong path in Settings, not a category to hide."""
        movies = [_t('movies/Film.2020/f.mkv', 'M1'), _t('movies/Other.2021/o.mkv', 'M2')]
        self.assertEqual(_suggest(movies + _games(3)), [])

    def test_a_copy_in_the_library_refuses_the_folder_and_one_elsewhere_does_not(self):
        games = _games(3)
        games[0]['duplicate_paths'] = ['/data/media/games/Game0/setup.iso']
        self.assertEqual(_suggest(_library_movies() + games), [])
        games = _games(3)
        games[0]['duplicate_paths'] = ['/data/torrents/backup/setup.iso']
        self.assertEqual([s['id'] for s in _suggest(_library_movies() + games)], ['torrents:games'])

    def test_files_at_the_torrent_root_are_no_folder(self):
        root = [_t('Game0.iso', 'G0'), _t('Game1.iso', 'G1'), _t('Game2.iso', 'G2')]
        self.assertEqual(_suggest(_library_movies() + root), [])

    def test_an_excluded_folder_is_not_suggested(self):
        self.assertEqual(_suggest(_library_movies() + _games(3, excluded=True)), [])

    def test_orphans_under_it_are_counted_and_said(self):
        """Excluding the folder takes them out of Cleanup too, so the page says so."""
        orphan = _t('games/Gone/old.iso', '', size=5 * GB, status='Orphaned')
        [s] = _suggest(_library_movies() + _games(2) + [orphan])
        self.assertEqual((s['size'], s['orphaned_size'], s['files'], s['torrents']), (7 * GB, 5 * GB, 3, 2))

    def test_the_category_is_named_only_when_it_is_one(self):
        games = _games(3)
        games[2]['category'] = 'pc'
        [s] = _suggest(_library_movies() + games)
        self.assertIsNone(s['category'])

    def test_largest_first_and_capped(self):
        torrents = _library_movies()
        for k in range(audit.EXCLUSION_SUGGESTIONS_MAX + 3):
            torrents += [_t(f'cat{k}/R{i}/x.bin', f'C{k}-{i}', size=(k + 1) * GB) for i in range(2)]
        got = _suggest(torrents)
        self.assertEqual(len(got), audit.EXCLUSION_SUGGESTIONS_MAX)
        self.assertEqual(got[0]['folder'], f'cat{audit.EXCLUSION_SUGGESTIONS_MAX + 2}')
        self.assertEqual([s['size'] for s in got], sorted((s['size'] for s in got), reverse=True))

    def test_a_rule_too_long_to_save_is_not_offered(self):
        long = 'g' * 190
        self.assertEqual(_suggest(_library_movies() + [_t(f'{long}/R{i}/x.iso', f'L{i}') for i in range(3)]), [])


class LibraryFolderTests(unittest.TestCase):
    def _media(self):
        return [_m('tv/Show/e1.mkv', linked=True), _m('tv/Show/e1.en.srt', size=1000)]

    def test_a_folder_no_arr_holds_is_suggested(self):
        media = self._media() + [_m('Home Videos/2019/clip.mp4', size=4 * GB, unmanaged=True),
                                 _m('Home Videos/2019/clip.srt', size=1000, unmanaged=True)]
        [s] = _suggest([], media)
        self.assertEqual(s, {'id': 'library:Home Videos', 'kind': 'library', 'folder': 'Home Videos',
                             'rule': 'literal:/data/media/Home Videos/', 'files': 2, 'size': 4 * GB + 1000})

    def test_not_when_it_is_the_whole_library(self):
        """Nothing else shows the arrs' answer covers this library."""
        self.assertEqual(_suggest([], [_m('Home Videos/a.mp4', unmanaged=True)]), [])

    def test_not_when_anything_in_it_is_managed_or_linked(self):
        for extra in (_m('Home Videos/b.mkv'), _m('Home Videos/c.mkv', linked=True)):
            media = self._media() + [_m('Home Videos/a.mp4', unmanaged=True), extra]
            self.assertEqual(_suggest([], media), [], extra['path'])

    def test_not_when_the_arrs_did_not_answer(self):
        """R1: no Unmanaged stamp without every arr's whole answer, so nothing to go on."""
        self.assertEqual(_suggest([], self._media() + [_m('Home Videos/a.mp4')]), [])


class PresetTests(unittest.TestCase):
    def test_a_disc_rip_suggests_its_preset_until_it_is_on(self):
        media = [_m('movies/Rip (2001)/BDMV/index.bdmv', size=200),
                 _m('movies/Rip (2001)/BDMV/STREAM/00000.m2ts', size=GB, linked=True)]
        torrents = [_t('radarr/Old.DVD/VIDEO_TS/VIDEO_TS.IFO', 'D1', size=10_000)]
        got = {s['id']: s for s in _suggest(torrents, media)}
        self.assertEqual(got['preset:bluray'], {'id': 'preset:bluray', 'kind': 'preset', 'preset': 'bluray',
                                                'discs': {'library': 1, 'torrents': 0}})
        self.assertEqual(got['preset:dvd']['discs'], {'library': 0, 'torrents': 1})
        on = dict(CFG, DISC_RIP_EXCLUSION_PRESETS=['bluray', 'dvd'])
        self.assertEqual([s for s in _suggest(torrents, media, on) if s['kind'] == 'preset'], [])


class RuleTests(unittest.TestCase):
    """The rule is the folder's full container path, through the real matcher."""

    def _match(self, rules, full, rel):
        matcher = compile_exclusions(expand_exclusion_patterns({'EXCLUSION_PATTERNS': rules}))

        class Spelled(str):
            """A str subclass takes the matcher's long way (SCAN SC2)."""
        name = full.rsplit('/', 1)[-1]
        short = matcher.match(full, rel, name)
        long = matcher.match(Spelled(full), Spelled(rel), Spelled(name))
        self.assertEqual(short, long, full)
        return short

    def test_it_covers_the_folder_and_nothing_beside_it(self):
        rule = audit._folder_rule('/data/torrents/', 'games')
        self.assertEqual(rule, 'literal:/data/torrents/games/')
        self.assertTrue(self._match([rule], '/data/torrents/games/Game0/setup.iso', 'games/Game0/setup.iso'))
        self.assertTrue(self._match([rule], '/data/torrents/games/loose.iso', 'games/loose.iso'))
        self.assertFalse(self._match([rule], '/data/torrents/games2/Game/setup.iso', 'games2/Game/setup.iso'))

    def test_the_other_trees_folder_of_the_same_name_is_untouched(self):
        """Why it's a full path: the usual layout names both trees' folders alike,
        and a relative rule matches both."""
        rule = audit._folder_rule('/data/media', 'music')
        self.assertTrue(self._match([rule], '/data/media/music/Album/01.flac', 'music/Album/01.flac'))
        self.assertFalse(self._match([rule], '/data/torrents/music/Album/01.flac', 'music/Album/01.flac'))
        self.assertTrue(self._match(['literal:music/'], '/data/torrents/music/Album/01.flac', 'music/Album/01.flac'))

    def test_a_bracket_in_the_name_is_matched_as_written(self):
        rule = audit._folder_rule('/data/torrents', '[Anime] Archive')
        self.assertTrue(self._match([rule], '/data/torrents/[Anime] Archive/x.mkv', '[Anime] Archive/x.mkv'))
        self.assertEqual(db.validate_config({'EXCLUSION_PATTERNS': [rule]}), [])


class PendingTests(unittest.TestCase):
    STORED = [{'id': 'torrents:games', 'rule': 'literal:/data/torrents/games/'},
              {'id': 'library:Home Videos', 'rule': 'literal:/data/media/Home Videos/'},
              {'id': 'preset:bluray', 'preset': 'bluray'}]

    def test_what_the_config_answers_is_left_out(self):
        pend = lambda cfg: [s['id'] for s in audit.pending_exclusion_suggestions(self.STORED, cfg)]
        self.assertEqual(pend({}), ['torrents:games', 'library:Home Videos', 'preset:bluray'])
        self.assertEqual(pend({'EXCLUSION_SUGGESTIONS_DISMISSED': ['library:Home Videos']}),
                         ['torrents:games', 'preset:bluray'])
        self.assertEqual(pend({'EXCLUSION_PATTERNS': ['  literal:/data/torrents/games/ ']}),
                         ['library:Home Videos', 'preset:bluray'])
        self.assertEqual(pend({'DISC_RIP_EXCLUSION_PRESETS': ['bluray']}),
                         ['torrents:games', 'library:Home Videos'])


class _ConfigRestore(unittest.TestCase):
    def setUp(self):
        self._cfg = db.db_load_config()

    def tearDown(self):
        db.db_save_config(self._cfg)


class EndpointTests(_ConfigRestore):
    def _get(self):
        with patch('app._is_local_client', return_value=True):
            return app.app.test_client().get('/api/exclusion_suggestions').get_json()

    def test_it_serves_the_stored_row_less_what_the_config_answers(self):
        db.db_save_file_results('exclusion_suggestions', PendingTests.STORED)
        db.db_save_config(dict(self._cfg, EXCLUSION_SUGGESTIONS_DISMISSED=['preset:bluray'],
                               EXCLUSION_PATTERNS=['literal:/data/torrents/games/'],
                               DISC_RIP_EXCLUSION_PRESETS=[]))
        self.assertEqual([s['id'] for s in self._get()['suggestions']], ['library:Home Videos'])

    def test_nothing_before_the_first_scan_that_writes_it(self):
        with patch('app.db_load_file_results', return_value=[]):
            self.assertEqual(self._get(), {'status': 'success', 'suggestions': []})


class ConfigKeyTests(_ConfigRestore):
    def _post(self, data):
        with patch('app._is_local_client', return_value=True), patch('app.restart_watchdog'):
            return app.app.test_client().post('/api/config', json=data)

    def test_a_save_keeps_the_list_and_one_without_it_keeps_the_stored_one(self):
        """A bundle that predates the key must not wipe every dismissal."""
        res = self._post({'MEDIA_PATH': '', 'EXCLUSION_SUGGESTIONS_DISMISSED': ['torrents:games']})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(db.db_load_config()['EXCLUSION_SUGGESTIONS_DISMISSED'], ['torrents:games'])
        self.assertEqual(self._post({'MEDIA_PATH': ''}).status_code, 200)
        self.assertEqual(db.db_load_config()['EXCLUSION_SUGGESTIONS_DISMISSED'], ['torrents:games'])

    def test_it_is_validated(self):
        v = db.validate_config
        self.assertEqual(v({'EXCLUSION_SUGGESTIONS_DISMISSED': ['torrents:a', 'library:b']}), [])
        self.assertTrue(v({'EXCLUSION_SUGGESTIONS_DISMISSED': 'torrents:a'}))
        self.assertTrue(v({'EXCLUSION_SUGGESTIONS_DISMISSED': [1]}))
        self.assertTrue(v({'EXCLUSION_SUGGESTIONS_DISMISSED': ['x' * 301]}))
        self.assertTrue(v({'EXCLUSION_SUGGESTIONS_DISMISSED': ['x'] * 501}))

    def test_it_never_reaches_the_debug_report(self):
        """It holds folder names; the report's config is an allowlist."""
        import debug
        out = debug._sanitized_config(dict(self._cfg, EXCLUSION_SUGGESTIONS_DISMISSED=['library:Home Videos']))
        self.assertNotIn('Home Videos', repr(out))


class WholeTorrentStampsTests(unittest.TestCase):
    """The tally rides `_mark_whole_torrents`' pass 1, whose per-registration
    states became bits (2026-11-20). Asking for the tally changes no stamp, and
    the stamps are what a set of states gave: homogeneous is exactly one of
    "in the library" and "not, and not a sidecar"."""

    def _records(self, seed):
        rnd = random.Random(seed)
        recs = []
        for n in range(300):
            h = f'H{rnd.randrange(40)}'
            top = rnd.choice(['tv', 'movies', 'games', 'books'])
            recs.append(_t(f'{top}/R{h}/{n}.{rnd.choice(["mkv", "nfo", "iso"])}', h,
                           imported=rnd.random() < 0.4, sidecar=rnd.random() < 0.2,
                           instance_id=rnd.choice([None, 1, 2]), category=top,
                           incomplete=rnd.random() < 0.05, status=rnd.choice(['Seeding'] * 9 + ['Orphaned']),
                           tracker_health=rnd.choice(['working', 'unregistered', 'unknown'])))
        return recs

    @staticmethod
    def _whole_by_sets(recs):
        """The old pass 1's answer, sets and all."""
        states, relevant = {}, set()
        for r in recs:
            h = audit.torrent_record_key(r)
            if not h:
                continue
            s = states.setdefault(h, set())
            imported = bool(r.get('imported'))
            if imported or not r.get('sidecar'):
                s.add(imported)
            if audit._is_triage_relevant(r):
                relevant.add(h)
        return {h for h in relevant if len(states[h]) == 1}

    def test_asking_for_the_tally_changes_no_stamp(self):
        for seed in range(20):
            a, b = self._records(seed), self._records(seed)
            audit._mark_whole_torrents(a, [])
            folders = {}
            audit._mark_whole_torrents(b, [], folders=folders)
            self.assertEqual(a, b, seed)
            self.assertEqual(sum(t['torrents'] for t in folders.values()),
                             len({audit.torrent_record_key(r) for r in b}), seed)

    def test_whole_is_what_a_set_of_states_gave(self):
        for seed in range(20):
            recs = self._records(seed)
            expected = self._whole_by_sets(copy.deepcopy(recs))
            audit._mark_whole_torrents(recs, [])
            stamped = {audit.torrent_record_key(r) for r in recs if r.get('whole_torrent')}
            # Stamped on the Triage-relevant records of a whole registration,
            # which is every whole one here (whole needs a relevant record).
            self.assertTrue(expected, seed)
            self.assertEqual(stamped, expected, seed)


if __name__ == '__main__':
    unittest.main()
