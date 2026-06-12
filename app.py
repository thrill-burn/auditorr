import os
import re
import json
import time
import random
import socket
import threading
import logging
import secrets
import functools
import urllib.parse
import urllib.error
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import sources
from db import (
    DATA_DIR,
    init_db,
    db_load_config, db_save_config, validate_config,
    db_load_results, db_save_results,
    db_load_file_results, db_stream_file_results,
    db_save_history,
    db_get_recent_runs,
    db_clear_audit_history,
    db_get_upload_snapshots,
    db_retag_upload_snapshots, db_count_upload_snapshots_by_source,
    db_delete_upload_snapshots,
    db_get_change_log,
    db_save_audit,
    db_get_meta, db_set_meta, db_delete_meta,
)
from state import get_state, set_state, try_start_scanning
from audit import run_audit_process, process_health_metrics, compute_upload_stats, _is_not_imported_torrent
from arr import _test_arr_connection, arr_rescan, arr_search, fetch_arr_media_index, test_arr_connections, fetch_arr_indexers, fetch_release_matrix, grab_release, normalize_arr_connections, poll_queue_until_clear, force_manual_import_by_id, get_arr_file_id, parse_release_info_for_path, fetch_arr_all_titles, title_match_keys
from scripts import generate_script, _build_dup_groups
from media_server_exclusions import normalize_disc_rip_presets, normalize_media_server_presets
from watchdog_handler import restart_watchdog, start_watchdog, _scheduled_audit_loop
from debug import install_ring_buffer, build_debug_report, memory_pressure, cgroup_oom_events

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

APP_VERSION = "1.6.1"

# Keep the last ~400 log records in memory for /api/debug/report
install_ring_buffer()

# Native crashes (SIGSEGV/SIGABRT) dump Python tracebacks to stderr → docker logs
import faulthandler
faulthandler.enable()


def _log_thread_exception(args):
    # Default excepthook prints to stderr only, bypassing the ring buffer —
    # route background-thread crashes (audit, watchdog, workflows) into logging
    # so they appear in /api/debug/report.
    log.error("Unhandled exception in thread '%s'",
              getattr(args.thread, 'name', '?'),
              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


threading.excepthook = _log_thread_exception

app = Flask(__name__, static_folder='frontend/dist', static_url_path='')
# Allow requests from localhost and private network ranges (LAN self-hosting).
# Wildcard CORS would allow any website to probe this server from a visitor's browser.
_CORS_ORIGINS = os.environ.get('CORS_ORIGINS', '').strip()
CORS(app, origins=_CORS_ORIGINS.split(',') if _CORS_ORIGINS else [
    "http://localhost:8677", "http://127.0.0.1:8677",
    # Accept any private-range origin by regex — flask-cors supports this
    r"http://192\.168\.\d+\.\d+(:\d+)?",
    r"http://10\.\d+\.\d+\.\d+(:\d+)?",
    r"http://172\.(1[6-9]|2\d|3[01])\.\d+\.\d+(:\d+)?",
])

AUDITORR_PORT   = int(os.environ.get('AUDITORR_PORT', 8677))
AUDITORR_SECRET = os.environ.get('AUDITORR_SECRET', '').strip()

# Initialise DB tables and run JSON migrations on import
init_db()

# Start the scheduled fallback audit loop
threading.Thread(target=_scheduled_audit_loop, daemon=True).start()

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not AUDITORR_SECRET:
            return f(*args, **kwargs)
        provided = (
            request.headers.get('X-Auditorr-Secret') or
            request.args.get('secret') or
            ''
        )
        if not secrets.compare_digest(provided, AUDITORR_SECRET):
            return jsonify({"status": "error", "message": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def _is_source_error_status(status):
    if not isinstance(status, str):
        return False
    return status.startswith((
        'qBittorrent error',
        'qBittorrent connection error',
        'qui error',
        'qui connection error',
        'qui HTTP error',
    ))

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _torrent_source_configured(cfg):
    source = cfg.get('TORRENT_SOURCE', 'qbit')
    host_key = 'QUI_HOST' if source == 'qui' else 'QB_HOST'
    return bool(cfg.get(host_key))


def _handle_aborted_scan():
    """Detect a scan that never finished because the process died mid-run.

    The audit writes a scan marker at start and removes it on any normal exit
    (success or handled error). A marker still present at boot means the
    previous process was killed — almost always the container being OOM-killed
    or restarted — so record an 'aborted' audit run (visible in Audit History)
    and bump the consecutive-abort streak. Returns the current streak.
    """
    marker = db_get_meta('scan_marker')
    if not marker:
        return 0
    streak = int(db_get_meta('consecutive_aborted_scans', 0) or 0) + 1
    db_set_meta('consecutive_aborted_scans', streak)
    db_set_meta('last_aborted_scan', {**marker, 'detected_at': datetime.now().isoformat(timespec='seconds')})
    db_delete_meta('scan_marker')

    # The kernel's per-cgroup oom_kill counter survives worker restarts (it only
    # resets when the container is recreated), so an increment here is hard
    # evidence the death was an OOM kill rather than e.g. a manual restart.
    oom_note = ""
    events = cgroup_oom_events() or {}
    count  = events.get('oom_kill') or 0
    prev   = int(db_get_meta('oom_kill_counter', 0) or 0)
    if count != prev:
        db_set_meta('oom_kill_counter', count)
    if count > prev:
        new_kills = count - prev
        db_set_meta('oom_kills_attributed',
                    int(db_get_meta('oom_kills_attributed', 0) or 0) + new_kills)
        oom_note = (f" The container cgroup recorded {new_kills} OOM kill(s) since the last "
                    f"boot — confirmed out-of-memory.")

    # Prefer the sampler's last reading (≤20s before death) over the last phase transition
    rss = marker.get('last_rss_mb') or marker.get('rss_mb', '?')
    msg = (f"Scan never finished — the process died during '{marker.get('phase', 'unknown phase')}' "
           f"(started {marker.get('started_at', '?')}, last observed rss {rss} MB)."
           f"{oom_note} "
           f"This usually means the container was killed mid-scan (out-of-memory) or restarted. "
           f"See /api/debug/report.")
    log.warning(f"Aborted scan detected (streak: {streak}): {msg}")
    try:
        db_save_audit(marker.get('trigger', 'unknown'), None, 'aborted', msg, {},
                      source=db_load_config().get('TORRENT_SOURCE', 'qbit'))
    except Exception as e:
        log.warning(f"Could not record aborted audit run: {e}")
    return streak


def _run_startup_audit():
    """Run the startup audit, retrying once after 60s on connection errors (other containers may not be ready)."""
    if not _torrent_source_configured(db_load_config()):
        log.info("Torrent source not configured, skipping startup audit.")
        return
    if try_start_scanning("startup"):
        run_audit_process("startup", persist_source_errors=False)
    state = get_state()
    if state.get('last_scan_status') == 'error' and _is_source_error_status(state.get('status_message', '')):
        log.warning("Startup audit failed with connection error, retrying in 60s before recording a failure...")
        time.sleep(60)
        if try_start_scanning("startup"):
            run_audit_process("startup", persist_source_errors=True)


def _startup_sequence():
    mp = memory_pressure()
    log.info(f"Memory at boot: rss={mp['rss_mb']} MB, "
             f"container limit={mp['container'].get('limit_mb')} MB, "
             f"host available={mp['host_available_mb']} MB, "
             f"cgroup oom_kill events={(mp['cgroup_oom_events'] or {}).get('oom_kill')}")
    streak = _handle_aborted_scan()
    if streak >= 2:
        # Crash-loop breaker: the last scans all died mid-run. Re-running the
        # startup audit on every boot would hammer the disk for hours and crash
        # again — surface the problem instead and wait for a manual scan.
        warn = (f"Automatic scans paused: the last {streak} scans were killed mid-run "
                f"(likely out-of-memory). Start a scan manually when ready; a completed "
                f"scan re-enables automatic scanning. See /api/debug/report for details.")
        log.warning(warn)
        set_state(status_message=warn, last_scan_status="error")
        return
    _run_startup_audit()
    start_watchdog()


def startup():
    # Use a lock file to ensure only one gunicorn worker runs the startup audit.
    # Both workers import the module and hit this code, but only the first one
    # to acquire the exclusive lock proceeds.
    lock_file = os.path.join(DATA_DIR, 'startup.lock')
    try:
        import fcntl
        with open(lock_file, 'w') as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                log.info("Startup audit already running in another worker, skipping.")
                return
            _startup_sequence()
    except ImportError:
        # fcntl not available (Windows) — just run without locking
        _startup_sequence()

threading.Thread(target=startup, daemon=True).start()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/health')
def health_check():
    return jsonify({"status": "ok", "version": APP_VERSION}), 200


@app.route('/api/debug/report')
@require_auth
def debug_report():
    """Privacy-scrubbed diagnostic dump for bug reports — safe to paste publicly.

    No credentials; hosts/IPs/tokens redacted; media file and folder names
    replaced with stable short hashes.
    """
    return jsonify(build_debug_report(APP_VERSION))


@app.route('/api/results')
@require_auth
def get_results():
    return jsonify(db_load_results())


@app.route('/api/files')
@require_auth
def get_files():
    tab = request.args.get('tab', '')
    if tab not in ('media', 'torrents'):
        return jsonify({"error": "tab must be 'media' or 'torrents'"}), 400
    # Stream the stored JSON straight through without parsing it — for 500K+
    # file libraries json.loads + jsonify here costs minutes of CPU and several
    # GB of RAM, and a request that slow can stall the whole UI.
    return app.response_class(db_stream_file_results(tab), mimetype='application/json')


@app.route('/api/progress')
@require_auth
def get_progress():
    return jsonify(get_state())


@app.route('/api/changes')
@require_auth
def get_changes():
    # Use audit_runs for timestamps (cheap — no blob data) and change_log for the pre-computed diff.
    # This avoids deserializing two 300MB+ audit snapshots on every request.
    ok_runs = [r for r in db_get_recent_runs(limit=10) if r['status'] == 'ok']
    if len(ok_runs) < 2:
        return jsonify({"changes": None, "message": "Not enough audit history yet."})
    curr_ran_at = ok_runs[0]['ran_at']
    prev_ran_at = ok_runs[1]['ran_at']
    entries = db_get_change_log(limit=1)
    diff = entries[0]['diff'] if (entries and entries[0]['ran_at'] == curr_ran_at) else None
    return jsonify({"changes": diff, "prev_ran_at": prev_ran_at, "curr_ran_at": curr_ran_at})


@app.route('/api/change_log')
@require_auth
def get_change_log():
    return jsonify({"entries": db_get_change_log()})


@app.route('/api/audit_history')
@require_auth
def get_audit_history():
    return jsonify({"runs": db_get_recent_runs()})


@app.route('/api/clear_history', methods=['POST'])
@require_auth
def clear_history():
    """Delete all audit run history and snapshots from SQLite and reset history stats."""
    db_clear_audit_history()
    db_save_history({"hourly_stats": [], "daily_stats": []})
    # Also clear the dashboard history chart from results
    curr = db_load_results()
    if curr.get('dashboard'):
        curr['dashboard']['history_chart'] = []
        curr['dashboard']['trend'] = None
        db_save_results(curr)
    return jsonify({"status": "success"})


@app.route('/api/start_scan', methods=['POST'])
@require_auth
def start_scan():
    if try_start_scanning("manual"):
        threading.Thread(target=run_audit_process, args=("manual",), daemon=True).start()
    return jsonify({"status": "started"})


@app.route('/api/config', methods=['GET', 'POST'])
@require_auth
def handle_config():
    if request.method == 'POST':
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data"}), 400

        errors = validate_config(data)
        if errors:
            return jsonify({"status": "error", "message": errors[0]}), 400

        warnings = []
        for key, label in [('MEDIA_PATH','Media Path'), ('LOCAL_PATH','Local Torrent Path')]:
            p = str(data.get(key, ''))
            if p and not os.path.exists(p):
                warnings.append(f"{label} '{p}' does not exist inside the container")
        try:
            existing = db_load_config()
            new_conf = {
                'TORRENT_SOURCE':     str(data.get('TORRENT_SOURCE', existing.get('TORRENT_SOURCE', 'qbit'))),
                'QB_HOST':            str(data.get('QB_HOST', '')),
                'QB_USER':            str(data.get('QB_USER', '')),
                'QB_PASS':            str(data['QB_PASS']) if data.get('QB_PASS') else existing.get('QB_PASS',''),
                'QUI_HOST':           str(data.get('QUI_HOST', '')),
                'QUI_API_KEY':        str(data['QUI_API_KEY']) if data.get('QUI_API_KEY') else existing.get('QUI_API_KEY', ''),
                'MEDIA_PATH':         str(data.get('MEDIA_PATH', '')),
                'REMOTE_PATH':        str(data.get('REMOTE_PATH', '')),
                'LOCAL_PATH':         str(data.get('LOCAL_PATH', '')),
                'WATCHDOG_ENABLED':   bool(data.get('WATCHDOG_ENABLED', True)),
                'WATCHDOG_COOLDOWN':  int(data.get('WATCHDOG_COOLDOWN', 60)),
                'SCHEDULED_INTERVAL': int(data.get('SCHEDULED_INTERVAL', 360)),
                'OR_RATIO':           float(data.get('OR_RATIO',  0.01)),
                'NI_RATIO':           float(data.get('NI_RATIO',  0.01)),
                'DUP_RATIO':          float(data.get('DUP_RATIO', 0.01)),
                'EXCLUSION_PATTERNS':           [p for p in data.get('EXCLUSION_PATTERNS', []) if isinstance(p, str)],
                'DISC_RIP_EXCLUSION_PRESETS': normalize_disc_rip_presets(
                    data.get('DISC_RIP_EXCLUSION_PRESETS', [])
                ),
                'MEDIA_SERVER_EXCLUSION_PRESETS': normalize_media_server_presets(
                    data.get('MEDIA_SERVER_EXCLUSION_PRESETS', [])
                ),
                'EXCLUSION_HIDE_FROM_EXPLORER': bool(data.get('EXCLUSION_HIDE_FROM_EXPLORER', False)),
                'SONARR_URL':         str(data.get('SONARR_URL', '')),
                'SONARR_API_KEY':     str(data['SONARR_API_KEY']) if data.get('SONARR_API_KEY') else existing.get('SONARR_API_KEY', ''),
                'RADARR_URL':         str(data.get('RADARR_URL', '')),
                'RADARR_API_KEY':     str(data['RADARR_API_KEY']) if data.get('RADARR_API_KEY') else existing.get('RADARR_API_KEY', ''),
                'SONARR_REMOTE_PATH': str(data.get('SONARR_REMOTE_PATH', '')),
                'RADARR_REMOTE_PATH': str(data.get('RADARR_REMOTE_PATH', '')),
                'ARR_CONNECTIONS':    _merge_arr_connection_secrets(
                    data.get('ARR_CONNECTIONS', existing.get('ARR_CONNECTIONS', [])),
                    existing.get('ARR_CONNECTIONS', []),
                ),
                'ACQUIRE_DOWNLOAD_FROM': [s for s in data.get('ACQUIRE_DOWNLOAD_FROM', existing.get('ACQUIRE_DOWNLOAD_FROM', [])) if isinstance(s, str)],
                'ACQUIRE_SEEDING_ON':    [s for s in data.get('ACQUIRE_SEEDING_ON',    existing.get('ACQUIRE_SEEDING_ON',    [])) if isinstance(s, str)],
            }
        except (ValueError, TypeError) as e:
            return jsonify({"status": "error", "message": f"Invalid value: {e}"}), 400
        db_save_config(new_conf)
        threading.Thread(target=restart_watchdog, daemon=True).start()

        # Recompute health metrics immediately using existing scan results
        # so threshold changes are reflected on the dashboard without a full rescan.
        # Skip for very large libraries — deserializing both full file lists costs
        # multiple GB of RAM; the new thresholds apply on the next audit instead.
        try:
            stored_count = sum(
                (db_get_meta(f'file_results_{tab}_stats') or {}).get('count', 0)
                for tab in ('media', 'torrents')
            )
            if stored_count > 200_000:
                log.info(f"Skipping immediate health recompute ({stored_count} files stored) — "
                         f"new thresholds apply on the next audit.")
            else:
                curr         = db_load_results()
                media_files  = db_load_file_results('media')
                torrent_files = db_load_file_results('torrents')
                if media_files and torrent_files:
                    new_dashboard = process_health_metrics(
                        media_files, torrent_files, new_conf, update_history=False)
                    curr['dashboard'] = new_dashboard
                    db_save_results(curr)
        except Exception as e:
            log.warning(f"Could not recompute health metrics after config save: {e}")

        return jsonify({"status": "success", "warnings": warnings})

    cfg = db_load_config()
    if cfg.get('QB_PASS'):
        cfg['QB_PASS'] = '__stored__'
    if cfg.get('QUI_API_KEY'):
        cfg['QUI_API_KEY'] = '__stored__'
    if cfg.get('SONARR_API_KEY'):
        cfg['SONARR_API_KEY'] = '__stored__'
    if cfg.get('RADARR_API_KEY'):
        cfg['RADARR_API_KEY'] = '__stored__'
    if isinstance(cfg.get('ARR_CONNECTIONS'), list):
        cfg['ARR_CONNECTIONS'] = [
            {**c, 'api_key': '__stored__'} if c.get('api_key') else c
            for c in cfg['ARR_CONNECTIONS']
        ]
    return jsonify(cfg)


def _merge_arr_connection_secrets(incoming, existing):
    existing_by_id = {
        str(c.get('id')): c
        for c in existing
        if isinstance(c, dict) and c.get('id')
    } if isinstance(existing, list) else {}
    merged = []
    if not isinstance(incoming, list):
        return []
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        conn = dict(raw)
        if conn.get('api_key') == '__stored__' or not conn.get('api_key'):
            old = existing_by_id.get(str(conn.get('id')))
            if old and old.get('api_key'):
                conn['api_key'] = old['api_key']
        merged.append(conn)
    return merged


@app.route('/api/test_connection', methods=['POST'])
@require_auth
def test_connection():
    data = request.json or {}
    # If password fields are blank, fall back to stored values for live testing
    existing = db_load_config()
    if not data.get('QB_PASS') and data.get('TORRENT_SOURCE', 'qbit') == 'qbit':
        data = {**data, 'QB_PASS': existing.get('QB_PASS', '')}
    if not data.get('QUI_API_KEY') and data.get('TORRENT_SOURCE') == 'qui':
        data = {**data, 'QUI_API_KEY': existing.get('QUI_API_KEY', '')}

    result = {}

    def _connect():
        result.update(sources.test_connection(data))

    t = threading.Thread(target=_connect, daemon=True)
    t.start()
    t.join(timeout=10)

    if t.is_alive():
        return jsonify({"status": "error", "message": "Connection timed out"}), 400
    elif result.get('ok'):
        try:
            curr = db_load_results()
            if _is_source_error_status(curr.get('status', '')):
                curr['status'] = 'ok'
                db_save_results(curr)
        except Exception:
            pass
        resp = {"status": "success"}
        if result.get('version'):
            resp['version'] = result['version']
        if result.get('instances') is not None:
            resp['instances'] = result['instances']
        if result.get('eligible_count') is not None:
            resp['eligible_count'] = result['eligible_count']
        if result.get('skipped') is not None:
            resp['skipped'] = result['skipped']
        return jsonify(resp)
    else:
        return jsonify({"status": "error", "message": result.get('error', 'Unknown error')}), 400


@app.route('/api/source_info')
@require_auth
def source_info():
    cfg = db_load_config()
    result = {}
    def _fetch():
        try:
            result.update(sources.connection_info(cfg))
        except Exception as e:
            result['error'] = str(e)
    t = threading.Thread(target=_fetch, daemon=True); t.start(); t.join(timeout=12)
    if t.is_alive():
        return jsonify({'error': 'Connection timed out'}), 400
    if 'error' in result:
        return jsonify({'error': result['error']}), 400
    return jsonify(result)


@app.route('/api/source_save_path', methods=['POST'])
@require_auth
def source_save_path():
    data = request.json or {}
    existing = db_load_config()
    # Fall back to stored credentials when the caller sends a blank password
    if not data.get('QB_PASS') and data.get('TORRENT_SOURCE', 'qbit') == 'qbit':
        data = {**data, 'QB_PASS': existing.get('QB_PASS', '')}
    if not data.get('QUI_API_KEY') and data.get('TORRENT_SOURCE') == 'qui':
        data = {**data, 'QUI_API_KEY': existing.get('QUI_API_KEY', '')}
    result = {}
    def _fetch():
        try:
            result.update(sources.fetch_save_path_hint(data))
        except Exception as e:
            result['error'] = str(e)
    t = threading.Thread(target=_fetch, daemon=True); t.start(); t.join(timeout=12)
    if t.is_alive():
        return jsonify({'error': 'Connection timed out'}), 400
    if 'error' in result:
        return jsonify({'error': result['error']}), 400
    return jsonify(result)


@app.route('/api/browse_data')
@require_auth
def browse_data():
    base = '/data'
    if not os.path.isdir(base):
        return jsonify({'dirs': [], 'missing': True})
    try:
        dirs = sorted([
            d for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d))
        ])
        return jsonify({'dirs': dirs, 'missing': False})
    except Exception as e:
        return jsonify({'dirs': [], 'missing': True, 'error': str(e)})


@app.route('/api/test_paths', methods=['POST'])
@require_auth
def test_paths():
    data = request.json or {}
    results = {}
    for key in ('MEDIA_PATH', 'LOCAL_PATH'):
        path = data.get(key, '')
        if not path:
            results[key] = {'ok': False, 'message': 'Path is empty'}
        elif os.path.exists(path):
            results[key] = {'ok': True, 'message': 'Path exists'}
        else:
            results[key] = {'ok': False, 'message': f'{path} does not exist inside the container'}
    return jsonify({
        'media_path': results.get('MEDIA_PATH'),
        'local_path':  results.get('LOCAL_PATH'),
    })


@app.route('/api/test_sonarr', methods=['POST'])
@require_auth
def test_sonarr():
    data = request.json or {}
    existing = db_load_config()
    url     = data.get('url', '')     or existing.get('SONARR_URL', '')
    api_key = data.get('api_key', '') or existing.get('SONARR_API_KEY', '')
    ok, msg = _test_arr_connection(url, api_key)
    if ok:
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": msg}), 400


@app.route('/api/test_radarr', methods=['POST'])
@require_auth
def test_radarr():
    data = request.json or {}
    existing = db_load_config()
    url     = data.get('url', '')     or existing.get('RADARR_URL', '')
    api_key = data.get('api_key', '') or existing.get('RADARR_API_KEY', '')
    ok, msg = _test_arr_connection(url, api_key)
    if ok:
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": msg}), 400


@app.route('/api/test_arr_connections', methods=['POST'])
@require_auth
def test_arr_connections_route():
    data = request.json or {}
    existing = db_load_config()
    cfg = {**existing}

    for key in (
        'SONARR_URL', 'SONARR_API_KEY', 'SONARR_REMOTE_PATH',
        'RADARR_URL', 'RADARR_API_KEY', 'RADARR_REMOTE_PATH',
    ):
        if data.get(key):
            cfg[key] = data[key]

    if 'ARR_CONNECTIONS' in data:
        cfg['ARR_CONNECTIONS'] = _merge_arr_connection_secrets(
            data.get('ARR_CONNECTIONS', []),
            existing.get('ARR_CONNECTIONS', []),
        )

    try:
        result = test_arr_connections(cfg)
    except ValueError as e:
        return jsonify({
            "status": "error",
            "ok": False,
            "message": str(e),
            "connection_count": 0,
            "connections": [],
        })
    if result['connection_count'] == 0:
        result['message'] = 'No Sonarr/Radarr connections configured'
    return jsonify({
        "status": "success" if result.get('ok') else "error",
        **result,
    })


@app.route('/api/actions/script/<script_type>', methods=['GET', 'POST'])
@require_auth
def get_action_script(script_type):
    cfg = db_load_config()
    results = db_load_results()
    results['torrent_files'] = db_load_file_results('torrents')
    if script_type == 'dedupe':
        results['media_files'] = db_load_file_results('media')
    # POST carries a selection from a workflow page: {'paths': [...]} for
    # delete scripts, {'groups': [...]} (canonical paths) for dedupe.
    selection = (request.get_json(silent=True) or {}) if request.method == 'POST' else None
    try:
        script = generate_script(script_type, results, cfg, selection=selection)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    return app.response_class(script, mimetype='text/plain; charset=utf-8')


@app.route('/api/actions/sonarr_rescan', methods=['POST'])
@require_auth
def actions_sonarr_rescan():
    data = request.json or {}
    cfg  = db_load_config()
    try:
        count = arr_rescan(cfg, 'sonarr', data.get('paths', []))
        return jsonify({"status": "success", "count": count})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        log.exception("Error in sonarr_rescan")
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/actions/radarr_rescan', methods=['POST'])
@require_auth
def actions_radarr_rescan():
    data = request.json or {}
    cfg  = db_load_config()
    try:
        count = arr_rescan(cfg, 'radarr', data.get('paths', []))
        return jsonify({"status": "success", "count": count})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        log.exception("Error in radarr_rescan")
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/actions/sonarr_search', methods=['POST'])
@require_auth
def actions_sonarr_search():
    data      = request.json or {}
    file_path = data.get('path', '')
    cfg       = db_load_config()
    try:
        result = arr_search(cfg, 'sonarr', file_path)
        return jsonify({"status": "success", **result})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except LookupError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except ConnectionError as e:
        log.exception("HTTP error in sonarr_search")
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        log.exception("Error in sonarr_search")
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/actions/radarr_search', methods=['POST'])
@require_auth
def actions_radarr_search():
    data      = request.json or {}
    file_path = data.get('path', '')
    cfg       = db_load_config()
    try:
        result = arr_search(cfg, 'radarr', file_path)
        return jsonify({"status": "success", **result})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except LookupError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except ConnectionError as e:
        log.exception("HTTP error in radarr_search")
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        log.exception("Error in radarr_search")
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/upload_stats')
@require_auth
def get_upload_stats():
    from_date = request.args.get('from') or None
    to_date   = request.args.get('to')   or None
    if from_date or to_date:
        stats = compute_upload_stats(from_date=from_date, to_date=to_date)
    else:
        days = request.args.get('days', 30, type=int)
        if days != 0:
            days = max(1, min(365, days))
        stats = compute_upload_stats(days)
    if stats is None:
        return jsonify({"status": "pending", "message": "Not enough data yet. Upload stats require at least 2 audits."})
    return jsonify(stats)


@app.route('/api/upload_snapshots')
@require_auth
def get_upload_snapshots():
    days  = request.args.get('days', 7, type=int)
    days  = max(1, min(90, days))
    snaps = db_get_upload_snapshots(since_days=days)
    return jsonify({"snapshots": snaps})


@app.route('/api/upload_snapshots/source_counts')
@require_auth
def get_upload_snapshot_source_counts():
    return jsonify(db_count_upload_snapshots_by_source())


@app.route('/api/upload_snapshots/retag', methods=['POST'])
@require_auth
def retag_upload_snapshots():
    data      = request.json or {}
    from_date = (data.get('from') or '').strip() or None
    to_date   = (data.get('to')   or '').strip() or None
    source    = (data.get('source') or '').strip()
    if not from_date and not to_date:
        return jsonify({"status": "error", "message": "At least one of 'from' or 'to' is required"}), 400
    if source not in ('qbit', 'qui'):
        return jsonify({"status": "error", "message": "source must be 'qbit' or 'qui'"}), 400
    snap_count, run_count = db_retag_upload_snapshots(from_date, source, to_date_str=to_date)
    return jsonify({"status": "success", "updated": snap_count, "audit_runs_updated": run_count})


@app.route('/api/upload_snapshots/delete', methods=['POST'])
@require_auth
def delete_upload_snapshots():
    data      = request.json or {}
    from_date = (data.get('from') or '').strip() or None
    to_date   = (data.get('to')   or '').strip() or None
    if not from_date and not to_date:
        return jsonify({"status": "error", "message": "At least one of 'from' or 'to' is required"}), 400
    snap_count, run_count = db_delete_upload_snapshots(from_date, to_date_str=to_date)
    return jsonify({"status": "success", "deleted": snap_count, "audit_runs_deleted": run_count})


@app.route('/api/workflows/acquire_prefs', methods=['POST'])
@require_auth
def workflows_acquire_prefs():
    data = request.json or {}
    cfg = db_load_config()
    if 'ACQUIRE_DOWNLOAD_FROM' in data:
        cfg['ACQUIRE_DOWNLOAD_FROM'] = [s for s in data['ACQUIRE_DOWNLOAD_FROM'] if isinstance(s, str)]
    if 'ACQUIRE_SEEDING_ON' in data:
        cfg['ACQUIRE_SEEDING_ON'] = [s for s in data['ACQUIRE_SEEDING_ON'] if isinstance(s, str)]
    db_save_config(cfg)
    return jsonify({"status": "success"})


@app.route('/api/workflows/indexers')
@require_auth
def workflows_indexers():
    cfg = db_load_config()
    try:
        indexers = fetch_arr_indexers(cfg)
    except Exception as e:
        log.exception("Error fetching Arr indexers")
        return jsonify({"status": "error", "message": str(e)}), 400
    return jsonify({"status": "success", "indexers": indexers})


# ---------------------------------------------------------------------------
# Workflow report endpoints (Triage / Cleanup / Dedupe)
# ---------------------------------------------------------------------------

_VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.m2ts', '.ts', '.mov', '.wmv'}
_TRIAGE_GROUP_CAP = 500


@app.route('/api/workflows/exclude', methods=['POST'])
@require_auth
def workflows_exclude():
    """Append patterns to the Excluded Files & Folders config list."""
    data = request.json or {}
    patterns = [str(p).strip() for p in (data.get('patterns') or []) if str(p).strip()]
    if not patterns:
        return jsonify({"status": "error", "message": "No patterns provided"}), 400
    cfg = db_load_config()
    existing = [p for p in cfg.get('EXCLUSION_PATTERNS', []) if isinstance(p, str)]
    seen = {p.strip().lower() for p in existing}
    added = 0
    for p in patterns:
        if p.lower() not in seen:
            existing.append(p)
            seen.add(p.lower())
            added += 1
    cfg['EXCLUSION_PATTERNS'] = existing
    db_save_config(cfg)
    return jsonify({"status": "success", "added": added, "total": len(existing)})


@app.route('/api/workflows/triage')
@require_auth
def workflows_triage():
    """Classify every not-imported torrent into an actionable verdict.

    Verdicts (priority order):
      unregistered    — tracker says the torrent is no longer registered (dead seed)
      superseded      — the library already has this title (possibly different quality)
      import_pending  — title is managed by Sonarr/Radarr but has no library file
      not_in_library  — title matches nothing in any Arr instance
    """
    cfg = db_load_config()
    torrent_files = db_load_file_results('torrents')
    not_imported  = [f for f in torrent_files if _is_not_imported_torrent(f)]

    # Group files by torrent hash — verdicts are per torrent, not per file
    groups = {}
    for f in not_imported:
        key = f.get('hash') or f['path']
        g = groups.setdefault(key, {
            'hash':        f.get('hash') or '',
            'instance_id': f.get('instance_id'),
            'files':       [],
            'total_size':  0,
            'trackers':    set(),
        })
        g['files'].append(f)
        g['total_size'] += f['size']
        g['trackers'].update(t for t in (f.get('trackers') or []) if t != 'None')

    group_list = sorted(groups.values(), key=lambda g: -g['total_size'])
    truncated  = len(group_list) > _TRIAGE_GROUP_CAP
    group_list = group_list[:_TRIAGE_GROUP_CAP]

    # Live source lookup: upload stats + tracker registration status. The
    # tracker message ("Unregistered torrent") is the strongest signal here.
    details = {}
    try:
        details = sources.fetch_torrent_details(cfg, [
            {'hash': g['hash'], 'instance_id': g['instance_id']}
            for g in group_list if g['hash']
        ])
    except Exception as e:
        log.warning("Triage: torrent detail fetch failed: %s", e)

    try:
        media_index = fetch_arr_media_index(cfg)
    except Exception as e:
        log.warning("Triage: media index fetch failed: %s", e)
        media_index = []
    try:
        all_titles = fetch_arr_all_titles(cfg)
    except Exception as e:
        log.warning("Triage: title list fetch failed: %s", e)
        all_titles = []

    # Index arr titles under every match variant (apostrophes spaced/dropped)
    lib_by_title = {}
    for m in media_index:
        for key in title_match_keys(m.get('title') or ''):
            lib_by_title.setdefault(key, []).append(m)
    titles_by_norm = {}
    for t in all_titles:
        for key in title_match_keys(t.get('title') or ''):
            titles_by_norm.setdefault(key, t)

    conn_by_id = {c['id']: c for c in normalize_arr_connections(cfg)}

    def _arr_url(entry):
        conn = conn_by_id.get(entry.get('connection_id'))
        if not conn:
            return ''
        prefix = '/movie/' if entry.get('service') == 'radarr' else '/series/'
        slug = entry.get('title_slug') or ''
        return conn['base_url'].rstrip('/') + prefix + (slug or str(entry.get('arr_id') or ''))

    items = []
    for g in group_list:
        videos = [f for f in g['files']
                  if os.path.splitext(f['path'])[1].lower() in _VIDEO_EXTS]
        rep = max(videos or g['files'], key=lambda f: f['size'])
        parsed = parse_release_info_for_path(rep['path'])

        det            = details.get(g['hash'], {})
        tracker_health = det.get('tracker_health', 'unknown')
        parsed_keys    = title_match_keys(parsed['title'])
        is_episode     = parsed['season'] is not None

        # Library rows with files, preferring the service that fits the content type
        lib_rows = next((lib_by_title[k] for k in parsed_keys if k in lib_by_title), [])
        if lib_rows:
            preferred = 'sonarr' if is_episode else 'radarr'
            lib_rows = [r for r in lib_rows if r.get('service') == preferred] or lib_rows

        library_match = None
        if lib_rows:
            if is_episode and lib_rows[0].get('service') == 'sonarr':
                # Same episode, or any episode of the same season for season packs
                if parsed['episode'] is not None:
                    se_tag = f"s{parsed['season']:02d}e{parsed['episode']:02d}"
                else:
                    se_tag = f"s{parsed['season']:02d}e"
                for r in lib_rows:
                    base = os.path.basename(r.get('relative_path') or r.get('path') or '').lower()
                    if se_tag in base:
                        library_match = r
                        break
            else:
                library_match = lib_rows[0]

        arr_title_hit = next((titles_by_norm[k] for k in parsed_keys if k in titles_by_norm), None)
        in_arr = arr_title_hit is not None

        if tracker_health == 'unregistered':
            verdict = 'unregistered'
        elif library_match:
            verdict = 'superseded'
        elif in_arr or lib_rows:
            verdict = 'import_pending'
        else:
            verdict = 'not_in_library'

        lib_payload = None
        if library_match:
            lib_quality = library_match.get('file_quality_name') or ''
            lib_payload = {
                'title':        library_match.get('title') or '',
                'year':         library_match.get('year'),
                'service':      library_match.get('service') or '',
                'quality_name': lib_quality,
                'hdr':          library_match.get('file_hdr') or '',
                'filename':     os.path.basename(library_match.get('path') or ''),
                'arr_url':      _arr_url(library_match),
                'same_quality': bool(parsed['resolution'] and parsed['resolution'] in lib_quality),
            }
        elif in_arr:
            t = arr_title_hit
            lib_payload = {
                'title':        t.get('title') or '',
                'year':         t.get('year'),
                'service':      t.get('service') or '',
                'quality_name': '',
                'hdr':          '',
                'filename':     '',
                'arr_url':      _arr_url(t),
                'same_quality': False,
            }

        items.append({
            'hash':           g['hash'],
            'rep_path':       rep['path'],
            'paths':          [f['path'] for f in g['files']],
            'file_count':     len(g['files']),
            'total_size':     g['total_size'],
            'trackers':       sorted(g['trackers']),
            'verdict':        verdict,
            'parsed':         parsed,
            'library':        lib_payload,
            'tracker_health': tracker_health,
            'tracker_msg':    det.get('tracker_msg', ''),
            'uploaded':       det.get('uploaded'),
            'ratio':          det.get('ratio'),
            'added_on':       det.get('added_on'),
        })

    verdict_order = {'unregistered': 0, 'superseded': 1, 'import_pending': 2, 'not_in_library': 3}
    items.sort(key=lambda i: (verdict_order.get(i['verdict'], 9), -i['total_size']))
    counts = {}
    for i in items:
        counts[i['verdict']] = counts.get(i['verdict'], 0) + 1

    return jsonify({
        "status":         "success",
        "items":          items,
        "counts":         counts,
        "truncated":      truncated,
        "arr_configured": bool(conn_by_id),
    })


@app.route('/api/workflows/cleanup')
@require_auth
def workflows_cleanup():
    """Orphaned-torrent report grouped by top-level folder, with hardlink and age info."""
    cfg = db_load_config()
    torrent_files = db_load_file_results('torrents')
    local_path    = cfg.get('LOCAL_PATH', '')

    orphaned       = [f for f in torrent_files if f.get('status') == 'Orphaned' and not f.get('excluded')]
    excluded_count = sum(1 for f in torrent_files if f.get('status') == 'Orphaned' and f.get('excluded'))

    # mtime via os.stat is best-effort and capped — a pathological orphan count
    # shouldn't turn the report page into a second filesystem scan
    fetch_mtime = bool(local_path) and len(orphaned) <= 5000

    folders = {}
    for f in orphaned:
        rel = f['path'].replace('\\', '/')
        # Group at release-folder depth: with a TRaSH layout the first segment
        # is a category dir (movies/, tv/) and the second is the torrent's own
        # folder — cap at two segments so groups map to abandoned payloads.
        dir_segs = rel.split('/')[:-1]
        top = '/'.join(dir_segs[:2]) if dir_segs else '(root)'
        g = folders.setdefault(top, {
            'folder': top, 'files': [],
            'total_size': 0, 'freeable_size': 0, 'hardlinked_size': 0,
        })
        # Hardlinked elsewhere — deleting frees nothing until the last link goes
        hardlinked = bool(f.get('imported')) or bool(f.get('linked_paths'))
        entry = {'path': f['path'], 'size': f['size'], 'hardlinked': hardlinked, 'mtime': None}
        if fetch_mtime:
            try:
                entry['mtime'] = int(os.path.getmtime(os.path.join(local_path, f['path'])))
            except OSError:
                pass
        g['files'].append(entry)
        g['total_size'] += f['size']
        if hardlinked:
            g['hardlinked_size'] += f['size']
        else:
            g['freeable_size'] += f['size']

    groups = sorted(folders.values(), key=lambda g: -g['total_size'])
    for g in groups:
        g['files'].sort(key=lambda x: -x['size'])

    return jsonify({
        "status":         "success",
        "groups":         groups,
        "file_count":     len(orphaned),
        "total_size":     sum(g['total_size'] for g in groups),
        "freeable_size":  sum(g['freeable_size'] for g in groups),
        "excluded_count": excluded_count,
    })


@app.route('/api/workflows/dedupe')
@require_auth
def workflows_dedupe():
    """Duplicate-group report — the same groups the dedupe script is built from."""
    cfg = db_load_config()
    local_path = cfg.get('LOCAL_PATH', '')
    media_path = cfg.get('MEDIA_PATH', '')
    torrent_files = db_load_file_results('torrents')
    media_files   = db_load_file_results('media')
    tagged = ([{**f, '_file_root': local_path} for f in torrent_files]
              + [{**f, '_file_root': media_path} for f in media_files])
    dup_result = _build_dup_groups(tagged, local_path, media_path)

    groups_out = []
    for g in dup_result['groups']:
        canonical = next(f for f in g['files'] if f['canonical'])
        groups_out.append({
            'id':               canonical['path'],
            'files':            g['files'],
            'recoverable_size': g['recoverable_size'],
            'cross_fs':         g['skipped'],
        })
    groups_out.sort(key=lambda g: (g['cross_fs'], -g['recoverable_size']))

    return jsonify({
        "status":            "success",
        "groups":            groups_out,
        "script_root":       dup_result['script_root'],
        "excluded_count":    dup_result.get('excluded_count', 0),
        "total_recoverable": sum(g['recoverable_size'] for g in groups_out),
    })


@app.route('/api/workflows/acquire_candidates')
@require_auth
def workflows_acquire_candidates():
    cfg = db_load_config()
    media_files = db_load_file_results('media')

    candidates_raw = [
        f for f in media_files
        if not f.get('excluded') and not [t for t in (f.get('trackers') or []) if t != 'None']
    ]

    arr_media = fetch_arr_media_index(cfg)
    media_root = cfg.get('MEDIA_PATH', '')

    def _norm(p):
        return os.path.normpath(str(p or '')).replace('\\', '/')

    arr_index = {}
    for item in arr_media:
        key = _norm(item.get('path', ''))
        if key:
            arr_index[key] = item

    svc_map = {
        'sonarr': {'slug_prefix': '/series/'},
        'radarr': {'slug_prefix': '/movie/'},
    }

    all_conns = normalize_arr_connections(cfg)
    conn_by_id = {c['id']: c for c in all_conns}
    fallback_base_url = all_conns[0]['base_url'] if all_conns else ''

    candidates = []
    resolved_count = 0
    unresolved_count = 0

    for f in candidates_raw:
        rel_path = f.get('path', '')
        abs_path = _norm(os.path.join(media_root, rel_path) if not os.path.isabs(rel_path) else rel_path)
        arr_item = arr_index.get(abs_path)

        if arr_item:
            service = arr_item.get('service', '')
            conn_id = arr_item.get('connection_id', '')
            arr_id = arr_item.get('arr_id')
            title = arr_item.get('title', '')
            episode_ids = arr_item.get('episode_ids') or []
            episode_id = episode_ids[0] if episode_ids else None
            slug_prefix = svc_map.get(service, {}).get('slug_prefix', '/')
            title_slug = arr_item.get('titleSlug') or arr_item.get('title_slug')
            conn = conn_by_id.get(conn_id)
            base_url = conn['base_url'] if conn else ''
            if title_slug:
                arr_url = base_url.rstrip('/') + slug_prefix + title_slug
            else:
                arr_url = base_url.rstrip('/') + slug_prefix + str(arr_id) if arr_id else base_url
            candidates.append({
                **{k: f.get(k) for k in ('path', 'size', 'trackers', 'inode')},
                'resolved': True,
                'arr_service': service,
                'arr_connection_id': conn_id,
                'arr_id': arr_id,
                'arr_title': title,
                'episode_id': episode_id,
                'arr_url': arr_url,
            })
            resolved_count += 1
        else:
            filename = os.path.basename(rel_path)
            search_url = ''
            if fallback_base_url:
                term = os.path.splitext(filename)[0]
                search_url = fallback_base_url.rstrip('/') + '/add/new?term=' + urllib.parse.quote(term)
            candidates.append({
                **{k: f.get(k) for k in ('path', 'size', 'trackers', 'inode')},
                'resolved': False,
                'arr_service': None,
                'arr_connection_id': None,
                'arr_id': None,
                'arr_title': None,
                'episode_id': None,
                'arr_url': search_url,
            })
            unresolved_count += 1

    return jsonify({
        "status": "success",
        "candidates": candidates,
        "resolved_count": resolved_count,
        "unresolved_count": unresolved_count,
    })


_release_jobs = {}   # job_key -> {status, releases, message, ts}
_RELEASE_JOB_TTL = 600  # 10 minutes

def _release_job_key(service, connection_id, arr_id, episode_id, season_number, file_path):
    parts = (service, connection_id, str(arr_id), str(episode_id), str(season_number), file_path or '')
    return ':'.join(parts)

_RES_LABEL_MAP = {'2160p': 2160, '1080p': 1080, '720p': 720}

def _apply_release_filters(rows, download_from, seeding_on, res_filter=None, source_filter=None, hdr_filter=None):
    groups = {}
    for r in rows:
        key = (r['title'].lower().strip(), r['size'])
        groups.setdefault(key, []).append(r)
    if seeding_on:
        groups = {k: v for k, v in groups.items() if any(r['indexer'] in seeding_on for r in v)}
    filtered = []
    for v in groups.values():
        for r in v:
            if download_from and r['indexer'] not in download_from:
                continue
            filtered.append(r)
    if res_filter:
        target_resolutions = {_RES_LABEL_MAP[r] for r in res_filter if r in _RES_LABEL_MAP}
        if target_resolutions:
            filtered = [r for r in filtered if r.get('resolution') in target_resolutions]
    if source_filter:
        def _source_match(r):
            # Sonarr uses source="bluray" for both Remux and Bluray encodes.
            # Distinguish them by quality name so the two chips are independent.
            is_remux = 'remux' in (r.get('quality_name') or '').lower()
            return ('remux' if is_remux else r.get('source', '')) in source_filter
        filtered = [r for r in filtered if _source_match(r)]
    if hdr_filter:
        # 'SDR' maps to empty string (no HDR detected); other values match hdr field directly
        target_hdr = {'' if h == 'SDR' else h for h in hdr_filter}
        filtered = [r for r in filtered if r.get('hdr', '') in target_hdr]
    # Sort: custom format score desc, quality weight desc, seeders desc (matches Sonarr/Radarr interactive search order)
    filtered.sort(key=lambda r: (r.get('custom_format_score', 0), r.get('quality_weight', 0), r.get('seeders', 0)), reverse=True)
    return filtered


@app.route('/api/workflows/acquire_releases')
@require_auth
def workflows_acquire_releases():
    service = request.args.get('service', '')
    connection_id = request.args.get('connection_id', '')
    arr_id = request.args.get('arr_id', type=int)
    episode_id = request.args.get('episode_id', type=int)
    season_number = request.args.get('season_number', type=int)
    file_path = request.args.get('path', '') or None
    if not service or not connection_id or arr_id is None:
        return jsonify({"status": "error", "message": "service, connection_id, and arr_id are required"}), 400

    # Expire old jobs
    now = time.time()
    expired = [k for k, v in _release_jobs.items() if now - v['ts'] > _RELEASE_JOB_TTL]
    for k in expired:
        del _release_jobs[k]

    job_key = _release_job_key(service, connection_id, arr_id, episode_id, season_number, file_path)

    if job_key in _release_jobs:
        job = _release_jobs[job_key]
        if job['status'] == 'done':
            cfg = db_load_config()
            releases = _apply_release_filters(
                job['releases'],
                cfg.get('ACQUIRE_DOWNLOAD_FROM') or [],
                cfg.get('ACQUIRE_SEEDING_ON') or [],
            )
            return jsonify({"status": "done", "releases": releases})
        return jsonify({"status": job['status'], "message": job.get('message', '')})

    # Start a new background search
    cfg = db_load_config()
    _release_jobs[job_key] = {'status': 'searching', 'releases': None, 'ts': time.time()}

    def do_search():
        try:
            rows = fetch_release_matrix(
                cfg, service, connection_id, arr_id,
                episode_id=episode_id, season_number=season_number, file_path=file_path,
            )
            _release_jobs[job_key] = {'status': 'done', 'releases': rows, 'ts': time.time()}
        except Exception as e:
            log.warning("Release search failed for %s: %s", job_key, e)
            _release_jobs[job_key] = {'status': 'error', 'message': str(e), 'releases': None, 'ts': time.time()}

    threading.Thread(target=do_search, daemon=True).start()
    return jsonify({'status': 'searching'})


@app.route('/api/workflows/grab_release', methods=['POST'])
@require_auth
def workflows_grab_release():
    data = request.json or {}
    service      = data.get('service', '')
    connection_id = data.get('connection_id', '')
    guid         = data.get('guid', '')
    indexer_id   = data.get('indexer_id')
    if not service or not connection_id or not guid or indexer_id is None:
        return jsonify({"status": "error", "message": "service, connection_id, guid, and indexer_id are required"}), 400
    cfg = db_load_config()
    try:
        grab_release(cfg, service, connection_id, guid, indexer_id)
        return jsonify({"status": "success"})
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')
        msg = f"HTTP {e.code}: {e.reason}"
        try:
            msg = json.loads(body).get('message') or msg
        except Exception:
            pass
        log.warning("Grab failed for %s/%s: %s", service, guid, msg)
        return jsonify({"status": "error", "message": msg}), 400
    except Exception as e:
        log.warning("Grab failed for %s/%s: %s", service, guid, e)
        return jsonify({"status": "error", "message": str(e)}), 400


_gen_state = {'job': None}


def _gen_root_folder(path):
    if not path:
        return 'Other'
    normalized = path.replace('\\', '/').lstrip('/')
    parts = normalized.split('/')
    return parts[0] if parts else 'Other'


def _gen_parse_season(path):
    m = re.search(r'[Ss](\d{1,2})[Ee]', os.path.basename(str(path or '')))
    return int(m.group(1)) if m else None


def _build_generate_candidates(cfg, folders=None, limit=20, title_search=None):
    """Resolved, season-grouped candidates for the generate workflow."""
    media_files = db_load_file_results('media')
    candidates_raw = [
        f for f in media_files
        if not f.get('excluded') and not [t for t in (f.get('trackers') or []) if t != 'None']
    ]
    arr_media = fetch_arr_media_index(cfg)
    media_root = cfg.get('MEDIA_PATH', '')

    def _norm(p):
        return os.path.normpath(str(p or '')).replace('\\', '/')

    arr_index = {}
    for item in arr_media:
        key = _norm(item.get('path', ''))
        if key:
            arr_index[key] = item

    svc_slug = {'sonarr': '/series/', 'radarr': '/movie/'}
    conn_by_id = {c['id']: c for c in normalize_arr_connections(cfg)}

    flat = []
    for f in candidates_raw:
        rel_path = f.get('path', '')
        abs_path = _norm(os.path.join(media_root, rel_path) if not os.path.isabs(rel_path) else rel_path)
        arr_item = arr_index.get(abs_path)
        if not arr_item:
            continue
        service  = arr_item.get('service', '')
        conn_id  = arr_item.get('connection_id', '')
        arr_id   = arr_item.get('arr_id')
        title    = arr_item.get('title', '')
        ep_ids   = arr_item.get('episode_ids') or []
        t_slug   = arr_item.get('titleSlug') or arr_item.get('title_slug')
        conn     = conn_by_id.get(conn_id)
        base_url = conn['base_url'] if conn else ''
        slug     = svc_slug.get(service, '/')
        arr_url  = (base_url.rstrip('/') + slug + t_slug) if t_slug else (base_url.rstrip('/') + slug + str(arr_id) if arr_id else base_url)
        flat.append({
            'path': rel_path, 'size': f.get('size', 0),
            'arr_service': service, 'arr_connection_id': conn_id,
            'arr_id': arr_id, 'arr_title': title,
            'episode_id': ep_ids[0] if ep_ids else None,
            'arr_url': arr_url,
            'file_quality': arr_item.get('file_quality_name', ''),
            'file_hdr':     arr_item.get('file_hdr', ''),
        })

    # Group Sonarr episodes by (arr_id, season)
    groups = []
    sonarr_map = {}
    for c in flat:
        if c['arr_service'] == 'sonarr':
            season = _gen_parse_season(c['path'])
            key = f"{c['arr_id']}_S{season}"
            if key in sonarr_map:
                g = groups[sonarr_map[key]]
                g['file_count'] += 1
                g['total_size'] = g.get('total_size', 0) + (c.get('size') or 0)
                if not g.get('episode_id') and c.get('episode_id'):
                    g['episode_id'] = c['episode_id']
            else:
                sonarr_map[key] = len(groups)
                groups.append({**c, 'season_number': season, 'file_count': 1,
                                'total_size': c.get('size') or 0, 'rep_path': c['path']})
        else:
            groups.append({**c, 'season_number': None, 'file_count': 1,
                           'total_size': c.get('size') or 0, 'rep_path': c['path']})

    if folders:
        groups = [g for g in groups if _gen_root_folder(g.get('rep_path') or g.get('path', '')) in folders]
    if title_search:
        term = title_search.strip().lower()
        groups = [g for g in groups if term in (g.get('arr_title') or '').lower()]

    return groups[:limit]


def _sort_generate_candidates(groups, sort):
    if sort == 'largest':
        groups.sort(key=lambda g: g.get('total_size') or 0, reverse=True)
    elif sort == 'smallest':
        groups.sort(key=lambda g: g.get('total_size') or 0)
    elif sort == 'random':
        random.SystemRandom().shuffle(groups)
    elif sort == 'alpha':
        groups.sort(key=lambda g: (g.get('arr_title') or '').lower())
    return groups


@app.route('/api/workflows/generate', methods=['POST'])
@require_auth
def workflows_generate():
    data          = request.json or {}
    folders       = data.get('folders') or []
    count         = max(1, min(int(data.get('count', 10) or 10), 500))
    sort          = data.get('sort', 'largest')
    download_from = data.get('download_from') or []
    seeding_on    = data.get('seeding_on') or []
    res_filter    = data.get('res_filter') or []
    source_filter = data.get('source_filter') or []
    hdr_filter    = data.get('hdr_filter') or []
    title_search  = (data.get('title_search') or '').strip()

    existing = _gen_state.get('job')
    if existing and existing.get('status') == 'running':
        existing['stop_flag'] = True

    cfg = db_load_config()
    try:
        candidates = _sort_generate_candidates(
            _build_generate_candidates(cfg, folders=folders or None, limit=None, title_search=title_search or None),
            sort,
        )[:count]
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

    job_id = secrets.token_hex(8)
    job = {'id': job_id, 'status': 'running', 'total': len(candidates),
           'completed': 0, 'stop_flag': False, 'results': []}
    _gen_state['job'] = job

    def do_generate():
        for candidate in candidates:
            if job['stop_flag']:
                job['status'] = 'stopped'
                return
            result = {
                'arr_title':         candidate.get('arr_title', ''),
                'arr_service':       candidate.get('arr_service'),
                'arr_connection_id': candidate.get('arr_connection_id'),
                'arr_id':            candidate.get('arr_id'),
                'arr_url':           candidate.get('arr_url'),
                'path':              candidate.get('rep_path') or candidate.get('path'),
                'season_number':     candidate.get('season_number'),
                'file_count':        candidate.get('file_count', 1),
                'total_size':        candidate.get('total_size', 0),
                'file_quality':      candidate.get('file_quality', ''),
                'file_hdr':          candidate.get('file_hdr', ''),
                'status':            'searching',
                'releases':          None,
                'best_release':      None,
                'error':             None,
            }
            job['results'].append(result)
            try:
                rows = fetch_release_matrix(
                    cfg, candidate['arr_service'], candidate['arr_connection_id'], candidate['arr_id'],
                    episode_id=candidate.get('episode_id'),
                    season_number=candidate.get('season_number'),
                    file_path=candidate.get('rep_path') or candidate.get('path'),
                )
                filtered = _apply_release_filters(rows, download_from, seeding_on, res_filter=res_filter, source_filter=source_filter, hdr_filter=hdr_filter)
                best = filtered[0] if filtered else None
                result['status']       = 'found' if best else 'not_found'
                result['releases']     = filtered
                result['best_release'] = best
            except Exception as e:
                log.warning("Generate search failed for %s: %s", candidate.get('arr_title'), e)
                result['status'] = 'error'
                result['error']  = str(e)
            job['completed'] += 1

        job['status'] = 'done'

    threading.Thread(target=do_generate, daemon=True).start()
    return jsonify({'job_id': job_id, 'total': len(candidates)})


@app.route('/api/workflows/generate/status')
@require_auth
def workflows_generate_status():
    job_id = request.args.get('job_id', '')
    job = _gen_state.get('job')
    if not job or job['id'] != job_id:
        return jsonify({'status': 'error', 'message': 'Job not found'}), 404
    return jsonify({'status': job['status'], 'total': job['total'],
                    'completed': job['completed'], 'results': list(job['results'])})


@app.route('/api/workflows/generate/stop', methods=['POST'])
@require_auth
def workflows_generate_stop():
    data   = request.json or {}
    job_id = data.get('job_id', '')
    job    = _gen_state.get('job')
    if job and job['id'] == job_id and job['status'] == 'running':
        job['stop_flag'] = True
    return jsonify({'status': 'ok'})


_import_watches = {}  # job_id -> {status, message, title, service, completed_at}


@app.route('/api/workflows/watch_import', methods=['POST'])
@require_auth
def workflows_watch_import():
    data          = request.json or {}
    service       = data.get('service', '')
    connection_id = data.get('connection_id', '')
    arr_id        = data.get('arr_id')
    title         = data.get('title', '') or ''
    if not service or not connection_id or arr_id is None:
        return jsonify({'status': 'error', 'message': 'Missing parameters'}), 400

    job_id = secrets.token_hex(8)
    watch  = {
        'status':       'queued',
        'message':      'Queued — waiting for download client',
        'title':        title,
        'service':      service,
        'completed_at': None,
    }
    _import_watches[job_id] = watch
    cfg = db_load_config()

    def do_watch():
        try:
            # Brief delay so qBit + Sonarr/Radarr have time to register the grab before we poll
            time.sleep(8)
            def on_downloading():
                watch['status']  = 'downloading'
                watch['message'] = 'Downloading — verifying in qBittorrent'

            # Snapshot the current file ID so we can confirm import even when the
            # ManualImport command reports status='failed' internally (Radarr quirk)
            original_file_id = get_arr_file_id(cfg, service, connection_id, arr_id)

            last_active = poll_queue_until_clear(cfg, service, connection_id, arr_id, on_downloading=on_downloading)

            if not last_active:
                # Queue cleared naturally (standard quality upgrade auto-imported)
                watch['status']       = 'done'
                watch['message']      = 'Imported successfully'
                watch['completed_at'] = time.time()
                return

            # If the item is still downloading (not yet importPending), the 300 s poll
            # timed out before the download finished — extend the wait instead of
            # firing force import against an incomplete file (which causes a 500 error).
            if not any(r.get('trackedDownloadState') == 'importPending' for r in last_active):
                watch['status']  = 'downloading'
                watch['message'] = 'Downloading — waiting for completion'
                last_active = poll_queue_until_clear(cfg, service, connection_id, arr_id, timeout=7200)
                if not last_active:
                    watch['status']       = 'done'
                    watch['message']      = 'Imported successfully'
                    watch['completed_at'] = time.time()
                    return

            # Queue didn't clear — extract context for manual import
            rec             = last_active[0]
            download_id     = rec.get('downloadId') or ''
            output_path     = rec.get('outputPath') or ''
            download_folder = None
            if output_path:
                import os as _os
                download_folder = _os.path.dirname(output_path) if '.' in _os.path.basename(output_path) else output_path
            if download_id:
                log.info("Manual import will use downloadId %s", download_id)
            elif download_folder:
                log.info("Manual import will use download folder %s", download_folder)

            watch['status']  = 'importing'
            watch['message'] = 'Importing — triggering manual import'

            # Retry loop: fire the command up to 3 times, confirming via both queue state
            # and a direct Arr API check (file ID change) after each attempt.
            still_active = last_active
            for attempt in range(3):
                force_manual_import_by_id(cfg, service, connection_id, arr_id,
                                          download_id=download_id, download_folder=download_folder)
                still_active = poll_queue_until_clear(cfg, service, connection_id, arr_id, timeout=60)
                if not still_active:
                    break
                # Verify directly with Arr — command may have imported even if queue
                # hasn't reflected it yet or command reported 'failed' internally
                current_file_id = get_arr_file_id(cfg, service, connection_id, arr_id)
                if current_file_id is not None and current_file_id != original_file_id:
                    still_active = []
                    break
                if attempt < 2:
                    watch['message'] = f'Importing — retrying ({attempt + 2}/3)'
                    time.sleep(20)

            if still_active:
                stuck_rec = still_active[0]
                msgs = [m for msg in stuck_rec.get('statusMessages', []) for m in msg.get('messages', [])]
                watch['status']  = 'error'
                watch['message'] = 'Import stalled: ' + ('; '.join(msgs) or 'queue item remained')
            else:
                watch['status']       = 'done'
                watch['message']      = 'Imported successfully'
            watch['completed_at'] = time.time()
        except Exception as e:
            log.warning("Auto-import failed for %s/%s: %s", service, arr_id, e)
            watch['status']       = 'error'
            watch['message']      = str(e)
            watch['completed_at'] = time.time()

    threading.Thread(target=do_watch, daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/workflows/watch_import/status')
@require_auth
def workflows_watch_import_status():
    job_id = request.args.get('job_id', '')
    watch  = _import_watches.get(job_id)
    if not watch:
        return jsonify({'status': 'error', 'message': 'Watch not found'}), 404
    return jsonify(watch)


@app.route('/api/workflows/watch_import/active')
@require_auth
def workflows_watch_import_active():
    now = time.time()
    # Expire jobs completed more than 5 minutes ago
    expired = [k for k, v in _import_watches.items() if v.get('completed_at') and now - v['completed_at'] > 300]
    for k in expired:
        del _import_watches[k]
    # Return active jobs + jobs completed within the last 60s (so Done/Error states are briefly visible)
    jobs = []
    for job_id, watch in _import_watches.items():
        ct = watch.get('completed_at')
        if watch['status'] not in ('done', 'error') or (ct and now - ct < 60):
            jobs.append({'job_id': job_id, **{k: v for k, v in watch.items() if k != 'completed_at'}})
    return jsonify({'jobs': jobs})


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    dist = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
    if path and os.path.exists(os.path.join(dist, path)):
        return send_from_directory(dist, path)
    return send_from_directory(dist, 'index.html')
