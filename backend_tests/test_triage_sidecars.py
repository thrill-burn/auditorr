"""TRIAGE T19: a torrent's `.nfo` is not an unimported torrent (issue #26).

Sonarr and Radarr import a release's video and leave its `.nfo`, `.txt`, `.jpg`
and sample clip where they are. Each of those read as *not imported*, which is
the Triage pile, so a 7.4 GB film whose video the library held was a Triage row
reading "1 of 2 files · 24.0 KB of 7.4 GB", the `.nfo` under "Same quality as
library" with a remove beside it. The way out the page offered was an `ext:nfo`
chip, a global rule that also hid stray files from Cleanup — which is how the
reporter's stray `example.txt` went unreported.

`audit._mark_sidecars` stamps those files `sidecar`, and every "not imported"
reading honours it: the Triage predicates, the score, the per-tracker figures and
the shovel counter.
"""
import unittest
from unittest.mock import patch

import app
import audit
from audit import (
    _assemble_records, _compute_tracker_file_stats, _is_not_imported_torrent,
    _is_triage_relevant, _mark_sidecars, _mark_whole_torrents, count_pile_resolved,
    count_triage_items, file_signatures, process_health_metrics,
)


REL = 'radarr/Movie.X.2025.German.DL.1080p.WEB.h264-GRP'


def _rec(path, **over):
    base = {
        'path': path, 'size': 100, 'status': 'Seeding', 'imported': False,
        'excluded': False, 'hash': 'AAAA', 'instance_id': None,
        'trackers': ['tracker.example'], 'tracker_health': 'working', 'tracker_msg': '',
    }
    base.update(over)
    return base


def _film(imported=True, **over):
    """The report's torrent: a video the library holds, and its `.nfo`."""
    return [
        _rec(f'{REL}/movie.x.2025.german.dl.1080p.web.h264-grp.mkv', size=7_400_000_000,
             imported=imported, **over),
        _rec(f'{REL}/movie.x.2025.german.dl.1080p.web.h264-grp.nfo', size=24_000, **over),
    ]


def _stamp(records):
    _mark_sidecars(records)
    _mark_whole_torrents(records, [])
    return records


def _triage(records):
    """GET /api/workflows/triage over the compact row the audit would store."""
    row = [r for r in records if _is_triage_relevant(r)]
    with patch.object(app, 'db_load_config', return_value={}), \
         patch.object(app, 'db_has_file_results', return_value=True), \
         patch.object(app, 'db_load_file_results', return_value=row), \
         patch.object(app, 'fetch_arr_media_index_result', return_value=([], [])), \
         patch.object(app, 'fetch_arr_all_titles_result', return_value=([], [])), \
         patch.object(app, 'normalize_arr_connections', return_value=[]):
        return app.app.test_client().get('/api/workflows/triage').get_json()


class SidecarStampTests(unittest.TestCase):

    def test_the_reported_torrent_is_not_a_triage_row(self):
        records = _stamp(_film())
        mkv, nfo = records
        self.assertTrue(nfo.get('sidecar'))
        self.assertNotIn('sidecar', mkv, 'sparse: only the sidecar carries it')
        self.assertFalse(_is_not_imported_torrent(nfo))
        self.assertFalse(_is_triage_relevant(nfo))
        self.assertEqual(count_triage_items(records)['total'], 0)
        self.assertEqual(_triage(records)['items'], [])

    def test_the_stamp_rides_the_record_assembly(self):
        """The audit's own path, not just the helper."""
        def inode(rel, media_paths):
            return {'torrent_rel_path': rel, 'size': 10, 'status': 'Seeding', 'torrent_nlink': 2,
                    'media_paths': media_paths, 'torrent_paths': [f'/data/torrents/{rel}'],
                    'trackers': {'tracker.example'}, 'torrent_excluded': False, 'hash': 'AAAA',
                    'category': 'radarr', 'instance_id': None, 'instance_name': None,
                    'tracker_health': 'working', 'tracker_msg': '', 'unreg_claimants': {}}
        imap = {(1, 1): inode(f'{REL}/movie.mkv', ['/data/media/Movie X (2025)/Movie X.mkv']),
                (1, 2): inode(f'{REL}/movie.nfo', [])}
        torrents, _ = _assemble_records([(1, 1), (1, 2)], [], imap, {})
        self.assertEqual([bool(t.get('sidecar')) for t in torrents], [False, True])

    def test_every_kind_of_sidecar(self):
        records = [
            _rec(f'{REL}/movie.mkv', imported=True),
            _rec(f'{REL}/movie.nfo'),
            _rec(f'{REL}/example.txt'),
            _rec(f'{REL}/poster.jpg'),
            _rec(f'{REL}/movie.en.srt'),
            _rec(f'{REL}/Sample/movie-sample.mkv'),
            _rec(f'{REL}/movie.sample.mkv'),
            _rec(f'{REL}/README'),
        ]
        _stamp(records)
        self.assertEqual([r['path'].rsplit('/', 1)[-1] for r in records if r.get('sidecar')],
                         ['movie.nfo', 'example.txt', 'poster.jpg', 'movie.en.srt',
                          'movie-sample.mkv', 'movie.sample.mkv', 'README'])

    def test_a_torrent_with_nothing_imported_keeps_every_file_on_its_row(self):
        """The evidence is the torrent's own video in the library. Without it the
        `.nfo` goes with the row, as it always did — a delete takes it too."""
        records = _stamp(_film(imported=False))
        self.assertFalse(any(r.get('sidecar') for r in records))
        items = _triage(records)['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['file_count'], 2)

    def test_a_torrent_with_no_video_is_still_listed(self):
        """Music and books: nothing an arr imports, which is what the extension
        chips are for, and they still say so."""
        records = _stamp([_rec('music/Album/01.flac', size=40), _rec('music/Album/album.nfo', size=1)])
        self.assertFalse(any(r.get('sidecar') for r in records))
        body = _triage(records)
        self.assertEqual(len(body['items']), 1)
        self.assertEqual([s['id'] for s in body['suggestions']], ['ext:.flac'])
        self.assertIn('no video', body['suggestions'][0]['detail'])

    def test_a_partly_imported_season_lists_only_its_missing_episode(self):
        pack = 'tv/Show.S01.1080p.WEB-DL-GRP'
        records = [_rec(f'{pack}/Show.S01E{e:02d}.1080p.WEB-DL-GRP.mkv', imported=e != 10)
                   for e in range(1, 11)]
        records += [_rec(f'{pack}/show.s01.nfo'), _rec(f'{pack}/Sample/show.s01e01.sample.mkv')]
        _stamp(records)
        items = _triage(records)['items']
        self.assertEqual(len(items), 1)
        self.assertEqual([p.rsplit('/', 1)[-1] for p in items[0]['paths']],
                         ['Show.S01E10.1080p.WEB-DL-GRP.mkv'])
        self.assertEqual(items[0]['torrent_files'], 12, 'a delete still takes the whole torrent')

    def test_a_dead_seed_with_an_nfo_reads_as_a_dead_seed(self):
        """Its `.nfo` made it a not-imported torrent, which outranks the dead seed,
        so the one lossless removal Triage has read *Unregistered — not imported*."""
        records = _stamp(_film(tracker_health='unregistered'))
        items = _triage(records)['items']
        self.assertEqual([i['verdict'] for i in items], ['dead_seed'])
        self.assertEqual(items[0]['torrent_files'], 2)
        self.assertEqual(count_triage_items(records),
                         {'not_imported': 0, 'dead_seeds': 1, 'dead_registrations': 0, 'total': 1})

    def test_a_dead_seeds_nfo_does_not_cost_it_the_folder_rule(self):
        records = _stamp(_film(tracker_health='unregistered'))
        mkv = records[0]
        self.assertTrue(mkv.get('whole_torrent'))
        self.assertEqual(mkv.get('excl_folder'), REL)

    def test_the_stamp_is_per_registration(self):
        """S05: the same hash on two qui instances is two torrents. Instance 2's
        copy sits at its own save path with nothing imported, so its `.nfo`
        stays on its row."""
        records = [
            _rec('a/Rel/rel.mkv', imported=True, instance_id=1),
            _rec('a/Rel/rel.nfo', instance_id=1),
            _rec('b/Rel/rel.mkv', instance_id=2),
            _rec('b/Rel/rel.nfo', instance_id=2),
        ]
        _stamp(records)
        self.assertEqual([bool(r.get('sidecar')) for r in records], [False, True, False, False])

    def test_absence_never_reads_as_a_sidecar(self):
        """No hash, an excluded file, an orphan or an unfinished download: none
        is stamped. An imported sample is not the torrent's video either."""
        records = [
            _rec('x/Rel/rel.mkv', imported=True, hash=''),
            _rec('x/Rel/rel.nfo', hash=''),
            _rec('y/Rel/rel.mkv', imported=True, hash='BBBB'),
            _rec('y/Rel/excluded.nfo', hash='BBBB', excluded=True),
            _rec('y/Rel/orphan.nfo', hash='BBBB', status='Orphaned'),
            _rec('y/Rel/partial.nfo', hash='BBBB', incomplete=True),
            _rec('z/Rel/Sample/rel-sample.mkv', imported=True, hash='CCCC'),
            _rec('z/Rel/rel.nfo', hash='CCCC'),
        ]
        _stamp(records)
        self.assertFalse(any(r.get('sidecar') for r in records))

    def test_a_release_folder_named_sample_does_not_make_its_files_samples(self):
        self.assertFalse(audit._is_sample_path('movies/Sample.2019.1080p/featurette.mkv'))
        self.assertFalse(audit._is_sample_path('movies/Free.Sampler.2019/free.sampler.2019.mkv'))
        self.assertTrue(audit._is_sample_path('movies/Rel/Samples/rel.mkv'))


class NotImportedFiguresTests(unittest.TestCase):
    """The score, the dashboard's card and the per-tracker figures read the
    same thing Triage does. Rounds' clean-library state and its Purity ladder
    need `not_imported_count == 0`, which a library of scene releases never
    reached while every `.nfo` counted."""

    def test_the_score_and_tracker_figures_skip_sidecars(self):
        records = _stamp(_film())
        cfg = {'OR_RATIO': 0.01, 'NI_RATIO': 0.01, 'DUP_RATIO': 0.01}
        with patch('audit.db_load_history', return_value={'hourly_stats': [], 'daily_stats': []}):
            det = process_health_metrics([], records, cfg, update_history=False)['current']['details']
        self.assertEqual((det['not_imported_count'], det['not_imported_size']), (0, 0))
        stats = _compute_tracker_file_stats(records)['tracker.example']
        self.assertEqual((stats['not_imported_count'], stats['not_imported_size']), (0, 0))

    def test_the_first_stamping_scan_pays_no_shovel_credit(self):
        """Every `.nfo` of every imported torrent leaves the pile on the scan
        after the upgrade. The file didn't change; what counts as the pile did."""
        before = _film()
        after = _stamp(_film())
        self.assertEqual(count_pile_resolved(file_signatures(before), after), 0)

    def test_an_import_still_pays_for_its_video(self):
        before = _film(imported=False)
        after = _stamp(_film())
        self.assertEqual(count_pile_resolved(file_signatures(before), after), 1)

    def test_imported_stays_false(self):
        """The file has no library link, and the change log's "newly imported"
        diff reads that flag."""
        before = _film()
        after = _stamp(_film())
        self.assertFalse(after[1]['imported'])
        diff = audit.compute_diff({'torrent_files': before}, {'torrent_files': after})
        self.assertIsNone(diff)


if __name__ == '__main__':
    unittest.main()
