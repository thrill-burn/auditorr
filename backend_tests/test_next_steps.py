"""Next steps page — workflow ordering, states, and the (useless) prize ladders."""

import os
from datetime import datetime, timedelta

import next_steps
from audit import count_pile_resolved, file_signatures, SIG_IMPORTED, _library_shape


GB = 1024 ** 3
TB = 1024 ** 4


def _cfg(**over):
    # Paths point at real directories: the setup tier verifies they resolve,
    # not merely that the config keys are non-empty.
    here = os.path.dirname(os.path.abspath(__file__))
    base = {
        'TORRENT_SOURCE': 'qbit', 'QB_HOST': 'http://qb',
        'MEDIA_PATH': here, 'LOCAL_PATH': here,
        'SONARR_URL': 'http://sonarr', 'SONARR_API_KEY': 'k',
    }
    base.update(over)
    return base


def _details(**over):
    base = {
        'total_media_size': 10 * TB, 'hardlinked_media_size': 10 * TB,
        'total_torrents_size': 10 * TB,
        'orphaned_torrent_size': 0, 'not_imported_size': 0, 'duplicate_size': 0,
        'orphaned_torrent_count': 0, 'not_imported_count': 0,
        'dead_seed_count': 0, 'duplicate_count': 0,
        'media_file_count': 1000, 'torrent_file_count': 1000,
        'or_limit': 100 * GB, 'ni_limit': 100 * GB, 'dup_limit': 100 * GB,
        'hl_score': 70.0, 'or_score': 10.0, 'ni_score': 10.0, 'dup_score': 10.0,
        'hl_max': 70, 'or_max': 10, 'ni_max': 10, 'dup_max': 10,
    }
    base.update(over)
    return base


def _results(det, **over):
    res = {'dashboard': {'score': 90.0, 'status': 'Great', 'current': {'details': det}}}
    res.update(over)
    return res


def _runs(n=5, score=90.0, **over):
    run = {'ran_at': '2026-08-06T10:00:00', 'status': 'ok', 'health_score': score,
           'trigger': 'manual', 'duration_seconds': 300}
    run.update(over)
    return [dict(run) for _ in range(n)]


def _row(state, wf_id):
    return next(r for r in state['rows'] if r['id'] == wf_id)


# ── The spine ────────────────────────────────────────────────────────────────

def test_clean_library_never_bottoms_out():
    """Every workflow still gets a row, and none of them read as a problem."""
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    assert len(st['rows']) == 5
    assert {r['state'] for r in st['rows']} <= {'maintain', 'standby'}
    assert st['hero'] is not None


def test_orphans_over_threshold_become_fix():
    det = _details(orphaned_torrent_count=12, orphaned_torrent_size=500 * GB, or_score=4.5)
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    assert _row(st, 'cleanup')['state'] == 'fix'
    assert st['hero'] == 'cleanup'


def test_under_threshold_is_optimize_not_fix():
    """Below the configured ratio it is a nice-to-have, not a problem."""
    det = _details(orphaned_torrent_count=2, orphaned_torrent_size=1 * GB, or_score=9.9)
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    assert _row(st, 'cleanup')['state'] == 'optimize'


def test_baseline_work_outranks_ongoing_work():
    """Backfill carries 70 of 100 points; scored ordering would always pick it.

    Hardlinked media is weighted high because it is *hard*, not because it is
    an early priority. Orphans and duplicates are the baseline work that must
    be cleared first.
    """
    det = _details(
        orphaned_torrent_count=12, orphaned_torrent_size=500 * GB, or_score=4.5,
        hardlinked_media_size=int(9.7 * TB), hl_score=68.0,
    )
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    assert st['hero'] == 'cleanup'


def test_even_a_catastrophic_hardlink_gap_waits_for_the_baseline():
    """The stage gate is structural — no magnitude jumps the queue."""
    det = _details(
        orphaned_torrent_count=1, orphaned_torrent_size=1 * GB, or_score=9.7,
        hardlinked_media_size=1 * TB, hl_score=7.0,
    )
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    assert _row(st, 'backfill')['state'] == 'fix'
    assert st['hero'] == 'cleanup'


def test_backfill_leads_once_the_baseline_is_clear():
    det = _details(hardlinked_media_size=4 * TB, hl_score=28.0)
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    assert st['baseline_clear']
    assert st['hero'] == 'backfill'


def test_both_baseline_rows_precede_all_ongoing_work():
    """The stage gate is what must not be jumped."""
    det = _details(
        duplicate_count=8, duplicate_size=400 * GB, dup_score=3.0,
        orphaned_torrent_count=12, orphaned_torrent_size=500 * GB, or_score=4.5,
        hardlinked_media_size=4 * TB, hl_score=28.0,
    )
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    order = [r['id'] for r in st['rows']]
    assert set(order[:2]) == {'cleanup', 'dedupe'}
    assert order.index('backfill') > max(order.index('cleanup'), order.index('dedupe'))


def test_sequence_inside_a_stage_is_fixed_not_impact_ranked():
    """A teaching page teaches the same order every time. A much larger dedupe
    problem must not reshuffle the list ahead of a smaller orphan problem."""
    det = _details(
        duplicate_count=8, duplicate_size=400 * GB, dup_score=3.0,   # 7.0 lost
        orphaned_torrent_count=2, orphaned_torrent_size=200 * GB, or_score=9.0,  # 1.0 lost
    )
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    assert st['hero'] == 'cleanup'


def test_triage_is_ongoing_and_ranks_ahead_of_backfill():
    """Not-imported is recurring maintenance; hardlinking is aspirational."""
    det = _details(
        not_imported_count=6, not_imported_size=200 * GB, ni_score=8.0,
        hardlinked_media_size=4 * TB, hl_score=28.0,
    )
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    order = [r['id'] for r in st['rows']]
    assert order.index('triage') < order.index('backfill')


def test_each_row_states_whether_the_job_ever_ends():
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    natures = {r['id']: r['nature'] for r in st['rows']}
    assert natures['cleanup'] == natures['dedupe'] == 'Clear once, then watch'
    assert natures['triage'] == 'Keeps coming back'
    assert natures['backfill'] == 'Never really finishes'


def test_stage_labels_are_present_for_grouping():
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    stages = {r['id']: r['stage'] for r in st['rows']}
    assert stages['cleanup'] == stages['dedupe'] == 'baseline'
    assert stages['triage'] == stages['backfill'] == 'ongoing'
    assert stages['trumped'] == 'ondemand'


def test_blocked_never_outranks_an_actionable_fix():
    """A user who simply doesn't run an arr must not be nagged forever."""
    det = _details(orphaned_torrent_count=12, orphaned_torrent_size=500 * GB, or_score=4.5)
    cfg = _cfg(SONARR_URL='', SONARR_API_KEY='')
    st = next_steps.build_state(cfg, _results(det), _runs())
    assert _row(st, 'backfill')['state'] == 'blocked'
    assert st['hero'] == 'cleanup'


def test_dead_seeds_alone_raise_triage_off_maintain():
    det = _details(dead_seed_count=3)
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    assert _row(st, 'triage')['state'] == 'optimize'


def test_trumped_is_never_prioritized():
    """It is PM-driven — no audit signal should ever promote it."""
    det = _details(orphaned_torrent_count=50, orphaned_torrent_size=2 * TB, or_score=0.0)
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    assert _row(st, 'trumped')['state'] == 'standby'
    assert st['rows'][-1]['id'] == 'trumped'


def test_no_audit_yet_is_setup_stage():
    st = next_steps.build_state(_cfg(), {}, [])
    assert st['stage'] == 'setup'
    assert st['rows'] == []
    assert st['hero'] is None


def test_every_row_has_teaching_copy_and_a_summary():
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    for r in st['rows']:
        assert r['teaching'] and len(r['teaching']) > 40
        assert r['summary']


def test_only_rows_with_something_to_count_carry_a_stat_line():
    """The stat is the mono readout at the foot of the card. A cleared or
    blocked row has no figure worth one, and a fabricated "0 orphans" would
    hand the calmest cards the exact signal the busy ones use for work."""
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    for r in st['rows']:
        if r['state'] in ('blocked', 'maintain', 'standby'):
            assert r['stat'] == ''
        else:
            assert r['stat']


def test_backfill_says_it_all_on_one_line():
    """Backfill's foot is a single sans payout line, matching Triage and
    Trumped beside it. It carries the ratio and the idle bytes, and the mono
    stat slot stays empty — filling both is what made this card three lines
    tall next to their one."""
    det = _details(total_media_size=100 * TB, hardlinked_media_size=85 * TB)
    st  = next_steps.build_state(_cfg(), _results(det), _runs())
    bf  = _row(st, 'backfill')
    assert bf['state'] == 'fix', 'the busiest state, where the foot was widest'
    assert bf['stat'] == ''
    head = bf['reward']['headline']
    assert '85' in head and 'hardlinked' in head and 'TB' in head
    assert bf['reward']['detail'] == '', 'a second sentence is the third line'


def test_backfill_never_carries_a_stat_in_any_state():
    for det in (_details(total_media_size=100 * TB, hardlinked_media_size=85 * TB),
                _details(total_media_size=100 * TB, hardlinked_media_size=99 * TB),
                _details()):
        st = next_steps.build_state(_cfg(), _results(det), _runs())
        assert _row(st, 'backfill')['stat'] == ''


# ── Setup tier ───────────────────────────────────────────────────────────────

def test_setup_incomplete_without_source():
    st = next_steps.build_state(_cfg(QB_HOST=''), {}, [])
    assert not st['setup']['complete']
    assert not next(s for s in st['setup']['steps'] if s['id'] == 'source')['done']


def test_setup_completes_with_either_arr():
    cfg = _cfg(SONARR_URL='', SONARR_API_KEY='', RADARR_URL='http://r', RADARR_API_KEY='k')
    st = next_steps.build_state(cfg, _results(_details()), _runs())
    assert st['setup']['complete']


def test_arr_connections_list_counts_as_configured():
    cfg = _cfg(SONARR_URL='', SONARR_API_KEY='',
               ARR_CONNECTIONS=[{'service': 'sonarr', 'url': 'http://s', 'api_key': 'k'}])
    st = next_steps.build_state(cfg, _results(_details()), _runs())
    assert next(s for s in st['setup']['steps'] if s['id'] == 'sonarr')['done']
    assert _row(st, 'backfill')['state'] != 'blocked'


# ── Useless prizes ───────────────────────────────────────────────────────────

def test_ladders_are_dense():
    """Progress Quest rules: lots of tiers, so the next one is always close."""
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    assert len(st['ladders']) >= 10
    assert st['prizes']['total'] >= 100


def test_every_ladder_reports_where_you_are_on_it():
    """A band name alone ("Pack Rat") says nothing about how far up you are."""
    st = next_steps.build_state(_cfg(), _results(_details(total_media_size=15 * TB)), _runs())
    for l in st['ladders']:
        assert l['tiers_total'] == len(l['tiers'])
        assert 0 <= l['tier'] <= l['tiers_total']
        if l['maxed']:
            assert l['next_n'] is None and l['tier'] == l['tiers_total']
        else:
            assert l['next_n'] == l['tier'] + 1
            assert 1 <= l['next_n'] <= l['tiers_total']


def test_every_ladder_rung_is_individually_named():
    """Roman-numeral fallback is a safety net, not a shipping state."""
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    for l in st['ladders']:
        titles = next_steps.TIER_TITLES.get(l['id']) or []
        assert len(titles) >= l['tiers_total'], f"{l['id']} runs out of names"


def test_every_ladder_says_what_its_number_counts():
    """Eight ladders render bytes. "12.4 TB" alone names none of them."""
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    known = {gid for gid, _, _ in next_steps.LADDER_GROUPS}
    for l in st['ladders']:
        assert l['id'] in next_steps.LADDER_FACET, f"{l['id']} has no facet"
        assert l['measures'], f"{l['id']} does not say what it measures"
        assert l['group'] in known
    for g in st['ladder_groups']:
        assert g['total'] > 0, f"{g['id']} is an empty shelf"
        assert g['earned'] == sum(l['tier'] for l in st['ladders'] if l['group'] == g['id'])
        assert g['total'] == sum(l['tiers_total'] for l in st['ladders'] if l['group'] == g['id'])
    # Every ladder lands on exactly one shelf — none orphaned, none duplicated.
    assert sum(g['total'] for g in st['ladder_groups']) == st['prizes']['total'] - len(st['feats'])


def test_next_prize_names_its_ladder_and_its_rung():
    det = _details(not_imported_count=40, not_imported_size=500 * GB, ni_score=3.0)
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    prize = _row(st, 'triage')['next_prize']
    assert prize['ladder'] and prize['ladder_id']
    assert prize['n'] and prize['of'] and prize['n'] <= prize['of']


# ── Feats ────────────────────────────────────────────────────────────────────

def test_every_feat_belongs_to_a_declared_group():
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    known = {gid for gid, _, _ in next_steps.FEAT_GROUPS}
    assert {f['group'] for f in st['feats']} <= known
    assert len({f['id'] for f in st['feats']}) == len(st['feats']), 'duplicate feat id'
    for g in st['feat_groups']:
        assert g['total'] > 0, f"{g['id']} has no feats"
        assert g['earned'] == sum(
            1 for f in st['feats'] if f['group'] == g['id'] and f['earned'])


def test_a_small_library_still_has_something_to_earn_on_every_axis():
    """Feats are free to give, and the cheap ones do the work.

    A 200 GB library with one tracker is the ordinary case. If the size and
    upload axes only start rewarding at 100 TB, this whole layer reads as
    written for somebody else.
    """
    det = _details(
        total_media_size=200 * GB, hardlinked_media_size=180 * GB,
        total_torrents_size=190 * GB,
        media_file_count=300, torrent_file_count=300,
    )
    res = _results(det, tracker_file_stats={'one.cc': {'seeding_size': 150 * GB,
                                                       'seeding_count': 90}})
    st = next_steps.build_state(_cfg(), res, _runs(3), lifetime_uploaded=20 * GB)
    by_group = {g['id']: g for g in st['feat_groups']}
    for gid in ('start', 'clean', 'scale', 'give'):
        assert by_group[gid]['earned'] >= 1, f"nothing reachable in '{gid}'"


def test_ladder_progress_is_between_tiers_not_from_zero():
    st = next_steps.build_state(_cfg(), _results(_details(total_media_size=15 * TB)), _runs())
    hoard = next(l for l in st['ladders'] if l['id'] == 'hoard')
    assert hoard['tier'] > 0
    assert 0 <= hoard['pct'] <= 100
    assert hoard['next_at'] > hoard['value']


def test_unearned_ladder_awards_no_points():
    st = next_steps.build_state(_cfg(QB_HOST=''), {}, [])
    for ladder in st['ladders']:
        if ladder['tier'] == 0:
            assert ladder['points'] == 0


def test_streak_requires_consecutive_days():
    runs = [
        {'ran_at': '2026-08-06T10:00:00', 'status': 'ok', 'health_score': 95.0},
        {'ran_at': '2026-08-05T10:00:00', 'status': 'ok', 'health_score': 40.0},
        {'ran_at': '2026-08-04T10:00:00', 'status': 'ok', 'health_score': 99.0},
    ]
    # The 40.0 day breaks it — the older 99.0 must not count.
    assert next_steps._days_at_or_above(runs, 90) <= 1


def test_failed_runs_are_ignored_everywhere():
    runs = [{'ran_at': '2026-08-06T10:00:00', 'status': 'aborted', 'health_score': None}]
    st = next_steps.build_state(_cfg(), {}, runs)
    assert st['audits'] == 0
    assert st['stage'] == 'setup'


def test_rank_advances_with_points_and_reports_next():
    low  = next_steps.build_state(_cfg(QB_HOST=''), {}, [])
    high = next_steps.build_state(_cfg(), _results(_details(total_media_size=50 * TB)), _runs(200))
    assert high['rank']['points'] > low['rank']['points']
    assert high['rank']['index'] >= low['rank']['index']
    assert low['rank']['next_name']
    assert 0 <= low['rank']['pct'] <= 100


def test_payload_carries_no_file_lists():
    """This endpoint is polled — it must never grow a full file list."""
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    assert 'media_files' not in st and 'torrent_files' not in st


def test_default_paths_do_not_tick_the_box_on_a_fresh_install():
    """Config ships non-empty path defaults — they must not read as configured."""
    cfg = _cfg(MEDIA_PATH='/data/media/definitely-not-here',
               LOCAL_PATH='/data/torrents/definitely-not-here')
    st = next_steps.build_state(cfg, {}, [])
    assert not next(s for s in st['setup']['steps'] if s['id'] == 'paths')['done']


# ── Reward archetypes ────────────────────────────────────────────────────────
# Each workflow pays out in a different shape because the work has a different
# shape: a defended streak, a cumulative pile, a ratcheting best.

def test_reward_kinds_match_the_work():
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    kinds = {r['id']: r['reward_kind'] for r in st['rows']}
    assert kinds['cleanup'] == kinds['dedupe'] == 'zombie'
    assert kinds['triage'] == 'shovel'
    assert kinds['backfill'] == 'crystal'


def test_shovel_count_is_cumulative_and_never_decreases():
    """The pile refills forever; what you already dug is permanent."""
    cfg, p = _cfg(), None
    p = next_steps.update_progress(p, cfg, _details(not_imported_count=100))
    assert p['shoveled'] == 0, 'the first audit has no previous scan to credit against'
    p = next_steps.update_progress(p, cfg, _details(not_imported_count=40), resolved=60)
    assert p['shoveled'] == 60
    p = next_steps.update_progress(p, cfg, _details(not_imported_count=90), resolved=0)
    assert p['shoveled'] == 60, 'a growing pile must not erase past work'
    p = next_steps.update_progress(p, cfg, _details(not_imported_count=10), resolved=80)
    assert p['shoveled'] == 140


def test_shovel_credit_survives_an_exclusion_change():
    """The regression this counter was rebuilt to fix.

    Triage renders its exclusion suggestions beside the delete button, so a
    session that deletes real files *and* clicks a chip is the normal case, not
    the edge case. The count-delta this replaced was gated on an exclusion
    fingerprint and voided the entire interval whenever the set moved — a field
    case cleared five real video files and was paid nothing because a `*.sfv`
    pattern was added in the same minute. Credit is now counted per file
    upstream, where hiding something simply produces no transition to count.
    """
    cfg = _cfg()
    p = next_steps.update_progress(None, cfg, _details(not_imported_count=100))
    hidden = _cfg(EXCLUSION_PATTERNS=['*.sfv'])
    p = next_steps.update_progress(p, hidden, _details(not_imported_count=5), resolved=5)
    assert p['shoveled'] == 5, 'deleted files must pay out even if a pattern was added too'


# ── Shovel credit — what counts as digging (audit.count_pile_resolved) ───────
# The counter reads per-file transitions off the signature map rather than a
# drop in not_imported_count, because a count knows only that the pile shrank,
# never why.

def _tf(path, imported=False, status='Seeding', excluded=False, **over):
    rec = {'path': path, 'size': GB, 'imported': imported,
           'status': status, 'excluded': excluded, 'duplicate_paths': []}
    rec.update(over)
    return rec


def test_a_real_clearout_credits_only_what_was_on_the_pile():
    """Field case, 2026-08-15: one Triage session removed eight torrent files.

    Three were excluded sidecars — a .sfv, a .nfo (via the media-server presets)
    and a Sample/ clip — which never counted toward not_imported_count in the
    first place. Five is the honest number.
    """
    before = [
        _tf('The Lion King (1994) HONE.mkv'),
        _tf('radarr/Obsession.2025.mkv'),
        _tf('radarr/Spirited.Away.2001.mkv'),
        _tf('radarr/Michael.2026.mkv'),
        _tf('tv-sonarr/Furious.S01E05/ep.mkv'),
        _tf('tv-sonarr/Furious.S01E05/ep.sfv',       excluded=True),
        _tf('tv-sonarr/Furious.S01E05/ep.nfo',       excluded=True),
        _tf('tv-sonarr/Furious.S01E05/Sample/s.mkv', excluded=True),
        _tf('radarr/Untouched.2024.mkv'),
    ]
    survivor = [_tf('radarr/Untouched.2024.mkv')]
    assert count_pile_resolved(file_signatures(before), survivor) == 5


def test_an_import_clears_a_pile_item_too():
    """The other honest exit from the pile: an arr picked it up."""
    before = [_tf('radarr/Dont.Say.Good.Luck.2026.mkv')]
    after  = [_tf('radarr/Dont.Say.Good.Luck.2026.mkv', imported=True)]
    assert count_pile_resolved(file_signatures(before), after) == 1


def test_hiding_or_orphaning_a_pile_item_is_not_digging():
    """Both leave not_imported_count, and both fooled the old count-delta.

    An excluded item was hidden, not resolved. An orphaned one lost its torrent
    but kept its bytes — that is Cleanup's work, and it pays out on the orphan
    zombie ladder instead.
    """
    before = [_tf('radarr/A.mkv'), _tf('radarr/B.mkv')]
    after  = [_tf('radarr/A.mkv', excluded=True),
              _tf('radarr/B.mkv', status='Orphaned')]
    assert count_pile_resolved(file_signatures(before), after) == 0


def test_deleting_an_imported_file_is_not_digging():
    """It was never on the pile — and it just cost the library a hardlink."""
    before = [_tf('radarr/Splitsville.2025.mkv', imported=True)]
    assert count_pile_resolved(file_signatures(before), []) == 0


def test_credit_is_not_capped_like_the_change_log():
    """compute_diff caps its lists at 50 entries; a real clearout can exceed it."""
    before = [_tf(f'radarr/{i}.mkv') for i in range(200)]
    assert count_pile_resolved(file_signatures(before), []) == 200


def test_first_scan_credits_nothing():
    assert count_pile_resolved({}, [_tf('radarr/A.mkv')]) == 0


# ── …and the rest of the pile: dead seeds and dead registrations ─────────────
# The pile is everything Triage lists, not just the not-imported files. Both of
# these are invisible to a "not imported" test: a dead seed IS imported, and a
# dead registration is not a file record at all.

def _seed(path, **over):
    """An imported, tracker-dead torrent — a dead seed."""
    return _tf(path, imported=True, **{'tracker_health': 'unregistered', **over})


def _carrier(path, dead_hashes, **over):
    """A healthy imported file carrying dead cross-seed registrations."""
    return _tf(path, imported=True, **{
        'tracker_health': 'working',
        'dead_siblings': [{'hash': h, 'instance_id': 1} for h in dead_hashes],
        **over})


def test_removing_a_dead_seed_is_digging():
    """Imported, so SIG_IMPORTED is set — only SIG_ON_PILE can see this one."""
    before = [_seed('radarr/Dead.2020.mkv')]
    assert count_pile_resolved(file_signatures(before), []) == 1


def test_a_dead_seed_that_re_registers_leaves_the_pile_too():
    before = [_seed('radarr/Dead.2020.mkv')]
    after  = [_tf('radarr/Dead.2020.mkv', imported=True, tracker_health='working')]
    assert count_pile_resolved(file_signatures(before), after) == 1


def test_retiring_dead_registrations_is_digging():
    """Counted by hash: the carrier file is healthy and never leaves the walk,
    so a path diff sees nothing at all."""
    before = ['H1', 'H2', 'H3']
    after  = [_carrier('radarr/Live.2021.mkv', ['H3'])]
    assert count_pile_resolved({}, after, prev_dead_regs=before) == 2


def test_excluding_a_carrier_does_not_pay_out_its_registrations():
    """Excluding hides them from Triage; hiding is not digging.

    This is why `dead_registration_hashes` ignores the excluded flag while
    `count_triage_items` honours it — otherwise clicking an exclusion
    suggestion chip would read as retiring every registration under it.
    """
    before = ['H1', 'H2']
    after  = [_carrier('radarr/Live.2021.mkv', ['H1', 'H2'], excluded=True)]
    assert count_pile_resolved({}, after, prev_dead_regs=before) == 0


def test_both_halves_of_the_pile_add_up():
    before_files = [_tf('radarr/NotImported.mkv'), _seed('radarr/DeadSeed.mkv'),
                    _carrier('radarr/Live.mkv', ['H1', 'H2'])]
    after_files  = [_carrier('radarr/Live.mkv', [])]
    assert count_pile_resolved(file_signatures(before_files), after_files,
                               prev_dead_regs=['H1', 'H2']) == 4


def test_a_carrier_is_not_double_counted_as_a_file_and_a_hash():
    """The carrier is a healthy imported file — it must never set SIG_ON_PILE,
    or clearing its siblings would pay twice."""
    carrier = _carrier('radarr/Live.mkv', ['H1'])
    assert file_signatures([carrier])['radarr/Live.mkv'] == SIG_IMPORTED
    after = [_carrier('radarr/Live.mkv', [])]
    assert count_pile_resolved(file_signatures([carrier]), after,
                               prev_dead_regs=['H1']) == 1


def test_legacy_signatures_still_credit_not_imported_files():
    """Rows written before SIG_ON_PILE existed carry no bit; an upgrade must
    cost at most that interval's dead seeds, not the whole interval."""
    legacy = {'radarr/A.mkv': 0, 'radarr/B.mkv': SIG_IMPORTED}
    assert count_pile_resolved(legacy, []) == 1


def test_update_progress_round_trips_the_dead_registration_set():
    cfg = _cfg()
    p = next_steps.update_progress(None, cfg, _details(), dead_regs={'H2', 'H1'})
    assert p['last_dead_regs'] == ['H1', 'H2']
    # None must not read as "they all went away" on the next scan.
    p2 = next_steps.update_progress(p, cfg, _details())
    assert p2['last_dead_regs'] == ['H1', 'H2']


def test_crystal_ratchets_on_best_ever():
    """Hardlinking goes up and down; a bad week must not take a tier away."""
    cfg = _cfg()
    p = next_steps.update_progress(None, cfg, _details(
        total_media_size=100 * GB, hardlinked_media_size=90 * GB))
    assert round(p['hl_peak'], 1) == 90.0
    p = next_steps.update_progress(p, cfg, _details(
        total_media_size=100 * GB, hardlinked_media_size=60 * GB))
    assert round(p['hl_peak'], 1) == 90.0, 'peak must not regress'


def test_sentinel_streak_starts_when_clean_and_breaks_loudly():
    cfg = _cfg()
    p = next_steps.update_progress(None, cfg, _details(orphaned_torrent_count=0))
    assert p['orphan_clean_since'] is not None
    assert p['orphan_breaks'] == 0
    p = next_steps.update_progress(p, cfg, _details(orphaned_torrent_count=7))
    assert p['orphan_clean_since'] is None
    assert p['orphan_breaks'] == 1, 'a clean state coming undone is worth telling them'


def test_a_break_never_subtracts_points():
    """Regressions are information, not punishment."""
    cfg = _cfg()
    p = next_steps.update_progress(None, cfg, _details(orphaned_torrent_count=0))
    p = next_steps.update_progress(p, cfg, _details(orphaned_torrent_count=7))
    st = next_steps.build_state(cfg, _results(_details(orphaned_torrent_count=7)),
                                _runs(), progress=p)
    assert st['rank']['points'] > 0
    assert next(f for f in st['feats'] if f['id'] == 'orphans_returned')['earned']


def test_never_clean_shows_no_phantom_kills():
    cfg = _cfg()
    p = next_steps.update_progress(None, cfg, _details(orphaned_torrent_count=5))
    st = next_steps.build_state(cfg, _results(_details(orphaned_torrent_count=5)),
                                _runs(), progress=p)
    assert 'no kills yet' in _row(st, 'cleanup')['reward']['headline']


def test_every_row_explains_how_it_pays_out():
    """A headline always. The second sentence is optional — Backfill's foot is
    deliberately one line, so it ships a headline and nothing under it."""
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    for r in st['rows']:
        assert r['reward']['headline']
        assert 'detail' in r['reward']
        if r['id'] != 'backfill':
            assert r['reward']['detail']


def test_killing_a_zombie_is_repeatable_and_cumulative():
    """They should have stayed dead. Every trip back to zero is a fresh kill."""
    cfg = _cfg()
    p = next_steps.update_progress(None, cfg, _details(orphaned_torrent_count=40))
    p = next_steps.update_progress(p, cfg, _details(orphaned_torrent_count=0))   # kill 1
    assert p['orphan_kills'] == 1 and p['orphan_breaks'] == 0
    p = next_steps.update_progress(p, cfg, _details(orphaned_torrent_count=9))   # risen
    assert p['orphan_kills'] == 1 and p['orphan_breaks'] == 1
    p = next_steps.update_progress(p, cfg, _details(orphaned_torrent_count=0))   # kill 2
    assert p['orphan_kills'] == 2


def test_a_library_clean_from_day_one_killed_nothing():
    cfg = _cfg()
    p = next_steps.update_progress(None, cfg, _details(orphaned_torrent_count=0))
    assert p['orphan_kills'] == 0
    assert p['orphan_clean_since'] is not None


def test_kills_and_breaks_are_tracked_per_zombie():
    cfg = _cfg()
    p = next_steps.update_progress(None, cfg, _details(orphaned_torrent_count=5, duplicate_count=5))
    p = next_steps.update_progress(p, cfg, _details(orphaned_torrent_count=0, duplicate_count=5))
    assert p['orphan_kills'] == 1 and p['dupe_kills'] == 0


def test_points_never_decrease_across_any_sequence_of_audits():
    """The core rule: this page rewards action and never punishes inaction.

    Walks a deliberately hostile history — the library shrinks, zombies rise,
    the hardlink ratio collapses, a tracker disappears, the streak resets — and
    asserts the running total only ever goes up.
    """
    cfg = _cfg()
    # (details, shovel credit earned since the previous entry)
    history = [
        (_details(), 0),                                                   # pristine
        (_details(orphaned_torrent_count=40, duplicate_count=9,
                  not_imported_count=300), 0),                             # everything breaks
        (_details(not_imported_count=20), 280),                            # big shovel + kills
        (_details(total_media_size=1 * TB, hardlinked_media_size=100 * GB,
                  hl_score=7.0, not_imported_count=400,
                  orphaned_torrent_count=88, duplicate_count=30), 0),      # disaster
        (_details(total_media_size=500 * GB,
                  hardlinked_media_size=50 * GB), 0),                      # library shrinks
    ]
    progress, last_points = None, -1
    for i, (det, resolved) in enumerate(history):
        state = next_steps.build_state(cfg, _results(det), _runs(i + 1), progress=progress)
        pts = state['rank']['points']
        assert pts >= last_points, (
            f'points dropped at step {i}: {last_points} -> {pts}')
        last_points = pts
        progress = next_steps.update_progress(progress, cfg, det, state=state,
                                              resolved=resolved)


def test_a_kill_earns_points_and_a_break_never_removes_them():
    cfg = _cfg()
    p = next_steps.update_progress(None, cfg, _details(orphaned_torrent_count=40))
    st = next_steps.build_state(cfg, _results(_details()), _runs(), progress=p)
    p = next_steps.update_progress(p, cfg, _details(orphaned_torrent_count=0), state=st)
    after_kill = next_steps.build_state(
        cfg, _results(_details()), _runs(), progress=p)['rank']['points']

    risen = _details(orphaned_torrent_count=9)
    st2 = next_steps.build_state(cfg, _results(risen), _runs(), progress=p)
    p = next_steps.update_progress(p, cfg, risen, state=st2)
    after_break = next_steps.build_state(
        cfg, _results(risen), _runs(), progress=p)['rank']['points']

    assert after_break >= after_kill, 'a zombie rising must never cost points'
    assert next(f for f in next_steps.build_state(
        cfg, _results(risen), _runs(), progress=p)['feats']
        if f['id'] == 'first_kill')['earned']


def test_ladder_tiers_are_never_demoted():
    """A shrinking library must not take back a Hoarder tier."""
    cfg = _cfg()
    big = _details(total_media_size=20 * TB)
    st = next_steps.build_state(cfg, _results(big), _runs(), progress=None)
    tier_at_peak = next(l for l in st['ladders'] if l['id'] == 'hoard')['tier']
    p = next_steps.update_progress(None, cfg, big, state=st)

    small = _details(total_media_size=1 * GB)
    st2 = next_steps.build_state(cfg, _results(small), _runs(), progress=p)
    assert next(l for l in st2['ladders'] if l['id'] == 'hoard')['tier'] == tier_at_peak


# ── Trumped: the one workflow counted at execute time ────────────────────────
#
# A trump swap deletes one release and grabs its replacement, so the audit that
# follows sees a library in much the same shape as the one before it. There is
# no state change to infer the action from — hence `record_trump`, called from
# the execute endpoint rather than from `run_audit_process`.

def test_record_trump_counts_swaps_groups_and_the_biggest_group():
    p = next_steps.record_trump(None, torrents=4)
    assert p['trumps'] == 1
    assert p['trump_torrents'] == 4
    assert p['trump_max_group'] == 4
    assert p['last_trump_at']

    p = next_steps.record_trump(p, torrents=1)
    assert p['trumps'] == 2
    assert p['trump_torrents'] == 5
    # A later, smaller swap must not lower the latched peak.
    assert p['trump_max_group'] == 4


def test_record_trump_is_pure_and_defaults_to_one_torrent():
    before = next_steps.record_trump(None)
    after = next_steps.record_trump(before)
    assert before['trumps'] == 1 and after['trumps'] == 2, 'must not mutate its input'
    assert after['trump_torrents'] == 2


def test_trump_swaps_climb_the_kingmaker_ladder_and_earn_points():
    cfg = _cfg()
    res = _results(_details())
    base = next_steps.build_state(cfg, res, _runs(), progress=None)
    km = next(l for l in base['ladders'] if l['id'] == 'kingmaker')
    assert km['tier'] == 0 and km['value'] == 0

    p = None
    for _ in range(5):
        p = next_steps.record_trump(p, torrents=1)
    after = next_steps.build_state(cfg, res, _runs(), progress=p)
    km2 = next(l for l in after['ladders'] if l['id'] == 'kingmaker')
    assert km2['tier'] >= 4, km2['tier']
    assert after['rank']['points'] > base['rank']['points']

    earned = {f['id'] for f in after['feats'] if f['earned']}
    assert {'trump_first', 'trump_five'} <= earned
    assert 'trump_entourage' not in earned, 'five single-torrent swaps is not four in one'


def test_bulk_feat_needs_one_big_group_not_an_accumulated_total():
    cfg = _cfg()
    res = _results(_details())
    many_small = None
    for _ in range(6):
        many_small = next_steps.record_trump(many_small, torrents=1)
    st = next_steps.build_state(cfg, res, _runs(), progress=many_small)
    assert not next(f for f in st['feats'] if f['id'] == 'trump_entourage')['earned']

    one_big = next_steps.record_trump(None, torrents=4)
    st2 = next_steps.build_state(cfg, res, _runs(), progress=one_big)
    assert next(f for f in st2['feats'] if f['id'] == 'trump_entourage')['earned']


def test_trumped_row_carries_its_reward_and_next_prize():
    cfg = _cfg()
    res = _results(_details())
    cold = _row(next_steps.build_state(cfg, res, _runs(), progress=None), 'trumped')
    assert cold['reward_kind'] == 'tribute'
    assert 'not asked' in cold['reward']['headline']
    # Even at zero the card names the rung it is working toward.
    assert cold['next_prize']['ladder_id'] == 'kingmaker'

    p = next_steps.record_trump(next_steps.record_trump(None, torrents=3))
    warm = _row(next_steps.build_state(cfg, res, _runs(), progress=p), 'trumped')
    assert '2 paid' in warm['reward']['headline']
    assert '4 registrations' in warm['reward']['headline']


def test_an_audit_never_clears_trump_credit():
    """update_progress rebuilds progress from EMPTY_PROGRESS each run — the
    trump counters must survive that, or every scan would wipe them."""
    cfg = _cfg()
    det = _details()
    p = next_steps.record_trump(None, torrents=3)
    st = next_steps.build_state(cfg, _results(det), _runs(), progress=p)
    p2 = next_steps.update_progress(p, cfg, det, state=st, resolved=0)
    assert p2['trumps'] == 1
    assert p2['trump_torrents'] == 3
    assert p2['trump_max_group'] == 3


# ── The aspirational tier ────────────────────────────────────────────────────
#
# Everything below exists because the prize layer used to go dead for exactly
# the user who had done the work: the zombie ladders need a mess to return
# before they pay again, every "clean library" feat is earned once on the first
# scan, and the `have` shelf was six tiles all reading the library's size.

def _ladder(st, lid):
    return next(l for l in st['ladders'] if l['id'] == lid)


def test_clean_byte_ladders_no_longer_restate_the_torrent_directory():
    """Packrat, Tidiness and Purity used to be one number on three tiles.

    Each subtracted a penalty term that is a couple of percent on any library
    worth the name, against rungs 2-2.5x apart — so all three sat on the same
    rung forever and "No Dust Exists Here" was awarded for owning 10 TB.
    """
    dirty = _details(orphaned_torrent_count=5, orphaned_torrent_size=50 * GB,
                     not_imported_count=5, not_imported_size=50 * GB)
    st = next_steps.build_state(_cfg(), _results(dirty), _runs())
    assert _ladder(st, 'packrat')['tier'] > 0
    assert _ladder(st, 'tidiness')['tier'] == 0
    assert _ladder(st, 'purity')['tier'] == 0

    st2 = next_steps.build_state(_cfg(), _results(_details()), _runs())
    assert _ladder(st2, 'tidiness')['tier'] == _ladder(st2, 'packrat')['tier']


def test_a_spotless_small_library_outranks_a_filthy_large_one():
    """The whole point of the rebase: on the clean shelves, care beats spend."""
    small_clean = _details(total_media_size=2 * TB, hardlinked_media_size=2 * TB,
                           total_torrents_size=2 * TB)
    big_dirty = _details(total_media_size=500 * TB, hardlinked_media_size=500 * TB,
                         total_torrents_size=500 * TB,
                         orphaned_torrent_count=900, orphaned_torrent_size=9 * TB)
    clean = next_steps.build_state(_cfg(), _results(small_clean), _runs())
    filthy = next_steps.build_state(_cfg(), _results(big_dirty), _runs())
    assert _ladder(clean, 'tidiness')['tier'] > _ladder(filthy, 'tidiness')['tier']
    assert _ladder(filthy, 'packrat')['tier'] > _ladder(clean, 'packrat')['tier']


def test_conservator_needs_every_pile_empty():
    st = next_steps.build_state(_cfg(), _results(_details(duplicate_count=1)), _runs())
    assert _ladder(st, 'conservator')['tier'] == 0
    st2 = next_steps.build_state(_cfg(), _results(_details()), _runs())
    assert _ladder(st2, 'conservator')['tier'] > 0


def test_conservator_counts_hardlinked_bytes_once():
    """`total_media_size` and `total_torrents_size` are two walks of two trees
    that hold the same bytes, so a hardlinked file lands in both sums. Adding
    them made a 10 TB library read 20 TB — and the error grew with how well
    hardlinked you were, i.e. worst in the state the ladder rewards."""
    det = _details(total_media_size=10 * TB, hardlinked_media_size=10 * TB,
                   total_torrents_size=10 * TB)
    st  = next_steps.build_state(_cfg(), _results(det), _runs())
    assert _ladder(st, 'conservator')['value'] == 10 * TB
    # Under the gate the torrent tree is a subset of the media tree, so
    # Conservator and Hoarder describe the same bytes. It stays a separate
    # ladder because the *peak* differs: Hoarder latches your largest library,
    # Conservator your largest spotless one.
    assert _ladder(st, 'conservator')['value'] == _ladder(st, 'hoard')['value']
    assert _ladder(st, 'conservator')['tier'] == _ladder(st, 'hoard')['tier']


def test_conservator_only_latches_a_library_that_was_clean_at_the_time():
    """The two peaks are what separate this from Hoarder. A library that grows
    while messy advances Hoarder and leaves Conservator where it was."""
    cfg   = _cfg()
    clean = _details(total_media_size=2 * TB, hardlinked_media_size=2 * TB,
                     total_torrents_size=2 * TB)
    st    = next_steps.build_state(cfg, _results(clean), _runs())
    p     = next_steps.update_progress(None, cfg, clean, state=st)

    grew_dirty = _details(total_media_size=50 * TB, hardlinked_media_size=50 * TB,
                          total_torrents_size=50 * TB, orphaned_torrent_count=9)
    st2 = next_steps.build_state(cfg, _results(grew_dirty), _runs(), progress=p)
    assert _ladder(st2, 'hoard')['tier'] > _ladder(st, 'hoard')['tier']
    assert _ladder(st2, 'conservator')['tier'] == _ladder(st, 'conservator')['tier']


def test_rebased_ladders_keep_grandfathered_rungs():
    """A latched peak is earned. The rebase may stall a ladder, never demote it."""
    cfg = _cfg()
    clean = _details()
    st = next_steps.build_state(cfg, _results(clean), _runs())
    peak_tier = _ladder(st, 'tidiness')['tier']
    p = next_steps.update_progress(None, cfg, clean, state=st)

    dirty = _details(orphaned_torrent_count=500, orphaned_torrent_size=TB)
    st2 = next_steps.build_state(cfg, _results(dirty), _runs(), progress=p)
    assert _ladder(st2, 'tidiness')['tier'] == peak_tier


def test_a_mess_cleared_the_same_day_is_a_fast_fix():
    """Exterminator only pays when a mess *returns*, so a permanently clean
    library can never earn another kill. Fire Brigade pays for the response."""
    cfg = _cfg()
    clean, dirty = _details(), _details(orphaned_torrent_count=3)
    t0 = datetime(2026, 8, 1, 9, 0, 0)

    p = next_steps.update_progress(None, cfg, clean, now=t0)
    p = next_steps.update_progress(p, cfg, dirty, now=t0 + timedelta(hours=1))
    assert p['orphan_break_at'] and p['orphan_breaks'] == 1
    p = next_steps.update_progress(p, cfg, clean, now=t0 + timedelta(hours=5))
    assert p['fast_fixes'] == 1
    assert p['orphan_break_at'] is None, 'the timer must reset with the kill'


def test_a_mess_left_for_a_week_is_not_a_fast_fix():
    cfg = _cfg()
    clean, dirty = _details(), _details(duplicate_count=4)
    t0 = datetime(2026, 8, 1, 9, 0, 0)
    p = next_steps.update_progress(None, cfg, clean, now=t0)
    p = next_steps.update_progress(p, cfg, dirty, now=t0 + timedelta(hours=1))
    p = next_steps.update_progress(p, cfg, clean, now=t0 + timedelta(days=7))
    assert p['dupe_kills'] == 1
    assert p['fast_fixes'] == 0


def test_a_mess_that_predates_auditorr_earns_no_fast_fix():
    """No break was observed, so there is no clock. Guessing one would pay for
    work done before the first scan."""
    cfg = _cfg()
    t0 = datetime(2026, 8, 1, 9, 0, 0)
    p = next_steps.update_progress(None, cfg, _details(orphaned_torrent_count=9), now=t0)
    p = next_steps.update_progress(p, cfg, _details(), now=t0 + timedelta(hours=2))
    assert p['orphan_kills'] == 1, 'still a kill'
    assert p['fast_fixes'] == 0, 'but not a timed one'


def test_immaculate_streak_holds_and_breaks():
    cfg = _cfg()
    t0 = datetime(2026, 8, 1, 9, 0, 0)
    p = next_steps.update_progress(None, cfg, _details(), now=t0)
    started = p['immaculate_since']
    assert started
    p = next_steps.update_progress(p, cfg, _details(), now=t0 + timedelta(days=3))
    assert p['immaculate_since'] == started, 'a held streak must not restart'

    p = next_steps.update_progress(p, cfg, _details(not_imported_count=2),
                                   now=t0 + timedelta(days=4))
    assert p['immaculate_since'] is None

    st = next_steps.build_state(cfg, _results(_details()), _runs(),
                                progress={**p, 'immaculate_since': started})
    assert _ladder(st, 'unblemished')['tier'] > 0


def test_seeding_time_feeds_atlas_and_old_faithful():
    """Both read summary scalars the source layer already had in hand."""
    year = 365.25 * 86400
    det = _details(seed_byte_secs=int(40 * TB * year), max_seed_secs=int(2.5 * year))
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    atlas = _ladder(st, 'atlas')
    assert atlas['tier'] > 0 and 'TB·yr' in atlas['value_label']
    assert _ladder(st, 'oldfaithful')['tier'] >= 7, 'two and a half years of uptime'


def test_patience_beats_capacity_on_the_time_ladders():
    """The one shelf a small library can win: Old Faithful has nothing to do
    with size, so a 2 TB library holds a rung a 500 TB seedbox cannot."""
    year = 365.25 * 86400
    patient = _details(total_media_size=2 * TB, total_torrents_size=2 * TB,
                       max_seed_secs=int(6 * year))
    huge = _details(total_media_size=500 * TB, total_torrents_size=500 * TB,
                    max_seed_secs=int(30 * 86400))
    a = next_steps.build_state(_cfg(), _results(patient), _runs())
    b = next_steps.build_state(_cfg(), _results(huge), _runs())
    assert _ladder(a, 'oldfaithful')['tier'] > _ladder(b, 'oldfaithful')['tier']
    assert _ladder(b, 'hoard')['tier'] > _ladder(a, 'hoard')['tier']


def test_library_shape_counts_titles_not_files():
    """Two seasons of one show are one title; the category dir is never one."""
    media = [
        {'path': 'tv/Some Show/Season 1/ep1.mkv', 'size': 1},
        {'path': 'tv/Some Show/Season 2/ep2.mkv', 'size': 1},
        {'path': 'tv/Other Show/Season 1/ep1.mkv', 'size': 1},
        {'path': 'movies/A Film (2020)/film.mkv', 'size': 1},
    ]
    assert _library_shape(media)['title_count'] == 3


def test_library_shape_totals_uhd_bytes():
    media = [
        {'path': 'movies/A Film (2020) 2160p WEB-DL/film.mkv', 'size': 100},
        {'path': 'movies/B Film (2021) UHD BluRay/film.mkv', 'size': 50},
        {'path': 'movies/C Film (2019) 1080p WEB-DL/film.mkv', 'size': 25},
    ]
    assert _library_shape(media)['uhd_bytes'] == 150


def test_windows_paths_do_not_inflate_the_title_count():
    media = [
        {'path': 'tv\\Some Show\\Season 1\\ep1.mkv', 'size': 1},
        {'path': 'tv\\Some Show\\Season 2\\ep2.mkv', 'size': 1},
    ]
    assert _library_shape(media)['title_count'] == 1


def test_titles_and_uhd_climb_their_own_ladders():
    det = _details(title_count=1200, uhd_bytes=3 * TB)
    st = next_steps.build_state(_cfg(), _results(det), _runs())
    assert _ladder(st, 'librarian')['tier'] >= 7
    assert _ladder(st, 'videophile')['tier'] > 0
    earned = {f['id'] for f in st['feats'] if f['earned']}
    assert {'hundred_titles', 'thousand_titles'} <= earned


def test_nothing_left_to_do_needs_the_whole_spine_clear():
    st = next_steps.build_state(_cfg(), _results(_details()), _runs())
    assert next(f for f in st['feats'] if f['id'] == 'nothing_left')['earned']

    busy = _details(orphaned_torrent_count=99, orphaned_torrent_size=TB, or_score=1.0)
    st2 = next_steps.build_state(_cfg(), _results(busy), _runs())
    assert not next(f for f in st2['feats'] if f['id'] == 'nothing_left')['earned']


def test_the_new_ladders_ratchet_like_the_old_ones():
    """Same hostile-history rule: points accrue, and never come back."""
    cfg = _cfg()
    year = 365.25 * 86400
    good = _details(title_count=5000, uhd_bytes=10 * TB, oldest_media_age_days=2000,
                    seed_byte_secs=int(50 * TB * year), max_seed_secs=int(3 * year))
    st = next_steps.build_state(cfg, _results(good), _runs())
    p = next_steps.update_progress(None, cfg, good, state=st)
    before = st['rank']['points']

    # Everything gets worse at once: the library shrinks, the shelves empty, the
    # oldest file is deleted and half the seeds are dropped.
    ruin = _details(total_media_size=GB, total_torrents_size=GB, title_count=1,
                    uhd_bytes=0, oldest_media_age_days=0, seed_byte_secs=0,
                    max_seed_secs=0, orphaned_torrent_count=50, duplicate_count=9)
    st2 = next_steps.build_state(cfg, _results(ruin), _runs(), progress=p)
    assert st2['rank']['points'] >= before
