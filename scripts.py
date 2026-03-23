import os
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


def _build_dup_groups(torrent_files, local_path):
    """Group torrent files with duplicate_paths into structured groups for the Actions page."""
    groups     = []
    seen_inodes = set()
    for f in torrent_files:
        if not f.get('duplicate_paths'):
            continue
        inode = f['inode']
        if inode in seen_inodes:
            continue
        seen_inodes.add(inode)
        torrent_full = os.path.join(local_path, f['path']) if local_path else f['path']
        try:
            torrent_dev = os.stat(torrent_full).st_dev
        except OSError:
            torrent_dev = None
        group_files = [{"path": torrent_full, "size": f['size'], "inode": inode, "canonical": True, "same_fs": True}]
        is_cross_fs = False
        for dup_path in f.get('duplicate_paths', []):
            try:
                same_fs = (torrent_dev is not None and os.stat(dup_path).st_dev == torrent_dev)
            except OSError:
                same_fs = False
            if not same_fs:
                is_cross_fs = True
            group_files.append({"path": dup_path, "size": f['size'], "inode": 0, "canonical": False, "same_fs": same_fs})
        recoverable = 0 if is_cross_fs else f['size'] * len(f.get('duplicate_paths', []))
        groups.append({"files": group_files, "recoverable_size": recoverable, "skipped": is_cross_fs})
    return groups


def generate_script(script_type, results, cfg):
    """Generate and return a shell script string. Raises ValueError for unknown script_type."""
    torrent_files = results.get('torrent_files', [])
    local_path    = cfg.get('LOCAL_PATH', '')
    now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if script_type == 'orphaned_torrents_delete':
        orphaned   = [f for f in torrent_files if f.get('status') == 'Orphaned']
        total_size = sum(f['size'] for f in orphaned)
        lines = [
            '#!/bin/bash',
            '# auditorr — Orphaned Torrent Cleanup Script',
            f'# Generated: {now_str}',
            '# WARNING: Review carefully before running. This permanently deletes files.',
            f'# {len(orphaned)} files — {_human_size(total_size)} expected to be freed',
            '#',
            '# This script will:',
            '#   1. Record free disk space before deletions',
            '#   2. Delete each orphaned file with progress output',
            '#   3. Record free disk space after deletions',
            '#   4. Compare actual space freed vs expected',
            '',
            'set -euo pipefail',
            '',
            f'TOTAL={len(orphaned)}',
            'DONE=0',
            'ERRORS=0',
            f'EXPECTED_BYTES={total_size}',
            '',
            '# Get free space in bytes on the relevant filesystem',
            'FREE_BEFORE=$(df --output=avail -B1 "$(dirname "$0")" 2>/dev/null | tail -1 || df -k . | awk \'NR==2{print $4*1024}\')',
            '',
            'echo "================================================"',
            f'echo "auditorr Orphaned Torrent Cleanup"',
            f'echo "Files to delete: {len(orphaned)}"',
            f'echo "Expected to free: {_human_size(total_size)}"',
            'echo "================================================"',
            'echo ""',
        ]

        for i, f in enumerate(orphaned):
            full_path = os.path.join(local_path, f['path']) if local_path else f['path']
            filename  = os.path.basename(full_path)
            qfull     = shlex.quote(full_path)
            qname     = shlex.quote(filename)
            lines += [
                f'# File {i+1}/{len(orphaned)}: {filename} — {_human_size(f["size"])}',
                f'printf "[{i+1}/{len(orphaned)}] Deleting: %s ({_human_size(f["size"])})\\n" {qname}',
                f'if [ -f {qfull} ]; then',
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
            'if [ "$ERRORS" -gt 0 ]; then',
            '  echo "  Warnings: $ERRORS file(s) not found (already deleted?)"',
            'fi',
            '',
            '# Measure actual space freed',
            'FREE_AFTER=$(df --output=avail -B1 "$(dirname "$0")" 2>/dev/null | tail -1 || df -k . | awk \'NR==2{print $4*1024}\')',
            'FREED=$((FREE_AFTER - FREE_BEFORE))',
            f'EXPECTED={total_size}',
            '',
            '# Format freed bytes for display',
            'if [ "$FREED" -gt 1073741824 ]; then',
            '  FREED_DISPLAY=$(echo "scale=1; $FREED/1073741824" | bc)GB',
            'elif [ "$FREED" -gt 1048576 ]; then',
            '  FREED_DISPLAY=$(echo "scale=1; $FREED/1048576" | bc)MB',
            'else',
            '  FREED_DISPLAY="${FREED}B"',
            'fi',
            '',
            f'echo "  Expected: {_human_size(total_size)}"',
            'echo "  Actual:   ${FREED_DISPLAY}"',
            '',
            '# Warn if actual differs significantly from expected (>10% variance)',
            'if [ "$FREED" -gt 0 ]; then',
            '  VARIANCE=$(( (FREED - EXPECTED) * 100 / EXPECTED ))',
            '  if [ "${VARIANCE#-}" -gt 10 ]; then',
            '    echo "  ⚠ Note: actual space freed differs from expected by ${VARIANCE}%"',
            '    echo "    This is normal if files were hardlinked (inode still referenced elsewhere)"',
            '  fi',
            'fi',
            'echo "================================================"',
        ]
        return '\n'.join(lines)

    elif script_type == 'dedupe':
        groups             = _build_dup_groups(torrent_files, local_path)
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
            '#',
            '# This script replaces duplicate files with hardlinks.',
            '# All file paths will continue to exist after running.',
            '# All torrents will continue seeding normally.',
            '# Review each group carefully before running.',
            '',
            f'TOTAL={total_non_skipped}',
            'DONE=0',
            'SKIPPED=0',
            'RECLAIMED=0',
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
            for nc in non_canonical:
                nc_path    = nc['path']
                size_human = _human_size(nc['size'])
                size_bytes = nc['size']
                qcanon = shlex.quote(canon_path)
                qnc    = shlex.quote(nc_path)
                qname  = shlex.quote(filename)
                lines.append(f'# Duplicate: {nc_path}')
                lines.append(f'printf "[{group_num}/{total_non_skipped}] Verifying %s...\\n" {qname}')
                lines.append(f"HASH_A=$(md5sum {qcanon} | cut -d' ' -f1)")
                lines.append(f"HASH_B=$(md5sum {qnc} | cut -d' ' -f1)")
                lines.append('if [ "$HASH_A" != "$HASH_B" ]; then')
                lines.append('  echo "  SKIP: Hash mismatch — files differ, skipping this group"')
                lines.append('  SKIPPED=$((SKIPPED+1))')
                lines.append('else')
                lines.append('  echo "  Hash verified. Creating hardlink..."')
                lines.append(f'  ln -f {qcanon} {qnc}')
                lines.append(f'  echo "  Done. {size_human} reclaimed."')
                lines.append(f'  RECLAIMED=$((RECLAIMED+{size_bytes}))')
                lines.append('fi')
                lines.append('echo ""')
            lines.append('DONE=$((DONE+1))')
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

    else:
        raise ValueError("Unknown script type")
