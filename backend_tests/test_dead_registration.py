import unittest
from unittest.mock import patch

from audit import _assemble_records
from sources import new_source_report
import app


def _inode(**over):
    """A minimal torrent inode_map entry for _assemble_records."""
    base = {
        'torrent_rel_path': 'radarr/Movie.mkv', 'size': 100, 'status': 'Seeding',
        'torrent_nlink': 2, 'media_paths': ['/data/media/Movie.mkv'],
        'torrent_paths': ['/data/torrents/radarr/Movie.mkv'],
        'trackers': {'hawke.uno', 'aither.cc'}, 'torrent_excluded': False,
        'hash': 'HAWKE', 'category': 'radarr', 'instance_id': 1, 'instance_name': 'main',
        'tracker_health': 'working', 'tracker_msg': '', 'unreg_claimants': {},
    }
    base.update(over)
    return base


class ImportDetectionTests(unittest.TestCase):
    """A torrent is imported iff a hardlink exists in the media library
    (media_paths), never because nlink > 1 — cross-seeding hardlinks the same
    release across tracker dirs without importing it (issue #15)."""

    def test_cross_seed_without_library_link_is_not_imported(self):
        # Issue #15: payload cross-seeded across two trackers (torrent_nlink 2)
        # but absent from the media library must read as NOT imported.
        key = (1, 100)
        imap = {key: _inode(torrent_nlink=2, media_paths=[])}
        torrents, _ = _assemble_records([key], [], imap, {})
        self.assertFalse(torrents[0]['imported'])

    def test_library_link_is_imported(self):
        key = (1, 100)
        imap = {key: _inode(media_paths=['/data/media/Movie.mkv'])}
        torrents, _ = _assemble_records([key], [], imap, {})
        self.assertTrue(torrents[0]['imported'])


class AssembleDeadSiblingsTests(unittest.TestCase):
    def test_dead_sibling_surfaced_when_kept_claimant_alive(self):
        key = (1, 100)
        imap = {key: _inode(unreg_claimants={
            'AITHER': {'hash': 'AITHER', 'instance_id': 1, 'tracker_msg': 'Torrent has been deleted.'},
        })}
        torrents, _ = _assemble_records([key], [], imap, {})
        self.assertEqual(torrents[0]['hash'], 'HAWKE')          # healthiest kept
        self.assertEqual(torrents[0]['dead_siblings'],
                         [{'hash': 'AITHER', 'instance_id': 1, 'tracker_msg': 'Torrent has been deleted.'}])

    def test_no_dead_siblings_key_when_none(self):
        key = (1, 100)
        torrents, _ = _assemble_records([key], [], {key: _inode()}, {})
        self.assertNotIn('dead_siblings', torrents[0])

    def test_kept_hash_is_excluded_from_siblings(self):
        # The kept (healthiest) hash must never list itself as a dead sibling.
        key = (1, 100)
        imap = {key: _inode(unreg_claimants={
            'HAWKE':  {'hash': 'HAWKE',  'instance_id': 1, 'tracker_msg': ''},
            'AITHER': {'hash': 'AITHER', 'instance_id': 1, 'tracker_msg': 'unregistered'},
        })}
        torrents, _ = _assemble_records([key], [], imap, {})
        self.assertEqual([s['hash'] for s in torrents[0]['dead_siblings']], ['AITHER'])

    def test_fully_dead_path_emits_no_dead_siblings(self):
        # When the kept claimant is itself unregistered the whole path is a
        # dead_seed — not a "dead registration with a live payload".
        key = (1, 100)
        imap = {key: _inode(hash='AITHER', tracker_health='unregistered',
                            unreg_claimants={'AITHER': {'hash': 'AITHER', 'instance_id': 1, 'tracker_msg': 'x'}})}
        torrents, _ = _assemble_records([key], [], imap, {})
        self.assertNotIn('dead_siblings', torrents[0])


class PartitionRemovalTests(unittest.TestCase):
    """delete_files='auto': keep files a survivor still holds, delete files unique
    to the removed torrent.

    **Rewritten deliberately in Phase 9, every fixture kept.** These four used to
    call `_partition_removal_by_file_sharing` with a *static* paths map, which
    answers for torrents the code never asked about — so the exact-size
    pre-filter they sat behind (S01) could never show in them. They now drive the
    route with a listing that answers only what it is asked, and assert on what
    `sources.remove_torrents` is called with. The rows carry no `save_path` or
    `name`, so the content-root rule is inert and the size rule decides; the
    shapes S01 is about live in `test_triage_removal.py`.
    """

    def _run(self, rows, paths_map, items):
        removals = []

        def fetch(_cfg, asked):
            # Registration-keyed (S05); qbit rows carry no instance, so these
            # keys are the bare hashes they always were.
            return {app._reg(i): paths_map.get(i['hash']) for i in asked}

        def remove(_cfg, removing, delete_files=True):
            removals.append(([i['hash'] for i in removing], delete_files))
            return len(removing)

        report = new_source_report('qbit')
        report.update(instances_total=1, instances_ok=1)
        with patch.object(app, 'db_load_config', return_value={'ALLOW_CLIENT_DELETE': True}), \
             patch.object(app.sources, 'list_torrents', return_value=rows), \
             patch.object(app.sources, 'list_torrents_detailed', return_value=([], report)), \
             patch.object(app.sources, 'fetch_torrent_file_paths', side_effect=fetch), \
             patch.object(app.sources, 'remove_torrents', side_effect=remove), \
             patch.object(app, 'nudge_watchdog'), \
             patch.object(app.time, 'sleep'):
            resp = app.app.test_client().post('/api/workflows/remove_torrents', json={
                'items': items, 'delete_files': 'auto'})
        self.assertEqual(resp.status_code, 200)
        delete = sorted(h for hashes, d in removals if d for h in hashes)
        keep = sorted(h for hashes, d in removals if not d for h in hashes)
        return delete, keep

    def test_shared_path_keeps_files(self):
        # Topology A: dead AITHER + working HAWKE point at the SAME file.
        rows = [{'hash': 'AITHER', 'size': 100}, {'hash': 'HAWKE', 'size': 100}]
        paths = {'AITHER': ['/d/Movie.mkv'], 'HAWKE': ['/d/Movie.mkv']}
        delete, keep = self._run(rows, paths, [{'hash': 'AITHER'}])
        self.assertEqual(keep, ['AITHER'])
        self.assertEqual(delete, [])

    def test_distinct_hardlink_deletes_files(self):
        # Topology B: each torrent owns its own hardlink path.
        rows = [{'hash': 'AITHER', 'size': 100}, {'hash': 'HAWKE', 'size': 100}]
        paths = {'AITHER': ['/d/a/Movie.mkv'], 'HAWKE': ['/d/h/Movie.mkv']}
        delete, keep = self._run(rows, paths, [{'hash': 'AITHER'}])
        self.assertEqual(delete, ['AITHER'])
        self.assertEqual(keep, [])

    def test_standalone_deletes_files(self):
        rows = [{'hash': 'SOLO', 'size': 100}]
        paths = {'SOLO': ['/d/Movie.mkv']}
        delete, keep = self._run(rows, paths, [{'hash': 'SOLO'}])
        self.assertEqual(delete, ['SOLO'])
        self.assertEqual(keep, [])

    def test_removing_whole_shared_group_deletes_files(self):
        # Both shared-path claimants removed → no survivor → safe to delete.
        rows = [{'hash': 'AITHER', 'size': 100}, {'hash': 'HAWKE', 'size': 100}]
        paths = {'AITHER': ['/d/Movie.mkv'], 'HAWKE': ['/d/Movie.mkv']}
        delete, keep = self._run(rows, paths, [{'hash': 'AITHER'}, {'hash': 'HAWKE'}])
        self.assertEqual(delete, ['AITHER', 'HAWKE'])
        self.assertEqual(keep, [])


if __name__ == '__main__':
    unittest.main()
