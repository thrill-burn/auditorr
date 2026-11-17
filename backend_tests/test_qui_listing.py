"""SCAN D3 — the qui listing keeps only what the file map reads.

SC6's change for qui. `_fetch_torrent_data` normalised every file into a
three-key dict and `files_map` held them all until the map was built, though
only each file's name and priority are read again. It keeps `(names,
priorities)` per torrent now (by `_norm_file`'s rules), and a path's entry is
built once, with the torrent's tracker health read once per torrent. On the
harness's 650,000-file library the listing's peak went 544 → 467 MB.

R1 on qui: a failed listing *and an empty one* reach the claim rule as `None`
(qui may not expose per-torrent file lists, so an empty answer isn't evidence),
and only a failure is counted as one. `test_source_absence.py` holds the rest.
"""

import os
import sys
import tracemalloc
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sources
from sources import _qui

_CFG = {'QUI_HOST': 'http://qui:7476', 'QUI_API_KEY': 'k', 'TORRENT_SOURCE': 'qui',
        'LOCAL_PATH': '/data/torrents', 'REMOTE_PATH': '/data/torrents'}
_SAVE = '/data/torrents/movies'


class _Resp:
    def __init__(self, payload, ok=True):
        self._payload, self._ok = payload, ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError('HTTP 500')

    def json(self):
        return self._payload


def _torrent(h, category='movies'):
    return {'hash': h, 'name': h, 'save_path': _SAVE, 'content_path': f'{_SAVE}/{h}',
            'size': 1, 'state': 'uploading', 'progress': 1.0, 'completion_on': 1789000000,
            'category': category, 'uploaded': 0}


class _Session:
    """qui's HTTP edge. Every answer is built afresh, as `resp.json()` does, so
    nothing here holds what the listing is meant to let go."""
    headers = {}

    def __init__(self, torrents, files, fail_files=(), trackers=None):
        self._torrents, self._files = torrents, files
        self._fail_files, self._trackers = set(fail_files), trackers or {}

    def get(self, url, params=None, **kw):
        if url.endswith('/api/instances'):
            return _Resp([{'id': 1, 'name': 'main', 'connected': True,
                           'hasLocalFilesystemAccess': True}])
        if url.endswith('/torrents'):
            return _Resp({'torrents': [dict(t) for t in self._torrents],
                          'total': len(self._torrents)})
        h, what = url.split('/torrents/')[1].split('/')[:2]
        if what == 'trackers':
            status, msg = self._trackers.get(h, (2, ''))
            return _Resp([{'url': 'https://tracker.example/announce', 'status': status, 'msg': msg}])
        if h in self._fail_files:
            return _Resp(None, ok=False)
        spec = self._files.get(h, [])
        return _Resp(spec() if callable(spec) else [dict(f) for f in spec])


def _run(sess, claims=None, isdir=(), walk=None, held=None):
    """`fetch_file_map` on `sess`, recording what each torrent hands the claim
    rule, and (with `held`) the traced bytes held when the first one does."""
    real = _qui.torrent_claimed_paths

    def spy(save_path, name, content_path, listed, complete, torrent_hash='', priorities=None):
        if held is not None and not held:
            held.append(tracemalloc.get_traced_memory()[0])
        if claims is not None:
            claims.append({'hash': torrent_hash, 'listed': listed, 'priorities': priorities})
        return real(save_path, name, content_path, listed, complete,
                    torrent_hash=torrent_hash, priorities=priorities)

    with patch.object(_qui, '_session', return_value=sess), \
         patch.object(_qui, 'torrent_claimed_paths', spy), \
         patch.object(sources.os.path, 'isfile', lambda p: False), \
         patch.object(sources.os.path, 'isdir', lambda p: p in isdir), \
         patch.object(sources.os, 'walk', lambda root: (walk or {}).get(root, [])):
        return _qui.fetch_file_map(_CFG)


class QuiListingTests(unittest.TestCase):

    def test_the_claim_rule_gets_names_and_priorities_in_the_clients_order(self):
        """`_norm_file`'s rules: `path` when there's no `name`, a priority of 0
        kept, a missing one `None`, per file."""
        sess = _Session([_torrent('aaa')], {'aaa': [
            {'name': 'aaa/2.mkv', 'priority': 1, 'size': 5},
            {'path': 'aaa/1.mkv', 'priority': 0, 'size': 5},
            {'name': 'aaa/a.nfo', 'size': 1}]})
        claims = []
        file_map, _, _, report = _run(sess, claims)
        self.assertEqual(claims, [{'hash': 'aaa', 'listed': ['aaa/2.mkv', 'aaa/1.mkv', 'aaa/a.nfo'],
                                   'priorities': [1, 0, None]}])
        self.assertEqual(len(file_map), 3)
        self.assertEqual(report['listing_failures'], 0)

    def test_a_failed_listing_reaches_the_claim_rule_as_none_and_is_counted(self):
        sess = _Session([_torrent('aaa'), _torrent('bbb')],
                        {'aaa': [{'name': 'aaa/a.mkv'}], 'bbb': [{'name': 'bbb/b.mkv'}]},
                        fail_files={'bbb'})
        claims = []
        _, _, _, report = _run(sess, claims)
        by_hash = {c['hash']: c for c in claims}
        self.assertIsNone(by_hash['bbb']['listed'])
        self.assertIsNone(by_hash['bbb']['priorities'])
        self.assertEqual(by_hash['aaa']['listed'], ['aaa/a.mkv'])
        self.assertEqual(report['listing_failures'], 1)

    def test_an_empty_listing_takes_the_disk_fallback_and_is_not_a_failure(self):
        """qui's own rule, kept: an empty answer claims the folder from disk."""
        root = f'{_SAVE}/bbb'
        sess = _Session([_torrent('aaa'), _torrent('bbb')],
                        {'aaa': [{'name': 'aaa/a.mkv'}], 'bbb': []})
        claims = []
        file_map, _, _, report = _run(sess, claims, isdir={root},
                                      walk={root: [(root, [], ['b.mkv'])]})
        by_hash = {c['hash']: c for c in claims}
        self.assertIsNone(by_hash['bbb']['listed'])
        self.assertIsNone(by_hash['bbb']['priorities'])
        self.assertIn(os.path.join(root, 'b.mkv'), file_map)
        self.assertEqual(report['listing_failures'], 0)

    def test_a_file_entry_that_isnt_an_object_fails_the_listing(self):
        """As `_norm_file` did: an answer that can't be read is "could not ask"."""
        sess = _Session([_torrent('aaa')], {'aaa': lambda: ['aaa/a.mkv']})
        claims = []
        _, _, _, report = _run(sess, claims)
        self.assertIsNone(claims[0]['listed'])
        self.assertEqual(report['listing_failures'], 1)

    def test_the_listing_holds_no_dict_per_file(self):
        """The saving itself. When the first torrent's paths are claimed, what
        the listing holds is about 80 bytes a file: its name and two list slots.
        A dict per file (`_norm_file`'s) held about 290 (288 measured, 80 now)."""
        n_torrents, per = 20, 500
        files = {f't{i:02d}': (lambda i=i: [{'name': f't{i:02d}/{j}.mkv', 'size': 10**9 + j,
                                              'priority': 1} for j in range(per)])
                 for i in range(n_torrents)}
        sess = _Session([_torrent(h) for h in files], files)
        held = []
        tracemalloc.start()
        try:
            start = tracemalloc.get_traced_memory()[0]
            _run(sess, held=held)
        finally:
            tracemalloc.stop()
        per_file = (held[0] - start) / (n_torrents * per)
        self.assertLess(per_file, 150, f'{per_file:.0f} bytes held per listed file')

    def test_each_torrent_brings_its_own_health_and_a_shared_path_the_healthiest(self):
        """Health is read once per torrent now, so check every torrent gets its
        own: a dead torrent's path is dead, a working one's working, and a path
        both claim keeps the first category, the working hash and the dead one
        as an unregistered claimant."""
        sess = _Session(
            [_torrent('dead', category='tv'), _torrent('live', category='tv-cross')],
            {'dead': [{'name': 'shared/s.mkv'}, {'name': 'dead/d.mkv'}],
             'live': [{'name': 'shared/s.mkv'}, {'name': 'live/l.mkv'}]},
            trackers={'dead': (4, 'Unregistered torrent')})
        file_map, _, _, _ = _run(sess)
        # The listing's join is `os.path.join`, deliberately (CLAUDE.md, C4).
        self.assertEqual(file_map[os.path.join(_SAVE, 'dead/d.mkv')]['tracker_health'], 'unregistered')
        self.assertEqual(file_map[os.path.join(_SAVE, 'live/l.mkv')]['tracker_health'], 'working')
        shared = file_map[os.path.join(_SAVE, 'shared/s.mkv')]
        self.assertEqual(shared['hash'], 'live')
        self.assertEqual(shared['category'], 'tv')
        self.assertEqual(shared['tracker_health'], 'working')
        self.assertEqual(shared['instance_id'], 1)
        self.assertEqual(set(shared['unreg_claimants']), {'dead'})


if __name__ == '__main__':
    unittest.main()
