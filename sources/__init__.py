"""
Torrent source dispatcher. Reads TORRENT_SOURCE from cfg and delegates
to the appropriate backend (_qbit or _qui).

Each backend implements:
  fetch_file_map(cfg)          -> (file_map, sorted_trackers, tracker_snapshot, report)
  test_connection(payload)     -> {'ok': bool, 'version': str|None, 'error': str|None, 'instances': [...]}
  connection_info(cfg)         -> {'version': str|None, 'instance_summary': str, 'instances': [...]}
  fetch_save_path_hint(payload)-> {'save_path': str|None, 'version': str|None, 'torrent_count': int, 'seeding_size': int, 'instances': [...]}
  fetch_torrent_details(cfg, items) -> {hash: {'uploaded', 'ratio', 'added_on', 'tracker_health', 'tracker_msg'}}
  remove_torrents(cfg, items, delete_files=True) -> int (count submitted for deletion)
  list_torrents(cfg) -> ([rows], report)
  fetch_torrent_file_paths(cfg, items) -> {hash: [paths] | None}   # None = could not ask

**Absence is reportable.** Every primitive here answers two different questions
and must never collapse them: "the client says there is nothing" and "the client
could not be asked". An empty collection means the former only. The latter is
carried by an explicit `None` (per torrent) or by the `report` dict (per scan).
"""

import os


class SourceConnectionError(Exception):
    """Raised by backends when they cannot connect or authenticate."""


# ---------------------------------------------------------------------------
# Source report — the per-scan "how completely could we ask" channel
# ---------------------------------------------------------------------------

# Notes are diagnostic strings for the debug report and the UI. Bounded because
# the natural thing to write is one per torrent, and a 15k-torrent client with a
# flaky WebUI would otherwise put 15k strings into an app_meta row.
_MAX_REPORT_NOTES = 10


def new_source_report(source):
    """A fresh per-scan completeness report.

    `fetch_file_map` and `list_torrents` fill this in as they go, and the audit
    persists it. Every field answers some form of "what could we not see":

      torrent_count       torrents the client listed (0 is a real answer)
      file_map_size       paths the map ended up claiming
      listing_failures    torrents whose per-file listing could not be fetched
      listing_recovered   of those, how many were claimed from disk instead
      listing_unresolved  of those, how many are still unaccounted for — these
                          are the files that will read as orphaned without ever
                          having been asked about
      instances_*         qui only; qbit reports a single instance
      partial             the scan is known-incomplete for any reason at all
    """
    return {
        'source':             source,
        'torrent_count':      0,
        'file_map_size':      0,
        'listing_failures':   0,
        'listing_recovered':  0,
        'listing_unresolved': 0,
        'instances_total':    0,
        'instances_ok':       0,
        'instances_failed':   [],
        'partial':            False,
        'notes':              [],
    }


def report_note(report, message):
    """Record a diagnostic note on a source report (bounded, de-duplicated)."""
    if report is None:
        return
    notes = report.setdefault('notes', [])
    if message not in notes and len(notes) < _MAX_REPORT_NOTES:
        notes.append(message)


def report_instance_failure(report, name, reason):
    """Record an instance that could not be listed, and mark the scan partial."""
    if report is None:
        return
    report.setdefault('instances_failed', []).append(
        {'name': str(name), 'reason': str(reason)[:300]})
    report['partial'] = True


def disk_fallback_paths(save_path, torrent_name):
    """On-disk file paths for a torrent whose client file listing failed.

    Walks `save_path/name`, which is where both clients put a torrent's payload.
    A failed listing is an *unknown*, and an unknown left alone becomes a
    positive claim of orphanhood by default — every file of that torrent turns
    up in the walk with no client entry against it. Enumerating the payload from
    disk converts the unknown into a conservative *claimed* instead, which is
    the fail-safe direction and the only thing that lets a failure be attributed
    to specific paths at all: the paths are precisely what the listing failed to
    return.

    Returns [] when nothing is found there — which is itself information, and is
    counted as `listing_unresolved` rather than passed off as "no files".
    """
    if not (save_path and torrent_name):
        return []
    root = os.path.join(save_path, torrent_name)
    try:
        if os.path.isfile(root):
            return [root]
        if not os.path.isdir(root):
            return []
        found = []
        for dir_root, _, dir_files in os.walk(root):
            for fname in dir_files:
                found.append(os.path.join(dir_root, fname))
        return found
    except OSError:
        return []


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
    list_torrents         as _qbit_list_torrents,
    fetch_torrent_file_paths as _qbit_fetch_torrent_file_paths,
)
from sources._qui import (  # noqa: E402
    fetch_file_map        as _qui_fetch_file_map,
    test_connection       as _qui_test_connection,
    connection_info       as _qui_connection_info,
    fetch_save_path_hint  as _qui_fetch_save_path_hint,
    fetch_torrent_details as _qui_fetch_torrent_details,
    remove_torrents       as _qui_remove_torrents,
    list_torrents         as _qui_list_torrents,
    fetch_torrent_file_paths as _qui_fetch_torrent_file_paths,
)


def _source(cfg):
    return cfg.get('TORRENT_SOURCE', 'qbit')


def fetch_file_map(cfg):
    """(file_map, sorted_trackers, tracker_snapshot, report).

    `report` is a `new_source_report` dict describing how completely the client
    could be asked — see the module docstring. The audit reads it to decide
    whether this scan's orphan classification is trustworthy enough to persist.
    """
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


def list_torrents_detailed(cfg):
    """(rows, report) — the listing plus which instances actually answered.

    Callers that can act on a partial answer (report it, narrow a group, refuse
    a delete) use this. Everything else uses `list_torrents`, which refuses a
    partial answer outright rather than handing over a short list that looks
    complete.
    """
    if _source(cfg) == 'qui':
        return _qui_list_torrents(cfg)
    return _qbit_list_torrents(cfg)


def list_torrents(cfg):
    """Live, light listing of every torrent (Trumped workflow group resolution).

    Raises `SourceConnectionError` if any instance failed to answer. A short
    listing is not a smaller answer here, it is a wrong one: a cross-seed group
    resolved against it loses the members that lived on the instance that did
    not reply, and `execute` then deletes their payload out from under them.
    """
    rows, report = list_torrents_detailed(cfg)
    failed = report.get('instances_failed') or []
    if failed:
        names = ', '.join(f.get('name', '?') for f in failed[:3])
        raise SourceConnectionError(
            f"{len(failed)} of {report.get('instances_total', '?')} torrent-client "
            f"instance(s) could not be listed ({names}). The listing would be "
            f"incomplete, so it is not being used.")
    return rows


def fetch_torrent_file_paths(cfg, items):
    """Client-side file paths per torrent — {hash: [paths] | None}.

    **`None` means the listing could not be fetched; `[]` means the client
    answered that there are none.** Callers must not conflate them: an empty
    list tests as "shares no paths with anything", which silently shrinks every
    set membership built from it — a cross-seed group down to its seed, a
    removal partition down to "nothing else claims these files".
    """
    if _source(cfg) == 'qui':
        return _qui_fetch_torrent_file_paths(cfg, items)
    return _qbit_fetch_torrent_file_paths(cfg, items)
