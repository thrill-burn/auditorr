"""A scan stores the kernel's peak, not a sample (SCAN SC7).

`audit_runs.peak_rss_mb` was the highest of the memory sampler's readings, one
every 20 s, so a scan under 20 s (every scan on the reference box) stored its
RSS at save time, and a longer one whichever reading landed nearest its peak:
five scans whose kernel peak was 2,942 MB stored between 1,000 and 1,191.

`run_audit_process` now restarts the kernel's high-water mark (`VmHWM`, through
`/proc/self/clear_refs`) as the scan begins, and stores what it reads at the end.
Where that write can't be made it stores what it always did. The reset is the
process's, so the debug report's own process-lifetime peak keeps the highest
value read before each reset and folds it back in.

The scan tests run the real `run_audit_process` over real directories and a real
database (`test_publish_atomicity`'s harness), with the two kernel readings
replaced, so they hold on any platform. One test asks the real kernel, on Linux.
"""

import sqlite3
import sys
from unittest.mock import patch

import pytest

import audit
import debug
from backend_tests.test_publish_atomicity import _answer, real_db, run_scan
from db import db_get_meta, db_set_meta


# ---------------------------------------------------------------------------
# debug: the reset, and the process's peak across it
# ---------------------------------------------------------------------------

class _FakeProc:
    """`VmHWM` as a number the test sets, and `clear_refs` as a file."""

    def __init__(self, tmp_path, writable=True):
        self.hwm_kb = None
        self.path = str(tmp_path / ('clear_refs' if writable else 'missing/clear_refs'))

    def __call__(self, field):
        assert field == 'VmHWM'
        return self.hwm_kb


@pytest.fixture
def fake_proc(tmp_path):
    proc = _FakeProc(tmp_path)
    with patch.object(debug, '_proc_status_kb', proc), \
         patch.object(debug, '_CLEAR_REFS', proc.path), \
         patch.object(debug, '_hwm_before_reset_kb', 0):
        yield proc


def test_the_process_peak_survives_a_scans_reset(fake_proc):
    fake_proc.hwm_kb = 900 * 1024
    assert debug.process_peak_rss_mb() == 900

    assert debug.reset_peak_rss() is True
    with open(fake_proc.path) as f:
        assert f.read() == '5'

    # The kernel now reads from the reset: the scan's own peak.
    fake_proc.hwm_kb = 300 * 1024
    assert debug.peak_rss_since_reset_mb() == 300
    assert debug.process_peak_rss_mb() == 900

    # A scan that goes higher than anything before it is the process's peak.
    fake_proc.hwm_kb = 1200 * 1024
    assert debug.process_peak_rss_mb() == 1200
    assert debug.reset_peak_rss() is True
    fake_proc.hwm_kb = 100 * 1024
    assert debug.peak_rss_since_reset_mb() == 100
    assert debug.process_peak_rss_mb() == 1200


def test_a_reset_that_cant_be_made_says_so(tmp_path):
    proc = _FakeProc(tmp_path, writable=False)
    with patch.object(debug, '_proc_status_kb', proc), \
         patch.object(debug, '_CLEAR_REFS', proc.path), \
         patch.object(debug, '_hwm_before_reset_kb', 0):
        proc.hwm_kb = 500 * 1024
        assert debug.reset_peak_rss() is False
        assert debug.process_peak_rss_mb() == 500

        proc.hwm_kb = None              # no /proc at all: a dev machine
        assert debug.reset_peak_rss() is False
        assert debug.process_peak_rss_mb() is None
        assert debug.peak_rss_since_reset_mb() is None


def _clear_refs_writable():
    if not sys.platform.startswith('linux'):
        return False
    try:
        with open('/proc/self/clear_refs', 'w'):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _clear_refs_writable(), reason='needs a Linux /proc that takes the write')
def test_the_real_kernel_restarts_its_peak():
    def hwm_mb():
        return debug._proc_status_kb('VmHWM') / 1024

    # Held throughout: a scan another test left running resets the same mark.
    with debug._hwm_lock:
        debug.reset_peak_rss()
        block = b'x' * (192 * 2 ** 20)     # filled, so every page is resident
        high = hwm_mb()
        del block
        # Within the slack of the kernel's per-thread RSS counters.
        assert debug.peak_rss_since_reset_mb() >= high - 8

        assert debug.reset_peak_rss() is True
        # The kernel restarted from the RSS now, 192 MB lower; the process kept it.
        assert hwm_mb() < high - 128
        assert debug.process_peak_rss_mb() >= high - 8


# ---------------------------------------------------------------------------
# audit: what the scan stores
# ---------------------------------------------------------------------------

def _kernel(reset_ok, seen):
    """The two kernel readings, replaced. The peak is 4,321 MB whether or not
    the reset was made, so a scan that reads it after a failed reset (when it is
    the process's lifetime peak, not the scan's) stores the wrong number."""
    def reset():
        seen.append('reset')
        return reset_ok
    return (patch('audit.reset_peak_rss', side_effect=reset),
            patch('audit.peak_rss_since_reset_mb', return_value=4321),
            patch('audit.process_rss_mb', return_value=17))


def _client(tmp_path, claimed, seen, raises=None):
    """`fetch_file_map`, noting the marker the scan started with."""
    answer = _answer(str(tmp_path / 'torrents'), claimed)

    def fetch(*args, **kwargs):
        seen.append(('marker', (db_get_meta('scan_marker') or {}).get('peak_from_kernel')))
        if raises is not None:
            raise raises
        return answer
    return patch('audit.sources.fetch_file_map', side_effect=fetch)


def _runs(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute('SELECT status, peak_rss_mb FROM audit_runs ORDER BY id').fetchall()
    finally:
        conn.close()


def test_a_scan_stores_the_kernels_peak(tmp_path):
    seen = []
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=3,
                 extra=(*_kernel(True, seen), _client(tmp_path, 3, seen)))
        assert _runs(path) == [('ok', 4321)]
    # Restarted once, before the client is asked, and said on the marker.
    assert seen == ['reset', ('marker', True)]


def test_where_the_reset_fails_a_scan_stores_what_it_did(tmp_path):
    seen = []
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=3,
                 extra=(*_kernel(False, seen), _client(tmp_path, 3, seen)))
        # No sampler reading (the scan is under 20 s), so RSS at save time.
        assert _runs(path) == [('ok', 17)]
    assert seen == ['reset', ('marker', False)]


def test_a_refused_scan_stores_the_kernels_peak(tmp_path):
    seen = []
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=4)
        run_scan(tmp_path, torrents=4, claimed=0, trigger='watchdog',
                 extra=(*_kernel(True, seen), _client(tmp_path, 0, seen)))
        assert _runs(path)[1] == ('anomaly', 4321)


@pytest.mark.parametrize('error', [RuntimeError('boom'), MemoryError()],
                         ids=['unexpected', 'out of memory'])
def test_a_scan_that_raises_stores_the_kernels_peak(tmp_path, error):
    seen = []
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=2,
                 extra=(*_kernel(True, seen), _client(tmp_path, 2, seen, raises=error)))
        assert _runs(path) == [('error', 4321)]


class _Ticks:
    """A stop event that lets the sampler take one reading."""

    def __init__(self):
        self.calls = 0

    def wait(self, interval):
        self.calls += 1
        return self.calls > 1


@pytest.mark.parametrize('from_kernel, peak', [(True, 700), (False, 100)])
def test_the_sampler_leaves_the_kernels_peak_for_a_killed_scan(tmp_path, from_kernel, peak):
    """A scan killed mid-way gets its aborted row's peak from the marker, at
    the next start; with the reset made, that's the kernel's peak so far."""
    with real_db(tmp_path):
        db_set_meta('scan_marker', {'phase': 'x', 'peak_from_kernel': from_kernel,
                                    'peak_rss_mb': 50})
        with patch('audit.process_rss_mb', return_value=100), \
             patch('audit.peak_rss_since_reset_mb', return_value=700), \
             patch('audit.container_memory', return_value={}), \
             patch('audit.host_available_mb', return_value=None):
            audit._memory_sampler(_Ticks())
        marker = db_get_meta('scan_marker')
    assert marker['last_rss_mb'] == 100
    assert marker['peak_rss_mb'] == peak
