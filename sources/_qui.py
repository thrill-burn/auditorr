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
    {"torrents": [...], "total": N, "page": N, "limit": N}
    torrent fields (confirmed against live API): hash, infohash_v1, name, save_path,
    size, total_size, state, uploaded, uploaded_session, ratio, upspeed, tracker, ...
    NOTE: OpenAPI spec is incomplete — actual response uses snake_case and includes
    uploaded (cumulative bytes). The spec's Torrent schema is inaccurate.

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

from sources import SourceConnectionError, classify_tracker_entries

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


def _connection_error_message(base):
    return f"Could not reach qui at '{base}' - check the host URL and ensure qui is running."


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
        'category':  t.get('category') or '',
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
        for key in ('torrents', 'cross_instance_torrents', 'data', 'files', 'trackers'):
            if key in response_json:
                return response_json[key] or []
    return []


def _get_version(sess, base, eligible_instances):
    """Get qBittorrent version from the first eligible instance's app-info."""
    if not eligible_instances:
        return None
    try:
        inst_id = eligible_instances[0]['id']
        vr = sess.get(f'{base}/api/instances/{inst_id}/app-info', timeout=5)
        if vr.ok:
            return vr.json().get('version')
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Per-instance helpers
# ---------------------------------------------------------------------------

def _fetch_all_torrents(session, base, inst_id):
    """Fetch all torrents for one instance.

    Uses hash-deduplication so the loop terminates even if the API ignores
    the offset parameter and returns the same page on every request.
    """
    # qui uses page-based pagination (0-indexed), max limit=2000 per page
    all_torrents = []
    seen_hashes  = set()
    page         = 0
    limit        = 2000
    total_hint   = None  # populated from first response's 'total' field

    for _ in range(10000):  # safety cap
        resp = session.get(
            f'{base}/api/instances/{inst_id}/torrents',
            params={'limit': limit, 'page': page},
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()

        # Extract total count from envelope if present
        if isinstance(raw, dict) and 'total' in raw and total_hint is None:
            total_hint = raw['total']

        batch = _unwrap(raw)

        if not batch:
            log.info('qui: instance %s page %d: empty — done. total=%d (expected=%s)',
                     inst_id, page, len(all_torrents), total_hint)
            break

        new_items = []
        for t in batch:
            h = t.get('hash') or t.get('infohash_v1') or ''
            if not h or h not in seen_hashes:
                if h:
                    seen_hashes.add(h)
                new_items.append(t)

        all_torrents.extend(new_items)
        log.info('qui: instance %s page %d: %d items, %d new, running_total=%d/%s',
                 inst_id, page, len(batch), len(new_items), len(all_torrents), total_hint)
        page += 1

        # Done if: last page, all caught up, or API looping
        if len(batch) < limit:
            break
        if total_hint is not None and len(all_torrents) >= total_hint:
            break
        if not new_items:
            log.warning(
                'qui: instance %s returned all-duplicate hashes on page %d — '
                'stopping. Got %d/%s unique torrents.',
                inst_id, page, len(all_torrents), total_hint,
            )
            break

    return all_torrents


def _fetch_torrent_data(session, base, inst_id, torrent_hash):
    """Fetch files + trackers for a single torrent.

    Returns (hosts, (health, msg), files) — the tracker entries are classified
    here (dead-seed detection) since status/msg ride along with the same call.
    """
    try:
        tr_resp = session.get(
            f'{base}/api/instances/{inst_id}/torrents/{torrent_hash}/trackers',
            timeout=8,
        )
        tr_resp.raise_for_status()
        raw_trackers = _unwrap(tr_resp.json())
        hosts = _tracker_hosts(raw_trackers)
        health = classify_tracker_entries([
            {
                'url':    t.get('url') or t.get('announce') or '',
                'status': t.get('status'),
                'msg':    t.get('msg') or t.get('message') or '',
            }
            for t in raw_trackers
        ])
    except Exception:
        hosts  = ['Unknown']
        health = ('unknown', '')

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

    return hosts, health, files


def _process_instance(session, base, inst, remote_path, local_path,
                      file_map, trackers_set, tracker_upload, tracker_seeding_size,
                      seen_hashes):
    inst_id   = inst['id']
    inst_name = inst.get('name', str(inst_id))

    torrents = _fetch_all_torrents(session, base, inst_id)

    tracker_map = {}
    health_map  = {}
    files_map   = {}

    def _fetch(t):
        th = _norm_torrent(t)['hash']
        if not th:
            return th, ['Unknown'], ('unknown', ''), []
        hosts, health, files = _fetch_torrent_data(session, base, inst_id, th)
        return th, hosts, health, files

    # Per-torrent file/tracker fetch — bounded total timeout so a single
    # hung request can't stall the whole scan indefinitely.
    _PER_INSTANCE_TIMEOUT = 300  # 5 min ceiling regardless of library size
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch, t): t for t in torrents}
        try:
            for future in as_completed(futures, timeout=_PER_INSTANCE_TIMEOUT):
                try:
                    th, hosts, health, files = future.result()
                except Exception:
                    th    = _norm_torrent(futures[future])['hash']
                    hosts, health, files = ['Unknown'], ('unknown', ''), []
                if th:
                    tracker_map[th] = hosts
                    health_map[th]  = health
                    files_map[th]   = files
        except FuturesTimeout:
            log.warning('qui: timed out fetching per-torrent data for instance %s — '
                        'using partial results', inst_name)
            for f, t in futures.items():
                if not f.done():
                    th = _norm_torrent(t)['hash']
                    if th:
                        tracker_map.setdefault(th, ['Unknown'])
                        health_map.setdefault(th, ('unknown', ''))
                        files_map.setdefault(th, [])

    empty_file_count    = 0
    nonempty_file_count = 0
    disk_fallback_count = 0
    sample_paths        = []

    def _add_entry(full_path, status, hosts, category=''):
        health, health_msg = health_map.get(th, ('unknown', ''))
        entry = file_map.setdefault(full_path, {
            'status':        status,
            'trackers':      set(),
            'hash':          th,
            'instance_id':   inst_id,
            'instance_name': inst_name,
            'category':      category or '',
            'tracker_health': health,
            'tracker_msg':    health_msg,
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

        # Attribute upload/seeding stats once per unique torrent hash.
        # In multi-instance qui setups the per-instance torrent endpoint can
        # return all managed torrents regardless of which instance_id is queried,
        # so the same hash appears N times and inflates seeding_size by N×.
        # seen_hashes is shared across all _process_instance calls to prevent this.
        if th not in seen_hashes:
            seen_hashes.add(th)
            for h in hosts:
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
                _add_entry(full_path, status, hosts, nt.get('category', ''))
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
                    _add_entry(torrent_root, status, hosts, nt.get('category', ''))
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
                            _add_entry(full_path, status, hosts, nt.get('category', ''))

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
        base = cfg.get('QUI_HOST', '').rstrip('/')
        raise SourceConnectionError(f"qui connection error: {_connection_error_message(base)}") from e
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
    seen_hashes          = set()  # deduplicate stats across all instances

    for inst in eligible:
        try:
            _process_instance(sess, base, inst, remote_path, local_path,
                               file_map, trackers_set, tracker_upload, tracker_seeding_size,
                               seen_hashes)
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
    tracker_snapshot['_instance_count'] = len(eligible)

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
# fetch_torrent_details
# ---------------------------------------------------------------------------

def fetch_torrent_details(cfg, items):
    """Live lookup of upload stats + tracker health for specific torrents.

    items: [{'hash': str, 'instance_id': int|None}]. Hashes with a known
    instance_id only query that instance; hashes without one are looked up on
    every eligible instance. Returns {hash: details}; failures are best-effort
    (missing entries, never an exception).
    """
    base    = (cfg.get('QUI_HOST') or '').rstrip('/')
    api_key = cfg.get('QUI_API_KEY', '')
    if not base:
        return {}

    wanted = {}  # hash -> instance_id|None
    for i in items:
        h = i.get('hash')
        if h:
            wanted.setdefault(h, i.get('instance_id'))
    if not wanted:
        return {}

    details = {}
    try:
        sess = _session(api_key)
        resp = sess.get(f'{base}/api/instances', timeout=15)
        resp.raise_for_status()
        all_instances = resp.json()
        if not isinstance(all_instances, list):
            all_instances = _unwrap(all_instances)
        eligible = {i['id']: i for i in all_instances if _eligible(i)}
        if not eligible:
            return {}

        # Upload stats come from the per-instance torrent lists (no single-hash
        # endpoint is documented) — fetch each involved instance's list once.
        involved_ids = {inst_id for inst_id in wanted.values() if inst_id in eligible}
        if any(inst_id not in eligible for inst_id in wanted.values()):
            involved_ids = set(eligible)  # unknown instance — search everywhere
        hash_to_instance = {}
        for inst_id in involved_ids:
            try:
                for t in _fetch_all_torrents(sess, base, inst_id):
                    nt = _norm_torrent(t)
                    th = nt['hash']
                    if th in wanted and th not in details:
                        details[th] = {
                            'uploaded':       nt['uploaded'],
                            'ratio':          round(float(t.get('ratio') or 0), 3),
                            'seeding_time':   t.get('seeding_time') or t.get('seedingTime'),
                            'added_on':       t.get('added_on') or t.get('addedOn'),
                            'tracker_health': 'unknown',
                            'tracker_msg':    '',
                        }
                        hash_to_instance[th] = inst_id
            except Exception as e:
                log.warning('qui: torrent detail list failed for instance %s: %s', inst_id, e)

        def _fetch_health(torrent_hash, inst_id):
            try:
                tr_resp = sess.get(
                    f'{base}/api/instances/{inst_id}/torrents/{torrent_hash}/trackers',
                    timeout=8,
                )
                tr_resp.raise_for_status()
                entries = [
                    {
                        'url':    t.get('url') or t.get('announce') or '',
                        'status': t.get('status'),
                        'msg':    t.get('msg') or t.get('message') or '',
                    }
                    for t in _unwrap(tr_resp.json())
                ]
                return torrent_hash, classify_tracker_entries(entries)
            except Exception:
                return torrent_hash, ('unknown', '')

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_fetch_health, th, inst_id)
                       for th, inst_id in hash_to_instance.items()]
            for future in as_completed(futures, timeout=120):
                torrent_hash, (health, msg) = future.result()
                details[torrent_hash]['tracker_health'] = health
                details[torrent_hash]['tracker_msg']    = msg
    except Exception as e:
        log.warning('qui: fetch_torrent_details failed: %s', e)
    return details


# ---------------------------------------------------------------------------
# remove_torrents
# ---------------------------------------------------------------------------

def remove_torrents(cfg, items, delete_files=True):
    """Delete torrents — and their downloaded files — across qui instances.

    items: [{'hash': str, 'instance_id': int|None}]. Hashes without a known
    instance_id are located by listing every eligible instance first (same
    approach as fetch_torrent_details). Uses qui's bulk-action endpoint
    (POST /api/instances/{id}/torrents/bulk-action) with action
    'deleteWithFiles' / 'delete' — both confirmed in the action enum of a
    live instance's /api/openapi.json (2026-06-12).
    Returns the number of torrents submitted for deletion.
    """
    base    = (cfg.get('QUI_HOST') or '').rstrip('/')
    api_key = cfg.get('QUI_API_KEY', '')
    if not base:
        raise SourceConnectionError("QUI_HOST is not configured")

    wanted = {}  # hash -> instance_id|None
    for i in items:
        h = i.get('hash')
        if h:
            wanted.setdefault(h, i.get('instance_id'))
    if not wanted:
        return 0

    socket.setdefaulttimeout(30)
    try:
        sess = _session(api_key)
        resp = sess.get(f'{base}/api/instances', timeout=15)
        resp.raise_for_status()
        all_instances = resp.json()
        if not isinstance(all_instances, list):
            all_instances = _unwrap(all_instances)
        eligible = {i['id'] for i in all_instances if _eligible(i)}
        if not eligible:
            raise SourceConnectionError("No eligible qui instances")

        by_instance = {}  # instance_id -> [hashes]
        unresolved  = []
        for h, inst_id in wanted.items():
            if inst_id in eligible:
                by_instance.setdefault(inst_id, []).append(h)
            else:
                unresolved.append(h)

        if unresolved:
            remaining = set(unresolved)
            for inst_id in eligible:
                if not remaining:
                    break
                try:
                    found = []
                    for t in _fetch_all_torrents(sess, base, inst_id):
                        h = _norm_torrent(t)['hash']
                        if h in remaining:
                            found.append(h)
                except Exception as e:
                    log.warning('qui: torrent list failed for instance %s: %s', inst_id, e)
                    continue
                if found:
                    by_instance.setdefault(inst_id, []).extend(found)
                    remaining -= set(found)

        removed = 0
        for inst_id, hashes in by_instance.items():
            action_resp = sess.post(
                f'{base}/api/instances/{inst_id}/torrents/bulk-action',
                json={'hashes': hashes,
                      'action': 'deleteWithFiles' if delete_files else 'delete'},
                timeout=30,
            )
            action_resp.raise_for_status()
            removed += len(hashes)
        return removed
    except SourceConnectionError:
        raise
    except requests.exceptions.ConnectionError as e:
        raise SourceConnectionError(f"qui connection error: {_connection_error_message(base)}") from e
    except requests.exceptions.HTTPError as e:
        raise SourceConnectionError(f"qui HTTP error: {e}") from e
    except Exception as e:
        raise SourceConnectionError(f"qui error: {e}") from e
    finally:
        socket.setdefaulttimeout(None)


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

        eligible = [i for i in all_instances if _eligible(i)]
        version = _get_version(sess, base, eligible)
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

    eligible = [i for i in all_instances if _eligible(i)]
    n = len(all_instances)
    e = len(eligible)
    s = n - e
    summary = f'{n} instance{"s" if n != 1 else ""} ({e} scannable, {s} skipped)'
    version  = _get_version(sess, base, eligible)

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

    # Ask first eligible instance for path detection (page=0, limit=50 is enough).
    # Shared-fs setups report the same prefix from any instance.
    resp2 = sess.get(
        f'{base}/api/instances/{inst_id}/torrents',
        params={'limit': 50, 'page': 0},
        timeout=15,
    )
    resp2.raise_for_status()
    raw       = resp2.json()
    total     = raw.get('total', 0) if isinstance(raw, dict) else 0
    raw_torrents = _unwrap(raw)

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

    version = _get_version(sess, base, eligible)

    return {
        'save_path':     save_path,
        'version':       version,
        'torrent_count': total or len(raw_torrents),
        'seeding_size':  seeding_size,
        'instances':     all_instances,
    }
