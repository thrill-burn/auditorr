"""
qBittorrent backend — lifted verbatim from audit.py / app.py.

Public interface:
  fetch_file_map(cfg)          -> (file_map, sorted_trackers, tracker_snapshot)
  test_connection(payload)     -> {'ok': bool, 'version': str|None, 'error': str|None, 'instances': []}
  connection_info(cfg)         -> {'version': str|None, 'instance_summary': str, 'instances': []}
  fetch_save_path_hint(payload)-> {'save_path': str|None, 'version': str|None, 'torrent_count': int, 'seeding_size': int, 'instances': []}
"""

import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import qbittorrentapi

from sources import SourceConnectionError, classify_tracker_entries


# ---------------------------------------------------------------------------
# fetch_file_map
# ---------------------------------------------------------------------------

def fetch_file_map(cfg):
    socket.setdefaulttimeout(30)
    try:
        return _fetch_inner(cfg)
    except (qbittorrentapi.LoginFailed, qbittorrentapi.APIConnectionError) as e:
        raise SourceConnectionError(f"qBittorrent error: {e}") from e
    finally:
        socket.setdefaulttimeout(None)


def _fetch_inner(cfg):
    qbt = qbittorrentapi.Client(
        host=cfg.get('QB_HOST'),
        username=cfg.get('QB_USER'),
        password=cfg.get('QB_PASS'),
    )
    qbt.auth_log_in()
    torrents = list(qbt.torrents_info())

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
    # two API calls per worker instead of two separate executor pools.
    def _fetch_torrent_data(torrent):
        try:
            thread_qbt = _get_thread_client()
            raw   = [t.url for t in thread_qbt.torrents_trackers(torrent_hash=torrent.hash)
                     if t.url.startswith('http') or t.url.startswith('udp')]
            hosts = [t.split('/')[2] for t in raw if len(t.split('/')) > 2] or ['Unknown']
            files = list(thread_qbt.torrents_files(torrent_hash=torrent.hash))
        except Exception:
            hosts = ['Unknown']
            files = []
        return torrent.hash, hosts, files

    tracker_map = {}
    files_map   = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(_fetch_torrent_data, t): t for t in torrents}
        for future in as_completed(futures):
            torrent_hash, hosts, files = future.result()
            tracker_map[torrent_hash] = hosts
            files_map[torrent_hash]   = files

    # Build file map using pre-fetched tracker and file data
    file_map             = {}
    trackers_set         = set()
    tracker_upload       = {}
    tracker_seeding_size = {}
    remote_path          = cfg.get('REMOTE_PATH', '')
    local_path           = cfg.get('LOCAL_PATH', '')

    for torrent in torrents:
        hosts     = tracker_map.get(torrent.hash, ['Unknown'])
        for h in hosts:
            trackers_set.add(h)
            tracker_upload[h] = tracker_upload.get(h, 0) + torrent.uploaded
            if torrent.state in ('uploading', 'stalledUP', 'forcedUP'):
                tracker_seeding_size[h] = tracker_seeding_size.get(h, 0) + torrent.size
        save_path = torrent.save_path
        if remote_path and save_path.startswith(remote_path) and \
                save_path[len(remote_path):][:1] in ('/', ''):
            save_path = local_path + save_path[len(remote_path):]
        if torrent.state in ('uploading', 'stalledUP', 'forcedUP'):
            status = 'Seeding'
        elif torrent.state in ('downloading', 'stalledDL'):
            status = 'Downloading'
        else:
            status = 'Paused'
        for f in files_map.get(torrent.hash, []):
            full_path = os.path.join(save_path, f.name)
            entry = file_map.setdefault(full_path, {
                "status": status,
                "trackers": set(),
                "hash": torrent.hash,
                "category": getattr(torrent, 'category', '') or '',
            })
            entry["trackers"].update(hosts)
            if status == 'Seeding' or entry["status"] == 'Seeding':
                entry["status"] = 'Seeding'
            elif entry["status"] == 'Paused':
                entry["status"] = status
    del tracker_map, files_map

    all_hosts        = set(tracker_upload) | set(tracker_seeding_size)
    tracker_snapshot = {
        host: {
            "uploaded":     tracker_upload.get(host, 0),
            "seeding_size": tracker_seeding_size.get(host, 0),
        }
        for host in all_hosts
    }
    tracker_snapshot['_instance_count'] = 1

    return file_map, sorted(trackers_set), tracker_snapshot


# ---------------------------------------------------------------------------
# fetch_torrent_details
# ---------------------------------------------------------------------------

def fetch_torrent_details(cfg, items):
    """Live lookup of upload stats + tracker health for specific torrents.

    items: [{'hash': str, ...}] — instance_id is ignored (single instance).
    Returns {hash: {'uploaded', 'ratio', 'added_on', 'tracker_health', 'tracker_msg'}}.
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
                'added_on':       getattr(torrent, 'added_on', None),
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
        return details
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
