"""The upload series is differenced, so a snapshot that misattributes upload
reads as upload.

`compute_upload_stats` diffs each tracker's cumulative upload between
consecutive snapshots and skips a negative delta as a counter reset. A scan
whose tracker calls failed for some torrents filed their upload under `Unknown`:
their trackers dipped for that scan and returned on the next, and the return
counted in full. The figures below are the reference box's, 2026-10-11 (GB): a
startup scan at 12:58 put 3,996 GB on a day that uploaded 94.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import audit

GB = 10 ** 9


def _row(at, uploads, unanswered=None, library=None, seeding=1000):
    snap = {'_instance_count': 1}
    for host, up in uploads.items():
        snap[host] = {'uploaded': up * GB, 'seeding_size': seeding * GB}
    if unanswered:
        snap['_trackers_unanswered'] = unanswered
    if library:
        snap['_library'] = library
    return {'taken_at': at, 'snapshot': snap, 'source': 'qui'}


BEFORE = _row('2026-10-11T07:51:33', {'aither.cc': 10201, 'seedpool.org': 9137})
# The startup scan: 186 tracker calls failed, and their upload went to Unknown.
STARTUP = _row('2026-10-11T12:58:22',
               {'aither.cc': 9139, 'seedpool.org': 6651, 'Unknown': 3897},
               unanswered=186, library={'total_media_size': 7}, seeding=700)
AFTER = _row('2026-10-11T18:59:13', {'aither.cc': 10241, 'seedpool.org': 9144})


def _stats(rows):
    with patch.object(audit, 'db_get_upload_snapshots', return_value=rows):
        return audit.compute_upload_stats(30)


class UnansweredTrackerSnapshotTests(unittest.TestCase):
    def test_the_scan_is_left_out_and_the_interval_still_counts(self):
        """Before → after, not zero: the 47 GB really were uploaded."""
        stats = _stats([BEFORE, STARTUP, AFTER])
        [day] = stats['daily_uploads']
        self.assertEqual(day['by_tracker'], {'aither.cc': 40 * GB, 'seedpool.org': 7 * GB})
        self.assertEqual(stats['total_uploaded'], 47 * GB)

    def test_an_unmarked_snapshot_still_counts(self):
        """The rule is the mark, not `Unknown`: a torrent with no tracker URL is
        filed there by a healthy scan too."""
        healthy = _row('2026-10-11T12:58:22',
                       {'aither.cc': 10220, 'seedpool.org': 9140, 'Unknown': 5})
        stats = _stats([BEFORE, healthy, AFTER])
        self.assertEqual(stats['total_uploaded'], 47 * GB)
        self.assertEqual(len(stats['daily_uploads']), 1)

    def test_it_supplies_no_per_tracker_point_or_latest_sizes(self):
        """Its trackers' seeding sizes are short by whatever went to Unknown,
        so as the day's last scan or the latest one it would chart a dip."""
        stats = _stats([BEFORE, AFTER, STARTUP])
        [point] = stats['daily_tracker_stats']
        self.assertEqual(point['by_tracker']['aither.cc']['seeding_size'], 1000 * GB)
        self.assertEqual({t['tracker']: t['seeding_size'] for t in stats['tracker_yields']},
                         {'aither.cc': 1000 * GB, 'seedpool.org': 1000 * GB})

    def test_its_library_block_is_kept(self):
        """Hardlinked and duplicate sizes don't depend on a tracker list."""
        stats = _stats([BEFORE, STARTUP, AFTER])
        self.assertEqual(stats['daily_library_stats'],
                         [{'date': '2026-10-11', 'total_media_size': 7}])

    def test_fewer_than_two_answered_snapshots_is_no_series(self):
        self.assertIsNone(_stats([BEFORE, STARTUP]))


if __name__ == '__main__':
    unittest.main()
