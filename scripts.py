import os
import re
import posixpath
import shlex
import logging
from datetime import datetime

from media_server_exclusions import is_tombstone_path

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
    #
    # Filesystem tombstones (`.fuse_hidden*`, `.nfs*`) are dropped the same way,
    # and here as well as at the walk on purpose: the walk's exclusion only takes
    # effect on the next scan, while these records are read from the *last* one.
    # A tombstone is the discarded side of a delete or a move the filesystem has
    # not finished, so hardlinking to it is meaningless — and because a group's
    # canonical is its smallest path and `.` sorts first, an unfiltered tombstone
    # becomes the copy every other file in the group is replaced with. This is
    # the last thing between a stale record and `ln`.
    excluded_abs   = set()
    excluded_count = 0
    for f in all_files:
        if is_tombstone_path(f.get('path')):
            file_root = f.get('_file_root', local_path)
            excluded_abs.add(posixpath.join(file_root, f['path']) if file_root else f['path'])
            continue
        if f.get('excluded'):
            file_root = f.get('_file_root', local_path)
            excluded_abs.add(posixpath.join(file_root, f['path']) if file_root else f['path'])
            if f.get('duplicate_paths'):
                excluded_count += 1

    for f in all_files:
        if not f.get('duplicate_paths') or f.get('excluded'):
            continue
        if is_tombstone_path(f.get('path')):
            continue        # never a canonical; see excluded_abs above
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
    """Generate the dedupe script. Raises ValueError for any other script_type.

    selection (optional dict) narrows the script to a user-chosen subset:
      {'groups': [...]} — canonical relative paths of duplicate groups

    The Cleanup delete script is **not** built here any more: it needs a live
    check of the torrent client immediately before it is emitted (CLEANUP C3),
    which is `app._cleanup_script_response`'s job, and then `build_cleanup_script`
    below. Keeping an unverified branch in this function would leave a way to
    build it that skips the check. `delete_selected` is gone for the same reason
    (C14): it had no caller, and no selection ever reached it re-verified.
    """
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    selection = selection or {}
    if script_type == 'dedupe':
        return _build_dedupe_script(results, cfg, now_str, selection)
    raise ValueError("Unknown script type")


# What each state means for the bytes, as a trailing comment on its delete line.
# Fixed vocabulary only — nothing path-derived is written into a comment without
# `_comment_safe`.
_STATE_NOTE = {
    'library_copy':     'your library keeps a copy',
    'linked_elsewhere': 'another link keeps the data',
    'last_copy':        'the only copy',
}

_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')


def _comment_safe(text):
    """Text that can sit on a `#` line without ending it.

    A filename may contain a newline, and a newline inside a comment ends the
    comment: whatever follows it on the next line is a command. `shlex.quote`
    protects arguments and cannot help a comment, so anything path-derived that
    is written into one goes through this. The old delete script wrote
    `# File 1/3: {filename}` raw, which made a crafted file name a command in a
    script the user was told to run.
    """
    return _CONTROL_CHARS.sub('?', str(text))


# `bc` is not on Git Bash and nobody had checked it on Unraid; awk is on both.
_FMT_BYTES = """\
_fmt_bytes() {
  awk -v b="${1:-0}" 'BEGIN {
    s = ""; if (b < 0) { s = "-"; b = -b }
    if (b >= 1073741824)   printf "%s%.1f GB", s, b / 1073741824
    else if (b >= 1048576) printf "%s%.1f MB", s, b / 1048576
    else if (b >= 1024)    printf "%s%.1f KB", s, b / 1024
    else                   printf "%s%d B", s, b
  }'
}"""

_DELETE_FILE = """\
# delete_file PATH SIZE HUMAN_SIZE
# Three outcomes, each counted: deleted, already gone, FAILED. A failed rm is
# never reported as done and never counted as space freed.
delete_file() {
  local f="$1" size="$2" human="$3" label links
  label="${f#./}"
  N=$((N+1))
  if [ ! -e "$f" ] && [ ! -L "$f" ]; then
    printf '[%s/%s] Already gone: %s\\n' "$N" "$TOTAL" "$label"
    MISSING=$((MISSING+1))
    return 0
  fi
  if [ ! -f "$f" ]; then
    printf '[%s/%s] FAILED — not a regular file, left alone: %s\\n' "$N" "$TOTAL" "$label"
    FAILED=$((FAILED+1))
    return 0
  fi
  links=$(stat -c '%h' -- "$f" 2>/dev/null || stat -f '%l' -- "$f" 2>/dev/null || echo 1)
  case "$links" in ''|*[!0-9]*) links=1 ;; esac
  if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$links" -gt 1 ]; then
      printf '[%s/%s] Would delete: %s (%s — hardlinked, %s references, frees nothing yet)\\n' "$N" "$TOTAL" "$label" "$human" "$links"
    else
      printf '[%s/%s] Would delete: %s (%s)\\n' "$N" "$TOTAL" "$label" "$human"
    fi
    WOULD=$((WOULD+1))
    return 0
  fi
  printf '[%s/%s] Deleting: %s (%s)\\n' "$N" "$TOTAL" "$label" "$human"
  if rm -- "$f"; then
    if [ "$links" -gt 1 ]; then
      printf '  (hardlinked — %s references, space freed when last link removed)\\n' "$links"
      HARDLINKED_COUNT=$((HARDLINKED_COUNT+1))
      HARDLINKED_BYTES=$((HARDLINKED_BYTES+size))
    else
      STANDALONE_COUNT=$((STANDALONE_COUNT+1))
      STANDALONE_BYTES=$((STANDALONE_BYTES+size))
    fi
    echo "  ✓ Deleted"
    DELETED=$((DELETED+1))
  else
    echo "  ✗ FAILED — rm could not delete it, and it is still there"
    FAILED=$((FAILED+1))
  fi
}

# prune_dirs DIR... — deepest first, each no higher than a release folder the
# audit established is safe to act on. rmdir refuses anything that is not empty,
# so an excluded file, a file a torrent claimed at the last check or another
# torrent's file keeps its folder. Never `rmdir -p`, which climbs to the path's
# first component.
prune_dirs() {
  local d
  for d in "$@"; do
    [ -d "$d" ] || continue
    if [ "$DRY_RUN" -eq 1 ]; then
      printf '  Would remove this folder if it ends up empty: %s\\n' "${d#./}"
      continue
    fi
    if rmdir -- "$d" 2>/dev/null; then
      printf '  Removed empty folder: %s\\n' "${d#./}"
      PRUNED=$((PRUNED+1))
    fi
  done
}"""

_SUMMARY = """\
echo ""
echo "================================================"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run complete — nothing was deleted or removed."
  echo "  Would delete:   $WOULD / $TOTAL files"
  echo "  Already gone:   $MISSING"
  if [ "$FAILED" -gt 0 ]; then
    echo "  Not a file:     $FAILED (would be left alone)"
  fi
  echo "================================================"
  exit 0
fi
echo "Cleanup complete."
echo "  Deleted:        $DELETED / $TOTAL files"
echo "  Already gone:   $MISSING"
if [ "$FAILED" -gt 0 ]; then
  echo "  FAILED:         $FAILED file(s) could not be deleted and are still there."
  echo "                  Check permissions, a read-only mount, or an immutable flag."
fi
if [ "$HARDLINKED_COUNT" -gt 0 ]; then
  echo "    Hardlinked (space not freed yet): $HARDLINKED_COUNT file(s) ($(_fmt_bytes "$HARDLINKED_BYTES"))"
fi
if [ "$STANDALONE_COUNT" -gt 0 ]; then
  echo "    Standalone (space freed):         $STANDALONE_COUNT file(s) ($(_fmt_bytes "$STANDALONE_BYTES"))"
fi
if [ "$PRUNED" -gt 0 ]; then
  echo "  Removed $PRUNED empty release folder(s)."
fi
if [ "$DELETED" -eq 0 ] && [ "$FAILED" -eq 0 ]; then
  echo "  Everything was already gone. If you expected deletions, check this ran in your torrent folder."
fi

# Measure actual space freed — only counted deletions are compared, so a failed
# rm can never read as unexplained disk activity.
if [ "$DELETED" -gt 0 ]; then
  FREE_AFTER=$(df --output=avail -B1 "." 2>/dev/null | tail -1 | tr -d " ")
  [ -z "$FREE_AFTER" ] && FREE_AFTER=$(df -k . 2>/dev/null | awk 'NR==2{print $4*1024}')
  echo "  Expected: $(_fmt_bytes "$STANDALONE_BYTES") from $STANDALONE_COUNT standalone file(s)"
  if [ -z "$FREE_BEFORE" ] || [ -z "$FREE_AFTER" ]; then
    echo "  Actual:   (unable to measure — df unavailable on this system)"
  else
    FREED=$((FREE_AFTER - FREE_BEFORE))
    echo "  Actual:   $(_fmt_bytes "$FREED")"
    if [ "$STANDALONE_BYTES" -gt 0 ]; then
      VARIANCE=$(( (FREED - STANDALONE_BYTES) * 100 / STANDALONE_BYTES ))
      ABS_VARIANCE="${VARIANCE#-}"
      if [ "$ABS_VARIANCE" -le 2 ]; then
        echo "  ✓ Actual matches the standalone files deleted (within 2%)"
      else
        echo "  ⚠ Actual differs from the standalone files deleted by ${VARIANCE}%."
        echo "    Free space moves in whole disk blocks and with anything else writing to this disk,"
        echo "    so a small cleanup or a busy disk reads high or low."
      fi
    elif [ "$FREED" -eq 0 ]; then
      echo "  ✓ Every deleted file was hardlinked elsewhere — 0 freed is correct"
    else
      echo "  (every deleted file was hardlinked elsewhere; the change in free space is other disk activity)"
    fi
  fi
fi
echo "================================================"
if [ "$FAILED" -gt 0 ]; then
  exit 1
fi
exit 0"""


def build_cleanup_script(units, *, verified_at, dropped=0, not_in_report=0,
                         excluded_count=None, safe_folders=()):
    """The Cleanup delete script, for a selection the client was just asked about.

    `units` is one entry per inode: `{'paths': [rel, ...], 'size', 'state',
    'frees'}` — every selected path of that inode, its size once, its
    `_cleanup_state`, and whether deleting these paths frees the bytes (a last
    copy with every path selected). `verified_at` is the epoch of the live
    re-verify, `dropped` the selected paths a torrent claims now, and
    `safe_folders` the release folders the audit stamped as `excl_folder`.

    The contract (CLEANUP §5.5), each part of which `backend_tests/test_cleanup.py`
    checks by **running** the script:

    * `set -uo pipefail`, **no `-e`** — a failed `rm` is counted, never an abort.
    * `--` on every command that takes a path, and every path is `./`-prefixed
      (C12): `shlex.quote` protects the shell, not the program's option parser,
      and `shlex.quote('-something.mkv')` returns it unquoted.
    * Three buckets — deleted, already gone, **FAILED** — and a failed file is
      never counted as space freed (C11). Exits 1 if anything failed.
    * **Idempotent.** The working-directory guard checks for folders a completed
      run leaves in place (the parent of each safe release folder, or the folder
      itself where the script removes nothing), not for the first file it
      deletes — which made every finished run fail its own re-run. A directory
      holding none of them is still an error.
    * After a group's files, empty folders are removed **no higher than the
      group's `excl_folder`**, and not at all for a group without one — a
      category dir, a folder shared with a live torrent, or the root (C13).
    * `--dry-run` prints what it would do and touches nothing.
    * `VERIFIED_AT` and a warning past a day, never a refusal: the host clock
      belongs to another machine.
    * The nlink accounting is kept as it was — it was already the one honest
      part of the script, and under C5 it is what counts an inode's bytes once.
    """
    safe = {str(f).replace('\\', '/').strip('/') for f in (safe_folders or ()) if f}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    verified_str = datetime.fromtimestamp(int(verified_at)).strftime("%Y-%m-%d %H:%M:%S")

    by_folder = {}
    for u in units:
        paths = [str(p).replace('\\', '/') for p in u['paths']]
        for p in paths:
            segs = p.split('/')[:-1]
            folder = '/'.join(segs[:2]) if segs else ''
            by_folder.setdefault(folder, []).append(
                (p, int(u.get('size') or 0), u.get('state'), len(paths)))
    total_files = sum(len(v) for v in by_folder.values())
    freeable = sum(int(u.get('size') or 0) for u in units if u.get('frees'))
    # What a completed run leaves behind: the category above each safe release
    # folder, and every folder the script does not prune.
    anchors = sorted({(posixpath.dirname(f) if f in safe else f) for f in by_folder} - {''})

    lines = [
        '#!/bin/bash',
        '# auditorr — Orphaned Torrent Cleanup Script',
        f'# Generated: {now_str}',
        f'# Verified against your torrent client: {verified_str} — just before this script was',
        '# built, no torrent in your client claimed any file below.',
        '# WARNING: Review carefully before running. This permanently deletes files.',
        f'# {total_files} file(s) — up to {_human_size(freeable)} freed (the script reports what it actually frees)',
    ]
    if dropped:
        lines.append(f'# {dropped} file(s) are no longer orphaned and were removed from this script.')
    if not_in_report:
        lines.append(f'# {not_in_report} selected file(s) were not orphaned in the last scan and were left out.')
    if excluded_count:
        lines.append(f'# {excluded_count} orphaned file(s) match your Excluded Files & Folders settings '
                     f'and never appear here.')
    lines += [
        '#',
        '# This script will:',
        '#   1. Check it is running in your torrent folder',
        '#   2. Warn if it was generated more than a day ago',
        "#   3. Check each file's link count (hardlinked = still referenced elsewhere), then delete it",
        '#   4. Count every file as deleted, already gone, or FAILED — a failed delete is never reported as done',
        '#   5. Remove release folders it emptied — never a category folder, never one with anything left in it',
        '#   6. Compare the space actually freed with what was expected',
        '#',
        '# USAGE:',
        '#   cd /path/to/your/torrent/directory',
        '#   bash orphaned_torrents_delete.sh             # delete',
        '#   bash orphaned_torrents_delete.sh --dry-run   # show what it would do; change nothing',
        '#',
        '# All file paths are relative to your torrent directory.',
        '# Safe to run again: anything already deleted is reported as already gone.',
        '',
        'set -uo pipefail',
        '',
        f'VERIFIED_AT={int(verified_at)}',
        'DRY_RUN=0',
        'for arg in "$@"; do',
        '  case "$arg" in',
        '    --dry-run|-n) DRY_RUN=1 ;;',
        '    *) echo "Unknown option: $arg (the only option is --dry-run)"; exit 2 ;;',
        '  esac',
        'done',
        '',
        _FMT_BYTES,
        '',
        _DELETE_FILE,
        '',
    ]
    if anchors:
        lines += [
            '# Working-directory guard. It looks for folders a completed run leaves in place,',
            '# so a second run of a finished script still passes it.',
            '_found=0',
            'for _d in ' + ' '.join(f'./{shlex.quote(a)}' for a in anchors) + '; do',
            '  if [ -d "$_d" ]; then _found=1; break; fi',
            'done',
            'if [ "$_found" -eq 0 ]; then',
            '  echo "ERROR: This does not look like your torrent directory."',
            f'  printf \'  Expected to find the folder: %s\\n\' {shlex.quote(anchors[0])}',
            '  echo "  cd into your torrent folder and try again."',
            '  exit 1',
            'fi',
            '',
        ]
    lines += [
        f'TOTAL={total_files}',
        'N=0; DELETED=0; MISSING=0; FAILED=0; WOULD=0; PRUNED=0',
        'HARDLINKED_COUNT=0; HARDLINKED_BYTES=0; STANDALONE_COUNT=0; STANDALONE_BYTES=0',
        '',
        'NOW=$(date +%s 2>/dev/null || true)',
        'case "$NOW" in \'\'|*[!0-9]*) NOW="" ;; esac',
        'if [ -n "$NOW" ] && [ "$NOW" -gt $((VERIFIED_AT + 86400)) ]; then',
        '  echo "⚠ This script was checked against your torrent client $(( (NOW - VERIFIED_AT) / 3600 )) hours ago."',
        '  echo "  A torrent added since then could be using some of these files."',
        '  echo "  Regenerate it in auditorr to check again, or carry on if nothing has changed."',
        '  echo ""',
        'fi',
        '',
        'FREE_BEFORE=""',
        'if [ "$DRY_RUN" -eq 0 ]; then',
        '  FREE_BEFORE=$(df --output=avail -B1 "." 2>/dev/null | tail -1 | tr -d " ")',
        '  [ -z "$FREE_BEFORE" ] && FREE_BEFORE=$(df -k . 2>/dev/null | awk \'NR==2{print $4*1024}\')',
        'fi',
        '',
        'echo "================================================"',
        'echo "auditorr Orphaned Torrent Cleanup"',
        f'echo "Checked against your torrent client: {verified_str}"',
        '[ "$DRY_RUN" -eq 1 ] && echo "DRY RUN — nothing will be deleted."',
        f'echo "Files: {total_files} — up to {_human_size(freeable)} freed"',
        'echo "================================================"',
        'echo ""',
        '',
    ]

    for folder in sorted(by_folder, key=lambda f: (f != '', f)):
        files = sorted(by_folder[folder])
        lines.append(f'# ── {_comment_safe(folder) if folder else "top of the torrent folder"}')
        for p, size, state, n_paths in files:
            if n_paths > 1:
                lines.append(f'# one file at {n_paths} paths here — its space is freed only when '
                             f'every one of them is deleted')
            note = _STATE_NOTE.get(state)
            lines.append(f'delete_file ./{shlex.quote(p)} {size} {shlex.quote(_human_size(size))}'
                         + (f'   # {note}' if note else ''))
        if folder in safe:
            dirs = set()
            for p, *_ in files:
                d = posixpath.dirname(p)
                while d and (d == folder or d.startswith(folder + '/')):
                    dirs.add(d)
                    d = posixpath.dirname(d)
            if dirs:
                ordered = sorted(dirs, key=lambda d: (-d.count('/'), d))
                lines.append('prune_dirs ' + ' '.join(f'./{shlex.quote(d)}' for d in ordered))
        lines.append('')

    lines.append(_SUMMARY)
    return '\n'.join(lines) + '\n'


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

