"""What a tracker's Seeding size counts (2026-11-23).

Three changes, each from a report that the Trackers page read lower than the
tracker did:

- **A queued or checking seed is seeding** (`sources.torrent_status`, one rule
  for both backends). Only `uploading`, `stalledUP` and `forcedUP` counted, so a
  client that queues its seeds read low all the time, and a scan landing while
  the client was starting up read low until the next one.
- **An excluded file a torrent seeds is seeding.** An exclusion takes a file out
  of the workflows, and it used to take it out of its tracker's figure too.
- **A tracker list that fails is asked for once more** (`retry_tracker_lists`),
  after the first pass and held to a time budget. A failure files the torrent
  under Unknown for the scan.
"""

import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sources
from sources import _qbit, _qui, torrent_status, retry_tracker_lists
import audit

DONE = 1789000000


# ---------------------------------------------------------------------------
# The status rule
# ---------------------------------------------------------------------------

class TorrentStatusTests(unittest.TestCase):
    def test_every_seed_state_is_seeding(self):
        for state in ('uploading', 'stalledUP', 'forcedUP', 'queuedUP', 'checkingUP',
                      'forcedUploadingUP'):
            self.assertEqual(torrent_status(state, 1.0, DONE), 'Seeding', state)

    def test_a_checking_seed_is_seeding_whatever_the_check_has_reached(self):
        """qBittorrent reports the check's own progress while it checks."""
        self.assertEqual(torrent_status('checkingUP', 0.3, DONE), 'Seeding')

    def test_downloads_are_downloading(self):
        """`forcedDL` read Paused on qbit and Downloading on qui."""
        for state in ('downloading', 'stalledDL', 'forcedDL', 'forcedDownloadingDL'):
            self.assertEqual(torrent_status(state, 0.5, 0), 'Downloading', state)

    def test_what_nobody_seeds_is_paused(self):
        for state in ('stoppedUP', 'pausedUP', 'error', 'missingFiles', 'stoppedDL',
                      'queuedDL', 'checkingDL', 'metaDL', 'allocating', 'unknown', ''):
            self.assertEqual(torrent_status(state, 1.0, DONE), 'Paused', state)

    def test_a_move_is_seeding_once_the_torrent_is_whole(self):
        self.assertEqual(torrent_status('moving', 1.0, DONE), 'Seeding')
        self.assertEqual(torrent_status('moving', 0.4, DONE), 'Paused')
        self.assertEqual(torrent_status('moving', None, DONE), 'Seeding')
        self.assertEqual(torrent_status('moving', None, None), 'Paused')

    def test_a_startup_check_goes_by_completion_on_not_progress(self):
        """At startup every torrent is `checkingResumeData`, and its progress is
        the check's. Read as completion, a startup scan dropped every seed."""
        self.assertEqual(torrent_status('checkingResumeData', 0.0, DONE), 'Seeding')
        self.assertEqual(torrent_status('checkingResumeData', 0.0, 0), 'Paused')
        self.assertEqual(torrent_status('checkingResumeData', 0.0, -1), 'Paused')
        self.assertEqual(torrent_status('checkingResumeData', 1.0, None), 'Paused')


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _qbit_torrent(h, name, state='uploading', progress=1.0, completion_on=DONE, size=100):
    save = '/data/torrents/movies'
    return _Obj(hash=h, name=name, save_path=save, size=size, state=state, uploaded=10,
                category='movies', seeding_time=3600, progress=progress,
                completion_on=completion_on, content_path=f'{save}/{name}')


class _QbtClient:
    """`flaky` maps a hash to how many of its tracker calls fail before one answers."""

    def __init__(self, torrents, flaky=None):
        self._torrents = torrents
        self._flaky = dict(flaky or {})
        self._lock = threading.Lock()
        self.tracker_calls = {}

    def auth_log_in(self):
        return True

    def torrents_info(self, torrent_hashes=None, **kw):
        return list(self._torrents)

    def torrents_trackers(self, torrent_hash=None):
        with self._lock:
            n = self.tracker_calls[torrent_hash] = self.tracker_calls.get(torrent_hash, 0) + 1
        if n <= self._flaky.get(torrent_hash, 0):
            raise RuntimeError('tracker call timed out')
        return [_Obj(url='http://tracker.example/announce', status=2, msg='')]

    def torrents_files(self, torrent_hash=None):
        t = next(t for t in self._torrents if t.hash == torrent_hash)
        return [_Obj(name=f'{t.name}/{t.name}.mkv', priority=1)]


def _run_qbit(torrents, flaky=None):
    client = _QbtClient(torrents, flaky)
    cfg = {'QB_HOST': 'http://localhost:8080', 'QB_USER': 'u', 'QB_PASS': 'p',
           'LOCAL_PATH': '/data/torrents', 'REMOTE_PATH': '/data/torrents'}
    with patch.object(_qbit.qbittorrentapi, 'Client', return_value=client):
        return _qbit.fetch_file_map(cfg) + (client,)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _run_qui(torrents, flaky=None):
    """`torrents` as `(hash, name, state, progress, completion_on)`."""
    rows = [{'hash': h, 'name': n, 'save_path': '/data/torrents/movies', 'size': 100,
             'state': st, 'uploaded': 10, 'progress': p, 'completion_on': c,
             'content_path': f'/data/torrents/movies/{n}'}
            for h, n, st, p, c in torrents]
    names = {h: n for h, n, *_ in torrents}
    flaky = dict(flaky or {})
    calls = {}
    lock = threading.Lock()

    class _Sess:
        headers = {}

        def get(_self, url, **kw):
            if url.endswith('/api/instances'):
                return _Resp([{'id': 1, 'name': 'main', 'connected': True,
                               'hasLocalFilesystemAccess': True}])
            if url.endswith('/torrents'):
                page = (kw.get('params') or {}).get('page', 0)
                return _Resp({'torrents': rows if page == 0 else [], 'total': len(rows)})
            h = url.split('/torrents/')[1].split('/')[0]
            if url.endswith('/trackers'):
                with lock:
                    n = calls[h] = calls.get(h, 0) + 1
                if n <= flaky.get(h, 0):
                    raise RuntimeError('tracker call timed out')
                return _Resp([{'url': 'http://tracker.example/announce', 'status': 2, 'msg': ''}])
            return _Resp([{'name': f'{names[h]}/{names[h]}.mkv', 'priority': 1}])

    with patch.object(_qui, '_session', return_value=_Sess()):
        out = _qui.fetch_file_map({'QUI_HOST': 'http://qui:7476', 'QUI_API_KEY': 'k',
                                   'LOCAL_PATH': '/data/torrents',
                                   'REMOTE_PATH': '/data/torrents'})
    return out + (calls,)


def _status_of(file_map, name):
    [rec] = [r for p, r in file_map.items() if p.endswith(f'{name}.mkv')]
    return rec['status']


# ---------------------------------------------------------------------------
# Both backends read states by the one rule
# ---------------------------------------------------------------------------

class BackendStatusTests(unittest.TestCase):
    CASES = [
        ('a1', 'Queued',   'queuedUP',           1.0, DONE, 'Seeding'),
        ('a2', 'Checking', 'checkingUP',         0.2, DONE, 'Seeding'),
        ('a3', 'Starting', 'checkingResumeData', 0.0, DONE, 'Seeding'),
        ('a4', 'Moving',   'moving',             1.0, DONE, 'Seeding'),
        ('a5', 'Stopped',  'stoppedUP',          1.0, DONE, 'Paused'),
        ('a6', 'Broken',   'error',              1.0, DONE, 'Paused'),
        ('a7', 'Forced',   'forcedDL',           0.5, 0,    'Downloading'),
    ]

    def test_qbit(self):
        file_map, _, snap, _, _ = _run_qbit(
            [_qbit_torrent(h, n, st, p, c) for h, n, st, p, c, _ in self.CASES])
        for _, name, state, *_, want in self.CASES:
            self.assertEqual(_status_of(file_map, name), want, state)
        # The client's own per-tracker figure follows the same rule.
        self.assertEqual(snap['tracker.example']['seeding_size'], 4 * 100)

    def test_qui(self):
        file_map, _, snap, _, _ = _run_qui([case[:5] for case in self.CASES])
        for _, name, state, *_, want in self.CASES:
            self.assertEqual(_status_of(file_map, name), want, state)
        self.assertEqual(snap['tracker.example']['seeding_size'], 4 * 100)

    def test_a_startup_scan_keeps_its_incomplete_flag(self):
        """Seeding is what the client is doing; whether the bytes are whole is
        R2's question, and a startup check's progress still says not yet."""
        file_map, *_ = _run_qbit([_qbit_torrent('a3', 'Starting', 'checkingResumeData', 0.0)])
        [rec] = [r for p, r in file_map.items() if p.endswith('Starting.mkv')]
        self.assertEqual(rec['status'], 'Seeding')
        self.assertTrue(rec.get('incomplete'))


# ---------------------------------------------------------------------------
# A failed tracker list is asked for once more
# ---------------------------------------------------------------------------

class TrackerRetryTests(unittest.TestCase):
    def test_qbit_a_list_that_answers_the_second_time_counts(self):
        file_map, trackers, snap, report, client = _run_qbit(
            [_qbit_torrent('aaa', 'Alpha'), _qbit_torrent('bbb', 'Bravo')], flaky={'bbb': 1})
        self.assertEqual(report['tracker_failures'], 0)
        self.assertNotIn('_trackers_unanswered', snap)
        self.assertNotIn('Unknown', snap)
        self.assertEqual(snap['tracker.example']['uploaded'], 20)
        self.assertEqual(trackers, ['tracker.example'])
        self.assertEqual(client.tracker_calls, {'aaa': 1, 'bbb': 2})
        [rec] = [r for p, r in file_map.items() if p.endswith('Bravo.mkv')]
        self.assertEqual(rec['tracker_health'], 'working')

    def test_qbit_asks_twice_and_no_more(self):
        _, _, snap, report, client = _run_qbit(
            [_qbit_torrent('aaa', 'Alpha'), _qbit_torrent('bbb', 'Bravo')], flaky={'bbb': 9})
        self.assertEqual(report['tracker_failures'], 1)
        self.assertEqual(snap['_trackers_unanswered'], 1)
        self.assertEqual(client.tracker_calls['bbb'], 2)

    def test_qbit_a_healthy_scan_asks_once(self):
        _, _, _, _, client = _run_qbit([_qbit_torrent('aaa', 'Alpha')])
        self.assertEqual(client.tracker_calls, {'aaa': 1})

    def test_qui_a_list_that_answers_the_second_time_counts(self):
        _, _, snap, report, calls = _run_qui(
            [('aaa', 'Alpha', 'uploading', 1.0, DONE), ('bbb', 'Bravo', 'uploading', 1.0, DONE)],
            flaky={'bbb': 1})
        self.assertEqual(report['tracker_failures'], 0)
        self.assertNotIn('_trackers_unanswered', snap)
        self.assertNotIn('Unknown', snap)
        self.assertEqual(calls, {'aaa': 1, 'bbb': 2})

    def test_qui_asks_twice_and_no_more(self):
        _, _, snap, report, calls = _run_qui(
            [('aaa', 'Alpha', 'uploading', 1.0, DONE), ('bbb', 'Bravo', 'uploading', 1.0, DONE)],
            flaky={'bbb': 9})
        self.assertEqual(report['tracker_failures'], 1)
        self.assertEqual(snap['_trackers_unanswered'], 1)
        self.assertEqual(calls['bbb'], 2)

    def test_the_second_ask_is_held_to_its_budget(self):
        """A client that's down must not wait out every call's timeout a second
        time: what hasn't answered when the budget runs out stays failed."""
        release = threading.Event()

        def ask(key):
            if key == 'slow':
                release.wait(5)
                return 'late'
            return 'quick'

        with patch.object(sources, 'TRACKER_RETRY_SECS', 0.3):
            start = time.monotonic()
            got = retry_tracker_lists(['fast', 'slow'], ask, 4)
            took = time.monotonic() - start
        release.set()
        self.assertEqual(got, {'fast': 'quick'})
        self.assertLess(took, 2)

    def test_a_call_that_raises_again_is_left_out(self):
        def ask(key):
            if key == 'bad':
                raise RuntimeError('timed out')
            return key.upper()
        self.assertEqual(retry_tracker_lists(['ok', 'bad'], ask, 4), {'ok': 'OK'})
        self.assertEqual(retry_tracker_lists([], ask, 4), {})


# ---------------------------------------------------------------------------
# The per-tracker figures: excluded files still seed
# ---------------------------------------------------------------------------

class TrackerFileStatsTests(unittest.TestCase):
    def _stats(self, records):
        return audit._compute_tracker_file_stats(
            [{'trackers': ['tracker.example'], 'imported': False, **r} for r in records]
        )['tracker.example']

    def test_an_excluded_file_a_torrent_seeds_counts_as_seeding(self):
        st = self._stats([
            {'size': 1000, 'status': 'Seeding', 'excluded': False, 'imported': True},
            {'size': 40, 'status': 'Seeding', 'excluded': True},
        ])
        self.assertEqual((st['seeding_count'], st['seeding_size']), (2, 1040))

    def test_but_never_in_a_workflow_count(self):
        st = self._stats([
            {'size': 40, 'status': 'Seeding', 'excluded': True},
            {'size': 7, 'status': 'Orphaned', 'excluded': True},
            {'size': 5, 'status': 'Paused', 'excluded': True},
        ])
        self.assertEqual(st['not_imported_size'], 0)
        self.assertEqual(st['orphaned_size'], 0)
        self.assertEqual(st['seeding_size'], 40)

    def test_the_workflow_counts_are_what_they_were(self):
        st = self._stats([
            {'size': 1, 'status': 'Seeding', 'excluded': False},
            {'size': 2, 'status': 'Seeding', 'excluded': False, 'imported': True},
            {'size': 4, 'status': 'Seeding', 'excluded': False, 'sidecar': True},
            {'size': 8, 'status': 'Orphaned', 'excluded': False},
            {'size': 16, 'status': 'Paused', 'excluded': False},
            {'size': 32, 'status': 'Downloading', 'excluded': False},
        ])
        self.assertEqual(st, {
            'seeding_count': 3, 'seeding_size': 7,
            'orphaned_count': 1, 'orphaned_size': 8,
            'not_imported_count': 3, 'not_imported_size': 1 + 16 + 32,
        })


if __name__ == '__main__':
    unittest.main()
