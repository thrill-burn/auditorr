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
REWARD_KIND = {
    'cleanup': 'zombie', 'dedupe': 'zombie',
    'triage': 'shovel', 'backfill': 'crystal',
    'trumped': 'ondemand',
}

# Which ladders a given workflow actually feeds. Used to show a concrete carrot
# on each row — "do this and you get that" — instead of leaving the prize layer
# as an abstract shelf the user has to connect to their own actions. Ladders
# absent here are library-wide (Hoarder, Auditor, Chronicler…) and appear only
# in the top shelf, because no single workflow moves them.
LADDER_OWNER = {
    'exterminator': ('cleanup',),
    'sentinel':     ('cleanup',),
    'clonehunter':  ('dedupe',),
    'shoveler':     ('triage',),
    'lapidary':     ('backfill',),
    'alchemist':    ('backfill',),
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
    'last_ni_count': None,
    'last_excl_fp': None,
    # Everything below exists to make the prize layer **ratchet**. Most ladders
    # and feats read current state, which can regress — a library that shrinks,
    # a tracker you stop using, a zombie that rises. Without latching, a break
    # would silently claw back points already awarded, which is precisely the
    # punishment this design refuses to hand out. Earned is earned.
    'peaks': {},              # ladder id -> best value ever seen
    'feats_earned': [],       # feat ids, latched on first earn
}

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
    (0,     'Unaudited'),
    (100,   'Apprentice Indexer'),
    (300,   'Journeyman Linker'),
    (600,   'Hardlink Adept'),
    (1000,  'Orphan Warden'),
    (1600,  'Seed Custodian'),
    (2400,  'Ratio Alchemist'),
    (3500,  'Library Curator'),
    (5000,  'Master Archivist'),
    (7000,  'Grand Deduplicator'),
    (10000, 'Keeper of Inodes'),
    (14000, 'Lord of the Hardlink'),
    (20000, 'Filesystem Ascendant'),
]

GB = 1024 ** 3
TB = 1024 ** 4


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
    suggests them. Without this, adding one pattern would register as a big
    shovelful of work the user never did.
    """
    blob = json.dumps([
        sorted(cfg.get('EXCLUSION_PATTERNS') or []),
        sorted(cfg.get('DISC_RIP_EXCLUSION_PRESETS') or []),
        sorted(cfg.get('MEDIA_SERVER_EXCLUSION_PRESETS') or []),
    ], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def update_progress(progress, cfg, det, state=None, now=None):
    """Advance the reward counters by one audit. Pure — returns a new dict.

    Called from `run_audit_process` after stats are computed. Everything here
    is monotonic: nothing a user does can take away a shovelful they already
    dug, a zombie they already killed, or a peak they already hit.

    `state` is the freshly built payload (`build_state` with the *previous*
    progress). It is used only to latch ladder peaks and earned feats, which is
    what stops current-state prizes from un-earning on a regression.
    """
    p = {**EMPTY_PROGRESS, **(progress or {})}
    now = now or datetime.now()
    stamp = now.isoformat()
    # A library that is already clean on its very first audit was never killed —
    # there was nothing there to kill.
    first_run = p['last_ni_count'] is None

    ni_count = det.get('not_imported_count', 0) or 0
    excl_fp  = exclusion_fingerprint(cfg)
    # Only count the delta when the exclusion set is unchanged — otherwise a
    # pile that "shrank" may just be a pile that got hidden.
    if p['last_ni_count'] is not None and excl_fp == p['last_excl_fp']:
        p['shoveled'] += max(0, p['last_ni_count'] - ni_count)
    p['last_ni_count'] = ni_count
    p['last_excl_fp']  = excl_fp

    total_media = det.get('total_media_size', 0) or 0
    if total_media > 0:
        hl_pct = (det.get('hardlinked_media_size', 0) or 0) / total_media * 100
        p['hl_peak'] = max(float(p['hl_peak'] or 0.0), hl_pct)

    # Zombies: a kill each time the count is driven back to zero, a streak for
    # how long it stays there, and a break counter when it rises again.
    for key, count_key in (('orphan', 'orphaned_torrent_count'),
                           ('dupe',   'duplicate_count')):
        since_key = f'{key}_clean_since'
        is_clean  = (det.get(count_key, 0) or 0) == 0
        if is_clean:
            if not p[since_key]:
                # Was dirty (or unobserved) and is now zero. Only the former is
                # a kill — a first audit that happens to be clean killed nothing.
                if not first_run:
                    p[f'{key}_kills'] = int(p[f'{key}_kills'] or 0) + 1
                p[since_key] = stamp
        else:
            if p[since_key]:
                # It was clean and now it isn't — that is the thing to tell them.
                p[f'{key}_breaks'] = int(p[f'{key}_breaks'] or 0) + 1
            p[since_key] = None

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


def _days_since(stamp, now=None):
    if not stamp:
        return None
    try:
        delta = (now or datetime.now()) - datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
    return max(0, delta.days)


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
            "Files in your torrent folder that your torrent client has no knowledge of. "
            "They cost you disk and earn you nothing. Cleanup groups them by release folder "
            "and writes a delete script you review and run yourself."
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
            "Bit-for-bit identical files that share no inode — you are paying for the same "
            "bytes twice. Dedupe groups them and writes a script that replaces the copies "
            "with hardlinks."
        ),
        'clear_line': 'No duplicates. You are not paying for the same bytes twice.',
    })

    # ── Triage ───────────────────────────────────────────────────────────────
    n_count = det.get('not_imported_count', 0) or 0
    n_size  = det.get('not_imported_size', 0) or 0
    dead    = det.get('dead_seed_count', 0) or 0
    tri_state = 'blocked' if not source_ok else _threshold_state(n_size, det.get('ni_limit'), n_count)
    if tri_state == 'maintain' and dead:
        tri_state = 'optimize'
    rows.append({
        'id': 'triage', 'label': 'Triage', 'accent': 'var(--red)',
        'card': 'Not Imported',
        'state': tri_state,
        'blocked_reason': None if source_ok else 'Connect your torrent client first.',
        'count': n_count + dead, 'bytes': n_size, 'dead_seeds': dead,
        'score_lost': _recoverable(det.get('ni_score'), det.get('ni_max', 10)),
        'score_max': round(float(det.get('ni_max', 10) or 0), 1),
        'teaching': (
            "Seeding torrents with no matching file in your media folder. Triage explains "
            "why each one never landed — unregistered, superseded, still importing — and "
            "gives you the right action per verdict instead of one blunt delete."
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
            "Media in your library with no torrent behind it — dead weight on your disk that "
            "earns no ratio. Backfill matches it against your indexers and grabs a release "
            "that hardlinks straight onto the file you already have."
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
            "Got a trump PM from a tracker? Paste it here. Trumped finds the torrents you are "
            "seeding for that release, confirms the cross-seed group, and swaps in the "
            "replacement without breaking the hardlinks."
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
        pile = row['count']
        return {
            'kind': kind,
            'headline': f"{p['shoveled']:,} shovelled so far. {_pluralize(pile, 'item')} on the pile today.",
            'detail': 'The pile refills forever. Every shovelful counts, permanently.',
        }

    if kind == 'crystal':
        cur  = row.get('ratio_pct', 0.0)
        peak = round(float(p['hl_peak'] or 0.0), 1)
        gap  = round(max(0.0, 100.0 - peak), 1)
        line = f"{cur}% now · best ever {peak}%"
        detail = (f"{gap} points from perfect." if gap
                  else 'Perfect. Genuinely nothing left to hardlink.')
        return {'kind': kind, 'headline': line,
                'detail': detail + ' Your best never goes back down.'}

    return {'kind': kind, 'headline': 'No prize. No pile. No streak.',
            'detail': 'Trumped only matters when a tracker says so.'}


def _fmt_bytes(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit in ('B', 'KB') else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _headline(row):
    if row['state'] == 'blocked':
        return row['blocked_reason'] or 'Not configured yet.'
    if row['state'] in ('maintain', 'standby'):
        return row['clear_line']
    size = _fmt_bytes(row['bytes'])
    if row['id'] == 'cleanup':
        return f"{_pluralize(row['count'], 'orphaned file')} · {size}"
    if row['id'] == 'dedupe':
        return f"{_pluralize(row['count'], 'duplicate')} · {size} recoverable"
    if row['id'] == 'triage':
        parts = []
        live = row['count'] - row.get('dead_seeds', 0)
        if live > 0:
            parts.append(_pluralize(live, 'not-imported torrent'))
        if row.get('dead_seeds'):
            parts.append(_pluralize(row['dead_seeds'], 'dead seed'))
        return ' · '.join(parts) or 'Items need a verdict'
    if row['id'] == 'backfill':
        return f"{row['ratio_pct']}% hardlinked · {size} earning nothing"
    return ''


# ---------------------------------------------------------------------------
# Useless prizes. They do nothing. That is the point.
#
# Progress Quest rules: many tiers, mock-heroic names, the next one always in
# sight. Every threshold below is derivable from audit history, so no ladder
# can advance on a number auditorr did not actually measure.
# ---------------------------------------------------------------------------

_ROMAN = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X',
          'XI', 'XII', 'XIII', 'XIV', 'XV']


def _roman(n):
    return _ROMAN[n - 1] if 1 <= n <= len(_ROMAN) else str(n)


def _fmt_plain(n):
    n = float(n)
    return str(int(n)) if n == int(n) else f"{n:g}"


def _fmt_pct(n):
    return f"{float(n):g}%"


def _fmt_days(n):
    return _pluralize(int(n), 'day')


def _fmt_x(n):
    return f"{float(n):.2f}×"


def _ladder(lid, name, blurb, value, thresholds, fmt, points_step=25, peaks=None):
    """Build one tiered ladder. Points escalate so later tiers feel worth it.

    Every ladder ratchets on the best value ever recorded (`peaks`), so a
    library that shrinks or a tracker you stop using can never demote a tier
    you already earned.
    """
    value = max(float(value or 0), float((peaks or {}).get(lid) or 0))
    tiers = []
    for i, at in enumerate(thresholds):
        tiers.append({
            'n': i + 1,
            'label': f"{name} {_roman(i + 1)}",
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
    return {
        'id': lid, 'name': name, 'blurb': blurb,
        'value': value, 'value_label': fmt(value),
        'tier': len(earned), 'tiers_total': len(tiers),
        'tier_label': earned[-1]['label'] if earned else None,
        'next_label': nxt['label'] if nxt else None,
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
    n_trackers  = len([t for t in (tracker_stats or {}) if t and t != 'None'])
    ok_runs     = [r for r in runs if r.get('status') == 'ok']
    peaks       = (progress or {}).get('peaks') or {}

    def L(*args, **kw):
        return _ladder(*args, peaks=peaks, **kw)

    return [
        L('hoard', 'Hoarder',
                'Total media audited. No, this is not a problem. You can stop whenever you want.',
                total_media,
                [100 * GB, 500 * GB, 1 * TB, 2 * TB, 5 * TB, 10 * TB, 20 * TB,
                 50 * TB, 100 * TB, 250 * TB],
                _fmt_bytes, 30),
        L('seedbearer', 'Seedbearer',
                'Total size of everything you are seeding.',
                seeding,
                [50 * GB, 250 * GB, 1 * TB, 2 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB, 100 * TB],
                _fmt_bytes, 30),
        L('benefactor', 'Benefactor',
                'Lifetime bytes uploaded across every tracker. Somebody out there is grateful.',
                lifetime_up,
                [100 * GB, 500 * GB, 1 * TB, 5 * TB, 10 * TB, 25 * TB, 50 * TB,
                 100 * TB, 500 * TB, 1024 * TB],
                _fmt_bytes, 40),
        L('auditor', 'Auditor',
                'Audits completed. Each one was a choice.',
                len(ok_runs),
                [1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 10000],
                _fmt_plain, 20),
        L('custodian', 'Custodian',
                'Best health score ever recorded. It only goes up from here. Statistically.',
                best_score,
                [25, 40, 50, 60, 70, 75, 80, 85, 90, 95, 98, 99, 100],
                _fmt_pct, 25),
        # Crystal: the hardlink high-water mark, tracked explicitly in progress
        # as well as via the generic peak latch.
        L('lapidary', 'Lapidary',
                'Best hardlinked share you have ever reached. Cut slowly toward a perfect 100%.',
                max(hl_pct, float((progress or {}).get('hl_peak') or 0.0)),
                [50, 70, 80, 90, 95, 98, 99, 99.5, 99.9, 100],
                _fmt_pct, 35),
        # Shovel: cumulative and permanent. The pile refills; the count doesn't reset.
        L('shoveler', 'Shoveler',
                'Not-imported items you have worked through, ever. The pile always comes back.',
                (progress or {}).get('shoveled') or 0,
                [10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000],
                _fmt_plain, 35),
        # Zombies, tracked separately: orphans and duplicates come back for
        # different reasons, and each row deserves its own carrot rather than
        # both pointing at one shared counter.
        L('exterminator', 'Exterminator',
                'Times you have driven orphaned torrents back to zero. They keep getting up.',
                int((progress or {}).get('orphan_kills') or 0),
                [1, 2, 3, 5, 10, 20, 35, 50, 100],
                _fmt_plain, 40),
        L('clonehunter', 'Clone Hunter',
                'Times you have driven duplicate files back to zero. They keep coming back too.',
                int((progress or {}).get('dupe_kills') or 0),
                [1, 2, 3, 5, 10, 20, 35, 50, 100],
                _fmt_plain, 40),
        # Zombie: the streak of a clean state holding.
        L('sentinel', 'Sentinel',
                'Consecutive days with zero orphaned torrents. Killed once, then guarded.',
                _days_since((progress or {}).get('orphan_clean_since')) or 0,
                [1, 7, 30, 90, 180, 365, 730],
                _fmt_days, 35),
        L('alchemist', 'Ratio Alchemist',
                'Cross-seed multiplier — how many times over each byte is earning.',
                multiplier,
                [1.05, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0],
                _fmt_x, 45),
        L('diplomat', 'Diplomat',
                'Distinct trackers you are seeding on.',
                n_trackers,
                [1, 2, 3, 5, 8, 12, 20, 30],
                _fmt_plain, 30),
        L('steady', 'Steady Hand',
                'Consecutive days holding a health score of 90 or better.',
                _days_at_or_above(runs, 90),
                [1, 3, 7, 14, 30, 60, 90, 180, 365],
                _fmt_days, 40),
        L('chronicler', 'Chronicler',
                'Days since your first audit. Time passes whether you scan or not.',
                _days_observed(runs),
                [1, 7, 30, 90, 180, 365, 730, 1825],
                _fmt_days, 25),
        L('archivist', 'Archivist',
                'Files under audit. Every one of them personally inspected. By a computer.',
                (det.get('media_file_count') or 0) + (det.get('torrent_file_count') or 0),
                [100, 1000, 5000, 10000, 50000, 100000, 250000, 500000, 1000000],
                _fmt_plain, 25),
    ]


def _feats(det, runs, cross, best_score, progress=None):
    p = {**EMPTY_PROGRESS, **(progress or {})}
    total_media = det.get('total_media_size', 0) or 0
    total_tor   = det.get('total_torrents_size', 0) or 0
    orphans     = det.get('orphaned_torrent_count', 0) or 0
    dupes       = det.get('duplicate_count', 0) or 0
    not_imp     = det.get('not_imported_count', 0) or 0
    dead        = det.get('dead_seed_count', 0) or 0
    ok_runs     = [r for r in runs if r.get('status') == 'ok']
    fast        = any((r.get('duration_seconds') or 1e9) < 60 for r in ok_runs)
    triggers    = {r.get('trigger') for r in ok_runs}

    defs = [
        ('first_contact', 'First Contact', 'Complete a single audit.',
         bool(ok_runs), 50),
        ('nothing_behind', 'Nothing Left Behind', 'Hold zero orphaned torrents.',
         total_tor > 0 and orphans == 0, 150),
        ('no_report', 'Nothing to Report', 'Hold zero duplicate files.',
         total_media > 0 and dupes == 0, 150),
        ('all_landed', 'Stuck the Landing', 'Hold zero not-imported torrents.',
         total_tor > 0 and not_imp == 0, 150),
        ('immaculate', 'Immaculate', 'Zero orphans, zero duplicates and zero not-imported at once.',
         total_tor > 0 and total_media > 0 and orphans == 0 and dupes == 0 and not_imp == 0, 500),
        ('necromancer', 'Necromancer', 'Clear every dead seed from your client.',
         total_tor > 0 and dead == 0, 150),
        ('perfect', 'The Perfect Library', 'Record a health score of 100.',
         best_score >= 100, 500),
        ('speedrun', 'Speedrun', 'Complete an audit in under 60 seconds.',
         fast, 100),
        ('watched', 'The Long Watch', 'Let the watchdog trigger an audit for you.',
         'watchdog' in triggers, 75),
        ('punctual', 'Punctual', 'Let a scheduled audit run on its own.',
         'scheduled' in triggers, 75),
        ('century', 'Century', 'Complete 100 audits.',
         len(ok_runs) >= 100, 200),
        ('petabyte', 'Absolutely Unhinged', 'Audit a library of 100 TB or more.',
         total_media >= 100 * TB, 300),
        # Sentinel breaks. Cleanup and Dedupe are done-once jobs, so the useful
        # signal is not "you did it again" — it is "it came undone." Awarded,
        # never deducted: a break is information the user wants.
        ('first_kill', 'It Had It Coming',
         'Drive orphans or duplicates back to zero for the first time.',
         int(p.get('orphan_kills') or 0) + int(p.get('dupe_kills') or 0) >= 1, 100),
        ('orphans_returned', 'Well, That Didn\'t Last',
         'Have orphaned torrents claw their way back after you cleared them all.',
         int(p.get('orphan_breaks') or 0) >= 1, 75),
        ('dupes_returned', 'It Came Back',
         'Have duplicate files claw their way back after you cleared them all.',
         int(p.get('dupe_breaks') or 0) >= 1, 75),
        ('double_tap', 'Double Tap',
         'Kill the same mess a second time. You knew it was not over.',
         int(p.get('orphan_kills') or 0) >= 2 or int(p.get('dupe_kills') or 0) >= 2, 150),
        ('groundhog', 'Groundhog Day',
         'Let the same mess come back five times. Something upstream is misbehaving.',
         int(p.get('orphan_breaks') or 0) + int(p.get('dupe_breaks') or 0) >= 5, 150),
        ('first_shovel', 'First Shovelful', 'Work through a single not-imported item.',
         int(p.get('shoveled') or 0) >= 1, 50),
        ('mountain', 'The Mountain Wins Anyway',
         'Shovel 1,000 not-imported items. It will refill. It always refills.',
         int(p.get('shoveled') or 0) >= 1000, 300),
    ]
    # Latched: a feat earned once is earned forever. Most of these read current
    # state, so without this a zombie rising would quietly claw back the points
    # its own kill awarded.
    latched = set(p.get('feats_earned') or [])
    return [
        {'id': fid, 'label': label, 'desc': desc,
         'earned': bool(earned) or fid in latched, 'points': pts}
        for fid, label, desc, earned, pts in defs
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
    feats   = _feats(det, runs, cross, best_score, progress)

    points = (
        sum(s['points'] for s in setup_done)
        + sum(l['points'] for l in ladders)
        + sum(f['points'] for f in feats if f['earned'])
    )

    tiers_earned = sum(l['tier'] for l in ladders) + sum(1 for f in feats if f['earned'])
    tiers_total  = sum(l['tiers_total'] for l in ladders) + len(feats)

    rows = _workflow_rows(cfg, det, source_ok, arr_ok) if has_audit else []
    by_id = {l['id']: l for l in ladders}
    for r in rows:
        r['headline'] = _headline(r)
        r['reward']   = _reward_line(r, progress, det)
        # The carrot: which prizes this specific workflow moves, and the nearest
        # one still locked. Abstract shelves do not motivate; "clear these and
        # you hit Shoveler V" does.
        owned = [by_id[lid] for lid, owners in LADDER_OWNER.items()
                 if r['id'] in owners and lid in by_id]
        owned.sort(key=lambda l: (l['maxed'], -l['pct']))
        r['prizes'] = owned
        nxt = next((l for l in owned if not l['maxed']), None)
        r['next_prize'] = None if not nxt else {
            'ladder': nxt['name'], 'label': nxt['next_label'],
            'at': nxt['next_at_label'], 'pct': nxt['pct'],
            'value_label': nxt['value_label'],
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
        'enabled': True,
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
        'feats': feats,
        'prizes': {'earned': tiers_earned, 'total': tiers_total},
        'streak_90': _days_at_or_above(runs, 90),
        'audits': len(ok_runs),
    }
