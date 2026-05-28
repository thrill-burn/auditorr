import os
import socket
import threading
import logging
import secrets
import functools

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import sources
from db import (
    DATA_DIR,
    init_db,
    db_load_config, db_save_config, validate_config,
    db_load_results, db_save_results,
    db_load_file_results,
    db_save_history,
    db_get_recent_runs,
    db_clear_audit_history,
    db_get_upload_snapshots,
    db_retag_upload_snapshots, db_count_upload_snapshots_by_source,
    db_delete_upload_snapshots,
    db_get_change_log,
)
from state import get_state, set_state, try_start_scanning
from audit import run_audit_process, process_health_metrics, compute_upload_stats
from arr import _test_arr_connection, arr_rescan, arr_search, fetch_arr_media_index, test_arr_connections
from scripts import generate_script
from relink import find_relink_candidates
from media_server_exclusions import normalize_media_server_presets
from watchdog_handler import restart_watchdog, start_watchdog, _scheduled_audit_loop

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

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
            if _torrent_source_configured(db_load_config()):
                log.info("Running startup audit...")
                if try_start_scanning("startup"):
                    run_audit_process("startup")
            else:
                log.info("Torrent source not configured, skipping startup audit.")
            start_watchdog()
    except ImportError:
        # fcntl not available (Windows) — just run without locking
        if _torrent_source_configured(db_load_config()):
            log.info("Running startup audit...")
            if try_start_scanning("startup"):
                run_audit_process("startup")
        else:
            log.info("Torrent source not configured, skipping startup audit.")
        start_watchdog()

threading.Thread(target=startup, daemon=True).start()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/health')
def health_check():
    return jsonify({"status": "ok", "version": "1.5.0"}), 200


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
    return jsonify(db_load_file_results(tab))


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
            }
        except (ValueError, TypeError) as e:
            return jsonify({"status": "error", "message": f"Invalid value: {e}"}), 400
        db_save_config(new_conf)
        threading.Thread(target=restart_watchdog, daemon=True).start()

        # Recompute health metrics immediately using existing scan results
        # so threshold changes are reflected on the dashboard without a full rescan
        try:
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


@app.route('/api/actions/script/<script_type>')
@require_auth
def get_action_script(script_type):
    cfg = db_load_config()
    results = db_load_results()
    results['torrent_files'] = db_load_file_results('torrents')
    if script_type == 'relink_media_hardlinks':
        media_files = db_load_file_results('media')
        results['media_files'] = media_files
        results['relink_candidates'] = find_relink_candidates(
            media_files,
            results['torrent_files'],
            fetch_arr_media_index(cfg),
            cfg,
        )
    try:
        script = generate_script(script_type, results, cfg)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    return app.response_class(script, mimetype='text/plain; charset=utf-8')


@app.route('/api/actions/relink_candidates')
@require_auth
def actions_relink_candidates():
    cfg = db_load_config()
    media_files = db_load_file_results('media')
    torrent_files = db_load_file_results('torrents')
    arr_media = fetch_arr_media_index(cfg)
    candidates = find_relink_candidates(media_files, torrent_files, arr_media, cfg)
    return jsonify({"status": "success", "candidates": candidates})


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


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    dist = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
    if path and os.path.exists(os.path.join(dist, path)):
        return send_from_directory(dist, path)
    return send_from_directory(dist, 'index.html')
