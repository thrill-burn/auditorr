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
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sources
from sources import _qbit, _qui
from audit import source_plausibility, _guard_scan, _SourceAnomaly


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


def _torrent(h, name, save_path='/data/torrents/movies', size=100):
    return _FakeTorrent(hash=h, name=name, save_path=save_path, size=size,
                        state='uploading', uploaded=10, category='movies',
                        seeding_time=3600)


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
        self.assertEqual(out['aaa'], ['/d/a.mkv'])
        self.assertIsNone(out['bbb'])

    def test_an_instance_that_answers_with_no_files_answers_an_empty_list(self):
        with patch.object(_qui, '_session', return_value=self._session({'aaa': []})):
            out = _qui.fetch_torrent_file_paths(
                self.CFG, [{'hash': 'aaa', 'instance_id': 1, 'save_path': '/d'}])
        self.assertEqual(out['aaa'], [])


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

    def test_a_manual_scan_is_the_override(self):
        anomaly = _guard_scan(_report(**self.BAD), self.PREV, 'manual', disk_file_count=5000)
        self.assertIsNotNone(anomaly)


if __name__ == '__main__':
    unittest.main()
