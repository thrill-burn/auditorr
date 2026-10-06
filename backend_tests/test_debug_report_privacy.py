"""/api/debug/report is safe to paste in public — the source-health block too.

CODE_REVIEW_2026-09-27 CR7: `last_anomaly` sanitized its `message` and nothing
else, so an `instances_unavailable` refusal published each failed instance's raw
exception text in `detail.instances_failed[].reason` — a qui or qBittorrent
connection error names the host and port it could not reach. `last_report`, just
above it, already sanitized the same field.
"""

import json
import unittest

import app
import debug
from db import db_get_meta, db_set_meta


_REASON = ("HTTPConnectionPool(host='192.168.1.20', port=7476): Max retries exceeded "
           "with url: /api/v2/torrents/info (Caused by NewConnectionError)")


class SourceHealthPrivacyTests(unittest.TestCase):
    def setUp(self):
        for key in ('last_source_anomaly', 'last_source_report'):
            self.addCleanup(db_set_meta, key, db_get_meta(key))

    def _report(self):
        return json.dumps(debug.build_debug_report(app.APP_VERSION))

    def test_a_refused_scans_instance_failure_carries_no_host(self):
        failed = [{'name': 'seedbox 192.168.1.20', 'reason': _REASON}]
        db_set_meta('last_source_anomaly', {
            'code': 'instances_unavailable',
            'message': '1 of 2 torrent-client instance(s) did not answer (seedbox 192.168.1.20).',
            'detail': {'instances_failed': failed},
        })
        report = self._report()
        self.assertNotIn('192.168.1.20', report)
        anomaly = json.loads(report)['source_health']['last_anomaly']
        self.assertEqual(anomaly['code'], 'instances_unavailable')
        self.assertEqual(len(anomaly['detail']['instances_failed']), 1)
        self.assertIn('<ip>', anomaly['detail']['instances_failed'][0]['reason'])

    def test_the_last_persisted_report_carries_no_host_either(self):
        db_set_meta('last_source_report', {
            'torrent_count': 10, 'partial': True,
            'instances_failed': [{'name': 'nas', 'reason': _REASON}],
            'notes': [f'instance nas: {_REASON}'],
        })
        self.assertNotIn('192.168.1.20', self._report())

    def test_an_anomaly_whose_detail_holds_only_counts_is_unchanged(self):
        detail = {'listing_unresolved': 40, 'torrent_count': 100}
        db_set_meta('last_source_anomaly', {
            'code': 'listings_unavailable', 'message': '40 of 100', 'detail': detail})
        anomaly = json.loads(self._report())['source_health']['last_anomaly']
        self.assertEqual(anomaly['detail'], detail)


if __name__ == '__main__':
    unittest.main()
