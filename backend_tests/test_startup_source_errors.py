import json
import os
import sys
import time
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import audit
import sources
from audit import run_audit_process


def _minimal_cfg():
    return {
        'TORRENT_SOURCE': 'qui',
        'QB_HOST': 'http://localhost:8080',
        'QB_USER': 'admin',
        'QB_PASS': 'password',
        'LOCAL_PATH': '/data/torrents',
        'MEDIA_PATH': '/data/media',
        'REMOTE_PATH': '/data/torrents',
        'OR_RATIO': 0.01,
        'NI_RATIO': 0.01,
        'DUP_RATIO': 0.01,
        'EXCLUSION_PATTERNS': [],
    }


@patch('audit.db_save_audit')
@patch('audit._save_error_status')
@patch('audit.db_load_config')
@patch('audit.sources.fetch_file_map')
def test_run_audit_process_can_suppress_transient_source_error_persistence(
    mock_fetch, mock_load_config, mock_save_error_status, mock_save_audit
):
    mock_load_config.return_value = _minimal_cfg()
    mock_fetch.side_effect = sources.SourceConnectionError('qui connection error: not ready')

    run_audit_process('startup', persist_source_errors=False)

    mock_save_error_status.assert_not_called()
    mock_save_audit.assert_not_called()


@patch('audit.db_save_audit')
@patch('audit._save_error_status')
@patch('audit.db_load_config')
@patch('audit.sources.fetch_file_map')
def test_run_audit_process_persists_source_errors_by_default(
    mock_fetch, mock_load_config, mock_save_error_status, mock_save_audit
):
    mock_load_config.return_value = _minimal_cfg()
    mock_fetch.side_effect = sources.SourceConnectionError('qui connection error: still down')

    run_audit_process('manual')

    mock_save_error_status.assert_called_once_with('qui connection error: still down')
    mock_save_audit.assert_called_once()


# ---------------------------------------------------------------------------
# The plausibility guard, driven through run_audit_process (CLEANUP C2)
# ---------------------------------------------------------------------------

class _FakeMeta:
    """Stands in for the app_meta row set so the guard's interactions with the
    crash-loop breaker can be asserted rather than reasoned about.

    Every method takes and ignores `conn` — since Phase 13 the meta writes of a
    scan ride the publish transaction (S04), so the audit passes one. This
    harness stands in for the database entirely, so there is nothing to pass it
    to; `test_publish_atomicity.py` is where a real file is used instead.
    """

    def __init__(self, **initial):
        self.store = dict(initial)

    def get(self, key, default=None, conn=None):
        return self.store.get(key, default)

    def set(self, key, value, conn=None):
        self.store[key] = value

    def delete(self, key, conn=None):
        self.store.pop(key, None)

    def update(self, key, fn, default=None, conn=None):
        self.store[key] = fn(self.store.get(key, default))
        return self.store[key]


@contextmanager
def _fake_publish():
    """`db_publish` with no database behind it — the writes are all mocked."""
    yield None


def _source_answer(torrents, mapped):
    report = sources.new_source_report('qui')
    report['torrent_count']   = torrents
    report['file_map_size']   = mapped
    report['instances_total'] = 1
    report['instances_ok']    = 1
    file_map = {f'/data/torrents/f{i}': {'status': 'Seeding', 'trackers': set()}
                for i in range(mapped)}
    return file_map, [], {'_instance_count': 1}, report


class _WalkReached(BaseException):
    """Sentinel: the scan got past the guard and started walking.

    `BaseException` on purpose — `run_audit_process` catches `Exception` broadly
    and would swallow it, which is exactly what it should do with a real failure.
    """


def _run_guarded(trigger, *, torrents, mapped, baseline, walk_raises=False):
    """Drive a whole audit against a client answering `torrents`/`mapped`."""
    meta = _FakeMeta(source_baseline=baseline, consecutive_aborted_scans=3)

    def _walk(*a, **kw):
        if walk_raises:
            raise _WalkReached()
        return (['k1', 'k2', 'k3'], 3, 0, None)

    # The walk is stood in for, so the roots it would walk are stood in for too
    # (Phase 12): '/data/…' is not a directory on the machine running the tests.
    with patch('audit.db_load_config', return_value=_minimal_cfg()), \
         patch('audit._root_state', return_value={'configured': True, 'exists': True}), \
         patch('audit.sources.fetch_file_map',
               return_value=_source_answer(torrents, mapped)), \
         patch('audit.db_get_meta', side_effect=meta.get), \
         patch('audit.db_set_meta', side_effect=meta.set), \
         patch('audit.db_delete_meta', side_effect=meta.delete), \
         patch('audit._walk_directory', side_effect=_walk), \
         patch('audit.db_publish', _fake_publish), \
         patch('audit._save_error_status') as save_error, \
         patch('audit.db_save_audit') as save_audit, \
         patch('audit.db_save_file_results') as save_files, \
         patch('audit.db_save_file_signatures') as save_sigs, \
         patch('audit.db_save_results') as save_results, \
         patch('audit.db_save_upload_snapshot') as save_snapshot, \
         patch('audit.db_save_change_log_entry') as save_changes:
        run_audit_process(trigger)
        return {
            'meta': meta, 'save_error': save_error, 'save_audit': save_audit,
            'save_files': save_files, 'save_sigs': save_sigs,
            'save_results': save_results, 'save_snapshot': save_snapshot,
            'save_changes': save_changes,
        }


_HEALTHY_BASELINE = {'torrent_count': 1000, 'file_map_size': 5000}


def test_a_client_that_forgot_its_torrents_does_not_get_its_answer_persisted():
    """C2: a client answering with far fewer torrents is believed absolutely
    today, and every file under LOCAL_PATH is then classified Orphaned — the
    whole tree, in green, under one `Select all`, one click from `rm`."""
    out = _run_guarded('watchdog', torrents=0, mapped=0, baseline=_HEALTHY_BASELINE)

    out['save_files'].assert_not_called()
    out['save_results'].assert_not_called()
    out['save_snapshot'].assert_not_called()
    out['save_changes'].assert_not_called()
    # The signature map is the diff baseline. Writing a blackout's signatures
    # would make the *next* good scan report every file coming back, and
    # `count_pile_resolved` would pay shovel credit for a recovery that never
    # happened — which the monotonic Rounds layer can never take back.
    out['save_sigs'].assert_not_called()


def test_a_refused_scan_says_why():
    out = _run_guarded('watchdog', torrents=0, mapped=0, baseline=_HEALTHY_BASELINE)

    message = out['save_error'].call_args[0][0]
    assert message.startswith('Source anomaly:')
    assert 'run a scan manually' in message

    args = out['save_audit'].call_args[0]
    assert args[2] == 'anomaly'
    assert out['meta'].get('last_source_anomaly')['code'] == 'torrent_count_collapse'


def test_a_refused_scan_exits_normally_and_does_not_trip_the_crash_loop_breaker():
    """CLEANUP §8. A scan that refuses to persist must still clear `scan_marker`
    and clear the aborted streak, or the next boot reads it as a process killed
    mid-scan; at two the watchdog and startup audit stop entirely. A safety
    guard that disables scanning is worse than the bug it guards against."""
    out = _run_guarded('watchdog', torrents=0, mapped=0, baseline=_HEALTHY_BASELINE)

    assert out['meta'].get('scan_marker') is None
    assert out['meta'].get('consecutive_aborted_scans') == 0


def test_a_refused_scan_does_not_move_the_baseline():
    """A refused collapse must not become the next scan's baseline. This used to
    say the rule also stopped a collapse arriving in instalments; it did not — two
    *accepted* 40% declines each passed against the scan before (S02). The
    reference does that: `test_two_accepted_forty_percent_drops_trip_the_reference`."""
    out = _run_guarded('watchdog', torrents=400, mapped=2000, baseline=_HEALTHY_BASELINE)
    assert out['meta'].get('source_baseline') == _HEALTHY_BASELINE


def test_a_manual_scan_accepts_a_real_drop():
    """Rewritten in Phase 12 under the user's decision 1 (a), fixture kept. This
    was `test_a_manual_scan_overrides_the_guard` — "explicit intent wins", for
    every rule. A manual scan now accepts a change in what the client holds (a
    library can really shrink, and accepting it stays one click away), and never
    a read that failed: `test_a_failed_read_is_refused_on_a_manual_scan`, below.
    Reaching the walk is the proof — the guard sits between the answer and it."""
    with pytest.raises(_WalkReached):
        _run_guarded('manual', torrents=0, mapped=0,
                     baseline=_HEALTHY_BASELINE, walk_raises=True)


def test_a_startup_scan_does_not_override_the_guard():
    """A rebuilt container with a fresh session directory is exactly C2."""
    out = _run_guarded('startup', torrents=0, mapped=0,
                       baseline=_HEALTHY_BASELINE, walk_raises=True)
    out['save_files'].assert_not_called()
    assert out['meta'].get('last_source_anomaly')['code'] == 'torrent_count_collapse'


def test_a_plausible_scan_is_not_blocked():
    """The guard must be invisible on a healthy install — it lets the scan
    through to the walk, where this fixture stops it."""
    with pytest.raises(_WalkReached):
        _run_guarded('watchdog', torrents=1010, mapped=5050,
                     baseline=_HEALTHY_BASELINE, walk_raises=True)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 12 — S03 (the filesystem half of R1) and S02, from the 2026-09-10
# outside review, through a scan that can actually persist: real walks over a
# tmp tree, every write captured. `_run_guarded` above stops at the walk, which
# is exactly the part S03 is about.
# ═════════════════════════════════════════════════════════════════════════════

def _roots(tmp_path, torrents=3, media=3):
    """Two real roots holding `torrents` / `media` files, one release folder
    each, sizes distinct so nothing is a duplicate. Returns (local, media)."""
    local, lib = tmp_path / 'torrents', tmp_path / 'media'
    for root, n, sub in ((local, torrents, 'movies'), (lib, media, 'Movies')):
        folder = root / sub / 'Rel'
        folder.mkdir(parents=True)
        for i in range(n):
            (folder / f'f{i}.mkv').write_bytes(b'x' * (10 + i))
    return str(local), str(lib)


def _failed_instance_answer(torrents, mapped):
    answer = _source_answer(torrents, mapped)
    answer[3]['instances_total'] = 2
    sources.report_instance_failure(answer[3], 'second', 'timed out')
    return answer


# What a refused scan must leave exactly as it found it.
_GUARDED_META = ('ns_progress', 'source_baseline', 'source_reference', 'last_source_report')


def _run_scan(trigger, *, local, media, answer=None, meta=None, scandir=None):
    """Drive a whole audit that can persist. `answer` is `fetch_file_map`'s."""
    meta = meta if meta is not None else _FakeMeta()
    cfg = dict(_minimal_cfg(), LOCAL_PATH=local, MEDIA_PATH=media, REMOTE_PATH=local)
    before = {k: meta.get(k) for k in _GUARDED_META}
    writes = {'save_error': 'audit._save_error_status', 'save_audit': 'audit.db_save_audit',
              'save_files': 'audit.db_save_file_results', 'save_sigs': 'audit.db_save_file_signatures',
              'save_results': 'audit.db_save_results', 'save_snapshot': 'audit.db_save_upload_snapshot',
              'save_changes': 'audit.db_save_change_log_entry'}
    with ExitStack() as stack:
        for target, kw in (
                ('audit.db_load_config', {'return_value': cfg}),
                ('audit.sources.fetch_file_map', {'return_value': answer or _source_answer(0, 0)}),
                ('audit.db_get_meta', {'side_effect': meta.get}),
                ('audit.db_set_meta', {'side_effect': meta.set}),
                ('audit.db_delete_meta', {'side_effect': meta.delete}),
                ('audit.db_update_meta', {'side_effect': meta.update}),
                ('audit.db_load_history', {'return_value': {'hourly_stats': [], 'daily_stats': []}}),
                ('audit.db_save_history', {}),
                ('audit.db_load_results', {'return_value': {}}),
                ('audit.db_get_upload_snapshots', {'return_value': []}),
                ('audit.db_get_recent_runs', {'return_value': []}),
                ('audit.db_load_file_signatures', {'return_value': {}}),
                ('audit.db_publish', {'new': _fake_publish})):
            stack.enter_context(patch(target, **kw))
        if scandir is not None:
            stack.enter_context(patch('os.scandir', scandir))
        mocks = {name: stack.enter_context(patch(target)) for name, target in writes.items()}
        run_audit_process(trigger)
    return SimpleNamespace(meta=meta, before=before, **mocks)


def _nothing_persisted(out):
    """C2's rule, which S03 inherits whole: nothing derived from a refused scan —
    no file lists, signatures, results, upload snapshot, change log or progress,
    and the baseline and reference do not move."""
    for mock in (out.save_files, out.save_sigs, out.save_results,
                 out.save_snapshot, out.save_changes):
        mock.assert_not_called()
    for key, value in out.before.items():
        assert out.meta.get(key) == value, key


def test_a_healthy_scan_persists_the_same_figures(tmp_path):
    """Characterisation. The health score must not move for any of Phase 12 on a
    healthy install; these figures were read off this scan before the phase."""
    local, media = _roots(tmp_path, torrents=3, media=0)
    rel, lib = os.path.join(local, 'movies', 'Rel'), os.path.join(media, 'Movies', 'Rel')
    os.link(os.path.join(rel, 'f0.mkv'), os.path.join(lib, 'f0.mkv'))
    with open(os.path.join(lib, 'extra.mkv'), 'wb') as fh:
        fh.write(b'y' * 20)
    answer = _source_answer(0, 0)
    for name in ('f0.mkv', 'f1.mkv'):
        answer[0][os.path.join(rel, name)] = {
            'status': 'Seeding', 'trackers': {'t.example'}, 'hash': name,
            'tracker_health': 'working', 'tracker_msg': ''}
    answer[3].update(torrent_count=2, file_map_size=2)

    out = _run_scan('watchdog', local=local, media=media, answer=answer)

    dashboard = out.save_results.call_args[0][0]['dashboard']
    details = dashboard['current']['details']
    assert dashboard['score'] == 33.3
    assert {k: details[k] for k in (
        'total_media_size', 'hardlinked_media_size', 'total_torrents_size',
        'orphaned_torrent_size', 'not_imported_size', 'orphaned_torrent_count',
        'not_imported_count', 'media_file_count', 'torrent_file_count')} == {
        'total_media_size': 30, 'hardlinked_media_size': 10, 'total_torrents_size': 33,
        'orphaned_torrent_size': 12, 'not_imported_size': 11, 'orphaned_torrent_count': 1,
        'not_imported_count': 1, 'media_file_count': 2, 'torrent_file_count': 3}


def test_a_missing_media_root_refuses_to_persist(tmp_path):
    """S03. A media folder that is not there walked as an empty library — a log
    line and nothing else — and the scan persisted every torrent as not imported:
    Triage fills, the health score falls, Rounds' history records it. The
    realistic trigger is a container that starts before the array mounts.

    Refused on every trigger, a manual scan included (decision 1 (a)): asking for
    a scan is not authority to believe a read that failed."""
    local, _ = _roots(tmp_path)
    meta = _FakeMeta(consecutive_aborted_scans=3)
    out = _run_scan('manual', local=local, media=str(tmp_path / 'not-mounted'),
                    answer=_source_answer(3, 3), meta=meta)

    _nothing_persisted(out)
    anomaly = meta.get('last_source_anomaly')
    assert anomaly['code'] == 'root_missing'
    assert anomaly['detail']['root'] == 'media'
    # Which root, never its path: this reaches /api/debug/report.
    assert 'not-mounted' not in json.dumps(anomaly)
    assert 'not-mounted' not in out.save_error.call_args[0][0]
    assert meta.get('scan_marker') is None
    assert meta.get('consecutive_aborted_scans') == 0


def test_a_missing_torrent_root_refuses_to_persist(tmp_path):
    """S03, the other root. `client_blackout` needs a non-zero disk count, so
    after an empty torrent walk every rule passed, and a missing LOCAL_PATH
    persisted the whole library as unseeded — Backfill fills."""
    _, media = _roots(tmp_path)
    meta = _FakeMeta()
    out = _run_scan('watchdog', local=str(tmp_path / 'not-mounted'), media=media,
                    answer=_source_answer(40, 40), meta=meta)

    _nothing_persisted(out)
    anomaly = meta.get('last_source_anomaly')
    assert anomaly['code'] == 'root_missing'
    assert anomaly['detail']['root'] == 'torrents'


def test_an_empty_but_readable_root_still_persists(tmp_path):
    """A fresh install is not a missing mount: both folders are there and
    readable, nothing is in them yet, and there is no baseline — nothing to
    protect. Passes on the old code by design: it is what stops S03's fix from
    refusing every first scan."""
    local, media = _roots(tmp_path, torrents=0, media=0)
    meta = _FakeMeta()
    out = _run_scan('startup', local=local, media=media, meta=meta)

    out.save_results.assert_called_once()
    assert meta.get('last_source_anomaly') is None


def test_a_disk_walk_that_collapses_is_an_anomaly(tmp_path):
    """S03. A mount point that exists but is empty — what a Docker bind mount of
    a host path that is not there looks like — walks cleanly and finds nothing.
    Against the persisted disk baseline, that is a collapse."""
    local, media = _roots(tmp_path, torrents=0, media=3)
    meta = _FakeMeta(source_baseline={'torrent_count': 10, 'file_map_size': 10,
                                      'torrent_files': 400, 'media_files': 3})
    out = _run_scan('watchdog', local=local, media=media,
                    answer=_source_answer(10, 10), meta=meta)

    _nothing_persisted(out)
    anomaly = meta.get('last_source_anomaly')
    assert anomaly['code'] == 'disk_collapse'
    assert anomaly['detail']['root'] == 'torrents'


def test_a_manual_scan_accepts_a_disk_collapse_and_resets_the_reference(tmp_path):
    """A library can really shrink, and accepting that stays one click away
    (decision 1 (a)). The accepted scan becomes the new reference (decision
    2 (a)), or the next scheduled scan would refuse the same drop again. Passes
    on the old code, which had no disk guard at all: it is here so the fix
    cannot over-refuse."""
    local, media = _roots(tmp_path, torrents=0, media=3)
    meta = _FakeMeta(source_baseline={'torrent_count': 10, 'file_map_size': 10,
                                      'torrent_files': 400, 'media_files': 3})
    accepted = _run_scan('manual', local=local, media=media,
                         answer=_source_answer(10, 10), meta=meta)
    after = _run_scan('watchdog', local=local, media=media,
                      answer=_source_answer(10, 10), meta=meta)

    accepted.save_results.assert_called_once()
    after.save_results.assert_called_once()


def test_an_unlistable_release_folder_refuses_to_persist(tmp_path):
    """S03. `os.walk` had no `onerror`, so a directory that could not be listed
    dropped everything under it without a word. Within two segments of a root —
    a category or a release folder — that hides whole releases, so the scan
    refuses, on every trigger."""
    local, media = _roots(tmp_path)
    blocked = os.path.normcase(os.path.normpath(os.path.join(local, 'movies', 'Rel')))
    real_scandir = os.scandir

    def scandir(path='.'):
        if os.path.normcase(os.path.normpath(os.fspath(path))) == blocked:
            raise PermissionError(13, 'Permission denied', os.fspath(path))
        return real_scandir(path)

    meta = _FakeMeta()
    out = _run_scan('manual', local=local, media=media, answer=_source_answer(3, 3),
                    meta=meta, scandir=scandir)

    _nothing_persisted(out)
    anomaly = meta.get('last_source_anomaly')
    assert anomaly['code'] == 'root_unlistable'
    assert anomaly['detail']['root'] == 'torrents'


def test_a_failed_read_is_refused_on_a_manual_scan(tmp_path):
    """S02, decision 1 (a). A manual scan overrode every rule, a failed instance
    included, so Triage, Backfill, the health score and the change log read a
    scan missing a whole instance as the truth — Phase 8's `unverified` kept only
    Cleanup safe. A manual scan accepts a change in what the client holds, never
    a read that failed, and the message says what to fix rather than "run a scan
    manually", which would not help."""
    local, media = _roots(tmp_path)
    meta = _FakeMeta(source_baseline=dict(_HEALTHY_BASELINE))
    out = _run_scan('manual', local=local, media=media,
                    answer=_failed_instance_answer(900, 4800), meta=meta)

    _nothing_persisted(out)
    assert meta.get('last_source_anomaly')['code'] == 'instances_unavailable'
    assert 'run a scan manually' not in out.save_error.call_args[0][0]


def test_two_accepted_forty_percent_drops_trip_the_reference(tmp_path):
    """S02, decision 2 (a). The comment beside the baseline write, CLAUDE.md and
    CLEANUP's C2 record all said two 40% declines in a row are caught. They were
    not: refusing to advance on an *anomalous* scan does nothing about two
    *accepted* ones, and 100 → 60 → 36 each passed against the scan before it.
    Collapse is now measured against the largest count persisted in the window,
    so the second instalment is 64% below it."""
    local, media = _roots(tmp_path)
    meta = _FakeMeta()
    first = _run_scan('watchdog', local=local, media=media,
                      answer=_source_answer(100, 100), meta=meta)
    second = _run_scan('watchdog', local=local, media=media,
                       answer=_source_answer(60, 60), meta=meta)
    third = _run_scan('watchdog', local=local, media=media,
                      answer=_source_answer(36, 36), meta=meta)

    first.save_results.assert_called_once()
    second.save_results.assert_called_once()
    _nothing_persisted(third)
    assert meta.get('last_source_anomaly')['code'] == 'torrent_count_collapse'


def _startup_attempts(code):
    """Run the startup sequence against scans that each refuse with `code`.
    Returns how many scans it ran."""
    import app
    import audit as audit_mod
    attempts = []

    def refused_scan(trigger, persist_source_errors=True):
        attempts.append(persist_source_errors)
        audit_mod._record_source_anomaly(
            {'code': code, 'message': 'x.', 'detail': {'root': 'media'}},
            trigger, {}, time.time(), persist=persist_source_errors)

    with patch.object(app, '_torrent_source_configured', return_value=True), \
         patch.object(app, 'db_load_config', return_value={}), \
         patch.object(app, 'try_start_scanning', return_value=True), \
         patch.object(app, 'run_audit_process', side_effect=refused_scan), \
         patch.object(app.time, 'sleep'), \
         patch('audit.db_set_meta'), patch('audit.db_save_audit'), \
         patch('audit._save_error_status'):
        app._run_startup_audit()
    return attempts


def test_a_missing_root_at_startup_is_retried_more_than_once():
    """The one retry a refused startup scan gets was written for a client still
    loading its session, which answers within seconds. An array or a network
    share still mounting takes minutes — and the watchdog starts after the
    startup audit and watches only roots that exist, so without more retries the
    next scan would be the scheduled one, hours later. Only the last attempt
    records a failed run."""
    attempts = _startup_attempts('root_missing')
    assert len(attempts) > 2
    assert attempts[-1] is True and not any(attempts[:-1])


def test_a_client_anomaly_at_startup_still_retries_once():
    assert _startup_attempts('torrent_count_collapse') == [False, True]
