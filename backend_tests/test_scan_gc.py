"""Full collections wait for the scan's end (SCAN SC3).

A scan builds millions of containers that all live until it finishes, and each
full pass of the cyclic collector walks every one of them: 5.4 s in 22 passes
at 650,000 files, none of which could free anything. For the length of a scan
`run_audit_process` raises the collector's third threshold, so full passes wait
for the `gc.collect()` the scan already ends with. The first two thresholds are
left alone: young collections carry on, for the scan and for request threads.

The setting belongs to the process, not the scan, so it has to come back
whichever way the scan ends: stored, refused by a guard, or raised out of.

**Every test runs the real `run_audit_process`** over real directories and a
real database (`test_publish_atomicity`'s harness), and reads the thresholds
from inside the scan, at the client call.
"""

import gc
from unittest.mock import call, patch

import pytest

import audit
import sources
from backend_tests.test_publish_atomicity import _answer, real_db, run_scan, stored


@pytest.fixture
def thresholds():
    """`(set, raised)`: the process's thresholds moved off the interpreter's
    defaults, so "put back" can't pass by resetting them, and what this
    interpreter reads them as once the third is raised."""
    before = gc.get_threshold()
    try:
        gc.set_threshold(before[0] + 1, before[1] + 1, audit.SCAN_GC_FULL_THRESHOLD)
        raised = gc.get_threshold()
        gc.set_threshold(before[0] + 1, before[1] + 1, before[2] + 1)
        yield gc.get_threshold(), raised
    finally:
        gc.set_threshold(*before)


def _client(tmp_path, claimed, seen, raises=None):
    """`fetch_file_map`, noting the thresholds the scan is running under."""
    answer = _answer(str(tmp_path / 'torrents'), claimed)

    def fetch(*args, **kwargs):
        seen.append(gc.get_threshold())
        if raises is not None:
            raise raises
        return answer
    return patch('audit.sources.fetch_file_map', side_effect=fetch)


def _statuses(path):
    return [run['status'] for run in stored(path)['runs']]


def test_full_collections_wait_while_a_scan_runs(tmp_path, thresholds):
    mine, raised = thresholds
    seen = []
    with real_db(tmp_path) as path, \
         patch.object(gc, 'set_threshold', wraps=gc.set_threshold) as set_threshold:
        run_scan(tmp_path, torrents=3, extra=(_client(tmp_path, 3, seen),))
        assert _statuses(path) == ['ok']

    # Raised once, to the scan's value over the process's own first two, and
    # put back once, to exactly what was there.
    assert set_threshold.call_args_list == [
        call(mine[0], mine[1], audit.SCAN_GC_FULL_THRESHOLD), call(*mine)]
    assert seen == [raised]
    assert raised[:2] == mine[:2]
    assert gc.get_threshold() == mine


def test_a_refused_scan_puts_the_thresholds_back(tmp_path, thresholds):
    mine, raised = thresholds
    seen = []
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=4)
        # A client that forgot its torrents, on a trigger that can't accept it.
        run_scan(tmp_path, torrents=4, claimed=0, trigger='watchdog',
                 extra=(_client(tmp_path, 0, seen),))
        assert _statuses(path) == ['ok', 'anomaly']

    assert seen == [raised]
    assert gc.get_threshold() == mine


@pytest.mark.parametrize('error', [
    RuntimeError('boom'), sources.SourceConnectionError('client down'), MemoryError()],
    ids=['unexpected', 'client unreachable', 'out of memory'])
def test_a_scan_that_raises_puts_the_thresholds_back(tmp_path, thresholds, error):
    """One case per `except` the scan has: each ends in the same `finally`."""
    mine, raised = thresholds
    seen = []
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=2, extra=(_client(tmp_path, 2, seen, raises=error),))
        assert _statuses(path) == ['error']

    assert seen == [raised]
    assert gc.get_threshold() == mine


def test_a_scan_that_raises_after_its_walks_puts_the_thresholds_back(tmp_path, thresholds):
    mine, raised = thresholds
    seen = []
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=2, extra=(
            _client(tmp_path, 2, seen),
            patch('audit._assemble_records', side_effect=RuntimeError('boom'))))
        assert _statuses(path) == ['error']

    assert seen == [raised]
    assert gc.get_threshold() == mine
