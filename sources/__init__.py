"""
Torrent source dispatcher. Reads TORRENT_SOURCE from cfg and delegates
to the appropriate backend (_qbit or _qui).

Each backend implements:
  fetch_file_map(cfg)          -> (file_map, sorted_trackers, tracker_snapshot)
  test_connection(payload)     -> {'ok': bool, 'version': str|None, 'error': str|None, 'instances': [...]}
  connection_info(cfg)         -> {'version': str|None, 'instance_summary': str, 'instances': [...]}
  fetch_save_path_hint(payload)-> {'save_path': str|None, 'version': str|None, 'torrent_count': int, 'seeding_size': int, 'instances': [...]}
  fetch_torrent_details(cfg, items) -> {hash: {'uploaded', 'ratio', 'added_on', 'tracker_health', 'tracker_msg'}}
  remove_torrents(cfg, items, delete_files=True) -> int (count submitted for deletion)
"""


class SourceConnectionError(Exception):
    """Raised by backends when they cannot connect or authenticate."""


# Substrings (lowercased) of tracker status messages that mean the torrent is
# no longer registered on the tracker — trumped, deleted, or nuked. Seeding
# such a torrent earns nothing; it is the strongest "dead weight" signal the
# Triage workflow has. Kept conservative: passkey/authorization problems are
# NOT included because the torrent itself may still be alive.
UNREGISTERED_TRACKER_PATTERNS = (
    'unregistered',
    'not registered',
    'torrent not found',
    'torrent does not exist',
    'torrent not exists',
    'infohash not found',
    'torrent has been deleted',
    'torrent deleted',
    'trumped',
    'nuked',
)


# Merge priority for per-path tracker health when several torrents claim the
# same file (cross-seeds): least-dead wins — a path with any live torrent
# must never be classified as a dead seed, because deleting its files would
# break that live torrent.
HEALTH_RANK = {'unregistered': 0, 'unknown': 1, 'not_working': 2, 'working': 3}


def classify_tracker_entries(entries):
    """Classify a torrent's tracker list into a single health verdict.

    entries: [{'url': str, 'status': int|None, 'msg': str}] — qBittorrent
    tracker status codes: 0=disabled, 1=not contacted, 2=working, 3=updating,
    4=not working; qBittorrent 5.x adds 5=tracker error and 6=unreachable.

    Returns (health, msg) where health is one of
    'unregistered' | 'working' | 'not_working' | 'unknown'.
    """
    real = [e for e in entries
            if (e.get('url') or '').startswith(('http', 'udp'))]
    for e in real:
        m = (e.get('msg') or '').strip()
        if m and any(p in m.lower() for p in UNREGISTERED_TRACKER_PATTERNS):
            return 'unregistered', m
    statuses = [e.get('status') for e in real]
    if any(s in (2, 3) for s in statuses):
        return 'working', ''
    if any(s in (4, 5, 6) for s in statuses):
        first_msg = next(((e.get('msg') or '').strip() for e in real
                          if e.get('status') in (4, 5, 6) and (e.get('msg') or '').strip()), '')
        return 'not_working', first_msg
    return 'unknown', ''


# Imports come after SourceConnectionError so backends can import it without
# triggering a circular-import error (the name is already bound by the time
# Python starts importing the sub-modules).
from sources._qbit import (  # noqa: E402
    fetch_file_map        as _qbit_fetch_file_map,
    test_connection       as _qbit_test_connection,
    connection_info       as _qbit_connection_info,
    fetch_save_path_hint  as _qbit_fetch_save_path_hint,
    fetch_torrent_details as _qbit_fetch_torrent_details,
    remove_torrents       as _qbit_remove_torrents,
)
from sources._qui import (  # noqa: E402
    fetch_file_map        as _qui_fetch_file_map,
    test_connection       as _qui_test_connection,
    connection_info       as _qui_connection_info,
    fetch_save_path_hint  as _qui_fetch_save_path_hint,
    fetch_torrent_details as _qui_fetch_torrent_details,
    remove_torrents       as _qui_remove_torrents,
)


def _source(cfg):
    return cfg.get('TORRENT_SOURCE', 'qbit')


def fetch_file_map(cfg):
    if _source(cfg) == 'qui':
        return _qui_fetch_file_map(cfg)
    return _qbit_fetch_file_map(cfg)


def test_connection(payload):
    if payload.get('TORRENT_SOURCE', 'qbit') == 'qui':
        return _qui_test_connection(payload)
    return _qbit_test_connection(payload)


def connection_info(cfg):
    if _source(cfg) == 'qui':
        return _qui_connection_info(cfg)
    return _qbit_connection_info(cfg)


def fetch_save_path_hint(payload):
    if payload.get('TORRENT_SOURCE', 'qbit') == 'qui':
        return _qui_fetch_save_path_hint(payload)
    return _qbit_fetch_save_path_hint(payload)


def fetch_torrent_details(cfg, items):
    """Live per-torrent detail lookup for workflow pages.

    items: [{'hash': str, 'instance_id': int|None}] — instance_id is only
    meaningful for qui. Returns {hash: details} for every hash that could be
    resolved; missing hashes simply have no entry.
    """
    if _source(cfg) == 'qui':
        return _qui_fetch_torrent_details(cfg, items)
    return _qbit_fetch_torrent_details(cfg, items)


def remove_torrents(cfg, items, delete_files=True):
    """Delete torrents (and optionally their files) from the client.

    items: [{'hash': str, 'instance_id': int|None}] — instance_id is only
    meaningful for qui. Returns the number of torrents submitted for deletion.
    Destructive: callers must check the ALLOW_CLIENT_DELETE config flag first.
    """
    if _source(cfg) == 'qui':
        return _qui_remove_torrents(cfg, items, delete_files)
    return _qbit_remove_torrents(cfg, items, delete_files)
