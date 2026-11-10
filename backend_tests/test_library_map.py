"""The dashboard's disk-and-links cards (2026-11-09), and what they read.

* The library map's row (`audit.library_map_row`): every title's bytes by the
  workflow that acts on them, linked bytes counted once, in the library.
* `shared_size`: the bytes both trees hold, which `hardlinked_media_size`
  over-counts when a linked file is excluded on the torrent side only.
* New in the library: the change-log diff counts how each new library file
  arrived, and the upload stats sum them per day.
* Last scan: `/api/results` carries the last scan's report, counts only.
* BACKFILL B15: the map opens Backfill on one title, by folder path.
* CLEANUP C28: qui's preference view is a list of fields, which read as
  "couldn't ask" and switched empty-folder detection off without a word.
"""

import json
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app
import audit
import db
from sources import _qui

GB = 1024 ** 3


def _media(path, size=GB, linked=False, dups=(), excluded=False):
    return {'path': path, 'size': size, 'linked_paths': ['/t/x'] if linked else [],
            'duplicate_paths': list(dups), 'excluded': excluded,
            'status': 'Seeding' if linked else 'Orphaned', 'imported': linked}


def _torrent(path, size=GB, status='Seeding', imported=False, linked=False, **extra):
    return {'path': path, 'size': size, 'status': status, 'imported': imported,
            'linked_paths': ['/m/x'] if linked else [], 'duplicate_paths': [], 'excluded': False,
            **extra}


def _titles(row):
    """`{(root, folder, title): [files, b0 … b5]}` from a stored row."""
    folders = row['folders']
    return {(folders[t[0]][0], folders[t[0]][1], t[1]): t[2:] for t in row['titles']}


class LibraryMapRowTests(unittest.TestCase):
    def test_each_state_is_the_workflow_that_acts_on_it(self):
        row = audit.library_map_row(
            [_media('television/Show/e1.mkv', linked=True),
             _media('television/Show/e2.mkv'),
             _media('movies/Film (2020)/f.mkv', dups=['/data/torrents/f.mkv'])],
            [_torrent('radarr/Gone/g.mkv', status='Orphaned'),
             _torrent('radarr/Waiting/w.mkv'),
             _torrent('radarr/Waiting/w.nfo', sidecar=True),
             _torrent('radarr/Busy/b.mkv', incomplete=True)])
        t = _titles(row)
        self.assertEqual(t[('library', 'television', 'Show')], [2, GB, GB, 0, 0, 0, 0])
        self.assertEqual(t[('library', 'movies', 'Film (2020)')], [1, 0, 0, 0, 0, GB, 0])
        self.assertEqual(t[('torrents', 'radarr', 'Gone')], [1, 0, 0, GB, 0, 0, 0])
        # A sidecar of an imported torrent isn't "not imported" (T19), and a
        # download in progress is nobody's yet (T4): both Other.
        self.assertEqual(t[('torrents', 'radarr', 'Waiting')], [2, 0, 0, 0, GB, 0, GB])
        self.assertEqual(t[('torrents', 'radarr', 'Busy')], [1, 0, 0, 0, 0, 0, GB])

    def test_linked_bytes_count_once_in_the_library(self):
        """The torrent-tree side of a linked file is the same inode the library
        side already counted; drawing it twice is the double count the map exists
        to show isn't on disk."""
        row = audit.library_map_row([_media('tv/Show/e1.mkv', linked=True)],
                                    [_torrent('sonarr/Show.S01/e1.mkv', imported=True, linked=True)])
        self.assertEqual(list(_titles(row)), [('library', 'tv', 'Show')])

    def test_excluded_files_are_left_out(self):
        row = audit.library_map_row([_media('tv/Show/e1.nfo', excluded=True)], [])
        self.assertEqual(row['titles'], [])

    def test_a_file_directly_in_a_folder_is_its_own_title(self):
        row = audit.library_map_row([_media('movies/Film.2020.mkv'), _media('loose.mkv')], [])
        self.assertEqual(set(_titles(row)), {('library', 'movies', 'Film.2020.mkv'),
                                             ('library', '', 'loose.mkv')})

    def test_titles_past_the_cap_fold_into_one_per_folder(self):
        media = [_media(f'movies/T{i}/f.mkv', size=(10 - i) * GB) for i in range(5)]
        row = audit.library_map_row(media, [], titles_max=2)
        self.assertEqual([t[1] for t in row['titles']], ['T0', 'T1'])     # largest kept
        [[fi, n_titles, n_files, *by]] = row['folded']
        self.assertEqual((row['folders'][fi], n_titles, n_files), (['library', 'movies'], 3, 3))
        self.assertEqual(by, [0, (8 + 7 + 6) * GB, 0, 0, 0, 0])

    def test_the_row_is_stored_and_served_as_written(self):
        stored = audit.library_map_row([_media('tv/Show/e1.mkv')], [])
        db.db_save_file_results('library_map', stored)
        client = app.app.test_client()
        with patch('app._is_local_client', return_value=True):
            res = client.get('/api/library_map')
        self.assertEqual(json.loads(res.data), stored)


class SharedSizeTests(unittest.TestCase):
    def test_a_library_file_whose_torrent_twin_is_excluded_is_not_held_twice(self):
        """The box: one disc rip linked into the library and excluded on the
        torrent side made `hardlinked_media_size` 23 GB larger than the torrent
        folder, so "on disk" would have come out smaller than the library."""
        media = [_media('m/A/a.mkv', size=10 * GB, linked=True), _media('m/B/b.m2ts', size=3 * GB, linked=True)]
        torrents = [_torrent('t/a.mkv', size=10 * GB, imported=True, linked=True),
                    dict(_torrent('t/b.m2ts', size=3 * GB, imported=True, linked=True), excluded=True)]
        stats = audit.process_health_metrics(media, torrents, {}, update_history=False)
        det = stats['current']['details']
        self.assertEqual(det['hardlinked_media_size'], 13 * GB)
        self.assertEqual(det['shared_size'], 10 * GB)

    def test_disk_space_is_carried_through_a_recompute(self):
        """A config save rebuilds the dashboard from the lists, which can't measure
        free space; it carries the scan's figure forward like the other scan-only
        details."""
        self.assertIn('disk_space', audit.SCAN_ONLY_DETAILS)


class NewMediaTests(unittest.TestCase):
    ROOT = '/data/torrents'

    def _count(self, files, prev=None):
        counts = {'linked': 0, 'copies': 0, 'unseeded': 0}
        audit.compute_diff_from_signatures(
            {'media': prev if prev is not None else {'old/x.mkv': 0}, 'torrents': {}},
            {'media_files': files, 'torrent_files': [], 'dashboard': {}},
            new_media=counts, torrent_root=self.ROOT)
        return counts

    def test_each_new_library_file_is_classed_by_how_it_arrived(self):
        counts = self._count([
            _media('tv/Show/e1.mkv', linked=True),
            _media('tv/Show/e2.mkv', dups=['/data/torrents/sonarr/Show/e2.mkv']),
            _media('tv/Show/e3.mkv', dups=['/data/media/tv/Show/e3 copy.mkv']),
            _media('tv/Show/e4.mkv'),
            _media('old/x.mkv'),                         # not new
            _media('tv/Show/e5.nfo', excluded=True),     # excluded
        ])
        # A copy of another *library* file isn't a failed link; it's unseeded.
        self.assertEqual(counts, {'linked': 1, 'copies': 1, 'unseeded': 2})

    def test_counted_even_when_nothing_else_changed(self):
        """The diff returns None when nothing changed; the counters are an
        out-parameter, so a quiet scan still says "0 new" rather than nothing."""
        counts = {'linked': 0, 'copies': 0, 'unseeded': 0}
        out = audit.compute_diff_from_signatures(
            {'media': {'tv/a.mkv': audit.SIG_IMPORTED}, 'torrents': {}},
            {'media_files': [_media('tv/a.mkv', linked=True)], 'torrent_files': [], 'dashboard': {}},
            new_media=counts, torrent_root=self.ROOT)
        self.assertIsNone(out)
        self.assertEqual(counts, {'linked': 0, 'copies': 0, 'unseeded': 0})

    def test_a_first_scan_of_the_library_counts_nothing(self):
        """With no previous signatures every path is new and none of them arrived."""
        self.assertEqual(self._count([_media('tv/a.mkv')], prev={}),
                         {'linked': 0, 'copies': 0, 'unseeded': 0})

    def test_a_day_sums_its_scans_and_the_library_stats_keep_the_last(self):
        def row(at, nm=None, lib=None):
            snap = {'_instance_count': 1, 'a.cc': {'uploaded': 0, 'seeding_size': 1}}
            library = dict(lib or {'total_media_size': 1})
            if nm is not None:
                library['new_media'] = nm
            snap['_library'] = library
            return {'taken_at': at, 'snapshot': snap, 'source': 'qui'}
        rows = [row('2026-11-08T09:00:00'),                                    # before counting
                row('2026-11-09T09:00:00', {'linked': 3, 'copies': 0, 'unseeded': 1}),
                row('2026-11-09T15:00:00', {'linked': 2, 'copies': 1, 'unseeded': 0},
                    lib={'total_media_size': 2})]
        with patch.object(audit, 'db_get_upload_snapshots', return_value=rows):
            stats = audit.compute_upload_stats(30)
        self.assertEqual(stats['daily_new_media'],
                         [{'date': '2026-11-09', 'linked': 5, 'copies': 1, 'unseeded': 1}])
        self.assertEqual(stats['daily_library_stats'][-1], {'date': '2026-11-09', 'total_media_size': 2})


class ScanReportTests(unittest.TestCase):
    REPORT = {
        'source': 'qui', 'torrent_count': 1444, 'file_map_size': 3583, 'instances_ok': 1,
        'instances_total': 2, 'listing_failures': 0, 'listing_recovered': 3, 'listing_unresolved': 1,
        'tracker_failures': 0, 'partial': True,
        'instances_failed': [{'instance': 'nas', 'reason': "Could not reach qui at 'http://10.0.0.5:7476'"}],
        'notes': ['listing failed for abcdef0123456789abcdef0123 at /data/torrents/secret'],
        'filesystem': {'torrents': {'configured': True, 'exists': True, 'files': 3583, 'stat_errors': 0,
                                    'unlistable': 0, 'unlistable_shallow': 0}},
    }

    def _get(self, runs, meta):
        client = app.app.test_client()
        with patch('app._is_local_client', return_value=True), \
             patch('app.db_load_results', return_value={'dashboard': None}), \
             patch('app.db_get_recent_runs', return_value=runs), \
             patch('app.db_count_runs_since', return_value={'ok': 250, 'anomaly': 4}), \
             patch('app.db_get_meta', side_effect=lambda k, d=None: meta.get(k, d)):
            return client.get('/api/results').get_json()['scan_report']

    def test_counts_only_never_a_note_or_a_reason(self):
        rep = self._get([{'ran_at': '2026-11-09T19:31:42', 'trigger': 'watchdog', 'status': 'ok',
                          'duration_seconds': 2.9, 'peak_rss_mb': 106, 'error_message': None}],
                        {'last_source_report': self.REPORT})
        self.assertEqual(rep['report']['instances_failed'], 1)
        self.assertEqual(rep['report']['listing_unresolved'], 1)
        self.assertEqual(rep['report']['filesystem']['torrents']['files'], 3583)
        text = json.dumps(rep)
        self.assertNotIn('10.0.0.5', text)
        self.assertNotIn('abcdef0123456789', text)
        self.assertNotIn('notes', rep['report'])
        self.assertIsNone(rep['anomaly'])
        self.assertEqual(rep['runs_30d'], {'ok': 250, 'anomaly': 4})

    def test_a_refused_latest_run_carries_why(self):
        rep = self._get([{'ran_at': '2026-11-09T08:12:40', 'trigger': 'scheduled', 'status': 'anomaly',
                          'duration_seconds': 1.0, 'peak_rss_mb': 90,
                          'error_message': 'Source anomaly: …'}],
                        {'last_source_report': self.REPORT,
                         'last_source_anomaly': {'at': '2026-11-09T08:12:40', 'trigger': 'scheduled',
                                                 'code': 'torrent_count_collapse', 'message': 'down from 1,503',
                                                 'detail': {'torrent_count': 612}}})
        self.assertEqual(rep['last_run']['status'], 'anomaly')
        self.assertEqual(rep['anomaly'], {'at': '2026-11-09T08:12:40', 'trigger': 'scheduled',
                                          'code': 'torrent_count_collapse', 'message': 'down from 1,503'})


class BackfillUnderTests(unittest.TestCase):
    """B15: the map's tiles are folders, and a folder's name isn't the arr's title."""
    GROUPS = [
        {'key': 'a', 'folder': '/data/media/television', 'arr_title': 'PLUR1BUS',
         'path': 'television/Pluribus/Season 1/Pluribus.S01E01.mkv'},
        {'key': 'b', 'folder': '/data/media/television', 'arr_title': 'The Pitt',
         'path': 'television/The Pitt/Season 2/The.Pitt.S02E01.mkv'},
        {'key': 'c', 'folder': '/data/media/television', 'arr_title': 'The Pittsburgh Diaries',
         'path': 'television/The Pittsburgh Diaries/S01E01.mkv'},
        {'key': 'd', 'folder': '/data/media/movies', 'arr_title': 'Ran',
         'path': 'movies/Ran.1985.mkv'},
    ]

    def _keys(self, under):
        return [g['key'] for g in app._narrow_backfill(list(self.GROUPS), under=under)]

    def test_a_folder_finds_its_title_whatever_the_arr_calls_it(self):
        self.assertEqual(self._keys('television/Pluribus'), ['a'])

    def test_a_folder_never_matches_a_sibling_that_starts_the_same(self):
        self.assertEqual(self._keys('television/The Pitt'), ['b'])

    def test_a_file_directly_in_a_folder_is_its_own_scope(self):
        self.assertEqual(self._keys('movies/Ran.1985.mkv'), ['d'])

    def test_no_scope_narrows_nothing(self):
        self.assertEqual(self._keys(None), ['a', 'b', 'c', 'd'])
        self.assertEqual(self._keys(''), ['a', 'b', 'c', 'd'])


class QuiPreferenceViewTests(unittest.TestCase):
    """C28: qui answers `/preferences` with a list of fields since its preference
    catalog. `client_save_dirs` read the list as "couldn't ask", silently, so the
    box offered no empty folder for a month."""
    VIEW = [{'key': 'save_path', 'value': '/data/torrents', 'tab': 'downloads', 'control': 'path'},
            {'key': 'use_subcategories', 'value': False, 'tab': 'downloads', 'control': 'switch'},
            {'key': 'temp_path_enabled', 'value': True, 'tab': 'downloads', 'control': 'switch'},
            {'key': 'temp_path', 'value': '/data/torrents/incomplete', 'tab': 'downloads', 'control': 'path'}]

    def _scope(self, prefs, cats):
        sess = MagicMock()
        replies = {'preferences': prefs, 'categories': cats}
        sess.get.side_effect = lambda url, timeout: MagicMock(
            raise_for_status=lambda: None, json=lambda: replies[url.rsplit('/', 1)[1]])
        scope = {'dirs': set(), 'unknown': False}
        _qui._read_save_scope(sess, 'http://qui', {'id': 1, 'name': 'main'},
                              '/data/torrents', '/data/torrents', scope)
        return scope

    def test_the_field_list_is_read(self):
        scope = self._scope(self.VIEW, {'radarr': {'name': 'radarr', 'savePath': ''}})
        self.assertFalse(scope['unknown'])
        self.assertEqual(scope['dirs'], {'/data/torrents', '/data/torrents/radarr',
                                         '/data/torrents/incomplete'})

    def test_the_old_dict_still_reads(self):
        scope = self._scope({f['key']: f['value'] for f in self.VIEW}, {})
        self.assertFalse(scope['unknown'])
        self.assertIn('/data/torrents/incomplete', scope['dirs'])

    def test_a_shape_it_cannot_read_is_unknown_and_says_so(self):
        with self.assertLogs('sources._qui', logging.INFO) as logs:
            scope = self._scope('not a dict', {})
        self.assertTrue(scope['unknown'])
        self.assertIn("shape auditorr doesn't read", logs.output[0])
        self.assertIn('str', logs.output[0])


class DiskSpaceTests(unittest.TestCase):
    def test_both_roots_on_one_filesystem_say_so(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, 'a'), os.path.join(d, 'b')
            os.mkdir(a); os.mkdir(b)
            out = audit.disk_space(a, b)
        self.assertTrue(out['same'])
        self.assertGreater(out['torrents']['total'], 0)

    def test_a_root_that_is_not_there_is_left_out(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = audit.disk_space(d, os.path.join(d, 'missing'))
        self.assertNotIn('media', out)
        self.assertFalse(out['same'])
        self.assertIsNone(audit.disk_space('', ''))


if __name__ == '__main__':
    unittest.main()
