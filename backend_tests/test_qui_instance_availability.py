"""A qui instance qui cannot reach is "could not ask", never "not there".

CODE_REVIEW_2026-09-27, the qui half of R1 and S05 that the plan's mocks never
reached: no test used `'connected': False`, and none posted both registrations
of one hash in one call.

* **CR1 (CLEANUP C23)** —`_qui._eligible` filtered a disconnected instance out
  with the configuration cases, so it was recorded nowhere: `instances_failed`
  stayed empty, `instances_unavailable` could not fire, and every torrent it
  held read as gone. Its payload became orphans, which Cleanup's live
  re-verify (the same blind spot) let into a delete script.
* **CR1a** — `fetch_torrent_details` answered `{'found': False}` for a
  registration on that instance, because the *other* instances answering
  counted as proof. Triage dropped the row as "no longer in your client".
* **CR1b** — `remove_torrents` looked a registration naming that instance up by
  hash on the others and removed it from an instance nobody asked about.
* **CR2 (TRUMPED TR22)** — `remove_torrents` keyed its request by hash, so the
  same release on two instances was removed from the first alone, and the
  second stayed registered on files that had just been deleted.

A disabled instance (`isActive: false`, checked against `autobrr/qui`'s
`InstanceCard`, which shows disabled before it looks at `connected`) and one
without local filesystem access are configuration, and stay left out.
"""

from unittest.mock import patch

import pytest

import app
import audit
import sources
from sources import _qui
from backend_tests.test_registration_identity import (
    CFG, H, NAME, OTHER, SP1, SP2, _Qui, torrent, route)


MAIN   = {'id': 1, 'name': 'main',   'connected': True, 'hasLocalFilesystemAccess': True,
          'isActive': True}
DOWN   = {'id': 2, 'name': 'second', 'connected': False, 'hasLocalFilesystemAccess': True,
          'isActive': True}
OFF    = {**DOWN, 'isActive': False}                       # disabled in qui
NO_FS  = {**DOWN, 'hasLocalFilesystemAccess': False}       # auditorr never read it
OLD    = {k: v for k, v in DOWN.items() if k != 'isActive'}  # a qui before the toggle


@pytest.fixture(autouse=True)
def _fresh_listing_cache():
    _qui._forget_detail_listings()
    yield
    _qui._forget_detail_listings()


def _client(second):
    """Instance 1 answers; instance 2 is `second`. Each holds its own torrent
    (and instance 2 the shared one too), so a function that asks the wrong
    instance gets the wrong answer."""
    return _Qui({1: [torrent(h=H, save_path=SP1), torrent(h=OTHER, name='Other.Film')],
                 2: [torrent(h=H, save_path=SP2)]},
                files={(1, H): [f'{NAME}/f.mkv'], (1, OTHER): ['Other.Film/o.mkv'],
                       (2, H): [f'{NAME}/f.mkv']},
                instances=[MAIN, second])


def _call(sess, fn, *args, **kw):
    with patch.object(_qui, '_session', return_value=sess):
        return fn(CFG, *args, **kw)


# ── CR1: a disconnected instance is a failed instance ────────────────────────

@pytest.mark.parametrize('second', [DOWN, OLD], ids=['active', 'no isActive field'])
def test_a_scan_with_a_disconnected_instance_refuses(second):
    sess = _client(second)
    _fmap, _t, _s, report = _call(sess, _qui.fetch_file_map)

    assert report['instances_total'] == 2
    assert [f['name'] for f in report['instances_failed']] == ['second']
    assert report['partial'] is True
    anomaly = audit.source_plausibility(report, None)
    assert anomaly['code'] == 'instances_unavailable'
    assert anomaly['code'] not in audit._ACCEPTABLE_BY_HAND, "a manual scan could accept it"
    assert 'disable it in qui' in report['instances_failed'][0]['reason']
    assert (2, 'torrents') not in sess.asked


def test_the_live_listing_reports_it_and_the_wrapper_refuses():
    rows, report = _call(_client(DOWN), _qui.list_torrents)
    assert [f['name'] for f in report['instances_failed']] == ['second']
    assert {r['instance_id'] for r in rows} == {1}

    with patch.object(_qui, '_session', return_value=_client(DOWN)):
        with pytest.raises(sources.SourceConnectionError):
            sources.list_torrents(CFG)


@pytest.mark.parametrize('second', [OFF, NO_FS], ids=['disabled', 'no filesystem access'])
def test_configuration_still_leaves_an_instance_out(second):
    """Turning an abandoned instance off in qui is how a user gets their scans
    back, so it must work."""
    _fmap, _t, _s, report = _call(_client(second), _qui.fetch_file_map)
    assert report['instances_total'] == 1
    assert report.get('instances_failed', []) == []
    assert audit.source_plausibility(report, None) is None

    rows, live = _call(_client(second), _qui.list_torrents)
    assert live.get('instances_failed', []) == []


def test_the_refusal_says_how_to_leave_an_abandoned_instance_out():
    assert 'disabled in qui' in audit._ANOMALY_FIXES['instances_unavailable']


# ── CR1a: the details lookup ─────────────────────────────────────────────────

def test_a_registration_on_a_disconnected_instance_is_not_called_gone():
    sess = _client(DOWN)
    details = _call(sess, _qui.fetch_torrent_details, [{'hash': H, 'instance_id': 2}])
    assert sources.registration_key(2, H) not in details, \
        "another instance answering was taken as proof this one does not hold it"
    assert (2, 'torrents') not in sess.asked


def test_an_unnamed_hash_is_not_called_gone_while_an_instance_is_down():
    missing = 'c' * 40
    details = _call(_client(DOWN), _qui.fetch_torrent_details,
                    [{'hash': missing, 'instance_id': None}])
    assert missing not in details


def test_with_every_instance_answering_gone_is_still_gone():
    """The T10 rule this narrows, unchanged where nothing is out of reach."""
    missing = 'c' * 40
    details = _call(_client(MAIN), _qui.fetch_torrent_details,
                    [{'hash': missing, 'instance_id': 1}])
    assert details[sources.registration_key(1, missing)] == {'found': False}


# ── CR1: the file-path lookup ────────────────────────────────────────────────

def test_a_named_instance_out_of_reach_has_no_listing_rather_than_another_ones():
    sess = _client(DOWN)
    paths = _call(sess, _qui.fetch_torrent_file_paths,
                  [{'hash': H, 'instance_id': 2, 'save_path': SP2}])
    assert paths[sources.registration_key(2, H)] is None
    assert (1, 'files') not in sess.asked, "instance 1's listing answered for instance 2"


def test_an_unnamed_hash_has_no_listing_while_an_instance_is_down():
    sess = _client(DOWN)
    paths = _call(sess, _qui.fetch_torrent_file_paths, [{'hash': OTHER, 'save_path': SP1}])
    assert paths[OTHER] is None


# ── CR1b: removal ────────────────────────────────────────────────────────────

def test_a_removal_naming_a_disconnected_instance_removes_nothing_elsewhere():
    sess = _client(DOWN)
    submitted = _call(sess, _qui.remove_torrents, [{'hash': H, 'instance_id': 2}],
                      delete_files=False)
    assert sess.posts == [], "instance 1's registration was removed when instance 2 was asked for"
    assert submitted == 0


def test_an_unnamed_removal_waits_for_every_instance():
    sess = _client(DOWN)
    _call(sess, _qui.remove_torrents, [{'hash': OTHER, 'instance_id': None}], delete_files=True)
    assert sess.posts == [], "removed on the strength of the instances that could be asked"


def test_an_unnamed_removal_is_skipped_when_a_listing_fails():
    """The same rule `_instances_holding` follows: an instance that failed to
    list may hold the hash too. This used to `continue` past it."""
    sess = _client({**DOWN, 'connected': True})
    real_get = sess.get

    def _get(url, params=None, **kw):
        if '/api/instances/2/torrents' in url and url.endswith('/torrents'):
            raise RuntimeError('timed out')
        return real_get(url, params=params, **kw)

    sess.get = _get
    _call(sess, _qui.remove_torrents, [{'hash': OTHER, 'instance_id': None}], delete_files=True)
    assert sess.posts == []


def test_the_triage_route_reports_it_unknown_and_removes_nothing():
    sess = _client(DOWN)
    resp = route(sess, '/api/workflows/remove_torrents',
                 {'items': [{'hash': H, 'instance_id': 2}], 'delete_files': False})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['outcomes'][sources.registration_key(2, H)] == 'unknown'
    assert sess.posts == []


def test_a_checked_removal_refuses_while_an_instance_is_down():
    """Its torrents are invisible to the ownership check, not merely uncounted."""
    sess = _client(DOWN)
    resp = route(sess, '/api/workflows/remove_torrents',
                 {'items': [{'hash': OTHER, 'instance_id': 1}], 'delete_files': 'auto'})
    assert resp.status_code == 502
    assert sess.posts == []


# ── CR2: both registrations of one hash ──────────────────────────────────────

def test_removing_both_registrations_posts_to_both_instances():
    sess = _Qui({1: [torrent(save_path=SP1)], 2: [torrent(save_path=SP1)]},
                files={(1, H): [f'{NAME}/f.mkv'], (2, H): [f'{NAME}/f.mkv']})
    submitted = _call(sess, _qui.remove_torrents,
                      [{'hash': H, 'instance_id': 1}, {'hash': H, 'instance_id': 2}],
                      delete_files=True)
    assert sorted(sess.posts) == [(1, [H], 'deleteWithFiles'), (2, [H], 'deleteWithFiles')]
    assert submitted == 2


def test_a_registration_posted_twice_is_removed_once():
    sess = _Qui({1: [torrent(save_path=SP1)]}, files={(1, H): [f'{NAME}/f.mkv']})
    _call(sess, _qui.remove_torrents,
          [{'hash': H, 'instance_id': 1}, {'hash': H, 'instance_id': 1},
           {'hash': H, 'instance_id': None}], delete_files=False)
    assert sess.posts == [(1, [H], 'delete')]


def test_triage_all_cross_seeds_leaves_neither_registration_behind():
    """Through the route the page uses: both registrations share one payload
    and both are in the removal, so the ownership check lets both delete."""
    sess = _Qui({1: [torrent(save_path=SP1)], 2: [torrent(save_path=SP1)]},
                files={(1, H): [f'{NAME}/f.mkv'], (2, H): [f'{NAME}/f.mkv']})
    resp = route(sess, '/api/workflows/remove_torrents',
                 {'items': [{'hash': H, 'instance_id': 1}, {'hash': H, 'instance_id': 2}],
                  'delete_files': 'auto'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body['outcomes'].values()) == {'removed'}
    assert sorted(i for i, _h, _a in sess.posts) == [1, 2]
