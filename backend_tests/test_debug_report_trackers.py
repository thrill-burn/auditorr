"""/api/debug/report names no tracker (2026-11-17).

The report is advertised as safe to paste in public, and a private tracker's
name is exactly what must not go there: it says which trackers someone is on,
and most trackers' rules forbid naming them. URLs were already redacted, but a
bare host (`tracker.example:2710`) and an indexer's name in an arr's error text
("Unable to connect to indexer Example", "Indexer Example (API) (Prowlarr) is
disabled") went through as written, into `recent_logs`.

The last pass now redacts every tracker the last scan saw (and its aliases)
and every indexer name in Backfill's settings, wherever they appear. The names
here are made up.
"""

import json
import unittest
from unittest import mock

import app
import debug
from db import db_get_meta, db_load_config, db_save_config, db_set_meta


_RESULTS = {
    'trackers': ['alphatrk.example', 'tracker.bravotrk.example:2710', 'None'],
    'tracker_aliases': {'tracker.bravo-mirror.example': 'tracker.bravotrk.example'},
}
_LOGS = [
    'Release search failed for sonarr-1:12:1: Unable to connect to indexer Alphatrk, check the log',
    'Grab failed for radarr/abc123: Indexer Gammatrk (API) (Prowlarr) is disabled till 2026-11-18',
    'tracker.bravotrk.example:2710 Change your port and try again',
    'BravoTrk rejected the grab; tracker.bravo-mirror.example answered 403',
    'Search on HDZ failed',
]
_NAMES = ('alphatrk', 'bravotrk', 'bravo-mirror', 'gammatrk', 'hdz')


class TrackerNameTests(unittest.TestCase):
    def setUp(self):
        cfg = db_load_config()
        self.addCleanup(db_save_config, cfg)
        self.addCleanup(db_set_meta, 'scan_marker', db_get_meta('scan_marker'))
        db_save_config({**cfg, 'ACQUIRE_SEEDING_ON': ['Gammatrk (API) (Prowlarr)'],
                        'ACQUIRE_DOWNLOAD_FROM': ['HDZ']})

    def _report(self, results=_RESULTS, logs=_LOGS):
        records = [{'ts': '2026-11-17 12:00:00', 'level': 'WARNING', 'msg': m} for m in logs]
        with mock.patch.object(debug, 'db_load_results', return_value=results), \
             mock.patch.object(debug, '_recent_logs', return_value=records):
            return json.dumps(debug.build_debug_report(app.APP_VERSION))

    def test_no_tracker_or_indexer_name_reaches_the_report(self):
        # `scan_marker` is emitted as stored, past every per-field sanitizer.
        db_set_meta('scan_marker', {'phase': 'Asking ALPHATRK.example and Gammatrk'})
        report = self._report().lower()
        for name in _NAMES:
            self.assertNotIn(name, report)
        self.assertIn('<tracker>', report)

    def test_the_log_line_keeps_its_shape(self):
        logs = json.loads(self._report())['recent_logs']
        msgs = [r['msg'] for r in logs]
        self.assertIn('<tracker>:2710 Change your port and try again', msgs)
        self.assertTrue(any('Unable to connect to indexer <tracker>, check the log' in m for m in msgs))
        self.assertTrue(any('Indexer <tracker> is disabled' in m for m in msgs))

    def test_a_host_named_like_an_ordinary_word_leaves_the_word(self):
        # A tracker at `torrents.example.org` must not erase "torrents" (a key
        # of the report's own) or "upload"; its host still goes.
        results = {'trackers': ['torrents.example.org', 'upload.example'], 'tracker_aliases': {}}
        report = json.loads(self._report(results, ['torrents.example.org: upload stats unavailable']))
        self.assertIn('torrents', report['library'])
        self.assertEqual(report['recent_logs'][0]['msg'], '<tracker>: upload stats unavailable')

    def test_the_walks_none_is_not_a_tracker(self):
        hosts, words = debug._known_tracker_names(db_load_config())
        with mock.patch.object(debug, 'db_load_results', return_value=_RESULTS):
            hosts, words = debug._known_tracker_names(db_load_config())
        self.assertNotIn('none', hosts)
        self.assertNotIn('none', {w.lower() for w in words})
        self.assertIn('tracker.bravotrk.example', hosts)      # the port is dropped
        self.assertIn('bravotrk', words)                       # the `tracker.` prefix too

    def test_nothing_known_means_no_pattern(self):
        cfg = {**db_load_config(), 'ACQUIRE_SEEDING_ON': [], 'ACQUIRE_DOWNLOAD_FROM': []}
        with mock.patch.object(debug, 'db_load_results', return_value={}):
            self.assertIsNone(debug._tracker_name_re(cfg))


if __name__ == '__main__':
    unittest.main()
