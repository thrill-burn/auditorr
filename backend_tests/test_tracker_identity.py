"""A tracker is not an announce host (Trackers page, 2026-11-13).

TorrentLeech's torrents announce to two hosts, `tracker.torrentleech.org` and
`tracker.tleechreload.org`, and every per-tracker number counted each host as a
tracker: two rows on the Trackers page, two lines on its chart, its upload twice
in the library total, and every TorrentLeech-only file "cross-seeded" on itself.
On the reference box that was 134.4 GB counted twice over 122 days, and the
seed multiplier read 1.703 where it is 1.698. A host stored with its port
(`xspeeds.eu:2710`) was a second spelling of the same kind.

The fold is statistics-only: a file record keeps every host, because the
workflows match a host to an *arr indexer by name and either host may be the
one that matches.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sources
from sources import TrackerTally, announce_hosts, canonical_trackers, strip_port, _qbit
import audit

GB = 10 ** 9
TL_A, TL_B = 'tracker.torrentleech.org', 'tracker.tleechreload.org'


def _h(n):
    """An infohash-shaped hex string, distinct per n."""
    return f'{n:040x}'


class AnnounceHostTests(unittest.TestCase):
    def test_the_port_comes_off(self):
        self.assertEqual(announce_hosts(['http://xspeeds.eu:2710/abc/announce']), ['xspeeds.eu'])

    def test_a_host_listed_twice_is_one_host(self):
        """An http and an https announce on one host added the torrent's upload twice."""
        self.assertEqual(announce_hosts(['http://aither.cc/a/announce',
                                         'https://aither.cc/a/announce']), ['aither.cc'])

    def test_dht_rows_are_not_trackers(self):
        self.assertEqual(announce_hosts(['** [DHT] **', '** [PeX] **']), ['Unknown'])

    def test_ipv6_keeps_its_brackets(self):
        self.assertEqual(strip_port('[2001:db8::1]:6969'), '[2001:db8::1]')
        self.assertEqual(strip_port('Unknown'), 'Unknown')


class TrackerTallyTests(unittest.TestCase):
    def _tl(self, n=3, first=TL_B):
        t = TrackerTally()
        second = TL_A if first == TL_B else TL_B
        for i in range(n):
            t.add(_h(i), [first, second], ('working', ''))
        return t

    def test_two_hosts_on_the_same_torrents_are_one_tracker(self):
        self.assertEqual(self._tl(first=TL_A).aliases(), {TL_B: TL_A})

    def test_the_name_kept_is_the_host_listed_first(self):
        self.assertEqual(self._tl(first=TL_B).aliases(), {TL_A: TL_B})

    def test_cross_seeded_trackers_stay_two(self):
        """A cross-seed is a second torrent: two trackers seeding the same files
        never have the same torrents."""
        t = TrackerTally()
        for i in range(4):
            t.add(_h(i), ['aither.cc'], ('working', ''))
            t.add(_h(100 + i), ['seedpool.org'], ('working', ''))
        self.assertEqual(t.aliases(), {})

    def test_one_torrent_is_not_evidence(self):
        """A lone multi-tracker torrent (a public one) proves nothing about its hosts."""
        t = TrackerTally()
        t.add(_h(1), ['a.example', 'b.example'], ('working', ''))
        self.assertEqual(t.aliases(), {})

    def test_a_host_on_one_more_torrent_is_its_own_tracker(self):
        t = self._tl(n=3)
        t.add(_h(9), [TL_A], ('working', ''))
        self.assertEqual(t.aliases(), {})

    def test_health_is_counted_per_tracker(self):
        t = TrackerTally()
        t.add(_h(1), ['xspeeds.eu'], ('not_working', 'Change your port and try again.'))
        t.add(_h(2), ['xspeeds.eu'], ('not_working', 'second message'))
        t.add(_h(3), ['xspeeds.eu'], ('unregistered', 'Unregistered torrent'))
        t.add(_h(4), ['xspeeds.eu'], ('working', ''))
        self.assertEqual(t.stats['xspeeds.eu'], {
            'torrents': 4, 'not_working': 2, 'unregistered': 1,
            'msg': 'Change your port and try again.'})

    def test_finish_folds_the_alias_out_of_the_snapshot(self):
        t = self._tl(n=2, first=TL_A)
        snap = {TL_A: {'uploaded': 5 * GB, 'seeding_size': 9 * GB},
                TL_B: {'uploaded': 5 * GB, 'seeding_size': 9 * GB},
                'aither.cc': {'uploaded': 1, 'seeding_size': 1}}
        trackers, aliases = t.finish(snap, {TL_A, TL_B, 'aither.cc'})
        self.assertEqual(trackers, ['aither.cc', TL_A])
        self.assertEqual(aliases, {TL_B: TL_A})
        self.assertNotIn(TL_B, snap)
        self.assertEqual(snap['_aliases'], {TL_B: TL_A})
        self.assertEqual(snap[TL_A]['uploaded'], 5 * GB)
        self.assertEqual(snap[TL_A]['torrents'], 2)

    def test_canonical_trackers(self):
        self.assertEqual(canonical_trackers([TL_A, TL_B, 'None'], {TL_B: TL_A}), [TL_A])
        self.assertEqual(canonical_trackers(['aither.cc', 'None'], {}), ['aither.cc'])


# ---------------------------------------------------------------------------
# The source: what the qbit backend hands the audit
# ---------------------------------------------------------------------------

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Client:
    def __init__(self, torrents, trackers):
        self._torrents, self._trackers = torrents, trackers

    def auth_log_in(self):
        return True

    def torrents_info(self, torrent_hashes=None, **kw):
        return list(self._torrents)

    def torrents_trackers(self, torrent_hash=None):
        return [_Obj(url=u, status=s, msg=m) for u, s, m in self._trackers[torrent_hash]]

    def torrents_files(self, torrent_hash=None):
        t = next(t for t in self._torrents if t.hash == torrent_hash)
        return [_Obj(name=f'{t.name}/{t.name}.mkv')]


def _torrent(h, name, uploaded):
    save = '/data/torrents/movies'
    return _Obj(hash=h, name=name, save_path=save, size=10 * GB, state='uploading',
                uploaded=uploaded, category='movies', seeding_time=3600, progress=1.0,
                completion_on=1789000000, content_path=f'{save}/{name}')


class QbitSourceTests(unittest.TestCase):
    def _run(self):
        tl = lambda key: [(f'https://{TL_A}/a/{key}/announce', 2, ''),
                          (f'https://{TL_B}/a/{key}/announce', 2, '')]
        torrents = [_torrent(_h(1), 'One', 3 * GB), _torrent(_h(2), 'Two', 4 * GB),
                    _torrent(_h(3), 'Three', 7 * GB)]
        trackers = {
            _h(1): tl('k'), _h(2): tl('k'),
            _h(3): [('http://xspeeds.eu:2710/k/announce', 4, 'Change your port and try again.'),
                    ('https://xspeeds.eu:2710/k/announce', 4, 'Change your port and try again.')],
        }
        cfg = {'QB_HOST': 'http://localhost:8080', 'QB_USER': 'u', 'QB_PASS': 'p',
               'LOCAL_PATH': '/data/torrents', 'REMOTE_PATH': '/data/torrents'}
        with patch.object(_qbit.qbittorrentapi, 'Client', return_value=_Client(torrents, trackers)):
            return _qbit.fetch_file_map(cfg)

    def test_torrentleech_is_one_tracker_and_counts_once(self):
        file_map, trackers, snap, _ = self._run()
        self.assertEqual(trackers, [TL_A, 'xspeeds.eu'])
        self.assertEqual(snap['_aliases'], {TL_B: TL_A})
        self.assertEqual(snap[TL_A]['uploaded'], 7 * GB)
        self.assertEqual(snap[TL_A]['torrents'], 2)
        # The record keeps both hosts, for the workflows' indexer match.
        [rec] = [r for p, r in file_map.items() if p.endswith('One.mkv')]
        self.assertEqual(set(rec['trackers']), {TL_A, TL_B})

    def test_the_port_is_dropped_and_a_double_announce_counts_once(self):
        _, _, snap, _ = self._run()
        self.assertNotIn('xspeeds.eu:2710', snap)
        self.assertEqual(snap['xspeeds.eu']['uploaded'], 7 * GB)
        self.assertEqual(snap['xspeeds.eu']['not_working'], 1)
        self.assertEqual(snap['xspeeds.eu']['msg'], 'Change your port and try again.')


# ---------------------------------------------------------------------------
# The audit's per-tracker numbers
# ---------------------------------------------------------------------------

class StatsTests(unittest.TestCase):
    ALIASES = {TL_B: TL_A}

    def test_a_torrentleech_file_is_seeded_once(self):
        media = [
            {'size': 10 * GB, 'trackers': [TL_A, TL_B]},
            {'size': 30 * GB, 'trackers': ['aither.cc', 'seedpool.org']},
            {'size': 60 * GB, 'trackers': ['aither.cc']},
        ]
        st = audit._compute_cross_seed_stats(media, self.ALIASES)
        self.assertAlmostEqual(st['multiplier'], (10 + 60 + 30 * 2) / 100)
        self.assertEqual([s['size'] for s in st['segments']], [0, 70 * GB, 30 * GB])
        by = {t['name']: t for t in st['tracker_stats']}
        self.assertNotIn(TL_B, by)
        self.assertEqual(by[TL_A]['only_size'], 10 * GB)
        self.assertEqual(by['aither.cc']['only_size'], 60 * GB)
        self.assertEqual(by['seedpool.org']['only_size'], 0)
        self.assertEqual(st['pairs'], [{'trackers': ['aither.cc', 'seedpool.org'], 'size': 30 * GB}])

    def test_tracker_file_stats_fold_the_alias(self):
        torrents = [{'size': 5, 'status': 'Seeding', 'imported': True, 'trackers': [TL_A, TL_B]}]
        st = audit._compute_tracker_file_stats(torrents, self.ALIASES)
        self.assertEqual(list(st), [TL_A])
        self.assertEqual(st[TL_A]['seeding_size'], 5)


# ---------------------------------------------------------------------------
# Stored history: read the way the latest scan names trackers
# ---------------------------------------------------------------------------

def _row(at, snap):
    return {'taken_at': at, 'snapshot': {'_instance_count': 1, **snap}, 'source': 'qui'}


class HistoryTests(unittest.TestCase):
    def _stats(self, rows, aliases):
        with patch.object(audit, 'db_get_upload_snapshots', return_value=rows), \
             patch.object(audit, 'db_get_meta', return_value=aliases):
            return audit.compute_upload_stats(30)

    def test_old_snapshots_join_the_new_series(self):
        """Before the fix both hosts were stored; after it, one. The series must
        neither double nor dip at the change."""
        rows = [
            _row('2026-11-10T08:00:00', {TL_A: {'uploaded': 100 * GB, 'seeding_size': 9 * GB},
                                          TL_B: {'uploaded': 100 * GB, 'seeding_size': 9 * GB},
                                          'xspeeds.eu:2710': {'uploaded': 50 * GB, 'seeding_size': 1}}),
            _row('2026-11-11T08:00:00', {TL_A: {'uploaded': 103 * GB, 'seeding_size': 9 * GB},
                                          TL_B: {'uploaded': 103 * GB, 'seeding_size': 9 * GB},
                                          'xspeeds.eu:2710': {'uploaded': 52 * GB, 'seeding_size': 1}}),
            _row('2026-11-12T08:00:00', {TL_A: {'uploaded': 105 * GB, 'seeding_size': 9 * GB},
                                          'xspeeds.eu': {'uploaded': 53 * GB, 'seeding_size': 1},
                                          '_aliases': {TL_B: TL_A}}),
        ]
        stats = self._stats(rows, {TL_B: TL_A})
        by_day = {d['date']: d['by_tracker'] for d in stats['daily_uploads']}
        self.assertEqual(by_day['2026-11-11'], {TL_A: 3 * GB, 'xspeeds.eu': 2 * GB})
        self.assertEqual(by_day['2026-11-12'], {TL_A: 2 * GB, 'xspeeds.eu': 1 * GB})
        self.assertEqual(stats['total_uploaded'], 8 * GB)
        self.assertEqual({t['tracker'] for t in stats['tracker_yields']}, {TL_A, 'xspeeds.eu'})

    def test_without_aliases_a_snapshot_reads_as_stored(self):
        snap = {'aither.cc': {'uploaded': 1}, '_library': {'x': 1}}
        self.assertEqual(audit._canon_upload_snapshot(snap, {}), snap)


class PreviousWindowTests(unittest.TestCase):
    def _prev(self, rows, **kw):
        with patch.object(audit, 'db_get_upload_snapshots', return_value=rows) as q, \
             patch.object(audit, 'db_get_meta', return_value={}):
            return audit.compute_previous_upload(**kw), q

    def test_the_window_before_a_from_to_range(self):
        rows = [_row('2026-10-01T00:30:00', {'aither.cc': {'uploaded': 10 * GB}}),
                _row('2026-10-15T00:00:00', {'aither.cc': {'uploaded': 14 * GB}}),
                _row('2026-10-30T23:00:00', {'aither.cc': {'uploaded': 20 * GB}})]
        prev, q = self._prev(rows, from_date='2026-10-31', to_date='2026-11-29')
        _, kw = q.call_args
        self.assertEqual(kw['from_date'][:10], '2026-10-01')
        self.assertEqual(kw['to_date'][:10], '2026-10-31')
        self.assertEqual(prev['total'], 10 * GB)
        self.assertEqual(prev['by_tracker'], {'aither.cc': 10 * GB})
        self.assertEqual(prev['days'], 30)

    def test_history_that_starts_inside_the_window_is_no_comparison(self):
        rows = [_row('2026-10-20T00:00:00', {'aither.cc': {'uploaded': 10 * GB}}),
                _row('2026-10-30T00:00:00', {'aither.cc': {'uploaded': 20 * GB}})]
        prev, _ = self._prev(rows, from_date='2026-10-31', to_date='2026-11-29')
        self.assertIsNone(prev)

    def test_all_has_no_window_before_it(self):
        prev, _ = self._prev([], days=0)
        self.assertIsNone(prev)


if __name__ == '__main__':
    unittest.main()
