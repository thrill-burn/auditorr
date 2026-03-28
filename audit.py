import os
import math
import socket
import hashlib
import logging
import fnmatch
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import qbittorrentapi

from db import (
    db_load_config, db_load_history, db_save_history,
    db_load_results, db_save_results, db_save_audit,
    db_save_upload_snapshot, db_get_upload_snapshots,
)
from state import get_state, set_state, update_progress

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def count_files(directory):
    count = 0
    if os.path.exists(directory):
        for _, _, files in os.walk(directory):
            count += len(files)
    return count


def get_fast_hash(filepath, size, chunk_size=65536):
    try:
        hasher = hashlib.md5()
        with open(filepath, 'rb') as f:
            if size <= chunk_size * 2:
                hasher.update(f.read())
            else:
                hasher.update(f.read(chunk_size))
                f.seek(-chunk_size, 2)
                hasher.update(f.read(chunk_size))
        return hasher.hexdigest()
    except Exception as e:
        log.warning(f"Hash failed for {filepath}: {e}")
        return None


# ---------------------------------------------------------------------------
# Audit stages
# ---------------------------------------------------------------------------

def _fetch_qbit_file_map(cfg):
    socket.setdefaulttimeout(10)
    try:
        return _fetch_qbit_file_map_inner(cfg)
    finally:
        socket.setdefaulttimeout(None)


def _fetch_qbit_file_map_inner(cfg):
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
    # Each worker creates its own Client to avoid sharing a requests.Session
    # across threads (Session is not documented as thread-safe).
    _host = cfg.get('QB_HOST')
    _user = cfg.get('QB_USER')
    _pass = cfg.get('QB_PASS')

    # Fetch trackers and file lists in a single parallel pass — one login and
    # two API calls per worker instead of two separate executor pools.
    def _fetch_torrent_data(torrent):
        try:
            thread_qbt = qbittorrentapi.Client(host=_host, username=_user, password=_pass)
            thread_qbt.auth_log_in()
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
    qbit_file_map        = {}
    trackers_set         = set()
    tracker_upload       = {}  # {host: cumulative uploaded bytes}
    tracker_seeding_size = {}  # {host: total seeding bytes}
    remote_path          = cfg.get('REMOTE_PATH', '')
    local_path           = cfg.get('LOCAL_PATH', '')

    for torrent in torrents:
        hosts     = tracker_map.get(torrent.hash, ['Unknown'])
        for h in hosts:
            trackers_set.add(h)
            # Per-tracker upload totals — each tracker independently credits the upload
            tracker_upload[h] = tracker_upload.get(h, 0) + torrent.uploaded
            # Seeding size — only count torrents actively seeding
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
            entry = qbit_file_map.setdefault(full_path, {"status": status, "trackers": set(), "hash": torrent.hash})
            entry["trackers"].update(hosts)
            if status == 'Seeding' or entry["status"] == 'Seeding':
                entry["status"] = 'Seeding'
            elif entry["status"] == 'Paused':
                entry["status"] = status

    # Combine upload and seeding_size into snapshot schema
    all_hosts        = set(tracker_upload) | set(tracker_seeding_size)
    tracker_snapshot = {
        host: {
            "uploaded":     tracker_upload.get(host, 0),
            "seeding_size": tracker_seeding_size.get(host, 0),
        }
        for host in all_hosts
    }

    return qbit_file_map, sorted(trackers_set), tracker_snapshot


def _is_excluded(rel_path, filename, patterns):
    """Return True if the file matches any exclusion glob pattern."""
    if not patterns:
        return False
    norm  = rel_path.replace('\\', '/')
    parts = norm.split('/')
    for pat in patterns:
        if fnmatch.fnmatch(filename, pat):
            return True
        if fnmatch.fnmatch(norm, pat):
            return True
        # Check each parent directory component
        for part in parts[:-1]:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def _walk_directory(base_path, source_label, inode_map, qbit_file_map, scanned_so_far, total_files, exclusion_patterns=None):
    records     = []
    scanned     = scanned_so_far
    stat_errors = 0
    if not os.path.exists(base_path):
        log.warning(f"Path does not exist, skipping: {base_path}")
        return records, scanned, stat_errors
    for root, _, files in os.walk(base_path):
        for filename in files:
            full_path = os.path.join(root, filename)
            try:
                st       = os.stat(full_path)
                inode    = st.st_ino
                size     = st.st_size
                nlink    = st.st_nlink
                rel_path = os.path.relpath(full_path, base_path)
                inode_map.setdefault(inode, {
                    'trackers': set(), 'status': 'Orphaned',
                    'torrent_paths': [], 'media_paths': [], 'hash': '',
                })
                if source_label == 'Torrent':
                    inode_map[inode]['torrent_paths'].append(full_path)
                    qbit_info = qbit_file_map.get(full_path)
                    if qbit_info:
                        inode_map[inode]['trackers'].update(qbit_info['trackers'])
                        inode_map[inode]['hash'] = qbit_info.get('hash', '')
                        cur = inode_map[inode]['status']
                        if qbit_info['status'] == 'Seeding' or cur == 'Seeding':
                            inode_map[inode]['status'] = 'Seeding'
                        elif cur == 'Orphaned':
                            inode_map[inode]['status'] = qbit_info['status']
                else:
                    inode_map[inode]['media_paths'].append(full_path)
                records.append({
                    "full_path": full_path, "rel_path": rel_path,
                    "size": size, "inode": inode, "nlink": nlink, "source": source_label,
                    "excluded": _is_excluded(rel_path, filename, exclusion_patterns),
                })
            except Exception as e:
                log.warning(f"Could not stat {full_path}: {e}")
                stat_errors += 1
            scanned += 1
            update_progress(scanned, total_files)
    return records, scanned, stat_errors


def _build_duplicate_map(all_records):
    """O(n) duplicate detection: group by size, then inode, then hash representatives only."""
    size_groups = {}
    for f in all_records:
        if f['size'] > 0:
            size_groups.setdefault(f['size'], []).append(f)

    duplicate_map = {}
    for size, items in size_groups.items():
        inode_to_rep = {}
        for item in items:
            if item['inode'] not in inode_to_rep:
                inode_to_rep[item['inode']] = item
        if len(inode_to_rep) <= 1:
            continue
        hash_to_inodes = {}
        for inode, rep in inode_to_rep.items():
            fh = get_fast_hash(rep['full_path'], size)
            if fh:
                hash_to_inodes.setdefault(fh, []).append(inode)
        for fh, inodes in hash_to_inodes.items():
            if len(inodes) <= 1:
                continue
            for inode in inodes:
                others = [inode_to_rep[o]['full_path'] for o in inodes if o != inode]
                duplicate_map.setdefault(inode, []).extend(others)
    return duplicate_map


def _assemble_records(torrent_records, media_records, inode_map, duplicate_map):
    torrent_files_data = []
    for item in torrent_records:
        inode = item['inode']
        info  = inode_map[inode]
        torrent_files_data.append({
            "path": item['rel_path'], "size": item['size'], "inode": inode,
            "status": info['status'],
            "imported": item['nlink'] > 1 or len(info['media_paths']) > 0,
            "trackers": list(info['trackers']) or ["None"],
            "linked_paths": info['media_paths'],
            "duplicate_paths": duplicate_map.get(inode, []),
            "excluded": item.get('excluded', False),
            "hash": info.get('hash', ''),
        })
    media_files_data = []
    for item in media_records:
        inode = item['inode']
        info  = inode_map[inode]
        media_files_data.append({
            "path": item['rel_path'], "size": item['size'], "inode": inode,
            "status": info['status'], "imported": True,
            "trackers": list(info['trackers']) or ["None"],
            "linked_paths": info['torrent_paths'],
            "duplicate_paths": duplicate_map.get(inode, []),
            "excluded": item.get('excluded', False),
        })
    return torrent_files_data, media_files_data


# ---------------------------------------------------------------------------
# Health metrics
# ---------------------------------------------------------------------------

def process_health_metrics(media_files, torrent_files, cfg, update_history=True):
    history = db_load_history()
    now     = datetime.now()
    or_ratio  = float(cfg.get('OR_RATIO',  0.01))
    ni_ratio  = float(cfg.get('NI_RATIO',  0.01))
    dup_ratio = float(cfg.get('DUP_RATIO', 0.01))
    # Exclude files marked as excluded from all scoring
    scoring_media    = [f for f in media_files    if not f.get('excluded')]
    scoring_torrents = [f for f in torrent_files  if not f.get('excluded')]
    total_media_size      = sum(f['size'] for f in scoring_media)
    hardlinked_media_size = sum(f['size'] for f in scoring_media if f.get('linked_paths'))
    total_torrents_size   = sum(f['size'] for f in scoring_torrents)
    orphaned_torrent_size = sum(f['size'] for f in scoring_torrents if f['status'] == 'Orphaned')
    not_imported_size     = sum(f['size'] for f in scoring_torrents
                                if not f['imported'] and f['status'] != 'Orphaned')
    seen_inodes = set()
    dup_size = dup_count = 0
    for f in scoring_media + scoring_torrents:
        if f.get('duplicate_paths') and f.get('inode') not in seen_inodes:
            seen_inodes.add(f['inode']); dup_size += f['size']; dup_count += 1
    hl_ratio = (hardlinked_media_size / total_media_size) if total_media_size > 0 else 1.0
    hl_score = hl_ratio * 70
    or_limit   = total_torrents_size * or_ratio
    or_penalty = (orphaned_torrent_size / or_limit) * 10 if or_limit > 0 else (10 if orphaned_torrent_size > 0 else 0)
    or_score   = max(0, 10 - or_penalty)
    ni_limit   = total_torrents_size * ni_ratio
    ni_penalty = (not_imported_size / ni_limit) * 10 if ni_limit > 0 else (10 if not_imported_size > 0 else 0)
    ni_score   = max(0, 10 - ni_penalty)
    dup_limit   = total_torrents_size * dup_ratio
    dup_penalty = (dup_size / dup_limit) * 10 if dup_limit > 0 else (10 if dup_size > 0 else 0)
    dup_score   = max(0, 10 - dup_penalty)
    final_score = round(max(0, min(100, hl_score + or_score + ni_score + dup_score)), 1)
    if   final_score >= 90: status_text = "Excellent"
    elif final_score >= 75: status_text = "Good"
    elif final_score >= 50: status_text = "Fair"
    else:                   status_text = "Poor"
    current_stat = {
        "timestamp": now.isoformat(), "health_score": final_score,
        "details": {
            "total_media_size": total_media_size, "hardlinked_media_size": hardlinked_media_size,
            "total_torrents_size": total_torrents_size, "orphaned_torrent_size": orphaned_torrent_size,
            "not_imported_size": not_imported_size, "duplicate_size": dup_size,
            "orphaned_torrent_count": sum(1 for f in scoring_torrents if f['status'] == 'Orphaned'),
            "not_imported_count": sum(1 for f in scoring_torrents if not f['imported'] and f['status'] != 'Orphaned'),
            "duplicate_count": dup_count, "or_limit": or_limit, "ni_limit": ni_limit,
            "dup_limit": dup_limit, "hl_score": round(hl_score,1), "or_score": round(or_score,1),
            "ni_score": round(ni_score,1), "dup_score": round(dup_score,1),
        }
    }
    if update_history:
        history['hourly_stats'].append(current_stat)
        cutoff   = now - timedelta(hours=48)
        to_daily = [s for s in history['hourly_stats'] if datetime.fromisoformat(s['timestamp']) < cutoff]
        history['hourly_stats'] = [s for s in history['hourly_stats'] if datetime.fromisoformat(s['timestamp']) >= cutoff]
        daily_groups = {}
        for s in to_daily:
            daily_groups.setdefault(s['timestamp'][:10], []).append(s['health_score'])
        for day, scores in daily_groups.items():
            if not any(d['date'] == day for d in history['daily_stats']):
                history['daily_stats'].append({"date": day, "avg_score": round(sum(scores)/len(scores),1),
                                               "min_score": min(scores), "max_score": max(scores)})
        history['daily_stats'] = history['daily_stats'][-90:]
        db_save_history(history)
    combined_chart = list(history['daily_stats'])
    recent_groups  = {}
    for s in history['hourly_stats']:
        day_str = s['timestamp'][:10]
        if not any(d['date'] == day_str for d in history['daily_stats']):
            recent_groups.setdefault(day_str, []).append(s['health_score'])
    for day in sorted(recent_groups):
        scores = recent_groups[day]
        combined_chart.append({"date": day, "avg_score": round(sum(scores)/len(scores),1),
                                "min_score": min(scores), "max_score": max(scores)})
    trend = None
    if len(combined_chart) >= 2:
        trend = round(combined_chart[-1]['avg_score'] - combined_chart[-2]['avg_score'], 1)
    return {"score": final_score, "status": status_text, "trend": trend,
            "current": current_stat, "history_chart": combined_chart}


# ---------------------------------------------------------------------------
# Upload / yield stats
# ---------------------------------------------------------------------------

def compute_upload_stats(days=30):
    """Compute per-tracker upload deltas and yield from stored snapshots.

    Returns None if fewer than 2 snapshots exist (not enough data for deltas).
    """
    rows = db_get_upload_snapshots(since_days=days)
    if len(rows) < 2:
        return None

    # Daily buckets: {date_str: {host: delta_bytes}}
    daily_by_tracker = {}

    for i in range(1, len(rows)):
        prev_row = rows[i - 1]
        curr_row = rows[i]
        prev_snap = prev_row['snapshot']
        curr_snap = curr_row['snapshot']
        try:
            t_prev = datetime.fromisoformat(prev_row['taken_at'])
            t_curr = datetime.fromisoformat(curr_row['taken_at'])
        except ValueError:
            continue
        date_str = t_curr.strftime('%Y-%m-%d')
        bucket = daily_by_tracker.setdefault(date_str, {})

        for host, curr_data in curr_snap.items():
            if host == 'Unknown':
                continue
            prev_data = prev_snap.get(host)
            if prev_data is None:
                continue
            delta = curr_data['uploaded'] - prev_data['uploaded']
            # Counter reset (qBit restart) — skip rather than go negative
            if delta < 0:
                continue
            bucket[host] = bucket.get(host, 0) + delta

    # Build daily_uploads list in date order
    daily_uploads = [
        {
            "date":       date_str,
            "total":      sum(v for v in by_tracker.values()),
            "by_tracker": dict(by_tracker),
        }
        for date_str, by_tracker in sorted(daily_by_tracker.items())
    ]

    # Total uploaded over the period
    total_uploaded = sum(d['total'] for d in daily_uploads)

    # Use latest snapshot for seeding sizes
    latest_snap = rows[-1]['snapshot']

    # Earliest and latest timestamps for actual period coverage
    try:
        t_first = datetime.fromisoformat(rows[0]['taken_at'])
        t_last  = datetime.fromisoformat(rows[-1]['taken_at'])
        period_days = max(1, math.ceil((t_last - t_first).total_seconds() / 86400)) if t_last > t_first else 1
    except ValueError:
        period_days = days if days > 0 else 1

    # Per-tracker totals across the full period
    tracker_totals = {}
    for d in daily_uploads:
        for host, delta in d['by_tracker'].items():
            tracker_totals[host] = tracker_totals.get(host, 0) + delta

    # Build tracker_yields list
    tracker_yields = []
    total_seeding_size = 0
    for host, snap_data in latest_snap.items():
        if host == 'Unknown':
            continue
        seeding_size = snap_data.get('seeding_size', 0)
        total_seeding_size += seeding_size
        uploaded = tracker_totals.get(host, 0)
        yld = (uploaded / seeding_size) if seeding_size > 0 else None
        tracker_yields.append({
            "tracker":      host,
            "uploaded":     uploaded,
            "seeding_size": seeding_size,
            "yield":        round(yld, 4) if yld is not None else None,
        })
    tracker_yields.sort(key=lambda x: (x['yield'] is None, -(x['yield'] or 0)))

    library_yield = (total_uploaded / total_seeding_size) if total_seeding_size > 0 else None

    return {
        "period_days":       period_days,
        "library_yield":     round(library_yield, 4) if library_yield is not None else None,
        "total_uploaded":    total_uploaded,
        "total_seeding_size": total_seeding_size,
        "daily_uploads":     daily_uploads,
        "tracker_yields":    tracker_yields,
    }


def _build_yield_summary():
    """Lightweight yield summary for embedding in /api/results."""
    stats = compute_upload_stats(30)
    if stats is None:
        return None
    top = next((t for t in stats['tracker_yields'] if t['yield'] is not None), None)
    return {
        "library_yield_30d":  stats['library_yield'],
        "total_uploaded_30d": stats['total_uploaded'],
        "top_tracker": {"name": top['tracker'], "yield": top['yield']} if top else None,
    }


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------

def compute_diff(prev_snap, curr_snap):
    if not prev_snap or not curr_snap:
        return None

    def fset(snap, key):
        return {f['path']: f for f in snap.get(key, [])}

    changes = {
        'newly_orphaned':      [],
        'newly_imported':      [],
        'new_duplicates':      [],
        'resolved_duplicates': [],
        'new_files':           [],
        'removed_files':       [],
        'score_delta':         None,
    }

    ps = prev_snap.get('dashboard', {}).get('score')
    cs = curr_snap.get('dashboard', {}).get('score')
    if ps is not None and cs is not None:
        changes['score_delta'] = round(cs - ps, 1)

    for tab, key in [('media', 'media_files'), ('torrents', 'torrent_files')]:
        prev = fset(prev_snap, key)
        curr = fset(curr_snap, key)

        for path, f in curr.items():
            if path not in prev:
                changes['new_files'].append({'path': path, 'size': f['size'], 'tab': tab})
            else:
                pf = prev[path]
                if pf.get('status') != 'Orphaned' and f.get('status') == 'Orphaned':
                    changes['newly_orphaned'].append({'path': path, 'size': f['size'], 'tab': tab})
                if tab == 'torrents' and not pf.get('imported') and f.get('imported'):
                    changes['newly_imported'].append({'path': path, 'size': f['size'], 'tab': tab})
                if not pf.get('duplicate_paths') and f.get('duplicate_paths'):
                    changes['new_duplicates'].append({'path': path, 'size': f['size'], 'tab': tab})
                if pf.get('duplicate_paths') and not f.get('duplicate_paths'):
                    changes['resolved_duplicates'].append({'path': path, 'size': f['size'], 'tab': tab})

        for path in prev:
            if path not in curr:
                changes['removed_files'].append({'path': path, 'tab': tab})

    for k in changes:
        if isinstance(changes[k], list):
            changes[k] = changes[k][:50]

    has_changes = any(isinstance(v, list) and v for v in changes.values())
    return changes if has_changes else None


# ---------------------------------------------------------------------------
# Main audit process
# ---------------------------------------------------------------------------

def _save_error_status(message):
    curr = db_load_results()
    curr["status"] = message
    db_save_results(curr)


def run_audit_process(trigger=None):
    cfg = db_load_config()
    # Accept trigger as parameter so callers can pass it explicitly,
    # avoiding a race between set_state(trigger=...) and reading it back
    if trigger is None:
        trigger = get_state().get('trigger', 'manual')
    set_state(is_scanning=True, progress=0, scanned_files=0, total_files=0,
              status_message="Connecting to qBittorrent...", last_scan_status="running")
    try:
        qbit_file_map, trackers, tracker_snapshot = _fetch_qbit_file_map(cfg)
        set_state(status_message="Counting files...")
        total = count_files(cfg.get('MEDIA_PATH','')) + count_files(cfg.get('LOCAL_PATH',''))
        set_state(total_files=total, status_message="Scanning torrent directory...")
        inode_map          = {}
        exclusion_patterns = cfg.get('EXCLUSION_PATTERNS', [])
        torrent_records, scanned, torrent_errors = _walk_directory(
            cfg.get('LOCAL_PATH',''), 'Torrent', inode_map, qbit_file_map, 0, total,
            exclusion_patterns=exclusion_patterns)
        set_state(status_message="Scanning media directory...")
        media_records, _, media_errors = _walk_directory(
            cfg.get('MEDIA_PATH',''), 'Media', inode_map, qbit_file_map, scanned, total,
            exclusion_patterns=exclusion_patterns)
        stat_errors = torrent_errors + media_errors
        set_state(status_message="Detecting duplicates...")
        duplicate_map = _build_duplicate_map(torrent_records + media_records)
        torrent_files_data, media_files_data = _assemble_records(
            torrent_records, media_records, inode_map, duplicate_map)
        set_state(status_message="Computing health metrics...")
        dashboard_stats = process_health_metrics(media_files_data, torrent_files_data, cfg)
        result = {
            "media_files": media_files_data, "torrent_files": torrent_files_data,
            "trackers": trackers, "status": "ok", "dashboard": dashboard_stats,
        }
        # Save upload snapshot — only on successful audits
        try:
            db_save_upload_snapshot(tracker_snapshot)
        except Exception as e:
            log.warning(f"Could not save upload snapshot: {e}")

        # Compute yield summary for results
        try:
            yield_summary = _build_yield_summary()
        except Exception as e:
            log.warning(f"Could not compute yield summary: {e}")
            yield_summary = None
        result["yield_summary"] = yield_summary

        db_save_results(result)
        snapshot = {"media_files": media_files_data, "torrent_files": torrent_files_data,
                    "dashboard": dashboard_stats}
        db_save_audit(trigger, dashboard_stats['score'], 'ok', None, snapshot)
        log.info("Audit complete.")
        if stat_errors:
            log.warning(f"Audit complete with {stat_errors} unreadable file(s) — check earlier warnings.")
        status_msg = f"Audit complete. {stat_errors} file(s) could not be read — check logs." if stat_errors else "Audit complete."
        set_state(status_message=status_msg, last_scan_status="ok")
    except (qbittorrentapi.LoginFailed, qbittorrentapi.APIConnectionError) as e:
        msg = f"qBittorrent error: {e}"
        log.error(msg); _save_error_status(msg)
        db_save_audit(trigger, None, 'error', msg, {})
        set_state(status_message=msg, last_scan_status="error")
    except Exception as e:
        msg = f"Audit error: {e}"
        log.exception("Unexpected error during audit")
        _save_error_status(msg)
        db_save_audit(trigger, None, 'error', msg, {})
        set_state(status_message=msg, last_scan_status="error")
    finally:
        set_state(progress=100, is_scanning=False,
                  last_audit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  trigger="idle")
