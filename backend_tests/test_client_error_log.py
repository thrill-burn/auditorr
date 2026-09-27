"""POST /api/debug/client_error — a render error the frontend's error boundary
caught becomes one server log line (2026-09-25).

Before it, a render error existed only in the browser that had it: the app
unmounted to a blank page and nothing reached `docker logs` or
`/api/debug/report`, so the Segmented crash had to be reproduced to be found.
"""

import logging
import unittest
from unittest.mock import patch

import app
import debug


def _post(client, body, remote_addr='127.0.0.1'):
    return client.post('/api/debug/client_error', json=body,
                       environ_base={'REMOTE_ADDR': remote_addr})


class ClientErrorLogTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        for name, value in (('AUDITORR_SECRET', ''),
                            ('AUDITORR_REQUIRE_AUTH', False),
                            ('AUDITORR_TRUSTED_NETWORKS', [])):
            p = patch.object(app, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_logs_one_error_line_with_page_message_and_where(self):
        with self.assertLogs(app.log, level='ERROR') as logs:
            res = _post(self.client, {
                'page': 'dashboard', 'name': 'TypeError',
                'message': "Cannot read properties of null (reading 'length')",
                'where': 'Dashboard › ChangesPanel › Segmented',
            })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(logs.records), 1)
        line = logs.records[0].getMessage()
        self.assertIn("page 'dashboard'", line)
        self.assertIn("TypeError: Cannot read properties of null (reading 'length')", line)
        self.assertIn('(in Dashboard › ChangesPanel › Segmented)', line)

    def test_newlines_cannot_forge_a_second_log_line(self):
        with self.assertLogs(app.log, level='ERROR') as logs:
            _post(self.client, {
                'page': 'triage', 'name': 'Error',
                'message': 'boom\n2026-09-25 12:00:00 [INFO] Audit complete\r\nmore',
            })
        line = logs.records[0].getMessage()
        self.assertNotIn('\n', line)
        self.assertNotIn('\r', line)
        self.assertIn('boom 2026-09-25 12:00:00 [INFO] Audit complete more', line)

    def test_fields_are_capped(self):
        with self.assertLogs(app.log, level='ERROR') as logs:
            _post(self.client, {'page': 'p' * 500, 'name': 'Error',
                                'message': 'm' * 5000, 'where': 'W' * 5000})
        line = logs.records[0].getMessage()
        caps = app._CLIENT_ERROR_CAPS
        self.assertIn('p' * (caps['page'] - 1) + '…', line)
        self.assertNotIn('p' * caps['page'], line)
        self.assertIn('m' * (caps['message'] - 1) + '…', line)
        self.assertNotIn('m' * caps['message'], line)
        self.assertNotIn('W' * caps['where'], line)

    def test_optional_fields_may_be_missing_or_not_strings(self):
        with self.assertLogs(app.log, level='ERROR') as logs:
            res = _post(self.client, {'message': 'x is not a function',
                                      'page': 7, 'where': ['A', 'B']})
        self.assertEqual(res.status_code, 200)
        line = logs.records[0].getMessage()
        self.assertIn("page '?'", line)
        self.assertIn('Error: x is not a function', line)
        self.assertNotIn('(in', line)

    def test_a_body_without_a_message_is_refused_and_not_logged(self):
        for body in ({}, {'message': 12}, ['message']):
            with self.subTest(body=body), patch.object(app.log, 'error') as err:
                res = _post(self.client, body)
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.get_json()['code'], 'bad_request')
                err.assert_not_called()

    def test_requires_auth_like_every_other_api_route(self):
        with patch.object(app.log, 'error') as err:
            res = _post(self.client, {'message': 'boom'}, remote_addr='8.8.8.8')
        self.assertEqual(res.status_code, 401)
        err.assert_not_called()

    def test_reaches_the_debug_report_with_paths_scrubbed(self):
        # The report is safe to paste in public; a path that lands in a message
        # must be hashed there like in any other log line.
        _post(self.client, {
            'page': 'media', 'name': 'Error',
            'message': 'bad row /data/media/movies/Secret.Film.2020/Secret.Film.2020.mkv',
        })
        report = debug.build_debug_report(app.APP_VERSION)
        lines = [r['msg'] for r in report['recent_logs'] if 'Frontend render error' in r['msg']]
        self.assertTrue(lines)
        self.assertNotIn('Secret', lines[-1])
        self.assertIn('/data/media/movies/', lines[-1])


if __name__ == '__main__':
    logging.basicConfig()
    unittest.main()
