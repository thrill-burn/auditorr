"""Acting on a workflow row has to be reflected everywhere, not just in the row.

Two failures sat behind this, and only the first one was visible. Removing a
torrent dropped the row on the page but left the sidebar badge, the dashboard
and every other workflow report reading the pre-action audit — and navigating
away and back re-fetched the deleted row, because the server's answer *is* that
audit. The second is worse: a keep-files removal or an added exclusion changes
no file at all, so the filesystem watcher never fires and nothing scheduled a
scan to correct any of it until the next scheduled audit, hours later.
"""

import unittest
from unittest.mock import patch

import watchdog_handler
import app


class NudgeTests(unittest.TestCase):
    def tearDown(self):
        watchdog_handler._handler = None

    def test_nudge_without_a_running_watchdog_is_a_no_op(self):
        """Watchdog disabled in config, or not started yet. Must not raise —
        every action endpoint calls this on its success path."""
        watchdog_handler._handler = None
        self.assertFalse(watchdog_handler.nudge_watchdog('test'))

    def test_nudge_restarts_the_debounce_clock(self):
        handler = watchdog_handler.AuditDebounceHandler(lambda: 60)
        watchdog_handler._handler = handler
        with patch.object(handler, '_reset_timer') as reset:
            self.assertTrue(watchdog_handler.nudge_watchdog('test'))
            reset.assert_called_once_with()

    def test_nudge_goes_through_the_debounce_not_straight_to_a_scan(self):
        """The whole reason this reuses the filesystem-event entry point: a bulk
        page produces a burst of actions, and each one must reset one timer
        rather than start one scan."""
        handler = watchdog_handler.AuditDebounceHandler(lambda: 60)
        watchdog_handler._handler = handler
        with patch.object(handler, '_reset_timer') as reset, \
             patch('watchdog_handler.try_start_scanning') as start:
            for _ in range(20):
                watchdog_handler.nudge_watchdog('bulk')
            self.assertEqual(reset.call_count, 20)
            start.assert_not_called()


class ExcludeNudgesTests(unittest.TestCase):
    """An exclusion touches config only — no file changes, so the watcher is
    blind to it, yet every count auditorr reports just moved."""

    def setUp(self):
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()

    def _post(self, patterns, existing=None):
        cfg = {'EXCLUSION_PATTERNS': list(existing or [])}
        with patch('app.db_load_config', return_value=cfg), \
             patch('app.db_save_config'), \
             patch('app._is_local_client', return_value=True), \
             patch('app.nudge_watchdog') as nudge:
            resp = self.client.post('/api/workflows/exclude', json={'patterns': patterns})
        return resp, nudge

    def test_added_pattern_schedules_an_audit(self):
        resp, nudge = self._post(['ext:sfv'])
        self.assertEqual(resp.status_code, 200)
        nudge.assert_called_once()

    def test_duplicate_pattern_does_not(self):
        """Nothing changed, so nothing needs re-counting. Clicking the same
        suggestion chip twice must not cost a second scan."""
        resp, nudge = self._post(['ext:sfv'], existing=['ext:sfv'])
        self.assertEqual(resp.get_json()['added'], 0)
        nudge.assert_not_called()


class ImportCheckTests(unittest.TestCase):
    """A Triage rescan hands the file to Sonarr/Radarr, which imports on its own
    schedule and reports nothing back. The arr's own file id is the only honest
    signal that it landed — the same one force_import confirms with."""

    def setUp(self):
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()

    def _post(self, items, file_id=None, raises=None):
        """Stands in for `read_arr_file_id`, the reader the route calls.

        These used to stub `get_arr_file_id` to raise, and that is what hid S08:
        the real helper never raised — it swallowed the failure and answered
        `None`. `read_arr_file_id`'s contract *is* "raise when the read fails",
        so a raising stand-in is now a faithful one; `ImportCheckReadTests`
        below drives the real reader with only `_arr_get` mocked.
        """
        def fake(cfg, service, conn, arr_id):
            if raises:
                raise raises
            return file_id
        with patch('app.db_load_config', return_value={}), \
             patch('app._is_local_client', return_value=True), \
             patch('app.read_arr_file_id', side_effect=fake):
            return self.client.post('/api/workflows/import_check', json={'items': items})

    def test_reports_the_current_file_id(self):
        resp = self._post([{'key': 'a', 'service': 'radarr',
                            'connection_id': 'c1', 'arr_id': 7}], file_id=42)
        row = resp.get_json()['results'][0]
        self.assertEqual((row['key'], row['file_id'], row['checked']), ('a', 42, True))

    def test_an_unreachable_arr_is_not_an_import(self):
        """`checked: false` must stay distinct from `file_id: null`. Collapsing
        them would read a timeout as 'the file changed' and drop a live row."""
        resp = self._post([{'key': 'a', 'service': 'sonarr',
                            'connection_id': 'c1', 'arr_id': 7}],
                          raises=RuntimeError('timeout'))
        row = resp.get_json()['results'][0]
        self.assertFalse(row['checked'])
        self.assertIsNone(row['file_id'])

    def test_item_with_no_library_match_is_reported_unchecked(self):
        resp = self._post([{'key': 'a', 'service': '', 'connection_id': '', 'arr_id': None}])
        row = resp.get_json()['results'][0]
        self.assertFalse(row['checked'])

    def test_batch_is_capped(self):
        """One arr call per item, polled every few seconds — deliberately a
        smaller cap than the tracker verify batch."""
        items = [{'key': str(i), 'service': 'radarr', 'connection_id': 'c', 'arr_id': i}
                 for i in range(app._IMPORT_CHECK_MAX + 25)]
        resp = self._post(items, file_id=1)
        self.assertEqual(len(resp.get_json()['results']), app._IMPORT_CHECK_MAX)

    def test_empty_request_is_rejected(self):
        resp = self._post([])
        self.assertEqual(resp.status_code, 400)

    def test_a_sonarr_item_actually_serializes(self):
        """The real `get_arr_file_id`, not a mocked int — every other test here
        patches it away, which is exactly why this shipped broken.

        Sonarr's answer used to be a `frozenset`, which is not JSON-serializable,
        so `jsonify` raised and the *whole* request 500'd. The rescan
        follow-through has never worked for Sonarr, and a mixed selection lost
        its Radarr answers too."""
        conns = [{'id': 'c1', 'base_url': 'http://sonarr:8989', 'api_key': 'k'}]
        eps   = [{'id': 30}, {'id': 10}, {'id': 20}]
        with patch('app.db_load_config', return_value={}), \
             patch('app._is_local_client', return_value=True), \
             patch('arr.normalize_arr_connections', return_value=conns), \
             patch('arr._arr_get', return_value=eps):
            resp = self.client.post('/api/workflows/import_check', json={'items': [
                {'key': 'a', 'service': 'sonarr', 'connection_id': 'c1', 'arr_id': 7}]})
        self.assertEqual(resp.status_code, 200)
        row = resp.get_json()['results'][0]
        self.assertTrue(row['checked'])
        # Sorted, not just listed: the caller detects an import by comparing two
        # readings with `!=`, so a reordered response would read as a change.
        self.assertEqual(row['file_id'], [10, 20, 30])


class ImportCheckReadTests(unittest.TestCase):
    """S08 + T11 — the watch confirms only a read that happened, of the file meant.

    Mocked **below** the helper (`arr._arr_get`), never at `get_arr_file_id`.
    `test_an_unreachable_arr_is_not_an_import` above patches the helper to raise,
    and that is exactly what hid S08: the real helper swallowed every exception
    and answered `None`, the route reported `checked: true, file_id: null`, and
    the page read a timed-out read that differed from its baseline as a landed
    import — dismissing the row and toasting "import confirmed". The fixture was
    the bug, the third time (T12, 4d).
    """

    CONNS = [{'id': 'tv', 'service': 'sonarr', 'name': 'TV',
              'base_url': 'http://sonarr:8989', 'api_key': 'k'},
             {'id': 'films', 'service': 'radarr', 'name': 'Films',
              'base_url': 'http://radarr:7878', 'api_key': 'k'}]

    EPISODES = [
        {'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'episodeFileId': 100},
        {'id': 2, 'seasonNumber': 1, 'episodeNumber': 2, 'episodeFileId': 0},
        {'id': 3, 'seasonNumber': 2, 'episodeNumber': 1, 'episodeFileId': 300},
    ]

    def setUp(self):
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()

    def _check(self, items, arr_get):
        def conns(_cfg, service=None):
            return [c for c in self.CONNS if service in (None, c['service'])]
        with patch('app.db_load_config', return_value={}), \
             patch('app._is_local_client', return_value=True), \
             patch('arr.normalize_arr_connections', side_effect=conns), \
             patch('arr._arr_get', side_effect=arr_get):
            resp = self.client.post('/api/workflows/import_check', json={'items': items})
        self.assertEqual(resp.status_code, 200)
        return {r['key']: r for r in resp.get_json()['results']}

    def _sonarr(self, episodes):
        """Series 5's two Sonarr listings, consistent with each other."""
        files = sorted({e['episodeFileId'] for e in episodes if e['episodeFileId']})

        def get(_base, _key, path, **_kw):
            if path.startswith('/api/v3/episode?seriesId=5'):
                return [dict(e) for e in episodes]
            if path.startswith('/api/v3/episodefile?seriesId=5'):
                return [{'id': f} for f in files]
            raise AssertionError(f'unexpected arr call: {path}')
        return get

    def test_import_check_reports_a_failed_arr_read_as_unchecked(self):
        def down(*_a, **_kw):
            raise OSError('timed out')
        got = self._check([
            {'key': 'film', 'service': 'radarr', 'connection_id': 'films', 'arr_id': 7},
            {'key': 'series', 'service': 'sonarr', 'connection_id': 'tv', 'arr_id': 5},
            {'key': 'episode', 'service': 'sonarr', 'connection_id': 'tv', 'arr_id': 5,
             'season': 1, 'episode': 2},
        ], down)
        for key in ('film', 'series', 'episode'):
            self.assertFalse(got[key]['checked'], key)
            self.assertIsNone(got[key]['file_id'], key)

    def test_a_radarr_movie_with_no_file_is_still_a_checked_answer(self):
        """The other half of the distinction: asked, and it holds nothing."""
        got = self._check([{'key': 'film', 'service': 'radarr', 'connection_id': 'films', 'arr_id': 7}],
                          lambda _b, _k, path, **_kw: {'id': 7, 'movieFileId': 0})
        self.assertTrue(got['film']['checked'])

    def test_a_sonarr_import_check_watches_the_episode_not_the_series(self):
        item = {'key': 'e2', 'service': 'sonarr', 'connection_id': 'tv', 'arr_id': 5,
                'season': 1, 'episode': 2}
        before = self._check([item], self._sonarr(self.EPISODES))['e2']
        self.assertTrue(before['checked'])

        # Sonarr upgrades an unrelated episode of the same series mid-watch.
        busy = [dict(e) for e in self.EPISODES]
        busy[2]['episodeFileId'] = 301
        during = self._check([item], self._sonarr(busy))['e2']
        self.assertEqual(during['file_id'], before['file_id'])

        # The episode the row is for landing is what moves it.
        landed = [dict(e) for e in busy]
        landed[1]['episodeFileId'] = 102
        after = self._check([item], self._sonarr(landed))['e2']
        self.assertNotEqual(after['file_id'], before['file_id'])

    def test_a_season_pack_row_watches_that_seasons_files(self):
        item = {'key': 'pack', 'service': 'sonarr', 'connection_id': 'tv', 'arr_id': 5,
                'season': 1, 'episode': None}
        got = self._check([item], self._sonarr(self.EPISODES))['pack']
        self.assertTrue(got['checked'])
        self.assertEqual(got['file_id'], [100])


if __name__ == '__main__':
    unittest.main()
