import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sources
from audit import run_audit_process


def _minimal_cfg():
    return {
        'TORRENT_SOURCE': 'qui',
        'QB_HOST': 'http://localhost:8080',
        'QB_USER': 'admin',
        'QB_PASS': 'password',
        'LOCAL_PATH': '/data/torrents',
        'MEDIA_PATH': '/data/media',
        'REMOTE_PATH': '/data/torrents',
        'OR_RATIO': 0.01,
        'NI_RATIO': 0.01,
        'DUP_RATIO': 0.01,
        'EXCLUSION_PATTERNS': [],
    }


@patch('audit.db_save_audit')
@patch('audit._save_error_status')
@patch('audit.db_load_config')
@patch('audit.sources.fetch_file_map')
def test_run_audit_process_can_suppress_transient_source_error_persistence(
    mock_fetch, mock_load_config, mock_save_error_status, mock_save_audit
):
    mock_load_config.return_value = _minimal_cfg()
    mock_fetch.side_effect = sources.SourceConnectionError('qui connection error: not ready')

    run_audit_process('startup', persist_source_errors=False)

    mock_save_error_status.assert_not_called()
    mock_save_audit.assert_not_called()


@patch('audit.db_save_audit')
@patch('audit._save_error_status')
@patch('audit.db_load_config')
@patch('audit.sources.fetch_file_map')
def test_run_audit_process_persists_source_errors_by_default(
    mock_fetch, mock_load_config, mock_save_error_status, mock_save_audit
):
    mock_load_config.return_value = _minimal_cfg()
    mock_fetch.side_effect = sources.SourceConnectionError('qui connection error: still down')

    run_audit_process('manual')

    mock_save_error_status.assert_called_once_with('qui connection error: still down')
    mock_save_audit.assert_called_once()
