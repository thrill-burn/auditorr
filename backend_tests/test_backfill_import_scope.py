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
"""
import json
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
    """The watch thread, run inline so its decisions can be asserted on."""

    def __init__(self, target=None, **_kw):
        self.target = target

    def start(self):
        self.target()


_PENDING = {'trackedDownloadState': 'importPending', 'downloadId': 'HASH',
            'outputPath': '/downloads/Show.S01', 'statusMessages': []}


_JOIN = {501: [(101, 1, 1)], 502: [(102, 1, 2)]}


def _watch(body, queue=(_PENDING,), episodes=_JOIN):
    """`episodes` is `sonarr_episodes_by_file`'s answer: None is "could not ask"."""
    fmi = MagicMock()
    polls = iter([list(queue), [], [], []])
    by_file = episodes
    with patch.object(app, 'AUDITORR_SECRET', ''), \
         patch.object(app, 'AUDITORR_REQUIRE_AUTH', False), \
         patch.object(app, 'db_load_config', return_value=_cfg()), \
         patch.object(app, 'db_update_meta'), \
         patch.object(app.threading, 'Thread', _Sync), \
         patch.object(app.time, 'sleep'), \
         patch.object(app, 'get_arr_file_id', return_value=[1]), \
         patch.object(app, 'poll_queue_until_clear', side_effect=lambda *a, **k: next(polls)), \
         patch.object(app, 'sonarr_episodes_by_file', return_value=by_file), \
         patch.object(app, 'force_manual_import_by_id', fmi):
        res = app.app.test_client().post('/api/workflows/watch_import', json=body,
                                         environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert res.status_code == 200
    return fmi, app._import_watches[res.get_json()['job_id']]


_SONARR = {'service': 'sonarr', 'connection_id': 'sonarr-tv', 'arr_id': 1,
           'title': 'Show', 'files': 1, 'file_ids': [501]}


def test_the_watch_scopes_its_force_import_to_the_candidates_episodes():
    fmi, watch = _watch(_SONARR)

    assert fmi.call_args.kwargs['only_episode_ids'] == [101]
    assert fmi.call_args.kwargs['media_folder_fallback'] is False
    assert watch['status'] == 'done'


def test_no_download_location_means_no_force_import():
    """B11's honest failure: nothing to import from is reported, not replaced
    by the library folder."""
    fmi, watch = _watch(_SONARR, queue=({'trackedDownloadState': 'importPending'},))

    fmi.assert_not_called()
    assert watch['status'] == 'error'
    assert 'did not report where' in watch['message']


def test_a_sonarr_watch_without_file_ids_never_force_imports():
    """An older bundle sends no ids. Unknown scope is not the whole download."""
    fmi, watch = _watch({**_SONARR, 'file_ids': None})

    fmi.assert_not_called()
    assert watch['status'] == 'error'


def test_an_unanswerable_episode_list_never_force_imports():
    """Could not ask which episodes the files hold: not a licence to import all."""
    fmi, watch = _watch(_SONARR, episodes=None)

    fmi.assert_not_called()
    assert watch['status'] == 'error'


def test_file_ids_that_join_to_nothing_never_force_import():
    fmi, watch = _watch(_SONARR, episodes={})

    fmi.assert_not_called()
    assert watch['status'] == 'error'


def test_a_radarr_watch_needs_no_episode_scope_but_keeps_the_fallback_off():
    fmi, _watch_state = _watch({'service': 'radarr', 'connection_id': 'radarr-mv',
                                'arr_id': 7, 'title': 'Film', 'files': 1})

    assert fmi.call_args.kwargs['only_episode_ids'] is None
    assert fmi.call_args.kwargs['media_folder_fallback'] is False
