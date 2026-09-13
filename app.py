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

import rounds
import sources
from db import (
    DATA_DIR,
    DEFAULT_CONFIG, SCORE_WEIGHT_KEYS, API_URL_KEYS, url_problem,
    EXCLUSION_PATTERNS_MAX, EXCLUSION_PATTERN_MAX_CHARS,
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
    db_get_latest_upload_snapshot,
    db_save_audit,
    db_get_meta, db_set_meta, db_update_meta, db_delete_meta,
)
from state import (
    get_state, set_state, try_start_scanning,
    note_workflow_request_start, note_workflow_request_end, workflow_active,
)
from audit import run_audit_process, process_health_metrics, compute_upload_stats, _is_not_imported_torrent, _compute_cross_seed_stats
from arr import _test_arr_connection, arr_rescan, arr_search, fetch_arr_media_index, arr_media_index_errors, arr_root_folders, VIDEO_EXTENSIONS, queue_records_for_item, arr_titles_errors, arr_year_ok, rank_arr_candidates, test_arr_connections, fetch_arr_indexers, fetch_release_matrix, season_episodes_from_name, sonarr_episodes_by_file, grab_release, normalize_arr_connections, link_base, poll_queue_until_clear, force_manual_import_by_id, force_import_files, get_arr_file_id, parse_release_info_for_path, fetch_arr_all_titles, title_match_keys, title_alias_keys, with_title_aliases, compare_release_quality, parse_quality_name, parse_trump_pm, match_trump_release, match_trumped_torrent, rank_release_matches, score_release_match, title_soft_match, tracker_matches_indexer
from scripts import generate_script, _build_dup_groups, dup_group_inputs
from media_server_exclusions import normalize_disc_rip_presets, normalize_media_server_presets
from watchdog_handler import restart_watchdog, start_watchdog, _scheduled_audit_loop, nudge_watchdog
from debug import (
    install_ring_buffer, build_debug_report, memory_pressure, cgroup_oom_events,
    start_memory_monitor, record_heavy_request, malloc_trim, process_rss_mb,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

APP_VERSION = "1.7.3"

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
        # A refused scan (the source plausibility guard). Listed here for the
        # startup retry above all: a client still loading its session answers
        # with few or no torrents, which is precisely what the guard refuses,
        # and 60 seconds later it answers properly.
        'Source anomaly',
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
        # Backfill runs retained (bounded by _GEN_JOBS_KEPT + one running) and
        # the result rows they hold between them.
        'generate_jobs':        len(_gen_jobs),
        'generate_job_results': sum(len(j.get('results') or []) for j in list(_gen_jobs.values())),
        # True while background scans are deferring to an active workflow
        # session — evidence for "why didn't the watchdog scan run yet"
        'workflow_active':      workflow_active(),
    }
    return jsonify(report)


@app.route('/api/results')
@require_auth
def get_results():
    return jsonify(db_load_results())


@app.route('/api/next_steps')
@require_auth
def get_next_steps():
    """Next steps page — prioritized workflows plus the (useless) prize layer.

    Summary data only: `latest_results` minus file lists, `audit_runs`, and the
    latest upload snapshot. Deliberately never touches `file_results` — this
    endpoint is polled and deserializing full file lists is the known RAM
    hotspot. Do not add it to `_is_heavy_mem_path`; it must never qualify.
    """
    cfg = db_load_config()
    lifetime_up = 0
    try:
        snap = db_get_latest_upload_snapshot() or {}
        lifetime_up = sum(
            (v or {}).get('uploaded', 0)
            for k, v in snap.items()
            if not k.startswith('_') and isinstance(v, dict)
        )
    except Exception as e:
        log.warning(f"Next steps: could not read upload snapshot: {e}")
    return jsonify(rounds.build_state(
        cfg, db_load_results(), db_get_recent_runs(),
        lifetime_uploaded=lifetime_up, progress=db_get_meta('ns_progress')))


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
        # A warning, not an error: an install that has held a schemeless URL for
        # months still saves, and still gets told. Blocking here would lock it out
        # of every unrelated setting on the page.
        for key, label in API_URL_KEYS:
            if key in data:
                problem = url_problem(label, data.get(key))
                if problem:
                    warnings.append(problem)
        try:
            existing = db_load_config()
            new_conf = {
                'TORRENT_SOURCE':     str(data.get('TORRENT_SOURCE', existing.get('TORRENT_SOURCE', 'qbit'))),
                'QB_HOST':            str(data.get('QB_HOST', '')),
                'QB_USER':            str(data.get('QB_USER', '')),
                'QB_PASS':            str(data['QB_PASS']) if data.get('QB_PASS') else existing.get('QB_PASS',''),
                'QUI_HOST':           str(data.get('QUI_HOST', '')),
                'QUI_API_KEY':        str(data['QUI_API_KEY']) if data.get('QUI_API_KEY') else existing.get('QUI_API_KEY', ''),
                # This dict is a full replacement, and most string keys above
                # default to '' — safe only because Config.jsx always sends them.
                # These fall back to the stored value instead, so a save from a
                # client that doesn't know the keys (a browser holding a stale
                # bundle, most likely) leaves them alone rather than silently
                # clearing someone's proxy URLs.
                'QB_EXTERNAL_URL':    str(data.get('QB_EXTERNAL_URL',    existing.get('QB_EXTERNAL_URL', ''))),
                'QUI_EXTERNAL_URL':   str(data.get('QUI_EXTERNAL_URL',   existing.get('QUI_EXTERNAL_URL', ''))),
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
                'SONARR_EXTERNAL_URL': str(data.get('SONARR_EXTERNAL_URL', existing.get('SONARR_EXTERNAL_URL', ''))),
                'RADARR_EXTERNAL_URL': str(data.get('RADARR_EXTERNAL_URL', existing.get('RADARR_EXTERNAL_URL', ''))),
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
    return _rescan_response('sonarr')


@app.route('/api/actions/radarr_rescan', methods=['POST'])
@require_auth
def actions_radarr_rescan():
    return _rescan_response('radarr')


def _rescan_response(service):
    """Run a rescan and report what the arr actually decided, not just that we asked.

    arr_rescan probes each target through /api/v3/manualimport, so a scan the arr
    will refuse (a downgrade, or a non-repack over a repack) comes back with its
    reason attached instead of a bare success the UI would render as a green toast.
    """
    data = request.json or {}
    cfg  = db_load_config()
    try:
        outcome = arr_rescan(cfg, service, data.get('paths', []))
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        log.exception("Error in %s_rescan", service)
        return jsonify({"status": "error", "message": str(e)}), 400
    rejected = [r for r in outcome['results'] if r['rejections']]
    return jsonify({
        "status":   "success",
        "count":    outcome['count'],
        "results":  outcome['results'],
        "rejected": len(rejected),
    })


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

# Cap on one import-check request. This is one arr call per item and the client
# polls it every few seconds, so it is deliberately smaller than the tracker
# verify batch — a rescan selection is a handful of rows, not a whole library.
_IMPORT_CHECK_MAX = 60


def _literal_pattern(path, subtree=False):
    """One exclusion rule built from a real path — never a glob (C8).

    `literal:` exists because release names contain `[`, `]`, `*` and `?`, and
    every other path rule runs through fnmatch. See `exclusions._LITERAL_DOC`.
    """
    p = str(path).replace('\\', '/').strip('/')
    return f"literal:{p}/" if subtree else f"literal:{p}"


def _excl_folder(records):
    """The audit's agreed `excl_folder` for a torrent's records, or ''.

    Every record of a hash carries the same stamp, so disagreement means the
    group spans hashes or a record predates the field — either way the honest
    answer is "not established", which falls back to per-file rules.
    """
    folders = {str(f.get('excl_folder') or '') for f in records}
    return folders.pop() if len(folders) == 1 else ''


def _triage_exclusion_patterns(paths, folder):
    """Exclusion rules for one Triage row (T6).

    `folder` is the audit's `excl_folder` stamp — the directory it established is
    safe to exclude wholesale, or empty. When there is one, the row is a single
    literal subtree rule; otherwise every file gets its own exact rule.

    **The endpoint deliberately makes no judgement of its own here.** It used to
    re-derive the common folder from `paths` and accept it at two segments or
    deeper, which was a proxy for "is this a category dir" and measurably wrong:
    a torrent saved with no category directory has its release folder one
    segment down, and the rule refused it in favour of nine per-file rules long
    enough for the config cap to refuse those in turn. Only the audit can see
    the whole torrent, every other torrent's paths, and the media tree, which is
    what the real test needs — see `audit._mark_whole_torrents`.

    A single-file torrent gets an exact rule regardless: the audit will not stamp
    a folder it does not own, and a lone file's parent is usually shared.
    """
    norm = [str(p).replace('\\', '/') for p in paths if p]
    if folder and len(norm) > 1:
        return [_literal_pattern(folder, subtree=True)]
    return [_literal_pattern(p) for p in norm]


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
    """Append patterns to the Excluded Files & Folders config list.

    This writes `EXCLUSION_PATTERNS` through `db_save_config`, which is a bare
    INSERT OR REPLACE and validates nothing — `validate_config` runs only on the
    config POST. So the caps it enforces on this exact key have to be enforced
    *here* too, or a click-through session writes a list the Config page then
    refuses to save, and the error ("EXCLUSION_PATTERNS[47] must not exceed 200
    characters") blocks every unrelated setting on that page until the user
    finds and hand-trims the list. Both limits are reachable in one sitting: a
    200-character relative path on a nested season pack with a long scene name,
    and 100 patterns in an afternoon of clicking Exclude on a messy library.

    Refusal is reported rather than swallowed — "3 of 5 added; 2 were too long"
    beats a green toast over a list that is now unsaveable.
    """
    data = request.json or {}
    patterns = [str(p).strip() for p in (data.get('patterns') or []) if str(p).strip()]
    if not patterns:
        return jsonify({"status": "error", "message": "No patterns provided"}), 400
    cfg = db_load_config()
    existing = [p for p in cfg.get('EXCLUSION_PATTERNS', []) if isinstance(p, str)]
    seen = {p.strip().lower() for p in existing}
    added = duplicates = too_long = no_room = 0
    for p in patterns:
        if p.lower() in seen:
            duplicates += 1
            continue
        if len(p) > EXCLUSION_PATTERN_MAX_CHARS:
            too_long += 1
            continue
        if len(existing) >= EXCLUSION_PATTERNS_MAX:
            no_room += 1
            continue
        existing.append(p)
        seen.add(p.lower())
        added += 1
    cfg['EXCLUSION_PATTERNS'] = existing
    if added:
        db_save_config(cfg)
        # No file changed, so the watcher will never notice this on its own — but
        # every count auditorr reports just moved. Debounced, so clicking through a
        # page of suggestion chips still costs one scan.
        nudge_watchdog('exclusion patterns added')

    # Each refusal names its own remedy: trimming the list fixes the count cap
    # and does nothing for an over-long path, so one shared "trim the list"
    # sent half of these users somewhere useless.
    refusals, remedies = [], []
    if too_long:
        refusals.append(f"{too_long} too long (over {EXCLUSION_PATTERN_MAX_CHARS} characters)")
        remedies.append("write a shorter rule for those by hand")
    if no_room:
        refusals.append(f"{no_room} would pass the {EXCLUSION_PATTERNS_MAX}-pattern limit")
        remedies.append("remove rules you no longer need")
    if refusals:
        message = (f"Added {added} of {len(patterns)} — "
                   + ", ".join(refusals)
                   + f". In Config → Excluded Files & Folders, {' and '.join(remedies)}.")
        log.warning("Exclude refused %d pattern(s): %s", too_long + no_room, "; ".join(refusals))
    else:
        message = f"Added {added} exclusion rule{'' if added == 1 else 's'}"
    return jsonify({
        "status":     "success",
        "added":      added,
        "duplicates": duplicates,
        "refused":    too_long + no_room,
        "too_long":   too_long,
        "no_room":    no_room,
        "total":      len(existing),
        "message":    message,
    })


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

    # `fetch_torrent_file_paths` returns None for a torrent it could not ask
    # about (vs [] for one the client says holds no files). Treated as "no known
    # paths" here, which is what it has always been — reporting the difference
    # to the user is the Cleanup/Trumped re-verify work, not this function's.
    owners = {}  # path -> set(hashes referencing it)
    for h, paths in paths_map.items():
        for p in (paths or []):
            owners.setdefault(p, set()).add(h)

    delete_items, keep_items = [], []
    for it in items:
        h = it.get('hash')
        if not h:
            continue
        my_paths = paths_map.get(h) or []
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
    # A keep-files removal touches the client and nothing else, so there is no
    # filesystem event for the watcher to see — yet the torrent is gone and
    # every count that mentions it is now wrong. Even a delete-files removal is
    # worth nudging: it makes the audit start from the last *action* rather than
    # from whichever inotify event happened to arrive last.
    if removed:
        nudge_watchdog('torrents removed via the client')
    return jsonify({"status": "success", "removed": removed, "requested": len(items),
                    "files_deleted": files_deleted, "files_kept": files_kept})


@app.route('/api/workflows/force_import', methods=['POST'])
@require_auth
def workflows_force_import():
    """Import a torrent's files over the file Sonarr/Radarr already holds.

    The rescan action cannot reach these: the arr evaluates its upgrade and
    revision specs against the existing file and refuses anything that is not
    strictly better, which a same-quality trump replacement never is. This is
    the API form of the arr's own "Import Anyway".

    Each item is {service, connection_id, arr_id, paths}. Items are independent —
    one failure is reported against that item, not the request.
    """
    cfg   = db_load_config()
    data  = request.json or {}
    items = data.get('items') or []
    if not items:
        return jsonify({"status": "error", "message": "No items provided"}), 400

    results = []
    for item in items:
        service = str(item.get('service') or '')
        key     = str(item.get('key') or '')
        if service not in ('sonarr', 'radarr'):
            results.append({"key": key, "imported": False, "message": "Unknown service"})
            continue
        if not item.get('arr_id') or not item.get('connection_id'):
            results.append({"key": key, "imported": False,
                            "message": "No library item to replace — nothing to import over"})
            continue
        try:
            outcome = force_import_files(cfg, service, item['connection_id'], item['arr_id'],
                                         [str(p) for p in (item.get('paths') or [])])
            results.append({"key": key, **outcome})
        except ValueError as e:
            results.append({"key": key, "imported": False, "message": str(e)})
        except Exception as e:
            log.exception("Error force-importing %s %s", service, item.get('arr_id'))
            results.append({"key": key, "imported": False, "message": str(e)})

    imported = sum(1 for r in results if r.get('imported'))
    log.info("Force import: %d/%d item(s) imported", imported, len(results))
    if imported:
        nudge_watchdog('files force-imported')
    return jsonify({"status": "success", "imported": imported,
                    "requested": len(results), "results": results})


@app.route('/api/workflows/import_check', methods=['POST'])
@require_auth
def workflows_import_check():
    """Report each item's current Sonarr/Radarr file id.

    Exists so a Triage rescan can finish visibly. Rescanning hands the file to
    the arr, which imports on its own schedule and reports nothing back, and the
    Triage row is built from the last audit — so the row used to sit there
    looking untouched until the watchdog eventually scanned, minutes later. The
    client snapshots these ids before the rescan and polls afterwards: an id
    that changed means the arr took the file, and the row can go.

    Deliberately the same primitive `force_import_files` confirms success with,
    for the same reason — the arr's command status is not trustworthy, but its
    own file id is. Stateless: the caller holds the baseline, so nothing here
    has to be remembered between requests.
    """
    cfg   = db_load_config()
    items = (request.json or {}).get('items') or []
    if not items:
        return jsonify({"status": "error", "message": "No items provided"}), 400

    results = []
    for item in items[:_IMPORT_CHECK_MAX]:
        key     = str(item.get('key') or '')
        service = str(item.get('service') or '')
        if service not in ('sonarr', 'radarr') or not item.get('connection_id') \
                or item.get('arr_id') is None:
            # Not something the arr can be asked about — the caller keeps
            # showing it until an audit clears it.
            results.append({"key": key, "file_id": None, "checked": False})
            continue
        try:
            fid = get_arr_file_id(cfg, service, item['connection_id'], item['arr_id'])
            results.append({"key": key, "file_id": fid, "checked": True})
        except Exception as e:
            # `checked: false` is not `file_id: null` — one means "could not
            # ask", the other means "asked, and it holds no file". Collapsing
            # them would read an unreachable arr as a successful import.
            log.warning("Import check failed for %s %s: %s", service, item.get('arr_id'), e)
            results.append({"key": key, "file_id": None, "checked": False})

    return jsonify({"status": "success", "results": results})


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

# A daily series names its episodes by air date rather than SxxExx, so the
# season token that normally identifies TV is absent and the release looks like
# a film. The service below is a *gate*, so guessing radarr for "Show.2024.01.05"
# would send the lookup to the wrong service and 404 rather than merely
# mis-rank — this recovers that one case at the cost of four lines.
_DAILY_DATE_RE = re.compile(r'\b(19|20)\d{2}[.\-_ ]\d{2}[.\-_ ]\d{2}\b')


def _trump_service_for(parsed, name=''):
    """Which arr service a parsed trump release belongs to."""
    if parsed.get('season') is not None:
        return 'sonarr'
    return 'sonarr' if _DAILY_DATE_RE.search(str(name or '')) else 'radarr'


def _trump_find_arr_item(cfg, parsed, titles=None, name=''):
    """Match a parsed release against managed arr titles (title + year aware).

    Exact first (diacritic/apostrophe-folded title keys, same as Triage), then
    the arrs' own **alternate titles** — non-English content is released under
    its original-language name while the arr stores the English one, and that
    field is authoritative precisely because it is how the arr matched the grab
    in the first place (TRIAGE T15, same primitives) — then a **soft fallback**
    ranked by shared title core, because a trump's new-release name often
    carries a stray season token or extra scene tokens the exact key match can't
    fold ('… Rides Again S01' vs the series 'The Magic School Bus Rides Again')
    and a graded title match beats a hard 404.

    **Content type is a gate, not a sort key**, at every tier — TRIAGE T1's fix
    at its second call site. It used to be `sort(key=preferred first)` followed
    by `[0]`, which silently falls back to the wrong type whenever the right one
    has no rows: a same-titled film answering for a series, searched and grabbed
    on the wrong instance. Within the surviving rows, `rank_arr_candidates`
    replaces `[0]` so ties stop being broken by `normalize_arr_connections`
    emission order.
    """
    if titles is None:
        titles = fetch_arr_all_titles(cfg)
    service = _trump_service_for(parsed, name)

    keys  = title_match_keys(parsed['title'])
    exact = rank_arr_candidates(
        [t for t in titles if title_match_keys(t.get('title') or '') & keys],
        parsed, service=service)
    if exact:
        return exact[0]

    # Alias pass — kept strictly behind the canonical one, so an alias can only
    # ever rescue a release that would otherwise have matched nothing.
    alias_keys = set(with_title_aliases(keys, title_alias_keys(titles))) - keys
    if alias_keys:
        aliased = rank_arr_candidates(
            [t for t in titles if title_match_keys(t.get('title') or '') & alias_keys],
            parsed, service=service)
        if aliased:
            return aliased[0]

    # Soft fallback — best title-core overlap above a real floor.
    best, best_sim = None, 0.0
    for t in titles:
        if t.get('service') != service or not arr_year_ok(parsed, t):
            continue
        sim = title_soft_match(parsed['title'], t.get('title') or '')
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
    files). The seed itself is always included. `paths_map` is
    {hash: [paths] | None}, where None is "could not ask" — treated here as no
    known paths, which only ever narrows a group. The caller refuses outright
    when a *seed's* paths are unknown; flagging a narrowed group when a
    *candidate's* are is TR1's remaining half.
    Each returned row gains a sorted 'paths' list.
    """
    seed_paths = set(paths_map.get(seed['hash']) or [])
    group = []
    for r in rows:
        if r['size'] != seed['size']:
            continue
        ps = set(paths_map.get(r['hash']) or [])
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


def _trump_prefer_pm_tracker(ranked, auto_hash, indexer):
    """Break candidate ties with the tracker that sent the PM.

    A cross-seed group carries one release name on several trackers, so its rows
    score identically and the pre-selected seed was whichever the client listed
    first. The trumped registration is the one on the tracker that sent the PM,
    so that's the better seed to offer.

    Strictly a tie-break: the sort is score-first and stable, so a stronger title
    match is never demoted, and nothing is dropped for being on another tracker
    (the PM's tracker may not be in the client under a recognizable name at all).
    Reorders `ranked` in place and returns the possibly-upgraded auto hash.
    """
    if not indexer or not ranked:
        return auto_hash
    on_tracker = {c['hash'] for c in ranked
                  if tracker_matches_indexer(c.get('tracker'), indexer)}
    if not on_tracker:
        return auto_hash
    pinned = next((c for c in ranked if c['hash'] == auto_hash), None)
    ranked.sort(key=lambda c: ((c.get('match_score') or 0), c['hash'] in on_tracker),
                reverse=True)
    if pinned is None:
        return ranked[0]['hash']
    # Only swap for a sibling that matched the PM equally well.
    sib = next((c for c in ranked if c['hash'] in on_tracker
                and c.get('match_score') == pinned.get('match_score')), None)
    return sib['hash'] if sib else auto_hash


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
    indexer     = str(data.get('indexer') or '').strip()

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
            auto_hash = _trump_prefer_pm_tracker(ranked, auto_hash, indexer)
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

    # An empty file list means "could not ask", not "holds no files" — both
    # backends catch every failure and return []. `_cross_seed_group` tests each
    # sibling against the seed's own paths, so an empty seed list short-circuits
    # every test and collapses the group to the seed alone. `execute` then
    # deletes that one torrent's files while its cross-seed siblings stay
    # registered and keep seeding on top of the hole — silently, in the default
    # configuration, reported to the user as a successful one-torrent group.
    # There is no honest degraded answer for a *seed*, only a smaller one.
    # (A *candidate* whose list is unknown is a different case and still only
    # narrows the group; reporting that is Phase 2's job.)
    unknown = [s for s in seeds if not paths_map.get(s['hash'])]
    if unknown:
        log.warning("Trump: refusing to resolve — no file listing for %d of %d seed(s): %s",
                    len(unknown), len(seeds), ', '.join(s['hash'][:8] for s in unknown))
        return jsonify({
            "status": "error",
            "message": f"Could not read the file list for {len(unknown)} of the {len(seeds)} "
                       "selected torrent(s), so their cross-seed groups cannot be resolved. "
                       "Check that the torrent client is reachable and try again.",
        }), 502

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
    # Fetch, then read the errors accessor, in that order and in this request:
    # the title cache is 120s, so the accessor describes the list just handed
    # back and nothing else.
    titles     = fetch_arr_all_titles(cfg)
    arr_errors = arr_titles_errors()
    item       = _trump_find_arr_item(cfg, parsed, titles=titles, name=new_title)
    if item is None:
        # "No arr knows this title" and "an arr could not be asked" are the same
        # empty list, and they are not the same answer — telling a user to add a
        # title their arr is already managing is how a trump ends in the wrong
        # grab (TR7). Say which one happened.
        if arr_errors:
            names = ', '.join(e.get('name') or e.get('connection_id') or '?' for e in arr_errors)
            return jsonify({
                "status": "error",
                "arr_errors": arr_errors,
                "message": f"Could not check whether “{parsed['title'] or new_title}” is managed — "
                           f"{names} did not answer. Fix the connection and retry rather than "
                           f"adding the title again.",
            }), 502
        return jsonify({
            "status": "error",
            "arr_errors": [],
            "message": f"No Sonarr/Radarr entry matches “{parsed['title'] or new_title}” — add the title to an arr first, then retry.",
        }), 404

    conn = next((c for c in normalize_arr_connections(cfg)
                 if c['id'] == item.get('connection_id')), None)
    prefix = '/movie/' if item.get('service') == 'radarr' else '/series/'
    fallback_url = ''
    if conn:
        fallback_url = (link_base(conn) + prefix
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
    # exactly, and an "other matches" affordance when it does.
    #
    # The PM's indexer is a *priority*, not a filter. Its copy is the one that
    # was trumped (and the one carrying the PM's freeleech), so it wins every
    # tie — but the release is often listed on several trackers, and hiding
    # those turned "not up on this one yet" into a dead end. Ranked wide, then
    # reordered, then cut, so a lower-scoring copy on the PM's tracker can't be
    # truncated away before the tie-break runs.
    release = (match_trump_release(releases, new_title, indexer)
               or match_trump_release(releases, new_title))
    candidates = rank_release_matches(releases, new_title, name_key='title', limit=40)
    if indexer:
        candidates.sort(key=lambda r: ((r.get('match_score') or 0),
                                       tracker_matches_indexer(r.get('indexer'), indexer)),
                        reverse=True)
    candidates = candidates[:8]
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
        # A match found *despite* an unreachable instance is still worth
        # flagging: the instance that did not answer may hold a better one.
        "arr_errors":      arr_errors,
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

    # Credit the swap on the Next steps prize layer. Trumped is the one workflow
    # counted at execute time: the swap trades one release for another, so the
    # re-audit below sees a library in much the same shape and has nothing to
    # infer the action from. Recorded before the rescan so that scan's own
    # progress pass reads the updated counter.
    if removed:
        try:
            db_update_meta('ns_progress',
                           lambda p: rounds.record_trump(p, torrents=removed))
        except Exception as e:
            log.warning("Could not record trump on Next steps progress: %s", e)

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

    Torrents the client says are still downloading are **not** candidates: an
    unfinished payload is not-imported by definition and was being reported as
    junk (T4). The filter lives in `_is_not_imported_torrent`, so this endpoint,
    the compact `triage` row's predicate and the sidebar badge's count all move
    together.

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

    # Both fetches are followed *immediately* by their errors accessor, in the
    # same request: each accessor is cached alongside its data (120s TTL) and
    # its contract is that it describes the list the caller just received.
    #
    # An arr that did not answer contributes no rows, which is the same empty
    # list as an arr that manages nothing — and Triage turns that silence into a
    # verdict with a delete button under it. `arr_configured` is derived from
    # the config rather than from the fetch, so it stays true and the existing
    # banner never fires.
    arr_errors = []
    try:
        media_index = fetch_arr_media_index(cfg)
        arr_errors.extend(arr_media_index_errors())
    except Exception as e:
        log.warning("Triage: media index fetch failed: %s", e)
        media_index = []
        # The per-connection loop inside has its own try, so reaching here means
        # the whole call failed and the cache was never written — the accessor
        # would describe a previous fetch. Synthesize instead.
        arr_errors.append({'connection_id': '', 'name': 'Sonarr/Radarr library',
                           'service': '', 'partial': False, 'message': str(e)})
    try:
        all_titles = fetch_arr_all_titles(cfg)
        arr_errors.extend(arr_titles_errors())
    except Exception as e:
        log.warning("Triage: title list fetch failed: %s", e)
        all_titles = []
        arr_errors.append({'connection_id': '', 'name': 'Sonarr/Radarr titles',
                           'service': '', 'partial': False, 'message': str(e)})
    arr_degraded = bool(arr_errors)

    # Index arr titles under every match variant (apostrophes spaced/dropped)
    lib_by_title = {}
    for m in media_index:
        for key in title_match_keys(m.get('title') or ''):
            lib_by_title.setdefault(key, []).append(m)
    titles_by_norm = {}
    for t in all_titles:
        for key in title_match_keys(t.get('title') or ''):
            titles_by_norm.setdefault(key, []).append(t)

    # Non-English content is released under its original-language name while the
    # arr stores the English one, so matching on the release title alone answers
    # "no arr has ever heard of this" for a series the arr is actively managing.
    # The arrs' own `alternateTitles` carry the mapping — and they must, because
    # it is how the arr matched the grab in the first place. Built once per
    # request from `all_titles` (one row per series/movie, already cached), so
    # the per-file media index pays no memory for it.
    title_aliases = title_alias_keys(all_titles)

    conn_by_id = {c['id']: c for c in normalize_arr_connections(cfg)}

    def _arr_url(entry):
        conn = conn_by_id.get(entry.get('connection_id'))
        if not conn:
            return ''
        prefix = '/movie/' if entry.get('service') == 'radarr' else '/series/'
        slug = entry.get('title_slug') or ''
        return link_base(conn) + prefix + (slug or str(entry.get('arr_id') or ''))

    items = []
    for g in group_list:
        videos = [f for f in g['files']
                  if os.path.splitext(f['path'])[1].lower() in _VIDEO_EXTS]
        rep = max(videos or g['files'], key=lambda f: f['size'])
        parsed = parse_release_info_for_path(rep['path'])

        tracker_health = g.get('stored_health') or 'unknown'
        # Canonical keys first, then the arrs' alternate titles — an exact match
        # always wins, an alias only rescues what would otherwise match nothing.
        parsed_keys    = with_title_aliases(title_match_keys(parsed['title']), title_aliases)
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

        # Library rows with files, gated to the service that fits the content
        # type. A *gate*, not a tiebreak: this used to end `... or lib_rows`,
        # which fell back to the wrong-type rows whenever the right type had
        # none, so a TV episode of a same-titled series absent from Sonarr
        # matched the film in Radarr (Fargo, Hannibal, Dune, Shōgun — the
        # collisions are not exotic). That produced quality_cmp 'same', which is
        # exactly what renders the Force import button, and force_import_files
        # then posts replaceExistingFiles against the movie's id: one episode
        # deliberately written over a library film, past every rejection spec.
        # With no row of the right type the item correctly falls through to
        # import_pending / not_in_library.
        preferred = 'sonarr' if is_episode else 'radarr'
        lib_rows = next((lib_by_title[k] for k in parsed_keys if k in lib_by_title), [])
        lib_rows = [r for r in lib_rows if _year_ok(r) and r.get('service') == preferred]

        library_match = None
        if lib_rows:
            if is_episode:
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
        elif arr_degraded:
            # `not_in_library` means "no arr has ever heard of this", and its
            # copy ends "junk can be deleted" — a not-imported torrent has no
            # hardlink anywhere by definition, so those files are the only copy.
            # That verdict must not be reachable when no arr answered: absence
            # of evidence is not evidence of absence (the same rule Phase 2's
            # source guard applies to the torrent client). `superseded` and
            # `import_pending` are positive matches and stand on their own; only
            # the verdict derived purely from silence is suppressed.
            fallback = 'library_unknown'
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
                # Addressing for force-import: the arr already holds a file for
                # this title, so replacing it needs the item's own id, not a path.
                'arr_id':        library_match.get('arr_id'),
                'connection_id': library_match.get('connection_id'),
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
                'arr_id':        t.get('arr_id'),
                'connection_id': t.get('connection_id'),
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
            # Built here, not in the browser — and the safe folder comes off the
            # audit's own stamp, because deciding it needs the whole torrent,
            # every other torrent's paths and the media tree.
            'exclusion_patterns': _triage_exclusion_patterns(
                [f['path'] for f in g['files']], _excl_folder(g['files'])),
            'is_duplicate':   is_duplicate,
            'parsed':         parsed,
            'library':        lib_payload,
            'tracker_health': tracker_health,
            'tracker_msg':    g['stored_msg'],
            # What the client is doing, and whether the payload is whole. The
            # item carried neither, so the UI could not have told an in-flight
            # download from junk even if it wanted to (T4). Known-incomplete
            # torrents no longer reach this list at all; `completion_unknown`
            # ones do, and say so rather than vanishing.
            'status':             rep.get('status') or '',
            'completion_unknown': bool(rep.get('completion_unknown')),
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
            # Deliberately empty, and the UI renders no Exclude action for these
            # rows (T6). These `paths` are the **healthy carrier's** — a file a
            # working cross-seed is seeding right now. Excluding one hides a live
            # file from the walk while the dead registration it was meant to
            # address is still sitting in the client. Exclusion is not a
            # meaningful answer to this row at all; removing the registration is.
            'exclusion_patterns': [],
            'is_duplicate':   False,
            'parsed':         parse_release_info_for_path(rep['path']),
            'library':        None,
            'tracker_health': 'unregistered',
            'tracker_msg':    g['stored_msg'],
            'status':             rep.get('status') or '',
            'completion_unknown': bool(rep.get('completion_unknown')),
            'uploaded':       None,
            'ratio':          None,
            'seeding_time':   None,
            'added_on':       None,
            'alive_library':  g['alive_library'],
            'alive_sibling':  g['alive_sibling'],
        })

    verdict_order = {'dead_seed': 0, 'dead_registration': 1, 'unregistered': 2,
                     'superseded': 3, 'import_pending': 4, 'library_unknown': 5,
                     'not_in_library': 6}
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
        "arr_errors":     arr_errors,
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
        # A folder pattern needs ≥2 segments, and a group that has fewer must
        # say so *here* rather than leaving the client to re-derive the depth —
        # a rule reimplemented on both sides of the wire is a rule that will
        # disagree (Phase 3's and 4d's lesson). Single-file torrents saved
        # straight into the category dir — qBittorrent's default — land at one
        # segment, so every such orphan in `movies/` collapsed into one group
        # whose header checkbox emitted `movies/`: a subtree prefix that matches
        # the **media library** as well as the torrent tree, silently dropping
        # the whole category out of scoring (C7).
        #
        # The ≥2-segment rule is the one `arr._scan_target` ("the release
        # folder, the second segment below LOCAL_PATH") and Triage's exclusion
        # granularity already follow; Cleanup was the one place it was not
        # applied. **Measured 2026-09-11, and it is not a depth property:** the
        # category dir is shared between the two trees by construction, while
        # the release folder is not, because the arr renames on import. So there
        # is a residual — an install whose library folders carry the release
        # name (no arr rename, or a hand-built library) gets both trees from a
        # 2-segment pattern too. That is not engineered around; it is what the
        # confirm step showing the pattern before writing it is for.
        loose = len(dir_segs) < 2
        g = folders.setdefault(top, {
            'folder': top, 'files': [], 'loose': loose,
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
    """Backfill's candidates exactly as they will be searched, plus the file counts.

    Grouped and scoped by `_resolve_backfill` — the same call `generate` searches
    from — and by nothing else. This used to ship a row for every unseeded file,
    resolved or not, so the browser could discard the unresolved ones and regroup
    the rest with its own copy of the season key (B7). After B1 that copy could
    not be kept correct at all: whether a season is one pack or N episodes turns
    on how many files the arr holds in it, which the client never sees (B7b).

    Two units, and the page keeps them as two sentences: `counts` is candidates
    (groups — a season pack is one), `resolved_count` / `unresolved_count` are
    files.
    """
    cfg = db_load_config()
    backfill = _resolve_backfill(cfg)
    # Read straight after the resolve, which is what fetched the index: the
    # accessor describes the list the caller just received. An instance whose
    # index failed contributes no rows — indistinguishable from one managing
    # nothing — so it is reported rather than left to be inferred from a gap.
    arr_errors = arr_media_index_errors()
    groups = backfill['groups']
    by_scope = {'season': 0, 'episode': 0, 'movie': 0}
    for g in groups:
        by_scope[g['scope']] += 1
    return jsonify({
        "status": "success",
        "candidates": groups,
        "counts": {'candidates': len(groups), **by_scope},
        "resolved_count": backfill['resolved_count'],
        "unresolved_count": backfill['unresolved_count'],
        # B9: how many of the unresolved files are video. The rest are sidecars
        # no arr indexes; these are the ones worth acting on.
        "unresolved_video_count": backfill['unresolved_video_count'],
        "arr_errors": arr_errors,
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
        # Match on the *quality name*, never the raw `source` field. That field
        # is a serialized C# enum and the two services do not spell it the same
        # way: Sonarr says 'web' where Radarr says 'webdl', 'webRip' where
        # Radarr says 'webrip', and HDTV is 'television' on Sonarr against 'tv'
        # on Radarr — so the HDTV chip matched nothing on either service and
        # WEB-DL/WEBRip matched nothing on any Sonarr candidate. The failure was
        # silent: the candidate came back `not_found`, indistinguishable from
        # "no release exists". `parse_quality_name` maps the display string both
        # services do agree on ('WEBDL-1080p', 'HDTV-720p', 'Bluray-1080p
        # Remux') onto the chips' own vocabulary, and it is what the rest of
        # auditorr already uses — this was the last consumer of the raw enum.
        # It also subsumes the old Remux special case, since 'remux' is matched
        # ahead of 'bluray' in _SOURCE_NAME_PATTERNS.
        def _source_match(r):
            _, src = parse_quality_name(r.get('quality_name'))
            return src in source_filter
        filtered = [r for r in filtered if _source_match(r)]
    if hdr_filter:
        # 'SDR' maps to empty string (no HDR detected); other values match hdr field directly
        target_hdr = {'' if h == 'SDR' else h for h in hdr_filter}
        filtered = [r for r in filtered if r.get('hdr', '') in target_hdr]
    # Sort: custom format score desc, quality weight desc, seeders desc (matches
    # Sonarr/Radarr interactive search order). `or 0`, not a .get default: both
    # arrs declare Seeders as `int?`, so the key is present and null on usenet.
    filtered.sort(key=lambda r: (r.get('custom_format_score') or 0, r.get('quality_weight') or 0,
                                 r.get('seeders') or 0), reverse=True)
    return filtered


# Within this fraction of the local size a release still counts as "the same
# payload". A scene torrent carries an .nfo, an .srr and often a sample beside
# the video, so byte equality with the one library file is the rarer case.
_SIZE_MATCH_TOLERANCE = 0.01


def _release_closeness(release, local):
    """`(sort_key, size_delta, match)` — how close a release is to the file on disk.

    `match` is the per-field breakdown the page renders beside each release, in
    Trumped's vocabulary: `same` / `partial` / `diff`, or `''` where there is no
    evidence either way (an unparseable quality, SDR on both sides).
    """
    want = local.get('total_size') or 0
    size = release.get('size') or 0
    delta = size - want if (want and size) else None
    if delta is None:
        size_match, size_tier = '', 2
    elif delta == 0:
        size_match, size_tier = 'same', 0
    elif abs(delta) <= want * _SIZE_MATCH_TOLERANCE:
        size_match, size_tier = 'partial', 1
    else:
        size_match, size_tier = 'diff', 2

    l_res, l_src = parse_quality_name(local.get('file_quality'))
    r_res, r_src = parse_quality_name(release.get('quality_name'))
    if not (l_res or l_src) or not (r_res or r_src):
        quality_match, quality_tier = '', 2
    elif (l_res, l_src) == (r_res, r_src):
        quality_match, quality_tier = 'same', 0
    elif (l_res and l_res == r_res) or (l_src and l_src == r_src):
        quality_match, quality_tier = 'partial', 1
    else:
        quality_match, quality_tier = 'diff', 2

    l_hdr, r_hdr = local.get('file_hdr') or '', release.get('hdr') or ''
    hdr_match = '' if not (l_hdr or r_hdr) else ('same' if l_hdr == r_hdr else 'diff')
    hdr_tier = 0 if l_hdr == r_hdr else 1

    key = (size_tier, quality_tier, hdr_tier, -(release.get('seeders') or 0),
           abs(delta) if delta is not None else float('inf'))
    return key, delta, {'size': size_match, 'quality': quality_match, 'hdr': hdr_match}


def _rank_releases(rows, rank='closest', local=None):
    """Order one candidate's filtered releases, and attach the evidence (B3).

    Backfill's question is not the arr's. Interactive search ranks by custom
    format score and quality weight, which answers *"what is the best copy of
    this?"* — the upgrade question. Backfill asks *"which of these is the file I
    already have?"*, and the answer is the release whose size and quality match
    the one on disk: that is the release the file came from, and grabbing it is
    the only outcome that leaves the library where it started with a seed behind
    it. The top-scoring release instead means a bigger download, a library file
    replaced by a different encode, and — for anyone whose quality profile
    already had its chance at that upgrade — a release the arr passed over.

    `closest` (the default): exact size, then within `_SIZE_MATCH_TOLERANCE`,
    then quality, then HDR, then seeders. `upgrade` keeps the arr's order, for
    the user who does want to upgrade while backfilling — deliberately, not by
    accident. Rows are expected in upgrade order already (`_apply_release_filters`)
    and the sort is stable, so ties keep it.

    Without a `local` file there is nothing to be close to and rows come back
    as given.
    """
    if not local:
        return rows
    scored = []
    for r in rows:
        key, delta, match = _release_closeness(r, local)
        scored.append((key, dict(r, size_delta=delta, match=match)))
    if rank != 'upgrade':
        scored.sort(key=lambda kr: kr[0])
    return [r for _key, r in scored]


def _sweep_backfill_jobs():
    """Expire Backfill's in-memory jobs: release searches and finished generate runs.

    Called from `watch_import/active` — which every open auditorr page polls every
    five seconds — as well as from the endpoints that own the jobs. Release
    searches used to be swept only from inside `acquire_releases`, so an entry
    lived until the next release search, which may never come (B13).
    """
    now = time.time()
    for k in [k for k, v in list(_release_jobs.items()) if now - v['ts'] > _RELEASE_JOB_TTL]:
        _release_jobs.pop(k, None)
    with _gen_jobs_lock:
        _sweep_gen_jobs(now)


def _csv_arg(name):
    return [v for v in (request.args.get(name) or '').split(',') if v]


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

    _sweep_backfill_jobs()

    job_key = _release_job_key(service, connection_id, arr_id, episode_id, season_number, file_path)

    if job_key in _release_jobs:
        job = _release_jobs[job_key]
        if job['status'] == 'done':
            cfg = db_load_config()
            # The same filters `generate` applies, on this branch too (B13). It
            # applied the indexer strategy and dropped resolution/source/HDR, so
            # one endpoint answered the same inputs two ways depending on whether
            # the search happened to be cached.
            releases = _apply_release_filters(
                job['releases'],
                cfg.get('ACQUIRE_DOWNLOAD_FROM') or [],
                cfg.get('ACQUIRE_SEEDING_ON') or [],
                res_filter=_csv_arg('res_filter'),
                source_filter=_csv_arg('source_filter'),
                hdr_filter=_csv_arg('hdr_filter'),
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

    # B12 — a candidate whose download is already in the arr's queue is not
    # grabbed again: not by a retry after a timeout the arr had in fact
    # processed, not by a second run, not by a click on a row that reported
    # failure. `force` is the page's "Grab anyway". Advisory: a queue that cannot
    # be read does not block the grab — the cost there is a possible duplicate
    # download, not a lost file — and the answer says it was not checked.
    queue_checked = None
    arr_id = data.get('arr_id')
    if isinstance(arr_id, int) and not data.get('force'):
        season = data.get('season_number')
        queued = queue_records_for_item(
            cfg, service, connection_id, arr_id,
            episode_ids=_int_list(data.get('episode_ids')) or None,
            season_number=season if isinstance(season, int) else None)
        queue_checked = queued is not None
        if queued:
            titles = [q.get('title') for q in queued if q.get('title')]
            name = 'Sonarr' if service == 'sonarr' else 'Radarr'
            return jsonify({"status": "error", "code": "already_queued", "titles": titles[:5],
                            "message": f"Already in {name}'s queue: {titles[0] if titles else 'this item'}"}), 409
    try:
        grab_release(cfg, service, connection_id, guid, indexer_id)
        return jsonify({"status": "success", "queue_checked": queue_checked})
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')
        msg = f"HTTP {e.code}: {e.reason}"
        parsed = False
        try:
            msg = json.loads(body).get('message') or msg
            parsed = True
        except Exception:
            pass
        log.warning("Grab failed for %s/%s: %s", service, guid, msg)
        out = {"status": "error", "message": msg}
        # The one failure a fresh search fixes: the guid fell out of the arr's
        # release cache, which both arrs answer with a 404 and this message.
        # Anything else — an indexer error, an auth failure, a timeout on a grab
        # the arr may already have processed — is surfaced as it is and never
        # retried automatically, because the retry grabs a second copy (B12).
        if 'requested release in cache' in msg.lower() or (e.code == 404 and not parsed):
            out['code'] = 'stale_release'
        return jsonify(out), 400
    except Exception as e:
        log.warning("Grab failed for %s/%s: %s", service, guid, e)
        return jsonify({"status": "error", "message": str(e)}), 400


# Generate runs, keyed by id (B4). This was one process-wide slot: navigating
# away lost the only handle to a run while its searches carried on against the
# user's indexers, a second tab's Generate silently stopped the first tab's
# half-finished run, and a finished run's full result set stayed resident until
# the next run replaced it.
_gen_jobs = {}
_gen_jobs_lock = threading.Lock()
_GEN_JOB_TTL = 3600       # a finished run stays readable, and reattachable, for an hour
_GEN_JOBS_KEPT = 3        # and at most this many finished runs are kept, newest first
_GEN_ABANDON_SECS = 600   # a running job nobody has polled for this long stops itself


def _sweep_gen_jobs(now=None):
    """Drop finished runs past their TTL or beyond the newest `_GEN_JOBS_KEPT`.

    Callers hold `_gen_jobs_lock`. A running job is never swept — it ends by
    finishing, by a stop, or by nobody polling it for `_GEN_ABANDON_SECS`.
    """
    now = now or time.time()
    finished = sorted((j for j in list(_gen_jobs.values()) if j.get('finished_at')),
                      key=lambda j: j['finished_at'], reverse=True)
    for i, job in enumerate(finished):
        if i >= _GEN_JOBS_KEPT or now - job['finished_at'] > _GEN_JOB_TTL:
            _gen_jobs.pop(job['id'], None)


def _running_gen_job():
    return next((j for j in list(_gen_jobs.values()) if j['status'] == 'running'), None)


def _gen_job_running_response(job):
    """Refused, not replaced: a new run never stops somebody else's (B4)."""
    return jsonify({'status': 'error', 'code': 'job_running', 'job_id': job['id'],
                    'message': 'A search is already running — showing it instead.'}), 409


def _gen_parse_season(path):
    """The filename's season, as a *label* for an episode Sonarr gave no season.

    Never evidence of coverage: a pack is only ever searched for a season Sonarr
    itself numbered (B1). It misses daily series, anime absolute numbering and
    `S01.E02`, which is why the season is read off the arr's record first (B8).
    """
    m = re.search(r'[Ss](\d{1,2})[Ee]', os.path.basename(str(path or '')))
    return int(m.group(1)) if m else None


def _sonarr_season_coverage(arr_media):
    """How many episode files each Sonarr holds per season — B1's denominator.

    Returns `(held, unknown)`. `held[(connection_id, arr_id, season)]` counts the
    episode-file records the arr reported in that season; `unknown` is the set of
    `(connection_id, arr_id)` series with at least one record whose season the
    arr did not report.

    The unit is **files the arr holds**, not episodes the season has. A season
    still airing is fully covered the moment every file the arr holds is
    unseeded — the case a pack search was written for, and there it is strictly
    right: nothing was seeded, so nothing is orphaned by replacing it.

    A series in `unknown` has no season that can be proven covered: the record
    with no season could sit in any of them, seeded, and a pack would replace it.
    """
    held, unknown = {}, set()
    for item in arr_media:
        if item.get('service') != 'sonarr':
            continue
        series = (item.get('connection_id'), item.get('arr_id'))
        season = item.get('season_number')
        if season is None:
            unknown.add(series)
        else:
            key = series + (season,)
            held[key] = held.get(key, 0) + 1
    return held, unknown


def _backfill_search(group, episode_id=None):
    """The release search for one candidate — the only place a scope becomes a query.

    The same dict is what `acquire_releases` is re-asked with when a grab has to
    be retried, so a retry can never widen an episode row into a pack search by
    rebuilding the query from a season number the row merely displays.
    """
    params = {'service': group['arr_service'], 'connection_id': group['arr_connection_id'],
              'arr_id': group['arr_id']}
    if group['scope'] == 'season':
        params['season_number'] = group['season_number']
    elif group['scope'] == 'episode':
        eid = episode_id or group.get('episode_id')
        if eid:
            params['episode_id'] = eid
        params['path'] = group['rep_path']
    return params


def _backfill_group(scope, files, season=None, held=None, unseeded=None):
    """One searchable Backfill candidate, built from the resolved files it covers."""
    first = files[0]
    group = {k: first[k] for k in ('arr_service', 'arr_connection_id', 'arr_id',
                                   'arr_title', 'arr_url', 'file_quality', 'file_hdr')}
    conn, arr_id = first['arr_connection_id'], first['arr_id']
    if scope == 'movie':
        key = f'{conn}_{arr_id}'
    elif scope == 'season':
        key = f'{conn}_{arr_id}_S{season}'
    else:
        ident = first['file_id'] if first.get('file_id') is not None else first['path']
        key = f'{conn}_{arr_id}_S{season}_F{ident}'
    episode_numbers, episode_id = [], None
    if scope == 'episode':
        episode_numbers = first['episode_numbers'] or season_episodes_from_name(first['path'])[1]
        episode_id = first['episode_ids'][0] if first['episode_ids'] else None
    group.update({
        'key':             key,
        'scope':           scope,
        'path':            first['path'],
        'rep_path':        first['path'],
        'season_number':   season,
        'episode_numbers': episode_numbers,
        'episode_id':      episode_id,
        'file_count':      len(files),
        'total_size':      sum(c['size'] for c in files),
        # The arr's own ids for the files this candidate is about. The import
        # watch scopes a force import to their episodes (B11), and that join
        # has to happen before the grab replaces them.
        'file_ids':        [c['file_id'] for c in files if c.get('file_id') is not None],
        # The two numbers the pack-or-episode decision was made on, so a row can
        # say why an episode was not searched as part of its season.
        'season_files_held':     held,
        'season_files_unseeded': unseeded,
    })
    return group


def _resolve_backfill(cfg):
    """Backfill's candidates — resolved, scoped and grouped — plus the file counts.

    The one computation behind both `acquire_candidates` (what the page counts)
    and `generate` (what gets searched), so the number on the button and the
    searches that run cannot disagree (B7b).

    Sonarr files are bucketed by `(connection, series, season)` — ids are
    per-instance (#22) and the season is Sonarr's own (B8) — and a bucket becomes
    **one season-pack candidate only if it covers the season**: every episode
    file the arr holds in that season is unseeded. Otherwise each file is its own
    episode candidate (B1). A pack search for a partly seeded season downloads
    the whole season and then force-imports it over the files that were already
    hardlinked, orphaning every one of their torrents. Where coverage cannot be
    established — the arr reported no season on this file, or on any file of the
    series — the answer is per-episode, never a pack: a missing count read as
    "covers the season" is precisely the harm.
    """
    media_files = db_load_file_results('media')
    arr_media = fetch_arr_media_index(cfg)
    media_root = cfg.get('MEDIA_PATH', '')

    def _norm(p):
        return os.path.normpath(str(p or '')).replace('\\', '/')

    arr_index = {}
    for item in arr_media:
        key = _norm(item.get('path', ''))
        if key:
            arr_index[key] = item
    held, unknown_series = _sonarr_season_coverage(arr_media)
    conns = normalize_arr_connections(cfg)
    # Read straight after the fetch, per the accessor's contract.
    root_labels = _root_folder_labels(conns, arr_root_folders())

    svc_slug = {'sonarr': '/series/', 'radarr': '/movie/'}
    conn_by_id = {c['id']: c for c in conns}

    # Encounter order is kept so ties under the chosen sort land as they did.
    order, seasons, folder_of = [], {}, {}
    resolved = unresolved = unresolved_video = 0
    for f in media_files:
        if f.get('excluded') or [t for t in (f.get('trackers') or []) if t != 'None']:
            continue
        rel_path = f.get('path', '')
        abs_path = _norm(os.path.join(media_root, rel_path) if not os.path.isabs(rel_path) else rel_path)
        arr_item = arr_index.get(abs_path)
        if not arr_item:
            unresolved += 1
            # B9: a subtitle or a poster failing to match is normal — no arr
            # indexes them. A *video* failing to match is the number worth
            # acting on, and is usually a path-mapping mismatch.
            if os.path.splitext(rel_path)[1].lower() in VIDEO_EXTENSIONS:
                unresolved_video += 1
            continue
        resolved += 1
        service  = arr_item.get('service', '')
        conn_id  = arr_item.get('connection_id', '')
        arr_id   = arr_item.get('arr_id')
        t_slug   = arr_item.get('titleSlug') or arr_item.get('title_slug')
        conn     = conn_by_id.get(conn_id)
        base_url = link_base(conn) if conn else ''
        slug     = svc_slug.get(service, '/')
        # Matched on the arr-side path, which is what a root folder is written in.
        folder_of[rel_path] = _root_folder_of(
            root_labels, conn_id,
            str(arr_item.get('arr_path') or arr_item.get('path') or '').replace('\\', '/'))
        c = {
            'path': rel_path, 'size': f.get('size') or 0,
            'arr_service': service, 'arr_connection_id': conn_id,
            'arr_id': arr_id, 'arr_title': arr_item.get('title', ''),
            'arr_url': (base_url + slug + t_slug) if t_slug else (base_url + slug + str(arr_id) if arr_id else base_url),
            'file_id': arr_item.get('file_id'),
            'episode_ids': list(arr_item.get('episode_ids') or []),
            'episode_numbers': list(arr_item.get('episode_numbers') or []),
            'file_quality': arr_item.get('file_quality_name', ''),
            'file_hdr': arr_item.get('file_hdr', ''),
        }
        if service != 'sonarr':
            order.append(('movie', c))
            continue
        season = arr_item.get('season_number')
        if season is None:
            order.append(('loose', c))
            continue
        bucket = (conn_id, arr_id, season)
        if bucket not in seasons:
            seasons[bucket] = []
            order.append(('season', bucket))
        seasons[bucket].append(c)

    groups = []
    for kind, ref in order:
        if kind == 'movie':
            groups.append(_backfill_group('movie', [ref]))
        elif kind == 'loose':
            # No season from the arr: the filename is only a label here, never
            # evidence of coverage.
            groups.append(_backfill_group('episode', [ref], season=_gen_parse_season(ref['path'])))
        else:
            files = seasons[ref]
            n_held = held.get(ref)
            covered = (ref[:2] not in unknown_series and n_held is not None
                       and len(files) == n_held)
            if covered:
                groups.append(_backfill_group('season', files, season=ref[2],
                                              held=n_held, unseeded=len(files)))
            else:
                groups.extend(_backfill_group('episode', [c], season=ref[2],
                                              held=n_held, unseeded=len(files))
                              for c in files)
    for g in groups:
        g['search'] = _backfill_search(g)
        g['folder'] = folder_of.get(g['rep_path'], 'Other')
    return {'groups': groups, 'resolved_count': resolved, 'unresolved_count': unresolved,
            'unresolved_video_count': unresolved_video}


def _root_folder_labels(conns, roots):
    """`{connection_id: [(root, label), ...]}`, longest root first (B10).

    The folder chips used to be the first path segment below MEDIA_PATH, taken
    over resolved candidates and headed "Root Folders" — auditorr had never
    asked an arr for its root folders. They collapsed when the real roots sat
    deeper than one level and were named after a directory rather than the thing
    configured in Sonarr/Radarr. These are each arr's own `/api/v3/rootfolder`
    paths, labelled verbatim; the instance name is added only where two
    instances configure the same path string, which would otherwise be one chip
    covering two libraries. A connection whose root folders could not be read
    contributes nothing, so its files land in `Other` rather than silently
    defining a root from their first segment.
    """
    name_of = {c['id']: c.get('name') or c['id'] for c in conns}
    owners = {}
    for conn_id, paths in (roots or {}).items():
        for p in paths or []:
            owners.setdefault(p, set()).add(conn_id)
    labels = {}
    for conn_id, paths in (roots or {}).items():
        labels[conn_id] = [
            (p, p if len(owners[p]) == 1 else f'{p} ({name_of.get(conn_id, conn_id)})')
            for p in sorted(paths or [], key=lambda p: (-len(p), p))
        ]
    return labels


def _root_folder_of(labels, conn_id, arr_path):
    """The label of the longest root holding `arr_path`, on whole path segments."""
    for root, label in labels.get(conn_id) or []:
        if arr_path == root or arr_path.startswith(root + '/'):
            return label
    return 'Other'


def _release_in_scope(release, scope, episode_numbers):
    """May this release be offered for a candidate of this scope? (B1, at the release)

    An episode candidate exists *because* part of its season is already seeded,
    so a release covering more than the candidate's own file replaces library
    files other torrents are hardlinked to — B1's harm, reached through the
    results list instead of the search path. The episode search alone does not
    prevent it: interactive search returns rejected releases alongside approved
    ones (a pack in an episode search comes back flagged, not absent), and a grab
    through `/api/v3/release` bypasses the rejection.

    Only what a release *says* it covers is acted on — the pack flag, or episode
    numbers outside the candidate's. One that reports no episode numbers (daily
    and absolute-numbered series) is kept: refusing those would empty the
    workflow for exactly the libraries B8 was written for, and the import watch
    scopes by episode id as well (B11). Where the candidate's own episodes are
    unknown, a release that names episodes cannot be shown to fit, so it goes.
    """
    if scope != 'episode':
        return True
    if release.get('full_season'):
        return False
    covers = set(release.get('episode_numbers') or [])
    if not covers:
        return True
    mine = set(episode_numbers or [])
    return bool(mine) and covers <= mine


def _episode_scope(cfg, candidate, cache):
    """`(episode_id, episode_numbers)` for an episode candidate, off Sonarr's own join.

    The media index cannot answer this — `/api/v3/episodefile` carries no episode
    ids — so it is joined in from the series' episode list by file id, once per
    series per job (`cache`). Falls back to what the candidate already carries
    (the filename parse) when the join is unavailable, which is where the search
    has always come from.
    """
    episode_id = candidate.get('episode_id')
    numbers = candidate.get('episode_numbers') or []
    file_id = next(iter(candidate.get('file_ids') or []), None)
    if episode_id or file_id is None:
        return episode_id, numbers
    series = (candidate['arr_connection_id'], candidate['arr_id'])
    if series not in cache:
        cache[series] = sonarr_episodes_by_file(cfg, *series)
    eps = (cache[series] or {}).get(file_id)
    if not eps:
        return episode_id, numbers
    return eps[0][0], [e[2] for e in eps if e[2] is not None]


def _build_generate_candidates(cfg, folders=None, title_search=None):
    """Resolved, scoped candidates for the generate workflow, narrowed by the page's
    folder and title filters. Uncapped: `generate` sorts and cuts to its own count
    (the old `limit=20` default had one caller, which passed None — B13)."""
    groups = _resolve_backfill(cfg)['groups']
    if folders:
        groups = [g for g in groups if g['folder'] in folders]
    if title_search:
        term = title_search.strip().lower()
        groups = [g for g in groups if term in (g.get('arr_title') or '').lower()]
    return groups


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

    # Checked before the build, which deserializes the media list, so a refused
    # request costs nothing; and again at insert, because the build takes long
    # enough for a second tab to get there first.
    with _gen_jobs_lock:
        _sweep_gen_jobs()
        running = _running_gen_job()
    if running:
        return _gen_job_running_response(running)

    cfg = db_load_config()
    try:
        candidates = _sort_generate_candidates(
            _build_generate_candidates(cfg, folders=folders or None, title_search=title_search or None),
            sort,
        )[:count]
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    # Read immediately after the build, which is what fetched the index. The
    # cache is 120s, so a user who spends longer than that setting filters
    # starts the run against a *re-fetched* library — one that may have lost an
    # instance since the page described it. The accessor's contract is "the list
    # the caller just received", and the caller that just received one is this
    # request, not the one that painted the page (B13).
    arr_errors = arr_media_index_errors()

    job_id = secrets.token_hex(8)
    now = time.time()
    job = {'id': job_id, 'status': 'running', 'total': len(candidates),
           'completed': 0, 'stop_flag': False, 'stop_reason': None, 'results': [],
           'started_at': now, 'last_polled': now, 'finished_at': None,
           # Kept on the job so a page that reattaches can still say which
           # instances this run could not read.
           'arr_errors': arr_errors}
    with _gen_jobs_lock:
        running = _running_gen_job()
        if running:
            return _gen_job_running_response(running)
        _gen_jobs[job_id] = job

    def do_generate():
        episode_cache = {}   # (connection, series) -> Sonarr's file->episode join, per job
        for candidate in candidates:
            if not job['stop_flag'] and time.time() - job['last_polled'] > _GEN_ABANDON_SECS:
                # Nobody has asked about this run for ten minutes: the tab that
                # started it is gone. Stop querying indexers for results no one
                # will see (B4). A page that merely navigated away reattaches
                # well inside that window.
                job['stop_flag'] = True
                job['stop_reason'] = 'abandoned'
            if job['stop_flag']:
                job['status'] = 'stopped'
                return
            scope = candidate.get('scope')
            result = {
                'key':               candidate.get('key'),
                'scope':             scope,
                'arr_title':         candidate.get('arr_title', ''),
                'arr_service':       candidate.get('arr_service'),
                'arr_connection_id': candidate.get('arr_connection_id'),
                'arr_id':            candidate.get('arr_id'),
                'arr_url':           candidate.get('arr_url'),
                'path':              candidate.get('rep_path') or candidate.get('path'),
                'season_number':     candidate.get('season_number'),
                'episode_numbers':   candidate.get('episode_numbers') or [],
                'file_count':        candidate.get('file_count', 1),
                'total_size':        candidate.get('total_size', 0),
                'file_quality':      candidate.get('file_quality', ''),
                'file_hdr':          candidate.get('file_hdr', ''),
                'file_ids':          candidate.get('file_ids') or [],
                'season_files_held':     candidate.get('season_files_held'),
                'season_files_unseeded': candidate.get('season_files_unseeded'),
                'search':            candidate.get('search'),
                'status':            'searching',
                'releases':          None,
                'best_release':      None,
                'error':             None,
            }
            job['results'].append(result)
            try:
                search = candidate['search']
                episode_numbers = candidate.get('episode_numbers') or []
                if scope == 'episode':
                    episode_id, episode_numbers = _episode_scope(cfg, candidate, episode_cache)
                    search = _backfill_search(candidate, episode_id=episode_id)
                    result['search'] = search
                    result['episode_numbers'] = episode_numbers
                rows = fetch_release_matrix(
                    cfg, search['service'], search['connection_id'], search['arr_id'],
                    episode_id=search.get('episode_id'),
                    season_number=search.get('season_number'),
                    file_path=search.get('path'),
                )
                rows = [r for r in rows if _release_in_scope(r, scope, episode_numbers)]
                filtered = _apply_release_filters(rows, download_from, seeding_on, res_filter=res_filter, source_filter=source_filter, hdr_filter=hdr_filter)
                # Closest to the file on disk unless the user asked for the
                # upgrade order (B3). `best` is what a single-release row grabs,
                # so the default pick must answer Backfill's question, not the arr's.
                filtered = _rank_releases(
                    filtered,
                    'upgrade' if data.get('release_rank') == 'upgrade' else 'closest',
                    {'total_size':   candidate.get('total_size') or 0,
                     'file_quality': candidate.get('file_quality') or '',
                     'file_hdr':     candidate.get('file_hdr') or ''},
                )
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

    def run():
        try:
            do_generate()
        except Exception as e:
            # A run left 'running' would refuse every later run forever.
            log.warning("Generate job %s failed: %s", job_id, e)
            job['status'] = 'error'
        finally:
            job['finished_at'] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'job_id': job_id, 'total': len(candidates), 'arr_errors': arr_errors})


@app.route('/api/workflows/generate/status')
@require_auth
def workflows_generate_status():
    """One run's progress, incrementally (B5).

    `since=n` returns only `results[n:completed]` — the rows that finished since
    the client last read — plus the in-flight row as `current`, without its
    releases. It used to re-send the whole growing result set, every candidate's
    full release list, every two seconds for a run that can last hours.
    Completed rows never change, so there is nothing to reconcile: the client
    appends them and asks again from `next`. Omitting `since` returns everything
    completed so far, which is how a page that navigated away catches up (B4).

    Polling is also what keeps a run alive: `last_polled` is what
    `_GEN_ABANDON_SECS` measures.
    """
    job = _gen_jobs.get(request.args.get('job_id', ''))
    if not job:
        # Swept, or from before a restart. The page forgets the id on this code.
        return jsonify({'status': 'error', 'code': 'job_not_found', 'message': 'Job not found'}), 404
    job['last_polled'] = time.time()
    # Rows below `completed` are final: the job thread bumps the count only after
    # it has finished writing the row.
    completed = job['completed']
    since = max(0, min(request.args.get('since', 0, type=int) or 0, completed))
    current = None
    if job['status'] == 'running' and len(job['results']) > completed:
        current = {k: v for k, v in job['results'][completed].items() if k != 'releases'}
    return jsonify({'status': job['status'], 'total': job['total'], 'completed': completed,
                    'since': since, 'next': completed,
                    'results': job['results'][since:completed],
                    'current': current,
                    'stop_reason': job.get('stop_reason'),
                    'arr_errors': job.get('arr_errors') or []})


@app.route('/api/workflows/generate/stop', methods=['POST'])
@require_auth
def workflows_generate_stop():
    data = request.json or {}
    job  = _gen_jobs.get(data.get('job_id', ''))
    if job and job['status'] == 'running':
        job['stop_flag'] = True
        job['stop_reason'] = 'stopped'
    return jsonify({'status': 'ok'})


_import_watches = {}  # job_id -> {status, message, title, service, completed_at}


def _record_backfill_credit(files):
    """Credit a Backfill grab on the Rounds prize layer.

    Counted at the event rather than in `run_audit_process`, for the reason
    `rounds.record_backfill` documents: the next scan sees a library that got a
    little better hardlinked, which is indistinguishable from an arr upgrading
    something on its own, and the media file is replaced on import so the path a
    transition diff would key on frequently does not survive the import.

    Fired when the grab is accepted, **not** when the import watch below
    confirms the file landed. The watch is an in-memory thread that has to live
    through the whole download to award a point for something that already
    happened, and every way it can end early — the container restarting mid
    download, an arr blip, an import that stalls until the user finishes it by
    hand — used to drop the credit with no way to earn it back. `db_update_meta`
    rather than get/set because a scan may be writing the same row.
    """
    try:
        db_update_meta('ns_progress',
                       lambda p: rounds.record_backfill(p, files=files))
    except Exception as e:
        log.warning("Could not record backfill on Rounds progress: %s", e)


def _int_list(value, cap=500):
    """A client-sent id list, reduced to at most `cap` ints. Anything else is dropped."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)][:cap]


def _watch_episode_scope(cfg, connection_id, series_id, file_ids):
    """The episode ids a Sonarr backfill may force-import over, or None if unknown.

    Joined from the library files the candidate was built from, through Sonarr's
    own episode list — `/api/v3/episodefile` carries no episode ids. None when
    there is nothing to join or Sonarr could not be asked, and the watch then
    force-imports nothing: a scope that cannot be established is not a licence
    to import the whole download.
    """
    if not file_ids:
        return None
    by_file = sonarr_episodes_by_file(cfg, connection_id, series_id)
    if by_file is None:
        return None
    ids = sorted({ep_id for fid in file_ids for ep_id, _s, _e in by_file.get(fid, [])})
    return ids or None


@app.route('/api/workflows/watch_import', methods=['POST'])
@require_auth
def workflows_watch_import():
    data          = request.json or {}
    service       = data.get('service', '')
    connection_id = data.get('connection_id', '')
    arr_id        = data.get('arr_id')
    title         = data.get('title', '') or ''
    # The candidate group's file count — a Sonarr season pack is one grab and N
    # episodes, and Matchmaker counts files. Clamped in `record_backfill`;
    # missing (an older frontend bundle) simply credits one.
    files         = data.get('files', 1)
    # The arr's ids for the library files this backfill is for. Only narrows:
    # a force import is scoped to their episodes (B11), and a Sonarr watch sent
    # none (an older bundle) force-imports nothing rather than everything.
    file_ids      = _int_list(data.get('file_ids'))
    if not service or not connection_id or arr_id is None:
        return jsonify({'status': 'error', 'message': 'Missing parameters'}), 400
    arr_name = 'Sonarr' if service == 'sonarr' else 'Radarr'

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

    # Scored here, at the grab, rather than from `mark_done` below: the watch is
    # a best-effort helper that nurses the download into the library, and tying
    # the prize to its survival meant a container restart or a stalled import
    # erased credit for work the user had already done. See
    # `_record_backfill_credit`.
    _record_backfill_credit(files)

    def do_watch():
        def mark_done():
            watch['status']       = 'done'
            watch['message']      = 'Imported successfully'
            watch['completed_at'] = time.time()

        def fail(message):
            watch['status']       = 'error'
            watch['message']      = message
            watch['completed_at'] = time.time()

        try:
            # Joined first, long before the import can land: the join runs from
            # file id to episode, and the import is what replaces those files and
            # retires their ids.
            scope = (_watch_episode_scope(cfg, connection_id, arr_id, file_ids)
                     if service == 'sonarr' else None)
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
                mark_done()
                return

            # If the item is still downloading (not yet importPending), the 300 s poll
            # timed out before the download finished — extend the wait instead of
            # firing force import against an incomplete file (which causes a 500 error).
            if not any(r.get('trackedDownloadState') == 'importPending' for r in last_active):
                watch['status']  = 'downloading'
                watch['message'] = 'Downloading — waiting for completion'
                last_active = poll_queue_until_clear(cfg, service, connection_id, arr_id, timeout=7200)
                if not last_active:
                    mark_done()
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
            else:
                # The arr's library folder used to stand in here, and its listing
                # is the library file itself — a force import from it re-imports
                # the file it is replacing (B11). Say so instead.
                fail(f"{arr_name} did not report where this download is, so nothing was "
                     f"force-imported — finish it from Activity → Queue in {arr_name}")
                return
            if service == 'sonarr' and not scope:
                # Unknown episodes are not every episode. A pack forced in whole
                # replaces files other torrents are hardlinked to (B1).
                fail("Could not tell which episodes this backfill was for, so nothing was "
                     "force-imported over your library — finish it from Activity → Queue in Sonarr")
                return

            watch['status']  = 'importing'
            watch['message'] = 'Importing — triggering manual import'

            # Retry loop: fire the command up to 3 times, confirming via both queue state
            # and a direct Arr API check (file ID change) after each attempt.
            still_active = last_active
            scope_error  = None
            for attempt in range(3):
                try:
                    force_manual_import_by_id(cfg, service, connection_id, arr_id,
                                              download_id=download_id, download_folder=download_folder,
                                              only_episode_ids=scope, media_folder_fallback=False)
                except ValueError as e:
                    # Typically: nothing in scope is importable any more — the arr
                    # took those episodes itself, and what is left is out of scope.
                    scope_error = str(e)
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
                watch['message'] = 'Import stalled: ' + ('; '.join(msgs) or scope_error or 'queue item remained')
                watch['completed_at'] = time.time()
            else:
                mark_done()
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
    # Every open auditorr page polls this every five seconds, which makes it the
    # one place Backfill's in-memory jobs are reliably swept (B13).
    _sweep_backfill_jobs()
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
