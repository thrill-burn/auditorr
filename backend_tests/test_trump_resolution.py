"""Trumped: the code that decides what gets deleted (TRUMPED TR18).

`test_trump.py` covers the string matching, and covers it well. Nothing covered
`_cross_seed_group`, `_trump_prefer_pm_tracker`, `_trump_find_arr_item` at its
endpoint, either `resolve_group` phase, or `execute` — every line between a
pasted PM and a `remove_torrents(delete_files=True)` with no script, no `cmp` and
no undo.

Two kinds of test live here and are kept apart:

* **Characterisation** — behaviour that is already right. It passed before
  Phase 7 and must keep passing through it.
* **Findings** — TR1c, TR2, TR3, TR4, TR5, TR7, TR9, TR10. Each was written
  before its fix and failed for the reason its finding names.

Assertions are on the response and on what `sources.remove_torrents` is called
with, never on a helper's return value alone: a helper can be right while the
endpoint ignores it, which is TR1c exactly — the paths map could say "could not
ask" for a whole phase before anything read it.

Client listings in these fixtures carry only what the client sends: a file list
is client-side paths under the torrent's own `save_path`, which neither backend
remaps (`_qbit` joins `t.save_path`, `_qui` joins `_norm_torrent`'s
`save_path`, and only `fetch_file_map` applies `REMOTE_PATH` → `LOCAL_PATH`).
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app
from arr import parse_release_info_for_path, rank_arr_candidates

NAME = 'Rel.2020.1080p.WEB-DL-GRP'
SP = '/data/torrents/movies'


def _row(h, size=1000, name=NAME, tracker='t1.example', save_path=SP, inst=1):
    return {'hash': h, 'name': name, 'size': size, 'save_path': save_path,
            'tracker': tracker, 'instance_id': inst, 'instance_name': 'main'}


ROWS3 = [_row('aaa', tracker='aither.cc'), _row('bbb', tracker='blutopia.cc'),
         _row('ccc', tracker='hawke.uno')]
SHARED = f'{SP}/{NAME}/j.mkv'
SHARED3 = {'aaa': [SHARED], 'bbb': [SHARED], 'ccc': [SHARED]}


def _items(*hashes):
    return [{'hash': h, 'instance_id': 1} for h in hashes]


def _resolve(rows=ROWS3, paths=SHARED3, seeds=('aaa',), cfg=None, **extra):
    fetch = MagicMock(side_effect=lambda _c, items: {i['hash']: paths.get(i['hash'])
                                                     for i in items if i['hash'] in paths})
    with patch.object(app, 'db_load_config', return_value=dict(cfg or {})), \
         patch.object(app.sources, 'list_torrents', return_value=list(rows)), \
         patch.object(app.sources, 'fetch_torrent_file_paths', fetch), \
         patch.object(app.sources, 'fetch_torrent_details', return_value={}):
        body = {'old_titles': [NAME], 'seed_hashes': list(seeds), **extra}
        resp = app.app.test_client().post('/api/workflows/trump/resolve_group', json=body)
    resp.fetch = fetch
    return resp


def _hashes(resp):
    return sorted(t['hash'] for t in resp.get_json()['torrents'])


# ── a real hardlink tree ──────────────────────────────────────────────────────

@pytest.fixture
def tree(tmp_path):
    """A torrent tree with one file hardlinked into the library and one not.

    The client names these files under `/data/torrents`; the container sees
    them under `tmp_path/torrents`. `os.link` works on NTFS as well.
    """
    torrents, media = tmp_path / 'torrents', tmp_path / 'media'
    (torrents / 'movies' / NAME).mkdir(parents=True)
    (media / 'Rel (2020)').mkdir(parents=True)
    linked = torrents / 'movies' / NAME / 'Rel.mkv'
    linked.write_bytes(b'v' * 1000)
    library = media / 'Rel (2020)' / 'Rel (2020).mkv'
    os.link(linked, library)
    (torrents / 'movies' / NAME / 'Rel.nfo').write_bytes(b'n' * 40)
    return SimpleNamespace(
        cfg={'REMOTE_PATH': '/data/torrents', 'LOCAL_PATH': str(torrents)},
        linked=f'{SP}/{NAME}/Rel.mkv',
        only=f'{SP}/{NAME}/Rel.nfo',
        missing=f'{SP}/{NAME}/gone.mkv',
        library=str(library),
        torrents=torrents,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Characterisation — already right, must stay right
# ═════════════════════════════════════════════════════════════════════════════

class TestAlreadyRight:

    def test_phase_one_prefers_the_pm_trackers_copy_on_an_equal_score(self):
        rows = [_row('aaa', tracker='blutopia.cc'), _row('bbb', tracker='aither.cc')]
        with patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app.sources, 'list_torrents', return_value=rows):
            client = app.app.test_client()
            plain = client.post('/api/workflows/trump/resolve_group',
                                json={'old_titles': [NAME]}).get_json()
            pm = client.post('/api/workflows/trump/resolve_group',
                             json={'old_titles': [NAME],
                                   'indexer': 'Aither (API) (Prowlarr)'}).get_json()
        assert plain['status'] == 'needs_pick'
        assert plain['picks'][0]['auto'] == 'aaa'
        assert pm['picks'][0]['auto'] == 'bbb'
        assert len(pm['picks'][0]['candidates']) == 2, 'a tie-break drops nothing'

    def test_the_pm_tracker_never_outranks_a_better_title_match(self):
        ranked = [dict(_row('aaa', tracker='blutopia.cc'), match_score=1.0),
                  dict(_row('bbb', tracker='aither.cc'), match_score=0.94)]
        assert app._trump_prefer_pm_tracker(ranked, 'aaa', 'Aither (API) (Prowlarr)') == 'aaa'

    def test_the_service_gate_holds_at_the_endpoint(self):
        """A same-titled film never answers for an episode — searched on Sonarr."""
        titles = [
            {'service': 'radarr', 'connection_id': 'r1', 'arr_id': 42, 'title': 'Fargo', 'year': 1996},
            {'service': 'sonarr', 'connection_id': 's1', 'arr_id': 9, 'title': 'Fargo', 'year': 2014},
        ]
        matrix = MagicMock(return_value=[])
        with patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app, 'fetch_arr_all_titles', return_value=titles), \
             patch.object(app, 'arr_titles_errors', return_value=[]), \
             patch.object(app, 'normalize_arr_connections', return_value=[]), \
             patch.object(app, 'fetch_release_matrix', matrix):
            resp = app.app.test_client().post('/api/workflows/trump/search_release',
                                              json={'new_title': 'Fargo.S05E01.1080p.WEB-DL-NEW'})
        assert resp.status_code == 200
        assert resp.get_json()['arr_id'] == 9
        assert matrix.call_args[0][1] == 'sonarr'

    def test_a_healthy_shared_path_group_resolves_whole(self):
        resp = _resolve()
        assert resp.status_code == 200
        assert _hashes(resp) == ['aaa', 'bbb', 'ccc']

    @pytest.mark.parametrize('spelling', [None, []])
    def test_a_seed_whose_listing_is_unusable_refuses(self, spelling):
        resp = _resolve(paths=dict(SHARED3, aaa=spelling))
        assert resp.status_code == 502

    def test_a_failed_instance_refuses_rather_than_narrowing(self):
        """Deliberately *not* loosened into `partial`: a sibling on an instance
        that did not answer is invisible, not narrowed."""
        with patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app.sources, 'list_torrents',
                          side_effect=app.sources.SourceConnectionError('1 of 2 instances')):
            resp = app.app.test_client().post('/api/workflows/trump/resolve_group',
                                              json={'old_titles': [NAME], 'seed_hashes': ['aaa']})
        assert resp.status_code == 502

    def test_execute_deletes_an_unchanged_group_with_its_files(self):
        resp, m = _execute({'hashes': _items('aaa', 'bbb', 'ccc'), 'seed_hashes': ['aaa']})
        assert resp.status_code == 200
        (_cfg, items), kw = m.remove.call_args
        assert sorted(i['hash'] for i in items) == ['aaa', 'bbb', 'ccc']
        assert kw.get('delete_files', True) is True

    def test_deletion_with_the_flag_off_is_still_forbidden(self):
        resp, m = _execute({'hashes': _items('aaa')}, cfg={'ALLOW_CLIENT_DELETE': False})
        assert resp.status_code == 403
        m.remove.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# TR1c + TR2 — partial, and membership by paths alone
# ═════════════════════════════════════════════════════════════════════════════

class TestPartialAndMembership:

    def test_a_candidate_whose_listing_failed_marks_the_group_partial(self):
        resp = _resolve(paths=dict(SHARED3, ccc=None))
        assert resp.status_code == 200
        body = resp.get_json()
        assert _hashes(resp) == ['aaa', 'bbb']
        assert body['partial'] is True
        assert body['unknown_listings'] == 1

    def test_a_candidate_the_client_says_holds_nothing_is_not_partial(self):
        """`[]` is an answer — nothing on disk, so nothing to share or lose."""
        resp = _resolve(paths=dict(SHARED3, ccc=[]))
        body = resp.get_json()
        assert _hashes(resp) == ['aaa', 'bbb']
        assert body['partial'] is False
        assert body['unknown_listings'] == 0

    def test_a_sibling_carrying_an_extra_nfo_joins_the_group(self):
        """TR2's probe: `bbb` is 4 KB larger and shares `j.mkv`. The size
        pre-filter removed it before its paths were ever fetched."""
        rows = [_row('aaa'), _row('bbb', size=1000 + 4096), _row('ccc')]
        paths = dict(SHARED3, bbb=[SHARED, f'{SP}/{NAME}/j.nfo'])
        resp = _resolve(rows=rows, paths=paths)
        assert _hashes(resp) == ['aaa', 'bbb', 'ccc']

    def test_membership_is_the_closure_not_the_seeds_neighbours(self):
        """C shares a file only with B. Deleting B with its files breaks C."""
        paths = {'aaa': [f'{SP}/{NAME}/x.mkv'],
                 'bbb': [f'{SP}/{NAME}/x.mkv', f'{SP}/{NAME}/y.mkv'],
                 'ccc': [f'{SP}/{NAME}/y.mkv']}
        resp = _resolve(paths=paths)
        assert _hashes(resp) == ['aaa', 'bbb', 'ccc']

    def test_an_unrelated_torrent_of_the_same_size_stays_out(self):
        rows = ROWS3 + [_row('zzz', name='Other.2019.1080p-X', save_path='/data/torrents/other')]
        resp = _resolve(rows=rows, paths=dict(SHARED3, zzz=['/data/torrents/other/o.mkv']))
        assert _hashes(resp) == ['aaa', 'bbb', 'ccc']

    def test_a_prefilter_past_its_bound_falls_back_to_size_exact_and_says_so(self):
        rows = [_row('aaa'), _row('bbb'),
                _row('ccc', size=1005, name='Near.A-X', save_path='/data/torrents/a'),
                _row('ddd', size=1008, name='Near.B-X', save_path='/data/torrents/b')]
        paths = {'aaa': [SHARED], 'bbb': [SHARED], 'ccc': ['/data/torrents/a/c.mkv'],
                 'ddd': ['/data/torrents/b/d.mkv']}
        with patch.object(app, '_TRUMP_CANDIDATE_BOUND', 2, create=True):
            resp = _resolve(rows=rows, paths=paths)
        body = resp.get_json()
        assert _hashes(resp) == ['aaa', 'bbb']
        assert body['partial'] is True
        assert body['prefilter']['bounded'] is True
        asked = sorted(i['hash'] for i in resp.fetch.call_args[0][1])
        assert asked == ['aaa', 'bbb']


# ═════════════════════════════════════════════════════════════════════════════
# TR3 — does anything outside this group still hold the bytes?
# ═════════════════════════════════════════════════════════════════════════════

def _members(resp):
    return {t['hash']: t for t in resp.get_json()['torrents']}


class TestHardlinkCheck:

    def test_a_file_linked_outside_the_group_is_hardlinked(self, tree):
        resp = _resolve(rows=[_row('aaa')], paths={'aaa': [tree.linked]}, cfg=tree.cfg)
        assert _members(resp)['aaa']['hardlinked'] is True

    def test_one_only_copy_beside_a_linked_file_makes_the_torrent_false(self, tree):
        resp = _resolve(rows=[_row('aaa')], paths={'aaa': [tree.linked, tree.only]}, cfg=tree.cfg)
        body = resp.get_json()
        assert _members(resp)['aaa']['hardlinked'] is False
        assert body['link_check']['only_copy_bytes'] == 40

    def test_a_path_that_does_not_exist_is_unknown_never_safe(self, tree):
        resp = _resolve(rows=[_row('aaa')], paths={'aaa': [tree.missing]}, cfg=tree.cfg)
        assert _members(resp)['aaa']['hardlinked'] is None

    def test_false_outranks_unknown(self, tree):
        resp = _resolve(rows=[_row('aaa')], paths={'aaa': [tree.only, tree.missing]}, cfg=tree.cfg)
        assert _members(resp)['aaa']['hardlinked'] is False

    def test_shared_path_siblings_count_their_one_path_once(self, tree):
        rows = [_row('aaa'), _row('bbb')]
        linked = _resolve(rows=rows, paths={'aaa': [tree.linked], 'bbb': [tree.linked]}, cfg=tree.cfg)
        assert {h: m['hardlinked'] for h, m in _members(linked).items()} == {'aaa': True, 'bbb': True}
        only = _resolve(rows=rows, paths={'aaa': [tree.only], 'bbb': [tree.only]}, cfg=tree.cfg)
        assert {h: m['hardlinked'] for h, m in _members(only).items()} == {'aaa': False, 'bbb': False}

    def test_a_distinct_hardlink_inside_the_group_is_not_an_outside_link(self, tree):
        """Two members, two paths, one inode, nlink 2 — nothing outside holds it."""
        other = tree.torrents / 'movies' / 'Cross' / 'Rel.nfo'
        other.parent.mkdir(parents=True)
        os.link(tree.torrents / 'movies' / NAME / 'Rel.nfo', other)
        rows = [_row('aaa'), _row('bbb', name='Cross')]
        paths = {'aaa': [tree.only], 'bbb': [f'{SP}/Cross/Rel.nfo']}
        resp = _resolve(rows=rows, paths=paths, seeds=('aaa', 'bbb'), cfg=tree.cfg)
        assert {h: m['hardlinked'] for h, m in _members(resp).items()} == {'aaa': False, 'bbb': False}


# ═════════════════════════════════════════════════════════════════════════════
# TR5 — re-verify inside execute; TR4 — grab without deleting
# ═════════════════════════════════════════════════════════════════════════════

class _Thread:
    """A thread that is created and recorded but never run."""
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_kw):
        self.target = target

    def start(self):
        _Thread.started.append(self.target)


def _execute(body, rows=ROWS3, paths=SHARED3, cfg=None, queue=None, thread=_Thread, list_error=None):
    cfg = {'ALLOW_CLIENT_DELETE': True, **(cfg or {})}
    _Thread.started = []
    remove = MagicMock(side_effect=lambda _c, items, delete_files=True: len(items))
    grab = MagicMock(return_value={})
    fetch = MagicMock(side_effect=lambda _c, items: {i['hash']: paths.get(i['hash'])
                                                     for i in items if i['hash'] in paths})
    listing = (MagicMock(side_effect=list_error) if list_error
               else MagicMock(return_value=list(rows)))
    with patch.object(app, 'db_load_config', return_value=cfg), \
         patch.object(app.sources, 'list_torrents', listing), \
         patch.object(app.sources, 'fetch_torrent_file_paths', fetch), \
         patch.object(app.sources, 'fetch_torrent_details', return_value={}), \
         patch.object(app.sources, 'remove_torrents', remove), \
         patch.object(app, 'grab_release', grab), \
         patch.object(app, 'queue_records_for_item', return_value=[] if queue is None else queue), \
         patch.object(app, 'db_update_meta') as meta, \
         patch.object(app, 'try_start_scanning', return_value=True) as scan, \
         patch.object(app, 'nudge_watchdog') as nudge, \
         patch.object(app.threading, 'Thread', thread):
        resp = app.app.test_client().post('/api/workflows/trump/execute', json=body)
    return resp, SimpleNamespace(remove=remove, grab=grab, meta=meta, scan=scan,
                                 nudge=nudge, threads=list(_Thread.started))


RELEASE = {'release': {'guid': 'g-1', 'indexer_id': 3}, 'service': 'radarr',
           'connection_id': 'r1', 'arr_id': 7, 'arr_title': 'Rel (2020)'}


class TestExecuteReverifies:

    def test_a_group_that_grew_is_refused_and_nothing_is_deleted(self):
        """The user's decision (2026-09-13): a grown group is new information
        about what will be touched, and it must be seen before acting."""
        resp, m = _execute({'hashes': _items('aaa', 'bbb'), 'seed_hashes': ['aaa']})
        assert resp.status_code == 409
        body = resp.get_json()
        assert body['code'] == 'group_changed'
        assert body['added'] == 1 and body['missing'] == 0
        m.remove.assert_not_called()

    def test_a_hash_the_client_no_longer_holds_is_refused(self):
        resp, m = _execute({'hashes': _items('aaa', 'bbb', 'ccc'), 'seed_hashes': ['aaa']},
                           rows=ROWS3[:2])
        assert resp.status_code == 409
        assert resp.get_json()['code'] == 'group_changed'
        m.remove.assert_not_called()

    def test_a_member_that_stopped_sharing_is_refused(self):
        resp, m = _execute({'hashes': _items('aaa', 'bbb', 'ccc'), 'seed_hashes': ['aaa']},
                           paths=dict(SHARED3, ccc=['/data/torrents/elsewhere/j.mkv']))
        assert resp.status_code == 409
        assert resp.get_json()['missing'] == 1
        m.remove.assert_not_called()

    def test_a_seed_listing_that_fails_on_reverify_refuses(self):
        resp, m = _execute({'hashes': _items('aaa', 'bbb', 'ccc'), 'seed_hashes': ['aaa']},
                           paths=dict(SHARED3, aaa=None))
        assert resp.status_code == 502
        m.remove.assert_not_called()

    def test_a_failed_instance_on_reverify_refuses(self):
        resp, m = _execute({'hashes': _items('aaa')},
                           list_error=app.sources.SourceConnectionError('1 of 2 instances'))
        assert resp.status_code == 502
        m.remove.assert_not_called()

    def test_partial_on_reverify_needs_an_acknowledgement(self):
        paths = dict(SHARED3, ccc=None)
        resp, m = _execute({'hashes': _items('aaa', 'bbb'), 'seed_hashes': ['aaa']}, paths=paths)
        assert resp.status_code == 409
        assert resp.get_json()['code'] == 'partial'
        m.remove.assert_not_called()

        resp, m = _execute({'hashes': _items('aaa', 'bbb'), 'seed_hashes': ['aaa'],
                            'acknowledge_partial': True}, paths=paths)
        assert resp.status_code == 200
        assert sorted(i['hash'] for i in m.remove.call_args[0][1]) == ['aaa', 'bbb']

    def test_an_only_copy_needs_its_own_acknowledgement(self, tree):
        rows, paths = [_row('aaa')], {'aaa': [tree.linked, tree.only]}
        resp, m = _execute({'hashes': _items('aaa')}, rows=rows, paths=paths, cfg=tree.cfg)
        assert resp.status_code == 409
        body = resp.get_json()
        assert body['code'] == 'only_copy'
        assert body['only_copy_bytes'] == 40
        m.remove.assert_not_called()

        resp, m = _execute({'hashes': _items('aaa'), 'acknowledge_only_copy': True},
                           rows=rows, paths=paths, cfg=tree.cfg)
        assert resp.status_code == 200
        m.remove.assert_called_once()

    def test_an_unknown_link_state_needs_no_acknowledgement(self, tree):
        """`null` never renders as safe, and it is not a known destruction either."""
        resp, m = _execute({'hashes': _items('aaa')}, rows=[_row('aaa')],
                           paths={'aaa': [tree.missing]}, cfg=tree.cfg)
        assert resp.status_code == 200
        m.remove.assert_called_once()


class TestGrabWithoutDeleting:

    def test_a_grab_needs_no_client_delete_permission(self):
        resp, m = _execute(dict(RELEASE), cfg={'ALLOW_CLIENT_DELETE': False})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['removed'] == 0 and body['grabbed'] is True
        m.remove.assert_not_called()
        m.grab.assert_called_once()
        assert not [c for c in m.meta.call_args_list if c[0][0] == 'ns_progress'], \
            'Kingmaker pays for a swap, not for a grab'

    def test_nothing_to_do_is_a_400(self):
        resp, m = _execute({}, cfg={'ALLOW_CLIENT_DELETE': False})
        assert resp.status_code == 400

    def test_a_double_submit_is_not_a_second_download(self):
        resp, m = _execute(dict(RELEASE), queue=[{'title': 'Rel.2020.2160p-NEW'}])
        assert resp.status_code == 409
        assert resp.get_json()['code'] == 'already_queued'
        m.grab.assert_not_called()

        resp, m = _execute(dict(RELEASE, force=True), queue=[{'title': 'Rel.2020.2160p-NEW'}])
        assert resp.status_code == 200
        m.grab.assert_called_once()

    def test_the_queue_is_checked_before_anything_is_deleted(self):
        resp, m = _execute(dict(RELEASE, hashes=_items('aaa', 'bbb', 'ccc'), seed_hashes=['aaa']),
                           queue=[{'title': 'Rel.2020.2160p-NEW'}])
        assert resp.status_code == 409
        m.remove.assert_not_called()

    def test_a_swap_still_pays_kingmaker(self):
        resp, m = _execute(dict(RELEASE, hashes=_items('aaa', 'bbb', 'ccc'), seed_hashes=['aaa']))
        assert resp.status_code == 200
        assert [c for c in m.meta.call_args_list if c[0][0] == 'ns_progress']


# ═════════════════════════════════════════════════════════════════════════════
# TR9 + TR10 — follow the grab to the import, then scan
# ═════════════════════════════════════════════════════════════════════════════

class TestFollowThrough:

    def test_execute_no_longer_scans_seconds_after_the_delete(self):
        resp, m = _execute(dict(RELEASE, hashes=_items('aaa', 'bbb', 'ccc'), seed_hashes=['aaa']))
        assert resp.status_code == 200
        m.scan.assert_not_called()
        assert app.run_audit_process not in m.threads

    def test_a_grab_is_followed_by_an_import_watch_on_the_shared_panel(self):
        resp, m = _execute(dict(RELEASE, hashes=_items('aaa', 'bbb', 'ccc'), seed_hashes=['aaa']))
        job_id = resp.get_json().get('watch_job_id')
        assert job_id and job_id in app._import_watches
        watch = app._import_watches[job_id]
        assert watch['source'] == 'trump'
        assert watch['service'] == 'radarr' and watch['title'] == 'Rel (2020)'
        assert len(m.threads) == 1

    def test_a_removal_with_nothing_grabbed_leaves_the_scan_to_the_watchdog(self):
        resp, m = _execute({'hashes': _items('aaa', 'bbb', 'ccc'), 'seed_hashes': ['aaa']})
        assert resp.status_code == 200
        assert resp.get_json().get('watch_job_id') is None
        m.scan.assert_not_called()
        m.nudge.assert_called_once()

    def test_a_trump_watch_records_no_backfill_credit(self):
        store = {}

        def fake_update_meta(key, fn, default=None):
            store[key] = fn(store.get(key, default))
            return store[key]

        with patch.object(app, 'db_update_meta', fake_update_meta), \
             patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app.threading, 'Thread', _Thread):
            resp = app.app.test_client().post('/api/workflows/watch_import', json={
                'service': 'radarr', 'connection_id': 'r1', 'arr_id': 7,
                'title': 'Rel (2020)', 'files': 1, 'source': 'trump'})
        assert resp.status_code == 200
        assert 'backfilled' not in (store.get('ns_progress') or {})

    def _run_watch(self, body, source):
        """Drive the watch thread inline; the audit thread it may start is recorded, not run."""
        scans = []

        class _Sync:
            def __init__(self, target=None, args=(), **_kw):
                self.target = target

            def start(self):
                if self.target is app.run_audit_process:
                    scans.append(self.target)
                else:
                    self.target()

        fmi = MagicMock()
        polls = iter([[{'trackedDownloadState': 'importPending', 'downloadId': 'HASH',
                        'outputPath': '/downloads/Show.S01', 'statusMessages': []}], [], [], []])
        with patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app, 'db_update_meta'), \
             patch.object(app.threading, 'Thread', _Sync), \
             patch.object(app.time, 'sleep'), \
             patch.object(app, 'get_arr_file_id', return_value=[1]), \
             patch.object(app, 'poll_queue_until_clear', side_effect=lambda *a, **k: next(polls)), \
             patch.object(app, 'sonarr_episodes_by_file', return_value={501: [(101, 1, 1)]}), \
             patch.object(app, 'force_manual_import_by_id', fmi), \
             patch.object(app, 'try_start_scanning', return_value=True) as scan:
            resp = app.app.test_client().post('/api/workflows/watch_import',
                                              json=dict(body, source=source))
        return resp, fmi, scan, scans

    def test_a_trump_import_scopes_to_the_trumped_files_and_then_scans(self):
        body = {'service': 'sonarr', 'connection_id': 's1', 'arr_id': 5,
                'title': 'Show', 'file_ids': [501]}
        resp, fmi, scan, scans = self._run_watch(body, 'trump')
        assert resp.status_code == 200
        assert fmi.call_args.kwargs['only_episode_ids'] == [101]
        scan.assert_called_once_with('trump')
        assert scans == [app.run_audit_process]

    def test_a_backfill_import_still_leaves_the_scan_to_the_watchdog(self):
        body = {'service': 'sonarr', 'connection_id': 's1', 'arr_id': 5,
                'title': 'Show', 'file_ids': [501]}
        resp, fmi, scan, scans = self._run_watch(body, 'backfill')
        scan.assert_not_called()
        assert scans == []


# ═════════════════════════════════════════════════════════════════════════════
# TR7 — the arr item from the group's paths
# ═════════════════════════════════════════════════════════════════════════════

def _index_row(path, arr_id=7, conn='r1', service='radarr', title='Rel', year=2020, size=1000, **kw):
    return {'service': service, 'connection_id': conn, 'connection_name': conn, 'title': title,
            'year': year, 'path': path, 'relative_path': os.path.basename(path), 'arr_id': arr_id,
            'file_id': arr_id * 10, 'title_slug': title.lower(), 'size': size, **kw}


def _title(arr_id, title='Rel', year=2020, service='radarr', conn='r1'):
    return {'service': service, 'connection_id': conn, 'arr_id': arr_id, 'title': title,
            'year': year, 'title_slug': title.lower(), 'has_file': True}


def _search(tree, index, titles, **extra):
    matrix = MagicMock(return_value=[])
    with patch.object(app, 'db_load_config', return_value=dict(tree.cfg)), \
         patch.object(app, 'fetch_arr_all_titles', return_value=titles), \
         patch.object(app, 'arr_titles_errors', return_value=[]), \
         patch.object(app, 'fetch_arr_media_index', return_value=index), \
         patch.object(app, 'arr_media_index_errors', return_value=[]), \
         patch.object(app, 'normalize_arr_connections', return_value=[]), \
         patch.object(app, 'fetch_release_matrix', matrix):
        resp = app.app.test_client().post('/api/workflows/trump/search_release', json={
            'new_title': 'Rel.2020.2160p.UHD.BluRay-NEW', 'group_paths': [tree.linked], **extra})
    resp.matrix = matrix
    return resp


class TestArrItemFromPaths:

    def test_the_group_resolves_its_arr_item_by_inode_with_no_title_match(self, tree):
        resp = _search(tree, [_index_row(tree.library)], titles=[])
        assert resp.status_code == 200
        body = resp.get_json()
        assert (body['arr_id'], body['resolved_by']) == (7, 'path')
        assert body['library_file_ids'] == [70]

    def test_a_path_hit_that_agrees_with_the_title_carries_its_files(self, tree):
        resp = _search(tree, [_index_row(tree.library)], titles=[_title(7)])
        body = resp.get_json()
        assert (body['arr_id'], body['resolved_by'], body['library_file_ids']) == (7, 'path', [70])

    def test_a_path_hit_and_a_title_hit_that_disagree_are_reported_not_picked(self, tree):
        resp = _search(tree, [_index_row(tree.library, arr_id=7)], titles=[_title(9)])
        assert resp.status_code == 409
        body = resp.get_json()
        assert body['code'] == 'arr_item_conflict'
        assert body['path_item']['arr_id'] == 7 and body['title_item']['arr_id'] == 9
        resp.matrix.assert_not_called()

    def test_the_users_choice_settles_a_conflict(self, tree):
        index, titles = [_index_row(tree.library, arr_id=7)], [_title(9)]
        chose_title = _search(tree, index, titles,
                              arr_item={'service': 'radarr', 'connection_id': 'r1', 'arr_id': 9})
        body = chose_title.get_json()
        assert (body['arr_id'], body['library_file_ids']) == (9, [])
        chose_path = _search(tree, index, titles,
                             arr_item={'service': 'radarr', 'connection_id': 'r1', 'arr_id': 7})
        body = chose_path.get_json()
        assert (body['arr_id'], body['library_file_ids']) == (7, [70])

    def test_no_path_hit_falls_back_to_the_title(self, tree):
        resp = _search(tree, [_index_row('/nowhere/Rel (2020).mkv')], titles=[_title(7)])
        body = resp.get_json()
        assert (body['arr_id'], body['resolved_by'], body['library_file_ids']) == (7, 'title', [])

    def test_two_instances_holding_the_payload_are_flagged(self, tree):
        """4b's UI half is unreachable on the reference box (M4), so it is built
        as a payload flag and tested by fixture."""
        index = [_index_row(tree.library, arr_id=7, conn='r1'),
                 _index_row(tree.library, arr_id=3, conn='r4k')]
        body = _search(tree, index, titles=[]).get_json()
        assert body['arr_item_ambiguous'] is True
        assert len(body['path_items']) == 2


def test_the_episode_anchor_reads_media_index_rows():
    """Phase 6's note: index rows carry `season_number`, which skipped the
    filename fallback, and `episode_numbers` is always empty on a real Sonarr —
    so the episode anchor never fired there. Fixtures carry no episode fields."""
    rows = [
        {'service': 'sonarr', 'arr_id': 5, 'title': 'Show', 'year': 2019, 'season_number': 1,
         'path': '/tv/Show/Season 01/Show - S01E01 - Pilot.mkv',
         'relative_path': 'Season 01/Show - S01E01 - Pilot.mkv', 'episode_ids': [], 'episode_numbers': []},
        {'service': 'sonarr', 'arr_id': 6, 'title': 'Show', 'year': 2019, 'season_number': 1,
         'path': '/tv/Show (2)/Season 01/Show - S01E02 - Two.mkv',
         'relative_path': 'Season 01/Show - S01E02 - Two.mkv', 'episode_ids': [], 'episode_numbers': []},
    ]
    parsed = parse_release_info_for_path('Show.S01E02.1080p.WEB-DL-GRP')
    assert rank_arr_candidates(rows, parsed, service='sonarr')[0]['arr_id'] == 6
