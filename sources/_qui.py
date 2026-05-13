"""
qui backend for the torrent-source abstraction.

Verified against: getqui.com/docs/api/overview (2026-05-12).
Confirmed from docs:
  - Auth: X-API-Key request header (created via Settings → API Keys; full-access)
  - GET /api/instances — confirmed in curl example on docs page
  - Default port: 7476
  - Interactive swagger at {QUI_HOST}/api/docs on each instance

Torrent sub-endpoints, pagination params, and response field names are
derived from the prompt spec + qBittorrent-compatible REST conventions.
Verify against /api/docs on your qui instance before reporting discrepancies.

Expected response shapes (may differ — check /api/docs):
  GET /api/instances
    [{id, name, host, connected, hasLocalFilesystemAccess, useHardlinks, useReflinks, ...}]

  GET /api/instances/{id}/torrents?limit=N&offset=M
    [{hash, state, save_path, size, uploaded, name, ...}]  (qBit-compatible field names)
    OR {"data": [...], "total": N}  (paginated envelope)

  GET /api/instances/{id}/torrents/{hash}/files
    [{name, size, ...}]  (name = relative path inside torrent)

  GET /api/instances/{id}/torrents/{hash}/trackers
    [{url, ...}]

Public interface:
  fetch_file_map(cfg)          -> (file_map, sorted_trackers, tracker_snapshot)
  test_connection(payload)     -> {'ok': bool, 'version': str|None, 'error': str|None, 'instances': [...]}
  connection_info(cfg)         -> {'version': str|None, 'instance_summary': str, 'instances': [...]}
  fetch_save_path_hint(payload)-> {'save_path': str|None, 'version': str|None, 'torrent_count': int, 'seeding_size': int, 'instances': [...]}
"""

import os
import socket
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

import requests

from sources import SourceConnectionError

log = logging.getLogger(__name__)

_SEEDING_STATES    = {'uploading', 'stalledUP', 'forcedUP', 'forcedUploadingUP'}
_DOWNLOADING_STATES = {'downloading', 'stalledDL', 'forcedDL', 'forcedDownloadingDL'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session(api_key):
    s = requests.Session()
    s.headers['X-API-Key'] = api_key
    return s


def _eligible(instance):
    return (
        instance.get('connected')
        and instance.get('hasLocalFilesystemAccess')
    )


def _skip_reason(instance):
    if not instance.get('connected'):
        return 'disconnected'
    return 'no local filesystem access'


def _norm_torrent(t):
    """Normalise a torrent dict — handles both snake_case and camelCase field names."""
    return {
        'hash':      t.get('hash') or t.get('infohash_v1') or '',
        'state':     t.get('state') or '',
        'save_path': (t.get('save_path') or t.get('savePath') or '').rstrip('/'),
        'size':      t.get('size') or t.get('total_size') or t.get('totalSize') or 0,
        'uploaded':  t.get('uploaded') or t.get('uploadedEver') or 0,
        'name':      t.get('name') or '',
    }


def _norm_file(f):
    return {
        'name': f.get('name') or f.get('path') or '',
        'size': f.get('size') or 0,
    }


def _tracker_hosts(raw_trackers):
    hosts = []
    for t in raw_trackers:
        url = t.get('url') or t.get('announce') or ''
        if url.startswith('http') or url.startswith('udp'):
            parts = url.split('/')
            if len(parts) > 2:
                hosts.append(parts[2])
    return hosts or ['Unknown']


def _torrent_status(state):
    if state in _SEEDING_STATES:
        return 'Seeding'
    if state in _DOWNLOADING_STATES:
        return 'Downloading'
    return 'Paused'


def _unwrap(response_json):
    """Handle both bare list and {data:[...]} / {torrents:[...]} envelope."""
    if isinstance(response_json, list):
        return response_json
    if isinstance(response_json, dict):
        return (response_json.get('data')
                or response_json.get('torrents')
                or response_json.get('files')
                or response_json.get('trackers')
                or [])
    return []


# ---------------------------------------------------------------------------
# Per-instance helpers
# ---------------------------------------------------------------------------

def _fetch_all_torrents(session, base, inst_id):
    """Fetch all torrents for one instance.

    Uses hash-deduplication so the loop terminates even if the API ignores
    the offset parameter and returns the same page on every request.
    """
    all_torrents = []
    seen_hashes  = set()
    offset       = 0
    limit        = 1000

    for _ in range(200):  # safety cap: at most 200,000 torrents
        resp = session.get(
            f'{base}/api/instances/{inst_id}/torrents',
            params={'limit': limit, 'offset': offset},
            timeout=30,
        )
        resp.raise_for_status()
        batch = _unwrap(resp.json())
        if not batch:
            break

        new_items = []
        for t in batch:
            h = t.get('hash') or t.get('infohash_v1') or ''
            if not h or h not in seen_hashes:
                if h:
                    seen_hashes.add(h)
                new_items.append(t)

        all_torrents.extend(new_items)

        # Stop when: last page (fewer than limit), or API ignored offset
        # (no new unique hashes came back — we've seen everything).
        if len(batch) < limit or not new_items:
            break
        offset += limit

    return all_torrents


def _fetch_torrent_data(session, base, inst_id, torrent_hash):
    """Fetch files + trackers for a single torrent; returns (hosts, files)."""
    try:
        tr_resp = session.get(
            f'{base}/api/instances/{inst_id}/torrents/{torrent_hash}/trackers',
            timeout=8,
        )
        tr_resp.raise_for_status()
        hosts = _tracker_hosts(_unwrap(tr_resp.json()))
    except Exception:
        hosts = ['Unknown']

    try:
        fi_resp = session.get(
            f'{base}/api/instances/{inst_id}/torrents/{torrent_hash}/files',
            timeout=8,
        )
        fi_resp.raise_for_status()
        raw_files = _unwrap(fi_resp.json())
        files = [_norm_file(f) for f in raw_files]
        if not files and raw_files is not None:
            log.debug('qui: files endpoint returned empty list for %s', torrent_hash)
    except Exception as e:
        log.debug('qui: files endpoint failed for %s: %s', torrent_hash, e)
        files = []

    return hosts, files


def _process_instance(session, base, inst, remote_path, local_path,
                      file_map, trackers_set, tracker_upload, tracker_seeding_size):
    inst_id   = inst['id']
    inst_name = inst.get('name', str(inst_id))

    torrents = _fetch_all_torrents(session, base, inst_id)

    tracker_map = {}
    files_map   = {}

    def _fetch(t):
        th = _norm_torrent(t)['hash']
        if not th:
            return th, ['Unknown'], []
        hosts, files = _fetch_torrent_data(session, base, inst_id, th)
        return th, hosts, files

    # Per-torrent file/tracker fetch — bounded total timeout so a single
    # hung request can't stall the whole scan indefinitely.
    _PER_INSTANCE_TIMEOUT = 300  # 5 min ceiling regardless of library size
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch, t): t for t in torrents}
        try:
            for future in as_completed(futures, timeout=_PER_INSTANCE_TIMEOUT):
                try:
                    th, hosts, files = future.result()
                except Exception:
                    th    = _norm_torrent(futures[future])['hash']
                    hosts, files = ['Unknown'], []
                if th:
                    tracker_map[th] = hosts
                    files_map[th]   = files
        except FuturesTimeout:
            log.warning('qui: timed out fetching per-torrent data for instance %s — '
                        'using partial results', inst_name)
            for f, t in futures.items():
                if not f.done():
                    th = _norm_torrent(t)['hash']
                    if th:
                        tracker_map.setdefault(th, ['Unknown'])
                        files_map.setdefault(th, [])

    empty_file_count    = 0
    nonempty_file_count = 0
    disk_fallback_count = 0
    sample_paths        = []

    def _add_entry(full_path, status, hosts):
        entry = file_map.setdefault(full_path, {
            'status':        status,
            'trackers':      set(),
            'hash':          th,
            'instance_id':   inst_id,
            'instance_name': inst_name,
        })
        entry['trackers'].update(hosts)
        if status == 'Seeding' or entry['status'] == 'Seeding':
            entry['status'] = 'Seeding'
        elif entry['status'] == 'Paused':
            entry['status'] = status

    for torrent in torrents:
        nt = _norm_torrent(torrent)
        th = nt['hash']
        if not th:
            continue
        hosts  = tracker_map.get(th, ['Unknown'])
        status = _torrent_status(nt['state'])

        for h in hosts:
            trackers_set.add(h)
            tracker_upload[h] = tracker_upload.get(h, 0) + nt['uploaded']
            if status == 'Seeding':
                tracker_seeding_size[h] = tracker_seeding_size.get(h, 0) + nt['size']

        save_path = nt['save_path']
        raw_save_path = save_path
        if remote_path and save_path.startswith(remote_path) and \
                save_path[len(remote_path):][:1] in ('/', ''):
            save_path = local_path + save_path[len(remote_path):]

        torrent_files = files_map.get(th, [])

        if torrent_files:
            nonempty_file_count += 1
            for f in torrent_files:
                full_path = os.path.join(save_path, f['name'])
                if len(sample_paths) < 3:
                    sample_paths.append({
                        'source': 'api',
                        'raw_save_path': raw_save_path,
                        'mapped_save_path': save_path,
                        'file_name': f['name'],
                        'full_path': full_path,
                    })
                _add_entry(full_path, status, hosts)
        else:
            empty_file_count += 1
            # Fallback: qui may not expose per-torrent file lists.
            # Use torrent name to locate files on local filesystem.
            torrent_name = nt['name']
            if torrent_name and save_path:
                torrent_root = os.path.join(save_path, torrent_name)
                if os.path.isfile(torrent_root):
                    # Single-file torrent
                    disk_fallback_count += 1
                    if len(sample_paths) < 3:
                        sample_paths.append({
                            'source': 'disk_fallback_file',
                            'raw_save_path': raw_save_path,
                            'mapped_save_path': save_path,
                            'file_name': torrent_name,
                            'full_path': torrent_root,
                        })
                    _add_entry(torrent_root, status, hosts)
                elif os.path.isdir(torrent_root):
                    # Multi-file torrent — walk only the torrent's own subtree
                    for dir_root, _, dir_files in os.walk(torrent_root):
                        for fname in dir_files:
                            full_path = os.path.join(dir_root, fname)
                            disk_fallback_count += 1
                            if len(sample_paths) < 3:
                                sample_paths.append({
                                    'source': 'disk_fallback_dir',
                                    'raw_save_path': raw_save_path,
                                    'mapped_save_path': save_path,
                                    'file_name': os.path.relpath(full_path, save_path),
                                    'full_path': full_path,
                                })
                            _add_entry(full_path, status, hosts)

    if disk_fallback_count:
        log.warning(
            'qui[%s]: %d torrents had no file data from API — used disk fallback '
            '(walked save_path/name on local filesystem). '
            'Check if qui exposes GET /api/instances/{id}/torrents/{hash}/files',
            inst_name, empty_file_count,
        )

    log.info(
        'qui[%s]: %d torrents — %d with file lists, %d empty '
        '(%d disk-fallback entries). file_map total: %d. remote_path=%r local_path=%r',
        inst_name, len(torrents), nonempty_file_count, empty_file_count,
        disk_fallback_count, len(file_map), remote_path, local_path,
    )
    for sp in sample_paths:
        log.info(
            'qui[%s] sample path [%s] — raw_save=%r mapped_save=%r file_name=%r full_path=%r',
            inst_name, sp['source'], sp['raw_save_path'], sp['mapped_save_path'],
            sp['file_name'], sp['full_path'],
        )


# ---------------------------------------------------------------------------
# fetch_file_map
# ---------------------------------------------------------------------------

def fetch_file_map(cfg):
    socket.setdefaulttimeout(30)
    try:
        return _fetch_inner(cfg)
    except SourceConnectionError:
        raise
    except requests.exceptions.ConnectionError as e:
        raise SourceConnectionError(f"qui connection error: {e}") from e
    except requests.exceptions.HTTPError as e:
        raise SourceConnectionError(f"qui HTTP error: {e}") from e
    except Exception as e:
        raise SourceConnectionError(f"qui error: {e}") from e
    finally:
        socket.setdefaulttimeout(None)


def _fetch_inner(cfg):
    base    = cfg.get('QUI_HOST', '').rstrip('/')
    api_key = cfg.get('QUI_API_KEY', '')
    remote_path = cfg.get('REMOTE_PATH', '')
    local_path  = cfg.get('LOCAL_PATH', '')

    if not base:
        raise SourceConnectionError("QUI_HOST is not configured")

    sess = _session(api_key)

    resp = sess.get(f'{base}/api/instances', timeout=15)
    resp.raise_for_status()
    all_instances = resp.json()
    if not isinstance(all_instances, list):
        all_instances = _unwrap(all_instances)

    eligible = [i for i in all_instances if _eligible(i)]
    skipped  = [i for i in all_instances if not _eligible(i)]

    if not eligible:
        reasons = '; '.join(f"{i.get('name','?')}: {_skip_reason(i)}" for i in skipped[:5])
        raise SourceConnectionError(
            f"No eligible qui instances (need connected + hasLocalFilesystemAccess). "
            f"Skipped: {reasons or 'none'}"
        )

    log.info(f"qui: {len(eligible)} eligible instance(s), {len(skipped)} skipped")

    file_map             = {}
    trackers_set         = set()
    tracker_upload       = {}
    tracker_seeding_size = {}

    for inst in eligible:
        try:
            _process_instance(sess, base, inst, remote_path, local_path,
                               file_map, trackers_set, tracker_upload, tracker_seeding_size)
        except Exception as e:
            log.warning(f"qui: skipping instance {inst.get('name','?')} due to error: {e}")

    all_hosts        = set(tracker_upload) | set(tracker_seeding_size)
    tracker_snapshot = {
        host: {
            'uploaded':     tracker_upload.get(host, 0),
            'seeding_size': tracker_seeding_size.get(host, 0),
        }
        for host in all_hosts
    }

    if not file_map:
        log.warning(
            'qui: file_map is EMPTY after processing all instances — all torrent '
            'files will appear Orphaned. Likely cause: REMOTE_PATH/LOCAL_PATH '
            'path mapping is incorrect so save_path substitution failed, OR '
            'qui reported no torrents. Check per-instance logs above.'
        )
    else:
        log.info('qui: total file_map entries across all instances: %d', len(file_map))

    return file_map, sorted(trackers_set), tracker_snapshot


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------

def test_connection(payload):
    base    = (payload.get('QUI_HOST') or '').rstrip('/')
    api_key = payload.get('QUI_API_KEY', '')

    if not base:
        return {'ok': False, 'version': None, 'error': 'QUI_HOST is required', 'instances': []}

    try:
        sess = _session(api_key)
        resp = sess.get(f'{base}/api/instances', timeout=8)
        if resp.status_code == 401:
            return {'ok': False, 'version': None, 'error': 'Invalid API key — check Settings → API Keys in qui', 'instances': []}
        resp.raise_for_status()
        all_instances = resp.json()
        if not isinstance(all_instances, list):
            all_instances = _unwrap(all_instances)

        version = None
        try:
            vr = sess.get(f'{base}/api/version', timeout=5)
            if vr.ok:
                vd = vr.json()
                version = (vd.get('version') or vd.get('qui_version')
                           or vd.get('app_version') or str(vd) if isinstance(vd, str) else None)
        except Exception:
            pass

        eligible = [i for i in all_instances if _eligible(i)]
        skipped  = [
            {**i, '_skip_reason': _skip_reason(i)}
            for i in all_instances if not _eligible(i)
        ]

        return {
            'ok':              True,
            'version':         version,
            'error':           None,
            'instances':       all_instances,
            'eligible_count':  len(eligible),
            'skipped':         skipped,
        }
    except requests.exceptions.ConnectionError:
        return {'ok': False, 'version': None,
                'error': f"Could not reach qui at '{base}' — check the host URL and ensure qui is running.",
                'instances': []}
    except requests.exceptions.Timeout:
        return {'ok': False, 'version': None, 'error': 'Connection timed out', 'instances': []}
    except requests.exceptions.HTTPError as e:
        return {'ok': False, 'version': None, 'error': f'HTTP {e.response.status_code}', 'instances': []}
    except Exception as e:
        return {'ok': False, 'version': None, 'error': str(e), 'instances': []}


# ---------------------------------------------------------------------------
# connection_info
# ---------------------------------------------------------------------------

def connection_info(cfg):
    base    = cfg.get('QUI_HOST', '').rstrip('/')
    api_key = cfg.get('QUI_API_KEY', '')

    sess = _session(api_key)
    resp = sess.get(f'{base}/api/instances', timeout=15)
    resp.raise_for_status()
    all_instances = resp.json()
    if not isinstance(all_instances, list):
        all_instances = _unwrap(all_instances)

    n = len(all_instances)
    e = sum(1 for i in all_instances if _eligible(i))
    s = n - e
    summary = f'{n} instance{"s" if n != 1 else ""} ({e} scannable, {s} skipped)'

    version = None
    try:
        vr = sess.get(f'{base}/api/version', timeout=5)
        if vr.ok:
            vd = vr.json()
            version = vd.get('version') or vd.get('qui_version') or vd.get('app_version')
    except Exception:
        pass

    return {'version': version, 'instance_summary': summary, 'instances': all_instances}


# ---------------------------------------------------------------------------
# fetch_save_path_hint
# ---------------------------------------------------------------------------

def fetch_save_path_hint(payload):
    base    = (payload.get('QUI_HOST') or '').rstrip('/')
    api_key = payload.get('QUI_API_KEY', '')

    sess = _session(api_key)
    resp = sess.get(f'{base}/api/instances', timeout=15)
    resp.raise_for_status()
    all_instances = resp.json()
    if not isinstance(all_instances, list):
        all_instances = _unwrap(all_instances)

    eligible = [i for i in all_instances if _eligible(i)]
    if not eligible:
        return {'save_path': None, 'version': None, 'torrent_count': 0, 'seeding_size': 0,
                'instances': all_instances}

    first   = eligible[0]
    inst_id = first['id']

    # Ask first eligible instance for ~50 torrents for path detection.
    # Shared-fs setups report the same prefix from any instance.
    resp2 = sess.get(
        f'{base}/api/instances/{inst_id}/torrents',
        params={'limit': 50, 'offset': 0},
        timeout=15,
    )
    resp2.raise_for_status()
    raw_torrents = _unwrap(resp2.json())

    paths        = []
    seeding_size = 0
    for t in raw_torrents:
        nt = _norm_torrent(t)
        if nt['save_path']:
            paths.append(nt['save_path'])
        if _torrent_status(nt['state']) == 'Seeding':
            seeding_size += nt['size']

    save_path = None
    if paths:
        try:
            save_path = os.path.commonpath(paths) if len(paths) > 1 else paths[0]
        except ValueError:
            save_path = paths[0]

    version = None
    try:
        vr = sess.get(f'{base}/api/version', timeout=5)
        if vr.ok:
            vd = vr.json()
            version = vd.get('version') or vd.get('qui_version') or vd.get('app_version')
    except Exception:
        pass

    return {
        'save_path':     save_path,
        'version':       version,
        'torrent_count': len(raw_torrents),
        'seeding_size':  seeding_size,
        'instances':     all_instances,
    }
