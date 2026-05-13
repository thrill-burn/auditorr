"""
Torrent source dispatcher. Reads TORRENT_SOURCE from cfg and delegates
to the appropriate backend (_qbit or _qui).

Each backend implements:
  fetch_file_map(cfg)          -> (file_map, sorted_trackers, tracker_snapshot)
  test_connection(payload)     -> {'ok': bool, 'version': str|None, 'error': str|None, 'instances': [...]}
  connection_info(cfg)         -> {'version': str|None, 'instance_summary': str, 'instances': [...]}
  fetch_save_path_hint(payload)-> {'save_path': str|None, 'version': str|None, 'torrent_count': int, 'seeding_size': int, 'instances': [...]}
"""


class SourceConnectionError(Exception):
    """Raised by backends when they cannot connect or authenticate."""


# Imports come after SourceConnectionError so backends can import it without
# triggering a circular-import error (the name is already bound by the time
# Python starts importing the sub-modules).
from sources._qbit import (  # noqa: E402
    fetch_file_map       as _qbit_fetch_file_map,
    test_connection      as _qbit_test_connection,
    connection_info      as _qbit_connection_info,
    fetch_save_path_hint as _qbit_fetch_save_path_hint,
)
from sources._qui import (  # noqa: E402
    fetch_file_map       as _qui_fetch_file_map,
    test_connection      as _qui_test_connection,
    connection_info      as _qui_connection_info,
    fetch_save_path_hint as _qui_fetch_save_path_hint,
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
