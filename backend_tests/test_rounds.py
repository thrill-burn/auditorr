"""Next steps page — workflow ordering, states, and the (useless) prize ladders."""

import os
from datetime import datetime, timedelta

import rounds
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
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    assert len(st['rows']) == 5
    assert {r['state'] for r in st['rows']} <= {'maintain', 'standby'}
    assert st['hero'] is not None


def test_orphans_over_threshold_become_fix():
    det = _details(orphaned_torrent_count=12, orphaned_torrent_size=500 * GB, or_score=4.5)
    st = rounds.build_state(_cfg(), _results(det), _runs())
    assert _row(st, 'cleanup')['state'] == 'fix'
    assert st['hero'] == 'cleanup'


def test_under_threshold_is_optimize_not_fix():
    """Below the configured ratio it is a nice-to-have, not a problem."""
    det = _details(orphaned_torrent_count=2, orphaned_torrent_size=1 * GB, or_score=9.9)
    st = rounds.build_state(_cfg(), _results(det), _runs())
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
    st = rounds.build_state(_cfg(), _results(det), _runs())
    assert st['hero'] == 'cleanup'


def test_even_a_catastrophic_hardlink_gap_waits_for_the_baseline():
    """The stage gate is structural — no magnitude jumps the queue."""
    det = _details(
        orphaned_torrent_count=1, orphaned_torrent_size=1 * GB, or_score=9.7,
        hardlinked_media_size=1 * TB, hl_score=7.0,
    )
    st = rounds.build_state(_cfg(), _results(det), _runs())
    assert _row(st, 'backfill')['state'] == 'fix'
    assert st['hero'] == 'cleanup'


def test_backfill_leads_once_the_baseline_is_clear():
    det = _details(hardlinked_media_size=4 * TB, hl_score=28.0)
    st = rounds.build_state(_cfg(), _results(det), _runs())
    assert st['baseline_clear']
    assert st['hero'] == 'backfill'


def test_both_baseline_rows_precede_all_ongoing_work():
    """The stage gate is what must not be jumped."""
    det = _details(
        duplicate_count=8, duplicate_size=400 * GB, dup_score=3.0,
        orphaned_torrent_count=12, orphaned_torrent_size=500 * GB, or_score=4.5,
        hardlinked_media_size=4 * TB, hl_score=28.0,
    )
    st = rounds.build_state(_cfg(), _results(det), _runs())
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
    st = rounds.build_state(_cfg(), _results(det), _runs())
    assert st['hero'] == 'cleanup'


def test_triage_is_ongoing_and_ranks_ahead_of_backfill():
    """Not-imported is recurring maintenance; hardlinking is aspirational."""
    det = _details(
        not_imported_count=6, not_imported_size=200 * GB, ni_score=8.0,
        hardlinked_media_size=4 * TB, hl_score=28.0,
    )
    st = rounds.build_state(_cfg(), _results(det), _runs())
    order = [r['id'] for r in st['rows']]
    assert order.index('triage') < order.index('backfill')


def test_each_row_states_whether_the_job_ever_ends():
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    natures = {r['id']: r['nature'] for r in st['rows']}
    assert natures['cleanup'] == natures['dedupe'] == 'Clear once, then watch'
    assert natures['triage'] == 'Keeps coming back'
    assert natures['backfill'] == 'Never really finishes'


def test_stage_labels_are_present_for_grouping():
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    stages = {r['id']: r['stage'] for r in st['rows']}
    assert stages['cleanup'] == stages['dedupe'] == 'baseline'
    assert stages['triage'] == stages['backfill'] == 'ongoing'
    assert stages['trumped'] == 'ondemand'


def test_blocked_never_outranks_an_actionable_fix():
    """A user who simply doesn't run an arr must not be nagged forever."""
    det = _details(orphaned_torrent_count=12, orphaned_torrent_size=500 * GB, or_score=4.5)
    cfg = _cfg(SONARR_URL='', SONARR_API_KEY='')
    st = rounds.build_state(cfg, _results(det), _runs())
    assert _row(st, 'backfill')['state'] == 'blocked'
    assert st['hero'] == 'cleanup'


def test_dead_seeds_alone_raise_triage_off_maintain():
    det = _details(dead_seed_count=3)
    st = rounds.build_state(_cfg(), _results(det), _runs())
    assert _row(st, 'triage')['state'] == 'optimize'


def test_trumped_is_never_prioritized():
    """It is PM-driven — no audit signal should ever promote it."""
    det = _details(orphaned_torrent_count=50, orphaned_torrent_size=2 * TB, or_score=0.0)
    st = rounds.build_state(_cfg(), _results(det), _runs())
    assert _row(st, 'trumped')['state'] == 'standby'
    assert st['rows'][-1]['id'] == 'trumped'


def test_no_audit_yet_is_setup_stage():
    st = rounds.build_state(_cfg(), {}, [])
    assert st['stage'] == 'setup'
    assert st['rows'] == []
    assert st['hero'] is None


def test_every_row_has_teaching_copy_and_a_summary():
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    for r in st['rows']:
        assert r['teaching'] and len(r['teaching']) > 40
        assert r['summary']


def test_only_rows_with_something_to_count_carry_a_stat_line():
    """The stat is the mono readout at the foot of the card. A cleared or
    blocked row has no figure worth one, and a fabricated "0 orphans" would
    hand the calmest cards the exact signal the busy ones use for work."""
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
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
    st  = rounds.build_state(_cfg(), _results(det), _runs())
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
        st = rounds.build_state(_cfg(), _results(det), _runs())
        assert _row(st, 'backfill')['stat'] == ''


# ── Setup tier ───────────────────────────────────────────────────────────────

def test_setup_incomplete_without_source():
    st = rounds.build_state(_cfg(QB_HOST=''), {}, [])
    assert not st['setup']['complete']
    assert not next(s for s in st['setup']['steps'] if s['id'] == 'source')['done']


def test_setup_completes_with_either_arr():
    cfg = _cfg(SONARR_URL='', SONARR_API_KEY='', RADARR_URL='http://r', RADARR_API_KEY='k')
    st = rounds.build_state(cfg, _results(_details()), _runs())
    assert st['setup']['complete']


def test_arr_connections_list_counts_as_configured():
    """The key the Config page actually writes is `base_url` (#22).

    This page used to read `conn['url']` — a legacy alias no save has produced
    for a long time — so an install whose arrs live only in the "Additional
    instances" list showed Backfill blocked against two working instances and
    never ticked its Sonarr/Radarr setup steps. Both spellings are asserted:
    the alias is still honoured for configs written before the UI settled.
    """
    for url_key in ('base_url', 'url'):
        cfg = _cfg(SONARR_URL='', SONARR_API_KEY='',
                   ARR_CONNECTIONS=[{'id': 'sonarr-uhd', 'service': 'sonarr',
                                     url_key: 'http://s', 'api_key': 'k'}])
        st = rounds.build_state(cfg, _results(_details()), _runs())
        assert next(s for s in st['setup']['steps'] if s['id'] == 'sonarr')['done'], url_key
        assert _row(st, 'backfill')['state'] != 'blocked', url_key


def test_an_arr_connection_without_a_key_is_not_a_connection():
    """Same rule normalize_arr_connections applies — a URL alone connects to
    nothing, and Backfill would be blocked in fact whatever this page claimed."""
    cfg = _cfg(SONARR_URL='', SONARR_API_KEY='',
               ARR_CONNECTIONS=[{'id': 'sonarr-uhd', 'service': 'sonarr',
                                 'base_url': 'http://s'}])
    st = rounds.build_state(cfg, _results(_details()), _runs())
    assert not next(s for s in st['setup']['steps'] if s['id'] == 'sonarr')['done']
    assert _row(st, 'backfill')['state'] == 'blocked'


def test_a_broken_connection_list_does_not_take_the_page_down():
    """Duplicate ids are a real config error and normalize_arr_connections
    raises on them. This endpoint is polled — degrade to "no arr", never 500."""
    cfg = _cfg(SONARR_URL='', SONARR_API_KEY='', ARR_CONNECTIONS=[
        {'id': 'dup', 'service': 'sonarr', 'base_url': 'http://a', 'api_key': 'k'},
        {'id': 'dup', 'service': 'sonarr', 'base_url': 'http://b', 'api_key': 'k'},
    ])
    st = rounds.build_state(cfg, _results(_details()), _runs())
    assert _row(st, 'backfill')['state'] == 'blocked'


# ── Useless prizes ───────────────────────────────────────────────────────────

def test_ladders_are_dense():
    """Progress Quest rules: lots of tiers, so the next one is always close."""
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    assert len(st['ladders']) >= 10
    assert st['prizes']['total'] >= 100


def test_every_ladder_reports_where_you_are_on_it():
    """A band name alone ("Pack Rat") says nothing about how far up you are."""
    st = rounds.build_state(_cfg(), _results(_details(total_media_size=15 * TB)), _runs())
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
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    for l in st['ladders']:
        titles = rounds.TIER_TITLES.get(l['id']) or []
        assert len(titles) >= l['tiers_total'], f"{l['id']} runs out of names"


def test_every_ladder_says_what_its_number_counts():
    """Eight ladders render bytes. "12.4 TB" alone names none of them."""
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    known = {gid for gid, _, _ in rounds.LADDER_GROUPS}
    for l in st['ladders']:
        assert l['id'] in rounds.LADDER_FACET, f"{l['id']} has no facet"
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
    st = rounds.build_state(_cfg(), _results(det), _runs())
    prize = _row(st, 'triage')['next_prize']
    assert prize['ladder'] and prize['ladder_id']
    assert prize['n'] and prize['of'] and prize['n'] <= prize['of']


# ── Feats ────────────────────────────────────────────────────────────────────

def test_every_feat_belongs_to_a_declared_group():
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    known = {gid for gid, _, _ in rounds.FEAT_GROUPS}
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
    st = rounds.build_state(_cfg(), res, _runs(3), lifetime_uploaded=20 * GB)
    by_group = {g['id']: g for g in st['feat_groups']}
    for gid in ('start', 'clean', 'scale', 'give'):
        assert by_group[gid]['earned'] >= 1, f"nothing reachable in '{gid}'"


def test_ladder_progress_is_between_tiers_not_from_zero():
    st = rounds.build_state(_cfg(), _results(_details(total_media_size=15 * TB)), _runs())
    hoard = next(l for l in st['ladders'] if l['id'] == 'hoard')
    assert hoard['tier'] > 0
    assert 0 <= hoard['pct'] <= 100
    assert hoard['next_at'] > hoard['value']


def test_unearned_ladder_awards_no_points():
    st = rounds.build_state(_cfg(QB_HOST=''), {}, [])
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
    assert rounds._days_at_or_above(runs, 90) <= 1


def test_failed_runs_are_ignored_everywhere():
    runs = [{'ran_at': '2026-08-06T10:00:00', 'status': 'aborted', 'health_score': None}]
    st = rounds.build_state(_cfg(), {}, runs)
    assert st['audits'] == 0
    assert st['stage'] == 'setup'


def test_rank_advances_with_points_and_reports_next():
    low  = rounds.build_state(_cfg(QB_HOST=''), {}, [])
    high = rounds.build_state(_cfg(), _results(_details(total_media_size=50 * TB)), _runs(200))
    assert high['rank']['points'] > low['rank']['points']
    assert high['rank']['index'] >= low['rank']['index']
    assert low['rank']['next_name']
    assert 0 <= low['rank']['pct'] <= 100


def test_payload_carries_no_file_lists():
    """This endpoint is polled — it must never grow a full file list."""
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    assert 'media_files' not in st and 'torrent_files' not in st


def test_default_paths_do_not_tick_the_box_on_a_fresh_install():
    """Config ships non-empty path defaults — they must not read as configured."""
    cfg = _cfg(MEDIA_PATH='/data/media/definitely-not-here',
               LOCAL_PATH='/data/torrents/definitely-not-here')
    st = rounds.build_state(cfg, {}, [])
    assert not next(s for s in st['setup']['steps'] if s['id'] == 'paths')['done']


# ── Reward archetypes ────────────────────────────────────────────────────────
# Each workflow pays out in a different shape because the work has a different
# shape: a defended streak, a cumulative pile, a ratcheting best.

def test_reward_kinds_match_the_work():
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    kinds = {r['id']: r['reward_kind'] for r in st['rows']}
    assert kinds['cleanup'] == kinds['dedupe'] == 'zombie'
    assert kinds['triage'] == 'shovel'
    assert kinds['backfill'] == 'crystal'


def test_shovel_count_is_cumulative_and_never_decreases():
    """The pile refills forever; what you already dug is permanent."""
    cfg, p = _cfg(), None
    p = rounds.update_progress(p, cfg, _details(not_imported_count=100))
    assert p['shoveled'] == 0, 'the first audit has no previous scan to credit against'
    p = rounds.update_progress(p, cfg, _details(not_imported_count=40), resolved=60)
    assert p['shoveled'] == 60
    p = rounds.update_progress(p, cfg, _details(not_imported_count=90), resolved=0)
    assert p['shoveled'] == 60, 'a growing pile must not erase past work'
    p = rounds.update_progress(p, cfg, _details(not_imported_count=10), resolved=80)
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
    p = rounds.update_progress(None, cfg, _details(not_imported_count=100))
    hidden = _cfg(EXCLUSION_PATTERNS=['*.sfv'])
    p = rounds.update_progress(p, hidden, _details(not_imported_count=5), resolved=5)
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
    p = rounds.update_progress(None, cfg, _details(), dead_regs={'H2', 'H1'})
    assert p['last_dead_regs'] == ['H1', 'H2']
    # None must not read as "they all went away" on the next scan.
    p2 = rounds.update_progress(p, cfg, _details())
    assert p2['last_dead_regs'] == ['H1', 'H2']


def test_crystal_ratchets_on_best_ever():
    """Hardlinking goes up and down; a bad week must not take a tier away."""
    cfg = _cfg()
    p = rounds.update_progress(None, cfg, _details(
        total_media_size=100 * GB, hardlinked_media_size=90 * GB))
    assert round(p['hl_peak'], 1) == 90.0
    p = rounds.update_progress(p, cfg, _details(
        total_media_size=100 * GB, hardlinked_media_size=60 * GB))
    assert round(p['hl_peak'], 1) == 90.0, 'peak must not regress'


def test_sentinel_streak_starts_when_clean_and_breaks_loudly():
    cfg = _cfg()
    p = rounds.update_progress(None, cfg, _details(orphaned_torrent_count=0))
    assert p['orphan_clean_since'] is not None
    assert p['orphan_breaks'] == 0
    p = rounds.update_progress(p, cfg, _details(orphaned_torrent_count=7))
    assert p['orphan_clean_since'] is None
    assert p['orphan_breaks'] == 1, 'a clean state coming undone is worth telling them'


def test_a_break_never_subtracts_points():
    """Regressions are information, not punishment."""
    cfg = _cfg()
    p = rounds.update_progress(None, cfg, _details(orphaned_torrent_count=0))
    p = rounds.update_progress(p, cfg, _details(orphaned_torrent_count=7))
    st = rounds.build_state(cfg, _results(_details(orphaned_torrent_count=7)),
                                _runs(), progress=p)
    assert st['rank']['points'] > 0
    assert next(f for f in st['feats'] if f['id'] == 'orphans_returned')['earned']


def test_never_clean_leads_with_the_missing_streak_not_a_kill_count():
    """A zombie row's headline is about the state, never the kill tally.

    Both of these rows used to open on the kill count, so a library that had
    simply never let a duplicate through read "no kills yet" — which says the
    thing being measured is how often you have had to fix it. What is being
    measured is the streak you are holding.
    """
    cfg = _cfg()
    dirty = _details(orphaned_torrent_count=5, duplicate_count=5)
    p = rounds.update_progress(None, cfg, dirty)
    st = rounds.build_state(cfg, _results(dirty), _runs(), progress=p)
    for wf in ('cleanup', 'dedupe'):
        head = _row(st, wf)['reward']['headline']
        assert head.startswith('No clean streak yet'), head
        assert 'kill' not in head.lower(), head


def test_every_row_explains_how_it_pays_out():
    """A headline always. The second sentence is optional — Backfill's foot is
    deliberately one line, so it ships a headline and nothing under it."""
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    for r in st['rows']:
        assert r['reward']['headline']
        assert 'detail' in r['reward']
        if r['id'] != 'backfill':
            assert r['reward']['detail']


def test_killing_a_zombie_is_repeatable_and_cumulative():
    """They should have stayed dead. Every trip back to zero is a fresh kill."""
    cfg = _cfg()
    p = rounds.update_progress(None, cfg, _details(orphaned_torrent_count=40))
    p = rounds.update_progress(p, cfg, _details(orphaned_torrent_count=0))   # kill 1
    assert p['orphan_kills'] == 1 and p['orphan_breaks'] == 0
    p = rounds.update_progress(p, cfg, _details(orphaned_torrent_count=9))   # risen
    assert p['orphan_kills'] == 1 and p['orphan_breaks'] == 1
    p = rounds.update_progress(p, cfg, _details(orphaned_torrent_count=0))   # kill 2
    assert p['orphan_kills'] == 2


def test_a_library_clean_from_day_one_killed_nothing():
    cfg = _cfg()
    p = rounds.update_progress(None, cfg, _details(orphaned_torrent_count=0))
    assert p['orphan_kills'] == 0
    assert p['orphan_clean_since'] is not None


def test_kills_and_breaks_are_tracked_per_zombie():
    cfg = _cfg()
    p = rounds.update_progress(None, cfg, _details(orphaned_torrent_count=5, duplicate_count=5))
    p = rounds.update_progress(p, cfg, _details(orphaned_torrent_count=0, duplicate_count=5))
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
        state = rounds.build_state(cfg, _results(det), _runs(i + 1), progress=progress)
        pts = state['rank']['points']
        assert pts >= last_points, (
            f'points dropped at step {i}: {last_points} -> {pts}')
        last_points = pts
        progress = rounds.update_progress(progress, cfg, det, state=state,
                                              resolved=resolved)


def test_a_kill_earns_points_and_a_break_never_removes_them():
    cfg = _cfg()
    p = rounds.update_progress(None, cfg, _details(orphaned_torrent_count=40))
    st = rounds.build_state(cfg, _results(_details()), _runs(), progress=p)
    p = rounds.update_progress(p, cfg, _details(orphaned_torrent_count=0), state=st)
    after_kill = rounds.build_state(
        cfg, _results(_details()), _runs(), progress=p)['rank']['points']

    risen = _details(orphaned_torrent_count=9)
    st2 = rounds.build_state(cfg, _results(risen), _runs(), progress=p)
    p = rounds.update_progress(p, cfg, risen, state=st2)
    after_break = rounds.build_state(
        cfg, _results(risen), _runs(), progress=p)['rank']['points']

    assert after_break >= after_kill, 'a zombie rising must never cost points'
    assert next(f for f in rounds.build_state(
        cfg, _results(risen), _runs(), progress=p)['feats']
        if f['id'] == 'first_kill')['earned']


def test_ladder_tiers_are_never_demoted():
    """A shrinking library must not take back a Hoarder tier."""
    cfg = _cfg()
    big = _details(total_media_size=20 * TB)
    st = rounds.build_state(cfg, _results(big), _runs(), progress=None)
    tier_at_peak = next(l for l in st['ladders'] if l['id'] == 'hoard')['tier']
    p = rounds.update_progress(None, cfg, big, state=st)

    small = _details(total_media_size=1 * GB)
    st2 = rounds.build_state(cfg, _results(small), _runs(), progress=p)
    assert next(l for l in st2['ladders'] if l['id'] == 'hoard')['tier'] == tier_at_peak


# ── Trumped: the one workflow counted at execute time ────────────────────────
#
# A trump swap deletes one release and grabs its replacement, so the audit that
# follows sees a library in much the same shape as the one before it. There is
# no state change to infer the action from — hence `record_trump`, called from
# the execute endpoint rather than from `run_audit_process`.

def test_record_trump_counts_swaps_groups_and_the_biggest_group():
    p = rounds.record_trump(None, torrents=4)
    assert p['trumps'] == 1
    assert p['trump_torrents'] == 4
    assert p['trump_max_group'] == 4
    assert p['last_trump_at']

    p = rounds.record_trump(p, torrents=1)
    assert p['trumps'] == 2
    assert p['trump_torrents'] == 5
    # A later, smaller swap must not lower the latched peak.
    assert p['trump_max_group'] == 4


def test_record_trump_is_pure_and_defaults_to_one_torrent():
    before = rounds.record_trump(None)
    after = rounds.record_trump(before)
    assert before['trumps'] == 1 and after['trumps'] == 2, 'must not mutate its input'
    assert after['trump_torrents'] == 2


def test_trump_swaps_climb_the_kingmaker_ladder_and_earn_points():
    cfg = _cfg()
    res = _results(_details())
    base = rounds.build_state(cfg, res, _runs(), progress=None)
    km = next(l for l in base['ladders'] if l['id'] == 'kingmaker')
    assert km['tier'] == 0 and km['value'] == 0

    p = None
    for _ in range(5):
        p = rounds.record_trump(p, torrents=1)
    after = rounds.build_state(cfg, res, _runs(), progress=p)
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
        many_small = rounds.record_trump(many_small, torrents=1)
    st = rounds.build_state(cfg, res, _runs(), progress=many_small)
    assert not next(f for f in st['feats'] if f['id'] == 'trump_entourage')['earned']

    one_big = rounds.record_trump(None, torrents=4)
    st2 = rounds.build_state(cfg, res, _runs(), progress=one_big)
    assert next(f for f in st2['feats'] if f['id'] == 'trump_entourage')['earned']


def test_trumped_row_carries_its_reward_and_next_prize():
    cfg = _cfg()
    res = _results(_details())
    cold = _row(rounds.build_state(cfg, res, _runs(), progress=None), 'trumped')
    assert cold['reward_kind'] == 'tribute'
    assert 'not asked' in cold['reward']['headline']
    # Even at zero the card names the rung it is working toward.
    assert cold['next_prize']['ladder_id'] == 'kingmaker'

    p = rounds.record_trump(rounds.record_trump(None, torrents=3))
    warm = _row(rounds.build_state(cfg, res, _runs(), progress=p), 'trumped')
    assert '2 paid' in warm['reward']['headline']
    assert '4 registrations' in warm['reward']['headline']


def test_an_audit_never_clears_trump_credit():
    """update_progress rebuilds progress from EMPTY_PROGRESS each run — the
    trump counters must survive that, or every scan would wipe them."""
    cfg = _cfg()
    det = _details()
    p = rounds.record_trump(None, torrents=3)
    st = rounds.build_state(cfg, _results(det), _runs(), progress=p)
    p2 = rounds.update_progress(p, cfg, det, state=st, resolved=0)
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
    st = rounds.build_state(_cfg(), _results(dirty), _runs())
    assert _ladder(st, 'packrat')['tier'] > 0
    assert _ladder(st, 'tidiness')['tier'] == 0
    assert _ladder(st, 'purity')['tier'] == 0

    st2 = rounds.build_state(_cfg(), _results(_details()), _runs())
    assert _ladder(st2, 'tidiness')['tier'] == _ladder(st2, 'packrat')['tier']


def test_a_spotless_small_library_outranks_a_filthy_large_one():
    """The whole point of the rebase: on the clean shelves, care beats spend."""
    small_clean = _details(total_media_size=2 * TB, hardlinked_media_size=2 * TB,
                           total_torrents_size=2 * TB)
    big_dirty = _details(total_media_size=500 * TB, hardlinked_media_size=500 * TB,
                         total_torrents_size=500 * TB,
                         orphaned_torrent_count=900, orphaned_torrent_size=9 * TB)
    clean = rounds.build_state(_cfg(), _results(small_clean), _runs())
    filthy = rounds.build_state(_cfg(), _results(big_dirty), _runs())
    assert _ladder(clean, 'tidiness')['tier'] > _ladder(filthy, 'tidiness')['tier']
    assert _ladder(filthy, 'packrat')['tier'] > _ladder(clean, 'packrat')['tier']


def test_conservator_needs_every_pile_empty():
    st = rounds.build_state(_cfg(), _results(_details(duplicate_count=1)), _runs())
    assert _ladder(st, 'conservator')['tier'] == 0
    st2 = rounds.build_state(_cfg(), _results(_details()), _runs())
    assert _ladder(st2, 'conservator')['tier'] > 0


def test_conservator_counts_hardlinked_bytes_once():
    """`total_media_size` and `total_torrents_size` are two walks of two trees
    that hold the same bytes, so a hardlinked file lands in both sums. Adding
    them made a 10 TB library read 20 TB — and the error grew with how well
    hardlinked you were, i.e. worst in the state the ladder rewards."""
    det = _details(total_media_size=10 * TB, hardlinked_media_size=10 * TB,
                   total_torrents_size=10 * TB)
    st  = rounds.build_state(_cfg(), _results(det), _runs())
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
    st    = rounds.build_state(cfg, _results(clean), _runs())
    p     = rounds.update_progress(None, cfg, clean, state=st)

    grew_dirty = _details(total_media_size=50 * TB, hardlinked_media_size=50 * TB,
                          total_torrents_size=50 * TB, orphaned_torrent_count=9)
    st2 = rounds.build_state(cfg, _results(grew_dirty), _runs(), progress=p)
    assert _ladder(st2, 'hoard')['tier'] > _ladder(st, 'hoard')['tier']
    assert _ladder(st2, 'conservator')['tier'] == _ladder(st, 'conservator')['tier']


def test_rebased_ladders_keep_grandfathered_rungs():
    """A latched peak is earned. The rebase may stall a ladder, never demote it."""
    cfg = _cfg()
    clean = _details()
    st = rounds.build_state(cfg, _results(clean), _runs())
    peak_tier = _ladder(st, 'tidiness')['tier']
    p = rounds.update_progress(None, cfg, clean, state=st)

    dirty = _details(orphaned_torrent_count=500, orphaned_torrent_size=TB)
    st2 = rounds.build_state(cfg, _results(dirty), _runs(), progress=p)
    assert _ladder(st2, 'tidiness')['tier'] == peak_tier


def test_a_mess_cleared_the_same_day_is_a_fast_fix():
    """Exterminator only pays when a mess *returns*, so a permanently clean
    library can never earn another kill. Fire Brigade pays for the response."""
    cfg = _cfg()
    clean, dirty = _details(), _details(orphaned_torrent_count=3)
    t0 = datetime(2026, 8, 1, 9, 0, 0)

    p = rounds.update_progress(None, cfg, clean, now=t0)
    p = rounds.update_progress(p, cfg, dirty, now=t0 + timedelta(hours=1))
    assert p['orphan_break_at'] and p['orphan_breaks'] == 1
    p = rounds.update_progress(p, cfg, clean, now=t0 + timedelta(hours=5))
    assert p['fast_fixes'] == 1
    assert p['orphan_break_at'] is None, 'the timer must reset with the kill'


def test_a_mess_left_for_a_week_is_not_a_fast_fix():
    cfg = _cfg()
    clean, dirty = _details(), _details(duplicate_count=4)
    t0 = datetime(2026, 8, 1, 9, 0, 0)
    p = rounds.update_progress(None, cfg, clean, now=t0)
    p = rounds.update_progress(p, cfg, dirty, now=t0 + timedelta(hours=1))
    p = rounds.update_progress(p, cfg, clean, now=t0 + timedelta(days=7))
    assert p['dupe_kills'] == 1
    assert p['fast_fixes'] == 0


def test_a_mess_that_predates_auditorr_earns_no_fast_fix():
    """No break was observed, so there is no clock. Guessing one would pay for
    work done before the first scan."""
    cfg = _cfg()
    t0 = datetime(2026, 8, 1, 9, 0, 0)
    p = rounds.update_progress(None, cfg, _details(orphaned_torrent_count=9), now=t0)
    p = rounds.update_progress(p, cfg, _details(), now=t0 + timedelta(hours=2))
    assert p['orphan_kills'] == 1, 'still a kill'
    assert p['fast_fixes'] == 0, 'but not a timed one'


def test_immaculate_streak_holds_and_breaks():
    cfg = _cfg()
    t0 = datetime(2026, 8, 1, 9, 0, 0)
    p = rounds.update_progress(None, cfg, _details(), now=t0)
    started = p['immaculate_since']
    assert started
    p = rounds.update_progress(p, cfg, _details(), now=t0 + timedelta(days=3))
    assert p['immaculate_since'] == started, 'a held streak must not restart'

    p = rounds.update_progress(p, cfg, _details(not_imported_count=2),
                                   now=t0 + timedelta(days=4))
    assert p['immaculate_since'] is None

    st = rounds.build_state(cfg, _results(_details()), _runs(),
                                progress={**p, 'immaculate_since': started})
    assert _ladder(st, 'unblemished')['tier'] > 0


def test_seeding_time_feeds_atlas_and_old_faithful():
    """Both read summary scalars the source layer already had in hand."""
    year = 365.25 * 86400
    det = _details(seed_byte_secs=int(40 * TB * year), max_seed_secs=int(2.5 * year))
    st = rounds.build_state(_cfg(), _results(det), _runs())
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
    a = rounds.build_state(_cfg(), _results(patient), _runs())
    b = rounds.build_state(_cfg(), _results(huge), _runs())
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
    st = rounds.build_state(_cfg(), _results(det), _runs())
    assert _ladder(st, 'librarian')['tier'] >= 7
    assert _ladder(st, 'videophile')['tier'] > 0
    earned = {f['id'] for f in st['feats'] if f['earned']}
    assert {'hundred_titles', 'thousand_titles'} <= earned


def test_nothing_left_to_do_needs_the_whole_spine_clear():
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    assert next(f for f in st['feats'] if f['id'] == 'nothing_left')['earned']

    busy = _details(orphaned_torrent_count=99, orphaned_torrent_size=TB, or_score=1.0)
    st2 = rounds.build_state(_cfg(), _results(busy), _runs())
    assert not next(f for f in st2['feats'] if f['id'] == 'nothing_left')['earned']


def test_the_new_ladders_ratchet_like_the_old_ones():
    """Same hostile-history rule: points accrue, and never come back."""
    cfg = _cfg()
    year = 365.25 * 86400
    good = _details(title_count=5000, uhd_bytes=10 * TB, oldest_media_age_days=2000,
                    seed_byte_secs=int(50 * TB * year), max_seed_secs=int(3 * year))
    st = rounds.build_state(cfg, _results(good), _runs())
    p = rounds.update_progress(None, cfg, good, state=st)
    before = st['rank']['points']

    # Everything gets worse at once: the library shrinks, the shelves empty, the
    # oldest file is deleted and half the seeds are dropped.
    ruin = _details(total_media_size=GB, total_torrents_size=GB, title_count=1,
                    uhd_bytes=0, oldest_media_age_days=0, seed_byte_secs=0,
                    max_seed_secs=0, orphaned_torrent_count=50, duplicate_count=9)
    st2 = rounds.build_state(cfg, _results(ruin), _runs(), progress=p)
    assert st2['rank']['points'] >= before


# ── Every workflow prize is effort or time, never bytes ──────────────────────
#
# A size ladder pays you for buying a drive, and it goes still for exactly the
# user who has done the work. Five used to hang off a workflow card: Tidiness
# and Purity (torrent-directory bytes), Conservator (library bytes), and
# Lapidary/Alchemist (ratios that sit at their peak when there is nothing left
# to gain). All five are shelf-only now.

_SIZE_LADDERS = {'hoard', 'packrat', 'benefactor', 'seedbearer', 'vaultkeeper',
                 'pollinator', 'videophile', 'tidiness', 'purity', 'conservator',
                 'lapidary', 'alchemist', 'atlas', 'highwater', 'archivist'}


def test_no_workflow_card_is_paid_in_bytes():
    for lid, owners in rounds.LADDER_OWNER.items():
        assert lid not in _SIZE_LADDERS, f"{lid} is a size/ratio ladder and owns {owners}"


def test_the_retired_ladders_are_still_on_the_shelf():
    """Shelf-only is not a demotion — they still tier, score and latch."""
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    on_shelf = {l['id'] for l in st['ladders']}
    for lid in ('tidiness', 'purity', 'conservator', 'lapidary', 'alchemist'):
        assert lid in on_shelf
        assert _ladder(st, lid)['tiers_total'] > 0


def test_each_workflow_owns_exactly_the_ladders_its_work_has():
    owned = {}
    for lid, owners in rounds.LADDER_OWNER.items():
        for wf in owners:
            owned.setdefault(wf, set()).add(lid)
    # The two zombies each get both halves: the streak, and the times taken back.
    assert {'sentinel', 'exterminator'} <= owned['cleanup']
    assert {'singleton', 'clonehunter'} <= owned['dedupe']
    assert 'shoveler' in owned['triage']
    assert 'matchmaker' in owned['backfill']


def test_the_highlighted_prize_is_pinned_not_nearest():
    """The card's prize box must name the ladder its own payout line talks about.

    "Whichever rung is closest" is right for browsing the shelf and wrong here:
    it hands the highlight to whatever happens to be a percent from tipping
    over, so a Cleanup row clean for a month could point at Fire Brigade while
    the streak it is defending went unmentioned.
    """
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    for wf, lid in rounds.LADDER_PRIMARY.items():
        prize = _row(st, wf)['next_prize']
        assert prize and prize['ladder_id'] == lid, (wf, prize)
        assert prize['n'] and prize['of'] and prize['n'] <= prize['of']


# ── Dedupe's clean streak ────────────────────────────────────────────────────

def test_dedupe_has_a_clean_streak_of_its_own():
    """Dedupe used to own only its kill counter, so a library that had never let
    a duplicate through had nothing on the card and was told "no kills yet"."""
    cfg = _cfg()
    p = rounds.update_progress(None, cfg, _details(duplicate_count=0))
    p['dupe_clean_since'] = (datetime.now() - timedelta(days=40)).isoformat()
    st = rounds.build_state(cfg, _results(_details()), _runs(), progress=p)
    single = _ladder(st, 'singleton')
    assert single['value'] == 40
    assert single['tier'] >= 6, single['tier']
    assert single['group'] == 'work'
    head = _row(st, 'dedupe')['reward']['headline']
    assert head.startswith('Clean for 40 days'), head


def test_the_two_streak_ladders_never_share_a_rung_name():
    """They sit side by side in "Closest to unlocking"."""
    assert not (set(rounds.TIER_TITLES['sentinel'])
                & set(rounds.TIER_TITLES['singleton']))


# ── Backfill pays per file, at the event ─────────────────────────────────────
#
# Counted at the grab rather than from the audit, for the reason
# `record_backfill` documents: the next scan sees a library that got a little
# better hardlinked, which is what an arr upgrading something on its own looks
# like, and the media file is replaced on import so a path-keyed transition
# often cannot see it at all. The grab is the evidence — not the import watch,
# which is an in-memory thread that has to outlive the whole download.

def test_record_backfill_counts_files_releases_and_the_biggest_grab():
    p = rounds.record_backfill(None, files=12)
    assert p['backfilled'] == 12
    assert p['backfill_releases'] == 1
    assert p['backfill_max'] == 12
    assert p['last_backfill_at']

    p = rounds.record_backfill(p, files=1)
    assert p['backfilled'] == 13
    assert p['backfill_releases'] == 2
    assert p['backfill_max'] == 12, 'a later, smaller grab must not lower the peak'


def test_record_backfill_is_pure_and_clamps_what_the_client_sends():
    before = rounds.record_backfill(None)
    after = rounds.record_backfill(before)
    assert before['backfilled'] == 1 and after['backfilled'] == 2, 'must not mutate its input'
    assert rounds.record_backfill(None, files=0)['backfilled'] == 1
    assert rounds.record_backfill(None, files=-5)['backfilled'] == 1
    assert rounds.record_backfill(None, files='nonsense')['backfilled'] == 1
    assert (rounds.record_backfill(None, files=10 ** 9)['backfilled']
            == rounds._BACKFILL_FILES_CAP)


def test_backfilled_files_climb_the_matchmaker_ladder_and_earn_points():
    cfg = _cfg()
    res = _results(_details())
    base = rounds.build_state(cfg, res, _runs(), progress=None)
    assert _ladder(base, 'matchmaker')['tier'] == 0

    p = None
    for _ in range(10):
        p = rounds.record_backfill(p, files=1)
    after = rounds.build_state(cfg, res, _runs(), progress=p)
    assert _ladder(after, 'matchmaker')['tier'] >= 5
    assert after['rank']['points'] > base['rank']['points']

    earned = {f['id'] for f in after['feats'] if f['earned']}
    assert {'first_backfill', 'ten_backfills'} <= earned
    assert 'backfill_pack' not in earned, 'ten single grabs is not one season pack'


def test_the_season_pack_feat_needs_one_big_grab():
    st = rounds.build_state(_cfg(), _results(_details()), _runs(),
                            progress=rounds.record_backfill(None, files=12))
    assert next(f for f in st['feats'] if f['id'] == 'backfill_pack')['earned']


def test_backfill_row_leads_with_the_tally_not_the_ratio():
    cfg = _cfg()
    idle = _details(total_media_size=10 * TB, hardlinked_media_size=9 * TB, hl_score=63.0)
    cold = _row(rounds.build_state(cfg, _results(idle), _runs()), 'backfill')
    assert cold['reward_kind'] == 'crystal'
    assert cold['reward']['headline'].startswith('Nothing backfilled yet')
    assert cold['next_prize']['ladder_id'] == 'matchmaker'

    p = rounds.record_backfill(None, files=37)
    warm = _row(rounds.build_state(cfg, _results(idle), _runs(), progress=p), 'backfill')
    assert warm['reward']['headline'].startswith('37 backfilled so far')
    # The ratio survives as context, after the tally — it is still the figure
    # that says how much is left to do.
    assert '% hardlinked' in warm['reward']['headline']
    assert warm['reward']['detail'] == '', "Backfill's foot is deliberately one line"


def test_an_audit_never_clears_backfill_credit():
    """update_progress rebuilds from EMPTY_PROGRESS each run — an event counter
    must survive that, or every scan would wipe it."""
    cfg, det = _cfg(), _details()
    p = rounds.record_backfill(None, files=6)
    st = rounds.build_state(cfg, _results(det), _runs(), progress=p)
    p2 = rounds.update_progress(p, cfg, det, state=st, resolved=0)
    assert p2['backfilled'] == 6
    assert p2['backfill_releases'] == 1
    assert p2['backfill_max'] == 6


def test_a_credit_that_lands_mid_scan_survives_the_scans_write():
    """The audit reads `ns_progress` at the top of its final phase and writes it
    back when the phase completes. A grab credited in between is in the stored
    row but not in the audit's copy, and a plain write erased it — silently, and
    for good, since these counters are cumulative. Worse, the two are
    correlated: the watchdog scans right after the filesystem change an import
    causes."""
    cfg, det = _cfg(), _details()
    at_scan_start = rounds.record_backfill(None, files=2)
    st = rounds.build_state(cfg, _results(det), _runs(), progress=at_scan_start)

    # Two grabs land while the scan is finishing, plus a trump swap.
    latest = rounds.record_backfill(at_scan_start, files=3)
    latest = rounds.record_trump(latest, torrents=4)

    computed = rounds.update_progress(at_scan_start, cfg, det, state=st, resolved=7)
    assert computed['backfilled'] == 2, 'the audit only ever saw the stale read'

    merged = rounds.merge_event_counters(computed, latest)
    assert merged['backfilled'] == 5
    assert merged['backfill_releases'] == 2
    assert merged['backfill_max'] == 3
    assert merged['trumps'] == 1 and merged['trump_torrents'] == 4
    # Everything the audit *does* own still comes from the audit's own pass.
    assert merged['shoveled'] == computed['shoveled'] == 7


def test_merging_event_counters_never_lowers_one():
    """Whichever side is ahead wins, so the merge is safe in both directions —
    including the ordinary case where nothing landed during the scan."""
    ahead = rounds.record_backfill(rounds.record_backfill(None, files=9), files=1)
    behind = rounds.record_backfill(None, files=1)
    for a, b in ((ahead, behind), (behind, ahead)):
        merged = rounds.merge_event_counters(a, b)
        assert merged['backfilled'] == 10
        assert merged['backfill_max'] == 9
        assert merged['last_backfill_at'] == max(a['last_backfill_at'], b['last_backfill_at'])
    assert rounds.merge_event_counters(behind, None)['backfilled'] == 1
    assert rounds.merge_event_counters(behind, {})['backfilled'] == 1


def test_backfill_credit_survives_a_collapsing_hardlink_ratio():
    """The whole reason it is no longer a ratio: doing the work must pay even
    when the library grows faster than you can backfill it."""
    cfg = _cfg()
    p = None
    for _ in range(25):
        p = rounds.record_backfill(p, files=4)
    great = _details(total_media_size=10 * TB, hardlinked_media_size=10 * TB)
    peak = rounds.build_state(cfg, _results(great), _runs(), progress=p)
    before = peak['rank']['points']
    p = rounds.update_progress(p, cfg, great, state=peak)

    # The library quadruples overnight and almost none of the new material is
    # seeded — the state the old ratio ladder punished you for.
    worse = _details(total_media_size=40 * TB, hardlinked_media_size=10 * TB, hl_score=17.5)
    st = rounds.build_state(cfg, _results(worse), _runs(), progress=p)
    assert st['rank']['points'] >= before
    assert _ladder(st, 'matchmaker')['value'] == 100
    assert _row(st, 'backfill')['reward']['headline'].startswith('100 backfilled so far')


# ── The achievement record ───────────────────────────────────────────────────
#
# `peaks` and `feats_earned` are latches: they say *whether* something was
# earned and nothing about when. So the shelf could show thirty medallions and
# answer nothing about what you actually did, or in what order.

def _hist(p, kind=None):
    return [e for e in (p.get('history') or []) if kind is None or e['kind'] == kind]


def _advance(p, cfg, det, **kw):
    """One audit: build with the previous progress, then latch — exactly what
    `run_audit_process` does."""
    st = rounds.build_state(cfg, _results(det), _runs(), progress=p)
    return rounds.update_progress(p, cfg, det, state=st, **kw)


def test_first_audit_dates_every_rung_and_feat_it_earns():
    cfg, det = _cfg(), _details()
    p = _advance(None, cfg, det)
    rungs, feats = _hist(p, 'rung'), _hist(p, 'feat')
    assert rungs and feats
    # A fresh install has nothing standing before it — no marker, no apology.
    assert not _hist(p, 'prior')
    for e in rungs:
        assert e['at'] and e['id'] and e['n'] >= 1
    # It must agree with the shelf it is a record of.
    st = rounds.build_state(cfg, _results(det), _runs(), progress=p)
    assert len(rungs) == sum(l['tier'] for l in st['ladders'])
    assert len(feats) == sum(1 for f in st['feats'] if f['earned'])


def test_a_rung_is_recorded_once_and_only_when_it_is_crossed():
    """Peaks are monotonic, so re-auditing the same library records nothing."""
    cfg, det = _cfg(), _details(total_media_size=2 * TB)
    p = _advance(None, cfg, det)
    first = len(p['history'])
    assert first > 0

    p = _advance(p, cfg, det)
    assert len(p['history']) == first, 'an unchanged audit must record nothing'

    was = {(e['id'], e['n']) for e in _hist(p, 'rung')}
    p = _advance(p, cfg, _details(total_media_size=60 * TB))
    grew = {(e['id'], e['n']) for e in _hist(p, 'rung')} - was
    assert any(lid == 'hoard' for lid, _ in grew), 'a crossed rung must be dated'
    # Once each: the record holds no duplicate rung.
    rungs = [(e['id'], e['n']) for e in _hist(p, 'rung')]
    assert len(rungs) == len(set(rungs))

    # A library that shrinks back crosses no rung and un-records none. (It can
    # still *earn* something — Featherweight is a feat for a tiny library — which
    # is the monotonic rule working, not a regression.)
    before = {(e['id'], e['n']) for e in _hist(p, 'rung')}
    p = _advance(p, cfg, _details(total_media_size=GB))
    assert {(e['id'], e['n']) for e in _hist(p, 'rung')} == before


def test_the_record_survives_a_regression_like_everything_else_here():
    cfg = _cfg()
    p = _advance(None, cfg, _details(orphaned_torrent_count=0))
    p = _advance(p, cfg, _details(orphaned_torrent_count=40))
    ids = {e['id'] for e in _hist(p, 'feat')}
    assert 'nothing_behind' in ids, 'a feat that was earned stays in the record'


def _log(days, per_day=2, score=92.0, trigger='watchdog'):
    """An audit log spanning `days` days, newest first, like db_get_recent_runs."""
    out = []
    for d in range(days):
        for k in range(per_day):
            out.append({
                'ran_at': (datetime.now() - timedelta(days=d, hours=k)).isoformat(),
                'status': 'ok', 'health_score': score, 'trigger': trigger,
                'duration_seconds': 300, 'peak_rss_mb': 400,
            })
    return out


def test_the_audit_log_dates_what_it_can_prove():
    """Eleven ladders and twenty-seven feats are functions of the run list
    alone. On a long-running install that is most of the record, and it is real
    history rather than a guess."""
    runs = _log(400)
    dated = rounds.history_from_runs(runs)
    assert dated, 'a 400-day log must yield a datable history'

    # Only the whitelisted ids, and every entry stamped from a real run.
    stamps = {str(r['ran_at']) for r in runs}
    for e in dated:
        assert e['at'] in stamps
        if e['kind'] == 'rung':
            assert e['id'] in rounds._RUN_DERIVED_LADDERS
        else:
            assert e['id'] in rounds._RUN_DERIVED_FEATS

    # Oldest first, and each rung and feat recorded once.
    assert [e['at'] for e in dated] == sorted(e['at'] for e in dated)
    keys = [(e['kind'], e['id'], e.get('n')) for e in dated]
    assert len(keys) == len(set(keys))

    # Spread across the history, not stacked on one day — the whole point.
    assert len({e['at'][:10] for e in dated}) > 5

    # Auditor rung 1 lands on the first day; the later rungs cannot.
    first = min(e['at'] for e in dated)
    auditor = sorted((e for e in dated if e['id'] == 'auditor'), key=lambda e: e['n'])
    assert auditor[0]['n'] == 1 and auditor[0]['at'] == first
    assert auditor[-1]['at'] > auditor[0]['at']


def test_dating_never_invents_a_library_it_cannot_see():
    """The replay feeds empty details, so anything reading `det` would evaluate
    against zeros and come out earned. The whitelist is what stops that."""
    dated = rounds.history_from_runs(_log(400))
    ids = {e['id'] for e in dated}
    # Size, count and ratio ladders have no per-day record and must stay out.
    assert not (ids & {'hoard', 'packrat', 'seedbearer', 'vaultkeeper', 'archivist',
                       'librarian', 'shoveler', 'matchmaker', 'sentinel', 'singleton'})
    # `empty_handed` is the trap: true for any log against an empty library.
    assert 'empty_handed' not in ids
    assert 'featherweight' not in ids


def test_dating_an_empty_or_missing_log_is_simply_empty():
    assert rounds.history_from_runs(None) == []
    assert rounds.history_from_runs([]) == []
    assert rounds.history_from_runs([{'status': 'ok'}]) == []


def test_an_upgraded_install_dates_its_history_and_marks_the_rest():
    cfg, det, runs = _cfg(), _details(), _log(400)
    old = _advance(None, cfg, det)
    del old['history']

    st = rounds.build_state(cfg, _results(det), runs, progress=old)
    p = rounds.update_progress(old, cfg, det, state=st, runs=runs)

    dated = [e for e in p['history'] if e['kind'] != 'prior']
    assert len(dated) > 20, 'a 400-day log should date a real chunk of the record'
    assert len({e['at'][:10] for e in dated}) > 5

    # The marker covers only what is genuinely undatable, and does not
    # double-count anything the log just placed.
    prior = _hist(p, 'prior')[0]
    total_rungs = sum(l['tier'] for l in st['ladders'])
    assert prior['rungs'] == total_rungs - sum(1 for e in dated if e['kind'] == 'rung')
    assert prior['rungs'] > 0, 'library-size rungs stay undatable'
    # And it is anchored to the oldest audit, so it sits at the foot of the list.
    assert prior['at'] == min(str(r['ran_at']) for r in runs)
    assert p['history'][0] is prior

    # Points are untouched: dating something changes when, never whether.
    assert (rounds.build_state(cfg, _results(det), runs, progress=p)['rank']['points']
            == st['rank']['points'])


def test_the_payload_sorts_by_date_and_does_not_trust_write_order():
    """The regression that shipped in the first cut of the timeline.

    Insertion order is chronological only while every append is newer than the
    last, and the retroactive dating breaks exactly that: it appends entries
    stamped up to *today* after a marker anchored to the *oldest* run, in the
    same pass that then appends this run's own. Reversing the stored list put a
    July date above a September one.
    """
    cfg, det, runs = _cfg(), _details(), _log(300)
    old = _advance(None, cfg, det)
    del old['history']
    # `now` deliberately behind the newest run, which is what exposed it.
    p = rounds.update_progress(
        old, cfg, det, runs=runs,
        now=datetime.now() - timedelta(days=40),
        state=rounds.build_state(cfg, _results(det), runs, progress=old))

    stored = [e['at'] for e in p['history']]
    assert stored != sorted(stored), 'write order must genuinely be out of order here'

    shown = [e['at'] for e in rounds.build_state(
        cfg, _results(det), runs, progress=p)['history']]
    assert shown == sorted(shown, reverse=True)
    # And the marker still lands at the foot, on its own merits rather than by
    # being inserted first.
    last = rounds.build_state(cfg, _results(det), runs, progress=p)['history'][-1]
    assert last['kind'] == 'prior'


def test_dating_happens_once_and_only_on_the_upgrade():
    cfg, det, runs = _cfg(), _details(), _log(200)
    old = _advance(None, cfg, det)
    del old['history']
    p = rounds.update_progress(
        old, cfg, det, runs=runs,
        state=rounds.build_state(cfg, _results(det), runs, progress=old))
    n = len(p['history'])

    p2 = rounds.update_progress(
        p, cfg, det, runs=runs,
        state=rounds.build_state(cfg, _results(det), runs, progress=p))
    assert len(p2['history']) == n, 'a second audit must not re-date anything'


def test_a_caller_with_no_run_list_still_gets_a_correct_marker():
    """Omitting `runs` costs the dates, never correctness."""
    cfg, det = _cfg(), _details()
    old = _advance(None, cfg, det)
    del old['history']
    p = _advance(old, cfg, det)
    assert len(_hist(p, 'prior')) == 1
    assert not [e for e in p['history'] if e['kind'] == 'rung']


def test_an_upgraded_install_opens_with_an_honest_marker():
    """Progress that predates the timeline has latches but no dates, and there
    is no way to invent them. Say how much came before, then date the rest."""
    cfg, det = _cfg(), _details()
    # An install as it looked before `history` existed: latches, no key.
    old = _advance(None, cfg, det)
    del old['history']
    assert 'history' not in old

    p = _advance(old, cfg, det)
    prior = _hist(p, 'prior')
    assert len(prior) == 1
    assert prior[0]['rungs'] > 0 and prior[0]['feats'] > 0
    assert p['history'][0]['kind'] == 'prior', 'the marker is the oldest entry'
    # It counts what was already standing, not what this run earned.
    st = rounds.build_state(cfg, _results(det), _runs(), progress=old)
    assert prior[0]['rungs'] == sum(l['tier'] for l in st['ladders'])

    # Only once — the next audit has a history key and adds no second apology.
    p2 = _advance(p, cfg, det)
    assert len(_hist(p2, 'prior')) == 1


def test_a_fresh_install_never_gets_the_marker():
    p = _advance(None, _cfg(), _details())
    assert not _hist(p, 'prior')


def test_the_payload_ships_the_record_newest_first_and_unresolved():
    cfg, det = _cfg(), _details()
    p = _advance(None, cfg, det)
    p = _advance(p, cfg, _details(total_media_size=90 * TB))
    st = rounds.build_state(cfg, _results(det), _runs(), progress=p)

    assert sorted(map(str, st['history'])) == sorted(map(str, p['history']))
    ats = [e['at'] for e in st['history']]
    assert ats == sorted(ats, reverse=True)
    # Unresolved on purpose: the client names these from the ladders and feats
    # already in the payload, so no label is stored twice or frozen at earn time.
    for e in st['history']:
        assert set(e) <= {'at', 'kind', 'id', 'n', 'rungs', 'feats'}
    # Every entry must be nameable from what the payload already carries.
    lids = {l['id'] for l in st['ladders']}
    fids = {f['id'] for f in st['feats']}
    for e in st['history']:
        if e['kind'] == 'rung':
            assert e['id'] in lids
            ladder = next(l for l in st['ladders'] if l['id'] == e['id'])
            assert 1 <= e['n'] <= ladder['tiers_total']
        elif e['kind'] == 'feat':
            assert e['id'] in fids


def test_the_record_is_bounded():
    cfg = _cfg()
    p = {'history': [{'at': '2026-01-01T00:00:00', 'kind': 'feat', 'id': f'x{i}'}
                     for i in range(rounds._HISTORY_CAP + 500)]}
    p = _advance(p, cfg, _details())
    assert len(p['history']) == rounds._HISTORY_CAP
    # Trimmed from the oldest end, so the newest entries always survive.
    assert p['history'][-1]['kind'] in ('rung', 'feat')


def test_an_empty_record_ships_as_an_empty_list():
    st = rounds.build_state(_cfg(), _results(_details()), _runs())
    assert st['history'] == []
