"""Background scans defer to active workflow sessions: the request hooks feed
state's activity signal (polls excluded), and the watchdog/scheduled triggers
re-check until quiet — bounded for the watchdog, poll-proof for both."""

import time
import unittest
from unittest.mock import patch

import state
from state import (
    set_state, note_workflow_request_start, note_workflow_request_end,
    workflow_active,
)
import watchdog_handler
import app


class ActivitySignalTests(unittest.TestCase):
    def setUp(self):
        self._saved = (state._workflow_inflight, state._workflow_last_activity)
        state._workflow_inflight = 0
        state._workflow_last_activity = 0.0

    def tearDown(self):
        state._workflow_inflight, state._workflow_last_activity = self._saved

    def test_quiet_by_default(self):
        self.assertFalse(workflow_active())

    def test_inflight_request_is_active_regardless_of_grace(self):
        note_workflow_request_start()
        self.assertTrue(workflow_active(grace_s=0))
        note_workflow_request_end()

    def test_grace_window_after_last_request(self):
        note_workflow_request_start()
        note_workflow_request_end()
        self.assertTrue(workflow_active(grace_s=90))
        state._workflow_last_activity = time.time() - 91
        self.assertFalse(workflow_active(grace_s=90))

    def test_end_never_goes_negative(self):
        note_workflow_request_end()
        self.assertEqual(state._workflow_inflight, 0)


class ActivityPathTests(unittest.TestCase):
    """Frontend polls watch_import/active every 5s unconditionally — polls
    must not count as activity or background scans would defer forever."""

    def test_workflow_and_script_paths_count(self):
        for p in ('/api/workflows/triage', '/api/workflows/triage/verify',
                  '/api/workflows/remove_torrents', '/api/actions/script/dedupe'):
            self.assertTrue(app._is_workflow_activity_path(p), p)

    def test_polls_and_other_api_paths_do_not(self):
        for p in ('/api/workflows/watch_import/active',
                  '/api/workflows/generate/status',
                  '/api/workflows/watch_import/status',
                  '/api/results', '/api/progress'):
            self.assertFalse(app._is_workflow_activity_path(p), p)


class RequestHookTests(unittest.TestCase):
    def setUp(self):
        self._saved = (state._workflow_inflight, state._workflow_last_activity)
        state._workflow_inflight = 0
        state._workflow_last_activity = 0.0

    def tearDown(self):
        state._workflow_inflight, state._workflow_last_activity = self._saved

    def test_workflow_request_bumps_activity_and_releases_inflight(self):
        client = app.app.test_client()
        resp = client.post('/api/workflows/triage/verify', json={'items': []})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(state._workflow_inflight, 0)   # teardown released it
        self.assertTrue(workflow_active(grace_s=90))    # but activity is fresh

    def test_poll_endpoint_does_not_bump_activity(self):
        client = app.app.test_client()
        client.get('/api/workflows/watch_import/active')
        self.assertFalse(workflow_active(grace_s=90))


class WatchdogDeferralTests(unittest.TestCase):
    def setUp(self):
        set_state(last_scan_completed_at=0.0)   # skip post-scan suppression

    def _fire(self, handler, active, scan_mock_result=False):
        with patch.object(watchdog_handler, 'workflow_active', return_value=active), \
             patch.object(watchdog_handler, 'try_start_scanning',
                          return_value=scan_mock_result) as tss:
            handler._fire()
        if handler._timer:
            handler._timer.cancel()
        return tss

    def test_defers_and_rearms_while_workflow_active(self):
        h = watchdog_handler.AuditDebounceHandler(lambda: 60)
        tss = self._fire(h, active=True)
        tss.assert_not_called()
        self.assertIsNotNone(h._deferred_since)
        self.assertIsNotNone(h._timer)   # re-armed, not dropped

    def test_max_deferral_scans_anyway(self):
        h = watchdog_handler.AuditDebounceHandler(lambda: 60)
        h._deferred_since = time.time() - watchdog_handler._MAX_WORKFLOW_DEFER_S - 1
        tss = self._fire(h, active=True)
        tss.assert_called_once_with("watchdog")
        self.assertIsNone(h._deferred_since)   # episode over

    def test_fires_immediately_when_quiet(self):
        h = watchdog_handler.AuditDebounceHandler(lambda: 60)
        tss = self._fire(h, active=False)
        tss.assert_called_once_with("watchdog")
        self.assertIsNone(h._deferred_since)


if __name__ == '__main__':
    unittest.main()
