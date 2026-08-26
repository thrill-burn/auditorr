"""Triage's "Trigger Rescan" hands Sonarr/Radarr a path and an import mode.

Both were wrong for the case Triage exists to fix: a single-file torrent sitting
directly in its category dir. dirname() handed over the download root (the arr
parses "radarr" as a release name and imports nothing), and the default import
mode moves the file out from under the seeding torrent.
"""
import json
from unittest.mock import patch

from arr import arr_rescan


def _cfg(service='radarr', local='/data/torrents', remote='/data/torrents'):
    return {
        'RADARR_URL': 'http://radarr:7878', 'RADARR_API_KEY': 'k', 'RADARR_REMOTE_PATH': remote,
        'SONARR_URL': 'http://sonarr:8989', 'SONARR_API_KEY': 'k', 'SONARR_REMOTE_PATH': remote,
        'LOCAL_PATH': local, 'MEDIA_PATH': '',
    }


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
def test_nested_release_folder_stops_at_two_segments(mock_cmd):
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
