"""
Torrent source dispatcher. Reads TORRENT_SOURCE from cfg and delegates
to the appropriate backend (_qbit or _qui).

Each backend implements:
  fetch_file_map(cfg)          -> (file_map, sorted_trackers, tracker_snapshot, report)
  test_connection(payload)     -> {'ok': bool, 'version': str|None, 'error': str|None, 'instances': [...]}
  connection_info(cfg)         -> {'version': str|None, 'instance_summary': str, 'instances': [...]}
  fetch_save_path_hint(payload)-> {'save_path': str|None, 'version': str|None, 'torrent_count': int, 'seeding_size': int, 'instances': [...]}
  fetch_torrent_details(cfg, items) -> {hash: {'uploaded', 'ratio', 'added_on', 'tracker_health', 'tracker_msg'}}
  remove_torrents(cfg, items, delete_files=True) -> int (count submitted for deletion)
  list_torrents(cfg) -> ([rows], report)
  fetch_torrent_file_paths(cfg, items) -> {hash: [paths] | None}   # None = could not ask

**Absence is reportable.** Every primitive here answers two different questions
and must never collapse them: "the client says there is nothing" and "the client
could not be asked". An empty collection means the former only. The latter is
carried by an explicit `None` (per torrent) or by the `report` dict (per scan).

**Completion is a third answer, not a second vocabulary.** `status` says what
the client is *doing* ('Seeding' / 'Downloading' / 'Paused'); it has never said
whether the payload is *whole*, and a paused incomplete torrent is
indistinguishable from a paused complete one by state string alone. `torrent_complete`
answers that separately, tri-state, and its "could not determine" is the same
`None` the primitives above use.
"""

import os


class SourceConnectionError(Exception):
    """Raised by backends when they cannot connect or authenticate."""


# ---------------------------------------------------------------------------
# Source report — the per-scan "how completely could we ask" channel
# ---------------------------------------------------------------------------

# Notes are diagnostic strings for the debug report and the UI. Bounded because
# the natural thing to write is one per torrent, and a 15k-torrent client with a
# flaky WebUI would otherwise put 15k strings into an app_meta row.
_MAX_REPORT_NOTES = 10


def new_source_report(source):
    """A fresh per-scan completeness report.

    `fetch_file_map` and `list_torrents` fill this in as they go, and the audit
    persists it. Every field answers some form of "what could we not see":

      torrent_count       torrents the client listed (0 is a real answer)
      file_map_size       paths the map ended up claiming
      listing_failures    torrents whose per-file listing could not be fetched
      listing_recovered   of those, how many were claimed from disk instead
      listing_unresolved  of those, how many are still unaccounted for — these
                          are the files that will read as orphaned without ever
                          having been asked about
      instances_*         qui only; qbit reports a single instance
      partial             the scan is known-incomplete for any reason at all

    Two more answer a different question — not "what could we not see" but
    "which of these torrents is not finished". They live here rather than in a
    parallel channel because an unfinished download is a third answer alongside
    "could not ask" and "no files", and every consumer that reads one reads the
    other:

      incomplete_torrents  the client says the payload is not whole yet
      completion_unknown   the client exposed no usable completion field
    """
    return {
        'source':              source,
        'torrent_count':       0,
        'file_map_size':       0,
        'listing_failures':    0,
        'listing_recovered':   0,
        'listing_unresolved':  0,
        'incomplete_torrents': 0,
        'completion_unknown':  0,
        'instances_total':     0,
        'instances_ok':        0,
        'instances_failed':    [],
        'partial':             False,
        'notes':               [],
    }


def report_note(report, message):
    """Record a diagnostic note on a source report (bounded, de-duplicated)."""
    if report is None:
        return
    notes = report.setdefault('notes', [])
    if message not in notes and len(notes) < _MAX_REPORT_NOTES:
        notes.append(message)


def report_instance_failure(report, name, reason):
    """Record an instance that could not be listed, and mark the scan partial."""
    if report is None:
        return
    report.setdefault('instances_failed', []).append(
        {'name': str(name), 'reason': str(reason)[:300]})
    report['partial'] = True


# ---------------------------------------------------------------------------
# Completion — is this torrent's payload whole?
# ---------------------------------------------------------------------------

# qBittorrent's "Append .!qB extension to incomplete files" option. Off by
# default and absent from TRaSH's layout, so most installs never see it; where
# it is on, the on-disk name carries the suffix while the file listing reports
# the final name, path equality fails, and a file being actively written reads
# as an orphan with a green "frees X" beside it (CLEANUP C4a).
INCOMPLETE_SUFFIX = '.!qB'


def _posix(path):
    return (path or '').replace('\\', '/')


def torrent_complete(progress, completion_on):
    """True | False | None — is this torrent's payload whole?

    A **fallback chain, deliberately not a union.** Measured 2026-09-11 against
    a live client (ROADMAP §0.4):

    * `progress` is already computed over the **wanted** files. A season pack
      with five files set to *Do not download* — 32% of its payload wanted —
      reports `progress == 1.0` and `amount_left == 0`, because libtorrent
      computes `total_wanted_done / total_wanted` while the UI's 42.5% is
      `completed / total_size`. So `progress` alone is correct for the
      deprioritized case, and the per-file fan-out once held in reserve as "the
      fully correct rule" is exactly what `progress` already does.
    * `completion_on` is therefore the answer only when `progress` is missing,
      and **must not be OR'd with it**. A torrent that completed and was later
      rechecked (files deleted, a partial re-download) has `progress < 1.0`
      while `completion_on` retains its original timestamp; the union reads that
      as complete, which fails in the one direction DEDUPE F6 says must not —
      including an unfinished file in a duplicate group is how two torrents get
      hardlinked onto one inode and both end up corrupt. An `or` over a reliable
      signal can only ever add false completes.
    * `None` means the client exposed neither field. It is the same "could not
      ask" this module's other primitives carry, and each consumer resolves it
      in its own fail-safe direction: dedupe excludes the file, Triage shows the
      row with its status, orphan classification claims the payload either way.
    """
    if progress is not None:
        try:
            p = float(progress)
        except (TypeError, ValueError):
            p = None
        if p is not None:
            # A value above 1.0 can only be a 0-100 scale. Measured 0-1 on both
            # backends, but reading 42.5 as "complete" is the corrupting
            # direction, so the scale is inferred rather than assumed.
            return p >= 100.0 if p > 1.0 else p >= 1.0
    if completion_on is not None:
        try:
            return int(completion_on) > 0
        except (TypeError, ValueError):
            return None
    return None


def content_rooted_paths(content_path, torrent_name, file_names):
    """Where a torrent's files sit right now, rooted at the client's `content_path`.

    `save_path` is the payload's *final* location. `content_path` is where the
    client says it is at this moment, which is the one that survives an
    incomplete directory (CLEANUP C4b): with a temp path configured, an
    in-flight torrent's bytes are under it and `save_path/name` holds nothing.

    **`content_path` is sometimes a file and sometimes a directory** — qBittorrent
    defines it as the absolute file path for a single-file torrent and the root
    folder for a multi-file one. On the reference box 192 of 200 completed
    torrents have `content_path == save_path/name`; all 8 that differ are
    single-file torrents inside a release folder, where it descends one level
    past it (ROADMAP §0.4). Treating it as a directory unconditionally returns
    nothing for every one of them.

    Joined posix-style, not with `os.path.join`. A client file name already
    contains `/` separators, so joining one onto a root with the native
    separator yields a path that mixes both on Windows and can no longer be
    compared with the path built from `save_path` — which would make the
    de-duplication in `incomplete_claims` platform-dependent and, per CLAUDE.md's
    `_local_to_abs` note, make the checked-in tests exercise a different branch
    than the container.
    """
    if not content_path:
        return []
    names = [n for n in file_names if n]
    if not names:
        return []
    root = _posix(content_path).rstrip('/')
    if len(names) == 1 and \
            root.rsplit('/', 1)[-1] == _posix(names[0]).rstrip('/').rsplit('/', 1)[-1]:
        return [root]
    prefix = (_posix(torrent_name).rstrip('/') + '/') if torrent_name else ''
    out = []
    for n in names:
        rel = _posix(n)
        rel = rel[len(prefix):] if prefix and rel.startswith(prefix) else rel
        out.append(f"{root}/{rel.lstrip('/')}")
    return out


def incomplete_claims(content_path, torrent_name, file_names, final_paths):
    """Extra on-disk paths an unfinished torrent's payload may occupy.

    Two client options move the bytes away from where the file listing says they
    will end up, and both make a live download read as an orphan: a separate
    incomplete directory (`content_path` follows it) and the `.!qB` suffix. Both
    are opt-in and neither is in TRaSH's layout, which is why CLEANUP C4 is a
    per-install question rather than a critical — but where they are on, nothing
    downstream can tell the difference between "being written right now" and
    "nothing claims this".

    Claiming the extra spellings is free where the options are off: the derived
    paths are then identical to `final_paths` and dedupe to nothing. Only
    computed for torrents that are not known-complete, so a healthy library's
    `file_map` is byte-identical to before — which also keeps the plausibility
    guard's `file_map_size` baseline unmoved.

    The `.!qB` variants keep the caller's own spelling of each final path, since
    those have to match the keys the walk builds; only the equality test is
    separator-agnostic.
    """
    claims = []
    seen   = {_posix(p) for p in final_paths}

    def _claim(p):
        key = _posix(p)
        if key not in seen:
            seen.add(key)
            claims.append(p)

    for p in content_rooted_paths(content_path, torrent_name, file_names):
        _claim(p)
    for p in list(final_paths) + list(claims):
        _claim(p + INCOMPLETE_SUFFIX)
    return claims


def remap_path(path, remote_path, local_path):
    """Translate a client-side absolute path into auditorr's view of it.

    Both backends did this inline for `save_path` only. `content_path` needs the
    identical treatment or claiming it claims a path the walk can never match.
    """
    if not (path and remote_path):
        return path
    if path.startswith(remote_path) and path[len(remote_path):][:1] in ('/', ''):
        return local_path + path[len(remote_path):]
    return path


def _walk_payload(root):
    """Every file at or under `root`; [] if it is neither a file nor a directory."""
    try:
        if os.path.isfile(root):
            return [root]
        if not os.path.isdir(root):
            return []
        found = []
        for dir_root, _, dir_files in os.walk(root):
            for fname in dir_files:
                found.append(os.path.join(dir_root, fname))
        return found
    except OSError:
        return []


def disk_fallback_paths(save_path, torrent_name, content_path=''):
    """On-disk file paths for a torrent whose client file listing failed.

    A failed listing is an *unknown*, and an unknown left alone becomes a
    positive claim of orphanhood by default — every file of that torrent turns
    up in the walk with no client entry against it. Enumerating the payload from
    disk converts the unknown into a conservative *claimed* instead, which is
    the fail-safe direction and the only thing that lets a failure be attributed
    to specific paths at all: the paths are precisely what the listing failed to
    return.

    Both `content_path` and `save_path/name` are walked and the results
    **unioned**, rather than taking whichever answers first. `content_path` is
    what this argument was added for — a torrent downloading into a temp
    directory has no bytes at `save_path/name` at all, so the fallback was
    looking in the right place only for torrents that had already finished
    (ROADMAP §0.3) — but preferring it would *narrow* the answer for the
    commonest shape it differs on: a single-file torrent inside a release
    folder, where `content_path` is the file and `save_path/name` is the folder
    holding it plus any sidecars. Over-claiming is the fail-safe direction for a
    failed listing, by this function's own argument, and the union also leaves
    completed torrents claiming exactly what they claimed before this argument
    existed. `content_path` is branched on rather than walked blindly, per
    `content_rooted_paths`.

    Returns [] when nothing is found at either — which is itself information,
    and is counted as `listing_unresolved` rather than passed off as "no files".
    """
    roots = []
    if content_path:
        roots.append(content_path)
    if save_path and torrent_name:
        roots.append(os.path.join(save_path, torrent_name))
    found, seen = [], set()
    for root in roots:
        for p in _walk_payload(root):
            key = _posix(p)
            if key not in seen:
                seen.add(key)
                found.append(p)
    return found


def torrent_claimed_paths(save_path, torrent_name, content_path, file_names, complete):
    """Every on-disk path one torrent claims — **the** claim rule, in one place.

    "Orphaned" is the absence of a claim, so what counts as a claim decides what
    Cleanup offers for `rm`. It used to be written inline in both backends'
    `fetch_file_map` loops, and CLEANUP C3's live re-verify needs to ask the
    same question of a single torrent at script generation. A second copy of
    the rule there would be a rule that disagrees with the audit, and the one
    direction it must never disagree in is claiming less. So both backends and
    `app._cleanup_live_claims` call this.

    `save_path` and `content_path` are already remapped to auditorr's view
    (`remap_path`). `file_names` is the client's per-torrent listing, relative
    to `save_path`, or **`None` when the listing is not usable** — and *which*
    listings are unusable is the caller's to decide, deliberately: the qBittorrent
    backend means "the call failed", while qui also treats an empty listing that
    way because qui may not expose per-torrent file lists at all. Keeping that
    decision at the call site is what keeps each backend's `file_map`
    byte-identical to what it built before this function existed — and
    `file_map_size` is the plausibility guard's baseline.

    A torrent claims:

    * its listing's names joined on `save_path` — with `os.path.join`, as both
      backends always did (see `content_rooted_paths` for why the *extra* claims
      are posix; changing this join would move `file_map_size`);
    * plus `incomplete_claims(...)` when it is not known to be complete (C4a/C4b);
    * or, with no usable listing, `disk_fallback_paths(save_path, name,
      content_path)` — which over-claims, the fail-safe direction, and returns
      `[]` when nothing is on disk at either root. The caller counts that as
      `listing_unresolved`; it is never "this torrent has no files".
    """
    if file_names is None:
        return disk_fallback_paths(save_path, torrent_name, content_path)
    full_paths = [os.path.join(save_path, n) for n in file_names]
    if complete is not True:
        # An unfinished payload may not be where the listing says it will end
        # up. Adds nothing on a client with neither the temp directory nor the
        # `.!qB` suffix enabled.
        full_paths = full_paths + incomplete_claims(
            content_path, torrent_name, file_names, full_paths)
    return full_paths


# Substrings (lowercased) of tracker status messages that mean the torrent is
# no longer registered on the tracker — trumped, deleted, or nuked. Seeding
# such a torrent earns nothing; it is the strongest "dead weight" signal the
# Triage workflow has. Kept conservative: passkey/authorization problems are
# NOT included because the torrent itself may still be alive.
UNREGISTERED_TRACKER_PATTERNS = (
    'unregistered',
    'not registered',
    'torrent not found',
    'torrent does not exist',
    'torrent not exists',
    'infohash not found',
    'torrent has been deleted',
    'torrent deleted',
    'trumped',
    'nuked',
)


# Merge priority for per-path tracker health when several torrents claim the
# same file (cross-seeds): least-dead wins — a path with any live torrent
# must never be classified as a dead seed, because deleting its files would
# break that live torrent.
HEALTH_RANK = {'unregistered': 0, 'unknown': 1, 'not_working': 2, 'working': 3}


def classify_tracker_entries(entries):
    """Classify a torrent's tracker list into a single health verdict.

    entries: [{'url': str, 'status': int|None, 'msg': str}] — qBittorrent
    tracker status codes: 0=disabled, 1=not contacted, 2=working, 3=updating,
    4=not working; qBittorrent 5.x adds 5=tracker error and 6=unreachable.

    Returns (health, msg) where health is one of
    'unregistered' | 'working' | 'not_working' | 'unknown'.
    """
    real = [e for e in entries
            if (e.get('url') or '').startswith(('http', 'udp'))]
    for e in real:
        m = (e.get('msg') or '').strip()
        if m and any(p in m.lower() for p in UNREGISTERED_TRACKER_PATTERNS):
            return 'unregistered', m
    statuses = [e.get('status') for e in real]
    if any(s in (2, 3) for s in statuses):
        return 'working', ''
    if any(s in (4, 5, 6) for s in statuses):
        first_msg = next(((e.get('msg') or '').strip() for e in real
                          if e.get('status') in (4, 5, 6) and (e.get('msg') or '').strip()), '')
        return 'not_working', first_msg
    return 'unknown', ''


# Imports come after SourceConnectionError so backends can import it without
# triggering a circular-import error (the name is already bound by the time
# Python starts importing the sub-modules).
from sources._qbit import (  # noqa: E402
    fetch_file_map        as _qbit_fetch_file_map,
    test_connection       as _qbit_test_connection,
    connection_info       as _qbit_connection_info,
    fetch_save_path_hint  as _qbit_fetch_save_path_hint,
    fetch_torrent_details as _qbit_fetch_torrent_details,
    remove_torrents       as _qbit_remove_torrents,
    list_torrents         as _qbit_list_torrents,
    fetch_torrent_file_paths as _qbit_fetch_torrent_file_paths,
)
from sources._qui import (  # noqa: E402
    fetch_file_map        as _qui_fetch_file_map,
    test_connection       as _qui_test_connection,
    connection_info       as _qui_connection_info,
    fetch_save_path_hint  as _qui_fetch_save_path_hint,
    fetch_torrent_details as _qui_fetch_torrent_details,
    remove_torrents       as _qui_remove_torrents,
    list_torrents         as _qui_list_torrents,
    fetch_torrent_file_paths as _qui_fetch_torrent_file_paths,
)


def _source(cfg):
    return cfg.get('TORRENT_SOURCE', 'qbit')


def fetch_file_map(cfg, unresolved_roots=None):
    """(file_map, sorted_trackers, tracker_snapshot, report).

    `report` is a `new_source_report` dict describing how completely the client
    could be asked — see the module docstring. The audit reads it to decide
    whether this scan's orphan classification is trustworthy enough to persist.

    `unresolved_roots`, when a list, receives the remapped `save_path` (and
    `content_path`, if any) of every torrent whose listing failed *and* whose
    disk fallback found nothing — the torrents counted as `listing_unresolved`.
    Their files could be anywhere under those roots, so the audit marks the
    orphans there `unverified` (CLEANUP §5.3). **An out-parameter, in memory
    only, on purpose:** the report is persisted and reaches
    `/api/debug/report`, which must stay free of paths.
    """
    if _source(cfg) == 'qui':
        return _qui_fetch_file_map(cfg, unresolved_roots=unresolved_roots)
    return _qbit_fetch_file_map(cfg, unresolved_roots=unresolved_roots)


def test_connection(payload):
    if payload.get('TORRENT_SOURCE', 'qbit') == 'qui':
        return _qui_test_connection(payload)
    return _qbit_test_connection(payload)


def connection_info(cfg):
    if _source(cfg) == 'qui':
        return _qui_connection_info(cfg)
    return _qbit_connection_info(cfg)


def fetch_save_path_hint(payload):
    if payload.get('TORRENT_SOURCE', 'qbit') == 'qui':
        return _qui_fetch_save_path_hint(payload)
    return _qbit_fetch_save_path_hint(payload)


def fetch_torrent_details(cfg, items):
    """Live per-torrent detail lookup for workflow pages.

    items: [{'hash': str, 'instance_id': int|None}] — instance_id is only
    meaningful for qui. Returns {hash: details} for every hash that could be
    resolved; missing hashes simply have no entry.
    """
    if _source(cfg) == 'qui':
        return _qui_fetch_torrent_details(cfg, items)
    return _qbit_fetch_torrent_details(cfg, items)


def remove_torrents(cfg, items, delete_files=True):
    """Delete torrents (and optionally their files) from the client.

    items: [{'hash': str, 'instance_id': int|None}] — instance_id is only
    meaningful for qui. Returns the number of torrents submitted for deletion.
    Destructive: callers must check the ALLOW_CLIENT_DELETE config flag first.
    """
    if _source(cfg) == 'qui':
        return _qui_remove_torrents(cfg, items, delete_files)
    return _qbit_remove_torrents(cfg, items, delete_files)


def list_torrents_detailed(cfg):
    """(rows, report) — the listing plus which instances actually answered.

    Callers that can act on a partial answer (report it, narrow a group, refuse
    a delete) use this. Everything else uses `list_torrents`, which refuses a
    partial answer outright rather than handing over a short list that looks
    complete.
    """
    if _source(cfg) == 'qui':
        return _qui_list_torrents(cfg)
    return _qbit_list_torrents(cfg)


def list_torrents(cfg):
    """Live, light listing of every torrent (Trumped workflow group resolution).

    Raises `SourceConnectionError` if any instance failed to answer. A short
    listing is not a smaller answer here, it is a wrong one: a cross-seed group
    resolved against it loses the members that lived on the instance that did
    not reply, and `execute` then deletes their payload out from under them.
    """
    rows, report = list_torrents_detailed(cfg)
    failed = report.get('instances_failed') or []
    if failed:
        names = ', '.join(f.get('name', '?') for f in failed[:3])
        raise SourceConnectionError(
            f"{len(failed)} of {report.get('instances_total', '?')} torrent-client "
            f"instance(s) could not be listed ({names}). The listing would be "
            f"incomplete, so it is not being used.")
    return rows


def fetch_torrent_file_paths(cfg, items):
    """Client-side file paths per torrent — {hash: [paths] | None}.

    **`None` means the listing could not be fetched; `[]` means the client
    answered that there are none.** Callers must not conflate them: an empty
    list tests as "shares no paths with anything", which silently shrinks every
    set membership built from it — a cross-seed group down to its seed, a
    removal partition down to "nothing else claims these files".
    """
    if _source(cfg) == 'qui':
        return _qui_fetch_torrent_file_paths(cfg, items)
    return _qbit_fetch_torrent_file_paths(cfg, items)
