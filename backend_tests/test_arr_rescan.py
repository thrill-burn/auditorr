"""Triage's "Trigger Rescan" hands Sonarr/Radarr a path and an import mode, then
has to report what the arr actually decided.

All three were wrong for the cases Triage exists to fix. dirname() handed over
the download root for a single-file torrent (the arr parses "radarr" as a
release name and imports nothing). The default import mode moves the file out
from under the seeding torrent. And the command endpoint reports 'completed'
whether it imported everything or refused everything, so a refusal surfaced as
a green success toast.
"""
import json
from unittest.mock import patch

import pytest

from arr import arr_rescan, force_manual_import_by_id, import_rejections

_CONN = {'base_url': 'http://radarr:7878', 'api_key': 'k', 'id': 'radarr-default'}


def _cfg(local='/data/torrents', remote='/data/torrents'):
    return {
        'RADARR_URL': 'http://radarr:7878', 'RADARR_API_KEY': 'k', 'RADARR_REMOTE_PATH': remote,
        'SONARR_URL': 'http://sonarr:8989', 'SONARR_API_KEY': 'k', 'SONARR_REMOTE_PATH': remote,
        'LOCAL_PATH': local, 'MEDIA_PATH': '',
    }


@pytest.fixture(autouse=True)
def _no_import_probe(monkeypatch):
    """Default: no probe. Individual tests override to assert on the reporting."""
    monkeypatch.setattr('arr.import_rejections', lambda *a, **k: None)


# ---------------------------------------------------------------------------
# Which path the arr is told to scan
# ---------------------------------------------------------------------------

@patch('arr._arr_command')
def test_single_file_torrent_scans_the_file_not_the_category_dir(mock_cmd):
    arr_rescan(_cfg(), 'radarr', ['radarr/Disclosure.Day.2026.2160p.WEB-DL.mkv'])
    assert mock_cmd.call_args[0][3] == '/data/torrents/radarr/Disclosure.Day.2026.2160p.WEB-DL.mkv'


@patch('arr._arr_command')
def test_release_folder_is_used_when_the_file_sits_in_one(mock_cmd):
    """A season pack must scan as a folder so the whole pack imports at once."""
    arr_rescan(_cfg(), 'sonarr', ['sonarr/Show.S01.1080p.WEB-DL/Show.S01E01.mkv'])
    assert mock_cmd.call_args[0][3] == '/data/torrents/sonarr/Show.S01.1080p.WEB-DL'


@patch('arr._arr_command')
def test_nested_pack_stops_at_the_release_folder(mock_cmd):
    """"Season 1" identifies a title no better than "sonarr" does."""
    arr_rescan(_cfg(), 'sonarr', ['sonarr/Show.S01/Season 1/Show.S01E01.mkv'])
    assert mock_cmd.call_args[0][3] == '/data/torrents/sonarr/Show.S01'


@patch('arr._arr_command')
def test_remote_path_translation_still_applies(mock_cmd):
    arr_rescan(_cfg(remote='/downloads'), 'radarr', ['radarr/Film.2026.mkv'])
    assert mock_cmd.call_args[0][3] == '/downloads/radarr/Film.2026.mkv'


@patch('arr.urllib.request.urlopen')
def test_scan_command_asks_for_copy_never_move(mock_open):
    """Auto with no download-client id means move — it would break the seed."""
    arr_rescan(_cfg(), 'radarr', ['radarr/Film.2026.mkv'])
    body = json.loads(mock_open.call_args[0][0].data)
    assert body['name'] == 'DownloadedMoviesScan'
    assert body['importMode'] == 'Copy'


# ---------------------------------------------------------------------------
# Reporting what the arr decided
# ---------------------------------------------------------------------------

@patch('arr._arr_command')
def test_rejection_is_reported_against_the_path(mock_cmd, monkeypatch):
    monkeypatch.setattr('arr.import_rejections', lambda conn, folder, **k: [
        {'path': '/data/torrents/radarr/Film.2026.mkv',
         'rejections': ['Not a quality revision upgrade for existing movie file(s)']},
    ])
    out = arr_rescan(_cfg(), 'radarr', ['radarr/Film.2026.mkv'])
    assert out['count'] == 1
    assert out['results'][0]['rejections'] == ['Not a quality revision upgrade for existing movie file(s)']
    assert out['results'][0]['checked'] is True
    # The command still fires — a rejection is the arr's call, not ours to pre-empt
    assert mock_cmd.called


@patch('arr._arr_command')
def test_clean_import_reports_no_rejections(mock_cmd, monkeypatch):
    monkeypatch.setattr('arr.import_rejections', lambda conn, folder, **k: [
        {'path': '/data/torrents/radarr/Film.2026.mkv', 'rejections': []},
    ])
    out = arr_rescan(_cfg(), 'radarr', ['radarr/Film.2026.mkv'])
    assert out['results'][0]['rejections'] == []


@patch('arr._arr_command')
def test_neighbouring_files_in_the_category_dir_are_not_confused(mock_cmd, monkeypatch):
    """One probe answers for a whole category dir, so rows must be matched by path."""
    monkeypatch.setattr('arr.import_rejections', lambda conn, folder, **k: [
        {'path': '/data/torrents/radarr/Other.Film.2020.mkv', 'rejections': ['Not an upgrade']},
        {'path': '/data/torrents/radarr/Film.2026.mkv',       'rejections': []},
    ])
    out = arr_rescan(_cfg(), 'radarr', ['radarr/Film.2026.mkv'])
    assert out['results'][0]['rejections'] == []


@patch('arr._arr_command')
def test_file_the_arr_cannot_see_is_reported_honestly(mock_cmd, monkeypatch):
    monkeypatch.setattr('arr.import_rejections', lambda conn, folder, **k: [])
    out = arr_rescan(_cfg(), 'radarr', ['radarr/Film.2026.mkv'])
    assert out['results'][0]['rejections'] == ['Radarr does not list this file as importable']


@patch('arr._arr_command')
def test_failed_probe_reports_unchecked_rather_than_guessing(mock_cmd):
    out = arr_rescan(_cfg(), 'radarr', ['radarr/Film.2026.mkv'])
    assert out['results'][0]['checked'] is False
    assert out['results'][0]['rejections'] == []


# ---------------------------------------------------------------------------
# import_rejections — reading the arr's decision without acting on it
# ---------------------------------------------------------------------------

def test_probe_reports_spec_rejections():
    rows = [{'path': '/x/f.mkv', 'movie': {'id': 7},
             'rejections': [{'reason': 'Not an upgrade for existing movie file.'}]}]
    with patch('arr._arr_get', return_value=rows):
        assert import_rejections(_CONN, '/x') == [
            {'path': '/x/f.mkv', 'rejections': ['Not an upgrade for existing movie file.']}]


def test_probe_flags_a_file_the_arr_could_not_identify():
    """No title attached means no specs ran — silence there is not approval."""
    with patch('arr._arr_get', return_value=[{'path': '/x/f.mkv', 'movie': None, 'rejections': []}]):
        out = import_rejections(_CONN, '/x')
    assert out[0]['rejections'] == ['No matching title — the file could not be identified']


def test_probe_failure_is_none_not_an_empty_verdict():
    """None means "could not check"; [] means "checked, the arr offered nothing"."""
    with patch('arr._arr_get', side_effect=OSError('connection refused')):
        assert import_rejections(_CONN, '/x') is None


# ---------------------------------------------------------------------------
# Force import — only_paths keeps a category-dir lookup from sweeping neighbours
# ---------------------------------------------------------------------------

def test_only_paths_restricts_the_import_to_the_selected_file():
    rows = [
        {'path': '/data/torrents/radarr/Film.2026.mkv',       'quality': {}, 'languages': []},
        {'path': '/data/torrents/radarr/Unrelated.2019.mkv',  'quality': {}, 'languages': []},
    ]
    with patch('arr._arr_get') as get, patch('arr.urllib.request.urlopen') as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = b'{}'
        get.side_effect = lambda base, key, path, **k: (
            {'path': '/movies/Film (2026)'} if path.startswith('/api/v3/movie/') else rows
        )
        force_manual_import_by_id(_cfg(), 'radarr', 'radarr-default', 7,
                                  download_folder='/data/torrents/radarr',
                                  only_paths=['/data/torrents/radarr/Film.2026.mkv'])
        body = json.loads(urlopen.call_args[0][0].data)
    assert body['name'] == 'ManualImport'
    assert body['replaceExistingFiles'] is True
    assert [f['path'] for f in body['files']] == ['/data/torrents/radarr/Film.2026.mkv']


def test_force_import_asks_for_copy_so_the_seed_survives():
    """No tracked download stands behind a Triage item, so Auto would mean move."""
    from arr import force_import_files
    with patch('arr.force_manual_import_by_id') as fmi, patch('arr.time.sleep'), \
         patch('arr.get_arr_file_id', side_effect=[11, 42]):
        force_import_files(_cfg(), 'radarr', 'radarr-default', 7, ['radarr/Film.2026.mkv'])
    assert fmi.call_args.kwargs['import_mode'] == 'Copy'
    assert fmi.call_args.kwargs['only_paths'] == ['/data/torrents/radarr/Film.2026.mkv']


def test_import_is_confirmed_by_the_file_id_moving():
    from arr import force_import_files
    ids = iter([11, 11, 42])   # before, first poll (unchanged), second poll (replaced)
    with patch('arr.force_manual_import_by_id'), patch('arr.time.sleep'), \
         patch('arr.get_arr_file_id', side_effect=lambda *a: next(ids)):
        out = force_import_files(_cfg(), 'radarr', 'radarr-default', 7, ['radarr/Film.2026.mkv'])
    assert out['imported'] is True


def test_unreadable_baseline_never_reports_success():
    """0 ("no file") differs from None too — a missing baseline proves nothing."""
    from arr import force_import_files
    with patch('arr.force_manual_import_by_id'), patch('arr.time.sleep'), \
         patch('arr.get_arr_file_id', return_value=None):
        out = force_import_files(_cfg(), 'radarr', 'radarr-default', 7, ['radarr/Film.2026.mkv'])
    assert out['imported'] is False
    assert 'did not report its current file' in out['message']


def test_only_paths_matching_nothing_fails_loudly():
    with patch('arr._arr_get') as get, patch('arr.time.sleep'):
        get.side_effect = lambda base, key, path, **k: (
            {'path': '/movies/Film (2026)'} if path.startswith('/api/v3/movie/')
            else [{'path': '/data/torrents/radarr/Unrelated.2019.mkv'}]
        )
        with pytest.raises(ValueError, match='does not list the selected file'):
            force_manual_import_by_id(_cfg(), 'radarr', 'radarr-default', 7,
                                      download_folder='/data/torrents/radarr',
                                      only_paths=['/data/torrents/radarr/Film.2026.mkv'])
