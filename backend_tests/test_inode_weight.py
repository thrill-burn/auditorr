"""The weight of an inode entry and a record (SCAN SC9).

Once saving got light, the scan's memory peak was the assemble: the walk's
inode map and every file record alive together (1,034 MB traced at 650,000
files). Each trim here takes something off that without changing a stored byte:

- an inode no torrent claims has no tracker set of its own (one shared empty
  `frozenset`, where every inode had an empty `set`, 216 bytes), and a claim
  copies the client's set as `set.update` on an empty set did;
- `unreg_claimants` exists only on an inode that has one (an empty dict was 64
  bytes on every inode);
- every path of an inode appends the inode's one key tuple to the key order, not
  a fresh `(st_dev, st_ino)` with its two ints;
- the assemble marks an inode it has emitted on the inode entry (which has room:
  a 15-key dict holds 21), not in two sets of every key;
- a linked inode's library record takes its torrent record's `file_id` string
  and `trackers` list, which are equal, rather than building both again.

The walks run over real directories; the guard at the end holds the traced
bytes per inode at the assemble, the scan's peak.
"""

import os
import sys
import tracemalloc

import pytest

import audit
from audit import _assemble_records, _build_duplicate_map, _walk_directory

pytestmark = pytest.mark.skipif(not hasattr(os, 'link'), reason='needs hardlinks')


def _write(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def _claim(trackers, unreg=None, h='HASH'):
    entry = {'trackers': set(trackers), 'status': 'Seeding', 'hash': h, 'category': 'tv',
             'instance_id': None, 'instance_name': None, 'tracker_health': 'working',
             'tracker_msg': ''}
    if unreg:
        entry['unreg_claimants'] = dict(unreg)
    return entry


def _walk(tmp_path, file_map):
    torrents, media = str(tmp_path / 'torrents'), str(tmp_path / 'media')
    inode_map, total = {}, [0]
    torder, scanned, _, _ = _walk_directory(torrents, 'Torrent', inode_map, file_map, 0, 0,
                                            exclusion_patterns=[], total_ref=total)
    morder, _, _, _ = _walk_directory(media, 'Media', inode_map, None, scanned, 0,
                                      exclusion_patterns=[], total_ref=total)
    return inode_map, torder, morder


@pytest.fixture
def library(tmp_path):
    """A claimed file at two torrent paths (a cross-seed) and one library path, a
    claimed file nobody imported, an orphan, and a library file nothing seeds."""
    t, m = tmp_path / 'torrents', tmp_path / 'media'
    _write(str(t / 'a' / 'Show.S01E01.mkv'), b'one')
    os.makedirs(t / 'b', exist_ok=True)
    os.link(t / 'a' / 'Show.S01E01.mkv', t / 'b' / 'Show.S01E01.mkv')
    os.makedirs(m / 'Show', exist_ok=True)
    os.link(t / 'a' / 'Show.S01E01.mkv', m / 'Show' / 'S01E01.mkv')
    _write(str(t / 'a' / 'Show.S01E02.mkv'), b'two!')
    _write(str(t / 'stray' / 'old.mkv'), b'three')
    _write(str(m / 'Film' / 'Film.mkv'), b'four..')
    file_map = {
        str(t / 'a' / 'Show.S01E01.mkv'): _claim(['b.org', 'a.org'],
                                                 {'D1': {'hash': 'D1', 'n': 1}}),
        str(t / 'b' / 'Show.S01E01.mkv'): _claim(['c.org', 'a.org'],
                                                 {'D2': {'hash': 'D2'}, 'D1': {'hash': 'D1', 'n': 2}}),
        str(t / 'a' / 'Show.S01E02.mkv'): _claim(['a.org']),
    }
    return tmp_path, file_map


def _by_name(inode_map):
    return {os.path.basename((i['torrent_paths'] or i['media_paths'])[0]): i
            for i in inode_map.values()}


def test_an_inode_no_torrent_claims_has_no_tracker_set_or_claimant_dict(library):
    tmp_path, file_map = library
    inode_map, _, _ = _walk(tmp_path, file_map)
    infos = _by_name(inode_map)
    for name in ('old.mkv', 'Film.mkv'):
        assert infos[name]['trackers'] is audit._NO_TRACKERS
        assert 'unreg_claimants' not in infos[name]
    assert 'unreg_claimants' not in infos['Show.S01E02.mkv']


def test_a_claim_builds_the_set_and_the_claimants_as_before(library):
    tmp_path, file_map = library
    before = {p: (set(e['trackers']), dict(e.get('unreg_claimants') or {}))
              for p, e in file_map.items()}
    inode_map, _, _ = _walk(tmp_path, file_map)
    info = _by_name(inode_map)['Show.S01E01.mkv']

    # The order `set().update(...)` per path gave, and so the stored list's.
    expected = set()
    for p in info['torrent_paths']:
        expected.update(file_map[p]['trackers'])
    assert list(info['trackers']) == list(expected)
    assert list(info['unreg_claimants'].items()) == [('D1', {'hash': 'D1', 'n': 2}),
                                                     ('D2', {'hash': 'D2'})]
    # The inode's set and dict are its own: the client's entries are untouched.
    for p, e in file_map.items():
        assert (e['trackers'], e.get('unreg_claimants') or {}) == before[p]
        assert e['trackers'] is not info['trackers']


def test_every_path_of_an_inode_appends_its_one_key(library):
    tmp_path, file_map = library
    inode_map, torder, morder = _walk(tmp_path, file_map)
    keys = {k: k for k in inode_map}
    assert len(torder) == 4 and len(morder) == 2
    for k in torder + morder:
        assert k is keys[k]
        assert inode_map[k]['key'] is k


def test_a_linked_library_record_shares_its_torrent_records_id_and_trackers(library):
    tmp_path, file_map = library
    inode_map, torder, morder = _walk(tmp_path, file_map)
    torrents, media = _assemble_records(torder, morder, inode_map, _build_duplicate_map(inode_map))

    # One record per inode whatever its paths.
    assert len(torrents) == 3 and len(media) == 2
    t = {os.path.basename(r['path']): r for r in torrents}
    m = {os.path.basename(r['path']): r for r in media}
    linked, film = m['S01E01.mkv'], m['Film.mkv']
    assert linked['file_id'] is t['Show.S01E01.mkv']['file_id']
    assert linked['trackers'] is t['Show.S01E01.mkv']['trackers']
    assert linked['trackers'] == list(_by_name(inode_map)['Show.S01E01.mkv']['trackers'])
    # A library file nothing seeds builds its own, as before.
    key = next(k for k, i in inode_map.items() if i['media_paths'] and not i['torrent_paths'])
    assert film['file_id'] == f'{key[0]}:{key[1]}'
    assert film['trackers'] == ['None'] and t['old.mkv']['trackers'] == ['None']


def test_the_assemble_holds_less_per_inode(tmp_path):
    """The traced peak per inode through the walks and the assemble, where the
    inode map and every record are alive together: the scan's traced peak. 600 files: a third claimed and linked, a
    third claimed only, a third library only, less the full paths' own bytes
    (their length is the temp folder's). Linux 3.12: 1,884 bytes before SC9's
    trims, 1,604 after, and each trim undone alone 1,609 to 1,671 (each fails
    its own test above); Windows 3.14 after: 1,730. The bound sits between."""
    t, m = tmp_path / 'torrents', tmp_path / 'media'
    file_map = {}
    for i in range(200):
        a = t / 'tv' / f'Show.S01E{i:03}.mkv'
        _write(str(a), b'x' * (i + 1))
        os.makedirs(m / 'Show', exist_ok=True)
        os.link(a, m / 'Show' / f'E{i:03}.mkv')
        b = t / 'tv' / f'Other.S01E{i:03}.mkv'
        _write(str(b), b'y' * (i + 1))
        file_map[str(a)] = _claim(['a.org', 'b.org'])
        file_map[str(b)] = _claim(['a.org'])
        _write(str(m / 'Films' / f'Film{i:03}.mkv'), b'z' * (i + 1))
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        inode_map, torder, morder = _walk(tmp_path, file_map)
        records = _assemble_records(torder, morder, inode_map, {})
        held = tracemalloc.get_traced_memory()[1] - before
    finally:
        tracemalloc.stop()
    assert len(records[0]) == 400 and len(records[1]) == 400
    # Less the full path strings, whose length is the temp folder's.
    held -= sum(sys.getsizeof(p) for i in inode_map.values()
                for p in i['torrent_paths'] + i['media_paths'])
    if os.environ.get('GUARD_PRINT'):
        print(f'traced peak per inode {held / len(inode_map):.0f}')
    assert held / len(inode_map) < 1800
