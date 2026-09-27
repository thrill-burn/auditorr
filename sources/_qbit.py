"""
qBittorrent backend — lifted verbatim from audit.py / app.py.

Public interface:
  fetch_file_map(cfg)          -> (file_map, sorted_trackers, tracker_snapshot, report)
  test_connection(payload)     -> {'ok': bool, 'version': str|None, 'error': str|None, 'instances': []}
  connection_info(cfg)         -> {'version': str|None, 'instance_summary': str, 'instances': []}
  fetch_save_path_hint(payload)-> {'save_path': str|None, 'version': str|None, 'torrent_count': int, 'seeding_size': int, 'instances': []}
"""

import logging
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import qbittorrentapi

from sources import (
    SourceConnectionError, classify_tracker_entries, HEALTH_RANK as _HEALTH_RANK,
    new_source_report, report_note,
    torrent_complete, torrent_claimed_paths, remap_path, registration_key,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# fetch_file_map
# ---------------------------------------------------------------------------

def fetch_file_map(cfg, unresolved_roots=None):
    socket.setdefaulttimeout(30)
    try:
        return _fetch_inner(cfg, unresolved_roots)
    except (qbittorrentapi.LoginFailed, qbittorrentapi.APIConnectionError) as e:
        raise SourceConnectionError(f"qBittorrent error: {e}") from e
    finally:
        socket.setdefaulttimeout(None)


def _fetch_inner(cfg, unresolved_roots=None):
    qbt = qbittorrentapi.Client(
        host=cfg.get('QB_HOST'),
        username=cfg.get('QB_USER'),
        password=cfg.get('QB_PASS'),
    )
    qbt.auth_log_in()
    torrents = list(qbt.torrents_info())
    report = new_source_report('qbit')
    # One client, so a registration is a torrent and nothing can be registered
    # twice — the S05 counters are the same number and a constant zero.
    report['torrent_count']     = len(torrents)
    report['distinct_torrents'] = len(torrents)
    report['multi_registered']  = 0
    report['instances_total'] = 1
    report['instances_ok']    = 1

    # Fetch all tracker lists in parallel — eliminates N sequential API calls.
    # 16 workers gives significant speedup without overwhelming qBittorrent.
    # All trackers are fetched (not just primary) to preserve cross-seed stats.
    # threading.local() gives each worker thread its own authenticated Client,
    # logging in once per thread rather than once per torrent.
    _host = cfg.get('QB_HOST')
    _user = cfg.get('QB_USER')
    _pass = cfg.get('QB_PASS')
    _thread_local = threading.local()

    def _get_thread_client():
        if not hasattr(_thread_local, 'client'):
            client = qbittorrentapi.Client(host=_host, username=_user, password=_pass)
            client.auth_log_in()
            _thread_local.client = client
        return _thread_local.client

    # Fetch trackers and file lists in a single parallel pass — one login and
    # two API calls per worker instead of two separate executor pools. The
    # tracker entries are also classified here (dead-seed detection): the
    # status/msg fields come along for free with the same API call.
    #
    # The two calls are caught separately, and the file listing reports failure
    # as a value rather than as an empty list. One bare `except` used to cover
    # both, so a tracker timeout discarded a perfectly good file list as well —
    # and either way the torrent's files never entered `file_map`, turned up in
    # the walk with nothing claiming them, and were presented in Cleanup as
    # orphans with a green "frees X" beside them. Sixteen workers making two
    # WebUI calls each for the whole library on every scan; a timeout or a 5xx
    # during a client restart is an ordinary event, and it hit whichever
    # torrents lost the race, differently every scan.
    def _fetch_torrent_data(torrent):
        try:
            entries = [{'url': t.url, 'status': t.status, 'msg': t.msg}
                       for t in _get_thread_client().torrents_trackers(torrent_hash=torrent.hash)]
            raw   = [e['url'] for e in entries
                     if e['url'].startswith('http') or e['url'].startswith('udp')]
            hosts = [u.split('/')[2] for u in raw if len(u.split('/')) > 2] or ['Unknown']
            health, msg = classify_tracker_entries(entries)
        except Exception as e:
            log.debug('qbit: tracker listing failed for %s: %s', torrent.hash, e)
            hosts = ['Unknown']
            health, msg = 'unknown', ''
        try:
            files = list(_get_thread_client().torrents_files(torrent_hash=torrent.hash))
            files_ok = True
        except Exception as e:
            log.debug('qbit: file listing failed for %s: %s', torrent.hash, e)
            files, files_ok = [], False
        return torrent.hash, hosts, (health, msg), files, files_ok

    tracker_map = {}
    health_map  = {}
    files_map   = {}
    failed_listings = set()
    # Seeding-time aggregates for the Next steps prize layer. Both ride the
    # torrents list this loop already holds — `seeding_time` comes back on
    # `torrents_info`, so neither costs an API call or a per-torrent fan-out.
    # Scalars only: putting either on a file record would multiply it across
    # every file of every torrent and grow files_json, the known RAM hotspot.
    seed_byte_secs = 0
    max_seed_secs  = 0
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(_fetch_torrent_data, t): t for t in torrents}
        for future in as_completed(futures):
            torrent_hash, hosts, health, files, files_ok = future.result()
            tracker_map[torrent_hash] = hosts
            health_map[torrent_hash]  = health
            files_map[torrent_hash]   = files
            if not files_ok:
                failed_listings.add(torrent_hash)

    # Build file map using pre-fetched tracker and file data
    file_map             = {}
    trackers_set         = set()
    tracker_upload       = {}
    tracker_seeding_size = {}
    remote_path          = cfg.get('REMOTE_PATH', '')
    local_path           = cfg.get('LOCAL_PATH', '')

    for torrent in torrents:
        hosts     = tracker_map.get(torrent.hash, ['Unknown'])
        seed_secs = int(getattr(torrent, 'seeding_time', 0) or 0)
        if seed_secs > 0:
            seed_byte_secs += (torrent.size or 0) * seed_secs
            max_seed_secs   = max(max_seed_secs, seed_secs)
        for h in hosts:
            trackers_set.add(h)
            tracker_upload[h] = tracker_upload.get(h, 0) + torrent.uploaded
            if torrent.state in ('uploading', 'stalledUP', 'forcedUP'):
                tracker_seeding_size[h] = tracker_seeding_size.get(h, 0) + torrent.size
        save_path    = remap_path(torrent.save_path, remote_path, local_path)
        content_path = remap_path(
            getattr(torrent, 'content_path', '') or '', remote_path, local_path)
        if torrent.state in ('uploading', 'stalledUP', 'forcedUP'):
            status = 'Seeding'
        elif torrent.state in ('downloading', 'stalledDL'):
            status = 'Downloading'
        else:
            status = 'Paused'
        # Completion is its own question. `status` above is derived from the
        # state string, where a paused incomplete and a paused complete are the
        # same word — which is the root cause behind DEDUPE F6, TRIAGE T4 and
        # CLEANUP C4 all at once.
        complete = torrent_complete(getattr(torrent, 'progress', None),
                                    getattr(torrent, 'completion_on', None))
        if complete is False:
            report['incomplete_torrents'] += 1
        elif complete is None:
            report['completion_unknown'] += 1
        health, health_msg = health_map.get(torrent.hash, ('unknown', ''))
        # The claim rule is `sources.torrent_claimed_paths`, shared with qui and
        # with Cleanup's live re-verify so the three cannot disagree. A failed
        # listing is `None`: the paths are exactly what we do not have, so the
        # payload is enumerated from disk instead (ported from the qui backend)
        # rather than left to default to 'Orphaned'.
        listed = None if torrent.hash in failed_listings else \
            [f.name for f in files_map.get(torrent.hash, [])]
        full_paths = torrent_claimed_paths(
            save_path, getattr(torrent, 'name', '') or '', content_path, listed, complete)
        if listed is None:
            if full_paths:
                report['listing_recovered'] += 1
            else:
                # What the fallback cannot find stays unresolved and is counted,
                # not swallowed.
                report['listing_unresolved'] += 1
                if unresolved_roots is not None:
                    unresolved_roots.extend(r for r in (save_path, content_path) if r)
                # Deliberately no infohash: these notes reach the debug report,
                # which is meant to be safe to paste in public, and an infohash
                # names a specific release on a private tracker. A short prefix
                # would also sit under the 24-char threshold the report's token
                # redactor fires at. The save path goes through its sanitizer.
                report_note(report,
                            f"file listing failed and nothing was found at {save_path}")
        for full_path in full_paths:
            entry = file_map.setdefault(full_path, {
                "status": status,
                "trackers": set(),
                "hash": torrent.hash,
                "category": getattr(torrent, 'category', '') or '',
                "tracker_health": health,
                "tracker_msg": health_msg,
            })
            entry["trackers"].update(hosts)
            # Sparse, and sticky in the cautious direction. Absence means
            # complete (the overwhelming majority), so the flag costs memory
            # only where it is true — the `dead_siblings` pattern, and the rule
            # `seeding_time` established: a field on every file record
            # multiplies across every file of every torrent and grows
            # files_json, the known RAM hotspot.
            #
            # Sticky because cross-seeds share a path: if any claimant says the
            # bytes are not whole, hardlinking that inode can corrupt whatever
            # is still writing to it. The cost of being wrong here is one missed
            # reclaim; the cost the other way is two broken torrents.
            if complete is False:
                entry["incomplete"] = True
            elif complete is None:
                entry["completion_unknown"] = True
            # Cross-seeded paths: several torrents can claim the same file.
            # The record must reflect the HEALTHIEST claimant — a path with
            # any live torrent is not a dead seed, and deleting its files
            # would break that live torrent. The hash follows the health so
            # client actions target the torrent the verdict describes.
            if _HEALTH_RANK.get(health, 1) > _HEALTH_RANK.get(entry["tracker_health"], 1):
                entry["tracker_health"] = health
                entry["tracker_msg"]    = health_msg
                entry["hash"]           = torrent.hash
            # Record every unregistered claimant of this path. The merge above
            # keeps only the healthiest hash, so a dead cross-seed whose payload
            # is alive on a working sibling would otherwise vanish. Retaining it
            # lets Triage surface the dead registration (remove it, keep files).
            if health == 'unregistered':
                entry.setdefault("unreg_claimants", {})[torrent.hash] = {
                    "hash": torrent.hash, "instance_id": None, "tracker_msg": health_msg,
                }
            if status == 'Seeding' or entry["status"] == 'Seeding':
                entry["status"] = 'Seeding'
            elif entry["status"] == 'Paused':
                entry["status"] = status
    del tracker_map, health_map, files_map

    all_hosts        = set(tracker_upload) | set(tracker_seeding_size)
    tracker_snapshot = {
        host: {
            "uploaded":     tracker_upload.get(host, 0),
            "seeding_size": tracker_seeding_size.get(host, 0),
        }
        for host in all_hosts
    }
    tracker_snapshot['_instance_count'] = 1
    # '_'-prefixed: every per-tracker loop already skips these.
    tracker_snapshot['_seed_byte_secs'] = seed_byte_secs
    tracker_snapshot['_max_seed_secs']  = max_seed_secs

    report['file_map_size']    = len(file_map)
    report['listing_failures'] = len(failed_listings)
    if failed_listings:
        report['partial'] = True
        log.warning(
            'qbit: %d of %d torrent file listing(s) failed — %d recovered from disk, '
            '%d unaccounted for. Files of unaccounted torrents would otherwise read '
            'as orphaned.',
            len(failed_listings), len(torrents),
            report['listing_recovered'], report['listing_unresolved'])
    if report['incomplete_torrents'] or report['completion_unknown']:
        log.info(
            'qbit: %d torrent(s) not finished downloading, %d with no usable '
            'completion field. Their files are still claimed (so they do not read '
            'as orphans) but are kept out of duplicate groups and the Triage pile.',
            report['incomplete_torrents'], report['completion_unknown'])
    return file_map, sorted(trackers_set), tracker_snapshot, report


# ---------------------------------------------------------------------------
# fetch_torrent_details
# ---------------------------------------------------------------------------

def fetch_torrent_details(cfg, items):
    """Live lookup of upload stats + tracker health for specific torrents.

    items: [{'hash': str, ...}] — instance_id is ignored (single instance), so
    a registration key here is the bare hash (S05).
    Returns {hash: {'uploaded', 'ratio', 'seeding_time', 'added_on', 'size',
    'tracker_health', 'tracker_msg'}}, and **`{'found': False}` for a hash the
    client does not list** (T10). A single instance filtered server-side, so the
    listing is complete or it raised: a missing hash is gone, not unasked.

    `size` is qBittorrent's `size` — the files *selected for download* — and not
    `total_size`, which counts unselected files too (qBittorrent WebUI API,
    `torrents/info`). It is the number Triage puts beside a delete (T5), and an
    unselected file was never downloaded, so it is not bytes a delete removes.
    """
    hashes = sorted({i.get('hash') for i in items if i.get('hash')})
    if not hashes:
        return {}
    socket.setdefaulttimeout(30)
    try:
        qbt = qbittorrentapi.Client(
            host=cfg.get('QB_HOST'),
            username=cfg.get('QB_USER'),
            password=cfg.get('QB_PASS'),
        )
        qbt.auth_log_in()
        details = {}
        for torrent in qbt.torrents_info(torrent_hashes=hashes):
            details[torrent.hash] = {
                'uploaded':       torrent.uploaded,
                'ratio':          round(float(torrent.ratio), 3),
                'seeding_time':   getattr(torrent, 'seeding_time', None),
                'added_on':       getattr(torrent, 'added_on', None),
                'size':           getattr(torrent, 'size', None),
                'tracker_health': 'unknown',
                'tracker_msg':    '',
            }

        _host = cfg.get('QB_HOST')
        _user = cfg.get('QB_USER')
        _pass = cfg.get('QB_PASS')
        _thread_local = threading.local()

        def _get_thread_client():
            if not hasattr(_thread_local, 'client'):
                client = qbittorrentapi.Client(host=_host, username=_user, password=_pass)
                client.auth_log_in()
                _thread_local.client = client
            return _thread_local.client

        def _fetch_health(torrent_hash):
            try:
                entries = [
                    {'url': t.url, 'status': t.status, 'msg': t.msg}
                    for t in _get_thread_client().torrents_trackers(torrent_hash=torrent_hash)
                ]
                return torrent_hash, classify_tracker_entries(entries)
            except Exception:
                return torrent_hash, ('unknown', '')

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_fetch_health, h) for h in details]
            for future in as_completed(futures):
                torrent_hash, (health, msg) = future.result()
                details[torrent_hash]['tracker_health'] = health
                details[torrent_hash]['tracker_msg']    = msg
        for h in hashes:
            if h not in details:
                details[h] = {'found': False}
        return details
    except (qbittorrentapi.LoginFailed, qbittorrentapi.APIConnectionError) as e:
        raise SourceConnectionError(f"qBittorrent error: {e}") from e
    finally:
        socket.setdefaulttimeout(None)


# ---------------------------------------------------------------------------
# list_torrents / fetch_torrent_file_paths
# ---------------------------------------------------------------------------

def list_torrents(cfg):
    """Light live listing of every torrent in the client.

    Returns ([rows], report) where a row is {'reg', 'hash', 'name', 'size',
    'save_path', 'content_path', 'progress', 'completion_on', 'tracker',
    'instance_id', 'instance_name'} — size is the torrent's payload size, so
    cross-seeds of the same content report identical values (the Trumped
    workflow's sibling pre-filter). `save_path` and `content_path` are the
    client's own, unremapped. The last three fields are what Cleanup's live
    re-verify needs to apply the audit's claim rule to one torrent
    (`sources.torrent_claimed_paths`); `fetch_file_map` already read all three
    off this same call (M7, Phase 3's parity check).

    A single instance, so the listing is all-or-nothing: it either returns every
    torrent or raises. `report` exists to match the qui backend's shape, where
    one instance can fail while the others answer.

    **qbit has no instances, so S05 is a no-op here**: `instance_id` is `None`,
    `registration_key` gives the bare hash back, and `reg == hash` on every row.
    A hash cannot be registered twice in one client, so `distinct_torrents`
    equals `torrent_count` and `multi_registered` is always 0.
    """
    socket.setdefaulttimeout(30)
    try:
        qbt = qbittorrentapi.Client(
            host=cfg.get('QB_HOST'),
            username=cfg.get('QB_USER'),
            password=cfg.get('QB_PASS'),
        )
        qbt.auth_log_in()
        rows = []
        for t in qbt.torrents_info():
            tracker_url = getattr(t, 'tracker', '') or ''
            parts = tracker_url.split('/')
            rows.append({
                'reg':           registration_key(None, t.hash),
                'hash':          t.hash,
                'name':          t.name,
                'size':          t.size,
                'save_path':     t.save_path,
                'content_path':  getattr(t, 'content_path', '') or '',
                'progress':      getattr(t, 'progress', None),
                'completion_on': getattr(t, 'completion_on', None),
                'tracker':       parts[2] if len(parts) > 2 else '',
                'instance_id':   None,
                'instance_name': None,
            })
        report = new_source_report('qbit')
        report['torrent_count']     = len(rows)
        report['distinct_torrents'] = len({r['hash'] for r in rows})
        report['multi_registered']  = 0
        report['instances_total'] = 1
        report['instances_ok']    = 1
        return rows, report
    except (qbittorrentapi.LoginFailed, qbittorrentapi.APIConnectionError) as e:
        raise SourceConnectionError(f"qBittorrent error: {e}") from e
    finally:
        socket.setdefaulttimeout(None)


def fetch_torrent_file_paths(cfg, items):
    """Absolute client-side file paths for specific torrents.

    items: [{'hash', 'save_path'?, ...}] — save_path is used when provided
    (saves an API round-trip), looked up live otherwise.

    Returns {registration key: [paths] | None} — with one client
    `sources.registration_key(None, h)` is `h`, so this is the same map it
    always was (S05). **`None` means the listing could not be
    fetched; `[]` means the client answered that this torrent has no files.**
    This used to be `[]` for both, documented as deliberate ("failures yield
    empty lists, never exceptions") — and every consumer is a set-membership
    test, where an empty list reads as "shares nothing with anything" and
    quietly collapses whatever it was building. Still never raises per torrent:
    the failure is in the value now, not in control flow.
    """
    wanted = {}
    for i in items:
        h = i.get('hash')
        if h:
            wanted.setdefault(h, i.get('save_path') or '')
    if not wanted:
        return {}
    socket.setdefaulttimeout(30)
    try:
        qbt = qbittorrentapi.Client(
            host=cfg.get('QB_HOST'),
            username=cfg.get('QB_USER'),
            password=cfg.get('QB_PASS'),
        )
        qbt.auth_log_in()
        missing_sp = [h for h, sp in wanted.items() if not sp]
        if missing_sp:
            for t in qbt.torrents_info(torrent_hashes=missing_sp):
                wanted[t.hash] = t.save_path
        result = {}
        for h, sp in wanted.items():
            try:
                files = qbt.torrents_files(torrent_hash=h)
                result[h] = [os.path.join(sp, f.name).replace('\\', '/') for f in files]
            except Exception as e:
                log.debug('qbit: file paths unavailable for %s: %s', h, e)
                result[h] = None
        return result
    except (qbittorrentapi.LoginFailed, qbittorrentapi.APIConnectionError) as e:
        raise SourceConnectionError(f"qBittorrent error: {e}") from e
    finally:
        socket.setdefaulttimeout(None)


# ---------------------------------------------------------------------------
# remove_torrents
# ---------------------------------------------------------------------------

def remove_torrents(cfg, items, delete_files=True):
    """Delete torrents — and their downloaded files — from qBittorrent.

    items: [{'hash': str, ...}] — instance_id is ignored (single instance).
    Returns the number of torrents that existed and were submitted for
    deletion (qBittorrent silently ignores unknown hashes, so existence is
    checked first to give the caller an honest count).
    """
    hashes = sorted({i.get('hash') for i in items if i.get('hash')})
    if not hashes:
        return 0
    socket.setdefaulttimeout(30)
    try:
        qbt = qbittorrentapi.Client(
            host=cfg.get('QB_HOST'),
            username=cfg.get('QB_USER'),
            password=cfg.get('QB_PASS'),
        )
        qbt.auth_log_in()
        existing = [t.hash for t in qbt.torrents_info(torrent_hashes=hashes)]
        if not existing:
            return 0
        qbt.torrents_delete(delete_files=delete_files, torrent_hashes=existing)
        return len(existing)
    except (qbittorrentapi.LoginFailed, qbittorrentapi.APIConnectionError) as e:
        raise SourceConnectionError(f"qBittorrent error: {e}") from e
    finally:
        socket.setdefaulttimeout(None)


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------

def test_connection(payload):
    host     = payload.get('QB_HOST', '')
    user     = payload.get('QB_USER')
    password = payload.get('QB_PASS')
    try:
        socket.setdefaulttimeout(8)
        client = qbittorrentapi.Client(host=host, username=user, password=password)
        client.auth_log_in()
        version = client.app.version
        return {'ok': True, 'version': version, 'error': None, 'instances': []}
    except qbittorrentapi.LoginFailed:
        return {'ok': False, 'version': None,
                'error': "Login failed — check your username and password.", 'instances': []}
    except (qbittorrentapi.APIConnectionError, ConnectionRefusedError, socket.gaierror, OSError):
        return {'ok': False, 'version': None,
                'error': f"Could not reach qBittorrent at '{host}' — check the host URL and ensure qBittorrent is running.",
                'instances': []}
    except Exception as e:
        return {'ok': False, 'version': None, 'error': f"Unexpected error: {e}", 'instances': []}
    finally:
        socket.setdefaulttimeout(None)


# ---------------------------------------------------------------------------
# connection_info
# ---------------------------------------------------------------------------

def connection_info(cfg):
    try:
        socket.setdefaulttimeout(10)
        client = qbittorrentapi.Client(
            host=cfg.get('QB_HOST'), username=cfg.get('QB_USER'), password=cfg.get('QB_PASS'))
        client.auth_log_in()
        return {'version': client.app.version, 'instance_summary': '', 'instances': []}
    finally:
        socket.setdefaulttimeout(None)


# ---------------------------------------------------------------------------
# fetch_save_path_hint
# ---------------------------------------------------------------------------

def fetch_save_path_hint(payload):
    try:
        socket.setdefaulttimeout(10)
        client = qbittorrentapi.Client(
            host=payload.get('QB_HOST'), username=payload.get('QB_USER'), password=payload.get('QB_PASS'))
        client.auth_log_in()
        torrents = list(client.torrents_info(limit=50))
        version  = client.app.version
        seeding_size = sum(t.size for t in torrents if t.state in ('uploading', 'stalledUP', 'forcedUP'))
        paths = [t.save_path.rstrip('/') for t in torrents if t.save_path]
        if not paths:
            save_path = None
        else:
            try:
                save_path = os.path.commonpath(paths) if len(paths) > 1 else paths[0]
            except ValueError:
                save_path = paths[0]
        return {
            'save_path':     save_path,
            'version':       version,
            'torrent_count': len(torrents),
            'seeding_size':  seeding_size,
            'instances':     [],
        }
    finally:
        socket.setdefaulttimeout(None)
