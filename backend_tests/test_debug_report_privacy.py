"""/api/debug/report is safe to paste in public — the source-health block too.

CODE_REVIEW_2026-09-27 CR7: `last_anomaly` sanitized its `message` and nothing
else, so an `instances_unavailable` refusal published each failed instance's raw
exception text in `detail.instances_failed[].reason` — a qui or qBittorrent
connection error names the host and port it could not reach. `last_report`, just
above it, already sanitized the same field.

2026-10-06: and the sanitizer only knew two spellings of a host, an IPv4
address and `scheme://host`. requests names a host that is a *name* three more
ways in one message (`host='qui.lan'`, `Failed to resolve 'qui.lan'`,
`Connection to qui.lan timed out`), so CR7's fix still published the qui
address of anyone who reaches qui by name. The report now also redacts every
configured address from every string as its last step, so a field that skips
the sanitizer can't carry one either.
"""

import json
import unittest

import app
import debug
from db import db_get_meta, db_load_config, db_save_config, db_set_meta


_REASON = ("HTTPConnectionPool(host='192.168.1.20', port=7476): Max retries exceeded "
           "with url: /api/v2/torrents/info (Caused by NewConnectionError)")

# Verbatim from requests 2.32 / urllib3 2.6, host renamed: a name that doesn't
# resolve, and one that resolves and times out.
_NAME_REASONS = (
    "HTTPConnectionPool(host='qui.lan', port=7476): Max retries exceeded with url: "
    "/api/instances (Caused by NameResolutionError(\"HTTPConnection(host='qui.lan', "
    "port=7476): Failed to resolve 'qui.lan' ([Errno -2] Name or service not known)\"))",
    "HTTPSConnectionPool(host='qui.example.com', port=443): Max retries exceeded with "
    "url: /api/instances/1/torrents?limit=500 (Caused by ConnectTimeoutError("
    "<HTTPSConnection(host='qui.example.com', port=443) at 0x7f3a>, 'Connection to "
    "qui.example.com timed out. (connect timeout=30)'))",
)


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
        self.assertIn("host='<host>'", anomaly['detail']['instances_failed'][0]['reason'])

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


class HostSpellingTests(unittest.TestCase):
    """Every way a connection error spells a host that is a name."""

    def test_no_spelling_of_a_named_host_survives(self):
        for reason in _NAME_REASONS:
            out = debug.sanitize_text(reason)
            self.assertNotIn('qui.lan', out)
            self.assertNotIn('qui.example.com', out)
            self.assertIn("host='<host>'", out)

    def test_a_schemeless_host_in_quotes_is_redacted(self):
        for text in ("Could not reach qBittorrent at 'nas.lan:8080'",
                     "Could not reach qBittorrent at 'tower:8080'",
                     "certificate is not valid for 'qui.example.com'."):
            self.assertIn("'<host>'", debug.sanitize_text(text))

    def test_a_url_keeps_its_placeholder_through_path_hashing(self):
        out = debug.sanitize_text('502 Server Error for url: http://qui.lan:7476/api/instances')
        self.assertIn('http://<host>/', out)

    def test_quoted_things_that_are_not_hosts_are_left_alone(self):
        for text in ("KeyError: 'save_path'", "'utf-8' codec can't decode",
                     "version '1.8.0'", "at '12:30'"):
            self.assertEqual(debug.sanitize_text(text), text)
        # A file or release name is still hashed as one, never called a host.
        for text in ("file_name='example.txt'", "folder='Some.Show.S01E01.WEB-DL'"):
            out = debug.sanitize_text(text)
            self.assertNotIn('<host>', out)
            self.assertIn("'~", out)


class ConfiguredAddressTests(unittest.TestCase):
    """The last pass: an address auditorr is configured with appears nowhere,
    including fields that never go through the sanitizer."""

    def setUp(self):
        cfg = db_load_config()
        self.addCleanup(db_save_config, cfg)
        for key in ('scan_marker', 'last_source_report'):
            self.addCleanup(db_set_meta, key, db_get_meta(key))

    def _configure(self, **overrides):
        db_save_config({**db_load_config(), **overrides})

    def _report(self):
        return json.dumps(debug.build_debug_report(app.APP_VERSION))

    def test_a_configured_host_is_redacted_from_an_unsanitized_field(self):
        self._configure(
            QUI_HOST='https://qui.example.home', QB_HOST='seedbox.lan:8080',
            ARR_CONNECTIONS=[{'id': 'sonarr-4k', 'service': 'sonarr',
                              'base_url': 'http://sonarr4k.example.home:8989',
                              'external_url': 'https://tv.example.org', 'api_key': 'k'}])
        # `scan_marker` is emitted as stored, past every sanitizer.
        db_set_meta('scan_marker', {
            'phase': 'Fetching from QUI.example.home, seedbox.lan:8080, '
                     'sonarr4k.example.home and tv.example.org'})
        report = self._report()
        for host in ('qui.example.home', 'seedbox.lan', 'sonarr4k.example.home', 'tv.example.org'):
            self.assertNotIn(host, report.lower())
        marker = json.loads(report)['crash_evidence']['in_progress_scan_marker']
        self.assertIn('<host>:8080', marker['phase'])

    def test_a_docker_service_name_goes_only_where_it_is_a_host(self):
        self._configure(QUI_HOST='http://qui:7476')
        db_set_meta('scan_marker', {'phase': 'qui: listing qui:7476 and //qui/api'})
        report = json.loads(self._report())
        self.assertEqual(report['crash_evidence']['in_progress_scan_marker']['phase'],
                         'qui: listing <host>:7476 and //<host>/api')
        # The word is everywhere in the report, and stays.
        self.assertIn('qui_host_configured', report['config'])
        self.assertIn('qui instance', report['source_health']['_readme'])

    def test_a_named_failure_in_the_last_report_carries_no_host(self):
        self._configure(QUI_HOST='http://qui.lan:7476')
        db_set_meta('last_source_report', {
            'torrent_count': 10, 'partial': True,
            'instances_failed': [{'name': 'nas', 'reason': _NAME_REASONS[0]}],
            'notes': [f'instance nas: {_NAME_REASONS[1]}'],
        })
        report = self._report()
        self.assertNotIn('qui.lan', report)
        self.assertNotIn('qui.example.com', report)


if __name__ == '__main__':
    unittest.main()
