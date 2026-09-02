"""Next steps — the "this is your workflow" page.

The spine is a real, dynamically ordered workflow: the five existing workflows
ranked by what needs the user *now*. The gamification is deliberately secondary
— a side quest. Design test: strip the prizes and this must still be the most
useful page in auditorr.

The prize layer is modelled on **Progress Quest**: the fun is density and
absurd granularity, not stakes. Ladders have many thresholds so something is
always about to tick over, tier names are mock-heroic, and the page says out
loud that none of it does anything. That contrast — faux-epic titles bolted to
sysadmin drudgery, rendered on a calm instrument surface — is the joke.

Phase 1 is derivable entirely from config + audit history — there is no event
ledger yet, so nothing here invents a number it cannot justify. Ladders measure
*state* (library size, health peak, streaks, cross-seed) rather than deltas;
the reclaimed-bytes ladders arrive with Tier A awards in phase 2.

Row priority is driven by **health-score points actually recoverable** in each
category, which the audit already computes (`hl_score`/`hl_max` and friends in
`process_health_metrics`). That is honest, already on screen elsewhere, and it
doubles as the prize: "you are losing 6.4 of 10 points here."

See prompts/NEXT_STEPS.md for the full design record.
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# Ordering is **staged and sequenced**, not scored.
#
# A library has a natural order of operations, and it is the same order for
# everyone:
#
# There are two steps, and they differ in *nature*, not just sequence:
#
#   Step 1 — baseline. Concrete work you do once at the start and then *guard*.
#     1. Cleanup  — clear the torrent directory of orphans
#     2. Dedupe   — collapse duplicate files
#   ──────────────  that is a clean baseline
#   Deliberately not called "one-time": you clear it once, but an arr
#   re-downloading something or a move going wrong brings it back, which is the
#   whole reason these are modelled as zombies rather than as a checklist. The
#   distinction from step 2 is that a clean baseline is an achievable *state*;
#   step 2 has no such state to reach.
#   Step 2 — ongoing. Work that never has a last item.
#     3. Triage   — maintenance: what isn't imported, and why. New downloads
#                   and upgrades keep refilling this pile forever.
#     4. Backfill — aspirational: hardlink more of what you already own. It
#                   improves; it does not complete.
#
# Two consequences worth stating, because both were bugs in earlier passes:
#
#   * **Hardlinked media is weighted at 70 of the 100 health points because it
#     is _hard_, not because it is an early priority.** Ranking by score — or
#     even by score-per-effort — hands a brand-new user the slowest, fiddliest
#     workflow in the product as step one. The stage gate is structural: no
#     hardlink gap, however catastrophic, jumps ahead of baseline work.
#   * **The sequence inside a stage is fixed, not impact-ranked.** A page that
#     teaches always teaches the same order; a list that reshuffles by whichever
#     number is biggest today teaches nothing. Magnitude still decides *state*
#     (fix vs optimize), which is what actually promotes a row.
STAGE = {
    'cleanup': 'baseline', 'dedupe': 'baseline',
    'triage': 'ongoing', 'backfill': 'ongoing',
    'trumped': 'ondemand',
}
STAGE_LABEL = {
    'baseline': 'Baseline',
    'ongoing':  'Ongoing',
    'ondemand': 'On demand',
}
# The character of each job, said plainly. Sets expectations honestly: two of
# these end, two of them don't.
NATURE = {
    'cleanup':  'Clear once, then watch',
    'dedupe':   'Clear once, then watch',
    'triage':   'Keeps coming back',
    'backfill': 'Never really finishes',
    'trumped':  'Only when a tracker asks',
}

# Each workflow earns its prize in a different *shape*, because the work has a
# different shape. A single ladder for all four would misdescribe three of them:
#
#   zombie   — Cleanup, Dedupe. You should have killed these for good, but they
#              claw their way back: an arr re-downloads something, a move goes
#              wrong. So there are two rewards, not one — a **kill** every time
#              you drive the count back to zero (repeatable, cumulative), and a
#              **streak** for how long the clean state holds. When they rise
#              again you get a cheeky badge rather than a penalty, because a
#              break is information the user wants ("something upstream is
#              misbehaving"). Nothing here ever subtracts.
#   shovel   — Triage. The workhorse. A pile that refills forever: an arr grabs
#              a lower-quality upgrade, something fails to import, and the
#              mountain grows again. Rewarded per shovelful, cumulatively and
#              permanently — the pile going back up never costs you what you
#              already dug. There is always something to do and always a point
#              for doing it.
#   crystal  — Backfill. Aspirational polish toward a perfect 100%. It gets
#              better and worse as the library grows, so the ladder tracks your
#              **best ever** and ratchets: a bad week cannot take a tier away.
#   tribute  — Trumped. The only workflow you do not start: a tracker PMs you,
#              and complying wins you nothing except continued good standing.
#              So it pays in a tally of compliance rather than progress — a
#              discrete, countable act with no pile behind it and no state that
#              gets better. Counted at execute time, not at audit time, because
#              a swap is an event and leaves no trace in the next scan.
REWARD_KIND = {
    'cleanup': 'zombie', 'dedupe': 'zombie',
    'triage': 'shovel', 'backfill': 'crystal',
    'trumped': 'tribute',
}

# Which ladders a given workflow actually feeds. Used to show a concrete carrot
# on each row — "do this and you get that" — instead of leaving the prize layer
# as an abstract shelf the user has to connect to their own actions. Ladders
# absent here are library-wide (Hoarder, Auditor, Chronicler…) and appear only
# in the top shelf, because no single workflow moves them.
LADDER_OWNER = {
    'exterminator': ('cleanup',),
    'sentinel':     ('cleanup',),
    'tidiness':     ('cleanup',),
    'clonehunter':  ('dedupe',),
    'shoveler':     ('triage',),
    'purity':       ('triage',),
    'lapidary':     ('backfill',),
    'alchemist':    ('backfill',),
    'kingmaker':    ('trumped',),
    # The cross-cutting ones: nothing is wrong anywhere, so every pile-clearing
    # workflow feeds them. Flawless additionally needs a perfect hardlink ratio,
    # which is Backfill's job.
    'conservator':  ('cleanup', 'dedupe', 'triage'),
    'unblemished':  ('cleanup', 'dedupe', 'triage'),
    'firebrigade':  ('cleanup', 'dedupe'),
    'flawless':     ('cleanup', 'dedupe', 'triage', 'backfill'),
}

# Empty progress record. Persisted in app_meta under `ns_progress` and advanced
# once per audit, so the polled endpoint never recomputes history.
EMPTY_PROGRESS = {
    'shoveled': 0,            # cumulative not-imported items resolved, ever
    'hl_peak': 0.0,           # best hardlink % ever reached
    'orphan_clean_since': None,
    'dupe_clean_since': None,
    'orphan_kills': 0,        # times orphans were driven back to zero
    'dupe_kills': 0,
    'orphan_breaks': 0,       # times a clean orphan state regressed
    'dupe_breaks': 0,
    # When the current mess started, per zombie. Latched so the *next* kill can
    # be timed: a library that is genuinely well kept stops earning kills (a
    # kill needs a mess to return first), so the axis that still discriminates
    # between two clean libraries is how fast a mess dies once it appears.
    'orphan_break_at': None,
    'dupe_break_at': None,
    'fast_fixes': 0,          # messes cleared within _FAST_FIX_HOURS of appearing
    # Consecutive-clean streak across all three piles at once. Sentinel watches
    # orphans alone; this is the one a library in genuinely good shape can run.
    'immaculate_since': None,
    'last_ni_count': None,
    'last_excl_fp': None,
    # Last scan's dead cross-seed registrations, by hash. The shovel counter
    # diffs against this: several dead registrations can ride one healthy
    # carrier record, so a path-based signature diff cannot see one of three go.
    'last_dead_regs': [],
    # Trumped is the one workflow whose work leaves no trace in the next scan —
    # the swap deletes one release and grabs another, and the library ends up
    # roughly where it started. So it is counted where it happens (the execute
    # endpoint, via `record_trump`) rather than derived from audit state.
    'trumps': 0,              # completed trump swaps, ever
    'trump_torrents': 0,      # registrations retired by those swaps (cross-seed groups)
    'trump_max_group': 0,     # biggest cross-seed group retired in one swap
    'last_trump_at': None,
    # Everything below exists to make the prize layer **ratchet**. Most ladders
    # and feats read current state, which can regress — a library that shrinks,
    # a tracker you stop using, a zombie that rises. Without latching, a break
    # would silently claw back points already awarded, which is precisely the
    # punishment this design refuses to hand out. Earned is earned.
    'peaks': {},              # ladder id -> best value ever seen
    'feats_earned': [],       # feat ids, latched on first earn
}

# Ceiling on the persisted dead-registration hash set (see `last_dead_regs`).
_DEAD_REG_CAP = 10000

# How quickly a returning mess has to be cleared to count as a fast fix. A day
# is the honest unit: audits are hourly at best, most people look at this once
# an evening, and "I dealt with it the same day" is the behaviour worth paying
# for. Anything tighter would mostly measure scan cadence.
_FAST_FIX_HOURS = 24

# The canonical sequence. This is the sort order inside every bucket.
WORKFLOW_ORDER = ('cleanup', 'dedupe', 'triage', 'backfill', 'trumped')

# `fix` outranks `blocked` deliberately: a real problem the user can act on
# right now beats a setup gap, and setup gaps already have their own checklist
# section. Without this, a user who simply doesn't run Radarr would have a
# blocked row pinned to the hero slot forever.
STATE_RANK = {'fix': 0, 'blocked': 1, 'optimize': 2, 'maintain': 3, 'standby': 4}


def _bucket(row):
    """Priority buckets, best first.

    Baseline work outranks the long game whenever it is actionable at all —
    a couple of stray orphans is a 30-second job, and clearing it promotes the
    next row immediately.
    """
    stage, state = STAGE.get(row['id'], 'ongoing'), row['state']
    if state in ('fix', 'optimize'):
        return (0 if stage == 'baseline' else 1) * 2 + (0 if state == 'fix' else 1)
    return {'blocked': 4, 'maintain': 5, 'standby': 6}.get(state, 7)

# Deliberately long. Progress Quest's appeal was that the next level was always
# visible and always close; a seven-rank ladder tops out and stops mattering.
RANKS = [
    # Thresholds are absolute, not a share of the available total, so a user's
    # rank can never fall. Corollary: **do not raise these** — adding ladders,
    # rungs or feats raises everyone's points (fine, and the only safe way to
    # grow this layer), but raising a threshold would demote people, which is
    # the one thing this layer must never do. Lowering is always safe.
    #
    # **Every rank must be reachable.** Unlike a ladder, the rank scale has a
    # hard ceiling: total points are bounded by "every rung of every ladder,
    # every feat, full setup". The first cut ran to 800,000 against a ceiling of
    # ~138,000, so the top *seven* names could never be seen by anyone, and a
    # three-year 60 TB library topped out at rank 9 of 20 — half the scale was
    # scenery. `test_every_rank_is_reachable` now fails the build if the top
    # threshold ever drifts back above what is earnable.
    #
    # Calibrated against measured profiles: a fresh install sits at rank 1, a
    # day-one library lands around 4, a year in around 9, a three-year veteran
    # around 11, and the last few rungs are genuinely for completionists.
    (0,       'Unaudited'),
    (2500,    'Apprentice Indexer'),
    (6000,    'Journeyman Linker'),
    (10000,   'Hardlink Adept'),
    (14000,   'Orphan Warden'),
    (19000,   'Seed Custodian'),
    (24000,   'Library Curator'),
    (30000,   'Master Archivist'),
    (37000,   'Grand Deduplicator'),
    (45000,   'Keeper of Inodes'),
    (54000,   'Warden of the Array'),
    (64000,   'Duke of Deduplication'),
    (75000,   'Lord of the Hardlink'),
    (87000,   'Sovereign of Seeds'),
    (100000,  'High Priest of Hardlinks'),
    (112000,  'Filesystem Ascendant'),
    (124000,  'Avatar of the Array'),
    (135000,  'Demigod of Disk'),
    (143000,  'The Inode Eternal'),
    (150000,  'Ozymandias'),
]

GB = 1024 ** 3
TB = 1024 ** 4
PB = 1024 ** 5


# ---------------------------------------------------------------------------
# Setup tier — derivable from config + audit history, no event ledger needed.
# This is what makes the page useful on a brand-new install.
# ---------------------------------------------------------------------------

def _setup_steps(cfg, has_audit):
    source = cfg.get('TORRENT_SOURCE', 'qbit')
    source_ok = bool(cfg.get('QUI_HOST') if source == 'qui' else cfg.get('QB_HOST'))
    # MEDIA_PATH/LOCAL_PATH ship with non-empty defaults, so "is it set" would
    # tick this box on a fresh install that has never seen a real folder. Check
    # the directories actually resolve inside the container instead — the same
    # question Config's "Test Paths" answers.
    paths_ok = all(
        bool(p) and os.path.isdir(p)
        for p in (cfg.get('MEDIA_PATH'), cfg.get('LOCAL_PATH'))
    )
    sonarr_ok = bool(cfg.get('SONARR_URL') and cfg.get('SONARR_API_KEY'))
    radarr_ok = bool(cfg.get('RADARR_URL') and cfg.get('RADARR_API_KEY'))
    for conn in (cfg.get('ARR_CONNECTIONS') or []):
        if not conn.get('url'):
            continue
        if conn.get('service') == 'sonarr':
            sonarr_ok = True
        elif conn.get('service') == 'radarr':
            radarr_ok = True

    return [
        {'id': 'source', 'label': f"Connect {'qui' if source == 'qui' else 'qBittorrent'}",
         'hint': 'auditorr needs to see your torrents to tell them apart from orphans.',
         'done': source_ok, 'points': 50, 'tab': 'config'},
        {'id': 'paths', 'label': 'Point at your media and torrent folders',
         'hint': 'The two halves of the hardlink. Without both, nothing can be cross-referenced.',
         'done': paths_ok, 'points': 50, 'tab': 'config'},
        {'id': 'sonarr', 'label': 'Connect Sonarr',
         'hint': 'Optional. Unlocks Backfill and Triage verdicts for TV.',
         'done': sonarr_ok, 'points': 25, 'tab': 'config'},
        {'id': 'radarr', 'label': 'Connect Radarr',
         'hint': 'Optional. Unlocks Backfill and Triage verdicts for films.',
         'done': radarr_ok, 'points': 25, 'tab': 'config'},
        {'id': 'first_scan', 'label': 'Run your first audit',
         'hint': 'Everything on this page comes from the scan. Nothing works until one completes.',
         'done': has_audit, 'points': 100, 'tab': 'config'},
    ]


# ---------------------------------------------------------------------------
# The spine — one row per workflow, state + recoverable score points.
# ---------------------------------------------------------------------------

def exclusion_fingerprint(cfg):
    """Stable hash of every exclusion input.

    Exclusions raise the health score by *hiding* problems, and Triage actively
    suggests them, so anything inferred from a drop in a *count* has to know
    whether the exclusion set moved under it.

    The shovel counter no longer needs this — it counts per-file transitions
    (`audit.count_pile_resolved`), which exclusions cannot fake. Kept because
    it is still recorded on every audit and phase 2's outcome-verified awards
    are specced to need it (`prompts/NEXT_STEPS.md`).
    """
    blob = json.dumps([
        sorted(cfg.get('EXCLUSION_PATTERNS') or []),
        sorted(cfg.get('DISC_RIP_EXCLUSION_PRESETS') or []),
        sorted(cfg.get('MEDIA_SERVER_EXCLUSION_PRESETS') or []),
    ], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def update_progress(progress, cfg, det, state=None, now=None, resolved=None,
                    dead_regs=None):
    """Advance the reward counters by one audit. Pure — returns a new dict.

    Called from `run_audit_process` after stats are computed. Everything here
    is monotonic: nothing a user does can take away a shovelful they already
    dug, a zombie they already killed, or a peak they already hit.

    `state` is the freshly built payload (`build_state` with the *previous*
    progress). It is used only to latch ladder peaks and earned feats, which is
    what stops current-state prizes from un-earning on a regression.

    `resolved` is this interval's shovel credit — items that left the Triage
    pile (not-imported files, dead seeds, dead registrations) by being deleted,
    imported, or retired, counted by `audit.count_pile_resolved`. It is passed
    in rather than inferred from a drop in `not_imported_count` because a count
    knows only that the pile got smaller, not why: hiding items behind a new
    exclusion shrinks it just as well as clearing them. `None` means "no
    previous scan to compare against", which credits nothing.

    `dead_regs` is this scan's dead cross-seed registration hashes, kept so the
    next scan can diff against them. `None` leaves the stored set untouched —
    an audit that could not compute them must not read as "they all went away"
    and hand out credit for it on the following run.
    """
    p = {**EMPTY_PROGRESS, **(progress or {})}
    now = now or datetime.now()
    stamp = now.isoformat()
    # A library that is already clean on its very first audit was never killed —
    # there was nothing there to kill.
    first_run = p['last_ni_count'] is None

    p['shoveled'] += max(0, int(resolved or 0))
    # Still recorded: `last_ni_count` marks the first observed audit (above),
    # and both feed phase 2's outcome verification.
    p['last_ni_count'] = det.get('not_imported_count', 0) or 0
    p['last_excl_fp']  = exclusion_fingerprint(cfg)
    if dead_regs is not None:
        # Bounded so a pathological library cannot grow this app_meta row without
        # limit. Past the cap the shovel counter under-credits dead registrations
        # rather than bloating the record; realistic counts are single digits.
        p['last_dead_regs'] = sorted(dead_regs)[:_DEAD_REG_CAP]

    total_media = det.get('total_media_size', 0) or 0
    if total_media > 0:
        hl_pct = (det.get('hardlinked_media_size', 0) or 0) / total_media * 100
        p['hl_peak'] = max(float(p['hl_peak'] or 0.0), hl_pct)

    # Zombies: a kill each time the count is driven back to zero, a streak for
    # how long it stays there, and a break counter when it rises again.
    for key, count_key in (('orphan', 'orphaned_torrent_count'),
                           ('dupe',   'duplicate_count')):
        since_key = f'{key}_clean_since'
        break_key = f'{key}_break_at'
        is_clean  = (det.get(count_key, 0) or 0) == 0
        if is_clean:
            if not p[since_key]:
                # Was dirty (or unobserved) and is now zero. Only the former is
                # a kill — a first audit that happens to be clean killed nothing.
                if not first_run:
                    p[f'{key}_kills'] = int(p[f'{key}_kills'] or 0) + 1
                    # How long the mess survived. Only messes we watched arrive
                    # can be timed: a pile that was already there when auditorr
                    # was installed has no start, and guessing one would hand out
                    # a fast fix for work done before the first scan.
                    elapsed = _hours_since(p.get(break_key), now)
                    if elapsed is not None and elapsed <= _FAST_FIX_HOURS:
                        p['fast_fixes'] = int(p.get('fast_fixes') or 0) + 1
                p[since_key] = stamp
            p[break_key] = None
        else:
            if p[since_key]:
                # It was clean and now it isn't — that is the thing to tell them.
                p[f'{key}_breaks'] = int(p[f'{key}_breaks'] or 0) + 1
                p[break_key] = stamp
            p[since_key] = None

    # The all-three-zeros streak. Deliberately stricter than Sentinel and looser
    # than a health score of 100: it asks whether anything is *waiting on you*,
    # which the hardlink ratio is not.
    if _is_immaculate(det):
        if not p.get('immaculate_since'):
            p['immaculate_since'] = stamp
    else:
        p['immaculate_since'] = None

    # Latch everything that could otherwise regress.
    if state:
        peaks = dict(p.get('peaks') or {})
        for ladder in state.get('ladders') or []:
            prev = float(peaks.get(ladder['id']) or 0)
            peaks[ladder['id']] = max(prev, float(ladder.get('value') or 0))
        p['peaks'] = peaks
        p['feats_earned'] = sorted(
            set(p.get('feats_earned') or [])
            | {f['id'] for f in (state.get('feats') or []) if f.get('earned')}
        )
    return p


def record_trump(progress, torrents=1, now=None):
    """Credit one completed trump swap. Pure — returns a new dict.

    Called from the trump execute endpoint rather than from `run_audit_process`,
    which is where every other counter is advanced. Trumped is the exception on
    purpose: a swap deletes one release and grabs its replacement, so the next
    scan sees a library in much the same shape as the last one and there is no
    state change to infer the action from. The event is the only evidence.

    Safe to do outside the audit because this is a plain increment of a discrete
    act, not a reading of current state — there is nothing here that could
    regress and claw back points, which is the hazard `update_progress` exists
    to manage.
    """
    p = {**EMPTY_PROGRESS, **(progress or {})}
    n = max(1, int(torrents or 1))
    p['trumps'] = int(p.get('trumps') or 0) + 1
    p['trump_torrents'] = int(p.get('trump_torrents') or 0) + n
    # Latched separately from the running total: "four in one swap" is a fact
    # about a single cross-seed group, and a sum of small swaps must not add up
    # to it. Ratchets, like every other peak here.
    p['trump_max_group'] = max(int(p.get('trump_max_group') or 0), n)
    p['last_trump_at'] = (now or datetime.now()).isoformat()
    return p


def _days_since(stamp, now=None):
    if not stamp:
        return None
    try:
        delta = (now or datetime.now()) - datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
    return max(0, delta.days)


def _hours_since(stamp, now=None):
    if not stamp:
        return None
    try:
        delta = (now or datetime.now()) - datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
    return max(0.0, delta.total_seconds() / 3600.0)


def _is_immaculate(det):
    """Nothing is waiting on you: no orphans, no duplicates, nothing unimported.

    Deliberately *not* a health score of 100, which also demands a perfect
    hardlink ratio — that is Backfill's aspiration, not a statement about
    whether the library needs attention. The size guards keep an empty install
    from being congratulated for having nothing wrong with nothing.
    """
    det = det or {}
    return bool(
        (det.get('total_torrents_size', 0) or 0) > 0
        and (det.get('total_media_size', 0) or 0) > 0
        and (det.get('orphaned_torrent_count', 0) or 0) == 0
        and (det.get('duplicate_count', 0) or 0) == 0
        and (det.get('not_imported_count', 0) or 0) == 0
    )


def _recoverable(score, maximum):
    """Health-score points currently being lost in a category."""
    try:
        lost = float(maximum) - float(score)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, lost), 1)


def _pluralize(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _threshold_state(size, limit, count):
    """fix over the configured threshold, optimize under it, maintain at zero."""
    if not count:
        return 'maintain'
    if limit and limit > 0:
        return 'fix' if size > limit else 'optimize'
    return 'fix' if size > 0 else 'maintain'


def _workflow_rows(cfg, det, source_ok, arr_ok):
    rows = []

    # ── Cleanup ──────────────────────────────────────────────────────────────
    o_count = det.get('orphaned_torrent_count', 0) or 0
    o_size  = det.get('orphaned_torrent_size', 0) or 0
    rows.append({
        'id': 'cleanup', 'label': 'Cleanup', 'accent': 'var(--yellow)',
        'card': 'Orphaned Torrents',
        'state': 'blocked' if not source_ok else _threshold_state(o_size, det.get('or_limit'), o_count),
        'blocked_reason': None if source_ok else 'Connect your torrent client first.',
        'count': o_count, 'bytes': o_size,
        'score_lost': _recoverable(det.get('or_score'), det.get('or_max', 10)),
        'score_max': round(float(det.get('or_max', 10) or 0), 1),
        'teaching': (
            "Files in your torrent folder your client has no record of. Cleanup groups them "
            "by release folder and writes a delete script you run yourself."
        ),
        'clear_line': 'No orphans. Every file in your torrent folder is accounted for.',
    })

    # ── Dedupe ───────────────────────────────────────────────────────────────
    d_count = det.get('duplicate_count', 0) or 0
    d_size  = det.get('duplicate_size', 0) or 0
    rows.append({
        'id': 'dedupe', 'label': 'Dedupe', 'accent': 'var(--purple)',
        'card': 'Duplicate Files',
        'state': _threshold_state(d_size, det.get('dup_limit'), d_count),
        'blocked_reason': None,
        'count': d_count, 'bytes': d_size,
        'score_lost': _recoverable(det.get('dup_score'), det.get('dup_max', 10)),
        'score_max': round(float(det.get('dup_max', 10) or 0), 1),
        'teaching': (
            "Identical files that don't share an inode — the same bytes paid for twice. "
            "Dedupe writes a script that replaces the copies with hardlinks."
        ),
        'clear_line': 'No duplicates. You are not paying for the same bytes twice.',
    })

    # ── Triage ───────────────────────────────────────────────────────────────
    n_count = det.get('not_imported_count', 0) or 0
    n_size  = det.get('not_imported_size', 0) or 0
    dead    = det.get('dead_seed_count', 0) or 0
    # Per-torrent Triage row counts (audit.count_triage_items). The flat
    # *_count details are per file and know nothing about dead registrations,
    # so they only stand in for pre-upgrade results rows.
    tri      = det.get('triage_counts') or {}
    tri_live = tri.get('not_imported', n_count)
    tri_dead = tri.get('dead_seeds', dead)
    tri_reg  = tri.get('dead_registrations', 0) or 0
    tri_total = tri.get('total', n_count + dead)
    tri_state = 'blocked' if not source_ok else _threshold_state(n_size, det.get('ni_limit'), n_count)
    if tri_state == 'maintain' and (tri_dead or tri_reg):
        tri_state = 'optimize'
    rows.append({
        'id': 'triage', 'label': 'Triage', 'accent': 'var(--red)',
        'card': 'Not Imported',
        'state': tri_state,
        'blocked_reason': None if source_ok else 'Connect your torrent client first.',
        'count': tri_total, 'bytes': n_size,
        'not_imported_torrents': tri_live, 'dead_seeds': tri_dead,
        'dead_registrations': tri_reg,
        'score_lost': _recoverable(det.get('ni_score'), det.get('ni_max', 10)),
        'score_max': round(float(det.get('ni_max', 10) or 0), 1),
        'teaching': (
            "Seeding torrents with no file in your media folder. Triage says why each one "
            "never landed and gives you the right action per verdict."
        ),
        'clear_line': 'Everything you are seeding is imported and accounted for.',
    })

    # ── Backfill ─────────────────────────────────────────────────────────────
    total_media = det.get('total_media_size', 0) or 0
    linked      = det.get('hardlinked_media_size', 0) or 0
    unlinked    = max(0, total_media - linked)
    hl_ratio    = (linked / total_media) if total_media > 0 else 1.0
    if not arr_ok:
        bf_state = 'blocked'
    elif hl_ratio < 0.90:
        bf_state = 'fix'
    elif hl_ratio < 0.995:
        bf_state = 'optimize'
    else:
        bf_state = 'maintain'
    rows.append({
        'id': 'backfill', 'label': 'Backfill', 'accent': 'var(--blue)',
        'card': 'Hardlinked Media',
        'state': bf_state,
        'blocked_reason': None if arr_ok else 'Connect Sonarr or Radarr to search for releases.',
        'count': 0, 'bytes': unlinked, 'ratio_pct': round(hl_ratio * 100, 1),
        'score_lost': _recoverable(det.get('hl_score'), det.get('hl_max', 70)),
        'score_max': round(float(det.get('hl_max', 70) or 0), 1),
        'teaching': (
            "Media with no torrent behind it — disk that earns no ratio. Backfill searches "
            "your indexers for a release that hardlinks onto the file you already have."
        ),
        'clear_line': 'Effectively your whole library is hardlinked and seeding.',
    })

    # ── Trumped ──────────────────────────────────────────────────────────────
    # Never auto-prioritized: it is PM-driven, not audit-driven. There is no
    # signal in the audit that says "you have been trumped" — only the tracker
    # knows, so this row waits to be used rather than nagging.
    rows.append({
        'id': 'trumped', 'label': 'Trumped', 'accent': 'var(--green)',
        'card': None,
        'state': 'standby', 'blocked_reason': None,
        'count': 0, 'bytes': 0, 'score_lost': 0.0, 'score_max': 0.0,
        'teaching': (
            "Paste a trump PM. Trumped finds the torrents you are seeding for that release, "
            "confirms the cross-seed group, and swaps in the replacement."
        ),
        'clear_line': 'Nothing to do until a tracker sends you a trump notice.',
    })

    # Always the canonical sequence. The page renders fixed, labelled sections
    # (baseline → ongoing → on demand), so the layout carries the
    # ordering and nothing reshuffles under the user between visits. Magnitude
    # decides *state*, which decides whether a card is expanded or collapsed.
    for r in rows:
        r['stage']       = STAGE.get(r['id'], 'ongoing')
        r['stage_label'] = STAGE_LABEL[r['stage']]
        r['nature']      = NATURE.get(r['id'], '')
        r['reward_kind'] = REWARD_KIND.get(r['id'], 'ondemand')
    rows.sort(key=lambda r: (_bucket(r), WORKFLOW_ORDER.index(r['id'])))
    return rows


def _reward_line(row, progress, det):
    """One line describing how this workflow pays out, in its own shape."""
    p = {**EMPTY_PROGRESS, **(progress or {})}
    kind = row['reward_kind']

    if kind == 'zombie':
        key    = 'orphan' if row['id'] == 'cleanup' else 'dupe'
        days   = _days_since(p[f'{key}_clean_since'])
        kills  = int(p[f'{key}_kills'] or 0)
        breaks = int(p[f'{key}_breaks'] or 0)
        noun   = 'Orphans' if key == 'orphan' else 'Duplicates'
        kill_txt = f"{_pluralize(kills, 'kill')}" if kills else 'no kills yet'
        if days is not None:
            streak = 'Clean as of today' if days == 0 else f"Clean for {_pluralize(days, 'day')}"
            return {'kind': kind, 'headline': f"{streak} · {kill_txt}",
                    'detail': ('You killed it. auditorr keeps watch in case it gets back up.'
                               if kills else
                               'Clean so far. The counter starts the first time you have to fix it.')}
        if breaks:
            return {'kind': kind, 'headline': f"{noun} are back · {kill_txt}",
                    'detail': f"Risen {_pluralize(breaks, 'time')}. Drive it to zero again "
                              f"for another kill — something upstream keeps reviving these."}
        return {'kind': kind, 'headline': f"{noun} have never been cleared · {kill_txt}",
                'detail': 'Get the count to zero once and you bank your first kill.'}

    if kind == 'shovel':
        # The row count — every item Triage lists, including dead registrations.
        # `audit.count_pile_resolved` credits the same set, so the number here
        # and the number that pays out describe the same pile.
        pile = row['count']
        return {
            'kind': kind,
            'headline': f"{p['shoveled']:,} shovelled so far. {_pluralize(pile, 'item')} on the pile today.",
            'detail': 'The pile refills forever. Every shovelful counts, permanently.',
        }

    if kind == 'crystal':
        # Backfill's whole foot is this one line. It used to be three — a mono
        # stat readout, the best-ever ratchet, and a sentence explaining the
        # ratchet — while Triage and Trumped next to it were a single sans line
        # each, so the one card that never finishes was also the loudest thing
        # on the page. The ratio and the idle bytes are the only figures worth a
        # line here, and they read as a sentence, so they live in the sans
        # payout slot with the other workflows' lines rather than in `_stat`.
        # (The best-ever ratchet still drives the Lapidary/Alchemist ladders,
        # which the card's own `next_prize` box shows beside this line.)
        pct  = row.get('ratio_pct', 0.0)
        idle = float(row.get('bytes') or 0)
        line = (f"{pct}% hardlinked · {_fmt_bytes(idle)} earning nothing" if idle
                else f"{pct}% hardlinked · every byte is earning")
        return {'kind': kind, 'headline': line, 'detail': ''}

    if kind == 'tribute':
        swaps = int(p.get('trumps') or 0)
        rigs  = int(p.get('trump_torrents') or 0)
        if not swaps:
            return {
                'kind': kind, 'tally': 0,
                'headline': 'No tribute paid. The trackers have not asked.',
                'detail': 'Nothing accrues here until a PM arrives. Then it does, forever.',
            }
        since = _days_since(p.get('last_trump_at'))
        when  = ('today' if since == 0 else
                 f"{_pluralize(since, 'day')} ago" if since is not None else 'at some point')
        line = f"{swaps:,} paid · {_pluralize(rigs, 'registration')} retired"
        return {
            # `tally` drives the page: On demand is collapsed by default, so a
            # user with a Kingmaker streak would otherwise never see it.
            'kind': kind, 'tally': swaps,
            'headline': f"{line}. Last one {when}.",
            'detail': 'Every one of these was somebody else\'s idea. Counted anyway.',
        }

    return {'kind': kind, 'headline': 'No prize. No pile. No streak.',
            'detail': 'This one only matters when a tracker says so.'}


def _fmt_bytes(n):
    """Scales past TB — field reports include 100–500 TB libraries, and a
    petabyte rendered as "1024.0 TB" makes the top ladders look broken."""
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit in ('B', 'KB') else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} EB"


def _summary(row):
    """The one prose line under the card title — what this row is, right now.

    Every card gets exactly one, chosen by state: what is blocking it, what the
    workflow does, or what "clear" means for it. The *numbers* are not in here;
    they are the stat line at the foot of the card. Keeping the two apart is
    what stopped Backfill saying "85% hardlinked" twice on one card.
    """
    if row['state'] == 'blocked':
        return row['blocked_reason'] or 'Not configured yet.'
    if row['state'] in ('maintain', 'standby'):
        return row['clear_line']
    return row['teaching']


def _stat(row):
    """The mono readout at the foot of the card — the numbers, and only those.

    Empty for a row with nothing to count: a cleared or blocked workflow has no
    figure worth a monospace line, and inventing "0 orphans" for one would give
    the calmest cards a readout the busy ones use to signal work.

    Also empty for Backfill in *every* state — a ratio and the bytes behind it
    are a sentence, not a tally, so they ride the one payout line in
    `_reward_line` instead. Giving that row both slots is what made it three
    lines tall beside Triage's one.
    """
    if row['state'] in ('blocked', 'maintain', 'standby'):
        return ''
    size = _fmt_bytes(row['bytes'])
    if row['id'] == 'cleanup':
        return f"{_pluralize(row['count'], 'orphaned file')} · {size}"
    if row['id'] == 'dedupe':
        return f"{_pluralize(row['count'], 'duplicate')} · {size} recoverable"
    if row['id'] == 'triage':
        parts = []
        live = row.get('not_imported_torrents', 0)
        if live > 0:
            parts.append(_pluralize(live, 'not-imported torrent'))
        if row.get('dead_seeds'):
            parts.append(_pluralize(row['dead_seeds'], 'dead seed'))
        if row.get('dead_registrations'):
            parts.append(_pluralize(row['dead_registrations'], 'dead registration'))
        return ' · '.join(parts) or 'Items need a verdict'
    return ''


# ---------------------------------------------------------------------------
# Useless prizes. They do nothing. That is the point.
#
# Progress Quest rules: many tiers, mock-heroic names, the next one always in
# sight. Every threshold below is derivable from audit history, so no ladder
# can advance on a number auditorr did not actually measure.
# ---------------------------------------------------------------------------

# Long enough for the deepest ladder (Hoarder, 21 tiers) with room to spare —
# a tier rendered as "Packrat 16" instead of "Packrat XVI" breaks the joke.

# ---------------------------------------------------------------------------
# What each medallion is a medallion *of*.
#
# Feats explain themselves — every one carries a `desc` saying exactly what to
# do. Ladders had no equivalent: a tile read "Rack Owner / Hoarder / 12.4 TB",
# and eight of the thirty ladders are byte ladders, so the number alone is
# indistinguishable between the library, the torrent directory, what is seeding
# and what has been uploaded. The blurb answers it but only in a tooltip.
#
# Two cheap fixes, no extra tile height:
#   * `measures` — one or two words naming what the number counts, printed
#     faint straight after the value ("12.4 TB library", "3.90× ratio").
#   * `group` — the shelf is themed rather than a wall of thirty, mirroring the
#     feat groups exactly so the two sections read as one system.
LADDER_GROUPS = [
    ('have',    'What you have',
     'Size, count, and how much of it is in good order.'),
    ('give',    'What you give',
     'Bytes out, the ratio they earn, and how far they travel.'),
    ('work',    'What you did',
     'The workflows, and the states you drove them to.'),
    ('machine', 'What it did',
     'auditorr, running, whether or not you were watching.'),
    ('meta',    'Prizes for prizes',
     'The least useful things on the least useful page.'),
]

# ladder id -> (group, what the number counts)
LADDER_FACET = {
    'hoard':         ('have',    'library'),
    'packrat':       ('have',    'torrents'),
    'archivist':     ('have',    'files'),
    'librarian':     ('have',    'titles'),
    'videophile':    ('have',    '2160p'),
    'provenance':    ('have',    'oldest'),
    'vaultkeeper':   ('have',    'hardlinked'),
    # 'swept'/'landed', not 'clean' — Sentinel already owns 'clean' for days,
    # and these three now measure clean *bytes*, which is the exact ambiguity
    # this field exists to remove.
    'tidiness':      ('have',    'swept'),
    'purity':        ('have',    'landed'),
    'conservator':   ('have',    'spotless'),

    'seedbearer':    ('give',    'seeding'),
    'benefactor':    ('give',    'uploaded'),
    'usurer':        ('give',    'ratio'),
    # 'seeds', not 'seeding' — Seedbearer already owns that word for bytes, and
    # two tiles reading "… seeding" is exactly the ambiguity this field fixes.
    'seedling':      ('give',    'seeds'),
    'pollinator':    ('give',    'cross-seeded'),
    'alchemist':     ('give',    'multiplier'),
    'diplomat':      ('give',    'trackers'),
    'atlas':         ('give',    'held'),
    'oldfaithful':   ('give',    'oldest seed'),

    'shoveler':      ('work',    'shovelled'),
    'exterminator':  ('work',    'orphan kills'),
    'clonehunter':   ('work',    'dupe kills'),
    'kingmaker':     ('work',    'swaps'),
    'sentinel':      ('work',    'clean'),
    'firebrigade':   ('work',    'quick fixes'),
    'unblemished':   ('work',    'unbroken'),
    'lapidary':      ('work',    'hardlinked'),
    'custodian':     ('work',    'best health'),
    'steady':        ('work',    'at 90+'),
    'flawless':      ('work',    'at 100'),

    'auditor':       ('machine', 'audits'),
    'watcher':       ('machine', 'audited'),
    'chronicler':    ('machine', 'running'),
    'nightwatch':    ('machine', 'by watchdog'),
    'clockwork':     ('machine', 'scheduled'),
    'handson':       ('machine', 'by hand'),
    'marathoner':    ('machine', 'scanning'),
    'highwater':     ('machine', 'peak RAM'),

    'completionist': ('meta',    'rungs'),
    'trophyhunter':  ('meta',    'feats'),
}


# ---------------------------------------------------------------------------
# Tier titles. Progress Quest's joy was absurd *specificity* — "Fuzzy Wolf
# Spider", "Rat Tail x3" — not tidy grading. "Hoarder XVII" is a spreadsheet
# wearing a costume; "Structural Engineer" is a joke. Every rung gets a name.
#
# Lists shorter than their threshold list fall back to Roman numerals, so a
# mismatch degrades quietly instead of crashing.
# ---------------------------------------------------------------------------

TIER_TITLES = {
    'hoard': [
        'Curious', 'Hobbyist', 'Collector', 'Digital Squirrel', 'Pack Rat',
        'Shelf Bender', 'Basement Dweller', 'Drive Hoarder', 'Array Enjoyer',
        'NAS Goblin', 'Rack Owner', 'Server Room Guy', 'Small Datacenter',
        'Cooling Concerns', 'Structural Engineer', 'Power Bill Denier',
        'Regional Archive', 'National Archive', 'Library of Alexandria',
        'Seek Help', 'Why',
    ],
    'seedbearer': [
        'Sprout', 'Sapling', 'Contributor', 'Good Citizen', 'Reliable Source',
        'Backbone', 'Load Bearing', 'Infrastructure', 'Public Utility',
        'Tier 1 Peer', 'The Grid', 'Bandwidth Baron', 'Bit Fountain',
        'Eternal Spring', 'Upload Singularity', 'Ratio Messiah',
        'Patron Saint of Leechers', 'The Swarm Itself', 'Seedbox Ascendant',
    ],
    'packrat': [
        'A Few Files', 'Getting Started', 'Cluttered', 'Overflowing',
        'Spare Room Gone', 'Closet Situation', 'Load Bearing Boxes',
        'Fire Marshal Interested', 'Documented Case', 'Intervention Pending',
        'Beyond Reason', 'Geological Layer', 'Sedimentary', 'Metamorphic',
        'Tectonic', 'Visible From Orbit', 'Own Gravity', 'Event Horizon',
        'Singularity',
    ],
    'benefactor': [
        'First Crumb', 'Kind Stranger', 'Generous Sort', 'Good Samaritan',
        'Reliable Uploader', 'Community Pillar', 'Local Hero', 'Regional Hero',
        'Philanthropist', 'Benefactor', 'Great Benefactor', 'Endowment',
        'Trust Fund', 'Foundation', 'Institution', 'National Treasure',
        'Living Legend', 'Folk Hero', 'Mythologized', 'Deified', 'Pantheon',
        'Cosmic Background Upload', 'Ozymandias',
    ],
    'usurer': [
        'Break Even Adjacent', 'Half Decent', 'Even Steven', 'Turning a Profit',
        'Modest Returns', 'Compound Interest', 'Loan Shark', 'Usurer',
        'Robber Baron', 'Central Banker', 'Money Printer', 'Rentier',
        'Ratio Landlord', 'Feudal Lord', 'Economic Anomaly', 'Fictional',
    ],
    'auditor': [
        'First Look', 'Second Opinion', 'Curious', 'Checking Again',
        'Regular Checkup', 'Diligent', 'Thorough', 'Obsessive', 'Compulsive',
        'Certified Auditor', 'Chartered Accountant', 'Forensic',
        'Inspector General', 'Audit Daemon', 'The Numbers Speak',
        'Ledger Incarnate', 'Bureaucratic Sublime', 'Kafkaesque',
    ],
    'custodian': [
        'Barely Alive', 'Rough Shape', 'Stabilizing', 'Recovering', 'Passable',
        'Fair', 'Decent', 'Respectable', 'Good', 'Very Good', 'Great',
        'Excellent', 'Pristine', 'Immaculate', 'Flawless', 'Perfect',
    ],
    'lapidary': [
        'Rough Stone', 'Chipped', 'Shaped', 'Sanded', 'Polished', 'Glossy',
        'Faceted', 'Brilliant Cut', 'Gemstone', 'Flawless Clarity',
        'Museum Piece', 'Crown Jewel', 'Beyond Grading',
        'Theoretical Perfection', 'Asymptotic', 'Crystalline Ideal',
    ],
    'shoveler': [
        'First Shovelful', 'Second Scoop', 'Warmed Up', 'Blisters',
        'Committed', 'Digger', 'Excavator', 'Earthmover', 'Trench Warfare',
        'Strip Miner', 'Terraformer', 'The Pile Persists', 'Sisyphus',
        'Sisyphus Prime', 'Boulder Enthusiast', 'One With The Pile',
        'The Pile Is You', 'The Pile Was Always You',
    ],
    'exterminator': [
        'First Blood', 'Double Tap', 'Hat Trick', 'Pest Control',
        'Exterminator', 'Cleanser', 'Purifier', 'Scourge of Orphans',
        'Grim Reaper', 'They Fear You', 'Legend of the Torrent Dir',
        'Orphan Nemesis', 'Undefeated', 'Still They Come',
    ],
    'clonehunter': [
        'Spotted a Double', 'Seeing Double', 'Twin Slayer', 'Clone Hunter',
        'Duplicate Bane', 'Copy Killer', 'Redundancy Eliminator',
        'Blade Runner', 'There Can Be Only One', 'Original Only',
        'Uniqueness Enforcer', 'Xerox Nemesis', 'Singular', 'Still Copying',
    ],
    # Trumped is the one workflow where you are the loser of the exchange: a
    # better copy turned up and you stood aside. The names lean into it.
    'kingmaker': [
        'Stood Aside', 'Twice Deposed', 'Gracious in Defeat',
        'Succession Planner', 'Regime Change', 'Kingmaker',
        'Serial Abdicator', 'Palace Regular', 'Court Official',
        'Master of Ceremonies', 'Keeper of the Crown', 'Dynasty Manager',
        'The Throne Is a Chair', 'Long Live Whoever',
    ],
    'sentinel': [
        'One Quiet Day', 'Two Days Clean', 'Three Day Weekend', 'A Full Week',
        'Fortnight', 'A Clean Month', 'Two Months', 'A Quarter',
        'Half a Year', 'A Full Year', 'Two Years', 'Five Years',
        'A Decade of Vigilance',
    ],
    'alchemist': [
        'Barely Multiplied', 'A Little Extra', 'Getting Clever', 'Efficient',
        'Very Efficient', 'Doubled Up', 'Two and a Half', 'Tripled',
        'Quadrupled', 'Quintupled', 'Transmuter', 'Philosophers Stone',
        'Lead Into Gold', 'Violates Thermodynamics',
    ],
    'diplomat': [
        'One Friend', 'Two Trackers', 'A Small Circle', 'Networked',
        'Well Connected', 'Diplomat', 'Ambassador', 'Envoy Extraordinary',
        'Consul', 'Secretary General', 'Treaty Signatory', 'Trade Federation',
        'Galactic Senate', 'United Nations', 'Interdimensional Delegate',
    ],
    'steady': [
        'One Good Day', 'Two In a Row', 'Three Peat', 'A Solid Week',
        'Fortnight of Calm', 'A Month at Ninety', 'Two Months', 'A Quarter',
        'Half a Year', 'A Full Year', 'Two Years', 'Five Years', 'Unshakeable',
    ],
    'chronicler': [
        'Day One', 'Day Two', 'Day Three', 'A Week In', 'Fortnight',
        'A Month', 'Two Months', 'A Quarter', 'Half a Year', 'Anniversary',
        'Two Years', 'Five Years', 'A Decade', 'Twenty Years',
    ],
    'archivist': [
        'A Handful', 'A Couple Dozen', 'Some Files', 'A Hundred',
        'A Few Hundred', 'Getting Organized', 'A Thousand', 'Filing Cabinet',
        'Card Catalogue', 'Small Library', 'Municipal Library',
        'University Library', 'National Library', 'Deep Archive',
        'Continental Index', 'A Million Files', 'Beyond Counting',
        'Inode Exhaustion Risk', 'The Filesystem Weeps', 'fsck Would Take Days',
    ],
    'seedling': [
        'One Torrent', 'Two Torrents', 'A Few', 'A Dozen', 'Two Dozen',
        'Fifty', 'A Hundred', 'Small Farm', 'Plantation',
        'Agricultural Concern', 'Industrial Farming', 'Monoculture',
        'Breadbasket', 'Feeding the Swarm', 'Green Revolution',
        'Seed Bank of Record',
    ],
    'vaultkeeper': [
        'A Locked Drawer', 'Small Safe', 'Gun Safe', 'Wall Vault',
        'Strong Room', 'Bank Vault', 'Reserve Branch', 'Federal Reserve',
        'Fort Knox', 'Swiss Mountain', 'Doomsday Vault', 'Svalbard',
        'Deep Storage', 'Continental Reserve', 'Planetary Backup',
        'Off-World Copy', 'Redundant Universe', 'Heat Death Insurance',
        'Vault Keeper Eternal',
    ],
    'pollinator': [
        'First Cross', 'Busy Bee', 'Bee Keeper', 'Pollinator',
        'Orchard Keeper', 'Cross-Seed Enjoyer', 'Efficiency Merchant',
        'Multiplier Mage', 'Double Dipper', 'Triple Dipper',
        'Quadruple Dipper', 'Hydra', 'One File Many Homes', 'Ubiquitous',
        'Everywhere at Once', 'Simultaneous', 'Quantum Superposition',
        'Omnipresent',
    ],
    'marathoner': [
        'A Minute of Effort', 'Two Minutes', 'Five Minutes', 'Ten Minutes',
        'A Quarter Hour', 'An Hour', 'A Long Lunch', 'Half a Shift',
        'A Full Shift', 'A Full Day', 'A Long Weekend', 'A Working Week',
        'A Fortnight of CPU', 'A Month of Staring', 'A Season of Scanning',
    ],
    'watcher': [
        'First Day', 'Second Day', 'Third Day', 'A Week of Watching',
        'Fortnight', 'A Month', 'Two Months', 'A Quarter', 'Half a Year',
        'A Year of Vigilance', 'Two Years', 'Five Years',
    ],
    'nightwatch': [
        'It Woke Up Once', 'Twice in the Night', 'Light Sleeper', 'Attentive',
        'Ever Vigilant', 'Never Sleeps', 'The Night Watch', 'Insomniac',
        'Sleepless Sentinel', 'Perpetual Motion', 'It Does Not Rest',
        'It Does Not Blink', 'It Has Seen Things', 'It Remembers',
    ],
    'clockwork': [
        'On Schedule', 'Twice on Time', 'Punctual', 'Reliable', 'Metronome',
        'Clockwork', 'Swiss Movement', 'Atomic Clock', 'Cesium Standard',
        'More Punctual Than You', 'Timekeeper', 'Chronometer Absolute',
        'Time Itself', 'Outlasts Timezones',
    ],
    'handson': [
        'Clicked It Once', 'Impatient', 'Hands On', 'Control Freak',
        'Trust Issues', 'Manual Override', 'Micromanager',
        'Just Checking Again', 'It Has Not Changed', 'Compulsive Refresher',
        'The Button Is Worn', 'Seek Hobbies',
    ],
    'highwater': [
        'Featherweight', 'Lightweight', 'Comfortable', 'Roomy', 'A Full Gig',
        'Chunky', 'Two Gigs', 'Heavyweight', 'Four Gigs', 'Concerning',
        'Swap Enjoyer', 'OOM Adjacent',
    ],
    'tidiness': [
        'Swept Once', 'Tidy Corner', 'Clean Shelf', 'Clean Room',
        'Spotless Wing', 'Immaculate Floor', 'Clean Storey', 'Sterile',
        'Operating Theatre', 'Cleanroom Class 100', 'Vacuum Sealed',
        'No Dust Exists Here', 'Aggressively Clean', 'Surgical',
        'Beyond Sterile', 'Void of Filth', 'Nothing Grows Here',
        'Antiseptic Sublime', 'Clinically Empty', 'Cleaner Than Vacuum',
    ],
    'purity': [
        'Mostly Landed', 'Well Sorted', 'Properly Filed', 'Cleanly Imported',
        'Orderly', 'Meticulous', 'Fastidious', 'Beyond Reproach',
        'Unimpeachable', 'Certified Pure', 'Distilled', 'Twice Distilled',
        'Refined', 'Twice Refined', 'Thrice Refined', 'Elemental',
        'Isotopically Pure', 'Refined To Nothing', 'Theoretically Clean',
        'Purity Itself',
    ],
    # Clean *at scale*. The tier names are the whole argument for the rebase:
    # "No Dust Exists Here" was previously awarded for owning 10 TB.
    'conservator': [
        'Nothing Out of Place', 'Tidy Shelf', 'Dusted', 'White Glove',
        'Acid-Free', 'Archival Sleeve', 'Climate Controlled',
        'Humidity Regulated', 'Museum Grade', 'Behind Glass', 'Velvet Rope',
        'Nitrogen Atmosphere', 'Sealed Wing', 'No Visitors', 'Vacuum Vault',
        'Nobody Is Allowed In', 'Preserved for the Nation',
        'Sealed for Posterity', 'Legally a Monument', 'Outlives You',
    ],
    'librarian': [
        'A Shelf', 'A Bookcase', 'A Reading Room', 'Reference Section',
        # 'Civic Collection', not 'Municipal Library' — Archivist already has
        # that rung, and Archivist counts files while this counts titles, which
        # is precisely the pair that must not share a word.
        'Local Branch', 'Civic Collection', 'Regional Collection',
        'Legal Deposit', 'National Collection', 'Copyright Library',
        'Everything Ever Made', 'The Catalogue Is Its Own Project',
        'You Have Watched None of These', 'Accession Number Overflow',
    ],
    'videophile': [
        'First Pixels', 'Some of It', 'A Proper Screen', 'Worth the Bandwidth',
        'Discerning', 'Videophile', 'Format Snob', 'Only the Best',
        'Nothing Under 2160', 'Pixel Baron', 'Bitrate Enthusiast',
        'There Is No Higher Format', 'Waiting on 8K',
        'The Panel Cannot Keep Up', 'Beyond Human Vision',
        'More Pixels Than Sense', 'Resolution Maximalist', 'Awaiting Better Eyes',
    ],
    'provenance': [
        'Last Month', 'Last Season', 'Half a Year Back', 'Last Year',
        'Two Years On', 'Predates the Rebuild', 'Five Years In',
        'Older Than the Array', 'A Decade Held', 'Older Than the Format',
        'You Do Not Remember Downloading This',
    ],
    'atlas': [
        'A Small Favour', 'Holding Steady', 'The Long Shift', 'Bearing Up',
        'Never Set It Down', 'Atlas', 'Weight of the World',
        'Arms Have Gone Numb', 'Forgot It Was There', 'Tectonic Patience',
        'Continental Drift', 'Older Than Most Trackers', 'Measured in Geology',
        'Outlasts the Hardware', 'Still Holding', 'It Holds Itself Now',
        'Perpetual Load', 'The Sky Rests Here',
    ],
    'oldfaithful': [
        # 'Six Months Up', not 'Half a Year': Chronicler and Watcher already
        # share that rung name, and "Closest to unlocking" happily shows one
        # ladder's current rung beside another's next one — three tiles reading
        # "Half a Year" at once, for three unrelated numbers.
        'Still Up', 'A Week Old', 'A Month Up', 'Seasoned', 'Six Months Up',
        'Anniversary', 'Two Years Deep', 'Vintage', 'Five Years Untouched',
        'Older Than the Drive It Sits On', 'A Decade of Uptime',
        'Outlasted the Tracker',
    ],
    'unblemished': [
        'A Quiet Day', 'Two Quiet Days', 'Uneventful', 'A Week of Nothing',
        'Suspiciously Calm', 'A Month of Silence', 'Nothing Has Happened',
        'Nothing Continues To Happen', 'Half a Year, No Notes',
        'A Year Without Incident', 'Two Years of Nothing',
        'Nothing Ever Happens Here', 'A Decade of Silence',
    ],
    'flawless': [
        'Briefly Perfect', 'Twice Perfect', 'Three Days Perfect',
        'A Perfect Week', 'Statistically Improbable', 'A Perfect Month',
        'Showing Off', 'This Is Just Who You Are', 'Half a Year Flawless',
        'A Perfect Year', 'Nobody Asked For This', 'Perfection Sustained',
        'A Decade Without a Flaw',
    ],
    'firebrigade': [
        'On It', 'Quick Sweep', 'Same Day Service', 'Rapid Response',
        'Within the Hour', 'Fire Brigade', 'Standing Army', 'Always On Call',
        'Before You Noticed', 'It Was Handled', 'Preemptive',
        'Nothing Gets Old Here', 'Reflexive', 'It Never Had a Chance',
    ],
    'completionist': [
        'First Prize', 'Getting Started', 'Ten Trinkets', 'Collector',
        'Cabinet Filling', 'Serious Collection', 'Hoarder of Nothing',
        'Curator of Trivia', 'Museum of Pointlessness', 'Completionist',
        'Diminishing Returns', 'Why Are You Like This', 'It Never Ends',
    ],
    'trophyhunter': [
        'First Feat', 'Two Feats', 'Hat Trick', 'Trophy Shelf', 'Cabinet',
        'Trophy Room', 'Trophy Wing', 'Hall of Fame', 'Legendary',
        'Nothing Left To Prove', 'Still Here Somehow', 'Curator of Nonsense',
        'Beyond Trophies', 'The Shelf Groans',
    ],
}

_ROMAN = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X',
          'XI', 'XII', 'XIII', 'XIV', 'XV', 'XVI', 'XVII', 'XVIII', 'XIX', 'XX',
          'XXI', 'XXII', 'XXIII', 'XXIV', 'XXV', 'XXVI', 'XXVII', 'XXVIII',
          'XXIX', 'XXX']


def _roman(n):
    return _ROMAN[n - 1] if 1 <= n <= len(_ROMAN) else str(n)


def _fmt_plain(n):
    n = float(n)
    return str(int(n)) if n == int(n) else f"{n:g}"


def _fmt_pct(n):
    """One decimal at most. `%g` gives six *significant* figures, so a hardlink
    ratio rendered fine at 98.1% and came out as "85.0408%" the moment the
    number had more digits in front of the point."""
    v = round(float(n or 0), 1)
    return f"{int(v)}%" if v == int(v) else f"{v}%"


def _fmt_days(n):
    return _pluralize(int(n), 'day')


def _fmt_hours(seconds):
    h = float(seconds or 0) / 3600.0
    return f"{h:.1f} h" if h >= 1 else f"{float(seconds or 0) / 60:.0f} min"


def _fmt_mb(n):
    n = float(n or 0)
    return f"{n / 1024:.1f} GB" if n >= 1024 else f"{n:.0f} MB"


def _fmt_x(n):
    return f"{float(n):.2f}×"


# Seconds in a year, for the byte-seconds ladders. Julian year — nothing here is
# precise enough for the distinction to matter, but the constant should be one
# thing rather than two.
_YEAR_SECS = 365.25 * 86400


def _fmt_tb_years(byte_secs):
    """Byte-seconds rendered as TB·years — 'this much, held for this long'."""
    v = float(byte_secs or 0) / (TB * _YEAR_SECS)
    if v < 0.01:
        return f"{v:.3f} TB·yr"
    if v < 1:
        return f"{v:.2f} TB·yr"
    return f"{v:,.1f} TB·yr"


def _fmt_span(days):
    """Days, switching to years once 'n days' stops being readable."""
    d = float(days or 0)
    if d < 365:
        return _pluralize(int(d), 'day')
    return f"{d / 365.25:.1f} years"


def _ladder(lid, name, blurb, value, thresholds, fmt, points_step=25, peaks=None):
    """Build one tiered ladder. Points escalate so later tiers feel worth it.

    Every ladder ratchets on the best value ever recorded (`peaks`), so a
    library that shrinks or a tracker you stop using can never demote a tier
    you already earned.
    """
    value = max(float(value or 0), float((peaks or {}).get(lid) or 0))
    titles = TIER_TITLES.get(lid) or []
    tiers = []
    for i, at in enumerate(thresholds):
        tiers.append({
            'n': i + 1,
            # Named band if we wrote one, Roman numeral if we ran out.
            'label': titles[i] if i < len(titles) else f"{name} {_roman(i + 1)}",
            'at': at,
            'at_label': fmt(at),
            'earned': value >= at,
            'points': points_step * (i + 1),
        })
    earned = [t for t in tiers if t['earned']]
    nxt    = next((t for t in tiers if not t['earned']), None)
    prev_at = earned[-1]['at'] if earned else 0
    if nxt:
        span = max(1e-9, nxt['at'] - prev_at)
        pct  = max(0.0, min(1.0, (value - prev_at) / span)) * 100
    else:
        pct = 100.0
    group, measures = LADDER_FACET.get(lid, ('have', ''))
    return {
        'id': lid, 'name': name, 'blurb': blurb,
        'group': group, 'measures': measures,
        'value': value, 'value_label': fmt(value),
        'tier': len(earned), 'tiers_total': len(tiers),
        'tier_label': earned[-1]['label'] if earned else None,
        'next_label': nxt['label'] if nxt else None,
        # Which rung the next tier actually is. A name on its own ("Pack Rat")
        # says nothing about where you are on the ladder — "5 of 21" does.
        'next_n': nxt['n'] if nxt else None,
        'next_at': nxt['at'] if nxt else None,
        'next_at_label': nxt['at_label'] if nxt else None,
        'pct': round(pct, 1),
        'maxed': nxt is None,
        'points': sum(t['points'] for t in earned),
        'tiers': tiers,
    }


def _days_at_or_above(runs, floor):
    """Consecutive days, ending today, where every successful run scored >= floor."""
    by_day = {}
    for r in runs:
        if r.get('status') != 'ok' or r.get('health_score') is None:
            continue
        day = str(r['ran_at'])[:10]
        by_day[day] = min(by_day.get(day, 999.0), float(r['health_score']))
    if not by_day:
        return 0
    cursor = datetime.now().date()
    # Allow today to be missing (no scan yet today) without breaking the streak.
    if cursor.isoformat() not in by_day:
        cursor -= timedelta(days=1)
    streak = 0
    while by_day.get(cursor.isoformat(), -1.0) >= floor:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _days_observed(runs):
    days = {str(r['ran_at'])[:10] for r in runs if r.get('status') == 'ok'}
    if not days:
        return 0
    first = min(days)
    try:
        return (datetime.now().date() - datetime.fromisoformat(first).date()).days + 1
    except ValueError:
        return len(days)


def _ladders(det, runs, cross, tracker_stats, best_score, lifetime_up, progress=None):
    total_media = det.get('total_media_size', 0) or 0
    total_tor   = det.get('total_torrents_size', 0) or 0
    linked      = det.get('hardlinked_media_size', 0) or 0
    hl_pct      = (linked / total_media * 100) if total_media > 0 else 0
    multiplier  = (cross or {}).get('multiplier') or 0
    seeding     = sum((s or {}).get('seeding_size', 0) for s in (tracker_stats or {}).values())
    seed_count  = sum((s or {}).get('seeding_count', 0) for s in (tracker_stats or {}).values())
    n_trackers  = len([t for t in (tracker_stats or {}) if t and t != 'None'])
    ok_runs     = [r for r in runs if r.get('status') == 'ok']
    peaks       = (progress or {}).get('peaks') or {}
    # Bytes earning on more than one tracker at once — the payoff of cross-seeding.
    multi_seed  = sum(s.get('size', 0) for s in ((cross or {}).get('segments') or [])
                      if (s.get('count') or 0) >= 2)
    scan_secs   = sum(float(r.get('duration_seconds') or 0) for r in ok_runs)
    audit_days  = len({str(r['ran_at'])[:10] for r in ok_runs})
    peak_rss    = max([int(r.get('peak_rss_mb') or 0) for r in ok_runs] or [0])
    by_trigger  = lambda k: sum(1 for r in ok_runs if r.get('trigger') == k)

    def L(*args, **kw):
        return _ladder(*args, peaks=peaks, **kw)

    return [
        # Threshold curve: **dense at the bottom, exponential at the top.**
        # A new user with a 30 GB library must tier up within minutes or the
        # prize layer is dead weight to them, so the early rungs are close
        # together. The top runs to petabytes because exponential chasing
        # leaves everyone still searching for the next one, and a ladder a user
        # can max is a ladder that stopped working. 100-500 TB libraries turn
        # up in real field reports; none of these should cap there.
        L('hoard', 'Hoarder',
                'Everything auditorr can see. You could stop any time. You will not.',
                total_media,
                [1 * GB, 5 * GB, 10 * GB, 25 * GB, 50 * GB, 100 * GB, 250 * GB, 500 * GB,
                 1 * TB, 2 * TB, 5 * TB, 10 * TB, 20 * TB, 50 * TB, 100 * TB, 250 * TB,
                 500 * TB, 1 * PB, 2 * PB, 5 * PB, 10 * PB],
                _fmt_bytes, 30),
        L('seedbearer', 'Seedbearer',
                'What you are currently giving back. Strangers are downloading this as you read.',
                seeding,
                [1 * GB, 5 * GB, 10 * GB, 25 * GB, 50 * GB, 100 * GB, 250 * GB, 500 * GB,
                 1 * TB, 2 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB, 100 * TB, 250 * TB,
                 500 * TB, 1 * PB, 2 * PB],
                _fmt_bytes, 30),
        L('packrat', 'Packrat',
                'The other half of the hardlink. It only ever grows, and you know it.',
                total_tor,
                [1 * GB, 5 * GB, 10 * GB, 25 * GB, 50 * GB, 100 * GB, 250 * GB, 500 * GB,
                 1 * TB, 2 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB, 100 * TB, 250 * TB,
                 500 * TB, 1 * PB, 2 * PB],
                _fmt_bytes, 30),
        L('benefactor', 'Benefactor',
                'Lifetime bytes uploaded. Somewhere, someone got their show. They will never thank you.',
                lifetime_up,
                [1 * GB, 5 * GB, 10 * GB, 25 * GB, 50 * GB, 100 * GB, 250 * GB, 500 * GB,
                 1 * TB, 2 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB, 100 * TB, 250 * TB,
                 500 * TB, 1 * PB, 2 * PB, 5 * PB, 10 * PB, 25 * PB, 50 * PB],
                _fmt_bytes, 40),
        L('usurer', 'Usurer',
                'Uploaded divided by seeded. Rent, collected on bytes, forever, from strangers.',
                (lifetime_up / seeding) if seeding > 0 else 0,
                [0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 7.5, 10, 20, 50, 100, 250, 500],
                _fmt_x, 40),
        L('auditor', 'Auditor',
                'Audits completed. Each one was a decision you made freely.',
                len(ok_runs),
                [1, 2, 3, 5, 10, 15, 25, 50, 75, 100, 250, 500, 1000, 2500, 5000,
                 10000, 25000, 100000],
                _fmt_plain, 20),
        L('custodian', 'Custodian',
                'Your best health score ever. It can only go up from here. Statistically. Probably.',
                best_score,
                [10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 85, 90, 95, 98, 99, 100],
                _fmt_pct, 25),
        # Crystal: the hardlink high-water mark, tracked explicitly in progress
        # as well as via the generic peak latch.
        L('lapidary', 'Lapidary',
                'Your best hardlinked share ever. Cut slowly, like a gem, by a man with a spreadsheet.',
                max(hl_pct, float((progress or {}).get('hl_peak') or 0.0)),
                [10, 25, 40, 50, 60, 70, 80, 85, 90, 95, 97, 98, 99, 99.5, 99.9, 100],
                _fmt_pct, 35),
        # Shovel: cumulative and permanent. The pile refills; the count doesn't reset.
        # Starts at 1 so the very first shovelful tiers up.
        L('shoveler', 'Shoveler',
                'Items shovelled off the pile, ever. The pile does not care. The pile returns.',
                (progress or {}).get('shoveled') or 0,
                [1, 2, 3, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000,
                 25000, 50000, 100000, 250000],
                _fmt_plain, 35),
        # Zombies, tracked separately: orphans and duplicates come back for
        # different reasons, and each row deserves its own carrot rather than
        # both pointing at one shared counter.
        L('exterminator', 'Exterminator',
                'Times you drove the orphan count to zero. It got back up. It always gets back up.',
                int((progress or {}).get('orphan_kills') or 0),
                [1, 2, 3, 5, 8, 12, 20, 35, 50, 75, 100, 200, 500, 1000],
                _fmt_plain, 40),
        L('clonehunter', 'Clone Hunter',
                'Times you drove duplicates to zero. Somewhere, quietly, another copy is being made.',
                int((progress or {}).get('dupe_kills') or 0),
                [1, 2, 3, 5, 8, 12, 20, 35, 50, 75, 100, 200, 500, 1000],
                _fmt_plain, 40),
        # Tribute: counted at execute time, not inferred from the next scan.
        # A swap deletes one release and grabs its replacement, so the library
        # lands back roughly where it started and there is nothing for an audit
        # to notice. Curve is gentle — a trump PM is a rare event on most
        # trackers, and a ladder whose first rung takes a year is not a ladder.
        L('kingmaker', 'Kingmaker',
                'Trump swaps completed. Somebody had a better copy and you stepped aside, again.',
                int((progress or {}).get('trumps') or 0),
                [1, 2, 3, 5, 8, 12, 20, 30, 50, 75, 100, 200, 500, 1000],
                _fmt_plain, 40),
        # Zombie: the streak of a clean state holding.
        L('sentinel', 'Sentinel',
                'Consecutive days with zero orphans. You killed them. Now you stand watch, alone.',
                _days_since((progress or {}).get('orphan_clean_since')) or 0,
                [1, 2, 3, 7, 14, 30, 60, 90, 180, 365, 730, 1825, 3650],
                _fmt_days, 35),
        L('alchemist', 'Ratio Alchemist',
                'How many times over each byte earns its keep. Free real estate.',
                multiplier,
                [1.01, 1.05, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0, 20.0],
                _fmt_x, 45),
        L('diplomat', 'Diplomat',
                'Distinct trackers you answer to. Each one has rules. You have read none of them.',
                n_trackers,
                [1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 50, 75, 100],
                _fmt_plain, 30),
        L('steady', 'Steady Hand',
                'Consecutive days above 90. A streak is just a number waiting to be broken.',
                _days_at_or_above(runs, 90),
                [1, 2, 3, 7, 14, 30, 60, 90, 180, 365, 730, 1825, 3650],
                _fmt_days, 40),
        # Sentinel watches orphans alone. This one asks whether *anything* is
        # waiting on you, which is the streak a library in genuinely good shape
        # can actually run.
        L('unblemished', 'Unblemished',
                'Consecutive days with nothing wrong: no orphans, no duplicates, nothing unimported.',
                _days_since((progress or {}).get('immaculate_since')) or 0,
                [1, 2, 3, 7, 14, 30, 60, 90, 180, 365, 730, 1825, 3650],
                _fmt_days, 40),
        # Not "Flawless": Custodian's rung 15 already carries that word, and a
        # medallion sharing a name with another medallion's rung is the exact
        # ambiguity the `measures` field exists to kill.
        L('flawless', 'Full Marks',
                'Consecutive days at a health score of exactly 100. Steady Hand, for people who cannot stop.',
                _days_at_or_above(runs, 100),
                [1, 2, 3, 7, 14, 30, 60, 90, 180, 365, 730, 1825, 3650],
                _fmt_days, 45),
        # The fix for a perverse incentive: Exterminator and Clone Hunter only
        # pay when a mess returns, so a library that stays clean can never earn
        # another kill. This pays for how fast the mess dies instead, which is
        # the thing a well-run library is actually good at.
        L('firebrigade', 'Fire Brigade',
                f'Messes cleared within {_FAST_FIX_HOURS} hours of appearing. It never stood a chance.',
                int((progress or {}).get('fast_fixes') or 0),
                [1, 2, 3, 5, 8, 12, 20, 35, 50, 75, 100, 200, 500, 1000],
                _fmt_plain, 40),
        L('chronicler', 'Chronicler',
                'Days since your first audit. Time passes whether you scan or not. It passed anyway.',
                _days_observed(runs),
                [1, 2, 3, 7, 14, 30, 60, 90, 180, 365, 730, 1825, 3650, 7300],
                _fmt_days, 25),
        L('archivist', 'Archivist',
                'Files under audit. Each one personally inspected. By a computer. Not by you.',
                (det.get('media_file_count') or 0) + (det.get('torrent_file_count') or 0),
                [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000,
                 100000, 250000, 500000, 1000000, 2500000, 5000000, 10000000,
                 25000000],
                _fmt_plain, 25),
        # The `have` shelf used to ask one question six times: Hoarder, Packrat,
        # Vault Keeper, Tidiness and Purity are all bytes, and Archivist is the
        # same bytes counted as files. Every one of them moves when you buy a
        # drive and not one of them moves when you improve the library — which
        # is why the shelf goes dead for exactly the user who has finished
        # buying drives. These three ask different questions about the same pile.
        L('librarian', 'Librarian',
                'Distinct things you hold, rather than bytes. 60 TB of remuxes is not a big collection.',
                det.get('title_count') or 0,
                [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000,
                 50000, 100000, 250000],
                _fmt_plain, 25),
        # Not owned by any workflow: `uhd_bytes` is read off the media library,
        # and no auditorr workflow writes there. Backfill grabs torrents for
        # media you already hold and Trumped swaps one release for the same
        # release, so neither moves this — the arrs do, upgrading in place.
        L('videophile', 'Videophile',
                'Bytes held at 2160p. Not how much you have — how good what you have is.',
                det.get('uhd_bytes') or 0,
                [1 * GB, 5 * GB, 10 * GB, 25 * GB, 50 * GB, 100 * GB, 250 * GB,
                 500 * GB, 1 * TB, 2 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB,
                 100 * TB, 250 * TB, 500 * TB, 1 * PB],
                _fmt_bytes, 30),
        # Patience, not money — and mtime is the only witness. It lies after an
        # array rebuild or an rsync without -t, which the blurb says out loud
        # rather than implying a precision that is not there. The peaks latch
        # means a reset can only fail to advance the ladder, never undo it.
        L('provenance', 'Provenance',
                'Age of the oldest thing you still have. Assuming nothing ever touched its timestamp.',
                det.get('oldest_media_age_days') or 0,
                [30, 90, 180, 365, 730, 1095, 1825, 2555, 3650, 5475, 7300],
                _fmt_span, 30),
        L('seedling', 'Seedling',
                'Torrents currently seeding. Each one a small, anonymous act of charity.',
                seed_count,
                [1, 2, 3, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000,
                 25000, 50000],
                _fmt_plain, 25),
        L('vaultkeeper', 'Vault Keeper',
                'Bytes actually hardlinked and earning. Not what you own. What is working for you.',
                linked,
                [1 * GB, 5 * GB, 10 * GB, 25 * GB, 50 * GB, 100 * GB, 250 * GB, 500 * GB,
                 1 * TB, 2 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB, 100 * TB, 250 * TB,
                 500 * TB, 1 * PB, 2 * PB],
                _fmt_bytes, 35),
        L('pollinator', 'Cross-Pollinator',
                'Bytes earning on two or more trackers at once. One file, many masters.',
                multi_seed,
                [1 * GB, 5 * GB, 10 * GB, 25 * GB, 50 * GB, 100 * GB, 250 * GB,
                 500 * GB, 1 * TB, 2 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB,
                 100 * TB, 250 * TB, 500 * TB, 1 * PB],
                _fmt_bytes, 40),
        # Time under load. The only ladder on the shelf that cannot be bought:
        # every other byte ladder jumps the moment a drive is filled, and this
        # one can only be earned by not touching things. 20 TB held for five
        # years beats 500 TB held for three weeks, and there is nothing the
        # bigger library can do about it except wait.
        L('atlas', 'Atlas',
                'Everything you have ever held up, multiplied by how long you held it.',
                det.get('seed_byte_secs') or 0,
                [x * TB * _YEAR_SECS for x in
                 (0.001, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 25, 50, 100, 250,
                  500, 1000, 2500, 10000, 50000)],
                _fmt_tb_years, 35),
        # Deliberately a maximum, not a sum: a sum of seeding time is torrent-
        # hours, which grows by owning more torrents and is Seedling crossed
        # with Chronicler. The oldest single torrent has nothing to do with
        # library size, so this is the one rung a small tidy library can hold
        # that a hundred-terabyte seedbox cannot.
        L('oldfaithful', 'Old Faithful',
                'The longest you have held a single torrent up. Still there. Still nobody downloading it.',
                (det.get('max_seed_secs') or 0) / 86400.0,
                [1, 7, 30, 90, 180, 365, 730, 1095, 1825, 2555, 3650, 5475],
                _fmt_span, 30),
        L('marathoner', 'Marathoner',
                'Hours auditorr has spent staring at your files so that you did not have to.',
                scan_secs,
                [60, 120, 300, 600, 900, 3600, 3 * 3600, 6 * 3600, 12 * 3600,
                 24 * 3600, 3 * 86400, 7 * 86400, 14 * 86400, 30 * 86400,
                 90 * 86400],
                _fmt_hours, 25),
        L('watcher', 'Watcher',
                'Days on which an audit happened. Evidence, should anyone ever ask.',
                audit_days,
                [1, 2, 3, 7, 14, 30, 60, 90, 180, 365, 730, 1825],
                _fmt_days, 30),
        L('nightwatch', 'Nightwatch',
                'Audits the watchdog began while you were asleep, or eating, or living.',
                by_trigger('watchdog'),
                [1, 2, 3, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
                _fmt_plain, 25),
        L('clockwork', 'Clockwork',
                'Audits the scheduler ran alone. Punctual. Uncomplaining. Unloved.',
                by_trigger('scheduled'),
                [1, 2, 3, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
                _fmt_plain, 25),
        L('handson', 'Hands On',
                'Audits you started by hand, clicking the button yourself, like an animal.',
                by_trigger('manual'),
                [1, 2, 3, 5, 10, 25, 50, 100, 250, 500, 1000, 2500],
                _fmt_plain, 25),
        L('highwater', 'High Water',
                'Peak RAM a single scan ever demanded. Bigger library, bigger number, fewer options.',
                peak_rss,
                [128, 256, 512, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192, 16384],
                _fmt_mb, 25),
        # Clean **at scale**, and only while actually clean.
        #
        # These two began as percentages, which cap at 100% — two ladders that
        # could be maxed, in a design whose premise is that nothing tops out.
        # The fix was to re-base them on absolute bytes, and that fix collapsed
        # the axis: "100% clean" expressed in bytes is just `total_tor`, so
        # Tidiness, Purity and Packrat became three tiles reading one number,
        # tiering in lockstep on identical curves. "No Dust Exists Here" was
        # being awarded for owning 10 TB.
        #
        # Now the value is the *whole* torrent directory, but only on an audit
        # where the relevant pile is empty — zero otherwise, with the peaks
        # latch holding the high-water mark. So a spotless 5 TB library outranks
        # a filthy 500 TB one until it sweeps, the tier names describe what was
        # actually achieved, and the ladders still grow with the library forever.
        #
        # Existing users keep every rung: peaks latch by ladder id, so a
        # grandfathered value simply stops advancing until the library is
        # genuinely clean. Nothing is deducted, which is the rule.
        L('tidiness', 'Tidiness',
                'How big your torrent directory was, on a day when not one byte of it was orphaned.',
                total_tor if (det.get('orphaned_torrent_count', 0) or 0) == 0 else 0,
                [1 * GB, 5 * GB, 10 * GB, 25 * GB, 50 * GB, 100 * GB, 250 * GB,
                 500 * GB, 1 * TB, 2 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB,
                 100 * TB, 250 * TB, 500 * TB, 1 * PB, 2 * PB, 5 * PB],
                _fmt_bytes, 30),
        L('purity', 'Purity',
                'How much you were seeding, on a day when every last byte of it had landed in the library.',
                total_tor if (det.get('not_imported_count', 0) or 0) == 0 else 0,
                [1 * GB, 5 * GB, 10 * GB, 25 * GB, 50 * GB, 100 * GB, 250 * GB,
                 500 * GB, 1 * TB, 2 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB,
                 100 * TB, 250 * TB, 500 * TB, 1 * PB, 2 * PB, 5 * PB],
                _fmt_bytes, 30),
        # `total_media` alone, NOT `total_media + total_tor`. The two are
        # separate walks of two trees that, in a hardlink setup, are mostly the
        # same bytes — every hardlinked file carries its full size into both
        # sums, so adding them counted a 10 TB library as 20 TB. Worse, the
        # error scales with how well hardlinked you are, so the tile inflated
        # most in exactly the state it exists to reward.
        #
        # `total_media` is not a substitute for the sum, it *is* the sum
        # de-duplicated: `_is_immaculate` requires zero orphans and zero
        # not-imported, so under this gate every scoring torrent file has a
        # library counterpart and the torrent tree is a subset of the media
        # tree. The union of the two is the media tree.
        #
        # What keeps this from being Hoarder with extra steps is the gate plus
        # the peaks latch — the largest library you held *while spotless* is a
        # different peak from the largest you ever held, for anyone whose
        # library has ever been messy. Same shape as Tidiness and Purity above.
        L('conservator', 'Conservator',
                'The largest library you have ever held with nothing whatsoever wrong with it.',
                total_media if _is_immaculate(det) else 0,
                [1 * GB, 5 * GB, 10 * GB, 25 * GB, 50 * GB, 100 * GB, 250 * GB,
                 500 * GB, 1 * TB, 2 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB,
                 100 * TB, 250 * TB, 500 * TB, 1 * PB, 2 * PB, 5 * PB],
                _fmt_bytes, 35),
    ]


# Feats are one-offs, and there are a lot of them. Ungrouped they read as an
# undifferentiated wall, which hides the fact that most of them are reachable —
# so they ship in named sections, in roughly ascending difficulty within each.
#
# The *balance* rule: feats are free to give, and the cheap ones do the work.
# An early tier list that jumps from "run one audit" to "audit 100 TB" tells a
# 2 TB user the whole layer was written for somebody else. Every axis that has
# a top-end feat (size, files, uploaded, hardlink %, health, trackers, time)
# must also have rungs a small, new, ordinary library actually clears.
FEAT_GROUPS = [
    ('start',   'First steps',
     'Cheap and immediate. Everyone clears these.'),
    ('clean',   'A clean library',
     'The zero states, and the score that follows them.'),
    ('grind',   'The grind',
     'Zombies killed, pile shovelled, thrones surrendered. Cumulative and permanent.'),
    ('scale',   'Scale',
     'How much there is. Not strictly an achievement. Counted anyway.'),
    ('give',    'Giving back',
     'Bytes out, ratio earned, and how many trackers you answer to.'),
    ('machine', 'The machine',
     'What auditorr did while you were elsewhere.'),
    ('time',    'Time served',
     'Awarded for not turning it off.'),
    ('absurd',  'None of this means anything',
     'It never did. Here they are anyway.'),
]

_FEAT_GROUP_ORDER = {gid: i for i, (gid, _, _) in enumerate(FEAT_GROUPS)}


def _feats(det, runs, cross, best_score, progress=None, ladders=None,
           lifetime_up=0, seeding=0, n_trackers=0, all_clear=False):
    p = {**EMPTY_PROGRESS, **(progress or {})}
    durations   = [float(r.get('duration_seconds') or 0) for r in runs
                   if r.get('status') == 'ok' and r.get('duration_seconds')]
    longest_scan  = max(durations) if durations else 0
    shortest_scan = min(durations) if durations else 0
    peak_rss      = max([int(r.get('peak_rss_mb') or 0) for r in runs] or [0])
    had_abort     = any(r.get('status') != 'ok' for r in runs)
    auto_runs     = sum(1 for r in runs if r.get('status') == 'ok'
                        and r.get('trigger') in ('watchdog', 'scheduled'))
    manual_runs   = sum(1 for r in runs if r.get('status') == 'ok'
                        and r.get('trigger') == 'manual')
    night_audit   = any(str(r.get('ran_at', ''))[11:13] in ('02', '03', '04')
                        for r in runs if r.get('status') == 'ok')
    tiers_so_far  = sum(l['tier'] for l in (ladders or []))
    total_media = det.get('total_media_size', 0) or 0
    total_tor   = det.get('total_torrents_size', 0) or 0
    orphans     = det.get('orphaned_torrent_count', 0) or 0
    dupes       = det.get('duplicate_count', 0) or 0
    not_imp     = det.get('not_imported_count', 0) or 0
    dead        = det.get('dead_seed_count', 0) or 0
    hl_pct      = ((det.get('hardlinked_media_size', 0) or 0) / total_media * 100
                   if total_media > 0 else 0)
    ok_runs     = [r for r in runs if r.get('status') == 'ok']
    fast        = any((r.get('duration_seconds') or 1e9) < 60 for r in ok_runs)
    triggers    = {r.get('trigger') for r in ok_runs}
    observed    = _days_observed(runs)
    streak_90   = _days_at_or_above(runs, 90)
    files       = (det.get('media_file_count') or 0) + (det.get('torrent_file_count') or 0)
    multiplier  = float((cross or {}).get('multiplier') or 0)
    ratio       = (float(lifetime_up or 0) / seeding) if seeding else 0.0
    shoveled    = int(p.get('shoveled') or 0)
    kills       = int(p.get('orphan_kills') or 0) + int(p.get('dupe_kills') or 0)
    trumps      = int(p.get('trumps') or 0)
    up          = float(lifetime_up or 0)
    titles      = det.get('title_count') or 0
    uhd_bytes   = det.get('uhd_bytes') or 0
    oldest_days = det.get('oldest_media_age_days') or 0
    max_seed_d  = (det.get('max_seed_secs') or 0) / 86400.0
    immaculate  = _is_immaculate(det)
    imm_days    = _days_since(p.get('immaculate_since')) or 0
    # No score drop across the last ten audits, oldest → newest. Ten is enough
    # to mean something and short enough that one bad week does not lock it out
    # forever; the latch keeps it once earned.
    recent = [float(r['health_score']) for r in ok_runs[:10]
              if r.get('health_score') is not None][::-1]
    never_back = len(recent) >= 10 and all(
        b >= a for a, b in zip(recent, recent[1:]))

    defs = [
        # ── First steps ──────────────────────────────────────────────────────
        ('start', 'first_contact', 'First Contact', 'Complete a single audit.',
         bool(ok_runs), 50),
        ('start', 'ten_audits', 'Getting the Hang of It', 'Complete ten audits.',
         len(ok_runs) >= 10, 75),
        ('start', 'first_shovel', 'First Shovelful', 'Work through a single not-imported item.',
         shoveled >= 1, 50),
        ('start', 'first_kill', 'It Had It Coming',
         'Drive orphans or duplicates back to zero for the first time.',
         kills >= 1, 100),
        ('start', 'watched', 'The Long Watch', 'Let the watchdog trigger an audit for you.',
         'watchdog' in triggers, 75),
        ('start', 'punctual', 'Punctual', 'Let a scheduled audit run on its own.',
         'scheduled' in triggers, 75),
        ('start', 'speedrun', 'Speedrun', 'Complete an audit in under 60 seconds.',
         fast, 100),
        ('start', 'night_owl', 'Night Owl', 'Have an audit start between 2am and 5am.',
         night_audit, 100),

        # ── A clean library ──────────────────────────────────────────────────
        ('clean', 'health_half', 'Passing Grade', 'Record a health score of 50 or better.',
         best_score >= 50, 75),
        ('clean', 'health_good', 'Respectable', 'Record a health score of 75 or better.',
         best_score >= 75, 125),
        ('clean', 'health_great', 'Honour Roll', 'Record a health score of 90 or better.',
         best_score >= 90, 200),
        ('clean', 'perfect', 'The Perfect Library', 'Record a health score of 100.',
         best_score >= 100, 500),
        ('clean', 'nothing_behind', 'Nothing Left Behind', 'Hold zero orphaned torrents.',
         total_tor > 0 and orphans == 0, 150),
        ('clean', 'no_report', 'Nothing to Report', 'Hold zero duplicate files.',
         total_media > 0 and dupes == 0, 150),
        ('clean', 'all_landed', 'Stuck the Landing', 'Hold zero not-imported torrents.',
         total_tor > 0 and not_imp == 0, 150),
        ('clean', 'necromancer', 'Necromancer', 'Clear every dead seed from your client.',
         total_tor > 0 and dead == 0, 150),
        ('clean', 'immaculate', 'Immaculate', 'Zero orphans, zero duplicates and zero not-imported at once.',
         total_tor > 0 and total_media > 0 and orphans == 0 and dupes == 0 and not_imp == 0, 500),
        # The aspirational half of this shelf. Everything above is a zero state
        # a good library reaches once and then owns forever, which leaves the
        # section addressed to somebody who has not got there yet. These cross
        # the zero states with scale and with time, so there is still something
        # here for a library that has been spotless for a year.
        ('clean', 'spotless_ten', 'Spotless at Ten',
         'Hold zero orphans, zero duplicates and zero not-imported with 10 TB or more.',
         immaculate and total_media >= 10 * TB, 250),
        ('clean', 'spotless_fifty', 'Spotless at Fifty',
         'Hold all three zeros with 50 TB or more. At this size that is a decision, not luck.',
         immaculate and total_media >= 50 * TB, 400),
        ('clean', 'spotless_hundred', 'Spotless at a Hundred',
         'Hold all three zeros with 100 TB or more. Somebody is being very careful.',
         immaculate and total_media >= 100 * TB, 600),
        ('clean', 'no_notes', 'No Notes',
         'Score a perfect 100 with a library over a terabyte. No asterisk, no empty install.',
         best_score >= 100 and total_media >= TB, 400),
        ('clean', 'nothing_left', 'Nothing Left To Do',
         'Have every workflow report clear at the same time. Briefly, there is nothing to do.',
         bool(all_clear), 300),
        ('clean', 'never_back', 'Never Went Backwards',
         'Complete ten audits in a row without the health score dropping once.',
         never_back, 250),
        ('clean', 'overqualified', 'Overqualified',
         'Reach 99% hardlinked with 50 TB or more. The ratio was the easy part.',
         hl_pct >= 99 and total_media >= 50 * TB, 500),
        ('clean', 'half_linked', 'Halfway Home', 'Get half your library hardlinked.',
         hl_pct >= 50, 100),
        ('clean', 'mostly_linked', 'Nine Tenths', 'Reach 90% hardlinked.',
         hl_pct >= 90, 250),
        ('clean', 'hardlink_purist', 'Purist', 'Reach 99% hardlinked.',
         hl_pct >= 99, 500),

        # ── The grind ────────────────────────────────────────────────────────
        ('grind', 'ten_shovels', 'Ten Down', 'Shovel ten not-imported items off the pile.',
         shoveled >= 10, 75),
        ('grind', 'hundred_shovels', 'A Hundred Down', 'Shovel a hundred not-imported items.',
         shoveled >= 100, 150),
        ('grind', 'mountain', 'The Mountain Wins Anyway',
         'Shovel 1,000 not-imported items. It will refill. It always refills.',
         shoveled >= 1000, 300),
        ('grind', 'sisyphus', 'Sisyphus', 'Shovel 10,000 not-imported items. The boulder does not care.',
         shoveled >= 10000, 600),
        # Sentinel breaks. Cleanup and Dedupe are done-once jobs, so the useful
        # signal is not "you did it again" — it is "it came undone." Awarded,
        # never deducted: a break is information the user wants.
        ('grind', 'double_tap', 'Double Tap',
         'Kill the same mess a second time. You knew it was not over.',
         int(p.get('orphan_kills') or 0) >= 2 or int(p.get('dupe_kills') or 0) >= 2, 150),
        ('grind', 'five_kills', 'Persistent',
         'Rack up five kills across orphans and duplicates.',
         kills >= 5, 200),
        ('grind', 'fifteen_kills', 'Seasoned',
         'Rack up fifteen kills across orphans and duplicates.',
         kills >= 15, 300),
        ('grind', 'unkillable', 'They Keep Coming', 'Rack up 50 kills across orphans and duplicates.',
         kills >= 50, 500),
        ('grind', 'orphans_returned', 'Well, That Didn\'t Last',
         'Have orphaned torrents claw their way back after you cleared them all.',
         int(p.get('orphan_breaks') or 0) >= 1, 75),
        ('grind', 'dupes_returned', 'It Came Back',
         'Have duplicate files claw their way back after you cleared them all.',
         int(p.get('dupe_breaks') or 0) >= 1, 75),
        ('grind', 'groundhog', 'Groundhog Day',
         'Let the same mess come back five times. Something upstream is misbehaving.',
         int(p.get('orphan_breaks') or 0) + int(p.get('dupe_breaks') or 0) >= 5, 150),
        # Trumps. Balanced down like everything else here: a private tracker
        # sends these out rarely, so the first rung has to be reachable by
        # somebody who has done exactly one.
        ('grind', 'trump_first', 'Politely Replaced',
         'Complete a trump swap. Somebody had a better copy. You took it well.',
         trumps >= 1, 75),
        ('grind', 'trump_five', 'Five Times Deposed',
         'Complete five trump swaps. The throne was never yours.',
         trumps >= 5, 200),
        ('grind', 'trump_many', 'Professional Understudy',
         'Complete twenty-five trump swaps. At this point it is just what you do.',
         trumps >= 25, 400),
        ('absurd', 'trump_entourage', 'Deposed in Bulk',
         'Retire four or more registrations of the same release in a single trump swap.',
         int(p.get('trump_max_group') or 0) >= 4, 150),

        # ── Scale ────────────────────────────────────────────────────────────
        # Rungs all the way down. A 2 TB library is the common case and must not
        # look at this section and find nothing addressed to it.
        ('scale', 'featherweight', 'Featherweight',
         'Audit a library under 10 GB. Everyone starts somewhere.',
         0 < total_media < 10 * GB, 50),
        ('scale', 'hundred_gb', 'A Hundred Gigs', 'Audit a library of 100 GB or more.',
         total_media >= 100 * GB, 75),
        ('scale', 'terabyte', 'First Terabyte', 'Audit a library of 1 TB or more.',
         total_media >= TB, 125),
        ('scale', 'five_tb', 'Five Terabytes', 'Audit a library of 5 TB or more.',
         total_media >= 5 * TB, 175),
        ('scale', 'ten_tb', 'Double Digits', 'Audit a library of 10 TB or more.',
         total_media >= 10 * TB, 200),
        ('scale', 'fifty_tb', 'Fifty Terabytes', 'Audit a library of 50 TB or more.',
         total_media >= 50 * TB, 250),
        ('scale', 'petabyte', 'Absolutely Unhinged', 'Audit a library of 100 TB or more.',
         total_media >= 100 * TB, 300),
        # The top end keeps going — field reports include 100-500 TB libraries,
        # and those users should still have something left to chase.
        ('scale', 'quarter_pb', 'Storage Problem', 'Audit a library of 250 TB or more.',
         total_media >= 250 * TB, 400),
        ('scale', 'half_pb', 'Half a Petabyte', 'Audit a library of 500 TB or more.',
         total_media >= 500 * TB, 500),
        ('scale', 'full_pb', 'Petabyte Club', 'Audit a full petabyte. Membership is its own punishment.',
         total_media >= PB, 750),
        ('scale', 'two_pb', 'Are You a Datacenter?', 'Audit two petabytes or more. Genuinely, how.',
         total_media >= 2 * PB, 1000),
        # Titles, not files or bytes — the one scale axis that says what the
        # library *is*. A hundred is a shelf; a thousand is a problem.
        ('scale', 'hundred_titles', 'A Hundred Titles', 'Hold a hundred distinct releases.',
         titles >= 100, 75),
        ('scale', 'thousand_titles', 'A Thousand Titles',
         'Hold a thousand distinct releases. You have seen perhaps forty of them.',
         titles >= 1000, 150),
        ('scale', 'ten_thousand_titles', 'Ten Thousand Titles',
         'Hold ten thousand distinct releases. The catalogue is now the hobby.',
         titles >= 10000, 300),
        ('scale', 'all_4k', 'Nothing But the Best',
         'Have 90% of your library at 2160p. Storage is cheaper than compromise.',
         total_media > 0 and uhd_bytes >= 0.9 * total_media, 350),
        ('scale', 'thousand_files', 'A Thousand Files', 'Have a thousand files under audit at once.',
         files >= 1000, 75),
        ('scale', 'ten_thousand_files', 'Ten Thousand Files', 'Have ten thousand files under audit at once.',
         files >= 10000, 125),
        ('scale', 'hundred_thousand_files', 'A Hundred Thousand Files',
         'Have a hundred thousand files under audit at once.',
         files >= 100000, 200),
        ('scale', 'million_files', 'One Million Files', 'Have a million files under audit at once.',
         files >= 1000000, 400),
        ('scale', 'ten_million_files', 'Ten Million Files', 'Have ten million files under audit at once.',
         files >= 10000000, 800),

        # ── Giving back ──────────────────────────────────────────────────────
        ('give', 'gave_back', 'Gave Something Back', 'Upload a gigabyte, lifetime.',
         up >= GB, 50),
        ('give', 'gave_10', 'Ten Gigs Given', 'Upload 10 GB, lifetime.',
         up >= 10 * GB, 75),
        ('give', 'gave_100', 'A Hundred Gigs Given', 'Upload 100 GB, lifetime.',
         up >= 100 * GB, 100),
        ('give', 'gave_tb', 'A Terabyte Given', 'Upload a terabyte, lifetime.',
         up >= TB, 200),
        ('give', 'gave_10tb', 'Ten Terabytes Given', 'Upload 10 TB, lifetime.',
         up >= 10 * TB, 350),
        ('give', 'gave_100tb', 'The Firehose', 'Upload 100 TB, lifetime. Somebody had to.',
         up >= 100 * TB, 600),
        ('give', 'in_the_black', 'In the Black',
         'Upload more than you are seeding. A ratio of 1.0 across everything.',
         ratio >= 1.0, 150),
        ('give', 'free_estate', 'Free Real Estate',
         'Get your cross-seed multiplier above 1.5×. The same bytes, twice employed.',
         multiplier >= 1.5, 150),
        ('give', 'twice_over', 'Twice the Bytes',
         'Get your cross-seed multiplier to 2×. Every byte works two jobs.',
         multiplier >= 2.0, 300),
        ('give', 'lopsided', 'One Tracker To Rule Them All',
         'Seed on exactly one tracker. Loyal, or trapped.',
         n_trackers == 1, 75),
        ('give', 'small_circle', 'A Small Circle', 'Seed on three or more distinct trackers.',
         n_trackers >= 3, 75),
        ('give', 'well_connected', 'Well Connected', 'Seed on five or more distinct trackers.',
         n_trackers >= 5, 125),
        ('give', 'cosmopolitan', 'Cosmopolitan', 'Seed on ten or more distinct trackers.',
         n_trackers >= 10, 250),

        # ── The machine ──────────────────────────────────────────────────────
        ('machine', 'lean_machine', 'Lean Machine',
         'Complete a scan that peaked under 256 MB of RAM. Tidy.',
         0 < peak_rss < 256, 100),
        ('machine', 'blink', 'Blink and Miss It', 'Complete an audit in under ten seconds.',
         0 < shortest_scan < 10, 150),
        ('machine', 'crash_survivor', 'Walked It Off',
         'Survive an aborted scan and complete a successful one afterwards.',
         had_abort and bool(ok_runs), 150),
        ('machine', 'marathon_scan', 'Longer Than a Feature Film',
         'Complete a single audit that took more than two hours.',
         longest_scan >= 7200, 200),
        ('machine', 'workhorse', 'The Machine Does It All',
         'Let automation run 100 audits for you.',
         auto_runs >= 100, 200),
        ('machine', 'control_freak', 'Control Freak',
         'Start 100 audits by hand. The button works fine, you just like pressing it.',
         manual_runs >= 100, 200),
        ('machine', 'memory_hog', 'Four Gigabytes of RAM',
         'Have a single scan peak above 4 GB. Your server felt that one.',
         peak_rss >= 4096, 250),

        # ── Time served ──────────────────────────────────────────────────────
        ('time', 'first_week', 'A Week In', 'Keep auditorr running for a week.',
         observed >= 7, 75),
        ('time', 'first_month', 'A Month In', 'Keep auditorr running for a month.',
         observed >= 30, 150),
        ('time', 'first_quarter', 'A Season In', 'Keep auditorr running for ninety days.',
         observed >= 90, 250),
        ('time', 'flawless_week', 'A Quiet Week', 'Hold 90+ health for seven consecutive days.',
         streak_90 >= 7, 150),
        ('time', 'flawless_month', 'A Quiet Month', 'Hold 90+ health for thirty consecutive days.',
         streak_90 >= 30, 400),
        ('time', 'boring', 'Nothing Ever Happens',
         'Reach 50 audits. Most of them found nothing. That is the good outcome.',
         len(ok_runs) >= 50, 150),
        ('time', 'century', 'Century', 'Complete 100 audits.',
         len(ok_runs) >= 100, 200),
        ('time', 'week_incident', 'A Week Without Incident',
         'Hold all three zeros for seven consecutive days.',
         imm_days >= 7, 200),
        ('time', 'month_incident', 'A Month Without Incident',
         'Hold all three zeros for thirty consecutive days.',
         imm_days >= 30, 350),
        ('time', 'year_incident', 'A Year Without Incident',
         'Hold all three zeros for a full year. Nothing happened, at length.',
         imm_days >= 365, 750),
        ('time', 'anniversary', 'One Year Held',
         'Seed a single torrent for a full year without interruption.',
         max_seed_d >= 365, 200),
        # Both of these reward something that predates auditorr, which is the
        # whole point: the veteran arrives with history, and a prize layer that
        # starts everyone at zero has nothing to say to them on day one.
        ('time', 'old_guard', 'The Old Guard',
         'Still be seeding something you started before you installed auditorr.',
         max_seed_d > observed > 0, 150),
        ('time', 'predates_install', 'It Was Always Here',
         'Hold a file older than auditorr has been watching. It came with the drive.',
         oldest_days > observed > 0, 100),
        ('time', 'veteran', 'Old Timer', 'Keep auditorr running for a full year.',
         observed >= 365, 400),
        ('time', 'ancient', 'Still Here', 'Keep auditorr running for five years. Genuinely, why.',
         observed >= 1825, 1000),

        # ── Absurdity tier. None of these mean anything. That is the point. ───
        ('absurd', 'inverted', 'Backwards Library',
         'Have a torrent directory larger than your media library. Bold strategy.',
         total_tor > total_media > 0, 200),
        ('absurd', 'empty_handed', 'Nothing To Audit',
         'Run an audit against a completely empty library. Ambitious.',
         bool(ok_runs) and total_media == 0 and total_tor == 0, 100),
        ('absurd', 'ten_tiers', 'Trinket Collector', 'Unlock 10 prize tiers.',
         tiers_so_far >= 10, 75),
        ('absurd', 'fifty_tiers', 'Shelf Filling', 'Unlock 50 prize tiers.',
         tiers_so_far >= 50, 150),
        ('absurd', 'overachiever', 'Overachiever', 'Unlock 100 prize tiers.',
         tiers_so_far >= 100, 300),
        ('absurd', 'why', 'Why Are You Like This', 'Unlock 250 prize tiers.',
         tiers_so_far >= 250, 750),
        ('absurd', 'the_end', 'There Is No End', 'Unlock 400 prize tiers. There is still no end.',
         tiers_so_far >= 400, 2000),
    ]
    # Latched: a feat earned once is earned forever. Most of these read current
    # state, so without this a zombie rising would quietly claw back the points
    # its own kill awarded.
    latched = set(p.get('feats_earned') or [])
    return [
        {'id': fid, 'label': label, 'desc': desc, 'group': group,
         'earned': bool(earned) or fid in latched, 'points': pts}
        for group, fid, label, desc, earned, pts in defs
    ]


def _rank_for(points):
    name, floor, idx = RANKS[0][1], RANKS[0][0], 0
    for i, (threshold, label) in enumerate(RANKS):
        if points >= threshold:
            name, floor, idx = label, threshold, i
    nxt = RANKS[idx + 1] if idx + 1 < len(RANKS) else None
    span = (nxt[0] - floor) if nxt else 1
    return {
        'name': name, 'index': idx, 'floor': floor,
        'total_ranks': len(RANKS),
        'next_name': nxt[1] if nxt else None,
        'next_at':   nxt[0] if nxt else None,
        'pct': round(min(100.0, max(0.0, (points - floor) / span * 100)), 1) if nxt else 100.0,
    }


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def build_state(cfg, results, runs, lifetime_uploaded=0, progress=None):
    """Assemble the Next steps page payload.

    Reads summary data only — never `file_results`. This endpoint is polled, and
    deserializing full file lists is the known RAM hotspot (see CLAUDE.md).
    """
    dashboard = (results or {}).get('dashboard') or {}
    det       = ((dashboard.get('current') or {}).get('details')) or {}
    cross     = dashboard.get('cross_seed_stats')
    tracker_stats = (results or {}).get('tracker_file_stats') or {}
    ok_runs   = [r for r in runs if r.get('status') == 'ok']
    has_audit = bool(ok_runs)

    source = cfg.get('TORRENT_SOURCE', 'qbit')
    source_ok = bool(cfg.get('QUI_HOST') if source == 'qui' else cfg.get('QB_HOST'))
    arr_ok = bool(
        (cfg.get('SONARR_URL') and cfg.get('SONARR_API_KEY'))
        or (cfg.get('RADARR_URL') and cfg.get('RADARR_API_KEY'))
        or any(c.get('url') for c in (cfg.get('ARR_CONNECTIONS') or []))
    )

    setup_steps = _setup_steps(cfg, has_audit)
    setup_done  = [s for s in setup_steps if s['done']]
    # Sonarr/Radarr are genuinely optional, but at least one is needed for
    # Backfill — so "complete" means the required three plus either arr.
    required_ok = all(s['done'] for s in setup_steps if s['id'] not in ('sonarr', 'radarr'))
    setup_complete = required_ok and any(
        s['done'] for s in setup_steps if s['id'] in ('sonarr', 'radarr'))

    best_score = max((float(r['health_score']) for r in ok_runs
                      if r.get('health_score') is not None), default=0.0)

    ladders = _ladders(det, runs, cross, tracker_stats, best_score, lifetime_uploaded, progress)
    # Feats read the same seeding/tracker totals the ladders do, so a feat and
    # its neighbouring ladder can never disagree about how many trackers you
    # are on.
    seeding    = sum((s or {}).get('seeding_size', 0) for s in (tracker_stats or {}).values())
    n_trackers = len([t for t in (tracker_stats or {}) if t and t != 'None'])
    # Built before the feats because one of them ("Nothing Left To Do") is a
    # statement about the spine rather than about the audit numbers.
    rows = _workflow_rows(cfg, det, source_ok, arr_ok) if has_audit else []
    all_clear = bool(rows) and all(r['state'] in ('maintain', 'standby') for r in rows)
    feats   = _feats(det, runs, cross, best_score, progress, ladders,
                     lifetime_up=lifetime_uploaded, seeding=seeding,
                     n_trackers=n_trackers, all_clear=all_clear)

    # Meta ladders — prizes for collecting prizes, which is either the most or
    # the least useless thing on this page. Computed last because they measure
    # the others. Appended after `feats` so Trophy Hunter can count them.
    peaks = (progress or {}).get('peaks') or {}
    ladders.append(_ladder(
        'completionist', 'Completionist',
        'Prize tiers unlocked across every ladder. Yes, this is itself one of them. Sorry.',
        sum(l['tier'] for l in ladders),
        [1, 5, 10, 25, 50, 75, 100, 150, 200, 250, 300, 400, 500],
        _fmt_plain, 20, peaks=peaks))
    ladders.append(_ladder(
        'trophyhunter', 'Trophy Hunter',
        'One-off feats earned. The ones you cannot grind for. They simply happen to you.',
        sum(1 for f in feats if f['earned']),
        [1, 2, 3, 5, 8, 12, 16, 20, 25, 30, 40, 50, 60, 70],
        _fmt_plain, 30, peaks=peaks))

    points = (
        sum(s['points'] for s in setup_done)
        + sum(l['points'] for l in ladders)
        + sum(f['points'] for f in feats if f['earned'])
    )

    tiers_earned = sum(l['tier'] for l in ladders) + sum(1 for f in feats if f['earned'])
    tiers_total  = sum(l['tiers_total'] for l in ladders) + len(feats)

    by_id = {l['id']: l for l in ladders}
    for r in rows:
        r['summary'] = _summary(r)
        r['stat']    = _stat(r)
        r['reward']  = _reward_line(r, progress, det)
        # The carrot: which prizes this specific workflow moves, and the nearest
        # one still locked. Abstract shelves do not motivate; "clear these and
        # you hit Shoveler V" does.
        owned = [by_id[lid] for lid, owners in LADDER_OWNER.items()
                 if r['id'] in owners and lid in by_id]
        owned.sort(key=lambda l: (l['maxed'], -l['pct']))
        r['prizes'] = owned
        nxt = next((l for l in owned if not l['maxed']), None)
        r['next_prize'] = None if not nxt else {
            'ladder': nxt['name'], 'ladder_id': nxt['id'], 'label': nxt['next_label'],
            'at': nxt['next_at_label'], 'pct': nxt['pct'],
            'value_label': nxt['value_label'],
            # Where the rung sits on its ladder — a bare name gives no sense of
            # whether this is rung 2 or rung 12.
            'n': nxt['next_n'], 'of': nxt['tiers_total'],
        }

    # The moment step 1 is reached: orphans and duplicates cleared. Not a
    # permanent state — it is defended, not finished — but it is the point the
    # page shifts from a cleanup sprint to the ongoing loop.
    base = [r for r in rows if r['stage'] == 'baseline']
    baseline_clear = bool(base) and all(
        r['state'] not in ('fix', 'optimize') for r in base)

    # A fresh install has no audit to reason about — the honest answer to "what
    # should I be doing" is "finish setup", not an empty workflow list.
    stage = 'running' if has_audit else 'setup'

    return {
        'stage': stage,
        'health': {'score': dashboard.get('score'), 'status': dashboard.get('status')},
        'rank': {**_rank_for(points), 'points': points},
        'setup': {
            'complete': setup_complete,
            'done': len(setup_done), 'total': len(setup_steps),
            'steps': setup_steps,
        },
        'rows': rows,
        'hero': rows[0]['id'] if rows else None,
        'baseline_clear': baseline_clear,
        'ladders': ladders,
        # Rungs, not ladders — "34/97" answers "how far up this theme am I",
        # which is the same question the per-medallion x/y answers one level
        # down. Counting ladders would just say how many I have touched.
        'ladder_groups': [
            {'id': gid, 'label': label, 'blurb': blurb,
             'earned': sum(l['tier'] for l in ladders if l['group'] == gid),
             'total':  sum(l['tiers_total'] for l in ladders if l['group'] == gid)}
            for gid, label, blurb in LADDER_GROUPS
        ],
        'feats': feats,
        'feat_groups': [
            {'id': gid, 'label': label, 'blurb': blurb,
             'earned': sum(1 for f in feats if f['group'] == gid and f['earned']),
             'total':  sum(1 for f in feats if f['group'] == gid)}
            for gid, label, blurb in FEAT_GROUPS
        ],
        'prizes': {'earned': tiers_earned, 'total': tiers_total},
        'streak_90': _days_at_or_above(runs, 90),
        'audits': len(ok_runs),
    }
