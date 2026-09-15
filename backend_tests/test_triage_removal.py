"""Triage's removal: the resolver, the file decision, the route and its outcomes.

Triage is the only workflow that deletes **through the client**, with
`deleteWithFiles` — no script, no `cmp`, no second reader — and its default mode
decides per torrent whether the files go too. Three defects sat in that path, all
confirmed by running the code on 2026-09-14 (PHASE9_BRIEF):

* **S01** (the 2026-09-10 outside review) — `delete_files: 'auto'` read a listing
  it could not get as "no files", a survivor it could not list as "shares
  nothing", and pre-filtered candidate owners by *exact* size, so a carrier with
  one extra `.nfo` was never even asked. The worst case is a dead registration,
  whose files are by definition a live carrier's.
* **The Phase 7 regression** — `_cross_seed_group` started returning
  `(group, components, unknown)` and `resolve_groups` kept iterating it, so the
  delete modal 500'd whenever a selected torrent was in the client, printed "no
  cross-seeds" under every row, and left Remove enabled.
* **S09** — the page dismissed every selected row on any success, whatever
  actually left the client.

Two kinds of test, kept apart as `test_trump_resolution.py` keeps them:
characterisation (already right, must stay right) and findings (each written
before its fix, failing for the reason its finding names).

**A listing mock answers only for the hashes it is asked about.** The first run
of the brief's probe used a static paths map; the mock answered for a torrent
the code never asked about and reported the unequal-size shape as safe. That is
also why `PartitionRemovalTests` never caught the size pre-filter.

Assertions are on the response and on what `sources.remove_torrents` is called
with — including `delete_files` — never on a helper's return alone.
"""
from unittest.mock import patch

import app
from sources import SourceConnectionError, new_source_report, report_instance_failure

SP = '/data/torrents/movies'
REL = 'Rel.2020.1080p.WEB-DL-GRP'
MKV = f'{SP}/{REL}/{REL}.mkv'
NFO = f'{SP}/{REL}/{REL}.nfo'


def _row(h, size=1000, name=REL, save_path=SP, tracker='t.example', inst=1, inst_name='main'):
    return {'hash': h, 'name': name, 'size': size, 'save_path': save_path,
            'tracker': tracker, 'instance_id': inst, 'instance_name': inst_name}


class _Client:
    """A torrent client that answers only what it is asked, and forgets what it removes.

    `lingering` is `{hash: looks}` — how many post-removal listings still show a
    removed torrent (`None` for ever), which is a client that drops a torrent a
    moment after its API returns, or never. `failed_instances` do not answer the
    detailed listing; `remove_error` raises from the `remove_error_on`-th
    removal call onwards.
    """

    def __init__(self, rows, paths, *, lingering=None, failed_instances=(),
                 remove_error=None, remove_error_on=1):
        self.rows = {r['hash']: dict(r) for r in rows}
        self.paths = dict(paths)
        self.lingering = dict(lingering or {})
        self.failed_instances = set(failed_instances)
        self.remove_error = remove_error
        self.remove_error_on = remove_error_on
        self.asked = []
        self.removals = []          # [(hashes, delete_files)]
        self.removed = set()

    def list_torrents(self, _cfg):
        return list(self.rows.values())

    def list_torrents_detailed(self, _cfg):
        for h in list(self.lingering):
            if h not in self.removed or self.lingering[h] is None:
                continue
            if self.lingering[h] <= 0:
                self.rows.pop(h, None)
                del self.lingering[h]
            else:
                self.lingering[h] -= 1
        report = new_source_report('qui')
        names = {r.get('instance_name') for r in self.rows.values()} | self.failed_instances
        report['instances_total'] = len(names)
        for name in sorted(self.failed_instances):
            report_instance_failure(report, name, 'timed out')
        report['instances_ok'] = len(names) - len(self.failed_instances)
        rows = [r for r in self.rows.values() if r.get('instance_name') not in self.failed_instances]
        return rows, report

    def fetch_paths(self, _cfg, items):
        out = {}
        for i in items:
            self.asked.append(i['hash'])
            out[i['hash']] = self.paths.get(i['hash'])
        return out

    def remove(self, _cfg, items, delete_files=True):
        hashes = [i['hash'] for i in items]
        self.removals.append((hashes, delete_files))
        if self.remove_error is not None and len(self.removals) >= self.remove_error_on:
            raise self.remove_error
        for h in hashes:
            self.removed.add(h)
            if h not in self.lingering:
                self.rows.pop(h, None)
        return len(hashes)

    def files_for(self, h):
        """How `h` was removed: True (files deleted), False (files kept), None (not removed)."""
        for hashes, delete_files in self.removals:
            if h in hashes:
                return delete_files
        return None

    def deleted_with_files(self):
        return sorted(h for hashes, delete_files in self.removals if delete_files for h in hashes)


def _failing_listing(_cfg):
    raise SourceConnectionError('1 of 2 torrent-client instance(s) could not be listed')


def _post(path, body, client, cfg=None):
    cfg = {'ALLOW_CLIENT_DELETE': True, **(cfg or {})}
    with patch.object(app, 'db_load_config', return_value=cfg), \
         patch.object(app.sources, 'list_torrents', side_effect=client.list_torrents), \
         patch.object(app.sources, 'list_torrents_detailed', side_effect=client.list_torrents_detailed), \
         patch.object(app.sources, 'fetch_torrent_file_paths', side_effect=client.fetch_paths), \
         patch.object(app.sources, 'fetch_torrent_details', return_value={}), \
         patch.object(app.sources, 'remove_torrents', side_effect=client.remove), \
         patch.object(app, 'nudge_watchdog'), \
         patch.object(app.time, 'sleep'):
        return app.app.test_client().post(path, json=body)


def _remove(client, hashes, **body):
    return _post('/api/workflows/remove_torrents',
                 {'items': [{'hash': h, 'instance_id': 1} for h in hashes], **body}, client)


def _resolve(client, hashes):
    return _post('/api/workflows/triage/resolve_groups', {'hashes': list(hashes)}, client)


def _torrent(resp, h):
    return next(t for t in resp.get_json()['torrents'] if t['hash'] == h)


def _member(resp, seed, h):
    return next(m for m in resp.get_json()['groups'][seed] if m['hash'] == h)


ROWS3 = [_row('aaa', tracker='aither.cc'), _row('bbb', tracker='blutopia.cc'), _row('ccc', tracker='hawke.uno')]
SHARED3 = {'aaa': [MKV], 'bbb': [MKV], 'ccc': [MKV]}


# ═════════════════════════════════════════════════════════════════════════════
# Characterisation — already right, must stay right
# ═════════════════════════════════════════════════════════════════════════════

class TestAlreadyRight:

    def test_removal_needs_client_delete_enabled(self):
        client = _Client([_row('A')], {'A': [MKV]})
        resp = _post('/api/workflows/remove_torrents',
                     {'items': [{'hash': 'A'}], 'delete_files': 'auto'}, client,
                     cfg={'ALLOW_CLIENT_DELETE': False})
        assert resp.status_code == 403
        assert client.removals == []

    def test_an_unreachable_client_is_a_502_and_removes_nothing(self):
        client = _Client([_row('A')], {'A': [MKV]})
        client.list_torrents = _failing_listing
        resp = _remove(client, ['A'], delete_files='auto')
        assert resp.status_code == 502
        assert client.removals == []

    def test_a_removal_the_client_refuses_is_a_502(self):
        client = _Client([_row('A')], {'A': [MKV]}, remove_error=SourceConnectionError('HTTP 500'))
        resp = _remove(client, ['A'], delete_files=False)
        assert resp.status_code == 502

    def test_keeping_files_never_deletes_a_file(self):
        client = _Client([_row('A'), _row('B', tracker='u.example')], {'A': [MKV], 'B': [MKV]})
        resp = _remove(client, ['A', 'B'], delete_files=False)
        assert resp.status_code == 200
        assert client.removals and client.deleted_with_files() == []

    def test_a_standalone_torrent_still_deletes_its_files(self):
        """The rule must not cost the working path: a torrent nothing else holds,
        whose neighbours all answered, still goes with its files."""
        client = _Client([_row('A'), _row('B', size=5000, name='Other')],
                         {'A': [MKV], 'B': [f'{SP}/Other/o.mkv']})
        _remove(client, ['A'], delete_files='auto')
        assert client.files_for('A') is True

    def test_a_distinct_hardlink_that_survives_does_not_keep_files(self):
        """A survivor holding a *different path* to the same inode is not harmed:
        deleting this torrent drops only its own link."""
        client = _Client([_row('A'), _row('B', name='Cross', tracker='u.example')],
                         {'A': [MKV], 'B': [f'{SP}/Cross/{REL}.mkv']})
        _remove(client, ['A'], delete_files='auto')
        assert client.files_for('A') is True

    def test_removing_the_whole_shared_group_deletes_its_files(self):
        client = _Client(ROWS3, SHARED3)
        _remove(client, ['aaa', 'bbb', 'ccc'], delete_files='auto')
        assert client.deleted_with_files() == ['aaa', 'bbb', 'ccc']

    def test_a_failed_instance_refuses_resolve_groups(self):
        client = _Client(ROWS3, SHARED3)
        client.list_torrents = _failing_listing
        assert _resolve(client, ['aaa']).status_code == 502


# ═════════════════════════════════════════════════════════════════════════════
# S01 — unknown ownership never authorises deleting a file
# ═════════════════════════════════════════════════════════════════════════════

class TestUnknownOwnershipKeepsFiles:

    def test_a_dead_registration_never_deletes_its_live_carriers_file(self):
        """The brief's first test. A dead registration's files are, by definition,
        a healthy carrier's — and this carrier also carries an `.nfo`, so it is
        40 bytes larger and the exact-size pre-filter never asked about it."""
        client = _Client(
            [_row('dead', size=1000, tracker='dead.example'),
             _row('carrier', size=1040, tracker='live.example')],
            {'dead': [MKV], 'carrier': [MKV, NFO]})
        resp = _remove(client, ['dead'], delete_files='auto')
        assert resp.status_code == 200
        assert 'carrier' in client.asked
        assert client.deleted_with_files() == []
        assert client.files_for('dead') is False
        assert _torrent(resp, 'dead')['reason'] == 'shared'

    def test_an_unknown_listing_for_the_removed_torrent_keeps_its_files(self):
        client = _Client([_row('A')], {'A': None})
        resp = _remove(client, ['A'], delete_files='auto')
        assert resp.status_code == 200
        assert client.files_for('A') is False
        assert _torrent(resp, 'A')['reason'] == 'unusable_listing'

    def test_an_empty_listing_for_the_removed_torrent_keeps_its_files(self):
        """`[]` is an answer for a *candidate* — it holds nothing to share — but
        for the torrent being removed it is Trumped's unusable seed: the files a
        delete would touch are exactly what the listing failed to name."""
        client = _Client([_row('A')], {'A': []})
        resp = _remove(client, ['A'], delete_files='auto')
        assert client.files_for('A') is False
        assert _torrent(resp, 'A')['reason'] == 'unusable_listing'

    def test_an_unknown_listing_for_a_survivor_keeps_the_files(self):
        client = _Client([_row('A'), _row('B', tracker='u.example')], {'A': [MKV], 'B': None})
        resp = _remove(client, ['A'], delete_files='auto')
        assert resp.status_code == 200
        assert client.files_for('A') is False
        assert _torrent(resp, 'A')['reason'] == 'unknown'

    def test_a_survivor_of_a_different_size_is_asked_about(self):
        """The review's third probe: different names, 1% apart in size, one
        shared path. Payload size plays no part in sharing (TR2)."""
        client = _Client([_row('A', size=1000, name='A', save_path='/d'),
                          _row('B', size=1010, name='B', save_path='/d')],
                         {'A': ['/d/p'], 'B': ['/d/p']})
        resp = _remove(client, ['A'], delete_files='auto')
        assert sorted(set(client.asked)) == ['A', 'B']
        assert client.files_for('A') is False
        assert _torrent(resp, 'A')['reason'] == 'shared'

    def test_an_omitted_delete_files_does_not_delete_unchecked(self):
        """An omitted `delete_files` used to default to `True` — a request that
        said nothing deleted files with no ownership check at all. Decided by the
        user on 2026-09-15: omitted and `true` both take the checked path."""
        client = _Client([_row('A'), _row('B', tracker='u.example')], {'A': [MKV], 'B': [MKV]})
        resp = _post('/api/workflows/remove_torrents', {'items': [{'hash': 'A', 'instance_id': 1}]}, client)
        assert resp.status_code == 200
        assert client.files_for('A') is False

    def test_true_is_checked_too(self):
        client = _Client([_row('A'), _row('B', tracker='u.example')], {'A': [MKV], 'B': [MKV]})
        _remove(client, ['A'], delete_files=True)
        assert client.files_for('A') is False


# ═════════════════════════════════════════════════════════════════════════════
# The closure — everything a delete touches, found from every member
# ═════════════════════════════════════════════════════════════════════════════

# A single-file torrent inside a release folder (A), the release-folder torrent
# holding A's file and a sidecar (B), and a sidecar-only torrent (C). C shares a
# file with B and nothing with A: its root sits inside B's but not A's, and its
# size is nowhere near A's, so a candidate search run only from A never meets it.
CHAIN_ROWS = [_row('A', size=1000, name='Rel.mkv', save_path=f'{SP}/Rel'),
              _row('B', size=1100, name='Rel'),
              _row('C', size=100, name='Rel.nfo', save_path=f'{SP}/Rel')]
CHAIN_PATHS = {'A': [f'{SP}/Rel/Rel.mkv'],
               'B': [f'{SP}/Rel/Rel.mkv', f'{SP}/Rel/Rel.nfo'],
               'C': [f'{SP}/Rel/Rel.nfo']}


class TestClosure:

    def test_ownership_is_the_closure_over_shared_paths(self):
        """Remove A; B shares A's path and a second path with C; B survives, so
        A keeps its files — and the modal's group for A names C too, because
        removing B with its files would break C (the review's amendment 2)."""
        client = _Client(CHAIN_ROWS, CHAIN_PATHS)
        _remove(client, ['A'], delete_files='auto')
        assert client.files_for('A') is False

        resp = _resolve(_Client(CHAIN_ROWS, CHAIN_PATHS), ['A'])
        assert resp.status_code == 200
        assert sorted(m['hash'] for m in resp.get_json()['groups']['A']) == ['A', 'B', 'C']

    def test_a_sharer_of_a_members_file_keeps_that_members_files(self):
        """Candidates are searched near every member, not only near the seed:
        removing B with its files would delete the `.nfo` C is seeding."""
        resp = _resolve(_Client(CHAIN_ROWS, CHAIN_PATHS), ['A'])
        assert _member(resp, 'A', 'B')['files']['all'] == 'delete'
        assert _member(resp, 'A', 'A')['files']['all'] == 'delete'

        client = _Client(CHAIN_ROWS, CHAIN_PATHS)
        resp = _remove(client, ['A', 'B'], delete_files='auto')
        assert 'C' in client.asked
        assert client.files_for('B') is False
        assert _torrent(resp, 'B')['reason'] == 'shared'


# ═════════════════════════════════════════════════════════════════════════════
# resolve_groups — the modal's answer
# ═════════════════════════════════════════════════════════════════════════════

class TestResolveGroups:

    def test_resolve_groups_answers_when_a_seed_is_in_the_client(self):
        """500 since `a96bb15`: the caller iterated `_cross_seed_group`'s tuple."""
        resp = _resolve(_Client(ROWS3, SHARED3), ['aaa'])
        assert resp.status_code == 200
        body = resp.get_json()
        assert sorted(m['hash'] for m in body['groups']['aaa']) == ['aaa', 'bbb', 'ccc']
        assert body['checked'] is True
        seed = _member(resp, 'aaa', 'aaa')
        assert seed['shares_path'] is True
        assert seed['files'] == {'one': 'keep', 'all': 'delete'}
        assert seed['reason'] == {'one': 'shared', 'all': 'requested'}
        assert sorted(seed['shares_with']) == ['bbb', 'ccc']
        # A member that is not the seed is removed only with the whole group.
        assert _member(resp, 'aaa', 'bbb')['files']['one'] is None

    def test_resolve_groups_reports_what_it_could_not_check(self):
        paths = {'aaa': [MKV], 'bbb': [MKV], 'ccc': None}
        resp = _resolve(_Client(ROWS3, paths), ['aaa'])
        body = resp.get_json()
        assert body['checked'] is False
        assert body['unknown_listings'] == 1
        seed = _member(resp, 'aaa', 'aaa')
        assert seed['files'] == {'one': 'keep', 'all': 'keep'}
        assert seed['reason'] == {'one': 'shared', 'all': 'unknown'}

    def test_a_seed_whose_listing_failed_says_so(self):
        resp = _resolve(_Client([_row('aaa')], {'aaa': None}), ['aaa'])
        seed = _member(resp, 'aaa', 'aaa')
        assert seed['files'] == {'one': 'keep', 'all': 'keep'}
        assert seed['reason']['one'] == 'unusable_listing'

    def test_a_hash_no_longer_in_the_client_is_named(self):
        resp = _resolve(_Client([_row('aaa')], {'aaa': [MKV]}), ['aaa', 'gone'])
        body = resp.get_json()
        assert body['groups']['gone'] == []
        assert body['missing'] == ['gone']


# ═════════════════════════════════════════════════════════════════════════════
# The confirmed plan binds
# ═════════════════════════════════════════════════════════════════════════════

class TestPlanBinds:

    def test_a_changed_plan_at_confirm_is_refused(self):
        """The modal showed A keeping its files because B shared them. B moved
        on before confirm, so the fresh answer would delete them — a keep →
        delete change nobody was shown. Refused, nothing removed."""
        client = _Client([_row('A'), _row('B', tracker='u.example')],
                         {'A': [MKV], 'B': [f'{SP}/elsewhere/{REL}.mkv']})
        resp = _remove(client, ['A'], delete_files='auto',
                       plan={'seeds': ['A'], 'groups': {'A': ['A', 'B']}, 'files': {'A': 'keep'}})
        assert resp.status_code == 409
        body = resp.get_json()
        assert body['code'] == 'plan_changed'
        assert body['lost'] == 1
        assert client.removals == []

    def test_a_group_that_grew_is_refused(self):
        """The user's TR5 decision, applied here: a newcomer is new information
        about what a removal touches, and it is seen before acting."""
        client = _Client([_row('A'), _row('B', tracker='u.example')], {'A': [MKV], 'B': [MKV]})
        resp = _remove(client, ['A'], delete_files='auto',
                       plan={'seeds': ['A'], 'groups': {'A': ['A']}, 'files': {'A': 'delete'}})
        assert resp.status_code == 409
        assert resp.get_json()['added'] == 1
        assert client.removals == []

    def test_a_flip_towards_keeping_files_proceeds_and_is_reported(self):
        client = _Client([_row('A'), _row('B', tracker='u.example')], {'A': [MKV], 'B': None})
        resp = _remove(client, ['A'], delete_files='auto',
                       plan={'seeds': ['A'], 'groups': {'A': ['A']}, 'files': {'A': 'delete'}})
        assert resp.status_code == 200
        assert client.files_for('A') is False
        assert resp.get_json()['flipped_to_keep'] == 1

    def test_a_plan_that_never_named_a_decision_does_not_delete(self):
        """A removed torrent the plan says nothing about was never shown with
        its files going, so it is read as shown keeping them."""
        client = _Client([_row('A'), _row('B', size=5000, name='Other')],
                         {'A': [MKV], 'B': [f'{SP}/Other/o.mkv']})
        resp = _remove(client, ['A'], delete_files='auto',
                       plan={'seeds': ['A'], 'groups': {'A': ['A']}, 'files': {}})
        assert resp.status_code == 409
        assert client.removals == []


# ═════════════════════════════════════════════════════════════════════════════
# S09 — say which torrents actually left
# ═════════════════════════════════════════════════════════════════════════════

STANDALONE = [_row('A'), _row('B', size=5000, name='Other'), _row('C', size=9000, name='Third')]
STANDALONE_PATHS = {'A': [MKV], 'B': [f'{SP}/Other/o.mkv'], 'C': [f'{SP}/Third/t.mkv']}


class TestOutcomes:

    def test_the_response_says_which_torrents_actually_left_the_client(self):
        """B never leaves. C is still listed on the first look and gone on the
        second — a client can drop a torrent a moment after its API returns, so
        still-listed-once is asked again, once."""
        client = _Client(STANDALONE, STANDALONE_PATHS, lingering={'B': None, 'C': 1})
        resp = _remove(client, ['A', 'B', 'C'], delete_files=False)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['outcomes'] == {'A': 'removed', 'B': 'still_listed', 'C': 'removed'}
        assert body['removed'] == 2

    def test_a_torrent_on_an_instance_that_did_not_answer_is_unknown(self):
        client = _Client([_row('A'), _row('B', size=5000, name='Other', inst=2, inst_name='second')],
                         {'A': [MKV], 'B': [f'{SP}/Other/o.mkv']}, failed_instances={'second'})
        resp = _remove(client, ['A', 'B'], delete_files=False)
        assert resp.get_json()['outcomes'] == {'A': 'removed', 'B': 'unknown'}

    def test_a_removal_that_failed_part_way_reports_what_left(self):
        """qui posts per instance, and a later `raise_for_status` used to lose
        the earlier instances' outcomes along with the whole request."""
        rows = [_row('A'), _row('B', size=5000, name='Other'), _row('C', size=5000, name='Other')]
        paths = {'A': [MKV], 'B': [f'{SP}/Other/o.mkv'], 'C': [f'{SP}/Other/o.mkv']}
        client = _Client(rows, paths, remove_error=SourceConnectionError('HTTP 500'), remove_error_on=2)
        resp = _remove(client, ['A', 'B'], delete_files='auto')
        assert resp.status_code == 502
        body = resp.get_json()
        assert body['outcomes']['A'] == 'removed'
        assert body['outcomes']['B'] == 'still_listed'
