import os
import gc
import math
import time
import hashlib
import logging
import threading
from datetime import datetime, timedelta

import sources
from exclusions import is_excluded, compile_exclusions
from media_server_exclusions import expand_exclusion_patterns

from db import (
    score_weight_points,
    db_load_config, db_load_history, db_save_history,
    db_load_results, db_save_results, db_save_audit,
    db_save_upload_snapshot, db_get_upload_snapshots, db_get_recent_runs,
    db_save_change_log_entry,
    db_save_file_results,
    db_save_file_signatures, db_load_file_signatures,
    db_get_meta, db_set_meta, db_delete_meta,
)
import next_steps
from state import get_state, set_state, update_progress
from debug import process_rss_mb, container_memory, host_available_mb, malloc_trim

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


def _walk_directory(base_path, source_label, inode_map, qbit_file_map, scanned_so_far, total_files, exclusion_patterns=None, total_ref=None, compiled_exclusions=None):
    # Returns an ordered list of file_keys (one per filesystem entry, including
    # cross-seed duplicates) instead of full record dicts. Per-file metadata
    # (rel_path, size, excluded) is folded directly into inode_map to
    # avoid holding a second equally-large data structure in memory during the walk.
    key_order   = []
    scanned     = scanned_so_far
    stat_errors = 0
    if not os.path.exists(base_path):
        log.warning(f"Path does not exist, skipping: {base_path}")
        return key_order, scanned, stat_errors
    for root, _, files in os.walk(base_path):
        for filename in files:
            full_path = os.path.join(root, filename)
            try:
                st       = os.stat(full_path)
                file_key = (st.st_dev, st.st_ino)
                size     = st.st_size
                rel_path = os.path.relpath(full_path, base_path)
                excluded = (compiled_exclusions.match(full_path, rel_path, filename)
                            if compiled_exclusions is not None
                            else is_excluded(full_path, rel_path, filename, exclusion_patterns))
                inode_map.setdefault(file_key, {
                    'trackers': set(), 'status': 'Orphaned',
                    'torrent_paths': [], 'media_paths': [], 'hash': '',
                    'instance_id': None, 'instance_name': None,
                    'tracker_health': 'unknown', 'tracker_msg': '',
                    'unreg_claimants': {},
                    'size': 0,
                    'torrent_rel_path': None, 'torrent_excluded': False,
                    'media_rel_path': None, 'media_excluded': False,
                })
                if source_label == 'Torrent':
                    inode_map[file_key]['torrent_paths'].append(full_path)
                    if inode_map[file_key]['torrent_rel_path'] is None:
                        # First occurrence wins for cross-seeded inodes
                        inode_map[file_key]['size']             = size
                        inode_map[file_key]['torrent_rel_path'] = rel_path
                        inode_map[file_key]['torrent_excluded'] = excluded
                    qbit_info = qbit_file_map.get(full_path)
                    if qbit_info:
                        info = inode_map[file_key]
                        info['trackers'].update(qbit_info['trackers'])
                        info['category'] = qbit_info.get('category', '') or info.get('category', '')
                        # Cross-seeds via distinct hardlinks share an inode but
                        # have distinct paths, so each is a separate qbit_file_map
                        # entry merged here. Keep the HEALTHIEST claimant across
                        # all of this inode's paths (a path with any live torrent
                        # must never read as dead); hash/instance follow it.
                        new_health = qbit_info.get('tracker_health', 'unknown')
                        if info['hash'] == '' or \
                                sources.HEALTH_RANK.get(new_health, 1) > \
                                sources.HEALTH_RANK.get(info['tracker_health'], 1):
                            info['hash']           = qbit_info.get('hash', '')
                            info['instance_id']    = qbit_info.get('instance_id')
                            info['instance_name']  = qbit_info.get('instance_name')
                            info['tracker_health'] = new_health
                            info['tracker_msg']    = qbit_info.get('tracker_msg', '')
                        # Union unregistered claimants across every path of this
                        # inode so a dead cross-seed sibling survives the merge.
                        for h, c in (qbit_info.get('unreg_claimants') or {}).items():
                            info['unreg_claimants'][h] = c
                        cur = info['status']
                        if qbit_info['status'] == 'Seeding' or cur == 'Seeding':
                            info['status'] = 'Seeding'
                        elif cur == 'Orphaned':
                            info['status'] = qbit_info['status']
                else:
                    inode_map[file_key]['media_paths'].append(full_path)
                    if inode_map[file_key]['media_rel_path'] is None:
                        inode_map[file_key]['media_rel_path'] = rel_path
                        inode_map[file_key]['media_excluded'] = excluded
                        if inode_map[file_key]['size'] == 0:
                            inode_map[file_key]['size'] = size
                key_order.append(file_key)
            except Exception as e:
                log.warning(f"Could not stat {full_path}: {e}")
                stat_errors += 1
            scanned += 1
            if total_ref is not None:
                total_ref[0] += 1
                if total_ref[0] % 500 == 0:
                    set_state(total_files=total_ref[0])
            update_progress(scanned, total_ref[0] if total_ref is not None else total_files)
    return key_order, scanned, stat_errors


# Size groups larger than this are skipped entirely. Real media duplicates come
# in groups of 2-5; groups of hundreds of identically-sized files are structural
# (BDMV .bdmv/.clpi/.bup, DVD IFO/BUP) and hashing + cross-referencing them is
# what used to blow up both scan time and memory on disc-heavy libraries.
DUP_GROUP_LIMIT = 200
# Each file stores at most this many sibling paths. Without a cap a group of k
# identical files stores k*(k-1) path strings — quadratic, and the reason
# file_results JSON used to exceed SQLite's 1 GB limit on large libraries.
DUP_PATHS_PER_FILE = 10


def _info_excluded(info):
    """Effective excluded flag for an inode (torrent role wins when present)."""
    if info['torrent_rel_path'] is not None:
        return info['torrent_excluded']
    return info['media_excluded']


def _build_duplicate_map(inode_map):
    """O(n) duplicate detection: group by size, then file identity, then hash representatives only."""
    size_groups = {}
    for file_key, info in inode_map.items():
        if info['size'] > 0 and not _info_excluded(info):
            size_groups.setdefault(info['size'], []).append(file_key)

    duplicate_map = {}
    skipped_groups = 0
    for size, file_keys in size_groups.items():
        if len(file_keys) <= 1:
            continue
        if len(file_keys) > DUP_GROUP_LIMIT:
            skipped_groups += 1
            continue
        hash_to_keys = {}
        for file_key in file_keys:
            info = inode_map[file_key]
            paths = info['torrent_paths'] or info['media_paths']
            if not paths:
                continue
            fh = get_fast_hash(paths[0], size)
            if fh:
                hash_to_keys.setdefault(fh, []).append(file_key)
        for fh, dup_keys in hash_to_keys.items():
            if len(dup_keys) <= 1:
                continue
            for file_key in dup_keys:
                others = duplicate_map.setdefault(file_key, [])
                for o in dup_keys:
                    if len(others) >= DUP_PATHS_PER_FILE:
                        break
                    if o != file_key:
                        oinfo  = inode_map[o]
                        opaths = oinfo['torrent_paths'] or oinfo['media_paths']
                        if opaths:
                            others.append(opaths[0])
    if skipped_groups:
        log.info(f"Duplicate detection: skipped {skipped_groups} size group(s) larger than "
                 f"{DUP_GROUP_LIMIT} files (structural files, e.g. disc folders).")
    return duplicate_map


def _assemble_records(torrent_key_order, media_key_order, inode_map, duplicate_map):
    torrent_files_data = []
    seen_torrent_keys = set()
    for file_key in torrent_key_order:
        # Cross-seeded files share an inode across multiple torrent directories.
        # Only emit one entry per unique inode so the file browser and health
        # metrics don't count the same physical file N times (once per cross-seed).
        if file_key in seen_torrent_keys:
            continue
        seen_torrent_keys.add(file_key)
        info    = inode_map[file_key]
        file_id = f"{file_key[0]}:{file_key[1]}"
        # Unregistered cross-seed claimants other than the kept (healthiest) one:
        # dead torrent registrations whose payload is still alive on this inode.
        # Only meaningful (and only emitted) when the kept claimant is itself
        # alive — a fully-dead path is a dead_seed, handled separately.
        kept_hash     = info.get('hash', '')
        dead_siblings = [c for h, c in info.get('unreg_claimants', {}).items()
                         if h != kept_hash] if info.get('tracker_health') != 'unregistered' else []
        record = {
            "path": info['torrent_rel_path'], "size": info['size'], "inode": file_key[1],
            "file_id": file_id,
            "status": info['status'],
            # A torrent is imported iff a hardlink to its inode exists inside the
            # media library (the media walk populates media_paths). nlink > 1 is
            # NOT a valid proxy: cross-seeding hardlinks the same release across
            # several tracker dirs, so nlink > 1 is true for never-imported files
            # too (issue #15).
            "imported": len(info['media_paths']) > 0,
            "trackers": list(info['trackers']) or ["None"],
            "linked_paths": info['media_paths'],
            "duplicate_paths": duplicate_map.get(file_key, []),
            "excluded": info['torrent_excluded'],
            "hash": info.get('hash', ''),
            "category": info.get('category', ''),
            "instance_id":   info.get('instance_id'),
            "instance_name": info.get('instance_name'),
            "tracker_health": info.get('tracker_health', 'unknown'),
            "tracker_msg":    info.get('tracker_msg', ''),
        }
        if dead_siblings:
            record["dead_siblings"] = dead_siblings
        torrent_files_data.append(record)
    media_files_data = []
    seen_media_keys = set()
    for file_key in media_key_order:
        if file_key in seen_media_keys:
            continue
        seen_media_keys.add(file_key)
        info    = inode_map[file_key]
        file_id = f"{file_key[0]}:{file_key[1]}"
        media_files_data.append({
            "path": info['media_rel_path'], "size": info['size'], "inode": file_key[1],
            "file_id": file_id,
            "status": info['status'], "imported": True,
            "trackers": list(info['trackers']) or ["None"],
            "linked_paths": info['torrent_paths'],
            "duplicate_paths": duplicate_map.get(file_key, []),
            "excluded": info['media_excluded'],
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
    # How many of the 100 points each category is worth. Configurable so a user
    # whose workflow legitimately lacks a category (e.g. torrents removed once
    # seeding requirements are met, leaving healthy but unhardlinked media) can
    # stop being marked down for it. Defaults reproduce the original 70/10/10/10.
    pts     = score_weight_points(cfg)
    hl_max  = pts['WEIGHT_HARDLINKED']
    or_max  = pts['WEIGHT_ORPHANED']
    ni_max  = pts['WEIGHT_NOT_IMPORTED']
    dup_max = pts['WEIGHT_DUPLICATES']
    hl_ratio = (hardlinked_media_size / total_media_size) if total_media_size > 0 else 1.0
    hl_score = hl_ratio * hl_max
    or_limit   = total_torrents_size * or_ratio
    or_penalty = (orphaned_torrent_size / or_limit) * or_max if or_limit > 0 else (or_max if orphaned_torrent_size > 0 else 0)
    or_score   = max(0, or_max - or_penalty)
    ni_limit   = total_torrents_size * ni_ratio
    ni_penalty = (not_imported_size / ni_limit) * ni_max if ni_limit > 0 else (ni_max if not_imported_size > 0 else 0)
    ni_score   = max(0, ni_max - ni_penalty)
    dup_limit   = total_torrents_size * dup_ratio
    dup_penalty = (dup_size / dup_limit) * dup_max if dup_limit > 0 else (dup_max if dup_size > 0 else 0)
    dup_score   = max(0, dup_max - dup_penalty)
    final_score = round(max(0, min(100, hl_score + or_score + ni_score + dup_score)), 1)
    if   final_score >= 90: status_text = "Great"
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
            "dead_seed_count": sum(1 for f in scoring_torrents
                                   if f['imported'] and f['status'] != 'Orphaned'
                                   and f.get('tracker_health') == 'unregistered'),
            # Scored file counts — cheap here, and the only place the totals are
            # known without re-deserializing the full file lists.
            "media_file_count": len(scoring_media), "torrent_file_count": len(scoring_torrents),
            "duplicate_count": dup_count, "or_limit": or_limit, "ni_limit": ni_limit,
            "dup_limit": dup_limit, "hl_score": round(hl_score,1), "or_score": round(or_score,1),
            "ni_score": round(ni_score,1), "dup_score": round(dup_score,1),
            # Points each category was worth for this run. The dashboard reads
            # these as the card denominators — without them it would show the
            # old hardcoded 70/10/10/10 next to weighted scores.
            "hl_max": round(hl_max,1), "or_max": round(or_max,1),
            "ni_max": round(ni_max,1), "dup_max": round(dup_max,1),
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
    daily_library_by_date  = {}
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
        # Library-wide block (hardlinked/duplicates) — absent in old snapshots
        lib = row['snapshot'].get('_library')
        if isinstance(lib, dict):
            daily_library_by_date[date_str] = lib

    daily_tracker_stats = [
        {'date': date_str, 'by_tracker': stats}
        for date_str, stats in sorted(daily_point_by_tracker.items())
    ]
    daily_library_stats = [
        {'date': date_str, **stats}
        for date_str, stats in sorted(daily_library_by_date.items())
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
        "daily_library_stats": daily_library_stats,
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

# Signature bitmask bits — a compact per-file fingerprint of the only fields
# compute_diff cares about on the *previous* side. Stored as {path: int} so
# diffing against the last scan never deserializes the previous full record
# list (multi-GB of Python objects for 500K+ file libraries).
SIG_ORPHANED = 1
SIG_IMPORTED = 2
SIG_HAS_DUPS = 4


def file_signatures(files):
    """Build the compact {path: bitmask} diff signature for a file list."""
    return {
        f['path']: (
            (SIG_ORPHANED if f.get('status') == 'Orphaned' else 0)
            | (SIG_IMPORTED if f.get('imported') else 0)
            | (SIG_HAS_DUPS if f.get('duplicate_paths') else 0)
        )
        for f in files
    }


def compute_diff_from_signatures(prev_sigs_by_tab, curr_snap, prev_score=None):
    """Diff the current snapshot against compact signatures of the previous scan.

    prev_sigs_by_tab: {'media': {path: bitmask}, 'torrents': {path: bitmask}}
    Lists are capped at 50 entries during collection (not after) so a mass
    rename/move can't transiently allocate one diff entry per file.
    """
    if not prev_sigs_by_tab or not curr_snap:
        return None

    changes = {
        'newly_orphaned':      [],
        'newly_imported':      [],
        'new_duplicates':      [],
        'resolved_duplicates': [],
        'new_files':           [],
        'removed_files':       [],
        'score_delta':         None,
    }
    CAP = 50
    has_changes = False

    cs = (curr_snap.get('dashboard') or {}).get('score')
    if prev_score is not None and cs is not None:
        changes['score_delta'] = round(cs - prev_score, 1)

    def add(key, entry):
        nonlocal has_changes
        has_changes = True
        if len(changes[key]) < CAP:
            changes[key].append(entry)

    for tab, key in [('media', 'media_files'), ('torrents', 'torrent_files')]:
        prev = prev_sigs_by_tab.get(tab) or {}
        curr_paths = set()

        for f in curr_snap.get(key, []):
            path = f['path']
            curr_paths.add(path)
            if path not in prev:
                add('new_files', {'path': path, 'size': f['size'], 'tab': tab})
            else:
                sig = prev[path]
                if not (sig & SIG_ORPHANED) and f.get('status') == 'Orphaned':
                    add('newly_orphaned', {'path': path, 'size': f['size'], 'tab': tab})
                if tab == 'torrents' and not (sig & SIG_IMPORTED) and f.get('imported'):
                    add('newly_imported', {'path': path, 'size': f['size'], 'tab': tab})
                if not (sig & SIG_HAS_DUPS) and f.get('duplicate_paths'):
                    add('new_duplicates', {'path': path, 'size': f['size'], 'tab': tab})
                if (sig & SIG_HAS_DUPS) and not f.get('duplicate_paths'):
                    add('resolved_duplicates', {'path': path, 'size': f['size'], 'tab': tab})

        for path in prev:
            if path not in curr_paths:
                add('removed_files', {'path': path, 'tab': tab})

    return changes if has_changes else None


def compute_diff(prev_snap, curr_snap):
    """Diff two full snapshots. Thin wrapper over the signature-based diff,
    kept for callers/tests that hold both snapshots in memory."""
    if not prev_snap or not curr_snap:
        return None
    prev_sigs = {
        'media':    file_signatures(prev_snap.get('media_files', [])),
        'torrents': file_signatures(prev_snap.get('torrent_files', [])),
    }
    prev_score = (prev_snap.get('dashboard') or {}).get('score')
    return compute_diff_from_signatures(prev_sigs, curr_snap, prev_score=prev_score)


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


def _is_triage_relevant(f):
    """Torrent-file records the Triage workflow reads: not-imported files,
    imported dead seeds, and carriers of dead cross-seed registrations.

    Persisted as the compact 'triage' file_results row at save time so the
    Triage page never deserializes the full torrent list (a few hundred MB of
    object graph on large libraries) for the ~2% of records it acts on.
    Must stay in lockstep with the filters in app.workflows_triage.
    """
    if f.get('excluded'):
        return False
    if f.get('dead_siblings'):
        return True
    if f.get('status') == 'Orphaned':
        return False
    if not f.get('imported'):
        return True
    return f.get('tracker_health') == 'unregistered'


def _not_imported_paths(torrent_files):
    return [f['path'] for f in torrent_files if _is_not_imported_torrent(f)]


# ---------------------------------------------------------------------------
# Main audit process
# ---------------------------------------------------------------------------

def _save_error_status(message):
    curr = db_load_results()
    curr["status"] = message
    db_save_results(curr)


# Serializes read-modify-write of the scan marker between the audit thread
# (phase transitions) and the memory sampler thread (periodic RSS updates).
_marker_lock = threading.Lock()


def _enter_phase(phase, message):
    """Advance the scan to a new phase: update state, log RSS, and persist the
    scan marker so a process killed mid-scan (OOM, container restart) leaves
    evidence of exactly where it died for the next boot to report."""
    rss = process_rss_mb()
    set_state(status_message=message, phase=phase)
    log.info(f"Audit: {message}" + (f" (rss={rss} MB)" if rss is not None else ""))
    hist = (get_state().get('phase_history') or []) + [{
        'message': message, 'rss_mb': rss, 'at': datetime.now().isoformat(timespec='seconds'),
    }]
    set_state(phase_history=hist[-30:])
    try:
        with _marker_lock:
            marker = db_get_meta('scan_marker') or {}
            marker['phase']  = message
            marker['rss_mb'] = rss
            db_set_meta('scan_marker', marker)
    except Exception as e:
        log.warning(f"Could not update scan marker: {e}")


def _memory_sampler(stop_event, interval=20):
    """Sample RSS during a scan: persist breadcrumbs into the scan marker so an
    OOM-killed scan leaves a memory reading from seconds before death (the
    in-memory log buffer dies with the process — SQLite survives), and log
    warnings while there is still time to read them when usage approaches the
    container limit or host memory runs out."""
    warned = set()
    while not stop_event.wait(interval):
        rss = process_rss_mb()
        if rss is None:
            continue  # non-Linux dev machine
        try:
            with _marker_lock:
                marker = db_get_meta('scan_marker')
                if marker:
                    marker['last_rss_mb']    = rss
                    marker['peak_rss_mb']    = max(rss, marker.get('peak_rss_mb') or 0)
                    marker['last_sample_at'] = datetime.now().isoformat(timespec='seconds')
                    db_set_meta('scan_marker', marker)
        except Exception:
            pass
        limit = (container_memory() or {}).get('limit_mb')
        if limit:
            pct = rss * 100 // limit
            for threshold in (95, 85):
                if pct >= threshold and threshold not in warned:
                    warned.add(threshold)
                    log.warning(
                        f"Memory pressure: scan is using {rss} MB — {pct}% of the container's "
                        f"{limit} MB limit. If the scan dies here it was OOM-killed; raise the "
                        f"memory limit or add exclusions for large structural directories.")
                    break
        else:
            avail = host_available_mb()
            if avail is not None and avail < 400 and 'host' not in warned:
                warned.add('host')
                log.warning(
                    f"Memory pressure: host has only {avail} MB available — the host OOM killer "
                    f"may terminate the scan (no container memory limit is set).")


def _scan_peak_rss():
    """Best-effort per-scan peak RSS: the sampler folds highs into the scan
    marker every 20s; fall back to the current reading. Persisted onto the
    audit_runs row so peak-per-scan trends survive across days of history."""
    try:
        return (db_get_meta('scan_marker') or {}).get('peak_rss_mb') or process_rss_mb()
    except Exception:
        return None


def run_audit_process(trigger=None, persist_source_errors=True):
    cfg = db_load_config()
    # Accept trigger as parameter so callers can pass it explicitly,
    # avoiding a race between set_state(trigger=...) and reading it back
    if trigger is None:
        trigger = get_state().get('trigger', 'manual')
    scan_start = time.time()
    set_state(is_scanning=True, progress=0, scanned_files=0, total_files=0,
              status_message="Connecting to torrent source...", last_scan_status="running",
              phase="connecting", phase_history=[])
    try:
        db_set_meta('scan_marker', {
            'started_at': datetime.now().isoformat(timespec='seconds'),
            'trigger':    trigger,
            'pid':        os.getpid(),
            'phase':      'Connecting to torrent source...',
        })
    except Exception as e:
        log.warning(f"Could not write scan marker: {e}")
    sampler_stop = threading.Event()
    threading.Thread(target=_memory_sampler, args=(sampler_stop,), daemon=True,
                     name="audit-memory-sampler").start()
    try:
        qbit_file_map, trackers, tracker_snapshot = sources.fetch_file_map(cfg)
        set_state(source_file_count=len(qbit_file_map))
        total_ref = [0]
        set_state(total_files=0)
        _enter_phase("disk", "Scanning torrent directory...")
        inode_map          = {}
        exclusion_patterns = expand_exclusion_patterns(cfg)
        compiled_excl      = compile_exclusions(exclusion_patterns)
        torrent_key_order, scanned, torrent_errors = _walk_directory(
            cfg.get('LOCAL_PATH',''), 'Torrent', inode_map, qbit_file_map, 0, 0,
            exclusion_patterns=exclusion_patterns, total_ref=total_ref,
            compiled_exclusions=compiled_excl)
        _enter_phase("disk", "Scanning media directory...")
        media_key_order, _, media_errors = _walk_directory(
            cfg.get('MEDIA_PATH',''), 'Media', inode_map, qbit_file_map, scanned, 0,
            exclusion_patterns=exclusion_patterns, total_ref=total_ref,
            compiled_exclusions=compiled_excl)
        stat_errors = torrent_errors + media_errors
        # The source file map is only consulted during the walks — release it
        # (~300 MB at 650K files) before the memory-heavy assemble/save phases.
        del qbit_file_map
        _enter_phase("post", "Detecting duplicates...")
        duplicate_map = _build_duplicate_map(inode_map)
        _enter_phase("post", "Assembling file records...")
        torrent_files_data, media_files_data = _assemble_records(
            torrent_key_order, media_key_order, inode_map, duplicate_map)
        del torrent_key_order, media_key_order, inode_map, duplicate_map
        _enter_phase("post", "Computing health metrics...")
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
            # Library-wide stats (hardlinked %, duplicates) have no per-tracker
            # breakdown — stored under a '_'-prefixed key, which every tracker
            # loop already skips. Feeds the dashboard card trend sparklines.
            det = dashboard_stats['current']['details']
            aug['_library'] = {
                'hardlinked_media_size': det['hardlinked_media_size'],
                'total_media_size':      det['total_media_size'],
                'duplicate_size':        det['duplicate_size'],
            }
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

        # Compute diff against compact signatures of the previous scan — BEFORE overwriting.
        # Signatures are {path: bitmask}, so this never deserializes the previous full
        # record lists (multi-GB of Python objects for 500K+ file libraries).
        ran_at = datetime.now().isoformat()
        _enter_phase("post", "Computing changes vs previous scan...")
        try:
            prev_sigs = {
                'media':    db_load_file_signatures('media'),
                'torrents': db_load_file_signatures('torrents'),
            }
            if prev_sigs['media'] or prev_sigs['torrents']:
                curr_snap = {
                    "media_files": media_files_data, "torrent_files": torrent_files_data,
                    "dashboard":   dashboard_stats,
                }
                prev_score = (db_load_results().get('dashboard') or {}).get('score')
                diff = compute_diff_from_signatures(prev_sigs, curr_snap, prev_score=prev_score)
                del prev_sigs, curr_snap
                if diff:
                    db_save_change_log_entry(
                        ran_at=ran_at,
                        health_score=dashboard_stats['score'],
                        trigger=trigger,
                        source=cfg.get('TORRENT_SOURCE', 'qbit'),
                        diff=diff,
                    )
            else:
                log.info("No previous file signatures stored — change log skipped this run, "
                         "resumes on the next audit.")
        except Exception as e:
            log.warning(f"Could not save change log entry: {e}")
        # Persist file lists separately so /api/results only loads summary data
        _enter_phase("post", "Saving file results...")
        db_save_file_results('media',    media_files_data)
        db_save_file_results('torrents', torrent_files_data)
        # Compact Triage working set (references, not copies) — lets the
        # Triage page skip deserializing the full torrent list.
        db_save_file_results('triage',
                             [f for f in torrent_files_data if _is_triage_relevant(f)])
        db_save_file_signatures('media',    file_signatures(media_files_data))
        db_save_file_signatures('torrents', file_signatures(torrent_files_data))
        _enter_phase("post", "Saving audit results...")
        db_save_results(result)
        # Snapshot stores only dashboard stats — no file lists (eliminates 300MB+ per row)
        snapshot = {"dashboard": dashboard_stats}
        db_save_audit(trigger, dashboard_stats['score'], 'ok', None, snapshot,
                      source=cfg.get('TORRENT_SOURCE', 'qbit'),
                      duration_seconds=round(time.time() - scan_start, 1),
                      ran_at=ran_at, peak_rss_mb=_scan_peak_rss())
        # Advance the Next steps reward counters (cumulative shovel count,
        # hardlink high-water mark, clean-state streaks). Kept here rather than
        # in the endpoint so /api/next_steps never recomputes history — it is
        # polled, and this is a single small app_meta row.
        # NB: `update_progress` is also a state.py import, hence the namespace.
        try:
            _ns_prev = db_get_meta('ns_progress')
            _ns_det  = dashboard_stats['current']['details']
            # Built with the *previous* progress so prior latches still apply;
            # update_progress then unions in whatever was newly earned. This is
            # what makes the prize layer ratchet — points accrue for action and
            # nothing is ever deducted for inaction or regression.
            _ns_state = next_steps.build_state(
                cfg, result, db_get_recent_runs(limit=2000), progress=_ns_prev)
            db_set_meta('ns_progress', next_steps.update_progress(
                _ns_prev, cfg, _ns_det, state=_ns_state))
        except Exception as e:
            log.warning(f"Could not update Next steps progress: {e}")
        # Scan finished — clear the crash-loop streak so future startups scan normally
        try:
            db_set_meta('consecutive_aborted_scans', 0)
        except Exception:
            pass
        rss = process_rss_mb()
        log.info(f"Audit complete: {len(torrent_files_data)} torrent file(s), "
                 f"{len(media_files_data)} media file(s), {len(trackers)} tracker(s), "
                 f"{round(time.time() - scan_start)}s"
                 + (f", rss={rss} MB" if rss is not None else ""))
        if stat_errors:
            log.warning(f"Audit complete with {stat_errors} unreadable file(s) — check earlier warnings.")
        status_msg = f"Audit complete. {stat_errors} file(s) could not be read — check logs." if stat_errors else "Audit complete."
        set_state(status_message=status_msg, last_scan_status="ok")
    except sources.SourceConnectionError as e:
        msg = str(e)
        log.error(msg)
        if persist_source_errors:
            _save_error_status(msg)
            db_save_audit(trigger, None, 'error', msg, {}, source=cfg.get('TORRENT_SOURCE', 'qbit'), duration_seconds=round(time.time() - scan_start, 1), peak_rss_mb=_scan_peak_rss())
        set_state(status_message=msg, last_scan_status="error")
    except MemoryError:
        # Python-level allocation failure (the kernel OOM killer SIGKILLs instead —
        # that case is handled by the scan marker at next boot). Drop the big scan
        # structures first so this error path itself has memory to log and persist.
        try:
            del inode_map
        except NameError:
            pass
        try:
            del qbit_file_map
        except NameError:
            pass
        try:
            del duplicate_map
        except NameError:
            pass
        try:
            del torrent_files_data
        except NameError:
            pass
        try:
            del media_files_data
        except NameError:
            pass
        gc.collect()
        rss = process_rss_mb()
        msg = ("Audit error: out of memory (MemoryError)"
               + (f" at ~{rss} MB RSS" if rss is not None else "")
               + ". The scan exceeded available memory — raise the container memory limit "
                 "or exclude large structural directories (e.g. disc folders). "
                 "See /api/debug/report.")
        log.error(msg)
        try:
            _save_error_status(msg)
            db_save_audit(trigger, None, 'error', msg, {}, source=cfg.get('TORRENT_SOURCE', 'qbit'),
                          duration_seconds=round(time.time() - scan_start, 1),
                          peak_rss_mb=_scan_peak_rss())
        except Exception as e2:
            log.error(f"Could not persist out-of-memory error status: {e2}")
        set_state(status_message=msg, last_scan_status="error")
    except Exception as e:
        msg = f"Audit error: {e}"
        log.exception("Unexpected error during audit")
        _save_error_status(msg)
        db_save_audit(trigger, None, 'error', msg, {}, source=cfg.get('TORRENT_SOURCE', 'qbit'), duration_seconds=round(time.time() - scan_start, 1), peak_rss_mb=_scan_peak_rss())
        set_state(status_message=msg, last_scan_status="error")
    finally:
        sampler_stop.set()
        # Persist this scan's phase history (with per-phase RSS) so the debug
        # report can show it even after a process restart.
        try:
            db_set_meta('last_scan_phases', get_state().get('phase_history') or [])
        except Exception:
            pass
        # The process survived to this point — remove the crash marker. If the
        # process is killed mid-scan (OOM), this never runs and the marker is
        # found at next startup, which records an 'aborted' audit run.
        try:
            db_delete_meta('scan_marker')
        except Exception:
            pass
        # Drop any scan structures this frame still references (assignment is
        # NameError-proof across the success and error paths), then hand the
        # freed pages back to the OS — glibc otherwise keeps the scan's peak
        # resident for the container's lifetime, which on very large libraries
        # ratchets RSS upward until the next restart.
        qbit_file_map = inode_map = duplicate_map = None
        torrent_key_order = media_key_order = None
        torrent_files_data = media_files_data = None
        result = not_imported_paths = None
        try:
            rss_before = process_rss_mb()
            gc.collect()
            if malloc_trim() and rss_before is not None:
                log.info(f"Post-scan memory trim: rss {rss_before} -> {process_rss_mb()} MB")
        except Exception:
            pass
        scan_end = time.time()
        set_state(progress=100, is_scanning=False,
                  last_audit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  trigger="idle", phase="idle",
                  last_scan_completed_at=scan_end,
                  last_scan_duration=scan_end - scan_start)
