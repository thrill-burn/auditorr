"""External (reverse-proxy) link addresses.

The feature's whole promise is that it changes nothing for anyone who doesn't
use it, so the first class here pins the unchanged behaviour rather than the new
behaviour — those are the tests that fail if a refactor quietly starts routing
existing users' links somewhere new.
"""

import unittest

from arr import link_base, normalize_arr_connections
from db import DEFAULT_CONFIG, url_problem, validate_config


LEGACY_CFG = {
    "SONARR_URL": "http://sonarr.local:8989",
    "SONARR_API_KEY": "sonarr-key",
    "RADARR_URL": "http://radarr.local:7878",
    "RADARR_API_KEY": "radarr-key",
}


class TransparencyTests(unittest.TestCase):
    """Nothing configured => byte-identical to the pre-feature behaviour."""

    def test_blank_external_url_falls_back_to_api_address(self):
        for conn in normalize_arr_connections(LEGACY_CFG):
            self.assertEqual(link_base(conn), conn["base_url"])

    def test_config_absent_the_new_keys_is_unaffected(self):
        # A stored config row written before the upgrade has none of the new
        # keys at all. db_load_config merges DEFAULT_CONFIG under it, so they
        # arrive as '' — the fallback path, not a crash and not a None.
        cfg = {**DEFAULT_CONFIG, **LEGACY_CFG}
        conns = normalize_arr_connections(cfg)
        self.assertEqual([c["external_url"] for c in conns], ["", ""])
        self.assertEqual(
            [link_base(c) for c in conns],
            ["http://sonarr.local:8989", "http://radarr.local:7878"],
        )

    def test_new_keys_default_to_blank(self):
        for key in ("QB_EXTERNAL_URL", "QUI_EXTERNAL_URL",
                    "SONARR_EXTERNAL_URL", "RADARR_EXTERNAL_URL"):
            self.assertEqual(DEFAULT_CONFIG[key], "")

    def test_existing_schemeless_url_still_validates(self):
        # A hard error here would lock an install that has held this value for
        # months out of every unrelated setting on the page. It's a warning,
        # raised by the caller in app.py — never an error.
        self.assertEqual(validate_config({"SONARR_URL": "192.168.1.5:8989"}), [])


class LinkBaseTests(unittest.TestCase):
    def test_external_url_wins_when_set(self):
        cfg = {**LEGACY_CFG, "SONARR_EXTERNAL_URL": "https://sonarr.example.com"}
        by_id = {c["id"]: c for c in normalize_arr_connections(cfg)}
        self.assertEqual(link_base(by_id["sonarr-default"]), "https://sonarr.example.com")
        # The one not given an external URL is untouched.
        self.assertEqual(link_base(by_id["radarr-default"]), "http://radarr.local:7878")

    def test_api_address_is_never_replaced(self):
        cfg = {**LEGACY_CFG, "SONARR_EXTERNAL_URL": "https://sonarr.example.com"}
        conn = normalize_arr_connections(cfg, service="sonarr")[0]
        self.assertEqual(conn["base_url"], "http://sonarr.local:8989")

    def test_trailing_slash_is_normalized(self):
        cfg = {**LEGACY_CFG, "SONARR_EXTERNAL_URL": "https://media.example.com/sonarr/"}
        conn = normalize_arr_connections(cfg, service="sonarr")[0]
        self.assertEqual(link_base(conn) + "/series/x", "https://media.example.com/sonarr/series/x")

    def test_per_connection_external_url(self):
        cfg = {
            "ARR_CONNECTIONS": [
                {
                    "id": "radarr-4k",
                    "service": "radarr",
                    "base_url": "http://radarr-4k.local:7878",
                    "external_url": "https://radarr-4k.example.com/",
                    "api_key": "k",
                },
            ],
        }
        conn = normalize_arr_connections(cfg)[0]
        self.assertEqual(conn["base_url"], "http://radarr-4k.local:7878")
        self.assertEqual(link_base(conn), "https://radarr-4k.example.com")

    def test_external_url_has_no_key_alias(self):
        # base_url accepts a 'url' alias, which is why validate_config has to
        # check both names. A second alias here would let the validator check
        # one key while the normalizer reads another.
        cfg = {
            "ARR_CONNECTIONS": [{
                "id": "r", "service": "radarr", "base_url": "http://r.local:7878",
                "externalUrl": "https://evil.example.com", "api_key": "k",
            }],
        }
        self.assertEqual(link_base(normalize_arr_connections(cfg)[0]), "http://r.local:7878")

    def test_missing_connection_is_not_a_crash(self):
        self.assertEqual(link_base(None), "")
        self.assertEqual(link_base({}), "")


class UrlValidationTests(unittest.TestCase):
    def test_blank_is_allowed(self):
        self.assertIsNone(url_problem("X", ""))
        self.assertIsNone(url_problem("X", None))

    def test_http_and_https_pass(self):
        self.assertIsNone(url_problem("X", "http://a.local:8989"))
        self.assertIsNone(url_problem("X", "HTTPS://a.example.com"))

    def test_script_scheme_is_rejected(self):
        self.assertIsNotNone(url_problem("X", "javascript:alert(1)"))
        self.assertIsNotNone(url_problem("X", "data:text/html,x"))

    def test_missing_scheme_is_rejected(self):
        # Renders as a *relative* link into auditorr, so it fails silently and
        # looks like the feature is broken rather than the value.
        self.assertIsNotNone(url_problem("X", "media.example.com"))

    def test_overlong_is_rejected(self):
        self.assertIsNotNone(url_problem("X", "https://" + "a" * 400))

    def test_validate_config_errors_on_bad_external_url(self):
        errors = validate_config({"SONARR_EXTERNAL_URL": "javascript:alert(1)"})
        self.assertTrue(any("Sonarr External URL" in e for e in errors))

    def test_validate_config_errors_on_bad_connection_external_url(self):
        # _merge_arr_connection_secrets copies every incoming key verbatim, so
        # a field with no validator is stored entirely unchecked.
        errors = validate_config({
            "ARR_CONNECTIONS": [{
                "id": "r", "service": "radarr", "base_url": "http://r.local:7878",
                "external_url": "javascript:alert(1)",
            }],
        })
        self.assertTrue(any("external_url" in e for e in errors))

    def test_validate_config_accepts_good_external_urls(self):
        self.assertEqual(validate_config({
            "SONARR_EXTERNAL_URL": "https://sonarr.example.com",
            "QUI_EXTERNAL_URL": "https://qui.example.com",
        }), [])


if __name__ == "__main__":
    unittest.main()
