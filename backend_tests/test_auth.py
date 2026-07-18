"""API authentication (#18). With no AUDITORR_SECRET configured, only local
clients (loopback / private ranges / AUDITORR_TRUSTED_NETWORKS) are served —
non-local clients get a generic 401, so an internet-reachable instance no
longer runs open. A configured secret is enforced for every client via the
X-Auditorr-Secret header only (query-string auth leaks into logs and was
removed). AUDITORR_REQUIRE_AUTH=true drops the local exemption; combined with
no secret it fails closed (503 auth_not_configured). /health stays open."""

import ipaddress
import unittest
from unittest.mock import patch

import app


def _get(client, path, remote_addr='127.0.0.1', **kwargs):
    return client.get(path, environ_base={'REMOTE_ADDR': remote_addr}, **kwargs)


class NoSecretLocalExemptionTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        for name, value in (('AUDITORR_SECRET', ''),
                            ('AUDITORR_REQUIRE_AUTH', False),
                            ('AUDITORR_TRUSTED_NETWORKS', [])):
            p = patch.object(app, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_loopback_passes(self):
        self.assertEqual(_get(self.client, '/api/progress').status_code, 200)

    def test_private_ranges_pass(self):
        for addr in ('192.168.1.5', '10.0.0.2', '172.16.0.9'):
            self.assertEqual(
                _get(self.client, '/api/progress', remote_addr=addr).status_code,
                200, addr)

    def test_public_address_denied_generic_401(self):
        # NB: not a 203.0.113/24 documentation address — modern Python counts
        # those as is_private (IANA non-routable registry), which is fine for
        # the model (they can't arrive from the internet) but wrong for this test.
        res = _get(self.client, '/api/progress', remote_addr='8.8.8.8')
        self.assertEqual(res.status_code, 401)
        # Must not reveal whether a secret is configured
        self.assertNotIn('code', res.get_json())

    def test_missing_remote_addr_denied(self):
        res = self.client.get('/api/progress', environ_base={'REMOTE_ADDR': ''})
        self.assertEqual(res.status_code, 401)

    def test_trusted_network_extends_exemption(self):
        with patch.object(app, 'AUDITORR_TRUSTED_NETWORKS',
                          [ipaddress.ip_network('100.64.0.0/10')]):
            ok = _get(self.client, '/api/progress', remote_addr='100.64.0.3')
            still_denied = _get(self.client, '/api/progress', remote_addr='8.8.8.8')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(still_denied.status_code, 401)

    def test_health_open_to_public(self):
        res = _get(self.client, '/health', remote_addr='8.8.8.8')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['status'], 'ok')


class SecretConfiguredTests(unittest.TestCase):
    SECRET = 'test-secret-123'

    def setUp(self):
        self.client = app.app.test_client()
        p = patch.object(app, 'AUDITORR_SECRET', self.SECRET)
        p.start()
        self.addCleanup(p.stop)

    def test_enforced_even_for_local_clients(self):
        self.assertEqual(_get(self.client, '/api/progress').status_code, 401)

    def test_wrong_header_unauthorized(self):
        res = _get(self.client, '/api/progress',
                   headers={'X-Auditorr-Secret': 'wrong'})
        self.assertEqual(res.status_code, 401)

    def test_correct_header_passes_from_anywhere(self):
        res = _get(self.client, '/api/progress', remote_addr='203.0.113.7',
                   headers={'X-Auditorr-Secret': self.SECRET})
        self.assertEqual(res.status_code, 200)

    def test_query_string_auth_removed(self):
        res = _get(self.client, '/api/progress?secret=' + self.SECRET)
        self.assertEqual(res.status_code, 401)


class StrictModeTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        p = patch.object(app, 'AUDITORR_REQUIRE_AUTH', True)
        p.start()
        self.addCleanup(p.stop)

    def test_no_secret_fails_closed_even_locally(self):
        with patch.object(app, 'AUDITORR_SECRET', ''):
            res = _get(self.client, '/api/progress')
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.get_json()['code'], 'auth_not_configured')

    def test_with_secret_behaves_normally(self):
        with patch.object(app, 'AUDITORR_SECRET', 's'):
            denied = _get(self.client, '/api/progress')
            ok = _get(self.client, '/api/progress',
                      headers={'X-Auditorr-Secret': 's'})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(ok.status_code, 200)


class TrustedNetworkParsingTests(unittest.TestCase):
    def test_parses_and_skips_invalid(self):
        nets = app._parse_trusted_networks('100.64.0.0/10, junk, 192.168.0.0/16,')
        self.assertEqual([str(n) for n in nets],
                         ['100.64.0.0/10', '192.168.0.0/16'])

    def test_empty(self):
        self.assertEqual(app._parse_trusted_networks(''), [])


if __name__ == '__main__':
    unittest.main()
