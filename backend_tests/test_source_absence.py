"""R1 — absence must be reportable.

The reviews found the same root cause five times over: both source backends
catch everything and return an empty collection, so no caller can tell "the
client says there is nothing" from "the client could not be asked". Cleanup then
offers live files for `rm`, Trumped deletes payloads out from under live
registrations, and the health score, change log and zombie ladders all read a
fiction.

Every test here drives the real decision code with only the HTTP layer faked —
the whole point of R7 is that what was covered was the plumbing and what was
uncovered was every line that decides what gets deleted.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sources
from sources import _qbit, _qui
import audit
from audit import (
    source_plausibility, _guard_scan, _SourceAnomaly,
    _build_duplicate_map, _is_not_imported_torrent, _is_triage_relevant,
    count_triage_items, _walk_directory,
)


# ---------------------------------------------------------------------------
# Fakes — just enough qbittorrentapi / requests surface to drive the real code
# ---------------------------------------------------------------------------

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeTorrent(_Obj):
    pass


class _FakeQbtClient:
    """A qBittorrent client whose per-torrent calls can be made to fail."""

    def __init__(self, torrents, files, fail_files=(), fail_trackers=()):
        self._torrents      = torrents
        self._files         = files
        self._fail_files    = set(fail_files)
        self._fail_trackers = set(fail_trackers)

    def auth_log_in(self):
        return True

    def torrents_info(self, torrent_hashes=None, **kw):
        if torrent_hashes is None:
            return list(self._torrents)
        wanted = set(torrent_hashes)
        return [t for t in self._torrents if t.hash in wanted]

    def torrents_trackers(self, torrent_hash=None):
        if torrent_hash in self._fail_trackers:
            raise RuntimeError('tracker call timed out')
        return [_Obj(url='http://tracker.example/announce', status=2, msg='')]

    def torrents_files(self, torrent_hash=None):
        if torrent_hash in self._fail_files:
            raise RuntimeError('file listing timed out')
        return [_Obj(name=n) for n in self._files.get(torrent_hash, [])]


def _qbit_cfg():
    return {'QB_HOST': 'http://localhost:8080', 'QB_USER': 'u', 'QB_PASS': 'p',
            'LOCAL_PATH': '/data/torrents', 'REMOTE_PATH': '/data/torrents'}


def _torrent(h, name, save_path='/data/torrents/movies', size=100, **kw):
    """A finished, seeding torrent as qBittorrent actually reports one.

    `progress` and `completion_on` are part of that — the fake used to omit
    them, which made every fixture torrent read `completion_unknown` once the
    completion flag existed. Override either to build an unfinished or a
    rechecked torrent.
    """
    fields = dict(hash=h, name=name, save_path=save_path, size=size,
                  state='uploading', uploaded=10, category='movies',
                  seeding_time=3600, progress=1.0, completion_on=1789000000,
                  content_path=os.path.join(save_path, name))
    fields.update(kw)
    return _FakeTorrent(**fields)


# ---------------------------------------------------------------------------
# C1 — a failed file listing is not "this torrent has no files"
# ---------------------------------------------------------------------------

class QbitListingFailureTests(unittest.TestCase):
    """`_fetch_torrent_data`'s bare `except` → `files = []` in `fetch_file_map`.

    The files then never enter the map, `_walk_directory` finds them on disk
    with nothing claiming them, and `status` stays at its 'Orphaned' default —
    for a torrent that is live and seeding.
    """

    def _run(self, *, fail_files=(), fail_trackers=(), isfile=(), isdir=(), walk=None):
        client = _FakeQbtClient(
            torrents=[_torrent('aaa', 'Alpha'), _torrent('bbb', 'Bravo')],
            files={'aaa': ['Alpha/a.mkv'], 'bbb': ['Bravo/b.mkv']},
            fail_files=fail_files, fail_trackers=fail_trackers)
        with patch.object(_qbit.qbittorrentapi, 'Client', return_value=client), \
             patch.object(sources.os.path, 'isfile', lambda p: p in isfile), \
             patch.object(sources.os.path, 'isdir', lambda p: p in isdir), \
             patch.object(sources.os, 'walk', lambda root: (walk or {}).get(root, [])):
            return _qbit.fetch_file_map(_qbit_cfg())

    def test_a_healthy_scan_reports_no_failures(self):
        file_map, _, _, report = self._run()
        self.assertEqual(len(file_map), 2)
        self.assertEqual(report['listing_failures'], 0)
        self.assertFalse(report['partial'])
        self.assertEqual(report['torrent_count'], 2)
        self.assertEqual(report['file_map_size'], 2)

    def test_a_failed_listing_is_counted_not_swallowed(self):
        _, _, _, report = self._run(fail_files={'bbb'})
        self.assertEqual(report['listing_failures'], 1)
        self.assertTrue(report['partial'])

    def test_a_failed_listing_falls_back_to_disk_and_claims_the_files(self):
        """The ported qui fallback. Without it there is no way to attribute the
        failure to specific paths — the paths are what failed to arrive — so the
        files silently default to orphaned."""
        root = os.path.join('/data/torrents/movies', 'Bravo')
        _, _, _, report = self._run(
            fail_files={'bbb'}, isdir={root},
            walk={root: [(root, [], ['b.mkv', 'b.nfo'])]})
        self.assertEqual(report['listing_recovered'], 1)
        self.assertEqual(report['listing_unresolved'], 0)

    def test_a_recovered_torrent_is_claimed_rather_than_left_to_read_as_orphaned(self):
        root = os.path.join('/data/torrents/movies', 'Bravo')
        file_map, _, _, _ = self._run(
            fail_files={'bbb'}, isdir={root},
            walk={root: [(root, [], ['b.mkv'])]})
        self.assertIn(os.path.join(root, 'b.mkv'), file_map)
        self.assertEqual(file_map[os.path.join(root, 'b.mkv')]['hash'], 'bbb')

    def test_a_single_file_torrent_is_recovered_too(self):
        root = os.path.join('/data/torrents/movies', 'Bravo')
        file_map, _, _, report = self._run(fail_files={'bbb'}, isfile={root})
        self.assertIn(root, file_map)
        self.assertEqual(report['listing_recovered'], 1)

    def test_a_failure_with_nothing_on_disk_stays_unresolved(self):
        _, _, _, report = self._run(fail_files={'bbb'})
        self.assertEqual(report['listing_recovered'], 0)
        self.assertEqual(report['listing_unresolved'], 1)
        self.assertTrue(report['notes'])

    def test_a_tracker_failure_no_longer_discards_a_good_file_list(self):
        """One bare `except` covered both API calls, so a tracker timeout threw
        away a file listing that had arrived perfectly well — and the torrent's
        files read as orphans on the strength of an unrelated failure."""
        file_map, _, _, report = self._run(fail_trackers={'bbb'})
        self.assertEqual(report['listing_failures'], 0)
        self.assertEqual(len(file_map), 2)
        self.assertEqual(
            file_map[os.path.join('/data/torrents/movies', 'Bravo/b.mkv')]['tracker_health'],
            'unknown')


# ---------------------------------------------------------------------------
# TR1a — fetch_torrent_file_paths must distinguish None from []
# ---------------------------------------------------------------------------

class QbitFilePathsTests(unittest.TestCase):
    def test_a_failed_listing_answers_none_not_an_empty_list(self):
        client = _FakeQbtClient(
            torrents=[_torrent('aaa', 'Alpha'), _torrent('bbb', 'Bravo')],
            files={'aaa': ['Alpha/a.mkv'], 'bbb': []},
            fail_files={'bbb'})
        with patch.object(_qbit.qbittorrentapi, 'Client', return_value=client):
            out = _qbit.fetch_torrent_file_paths(
                _qbit_cfg(), [{'hash': 'aaa', 'save_path': '/data/torrents/movies'},
                              {'hash': 'bbb', 'save_path': '/data/torrents/movies'}])
        self.assertEqual(out['aaa'], ['/data/torrents/movies/Alpha/a.mkv'])
        self.assertIsNone(out['bbb'])

    def test_a_client_that_answers_with_no_files_answers_an_empty_list(self):
        client = _FakeQbtClient(torrents=[_torrent('aaa', 'Alpha')], files={'aaa': []})
        with patch.object(_qbit.qbittorrentapi, 'Client', return_value=client):
            out = _qbit.fetch_torrent_file_paths(
                _qbit_cfg(), [{'hash': 'aaa', 'save_path': '/data/torrents/movies'}])
        self.assertEqual(out['aaa'], [])


class QuiFilePathsTests(unittest.TestCase):
    CFG = {'QUI_HOST': 'http://qui:7476', 'QUI_API_KEY': 'k'}

    def _session(self, files_by_hash, fail=()):
        class _Resp:
            def __init__(self, payload, ok=True):
                self._payload, self._ok = payload, ok

            def raise_for_status(self):
                if not self._ok:
                    raise RuntimeError('HTTP 500')

            def json(self):
                return self._payload

        class _Sess:
            headers = {}

            def get(_self, url, **kw):
                if url.endswith('/api/instances'):
                    return _Resp([{'id': 1, 'name': 'main', 'connected': True,
                                   'hasLocalFilesystemAccess': True}])
                h = url.split('/torrents/')[1].split('/')[0]
                if h in fail:
                    return _Resp(None, ok=False)
                return _Resp([{'name': n} for n in files_by_hash.get(h, [])])

        return _Sess()

    def test_unreachable_host_leaves_every_hash_unknown(self):
        """A total failure must not come back as a map of empty lists — that is
        the exact shape that reads as 'these torrents claim nothing'."""
        with patch.object(_qui, '_session', side_effect=RuntimeError('down')):
            out = _qui.fetch_torrent_file_paths(self.CFG, [{'hash': 'aaa'}, {'hash': 'bbb'}])
        self.assertEqual(out, {'aaa': None, 'bbb': None})

    def test_per_torrent_failure_is_none_and_success_is_a_list(self):
        with patch.object(_qui, '_session',
                          return_value=self._session({'aaa': ['a.mkv']}, fail={'bbb'})):
            out = _qui.fetch_torrent_file_paths(
                self.CFG, [{'hash': 'aaa', 'instance_id': 1, 'save_path': '/d'},
                           {'hash': 'bbb', 'instance_id': 1, 'save_path': '/d'}])
        # Keyed by registration since S05 — these items name instance 1.
        self.assertEqual(out['1:aaa'], ['/d/a.mkv'])
        self.assertIsNone(out['1:bbb'])

    def test_an_instance_that_answers_with_no_files_answers_an_empty_list(self):
        with patch.object(_qui, '_session', return_value=self._session({'aaa': []})):
            out = _qui.fetch_torrent_file_paths(
                self.CFG, [{'hash': 'aaa', 'instance_id': 1, 'save_path': '/d'}])
        self.assertEqual(out['1:aaa'], [])


# ---------------------------------------------------------------------------
# S02 (the 2026-09-10 outside review) — a successful response is not a complete
# snapshot. qui pages its listing, and the loop stopped on a short page without
# comparing what it had against the `total` the first page advertised.
# qbit has no equivalent: `torrents_info()` is one call that returns every
# torrent or raises.
# ---------------------------------------------------------------------------

class QuiShortListingTests(unittest.TestCase):
    CFG = {'QUI_HOST': 'http://qui:7476', 'QUI_API_KEY': 'k', 'TORRENT_SOURCE': 'qui',
           'LOCAL_PATH': '/data/torrents', 'REMOTE_PATH': '/data/torrents'}

    @staticmethod
    def _torrent(h):
        return {'hash': h, 'name': h, 'save_path': '/data/torrents/movies', 'size': 1,
                'state': 'uploading', 'progress': 1.0, 'completion_on': 1789000000}

    def _session(self, pages, total=None):
        """`pages[n]` answers page n; a page past the end is empty."""
        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class _Sess:
            headers = {}

            def get(_self, url, **kw):
                if url.endswith('/api/instances'):
                    return _Resp([{'id': 1, 'name': 'main', 'connected': True,
                                   'hasLocalFilesystemAccess': True}])
                if url.endswith('/torrents'):
                    page = (kw.get('params') or {}).get('page', 0)
                    body = {'torrents': pages[page] if page < len(pages) else []}
                    if total is not None:
                        body['total'] = total
                    return _Resp(body)
                return _Resp([])

        return _Sess()

    def test_a_short_qui_page_short_of_total_is_a_failed_listing(self):
        """The first page advertised three torrents and carried one. The loop
        stopped on the short page and the instance was counted as answered —
        `instances_ok 1`, `partial false` — so a scan built on it read two live
        torrents' files as orphans, and Trumped and Cleanup's re-verify took the
        short list for the client's whole answer."""
        sess = self._session([[self._torrent('aaa')]], total=3)
        with patch.object(_qui, '_session', return_value=sess):
            _rows, report = _qui.list_torrents(self.CFG)
            _map, _t, _s, scan_report = _qui.fetch_file_map(self.CFG)
        self.assertEqual(report['instances_ok'], 0)
        self.assertEqual(len(report['instances_failed']), 1)
        self.assertIn('1 of 3', report['instances_failed'][0]['reason'])
        self.assertEqual(len(scan_report['instances_failed']), 1)
        self.assertEqual(source_plausibility(scan_report, None)['code'], 'instances_unavailable')
        with patch.object(_qui, '_session', return_value=sess), \
                self.assertRaises(sources.SourceConnectionError):
            sources.list_torrents(self.CFG)

    def test_a_listing_that_reaches_its_total_is_complete(self):
        sess = self._session([[self._torrent(h) for h in ('aaa', 'bbb', 'ccc')]], total=3)
        with patch.object(_qui, '_session', return_value=sess):
            rows, report = _qui.list_torrents(self.CFG)
        self.assertEqual((len(rows), report['instances_ok'], report['instances_failed']), (3, 1, []))

    def test_with_no_total_a_short_page_ends_the_listing(self):
        """Where the API omits `total`, a page shorter than the limit is the last
        page by the API's own convention — the only evidence there is."""
        sess = self._session([[self._torrent('aaa')]])
        with patch.object(_qui, '_session', return_value=sess):
            rows, report = _qui.list_torrents(self.CFG)
        self.assertEqual((len(rows), report['instances_ok']), (1, 1))

    def test_with_no_total_a_full_page_of_repeats_is_not_complete(self):
        """A full page followed by the same page again: the API is ignoring the
        page parameter, and nothing shows the list stops at one page."""
        page = [self._torrent(f'h{i:04d}') for i in range(2000)]
        sess = self._session([page, page])
        with patch.object(_qui, '_session', return_value=sess):
            _rows, report = _qui.list_torrents(self.CFG)
        self.assertEqual(report['instances_ok'], 0)
        self.assertEqual(len(report['instances_failed']), 1)


# ---------------------------------------------------------------------------
# TR1b — list_torrents polarity. qbit raises, qui logged and carried on.
# ---------------------------------------------------------------------------

class ListTorrentsPolarityTests(unittest.TestCase):
    def test_a_partial_qui_listing_is_refused_rather_than_returned_short(self):
        """Neither backend was consistently fail-safe: qbit raises here and qui
        logged-and-continued, the reverse of C1 where qui is the careful one. A
        short list is not a smaller answer for a cross-seed group, it is a wrong
        one — the missing members keep seeding on top of the deleted payload."""
        rows = [{'hash': 'aaa', 'name': 'Alpha', 'size': 1, 'save_path': '/d',
                 'tracker': 't', 'instance_id': 1, 'instance_name': 'one'}]
        report = sources.new_source_report('qui')
        report['instances_total'] = 2
        report['instances_ok']    = 1
        sources.report_instance_failure(report, 'two', 'connection refused')

        with patch.object(sources, 'list_torrents_detailed', return_value=(rows, report)):
            with self.assertRaises(sources.SourceConnectionError) as ctx:
                sources.list_torrents({'TORRENT_SOURCE': 'qui'})
        self.assertIn('two', str(ctx.exception))

    def test_a_complete_listing_is_returned(self):
        rows   = [{'hash': 'aaa'}]
        report = sources.new_source_report('qui')
        report['instances_total'] = 2
        report['instances_ok']    = 2
        with patch.object(sources, 'list_torrents_detailed', return_value=(rows, report)):
            self.assertEqual(sources.list_torrents({'TORRENT_SOURCE': 'qui'}), rows)


# ---------------------------------------------------------------------------
# C2 — the plausibility guard
# ---------------------------------------------------------------------------

def _report(**kw):
    r = sources.new_source_report(kw.pop('source', 'qbit'))
    r.update(kw)
    return r


class PlausibilityGuardTests(unittest.TestCase):
    def test_a_normal_scan_passes(self):
        self.assertIsNone(source_plausibility(
            _report(torrent_count=1000, file_map_size=5000),
            {'torrent_count': 990, 'file_map_size': 4900}, disk_file_count=5000))

    def test_growth_never_trips_it(self):
        self.assertIsNone(source_plausibility(
            _report(torrent_count=4000, file_map_size=20000),
            {'torrent_count': 1000, 'file_map_size': 5000}, disk_file_count=20000))

    def test_a_client_answering_zero_while_the_disk_holds_files_is_refused(self):
        anomaly = source_plausibility(
            _report(torrent_count=0, file_map_size=0),
            {'torrent_count': 1000, 'file_map_size': 5000}, disk_file_count=5000)
        self.assertIsNotNone(anomaly)

    def test_the_blackout_rule_needs_no_baseline(self):
        """The worst case has none: a first-ever scan landing while qBittorrent
        is still loading its session has nothing to compare against, and a guard
        phrased only as 'compare with the previous scan' waves it straight
        through — classifying the entire torrent tree as orphaned, permanently,
        with `Select all` above it."""
        anomaly = source_plausibility(
            _report(torrent_count=0, file_map_size=0), None, disk_file_count=5000)
        self.assertIsNotNone(anomaly)
        self.assertEqual(anomaly['code'], 'client_blackout')

    def test_an_empty_client_with_an_empty_disk_is_believed(self):
        self.assertIsNone(source_plausibility(
            _report(torrent_count=0, file_map_size=0), None, disk_file_count=0))

    def test_a_halved_torrent_count_is_refused(self):
        anomaly = source_plausibility(
            _report(torrent_count=400, file_map_size=4900),
            {'torrent_count': 1000, 'file_map_size': 5000}, disk_file_count=5000)
        self.assertEqual(anomaly['code'], 'torrent_count_collapse')

    def test_a_halved_file_map_is_refused(self):
        anomaly = source_plausibility(
            _report(torrent_count=990, file_map_size=2000),
            {'torrent_count': 1000, 'file_map_size': 5000}, disk_file_count=5000)
        self.assertEqual(anomaly['code'], 'file_map_collapse')

    def test_a_tiny_library_is_not_held_to_the_percentages(self):
        """Four torrents dropping to one is a 75% collapse and an ordinary
        Tuesday. Below the floor only the blackout rule applies."""
        self.assertIsNone(source_plausibility(
            _report(torrent_count=1, file_map_size=1),
            {'torrent_count': 4, 'file_map_size': 4}, disk_file_count=1))

    def test_a_failed_instance_is_refused(self):
        report = _report(torrent_count=600, file_map_size=3000, instances_total=2)
        sources.report_instance_failure(report, 'second', 'timed out')
        anomaly = source_plausibility(
            report, {'torrent_count': 1000, 'file_map_size': 5000}, disk_file_count=5000)
        self.assertEqual(anomaly['code'], 'instances_unavailable')
        self.assertIn('second', anomaly['message'])

    def test_wholesale_unresolved_listings_are_refused(self):
        anomaly = source_plausibility(
            _report(torrent_count=100, listing_failures=40, listing_unresolved=40,
                    file_map_size=3000),
            {'torrent_count': 100, 'file_map_size': 5000}, disk_file_count=5000)
        self.assertEqual(anomaly['code'], 'listings_unavailable')

    def test_a_few_unresolved_listings_do_not_refuse_the_whole_scan(self):
        """Per-file `unverified` is Cleanup's job (roadmap Phase 8). The guard is
        for a scan whose picture of the client is wrong overall."""
        self.assertIsNone(source_plausibility(
            _report(torrent_count=1000, listing_failures=5, listing_unresolved=5,
                    file_map_size=4900),
            {'torrent_count': 1000, 'file_map_size': 5000}, disk_file_count=4900))


class ReferenceBaselineTests(unittest.TestCase):
    """S02, decision 2 (a): collapse is measured against the largest count
    persisted in a window, not only against the scan before — or a client losing
    torrents in instalments, or a pruning script run twice, passes each time."""
    NOW = datetime(2026, 9, 15, 12, 0)

    def _point(self, days_ago, torrents, files=100):
        return {'day': (self.NOW - timedelta(days=days_ago)).date().isoformat(),
                'torrent_count': torrents, 'file_map_size': torrents,
                'torrent_files': files, 'media_files': files}

    def test_the_reference_is_the_largest_count_in_the_window(self):
        points = [self._point(3, 100), self._point(1, 60)]
        ref = audit.reference_counts(points, {'torrent_count': 60, 'file_map_size': 60}, now=self.NOW)
        self.assertEqual(ref['torrent_count'], 100)
        self.assertEqual(source_plausibility(_report(torrent_count=36, file_map_size=36),
                                             ref)['code'], 'torrent_count_collapse')

    def test_a_point_older_than_the_window_is_forgotten(self):
        points = [self._point(audit._GUARD_REFERENCE_DAYS + 1, 1000), self._point(0, 60)]
        ref = audit.reference_counts(points, None, now=self.NOW)
        self.assertEqual(ref['torrent_count'], 60)
        kept = audit.advance_reference(points, self._point(0, 60), now=self.NOW)
        self.assertEqual([p['torrent_count'] for p in kept], [60])

    def test_one_point_a_day_holding_the_days_largest_counts(self):
        points = audit.advance_reference([], self._point(0, 100), now=self.NOW)
        points = audit.advance_reference(points, self._point(0, 60), now=self.NOW)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]['torrent_count'], 100)

    def test_an_accepted_drop_resets_the_reference(self):
        points = [self._point(2, 100)]
        points = audit.advance_reference(points, self._point(0, 40), now=self.NOW, reset=True)
        self.assertEqual(audit.reference_counts(points, None, now=self.NOW)['torrent_count'], 40)

    def test_an_install_with_only_the_old_baseline_is_still_measured(self):
        """Every install upgrading into Phase 12 has a `source_baseline` and no
        points yet; the first scan must not be waved through for want of them."""
        ref = audit.reference_counts(None, {'torrent_count': 1000, 'file_map_size': 5000}, now=self.NOW)
        self.assertEqual((ref['torrent_count'], ref['file_map_size']), (1000, 5000))


class FilesystemPlausibilityTests(unittest.TestCase):
    """S03 — the filesystem half of R1. Codes and counts only, never a path."""

    def test_a_configured_root_that_is_not_there_is_missing(self):
        anomaly = audit.filesystem_plausibility(
            'media', {'configured': True, 'exists': False}, None)
        self.assertEqual((anomaly['code'], anomaly['detail']['root']), ('root_missing', 'media'))

    def test_an_unconfigured_root_is_not_missing(self):
        self.assertIsNone(audit.filesystem_plausibility(
            'media', {'configured': False, 'exists': False}, None))

    def test_an_unlistable_folder_near_the_root_refuses_and_a_deep_one_is_only_counted(self):
        shallow = {'configured': True, 'exists': True, 'files': 50,
                   'unlistable': 1, 'unlistable_shallow': 1}
        deep = dict(shallow, unlistable_shallow=0)
        self.assertEqual(audit.filesystem_plausibility('torrents', shallow, None)['code'],
                         'root_unlistable')
        self.assertIsNone(audit.filesystem_plausibility('torrents', deep, None))

    def test_a_walk_far_below_the_reference_is_a_collapse(self):
        block = {'configured': True, 'exists': True, 'files': 0, 'unlistable': 0,
                 'unlistable_shallow': 0}
        anomaly = audit.filesystem_plausibility('media', block, {'media_files': 400})
        self.assertEqual(anomaly['code'], 'disk_collapse')

    def test_a_tiny_or_growing_tree_is_not_held_to_the_percentages(self):
        block = {'configured': True, 'exists': True, 'files': 1, 'unlistable': 0,
                 'unlistable_shallow': 0}
        self.assertIsNone(audit.filesystem_plausibility('media', block, {'media_files': 4}))
        self.assertIsNone(audit.filesystem_plausibility(
            'media', dict(block, files=900), {'media_files': 400}))

    def test_a_client_holding_files_over_an_empty_torrent_folder_needs_no_baseline(self):
        """`client_blackout`'s mirror, and for the same reason: the worst case —
        a first scan landing on an empty bind mount — has no baseline to compare
        against. The client says these files are under the torrent folder."""
        block = {'configured': True, 'exists': True, 'files': 0, 'unlistable': 0,
                 'unlistable_shallow': 0}
        anomaly = audit.filesystem_plausibility('torrents', block, None, file_map_size=40)
        self.assertEqual(anomaly['code'], 'disk_collapse')
        self.assertIsNone(audit.filesystem_plausibility('torrents', block, None, file_map_size=0))


class GuardOverrideTests(unittest.TestCase):
    BAD = dict(torrent_count=0, file_map_size=0)
    PREV = {'torrent_count': 1000, 'file_map_size': 5000}

    def test_a_scheduled_scan_refuses(self):
        with self.assertRaises(_SourceAnomaly):
            _guard_scan(_report(**self.BAD), self.PREV, 'watchdog', disk_file_count=5000)

    def test_startup_is_not_an_override(self):
        """A startup scan after a container rebuild is precisely the scenario the
        guard exists for — a fresh session directory and a client that answers
        zero — so it must not count as explicit intent."""
        with self.assertRaises(_SourceAnomaly):
            _guard_scan(_report(**self.BAD), self.PREV, 'startup', disk_file_count=5000)

    def test_a_manual_scan_accepts_a_change_in_what_the_client_holds(self):
        """Rewritten in Phase 12 under the user's decision 1 (a), fixture kept.
        This was `test_a_manual_scan_is_the_override`, for every rule. A client
        that really holds fewer torrents is accepted by hand; the two tests below
        are the reads that never are."""
        anomaly = _guard_scan(_report(**self.BAD), self.PREV, 'manual', disk_file_count=5000)
        self.assertEqual(anomaly['code'], 'torrent_count_collapse')

    def test_a_manual_scan_never_accepts_an_instance_that_did_not_answer(self):
        report = _report(torrent_count=600, file_map_size=3000, instances_total=2)
        sources.report_instance_failure(report, 'second', 'timed out')
        with self.assertRaises(_SourceAnomaly):
            _guard_scan(report, self.PREV, 'manual', disk_file_count=5000)

    def test_a_manual_scan_never_accepts_listings_that_could_not_be_read(self):
        report = _report(torrent_count=1000, listing_failures=400, listing_unresolved=400,
                         file_map_size=4800)
        with self.assertRaises(_SourceAnomaly):
            _guard_scan(report, self.PREV, 'manual', disk_file_count=5000)

    def test_a_blackout_is_a_change_a_manual_scan_may_accept(self):
        """The client answered, with nothing — which a user who emptied their
        client really can have. It is the no-baseline form of the collapse rules,
        and is classified with them."""
        anomaly = _guard_scan(_report(**self.BAD), None, 'manual', disk_file_count=5000)
        self.assertEqual(anomaly['code'], 'client_blackout')


# ---------------------------------------------------------------------------
# R2 — the completion flag. One primitive, three consumers.
#
# `status` is derived from the client's state string, where a paused incomplete
# torrent and a paused complete one are the same word. That one blind spot is
# DEDUPE F6 (unfinished sparse files enter a duplicate group and pass `cmp`),
# TRIAGE T4 (in-flight downloads are reported as junk) and CLEANUP C4a/C4b
# (live downloads read as orphans) simultaneously.
# ---------------------------------------------------------------------------

class CompletionRuleTests(unittest.TestCase):
    """`torrent_complete` is a fallback chain, and the chain is the finding.

    The first draft of this phase proposed the union
    `progress >= 1.0 or completion_on > 0`, justified entirely by `progress`
    sitting below 1.0 forever on a torrent with deprioritized files. That premise
    was measured false (ROADMAP §0.4), and with it gone the `or` is not merely
    redundant — it is unsafe in the one direction F6 says must not fail.
    """

    def test_a_rechecked_torrent_reads_incomplete(self):
        """The case the chain exists for, and the one no live install will show
        you: a torrent that finished and was later rechecked (files deleted, a
        partial re-download) has `progress` correctly below 1.0 while
        `completion_on` still holds its original timestamp. Under the superseded
        union this reads *complete*, its half-written sparse file enters a
        duplicate group, and hardlinking it corrupts both torrents."""
        self.assertIs(sources.torrent_complete(0.4, 1789000000), False)

    def test_completion_on_answers_only_when_progress_is_absent(self):
        self.assertIs(sources.torrent_complete(None, 1789000000), True)
        self.assertIs(sources.torrent_complete(None, 0), False)
        self.assertIs(sources.torrent_complete(None, -1), False)

    def test_a_deprioritized_torrent_reads_complete(self):
        """Measured shape, 2026-09-11: a season pack with five files set to *Do
        not download* — 32% of its payload wanted — reports `progress == 1.0`
        and `amount_left == 0`, because libtorrent computes
        `total_wanted_done / total_wanted` while the UI's 42.5% is
        `completed / total_size`.

        This passes under a plain `progress >= 1.0`. The test exists to pin the
        *reason*, so a later change to the rule has to confront the measurement
        rather than rediscover it — and so that nobody rebuilds the per-file
        fan-out to answer a question `progress` already answers."""
        self.assertIs(sources.torrent_complete(1.0, 1789162167), True)

    def test_no_usable_completion_field_reads_unknown(self):
        self.assertIsNone(sources.torrent_complete(None, None))
        self.assertIsNone(sources.torrent_complete('nonsense', None))

    def test_a_percentage_scale_is_inferred_rather_than_assumed(self):
        """Measured 0-1 on both backends across 1316 torrents. Reading a
        hypothetical percent-scale 42.5 as 'complete' is the corrupting
        direction, so a value above 1.0 is treated as the scale it can only be."""
        self.assertIs(sources.torrent_complete(42.5, 0), False)
        self.assertIs(sources.torrent_complete(100.0, 0), True)


class CompletionFlagInSourceTests(unittest.TestCase):
    """The primitive, driven through the real qbit backend."""

    def _map(self, torrents, files):
        client = _FakeQbtClient(torrents=torrents, files=files)
        with patch.object(_qbit.qbittorrentapi, 'Client', return_value=client):
            return _qbit.fetch_file_map(_qbit_cfg())

    def test_a_paused_incomplete_is_distinguishable_from_a_paused_complete(self):
        """The root cause in one assertion. Both torrents are 'Paused', so no
        rule reading the state string can separate them — which is how an
        unfinished payload reached the duplicate map and the Triage pile."""
        file_map, _, _, report = self._map(
            [_torrent('aaa', 'Alpha', state='pausedUP', progress=1.0),
             _torrent('bbb', 'Bravo', state='pausedDL', progress=0.31)],
            {'aaa': ['Alpha/a.mkv'], 'bbb': ['Bravo/b.mkv']})
        done = file_map[os.path.join('/data/torrents/movies', 'Alpha/a.mkv')]
        part = file_map[os.path.join('/data/torrents/movies', 'Bravo/b.mkv')]
        self.assertEqual(done['status'], part['status'])   # both 'Paused'
        self.assertNotIn('incomplete', done)
        self.assertTrue(part['incomplete'])
        self.assertEqual(report['incomplete_torrents'], 1)
        self.assertEqual(report['completion_unknown'], 0)

    def test_a_client_exposing_no_completion_field_is_reported_as_unknown(self):
        file_map, _, _, report = self._map(
            [_torrent('aaa', 'Alpha', progress=None, completion_on=None)],
            {'aaa': ['Alpha/a.mkv']})
        entry = file_map[os.path.join('/data/torrents/movies', 'Alpha/a.mkv')]
        self.assertTrue(entry['completion_unknown'])
        self.assertNotIn('incomplete', entry)
        self.assertEqual(report['completion_unknown'], 1)

    def test_a_healthy_library_carries_no_completion_keys_at_all(self):
        """Sparse, like `dead_siblings`: absence means complete. A field on
        every file record multiplies across every file of every torrent and
        grows files_json, the known RAM hotspot."""
        file_map, _, _, report = self._map(
            [_torrent('aaa', 'Alpha')], {'aaa': ['Alpha/a.mkv']})
        entry = file_map[os.path.join('/data/torrents/movies', 'Alpha/a.mkv')]
        self.assertNotIn('incomplete', entry)
        self.assertNotIn('completion_unknown', entry)
        self.assertEqual(report['incomplete_torrents'], 0)
        self.assertEqual(report['completion_unknown'], 0)

    def test_an_unfinished_torrent_is_still_claimed_c4a(self):
        """C4a's resolution of 'unknown': claim the payload either way. An
        unknown-state torrent's files are still a payload, and the thing that
        must never happen is the walk finding them with nothing against them."""
        file_map, _, _, _ = self._map(
            [_torrent('bbb', 'Bravo', state='downloading', progress=0.1)],
            {'bbb': ['Bravo/b.mkv']})
        self.assertIn(os.path.join('/data/torrents/movies', 'Bravo/b.mkv'), file_map)

    def test_the_incomplete_suffix_is_claimed_for_an_unfinished_torrent(self):
        """CLEANUP C4a. With *Append .!qB extension to incomplete files* on, the
        on-disk name is `Foo.mkv.!qB` while the listing reports `Foo.mkv`, so
        path equality fails and a file being actively written is offered for
        `rm`. The option is off on the reference box and cannot be measured
        there, so both spellings are claimed defensively."""
        file_map, _, _, _ = self._map(
            [_torrent('bbb', 'Bravo', state='downloading', progress=0.1)],
            {'bbb': ['Bravo/b.mkv']})
        self.assertIn(os.path.join('/data/torrents/movies', 'Bravo/b.mkv') + '.!qB',
                      file_map)

    def test_a_finished_torrent_claims_no_extra_spellings(self):
        """So a healthy library's file_map is byte-identical to before — which
        also keeps the plausibility guard's `file_map_size` baseline unmoved."""
        file_map, _, _, _ = self._map(
            [_torrent('aaa', 'Alpha')], {'aaa': ['Alpha/a.mkv']})
        self.assertEqual(len(file_map), 1)

    def test_an_incomplete_directory_is_claimed_c4b(self):
        """CLEANUP C4b. `save_path` is the *final* location; while downloading,
        the bytes are under the client's temp path, which `content_path`
        follows. On the reference box that temp path is outside LOCAL_PATH so
        nothing is walked — but TRaSH's layout is a recommendation, not a
        guarantee, and where it sits inside LOCAL_PATH the walk sees files the
        map does not have."""
        file_map, _, _, _ = self._map(
            [_torrent('bbb', 'Bravo', state='downloading', progress=0.1,
                      content_path='/data/torrents/incomplete/Bravo')],
            {'bbb': ['Bravo/b.mkv']})
        self.assertIn('/data/torrents/incomplete/Bravo/b.mkv', file_map)


class ContentPathShapeTests(unittest.TestCase):
    """`content_path` is sometimes a file and sometimes a directory (§0.4)."""

    def test_a_multi_file_torrent_roots_at_the_folder(self):
        """Posix joins, not `os.path.join` — the container is posix and a client
        file name already carries `/`, so a native join would mix separators on
        Windows and make these tests exercise a different branch than the
        container does (CLAUDE.md's `_local_to_abs` lesson)."""
        self.assertEqual(
            sources.content_rooted_paths('/dl/Bravo', 'Bravo',
                                         ['Bravo/b.mkv', 'Bravo/subs/b.srt']),
            ['/dl/Bravo/b.mkv', '/dl/Bravo/subs/b.srt'])

    def test_a_single_file_torrent_inside_a_release_folder_is_the_file(self):
        """All 8 of 200 completed torrents on the reference box where
        `content_path != save_path/name` are this shape: qBittorrent resolves
        `content_path` to the file while `save_path/name` is the folder. Treating
        it as a directory to walk returns nothing for every one of them."""
        self.assertEqual(
            sources.content_rooted_paths('/dl/Bravo (2021)/bravo.mkv', 'Bravo (2021)',
                                         ['Bravo (2021)/bravo.mkv']),
            ['/dl/Bravo (2021)/bravo.mkv'])

    def test_no_content_path_claims_nothing(self):
        self.assertEqual(sources.content_rooted_paths('', 'Bravo', ['Bravo/b.mkv']), [])

    def test_claims_are_deduplicated_against_the_final_paths(self):
        final = [os.path.join('/data/torrents/movies', 'Bravo/b.mkv')]
        claims = sources.incomplete_claims(
            '/data/torrents/movies/Bravo', 'Bravo', ['Bravo/b.mkv'], final)
        self.assertEqual(claims, [final[0] + '.!qB'])


class DiskFallbackContentPathTests(unittest.TestCase):
    """ROADMAP §0.3 item 1: Phase 2's fallback walked `save_path`, which for an
    in-flight torrent holds nothing at all — so it counted a `listing_unresolved`
    and inflated the guard's `listings_unavailable` input with the one case that
    carries no risk."""

    def test_an_in_flight_torrent_is_found_at_its_content_path(self):
        """The case the argument was added for: nothing whatsoever is at
        `save_path/name` yet."""
        root = '/downloads/Bravo'
        with patch.object(sources.os.path, 'isfile', lambda p: False), \
             patch.object(sources.os.path, 'isdir', lambda p: p == root), \
             patch.object(sources.os, 'walk',
                          lambda r: [(root, [], ['b.mkv'])] if r == root else []):
            found = sources.disk_fallback_paths('/data/torrents/movies', 'Bravo', root)
        self.assertEqual(found, [os.path.join(root, 'b.mkv')])

    def test_save_path_still_answers_when_content_path_holds_nothing(self):
        root = os.path.join('/data/torrents/movies', 'Bravo')
        with patch.object(sources.os.path, 'isfile', lambda p: False), \
             patch.object(sources.os.path, 'isdir', lambda p: p == root), \
             patch.object(sources.os, 'walk',
                          lambda r: [(root, [], ['b.mkv'])] if r == root else []):
            found = sources.disk_fallback_paths(
                '/data/torrents/movies', 'Bravo', '/downloads/Bravo')
        self.assertEqual(found, [os.path.join(root, 'b.mkv')])

    def test_both_roots_are_unioned_rather_than_the_first_one_winning(self):
        """Preferring `content_path` would *narrow* the answer for the commonest
        shape the two differ on — a single-file torrent inside a release folder,
        where `content_path` is the file and `save_path/name` is the folder
        holding it plus sidecars. For a failed listing, over-claiming is the
        fail-safe direction (this function's whole argument), and the union also
        leaves a completed torrent claiming exactly what it claimed before the
        argument existed."""
        folder = os.path.join('/data/torrents/movies', 'Bravo')
        cp     = os.path.join(folder, 'b.mkv')
        with patch.object(sources.os.path, 'isfile', lambda p: p == cp), \
             patch.object(sources.os.path, 'isdir', lambda p: p == folder), \
             patch.object(sources.os, 'walk',
                          lambda r: [(folder, [], ['b.mkv', 'b.nfo'])] if r == folder else []):
            found = sources.disk_fallback_paths('/data/torrents/movies', 'Bravo', cp)
        self.assertEqual(found, [cp, os.path.join(folder, 'b.nfo')])

    def test_a_single_file_content_path_is_returned_not_walked(self):
        cp = '/downloads/Bravo (2021)/bravo.mkv'
        with patch.object(sources.os.path, 'isfile', lambda p: p == cp), \
             patch.object(sources.os.path, 'isdir', lambda p: False), \
             patch.object(sources.os, 'walk', lambda r: []):
            self.assertEqual(
                sources.disk_fallback_paths('/data/torrents/movies', 'Bravo (2021)', cp),
                [cp])


class QuiCompletionParityTests(unittest.TestCase):
    """Neither normalizer mentioned `progress`, `content_path` or `completion_on`
    anywhere. M7 answered 'does the client expose these' — yes, on both — which
    is a different question from 'does auditorr read them'. It did not, so
    `_qui.py` is as much of this phase as `_qbit.py`."""

    def test_the_normalizer_carries_the_completion_fields(self):
        nt = _qui._norm_torrent({
            'hash': 'aaa', 'name': 'Alpha', 'save_path': '/d',
            'progress': 0.42, 'completion_on': 0, 'content_path': '/dl/Alpha/'})
        self.assertEqual(nt['progress'], 0.42)
        self.assertEqual(nt['completion_on'], 0)
        self.assertEqual(nt['content_path'], '/dl/Alpha')

    def test_a_zero_completion_stamp_is_an_answer_not_an_absence(self):
        """`a or b or 0` would collapse `completion_on: 0` ("never finished")
        into the same falsy hole as "the field is absent", which is the R1
        mistake one layer down: the chain needs None to mean *could not ask*."""
        nt = _qui._norm_torrent({'hash': 'aaa', 'completion_on': 0})
        self.assertIs(sources.torrent_complete(nt['progress'], nt['completion_on']), False)
        nt2 = _qui._norm_torrent({'hash': 'aaa'})
        self.assertIsNone(sources.torrent_complete(nt2['progress'], nt2['completion_on']))

    def test_an_unfinished_qui_torrent_flags_its_entry_and_the_report(self):
        torrents = [
            {'hash': 'aaa', 'name': 'Alpha', 'save_path': '/data/torrents/movies',
             'size': 100, 'state': 'uploading', 'uploaded': 5, 'progress': 1.0,
             'completion_on': 1789000000,
             'content_path': '/data/torrents/movies/Alpha'},
            {'hash': 'bbb', 'name': 'Bravo', 'save_path': '/data/torrents/movies',
             'size': 100, 'state': 'pausedDL', 'uploaded': 0, 'progress': 0.2,
             'completion_on': 0,
             'content_path': '/data/torrents/movies/Bravo'},
        ]
        files = {'aaa': ['Alpha/a.mkv'], 'bbb': ['Bravo/b.mkv']}

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class _Sess:
            headers = {}

            def get(_self, url, **kw):
                if url.endswith('/api/instances'):
                    return _Resp([{'id': 1, 'name': 'main', 'connected': True,
                                   'hasLocalFilesystemAccess': True}])
                if url.endswith('/torrents'):
                    page = (kw.get('params') or {}).get('page', 0)
                    return _Resp({'torrents': torrents if page == 0 else [],
                                  'total': len(torrents)})
                if url.endswith('/trackers'):
                    return _Resp([{'url': 'http://tracker.example/announce',
                                   'status': 2, 'msg': ''}])
                h = url.split('/torrents/')[1].split('/')[0]
                return _Resp([{'name': n} for n in files.get(h, [])])

        with patch.object(_qui, '_session', return_value=_Sess()):
            file_map, _, _, report = _qui.fetch_file_map(
                {'QUI_HOST': 'http://qui:7476', 'QUI_API_KEY': 'k',
                 'LOCAL_PATH': '/data/torrents', 'REMOTE_PATH': '/data/torrents'})
        done = file_map[os.path.join('/data/torrents/movies', 'Alpha/a.mkv')]
        part = file_map[os.path.join('/data/torrents/movies', 'Bravo/b.mkv')]
        self.assertNotIn('incomplete', done)
        self.assertTrue(part['incomplete'])
        self.assertEqual(report['incomplete_torrents'], 1)
        self.assertEqual(report['completion_unknown'], 0)


# ---------------------------------------------------------------------------
# The three consumers, and the three directions `None` resolves in
# ---------------------------------------------------------------------------

def _inode(size=100, **kw):
    info = {
        'trackers': set(), 'status': 'Seeding', 'torrent_paths': [], 'media_paths': [],
        'hash': 'aaa', 'instance_id': None, 'instance_name': None,
        'tracker_health': 'working', 'tracker_msg': '', 'unreg_claimants': {},
        'size': size, 'torrent_rel_path': None, 'torrent_excluded': False,
        'media_rel_path': None, 'media_excluded': False,
    }
    info.update(kw)
    return info


class DuplicateMapCompletionTests(unittest.TestCase):
    """DEDUPE F6 — the only one of the consumers that can destroy data.

    qBittorrent writes **sparse** files by default: the final `st_size` is
    reported while unwritten regions read as zeros. Two unfinished files of
    equal size whose written regions do not overlap land in the same size group,
    produce the same head+tail fast hash, and pass the script's `cmp`, because
    at that moment both really are zeros there.
    """

    def _groups(self, inode_map):
        with patch.object(audit, 'get_fast_hash', lambda p, s: 'samehash'):
            return _build_duplicate_map(inode_map)

    def test_two_complete_files_still_group(self):
        m = {(1, 1): _inode(torrent_paths=['/t/a.mkv'], torrent_rel_path='a.mkv'),
             (1, 2): _inode(torrent_paths=['/t/b.mkv'], torrent_rel_path='b.mkv')}
        self.assertEqual(len(self._groups(m)), 2)

    def test_an_incomplete_file_is_never_grouped(self):
        m = {(1, 1): _inode(torrent_paths=['/t/a.mkv'], torrent_rel_path='a.mkv'),
             (1, 2): _inode(torrent_paths=['/t/b.mkv'], torrent_rel_path='b.mkv',
                            incomplete=True)}
        self.assertEqual(self._groups(m), {})

    def test_unknown_completion_excludes_too(self):
        """F6's resolution of `None`. The cost of excluding is a missed reclaim;
        the cost of including is two corrupt torrents. Not symmetric."""
        m = {(1, 1): _inode(torrent_paths=['/t/a.mkv'], torrent_rel_path='a.mkv'),
             (1, 2): _inode(torrent_paths=['/t/b.mkv'], torrent_rel_path='b.mkv',
                            completion_unknown=True)}
        self.assertEqual(self._groups(m), {})


class WalkCarriesCompletionTests(unittest.TestCase):
    """`_build_duplicate_map` reads `inode_map`, which the walk builds from the
    file map — so the flag has to arrive there, not only on the persisted
    record."""

    def test_the_walk_folds_the_flag_onto_the_inode(self):
        base = '/data/torrents'
        full = os.path.join(base, 'movies', 'b.mkv')
        file_map = {full: {'status': 'Downloading', 'trackers': {'t'}, 'hash': 'bbb',
                           'category': 'movies', 'tracker_health': 'working',
                           'tracker_msg': '', 'incomplete': True}}
        inode_map = {}
        st = os.stat_result((0o100644, 42, 1, 1, 0, 0, 100, 0, 0, 0))
        # `isdir` and an `onerror`-accepting walk since Phase 12 (S03): the walk
        # checks its root is a directory, and reports what it could not list.
        with patch.object(audit.os.path, 'isdir', lambda p: True), \
             patch.object(audit.os, 'walk',
                          lambda p, **kw: [(os.path.join(base, 'movies'), [], ['b.mkv'])]), \
             patch.object(audit.os, 'stat', lambda p: st):
            _walk_directory(base, 'Torrent', inode_map, file_map, 0, 0,
                            exclusion_patterns=[], total_ref=[0])
        self.assertTrue(inode_map[(st.st_dev, st.st_ino)]['incomplete'])


class WalkReportsWhatItCouldNotSeeTests(unittest.TestCase):
    """S03. The walk returned an empty result with zero stat errors for a root
    that does not exist, and `os.walk` ran with no `onerror`, so a directory that
    could not be listed dropped everything beneath it without a word."""

    def _tree(self, base):
        for rel in ('movies/Rel/a.mkv', 'movies/Rel/Subs/a.srt', 'tv/Show/S01/e1.mkv'):
            path = os.path.join(base, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as fh:
                fh.write(b'x')

    def test_an_unlistable_directory_is_counted_not_silent(self):
        with tempfile.TemporaryDirectory() as base:
            self._tree(base)
            refused = {os.path.normcase(os.path.join(base, 'movies', 'Rel', 'Subs')),   # depth 3
                       os.path.normcase(os.path.join(base, 'tv', 'Show'))}              # depth 2
            real_scandir = os.scandir

            def scandir(path='.'):
                if os.path.normcase(os.path.normpath(os.fspath(path))) in refused:
                    raise PermissionError(13, 'Permission denied', os.fspath(path))
                return real_scandir(path)

            walk = {}
            with patch('os.scandir', scandir):
                _walk_directory(base, 'Torrent', {}, {}, 0, 0, exclusion_patterns=[],
                                total_ref=[0], walk_report=walk)
        self.assertEqual(walk['files'], 1)
        self.assertEqual(walk['unlistable'], 2)
        self.assertEqual(walk['unlistable_shallow'], 1)
        self.assertTrue(walk['exists'])

    def test_a_root_that_is_not_there_says_so(self):
        walk = {}
        with tempfile.TemporaryDirectory() as base:
            _walk_directory(os.path.join(base, 'gone'), 'Media', {}, {}, 0, 0,
                            exclusion_patterns=[], total_ref=[0], walk_report=walk)
        self.assertEqual((walk['configured'], walk['exists'], walk['files']), (True, False, 0))


class TriageCompletionTests(unittest.TestCase):
    """TRIAGE T4 — a torrent at 0% is not-imported by definition, so the newest
    thing in the client was a full-size Triage row with a delete button under
    copy reading "junk can be deleted"."""

    @staticmethod
    def _rec(**kw):
        r = {'path': 'movies/Bravo/b.mkv', 'size': 100, 'hash': 'bbb',
             'status': 'Paused', 'imported': False, 'excluded': False,
             'tracker_health': 'working', 'trackers': ['t']}
        r.update(kw)
        return r

    def test_an_incomplete_torrent_is_not_a_not_imported_problem(self):
        self.assertFalse(_is_not_imported_torrent(self._rec(incomplete=True)))
        self.assertFalse(_is_triage_relevant(self._rec(incomplete=True)))

    def test_unknown_completion_keeps_the_row(self):
        """T4's resolution of `None`: show it, with its status. auditorr never
        hides anything silently — that is T3 and T15's rule and it holds here."""
        self.assertTrue(_is_not_imported_torrent(self._rec(completion_unknown=True)))
        self.assertTrue(_is_triage_relevant(self._rec(completion_unknown=True)))

    def test_a_finished_not_imported_torrent_is_unaffected(self):
        self.assertTrue(_is_not_imported_torrent(self._rec()))
        self.assertTrue(_is_triage_relevant(self._rec()))

    def test_the_badge_and_the_page_agree(self):
        """The filter exists in three places — `_is_not_imported_torrent`,
        `_is_triage_relevant` and `count_triage_items` — and when they disagree
        the badge reads 4 against a page of 10. Same shape as Phase 4d's three
        grouping keys."""
        records = [self._rec(hash='bbb', incomplete=True),
                   self._rec(hash='ccc', path='movies/Charlie/c.mkv')]
        counts = count_triage_items(records)
        self.assertEqual(counts['not_imported'], 1)
        self.assertEqual(counts['total'], 1)
        self.assertEqual(len([f for f in records if _is_triage_relevant(f)]), 1)


if __name__ == '__main__':
    unittest.main()
