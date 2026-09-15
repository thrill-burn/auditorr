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
import time
import socket
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

import requests

from sources import (
    SourceConnectionError, classify_tracker_entries, HEALTH_RANK as _HEALTH_RANK,
    new_source_report, report_instance_failure, report_note,
    torrent_complete, torrent_claimed_paths, remap_path,
)

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


def _pick(t, *keys):
    """First key present with a non-None value, or None.

    Not `a or b or 0` like the fields below it: `progress` of 0.0 and
    `completion_on` of 0 are real answers, and collapsing them into the same
    falsy hole as "the field is absent" is precisely the R1 mistake one layer
    down — `torrent_complete` needs None to mean *could not determine*.
    """
    for k in keys:
        v = t.get(k)
        if v is not None:
            return v
    return None


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
        # Cumulative seconds spent seeding. Present on the list payload (the
        # same field fetch_torrent_details reads), so the Next steps seeding-time
        # ladders cost no extra call.
        'seeding_time': t.get('seeding_time') or t.get('seedingTime') or 0,
        # Completion. M7 confirmed the client *exposes* all three on both
        # backends; it did not say auditorr read them, and until now this
        # normalizer carried none of them — so a paused incomplete torrent was
        # indistinguishable from a paused complete one for every consumer
        # downstream. All three ride the listing this already parses.
        'progress':      _pick(t, 'progress', 'percentDone'),
        'completion_on': _pick(t, 'completion_on', 'completionOn', 'completedOn'),
        'content_path':  (t.get('content_path') or t.get('contentPath') or '').rstrip('/'),
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

class ListingIncomplete(Exception):
    """An instance's torrent listing could not be shown to be complete."""


_LISTING_MAX_PAGES = 10000  # safety cap


def _fetch_all_torrents(session, base, inst_id):
    """Every torrent on one instance, or raise `ListingIncomplete`.

    S02 (the 2026-09-10 outside review): a successful response is not a complete
    snapshot. The loop stopped on a short page, an empty page or a page of
    repeats and never compared what it had with the `total` the first page
    advertised — it only logged it — so a listing that stopped short came back
    as complete and the instance counted as answered. Raising puts it on the
    channel every consumer already refuses on: `fetch_file_map` and
    `list_torrents` record a failed instance (the guard's
    `instances_unavailable`, Cleanup's re-verify, `sources.list_torrents`), and
    `fetch_torrent_details` treats it as a listing that did not answer (T10).

    Complete means, with a `total`: at least that many distinct torrents,
    measured against the *first* page's total — a torrent added mid-listing
    cannot fail it, and one removed mid-listing fails toward refusing. With no
    `total`: a page shorter than the limit is the last page, by the API's own
    convention and the only evidence there is; a full page of nothing but
    repeats means the API is ignoring `page`, and nothing shows the list ends
    there, so that fails. Hash de-duplication still guarantees termination.
    qbit has no equivalent — `torrents_info()` is one call that returns every
    torrent or raises.
    """
    # qui uses page-based pagination (0-indexed), max limit=2000 per page
    all_torrents = []
    seen_hashes  = set()
    page         = 0
    limit        = 2000
    total_hint   = None  # populated from first response's 'total' field

    for _ in range(_LISTING_MAX_PAGES):
        resp = session.get(
            f'{base}/api/instances/{inst_id}/torrents',
            params={'limit': limit, 'page': page},
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()

        # Extract total count from envelope if present
        if isinstance(raw, dict) and 'total' in raw and total_hint is None:
            try:
                total_hint = int(raw['total'])
            except (TypeError, ValueError):
                total_hint = None

        batch = _unwrap(raw)

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

        if total_hint is not None and len(all_torrents) >= total_hint:
            return all_torrents
        if not batch or len(batch) < limit:
            break  # the API's last page
        if not new_items:
            if total_hint is None:
                raise ListingIncomplete(
                    f"listed {len(all_torrents)} torrents and then the same page again, with no "
                    f"total to show the list ends there")
            break
    else:
        raise ListingIncomplete(f"listed {len(all_torrents)} torrents and hit the page cap")

    if total_hint is not None and len(all_torrents) < total_hint:
        log.warning('qui: instance %s listed %d of %d torrents — the listing stopped short',
                    inst_id, len(all_torrents), total_hint)
        raise ListingIncomplete(
            f"listed {len(all_torrents)} of {total_hint} torrents — the listing stopped short")
    return all_torrents


def _fetch_torrent_data(session, base, inst_id, torrent_hash):
    """Fetch files + trackers for a single torrent.

    Returns (hosts, (health, msg), files, files_ok) — the tracker entries are
    classified here (dead-seed detection) since status/msg ride along with the
    same call. `files_ok` is False when the file listing could not be fetched,
    which is a different thing from a torrent that reports no files.
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
        files_ok = True
        if not files and raw_files is not None:
            log.debug('qui: files endpoint returned empty list for %s', torrent_hash)
    except Exception as e:
        log.debug('qui: files endpoint failed for %s: %s', torrent_hash, e)
        files, files_ok = [], False

    return hosts, health, files, files_ok


def _process_instance(session, base, inst, remote_path, local_path,
                      file_map, trackers_set, tracker_upload, tracker_seeding_size,
                      seen_hashes, seed_totals=None, report=None, unresolved_roots=None):
    inst_id   = inst['id']
    inst_name = inst.get('name', str(inst_id))

    torrents = _fetch_all_torrents(session, base, inst_id)
    if report is not None:
        report['torrent_count'] += len(torrents)

    tracker_map = {}
    health_map  = {}
    files_map   = {}
    failed_listings = set()

    def _fetch(t):
        th = _norm_torrent(t)['hash']
        if not th:
            return th, ['Unknown'], ('unknown', ''), [], True
        hosts, health, files, files_ok = _fetch_torrent_data(session, base, inst_id, th)
        return th, hosts, health, files, files_ok

    # Per-torrent file/tracker fetch — bounded total timeout so a single
    # hung request can't stall the whole scan indefinitely.
    _PER_INSTANCE_TIMEOUT = 300  # 5 min ceiling regardless of library size
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch, t): t for t in torrents}
        try:
            for future in as_completed(futures, timeout=_PER_INSTANCE_TIMEOUT):
                try:
                    th, hosts, health, files, files_ok = future.result()
                except Exception:
                    th    = _norm_torrent(futures[future])['hash']
                    hosts, health, files, files_ok = ['Unknown'], ('unknown', ''), [], False
                if th:
                    tracker_map[th] = hosts
                    health_map[th]  = health
                    files_map[th]   = files
                    if not files_ok:
                        failed_listings.add(th)
        except FuturesTimeout:
            # The partial-results path. It used to be a log line and nothing
            # else, so a scan that reached this ceiling persisted its partial
            # answer as truth with nothing anywhere saying so. The un-answered
            # torrents are now marked as failed listings — which both routes
            # them through the disk fallback below and makes the scan's
            # incompleteness a value the audit can refuse to act on.
            timed_out = 0
            for f, t in futures.items():
                if not f.done():
                    th = _norm_torrent(t)['hash']
                    if th:
                        tracker_map.setdefault(th, ['Unknown'])
                        health_map.setdefault(th, ('unknown', ''))
                        files_map.setdefault(th, [])
                        failed_listings.add(th)
                        timed_out += 1
            log.warning('qui: timed out after %ds fetching per-torrent data for instance '
                        '%s — %d of %d torrents unanswered',
                        _PER_INSTANCE_TIMEOUT, inst_name, timed_out, len(torrents))
            if report is not None:
                report['partial'] = True
                report_note(report,
                            f"{inst_name}: per-torrent fetch hit the {_PER_INSTANCE_TIMEOUT}s "
                            f"ceiling with {timed_out} of {len(torrents)} torrents unanswered")

    empty_file_count    = 0
    nonempty_file_count = 0
    disk_fallback_count = 0
    sample_paths        = []

    # `complete` has no default on purpose: the safe-looking one (True) is the
    # unsafe one, so a call site that forgets it must fail loudly rather than
    # quietly declare an unfinished payload whole.
    def _add_entry(full_path, status, hosts, category, complete):
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
        # Sparse and sticky in the cautious direction — see the matching note
        # in _qbit.py. Absence means complete, so the flag costs memory only
        # where it is true; sticky because a cross-seed sharing this path whose
        # bytes are not whole makes hardlinking the inode a corruption risk.
        if complete is False:
            entry['incomplete'] = True
        elif complete is None:
            entry['completion_unknown'] = True
        # Cross-seeded paths: the record must reflect the HEALTHIEST claimant
        # — a path with any live torrent is not a dead seed, and deleting its
        # files would break that live torrent. Hash follows the health.
        if _HEALTH_RANK.get(health, 1) > _HEALTH_RANK.get(entry['tracker_health'], 1):
            entry['tracker_health'] = health
            entry['tracker_msg']    = health_msg
            entry['hash']           = th
            entry['instance_id']    = inst_id
            entry['instance_name']  = inst_name
        # Record every unregistered claimant of this path (see _qbit.py) so a
        # dead cross-seed whose payload is alive on a working sibling can still
        # be surfaced in Triage as a removable dead registration.
        if health == 'unregistered':
            entry.setdefault('unreg_claimants', {})[th] = {
                'hash': th, 'instance_id': inst_id, 'tracker_msg': health_msg,
            }
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
        # Completion is its own question: `_torrent_status` reads the state
        # string, where a paused incomplete and a paused complete are the same
        # word (R2, the root cause behind DEDUPE F6, TRIAGE T4 and CLEANUP C4).
        complete = torrent_complete(nt['progress'], nt['completion_on'])

        for h in hosts:
            trackers_set.add(h)

        # Attribute upload/seeding stats once per unique torrent hash.
        # In multi-instance qui setups the per-instance torrent endpoint can
        # return all managed torrents regardless of which instance_id is queried,
        # so the same hash appears N times and inflates seeding_size by N×.
        # seen_hashes is shared across all _process_instance calls to prevent this.
        if th not in seen_hashes:
            seen_hashes.add(th)
            # Inside the dedup block for the same reason the upload totals are:
            # qui's per-instance endpoint can return every managed torrent, so a
            # hash seen on three instances must be counted once.
            if report is not None:
                if complete is False:
                    report['incomplete_torrents'] += 1
                elif complete is None:
                    report['completion_unknown'] += 1
            for h in hosts:
                tracker_upload[h] = tracker_upload.get(h, 0) + nt['uploaded']
                if status == 'Seeding':
                    tracker_seeding_size[h] = tracker_seeding_size.get(h, 0) + nt['size']
            # Seeding-time aggregates for the Next steps prize layer — inside the
            # dedup block for the same reason the upload totals are: a hash seen
            # on three instances must be counted once.
            if seed_totals is not None:
                seed_secs = int(nt.get('seeding_time') or 0)
                if seed_secs > 0:
                    seed_totals['byte_secs'] += (nt['size'] or 0) * seed_secs
                    seed_totals['max_secs']   = max(seed_totals['max_secs'], seed_secs)

        raw_save_path = nt['save_path']
        save_path     = remap_path(raw_save_path, remote_path, local_path)
        # content_path needs the identical remapping or claiming it claims a
        # path the walk can never match.
        content_path  = remap_path(nt['content_path'], remote_path, local_path)

        torrent_files = files_map.get(th, [])

        listing_failed = th in failed_listings

        # The claim rule is `sources.torrent_claimed_paths`, shared with the
        # qbit backend and with Cleanup's live re-verify. qui passes `None` for
        # an *empty* listing as well as a failed one: qui may not expose
        # per-torrent file lists at all, so an empty answer is not evidence the
        # torrent holds nothing, and the payload is enumerated from disk — at
        # content_path while it is still downloading, at save_path/name once it
        # has finished — rather than left to default to 'Orphaned'.
        file_names = [f['name'] for f in torrent_files] if torrent_files else None
        full_paths = torrent_claimed_paths(
            save_path, nt['name'], content_path, file_names, complete)

        if file_names is not None:
            nonempty_file_count += 1
            for i, full_path in enumerate(full_paths):
                if len(sample_paths) < 3:
                    sample_paths.append({
                        # Anything past the file list is a claim on where an
                        # unfinished payload may actually be sitting.
                        'source': 'api' if i < len(file_names) else 'in_flight',
                        'raw_save_path': raw_save_path,
                        'mapped_save_path': save_path,
                        'file_name': file_names[i] if i < len(file_names) else '',
                        'full_path': full_path,
                    })
                _add_entry(full_path, status, hosts, nt.get('category', ''), complete)
        else:
            empty_file_count += 1
            recovered = full_paths
            for full_path in recovered:
                disk_fallback_count += 1
                if len(sample_paths) < 3:
                    sample_paths.append({
                        'source': 'disk_fallback',
                        'raw_save_path': raw_save_path,
                        'mapped_save_path': save_path,
                        # A content_path recovery can sit outside save_path
                        # entirely (a temp directory), where relpath is either
                        # a wall of '..' or, on Windows, a ValueError.
                        'file_name': (full_path[len(save_path):].lstrip('/\\')
                                      if full_path.startswith(save_path) else full_path),
                        'full_path': full_path,
                    })
                _add_entry(full_path, status, hosts, nt.get('category', ''), complete)
            if report is not None and listing_failed:
                report['listing_failures'] += 1
                if recovered:
                    report['listing_recovered'] += 1
                else:
                    report['listing_unresolved'] += 1
                    if unresolved_roots is not None:
                        unresolved_roots.extend(r for r in (save_path, content_path) if r)
                    # No infohash — see the matching note in _qbit.py.
                    report_note(report,
                                f"{inst_name}: file listing failed and nothing "
                                f"was found at {save_path}")

    if disk_fallback_count:
        log.warning(
            'qui[%s]: %d torrents had no file data from API — used disk fallback '
            '(walked save_path/name on local filesystem). '
            'Check if qui exposes GET /api/instances/{id}/torrents/{hash}/files',
            inst_name, empty_file_count,
        )

    log.info(
        'qui[%s]: %d torrents — %d with file lists, %d empty '
        '(%d failed outright, %d disk-fallback entries). file_map total: %d. '
        'remote_path=%r local_path=%r',
        inst_name, len(torrents), nonempty_file_count, empty_file_count,
        len(failed_listings), disk_fallback_count, len(file_map),
        remote_path, local_path,
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

def fetch_file_map(cfg, unresolved_roots=None):
    socket.setdefaulttimeout(30)
    try:
        return _fetch_inner(cfg, unresolved_roots)
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


def _fetch_inner(cfg, unresolved_roots=None):
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
    seed_totals          = {'byte_secs': 0, 'max_secs': 0}
    report               = new_source_report('qui')
    report['instances_total'] = len(eligible)

    for inst in eligible:
        try:
            _process_instance(sess, base, inst, remote_path, local_path,
                               file_map, trackers_set, tracker_upload, tracker_seeding_size,
                               seen_hashes, seed_totals, report=report,
                               unresolved_roots=unresolved_roots)
            report['instances_ok'] += 1
        except Exception as e:
            # Skipping an instance is still the right call — the others have
            # real answers and one unreachable box should not fail the scan —
            # but it is recorded rather than logged and forgotten. Every torrent
            # this instance manages is now absent from the map, so every file of
            # theirs is about to read as orphaned.
            log.warning(f"qui: skipping instance {inst.get('name','?')} due to error: {e}")
            report_instance_failure(report, inst.get('name', '?'), e)

    all_hosts        = set(tracker_upload) | set(tracker_seeding_size)
    tracker_snapshot = {
        host: {
            'uploaded':     tracker_upload.get(host, 0),
            'seeding_size': tracker_seeding_size.get(host, 0),
        }
        for host in all_hosts
    }
    tracker_snapshot['_instance_count'] = len(eligible)
    # '_'-prefixed: every per-tracker loop already skips these.
    tracker_snapshot['_seed_byte_secs'] = seed_totals['byte_secs']
    tracker_snapshot['_max_seed_secs']  = seed_totals['max_secs']

    report['file_map_size'] = len(file_map)
    if report['listing_failures']:
        report['partial'] = True
    if report['incomplete_torrents'] or report['completion_unknown']:
        log.info(
            'qui: %d torrent(s) not finished downloading, %d with no usable '
            'completion field. Their files are still claimed (so they do not read '
            'as orphans) but are kept out of duplicate groups and the Triage pile.',
            report['incomplete_torrents'], report['completion_unknown'])

    if not file_map:
        log.warning(
            'qui: file_map is EMPTY after processing all instances — all torrent '
            'files will appear Orphaned. Likely cause: REMOTE_PATH/LOCAL_PATH '
            'path mapping is incorrect so save_path substitution failed, OR '
            'qui reported no torrents. Check per-instance logs above.'
        )
    else:
        log.info('qui: total file_map entries across all instances: %d', len(file_map))

    return file_map, sorted(trackers_set), tracker_snapshot, report


# ---------------------------------------------------------------------------
# fetch_torrent_details
# ---------------------------------------------------------------------------

# T7 — Triage verifies a page in batches of 150, and every batch used to list each
# involved instance in full: a page at the cap issued seven full listings of a
# 20k-torrent instance where one would do. qBittorrent filters server-side
# (`torrents_info(torrent_hashes=…)`) and never had the problem. Each instance's
# listing is kept briefly — only the per-hash fields `fetch_torrent_details`
# returns, never the raw torrent JSON — and forgotten by any removal, or T10 would
# report a removed torrent as still there. The tracker fan-out is not cached and
# stays 8-wide.
_DETAIL_LISTING_TTL = 60
_detail_listings = {}            # (base, instance id) -> (monotonic ts, {hash: fields})
_detail_listings_lock = threading.Lock()


def _forget_detail_listings():
    """Drop every cached instance listing (any removal; tests)."""
    with _detail_listings_lock:
        _detail_listings.clear()


def _instance_detail_listing(sess, base, inst_id, fresh=False):
    """`(fields by hash, listed_now)` for one instance. Raises when the listing fails.

    `size` is `_norm_torrent`'s — qBittorrent's `size`, the files selected for
    download, before `total_size` (T5; see `_qbit.fetch_torrent_details`).
    `added_on` is read raw, as it always was: `_norm_torrent` carries it in
    neither spelling, and the probe's `phase9` section measures its presence.
    """
    key = (base, inst_id)
    if not fresh:
        with _detail_listings_lock:
            hit = _detail_listings.get(key)
        if hit is not None and time.monotonic() - hit[0] < _DETAIL_LISTING_TTL:
            return hit[1], False
    listing = {}
    for t in _fetch_all_torrents(sess, base, inst_id):
        nt = _norm_torrent(t)
        if nt['hash'] and nt['hash'] not in listing:
            listing[nt['hash']] = {
                'uploaded':     nt['uploaded'],
                'ratio':        round(float(t.get('ratio') or 0), 3),
                'seeding_time': t.get('seeding_time') or t.get('seedingTime'),
                'added_on':     t.get('added_on') or t.get('addedOn'),
                'size':         nt['size'],
            }
    with _detail_listings_lock:
        _detail_listings[key] = (time.monotonic(), listing)
    return listing, True


def fetch_torrent_details(cfg, items):
    """Live lookup of upload stats + tracker health for specific torrents.

    items: [{'hash': str, 'instance_id': int|None}]. Hashes with a known
    instance_id only query that instance; hashes without one are looked up on
    every eligible instance. Returns {hash: details}; failures are best-effort
    (missing entries, never an exception).

    **`{'found': False}` only where the listing that would hold the hash
    answered** (T10) — its own instance, or with no instance id every eligible
    instance. This backend logs and carries on when an instance's listing fails
    and swallows a total failure, so a hash with no entry is "could not ask",
    never "gone". A hash missing from a *cached* listing is listed again before
    it is called gone: the torrent may simply be newer than the listing.
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
        # endpoint is documented) — each involved instance's list, once.
        involved_ids = {inst_id for inst_id in wanted.values() if inst_id in eligible}
        if any(inst_id not in eligible for inst_id in wanted.values()):
            involved_ids = set(eligible)  # unknown instance — search everywhere
        hash_to_instance, listed_ok, listed_now = {}, set(), set()

        def _list(inst_id, fresh):
            try:
                listing, now = _instance_detail_listing(sess, base, inst_id, fresh=fresh)
            except Exception as e:
                log.warning('qui: torrent detail list failed for instance %s: %s', inst_id, e)
                listed_ok.discard(inst_id)
                return
            listed_ok.add(inst_id)
            if now:
                listed_now.add(inst_id)
            for th in wanted:
                if th not in details and th in listing:
                    details[th] = {**listing[th], 'tracker_health': 'unknown', 'tracker_msg': ''}
                    hash_to_instance[th] = inst_id

        for inst_id in involved_ids:
            _list(inst_id, fresh=False)
        if any(th not in details for th in wanted):
            for inst_id in [i for i in involved_ids if i in listed_ok and i not in listed_now]:
                _list(inst_id, fresh=True)
        for th, inst_id in wanted.items():
            if th not in details:
                asked = [inst_id] if inst_id in eligible else list(eligible)
                if all(i in listed_ok for i in asked):
                    details[th] = {'found': False}

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
# list_torrents / fetch_torrent_file_paths
# ---------------------------------------------------------------------------

def _eligible_instances(sess, base):
    resp = sess.get(f'{base}/api/instances', timeout=15)
    resp.raise_for_status()
    all_instances = resp.json()
    if not isinstance(all_instances, list):
        all_instances = _unwrap(all_instances)
    return [i for i in all_instances if _eligible(i)]


def list_torrents(cfg):
    """Light live listing of every torrent across all eligible qui instances.

    Returns ([rows], report); a row is {'hash', 'name', 'size', 'save_path',
    'content_path', 'progress', 'completion_on', 'tracker', 'instance_id',
    'instance_name'}, deduplicated by hash (qui per-instance endpoints can return
    all managed torrents regardless of which instance is queried). The paths are
    unremapped; the completion fields are `_norm_torrent`'s, the same ones
    `fetch_file_map` reads, so Cleanup's live re-verify applies the audit's own
    claim rule (`sources.torrent_claimed_paths`).

    An instance that fails to list is **recorded on the report**, which is what
    the `sources.list_torrents` wrapper refuses on. This used to log and carry
    on, handing back a short list indistinguishable from a complete one — the
    reverse polarity to the qbit backend, which raises. Neither backend was
    consistently fail-safe: each was careful in one function and careless in the
    other.
    """
    base    = (cfg.get('QUI_HOST') or '').rstrip('/')
    api_key = cfg.get('QUI_API_KEY', '')
    if not base:
        raise SourceConnectionError("QUI_HOST is not configured")
    socket.setdefaulttimeout(30)
    try:
        sess = _session(api_key)
        eligible = _eligible_instances(sess, base)
        if not eligible:
            raise SourceConnectionError("No eligible qui instances")
        report = new_source_report('qui')
        report['instances_total'] = len(eligible)
        rows = []
        seen = set()
        for inst in eligible:
            try:
                for t in _fetch_all_torrents(sess, base, inst['id']):
                    nt = _norm_torrent(t)
                    if not nt['hash'] or nt['hash'] in seen:
                        continue
                    seen.add(nt['hash'])
                    tracker_url = t.get('tracker') or ''
                    parts = tracker_url.split('/')
                    rows.append({
                        'hash':          nt['hash'],
                        'name':          nt['name'],
                        'size':          nt['size'],
                        'save_path':     nt['save_path'],
                        'content_path':  nt['content_path'],
                        'progress':      nt['progress'],
                        'completion_on': nt['completion_on'],
                        'tracker':       parts[2] if len(parts) > 2 else '',
                        'instance_id':   inst['id'],
                        'instance_name': inst.get('name', str(inst['id'])),
                    })
                report['instances_ok'] += 1
            except Exception as e:
                log.warning('qui: list_torrents failed for instance %s: %s', inst.get('name', '?'), e)
                report_instance_failure(report, inst.get('name', '?'), e)
        report['torrent_count'] = len(rows)
        return rows, report
    except SourceConnectionError:
        raise
    except requests.exceptions.ConnectionError as e:
        raise SourceConnectionError(f"qui connection error: {_connection_error_message(base)}") from e
    except Exception as e:
        raise SourceConnectionError(f"qui error: {e}") from e
    finally:
        socket.setdefaulttimeout(None)


def fetch_torrent_file_paths(cfg, items):
    """Absolute client-side file paths for specific torrents.

    items: [{'hash', 'instance_id'?, 'save_path'?, ...}]. Unknown instance ids
    are tried against every eligible instance.

    Returns {hash: [paths] | None}. **`None` means the listing could not be
    fetched; `[]` means the client answered that this torrent has no files.**
    This used to be `[]` for both, documented as deliberate ("failures yield
    empty lists, never exceptions") — and every consumer is a set-membership
    test, where an empty list reads as "shares nothing with anything" and
    quietly collapses whatever it was building. Still never raises per torrent:
    the failure is in the value now, not in control flow. A total failure
    (unreachable host, no eligible instances) leaves every requested hash at
    `None` rather than returning a map of empty lists.
    """
    base    = (cfg.get('QUI_HOST') or '').rstrip('/')
    api_key = cfg.get('QUI_API_KEY', '')
    hashes  = list(dict.fromkeys(i.get('hash') for i in items if i.get('hash')))
    if not base:
        return {h: None for h in hashes}
    result = {h: None for h in hashes}
    try:
        sess = _session(api_key)
        eligible_ids = [i['id'] for i in _eligible_instances(sess, base)]
        seen = set()
        for i in items:
            h = i.get('hash')
            if not h or h in seen:
                continue
            seen.add(h)
            sp = (i.get('save_path') or '').rstrip('/')
            inst = i.get('instance_id')
            try_ids = [inst] if inst in eligible_ids else eligible_ids
            for iid in try_ids:
                try:
                    resp = sess.get(
                        f'{base}/api/instances/{iid}/torrents/{h}/files', timeout=8)
                    resp.raise_for_status()
                    files = [_norm_file(f) for f in _unwrap(resp.json())]
                except Exception:
                    continue
                if files:
                    result[h] = [f"{sp}/{f['name']}" for f in files]
                    break
                # The instance answered and knows of no files for this hash.
                # Distinct from "no instance answered", which leaves None.
                result[h] = []
    except Exception as e:
        log.warning('qui: fetch_torrent_file_paths failed: %s', e)
    return result


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
        # Forgotten after the removal, not before: a verify batch could cache a
        # pre-removal listing while the bulk action is still in flight (T7/T10).
        _forget_detail_listings()


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
