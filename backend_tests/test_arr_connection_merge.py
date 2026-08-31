"""The primary Sonarr/Radarr fields and the "Additional instances" list are one
set of connections, not two competing ones (#22).

ARR_CONNECTIONS used to sit in an `else` against the legacy singletons, so
adding one extra instance silently retired the primary Sonarr and Radarr. Their
fields stayed on the Config page, kept saving, and kept passing their own test
button — nothing read them. A reporter with 4 instances had 2 of them invisible
to every arr-backed feature, which surfaced as a single Backfill root folder.
"""
import pytest

from arr import normalize_arr_connections


def _legacy():
    return {
        'SONARR_URL': 'http://sonarr:8989', 'SONARR_API_KEY': 'sk',
        'RADARR_URL': 'http://radarr:7878', 'RADARR_API_KEY': 'rk',
    }


def _uhd_pair():
    return [
        {'id': 'radarr-uhd', 'service': 'radarr', 'name': 'Radarr-UHD',
         'base_url': 'http://radarr-uhd:7878', 'api_key': 'a'},
        {'id': 'sonarr-uhd', 'service': 'sonarr', 'name': 'Sonarr-UHD',
         'base_url': 'http://sonarr-uhd:8989', 'api_key': 'b'},
    ]


def test_additional_instances_add_to_the_primary_fields():
    conns = normalize_arr_connections({**_legacy(), 'ARR_CONNECTIONS': _uhd_pair()})
    assert [c['id'] for c in conns] == [
        'sonarr-default', 'radarr-default', 'radarr-uhd', 'sonarr-uhd']


def test_primary_comes_first_so_the_link_fallback_is_the_main_instance():
    conns = normalize_arr_connections({**_legacy(), 'ARR_CONNECTIONS': _uhd_pair()})
    assert conns[0]['base_url'] == 'http://sonarr:8989'


def test_service_filter_spans_both_sources():
    conns = normalize_arr_connections(
        {**_legacy(), 'ARR_CONNECTIONS': _uhd_pair()}, service='radarr')
    assert [c['id'] for c in conns] == ['radarr-default', 'radarr-uhd']


def test_an_instance_listed_in_both_places_is_indexed_once():
    """The migration case — copying the primary into the list must not double it."""
    cfg = {**_legacy(), 'ARR_CONNECTIONS': [
        {'id': 'radarr-main', 'service': 'radarr', 'name': 'Radarr',
         'base_url': 'http://radarr:7878/', 'api_key': 'rk'},
    ]}
    conns = normalize_arr_connections(cfg)
    assert [c['id'] for c in conns] == ['sonarr-default', 'radarr-main']


def test_same_host_on_a_different_scheme_is_still_one_instance():
    cfg = {**_legacy(), 'ARR_CONNECTIONS': [
        {'id': 'radarr-main', 'service': 'radarr',
         'base_url': 'HTTPS://Radarr:7878', 'api_key': 'rk'},
    ]}
    assert [c['id'] for c in normalize_arr_connections(cfg)] == [
        'sonarr-default', 'radarr-main']


def test_same_host_under_a_different_service_is_kept():
    """One reverse proxy fronting both arrs on one host:port is two instances."""
    cfg = {'SONARR_URL': 'http://arrs.local', 'SONARR_API_KEY': 'sk',
           'ARR_CONNECTIONS': [
               {'id': 'radarr-main', 'service': 'radarr',
                'base_url': 'http://arrs.local', 'api_key': 'rk'}]}
    assert [c['id'] for c in normalize_arr_connections(cfg)] == [
        'sonarr-default', 'radarr-main']


def test_an_explicit_entry_may_claim_the_default_id():
    cfg = {**_legacy(), 'ARR_CONNECTIONS': [
        {'id': 'radarr-default', 'service': 'radarr', 'name': 'Renamed',
         'base_url': 'http://elsewhere:7878', 'api_key': 'x'},
    ]}
    conns = normalize_arr_connections(cfg)
    assert [c['id'] for c in conns] == ['sonarr-default', 'radarr-default']
    assert conns[1]['name'] == 'Renamed'


def test_duplicate_ids_within_the_list_are_still_refused():
    cfg = {'ARR_CONNECTIONS': [
        {'id': 'dup', 'service': 'radarr', 'base_url': 'http://a:7878', 'api_key': 'x'},
        {'id': 'dup', 'service': 'radarr', 'base_url': 'http://b:7878', 'api_key': 'y'},
    ]}
    with pytest.raises(ValueError):
        normalize_arr_connections(cfg)


def test_a_primary_with_no_api_key_is_not_a_connection():
    cfg = {'SONARR_URL': 'http://sonarr:8989', 'ARR_CONNECTIONS': _uhd_pair()}
    assert [c['id'] for c in normalize_arr_connections(cfg)] == [
        'radarr-uhd', 'sonarr-uhd']


def test_primary_only_and_list_only_installs_are_unchanged():
    assert [c['id'] for c in normalize_arr_connections(_legacy())] == [
        'sonarr-default', 'radarr-default']
    assert [c['id'] for c in normalize_arr_connections({'ARR_CONNECTIONS': _uhd_pair()})] == [
        'radarr-uhd', 'sonarr-uhd']
