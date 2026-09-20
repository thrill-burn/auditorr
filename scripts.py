import os
import re
import posixpath
import shlex
import logging
from datetime import datetime

from exclusions import compile_exclusions
from media_server_exclusions import expand_exclusion_patterns, is_tombstone_path

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


def is_dedupe_relevant(f):
    """The records duplicate grouping can use: excluded ones (they feed the
    partner filter) and ones with duplicate partners. One definition, read by
    `dup_group_inputs` and by the audit's compact `dedupe` row."""
    return bool(f.get('excluded') or f.get('duplicate_paths'))


def dedupe_row(torrent_files, media_files):
    """The compact `dedupe` `file_results` row (R6, Phase 14): `{'torrents': [...],
    'media': [...]}` — **references** to the records `is_dedupe_relevant` keeps,
    never copies, keyed by tree so the builder can tell the two apart.

    Both Dedupe endpoints read this instead of both full lists, which was the
    last endpoint deserializing the whole library to keep a sliver of it (C10's
    shape for Cleanup, v1.7.0's for Triage). Feeding it to `build_dedupe_report`
    is the same computation as feeding the full lists, because the builder's
    first step is this filter.
    """
    return {'torrents': [f for f in torrent_files if is_dedupe_relevant(f)],
            'media':    [f for f in media_files if is_dedupe_relevant(f)]}


def dedupe_group_count(torrent_files, media_files, cfg):
    """How many groups the Dedupe page lists for these records (the sidebar badge).

    The page's own grouping, minus the per-request `stat` that only classifies
    and never removes a group — so stale groups count, because the page lists
    them. The one way the two can still differ: the page applies the exclusion
    rules as they are *now*, and this applied them as they were when it ran.
    """
    local_path = cfg.get('LOCAL_PATH', '') or ''
    media_path = cfg.get('MEDIA_PATH', '') or ''
    matcher = compile_exclusions(expand_exclusion_patterns(cfg))
    built = _build_dup_groups(dup_group_inputs(torrent_files, media_files, local_path, media_path),
                              local_path, media_path, matcher=matcher, log_counts=False)
    return len(built['groups'])


def dup_group_inputs(torrent_files, media_files, local_path, media_path):
    """Tag files with their filesystem root and tree for _build_dup_groups,
    keeping only the files group building can actually use (`is_dedupe_relevant`).
    Copying every record just to add the tag doubled the multi-GB parsed lists
    on very large libraries."""
    return ([{**f, '_file_root': local_path, '_tree': 'torrents'}
             for f in torrent_files if is_dedupe_relevant(f)]
            + [{**f, '_file_root': media_path, '_tree': 'media'}
               for f in media_files if is_dedupe_relevant(f)])


def _abs(path):
    """An absolute path spelled for comparison: `/` separators, no `//` or `.`.

    The audit joins paths with `os.path.join`, which is `\\` on a Windows dev
    machine, so every path on both sides of a comparison goes through this —
    unconditionally, so the checked-in tests take the branch the container
    takes. A Linux file name that really contains a backslash is misspelled by
    it and then reads as missing: the `stale` state, which cannot be selected.
    """
    p = str(path or '').replace('\\', '/')
    return posixpath.normpath(p) if p else ''


def _join(root, rel):
    rel = str(rel or '').replace('\\', '/')
    return _abs(posixpath.join(_abs(root), rel)) if root else _abs(rel)


def _outside(rel):
    return rel == '..' or rel.startswith('../')


def _cause(members):
    """What linking a group does, read off its own paths (decision 2 (a)).

    `missing_hardlink` where a file that is only in one tree has a copy holding
    a path in the other tree: linking them puts a torrent path and a library
    path on one inode, which is what an import that hardlinked would have done.
    Otherwise `copies` — the same bytes stored twice in one tree, or two files
    that are each already imported. No claim about which kind dominates a
    library: that is QA-11, and it has not been measured.
    """
    trees = [frozenset(p['tree'] for p in m['paths']) for m in members]
    for i, own in enumerate(trees):
        if len(own) != 1:
            continue
        other = 'media' if 'torrents' in own else 'torrents'
        if any(other in t for j, t in enumerate(trees) if j != i):
            return 'missing_hardlink'
    return 'copies'


def _build_dup_groups(all_files, local_path, media_path='', matcher=None, log_counts=True):
    """Duplicate groups built around the inode (DEDUPE §5.2).

    `all_files` is `dup_group_inputs`' output. Returns `{'groups',
    'script_root', 'excluded_count', 'unresolved', 'already_hardlinked'}`, where
    each group is a set of equal files and **nothing is canonical**:

        {'id', 'size', 'frees_up_to', 'file_count', 'path_count', 'cause',
         'members': [{'key', 'size', 'paths': [{'path', 'tree', '_abs'}]}]}

    * **One member per inode** (F1/F9). Records are indexed by `file_id`, and
      every absolute path a record names — its own and each `linked_paths`
      entry, which are the other tree's paths of the same inode — belongs to
      it. The old rule consumed a record only in the role it was processed in,
      so an imported file's other role seeded a second group for the same pair,
      and the script traded the two inodes' paths: nothing freed, and both
      torrents stopped reading as imported.
    * **Union-find over `duplicate_paths`, treated as undirected** (F15). The
      audit stores at most `DUP_PATHS_PER_FILE` partners per file in walk order,
      so the edges are truncated and one-way: of fifteen copies the last knows
      the first and the first does not know the last. Taking one record's list
      as its group truncated at eleven. An entry that names no record is
      dropped and counted, never guessed at.
    * **A member's paths are the ones the records know**, which since F18 is
      every path both walks saw: a record's own path, `linked_paths` (the other
      tree's paths of that inode) and `dedupe_paths` (its own tree's other
      paths, which no record spells — a never-imported cross-seed's second
      tracker directory). What is still unknown is a link outside both trees,
      and the script refuses to replace part of a file (`stat -c %h` against the
      paths it lists), so that costs a reclaim, never topology.
    * **Excluded paths and tombstones are never a member's paths** (#14) — from
      the records' own flags and, for a path with no record of its own, the
      current exclusion rules through `matcher`. Tombstones are filtered here as
      well as at the walk on purpose: these records are the *last* scan's, and
      the walk's exclusion only lands on the next. A member left with no path is
      no member.
    * **Group id = the smallest path** (F10), which survives a re-audit.
    * **`frees_up_to` = size × (files − 1)** (F2/F3) — a maximum, true only if
      every copy shares a disk and every link to each is known.
    * **F12:** an edge between two paths of one inode cannot come out of
      `_build_duplicate_map`, which iterates an inode-keyed map. It is counted,
      logged and merged — never a state.

    Pure: no filesystem access and no shared state, so two requests on
    gunicorn's threads can build at once. `_classify` is what stats.
    """
    local_path, media_path = _abs(local_path), _abs(media_path)
    script_root = _compute_script_root(local_path, media_path)
    roots = {'torrents': local_path, 'media': media_path}
    other_tree = {'torrents': 'media', 'media': 'torrents'}

    def tree_of(f):
        tree = f.get('_tree')
        if tree in roots:
            return tree
        root = _abs(f.get('_file_root'))
        return 'media' if media_path and media_path != local_path and root == media_path else 'torrents'

    def own_path(f):
        return _join(f.get('_file_root', local_path), f.get('path'))

    def hidden(path, tree):
        if is_tombstone_path(path):
            return True
        if matcher is None:
            return False
        root = roots.get(tree) or ''
        under = root and (root == '/' or path == root or path.startswith(root + '/'))
        rel = posixpath.relpath(path, root) if under else path
        return bool(matcher.match(path, rel, posixpath.basename(path)))

    excluded_abs, excluded_count = set(), 0
    for f in all_files:
        tomb = is_tombstone_path(f.get('path'))
        if tomb or f.get('excluded'):
            excluded_abs.add(own_path(f))
            if not tomb and f.get('duplicate_paths'):
                excluded_count += 1

    known, sizes, edges = {}, {}, []
    for f in all_files:
        if not f.get('duplicate_paths') or f.get('excluded') or is_tombstone_path(f.get('path')):
            continue
        fid = str(f.get('file_id') or f.get('inode'))
        tree = tree_of(f)
        paths = known.setdefault(fid, {})
        sizes.setdefault(fid, int(f.get('size') or 0))
        paths.setdefault(own_path(f), tree)
        for p in f.get('dedupe_paths') or []:
            paths.setdefault(_abs(p), tree)
        for p in f.get('linked_paths') or []:
            paths.setdefault(_abs(p), other_tree[tree])
        edges.extend((fid, _abs(p)) for p in f['duplicate_paths'])

    owner = {}
    for fid, paths in known.items():
        for p in paths:
            owner.setdefault(p, fid)

    parent = {fid: fid for fid in known}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    unresolved, self_edges = set(), set()
    for fid, p in edges:
        target = owner.get(p)
        if target is None:
            if p not in excluded_abs and not is_tombstone_path(p):
                unresolved.add(p)
            continue
        if target == fid:
            self_edges.add((fid, p))
            continue
        a, b = find(fid), find(target)
        if a != b:
            parent[b] = a

    components = {}
    for fid in known:
        components.setdefault(find(fid), []).append(fid)

    groups = []
    for fids in components.values():
        members = []
        for fid in fids:
            paths = []
            for p, tree in known[fid].items():
                if p in excluded_abs or hidden(p, tree):
                    continue
                rel = posixpath.relpath(p, script_root) if script_root else p
                paths.append({'path': rel, 'tree': tree, '_abs': p})
            if paths:
                paths.sort(key=lambda x: x['path'])
                members.append({'key': fid, 'size': sizes[fid], 'paths': paths})
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m['paths'][0]['path'])
        size = members[0]['size']
        groups.append({
            'id':          members[0]['paths'][0]['path'],
            'size':        size,
            'frees_up_to': size * (len(members) - 1),
            'file_count':  len(members),
            'path_count':  sum(len(m['paths']) for m in members),
            'cause':       _cause(members),
            'members':     members,
        })

    # Counts only: these reach /api/debug/report through the log ring buffer.
    # The audit's badge count builds the same groups every scan and passes
    # `log_counts=False`, so the lines stay what they were: one per page load.
    if self_edges and log_counts:
        log.warning("Dedupe: %d duplicate record(s) named another path of the same file "
                    "— merged, not shown (DEDUPE F12)", len(self_edges))
    if unresolved and log_counts:
        log.info("Dedupe: %d duplicate partner path(s) matched no stored record and were "
                 "left out", len(unresolved))
    return {"groups": groups, "script_root": script_root, "excluded_count": excluded_count,
            "unresolved": len(unresolved), "already_hardlinked": len(self_edges)}


# ── Classification (DEDUPE §5.3) ──────────────────────────────────────────────

# Read at call time, never bound as a default, so a test can point it elsewhere.
MOUNTINFO_PATH = '/proc/self/mountinfo'

_MOUNT_ESCAPE = re.compile(r'\\([0-7]{3})')

_STATUS_ORDER = {'linkable': 0, 'cross_device': 1, 'unverifiable': 2, 'stale': 3}


def read_mountinfo(path=None):
    """`[(mount point, fstype)]` from `/proc/self/mountinfo`, or None when it
    cannot be read — which the report records as a fact, never as "checked".

    proc_pid_mountinfo(5): field 5 is the mount point; zero or more optional
    fields follow field 6 until a single `-`; the field after that is the
    filesystem type, as `type[.subtype]`. The kernel writes a space, tab,
    newline or backslash in a mount point as a three-digit octal escape
    (`\\040`). Read once per request.
    """
    try:
        with open(path or MOUNTINFO_PATH, 'r', encoding='utf-8', errors='replace') as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    mounts = []
    for line in lines:
        fields = line.split(' ')
        try:
            sep = fields.index('-', 6)
        except ValueError:
            continue
        if len(fields) <= sep + 1:
            continue
        point = _MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4])
        mounts.append((_abs(point) or '/', fields[sep + 1]))
    return mounts


def _mount_of(path, mounts):
    """The filesystem type of the longest mount holding `path`, on whole
    segments — `/mnt/user` does not hold `/mnt/user2`. Of two lines for one
    mount point the later wins, as an over-mount does."""
    best, fstype = -1, None
    for point, ftype in mounts:
        if point == '/' or path == point or path.startswith(point + '/'):
            if len(point) >= best:
                best, fstype = len(point), ftype
    return fstype


def _classify(group, mounts):
    """`status`, `reason`, `selectable` and `facts` for one group.

    Stats every path of every member, and nothing else — group members only.
    The status is the container's best evidence and never an authorisation:
    the script checks the device, the bytes and the link count itself when it
    runs (Principle 1), so `cross_device` and `unverifiable` set expectations
    rather than blocking. Only `stale` blocks — a copy has gone since the scan.

    The mount type is recorded and changes nothing. A pooled filesystem
    (`fuse.shfs`, `fuse.mergerfs`) reports one `st_dev` for every drive (F4),
    and this used to read `unverifiable` (`pooled_mount`). The reference box
    then showed shfs making every cross-disk link it was asked for, and the
    script reports a refused link when it runs — so on the commonest install
    that status put a caution on every group for normal operation, and it was
    removed (2026-09-15, the user's call).
    """
    devices, fstypes = set(), set()
    missing = failed = outside = False
    for m in group['members']:
        for p in m['paths']:
            if _outside(p['path']):
                outside = True
            try:
                st = os.stat(p['_abs'])
            except (FileNotFoundError, NotADirectoryError):
                missing = True
                continue
            except (OSError, ValueError):
                failed = True
                continue
            devices.add(st.st_dev)
            if mounts:
                fstypes.add(_mount_of(p['_abs'], mounts))
    fstypes.discard(None)
    if missing:
        status, reason = 'stale', 'missing'
    elif outside:
        status, reason = 'unverifiable', 'outside_script_root'
    elif failed:
        status, reason = 'unverifiable', 'stat_failed'
    elif len(devices) > 1:
        status, reason = 'cross_device', 'different_devices'
    else:
        status, reason = 'linkable', None
    return {
        'status': status, 'reason': reason, 'selectable': status != 'stale',
        'facts': {
            'devices': len(devices),
            'fstype': ', '.join(sorted(fstypes)) or None,
            'mount_checked': mounts is not None,
        },
    }


def build_dedupe_report(torrent_files, media_files, cfg):
    """The duplicate report — and the one builder the script is made from (F13).

    Not cached: the script verifies everything at run time, so a report built a
    few seconds before the script is no weaker than the same object (DEDUPE F13,
    "noted so nobody fixes it by caching"). Groups sort `linkable` →
    `cross_device` → `unverifiable` → `stale`, bytes descending within each. The
    totals count only groups that can be selected.
    """
    local_path = cfg.get('LOCAL_PATH', '') or ''
    media_path = cfg.get('MEDIA_PATH', '') or ''
    matcher = compile_exclusions(expand_exclusion_patterns(cfg))
    built = _build_dup_groups(dup_group_inputs(torrent_files, media_files, local_path, media_path),
                              local_path, media_path, matcher=matcher)
    mounts = read_mountinfo()
    groups = []
    for g in built['groups']:
        g.update(_classify(g, mounts))
        for m in g['members']:
            for p in m['paths']:
                p.pop('_abs', None)
        groups.append(g)
    groups.sort(key=lambda g: (_STATUS_ORDER[g['status']], -g['frees_up_to'], g['id']))
    live = [g for g in groups if g['selectable']]
    return {
        'groups':                 groups,
        'script_root':            built['script_root'],
        'excluded_count':         built['excluded_count'],
        'file_count':             sum(g['file_count'] for g in live),
        'frees_up_to':            sum(g['frees_up_to'] for g in live),
        'missing_hardlink_count': sum(1 for g in live if g['cause'] == 'missing_hardlink'),
        'stale_count':            len(groups) - len(live),
        'mount':                  {'checked': mounts is not None},
    }


def scriptable_members(group):
    """The members of a group the script may name: never one with a path
    outside the script root (§10). With no common ancestor the root falls back
    to `LOCAL_PATH`, and a library path would reach the script as `../…` —
    outside the folder the user was told to `cd` into."""
    return [m for m in group['members'] if not any(_outside(p['path']) for p in m['paths'])]


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


# ── The Dedupe script (DEDUPE §5.4) ───────────────────────────────────────────

_VERIFY_IDENTICAL = """\
# Compare two files byte for byte. cmp has no progress output of its own, so wrap
# it: a live progress bar through pv when installed, otherwise a heartbeat, so a
# large comparison is never silent.
verify_identical() {
  if command -v pv >/dev/null 2>&1; then
    cmp -s -- <(pv -N "  comparing" "$1") "$2"
  else
    cmp -s -- "$1" "$2" &
    local _pid=$! _i=0
    while kill -0 "$_pid" 2>/dev/null; do
      sleep 0.2 2>/dev/null || sleep 1
      _i=$((_i + 1))
      if [ $((_i % 5)) -eq 0 ]; then printf "."; fi
    done
    if [ "$_i" -ge 5 ]; then printf "\\n"; fi
    wait "$_pid"
  fi
}"""

# Everything the script does to a group. Raw, so the bash reads as bash. Nothing
# path-derived is ever in here: paths arrive as quoted `copy` arguments.
_DEDUPE_RUNNER = r'''# ── How a group is linked ─────────────────────────────────────────────────────
# Nothing below trusts the scan this script was built from. Every listed path is
# looked at again, now, and anything that does not pass is left alone and says why.

GP=(); GC=(); G_NUM=0; G_SIZE=0; G_COPIES=0
U_KEY=(); U_DEV=(); U_INO=(); U_NL=(); U_SZ=(); U_ALLOC=(); U_OWN=(); U_IDX=(); U_CMP=()
BUCKET=(); ASIDE=(); STAGED=(); LINK_RESULT=''

# group_begin NUMBER SIZE — a set of identical files, SIZE bytes each
group_begin() {
  G_NUM=$1
  G_SIZE=$2
  GP=()
  GC=()
  G_COPIES=0
}

# copy PATH... — every path auditorr knew for one of the files (one inode)
copy() {
  local p
  G_COPIES=$((G_COPIES + 1))
  for p in "$@"; do
    GP+=("$p")
    GC+=("$G_COPIES")
  done
}

# _label FILE — a file's first listed path, for messages
_label() {
  local idx=(${U_IDX[$1]})
  printf '%s' "${GP[${idx[0]}]#./}"
}

# Remove any temporary link this script made and has not renamed. Runs on every
# exit path, so an interruption never leaves one behind.
_drop_staged() {
  local t
  for t in "${STAGED[@]+"${STAGED[@]}"}"; do
    if [ -n "$t" ]; then rm -f -- "$t" 2>/dev/null; fi
  done
  STAGED=()
}
trap '_drop_staged' EXIT
# A dry run has nothing to finish, so it does not say to run it again.
trap '_drop_staged; if [ "$DRY_RUN" -eq 1 ]; then printf "\nInterrupted. Nothing was changed.\n"; else printf "\nInterrupted. Every path is still a whole file. Run the script again to finish.\n"; fi; trap - EXIT; exit 130' INT TERM HUP

# The copy to keep: the owner and permissions most copies share, then the most
# hardlinks (the fewest replacements), then the first listed.
_pick_canonical() {
  local u v n best='' best_share=-1 best_links=-1
  for u in "${BUCKET[@]}"; do
    n=0
    for v in "${BUCKET[@]}"; do
      if [ "${U_OWN[$v]}" = "${U_OWN[$u]}" ]; then n=$((n + 1)); fi
    done
    if [ "$n" -gt "$best_share" ] || { [ "$n" -eq "$best_share" ] && [ "${U_NL[$u]}" -gt "$best_links" ]; }; then
      best=$u
      best_share=$n
      best_links=${U_NL[$u]}
    fi
  done
  printf '%s' "$best"
}

# link_one KEEP FILE — replace every path of FILE with a hardlink to KEEP, or
# none of them. Sets LINK_RESULT: done, skip, failed, or aside (the link could
# not be made, so FILE is tried against another copy).
link_one() {
  local c=$1 u=$2 keep label known k d n tmp line renamed=0
  local ci=(${U_IDX[$c]}) ui=(${U_IDX[$u]})
  keep=${GP[${ci[0]}]}
  label=${GP[${ui[0]}]#./}
  known=${#ui[@]}
  LINK_RESULT=skip

  # Refuse partial replacement (DEDUPE §2). A file has one hardlink per path. If
  # this script does not list every one, replacing the ones it lists frees
  # nothing and splits the file: a torrent path and its library path would end
  # up on different files.
  if [ "${U_NL[$u]}" -gt "$known" ]; then
    printf '  left alone — it has %s more hardlink(s) than this script knows about, and replacing only some would split it: %s\n' "$(( ${U_NL[$u]} - known ))" "$label"
    SKIPPED=$((SKIPPED + 1))
    return
  fi

  # Copies set aside in an earlier round were already compared with that round's
  # kept copy, and so were the rest of the set aside with them: equal to one
  # file, equal to each other.
  if [ "${U_CMP[$u]}" != 1 ]; then
    printf '  Comparing with the kept copy (%s): %s\n' "${keep#./}" "$label"
    if ! verify_identical "$keep" "${GP[${ui[0]}]}"; then
      printf '  left alone — its contents differ from the kept copy: %s\n' "$label"
      SKIPPED=$((SKIPPED + 1))
      return
    fi
    U_CMP[$u]=1
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  Would link %s path(s) to the kept copy, freeing %s: %s\n' "$known" "$(_fmt_bytes "$G_SIZE")" "$label"
    WOULD=$((WOULD + 1))
    WOULD_BYTES=$((WOULD_BYTES + G_SIZE))
    LINK_RESULT=done
    return
  fi

  # A comparison can take hours on a spinning disk. Look again before touching anything.
  line=$(stat -c '%d %i' -- "$keep" 2>/dev/null) || line=''
  if [ -L "$keep" ] || [ "$line" != "${U_DEV[$c]} ${U_INO[$c]}" ]; then
    printf '  left alone — the kept copy changed while it was being compared: %s\n' "$label"
    SKIPPED=$((SKIPPED + 1))
    return
  fi
  for k in "${ui[@]}"; do
    d=${GP[$k]}
    line=$(stat -c '%d %i %h' -- "$d" 2>/dev/null) || line=''
    if [ -L "$d" ] || [ "$line" != "${U_DEV[$u]} ${U_INO[$u]} ${U_NL[$u]}" ]; then
      printf '  left alone — it changed while it was being compared: %s\n' "$label"
      SKIPPED=$((SKIPPED + 1))
      return
    fi
  done

  # Stage a new link beside every path first — never a forced link, which removes
  # the destination before it finds out the link cannot be made (F5) — and
  # rename nothing unless every one of them was made.
  STAGED=()
  for k in "${ui[@]}"; do
    d=${GP[$k]}
    tmp="${d%/*}/.auditorr-dedupe-$$-$k.tmp"
    if [ -e "$tmp" ] || [ -L "$tmp" ]; then
      _drop_staged
      printf '  left alone — the temporary name beside it is taken (%s): %s\n' "${tmp#./}" "$label"
      SKIPPED=$((SKIPPED + 1))
      return
    fi
    if ! ln -- "$keep" "$tmp" 2>/dev/null; then
      _drop_staged
      LINK_RESULT=aside
      return
    fi
    STAGED+=("$tmp")
    # A pooled filesystem can answer a link with a copy or a symlink (mergerfs's
    # link-exdev modes). Renamed over the file, either would replace it, so the
    # new name must be the kept copy itself before anything is renamed.
    line=$(stat -c '%d %i' -- "$tmp" 2>/dev/null) || line=''
    if [ -L "$tmp" ] || [ "$line" != "${U_DEV[$c]} ${U_INO[$c]}" ]; then
      _drop_staged
      printf '  the filesystem answered the link with something other than a hardlink (a copy or a symlink), so it was removed: %s\n' "$label"
      LINK_RESULT=aside
      return
    fi
  done

  # One rename replaces one path atomically, so a path is never missing and a
  # seeding torrent never sees a gap. A file's paths as a set are not atomic: a
  # failure part-way is FAILED, and running the script again finishes the file.
  for n in "${!STAGED[@]}"; do
    d=${GP[${ui[$n]}]}
    if ! mv -f -- "${STAGED[$n]}" "$d"; then
      _drop_staged
      printf '  FAILED — could not rename the new link over %s.\n' "${d#./}"
      printf '    %s of its %s path(s) now point at the kept copy and the rest are untouched; nothing is missing.\n' "$renamed" "$known"
      printf '    Run the script again to finish this file.\n'
      LINKED_PATHS=$((LINKED_PATHS + renamed))
      FAILED=$((FAILED + 1))
      LINK_RESULT=failed
      return
    fi
    STAGED[$n]=''
    line=$(stat -c '%d %i' -- "$d" 2>/dev/null) || line=''
    if [ "$line" != "${U_DEV[$c]} ${U_INO[$c]}" ]; then
      _drop_staged
      printf '  FAILED — %s is not the kept copy after the rename. Run the script again.\n' "${d#./}"
      LINKED_PATHS=$((LINKED_PATHS + renamed))
      FAILED=$((FAILED + 1))
      LINK_RESULT=failed
      return
    fi
    renamed=$((renamed + 1))
  done
  # Every link to this file was one of the renamed paths, so its data is gone
  # from the disk now — the only point at which space is counted as freed.
  LINKED=$((LINKED + 1))
  LINKED_PATHS=$((LINKED_PATHS + renamed))
  FREED_BYTES=$((FREED_BYTES + G_SIZE))
  printf '  Linked %s path(s) to the kept copy, freeing %s: %s\n' "$renamed" "$(_fmt_bytes "$G_SIZE")" "$label"
  LINK_RESULT=done
}

# link_bucket — every file in BUCKET reports one device. On a pooled filesystem
# (an Unraid share, mergerfs) that can be one device for several drives, so a
# link can still fail. A file whose link fails is set aside, and the set-aside
# files are tried against each other: one fewer each round.
link_bucket() {
  local c u
  while [ "${#BUCKET[@]}" -ge 2 ]; do
    c=$(_pick_canonical)
    ASIDE=()
    for u in "${BUCKET[@]}"; do
      if [ "$u" = "$c" ]; then continue; fi
      if [ "${U_OWN[$u]}" != "${U_OWN[$c]}" ]; then
        printf '  left alone — different owner or permissions from the kept copy (uid:gid:mode %s, kept %s), and linking would change them at this path: %s\n' "${U_OWN[$u]}" "${U_OWN[$c]}" "$(_label "$u")"
        SKIPPED=$((SKIPPED + 1))
        continue
      fi
      link_one "$c" "$u"
      if [ "$LINK_RESULT" = aside ]; then ASIDE+=("$u"); fi
    done
    if [ "${#ASIDE[@]}" -eq 0 ]; then
      return
    fi
    if [ "${#ASIDE[@]}" -eq 1 ]; then
      printf '  left alone — could not be linked to any other copy (a pooled filesystem can refuse a link between two of its drives; otherwise check permissions): %s\n' "$(_label "${ASIDE[0]}")"
      SKIPPED=$((SKIPPED + 1))
      return
    fi
    printf '  %s copies could not be linked to the kept one; trying them against each other\n' "${#ASIDE[@]}"
    BUCKET=("${ASIDE[@]}")
  done
}

group_run() {
  local i p line dev ino nl sz bl bs uid gid mode ftype key u found k c s d
  local idx=() copies=() cand=() devs=()
  U_KEY=(); U_DEV=(); U_INO=(); U_NL=(); U_SZ=(); U_ALLOC=(); U_OWN=(); U_IDX=(); U_CMP=()
  printf '\n[%s/%s] %s copies of one %s file — %s\n' "$G_NUM" "$G_TOTAL" "$G_COPIES" "$(_fmt_bytes "$G_SIZE")" "${GP[0]#./}"

  # 1. Look at every listed path as it is now, and gather the paths by the file
  #    (device and inode) each points at.
  for i in "${!GP[@]}"; do
    p=${GP[$i]}
    if [ -L "$p" ]; then
      printf '  left alone — not a regular file (a symlink): %s\n' "${p#./}"
      SKIPPED=$((SKIPPED + 1))
      continue
    fi
    if [ ! -e "$p" ]; then
      printf '  left alone — missing: %s\n' "${p#./}"
      SKIPPED=$((SKIPPED + 1))
      continue
    fi
    line=$(stat -c '%d %i %h %s %b %B %u %g %a %F' -- "$p" 2>/dev/null) || line=''
    read -r dev ino nl sz bl bs uid gid mode ftype <<<"$line"
    case "$dev$ino$nl$sz$bl$bs" in
      ''|*[!0-9]*)
        printf '  left alone — could not read it: %s\n' "${p#./}"
        SKIPPED=$((SKIPPED + 1))
        continue ;;
    esac
    case "$ftype" in
      'regular file'|'regular empty file') ;;
      'symbolic link')
        printf '  left alone — not a regular file (a symlink): %s\n' "${p#./}"
        SKIPPED=$((SKIPPED + 1))
        continue ;;
      *)
        printf '  left alone — not a regular file (%s): %s\n' "$ftype" "${p#./}"
        SKIPPED=$((SKIPPED + 1))
        continue ;;
    esac
    key="$dev $ino"
    found=''
    for u in "${!U_KEY[@]}"; do
      if [ "${U_KEY[$u]}" = "$key" ]; then found=$u; break; fi
    done
    if [ -z "$found" ]; then
      U_KEY+=("$key"); U_DEV+=("$dev"); U_INO+=("$ino"); U_NL+=("$nl"); U_SZ+=("$sz")
      U_ALLOC+=("$((bl * bs))"); U_OWN+=("$uid:$gid:$mode"); U_IDX+=("$i"); U_CMP+=(0)
    else
      U_IDX[$found]="${U_IDX[$found]} $i"
    fi
  done

  # 2. Listed copies that are already one file need nothing. This is what makes a
  #    second run a no-op and an interrupted one resumable.
  for u in "${!U_KEY[@]}"; do
    idx=(${U_IDX[$u]})
    copies=()
    for k in "${idx[@]}"; do
      s=0
      for c in "${copies[@]+"${copies[@]}"}"; do
        if [ "$c" = "${GC[$k]}" ]; then s=1; fi
      done
      if [ "$s" -eq 0 ]; then copies+=("${GC[$k]}"); fi
    done
    if [ "${#copies[@]}" -gt 1 ]; then
      ALREADY=$((ALREADY + ${#copies[@]} - 1))
      printf '  already linked: %s of these copies are one file: %s\n' "${#copies[@]}" "$(_label "$u")"
    fi
  done

  # 3. Size and completeness, before any copy can be chosen to keep. A sparse
  #    file is the one duplicate cmp cannot catch (DEDUPE F6): its unwritten
  #    parts read as zeros, so two unfinished files can compare equal.
  for u in "${!U_KEY[@]}"; do
    if [ "${U_SZ[$u]}" != "$G_SIZE" ]; then
      printf '  left alone — its size changed since the scan (%s bytes, expected %s): %s\n' "${U_SZ[$u]}" "$G_SIZE" "$(_label "$u")"
      SKIPPED=$((SKIPPED + 1))
      continue
    fi
    if [ "$ALLOW_SPARSE" -eq 0 ] && [ $(( ${U_SZ[$u]} - ${U_ALLOC[$u]} )) -gt 1048576 ]; then
      printf '  left alone — looks unfinished: only %s of %s is on disk (a sparse file). A compressed filesystem can make a complete file look like this; if yours is one, run again with --allow-sparse: %s\n' "$(_fmt_bytes "${U_ALLOC[$u]}")" "$(_fmt_bytes "${U_SZ[$u]}")" "$(_label "$u")"
      SKIPPED=$((SKIPPED + 1))
      continue
    fi
    cand+=("$u")
  done

  # 4. Link within each device, never across (F14).
  for u in "${cand[@]+"${cand[@]}"}"; do
    s=0
    for d in "${devs[@]+"${devs[@]}"}"; do
      if [ "$d" = "${U_DEV[$u]}" ]; then s=1; fi
    done
    if [ "$s" -eq 0 ]; then devs+=("${U_DEV[$u]}"); fi
  done
  for d in "${devs[@]+"${devs[@]}"}"; do
    BUCKET=()
    for u in "${cand[@]}"; do
      if [ "${U_DEV[$u]}" = "$d" ]; then BUCKET+=("$u"); fi
    done
    if [ "${#BUCKET[@]}" -lt 2 ]; then
      if [ "${#devs[@]}" -gt 1 ]; then
        printf '  left alone — no other copy is on its disk: %s\n' "$(_label "${BUCKET[0]}")"
        SKIPPED=$((SKIPPED + 1))
      fi
      continue
    fi
    link_bucket
  done
}'''

_DEDUPE_SUMMARY = """\
echo ""
echo "================================================"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run complete — nothing was changed."
  echo "  Would be linked:  $WOULD file(s), freeing up to $(_fmt_bytes "$WOULD_BYTES")"
  echo "  Already linked:   $ALREADY"
  echo "  Left alone:       $SKIPPED"
  echo "  A dry run makes no links, so it cannot tell whether one will fail between the"
  echo "  drives of a pooled share; the real run tries each and leaves alone any that fail."
  echo "================================================"
  exit 0
fi
echo "Dedupe complete."
echo "  Linked:           $LINKED file(s) now share a copy ($LINKED_PATHS path(s) replaced)"
echo "  Space freed:      $(_fmt_bytes "$FREED_BYTES") — counted only where every link to a copy was replaced"
echo "  Already linked:   $ALREADY"
if [ "$SKIPPED" -gt 0 ]; then
  echo "  Left alone:       $SKIPPED (each with its reason, above)"
else
  echo "  Left alone:       0"
fi
if [ "$FAILED" -gt 0 ]; then
  echo "  FAILED:           $FAILED — a file is only partly linked. Nothing is missing."
  echo "                    Run the script again: it finishes what it started."
fi
echo ""
echo "auditorr notices these changes and scans again shortly; the Dedupe page updates after that scan."
echo "================================================"
if [ "$FAILED" -gt 0 ]; then
  exit 1
fi
exit 0"""


def dedupe_script_units(groups):
    """`([(group, members)], copies left out)` — what a script for `groups` acts on.

    A member with a path outside the script root is left out (§10), and a group
    left with fewer than two members is not scripted at all. The endpoint reads
    this to refuse an empty script and to fill its headers; the builder reads it
    to write the script, so the two cannot disagree.
    """
    units, left_out = [], 0
    for g in groups:
        members = scriptable_members(g)
        left_out += len(g['members']) - len(members)
        if len(members) >= 2:
            units.append((g, members))
    return units, left_out


def build_dedupe_script(groups, *, script_root, generated_at, excluded_count=0, stale=0):
    """The Dedupe script for selected, non-stale groups (DEDUPE §5.4).

    The script is the authority (Principle 1): auditorr classifies and explains,
    and only the script, on the machine that holds the files, decides what is
    linked. Contract — each part run by `backend_tests/test_dedupe.py` in
    `tmp_path`:

    * Inherited from Cleanup's (CLEANUP §5.5): `set -uo pipefail` with no `-e`;
      every path `./`-prefixed with `--` before it; outcome buckets (linked,
      already linked, left alone with a reason, **FAILED**), with FAILED exiting
      1; `--dry-run`; a generated-at stamp that warns past a day; `awk` bytes;
      nothing path- or config-derived in a comment except through
      `_comment_safe` — `script_root` included (S10).
    * Refuses to start without GNU `stat -c`, and says up front how much `cmp`
      will read.
    * **No canonical from auditorr.** Per group the copies are re-stat'ed and
      gathered by inode, bucketed by `%d` (F14), and the copy kept in each
      bucket is the majority owner/mode, then the most links (F11: a copy whose
      owner or mode differs is reported, never relinked).
    * Per copy, failing safe at every step: regular file, not a symlink (F16);
      already the kept file → nothing to do (a re-run is a no-op); size; the
      link count equals the paths listed, or it is left alone (**refuse partial
      replacement**); not sparse (a second F6 guard — `--allow-sparse` for a
      compressed filesystem); `cmp`; a staged `ln` beside every path, **never
      `ln -f`** (F5 — essentrix83's); `mv -f` over each; the inode confirmed.
    * **A device bucket is preliminary** (the 2026-09-10 review's amendment 4):
      a link that fails sets the copy aside, and the set-aside copies are tried
      against each other — at most one round fewer each time — before the rest
      are reported as a conservative skip.
    * **Accounting:** bytes count as freed only when a file's last link is
      replaced, so an interrupted run claims nothing for a half-linked file, and
      running again finishes it.
    """
    units, left_out = dedupe_script_units(groups)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_groups = len(units)
    n_files = sum(len(m) for _, m in units)
    frees = sum(g['size'] * (len(m) - 1) for g, m in units)
    reads = 2 * frees
    root_note = _comment_safe(script_root) if script_root else 'the folders auditorr scans'

    lines = [
        '#!/bin/bash',
        '# auditorr — Dedupe Script',
        f'# Generated: {now_str}',
        f'# {n_groups} group(s) of identical files · {n_files} files · up to {_human_size(frees)} freed.',
        '# That figure is a maximum: every file is checked again when this runs, and the',
        '# summary at the end reports what was actually freed.',
        f'# Comparing the copies reads up to {_human_size(reads)} from disk (cmp reads both in full).',
    ]
    if left_out:
        lines.append(f'# {left_out} copy/copies sit outside the folder this script runs from and are left out.')
    if stale:
        lines.append(f'# {stale} selected group(s) changed since the scan and are left out — scan again to see them.')
    if excluded_count:
        lines.append(f'# {excluded_count} duplicate file(s) match your Excluded Files & Folders settings '
                     f'and never appear here.')
    lines += [
        '#',
        '# For each group, this script will:',
        '#   1. Look at every listed path again. A symlink, a missing file, a size that changed',
        '#      or a file that looks unfinished (sparse) is left alone',
        '#   2. Group the copies by disk, and link only copies on the same one',
        '#   3. Keep the copy with the owner and permissions most copies share, then the most',
        '#      hardlinks. A copy with a different owner or permissions is left alone, because',
        '#      every path of a linked file takes the kept copy\'s',
        '#   4. Leave alone a copy with hardlinks this script does not list: replacing only some',
        '#      of a file\'s paths frees nothing and splits it',
        '#   5. Compare each copy with the kept one byte for byte (cmp)',
        '#   6. Make the new hardlink under a temporary name beside each path, then rename it over',
        '#      the path: nothing is removed before its replacement exists, and no path is ever',
        '#      missing',
        '#   7. Count space as freed only when every link to a copy has been replaced',
        '#',
        '# USAGE:',
        f'#   cd <the folder on your host that auditorr sees as {root_note}>',
        '#   bash dedupe.sh                  # link',
        '#   bash dedupe.sh --dry-run        # check and compare everything; change nothing',
        '#   bash dedupe.sh --allow-sparse   # also link files that look unfinished (compressed filesystems)',
        '#',
        '# Needs GNU stat (any Linux; not macOS). Install "pv" for a progress bar while large',
        '# files are compared; without it you get a heartbeat.',
        '#',
        '# Safe to run again: copies already linked are reported and left alone, and a run that',
        '# was interrupted finishes what it started.',
        '',
        'set -uo pipefail',
        '',
        f'GENERATED_AT={int(generated_at)}',
        'DRY_RUN=0',
        'ALLOW_SPARSE=0',
        'for arg in "$@"; do',
        '  case "$arg" in',
        '    --dry-run|-n) DRY_RUN=1 ;;',
        '    --allow-sparse) ALLOW_SPARSE=1 ;;',
        '    *) echo "Unknown option: $arg (options: --dry-run, --allow-sparse)"; exit 2 ;;',
        '  esac',
        'done',
        '',
        '# BSD and macOS stat have no -c, and every check below reads it.',
        'if ! stat -c %i . >/dev/null 2>&1; then',
        '  echo "ERROR: this script needs GNU stat (stat -c), which BSD and macOS stat do not have."',
        '  echo "  Run it on the Linux machine that holds these files."',
        '  exit 2',
        'fi',
        '',
        _FMT_BYTES,
        '',
        _VERIFY_IDENTICAL,
        '',
        _DEDUPE_RUNNER,
        '',
    ]

    guard = [m[0]['paths'][0]['path'] for _, m in units[:20]]
    if guard:
        lines += [
            '# Working-directory guard. A run replaces paths and never removes one, so a',
            '# finished script still passes it.',
            '_found=0',
            'for _p in ' + ' '.join(shlex.quote('./' + p) for p in guard) + '; do',
            '  if [ -e "$_p" ] || [ -L "$_p" ]; then _found=1; break; fi',
            'done',
            'if [ "$_found" -eq 0 ]; then',
            '  echo "ERROR: This does not look like the folder this script was built for."',
            f'  printf \'  Expected to find: %s\\n\' {shlex.quote(guard[0])}',
            '  echo "  cd into the folder described at the top of this script and try again."',
            '  exit 1',
            'fi',
            '',
        ]
    lines += [
        f'G_TOTAL={n_groups}',
        'LINKED=0; LINKED_PATHS=0; FREED_BYTES=0; ALREADY=0; SKIPPED=0; FAILED=0; WOULD=0; WOULD_BYTES=0',
        '',
        'NOW=$(date +%s 2>/dev/null || true)',
        "case \"$NOW\" in ''|*[!0-9]*) NOW=\"\" ;; esac",
        'if [ -n "$NOW" ] && [ "$NOW" -gt $((GENERATED_AT + 86400)) ]; then',
        '  echo "⚠ This script was built from a scan $(( (NOW - GENERATED_AT) / 3600 )) hours ago."',
        '  echo "  It checks every file again before touching it, so this is only a warning:"',
        '  echo "  anything that changed since is left alone. Regenerate it in auditorr to see what did."',
        '  echo ""',
        'fi',
        '',
        'echo "================================================"',
        'echo "auditorr Dedupe"',
        '[ "$DRY_RUN" -eq 1 ] && echo "DRY RUN — nothing will be changed."',
        f'echo "{n_groups} group(s) · {n_files} files · up to {_human_size(frees)} freed"',
        f'echo "Comparing reads up to {_human_size(reads)} from disk."',
        'echo "================================================"',
        '',
    ]
    for num, (g, members) in enumerate(units, 1):
        lines.append(f'# ── Group {num} of {n_groups} · {len(members)} copies of one '
                     f'{_human_size(g["size"])} file · frees up to '
                     f'{_human_size(g["size"] * (len(members) - 1))}')
        lines.append(f'# {_comment_safe(members[0]["paths"][0]["path"])}')
        lines.append(f'group_begin {num} {int(g["size"])}')
        for m in members:
            lines.append('copy ' + ' '.join(shlex.quote('./' + p['path']) for p in m['paths']))
        lines.append('group_run')
        lines.append('')

    lines.append(_DEDUPE_SUMMARY)
    return '\n'.join(lines) + '\n'

