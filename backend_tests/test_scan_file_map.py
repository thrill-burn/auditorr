"""The client's file map goes before the media walk (SCAN SC5).

`fetch_file_map`'s answer holds an entry for every path a torrent claims: about
200 MB at 650,000 files. Only the torrent walk reads it. The two guards that
follow read the report's counts, and the media walk asks the client nothing.
It used to be held through the media walk all the same. It's now dropped once
the torrent root's guards have passed, and the media walk is handed no map.

The test runs the real `run_audit_process` and watches the map itself with a
weak reference, so it holds the release, not only the `del`: a reference kept
anywhere else (a closure of the walk, a cycle) fails it too.
"""

import weakref
from unittest.mock import patch

import audit
from backend_tests.test_publish_atomicity import _answer, real_db, run_scan, stored


class _Map(dict):
    """A dict a weak reference can watch (a plain dict can't be)."""


def test_the_file_map_is_gone_before_the_media_walk(tmp_path):
    watch, walks = [], []

    def fetch(*args, **kwargs):
        file_map, trackers, snapshot, report = _answer(str(tmp_path / 'torrents'), 3)
        file_map = _Map(file_map)
        watch.append(weakref.ref(file_map))
        return file_map, trackers, snapshot, report

    real_walk = audit._walk_directory

    def walk(base, label, inode_map, file_map, *args, **kwargs):
        walks.append((label, file_map is None, watch[0]() is None))
        return real_walk(base, label, inode_map, file_map, *args, **kwargs)

    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=3, media=2, extra=(
            patch('audit.sources.fetch_file_map', new=fetch),
            patch('audit._walk_directory', new=walk)))
        out = stored(path)

    # (root, handed no map, the map is gone): the torrent walk reads it, and
    # by the media walk nothing holds it.
    assert walks == [('Torrent', False, False), ('Media', True, True)]
    # The scan stored what it always did, every torrent file claimed.
    assert [run['status'] for run in out['runs']] == ['ok']
    assert len(out['torrents']) == 3 and len(out['media']) == 2
    assert all(record['status'] == 'Seeding' for record in out['torrents'])


def test_running_out_of_memory_in_the_media_walk_is_still_reported(tmp_path):
    """The `MemoryError` handler drops the scan's structures by name, and from
    the release to the scan's end the map's name is unbound. The media walk is
    the stretch that's new: it has to stay a handled case, reported as a
    failed run with the crash marker cleared."""
    real_walk = audit._walk_directory

    def walk(base, label, *args, **kwargs):
        if label == 'Media':
            raise MemoryError()
        return real_walk(base, label, *args, **kwargs)

    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=3, media=2,
                 extra=(patch('audit._walk_directory', new=walk),))
        out = stored(path)

    assert [run['status'] for run in out['runs']] == ['error']
    assert 'out of memory' in out['runs'][0]['error_message']
    assert out['scan_marker'] is None
