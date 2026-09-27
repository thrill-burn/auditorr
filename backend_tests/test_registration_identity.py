"""A registration is a registration (S05).

From the 2026-09-10 outside review: torrent identity collapsed to the infohash
across qui instances. The same torrent can legitimately be registered on two
instances, at two save paths, with independent removal intent — and the Phase 13
brief's probe, against `132849c`, found:

* `list_torrents` answered **1 row** for a hash on two instances, with
  `instances_ok 2`, `torrent_count 1` and `partial False` — nothing said a
  registration had been dropped;
* `fetch_file_map` counted that client's torrents as **2**, so the two listings
  disagreed about the number the plausibility guard reads;
* upload and seeding bytes were counted once however many registrations stood
  on a hash, and once however many save paths they sat at;
* `fetch_torrent_file_paths` with no instance id asked instance 1 and joined its
  file listing to the caller's save path — instance 2's;
* a removal that succeeded read `still_listed`, because the re-listing asked
  whether the *hash* was still there.

**The premise the old de-duplication rested on does not hold**: checked against
qui's source (`autobrr/qui`, `main`, 2026-09-15), the per-instance `ListTorrents`
handler is instance-scoped. The duplicate-*page* case it was for is handled
inside `_fetch_all_torrents`, per instance, and is pinned here.

**The mock answers only what it is asked.** `_Qui` is driven by the instance id
in each URL it receives — a listing, a file list, a tracker list or a
bulk action — so a function that asks the wrong instance gets the wrong
instance's answer, which is exactly the shape of the finding.

**qbit has no instances**, so every shape here is a no-op there: its rows carry
`instance_id: None`, `registration_key` gives the bare hash back, and its
answers are unchanged (`test_qbit_has_no_instances_and_nothing_changes`).

Two kinds of test, kept apart as Phases 6–12 did:

* **Characterisation** — a single-instance qui install's `file_map`, source
  report and live listing are exactly what they were; the duplicate-page case
  is still one torrent; bytes on one save path are still counted once.
* **Findings** — each written to fail on the pre-Phase-13 code for its own
  reason.
"""

import os
from unittest.mock import patch

import pytest

import app
import audit
import sources
from sources import _qbit, _qui


CFG = {
    'TORRENT_SOURCE': 'qui', 'QUI_HOST': 'http://qui:7476', 'QUI_API_KEY': 'k',
    'REMOTE_PATH': '/data/torrents', 'LOCAL_PATH': '/data/torrents',
    'ALLOW_CLIENT_DELETE': True,
}
H = 'a' * 40                      # the doubly registered torrent
OTHER = 'b' * 40
SP1, SP2 = '/data/torrents/movies', '/data/torrents/cross'
NAME = 'Some.Film.2021.1080p.WEB-DL-GRP'

INSTANCES = [{'id': 1, 'name': 'main',   'connected': True, 'hasLocalFilesystemAccess': True},
             {'id': 2, 'name': 'second', 'connected': True, 'hasLocalFilesystemAccess': True}]


def torrent(h=H, save_path=SP1, size=1000, uploaded=50, state='uploading', name=NAME,
            tracker='https://t.example/announce', seeding_time=0):
    return {'hash': h, 'name': name, 'save_path': save_path, 'size': size,
            'uploaded': uploaded, 'state': state, 'progress': 1.0,
            'completion_on': 1_789_000_000, 'tracker': tracker, 'content_path': '',
            'seeding_time': seeding_time}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    @property
    def ok(self):
        return self.status_code < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        return self._payload


class _Qui:
    """qui, answering each request from the instance its URL names."""
    headers = {}

    def __init__(self, torrents, files=None, instances=None):
        # torrents: {instance id: [torrent]}; files: {(instance id, hash): [names]}
        self.torrents = {k: [dict(t) for t in v] for k, v in torrents.items()}
        self.files = files or {}
        self.instances = instances or [i for i in INSTANCES if i['id'] in torrents]
        self.asked = []           # (instance id, endpoint) in call order
        self.posts = []           # (instance id, [hashes], action)

    @staticmethod
    def _instance(url):
        return int(url.split('/api/instances/')[1].split('/')[0])

    def get(self, url, params=None, **_kw):
        if url.endswith('/api/instances'):
            return _Resp(self.instances)
        inst = self._instance(url)
        if url.endswith('/torrents'):
            self.asked.append((inst, 'torrents'))
            rows = self.torrents.get(inst, [])
            page = (params or {}).get('page', 0)
            return _Resp({'torrents': rows if page == 0 else [], 'total': len(rows)})
        h = url.split('/torrents/')[1].split('/')[0]
        if not any(t['hash'] == h for t in self.torrents.get(inst, [])):
            return _Resp({'error': 'not found'}, status=404)
        if url.endswith('/files'):
            self.asked.append((inst, 'files'))
            return _Resp([{'name': n, 'size': 1} for n in self.files.get((inst, h), [])])
        if url.endswith('/trackers'):
            self.asked.append((inst, 'trackers'))
            return _Resp([{'url': 'https://t.example/announce', 'status': 2, 'msg': ''}])
        raise AssertionError(f'unexpected GET {url}')

    def post(self, url, json=None, **_kw):
        inst = self._instance(url)
        hashes = list((json or {}).get('hashes') or [])
        self.posts.append((inst, sorted(hashes), (json or {}).get('action')))
        self.torrents[inst] = [t for t in self.torrents.get(inst, []) if t['hash'] not in hashes]
        return _Resp({})


@pytest.fixture(autouse=True)
def _fresh_listing_cache():
    _qui._forget_detail_listings()
    yield
    _qui._forget_detail_listings()


def two_paths():
    """One hash on two instances at two save paths — two payloads on disk."""
    return _Qui({1: [torrent(save_path=SP1, uploaded=50)],
                 2: [torrent(save_path=SP2, uploaded=70)]},
                files={(1, H): [f'{NAME}/f.mkv'], (2, H): [f'{NAME}/f.mkv', f'{NAME}/f.nfo']})


def one_path():
    """One hash on two instances sharing one save path — one payload on disk."""
    return _Qui({1: [torrent(save_path=SP1, uploaded=50)],
                 2: [torrent(save_path=SP1, uploaded=70)]},
                files={(1, H): [f'{NAME}/f.mkv'], (2, H): [f'{NAME}/f.mkv']})


def listing(sess):
    with patch.object(_qui, '_session', return_value=sess):
        return _qui.list_torrents(CFG)


def file_map(sess):
    with patch.object(_qui, '_session', return_value=sess):
        return _qui.fetch_file_map(CFG)


def route(sess, path, body):
    with patch.object(_qui, '_session', return_value=sess), \
         patch.object(app, 'db_load_config', return_value=dict(CFG)), \
         patch.object(app, 'nudge_watchdog'), \
         patch.object(app.time, 'sleep'):
        return app.app.test_client().post(path, json=body)


# ═════════════════════════════════════════════════════════════════════════════
# Characterisation — true before Phase 13 and after it
# ═════════════════════════════════════════════════════════════════════════════

def test_a_single_instance_install_answers_exactly_what_it_answered():
    """The box runs one qui instance, and nothing about it may move (T2's
    precedent). Every value here was read off `132849c` before Phase 13; a new
    field (`reg`, the two new report counters) is an addition and is stripped
    before comparing, never a change to what was already there."""
    sess = _Qui({1: [torrent(h=H, save_path=SP1, size=1000, uploaded=50, seeding_time=100),
                     torrent(h=OTHER, save_path=SP1, size=300, uploaded=9, state='pausedUP',
                             name='Other.Film', seeding_time=40)]},
                files={(1, H): [f'{NAME}/f.mkv'], (1, OTHER): ['Other.Film/o.mkv']})

    rows, report = listing(sess)
    fmap, trackers, snapshot, scan = file_map(sess)

    assert [{k: v for k, v in r.items() if k != 'reg'} for r in rows] == [
        {'hash': H, 'name': NAME, 'size': 1000, 'save_path': SP1, 'content_path': '',
         'progress': 1.0, 'completion_on': 1_789_000_000, 'tracker': 't.example',
         'instance_id': 1, 'instance_name': 'main'},
        {'hash': OTHER, 'name': 'Other.Film', 'size': 300, 'save_path': SP1, 'content_path': '',
         'progress': 1.0, 'completion_on': 1_789_000_000, 'tracker': 't.example',
         'instance_id': 1, 'instance_name': 'main'},
    ]
    assert (report['torrent_count'], report['instances_ok'], report['partial']) == (2, 1, False)

    assert sorted(fmap) == sorted([os.path.join(SP1, f'{NAME}/f.mkv'),
                                   os.path.join(SP1, 'Other.Film/o.mkv')])
    assert {p: (e['hash'], e['instance_id'], e['status']) for p, e in fmap.items()} == {
        os.path.join(SP1, f'{NAME}/f.mkv'): (H, 1, 'Seeding'),
        os.path.join(SP1, 'Other.Film/o.mkv'): (OTHER, 1, 'Paused'),
    }
    assert trackers == ['t.example']
    assert snapshot == {'t.example': {'uploaded': 59, 'seeding_size': 1000},
                        '_instance_count': 1,
                        '_seed_byte_secs': 1000 * 100 + 300 * 40, '_max_seed_secs': 100}
    assert {k: scan[k] for k in ('torrent_count', 'file_map_size', 'listing_failures',
                                 'incomplete_torrents', 'completion_unknown',
                                 'instances_total', 'instances_ok', 'partial')} == {
        'torrent_count': 2, 'file_map_size': 2, 'listing_failures': 0,
        'incomplete_torrents': 0, 'completion_unknown': 0,
        'instances_total': 1, 'instances_ok': 1, 'partial': False}


def test_duplicate_pagination_rows_are_still_one_torrent():
    """The case the old de-duplication was really for: an API repeating a row
    inside one instance's listing. `_fetch_all_torrents` de-duplicates per
    instance, which is where that belongs."""
    sess = _Qui({1: [torrent(h=H), torrent(h=H), torrent(h=OTHER, name='Other.Film')]},
                files={(1, H): [f'{NAME}/f.mkv'], (1, OTHER): ['Other.Film/o.mkv']})
    # The listing advertises the two torrents it really holds.
    real_get = sess.get

    def _get(url, params=None, **kw):
        resp = real_get(url, params=params, **kw)
        if url.endswith('/torrents'):
            resp._payload['total'] = 2
        return resp

    sess.get = _get
    rows, report = listing(sess)
    _fmap, _t, snapshot, scan = file_map(sess)

    assert sorted(r['hash'] for r in rows) == sorted([H, OTHER])
    assert report['torrent_count'] == 2
    assert scan['torrent_count'] == 2
    assert snapshot['t.example']['uploaded'] == 100


def test_the_same_hash_on_one_save_path_counts_its_bytes_once():
    """Shared storage: two registrations on one save path are **one** set of
    bytes on disk — `seeding_size`, Atlas's byte-seconds and `file_map_size`
    de-duplicate by path. (Passes on the old code, which de-duplicated
    everything by hash; kept because the new rule must not start counting the
    shared payload twice.)"""
    _fmap, _t, snapshot, scan = file_map(one_path())

    assert snapshot['t.example']['seeding_size'] == 1000
    assert scan['file_map_size'] == 1


# ═════════════════════════════════════════════════════════════════════════════
# S05 — the findings
# ═════════════════════════════════════════════════════════════════════════════

def test_two_registrations_of_one_hash_are_two_rows():
    """The brief's probe: one row, with nothing saying a registration was
    dropped. Two instances hold it at two save paths, so they are two
    registrations with two sets of bytes and two removal intents."""
    rows, _report = listing(two_paths())

    assert sorted((r['instance_id'], r['save_path']) for r in rows) == [(1, SP1), (2, SP2)]
    assert {r['reg'] for r in rows} == {sources.registration_key(1, H),
                                        sources.registration_key(2, H)}


def test_a_dropped_registration_is_reported_not_silent():
    """Decision 3 (a): a row per registration, and the report says how much of
    that is one torrent seen twice — `instances_ok 2, torrent_count 1, partial
    False` used to be the whole answer."""
    _rows, report = listing(two_paths())

    assert report.get('multi_registered') == 1
    assert report.get('distinct_torrents') == 1
    assert report['torrent_count'] == 2


def test_torrent_count_means_the_same_thing_in_both_listings():
    """The guard reads `fetch_file_map`'s count; Trumped and Cleanup read
    `list_torrents`'. They counted the same client as 2 and 1."""
    _rows, live = listing(two_paths())
    _fmap, _t, _s, scan = file_map(two_paths())

    assert live['torrent_count'] == scan['torrent_count'] == 2
    assert scan.get('distinct_torrents') == live.get('distinct_torrents') == 1
    assert scan.get('multi_registered') == live.get('multi_registered') == 1


def test_uploaded_bytes_accrue_per_registration():
    """Two clients really did upload separately. Counted once by hash, the
    library's whole purpose was under-reported."""
    _fmap, _t, snapshot, _scan = file_map(two_paths())

    assert snapshot['t.example']['uploaded'] == 50 + 70


def test_two_save_paths_count_their_bytes_twice():
    """The other half of the byte rule: two registrations at two save paths are
    two payloads on disk. One shared `seen_hashes` used to count them once."""
    _fmap, _t, snapshot, scan = file_map(two_paths())

    assert snapshot['t.example']['seeding_size'] == 2000
    assert scan['file_map_size'] == 3


def test_file_paths_come_from_the_instance_that_holds_the_torrent():
    """With no instance named, the lookup asked instance 1 and joined **its**
    file listing to the caller's save path — instance 2's — which is a set of
    paths neither client holds. Named, each registration answers for itself;
    unnamed and doubly registered, it is `None`: could not ask unambiguously."""
    sess = two_paths()
    with patch.object(_qui, '_session', return_value=sess):
        unnamed = _qui.fetch_torrent_file_paths(CFG, [{'hash': H, 'save_path': SP2}])
    assert unnamed.get(H) is None, "a guess was joined to another instance's save path"

    with patch.object(_qui, '_session', return_value=two_paths()):
        named = _qui.fetch_torrent_file_paths(CFG, [
            {'hash': H, 'instance_id': 1, 'save_path': SP1},
            {'hash': H, 'instance_id': 2, 'save_path': SP2}])
    assert len(named) == 2, "two registrations collapsed into one answer"
    assert named[sources.registration_key(1, H)] == [f'{SP1}/{NAME}/f.mkv']
    assert named[sources.registration_key(2, H)] == [f'{SP2}/{NAME}/f.mkv', f'{SP2}/{NAME}/f.nfo']


def test_removing_one_registration_is_verified_against_that_instance():
    """S05 / S09. The removal check asked whether the *hash* was still listed,
    so removing instance 1's registration read `still_listed` — instance 2 still
    holds the hash — and the page marked a correct removal "unconfirmed"."""
    sess = two_paths()
    resp = route(sess, '/api/workflows/remove_torrents',
                 {'items': [{'hash': H, 'instance_id': 1}], 'delete_files': False})

    assert resp.status_code == 200
    body = resp.get_json()
    assert 'still_listed' not in body['outcomes'].values(), \
        "a removal that worked was reported as unconfirmed"
    assert body['outcomes'][sources.registration_key(1, H)] == 'removed'
    assert [(i, h) for i, h, _ in sess.posts] == [(1, [H])]


def test_a_removal_that_names_no_instance_refuses_rather_than_guessing():
    """An item with no instance, for a hash two instances hold, used to remove
    it from whichever instance the client listed first."""
    sess = two_paths()
    resp = route(sess, '/api/workflows/remove_torrents',
                 {'items': [{'hash': H, 'instance_id': None}], 'delete_files': False})

    assert resp.status_code == 409
    body = resp.get_json()
    assert body['code'] == 'registration_ambiguous'
    assert sorted(body['ambiguous'][0]['instances']) == ['main', 'second']
    assert sess.posts == [], "something was removed on a guess"


def test_the_backend_never_guesses_an_instance_for_a_removal():
    """The same rule one layer down, for any caller of `remove_torrents`."""
    sess = two_paths()
    with patch.object(_qui, '_session', return_value=sess):
        _qui.remove_torrents(CFG, [{'hash': H, 'instance_id': None}], delete_files=True)

    assert sess.posts == []


def test_removing_one_of_two_registrations_on_one_path_keeps_the_files():
    """The deletion shape. Two instances share one payload; removing instance
    1's registration *with its files* used to delete bytes instance 2 is still
    seeding, because the live listing had dropped instance 2 and nothing was
    left to hold the path."""
    sess = one_path()
    resp = route(sess, '/api/workflows/remove_torrents',
                 {'items': [{'hash': H, 'instance_id': 1}], 'delete_files': 'auto'})

    assert resp.status_code == 200
    body = resp.get_json()
    (only,) = body['torrents']
    assert (only['files'], only['reason']) == ('keep', 'shared'), \
        "files a second registration still holds were deleted"
    assert sess.posts == [(1, [H], 'delete')]


def test_a_trump_group_names_every_registration_or_refuses():
    """Decision 3 (a). A bare hash two instances hold refuses (409) rather than
    resolving against whichever the client listed first; posted as
    registrations, the group holds both — they share the payload."""
    ambiguous = route(one_path(), '/api/workflows/trump/resolve_group',
                      {'old_titles': [NAME], 'seed_hashes': [H]})
    assert ambiguous.status_code == 409, "a trump group resolved against one guessed instance"
    assert ambiguous.get_json()['code'] == 'registration_ambiguous'

    named = route(one_path(), '/api/workflows/trump/resolve_group',
                  {'old_titles': [NAME], 'seed_hashes': [sources.registration_key(1, H)]})
    assert named.status_code == 200
    members = named.get_json()['torrents']
    assert sorted((m['instance_id'], m['hash']) for m in members) == [(1, H), (2, H)]


def test_qbit_has_no_instances_and_nothing_changes():
    """Every S05 shape is a no-op on qbit: one client, `instance_id: None`, and
    a registration key that is the bare hash."""
    class _T:
        def __init__(self, h):
            self.hash, self.name, self.size, self.save_path = h, NAME, 1000, SP1
            self.tracker, self.content_path, self.progress, self.completion_on = \
                'https://t.example/announce', '', 1.0, 1

    class _Qbt:
        def auth_log_in(self):
            return True

        def torrents_info(self, **_kw):
            return [_T(H), _T(OTHER)]

    with patch.object(_qbit.qbittorrentapi, 'Client', return_value=_Qbt()):
        rows, report = _qbit.list_torrents({'QB_HOST': 'http://qb:8080'})

    assert [r['reg'] for r in rows] == [H, OTHER]
    assert all(r['instance_id'] is None for r in rows)
    assert (report['torrent_count'], report['distinct_torrents'], report['multi_registered']) == (2, 2, 0)
    assert sources.registration_key(None, H) == H


# ═════════════════════════════════════════════════════════════════════════════
# The audit side of S05 — Triage's rows, and the badge that counts them
# ═════════════════════════════════════════════════════════════════════════════

def _record(path, instance_id, **over):
    """A not-imported torrent file record, as the audit persists it."""
    base = {'path': path, 'size': 100, 'status': 'Seeding', 'imported': False,
            'excluded': False, 'hash': H, 'instance_id': instance_id,
            'trackers': ['t.example'], 'tracker_health': 'unknown', 'tracker_msg': ''}
    base.update(over)
    return base


def _triage_page(records):
    with patch.object(app, 'db_load_config', return_value={}), \
         patch.object(app, 'db_has_file_results', return_value=True), \
         patch.object(app, 'db_load_file_results', return_value=records), \
         patch.object(app, 'fetch_arr_media_index_result', return_value=([], [])), \
         patch.object(app, 'fetch_arr_all_titles_result', return_value=([], [])), \
         patch.object(app, 'normalize_arr_connections', return_value=[]):
        return app.app.test_client().get('/api/workflows/triage').get_json()


def test_triage_lists_each_registration_on_its_own_row():
    """Grouped by hash, two registrations at two save paths became **one** row
    carrying both instances' files and only the first instance's id — so verify
    reported one registration, and a removal from that row removed one while
    the row listed the other's files as going with it."""
    records = [_record('movies/Rel/f.mkv', 1), _record('cross/Rel/f.mkv', 2)]
    for r in records:
        audit._mark_whole_torrents([r], [])

    items = _triage_page(records)['items']

    assert sorted((i['instance_id'], tuple(i['paths'])) for i in items) == [
        (1, ('movies/Rel/f.mkv',)), (2, ('cross/Rel/f.mkv',))]
    assert {i['reg'] for i in items} == {sources.registration_key(1, H),
                                         sources.registration_key(2, H)}


def test_the_badge_counts_what_the_page_lists():
    """`count_triage_items` must stay in lockstep with the endpoint's grouping,
    or the sidebar badge and the Rounds card disagree with the page."""
    records = [_record('movies/Rel/f.mkv', 1), _record('cross/Rel/f.mkv', 2)]

    assert audit.count_triage_items(records)['not_imported'] == 2
    assert len(_triage_page(records)['items']) == 2


def test_one_registration_still_counts_once():
    """Characterisation: a season pack on one instance is one row and one count,
    however many files it holds."""
    records = [_record(f'tv/Show.S01/e{n}.mkv', 1) for n in range(1, 6)]

    assert audit.count_triage_items(records) == {
        'not_imported': 1, 'dead_seeds': 0, 'dead_registrations': 0, 'total': 1}
    assert len(_triage_page(records)['items']) == 1
