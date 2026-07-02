import os
import posixpath
import shlex
import logging
from datetime import datetime

log = logging.getLogger(__name__)


def _human_size(n):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def _compute_script_root(local_path, media_path):
    """Return the directory all script paths should be relative to."""
    if not media_path or local_path == media_path:
        return local_path
    try:
        common = posixpath.commonpath([local_path, media_path])
    except ValueError:
        return local_path
    if common in ('', '/'):
        return local_path
    return common


def dup_group_inputs(torrent_files, media_files, local_path, media_path):
    """Tag files with their filesystem root for _build_dup_groups, keeping only
    the files group building can actually use: excluded ones (they feed the
    partner filter) and ones with duplicate partners. Copying every record just
    to add the tag doubled the multi-GB parsed lists on very large libraries."""
    def keep(f):
        return f.get('excluded') or f.get('duplicate_paths')
    return ([{**f, '_file_root': local_path} for f in torrent_files if keep(f)]
            + [{**f, '_file_root': media_path} for f in media_files if keep(f)])


def _build_dup_groups(all_files, local_path, media_path=''):
    """Group files with duplicate_paths into structured groups for the Actions page.

    Files marked excluded never appear in a group — not as the canonical copy and
    not as a duplicate partner — so generated scripts never touch them (#14).
    """
    script_root   = _compute_script_root(local_path, media_path)
    groups        = []
    seen_file_ids = set()
    covered_paths = set()  # absolute paths already assigned to any group slot

    # Absolute paths of every excluded file, so excluded *partners* can be
    # dropped from other files' duplicate lists (duplicate_paths entries are
    # absolute paths with no excluded flag of their own).
    excluded_abs   = set()
    excluded_count = 0
    for f in all_files:
        if f.get('excluded'):
            file_root = f.get('_file_root', local_path)
            excluded_abs.add(posixpath.join(file_root, f['path']) if file_root else f['path'])
            if f.get('duplicate_paths'):
                excluded_count += 1

    for f in all_files:
        if not f.get('duplicate_paths') or f.get('excluded'):
            continue
        inode   = f['inode']
        file_id = f.get('file_id', inode)
        if file_id in seen_file_ids:
            continue
        file_root  = f.get('_file_root', local_path)
        canon_full = posixpath.join(file_root, f['path']) if file_root else f['path']
        if canon_full in covered_paths:
            continue
        dup_paths = [p for p in f.get('duplicate_paths', []) if p not in excluded_abs]
        if not dup_paths:
            continue  # every partner is excluded — nothing left to dedupe
        seen_file_ids.add(file_id)
        covered_paths.add(canon_full)

        canon_rel = posixpath.relpath(canon_full, script_root)
        try:
            canon_dev = os.stat(canon_full).st_dev
        except OSError:
            canon_dev = None
        group_files = [{"path": canon_rel, "size": f['size'], "inode": inode, "canonical": True, "same_fs": True}]
        is_cross_fs = False
        for dup_path in dup_paths:
            covered_paths.add(dup_path)
            try:
                same_fs = (canon_dev is not None and os.stat(dup_path).st_dev == canon_dev)
            except OSError:
                same_fs = False
            if not same_fs:
                is_cross_fs = True
            dup_rel = posixpath.relpath(dup_path, script_root)
            group_files.append({"path": dup_rel, "size": f['size'], "inode": 0, "canonical": False, "same_fs": same_fs})
        recoverable = 0 if is_cross_fs else f['size'] * len(dup_paths)
        groups.append({"files": group_files, "recoverable_size": recoverable, "skipped": is_cross_fs})
    return {"groups": groups, "script_root": script_root, "excluded_count": excluded_count}


def generate_script(script_type, results, cfg, selection=None):
    """Generate and return a shell script string. Raises ValueError for unknown script_type.

    selection (optional dict) narrows the script to a user-chosen subset:
      {'paths': [...]}  — relative torrent paths (delete scripts)
      {'groups': [...]} — canonical relative paths of duplicate groups (dedupe)
    """
    torrent_files = results.get('torrent_files', [])
    local_path    = cfg.get('LOCAL_PATH', '')
    media_path    = cfg.get('MEDIA_PATH', '')
    now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    selection     = selection or {}

    if script_type == 'orphaned_torrents_delete':
        # Excluded files are invisible to script generation — never emit a
        # delete command for something the user explicitly excluded (#14).
        orphaned       = [f for f in torrent_files if f.get('status') == 'Orphaned' and not f.get('excluded')]
        excluded_count = sum(1 for f in torrent_files if f.get('status') == 'Orphaned' and f.get('excluded'))
        if selection.get('paths'):
            wanted   = set(selection['paths'])
            orphaned = [f for f in orphaned if f['path'] in wanted]
        return _build_delete_script(
            orphaned, excluded_count, now_str,
            title='Orphaned Torrent Cleanup Script',
            heading='auditorr Orphaned Torrent Cleanup',
            script_name='orphaned_torrents_delete.sh',
            excluded_noun='orphaned file(s)',
        )

    elif script_type == 'delete_selected':
        # Explicit selection from a workflow page (Triage / Cleanup) — only the
        # given relative paths, and excluded files still never emitted.
        wanted         = set(selection.get('paths') or [])
        if not wanted:
            raise ValueError("delete_selected requires a non-empty 'paths' selection")
        files          = [f for f in torrent_files if f['path'] in wanted and not f.get('excluded')]
        excluded_count = sum(1 for f in torrent_files if f['path'] in wanted and f.get('excluded'))
        return _build_delete_script(
            files, excluded_count, now_str,
            title='Selected File Cleanup Script',
            heading='auditorr Selected File Cleanup',
            script_name='delete_selected.sh',
        )

    elif script_type == 'dedupe':
        return _build_dedupe_script(results, cfg, now_str, selection)

    else:
        raise ValueError("Unknown script type")


def _build_delete_script(files, excluded_count, now_str, title, heading, script_name, excluded_noun='file(s)'):
    """Shared body for all delete scripts: link-count-aware rm with space accounting."""
    total_size = sum(f['size'] for f in files)
    lines = [
        '#!/bin/bash',
        f'# auditorr — {title}',
        f'# Generated: {now_str}',
        '# WARNING: Review carefully before running. This permanently deletes files.',
        f'# {len(files)} files — {_human_size(total_size)} expected to be freed',
    ]
    if excluded_count:
        lines.append(f'# {excluded_count} {excluded_noun} skipped — they match your Excluded Files & Folders settings')
    lines += [
        '#',
        '# This script will:',
        '#   1. Record free disk space before deletions',
        '#   2. Check each file\'s inode link count (hardlinked = still referenced elsewhere)',
        '#   3. Delete each file with progress output',
        '#   4. Record free disk space after deletions',
        '#   5. Compare actual space freed vs standalone-only expected',
        '#',
        '# USAGE:',
        '#   cd /path/to/your/torrent/directory',
        f'#   bash {script_name}',
        '#',
        '# All file paths are relative to your torrent directory.',
        '# Run this from wherever that directory is mounted with write access.',
        '',
        '# Format byte count for display',
        '_fmt_bytes() {',
        '  local b=$1',
        '  if [ "$b" -ge 1073741824 ]; then',
        '    echo "$(echo "scale=1; $b/1073741824" | bc)GB"',
        '  elif [ "$b" -ge 1048576 ]; then',
        '    echo "$(echo "scale=1; $b/1048576" | bc)MB"',
        '  elif [ "$b" -ge 1024 ]; then',
        '    echo "$(echo "scale=1; $b/1024" | bc)KB"',
        '  else',
        '    echo "${b}B"',
        '  fi',
        '}',
        '',
    ]

    # Working-directory guard using the first file
    if files:
        first_rel = files[0]['path']
        qfirst    = shlex.quote(first_rel)
        lines += [
            f'FIRST_FILE={qfirst}',
            'if [ ! -e "$FIRST_FILE" ]; then',
            '  echo "ERROR: Cannot find files. Are you in the correct torrent directory?"',
            '  echo "  Expected to find: $FIRST_FILE"',
            '  echo "  cd into your torrent folder and try again."',
            '  exit 1',
            'fi',
            '',
        ]

    lines += [
        f'TOTAL={len(files)}',
        'DONE=0',
        'ERRORS=0',
        'HARDLINKED_COUNT=0',
        'HARDLINKED_BYTES=0',
        'STANDALONE_COUNT=0',
        'STANDALONE_BYTES=0',
        '',
        '# Get free space in bytes on the relevant filesystem',
        'FREE_BEFORE=$(df --output=avail -B1 "." 2>/dev/null | tail -1 | tr -d " ")',
        '[ -z "$FREE_BEFORE" ] && FREE_BEFORE=$(df -k . 2>/dev/null | awk \'NR==2{print $4*1024}\')',
        '',
        'echo "================================================"',
        f'echo "{heading}"',
        f'echo "Files to delete: {len(files)}"',
        f'echo "Expected to free: {_human_size(total_size)}"',
        'echo "================================================"',
        'echo ""',
    ]

    for i, f in enumerate(files):
        rel_path   = f['path']  # already relative to LOCAL_PATH
        filename   = os.path.basename(rel_path)
        qfull      = shlex.quote(rel_path)
        qname      = shlex.quote(filename)
        size_bytes = f['size']
        lines += [
            f'# File {i+1}/{len(files)}: {filename} — {_human_size(size_bytes)}',
            f'printf "[{i+1}/{len(files)}] Deleting: %s ({_human_size(size_bytes)})\\n" {qname}',
            f'if [ -f {qfull} ]; then',
            f'  NLINKS=$(stat -c \'%h\' {qfull} 2>/dev/null || stat -f \'%l\' {qfull} 2>/dev/null || echo 1)',
            '  if [ "$NLINKS" -gt 1 ]; then',
            f'    printf "  (hardlinked — %s references, space freed when last link removed)\\n" "$NLINKS"',
            f'    HARDLINKED_COUNT=$((HARDLINKED_COUNT+1))',
            f'    HARDLINKED_BYTES=$((HARDLINKED_BYTES+{size_bytes}))',
            '  else',
            f'    STANDALONE_COUNT=$((STANDALONE_COUNT+1))',
            f'    STANDALONE_BYTES=$((STANDALONE_BYTES+{size_bytes}))',
            '  fi',
            f'  rm {qfull}',
            f'  echo "  ✓ Deleted"',
            '  DONE=$((DONE+1))',
            'else',
            f'  printf "  ⚠ Not found, skipping: %s\\n" {qfull}',
            '  ERRORS=$((ERRORS+1))',
            'fi',
            '',
        ]

    lines += [
        'echo ""',
        'echo "================================================"',
        'echo "Cleanup complete."',
        'echo "  Deleted:  $DONE / $TOTAL files"',
        'if [ "$HARDLINKED_COUNT" -gt 0 ]; then',
        '  HL_DISPLAY=$(_fmt_bytes "$HARDLINKED_BYTES")',
        '  echo "    Hardlinked (space not freed yet): $HARDLINKED_COUNT file(s) ($HL_DISPLAY)"',
        'fi',
        'if [ "$STANDALONE_COUNT" -gt 0 ]; then',
        '  SL_DISPLAY=$(_fmt_bytes "$STANDALONE_BYTES")',
        '  echo "    Standalone (space freed):         $STANDALONE_COUNT file(s) ($SL_DISPLAY)"',
        'fi',
        'if [ "$ERRORS" -gt 0 ]; then',
        '  echo "  Warnings: $ERRORS file(s) not found (already deleted?)"',
        'fi',
        '',
        '# Measure actual space freed',
        'FREE_AFTER=$(df --output=avail -B1 "." 2>/dev/null | tail -1 | tr -d " ")',
        '[ -z "$FREE_AFTER" ] && FREE_AFTER=$(df -k . 2>/dev/null | awk \'NR==2{print $4*1024}\')',
        '',
        f'echo "  Expected: {_human_size(total_size)} total"',
        'if [ "$HARDLINKED_COUNT" -gt 0 ] && [ "$STANDALONE_COUNT" -gt 0 ]; then',
        '  SL_DISPLAY=$(_fmt_bytes "$STANDALONE_BYTES")',
        '  echo "    ($SL_DISPLAY from standalone, $HARDLINKED_COUNT hardlinked file(s) free 0)"',
        'elif [ "$HARDLINKED_COUNT" -gt 0 ]; then',
        '  echo "    (all files were hardlinked — 0 expected to free)"',
        'fi',
        '',
        'if [ -z "$FREE_BEFORE" ] || [ -z "$FREE_AFTER" ]; then',
        '  echo "  Actual:   (unable to measure — df unavailable on this system)"',
        'else',
        '  FREED=$((FREE_AFTER - FREE_BEFORE))',
        '  FREED_DISPLAY=$(_fmt_bytes "$FREED")',
        '  echo "  Actual:   $FREED_DISPLAY"',
        '  if [ "$STANDALONE_BYTES" -gt 0 ]; then',
        '    VARIANCE=$(( (FREED - STANDALONE_BYTES) * 100 / STANDALONE_BYTES ))',
        '    ABS_VARIANCE="${VARIANCE#-}"',
        '    if [ "$ABS_VARIANCE" -le 2 ]; then',
        '      echo "  ✓ Actual matches standalone expected (within 2%)"',
        '    else',
        '      echo "  ⚠ Actual differs from standalone expected by ${VARIANCE}% — unexpected"',
        '    fi',
        '  elif [ "$HARDLINKED_COUNT" -gt 0 ] && [ "$STANDALONE_COUNT" -eq 0 ]; then',
        '    if [ "$FREED" -eq 0 ]; then',
        '      echo "  ✓ All files were hardlinked — 0 freed is correct"',
        '    else',
        '      echo "  ⚠ Files were hardlinked but disk space changed — check for concurrent activity"',
        '    fi',
        '  fi',
        'fi',
        'echo "================================================"',
    ]
    return '\n'.join(lines)


def _build_dedupe_script(results, cfg, now_str, selection):
    torrent_files  = results.get('torrent_files', [])
    media_files    = results.get('media_files', [])
    local_path     = cfg.get('LOCAL_PATH', '')
    media_path     = cfg.get('MEDIA_PATH', '')
    dup_result         = _build_dup_groups(
        dup_group_inputs(torrent_files, media_files, local_path, media_path),
        local_path, media_path)
    groups             = dup_result['groups']
    script_root        = dup_result['script_root']
    excluded_count     = dup_result.get('excluded_count', 0)
    if selection.get('groups'):
        # Group identity = canonical file's relative path (stable per audit)
        wanted = set(selection['groups'])
        groups = [g for g in groups
                  if next(f['path'] for f in g['files'] if f['canonical']) in wanted]
    total_recoverable  = sum(g['recoverable_size'] for g in groups)
    skipped_count      = sum(1 for g in groups if g['skipped'])
    non_skipped_groups = [g for g in groups if not g['skipped']]
    total_non_skipped  = len(non_skipped_groups)
    lines = [
        '#!/bin/bash',
        '# auditorr — Dedupe Script',
        f'# Generated: {now_str}',
        '#',
        '# SUMMARY',
        f'# {len(groups)} duplicate groups found',
        f'# {_human_size(total_recoverable)} recoverable',
        f'# {skipped_count} groups skipped (cross-filesystem — cannot hardlink across mounts)',
    ]
    if excluded_count:
        lines.append(f'# {excluded_count} duplicate file(s) skipped — they match your Excluded Files & Folders settings')
    lines += [
        '#',
        '# This script replaces duplicate files with hardlinks.',
        '# All file paths will continue to exist after running.',
        '# All torrents will continue seeding normally.',
        '# Review each group carefully before running.',
        '#',
        '# USAGE:',
        f'#   cd <directory on your host that maps to {script_root}>',
        '#   bash dedupe.sh',
        '#',
        '# TIP: install "pv" (e.g. apt install pv) for a live progress bar while',
        '#      large files are verified — without it you get a heartbeat instead.',
        '#',
        f'# All paths are relative to {script_root} (auditorr\'s view).',
        '',
        f'TOTAL={total_non_skipped}',
        'DONE=0',
        'SKIPPED=0',
        'RECLAIMED=0',
        '',
        '# Re-verify two files are byte-identical before hardlinking. cmp has no',
        '# progress output of its own, so wrap it: a live progress bar via pv when',
        '# installed, otherwise a heartbeat so large-file checks are never silent.',
        'verify_identical() {',
        '  if command -v pv >/dev/null 2>&1; then',
        '    cmp -s <(pv -N "  comparing" "$1") "$2"',
        '  else',
        '    cmp -s "$1" "$2" &',
        '    local _pid=$!',
        '    while kill -0 "$_pid" 2>/dev/null; do printf "."; sleep 1; done',
        '    printf "\\n"',
        '    wait "$_pid"',
        '  fi',
        '}',
        '',
    ]

    # Working-directory guard using the first canonical file in a non-skipped group
    first_canon = next(
        (next(f for f in g['files'] if f['canonical']) for g in non_skipped_groups),
        None
    )
    if first_canon:
        qfirst = shlex.quote(first_canon['path'])
        lines += [
            f'FIRST_FILE={qfirst}',
            'if [ ! -e "$FIRST_FILE" ]; then',
            '  echo "ERROR: Cannot find files. Are you in the correct data directory?"',
            '  echo "  Expected to find: $FIRST_FILE"',
            '  echo "  cd into your parent data folder and try again."',
            '  exit 1',
            'fi',
            '',
        ]

    group_num = 0
    for g in groups:
        canonical     = next(f for f in g['files'] if f['canonical'])
        non_canonical = [f for f in g['files'] if not f['canonical']]
        filename      = os.path.basename(canonical['path'])
        if g['skipped']:
            lines.append(f'# SKIPPED Group: {filename} — cross-filesystem, cannot hardlink')
            lines.append('')
            continue
        group_num += 1
        canon_path = canonical['path']
        lines.append(f'# Group {group_num}: {filename} — {_human_size(g["recoverable_size"])} recoverable')
        lines.append(f'# Canonical: {canon_path}')
        lines.append('GROUP_LINKED=0')
        for nc in non_canonical:
            nc_path    = nc['path']
            size_human = _human_size(nc['size'])
            size_bytes = nc['size']
            qcanon = shlex.quote(canon_path)
            qnc    = shlex.quote(nc_path)
            qname  = shlex.quote(filename)
            lines.append(f'# Duplicate: {nc_path}')
            lines.append(f'printf "[{group_num}/{total_non_skipped}] Verifying %s ({size_human})...\\n" {qname}')
            # cmp stops at the first differing byte — md5sum would read both
            # files in full (and hash them) even when they differ immediately.
            # verify_identical wraps cmp with a progress bar / heartbeat.
            lines.append(f'if ! verify_identical {qcanon} {qnc}; then')
            lines.append('  echo "  SKIP: Files differ — skipping this group"')
            lines.append('  SKIPPED=$((SKIPPED+1))')
            lines.append('else')
            lines.append('  echo "  Verified identical. Creating hardlink..."')
            lines.append(f'  ln -f {qcanon} {qnc}')
            lines.append(f'  echo "  Done. {size_human} reclaimed."')
            lines.append(f'  RECLAIMED=$((RECLAIMED+{size_bytes}))')
            lines.append('  GROUP_LINKED=1')
            lines.append('fi')
            lines.append('echo ""')
        lines.append('if [ "$GROUP_LINKED" -gt 0 ]; then DONE=$((DONE+1)); fi')
        lines.append('')
    lines.extend([
        'echo "================================"',
        'echo "Dedupe complete."',
        'echo "Groups processed: $DONE / $TOTAL"',
        'echo "Groups skipped (hash mismatch): $SKIPPED"',
        'echo ""',
        "echo \"Run 'df -h' to verify space reclaimed.\"",
    ])
    return '\n'.join(lines)

