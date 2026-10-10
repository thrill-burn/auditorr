"""Triage T21 and T22: a pack's quality is every episode's, and *Replace with this pack*.

T21. A row's quality comparison was its largest video's, while Force import
posted every path on the row. A season Sonarr assembled episode by episode (the
box's Small Prophets: E01 and E02 at 2160p, E03–E06 1080p BluRay) can hold a
pack at the same quality as one episode and below another, and Force import then
wrote the 1080p file over the 2160p one.

T22. Consolidating such a season onto one pack the client holds: every episode
of the pack goes over Sonarr's file as a hardlink, a lower release only where
the library file has another link (the better bytes stay on disk), read live.
The replace tests stat real hardlinked files in `tmp_path`.

T23. The plan asked for the pack's file list with a bare `{hash, instance_id}`,
and qui roots each file at the save path it's handed, so on qui every episode
came back `unchecked`. The fixture's listing mock joins names to the save path
the way qui does, which is what catches it.

T24. A lower release over a library file nothing else links is `unseeded`:
replaced only when the confirm says `include_unseeded`.
"""
import os
from unittest.mock import patch

import pytest

import app


def _rec(**over):
    base = {
        'path': 'tv/Show.S01.1080p.WEB-DL-GRP/Show.S01E01.1080p.WEB-DL-GRP.mkv',
        'size': 100, 'status': 'Seeding', 'imported': False, 'excluded': False,
        'hash': 'AAAA', 'instance_id': 1, 'trackers': ['tracker.example'],
        'tracker_health': 'unknown', 'tracker_msg': '',
    }
    base.update(over)
    return base


def _lib(episode, quality, res, arr_id=12, conn='tv'):
    rel = f'Season 01/Show.S01E{episode:02d}.{res}.WEB-DL-LIB.mkv'
    return {'connection_id': conn, 'connection_name': 'TV', 'service': 'sonarr',
            'title': 'Show', 'year': 2019, 'arr_id': arr_id, 'file_id': arr_id * 10 + episode,
            'path': f'/media/Show/{rel}', 'relative_path': rel, 'season_number': 1,
            'title_slug': 'show', 'file_quality_name': quality, 'file_hdr': ''}


def _triage(records, media):
    with patch.object(app, 'db_load_config', return_value={}), \
         patch.object(app, 'db_has_file_results', return_value=True), \
         patch.object(app, 'db_load_file_results', return_value=records), \
         patch.object(app, 'fetch_arr_media_index_result', return_value=(list(media), [])), \
         patch.object(app, 'fetch_arr_all_titles_result', return_value=([], [])), \
         patch.object(app, 'normalize_arr_connections', return_value=[]):
        return app.app.test_client().get('/api/workflows/triage').get_json()['items']


def _pack(h, group, source, *episodes, size=100):
    folder = f'tv/Show.S01.1080p.{source}-{group}'
    return [_rec(hash=h, size=size + n, path=f'{folder}/Show.S01E{n:02d}.1080p.{source}-{group}.mkv')
            for n in episodes]


# ═════════════════════════════════════════════════════════════════════════════
# T21 — the comparison is folded over every episode
# ═════════════════════════════════════════════════════════════════════════════

class TestQualityFold:

    def test_same_on_one_episode_and_lower_on_another_force_imports_only_the_same_one(self):
        """The hazard: E02 is 2160p in the library. Force import used to post
        both paths, because the largest video (E02 here) was not what decided."""
        records = _pack('P', 'GRP', 'WEB-DL', 1, 2)
        media = [_lib(1, 'WEBDL-1080p', '1080p'), _lib(2, 'WEBDL-2160p', '2160p')]
        lib = _triage(records, media)[0]['library']
        assert lib['quality_cmp'] == 'same'
        assert lib['force_paths'] == [records[0]['path']]
        assert lib['quality_spread'] == {'same': 1, 'lower': 1}
        assert set(lib['qualities']) == {'WEBDL-1080p', 'WEBDL-2160p'}

    def test_every_episode_lower_is_lower(self):
        records = _pack('P', 'GRP', 'WEB-DL', 1, 2)
        media = [_lib(1, 'WEBDL-2160p', '2160p'), _lib(2, 'WEBDL-2160p', '2160p')]
        lib = _triage(records, media)[0]['library']
        assert lib['quality_cmp'] == 'lower'
        assert lib['force_paths'] == []
        assert 'quality_spread' not in lib

    def test_any_higher_episode_keeps_the_row_under_higher(self):
        """A rescan lets Sonarr take the upgrade and refuse the rest itself."""
        records = _pack('P', 'GRP', 'WEB-DL', 1, 2)
        media = [_lib(1, 'WEBDL-720p', '720p'), _lib(2, 'WEBDL-2160p', '2160p')]
        assert _triage(records, media)[0]['library']['quality_cmp'] == 'higher'

    def test_a_single_episode_row_reads_as_it_did(self):
        records = [_rec(hash='EP', path='tv/Show.S01E02.1080p.WEB-DL-GRP.mkv')]
        lib = _triage(records, [_lib(2, 'WEBDL-1080p', '1080p')])[0]['library']
        assert lib['quality_cmp'] == 'same'
        assert lib['force_paths'] == [records[0]['path']]


# ═════════════════════════════════════════════════════════════════════════════
# T22 — the season's packs, and the one to pick
# ═════════════════════════════════════════════════════════════════════════════

class TestSeasonPacks:

    def _two_packs(self):
        records = _pack('BLU', 'SbR', 'BluRay', 1, 2) + _pack('WEB', 'Kitsune', 'WEB-DL', 1, 2, 3, 4)
        media = [_lib(n, 'WEBDL-2160p', '2160p') for n in (1, 2, 3, 4)]
        return {i['hash']: i for i in _triage(records, media)}

    def test_two_packs_of_one_season_share_a_key_and_the_better_release_is_the_pick(self):
        """The box's case. Quality first: it's what Sonarr ranks first, so the
        pick is the pack Sonarr keeps rather than one it would upgrade away."""
        items = self._two_packs()
        blu, web = items['BLU'], items['WEB']
        assert blu['season_key'] == web['season_key'] == 'tv|12|1'
        assert blu['season_pick'] == web['season_pick'] == blu['reg']
        assert blu['season_pick_reason'] == 'quality'
        assert 'season_pick_reason' not in web

    def test_the_release_group_is_spelled_as_the_release_spells_it(self):
        items = self._two_packs()
        assert (items['BLU']['release_group'], items['WEB']['release_group']) == ('SbR', 'Kitsune')

    def test_a_season_with_one_pack_has_a_key_and_no_pick(self):
        records = _pack('BLU', 'SbR', 'BluRay', 1, 2)
        item = _triage(records, [_lib(n, 'WEBDL-2160p', '2160p') for n in (1, 2)])[0]
        assert item['season_key'] == 'tv|12|1'
        assert 'season_pick' not in item

    def test_a_tie_on_everything_picks_nothing(self):
        records = _pack('A', 'AAA', 'WEB-DL', 1, 2) + _pack('B', 'BBB', 'WEB-DL', 1, 2)
        items = _triage(records, [_lib(n, 'WEBDL-2160p', '2160p') for n in (1, 2)])
        assert [i['season_pick'] for i in items] == [None, None]

    def test_a_movie_or_an_unmatched_row_is_not_replaceable(self):
        item = _triage([_rec(hash='MV', path='radarr/Movie.2020.1080p.WEB-DL/Movie.2020.1080p.WEB-DL.mkv')], [])[0]
        assert 'season_key' not in item


# ═════════════════════════════════════════════════════════════════════════════
# T22 — the live plan, and the replace
# ═════════════════════════════════════════════════════════════════════════════

HASH = 'B' * 40
CONN = {'id': 'tv', 'service': 'sonarr', 'name': 'TV', 'base_url': 'http://sonarr:8989',
        'api_key': 'k', 'remote_path': '', 'media_path': '', 'local_media_path': ''}


def _base(path):
    return str(path).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]


def _touch(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


@pytest.fixture
def box(tmp_path):
    """A season assembled over time, and a BluRay pack of it in the client.

    E01 library 2160p, hardlinked to its own torrent → a lossless downgrade.
    E02 library 2160p, its only copy                   → skipped.
    E03 library is the pack's own file                 → already.
    E04 no library file                                → added.
    E05 library 720p, its only copy                    → replaced (an upgrade).
    """
    torrents, media = tmp_path / 'torrents', tmp_path / 'media'
    pack = torrents / 'tv' / 'Show.S01.1080p.BluRay-SbR'
    paths = {n: _touch(str(pack / f'Show.S01E{n:02d}.1080p.BluRay-SbR.mkv')) for n in (1, 2, 3, 4, 5)}
    season = media / 'Show' / 'Season 1'
    lib1 = str(season / 'Show.S01E01.2160p.WEB-CAKES.mkv')
    _touch(lib1)
    os.makedirs(torrents / 'tv' / 'single', exist_ok=True)
    os.link(lib1, str(torrents / 'tv' / 'single' / 'Show.S01E01.2160p.WEB-CAKES.mkv'))
    lib2 = _touch(str(season / 'Show.S01E02.2160p.WEB-FLUX.mkv'))
    lib3 = str(season / 'Show.S01E03.1080p.BluRay-SbR.mkv')
    os.link(paths[3], lib3)
    lib5 = _touch(str(season / 'Show.S01E05.720p.WEB-OLD.mkv'))
    index = [
        {'connection_id': 'tv', 'service': 'sonarr', 'arr_id': 12, 'file_id': 11, 'path': lib1, 'file_quality_name': 'WEBDL-2160p'},
        {'connection_id': 'tv', 'service': 'sonarr', 'arr_id': 12, 'file_id': 12, 'path': lib2, 'file_quality_name': 'WEBDL-2160p'},
        {'connection_id': 'tv', 'service': 'sonarr', 'arr_id': 12, 'file_id': 13, 'path': lib3, 'file_quality_name': 'Bluray-1080p'},
        {'connection_id': 'tv', 'service': 'sonarr', 'arr_id': 12, 'file_id': 15, 'path': lib5, 'file_quality_name': 'WEBDL-720p'},
    ]
    episodes = [{'id': 100 + n, 'season': 1, 'episode': n, 'file_id': fid}
                for n, fid in ((1, 11), (2, 12), (3, 13), (4, 0), (5, 15))]
    state = {
        'cfg': {'LOCAL_PATH': str(torrents), 'REMOTE_PATH': ''},
        'listing': [p for p in paths.values()] + [str(pack / 'Show.S01.1080p.BluRay-SbR.nfo')],
        'row': {'reg': f'1:{HASH}', 'hash': HASH, 'name': 'Show.S01.1080p.BluRay-SbR',
                'progress': 1.0, 'completion_on': 1, 'instance_id': 1,
                'save_path': str(torrents / 'tv'), 'content_path': str(pack)},
        'report': {'instances_failed': []},
        'episodes': episodes, 'index': index, 'paths': paths,
    }
    return state


def _listing_like_qui(listing, row):
    """`fetch_torrent_file_paths` as qui answers it (T23).

    qui's file listing names each file relative to the save path, and the
    backend roots it at the `save_path` of the item it's handed. A static map
    hid the bug, because it answered with absolute paths whatever was asked.
    """
    root = (row or {}).get('save_path', '')
    names = None if listing is None else [p[len(root) + 1:] for p in listing]

    def fetch(cfg, items):
        out = {}
        for i in items:
            key = app.sources.registration_key(i.get('instance_id'), i.get('hash'))
            sp = (i.get('save_path') or '').rstrip('/\\')
            # The listing's own spelling under its own root, so a posted path
            # still matches on Windows; `/<name>` under none, as qui did.
            out[key] = None if names is None else [
                p if sp == root else f'{sp}/{n}' for p, n in zip(listing, names)]
        return out
    return fetch


def _patched(box, **over):
    s = {**box, **over}
    rows = s['rows'] if 'rows' in s else ([s['row']] if s['row'] else [])
    return [
        patch.object(app, 'db_load_config', return_value=s['cfg']),
        patch.object(app, 'normalize_arr_connections', side_effect=lambda cfg, **kw: [CONN]),
        patch.object(app.sources, 'list_torrents_detailed', return_value=(rows, s['report'])),
        patch.object(app.sources, 'fetch_torrent_file_paths',
                     side_effect=_listing_like_qui(s['listing'], box['row'])),
        patch.object(app, 'sonarr_series_episodes', return_value=s['episodes']),
        patch.object(app, 'fetch_arr_media_index_result', return_value=(s['index'], [])),
        patch.object(app, 'queue_records_for_item', return_value=s.get('queue', [])),
    ]


def _post(url, box, body=None, **over):
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in _patched(box, **over):
            stack.enter_context(p)
        payload = {'hash': HASH, 'instance_id': 1, 'connection_id': 'tv', 'arr_id': 12, 'season': 1,
                   **(body or {})}
        resp = app.app.test_client().post(url, json=payload)
        return resp.status_code, resp.get_json()


def _plan(box, body=None, **over):
    return _post('/api/workflows/triage/replace_plan', box, body=body, **over)


class TestReplacePlan:

    def test_each_episode_says_what_the_replace_would_do(self, box):
        status, plan = _plan(box)
        assert status == 200
        acts = {e['label']: (e['action'], e.get('reason')) for e in plan['entries']}
        assert acts == {'E01': ('replace', None), 'E02': ('unseeded', None), 'E03': ('already', None),
                        'E04': ('add', None), 'E05': ('replace', None)}
        e01 = next(e for e in plan['entries'] if e['label'] == 'E01')
        assert (e01['cmp'], e01['replaces']['linked'], e01['replaces']['quality']) == ('lower', True, 'WEBDL-2160p')
        e02 = next(e for e in plan['entries'] if e['label'] == 'E02')
        assert (e02['cmp'], e02['replaces']['linked']) == ('lower', False)
        assert plan['downgrades'] == 1
        assert plan['counts']['unseeded'] == 1
        assert plan['release'] == 'Show.S01.1080p.BluRay-SbR'

    def test_the_file_list_is_asked_for_with_the_torrents_save_path(self, box):
        """T23 — the box's Small Prophets plan was six `unchecked`. qui roots each
        file at the save path it's handed, and the plan handed it none."""
        status, plan = _plan(box)
        assert status == 200
        assert not [e for e in plan['entries'] if e.get('reason') == 'unchecked']

    def test_a_hash_with_no_instance_resolves_to_its_one_registration(self, box):
        """The season-pack watch knows only the download's hash (T25)."""
        status, plan = _plan(box, body={'instance_id': None})
        assert status == 200
        assert (plan['reg'], plan['instance_id']) == (f'1:{HASH}', 1)

    def test_a_hash_with_no_instance_on_two_instances_refuses(self, box):
        rows = [box['row'], {**box['row'], 'reg': f'2:{HASH}', 'instance_id': 2, 'instance_name': 'second'}]
        status, body = _plan(box, body={'instance_id': None}, rows=rows)
        assert (status, body['code']) == (409, 'registration_ambiguous')

    def test_a_library_file_holding_more_episodes_than_the_pack_file_is_not_split(self, box):
        """Replacing one file recycles every episode of it (amendment 1)."""
        episodes = [dict(e) for e in box['episodes']]
        episodes[1]['file_id'] = 11            # file 11 now holds E01 and E02
        status, plan = _plan(box, episodes=episodes)
        acts = {e['label']: (e['action'], e.get('reason')) for e in plan['entries']}
        assert acts['E01'] == ('skip', 'split')
        assert acts['E02'] == ('skip', 'split')

    def test_a_library_file_that_cannot_be_stated_is_not_lossless(self, box):
        index = [dict(r) for r in box['index']]
        index[0]['path'] = '/nowhere/Show.S01E01.mkv'
        status, plan = _plan(box, index=index)
        assert next(e for e in plan['entries'] if e['label'] == 'E01')['reason'] == 'unchecked'

    @pytest.mark.parametrize('over, status, code', [
        ({'row': None}, 409, 'not_in_client'),
        ({'row': None, 'report': {'instances_failed': [{'name': 'main'}]}}, 502, 'client_unavailable'),
        ({'listing': None}, 502, 'listing_unavailable'),
        ({'episodes': None}, 502, 'sonarr_unavailable'),
    ])
    def test_what_could_not_be_asked_refuses(self, box, over, status, code):
        got_status, body = _plan(box, **over)
        assert (got_status, body['code']) == (status, code)

    def test_an_unfinished_torrent_refuses(self, box):
        """A file still being written must not be hardlinked into the library."""
        for progress, completion_on in ((0.5, 1), (None, None)):
            row = {**box['row'], 'progress': progress, 'completion_on': completion_on}
            status, body = _plan(box, row=row)
            assert (status, body['code']) == (409, 'incomplete')


class TestReplace:

    def _replace(self, box, paths, reads, extra=None, **over):
        reader = patch.object(app, 'read_arr_file_id', side_effect=reads)
        importer = patch.object(app, 'force_manual_import_by_id', return_value={})
        nudge = patch.object(app, 'nudge_watchdog')
        with reader as r, importer as imp, nudge as n, patch.object(app, '_REPLACE_WAIT', 0):
            status, body = _post('/api/workflows/triage/replace', box,
                                 body={'paths': paths, **(extra or {})}, **over)
        return status, body, r, imp, n

    def test_it_imports_what_was_confirmed_and_is_still_safe_as_hardlinks(self, box):
        p = box['paths']
        # E02 was posted, but it's unseeded and the confirm didn't include
        # unseeded episodes: dropped, never sent.
        reads = [[[101, 11], [104, 0]], [[101, 91], [104, 94]]]
        status, body, reader, imp, nudge = self._replace(box, [p[1], p[2], p[4]], reads)
        assert status == 200
        assert (body['replaced'], body['pending'], body['dropped']) == (['E01', 'E04'], [], ['E02'])
        kw = imp.call_args.kwargs
        assert kw['import_mode'] == 'Copy'
        assert kw['media_folder_fallback'] is False
        assert sorted(_base(x) for x in kw['only_paths']) == sorted(_base(x) for x in (p[1], p[4]))
        # The torrent's own folder, not one episode's file.
        assert _base(kw['download_folder']) == 'Show.S01.1080p.BluRay-SbR'
        assert kw['only_episode_ids'] == [101, 104]
        assert kw['whole_episode_sets'] == [[101]]
        assert kw['download_id'] is None
        assert reader.call_args_list[0].kwargs['episode_ids'] == [101, 104]
        nudge.assert_called_once()

    def test_an_unseeded_episode_is_replaced_only_when_the_switch_says_so(self, box):
        """T24 — the user's trade: E02's only copy goes, and E02 gains a seed."""
        p = box['paths']
        reads = [[[102, 12]], [[102, 92]]]
        status, body, _, imp, _ = self._replace(box, [p[2]], reads, extra={'include_unseeded': True})
        assert (status, body['replaced'], body['dropped']) == (200, ['E02'], [])
        kw = imp.call_args.kwargs
        assert [_base(x) for x in kw['only_paths']] == [_base(p[2])]
        assert kw['whole_episode_sets'] == [[102]]

    def test_a_truthy_switch_that_is_not_true_is_not_consent(self, box):
        status, body, _, imp, _ = self._replace(box, [box['paths'][2]], [], extra={'include_unseeded': 'yes'})
        assert (status, body['code'], body['dropped']) == (409, 'nothing_to_replace', ['E02'])
        imp.assert_not_called()

    def test_a_pack_sonarr_is_tracking_is_imported_against_its_download(self, box):
        """T25 — a grabbed pack sits in Sonarr's queue as importBlocked, and only
        an import naming the download closes it."""
        queue = [{'downloadId': HASH.upper(), 'seriesId': 12}]
        reads = [[[101, 11]], [[101, 91]]]
        _, _, _, imp, _ = self._replace(box, [box['paths'][1]], reads, queue=queue)
        assert imp.call_args.kwargs['download_id'] == HASH.upper()

    def test_a_replace_from_a_season_pack_watch_closes_the_watch(self, box):
        watch = {'status': 'ready', 'source': 'season_pack', 'message': '', 'completed_at': 1.0}
        reads = [[[101, 11]], [[101, 91]]]
        with patch.dict(app._import_watches, {'w1': watch}):
            self._replace(box, [box['paths'][1]], reads, extra={'watch_id': 'w1'})
        assert watch['status'] == 'done'
        assert watch['message'] == 'Replaced 1 library file'

    def test_an_episode_whose_file_did_not_change_is_pending_not_replaced(self, box):
        p = box['paths']
        reads = [[[101, 11]], [[101, 11]]]
        status, body, _, _, nudge = self._replace(box, [p[1]], reads)
        assert (body['replaced'], body['pending']) == ([], ['E01'])
        nudge.assert_not_called()

    def test_a_baseline_that_cannot_be_read_sends_nothing(self, box):
        status, body, _, imp, _ = self._replace(box, [box['paths'][1]], OSError('timed out'))
        assert (status, body['code']) == (502, 'sonarr_unavailable')
        imp.assert_not_called()

    def test_nothing_still_safe_sends_nothing(self, box):
        status, body, reader, imp, _ = self._replace(box, [box['paths'][2], box['paths'][3]], [])
        assert (status, body['code'], body['dropped']) == (409, 'nothing_to_replace', ['E02', 'E03'])
        imp.assert_not_called()
        reader.assert_not_called()

    def test_an_empty_confirmation_is_refused(self, box):
        status, body, _, imp, _ = self._replace(box, [], [])
        assert (status, body['code']) == (400, 'selection_required')
        imp.assert_not_called()
