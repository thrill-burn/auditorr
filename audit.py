import os
import gc
import math
import time
import posixpath
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
    db_get_meta, db_set_meta, db_update_meta, db_delete_meta,
)
import rounds
from state import get_state, set_state, update_progress
from debug import process_rss_mb, container_memory, host_available_mb, malloc_trim

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def get_fast_hash(filepath, size, chunk_size=65536):
    """md5 of the first, middle and last `chunk_size` bytes; the whole file when
    it is no bigger than the three chunks.

    The middle chunk is DEDUPE F7. Two encodes sharing a container header and
    trailer collided on head + tail, so they were offered as duplicates, counted
    in the headline and `duplicate_count`, and found to differ only by the
    script's `cmp` after reading both files in full. An extra chunk can only
    split a candidate set, never join one, and the hash is never persisted, so
    nothing migrates.
    """
    try:
        hasher = hashlib.md5()
        with open(filepath, 'rb') as f:
            if size <= chunk_size * 3:
                hasher.update(f.read())
            else:
                hasher.update(f.read(chunk_size))
                f.seek(size // 2 - chunk_size // 2)
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


_ROOT_NAMES = {'Torrent': 'torrents', 'Media': 'media'}
# A directory this many segments or fewer below a root is a category or a release
# folder, and one that cannot be listed hides whole releases (S03).
_UNLISTABLE_SHALLOW_DEPTH = 2


def _walk_directory(base_path, source_label, inode_map, qbit_file_map, scanned_so_far, total_files, exclusion_patterns=None, total_ref=None, compiled_exclusions=None, walk_report=None):
    # Returns an ordered list of file_keys (one per filesystem entry, including
    # cross-seed duplicates) instead of full record dicts. Per-file metadata
    # (rel_path, size, excluded) is folded directly into inode_map to
    # avoid holding a second equally-large data structure in memory during the walk.
    key_order   = []
    scanned     = scanned_so_far
    stat_errors = 0
    # Oldest media file, for the Next steps "Provenance" ladder. Tracked as a
    # running minimum here rather than stored per record on purpose: mtime is
    # already in the stat struct below (free), but an extra field on every
    # inode_map entry is ~8 bytes x every file in the library, which is the
    # known RAM hotspot. A scalar costs nothing.
    oldest_mtime = None
    # S03 (the 2026-09-10 outside review) — what this walk could not see. A root
    # that did not exist returned an empty walk with zero stat errors, and
    # `os.walk` had no `onerror`, so a directory that could not be listed dropped
    # everything beneath it without a word. `walk_report`, when a dict, is filled
    # in place — an out-parameter like `fetch_file_map`'s `unresolved_roots`, so
    # the four values every caller unpacks stay as they were. Counts and
    # booleans only: the audit persists it, and it reaches /api/debug/report.
    walk = walk_report if walk_report is not None else {}
    root_name = _ROOT_NAMES.get(source_label, source_label)
    walk.update(configured=bool(base_path), exists=bool(base_path) and os.path.isdir(base_path),
                files=0, stat_errors=0, unlistable=0, unlistable_shallow=0)
    if not walk['exists']:
        if base_path:
            log.warning("The %s root is not a directory inside the container — skipping its walk",
                        root_name)
        return key_order, scanned, stat_errors, oldest_mtime

    def _unlistable(err):
        walk['unlistable'] += 1
        where = getattr(err, 'filename', None)
        try:
            rel = os.path.relpath(os.fspath(where), base_path) if where else '.'
            depth = len([s for s in rel.replace('\\', '/').split('/') if s not in ('', '.')])
        except (TypeError, ValueError):
            depth = 0
        if depth <= _UNLISTABLE_SHALLOW_DEPTH:
            walk['unlistable_shallow'] += 1
        log.warning("Could not list a directory %d level(s) below the %s root (%s)",
                    depth, root_name, type(err).__name__)

    for root, _, files in os.walk(base_path, onerror=_unlistable):
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
                        # Completion (R2). Sparse — absent means complete, which
                        # is almost every record, and an extra key on every
                        # inode_map entry is the RAM hotspot this walk is
                        # careful about. Sticky across an inode's paths for the
                        # same reason it is sticky in the source layer: a
                        # cross-seed still writing to these bytes must not have
                        # them hardlinked out from under it.
                        if qbit_info.get('incomplete'):
                            info['incomplete'] = True
                        if qbit_info.get('completion_unknown'):
                            info['completion_unknown'] = True
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
                        if not excluded and st.st_mtime > 0 and (
                                oldest_mtime is None or st.st_mtime < oldest_mtime):
                            oldest_mtime = st.st_mtime
                key_order.append(file_key)
            except Exception as e:
                log.warning(f"Could not stat {full_path}: {e}")
                stat_errors += 1
            scanned += 1
            walk['files'] += 1
            if total_ref is not None:
                total_ref[0] += 1
                if total_ref[0] % 500 == 0:
                    set_state(total_files=total_ref[0])
            update_progress(scanned, total_ref[0] if total_ref is not None else total_files)
    walk['stat_errors'] = stat_errors
    return key_order, scanned, stat_errors, oldest_mtime


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


def _info_incomplete(info):
    """Whether this inode might not hold a whole file yet (DEDUPE F6).

    Both spellings disqualify it, and the asymmetry is the whole argument.
    qBittorrent does not preallocate by default — it writes **sparse** files,
    which report the final `st_size` while unwritten regions read as zeros. Two
    unfinished files of equal size whose written regions do not overlap land in
    the same size group, produce the same head+tail fast hash, and **pass the
    `cmp` in the generated script**, because at that moment both really are
    zeros there. `cmp` is the last line of defence for every other failure mode
    in Dedupe and it cannot help here, so the guard has to be upstream: two
    torrents hardlinked onto one inode both write to it, both are corrupt, and
    there is no recovery.

    So a missed reclaim is the cost of being wrong one way, and two destroyed
    torrents the cost of being wrong the other. "Could not determine" excludes.
    """
    return bool(info.get('incomplete') or info.get('completion_unknown'))


def _build_duplicate_map(inode_map):
    """O(n) duplicate detection: group by size, then file identity, then hash representatives only."""
    size_groups = {}
    for file_key, info in inode_map.items():
        if info['size'] > 0 and not _info_excluded(info) and not _info_incomplete(info):
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


def _norm_abs(path):
    p = str(path or '').replace('\\', '/')
    return posixpath.normpath(p) if p else ''


def _path_under(path, root):
    """`path` is `root` or inside it. Both already `_norm_abs`'d; a root of '' or
    '/' contains everything."""
    if root in ('', '/'):
        return True
    return path == root or path.startswith(root + '/')


def unverified_spec(report, unresolved_roots):
    """Which orphans the scan could not ask about, or None (CLEANUP §5.3).

    Since Phase 2 a failed listing claims whatever sits at its roots, so a
    per-file unknown is reachable in exactly two cases, and both are derived
    here rather than guessed:

    * **`all`** — the scan persisted even though a client instance failed. The
      plausibility guard refuses that on every trigger but a manual scan, which
      is the explicit override; a torrent on the instance that did not answer is
      invisible, so every orphan of that scan is `unverified`.
    * **`roots`** — torrents whose listing failed *and* whose disk fallback found
      nothing (`listing_unresolved`). Their files could be anywhere under their
      save path, so every orphan under it is `unverified`. Blunt when it fires —
      it can cover a whole category dir — but near-empty on a sane install, the
      fail-safe direction, and gone on the next clean scan.

    `unresolved_roots` comes out of `sources.fetch_file_map` in memory only; the
    persisted source report carries counts and must stay free of paths.
    """
    failed = bool((report or {}).get('instances_failed'))
    roots = sorted({_norm_abs(r) for r in (unresolved_roots or []) if r})
    if not failed and not roots:
        return None
    return {'all': failed, 'roots': roots}


def _torrent_tree_base(info):
    """The walk's own prefix for this inode's torrent paths, or None.

    `torrent_rel_path` is `os.path.relpath(first path, LOCAL_PATH)` and the first
    entry of `torrent_paths` is that path, both from one `os.walk` — so the base
    is a string suffix-strip, and every other torrent path of the inode shares
    it. No `relpath` over mixed separators (the `_local_to_abs` trap), and no
    `LOCAL_PATH` threaded through.
    """
    paths = info.get('torrent_paths') or []
    rel = info.get('torrent_rel_path')
    if not paths or not rel or not paths[0].endswith(rel):
        return None
    return paths[0][:len(paths[0]) - len(rel)]


def _stamp_orphan(record, info, compiled_exclusions, unverified):
    """What Cleanup needs about one orphaned inode that only the walk knows.

    Sparse, and written **only on non-excluded orphans** — the `dead_siblings` /
    `incomplete` rule: a field on every torrent-file record multiplies across
    the library and grows `files_json`. Bounded by the orphan count, and run once
    per scan where the page used to stat per load.

    * `mtime`, `nlink` — one `os.stat` of the walked path (C9, C6). Not a field
      on `inode_map`, which every file would pay for (`_walk_directory`'s
      `oldest_mtime` note). A failed stat leaves both absent, and absence reads
      as the alarming state downstream (`app._cleanup_state`).
    * `other_paths` — the inode's *other* torrent-tree paths, relative and posix
      (C5). `_assemble_records` emits one record per inode, and it used to keep
      only the first path: a distinct-hardlink cross-seed whose registrations
      had both gone listed one file, and the second could never be cleaned up
      through the workflow at all. An excluded sibling path is not listed — it
      is not the user's to delete — and `nlink` then counts it as a link that
      survives.
    * `unverified` — see `unverified_spec`.
    """
    paths = info.get('torrent_paths') or []
    if paths:
        try:
            st = os.stat(paths[0])
            record["mtime"] = int(st.st_mtime)
            record["nlink"] = int(st.st_nlink)
        except (OSError, ValueError, OverflowError):
            pass
    base = _torrent_tree_base(info)
    others = []
    for p in paths[1:]:
        if base is None or not p.startswith(base):
            continue
        rel = p[len(base):].replace('\\', '/')
        if compiled_exclusions is not None and \
                compiled_exclusions.match(p, rel, os.path.basename(p)):
            continue
        others.append(rel)
    if others:
        record["other_paths"] = others
    if unverified and (unverified.get('all') or any(
            _path_under(_norm_abs(p), root) for p in paths for root in unverified['roots'])):
        record["unverified"] = True


def _extra_torrent_paths(inode_map):
    """Every torrent-tree path that is *not* on a record: `(rel posix | None, orphan)`.

    A record carries its inode's first walked path only, so a live
    distinct-hardlink cross-seed's second path sits under some folder while
    appearing on no record at all. Lazy on purpose — the exclusivity test
    consumes it path by path and accumulates nothing. `None` means a path could
    not be placed relative to the torrent tree (not reachable by construction);
    the consumer then refuses every folder rather than guess.
    """
    for info in inode_map.values():
        paths = info.get('torrent_paths') or []
        if len(paths) < 2 or info.get('torrent_rel_path') is None:
            continue
        base = _torrent_tree_base(info)
        orphan = info.get('status') == 'Orphaned'
        for p in paths[1:]:
            if base is None or not p.startswith(base):
                yield None, orphan
            else:
                yield p[len(base):].replace('\\', '/'), orphan


def _assemble_records(torrent_key_order, media_key_order, inode_map, duplicate_map,
                      compiled_exclusions=None, unverified=None):
    if unverified:
        # Normalised once here as well as in `unverified_spec`: every comparison
        # below is against `_norm_abs` paths, and a root spelled any other way
        # would silently mark nothing — the unsafe direction.
        unverified = {'all': bool(unverified.get('all')),
                      'roots': [_norm_abs(r) for r in (unverified.get('roots') or []) if r]}
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
        # Completion, written only when it is not "complete" — the same sparse
        # shape as dead_siblings above, and for the reason `seeding_time`
        # established: a field on every file record multiplies across every file
        # of every torrent and grows files_json, the known RAM hotspot. This is
        # deliberately NOT a fourth value of `status`, which is read by
        # _is_not_imported_torrent, _is_triage_relevant, count_triage_items, the
        # File Explorer filters and Cleanup — widening that vocabulary would
        # touch all of them.
        if info.get('incomplete'):
            record["incomplete"] = True
        elif info.get('completion_unknown'):
            record["completion_unknown"] = True
        if info['status'] == 'Orphaned' and not info['torrent_excluded']:
            _stamp_orphan(record, info, compiled_exclusions, unverified)
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
    _mark_whole_torrents(torrent_files_data, media_files_data)
    _mark_cleanup_folders(torrent_files_data, media_files_data,
                          extra_paths=_extra_torrent_paths(inode_map))
    return torrent_files_data, media_files_data


def _media_root_names(media_files_data):
    """Lower-cased names of the directories at the top of the media library.

    The C7 half of both folder-rule tests (Triage's `_mark_whole_torrents` and
    Cleanup's `_mark_cleanup_folders`): a one-segment folder sharing its name
    with one of these matches *both* walks, because `_matches_prefix` matches a
    prefix anywhere in the path. One computation, two consumers.
    """
    return {p.split('/', 1)[0].lower()
            for p in (str(m.get('path') or '').replace('\\', '/') for m in media_files_data)
            if '/' in p}


# Precedence when a candidate folder is refused for more than one reason: the
# most fundamental answer is the one the page shows.
_FOLDER_REFUSAL_RANK = {'unverified': 1, 'live_torrent': 2, 'not_established': 3, 'media_root': 4}


def _mark_cleanup_folders(torrent_files_data, media_files_data, extra_paths=()):
    """Stamp the folder a Cleanup exclusion rule may name (C16 + Cleanup's C7).

    Cleanup groups orphans at `dir_segs[:2]`, and a fully selected group can be
    excluded with one subtree rule. That rule used to be offered by **depth**
    (`loose` below two segments), and nothing tested whether anything *other*
    than orphans lived under the folder. So one stray orphan inside a live
    torrent's release folder — a sample a repack dropped — made a one-file group
    whose rule hid the live torrent (C16, TRIAGE T6's bug on the side Phase 5 did
    not fix). Depth also refused the reference box's one-segment release
    folders, torrents saved with no category directory.

    **The field name is Triage's, deliberately: `excl_folder`** — one name, one
    meaning, *the folder a rule for this row may name*. Orphans are never
    `_is_triage_relevant` (they carry no hash), so the two stamps cannot
    collide. The candidate is the group folder, and it is safe only when:

    1. **Exclusivity** — every *non-excluded* torrent-tree path under it belongs
       to an orphan. Tested over **paths**, not records: a live cross-seed's
       second hardlink sits on no record (`extra_paths`). An excluded record
       does not disqualify — a folder rule cannot hide what is hidden, which is
       what keeps a tombstone from blocking it (see `_mark_whole_torrents`). A
       live inode's extra path is treated as not excluded, since only its first
       path's flag is known. An `unverified` orphan disqualifies it too: it may
       be a live torrent's file.
    2. **Not a media-tree root name**, at one segment (C7).

    Phase 5 recorded that "an orphan has no torrent to test exclusivity
    against". True of Triage's hash-owned test and not of the property:
    exclusivity against the *folder* is testable here, where every path is
    visible — so ROADMAP's "small persisted set" of media-root names is not
    needed either, because the audit has the media list in hand.

    Positive evidence only. `excl_folder` when safe; otherwise `excl_refused`
    (`media_root` | `live_torrent` | `unverified` | `not_established`) so the
    page can say why. **Absent both, the page falls back to per-file rules** —
    a database whose last audit predates these fields looks exactly like that.
    Written only on non-excluded orphans, and `O(paths × 2)` dict lookups.
    """
    candidates = {}
    for r in torrent_files_data:
        if r.get('status') != 'Orphaned' or r.get('excluded'):
            continue
        segs = str(r.get('path') or '').replace('\\', '/').split('/')[:-1]
        if segs:
            candidates.setdefault('/'.join(segs[:2]), None)
    if not candidates:
        return

    def refuse(path, reason):
        segs = str(path or '').replace('\\', '/').split('/')[:-1]
        for k in (1, 2):
            if len(segs) < k:
                break
            folder = '/'.join(segs[:k])
            if folder in candidates:
                cur = candidates[folder]
                if cur is None or _FOLDER_REFUSAL_RANK[reason] > _FOLDER_REFUSAL_RANK[cur]:
                    candidates[folder] = reason

    for r in torrent_files_data:
        if r.get('excluded'):
            continue
        if r.get('status') != 'Orphaned':
            refuse(r.get('path'), 'live_torrent')
        elif r.get('unverified'):
            for p in [r.get('path')] + list(r.get('other_paths') or []):
                refuse(p, 'unverified')
    for rel, orphan in extra_paths:
        if rel is None:
            for folder in candidates:
                if candidates[folder] is None:
                    candidates[folder] = 'not_established'
            break
        if not orphan:
            refuse(rel, 'live_torrent')

    media_roots = _media_root_names(media_files_data)
    for folder in candidates:
        if '/' not in folder and folder.lower() in media_roots:
            candidates[folder] = 'media_root'

    for r in torrent_files_data:
        if r.get('status') != 'Orphaned' or r.get('excluded'):
            continue
        segs = str(r.get('path') or '').replace('\\', '/').split('/')[:-1]
        if not segs:
            continue
        folder = '/'.join(segs[:2])
        reason = candidates.get(folder)
        if reason is None:
            r["excl_folder"] = folder
        else:
            r["excl_refused"] = reason


def _stamp_torrent_files(row_records, files_of):
    """`torrent_files` — how many files a torrent has, where its Triage row shows fewer (T5).

    A Triage row lists a subset: a partially imported torrent contributes only
    its not-imported files, and an excluded file never appears. The delete
    beside the row removes the whole torrent, so the row says "10 of 18 files".
    Counted here because only the audit sees every record of a hash — the
    compact `triage` row does not carry the rest. The torrent's *size* comes
    live from verify instead (`sources.fetch_torrent_details`' `size`).

    **Sparse**: written only on row records whose torrent has more files than
    the row shows — a fraction of the Triage pile, which is itself a fraction
    of the library. "Files" is what the audit walked for that hash, so a path a
    healthier cross-seed claims is counted on that torrent, not this one.
    """
    not_imported, dead_seed = {}, {}
    for r in row_records:
        bucket = not_imported if _is_not_imported_torrent(r) else dead_seed
        bucket[r['hash']] = bucket.get(r['hash'], 0) + 1
    for r in row_records:
        h = r['hash']
        shown = not_imported.get(h) or dead_seed.get(h, 0)
        if files_of.get(h, 0) > shown:
            r['torrent_files'] = files_of[h]


def _mark_whole_torrents(torrent_files_data, media_files_data):
    """Stamp the two facts Triage needs to build a folder exclusion safely (T6).

    `whole_torrent` — the torrent is *homogeneous*, every file in the same
    imported state, so the Triage row covers all of it. A partially-imported
    torrent contributes only its not-imported files to Triage, so from there it
    looks whole, while its common folder is the release folder that also holds
    the imported ones. Excluding that folder drops files from the walk that were
    never the problem, and they leave the health score with them.

    `excl_folder` — the folder that is actually safe to exclude as a subtree, or
    absent. **This replaced a "≥2 path segments" rule, which was a proxy for the
    real question and measurably the wrong one.** The reference install has
    torrents saved with no category directory at all, so their release folder
    sits one segment deep; the depth rule refused it and fell back to nine
    per-file rules of ~205 characters each — which the 200-character config cap
    then refused, while the confirm dialog told the user to select the release
    folder, the very thing the rule had just declined to do. A dead end, found
    by `.internal/probe_m_remaining.py --only phase5` on real data.

    The two properties the depth rule was standing in for, now tested directly:

    1. **Exclusivity** — nothing outside this torrent lives under the folder.
       That is what stops `movies/` (a category dir holding many unrelated
       torrents) being offered, and it holds whatever the depth is.
    2. **Not a media-tree root name** — the C7 half. A one-segment folder that
       shares its name with a directory at the top of the media library matches
       *both* walks, because `_matches_prefix` matches a prefix anywhere in the
       path. Deeper folders cannot collide that way, so the test only applies at
       one segment. The residual §0.5 records stays as recorded and accepted: an
       install whose library folders carry the release name (no arr rename) can
       still be matched by a release-folder pattern.

    Three deliberate choices:

    - **Positive evidence, not a "partial" flag.** An absent stamp means "not
      established" and falls back to per-file exact rules. Absence must never
      read as "safe" — that is R1 one layer up, and a database whose last audit
      predates these fields has exactly that absence.
    - **Written only on records Triage can act on** (`_is_triage_relevant`, the
      same subset the compact row keeps). A field on every torrent-file record
      multiplies across every file of every torrent and grows `files_json` —
      the rule `seeding_time`, `dead_siblings` and `incomplete` all follow.
    - **Path collection is bounded by the Triage pile, not by the library.**
      Pass 1 accumulates only booleans; only hashes that survive it collect
      their paths. The exclusivity walk that follows is dict lookups with no
      accumulation, so this stays O(files x depth) in time and O(pile) in space
      on a library where the pile is a fraction of a percent of the records.
    """
    # Pass 1 — homogeneity, which hashes Triage can act on at all, and how many
    # files each torrent has against how many its Triage row will show.
    imported_states, relevant = {}, set()
    files_of, row_records = {}, []
    for r in torrent_files_data:
        h = r.get('hash')
        if not h:
            continue
        imported_states.setdefault(h, set()).add(bool(r.get('imported')))
        files_of[h] = files_of.get(h, 0) + 1
        if _is_triage_relevant(r):
            relevant.add(h)
            if _is_not_imported_torrent(r) or _is_dead_seed_torrent(r):
                row_records.append(r)
    _stamp_torrent_files(row_records, files_of)
    del row_records, files_of
    whole = {h for h in relevant if len(imported_states[h]) == 1}
    if not whole:
        return

    # Pass 2 — the candidate folder per whole hash: its files' deepest common
    # directory. Only these hashes' paths are held, which is what bounds this.
    segs_by_hash = {}
    for r in torrent_files_data:
        h = r.get('hash')
        if h in whole:
            segs_by_hash.setdefault(h, []).append(
                str(r.get('path') or '').replace('\\', '/').split('/')[:-1])
    candidates = {}                       # folder -> owning hash, or None if shared
    for h, seg_lists in segs_by_hash.items():
        if any(not s for s in seg_lists):     # a file at the torrent-tree root
            continue
        common = seg_lists[0]
        for segs in seg_lists[1:]:
            i = 0
            while i < len(common) and i < len(segs) and common[i] == segs[i]:
                i += 1
            common = common[:i]
        if not common:
            continue
        folder = '/'.join(common)
        if folder in candidates and candidates[folder] != h:
            candidates[folder] = None
        else:
            candidates.setdefault(folder, h)
    del segs_by_hash

    # Pass 3 — exclusivity. One walk of every record, dict lookups only: any
    # candidate folder with a file from another torrent under it is disqualified.
    #
    # An already-excluded file does not disqualify anything: a folder rule cannot
    # hide what is hidden. That matters more than it sounds — a filesystem
    # tombstone left in a release folder by an interrupted move is excluded (see
    # media_server_exclusions.TOMBSTONE_PATTERNS) and would otherwise block the
    # real torrent's folder-level exclusion for as long as the handle stayed open.
    for r in torrent_files_data:
        if r.get('excluded'):
            continue
        h = r.get('hash')
        segs = str(r.get('path') or '').replace('\\', '/').split('/')[:-1]
        for i in range(1, len(segs) + 1):
            folder = '/'.join(segs[:i])
            owner = candidates.get(folder)
            if owner is not None and owner != h:
                candidates[folder] = None

    # Pass 4 — the C7 half, which only bites at one segment.
    media_roots = _media_root_names(media_files_data)
    safe = {}
    for folder, owner in candidates.items():
        if owner is None:
            continue
        if '/' not in folder and folder.lower() in media_roots:
            continue
        safe[owner] = folder

    for r in torrent_files_data:
        h = r.get('hash')
        if h in whole and _is_triage_relevant(r):
            r["whole_torrent"] = True
            if h in safe:
                r["excl_folder"] = safe[h]


# ---------------------------------------------------------------------------
# Health metrics
# ---------------------------------------------------------------------------

# Filename markers for a 2160p release. Substring tests, not a regex: this runs
# once per media file on libraries that reach eight figures, and the cheapest
# thing that is right most of the time wins. A folder called "4K Movies" marks
# everything beneath it, which is a false positive only in the sense that it is
# probably true.
_UHD_MARKERS = ('2160p', 'uhd', '4k')


def _library_shape(scoring_media):
    """Two 'what you have' facts that are not the library's size.

    * `title_count` — distinct release folders, at the same two-segment depth
      Cleanup groups orphans at (a category dir is never a title on its own).
    * `uhd_bytes` — bytes held at 2160p, the one library-shape number a
      workflow can move without buying a disk (Backfill and Trumped both
      raise it in place).

    Titles are counted into a set of *hashes* rather than strings: exact enough
    for a useless prize, and a set of ints on a 100k-title library is a few MB
    against the tens it would otherwise hold in path strings.
    """
    title_keys = set()
    uhd_bytes  = 0
    for f in scoring_media:
        path = f.get('path') or ''
        if not path:
            continue
        parts = path.replace('\\', '/').split('/')
        title_keys.add(hash('/'.join(parts[:2]) if len(parts) > 1 else parts[0]))
        low = path.lower()
        for marker in _UHD_MARKERS:
            if marker in low:
                uhd_bytes += f.get('size', 0) or 0
                break
    return {'title_count': len(title_keys), 'uhd_bytes': uhd_bytes}


def process_health_metrics(media_files, torrent_files, cfg, update_history=True,
                           extra_details=None):
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
    # Read off the unfiltered list: dead cross-seed registrations hang off
    # orphaned and imported records too, which the scoring subset still carries.
    triage_counts = count_triage_items(torrent_files)
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
            # Cleanup's Excluded box. Counted here rather than carried on the
            # compact `cleanup` row, which holds only what the page acts on —
            # see app._cleanup_records. Per record, i.e. per inode.
            "orphaned_excluded_count": sum(1 for f in torrent_files
                                           if f['status'] == 'Orphaned' and f.get('excluded')),
            "not_imported_count": sum(1 for f in scoring_torrents if not f['imported'] and f['status'] != 'Orphaned'),
            "dead_seed_count": sum(1 for f in scoring_torrents
                                   if f['imported'] and f['status'] != 'Orphaned'
                                   and f.get('tracker_health') == 'unregistered'),
            # Per-torrent Triage row counts (the counts above are per file).
            # The sidebar badge and the Next steps Triage card read this so
            # they agree with the page — see audit.count_triage_items.
            "triage_counts": triage_counts,
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
            # Library shape, for the Next steps prize layer. Scalars, folded into
            # a pass over scoring_media that already runs.
            **_library_shape(scoring_media),
            # Seeding time (from the source layer) and oldest media file (from
            # the walk). Passed in rather than computed here — neither is
            # derivable from the assembled file records.
            **(extra_details or {}),
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
# Excluded files are walked and stored like any other, but never scored. The bit
# records the exclusion state *as of that scan*, which is what lets
# `count_pile_resolved` tell "deleted a file that was on the pile" from "deleted
# a sidecar that never counted" — and immunises it against exclusion churn.
SIG_EXCLUDED = 8
# On the Triage pile in its own right: not-imported, or an imported dead seed.
# Needed because "not imported" alone no longer describes the pile — a dead seed
# is imported and still sits in Triage, so without this bit `count_pile_resolved`
# could not see one leave. Carriers of dead cross-seed registrations deliberately
# do NOT set this: those are counted by hash (several can ride one record, and
# the record itself is a healthy file that never leaves).
SIG_ON_PILE = 16


def file_signatures(files):
    """Build the compact {path: bitmask} diff signature for a file list."""
    return {
        f['path']: (
            (SIG_ORPHANED if f.get('status') == 'Orphaned' else 0)
            | (SIG_IMPORTED if f.get('imported') else 0)
            | (SIG_HAS_DUPS if f.get('duplicate_paths') else 0)
            | (SIG_EXCLUDED if f.get('excluded') else 0)
            | (SIG_ON_PILE if _is_pile_item(f) else 0)
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


def count_pile_resolved(prev_torrent_sigs, torrent_files, prev_dead_regs=None):
    """Count items that genuinely left the Triage pile since the last scan.

    This is the Next steps shovel counter (`ns_progress['shoveled']`), and it
    counts *transitions*, not a drop in `not_imported_count`. The count-delta it
    replaces was gated on an exclusion fingerprint — any change to the exclusion
    set voided the whole interval, so a Triage session that deleted five real
    files and clicked one suggestion chip credited nothing. Triage renders those
    chips next to the delete button, so the reward layer was reliably punishing
    the workflow the page encourages.

    Counting transitions needs no such guard. Excluding a file does not remove
    it from the walk, so it produces no transition at all and simply cannot be
    mistaken for work; nothing here has to be defended against exclusion churn.

    The pile is everything Triage lists, not just the not-imported files: a
    dead seed removed via the client and a dead cross-seed registration retired
    are both a successful triage, and shovel credit is cheap. Anything that
    leaves the pile counts.

    Two units, because the pile has two:
      - **file records** — not-imported files and imported dead seeds, tracked
        by path through `SIG_ON_PILE` on the previous scan's signature map.
      - **dead registrations** — tracked by hash (`prev_dead_regs`), because
        several ride a single healthy carrier record that never leaves the walk,
        so a path-based diff cannot see one of three go.

    Still NOT credited, because neither is a *successful* triage:
      - going orphaned — the torrent left but the file stayed. The item moved to
        Cleanup rather than being resolved, and it pays out there, on the orphan
        zombie ladder.
      - getting excluded — hidden, not dug. Triage renders its exclusion
        suggestion chips right beside the delete button, so this is the normal
        case, and paying for it would turn "add a pattern" into a points button.

    Counting transitions needs no exclusion guard beyond that: excluding a file
    leaves it in the walk, so it produces no phantom transition. This is why the
    count-delta version — gated on an exclusion fingerprint, which voided the
    *entire* interval whenever the exclusion set moved — is gone.

    Caps do not apply: the change log's lists stop at 50 entries, so a real
    clearout must be counted here, off the signature map, not off the diff.
    """
    resolved = 0

    # ── Dead registrations, by hash ──────────────────────────────────────────
    if prev_dead_regs:
        resolved += len(set(prev_dead_regs) - dead_registration_hashes(torrent_files))

    # ── File records, by path ────────────────────────────────────────────────
    if not prev_torrent_sigs:
        return resolved
    # `SIG_ON_PILE or not SIG_IMPORTED` reads both new and legacy signature rows:
    # scans written before the bit existed still identify their not-imported
    # files correctly, so an upgrade costs at most the dead seeds of one interval
    # rather than voiding it.
    on_pile = {
        path for path, sig in prev_torrent_sigs.items()
        if not (sig & (SIG_ORPHANED | SIG_EXCLUDED))
        and (sig & SIG_ON_PILE or not sig & SIG_IMPORTED)
    }
    if not on_pile:
        return resolved
    still = 0
    for f in torrent_files:
        if f['path'] not in on_pile:
            continue
        # Either still sitting on the pile, or it left by a route that isn't a
        # success (see the carve-outs above).
        if _is_pile_item(f) or f.get('status') == 'Orphaned' or f.get('excluded'):
            still += 1
    return resolved + len(on_pile) - still


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
    """A torrent file that ought to be in the library and is not (TRIAGE T4).

    An **unfinished download is not one of these**, and used to be. A torrent at
    0% is not-imported by definition — the arr cannot import what does not exist
    yet — so a brand-new grab was the newest thing in the client and also a
    Triage row at its full final size, verdict `not_in_library`, with a delete
    button under copy reading "junk can be deleted". `status` could not rescue
    it either: a **paused** incomplete reads 'Paused', exactly like a paused
    complete one, which is why this tests the completion flag and not the state
    string.

    `completion_unknown` is deliberately *not* excluded here. Where the client
    exposed no usable completion field the row stays visible carrying its
    status, because auditorr never hides anything silently (T3/T15's rule) — the
    honest failure is a row you can see and judge, not a row that vanished.
    """
    return (
        not f.get('excluded')
        and not f.get('imported')
        and f.get('status') != 'Orphaned'
        and not f.get('incomplete')
    )


def _is_triage_relevant(f):
    """Torrent-file records the Triage workflow reads: not-imported files,
    imported dead seeds, and carriers of dead cross-seed registrations.

    Persisted as the compact 'triage' file_results row at save time so the
    Triage page never deserializes the full torrent list (a few hundred MB of
    object graph on large libraries) for the ~2% of records it acts on.
    Must stay in lockstep with the filters in app.workflows_triage — the rule
    exists in three places (here, `_is_not_imported_torrent` above and
    `count_triage_items` below) and when they disagree the page, the stored
    subset and the sidebar badge each report a different number.
    """
    if f.get('excluded'):
        return False
    if f.get('dead_siblings'):
        return True
    if f.get('status') == 'Orphaned':
        return False
    if not f.get('imported'):
        return not f.get('incomplete')
    return f.get('tracker_health') == 'unregistered'


def _is_cleanup_relevant(f):
    """Torrent-file records the Cleanup workflow reads: non-excluded orphans.

    Persisted as the compact 'cleanup' file_results row, the way 'triage' is, so
    neither the Cleanup page nor its delete script deserializes the full torrent
    list for the ~1% of records it acts on (CLEANUP C10). **Must stay in lockstep
    with the details' `orphaned_torrent_count`** (orphans among the non-excluded
    records, in `process_health_metrics`) and with `app._cleanup_records`'
    fallback, which applies this same predicate to the full list — when they
    disagree, the page, the stored subset and the sidebar badge each report a
    different number. Excluded orphans are counted into the details
    (`orphaned_excluded_count`) rather than carried here.
    """
    return f.get('status') == 'Orphaned' and not f.get('excluded')


def _is_dead_seed_torrent(f):
    return (
        not f.get('excluded')
        and f.get('imported')
        and f.get('status') != 'Orphaned'
        and f.get('tracker_health') == 'unregistered'
    )


def _is_pile_item(f):
    """On the Triage pile as a file record — the shovel counter's unit.

    Dead cross-seed registrations are also on the pile but are not file records:
    several can ride a single healthy carrier, so they are counted by hash in
    `dead_registration_hashes` instead.
    """
    return _is_not_imported_torrent(f) or _is_dead_seed_torrent(f)


def dead_registration_hashes(torrent_files):
    """Hashes of every dead cross-seed registration still riding a walked record.

    Deliberately does NOT skip excluded carriers, unlike `count_triage_items`,
    which answers a different question ("what does Triage list", where exclusion
    is a hide). Here the set is diffed scan-over-scan to award shovel credit, so
    dropping excluded carriers would make "add an exclusion pattern" read as
    "retired those registrations" and pay out for hiding — the one thing the
    counter's carve-outs exist to prevent. A hash leaves this set when the
    registration is actually gone from the client, not when it stops being
    displayed.
    """
    out = set()
    for f in torrent_files:
        for s in (f.get('dead_siblings') or []):
            if s.get('hash'):
                out.add(s['hash'])
    return out


def count_triage_items(torrent_files):
    """How many rows the Triage page will list — per torrent, not per file.

    The sidebar badge and the Next steps card both read this, so it has to
    reproduce app.workflows_triage's grouping exactly: not-imported torrents
    and imported dead seeds collapse to one row per hash (a season pack is one
    row, not twenty), and every dead cross-seed registration stashed in
    `dead_siblings` is its own removable row. Counting not-imported *files*
    instead — what the badge used to do — overcounted multi-file torrents and
    missed dead registrations entirely.

    Live re-verification can still drop rows that recovered, so this is the
    audit-time figure: what the page renders before /triage/verify lands.
    """
    not_imported, dead_seeds, dead_reg = set(), set(), set()
    for f in torrent_files:
        if f.get('excluded'):
            continue
        # Dead siblings ride on any record, orphaned and imported ones included.
        for s in (f.get('dead_siblings') or []):
            if s.get('hash'):
                dead_reg.add(s['hash'])
        if _is_not_imported_torrent(f):
            not_imported.add(f.get('hash') or f['path'])
        elif _is_dead_seed_torrent(f):
            dead_seeds.add(f.get('hash') or f['path'])
    # A partially-imported torrent is triaged as not-imported, not as a dead
    # seed, and a hash already listed in its own right is not also a sibling row.
    dead_seeds -= not_imported
    dead_reg   -= (not_imported | dead_seeds)
    return {
        'not_imported':      len(not_imported),
        'dead_seeds':        len(dead_seeds),
        'dead_registrations': len(dead_reg),
        'total':             len(not_imported) + len(dead_seeds) + len(dead_reg),
    }


def _not_imported_paths(torrent_files):
    return [f['path'] for f in torrent_files if _is_not_imported_torrent(f)]


# ---------------------------------------------------------------------------
# Main audit process
# ---------------------------------------------------------------------------

def _save_error_status(message):
    curr = db_load_results()
    curr["status"] = message
    db_save_results(curr)


# ---------------------------------------------------------------------------
# Source plausibility guard
# ---------------------------------------------------------------------------

# "Orphaned" is not a property of a file. It is the *absence of evidence* in one
# torrent-client API snapshot, joined to the filesystem by string equality of
# absolute paths, and acted on later by a bash script with no access to the
# client. Everything below exists because absence of evidence arrives by several
# routes that look identical from here — a client still loading its session, a
# rebuilt container with an empty session directory, a qui instance that did not
# answer, a WebUI that timed out on half the library — and every one of them
# ends with the whole torrent tree rendered in Cleanup, in green, under one
# `Select all`. Nothing else in auditorr stands between that and `rm`.

# Below this many torrents the proportional rules are noise: a four-torrent
# client dropping to one is a 75% collapse and also a completely ordinary
# Tuesday.
_GUARD_MIN_BASELINE = 25
# A scan that lost more than half of either count against the last scan that
# persisted. Deliberately blunt — the rule has to be explicable in the sentence
# the user is shown.
_GUARD_DROP_FRACTION = 0.5
# Torrents whose file listing failed *and* whose payload could not be found on
# disk. These are the ones with no evidence either way.
_GUARD_UNRESOLVED_FRACTION = 0.25
# S02, the user's decision 2 (a) in Phase 12 (2026-09-15). A collapse is measured
# against the largest count persisted in this many days, not only against the
# last scan that persisted — or a client losing torrents in instalments, or a
# pruning script run twice, passes each time: 100 → 60 → 36 is two 40% drops.
_GUARD_REFERENCE_DAYS = 7
_REFERENCE_FIELDS = ('torrent_count', 'file_map_size', 'torrent_files', 'media_files')

# Decision 1 (a): what a manual scan may accept. A change in what the client or
# the disk holds can be real — a library really does shrink — and accepting it
# stays one click away. Every other code is a read that failed, and asking for a
# scan is not authority to believe one: those refuse on every trigger.
_ACCEPTABLE_BY_HAND = frozenset({
    'torrent_count_collapse', 'file_map_collapse', 'client_blackout', 'disk_collapse',
})


def _pct(part, whole):
    return int(round(part * 100.0 / whole)) if whole else 0


def source_plausibility(report, baseline, disk_file_count=None):
    """Is this scan's view of the client trustworthy enough to act on?

    Returns None when it is, or {'code', 'message', 'detail'} when it is not.
    `disk_file_count` is the number of files the torrent-tree walk found, and is
    only needed for the blackout rule; pass None to run the rules that do not
    need the walk (so a hopeless scan can bail before paying for it).

    The three rules, in the order they are cheapest to evaluate:

      collapse   the client answered, but with far fewer torrents or files than
                 `baseline` — the reference, the largest counts persisted in
                 the last `_GUARD_REFERENCE_DAYS` days (`reference_counts`)
      blind      too much of the client could not be asked at all
      blackout   the client claims nothing while the disk holds files

    `blackout` deliberately needs **no baseline**, because the worst case has
    none: a first-ever scan that lands while qBittorrent is still loading its
    session has nothing to compare against, and "compare against the previous
    scan" would wave it straight through.
    """
    torrents = int(report.get('torrent_count') or 0)
    mapped   = int(report.get('file_map_size') or 0)
    failed_instances = report.get('instances_failed') or []

    prev_torrents = int((baseline or {}).get('torrent_count') or 0)
    prev_mapped   = int((baseline or {}).get('file_map_size') or 0)

    if failed_instances:
        names = ', '.join(f.get('name', '?') for f in failed_instances[:3])
        return {
            'code': 'instances_unavailable',
            'message': (f"{len(failed_instances)} of {report.get('instances_total', '?')} "
                        f"torrent-client instance(s) did not answer, or listed only part of "
                        f"their torrents ({names}). Every torrent missing from the answer "
                        f"would have been classified as orphaned."),
            'detail': {'instances_failed': failed_instances},
        }

    if prev_torrents >= _GUARD_MIN_BASELINE and \
            torrents < prev_torrents * (1 - _GUARD_DROP_FRACTION):
        return {
            'code': 'torrent_count_collapse',
            'message': (f"The torrent client reported {torrents} torrent(s), down from "
                        f"{prev_torrents} (the most in the last {_GUARD_REFERENCE_DAYS} days) — "
                        f"a {_pct(prev_torrents - torrents, prev_torrents)}% drop."),
            'detail': {'torrent_count': torrents, 'previous': prev_torrents},
        }

    if prev_mapped >= _GUARD_MIN_BASELINE and \
            mapped < prev_mapped * (1 - _GUARD_DROP_FRACTION):
        return {
            'code': 'file_map_collapse',
            'message': (f"The torrent client accounted for {mapped} file(s), down from "
                        f"{prev_mapped} (the most in the last {_GUARD_REFERENCE_DAYS} days) — "
                        f"a {_pct(prev_mapped - mapped, prev_mapped)}% drop."),
            'detail': {'file_map_size': mapped, 'previous': prev_mapped},
        }

    unresolved = int(report.get('listing_unresolved') or 0)
    if torrents and unresolved > torrents * _GUARD_UNRESOLVED_FRACTION:
        return {
            'code': 'listings_unavailable',
            'message': (f"{unresolved} of {torrents} torrent(s) ({_pct(unresolved, torrents)}%) "
                        f"would not report their files, and their payload could not be found on "
                        f"disk either. There is no evidence either way about those files."),
            'detail': {'listing_unresolved': unresolved, 'torrent_count': torrents},
        }

    if disk_file_count and mapped == 0:
        return {
            'code': 'client_blackout',
            'message': (f"The torrent client accounted for no files at all, while the torrent "
                        f"directory holds {disk_file_count}. Every one of them would have been "
                        f"classified as orphaned."),
            'detail': {'disk_file_count': disk_file_count, 'torrent_count': torrents},
        }

    return None


def _reference_cutoff(now=None):
    return ((now or datetime.now()) - timedelta(days=_GUARD_REFERENCE_DAYS)).date().isoformat()


def reference_counts(points, baseline, now=None):
    """The counts a scan is measured against (S02, decision 2 (a)).

    The largest of each field persisted in the last `_GUARD_REFERENCE_DAYS`
    days, and the last persisted `baseline`. The baseline is folded in because
    every install upgrading into this has one and no points yet, and its first
    scan must not be waved through for want of them. A missing field reads 0,
    which no rule measures against.
    """
    cutoff = _reference_cutoff(now)
    ref = {f: int((baseline or {}).get(f) or 0) for f in _REFERENCE_FIELDS}
    for point in points or []:
        if str(point.get('day') or '') >= cutoff:
            for f in _REFERENCE_FIELDS:
                ref[f] = max(ref[f], int(point.get(f) or 0))
    return ref


def advance_reference(points, counts, now=None, reset=False):
    """`points` with a persisted scan's `counts` folded in.

    One point a day holding that day's largest counts, pruned to the window, so
    the stored row is at most `_GUARD_REFERENCE_DAYS + 1` small dicts — never
    per-file data. `reset`: a manual scan accepted a change, and the window
    restarts from this scan, or the next scheduled scan would refuse the same
    drop again.
    """
    day = counts.get('day') or (now or datetime.now()).date().isoformat()
    cutoff = _reference_cutoff(now)
    earlier = [] if reset else [p for p in points or [] if str(p.get('day') or '') >= cutoff]
    today = {'day': day}
    for f in _REFERENCE_FIELDS:
        today[f] = max([int(counts.get(f) or 0)] +
                       [int(p.get(f) or 0) for p in earlier if p.get('day') == day])
    return sorted([p for p in earlier if p.get('day') != day] + [today], key=lambda p: p['day'])


def _root_state(path):
    """A root before its walk: configured, and a directory. No walk counts yet."""
    return {'configured': bool(path), 'exists': bool(path) and os.path.isdir(path)}


def filesystem_plausibility(root, block, reference, file_map_size=0):
    """S03 — can one root's walk be believed? None, or an anomaly shaped like
    `source_plausibility`'s.

    `root` is 'torrents' or 'media', and is all that is said about where: the
    anomaly is persisted and reaches /api/debug/report, so it carries a root's
    name and counts, never its path.

      root_missing     a configured root that is not a directory inside the
                       container — walked, it read as an empty tree
      root_unlistable  a directory within `_UNLISTABLE_SHALLOW_DEPTH` segments of
                       the root could not be listed: at a category or release
                       depth it hides whole releases. Deeper ones are counted on
                       the report and in the status line, not refused
      disk_collapse    the walk found far fewer files than the reference; or the
                       torrent folder is empty while the client accounts for
                       files in it, which needs no baseline for the reason
                       `client_blackout` needs none — the worst case, a first
                       scan landing on an empty bind mount, has none

    A block with no walk counts yet (the check before the walks) runs only the
    first rule. An unconfigured root is not a missing one.
    """
    block = block or {}
    label = 'torrent' if root == 'torrents' else 'media'
    if block.get('configured') and not block.get('exists'):
        return {
            'code': 'root_missing',
            'message': (f"The {label} folder is not there — it does not exist inside the "
                        f"container, or is not a folder. Walked, it would have read as empty."),
            'detail': {'root': root},
        }
    if 'files' not in block:
        return None
    if block.get('unlistable_shallow'):
        return {
            'code': 'root_unlistable',
            'message': (f"{block['unlistable_shallow']} folder(s) at a category or release "
                        f"level in the {label} folder could not be listed. Everything under "
                        f"them would have been missing from the scan."),
            'detail': {'root': root, 'unlistable': block.get('unlistable', 0),
                       'unlistable_shallow': block['unlistable_shallow'],
                       'files': block.get('files', 0)},
        }
    files = int(block.get('files') or 0)
    ref = int((reference or {}).get('torrent_files' if root == 'torrents' else 'media_files') or 0)
    if ref >= _GUARD_MIN_BASELINE and files < ref * (1 - _GUARD_DROP_FRACTION):
        return {
            'code': 'disk_collapse',
            'message': (f"The {label} folder holds {files} file(s), down from {ref} (the most "
                        f"in the last {_GUARD_REFERENCE_DAYS} days) — a {_pct(ref - files, ref)}% drop."),
            'detail': {'root': root, 'files': files, 'previous': ref},
        }
    if root == 'torrents' and files == 0 and file_map_size >= _GUARD_MIN_BASELINE:
        return {
            'code': 'disk_collapse',
            'message': (f"The torrent folder is empty, while the torrent client accounts for "
                        f"{file_map_size} file(s) in it."),
            'detail': {'root': root, 'files': 0, 'file_map_size': file_map_size},
        }
    return None


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


class _SourceAnomaly(Exception):
    """The client's answer is not trustworthy enough to classify orphans from.

    Carried as an exception purely so the happy path stays linear — it is not an
    error in the sense the other handlers mean. The scan exits *normally*, which
    matters: `run_audit_process`'s `finally` clears `scan_marker`, and a marker
    left behind is read at the next boot as a process killed mid-scan and counts
    toward `consecutive_aborted_scans`. At two, automatic scanning stops. A
    safety guard that disabled scanning would be a worse bug than the one it
    guards against.
    """

    def __init__(self, anomaly):
        super().__init__(anomaly['message'])
        self.anomaly = anomaly


def _accept_or_refuse(anomaly, trigger):
    """Raise `_SourceAnomaly` unless there is none, or a manual scan may accept it."""
    if not anomaly:
        return None
    if trigger != 'manual' or anomaly['code'] not in _ACCEPTABLE_BY_HAND:
        raise _SourceAnomaly(anomaly)
    log.warning("Source anomaly on a manual scan, accepted as a real change: %s",
                anomaly['message'])
    return anomaly


def _guard_scan(report, baseline, trigger, disk_file_count=None):
    """Raise `_SourceAnomaly` if this scan must not persist; else return an
    anomaly a manual scan accepted, or None.

    The user's decision 1 (a) in Phase 12 (2026-09-15). A manual scan used to be
    the override for every rule, on the watchdog's precedent that explicit intent
    wins — a failed instance included, so Triage, Backfill, the health score and
    the change log read a scan missing a whole instance as the truth (S02). It
    still accepts a change in what the client or the disk holds
    (`_ACCEPTABLE_BY_HAND`), and never a read that failed: an instance that did
    not answer or listed short, listings that could not be read, a root that is
    missing or could not be listed. Startup accepts nothing — a startup scan
    following a container rebuild is exactly the case the guard exists for.
    """
    return _accept_or_refuse(source_plausibility(report, baseline, disk_file_count), trigger)


def _guard_filesystem(root, block, reference, trigger, file_map_size=0):
    """`_guard_scan` for one root's walk (S03), with the same exit."""
    return _accept_or_refuse(
        filesystem_plausibility(root, block, reference, file_map_size), trigger)


# What a refusal tells the user to do. A change a manual scan may accept says so;
# a failed read says what to fix instead, because a manual scan will not accept
# it (decision 1 (a)) and "run a scan manually" would not help.
_ACCEPT_BY_HAND_TEXT = "If this is expected, run a scan manually to accept it."
_ANOMALY_FIXES = {
    'instances_unavailable': ("Check that every qui instance is connected and answering, then "
                              "scan again. A manual scan will not accept a listing that failed."),
    'listings_unavailable':  ("Check the torrent path mapping (Remote and Local torrent path) and "
                              "that the client is answering, then scan again. A manual scan will "
                              "not accept listings that failed."),
    'root_missing':          ("Check that the folder is mounted into the container — an array or a "
                              "network share that has not finished mounting looks like this — then "
                              "scan again. A manual scan will not accept a missing folder."),
    'root_unlistable':       ("Check that the user auditorr runs as can read the folder, then scan "
                              "again. A manual scan will not accept a folder it could not list."),
}


def _record_source_anomaly(anomaly, trigger, cfg, scan_start, persist=True):
    """Report a refused scan and leave every stored figure as it was."""
    fix = (_ACCEPT_BY_HAND_TEXT if anomaly['code'] in _ACCEPTABLE_BY_HAND
           else _ANOMALY_FIXES.get(anomaly['code'], "Fix the cause, then scan again."))
    msg = (f"Source anomaly: {anomaly['message']} Nothing from this scan was saved — "
           f"the file lists, health score and change log still describe the last "
           f"scan that completed. {fix}")
    log.warning(msg)
    try:
        db_set_meta('last_source_anomaly', {
            'at':      datetime.now().isoformat(timespec='seconds'),
            'trigger': trigger,
            'code':    anomaly['code'],
            'message': anomaly['message'],
            'detail':  anomaly.get('detail') or {},
        })
    except Exception as e:
        log.warning(f"Could not record source anomaly: {e}")
    if persist:
        _save_error_status(msg)
        db_save_audit(trigger, None, 'anomaly', msg, {},
                      source=cfg.get('TORRENT_SOURCE', 'qbit'),
                      duration_seconds=round(time.time() - scan_start, 1),
                      peak_rss_mb=_scan_peak_rss())
    # The process is healthy — it declined to write, it did not die. Leaving the
    # crash-loop streak standing would let a run of anomalies trip the breaker.
    try:
        db_set_meta('consecutive_aborted_scans', 0)
    except Exception:
        pass
    # The code rides the state so the startup retry can tell a missing root
    # (minutes to mount) from a client still loading its session (seconds).
    set_state(status_message=msg, last_scan_status="error", anomaly_code=anomaly['code'])


def run_audit_process(trigger=None, persist_source_errors=True):
    cfg = db_load_config()
    # Accept trigger as parameter so callers can pass it explicitly,
    # avoiding a race between set_state(trigger=...) and reading it back
    if trigger is None:
        trigger = get_state().get('trigger', 'manual')
    scan_start = time.time()
    set_state(is_scanning=True, progress=0, scanned_files=0, total_files=0,
              status_message="Connecting to torrent source...", last_scan_status="running",
              phase="connecting", phase_history=[], anomaly_code=None)
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
        # Save roots of listings that failed with nothing found on disk — in
        # memory only, never on the persisted report (see `unverified_spec`).
        unresolved_roots = []
        qbit_file_map, trackers, tracker_snapshot, source_report = sources.fetch_file_map(
            cfg, unresolved_roots=unresolved_roots)
        set_state(source_file_count=len(qbit_file_map))
        # Everything downstream treats "no client entry for this path" as proof
        # of orphanhood. Check the client's answer against the counts last
        # believed *before* paying for two full filesystem walks — the collapse
        # and blind rules need neither.
        source_baseline  = db_get_meta('source_baseline')
        reference_points = db_get_meta('source_reference')
        reference        = reference_counts(reference_points, source_baseline)
        # Anomalies a manual scan accepted. Any one restarts the reference window
        # from this scan once it persists (decision 2 (a)).
        accepted = [_guard_scan(source_report, reference, trigger)]
        # S03 — the filesystem half of R1. A root that is not there is free to
        # check, so both are checked before either walk: a hopeless scan pays for
        # neither, and a missing root never walks as an empty tree.
        filesystem = {'torrents': _root_state(cfg.get('LOCAL_PATH', '')),
                      'media':    _root_state(cfg.get('MEDIA_PATH', ''))}
        for root in ('torrents', 'media'):
            _guard_filesystem(root, filesystem[root], reference, trigger)
        total_ref = [0]
        set_state(total_files=0)
        _enter_phase("disk", "Scanning torrent directory...")
        inode_map          = {}
        exclusion_patterns = expand_exclusion_patterns(cfg)
        compiled_excl      = compile_exclusions(exclusion_patterns)
        torrent_key_order, scanned, torrent_errors, _ = _walk_directory(
            cfg.get('LOCAL_PATH',''), 'Torrent', inode_map, qbit_file_map, 0, 0,
            exclusion_patterns=exclusion_patterns, total_ref=total_ref,
            compiled_exclusions=compiled_excl, walk_report=filesystem['torrents'])
        # The blackout rule needs the disk side — "the client claims nothing
        # while LOCAL_PATH holds files" — so it runs at the first point that
        # number exists, and before the media walk, the assemble phase and every
        # write. On a manual scan this re-reports an anomaly the pre-walk call
        # already accepted; the second line carries the disk count. The torrent
        # root's own checks run here too, for the same reason.
        accepted.append(_guard_scan(source_report, reference, trigger,
                                    disk_file_count=len(torrent_key_order)))
        accepted.append(_guard_filesystem(
            'torrents', filesystem['torrents'], reference, trigger,
            file_map_size=int(source_report.get('file_map_size') or 0)))
        _enter_phase("disk", "Scanning media directory...")
        media_key_order, _, media_errors, oldest_media_mtime = _walk_directory(
            cfg.get('MEDIA_PATH',''), 'Media', inode_map, qbit_file_map, scanned, 0,
            exclusion_patterns=exclusion_patterns, total_ref=total_ref,
            compiled_exclusions=compiled_excl, walk_report=filesystem['media'])
        # The media root's checks: after its walk, before the assemble phase and
        # every write.
        accepted.append(_guard_filesystem('media', filesystem['media'], reference, trigger))
        # Counts and booleans per root, named — never a path: the report is
        # persisted and reaches /api/debug/report.
        source_report['filesystem'] = filesystem
        stat_errors = torrent_errors + media_errors
        # The source file map is only consulted during the walks — release it
        # (~300 MB at 650K files) before the memory-heavy assemble/save phases.
        del qbit_file_map
        _enter_phase("post", "Detecting duplicates...")
        duplicate_map = _build_duplicate_map(inode_map)
        _enter_phase("post", "Assembling file records...")
        _unverified = unverified_spec(source_report, unresolved_roots)
        torrent_files_data, media_files_data = _assemble_records(
            torrent_key_order, media_key_order, inode_map, duplicate_map,
            compiled_exclusions=compiled_excl, unverified=_unverified)
        del torrent_key_order, media_key_order, inode_map, duplicate_map
        if _unverified:
            log.warning("Audit: %d orphaned file(s) marked unverified — the client could not "
                        "be fully asked on this scan (%s)",
                        sum(1 for f in torrent_files_data if f.get('unverified')),
                        'an instance did not answer' if _unverified['all']
                        else f"{len(_unverified['roots'])} unresolved save path(s)")
        _enter_phase("post", "Computing health metrics...")
        # Prize-layer inputs that only exist outside the file records: seeding
        # time rides the source layer's torrent list, oldest_media_age_days the
        # walk's own stat calls. Age is stored, not the timestamp — the ladder
        # wants "how long have you had this", and a stored age cannot drift into
        # the future if the clock moves.
        _extra_details = {
            'seed_byte_secs': tracker_snapshot.get('_seed_byte_secs', 0),
            'max_seed_secs':  tracker_snapshot.get('_max_seed_secs', 0),
            'oldest_media_age_days': (
                max(0, int((time.time() - oldest_media_mtime) // 86400))
                if oldest_media_mtime else 0),
        }
        dashboard_stats    = process_health_metrics(media_files_data, torrent_files_data, cfg,
                                                    extra_details=_extra_details)
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
        # Shovel credit for this interval, counted off the same signature map
        # (see count_pile_resolved). Stays 0 if there is no previous scan to
        # compare against, or if this block fails — never a guess.
        ns_resolved = 0
        # Read once, here: the dead-registration half of the shovel count needs
        # the previous scan's hash set, and the progress pass below needs the
        # same record to latch against.
        _ns_prev = db_get_meta('ns_progress')
        try:
            prev_sigs = {
                'media':    db_load_file_signatures('media'),
                'torrents': db_load_file_signatures('torrents'),
            }
            ns_resolved = count_pile_resolved(
                prev_sigs['torrents'], torrent_files_data,
                prev_dead_regs=(_ns_prev or {}).get('last_dead_regs'))
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
        # Compact Cleanup working set, after the orphan stamps (C10): the page
        # and the delete script read this and never the full torrent list.
        db_save_file_results('cleanup',
                             [f for f in torrent_files_data if _is_cleanup_relevant(f)])
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
            _ns_det  = dashboard_stats['current']['details']
            # Built with the *previous* progress so prior latches still apply;
            # update_progress then unions in whatever was newly earned. This is
            # what makes the prize layer ratchet — points accrue for action and
            # nothing is ever deducted for inaction or regression.
            _ns_runs  = db_get_recent_runs(limit=2000)
            _ns_state = rounds.build_state(
                cfg, result, _ns_runs, progress=_ns_prev)
            # `runs` is read once ever, on the audit that first writes a
            # `history`: an install that predates the achievement timeline gets
            # everything the audit log can prove dated retroactively. See
            # rounds.history_from_runs.
            _ns_next = rounds.update_progress(
                _ns_prev, cfg, _ns_det, state=_ns_state, resolved=ns_resolved,
                dead_regs=dead_registration_hashes(torrent_files_data),
                runs=_ns_runs)
            # Written through a locked read-modify-write, merging the event
            # counters as they stand *now*: `_ns_prev` was read at the top of
            # this phase, and a trump or backfill credited since then would
            # otherwise be erased by this write. See rounds.merge_event_counters.
            db_update_meta('ns_progress',
                           lambda latest: rounds.merge_event_counters(_ns_next, latest))
        except Exception as e:
            log.warning(f"Could not update Next steps progress: {e}")
        # This scan's view of the client and the disk is now the one every stored
        # figure is built from, so it becomes what the next scan is measured
        # against — advanced **only** here, on a scan that persisted, so a
        # refused collapse never becomes the next scan's baseline. That alone did
        # nothing about two *accepted* 40% declines, each of which passed against
        # the scan before it (100 → 60 → 36). This comment used to claim it did;
        # the 2026-09-10 outside review's S02 showed otherwise. The reference is
        # what catches instalments — the largest counts persisted in the last
        # `_GUARD_REFERENCE_DAYS` days — and a manual scan that accepted a change
        # restarts it from itself (decisions 1 and 2 (a), 2026-09-15).
        try:
            counts = {
                'torrent_count': source_report.get('torrent_count', 0),
                'file_map_size': source_report.get('file_map_size', 0),
                'torrent_files': filesystem['torrents'].get('files', 0),
                'media_files':   filesystem['media'].get('files', 0),
            }
            db_set_meta('source_baseline', {
                'at': ran_at, 'source': source_report.get('source'), **counts})
            db_set_meta('source_reference', advance_reference(
                reference_points, counts, reset=any(accepted)))
            db_set_meta('last_source_report', source_report)
            if not source_report.get('partial'):
                db_delete_meta('last_source_anomaly')
        except Exception as e:
            log.warning(f"Could not record the source baseline: {e}")
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
        # Folders deeper than a release folder that could not be listed: counted
        # and shown, not refused (S03 refuses only near the root).
        unlistable = sum(block.get('unlistable', 0) for block in filesystem.values())
        if unlistable:
            log.warning("Audit complete with %d folder(s) that could not be listed — "
                        "check earlier warnings.", unlistable)
        problems = ([f"{stat_errors} file(s) could not be read"] if stat_errors else []) + \
                   ([f"{unlistable} folder(s) could not be listed"] if unlistable else [])
        status_msg = (f"Audit complete. {' and '.join(problems)} — check logs." if problems
                      else "Audit complete.")
        set_state(status_message=status_msg, last_scan_status="ok")
    except _SourceAnomaly as e:
        # Not an error path: the scan ran, decided its own inputs were not
        # trustworthy, and declined to overwrite good data with them. Nothing is
        # written except the reason — see `_record_source_anomaly`.
        _record_source_anomaly(e.anomaly, trigger, cfg, scan_start,
                               persist=persist_source_errors)
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
