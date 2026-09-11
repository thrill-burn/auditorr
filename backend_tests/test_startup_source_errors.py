import os
import sys
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
    crash-loop breaker can be asserted rather than reasoned about."""

    def __init__(self, **initial):
        self.store = dict(initial)

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)

    def update(self, key, fn, default=None):
        self.store[key] = fn(self.store.get(key, default))
        return self.store[key]


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

    with patch('audit.db_load_config', return_value=_minimal_cfg()), \
         patch('audit.sources.fetch_file_map',
               return_value=_source_answer(torrents, mapped)), \
         patch('audit.db_get_meta', side_effect=meta.get), \
         patch('audit.db_set_meta', side_effect=meta.set), \
         patch('audit.db_delete_meta', side_effect=meta.delete), \
         patch('audit._walk_directory', side_effect=_walk), \
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
    """Otherwise two 40% declines in a row each stay under the threshold and the
    collapse arrives in instalments."""
    out = _run_guarded('watchdog', torrents=400, mapped=2000, baseline=_HEALTHY_BASELINE)
    assert out['meta'].get('source_baseline') == _HEALTHY_BASELINE


def test_a_manual_scan_overrides_the_guard():
    """Explicit intent wins, on the watchdog design's own precedent. Reaching
    the walk is the proof — the guard sits between the client answer and it."""
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
