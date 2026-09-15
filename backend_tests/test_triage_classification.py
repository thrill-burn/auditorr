"""Triage's library-matching block.

Phase 9 item 1 opens with this file properly — T2's instance ranking, T5's
per-file verdict spread and the rest of the 40 lines every review independently
flagged as the least-tested, highest-consequence code in the repo. What is here
now is the one assertion Phase 4a adds, written with the change it belongs to:

**an arr that did not answer must never produce `not_in_library`.**

That verdict means "no arr has ever heard of this", and its UI copy ends
"junk can be deleted". A not-imported torrent has no hardlink anywhere by
definition, so its files are the only copy — this is the verdict that cost a
field reporter 33 GB when title matching missed (T15), and an unreachable arr
reaches it by a shorter road: `fetch_arr_media_index` returns `[]` on failure,
`arr_configured` is derived from the config rather than the fetch so it stays
true, and nothing else on the response said anything had gone wrong.
"""
import unittest
from unittest.mock import patch

import app
import arr
import audit
from sources import _qbit, _qui


def _rec(**over):
    base = {
        'path': 'radarr/Movie.2020.1080p.WEB-DL/Movie.2020.1080p.WEB-DL.mkv',
        'size': 100, 'status': 'Seeding', 'imported': False, 'excluded': False,
        'hash': 'AAAA', 'instance_id': 1, 'trackers': ['tracker.example'],
        'tracker_health': 'unknown', 'tracker_msg': '',
    }
    base.update(over)
    return base


def _triage(records, *, index_errors=(), title_errors=(), media=(), titles=(), conns=()):
    """GET /api/workflows/triage with the arr layer's health stated explicitly."""
    with patch.object(app, 'db_load_config', return_value={}), \
         patch.object(app, 'db_has_file_results', return_value=True), \
         patch.object(app, 'db_load_file_results', return_value=records), \
         patch.object(app, 'fetch_arr_media_index_result', return_value=(list(media), list(index_errors))), \
         patch.object(app, 'fetch_arr_all_titles_result', return_value=(list(titles), list(title_errors))), \
         patch.object(app, 'normalize_arr_connections', return_value=list(conns)), \
         patch.object(app.sources, 'fetch_torrent_details',
                      side_effect=AssertionError('phase 1 must not call the client')):
        return app.app.test_client().get('/api/workflows/triage').get_json()


def _down(name='Main Sonarr', cid='s1'):
    return {'connection_id': cid, 'name': name, 'service': 'sonarr',
            'partial': False, 'message': 'timed out'}


class ArrUnreachableVerdictTests(unittest.TestCase):

    def test_an_unreachable_arr_never_produces_not_in_library(self):
        report = _triage([_rec()], index_errors=[_down()], title_errors=[_down()])
        self.assertEqual([i['verdict'] for i in report['items']], ['library_unknown'])

    def test_the_alternatives_carry_the_gate_too(self):
        """The client re-resolves every verdict from these after live tracker
        verification, so a gate applied only to the phase-1 verdict would be
        undone the moment the batch came back."""
        report = _triage([_rec()], index_errors=[_down()])
        alts = report['items'][0]['verdict_alternatives']
        self.assertEqual(alts['working'], 'library_unknown')
        self.assertEqual(alts['other'], 'library_unknown')
        self.assertEqual(alts['unregistered'], 'unregistered')

    def test_a_partial_read_counts_as_unreachable(self):
        """Sonarr needs one call per series, so the likely failure is that some
        of the library came back. The missing series are exactly the ones whose
        episodes would read as "no arr has heard of this"."""
        partial = {'connection_id': 's1', 'name': 'TV', 'service': 'sonarr',
                   'partial': True, 'failed': 12, 'total': 400,
                   'message': '12 of 400 series could not be read'}
        report = _triage([_rec()], index_errors=[partial])
        self.assertEqual(report['items'][0]['verdict'], 'library_unknown')

    def test_the_failure_is_named_on_the_response(self):
        report = _triage([_rec()], index_errors=[_down(name='TV')], title_errors=[_down(name='TV')])
        self.assertEqual(len(report['arr_errors']), 2)
        self.assertEqual(report['arr_errors'][0]['name'], 'TV')

    def test_a_healthy_arr_still_says_not_in_library(self):
        """The gate must not cost the working path — an arr that answered and
        has never heard of the title is a real verdict, and it is the one the
        exclusion suggestions and the delete button are for."""
        report = _triage([_rec()])
        self.assertEqual([i['verdict'] for i in report['items']], ['not_in_library'])
        self.assertEqual(report['arr_errors'], [])

    def test_a_positive_match_is_not_suppressed(self):
        """Only the verdict derived purely from silence is gated. An instance
        that *did* answer and holds the title still reports `superseded`, even
        while a different instance is down — suppressing that would throw away
        evidence auditorr actually has."""
        media = [{'connection_id': 'r1', 'connection_name': 'main', 'service': 'radarr',
                  'title': 'Movie', 'year': 2020, 'arr_id': 7, 'file_id': 1,
                  'path': '/media/movies/Movie (2020)/Movie.2020.1080p.WEB-DL.mkv',
                  'relative_path': 'Movie.2020.1080p.WEB-DL.mkv',
                  'title_slug': 'movie', 'file_quality_name': 'WEBDL-1080p', 'file_hdr': ''}]
        report = _triage([_rec()], media=media, index_errors=[_down()])
        self.assertEqual(report['items'][0]['verdict'], 'superseded')
        self.assertEqual(report['items'][0]['library']['arr_id'], 7)


# ═════════════════════════════════════════════════════════════════════════════
# T2 — rank the rows a title match produced; say so when two instances hold it
# ═════════════════════════════════════════════════════════════════════════════

def _episode_row(conn, name, quality, res, arr_id, episode=2):
    rel = f'Season 01/Show.S01E{episode:02d}.{res}.WEB-DL.mkv'
    return {'connection_id': conn, 'connection_name': name, 'service': 'sonarr',
            'title': 'Show', 'year': 2019, 'arr_id': arr_id, 'file_id': arr_id * 10 + episode,
            'path': f'/{conn}/Show/{rel}', 'relative_path': rel, 'season_number': 1,
            'title_slug': 'show', 'file_quality_name': quality, 'file_hdr': ''}


class TriageInstanceRankingTests(unittest.TestCase):
    """T2. `lib_rows[0]` and `arr_title_hit` took whichever row a title key pooled
    first, from any connection. With a 1080p and a 4K Sonarr both holding an
    episode, the 4K answered first, the comparison came back `lower`, and that
    pre-selected the whole cross-seed group for deletion — with rescan and force
    import pointed at the wrong instance. Unreachable on the reference box (M4:
    one instance per service), so this is tested on the payload."""

    EP = 'tv/Show.S01E02.1080p.WEB-DL/Show.S01E02.1080p.WEB-DL.mkv'

    def test_two_instances_holding_the_title_are_ranked_and_flagged(self):
        media = [_episode_row('tv4k', 'TV 4K', 'WEBDL-2160p', '2160p', 900),
                 _episode_row('tv', 'TV', 'WEBDL-1080p', '1080p', 12)]
        item = _triage([_rec(hash='EP', path=self.EP)], media=media)['items'][0]
        lib = item['library']
        self.assertEqual(item['verdict'], 'superseded')
        self.assertEqual((lib['connection_id'], lib['quality_cmp']), ('tv', 'same'))
        self.assertEqual(lib['connection_name'], 'TV')
        self.assertEqual(lib['others'], [{'connection_id': 'tv4k', 'name': 'TV 4K',
                                          'quality_name': 'WEBDL-2160p', 'quality_cmp': 'lower'}])

    def test_a_single_instance_answer_is_unchanged_by_ranking(self):
        """The 4b property: the ranker is stable, so rows nothing distinguishes
        keep their order and a single-instance install gets `[0]`'s answer."""
        media = [
            {'connection_id': 'r1', 'connection_name': 'Films', 'service': 'radarr', 'title': 'Movie',
             'year': 2020, 'arr_id': 7, 'file_id': 1, 'path': '/m/Movie (2020)/Movie.mkv',
             'relative_path': 'Movie.mkv', 'title_slug': 'movie', 'file_quality_name': 'WEBDL-1080p', 'file_hdr': ''},
            {'connection_id': 'r1', 'connection_name': 'Films', 'service': 'radarr', 'title': 'Movie',
             'year': 1990, 'arr_id': 8, 'file_id': 2, 'path': '/m/Movie (1990)/Movie.mkv',
             'relative_path': 'Movie.mkv', 'title_slug': 'movie-1990', 'file_quality_name': 'WEBDL-1080p', 'file_hdr': ''},
        ]
        item = _triage([_rec(hash='MV', path='radarr/Movie.1080p.WEB-DL/Movie.1080p.WEB-DL.mkv')],
                       media=media)['items'][0]
        self.assertEqual(item['library']['arr_id'], 7)
        self.assertFalse(item['library'].get('others'))

    def test_an_import_pending_title_is_gated_to_the_right_service(self):
        """`arr_title_hit` attempted no service preference at all, so an episode
        of a series absent from Sonarr was attributed to the same-titled film —
        and Rescan went to Radarr alone."""
        ep = 'tv/Fargo.S05E01.1080p.WEB-DL/Fargo.S05E01.1080p.WEB-DL.mkv'
        film = {'service': 'radarr', 'connection_id': 'r1', 'arr_id': 42, 'title': 'Fargo',
                'title_slug': 'fargo', 'year': 1996, 'has_file': False, 'alt_titles': []}
        series = {'service': 'sonarr', 'connection_id': 's1', 'arr_id': 7, 'title': 'Fargo',
                  'title_slug': 'fargo', 'year': 2014, 'has_file': False, 'alt_titles': []}

        item = _triage([_rec(hash='EP', path=ep)], titles=[film, series])['items'][0]
        self.assertEqual(item['verdict'], 'import_pending')
        self.assertEqual((item['library']['service'], item['library']['arr_id']), ('sonarr', 7))

        item = _triage([_rec(hash='EP', path=ep)], titles=[film])['items'][0]
        self.assertEqual(item['verdict'], 'not_in_library')
        self.assertIsNone(item['library'])


# ═════════════════════════════════════════════════════════════════════════════
# T5 — a row is honest about its spread, and about its size
# ═════════════════════════════════════════════════════════════════════════════

PACK = 'tv/Show.S01.1080p.WEB-DL'


class TriageVerdictSpreadTests(unittest.TestCase):
    """T5. The row's verdict was the largest video's. For a season pack that is
    one episode standing in for all of them."""

    def _pack(self):
        media = [_episode_row('tv', 'TV', 'WEBDL-1080p', '1080p', 12, episode=1)]
        titles = [{'service': 'sonarr', 'connection_id': 'tv', 'arr_id': 12, 'title': 'Show',
                   'title_slug': 'show', 'year': 2019, 'has_file': True, 'alt_titles': []}]
        records = [_rec(hash='PACK', size=200, path=f'{PACK}/Show.S01E01.1080p.WEB-DL.mkv'),
                   _rec(hash='PACK', size=100, path=f'{PACK}/Show.S01E02.1080p.WEB-DL.mkv'),
                   _rec(hash='PACK', size=1, path=f'{PACK}/Show.S01.1080p.WEB-DL.nfo')]
        return _triage(records, media=media, titles=titles)['items'][0]

    def test_a_multi_file_torrent_states_its_verdict_spread(self):
        """E01 is in the library (superseded), E02 is managed but missing
        (import pending). The row takes the verdict whose action deletes least,
        and live verify folds to the same answer."""
        item = self._pack()
        self.assertEqual(item['verdict'], 'import_pending')
        self.assertEqual(item['verdict_spread'], {'superseded': 1, 'import_pending': 1})
        self.assertEqual(item['verdict_alternatives']['working'], 'import_pending')
        self.assertEqual(item['verdict_alternatives']['other'], 'import_pending')

    def test_files_that_agree_carry_no_spread(self):
        titles = [{'service': 'sonarr', 'connection_id': 'tv', 'arr_id': 12, 'title': 'Show',
                   'title_slug': 'show', 'year': 2019, 'has_file': False, 'alt_titles': []}]
        records = [_rec(hash='PACK', size=200, path=f'{PACK}/Show.S01E01.1080p.WEB-DL.mkv'),
                   _rec(hash='PACK', size=100, path=f'{PACK}/Show.S01E02.1080p.WEB-DL.mkv')]
        item = _triage(records, titles=titles)['items'][0]
        self.assertEqual(item['verdict'], 'import_pending')
        self.assertFalse(item.get('verdict_spread'))

    def test_a_rows_size_is_the_torrents_not_the_visible_subsets(self):
        """The number beside a delete button is what the delete touches: the
        whole torrent. Phase 1 has no client call, so the size comes live from
        verify — qBittorrent's `size`, the files selected for download, since an
        unselected file was never on disk to delete — and the file count off the
        audit, which walked every file of the torrent."""
        class _T:
            def __init__(self, h, size):
                self.hash, self.size, self.uploaded, self.ratio = h, size, 0, 0.0
                self.seeding_time, self.added_on = 60, 1_700_000_000

        class _Qbt:
            def auth_log_in(self):
                return True

            def torrents_info(self, torrent_hashes=None, **_kw):
                return [t for t in (_T('PART', 72_000),) if torrent_hashes is None or t.hash in torrent_hashes]

            def torrents_trackers(self, torrent_hash=None):
                return []

        with patch.object(_qbit.qbittorrentapi, 'Client', return_value=_Qbt()):
            details = _qbit.fetch_torrent_details({}, [{'hash': 'PART'}])
        self.assertEqual(details['PART']['size'], 72_000)

        sess = _QuiSession({1: [{'hash': 'PART', 'size': 72_000, 'total_size': 90_000}]})
        _forget_qui_listings()
        with patch.object(_qui, '_session', return_value=sess):
            details = _qui.fetch_torrent_details(_QUI_CFG, [{'hash': 'PART', 'instance_id': 1}])
        _forget_qui_listings()
        self.assertEqual(details['PART']['size'], 72_000)

        records = [_rec(hash='PART', path='tv/P/a.mkv', size=40),
                   _rec(hash='PART', path='tv/P/b.mkv', size=32, imported=True, tracker_health='working')]
        audit._mark_whole_torrents(records, [])
        item = _triage([r for r in records if audit._is_triage_relevant(r)])['items'][0]
        self.assertEqual((item['file_count'], item['total_size']), (1, 40))
        self.assertEqual(item['torrent_files'], 2)


# ═════════════════════════════════════════════════════════════════════════════
# T8 — one cap, stated in numbers
# ═════════════════════════════════════════════════════════════════════════════

class TriageCapTests(unittest.TestCase):

    def test_both_caps_are_reported(self):
        """The cap was applied to the not-imported groups and again to dead
        registrations, `truncated` reflected only the first, and the banner said
        "500" whatever the cap was. One cap now applies to the combined list."""
        records = [_rec(hash=f'NI{n}', size=size, path=f'radarr/N{n}/n.mkv')
                   for n, size in ((1, 300), (2, 200), (3, 100))]
        records.append(_rec(hash='LIVE', size=50, path='radarr/Live/l.mkv', imported=True,
                            tracker_health='working',
                            dead_siblings=[{'hash': f'REG{n}', 'instance_id': 1} for n in (1, 2, 3)]))
        with patch.object(app, '_TRIAGE_GROUP_CAP', 2):
            body = _triage(records)
        self.assertEqual(len(body['items']), 2)
        self.assertEqual((body['shown'], body['total']), (2, 6))
        self.assertTrue(body['truncated'])
        self.assertNotIn('counts', body, 'T9: nothing read it')


# ═════════════════════════════════════════════════════════════════════════════
# T7 + T10 — the verify phase: one listing per instance, and gone means gone
# ═════════════════════════════════════════════════════════════════════════════

_QUI_CFG = {'QUI_HOST': 'http://qui:7476', 'QUI_API_KEY': 'k'}
_INSTANCES = [{'id': 1, 'name': 'main', 'connected': True, 'hasLocalFilesystemAccess': True},
              {'id': 2, 'name': 'second', 'connected': True, 'hasLocalFilesystemAccess': True}]


def _forget_qui_listings():
    forget = getattr(_qui, '_forget_detail_listings', None)
    if forget:
        forget()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _QuiSession:
    """qui, as far as `fetch_torrent_details` and `remove_torrents` touch it."""
    headers = {}

    def __init__(self, torrents, fail=(), instances=None):
        self.torrents = {k: [dict(t) for t in v] for k, v in torrents.items()}
        self.fail = set(fail)
        self.instances = instances or [i for i in _INSTANCES if i['id'] in torrents or i['id'] in fail]

    def get(self, url, params=None, **_kw):
        if url.endswith('/api/instances'):
            return _Resp(self.instances)
        if url.endswith('/trackers'):
            return _Resp([])
        inst = int(url.split('/api/instances/')[1].split('/')[0])
        if url.endswith('/torrents'):
            if inst in self.fail:
                raise ConnectionError('instance down')
            rows = self.torrents.get(inst, [])
            page = (params or {}).get('page', 0)
            return _Resp({'torrents': rows if page == 0 else [], 'total': len(rows)})
        raise AssertionError(url)

    def post(self, url, json=None, **_kw):
        inst = int(url.split('/api/instances/')[1].split('/')[0])
        gone = set((json or {}).get('hashes') or [])
        self.torrents[inst] = [t for t in self.torrents.get(inst, []) if t['hash'] not in gone]
        return _Resp({})


class TriageVerifyListingTests(unittest.TestCase):

    def setUp(self):
        _forget_qui_listings()

    def tearDown(self):
        _forget_qui_listings()

    def _details(self, sess, items):
        with patch.object(_qui, '_session', return_value=sess):
            return _qui.fetch_torrent_details(_QUI_CFG, items)

    def test_one_qui_listing_per_instance_serves_a_whole_page(self):
        """T7. A page at the cap is ⌈rows / 150⌉ batches, and every batch listed
        the whole instance again. qbit filters server-side and is unaffected."""
        sess = _QuiSession({1: [{'hash': h} for h in ('AAAA', 'BBBB', 'CCCC')]})
        with patch.object(_qui, '_fetch_all_torrents', wraps=_qui._fetch_all_torrents) as listing:
            for h in ('AAAA', 'BBBB', 'CCCC'):
                self.assertIn(h, self._details(sess, [{'hash': h, 'instance_id': 1}]))
        self.assertEqual(listing.call_count, 1)

    def test_a_removal_forgets_the_listing(self):
        sess = _QuiSession({1: [{'hash': 'AAAA'}, {'hash': 'BBBB'}]})
        with patch.object(_qui, '_session', return_value=sess), \
             patch.object(_qui, '_fetch_all_torrents', wraps=_qui._fetch_all_torrents) as listing:
            _qui.fetch_torrent_details(_QUI_CFG, [{'hash': 'AAAA', 'instance_id': 1}])
            _qui.remove_torrents(_QUI_CFG, [{'hash': 'AAAA', 'instance_id': 1}], delete_files=False)
            out = _qui.fetch_torrent_details(_QUI_CFG, [{'hash': 'AAAA', 'instance_id': 1}])
        self.assertEqual(out['AAAA'], {'found': False})
        self.assertEqual(listing.call_count, 2)

    def test_a_torrent_added_since_the_listing_is_not_called_gone(self):
        sess = _QuiSession({1: [{'hash': 'AAAA'}]})
        self._details(sess, [{'hash': 'AAAA', 'instance_id': 1}])
        sess.torrents[1].append({'hash': 'DDDD'})
        out = self._details(sess, [{'hash': 'DDDD', 'instance_id': 1}])
        self.assertNotEqual(out.get('DDDD'), {'found': False})
        self.assertIn('DDDD', out)

    def test_a_hash_the_client_no_longer_lists_is_found_false(self):
        """T10. A torrent removed since the audit stayed on the page under its
        audit-time verdict, marked verified, with a delete that removed nothing."""
        class _T:
            def __init__(self, h):
                self.hash, self.size, self.uploaded, self.ratio = h, 1, 0, 0.0
                self.seeding_time, self.added_on = 60, 1_700_000_000

        class _Qbt:
            def auth_log_in(self):
                return True

            def torrents_info(self, torrent_hashes=None, **_kw):
                return [t for t in (_T('AAAA'),) if t.hash in (torrent_hashes or [])]

            def torrents_trackers(self, torrent_hash=None):
                return []

        with patch.object(_qbit.qbittorrentapi, 'Client', return_value=_Qbt()):
            out = _qbit.fetch_torrent_details({}, [{'hash': 'AAAA'}, {'hash': 'BBBB'}])
        self.assertEqual(out['BBBB'], {'found': False})
        self.assertNotEqual(out['AAAA'].get('found'), False)

        out = self._details(_QuiSession({1: [{'hash': 'AAAA'}]}),
                            [{'hash': 'AAAA', 'instance_id': 1}, {'hash': 'BBBB', 'instance_id': 1}])
        self.assertEqual(out['BBBB'], {'found': False})

    def test_a_hash_on_an_instance_that_did_not_answer_is_not_found_false(self):
        """The trap: qui logs and carries on when one instance's listing fails,
        so a missing entry there is "could not ask", never "gone"."""
        sess = _QuiSession({1: [{'hash': 'AAAA'}]}, fail={2}, instances=_INSTANCES)
        out = self._details(sess, [{'hash': 'BBBB', 'instance_id': 2},
                                   {'hash': 'CCCC', 'instance_id': None}])
        self.assertNotEqual(out.get('BBBB', {}).get('found'), False)
        self.assertNotEqual(out.get('CCCC', {}).get('found'), False)


# ═════════════════════════════════════════════════════════════════════════════
# S11 — arr rows and their errors are one result
# ═════════════════════════════════════════════════════════════════════════════

def _forget_arr_caches():
    arr._arr_media_index_cache.update({'data': None, 'ts': 0, 'errors': [], 'roots': {}, 'snapshot': None})
    arr._arr_titles_cache.update({'data': None, 'ts': 0, 'errors': [], 'snapshot': None})


class ArrResultPairingTests(unittest.TestCase):

    def tearDown(self):
        _forget_arr_caches()

    def test_arr_errors_describe_the_rows_they_came_with(self):
        """The review's evidence probe 2, through the endpoint. Under 8 gthreads
        a successful refresh can land between this request's failed fetch and
        its errors read: the request then holds `[]` rows and no errors, the
        `library_unknown` gate reads that as healthy, and `not_in_library` —
        the verdict whose files are the only copy — is reachable from silence."""
        radarr = {'id': 'r1', 'service': 'radarr', 'name': 'Films', 'base_url': 'http://radarr',
                  'api_key': 'k', 'remote_path': ''}

        def refresh_then_read():
            with patch.object(arr, '_fetch_radarr_media', return_value=([], None)):
                arr.fetch_arr_media_index({}, force=True)
            return arr.arr_media_index_errors()

        _forget_arr_caches()
        with patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app, 'db_has_file_results', return_value=True), \
             patch.object(app, 'db_load_file_results', return_value=[_rec()]), \
             patch.object(arr, 'normalize_arr_connections', return_value=[radarr]), \
             patch.object(app, 'normalize_arr_connections', return_value=[radarr]), \
             patch.object(arr, '_fetch_radarr_media', side_effect=OSError('timed out')), \
             patch.object(arr, '_arr_get', return_value=[]), \
             patch('app.arr_media_index_errors', side_effect=refresh_then_read, create=True):
            report = app.app.test_client().get('/api/workflows/triage').get_json()
        self.assertEqual([i['verdict'] for i in report['items']], ['library_unknown'])
        self.assertTrue(report['arr_errors'])


# ═════════════════════════════════════════════════════════════════════════════
# One video extension set
# ═════════════════════════════════════════════════════════════════════════════

class TriageVideoExtensionTests(unittest.TestCase):

    def test_an_m4v_file_is_not_offered_as_a_junk_extension(self):
        """`_VIDEO_EXTS` was narrower than `arr.VIDEO_EXTENSIONS`, and anything
        outside it was a sidecar — so a `.m4v` film was offered as an "`.m4v`
        files" exclusion, one click from hiding it."""
        self.assertEqual(app._classify_triage_junk('movies/Film.2020/Film.2020.1080p.m4v'), (None, None))
        report = _triage([_rec(path='radarr/Film.2020.1080p.WEB-DL/Film.2020.1080p.WEB-DL.m4v')])
        self.assertEqual(report['suggestions'], [])


if __name__ == '__main__':
    unittest.main()
