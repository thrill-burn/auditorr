import os
import threading
import logging
import secrets
import functools
import urllib.parse

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import qbittorrentapi

from db import (
    DATA_DIR,
    init_db,
    db_load_config, db_save_config, validate_config,
    db_load_results, db_save_results,
    db_save_history,
    db_get_last_two_snapshots, db_get_recent_runs,
    db_clear_audit_history,
    db_get_upload_snapshots,
)
from state import get_state, set_state, try_start_scanning
from audit import run_audit_process, compute_diff, process_health_metrics, compute_upload_stats
from arr import _test_arr_connection, arr_rescan, arr_search
from scripts import generate_script
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

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

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
            log.info("Running startup audit...")
            if try_start_scanning("startup"):
                run_audit_process("startup")
            start_watchdog()
    except ImportError:
        # fcntl not available (Windows) — just run without locking
        log.info("Running startup audit...")
        if try_start_scanning("startup"):
            run_audit_process("startup")
        start_watchdog()

threading.Thread(target=startup, daemon=True).start()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/health')
def health_check():
    return jsonify({"status": "ok", "version": "3"}), 200


@app.route('/api/results')
@require_auth
def get_results():
    return jsonify(db_load_results())


@app.route('/api/progress')
@require_auth
def get_progress():
    return jsonify(get_state())


@app.route('/api/changes')
@require_auth
def get_changes():
    snaps = db_get_last_two_snapshots()
    if len(snaps) < 2:
        return jsonify({"changes": None, "message": "Not enough audit history yet."})
    curr = snaps[0]['snapshot']
    prev = snaps[1]['snapshot']
    diff = compute_diff(prev, curr)
    return jsonify({"changes": diff, "prev_ran_at": snaps[1]['ran_at'], "curr_ran_at": snaps[0]['ran_at']})


@app.route('/api/audit_history')
@require_auth
def get_audit_history():
    return jsonify({"runs": db_get_recent_runs(90)})


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
                'QB_HOST':            str(data.get('QB_HOST', '')),
                'QB_USER':            str(data.get('QB_USER', '')),
                'QB_PASS':            str(data['QB_PASS']) if data.get('QB_PASS') else existing.get('QB_PASS',''),
                'MEDIA_PATH':         str(data.get('MEDIA_PATH', '')),
                'REMOTE_PATH':        str(data.get('REMOTE_PATH', '')),
                'LOCAL_PATH':         str(data.get('LOCAL_PATH', '')),
                'WATCHDOG_COOLDOWN':  int(data.get('WATCHDOG_COOLDOWN', 60)),
                'SCHEDULED_INTERVAL': int(data.get('SCHEDULED_INTERVAL', 360)),
                'OR_RATIO':           float(data.get('OR_RATIO',  0.01)),
                'NI_RATIO':           float(data.get('NI_RATIO',  0.01)),
                'DUP_RATIO':          float(data.get('DUP_RATIO', 0.01)),
                'EXCLUSION_PATTERNS': [p for p in data.get('EXCLUSION_PATTERNS', []) if isinstance(p, str)],
                'SONARR_URL':         str(data.get('SONARR_URL', '')),
                'SONARR_API_KEY':     str(data.get('SONARR_API_KEY', '')),
                'RADARR_URL':         str(data.get('RADARR_URL', '')),
                'RADARR_API_KEY':     str(data.get('RADARR_API_KEY', '')),
                'SONARR_REMOTE_PATH': str(data.get('SONARR_REMOTE_PATH', '')),
                'RADARR_REMOTE_PATH': str(data.get('RADARR_REMOTE_PATH', '')),
            }
        except (ValueError, TypeError) as e:
            return jsonify({"status": "error", "message": f"Invalid value: {e}"}), 400
        db_save_config(new_conf)
        threading.Thread(target=restart_watchdog, daemon=True).start()

        # Recompute health metrics immediately using existing scan results
        # so threshold changes are reflected on the dashboard without a full rescan
        try:
            curr = db_load_results()
            if curr.get('media_files') and curr.get('torrent_files'):
                new_dashboard = process_health_metrics(
                    curr['media_files'], curr['torrent_files'], new_conf, update_history=False)
                curr['dashboard'] = new_dashboard
                db_save_results(curr)
        except Exception as e:
            log.warning(f"Could not recompute health metrics after config save: {e}")

        return jsonify({"status": "success", "warnings": warnings})

    cfg = db_load_config()
    if cfg.get('QB_PASS'):
        cfg['QB_PASS'] = '__stored__'
    return jsonify(cfg)


@app.route('/api/test_connection', methods=['POST'])
@require_auth
def test_connection():
    data = request.json or {}
    host = data.get('QB_HOST', '')
    if host:
        parsed = urllib.parse.urlparse(host)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            return jsonify({"status": "error", "message": "QB_HOST must be a valid http:// or https:// URL"}), 400
    try:
        client = qbittorrentapi.Client(
            host=host, username=data.get('QB_USER'), password=data.get('QB_PASS'))
        client.auth_log_in()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/test_sonarr', methods=['POST'])
@require_auth
def test_sonarr():
    data = request.json or {}
    ok, msg = _test_arr_connection(data.get('url', ''), data.get('api_key', ''))
    if ok:
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": msg}), 400


@app.route('/api/test_radarr', methods=['POST'])
@require_auth
def test_radarr():
    data = request.json or {}
    ok, msg = _test_arr_connection(data.get('url', ''), data.get('api_key', ''))
    if ok:
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": msg}), 400


@app.route('/api/actions/script/<script_type>')
@require_auth
def get_action_script(script_type):
    results = db_load_results()
    cfg     = db_load_config()
    try:
        script = generate_script(script_type, results, cfg)
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
    days  = request.args.get('days', 30, type=int)
    days  = max(1, min(365, days))
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


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    dist = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
    if path and os.path.exists(os.path.join(dist, path)):
        return send_from_directory(dist, path)
    return send_from_directory(dist, 'index.html')
