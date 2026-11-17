"""Two passes over every record the scan made twice, made once (SCAN SC11).

The compact Dedupe row keeps the records `is_dedupe_relevant` passes, and the
sidebar badge's group count filtered every record the same way again; the badge
now counts from the row's two lists. The shovel count and the progress pass
each worked out this scan's dead registrations; it's worked out once and handed
to both. At 650,000 files that's about 0.06 s a scan, measured on the harness's
records (`proto/e3_bench.py`).

Both run the real `run_audit_process` (`test_publish_atomicity`'s harness).
"""

from unittest.mock import patch

import audit
import scripts
from backend_tests.test_publish_atomicity import real_db, run_scan, stored
from db import db_get_meta, db_set_meta


def test_the_badge_counts_the_dedupe_rows_own_lists(tmp_path):
    with real_db(tmp_path) as path, \
         patch('audit.dedupe_row', wraps=scripts.dedupe_row) as row, \
         patch('audit.dedupe_group_count', wraps=scripts.dedupe_group_count) as count:
        run_scan(tmp_path, torrents=3, media=2)
        out = stored(path)
    assert row.call_count == 1 and count.call_count == 1
    torrents, media = count.call_args.args[:2]
    assert torrents == out['dedupe']['torrents'] and media == out['dedupe']['media']


def test_dead_registrations_are_worked_out_once(tmp_path):
    with real_db(tmp_path):
        run_scan(tmp_path, torrents=3)
        # A previous scan that had a dead registration, so the shovel count
        # asks for this scan's.
        progress = db_get_meta('ns_progress') or {}
        progress['last_dead_regs'] = ['GONE']
        db_set_meta('ns_progress', progress)
        shoveled = progress.get('shoveled', 0)
        with patch('audit.dead_registration_hashes',
                   wraps=audit.dead_registration_hashes) as dead:
            run_scan(tmp_path, torrents=3)
        assert dead.call_count == 1
        # The registration that left is still a shovel point, and the progress
        # pass still stores this scan's (none).
        after = db_get_meta('ns_progress')
        assert after['shoveled'] == shoveled + 1
        assert after['last_dead_regs'] == []
