"""Staging a file list or a signature map stores the bytes it always stored (SCAN SC1, SC8).

`db_prepare_file_results` used to hand `json.JSONEncoder().iterencode(files)` to
zlib one small string at a time: the pure-Python encoder, 68 strings per torrent
record, and a `b''.join` over 20 million mostly-empty results that was the whole
scan's memory peak. It now encodes `_JSON_BATCH` items per `json.dumps` call
(`db._json_chunks`) and compresses each batch as it's made.

The stored blob is what every reader, the stream endpoint and the next scan's
diff decode, so nothing about it may move: the new text is `json.dumps(obj)`
exactly, and the new blob is the old statement's blob byte for byte. The old
statement is kept here as the reference.

`db_prepare_file_signatures` built each map as one string and its bytes, 93 MB
for 350,000 paths, and goes the same way now. Its old statement is here too.
"""
import collections
import json
import random
import tracemalloc
import zlib

import pytest

import db


def _old_prepare_blob(files):
    """`db_prepare_file_results` as it was before SC1, verbatim."""
    cobj = zlib.compressobj(level=1)
    chunks = [cobj.compress(chunk.encode('utf-8', errors='replace'))
              for chunk in json.JSONEncoder().iterencode(files)]
    chunks.append(cobj.flush())
    return b''.join(chunks)


def _old_count(files):
    return (sum(len(v) for v in files.values() if isinstance(v, list))
            if isinstance(files, dict) else len(files))


_rnd = random.Random(3)

# 60,001 torrent-shaped records: thirty full batches and one record over.
RECORDS = [
    {'path': f'movies/Film {i} (20{i % 30:02d})/Film.{i}.mkv', 'size': _rnd.randint(1, 2 ** 40),
     'inode': i, 'file_id': f'41:{i}', 'status': 'Seeding', 'imported': bool(i % 3),
     'trackers': ['a.cc', 'b.org'][: 1 + i % 2],
     'linked_paths': [f'/data/media/x/{i}.mkv'] * (i % 3), 'duplicate_paths': [],
     'excluded': False, 'hash': '%040x' % i, 'category': 'radarr', 'instance_id': None,
     'instance_name': None, 'tracker_health': 'working', 'tracker_msg': ''}
    for i in range(60_001)]

CORPUS = [
    # Empties and nesting.
    [], {}, [[]], [{}], {'a': []}, {'a': {}},
    [1, 2.5, True, None, 'x', -0.0, 1e300, 2 ** 70],
    # The compact rows' shapes: `dedupe` is a dict of lists, `library_map` a dict of mixed values.
    {'torrents': [], 'media': []},
    {'version': 3, 'folders': [['library', 'tv']], 'titles': [[0, 'A', 1, 2]], 'folded': []},
    # Non-ASCII, lone surrogates (a name off disk that isn't UTF-8), every escape.
    [{'path': 'tv/Amélie (2001)/Amélie.mkv', 'size': 1, 'trackers': ['None'], 'linked_paths': []}],
    [{'path': 'bad\udcff\udc80name.mkv', 'q': '"\\/\b\f\n\r\t\x00\x1f '}],
    # Tuples, keys that aren't strings, inf and nan.
    {'k': (1, 2, (3, [4]))}, [(1, 2), (3,)],
    {1: 'int key', None: 'none key', 2.5: 'float key'}, {True: 'bool key', False: 'other'},
    {'a': [{1: 2}], 'b': {'c': [float('inf'), float('nan')]}, 'c': 5, 'd': 'e', 'f': [1]},
    # Past one batch: lists of lists, a flat dict, a dict whose every value is a list.
    [[i, f'T{i}', *range(9)] for i in range(4500)],
    {f'k{i}': i for i in range(4500)}, {f'k{i}': [i] for i in range(4500)},
    RECORDS,
    {'torrents': [{'path': f't/{i}', 'excluded': True} for i in range(2001)],
     'media': [{'path': f'm/{i}', 'duplicate_paths': ['/x']} for i in range(1999)]},
]
# The batch edges, as a list and as a dict.
for _n in (0, 1, 1999, 2000, 2001, 4000, 4001):
    CORPUS.append(list(range(_n)))
    CORPUS.append({str(i): i for i in range(_n)})
# A dict is batched whole unless a batch holds a list or dict of its own, and
# then that batch alone is walked: nested values at a batch's two ends and its
# middle, among plain ones, and in a batch after a plain one.
CORPUS += [
    {f'k{i}': ({'n': [i]} if i in (0, 1, 1999, 2000, 2001, 3999, 4499) else i) for i in range(4500)},
    {f'k{i}': ([i, i] if i % 700 == 699 else f'v{i}') for i in range(4500)},
    {f'k{i}': ([] if i >= 4000 else i) for i in range(4500)},
    # A nested value under a key `json` coerces, beside ones under string keys.
    {1: [1, 2], 'a': [3], None: {'b': []}, 'c': 4, 2.5: (5, 6), 'd': {}},
    # Subclasses of the container types: encoded, whichever way they're cut.
    {'a': collections.OrderedDict(x=1, y=[2]), 'b': collections.defaultdict(list, {'z': [1]}),
     'c': [1], 'd': collections.namedtuple('P', 'x y')(1, [2])},
    [collections.OrderedDict(x=1), collections.namedtuple('P', 'x y')(1, 2)],
]

_IDS = [f'{i}:{type(o).__name__}[{len(o)}]' for i, o in enumerate(CORPUS)]


def test_the_batch_size_the_edges_are_written_for():
    """The 1,999 / 2,000 / 2,001 cases above are edges only while this holds."""
    assert db._JSON_BATCH == 2000


@pytest.mark.parametrize('obj', CORPUS, ids=_IDS)
def test_chunks_join_to_json_dumps(obj):
    assert ''.join(db._json_chunks(obj)) == json.dumps(obj)


@pytest.mark.parametrize('obj', CORPUS, ids=_IDS)
def test_file_list_blob_is_the_old_statements_blob(obj):
    prepared = db.db_prepare_file_results(obj)
    assert prepared.blob == _old_prepare_blob(obj)
    assert zlib.decompress(prepared.blob) == json.dumps(obj).encode()
    assert prepared.count == _old_count(obj)


def test_no_chunk_is_the_whole_list():
    """What SC1 is for: the encoder's output is a batch, never the list."""
    chunks = list(db._json_chunks(RECORDS))
    whole = len(json.dumps(RECORDS))
    assert len(chunks) == 31 + 1                      # 31 batches and the closing bracket
    assert max(map(len, chunks)) < whole / 20


def test_staging_a_list_holds_a_few_batches_not_the_list():
    """The old statement peaked at about 350 MB on these records (measured
    2026-11-15: 1,762 MB on 300,005), nearly all of it the join over empty
    compress results. A regression to either of its halves is far over this."""
    whole = len(json.dumps(RECORDS))
    tracemalloc.start()
    try:
        db.db_prepare_file_results(RECORDS)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < whole / 2


# ── Signature maps (SC8) ──

def _old_signature_blob(sigs):
    """`db_prepare_file_signatures` as it was before SC8, verbatim."""
    return zlib.compress(json.dumps(sigs).encode('utf-8', errors='replace'), 1)


# 60,001 paths the length a library's are, each with its diff bitmask.
SIGS = {f'television/Show Title {i:05d} (1999)/Season 01/Show Title {i:05d} (1999) - S01E01 - '
        f'Episode Name [WEBDL-1080p][EAC3 5.1][h264]-GRP.mkv': i % 32 for i in range(60_001)}

SIG_CORPUS = [
    {}, {'a': 1}, {f'tv/Show {i}/Ep {i}.mkv': i % 32 for i in range(50_001)},
    {'bad\udcffname': 3, 'é/ü.mkv': 5},
    {str(i): i for i in range(2000)}, {str(i): i for i in range(2001)},
    SIGS,
]


@pytest.mark.parametrize('sigs', SIG_CORPUS, ids=[f'{i}:{len(s)}' for i, s in enumerate(SIG_CORPUS)])
def test_signature_blob_is_the_old_statements_blob(sigs):
    prepared = db.db_prepare_file_signatures(sigs)
    assert prepared.blob == _old_signature_blob(sigs)
    assert zlib.decompress(prepared.blob) == json.dumps(sigs).encode()
    assert prepared.count == len(sigs)


def test_a_signature_map_is_staged_in_batches():
    chunks = list(db._json_chunks(SIGS))
    assert len(chunks) == 31 + 1                      # 31 batches and the closing brace
    assert max(map(len, chunks)) < len(json.dumps(SIGS)) / 20


def test_staging_a_signature_map_holds_a_few_batches_not_the_map():
    """The old statement held the map's text and its bytes together: twice the text."""
    whole = len(json.dumps(SIGS))
    tracemalloc.start()
    try:
        db.db_prepare_file_signatures(SIGS)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < whole / 2
