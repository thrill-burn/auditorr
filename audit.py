import os
import math
import time
import hashlib
import logging
from datetime import datetime, timedelta

import sources
from exclusions import is_excluded
from media_server_exclusions import expand_exclusion_patterns

from db import (
    db_load_config, db_load_history, db_save_history,
    db_load_results, db_save_results, db_save_audit,
    db_save_upload_snapshot, db_get_upload_snapshots,
    db_save_change_log_entry,
    db_save_file_results, db_load_file_results,
)
from state import get_state, set_state, update_progress

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


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

def _is_excluded(rel_path, filename, patterns):
    """Backward-compatible wrapper used by older tests/imports."""
    return is_excluded(rel_path, rel_path, filename, patterns)


def _walk_directory(base_path, source_label, inode_map, qbit_file_map, scanned_so_far, total_files, exclusion_patterns=None, total_ref=None):
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
                file_key = (st.st_dev, st.st_ino)
                size     = st.st_size
                nlink    = st.st_nlink
                rel_path = os.path.relpath(full_path, base_path)
                inode_map.setdefault(file_key, {
                    'trackers': set(), 'status': 'Orphaned',
                    'torrent_paths': [], 'media_paths': [], 'hash': '',
                    'instance_id': None, 'instance_name': None,
                })
                if source_label == 'Torrent':
                    inode_map[file_key]['torrent_paths'].append(full_path)
                    qbit_info = qbit_file_map.get(full_path)
                    if qbit_info:
                        inode_map[file_key]['trackers'].update(qbit_info['trackers'])
                        inode_map[file_key]['hash']          = qbit_info.get('hash', '')
                        inode_map[file_key]['instance_id']   = qbit_info.get('instance_id')
                        inode_map[file_key]['instance_name'] = qbit_info.get('instance_name')
                        inode_map[file_key]['category']      = qbit_info.get('category', '')
                        cur = inode_map[file_key]['status']
                        if qbit_info['status'] == 'Seeding' or cur == 'Seeding':
                            inode_map[file_key]['status'] = 'Seeding'
                        elif cur == 'Orphaned':
                            inode_map[file_key]['status'] = qbit_info['status']
                else:
                    inode_map[file_key]['media_paths'].append(full_path)
                records.append({
                    "full_path": full_path, "rel_path": rel_path,
                    "size": size, "inode": inode, "file_key": file_key,
                    "nlink": nlink, "source": source_label,
                    "excluded": is_excluded(full_path, rel_path, filename, exclusion_patterns),
                })
            except Exception as e:
                log.warning(f"Could not stat {full_path}: {e}")
                stat_errors += 1
            scanned += 1
            if total_ref is not None:
                total_ref[0] += 1
                if total_ref[0] % 500 == 0:
                    set_state(total_files=total_ref[0])
            update_progress(scanned, total_ref[0] if total_ref is not None else total_files)
    return records, scanned, stat_errors


def _build_duplicate_map(all_records):
    """O(n) duplicate detection: group by size, then file identity, then hash representatives only."""
    size_groups = {}
    for f in all_records:
        if f['size'] > 0:
            size_groups.setdefault(f['size'], []).append(f)

    duplicate_map = {}
    for size, items in size_groups.items():
        key_to_rep = {}
        for item in items:
            file_key = item.get('file_key', item['inode'])
            if file_key not in key_to_rep:
                key_to_rep[file_key] = item
        if len(key_to_rep) <= 1:
            continue
        hash_to_keys = {}
        for file_key, rep in key_to_rep.items():
            fh = get_fast_hash(rep['full_path'], size)
            if fh:
                hash_to_keys.setdefault(fh, []).append(file_key)
        for fh, file_keys in hash_to_keys.items():
            if len(file_keys) <= 1:
                continue
            for file_key in file_keys:
                others = [key_to_rep[o]['full_path'] for o in file_keys if o != file_key]
                duplicate_map.setdefault(file_key, []).extend(others)
    return duplicate_map


def _assemble_records(torrent_records, media_records, inode_map, duplicate_map):
    torrent_files_data = []
    seen_torrent_keys = set()
    for item in torrent_records:
        inode = item['inode']
        file_key = item.get('file_key', inode)
        # Cross-seeded files share an inode across multiple torrent directories.
        # Only emit one entry per unique inode so the file browser and health
        # metrics don't count the same physical file N times (once per cross-seed).
        if file_key in seen_torrent_keys:
            continue
        seen_torrent_keys.add(file_key)
        file_id = f"{file_key[0]}:{file_key[1]}" if isinstance(file_key, tuple) else str(file_key)
        info  = inode_map[file_key]
        torrent_files_data.append({
            "path": item['rel_path'], "size": item['size'], "inode": inode,
            "file_id": file_id,
            "status": info['status'],
            "imported": item['nlink'] > 1 or len(info['media_paths']) > 0,
            "trackers": list(info['trackers']) or ["None"],
            "linked_paths": info['media_paths'],
            "duplicate_paths": duplicate_map.get(file_key, []),
            "excluded": item.get('excluded', False),
            "hash": info.get('hash', ''),
            "category": info.get('category', ''),
            "instance_id":   info.get('instance_id'),
            "instance_name": info.get('instance_name'),
        })
    media_files_data = []
    seen_media_keys = set()
    for item in media_records:
        inode = item['inode']
        file_key = item.get('file_key', inode)
        if file_key in seen_media_keys:
            continue
        seen_media_keys.add(file_key)
        file_id = f"{file_key[0]}:{file_key[1]}" if isinstance(file_key, tuple) else str(file_key)
        info  = inode_map[file_key]
        media_files_data.append({
            "path": item['rel_path'], "size": item['size'], "inode": inode,
            "file_id": file_id,
            "status": info['status'], "imported": True,
            "trackers": list(info['trackers']) or ["None"],
            "linked_paths": info['torrent_paths'],
            "duplicate_paths": duplicate_map.get(file_key, []),
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
    seen_files = set()
    dup_size = dup_count = 0
    for f in scoring_media + scoring_torrents:
        file_id = f.get('file_id', f.get('inode'))
        if f.get('duplicate_paths') and file_id not in seen_files:
            seen_files.add(file_id); dup_size += f['size']; dup_count += 1
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

def compute_upload_stats(days=30, from_date=None, to_date=None):
    """Compute per-tracker upload deltas and yield from stored snapshots.

    Returns None if fewer than 2 snapshots exist (not enough data for deltas).
    Pass from_date/to_date (ISO date strings) to query a specific range instead of days.
    """
    rows = db_get_upload_snapshots(since_days=days, from_date=from_date, to_date=to_date)
    if len(rows) < 2:
        return None

    # Daily buckets: {date_str: {host: delta_bytes}}
    daily_by_tracker = {}

    for i in range(1, len(rows)):
        prev_row = rows[i - 1]
        curr_row = rows[i]
        prev_snap = prev_row['snapshot']
        curr_snap = curr_row['snapshot']

        # Skip if the number of contributing instances changed — a step-change
        # in instance count means the cumulative totals shifted baseline (new
        # instance history added, or a partial snapshot when one was unreachable).
        # Treat missing _instance_count (old snapshots) as 1 for backward compat.
        if (prev_snap.get('_instance_count') or 1) != (curr_snap.get('_instance_count') or 1):
            continue

        try:
            t_prev = datetime.fromisoformat(prev_row['taken_at'])
            t_curr = datetime.fromisoformat(curr_row['taken_at'])
        except ValueError:
            continue
        date_str = t_curr.strftime('%Y-%m-%d')
        bucket = daily_by_tracker.setdefault(date_str, {})

        for host, curr_data in curr_snap.items():
            if host == 'Unknown' or host.startswith('_'):
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

    # Per-day point-in-time stats: seeding_size, orphaned_size, not_imported_size
    # Use the last snapshot of each day (all rows, not just delta pairs)
    daily_point_by_tracker = {}
    for row in rows:
        try:
            t = datetime.fromisoformat(row['taken_at'])
        except ValueError:
            continue
        date_str = t.strftime('%Y-%m-%d')
        day_stats = {}
        for host, snap_data in row['snapshot'].items():
            if host == 'Unknown' or host.startswith('_'):
                continue
            day_stats[host] = {
                'seeding_size':      snap_data.get('seeding_size', 0),
                'orphaned_size':     snap_data.get('orphaned_size', 0),
                'not_imported_size': snap_data.get('not_imported_size', 0),
            }
        daily_point_by_tracker[date_str] = day_stats

    daily_tracker_stats = [
        {'date': date_str, 'by_tracker': stats}
        for date_str, stats in sorted(daily_point_by_tracker.items())
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
        if host == 'Unknown' or host.startswith('_'):
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
        "period_days":         period_days,
        "library_yield":       round(library_yield, 4) if library_yield is not None else None,
        "total_uploaded":      total_uploaded,
        "total_seeding_size":  total_seeding_size,
        "daily_uploads":       daily_uploads,
        "daily_tracker_stats": daily_tracker_stats,
        "tracker_yields":      tracker_yields,
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
# Pre-computed summary stats (avoid shipping raw file lists on /api/results)
# ---------------------------------------------------------------------------

def _compute_cross_seed_stats(media_files):
    if not media_files:
        return None
    buckets      = {}
    weighted_sum = 0
    total_size   = 0
    tracker_map  = {}
    for f in media_files:
        real_trackers = [t for t in (f.get('trackers') or []) if t != 'None']
        n = len(real_trackers)
        buckets[n]    = buckets.get(n, 0) + f['size']
        weighted_sum += f['size'] * n
        total_size   += f['size']
        for t in real_trackers:
            if t not in tracker_map:
                tracker_map[t] = {'name': t, 'size': 0, 'count': 0}
            tracker_map[t]['size']  += f['size']
            tracker_map[t]['count'] += 1
    multiplier   = weighted_sum / total_size if total_size > 0 else 0
    max_count    = max(buckets.keys()) if buckets else 0
    segments     = [{'count': i, 'size': buckets.get(i, 0)} for i in range(max_count + 1)]
    tracker_stats = sorted(tracker_map.values(), key=lambda x: -x['size'])
    return {
        'multiplier':    multiplier,
        'segments':      segments,
        'total_size':    total_size,
        'tracker_stats': tracker_stats,
    }


def _compute_tracker_file_stats(torrent_files):
    stats = {}
    for f in torrent_files:
        if f.get('excluded'):
            continue
        for t in (f.get('trackers') or []):
            if t == 'None':
                continue
            if t not in stats:
                stats[t] = {
                    'seeding_count': 0, 'seeding_size': 0,
                    'orphaned_count': 0, 'orphaned_size': 0,
                    'not_imported_count': 0, 'not_imported_size': 0,
                }
            s = stats[t]
            if f['status'] == 'Seeding':
                s['seeding_count'] += 1
                s['seeding_size']  += f['size']
            elif f['status'] == 'Orphaned':
                s['orphaned_count'] += 1
                s['orphaned_size']  += f['size']
            if not f.get('imported') and f['status'] != 'Orphaned':
                s['not_imported_count'] += 1
                s['not_imported_size']  += f['size']
    return stats


def _is_not_imported_torrent(f):
    return (
        not f.get('excluded')
        and not f.get('imported')
        and f.get('status') != 'Orphaned'
    )


def _not_imported_paths(torrent_files):
    return [f['path'] for f in torrent_files if _is_not_imported_torrent(f)]


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
    scan_start = time.time()
    set_state(is_scanning=True, progress=0, scanned_files=0, total_files=0,
              status_message="Connecting to torrent source...", last_scan_status="running", phase="connecting")
    try:
        qbit_file_map, trackers, tracker_snapshot = sources.fetch_file_map(cfg)
        total_ref = [0]
        set_state(total_files=0, status_message="Scanning torrent directory...", phase="disk")
        inode_map          = {}
        exclusion_patterns = expand_exclusion_patterns(cfg)
        torrent_records, scanned, torrent_errors = _walk_directory(
            cfg.get('LOCAL_PATH',''), 'Torrent', inode_map, qbit_file_map, 0, 0,
            exclusion_patterns=exclusion_patterns, total_ref=total_ref)
        set_state(status_message="Scanning media directory...", phase="disk")
        media_records, _, media_errors = _walk_directory(
            cfg.get('MEDIA_PATH',''), 'Media', inode_map, qbit_file_map, scanned, 0,
            exclusion_patterns=exclusion_patterns, total_ref=total_ref)
        stat_errors = torrent_errors + media_errors
        set_state(status_message="Detecting duplicates...", phase="post")
        duplicate_map = _build_duplicate_map(torrent_records + media_records)
        torrent_files_data, media_files_data = _assemble_records(
            torrent_records, media_records, inode_map, duplicate_map)
        del torrent_records, media_records, inode_map, duplicate_map
        set_state(status_message="Computing health metrics...", phase="post")
        dashboard_stats    = process_health_metrics(media_files_data, torrent_files_data, cfg)
        cross_seed_stats   = _compute_cross_seed_stats(media_files_data)
        tracker_file_stats = _compute_tracker_file_stats(torrent_files_data)
        not_imported_paths = _not_imported_paths(torrent_files_data)
        if cross_seed_stats:
            dashboard_stats['cross_seed_stats'] = cross_seed_stats

        result = {
            "trackers":           trackers,
            "status":             "ok",
            "dashboard":          dashboard_stats,
            "tracker_file_stats": tracker_file_stats,
            "not_imported_paths": not_imported_paths,
        }
        # Save upload snapshot — only on successful audits
        # Augment with per-tracker file health stats so daily seeding/orphaned trends
        # can be plotted from the same snapshot rows.
        try:
            aug = {k: (dict(v) if isinstance(v, dict) else v) for k, v in tracker_snapshot.items()}
            for tracker, fstats in tracker_file_stats.items():
                if tracker not in aug:
                    aug[tracker] = {'uploaded': 0, 'seeding_size': 0}
                aug[tracker]['seeding_size']       = fstats['seeding_size']
                aug[tracker]['seeding_count']      = fstats['seeding_count']
                aug[tracker]['orphaned_size']      = fstats['orphaned_size']
                aug[tracker]['orphaned_count']     = fstats['orphaned_count']
                aug[tracker]['not_imported_size']  = fstats['not_imported_size']
                aug[tracker]['not_imported_count'] = fstats['not_imported_count']
            db_save_upload_snapshot(aug, source=cfg.get('TORRENT_SOURCE', 'qbit'))
        except Exception as e:
            log.warning(f"Could not save upload snapshot: {e}")

        # Compute yield summary for results
        try:
            yield_summary = _build_yield_summary()
        except Exception as e:
            log.warning(f"Could not compute yield summary: {e}")
            yield_summary = None
        result["yield_summary"] = yield_summary

        # Compute diff from old file_results vs current in-memory data — BEFORE overwriting.
        # This avoids storing file lists in audit_snapshots (300MB+ per row for large libraries)
        # and avoids deserializing two fat snapshots on every audit and every /api/changes call.
        ran_at = datetime.now().isoformat()
        try:
            old_media    = db_load_file_results('media')
            old_torrents = db_load_file_results('torrents')
            if old_media or old_torrents:
                prev_snap = {
                    "media_files": old_media, "torrent_files": old_torrents,
                    "dashboard":   db_load_results().get('dashboard'),
                }
                curr_snap = {
                    "media_files": media_files_data, "torrent_files": torrent_files_data,
                    "dashboard":   dashboard_stats,
                }
                diff = compute_diff(prev_snap, curr_snap)
                del old_media, old_torrents, prev_snap, curr_snap
                if diff:
                    db_save_change_log_entry(
                        ran_at=ran_at,
                        health_score=dashboard_stats['score'],
                        trigger=trigger,
                        source=cfg.get('TORRENT_SOURCE', 'qbit'),
                        diff=diff,
                    )
        except Exception as e:
            log.warning(f"Could not save change log entry: {e}")
        # Persist file lists separately so /api/results only loads summary data
        db_save_file_results('media',    media_files_data)
        db_save_file_results('torrents', torrent_files_data)
        db_save_results(result)
        # Snapshot stores only dashboard stats — no file lists (eliminates 300MB+ per row)
        snapshot = {"dashboard": dashboard_stats}
        db_save_audit(trigger, dashboard_stats['score'], 'ok', None, snapshot,
                      source=cfg.get('TORRENT_SOURCE', 'qbit'),
                      duration_seconds=round(time.time() - scan_start, 1),
                      ran_at=ran_at)
        log.info("Audit complete.")
        if stat_errors:
            log.warning(f"Audit complete with {stat_errors} unreadable file(s) — check earlier warnings.")
        status_msg = f"Audit complete. {stat_errors} file(s) could not be read — check logs." if stat_errors else "Audit complete."
        set_state(status_message=status_msg, last_scan_status="ok")
    except sources.SourceConnectionError as e:
        msg = str(e)
        log.error(msg); _save_error_status(msg)
        db_save_audit(trigger, None, 'error', msg, {}, source=cfg.get('TORRENT_SOURCE', 'qbit'), duration_seconds=round(time.time() - scan_start, 1))
        set_state(status_message=msg, last_scan_status="error")
    except Exception as e:
        msg = f"Audit error: {e}"
        log.exception("Unexpected error during audit")
        _save_error_status(msg)
        db_save_audit(trigger, None, 'error', msg, {}, source=cfg.get('TORRENT_SOURCE', 'qbit'), duration_seconds=round(time.time() - scan_start, 1))
        set_state(status_message=msg, last_scan_status="error")
    finally:
        set_state(progress=100, is_scanning=False,
                  last_audit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  trigger="idle", phase="idle")
