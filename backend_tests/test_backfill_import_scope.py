"""A Backfill import can only force-import over the files it was started for (B11).

The import watch nurses a grab into the library, and when the arr parks it as
`importPending` — which a backfill routinely is, since the library already holds
this content — it forces the import with `replaceExistingFiles`. It did that
with no scope and the default import mode:

  * **Scope.** Whatever the download resolved to was imported wholesale. That is
    B1's amplifier: a season pack grabbed for one episode replaced every episode
    it carried, and each replaced file's torrent became an orphan.
  * **The media-folder fallback.** With no download id and no output path it
    listed the arr's own library folder, whose rows are the library file itself —
    so it could re-import the very file it was replacing.
  * **Import mode.** `Auto` means move whenever no tracked download stands behind
    the rows, which pulls the payload out from under the seeding torrent.

A path scope cannot help a grab: the files to import do not exist when the watch
starts, and the library paths it knows are the files being replaced. Episodes are
the unit both sides share, so that is the scope.

Phase 12 (S07, from the 2026-09-10 outside review) runs the watch against an arr
that answers only what it is asked: `arr._arr_get` is the one thing faked, so
the queue poll, the episode join and the file-id reader are all the real ones.
This harness used to mock `poll_queue_until_clear` with lists and
`get_arr_file_id` with a constant — which is how an empty list could mean five
different things, one of them "Imported successfully", without a test noticing.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app
from arr import force_manual_import_by_id


def _cfg():
    return {'ARR_CONNECTIONS': [
        {'id': 'sonarr-tv', 'service': 'sonarr', 'name': 'TV',
         'base_url': 'http://tv:8989', 'api_key': 'a'},
        {'id': 'radarr-mv', 'service': 'radarr', 'name': 'Movies',
         'base_url': 'http://mv:7878', 'api_key': 'b'},
    ]}


def _pack_rows():
    """`/api/v3/manualimport` for a ten-episode pack download."""
    return [{'path': f'/downloads/Show.S01/Show.S01E{n:02d}.mkv', 'quality': {},
             'languages': [], 'seasonNumber': 1, 'episodes': [{'id': 100 + n}]}
            for n in range(1, 11)]


def _force(rows_for, **kw):
    """Run the real force import with only HTTP mocked. Returns (command body, GETs)."""
    requested = []

    def fake_get(_base, _key, path, **_kw):
        requested.append(path)
        if path.startswith('/api/v3/series/'):
            return {'path': '/tv/Show'}
        if path.startswith('/api/v3/movie/'):
            return {'path': '/movies/Film (2026)'}
        return rows_for(path)

    with patch('arr._arr_get', side_effect=fake_get), patch('arr.time.sleep'), \
         patch('arr.urllib.request.urlopen') as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = b'{}'
        try:
            force_manual_import_by_id(_cfg(), **kw)
        finally:
            _force.requested = requested
        body = json.loads(urlopen.call_args[0][0].data) if urlopen.call_args else None
    return body, requested


# ── force_manual_import_by_id ────────────────────────────────────────────────

def test_a_pack_download_imports_only_the_candidates_episodes():
    body, _ = _force(lambda path: _pack_rows(), service='sonarr', connection_id='sonarr-tv',
                     arr_id=1, download_id='HASH', only_episode_ids=[101])

    assert [f['path'] for f in body['files']] == ['/downloads/Show.S01/Show.S01E01.mkv']
    assert body['files'][0]['episodeIds'] == [101]


def test_a_row_naming_no_episode_is_out_of_scope():
    """A row the arr could not attach to an episode cannot be shown to fit."""
    rows = _pack_rows()[:1] + [{'path': '/downloads/Show.S01/sample.mkv', 'episodes': []}]
    body, _ = _force(lambda path: rows, service='sonarr', connection_id='sonarr-tv',
                     arr_id=1, download_id='HASH', only_episode_ids=[101])

    assert [f['path'] for f in body['files']] == ['/downloads/Show.S01/Show.S01E01.mkv']


def test_a_row_carrying_an_episode_outside_the_scope_is_dropped():
    """A multi-episode file holding one in-scope and one seeded episode would
    replace the seeded one too."""
    rows = [{'path': '/d/Show.S01E01E02.mkv', 'episodes': [{'id': 101}, {'id': 102}]}]
    with pytest.raises(ValueError, match='does not list the selected file'):
        _force(lambda path: rows, service='sonarr', connection_id='sonarr-tv',
               arr_id=1, download_id='HASH', only_episode_ids=[101])


def test_the_library_folder_is_never_listed_when_the_fallback_is_off():
    with pytest.raises(ValueError):
        _force(lambda path: _pack_rows(), service='sonarr', connection_id='sonarr-tv',
               arr_id=1, only_episode_ids=[101], media_folder_fallback=False)
    assert not [p for p in _force.requested if 'manualimport' in p]


def test_the_fallback_is_still_there_for_a_caller_that_guards_it():
    """Triage keeps it, behind `only_paths` — the switch is what changed."""
    _force(lambda path: [{'path': '/data/x.mkv'}], service='radarr', connection_id='radarr-mv',
           arr_id=7, only_paths=['/data/x.mkv'])
    assert any('folder=%2Fmovies%2FFilm%20%282026%29' in p for p in _force.requested)


def test_auto_is_honoured_on_the_download_id_branch():
    body, _ = _force(lambda path: _pack_rows()[:1], service='sonarr', connection_id='sonarr-tv',
                     arr_id=1, download_id='HASH')
    assert body['importMode'] == 'Auto'


def test_a_folder_branch_always_copies():
    """No tracked download behind the rows, so Auto would move them out from
    under the seeding torrent — and the caller cannot know which branch it gets."""
    def rows_for(path):
        return [] if 'downloadId=' in path else _pack_rows()[:1]

    body, _ = _force(rows_for, service='sonarr', connection_id='sonarr-tv', arr_id=1,
                     download_id='HASH', download_folder='/downloads/Show.S01')
    assert body['importMode'] == 'Copy'


# ── the watch endpoint ───────────────────────────────────────────────────────

class _Sync:
    """The watch thread, run inline so its decisions can be asserted on. An audit
    thread it starts — a trump's re-audit — is recorded, never run."""
    scans = []

    def __init__(self, target=None, **_kw):
        self.target = target

    def start(self):
        if self.target is app.run_audit_process:
            _Sync.scans.append(self.target)
        else:
            self.target()


class _Clock:
    """`time.monotonic` and `time.sleep` for the waits: sleeping moves time on."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


class _Arr:
    """One Sonarr or Radarr, answering only what it is asked (`arr._arr_get`).

    `queue` holds one frame per queue read — a list of records, or an exception
    to raise — and the last frame repeats. The download imports when the reads
    reach `imports_at` (the arr importing it by itself) or when the watch force-
    imports it; from then on the queue is empty and the target's file has a new
    id. `other_imports_at` imports a *different* episode of the series instead.
    `episodes=None` makes Sonarr's episode list unreadable and `episodes=False`
    an empty one; `movie_read_errors` fails that many Radarr movie reads first.
    """

    def __init__(self, queue, imports_at=None, other_imports_at=None, episodes=True,
                 movie_read_errors=0):
        self.queue = list(queue)
        self.imports_at, self.other_imports_at = imports_at, other_imports_at
        self.episodes, self.movie_read_errors = episodes, movie_read_errors
        self.imported = self.other_imported = False
        self.queue_reads = 0

    def get(self, _base, _key, path, timeout=10):
        if path.startswith('/api/v3/queue'):
            n = self.queue_reads
            self.queue_reads += 1
            if self.imports_at is not None and n >= self.imports_at:
                self.imported = True
            if self.other_imports_at is not None and n >= self.other_imports_at:
                self.other_imported = True
            frame = [] if self.imported else self.queue[min(n, len(self.queue) - 1)]
            if isinstance(frame, Exception):
                raise frame
            return {'page': 1, 'pageSize': 250, 'totalRecords': len(frame), 'records': list(frame)}
        if path.startswith('/api/v3/movie/'):
            if self.movie_read_errors:
                self.movie_read_errors -= 1
                raise OSError('radarr timed out')
            return {'id': 7, 'path': '/movies/Film (2026)',
                    'movieFileId': 900 if self.imported else 800}
        if path.startswith('/api/v3/series/'):
            return {'id': 1, 'path': '/tv/Show'}
        if path.startswith('/api/v3/episode?'):
            if self.episodes is None:
                raise OSError('episode list timed out')
            if self.episodes is False:
                return []
            return [{'id': 101, 'seasonNumber': 1, 'episodeNumber': 1,
                     'episodeFileId': 777 if self.imported else 501},
                    {'id': 102, 'seasonNumber': 1, 'episodeNumber': 2,
                     'episodeFileId': 778 if self.other_imported else 502}]
        if path.startswith('/api/v3/episodefile'):
            return [{'id': 777 if self.imported else 501},
                    {'id': 778 if self.other_imported else 502}]
        raise AssertionError(f'a read the watch has no business making: {path}')


def _episode(state='importPending', status='completed', download='HASH', **kw):
    """A Sonarr queue record, as `QueueResource` spells one."""
    return {'seriesId': 1, 'episodeId': 101, 'seasonNumber': 1, 'status': status,
            'trackedDownloadState': state, 'downloadId': download,
            'outputPath': '/downloads/Show.S01', 'title': 'Show.S01E01.1080p',
            'statusMessages': [], **kw}


def _movie(state='downloading', status='downloading', download='AAAA', **kw):
    return {'movieId': 7, 'status': status, 'trackedDownloadState': state,
            'downloadId': download, 'outputPath': '/downloads/Film.2026',
            'title': 'Film.2026.1080p', 'statusMessages': [], **kw}


def _watch(body, queue=None, **arr_kw):
    the_arr = _Arr(queue if queue is not None else [[_episode()]], **arr_kw)
    clock = _Clock()
    _Sync.scans = []
    force = MagicMock(side_effect=lambda *a, **k: setattr(the_arr, 'imported', True))
    with patch.object(app, 'AUDITORR_SECRET', ''), \
         patch.object(app, 'AUDITORR_REQUIRE_AUTH', False), \
         patch.object(app, 'db_load_config', return_value=_cfg()), \
         patch.object(app, 'db_update_meta'), \
         patch.object(app.threading, 'Thread', _Sync), \
         patch('arr._arr_get', side_effect=the_arr.get), \
         patch('time.monotonic', clock.monotonic), \
         patch('time.sleep', clock.sleep), \
         patch.object(app, 'force_manual_import_by_id', force), \
         patch.object(app, 'nudge_watchdog') as nudge, \
         patch.object(app, 'try_start_scanning', return_value=True) as scan:
        res = app.app.test_client().post('/api/workflows/watch_import', json=body,
                                         environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert res.status_code == 200
    return SimpleNamespace(watch=app._import_watches[res.get_json()['job_id']], force=force,
                           nudge=nudge, scan=scan, scans=list(_Sync.scans), arr=the_arr,
                           clock=clock)


_SONARR = {'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1,
           'title': 'Show', 'files': 1, 'file_ids': [501]}
_RADARR = {'service': 'radarr', 'connection_id': 'radarr-mv', 'arr_id': 7,
           'title': 'Film', 'files': 1}


def test_the_watch_scopes_its_force_import_to_the_candidates_episodes():
    out = _watch(_SONARR)

    assert out.force.call_args.kwargs['only_episode_ids'] == [101]
    assert out.force.call_args.kwargs['media_folder_fallback'] is False
    assert out.watch['status'] == 'done'


def test_no_download_location_means_no_force_import():
    """B11's honest failure: nothing to import from is reported, not replaced
    by the library folder."""
    out = _watch(_SONARR, queue=[[_episode(download='', outputPath='')]])

    out.force.assert_not_called()
    assert out.watch['status'] == 'error'
    assert 'did not report where' in out.watch['message']


def test_a_sonarr_watch_without_file_ids_never_force_imports():
    """An older bundle sends no ids. Unknown scope is not the whole download."""
    out = _watch({**_SONARR, 'file_ids': None})

    out.force.assert_not_called()
    assert out.watch['status'] == 'error'


def test_an_unanswerable_episode_list_never_force_imports():
    """Could not ask which episodes the files hold: not a licence to import all."""
    out = _watch(_SONARR, episodes=None)

    out.force.assert_not_called()
    assert out.watch['status'] == 'error'


def test_file_ids_that_join_to_nothing_never_force_import():
    out = _watch(_SONARR, episodes=False)

    out.force.assert_not_called()
    assert out.watch['status'] == 'error'


def test_a_radarr_watch_needs_no_episode_scope_but_keeps_the_fallback_off():
    out = _watch(_RADARR, queue=[[_movie(state='importPending', status='completed')]])

    assert out.force.call_args.kwargs['only_episode_ids'] is None
    assert out.force.call_args.kwargs['media_folder_fallback'] is False


# ── S07: a success needs an observation ──────────────────────────────────────
#
# `poll_queue_until_clear` answered `[]` for no connection, a failed download, a
# download that cleared, one never seen, and a window in which every read
# failed — and the watch turned every `[]` into "Imported successfully".

def test_an_unreadable_queue_is_never_imported_successfully():
    out = _watch(_RADARR, queue=[OSError('radarr timed out')])

    assert out.watch['status'] == 'unreadable'
    assert 'successfully' not in out.watch['message']
    out.force.assert_not_called()


def test_a_download_that_never_appears_is_not_success():
    out = _watch(_RADARR, queue=[[]])

    assert out.watch['status'] == 'unobserved'
    out.force.assert_not_called()


def test_a_failed_download_is_not_success():
    failed = _movie(state='failed', status='failed',
                    statusMessages=[{'title': 'Film', 'messages': ['Tracker returned 404']}])
    out = _watch(_RADARR, queue=[[failed]])

    assert out.watch['status'] == 'failed'
    assert 'Tracker returned 404' in out.watch['message']
    out.force.assert_not_called()


def test_a_cleared_queue_with_no_new_file_is_not_success():
    """Seen downloading, then gone — removed in the arr, or blocklisted — and the
    movie's file is the one the watch started from."""
    out = _watch(_RADARR, queue=[[_movie()], [_movie()], []])

    assert out.watch['status'] == 'no_new_file'
    out.force.assert_not_called()


def test_a_still_downloading_item_is_never_force_imported():
    """After the 300 s poll and the 7,200 s extended wait, a download still in
    progress fell through to three force imports of a file being written."""
    out = _watch(_RADARR, queue=[[_movie()]])

    out.force.assert_not_called()
    assert out.watch['status'] == 'timed_out'


def test_success_needs_the_targets_file_id_to_change():
    """A queue that clears is not an import. Radarr: the movie's file id.
    Sonarr: the file ids of the episodes this watch is for — the series' ids move
    whenever Sonarr imports *any* episode of it."""
    landed = _watch(_RADARR, queue=[[_movie()]], imports_at=1)
    assert landed.watch['status'] == 'done'
    assert 'successfully' in landed.watch['message']

    other_episode = _watch(_SONARR, queue=[[_episode(state='downloading', status='downloading')], []],
                           other_imports_at=1)
    # The queue cleared and the series' file ids moved — for another episode. The
    # one this watch is for never changed file.
    assert other_episode.watch['status'] == 'no_new_file'

    episode_landed = _watch(_SONARR, queue=[[_episode(state='downloading', status='downloading')]],
                            imports_at=1)
    assert episode_landed.watch['status'] == 'done'


def test_another_download_for_the_series_does_not_stand_in():
    """Correlation was by movie or series id, on the first queue page, taking
    the first record: another download of the same title supplied the download
    id a force import then used. A search result's `infoHash` is the download's
    identity — the arr's queue spells it `downloadId`, upper-cased for
    qBittorrent — and a record that is not this grab is not this grab."""
    someone_else = _movie(state='importPending', status='completed', download='BBBB')
    alone = _watch(dict(_RADARR, info_hash='aaaa'), queue=[[someone_else]])
    alone.force.assert_not_called()
    assert alone.watch['status'] != 'done'

    mine = _movie(state='importBlocked', status='completed', download='AAAA')
    both = _watch(dict(_RADARR, info_hash='aaaa'), queue=[[someone_else, mine]])
    assert both.force.call_args.kwargs['download_id'] == 'AAAA'


def test_two_downloads_with_no_identity_to_tell_them_apart_are_not_force_imported():
    """With no info hash the watch can only match on the movie — and with two
    downloads for it in the queue, the first record is a guess."""
    out = _watch(_RADARR, queue=[[_movie(state='importPending', status='completed', download='AAAA'),
                                  _movie(state='importPending', status='completed', download='BBBB')]])

    out.force.assert_not_called()
    assert out.watch['status'] == 'error'
    assert 'more than one download' in out.watch['message'].lower()


def test_an_import_blocked_download_is_force_imported_without_the_two_hour_wait():
    """Found writing Phase 12, against both arrs' `CompletedDownloadService`:
    Sonarr v4 and Radarr v5 park a rejected import — not an upgrade, which is a
    backfill's usual case — as `importBlocked`, not `importPending`. The watch
    only recognised `importPending`, so a parked backfill waited out the whole
    300 s poll and the 7,200 s extended wait before its force import."""
    out = _watch(_RADARR, queue=[[_movie(state='importBlocked', status='completed')]])

    out.force.assert_called_once()
    assert out.watch['status'] == 'done'
    assert out.clock.now < 600


def test_a_baseline_that_could_not_be_read_never_reports_done():
    """Success is a file that changed. With no reading of the file before the
    download, a change cannot be seen — the watch says what it did see."""
    out = _watch(_RADARR, queue=[[_movie()]], imports_at=1, movie_read_errors=1)

    assert out.watch['status'] == 'unconfirmed'


def test_a_failed_trump_watch_nudges_the_watchdog_and_does_not_rescan():
    """TR10's re-audit waits on the import. A false "done" started it at the
    wrong moment; a watch that ends in anything but an observed import leaves the
    scan to the watchdog, like every other client action."""
    out = _watch(dict(_RADARR, source='trump'),
                 queue=[[_movie(state='failed', status='failed')]])

    out.nudge.assert_called_once()
    out.scan.assert_not_called()
    assert out.scans == []


def test_a_trump_watch_that_sees_its_import_starts_the_re_audit():
    out = _watch(dict(_RADARR, source='trump'), queue=[[_movie()]], imports_at=1)

    assert out.watch['status'] == 'done'
    out.scan.assert_called_once_with('trump')
    out.nudge.assert_not_called()
