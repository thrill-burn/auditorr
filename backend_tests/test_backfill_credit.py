"""Backfill scores at the grab, not at the import watch.

The watch (`/api/workflows/watch_import`) is an in-memory daemon thread that
nurses a download into the library and can live for two hours. It used to be
what awarded the Matchmaker credit, so every way it could end early — the
container restarting mid-download, an arr blip, an import stalled until the
user finished it by hand — silently dropped the points for work that had
already been done, with no way to earn them back. A layer whose first rule is
that points are never taken away must not hang them on the least durable thing
in the request path.
"""

import unittest
from unittest.mock import patch

import app


class _NeverRuns:
    """The import-watch thread, created and started but never scheduled.

    Stands in for every way that thread can fail to reach `mark_done` — which,
    on a box that restarts mid-download, is the normal case rather than an
    exotic one.
    """
    instances = []

    def __init__(self, *args, **kwargs):
        _NeverRuns.instances.append(self)

    def start(self):
        pass


class BackfillCreditTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.store = {}
        _NeverRuns.instances = []

        def fake_update_meta(key, fn, default=None):
            self.store[key] = fn(self.store.get(key, default))
            return self.store[key]

        patches = [
            patch.object(app, 'AUDITORR_SECRET', ''),
            patch.object(app, 'AUDITORR_REQUIRE_AUTH', False),
            patch.object(app, 'db_update_meta', fake_update_meta),
            patch.object(app, 'db_load_config', lambda: {}),
            patch.object(app.threading, 'Thread', _NeverRuns),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _grab(self, **over):
        body = {'service': 'radarr', 'connection_id': 'c1', 'arr_id': 7,
                'title': 'Some Film (2024)', 'files': 12}
        body.update(over)
        return self.client.post('/api/workflows/watch_import', json=body,
                                environ_base={'REMOTE_ADDR': '127.0.0.1'})

    @property
    def progress(self):
        return self.store.get('ns_progress') or {}

    def test_a_grab_scores_without_waiting_for_the_import(self):
        res = self._grab()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get('job_id'))
        self.assertEqual(self.progress['backfilled'], 12)
        self.assertEqual(self.progress['backfill_releases'], 1)
        self.assertEqual(self.progress['backfill_max'], 12)

    def test_the_credit_does_not_depend_on_the_watch_thread(self):
        self._grab()
        self.assertEqual(len(_NeverRuns.instances), 1,
                         'the watch is still started — it just no longer scores')
        self.assertEqual(self.progress['backfilled'], 12)

    def test_a_season_pack_counts_its_episodes_and_a_stale_client_counts_one(self):
        # Matchmaker counts files: one Sonarr grab, a dozen episodes. A bundle
        # too old to send `files` credits one rather than nothing.
        self._grab(files=None)
        self.assertEqual(self.progress['backfilled'], 1)
        self._grab(service='sonarr', arr_id=9, files=12)
        self.assertEqual(self.progress['backfilled'], 13)
        self.assertEqual(self.progress['backfill_releases'], 2)
        self.assertEqual(self.progress['backfill_max'], 12)

    def test_a_rejected_grab_scores_nothing(self):
        res = self._grab(arr_id=None)
        self.assertEqual(res.status_code, 400)
        self.assertNotIn('ns_progress', self.store)
        self.assertEqual(_NeverRuns.instances, [])

    def test_a_storage_failure_does_not_fail_the_grab(self):
        # The watch is the user-visible half of this endpoint; a prize counter
        # that cannot be written must not take it down with it.
        with patch.object(app, 'db_update_meta', side_effect=RuntimeError('db locked')):
            res = self._grab()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(_NeverRuns.instances), 1)


if __name__ == '__main__':
    unittest.main()
