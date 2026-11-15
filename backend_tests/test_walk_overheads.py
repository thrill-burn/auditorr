"""What the walk does once per file (SCAN SC4).

`_walk_directory` visits every file on disk, so anything it does per file is
multiplied by the library. A file's path relative to its root was
`os.path.relpath(full_path, base_path)` per file, which normalises both paths
every time: 1.15 s of the two walks at 650,000 files. It's now the folder's
relative path, worked out once per folder, joined to the file's name.

That path is stored (`torrent_rel_path`, `media_rel_path`) and is what the
exclusion matcher reads, so it has to be the same string for every file under
every spelling of a root. The tests walk a real tree and hold each path to
`os.path.relpath`'s own answer.

The walk's progress writes are the second half of this file.
"""

import os
from unittest.mock import patch

import pytest

import audit
from audit import _walk_directory
from state import get_state, set_state

# A file at the root, nested folders, and names that aren't ASCII, start with
# dots, or hold spaces.
LAYOUT = (
    'top.mkv',
    'movies/Rel/a.mkv',
    'movies/Rel/Subs/a.en.srt',
    'tv/Show/Season 01/e1.mkv',
    'tv/Show/Season 01/e2.mkv',
    'anime/[Group] Tōhoku 東北/第01話.mkv',
    '...And Justice for All (1979)/film.mkv',
    'odd/a b/..c.txt',
)


def _tree(parent):
    lib = parent / 'lib'
    for rel in LAYOUT:
        path = lib.joinpath(*rel.split('/'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    return lib


# Every way a root can be spelled. Each takes the tree's folder and the test's
# `monkeypatch` (a relative root needs a working directory) and returns the
# string the walk is given.
def _plain(lib, mp):
    return str(lib)


def _trailing_separator(lib, mp):
    return str(lib) + os.sep


def _doubled_separator(lib, mp):
    return str(lib.parent) + os.sep + os.sep + 'lib'


def _dot_segment(lib, mp):
    return os.path.join(str(lib.parent), '.', 'lib')


def _up_and_back(lib, mp):
    return os.path.join(str(lib), 'movies', '..')


def _relative(lib, mp):
    mp.chdir(lib.parent)
    return 'lib'


def _relative_dot_slash(lib, mp):
    mp.chdir(lib.parent)
    return os.path.join('.', 'lib')


def _relative_up_and_back(lib, mp):
    mp.chdir(lib.parent)
    return os.path.join('lib', '..', 'lib')


def _the_working_directory(lib, mp):
    mp.chdir(lib)
    return '.'


def _two_leading_slashes(lib, mp):
    if os.sep != '/':
        pytest.skip('POSIX only: a path that starts with exactly two slashes')
    return '/' + str(lib)


SPELLINGS = [_plain, _trailing_separator, _doubled_separator, _dot_segment, _up_and_back,
             _relative, _relative_dot_slash, _relative_up_and_back, _the_working_directory,
             _two_leading_slashes]


class _Matcher:
    """Stands in for the compiled exclusions, keeping what the walk hands it."""

    def __init__(self):
        self.asked = []

    def match(self, full_path, rel_path, filename):
        self.asked.append((full_path, rel_path, filename))
        return False


@pytest.mark.parametrize('parent', ['plain', 'médiathèque 図書館'], ids=['ascii', 'non-ascii'])
@pytest.mark.parametrize('spelling', SPELLINGS, ids=lambda f: f.__name__.strip('_'))
@pytest.mark.parametrize('label, rel_key, paths_key', [
    ('Torrent', 'torrent_rel_path', 'torrent_paths'),
    ('Media', 'media_rel_path', 'media_paths')], ids=['torrents', 'media'])
def test_a_files_relative_path_is_relpaths_own(tmp_path, monkeypatch, parent, spelling,
                                               label, rel_key, paths_key):
    lib = _tree(tmp_path / parent)
    base = spelling(lib, monkeypatch)
    inode_map, matcher = {}, _Matcher()

    order, scanned, errors, _ = _walk_directory(
        base, label, inode_map, {}, 0, 0, exclusion_patterns=[], total_ref=[0],
        compiled_exclusions=matcher)

    assert errors == 0
    assert scanned == len(order) == len(inode_map) == len(LAYOUT)
    # What's stored, per file.
    for info in inode_map.values():
        assert info[rel_key] == os.path.relpath(info[paths_key][0], base)
    assert ({info[rel_key] for info in inode_map.values()}
            == {rel.replace('/', os.sep) for rel in LAYOUT})
    # What the matcher is asked about, per file.
    assert len(matcher.asked) == len(LAYOUT)
    for full_path, rel_path, filename in matcher.asked:
        assert rel_path == os.path.relpath(full_path, base)
        assert os.path.basename(rel_path) == filename


# ── Progress ─────────────────────────────────────────────────────────────────
# The walk also told `state` its count after every file: a lock and a division
# each, 0.1 to 0.3 s of the two walks at 650,000 files, for a figure the page
# polls every few seconds. It's written every 128 files now, and once when the
# walk ends, so during a walk the count can be up to 127 files behind and the
# count a walk leaves is exact.

def _walk_counting(tmp_path, files, already=0):
    """Walk `files` files as a scan's walk would, `already` files into the
    scan. Returns every `update_progress` call and the walk's own count."""
    lib = tmp_path / 'lib'
    lib.mkdir()
    for i in range(files):
        folder = lib / f'd{i % 7}'
        folder.mkdir(exist_ok=True)
        (folder / f'f{i}').write_bytes(b'')
    calls, total_ref = [], [already]
    with patch.object(audit, 'update_progress',
                      lambda scanned, total: calls.append((scanned, total))):
        _, scanned, _, _ = _walk_directory(str(lib), 'Media', {}, {}, already, 0,
                                           exclusion_patterns=[], total_ref=total_ref)
    return calls, scanned


@pytest.mark.parametrize('already', [0, 100], ids=['first walk', 'second walk'])
@pytest.mark.parametrize('files', [0, 1, 127, 128, 129, 300])
def test_progress_is_written_every_128_files_and_when_the_walk_ends(tmp_path, files, already):
    calls, scanned = _walk_counting(tmp_path, files, already)
    done = already + files

    assert scanned == done
    # The last word is the walk's whole count, against the whole total.
    assert calls[-1] == (done, done)
    # Before it, one write at each multiple of 128 the scan's count passed.
    assert [n for n, _ in calls[:-1]] == [n for n in range(already + 1, done + 1) if n % 128 == 0]


def test_a_finished_walk_reads_as_all_of_it(tmp_path):
    """The real `state`, no stand-in: what the progress poll reads once a walk
    whose length isn't a multiple of 128 has ended."""
    lib = tmp_path / 'lib'
    lib.mkdir()
    for i in range(130):
        (lib / f'f{i}').write_bytes(b'')
    before = get_state()
    try:
        _walk_directory(str(lib), 'Media', {}, {}, 0, 0, exclusion_patterns=[], total_ref=[0])
        after = get_state()
    finally:
        set_state(scanned_files=before['scanned_files'], progress=before['progress'],
                  total_files=before['total_files'])

    assert after['scanned_files'] == 130
    assert after['progress'] == 100
