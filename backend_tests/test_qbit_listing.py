"""SCAN SC6 — the qBittorrent listing keeps only what the file map reads.

`fetch_file_map` used to hold every torrent's file entries, eight fields each,
from the listing until the map was built, though only each file's name and
priority are read again. It keeps `(names, priorities)` per torrent now, and
reads a torrent's hash and category once rather than once per path.

R1 is the risk: a listing that failed must still reach the claim rule as `None`
(the disk fallback), and a client that answered with no files as `[]`.
`test_source_absence.py` holds the rest of that, unchanged.
"""

import os
import sys
import unittest
import weakref
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sources
from sources import _qbit


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Client:
    """qbittorrentapi's surface, with each file entry watched by a weak reference."""

    def __init__(self, torrents, files, fail_files=(), trackers=None):
        self._torrents   = torrents
        self._files      = files
        self._fail_files = set(fail_files)
        self._trackers   = trackers or {}
        self.entries     = []           # weak references, never the entries

    def auth_log_in(self):
        return True

    def torrents_info(self, torrent_hashes=None, **kw):
        return list(self._torrents)

    def torrents_trackers(self, torrent_hash=None):
        status, msg = self._trackers.get(torrent_hash, (2, ''))
        return [_Obj(url='http://tracker.example/announce', status=status, msg=msg)]

    def torrents_files(self, torrent_hash=None):
        if torrent_hash in self._fail_files:
            raise RuntimeError('file listing timed out')
        out = []
        for spec in self._files.get(torrent_hash, []):
            name, *prio = spec if isinstance(spec, tuple) else (spec,)
            entry = _Obj(name=name, size=1, progress=1.0, index=len(out),
                         is_seed=True, piece_range=[0, 0], availability=1.0,
                         **({'priority': prio[0]} if prio else {}))
            self.entries.append(weakref.ref(entry))
            out.append(entry)
        return out


def _torrent(h, name, category='movies', **kw):
    save_path = '/data/torrents/movies'
    fields = dict(hash=h, name=name, save_path=save_path, size=100,
                  state='uploading', uploaded=10, category=category,
                  seeding_time=3600, progress=1.0, completion_on=1789000000,
                  content_path=os.path.join(save_path, name))
    fields.update(kw)
    return _Obj(**fields)


_CFG = {'QB_HOST': 'http://localhost:8080', 'QB_USER': 'u', 'QB_PASS': 'p',
        'LOCAL_PATH': '/data/torrents', 'REMOTE_PATH': '/data/torrents'}


def _run(client, claims=None, isdir=(), walk=None):
    """`fetch_file_map` on `client`, recording what each torrent hands the claim
    rule and how many of the client's entries are still alive when it does."""
    real = _qbit.torrent_claimed_paths

    def spy(save_path, name, content_path, listed, complete, torrent_hash='',
            priorities=None):
        if claims is not None:
            claims.append({'hash': torrent_hash, 'listed': listed, 'priorities': priorities,
                           'alive': sum(r() is not None for r in client.entries)})
        return real(save_path, name, content_path, listed, complete,
                    torrent_hash=torrent_hash, priorities=priorities)

    with patch.object(_qbit.qbittorrentapi, 'Client', return_value=client), \
         patch.object(_qbit, 'torrent_claimed_paths', spy), \
         patch.object(sources.os.path, 'isfile', lambda p: False), \
         patch.object(sources.os.path, 'isdir', lambda p: p in isdir), \
         patch.object(sources.os, 'walk', lambda root: (walk or {}).get(root, [])):
        return _qbit.fetch_file_map(_CFG)


class QbitListingTests(unittest.TestCase):

    def test_the_clients_file_entries_are_let_go_before_the_map_is_built(self):
        """The saving itself: by the time the first torrent's paths are claimed,
        none of the client's entries is held. On the old code every one was."""
        client = _Client(
            [_torrent('aaa', 'Alpha'), _torrent('bbb', 'Bravo')],
            {'aaa': ['Alpha/a1.mkv', 'Alpha/a2.mkv'], 'bbb': ['Bravo/b.mkv']})
        claims = []
        file_map, _, _, _ = _run(client, claims)
        self.assertEqual(len(client.entries), 3)
        self.assertEqual([c['alive'] for c in claims], [0, 0])
        self.assertEqual(len(file_map), 3)

    def test_the_claim_rule_gets_names_and_priorities_in_the_clients_order(self):
        """A missing priority stays `None` per file, as `getattr` gave it before:
        it's what tells `part_file_claims` the listing didn't say."""
        client = _Client(
            [_torrent('aaa', 'Alpha'), _torrent('bbb', 'Bravo')],
            {'aaa': [('Alpha/a2.mkv', 1), ('Alpha/a1.mkv', 0), ('Alpha/a.nfo', 7)],
             'bbb': ['Bravo/b.mkv']})
        claims = []
        _run(client, claims)
        by_hash = {c['hash']: c for c in claims}
        self.assertEqual(by_hash['aaa']['listed'], ['Alpha/a2.mkv', 'Alpha/a1.mkv', 'Alpha/a.nfo'])
        self.assertEqual(by_hash['aaa']['priorities'], [1, 0, 7])
        self.assertEqual(by_hash['bbb']['listed'], ['Bravo/b.mkv'])
        self.assertEqual(by_hash['bbb']['priorities'], [None])

    def test_a_failed_listing_still_reaches_the_claim_rule_as_none(self):
        """R1: "could not ask" is never "the answer is no"."""
        client = _Client([_torrent('aaa', 'Alpha'), _torrent('bbb', 'Bravo')],
                         {'aaa': ['Alpha/a.mkv'], 'bbb': ['Bravo/b.mkv']},
                         fail_files={'bbb'})
        claims = []
        _, _, _, report = _run(client, claims)
        by_hash = {c['hash']: c for c in claims}
        self.assertIsNone(by_hash['bbb']['listed'])
        self.assertIsNone(by_hash['bbb']['priorities'])
        self.assertEqual(by_hash['aaa']['listed'], ['Alpha/a.mkv'])
        self.assertEqual(report['listing_failures'], 1)

    def test_a_client_that_lists_no_files_is_an_answer_not_a_failure(self):
        """The other side of R1: `[]` claims nothing, and the disk fallback that a
        failure would take is never taken, even with the folder there."""
        root = '/data/torrents/movies/Bravo'
        client = _Client([_torrent('aaa', 'Alpha'), _torrent('bbb', 'Bravo')],
                         {'aaa': ['Alpha/a.mkv'], 'bbb': []})
        claims = []
        file_map, _, _, report = _run(client, claims, isdir={root},
                                      walk={root: [(root, [], ['b.mkv'])]})
        by_hash = {c['hash']: c for c in claims}
        self.assertEqual(by_hash['bbb']['listed'], [])
        self.assertEqual(by_hash['bbb']['priorities'], [])
        self.assertNotIn(os.path.join(root, 'b.mkv'), file_map)
        self.assertEqual(report['listing_failures'], 0)
        self.assertEqual(report['listing_recovered'], 0)
        self.assertEqual(report['listing_unresolved'], 0)

    def test_a_shared_path_keeps_the_first_category_and_the_healthiest_hash(self):
        """The hash and category are read once per torrent now, so a record that
        two torrents claim is checked for both: the first claimant's category,
        the working torrent's hash, the dead one kept as an unregistered claimant."""
        client = _Client(
            [_torrent('dead', 'Shared', category='tv'),
             _torrent('live', 'Shared', category='tv-cross')],
            {'dead': ['Shared/s.mkv'], 'live': ['Shared/s.mkv']},
            trackers={'dead': (4, 'Unregistered torrent')})
        file_map, _, _, _ = _run(client)
        entry = file_map[os.path.join('/data/torrents/movies', 'Shared/s.mkv')]
        self.assertEqual(entry['hash'], 'live')
        self.assertEqual(entry['category'], 'tv')
        self.assertEqual(entry['tracker_health'], 'working')
        self.assertEqual(set(entry['unreg_claimants']), {'dead'})
        self.assertEqual(entry['unreg_claimants']['dead']['hash'], 'dead')


if __name__ == '__main__':
    unittest.main()
