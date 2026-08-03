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
import ipaddress
import urllib.parse
import urllib.error
from datetime import datetime

from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS

import sources
from db import (
    DATA_DIR,
    DEFAULT_CONFIG, SCORE_WEIGHT_KEYS,
    init_db,
    db_load_config, db_save_config, validate_config,
    db_load_results, db_save_results,
    db_load_file_results, db_stream_file_results, db_has_file_results,
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
from state import (
    get_state, set_state, try_start_scanning,
    note_workflow_request_start, note_workflow_request_end, workflow_active,
)
from audit import run_audit_process, process_health_metrics, compute_upload_stats, _is_not_imported_torrent, _compute_cross_seed_stats
from arr import _test_arr_connection, arr_rescan, arr_search, fetch_arr_media_index, test_arr_connections, fetch_arr_indexers, fetch_release_matrix, grab_release, normalize_arr_connections, poll_queue_until_clear, force_manual_import_by_id, get_arr_file_id, parse_release_info_for_path, fetch_arr_all_titles, title_match_keys, compare_release_quality, parse_trump_pm, match_trump_release, match_trumped_torrent, rank_release_matches, score_release_match, title_soft_match
from scripts import generate_script, _build_dup_groups, dup_group_inputs
from media_server_exclusions import normalize_disc_rip_presets, normalize_media_server_presets
from watchdog_handler import restart_watchdog, start_watchdog, _scheduled_audit_loop
from debug import (
    install_ring_buffer, build_debug_report, memory_pressure, cgroup_oom_events,
    start_memory_monitor, record_heavy_request, malloc_trim, process_rss_mb,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

APP_VERSION = "1.7.1"

# Keep the last ~400 log records in memory for /api/debug/report
install_ring_buffer()

# Always-on memory timeline (60s samples + persisted hourly aggregates) so the
# debug report can show whether RSS climbs continuously (leak) or staircases at
# scans / workflow page loads (allocator ratchet).
start_memory_monitor()

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
# #18: with no secret configured, only local (loopback/private-range) clients
# are served — a port-forwarded or otherwise internet-reachable instance no
# longer runs open. A configured secret is enforced for every client.
# AUDITORR_REQUIRE_AUTH=true drops the local exemption entirely (strict mode).
AUDITORR_REQUIRE_AUTH = os.environ.get(
    'AUDITORR_REQUIRE_AUTH', '').strip().lower() in ('1', 'true', 'yes')


def _parse_trusted_networks(raw):
    # Extra CIDRs treated as local, e.g. Tailscale's 100.64.0.0/10 — those
    # aren't RFC1918 so the built-in private-range check won't cover them.
    nets = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("Ignoring invalid AUDITORR_TRUSTED_NETWORKS entry: %r", part)
    return nets


AUDITORR_TRUSTED_NETWORKS = _parse_trusted_networks(
    os.environ.get('AUDITORR_TRUSTED_NETWORKS', ''))

# Initialise DB tables and run JSON migrations on import
init_db()

# Start the scheduled fallback audit loop
threading.Thread(target=_scheduled_audit_loop, daemon=True).start()

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

def _is_local_client(addr):
    # remote_addr only — X-Forwarded-For is attacker-controlled and trusting it
    # would let a remote client spoof a private address. Behind a same-host or
    # LAN reverse proxy the proxy's address is what's (correctly) evaluated.
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_private covers loopback, RFC1918, IPv6 ULA and link-local; on modern
    # Python it tracks the IANA registry, so other non-internet-routable blocks
    # (TEST-NETs, benchmarking) also pass — harmless, they can't arrive from
    # the internet as source addresses.
    return ip.is_private or any(ip in net for net in AUDITORR_TRUSTED_NETWORKS)


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if AUDITORR_SECRET:
            # A configured secret is enforced for every client, local included.
            # Header only — a secret in the query string leaks into logs and
            # browser history.
            provided = request.headers.get('X-Auditorr-Secret') or ''
            if not secrets.compare_digest(provided, AUDITORR_SECRET):
                return jsonify({"status": "error", "message": "Unauthorized"}), 401
            return f(*args, **kwargs)
        if AUDITORR_REQUIRE_AUTH:
            # Strict mode with nothing to authenticate against: refuse rather
            # than run open. 503, not 401 — no credential could succeed.
            return jsonify({
                "status": "error",
                "code": "auth_not_configured",
                "message": "AUDITORR_REQUIRE_AUTH is set but AUDITORR_SECRET is "
                           "not. Set AUDITORR_SECRET in the container environment "
                           "and restart auditorr.",
            }), 503
        if _is_local_client(request.remote_addr):
            return f(*args, **kwargs)
        # No secret configured and a non-local client: fail closed (#18) with a
        # generic 401 that doesn't reveal whether a secret exists.
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
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
# Heavy-endpoint memory hooks
# ---------------------------------------------------------------------------
# These endpoints deserialize the stored file lists in full — a multi-GB object
# graph on 500K+ file libraries. RSS is recorded around each call for the debug
# report's heavy_requests ring, and freed pages are handed back to the OS after
# the response is built so a workflow page visit doesn't permanently ratchet
# the process footprint. Exact paths, so generate/status polls don't match.

_HEAVY_MEM_PATHS = frozenset({
    '/api/workflows/triage',
    '/api/workflows/cleanup',
    '/api/workflows/dedupe',
    '/api/workflows/acquire_candidates',
    '/api/workflows/generate',
})


def _is_heavy_mem_path(path):
    return path in _HEAVY_MEM_PATHS or path.startswith('/api/actions/script/')


@app.before_request
def _heavy_request_mem_start():
    if _is_heavy_mem_path(request.path):
        g._heavy_mem_probe = (time.time(), process_rss_mb())


@app.after_request
def _heavy_request_mem_end(response):
    probe = g.pop('_heavy_mem_probe', None)
    if probe is not None:
        started, rss_before = probe
        rss_after = process_rss_mb()
        # The handler's parsed file lists are unreferenced once it returns —
        # trim so their pages actually leave the process instead of ratcheting.
        malloc_trim()
        record_heavy_request(request.path, rss_before, rss_after, process_rss_mb(),
                             round((time.time() - started) * 1000))
    return response


# ---------------------------------------------------------------------------
# Workflow activity hooks — background scans (watchdog / scheduled) defer
# while a workflow session is in use so scan RSS and request RSS don't stack
# (see state.workflow_active). Status/active polls are excluded: the frontend
# polls watch_import/active every 5s unconditionally, and counting those
# would keep the signal fresh forever.
# ---------------------------------------------------------------------------

def _is_workflow_activity_path(path):
    if path.startswith('/api/actions/script/'):
        return True
    if not path.startswith('/api/workflows/'):
        return False
    return not path.endswith(('/status', '/active'))


@app.before_request
def _workflow_activity_start():
    if _is_workflow_activity_path(request.path):
        g._workflow_activity = True
        note_workflow_request_start()


@app.teardown_request
def _workflow_activity_end(exc=None):
    # teardown_request (not after_request) so the in-flight counter can't
    # leak upward when a handler raises.
    if g.pop('_workflow_activity', False):
        note_workflow_request_end()


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
                      source=db_load_config().get('TORRENT_SOURCE', 'qbit'),
                      peak_rss_mb=marker.get('peak_rss_mb') or marker.get('last_rss_mb') or marker.get('rss_mb'))
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
    report = build_debug_report(APP_VERSION)
    # In-memory job-store sizes (owned by this module) — gauges for the
    # "slow unbounded growth" suspects.
    report['app_gauges'] = {
        'release_jobs':         len(_release_jobs),
        'import_watches':       len(_import_watches),
        'generate_job_results': len((_gen_state.get('job') or {}).get('results') or []),
        # True while background scans are deferring to an active workflow
        # session — evidence for "why didn't the watchdog scan run yet"
        'workflow_active':      workflow_active(),
    }
    return jsonify(report)


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
                'ALLOW_CLIENT_DELETE': bool(data.get('ALLOW_CLIENT_DELETE', existing.get('ALLOW_CLIENT_DELETE', False))),
                'MEDIA_PATH':         str(data.get('MEDIA_PATH', '')),
                'REMOTE_PATH':        str(data.get('REMOTE_PATH', '')),
                'LOCAL_PATH':         str(data.get('LOCAL_PATH', '')),
                'WATCHDOG_ENABLED':   bool(data.get('WATCHDOG_ENABLED', True)),
                'WATCHDOG_COOLDOWN':  int(data.get('WATCHDOG_COOLDOWN', 60)),
                'SCHEDULED_INTERVAL': int(data.get('SCHEDULED_INTERVAL', 360)),
                'OR_RATIO':           float(data.get('OR_RATIO',  0.01)),
                'NI_RATIO':           float(data.get('NI_RATIO',  0.01)),
                'DUP_RATIO':          float(data.get('DUP_RATIO', 0.01)),
                **{k: float(data.get(k, existing.get(k, DEFAULT_CONFIG[k])))
                   for k in SCORE_WEIGHT_KEYS},
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
                    # cross_seed_stats is added by the audit, not process_health_metrics —
                    # recompute it here so the config-save dashboard refresh doesn't drop
                    # the Cross-Seed Effectiveness / Tracker Leaderboard panels.
                    cs_stats = _compute_cross_seed_stats(media_files)
                    if cs_stats:
                        new_dashboard['cross_seed_stats'] = cs_stats
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
# Hard cap per /triage/verify request. The client sends smaller batches
# sequentially, so the per-torrent tracker fan-out (8-wide pools in sources/)
# never runs longer than a few seconds per HTTP request and total concurrency
# against qui/qBittorrent stays at the audit's own ceiling.
_TRIAGE_VERIFY_BATCH_MAX = 200


def _triage_verdict_under(alternatives, health):
    """Select a verdict from its live-health alternatives (None → drop row)."""
    if health == 'working':
        return alternatives['working']
    if health == 'unregistered':
        return alternatives['unregistered']
    return alternatives['other']


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


def _partition_removal_by_file_sharing(cfg, items):
    """Split a removal set by whether each torrent's files survive its removal.

    A cross-seed can share its files with another torrent in one of two ways:
      - shared path (one file, several registrations): deleting the file breaks
        every other torrent pointing at it → those files must be KEPT.
      - distinct hardlink (own path, shared inode): deleting drops only this
        torrent's link; the library and sibling hardlinks survive → safe to
        DELETE, and deleting avoids leaving an orphaned file behind.

    The deciding question is per torrent: is any of its files also owned by a
    torrent that is NOT being removed? If so, keep files; otherwise delete them.
    Only same-size torrents can share a file, so the live path lookup is bounded
    to that candidate set (mirrors the Trumped group resolver).

    Returns (delete_items, keep_items).
    """
    remove_hashes = {i['hash'] for i in items if i.get('hash')}
    rows      = sources.list_torrents(cfg)
    by_hash   = {r['hash']: r for r in rows}
    seeds     = [by_hash[i['hash']] for i in items if i.get('hash') in by_hash]
    sizes     = {s['size'] for s in seeds}
    # Removed torrents + any same-size torrent that could share a path with them.
    candidates = [r for r in rows if r['size'] in sizes]
    paths_map  = sources.fetch_torrent_file_paths(cfg, candidates)

    owners = {}  # path -> set(hashes referencing it)
    for h, paths in paths_map.items():
        for p in paths:
            owners.setdefault(p, set()).add(h)

    delete_items, keep_items = [], []
    for it in items:
        h = it.get('hash')
        if not h:
            continue
        my_paths = paths_map.get(h, [])
        shared = any(any(o not in remove_hashes for o in owners.get(p, ()))
                     for p in my_paths)
        (keep_items if shared else delete_items).append(it)
    return delete_items, keep_items


@app.route('/api/workflows/remove_torrents', methods=['POST'])
@require_auth
def workflows_remove_torrents():
    """Remove selected torrents from the client, optionally deleting their files.

    `delete_files` accepts true / false / "auto":
      true  — delete each torrent's files (caller asserts it is safe)
      false — remove the registration only, keep every file
      auto  — per torrent, delete files only when they are not shared with a
              surviving torrent (cross-seed safe across every topology); files
              shared with a still-seeding sibling are kept so it is never broken.

    Destructive — gated behind the ALLOW_CLIENT_DELETE config flag (off by
    default) so conservative users can keep auditorr strictly read-only
    against their client.
    """
    cfg = db_load_config()
    if not cfg.get('ALLOW_CLIENT_DELETE'):
        return jsonify({
            "status": "error",
            "message": "Client deletion is disabled — enable it in Config → Torrent Source first.",
        }), 403
    data  = request.json or {}
    items = [
        {'hash': str(i.get('hash') or ''), 'instance_id': i.get('instance_id')}
        for i in (data.get('items') or []) if i.get('hash')
    ]
    if not items:
        return jsonify({"status": "error", "message": "No torrent hashes provided"}), 400

    mode = data.get('delete_files', True)
    try:
        if mode == 'auto':
            delete_items, keep_items = _partition_removal_by_file_sharing(cfg, items)
            removed  = sources.remove_torrents(cfg, delete_items, delete_files=True) if delete_items else 0
            removed += sources.remove_torrents(cfg, keep_items, delete_files=False) if keep_items else 0
            files_deleted, files_kept = len(delete_items), len(keep_items)
        else:
            removed = sources.remove_torrents(cfg, items, delete_files=bool(mode))
            files_deleted = removed if bool(mode) else 0
            files_kept    = 0 if bool(mode) else removed
    except sources.SourceConnectionError as e:
        return jsonify({"status": "error", "message": str(e)}), 502
    log.info("Client delete: removed %d/%d torrent(s) (mode=%s, files_deleted=%d, files_kept=%d)",
             removed, len(items), mode, files_deleted, files_kept)
    return jsonify({"status": "success", "removed": removed, "requested": len(items),
                    "files_deleted": files_deleted, "files_kept": files_kept})


@app.route('/api/workflows/triage/resolve_groups', methods=['POST'])
@require_auth
def workflows_triage_resolve_groups():
    """Resolve each selected Triage torrent to its full live cross-seed group.

    Triage records keep one hash per path (the healthiest claimant), so the
    sibling cross-seeds are invisible in the report. This queries the client
    directly so the delete modal can offer 'this torrent only' vs 'all N
    cross-seeds'.

    Cross-seed file topology is NOT uniform: siblings may each hold a distinct
    hardlink to a shared inode (deleting one's files is safe), OR several may
    register against one shared file at the same path (deleting that file breaks
    the others). Each member therefore carries `shares_path` — true when it
    shares a content path with another member — so the UI can warn, and the
    server's delete_files='auto' mode keeps shared files while dropping
    distinct-hardlink ones.
    """
    data   = request.json or {}
    hashes = [str(h) for h in (data.get('hashes') or []) if h]
    if not hashes:
        return jsonify({"status": "error", "message": "No torrent hashes provided"}), 400
    cfg = db_load_config()
    try:
        rows = sources.list_torrents(cfg)
    except sources.SourceConnectionError as e:
        return jsonify({"status": "error", "message": str(e)}), 502

    by_hash    = {r['hash']: r for r in rows}
    seeds      = [by_hash[h] for h in hashes if h in by_hash]
    sizes      = {s['size'] for s in seeds}
    candidates = [r for r in rows if r['size'] in sizes]
    paths_map  = sources.fetch_torrent_file_paths(cfg, candidates)

    groups = {s['hash']: _cross_seed_group(candidates, paths_map, s) for s in seeds}

    # Enrich every unique group member with live details (seeding time, tracker
    # health) — one batched call across all resolved groups.
    seen, members_in = set(), []
    for g in groups.values():
        for t in g:
            if t['hash'] not in seen:
                seen.add(t['hash'])
                members_in.append({'hash': t['hash'], 'instance_id': t.get('instance_id')})
    try:
        details = sources.fetch_torrent_details(cfg, members_in)
    except Exception as e:
        log.warning("Triage resolve_groups: detail fetch failed: %s", e)
        details = {}

    out = {}
    for h, g in groups.items():
        members = []
        for t in g:
            det = details.get(t['hash'], {})
            t_paths = set(t.get('paths') or [])
            # Does this member share a content path with another group member?
            # If so, deleting its files would break that sibling (shared file);
            # if not, its files are its own distinct hardlink (safe to delete).
            shares_path = any(o is not t and t_paths and t_paths & set(o.get('paths') or [])
                              for o in g)
            members.append({
                'hash':           t['hash'],
                'instance_id':    t.get('instance_id'),
                'name':           t['name'],
                'tracker':        t.get('tracker') or '',
                'size':           t['size'],
                'seeding_time':   det.get('seeding_time'),
                'uploaded':       det.get('uploaded'),
                'tracker_health': det.get('tracker_health', 'unknown'),
                'tracker_msg':    det.get('tracker_msg', ''),
                'shares_path':    shares_path,
            })
        members.sort(key=lambda m: m['name'])
        out[h] = members

    # Hashes not found in the client get an empty group → the modal falls back
    # to a single-torrent delete for them.
    for h in hashes:
        out.setdefault(h, [])
    return jsonify({"status": "success", "groups": out})


# ---------------------------------------------------------------------------
# Trumped workflow — PM-driven swap of a trumped release for its replacement.
# See prompts/TRUMP.md for the full design. auditorr never contacts a tracker:
# group resolution and deletion go through the client, search/grab through
# Sonarr/Radarr.
# ---------------------------------------------------------------------------

def _trump_year_ok(parsed, t):
    """Radarr year guard (±1) so same-title remakes can't cross-match."""
    return not (parsed['year'] is not None and t.get('service') == 'radarr'
                and t.get('year') and abs(int(t['year']) - parsed['year']) > 1)


def _trump_find_arr_item(cfg, parsed):
    """Match a parsed release against managed arr titles (title + year aware).

    Exact first (diacritic/apostrophe-folded title keys, same as Triage), then a
    **soft fallback** ranked by shared title core — a trump's new-release name
    often carries a stray season token or extra scene tokens the exact key match
    can't fold ('… Rides Again S01' vs the series 'The Magic School Bus Rides
    Again'), so a graded title match beats a hard 404. Content-type service
    preference (season/episode → sonarr, otherwise radarr) breaks ties.
    """
    titles    = fetch_arr_all_titles(cfg)
    preferred = 'sonarr' if parsed['season'] is not None else 'radarr'

    keys = title_match_keys(parsed['title'])
    exact = [t for t in titles
             if (title_match_keys(t.get('title') or '') & keys) and _trump_year_ok(parsed, t)]
    if exact:
        exact.sort(key=lambda t: 0 if t.get('service') == preferred else 1)
        return exact[0]

    # Soft fallback — best title-core overlap above a real floor.
    best, best_sim = None, 0.0
    for t in titles:
        if not _trump_year_ok(parsed, t):
            continue
        sim = title_soft_match(parsed['title'], t.get('title') or '')
        # Nudge the preferred service so a movie/series tie goes the right way.
        if t.get('service') == preferred:
            sim += 0.001
        if sim > best_sim:
            best, best_sim = t, sim
    return best if best_sim >= 0.5 else None


@app.route('/api/workflows/trump/parse', methods=['POST'])
@require_auth
def workflows_trump_parse():
    """Extract old/new release titles from a pasted trump PM. Pure text parsing.

    `old_titles` is a list — a season-pack trump lists one trumped episode per
    line, all replaced by a single pack.
    """
    data = request.json or {}
    old_titles, new_title = parse_trump_pm(data.get('pm_text') or '')
    if not old_titles and not new_title:
        return jsonify({
            "status": "error",
            "message": "Could not find the “will be replaced by” phrase — paste the full PM or fill in the titles manually.",
        }), 422
    return jsonify({"status": "success", "old_titles": old_titles, "new_title": new_title})


def _cross_seed_group(rows, paths_map, seed):
    """The cross-seed group of `seed`, drawn from `rows`.

    A sibling is any torrent with the same payload size that shares at least one
    content file path with the seed (hardlinked cross-seeds point at the same
    files). The seed itself is always included. `paths_map` is {hash: [paths]}.
    Each returned row gains a sorted 'paths' list.
    """
    seed_paths = set(paths_map.get(seed['hash'], []))
    group = []
    for r in rows:
        if r['size'] != seed['size']:
            continue
        ps = set(paths_map.get(r['hash'], []))
        if r['hash'] == seed['hash'] or (seed_paths and ps & seed_paths):
            group.append({**r, 'paths': sorted(ps)})
    return group


def _trump_pick_row(row):
    """Trim a live torrent row to the fields the candidate picker needs."""
    return {
        'hash':          row.get('hash'),
        'name':          row.get('name') or '',
        'tracker':       row.get('tracker') or '',
        'size':          row.get('size') or 0,
        'instance_id':   row.get('instance_id'),
        'instance_name': row.get('instance_name'),
        'match_score':   row.get('match_score'),
        'match':         row.get('match'),
    }


@app.route('/api/workflows/trump/resolve_group', methods=['POST'])
@require_auth
def workflows_trump_resolve_group():
    """Resolve the trumped release(s) to their full cross-seed group(s), live.

    Two phases on one endpoint. **Phase 1** (no `seed_hashes`): for each old
    title, return a ranked list of candidate torrents the user picks from
    (`status: needs_pick`) — the confident exact/subset match is pre-selected,
    but the user always confirms before anything is expanded or deleted. Soft
    matching can mis-rank a PM whose rendering differs from the torrent name, so
    a list the user vets beats a single guess. **Phase 2** (with `seed_hashes`):
    take the confirmed seeds and expand each into its cross-seed group — every
    torrent with the same payload size that shares ≥1 content file path. Audit
    records keep one hash per path, so siblings are enumerated live from the
    client, not from records.
    """
    data = request.json or {}
    old_titles = data.get('old_titles')
    if not old_titles:
        single = str(data.get('old_title') or '').strip()
        old_titles = [single] if single else []
    old_titles = [str(t).strip() for t in old_titles if str(t).strip()]
    if not old_titles:
        return jsonify({"status": "error", "message": "old_titles is required"}), 400
    cfg = db_load_config()
    try:
        rows = sources.list_torrents(cfg)
    except sources.SourceConnectionError as e:
        return jsonify({"status": "error", "message": str(e)}), 502

    seed_hashes = [str(h).strip() for h in (data.get('seed_hashes') or []) if str(h).strip()]

    # Phase 1 — rank candidates per title; the user confirms the seed set.
    if not seed_hashes:
        picks = []
        for title in old_titles:
            ranked = rank_release_matches(rows, title, name_key='name', limit=8)
            auto   = match_trumped_torrent(rows, title)
            # The conservative exact/subset matcher is the trusted pre-selection;
            # make sure it's present in (and at the head of) the ranked list.
            if auto is not None and all(c['hash'] != auto['hash'] for c in ranked):
                s, brk = score_release_match(title, auto['name'])
                ranked.insert(0, {**auto, 'match_score': round(s, 3), 'match': brk})
            auto_hash = auto['hash'] if auto is not None else (ranked[0]['hash'] if ranked else None)
            picks.append({
                'title':      title,
                'auto':       auto_hash,
                'candidates': [_trump_pick_row(c) for c in ranked],
            })
        return jsonify({"status": "needs_pick", "picks": picks})

    # Phase 2 — expand the confirmed seeds into their full cross-seed groups.
    by_hash = {r['hash']: r for r in rows}
    seeds   = [by_hash[h] for h in dict.fromkeys(seed_hashes) if h in by_hash]
    if not seeds:
        return jsonify({
            "status": "error",
            "message": "None of the selected torrents are still in the client — re-run the search.",
        }), 404

    # Cross-seed siblings share a payload size; only those rows can join a group.
    sizes      = {s['size'] for s in seeds}
    candidates = [r for r in rows if r['size'] in sizes]
    paths_map  = sources.fetch_torrent_file_paths(cfg, candidates)

    group_by_hash, total_size = {}, 0
    for seed in seeds:
        # A seed already pulled into an earlier seed's group shares that payload —
        # its torrents are present and its size is already counted.
        if seed['hash'] in group_by_hash:
            continue
        total_size += seed['size']
        for g in _cross_seed_group(candidates, paths_map, seed):
            group_by_hash.setdefault(g['hash'], g)
    group = list(group_by_hash.values())

    try:
        details = sources.fetch_torrent_details(cfg, group)
    except Exception as e:
        log.warning("Trump: torrent detail fetch failed: %s", e)
        details = {}
    for g in group:
        det = details.get(g['hash'], {})
        g['uploaded']       = det.get('uploaded')
        g['seeding_time']   = det.get('seeding_time')
        g['tracker_health'] = det.get('tracker_health', 'unknown')
        g['tracker_msg']    = det.get('tracker_msg', '')
    group.sort(key=lambda g: g['name'])
    return jsonify({
        "status":       "success",
        "torrents":     group,
        "total_size":   total_size,
        "matched_name": seeds[0]['name'],
    })


@app.route('/api/workflows/trump/search_release', methods=['POST'])
@require_auth
def workflows_trump_search_release():
    """Find the replacement release in Sonarr/Radarr's release search.

    Exact normalized title match (release names are effectively unique ids),
    optionally restricted to the indexer the PM came from. Always returns the
    arr deep link as a manual fallback.
    """
    data      = request.json or {}
    new_title = str(data.get('new_title') or '').strip()
    indexer   = str(data.get('indexer') or '').strip()
    if not new_title:
        return jsonify({"status": "error", "message": "new_title is required"}), 400
    cfg    = db_load_config()
    parsed = parse_release_info_for_path(new_title)
    item   = _trump_find_arr_item(cfg, parsed)
    if item is None:
        return jsonify({
            "status": "error",
            "message": f"No Sonarr/Radarr entry matches “{parsed['title'] or new_title}” — add the title to an arr first, then retry.",
        }), 404

    conn = next((c for c in normalize_arr_connections(cfg)
                 if c['id'] == item.get('connection_id')), None)
    prefix = '/movie/' if item.get('service') == 'radarr' else '/series/'
    fallback_url = ''
    if conn:
        fallback_url = (conn['base_url'].rstrip('/') + prefix
                        + (item.get('title_slug') or str(item.get('arr_id') or '')))

    try:
        if item['service'] == 'radarr':
            releases = fetch_release_matrix(cfg, 'radarr', item['connection_id'], item['arr_id'])
        elif parsed['episode'] is not None:
            releases = fetch_release_matrix(cfg, 'sonarr', item['connection_id'], item['arr_id'],
                                            file_path=new_title)
        elif parsed['season'] is not None:
            releases = fetch_release_matrix(cfg, 'sonarr', item['connection_id'], item['arr_id'],
                                            season_number=parsed['season'])
        else:
            return jsonify({
                "status": "error",
                "message": "Could not determine season/episode from the new release name.",
                "fallback_url": fallback_url,
            }), 422
    except Exception as e:
        return jsonify({"status": "error", "message": f"Release search failed: {e}",
                        "fallback_url": fallback_url}), 502

    # The exact match is the trusted auto-pick; the ranked list is the fallback
    # when the PM's rendering of the new title doesn't match any release name
    # exactly, and an "other matches" affordance when it does. Restrict the pool
    # to the PM's indexer first (when given) so the candidate list stays focused.
    pool = [r for r in releases
            if not indexer or (r.get('indexer') or '').lower() == indexer.lower()]
    release    = match_trump_release(releases, new_title, indexer)
    candidates = rank_release_matches(pool or releases, new_title, name_key='title', limit=8)
    return jsonify({
        "status":          "success",
        "release":         release,
        "candidates":      candidates,
        "candidate_count": len(releases),
        "service":         item['service'],
        "connection_id":   item['connection_id'],
        "arr_id":          item['arr_id'],
        "arr_title":       item.get('title') or '',
        "arr_year":        item.get('year'),
        "fallback_url":    fallback_url,
    })


@app.route('/api/workflows/trump/execute', methods=['POST'])
@require_auth
def workflows_trump_execute():
    """Nuke the confirmed group via the client, grab the replacement, re-audit.

    Gated by ALLOW_CLIENT_DELETE like every destructive client action. The
    grab half is optional — when no release matched, the user grabs manually
    via the arr deep link and this only deletes.
    """
    cfg = db_load_config()
    if not cfg.get('ALLOW_CLIENT_DELETE'):
        return jsonify({
            "status": "error",
            "message": "Client deletion is disabled — enable it in Config → Torrent Source first.",
        }), 403
    data  = request.json or {}
    items = [{'hash': str(i.get('hash') or ''), 'instance_id': i.get('instance_id')}
             for i in (data.get('hashes') or []) if i.get('hash')]
    if not items:
        return jsonify({"status": "error", "message": "No torrent hashes provided"}), 400
    try:
        removed = sources.remove_torrents(cfg, items, delete_files=True)
    except sources.SourceConnectionError as e:
        return jsonify({"status": "error", "message": str(e)}), 502

    grabbed, grab_error = None, ''
    release = data.get('release') or {}
    if release.get('guid') and data.get('service'):
        try:
            grab_release(cfg, data['service'], data.get('connection_id'),
                         release['guid'], release.get('indexer_id'))
            grabbed = True
        except Exception as e:
            grabbed = False
            grab_error = str(e)

    rescan = False
    if try_start_scanning("trump"):
        threading.Thread(target=run_audit_process, args=("trump",), daemon=True).start()
        rescan = True
    log.info("Trump execute: removed %d/%d torrent(s), grabbed=%s", removed, len(items), grabbed)
    return jsonify({"status": "success", "removed": removed, "requested": len(items),
                    "grabbed": grabbed, "grab_error": grab_error, "rescan_started": rescan})


@app.route('/api/workflows/triage')
@require_auth
def workflows_triage():
    """Classify every problem torrent into an actionable verdict (phase 1).

    Covers two candidate sets: not-imported torrents, and imported torrents
    whose audit-time tracker check flagged the torrent as unregistered.

    Verdicts (priority order):
      dead_seed       — imported AND tracker-dead: deleting via the client is
                        lossless (the library hardlink keeps the data)
      unregistered    — not imported, tracker no longer registers the torrent
      superseded      — the library already has this title (possibly different quality)
      import_pending  — title is managed by Sonarr/Radarr but has no library file
      not_in_library  — title matches nothing in any Arr instance

    Two-phase contract: this endpoint answers from audit-time data only — no
    torrent-client calls — so the page renders immediately. Each item carries
    `verdict_alternatives` (its verdict under live health working/unregistered/
    other; None = drop as recovered); the client then re-verifies hashes in
    batches via /triage/verify and applies the alternatives, converging on
    exactly what the old single-shot endpoint returned.
    """
    cfg = db_load_config()
    # Compact working set persisted by the audit (the only records the filters
    # below can select). Databases whose last audit predates the subset row
    # fall back to the full torrent list until the next scan.
    if db_has_file_results('triage'):
        torrent_files = db_load_file_results('triage')
    else:
        torrent_files = db_load_file_results('torrents')
    not_imported  = [f for f in torrent_files if _is_not_imported_torrent(f)]
    # Dead seeds: fully imported but the tracker no longer registers the
    # torrent (flag captured at audit time). Deleting these via the client is
    # lossless — the hardlinked library copy keeps the data alive.
    dead_seeds = [f for f in torrent_files
                  if f.get('imported') and not f.get('excluded')
                  and f.get('status') != 'Orphaned'
                  and f.get('tracker_health') == 'unregistered']

    # Group files by torrent hash — verdicts are per torrent, not per file
    groups = {}
    for f in not_imported:
        key = f.get('hash') or f['path']
        g = groups.setdefault(key, {
            'hash':          f.get('hash') or '',
            'instance_id':   f.get('instance_id'),
            'files':         [],
            'total_size':    0,
            'trackers':      set(),
            'imported':      False,
            # Audit-time health/msg: the phase-1 answer until live verify lands
            'stored_health': f.get('tracker_health') or 'unknown',
            'stored_msg':    f.get('tracker_msg') or '',
        })
        g['files'].append(f)
        g['total_size'] += f['size']
        g['trackers'].update(t for t in (f.get('trackers') or []) if t != 'None')
    for f in dead_seeds:
        key = f.get('hash') or f['path']
        existing = groups.get(key)
        if existing is not None and not existing['imported']:
            continue  # partially-imported torrent — already triaged normally
        g = groups.setdefault(key, {
            'hash':          f.get('hash') or '',
            'instance_id':   f.get('instance_id'),
            'files':         [],
            'total_size':    0,
            'trackers':      set(),
            'imported':      True,
            'stored_health': 'unregistered',   # the dead_seeds filter above
            'stored_msg':    f.get('tracker_msg') or '',
        })
        g['files'].append(f)
        g['total_size'] += f['size']
        g['trackers'].update(t for t in (f.get('trackers') or []) if t != 'None')

    group_list = sorted(groups.values(), key=lambda g: -g['total_size'])
    truncated  = len(group_list) > _TRIAGE_GROUP_CAP
    group_list = group_list[:_TRIAGE_GROUP_CAP]

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
            titles_by_norm.setdefault(key, []).append(t)

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

        tracker_health = g.get('stored_health') or 'unknown'
        parsed_keys    = title_match_keys(parsed['title'])
        is_episode     = parsed['season'] is not None

        # Same-title remakes ("The Smashing Machine" 2002 vs 2025) must not
        # match each other: when the release name carries a year, a radarr
        # candidate with a different year is rejected. Only enforced for
        # movies — TV release names often carry air dates, not series years.
        def _year_ok(entry):
            if parsed['year'] is None or entry.get('service') != 'radarr':
                return True
            if not entry.get('year'):
                return True
            return abs(int(entry['year']) - parsed['year']) <= 1

        # Library rows with files, preferring the service that fits the content type
        lib_rows = next((lib_by_title[k] for k in parsed_keys if k in lib_by_title), [])
        lib_rows = [r for r in lib_rows if _year_ok(r)]
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

        arr_title_hit = next(
            (t for k in parsed_keys for t in titles_by_norm.get(k, []) if _year_ok(t)),
            None)
        in_arr = arr_title_hit is not None

        # What this torrent is when the tracker doesn't say 'unregistered' —
        # health is the only live input to classification, so precomputing the
        # verdict under every health outcome lets the client apply the live
        # answer without re-running any of this.
        if library_match:
            fallback = 'superseded'
        elif in_arr or lib_rows:
            fallback = 'import_pending'
        else:
            fallback = 'not_in_library'

        if g['imported']:
            # 'working' → None: a torrent the tracker answers for again has
            # recovered (re-registered) — the row disappears on live verify.
            alternatives = {'working': None, 'unregistered': 'dead_seed', 'other': 'dead_seed'}
        else:
            alternatives = {'working': fallback, 'unregistered': 'unregistered', 'other': fallback}

        verdict = _triage_verdict_under(alternatives, tracker_health)
        if verdict is None:
            continue

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
                'quality_cmp':  compare_release_quality(parsed, lib_quality),
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
                'quality_cmp':  'unknown',
            }

        # A byte-identical copy of this torrent's data already exists on disk
        # (audit duplicate detection) but isn't hardlinked — a lossless Dedupe
        # target. Distinct from a "same quality" alternate, which is separate data.
        is_duplicate = any(f.get('duplicate_paths') for f in g['files'])

        items.append({
            'hash':           g['hash'],
            'instance_id':    g['instance_id'],
            'rep_path':       rep['path'],
            'paths':          [f['path'] for f in g['files']],
            'file_count':     len(g['files']),
            'total_size':     g['total_size'],
            'trackers':       sorted(g['trackers']),
            'verdict':        verdict,
            'verdict_alternatives': alternatives,
            'is_duplicate':   is_duplicate,
            'parsed':         parsed,
            'library':        lib_payload,
            'tracker_health': tracker_health,
            'tracker_msg':    g['stored_msg'],
            # Live-only fields — filled in by /triage/verify
            'uploaded':       None,
            'ratio':          None,
            'seeding_time':   None,
            'added_on':       None,
        })

    # Dead registrations: torrents the tracker dropped whose payload is still
    # alive — on a working cross-seed sibling and/or the hardlinked library copy.
    # The audit merge keeps the healthy claimant per inode and stashes the dead
    # ones in `dead_siblings`; they are invisible everywhere else. Surface each
    # as its own removable registration (delete_files='auto' keeps shared files,
    # drops a distinct hardlink's own file so nothing is orphaned).
    seen_hashes  = {i['hash'] for i in items if i['hash']}
    dead_reg = {}
    for f in torrent_files:
        if f.get('excluded') or not f.get('dead_siblings'):
            continue
        for s in f['dead_siblings']:
            h = s.get('hash')
            if not h or h in seen_hashes:
                continue
            g = dead_reg.setdefault(h, {
                'hash': h, 'instance_id': s.get('instance_id'),
                'files': [], 'total_size': 0, 'trackers': set(),
                'stored_msg': s.get('tracker_msg') or '',
                'alive_library': False, 'alive_sibling': False,
            })
            g['files'].append(f)
            g['total_size'] += f['size']
            g['trackers'].update(t for t in (f.get('trackers') or []) if t != 'None')
            if f.get('imported'):
                g['alive_library'] = True
            if f.get('tracker_health') == 'working':
                g['alive_sibling'] = True

    dead_reg_list = sorted(dead_reg.values(), key=lambda g: -g['total_size'])[:_TRIAGE_GROUP_CAP]
    for g in dead_reg_list:
        videos = [f for f in g['files']
                  if os.path.splitext(f['path'])[1].lower() in _VIDEO_EXTS]
        rep = max(videos or g['files'], key=lambda f: f['size'])
        items.append({
            'hash':           g['hash'],
            'instance_id':    g['instance_id'],
            'rep_path':       rep['path'],
            'paths':          [f['path'] for f in g['files']],
            'file_count':     len(g['files']),
            'total_size':     g['total_size'],
            'trackers':       sorted(g['trackers']),
            'verdict':        'dead_registration',
            # Live re-verify happens client-side: a registration the tracker
            # answers for again has recovered — drop it, exactly like dead_seed.
            'verdict_alternatives': {'working': None,
                                     'unregistered': 'dead_registration',
                                     'other': 'dead_registration'},
            'is_duplicate':   False,
            'parsed':         parse_release_info_for_path(rep['path']),
            'library':        None,
            'tracker_health': 'unregistered',
            'tracker_msg':    g['stored_msg'],
            'uploaded':       None,
            'ratio':          None,
            'seeding_time':   None,
            'added_on':       None,
            'alive_library':  g['alive_library'],
            'alive_sibling':  g['alive_sibling'],
        })

    verdict_order = {'dead_seed': 0, 'dead_registration': 1, 'unregistered': 2,
                     'superseded': 3, 'import_pending': 4, 'not_in_library': 5}
    items.sort(key=lambda i: (verdict_order.get(i['verdict'], 9), -i['total_size']))
    counts = {}
    for i in items:
        counts[i['verdict']] = counts.get(i['verdict'], 0) + 1

    suggestions = _triage_exclusion_suggestions(items)

    return jsonify({
        "status":         "success",
        "items":          items,
        "counts":         counts,
        "truncated":      truncated,
        "arr_configured": bool(conn_by_id),
        "suggestions":    suggestions,
    })


@app.route('/api/workflows/triage/verify', methods=['POST'])
@require_auth
def workflows_triage_verify():
    """Live tracker verification for a batch of triage items (phase 2).

    The Triage page renders instantly from audit-time data, then posts the
    visible hashes here in sequential batches; each response carries live
    tracker health + upload stats which the client folds into the rows via
    their `verdict_alternatives`. Batches are hard-capped so a request
    finishes in seconds — the per-torrent tracker fan-out in sources/ runs
    8-wide (the ceiling that keeps qui and qBittorrent responsive), and
    sequential batches mean live verification never exceeds the concurrency
    the audit itself uses.
    """
    data = request.get_json(silent=True) or {}
    raw  = data.get('items') or []
    if not isinstance(raw, list) or len(raw) > _TRIAGE_VERIFY_BATCH_MAX:
        return jsonify({"status": "error",
                        "message": f"items must be a list of at most "
                                   f"{_TRIAGE_VERIFY_BATCH_MAX} entries"}), 400
    items = [{'hash': i['hash'], 'instance_id': i.get('instance_id')}
             for i in raw if isinstance(i, dict) and i.get('hash')]
    if not items:
        return jsonify({"status": "success", "details": {}})
    cfg = db_load_config()
    try:
        details = sources.fetch_torrent_details(cfg, items)
    except Exception as e:
        # qBittorrent raises SourceConnectionError when unreachable — surface
        # it so the UI shows an honest "verification failed" state instead of
        # silently treating every torrent as health-unknown.
        log.warning("Triage verify: torrent detail fetch failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 502
    return jsonify({"status": "success", "details": details})


# Junk auditorr can recognize in Triage and gently offer to exclude. Detection
# keys off each torrent's representative (largest) file. Patterns use the
# case-insensitive exclusion syntaxes (ext:/contains:/bareword segment) so they
# match regardless of how the release cased its names; the `match` substrings
# let the UI drop the affected rows immediately (the real exclusion only takes
# effect on the next audit walk). Subtitles and Extras/Featurettes are
# deliberately NOT suggested — those are often kept on purpose.
_DISC_SEGMENTS = {'bdmv': 'bluray', 'certificate': 'bluray', 'video_ts': 'dvd', 'audio_ts': 'dvd'}
_SAMPLE_RE     = re.compile(r'(?:^|[ .\-_/])sample(?:[ .\-_/]|$)')
_DISC_PATTERNS = {'bluray': ['BDMV', 'CERTIFICATE'], 'dvd': ['VIDEO_TS', 'AUDIO_TS']}
_DISC_MATCH    = {'bluray': ['bdmv', 'certificate'], 'dvd': ['video_ts', 'audio_ts']}


def _classify_triage_junk(rep_path):
    """('disc', 'bluray'|'dvd') | ('sample', None) | ('ext', '.sfv') | (None, None)."""
    norm = str(rep_path or '').replace('\\', '/').lower()
    for seg in norm.split('/'):
        if seg in _DISC_SEGMENTS:
            return ('disc', _DISC_SEGMENTS[seg])
    if _SAMPLE_RE.search(norm):
        return ('sample', None)
    ext = os.path.splitext(norm)[1]
    if ext and ext not in _VIDEO_EXTS:
        return ('ext', ext)
    return (None, None)


def _triage_exclusion_suggestions(items):
    buckets = {}
    for i in items:
        kind, sub = _classify_triage_junk(i['rep_path'])
        if kind == 'ext':
            key = f'ext:{sub}'
            b = buckets.setdefault(key, {
                'id': key, 'label': f'{sub} files',
                'detail': 'sidecar files Sonarr/Radarr never import',
                'patterns': [f'ext:{sub.lstrip(".")}'], 'match': [sub], 'count': 0, 'size': 0})
        elif kind == 'sample':
            b = buckets.setdefault('sample', {
                'id': 'sample', 'label': 'sample clips',
                'detail': 'scene sample videos, not the real release',
                'patterns': ['contains:sample'], 'match': ['sample'], 'count': 0, 'size': 0})
        elif kind == 'disc':
            key = f'disc:{sub}'
            b = buckets.setdefault(key, {
                'id': key, 'label': f'{"Blu-ray" if sub == "bluray" else "DVD"} disc folders',
                'detail': 'full-disc rip structure (matches the disc-rip preset)',
                'patterns': _DISC_PATTERNS[sub], 'match': _DISC_MATCH[sub], 'count': 0, 'size': 0})
        else:
            continue
        b['count'] += 1
        b['size']  += i['total_size']
    return sorted(buckets.values(), key=lambda s: -s['count'])


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
    dup_result = _build_dup_groups(
        dup_group_inputs(torrent_files, media_files, local_path, media_path),
        local_path, media_path)

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
