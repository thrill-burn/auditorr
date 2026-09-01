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
        def fake(cfg, service, conn, arr_id):
            if raises:
                raise raises
            return file_id
        with patch('app.db_load_config', return_value={}), \
             patch('app._is_local_client', return_value=True), \
             patch('app.get_arr_file_id', side_effect=fake):
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


if __name__ == '__main__':
    unittest.main()
