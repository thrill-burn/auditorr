import os
import posixpath
import shlex
import logging
import stat
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
    script_root = _compute_script_root(local_path, media_path)
    excluded = set()
    excluded_count = 0
    for f in all_files:
        excluded.update(f.get('excluded_paths', []))
        if f.get('excluded'):
            excluded.add(posixpath.join(f.get('_file_root', local_path), f['path']))
            excluded_count += bool(f.get('duplicate_paths'))

    excluded = {posixpath.normpath(p) for p in excluded}

    # Union overlapping candidate groups before assigning a canonical path.
    # Paths, rather than cached inode numbers, also support older audit snapshots.
    parents = {}

    def find(path):
        parents.setdefault(path, path)
        while parents[path] != path:
            parents[path] = parents[parents[path]]
            path = parents[path]
        return path

    for f in all_files:
        if not f.get('duplicate_paths'):
            continue
        paths = [posixpath.join(f.get('_file_root', local_path), f['path'])]
        paths += f.get('dedupe_paths', []) + f['duplicate_paths']
        paths = list(dict.fromkeys(posixpath.normpath(p) for p in paths))
        paths = [p for p in paths if p not in excluded]
        if paths:
            root = find(paths[0])
            for path in paths[1:]:
                parents[find(path)] = root
    components = {}
    for path in parents:
        components.setdefault(find(path), []).append(path)
    groups = []
    for paths in components.values():
        # Hardlinks can only be made within one device. A remote copy should
        # not prevent deduplication of local copies in the same component.
        devices = {}
        for path in paths:
            try:
                st = os.lstat(path)
                if not stat.S_ISREG(st.st_mode):
                    continue
            except OSError:
                continue
            devices.setdefault(st.st_dev, {}).setdefault(st.st_ino, []).append((path, st))
        for copies in devices.values():
            if len(copies) < 2:
                continue
            files = []
            recoverable = 0
            for index, siblings in enumerate(copies.values()):
                st = siblings[0][1]
                if index and len(siblings) == st.st_nlink:
                    recoverable += st.st_size
                for path, st in siblings:
                    files.append({'path': posixpath.relpath(path, script_root),
                                  'size': st.st_size, 'inode': st.st_ino,
                                  'canonical': not files, 'same_fs': True})
            groups.append({'files': files, 'recoverable_size': recoverable, 'skipped': False})
    return {'groups': groups, 'script_root': script_root, 'excluded_count': excluded_count}


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
    non_skipped_groups = [g for g in groups if not g['skipped']]
    lines = [
        '#!/bin/bash',
        '# auditorr — Dedupe Script',
        f'# Generated: {now_str}',
        f'# {len(groups)} duplicate groups; {_human_size(total_recoverable)} potentially recoverable',
        f'# {excluded_count} excluded duplicate records',
        f'# Run with bash from the host directory corresponding to {script_root!r}.',
        '# Uses Bash and system tools (cmp, stat, ln, mv, mktemp, rm, rmdir).',
        '# Stop writers before running; files must remain unchanged during deduplication.',
        '# Reported bytes are logical size of copies with no remaining hardlinks,',
        '# not a measurement of free disk space (snapshots/open files may retain data).',
        _DEDUPE_RUNTIME,
    ]
    for group in non_skipped_groups:
        # Prefix relative paths so option-like filenames are safe on BSD too.
        paths = [shlex.quote('./' + f['path']) for f in group['files']]
        lines.append('dedupe_group ' + ' '.join(paths))
    lines.extend([
        'printf "Paths linked: %s\\n" "$LINKED"',
        'printf "Copies skipped: %s\\n" "$SKIPPED"',
        'printf "Errors: %s\\n" "$ERRORS"',
        'printf "Reclaimed bytes (logical): %s\\n" "$RECLAIMED"',
        '[ "$ERRORS" -eq 0 ]',
        '',
    ])
    return '\n'.join(lines)


_DEDUPE_RUNTIME = r'''RECLAIMED=0
LINKED=0
SKIPPED=0
ERRORS=0
TMP_DIR=''

error() {
  printf 'ERROR: %s\n' "$*" >&2
  ERRORS=$((ERRORS+1))
}

cleanup_temp() {
  if [ -n "$TMP_DIR" ]; then
    rm -f -- "$TMP_DIR/replacement" && rmdir -- "$TMP_DIR" || return 1
    TMP_DIR=''
  fi
}
trap 'cleanup_temp' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Unraid uses GNU stat/mv. BSD variants also allow local macOS review/testing.
if stat -c '%d' -- . >/dev/null 2>&1; then
  STAT_STYLE=gnu
else
  STAT_STYLE=bsd
fi
file_stat() {
  local field=$1 path=$2 fmt
  if [ "$STAT_STYLE" = gnu ]; then
    case "$field" in
      key) fmt='%d:%i';;
      signature) fmt='%d:%i:%s:%y';;
      links) fmt='%h';;
      size) fmt='%s';;
    esac
    stat -L -c "$fmt" -- "$path"
  else
    case "$field" in
      key) fmt='%d:%i';;
      signature) fmt='%d:%i:%z:%m';;
      links) fmt='%l';;
      size) fmt='%z';;
    esac
    # BSD /dev/fd reports the devfs device, so fstat via stdin instead.
    case "$path" in
      /dev/fd/8) stat -f "$fmt" <&8;;
      /dev/fd/9) stat -f "$fmt" <&9;;
      *) stat -L -f "$fmt" "$path";;
    esac
  fi
}
regular_file() { [ -f "$1" ] && [ ! -L "$1" ]; }
unchanged() {
  local actual
  actual=$(file_stat signature "$1") && [ "$actual" = "$2" ]
}
replace_target() {
  if [ "$STAT_STYLE" = gnu ]; then
    # -T prevents treating a concurrently substituted directory as a destination.
    mv -fT -- "$1" "$2"
  else
    mv -fh -- "$1" "$2"
  fi
}

dedupe_group() {
  local canonical=$1 source_key source_sig old_key old_sig size links status
  local path key candidate i j seen
  local paths=("$@") keys=() processed=()
  if ! regular_file "$canonical"; then
    error "Missing or non-regular canonical: $canonical"
    return
  fi
  source_sig=$(file_stat signature "$canonical") || { error "Cannot stat $canonical"; return; }
  source_key=$(file_stat key "$canonical") || { error "Cannot stat $canonical"; return; }
  exec 8< "$canonical" || { error "Cannot open $canonical"; return; }
  if ! unchanged /dev/fd/8 "$source_sig"; then
    error "Canonical changed: $canonical"
    exec 8<&-
    return
  fi
  # Read identities at execution time, so an already completed/partial run is safe.
  for path in "${paths[@]}"; do
    if regular_file "$path" && key=$(file_stat key "$path"); then
      keys+=("$key")
    else
      error "Missing or non-regular target: $path"
      keys+=('')
    fi
  done
  for ((i=0; i<${#paths[@]}; i++)); do
    old_key=${keys[i]}
    [ -n "$old_key" ] && [ "$old_key" != "$source_key" ] || continue
    seen=0
    for key in "${processed[@]}"; do
      [ "$key" != "$old_key" ] || seen=1
    done
    [ "$seen" -eq 0 ] || continue
    processed+=("$old_key")
    if [ "${old_key%%:*}" != "${source_key%%:*}" ]; then
      printf 'SKIP: different filesystem: %s\n' "${paths[i]}"
      SKIPPED=$((SKIPPED+1))
      continue
    fi
    candidate=${paths[i]}
    old_sig=$(file_stat signature "$candidate") || { error "Cannot stat $candidate"; continue; }
    if ! regular_file "$candidate" || ! exec 9< "$candidate"; then
      error "Cannot open $candidate"
      continue
    fi
    key=$(file_stat key /dev/fd/9)
    if [ "$key" != "$old_key" ] || ! unchanged /dev/fd/9 "$old_sig"; then
      error "Target changed: $candidate"
      exec 9<&-
      continue
    fi
    size=$(file_stat size /dev/fd/9) || { error "Cannot stat $candidate"; exec 9<&-; continue; }
    printf 'Verifying: %s\n' "$candidate"
    cmp -s "$canonical" "$candidate"
    status=$?
    if [ "$status" -ne 0 ]; then
      if [ "$status" -eq 1 ]; then
        printf 'SKIP: files differ: %s\n' "$candidate"
        SKIPPED=$((SKIPPED+1))
      else
        error "Comparison failed: $candidate"
      fi
      exec 9<&-
      continue
    fi
    for ((j=i; j<${#paths[@]}; j++)); do
      [ "${keys[j]}" = "$old_key" ] || continue
      path=${paths[j]}
      if ! regular_file "$canonical" || ! regular_file "$path" ||
         ! unchanged "$canonical" "$source_sig" || ! unchanged /dev/fd/8 "$source_sig" ||
         ! unchanged "$path" "$old_sig" || ! unchanged /dev/fd/9 "$old_sig"; then
        error "File changed during verification: $path"
        continue
      fi
      # The temporary link is in the target's directory/filesystem. Never unlink
      # the target first: failed ln/mv or interruption leaves its pathname alive.
      TMP_DIR=$(mktemp -d "${path%/*}/.auditorr-dedupe-XXXXXXXX") || {
        error "Cannot create temporary directory for $path"; continue;
      }
      if ! ln -- "$canonical" "$TMP_DIR/replacement"; then
        error "Cannot create replacement hardlink: $path"
      elif ! unchanged "$TMP_DIR/replacement" "$source_sig" ||
           ! regular_file "$path" || ! unchanged "$path" "$old_sig"; then
        error "File changed before replacement: $path"
      elif replace_target "$TMP_DIR/replacement" "$path"; then
        LINKED=$((LINKED+1))
      else
        error "Cannot replace target: $path"
      fi
      if ! cleanup_temp; then
        error 'Cannot clean temporary hardlink; stopping'
        exit 1
      fi
    done
    # Stat the open OLD inode, even after its last pathname was replaced.
    # Excluded/unscanned hardlinks keep nlink > 0 and prevent any recovery claim.
    if links=$(file_stat links /dev/fd/9); then
      if [ "$links" -eq 0 ]; then
        RECLAIMED=$((RECLAIMED+size))
      else
        printf 'No space counted: old copy still has hardlinks.\n'
      fi
    else
      error "Cannot verify remaining links: $candidate"
    fi
    exec 9<&-
  done
  exec 8<&-
}
'''
