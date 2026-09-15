import os
import re
import json
import time
import posixpath
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
from audit import run_audit_process, process_health_metrics, compute_upload_stats, _is_not_imported_torrent, _is_cleanup_relevant, _compute_cross_seed_stats
from arr import _test_arr_connection, arr_rescan, arr_search, fetch_arr_media_index, fetch_arr_media_index_result, arr_media_index_errors, arr_root_folders, VIDEO_EXTENSIONS, queue_records_for_item, arr_titles_errors, arr_year_ok, rank_arr_candidates, test_arr_connections, fetch_arr_indexers, fetch_release_matrix, season_episodes_from_name, sonarr_episodes_by_file, grab_release, normalize_arr_connections, link_base, poll_queue_until_clear, force_manual_import_by_id, force_import_files, get_arr_file_id, read_arr_file_id, parse_release_info_for_path, fetch_arr_all_titles, fetch_arr_all_titles_result, title_match_keys, title_alias_keys, with_title_aliases, compare_release_quality, parse_quality_name, parse_trump_pm, match_trumped_torrent, rank_release_matches, score_release_match, title_soft_match, tracker_matches_indexer, release_match_cache_clear, _arr_candidate_score, rank_trump_replacements
from scripts import generate_script, build_cleanup_script, _build_dup_groups, dup_group_inputs
from media_server_exclusions import normalize_disc_rip_presets, normalize_media_server_presets, is_tombstone_path
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


# ---------------------------------------------------------------------------
# Cleanup — the orphan working set, its states, and the live re-verify (C3)
# ---------------------------------------------------------------------------

# Past this many torrents that *could* claim a selected path, the re-verify
# refuses (409 `selection_too_broad`) rather than issue that many per-torrent
# file listings inside one request. An orphan has no torrent by definition, so
# on a healthy library the count is zero; it climbs only where a client exposes
# no `content_path`, or a torrent is still downloading, under the save paths the
# selection sits in. The same bound Trumped's pre-filter uses, for the same
# reason: both backends list a torrent's files one call at a time.
_CLEANUP_VERIFY_BOUND = 150


class _CleanupRefusal(Exception):
    """A delete script that must not be built, with the reason as a response."""

    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def response(self):
        return jsonify({"status": "error", "code": self.code,
                        "message": self.message, **self.extra}), self.status


def _norm_abs(path):
    """An absolute path spelled for comparison: posix separators, no `//` or `..`.

    Every comparison in the re-verify goes through this on both sides, so a
    Windows test path, a trailing slash on `LOCAL_PATH` and a client that joins
    with a doubled separator all compare equal. Normalising can only make more
    paths match, and a match *drops* a file from the script — so it errs in the
    fail-safe direction.
    """
    p = str(path or '').replace('\\', '/')
    return posixpath.normpath(p) if p else ''


def _cleanup_paths(rec):
    """Every torrent-tree path of one orphan record — its own, then `other_paths`.

    A record is an **inode** (C5): `_assemble_records` emits one per inode and
    stamps the inode's other torrent-tree paths on orphans only. Relative and
    posix, the spelling `other_paths` is stored in.
    """
    return [str(rec.get('path') or '').replace('\\', '/')] + list(rec.get('other_paths') or [])


def _cleanup_state(rec):
    """What survives deleting every path of this orphan (CLEANUP §5.3, corrected).

    In precedence order — **an unknown never renders as safe**, TR3's ordering
    for TR3's reason:

    * `unverified` — the scan could not ask about this file (a failed instance
      on a scan the manual override persisted, or a failed listing whose disk
      fallback found nothing under its save path). It may be a live torrent's.
    * `library_copy` — a hardlink to the inode sits in `MEDIA_PATH`. Deleting
      is lossless and frees nothing.
    * `linked_elsewhere` — `nlink` exceeds the torrent-tree paths this row
      lists: a link outside both trees (a manual hardlink, a snapshot, a second
      library root auditorr is not configured for), or a sibling path that is
      excluded and so never listed. Lossless, frees nothing.
    * `last_copy` — nothing else is known to hold these bytes.

    **`cross_seed` is not a state**, which is the correction to §5.3: an orphan
    at two torrent-tree paths with no library copy is still the last copy of
    those bytes, and deleting both destroys it. Being cross-seeded is a fact
    about the row (`paths`), not about safety.

    A record with no `nlink` stamp — a failed stat, or a database whose last
    audit predates the field — has no evidence of another link, so it falls
    through to `last_copy`. Absence never produces the least alarming state.
    """
    if rec.get('unverified'):
        return 'unverified'
    if rec.get('imported') or rec.get('linked_paths'):
        return 'library_copy'
    nlink = rec.get('nlink')
    if isinstance(nlink, int) and not isinstance(nlink, bool) and nlink > len(_cleanup_paths(rec)):
        return 'linked_elsewhere'
    return 'last_copy'


def _cleanup_details():
    try:
        return ((db_load_results() or {}).get('dashboard') or {}) \
            .get('current', {}).get('details') or {}
    except Exception:
        return {}


def _cleanup_records():
    """(orphan records, excluded_count) — the compact row, or the full list filtered.

    The audit persists a compact `cleanup` row (`audit._is_cleanup_relevant`) so
    neither this page nor the delete script deserializes the full torrent list
    to read the ~1% it acts on (C10 — Triage's v1.7.0 fix, same shape). A
    database whose last audit predates the row falls back to the full list until
    the next scan.

    **Excluded orphans do not ride the compact row; their count rides the
    audit's details** (`orphaned_excluded_count`). The row is the working set
    and nothing on the page acts on an excluded file, while an install that has
    excluded a large folder of orphans would otherwise carry all of it in the
    row just to produce one integer. `None` where the details predate the key.

    Filesystem tombstones are dropped here as well as at the walk, for Dedupe's
    reason: records from a scan that predates the always-on rule can still carry
    one as `excluded: False`, and `rm` on a tombstone frees nothing.
    """
    if db_has_file_results('cleanup'):
        records = db_load_file_results('cleanup')
        excluded = _cleanup_details().get('orphaned_excluded_count')
    else:
        full = db_load_file_results('torrents')
        records = [f for f in full if _is_cleanup_relevant(f)]
        excluded = sum(1 for f in full if f.get('status') == 'Orphaned' and f.get('excluded'))
        del full
    return [r for r in records if not is_tombstone_path(r.get('path'))], excluded


def _cleanup_claim_candidates(rows, wanted_abs, remote, local):
    """The live torrents that could claim any selected path — and only those.

    A torrent claims its listing joined on `save_path`, the incomplete spellings
    of an unfinished payload, or whatever its disk fallback finds at
    `save_path/name` and `content_path` (`sources.torrent_claimed_paths`). So a
    torrent is a candidate when:

    * its `save_path/name` or its `content_path` **is** a selected path, is one
      without the `.!qB` suffix, or **contains** one; or
    * it exposes no `content_path`, or is not known to be complete, and its
      `save_path` contains one — without `content_path` a listing's names are
      not bounded to the torrent's own folder, and an unfinished payload's
      final paths need not sit under its name.

    Selected paths are expanded into the set of their ancestors once, so this is
    one pass over the listing with set lookups, not a product of the two.

    Deliberately **not** `_trump_candidates`, which answers a different question
    (which torrents can share a file with *each other*).
    """
    suffix = sources.INCOMPLETE_SUFFIX
    exact = set(wanted_abs) | {a[:-len(suffix)] for a in wanted_abs if a.endswith(suffix)}
    ancestors = set()
    for a in wanted_abs:
        d = posixpath.dirname(a)
        while d and d not in ancestors:
            ancestors.add(d)
            parent = posixpath.dirname(d)
            if parent == d:
                break
            d = parent
    out = []
    for r in rows:
        sp = _norm_abs(sources.remap_path(r.get('save_path') or '', remote, local))
        name = r.get('name') or ''
        cp_raw = r.get('content_path') or ''
        cp = _norm_abs(sources.remap_path(cp_raw, remote, local)) if cp_raw else ''
        root = _norm_abs(f'{sp}/{name}') if sp and name else ''
        if any(x and (x in exact or x in ancestors) for x in (root, cp)):
            out.append(r)
            continue
        complete = sources.torrent_complete(r.get('progress'), r.get('completion_on'))
        if (not cp or complete is not True) and (not sp or sp in ancestors):
            out.append(r)
    return out


def _cleanup_row_claims(row, client_paths, remote, local):
    """Paths one live torrent claims, by the audit's own rule.

    `fetch_torrent_file_paths` answers **client-side, unremapped** paths — both
    backends join the client's raw `save_path` and neither applies
    `REMOTE_PATH` → `LOCAL_PATH` (Phase 7 checked both). The claim rule wants the
    listing's names relative to that save path, so the prefix comes off here and
    the remap happens exactly once, on `save_path` and `content_path`, inside
    the shared rule. A path outside the torrent's save path is remapped and
    claimed as it stands.

    **`[]` is treated like `None`** — a torrent that lists no files is not
    evidence that nothing sits at its roots, and routing it through the disk
    fallback can only claim more. It is also what the audit itself does on qui.
    """
    sp_client = row.get('save_path') or ''
    save_path = sources.remap_path(sp_client, remote, local)
    cp_client = row.get('content_path') or ''
    content_path = sources.remap_path(cp_client, remote, local) if cp_client else ''
    complete = sources.torrent_complete(row.get('progress'), row.get('completion_on'))
    names, extra = None, []
    if client_paths:
        prefix = str(sp_client).replace('\\', '/').rstrip('/') + '/'
        names = []
        for cp in client_paths:
            c = str(cp).replace('\\', '/')
            if not sp_client:
                names.append(c)
            elif c.startswith(prefix):
                names.append(c[len(prefix):])
            else:
                extra.append(sources.remap_path(c, remote, local))
    return sources.torrent_claimed_paths(
        save_path, row.get('name') or '', content_path, names, complete) + extra


def _cleanup_live_claims(cfg, rel_paths):
    """The selected paths a live torrent claims **right now** (CLEANUP C3 + C4c).

    The fact a delete script rests on — *no torrent claims this file* — is the
    one fact the script cannot check: it runs on the host, often on another
    machine, with no client credentials, by construction. So the last honest
    verification point is here, server-side, immediately before the script is
    built. In the window since the audit a cross-seed script can inject a
    torrent for exactly these files, a torrent can be re-added and rechecked, or
    one added during the walk can land (C4c, the race no snapshot can close).

    Raises `_CleanupRefusal` instead of answering partially:

    * the client unreachable → 502 `client_unreachable`;
    * any instance failed → 502 `instances_unavailable`. A torrent on the
      instance that did not answer is invisible, not absent — the same reading
      Trumped takes of `list_torrents`, which refuses a partial listing;
    * more candidates than `_CLEANUP_VERIFY_BOUND` → 409 `selection_too_broad`.
      Never a script emitted unverified, never a check silently narrowed.

    Logs counts only: the log ring reaches `/api/debug/report`.
    """
    remote, local = cfg.get('REMOTE_PATH', ''), cfg.get('LOCAL_PATH', '')
    base = _norm_abs(local)
    abs_of = {p: _norm_abs(f'{base}/{p}') for p in rel_paths}
    wanted = set(abs_of.values())
    unreachable = ("Could not reach your torrent client to check the selection just before "
                   "building the script, so no script was built. A torrent added since the last "
                   "scan could be using these files — try again once the client answers.")
    try:
        rows, report = sources.list_torrents_detailed(cfg)
    except Exception as e:
        log.warning("Cleanup: no delete script — the torrent client could not be listed (%s)",
                    type(e).__name__)
        raise _CleanupRefusal(502, 'client_unreachable', unreachable)
    failed = (report or {}).get('instances_failed') or []
    if failed:
        total = (report or {}).get('instances_total', '?')
        names = ', '.join(str(f.get('name', '?')) for f in failed[:3])
        log.warning("Cleanup: no delete script — %d of %s client instance(s) did not answer",
                    len(failed), total)
        raise _CleanupRefusal(
            502, 'instances_unavailable',
            f"{len(failed)} of {total} torrent-client instance(s) did not answer ({names}), so no "
            f"script was built — a torrent on an instance that did not answer could be using "
            f"these files.")
    candidates = _cleanup_claim_candidates(rows, wanted, remote, local)
    if len(candidates) > _CLEANUP_VERIFY_BOUND:
        log.warning("Cleanup: no delete script — %d torrents could claim the selection (bound %d)",
                    len(candidates), _CLEANUP_VERIFY_BOUND)
        raise _CleanupRefusal(
            409, 'selection_too_broad',
            f"Checking this selection would mean reading the file lists of {len(candidates)} "
            f"torrents, past the limit of {_CLEANUP_VERIFY_BOUND}. Select fewer folders at a time.",
            candidates=len(candidates), bound=_CLEANUP_VERIFY_BOUND)
    if not candidates:
        return set()
    try:
        listings = sources.fetch_torrent_file_paths(cfg, candidates) or {}
    except Exception as e:
        log.warning("Cleanup: no delete script — torrent file listings failed (%s)",
                    type(e).__name__)
        raise _CleanupRefusal(502, 'client_unreachable', unreachable)
    claimed = set()
    for row in candidates:
        for p in _cleanup_row_claims(row, listings.get(row.get('hash')), remote, local):
            n = _norm_abs(p)
            if n in wanted:
                claimed.add(n)
    return {rel for rel, a in abs_of.items() if a in claimed}


def _cleanup_script_response(cfg, selection):
    """`orphaned_torrents_delete` — a delete script for an explicit, re-verified selection.

    **A selection is required** (C14, decided 2026-09-13: remove `delete_selected`
    end-to-end and require a non-empty selection here). An unselected script over
    every orphan had no UI caller, and CLEANUP §6 rules out "clean everything":
    C2's failure mode is one click from catastrophic, so nothing may be pre-armed.

    A path is accepted only if the last audit listed it as a non-excluded orphan
    — a record's own path or one of its `other_paths`. Then:

    * any `unverified` path → 409 `unverified`, so the rule is not only a
      disabled checkbox;
    * the live re-verify (`_cleanup_live_claims`) — refusals as documented there;
    * claimed paths are dropped and the header says how many;
    * nothing left → 409 `nothing_left`, not a script that does nothing.

    The body stays plain text so copy and download are unchanged; what the page
    needs to correct its subtitle rides response headers — the verification
    time, the dropped count, the files in the script and the bytes it can free
    at most. Dedupe's script path is untouched by any of this.
    """
    raw = selection.get('paths') if isinstance(selection, dict) else None
    wanted = list(dict.fromkeys(str(p).replace('\\', '/') for p in
                                (raw if isinstance(raw, list) else []) if str(p).strip()))
    if not wanted:
        return _CleanupRefusal(
            400, 'selection_required',
            "Select the files to delete first — a delete script is only built for an explicit "
            "selection.").response()
    if not cfg.get('LOCAL_PATH'):
        return _CleanupRefusal(
            400, 'local_path_unset',
            "Your torrent folder (LOCAL_PATH) is not configured, so the selection cannot be "
            "checked against your torrent client. Set it in Config first.").response()

    records, excluded_count = _cleanup_records()
    by_path = {}
    for rec in records:
        for p in _cleanup_paths(rec):
            by_path[p] = rec
    known = [p for p in wanted if p in by_path]
    not_in_report = len(wanted) - len(known)
    unverified = sum(1 for p in known if by_path[p].get('unverified'))
    if unverified:
        return _CleanupRefusal(
            409, 'unverified',
            f"{unverified} of the selected files could not be checked on the last scan, so no "
            f"delete script was built. They become checkable again after a scan that reads every "
            f"torrent's file list.", unverified=unverified).response()
    if not known:
        return _CleanupRefusal(
            409, 'nothing_left',
            "None of the selected files are orphaned in the last scan, so there is nothing to "
            "delete.", dropped=0, not_in_report=not_in_report).response()

    try:
        claimed = _cleanup_live_claims(cfg, known)
    except _CleanupRefusal as refusal:
        return refusal.response()
    kept = [p for p in known if p not in claimed]
    if claimed:
        log.info("Cleanup: %d of %d selected file(s) are claimed by a torrent now — "
                 "left out of the delete script", len(claimed), len(known))
    if not kept:
        return _CleanupRefusal(
            409, 'nothing_left',
            f"Every selected file is in use by a torrent in your client now "
            f"({len(claimed)} file{'s' if len(claimed) != 1 else ''}), so there is nothing to delete.",
            dropped=len(claimed), not_in_report=not_in_report).response()

    units, by_record = [], {}
    for p in kept:
        rec = by_path[p]
        unit = by_record.get(id(rec))
        if unit is None:
            unit = by_record[id(rec)] = {
                'paths': [], 'size': rec.get('size') or 0, 'state': _cleanup_state(rec),
                '_all': set(_cleanup_paths(rec)),
            }
            units.append(unit)
        unit['paths'].append(p)
    for unit in units:
        # An inode's bytes go only when every one of its paths goes, and only a
        # last copy frees anything when they do.
        unit['frees'] = unit['state'] == 'last_copy' and set(unit['paths']) == unit.pop('_all')

    verified_at = int(time.time())
    safe_folders = {str(r['excl_folder']).replace('\\', '/') for r in records if r.get('excl_folder')}
    script = build_cleanup_script(
        units, verified_at=verified_at, dropped=len(claimed), not_in_report=not_in_report,
        excluded_count=excluded_count, safe_folders=safe_folders)
    resp = app.response_class(script, mimetype='text/plain; charset=utf-8')
    resp.headers['X-Auditorr-Verified-At'] = str(verified_at)
    resp.headers['X-Auditorr-Dropped'] = str(len(claimed))
    resp.headers['X-Auditorr-Files'] = str(len(kept))
    resp.headers['X-Auditorr-Freeable'] = str(sum(u['size'] for u in units if u['frees']))
    return resp


@app.route('/api/actions/script/<script_type>', methods=['GET', 'POST'])
@require_auth
def get_action_script(script_type):
    cfg = db_load_config()
    # POST carries a selection from a workflow page: {'paths': [...]} for the
    # Cleanup delete script, {'groups': [...]} (canonical paths) for dedupe.
    selection = (request.get_json(silent=True) or {}) if request.method == 'POST' else None
    if script_type == 'orphaned_torrents_delete':
        # Built from the compact `cleanup` row and a live check of the client —
        # never from the full torrent list, and never without a selection.
        return _cleanup_script_response(cfg, selection or {})
    if script_type != 'dedupe':
        return jsonify({"status": "error", "message": "Unknown script type"}), 400
    results = db_load_results()
    results['torrent_files'] = db_load_file_results('torrents')
    results['media_files'] = db_load_file_results('media')
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

# One video extension set, shared with Backfill (`arr.VIDEO_EXTENSIONS`). This was
# a narrower local copy, and `_classify_triage_junk` calls anything outside it a
# sidecar — so a `.m4v` film was offered as "`.m4v` files", one click from hiding it.
_VIDEO_EXTS = VIDEO_EXTENSIONS
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


# Candidate searches a removal resolution runs before it stops and calls itself
# bounded. Each round searches near every member found so far; the second round
# almost always finds nothing new, so this only guards a pathological chain.
_REMOVAL_ROUNDS = 5
# A client can drop a torrent a moment after its API returns, so a removal still
# listed on the first look is looked at once more before it is reported as such.
_REMOVAL_RECHECK_SECS = 2


def _removal_ownership(cfg, rows, seed_hashes):
    """Everything removing these torrents could touch, and who else holds each file (S01).

    The question Trumped answers before a trump, asked with Trumped's primitives
    rather than a third copy of them: `_trump_candidates` for the pre-filter
    (overlapping content roots, or size within 1% — **never exact size**, TR2)
    and `_cross_seed_group` for membership, the closure over shared file paths.

    One thing is added. Candidates are searched near **every member found so
    far**, round after round, not only near the seeds. A sidecar-only torrent
    sharing a file with a seed's cross-seed need share nothing with the seed —
    its root sits inside the cross-seed's, not the seed's, and its size is
    nowhere near — so a single search from the seeds never meets it, and
    removing that cross-seed with its files breaks it (the outside review's
    design amendment 2). The rounds stop when a search finds nobody new, which
    is the second round on any ordinary library.

    Returns `{by_hash, paths, holders, groups, unknown, bounded}`: `paths` is
    `{hash: [paths] | None}` for every torrent asked, `holders` is
    `{path: {hashes}}`, `groups` is `{seed hash: [member rows]}`, `unknown`
    counts asked torrents outside every group whose listing is `None` (a torrent
    that may share a file and could not say), and `bounded` is true when the
    search was cut short. `_removal_file_decision` reads all of it.
    """
    by_hash = {r['hash']: r for r in rows}
    seeds = [by_hash[h] for h in dict.fromkeys(seed_hashes) if h in by_hash]
    paths, bounded, members = {}, False, list(seeds)
    for _ in range(_REMOVAL_ROUNDS):
        candidates, prefilter = _trump_candidates(rows, members)
        bounded = bounded or prefilter['bounded']
        fresh = [c for c in candidates if c['hash'] not in paths]
        room = _TRUMP_CANDIDATE_BOUND - len(paths)
        if len(fresh) > room:
            bounded, fresh = True, fresh[:max(room, 0)]
        if not fresh:
            break
        got = sources.fetch_torrent_file_paths(cfg, fresh)
        for c in fresh:
            paths[c['hash']] = got.get(c['hash'])
        members, _, _ = _cross_seed_group([by_hash[h] for h in paths], paths, seeds)
    else:
        bounded = True

    holders = {}
    for h, listing in paths.items():
        for p in listing or ():
            holders.setdefault(p, set()).add(h)
    asked = [by_hash[h] for h in paths]
    groups = {}
    for s in seeds:
        group, _, _ = _cross_seed_group(asked, paths, s)
        groups[s['hash']] = group or [{**s, 'paths': []}]
    in_groups = {g['hash'] for group in groups.values() for g in group}
    unknown = sum(1 for h, listing in paths.items() if listing is None and h not in in_groups)
    return {'by_hash': by_hash, 'paths': paths, 'holders': holders, 'groups': groups,
            'unknown': unknown, 'bounded': bounded}


def _removal_file_decision(own, h, removal):
    """`('delete' | 'keep', reason)` for one torrent of a removal set (S01).

    Files are deleted only when ownership is **established**: the torrent's own
    listing is usable, no torrent that survives the removal holds any of its
    paths, and nothing about the answer is unknown. Anything else keeps them —
    the user's decision (a), 2026-09-15: a registration-only removal harms
    nothing, and its worst case is an orphan, which Cleanup re-verifies against
    the client before it will delete anything.

    The reasons, in the order they are checked:

    * `not_in_client` — the torrent is not in the listing at all.
    * `unusable_listing` — its own listing is `None` *or* `[]`. `[]` is an
      honest answer for a candidate (it holds nothing to share) but not for the
      torrent being removed: its files are exactly what the listing failed to
      name. Trumped's seed rule.
    * `shared` — a survivor holds one of its paths. A survivor holding a
      *different path to the same inode* (a distinct hardlink) does not count:
      deleting this torrent drops only its own link.
    * `unknown` — a candidate's listing could not be read, or the search was
      bounded. Either could be a survivor this answer cannot see.
    * `requested` — none of the above; the files go, as asked.
    """
    if h not in own['by_hash']:
        return 'keep', 'not_in_client'
    listing = own['paths'].get(h)
    if not listing:
        return 'keep', 'unusable_listing'
    if any(o not in removal for p in listing for o in own['holders'].get(p, ())):
        return 'keep', 'shared'
    if own['unknown'] or own['bounded']:
        return 'keep', 'unknown'
    return 'delete', 'requested'


def _keeps_files(mode):
    """True when a removal request asked to keep every file.

    Only an explicit no keeps them. `auto`, `true` and an **omitted** value all
    take the ownership-checked path. An omitted value used to default to `True`
    and delete with no check at all, and the only caller in auditorr sends
    `auto` — decided by the user, 2026-09-15, so no unchecked delete mode
    survives as an API affordance.
    """
    if mode is None or mode is False:
        return True
    if isinstance(mode, str):
        return mode.strip().lower() in ('false', 'keep', 'no', '0')
    return mode == 0


def _removal_plan_refusal(own, plan, decisions):
    """None when the confirmed plan still holds, else a 409 `plan_changed`.

    **The confirmed plan binds** — the user's TR5 decision for Trumped
    ("grown ⇒ refuse", 2026-09-13), and the outside review's design amendment 2
    for any destructive group. The modal posts what it showed: each group's
    members and each removed torrent's file decision. The server has just
    re-resolved, and refuses when:

    * a group gained or lost a member — new information about what is touched;
    * a torrent shown **keeping** its files would now have them deleted. A
      decision the plan does not name was never shown going, so it counts as
      shown kept.

    A flip the other way — shown deleted, now kept — is the safe direction and
    goes ahead; the route reports it.
    """
    posted_groups = plan.get('groups') if isinstance(plan.get('groups'), dict) else {}
    posted_files = plan.get('files') if isinstance(plan.get('files'), dict) else {}
    added = lost = 0
    for seed, shown in posted_groups.items():
        shown = {str(h) for h in (shown if isinstance(shown, list) else [])}
        now = {g['hash'] for g in own['groups'].get(str(seed), [])}
        added += len(now - shown)
        lost += len(shown - now)
    now_deleting = sum(1 for h, (files, _) in decisions.items()
                       if files == 'delete' and posted_files.get(h) != 'delete')
    if not (added or lost or now_deleting):
        return None
    log.warning("Client delete: refusing — the plan changed since it was shown "
                "(%d added, %d gone, %d would now delete files)", added, lost, now_deleting)
    parts = ([f"{added} more torrent{'s' if added != 1 else ''} share these files"] if added else []) + \
            ([f"{lost} no longer {'do' if lost != 1 else 'does'}"] if lost else []) + \
            ([f"{now_deleting} would now have files deleted that were shown as kept"] if now_deleting else [])
    return jsonify({
        "status": "error", "code": "plan_changed",
        "added": added, "lost": lost, "now_deleting": now_deleting,
        "message": f"What this removal would do has changed since the dialog opened — {'; '.join(parts)}. "
                   "Nothing was removed. Review it and confirm again.",
    }), 409


def _removal_outcomes(cfg, hashes, before):
    """`{hash: outcome}` — what the client lists after a removal (S09).

    `sources.remove_torrents` reports what it *submitted*: qui counts what it
    posted (and a later instance's `raise_for_status` loses the earlier
    instances' outcomes with the request), qbit counts hashes that existed.
    Neither says what left. Rather than rewrite both backends' return contract,
    this looks — one `list_torrents_detailed` — and answers per hash:

    * `removed` — listed before, absent now, from an instance that answered;
    * `already_gone` — not listed before either;
    * `still_listed` — still there. Looked at once more after
      `_REMOVAL_RECHECK_SECS`, since a client can drop a torrent a moment
      after its API returns; **not verified against a live qui bulk action**,
      which may apply asynchronously — the re-check is the allowance for it;
    * `unknown` — its instance did not answer, so absence means nothing.

    The page dismisses only `removed` and `already_gone` rows and names the rest.
    """
    instance_of = {r['hash']: r.get('instance_name') for r in before}

    def _look():
        try:
            rows, report = sources.list_torrents_detailed(cfg)
        except Exception as e:
            log.warning("Client delete: could not list the client afterwards (%s)", type(e).__name__)
            return None
        failed = {str(f.get('name')) for f in report.get('instances_failed') or []}
        listed = {r['hash'] for r in rows}
        out = {}
        for h in hashes:
            inst = instance_of.get(h)
            if failed and (inst is None or str(inst) in failed):
                out[h] = 'unknown'
            elif h in listed:
                out[h] = 'still_listed'
            else:
                out[h] = 'removed' if h in instance_of else 'already_gone'
        return out

    out = _look() or {h: 'unknown' for h in hashes}
    lingering = [h for h in hashes if out[h] == 'still_listed']
    if lingering:
        time.sleep(_REMOVAL_RECHECK_SECS)
        again = _look()
        if again:
            for h in lingering:
                out[h] = again[h]
    return out


@app.route('/api/workflows/remove_torrents', methods=['POST'])
@require_auth
def workflows_remove_torrents():
    """Remove selected torrents from the client, deleting files only where it is established safe.

    `delete_files` is `false` (keep every file) or anything else — `auto`,
    `true`, or omitted — which takes the ownership-checked path: per torrent,
    files go only when no surviving torrent holds them and nothing about that
    answer is unknown (`_removal_file_decision`). `auto` used to delete whenever
    it *found* no survivor, and a listing it could not read, a survivor it could
    not list and a survivor one `.nfo` larger all found none (S01).

    `plan` — `{seeds, groups: {seed: [hashes]}, files: {hash: keep|delete}}`,
    what the confirm modal showed — binds: the group is re-resolved here, once,
    and a change refuses with 409 `plan_changed` (`_removal_plan_refusal`).
    Without a plan the checks still run; there is just nothing to hold them to.

    The response says, per torrent, what happened to its files and why, and
    whether it actually left the client (`_removal_outcomes`) — including on a
    502, where a removal that failed part-way still reports what it removed.

    Destructive — gated behind the ALLOW_CLIENT_DELETE config flag (off by
    default) so conservative users can keep auditorr strictly read-only
    against their client. A failed instance refuses a checked removal: its
    torrents are invisible to the ownership check, not merely uncounted.
    """
    cfg = db_load_config()
    if not cfg.get('ALLOW_CLIENT_DELETE'):
        return jsonify({
            "status": "error",
            "message": "Client deletion is disabled — enable it in Config → Torrent Source first.",
        }), 403
    data  = request.json or {}
    items = list({str(i.get('hash')): {'hash': str(i.get('hash')), 'instance_id': i.get('instance_id')}
                  for i in (data.get('items') or []) if isinstance(i, dict) and i.get('hash')}.values())
    if not items:
        return jsonify({"status": "error", "message": "No torrent hashes provided"}), 400

    keep_files = _keeps_files(data.get('delete_files', True))
    plan = data.get('plan') if isinstance(data.get('plan'), dict) else None
    hashes = [i['hash'] for i in items]
    try:
        # Keeping files needs no ownership answer, so a listing short of an
        # instance is still good enough to report outcomes against.
        before = sources.list_torrents_detailed(cfg)[0] if keep_files else sources.list_torrents(cfg)
    except sources.SourceConnectionError as e:
        return jsonify({"status": "error", "message": str(e)}), 502

    flipped = 0
    if keep_files:
        decisions = {h: ('keep', 'requested') for h in hashes}
    else:
        seeds = [*(str(s) for s in ((plan or {}).get('seeds') or [])),
                 *(str(s) for s in (((plan or {}).get('groups') or {}) if isinstance((plan or {}).get('groups'), dict) else {})),
                 *hashes]
        own = _removal_ownership(cfg, before, seeds)
        removal = set(hashes)
        decisions = {h: _removal_file_decision(own, h, removal) for h in hashes}
        if plan is not None:
            refusal = _removal_plan_refusal(own, plan, decisions)
            if refusal is not None:
                return refusal
            posted = plan.get('files') if isinstance(plan.get('files'), dict) else {}
            flipped = sum(1 for h, (files, _) in decisions.items()
                          if files == 'keep' and posted.get(h) == 'delete')
        kept = [r for f, r in decisions.values() if f == 'keep']
        if kept:
            log.info("Client delete: keeping files for %d of %d torrent(s) — %d shared, %d unknown, "
                     "%d unusable listing, %d not in client",
                     len(kept), len(hashes), kept.count('shared'), kept.count('unknown'),
                     kept.count('unusable_listing'), kept.count('not_in_client'))

    delete_items = [i for i in items if decisions[i['hash']][0] == 'delete']
    keep_items   = [i for i in items if decisions[i['hash']][0] != 'delete']
    submitted, error = 0, None
    try:
        if delete_items:
            submitted += sources.remove_torrents(cfg, delete_items, delete_files=True)
        if keep_items:
            submitted += sources.remove_torrents(cfg, keep_items, delete_files=False)
    except sources.SourceConnectionError as e:
        error = str(e)

    outcomes = _removal_outcomes(cfg, hashes, before)
    torrents = [{'hash': h, 'files': decisions[h][0], 'reason': decisions[h][1], 'outcome': outcomes[h]}
                for h in hashes]
    removed = sum(1 for t in torrents if t['outcome'] == 'removed')
    files_deleted = sum(1 for t in torrents if t['outcome'] == 'removed' and t['files'] == 'delete')
    log.info("Client delete: %d/%d torrent(s) left the client (%d with files deleted, %d submitted, "
             "%d still listed, %d unknown, %d kept files where deletion was shown)",
             removed, len(items), files_deleted, submitted,
             sum(1 for t in torrents if t['outcome'] == 'still_listed'),
             sum(1 for t in torrents if t['outcome'] == 'unknown'), flipped)
    # A keep-files removal touches the client and nothing else, so there is no
    # filesystem event for the watcher to see — yet the torrent is gone and
    # every count that mentions it is now wrong. Even a delete-files removal is
    # worth nudging: it makes the audit start from the last *action* rather than
    # from whichever inotify event happened to arrive last.
    if removed or submitted:
        nudge_watchdog('torrents removed via the client')
    body = {"removed": removed, "requested": len(items), "submitted": submitted,
            "files_deleted": files_deleted, "files_kept": removed - files_deleted,
            "flipped_to_keep": flipped, "torrents": torrents, "outcomes": outcomes}
    if error is not None:
        return jsonify({"status": "error", "message": error, **body}), 502
    return jsonify({"status": "success", **body})


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

    Two rules, each a Phase 9 fix:

    * **S08 — only a read that happened is `checked`.** The file id comes from
      `read_arr_file_id`, which raises on a failed read, rather than
      `get_arr_file_id`, which answered `None` — so a timeout used to come back
      `checked: true, file_id: null` and read as an import.
    * **T11 — a Sonarr row watches its own episodes, not the series.** The
      series' file ids move whenever Sonarr imports *any* episode of it, and a
      busy series retired a row whose file never landed. An item carrying
      `season` (and `episode`, or `episodes` for a multi-episode row; neither
      for a season pack) is answered with the file ids of those episodes,
      joined through `sonarr_episodes_by_file` once per series per request;
      a `None` join is `checked: false`. An item with no `season` — a browser
      bundle older than this — gets the series-wide reading it always did.

    Each result carries `scope`: `movie`, `episode`, `season` or `series`.
    """
    cfg   = db_load_config()
    items = (request.json or {}).get('items') or []
    if not items:
        return jsonify({"status": "error", "message": "No items provided"}), 400

    results, episode_lists = [], {}
    for item in items[:_IMPORT_CHECK_MAX]:
        key     = str(item.get('key') or '')
        service = str(item.get('service') or '')
        if service not in ('sonarr', 'radarr') or not item.get('connection_id') \
                or item.get('arr_id') is None:
            # Not something the arr can be asked about — the caller keeps
            # showing it until an audit clears it.
            results.append({"key": key, "file_id": None, "checked": False})
            continue
        season = item.get('season')
        by_episode = service == 'sonarr' and isinstance(season, int) and not isinstance(season, bool)
        try:
            if by_episode:
                fid, scope = _import_check_episode_files(cfg, item, episode_lists)
            else:
                fid = read_arr_file_id(cfg, service, item['connection_id'], item['arr_id'])
                scope = 'movie' if service == 'radarr' else 'series'
            results.append({"key": key, "file_id": fid, "checked": True, "scope": scope})
        except Exception as e:
            # `checked: false` is not `file_id: null` — one means "could not
            # ask", the other means "asked, and it holds no file". Collapsing
            # them would read an unreachable arr as a successful import.
            log.warning("Import check failed for a %s item (%s)", service, type(e).__name__)
            results.append({"key": key, "file_id": None, "checked": False})

    return jsonify({"status": "success", "results": results})


def _import_check_episode_files(cfg, item, episode_lists):
    """`(sorted episode-file ids, scope)` for the episodes one Sonarr row is about (T11).

    `episode_lists` caches `sonarr_episodes_by_file` per `(connection, series)`
    for the request. Raises when the series' episode list could not be read, so
    the caller reports `checked: false` rather than an empty answer.
    """
    series = (item['connection_id'], item['arr_id'])
    if series not in episode_lists:
        episode_lists[series] = sonarr_episodes_by_file(cfg, *series)
    by_file = episode_lists[series]
    if by_file is None:
        raise LookupError('episode list unavailable')
    season = item['season']
    listed = item.get('episodes')
    if isinstance(listed, list):
        wanted = {e for e in listed if isinstance(e, int) and not isinstance(e, bool)} or None
    elif isinstance(item.get('episode'), int) and not isinstance(item.get('episode'), bool):
        wanted = {item['episode']}
    else:
        wanted = None
    ids = sorted(fid for fid, eps in by_file.items()
                 if any(s == season and (wanted is None or e in wanted) for _, s, e in eps))
    return ids, ('season' if wanted is None else 'episode')


@app.route('/api/workflows/triage/resolve_groups', methods=['POST'])
@require_auth
def workflows_triage_resolve_groups():
    """Resolve each selected Triage torrent to everything its removal touches, live.

    Triage records keep one hash per path (the healthiest claimant), so sibling
    cross-seeds are invisible in the report and have to be asked of the client.
    The answer is `_removal_ownership`'s — **the same resolution the removal
    route re-runs at confirm** — so the modal shows exactly the decisions the
    server will hold it to.

    Per group member:

    * `shares_path` / `shares_with` — which other members hold one of its paths.
      Cross-seed topology is not uniform: a shared path (one file, several
      registrations) is broken by deleting it, a distinct hardlink is not.
    * `files` / `reason` — the server's file decision under "this torrent only"
      (the seed alone; `None` for other members, which that scope does not
      remove) and under "the whole group" (`_removal_file_decision`).

    And for the request: `checked` (false when a candidate's listing could not be
    read or the search was bounded — every decision then keeps files, and the
    modal says so before confirm), `unknown_listings`, `bounded`, and `missing`
    (selected hashes no longer in the client, which get an empty group).

    This returned HTTP 500 on every call with a seed in the client from `a96bb15`
    to Phase 9: Phase 7 changed `_cross_seed_group` to return a tuple and this
    caller, which no test called, kept iterating it.
    """
    data   = request.json or {}
    hashes = list(dict.fromkeys(str(h) for h in (data.get('hashes') or []) if h))
    if not hashes:
        return jsonify({"status": "error", "message": "No torrent hashes provided"}), 400
    cfg = db_load_config()
    try:
        rows = sources.list_torrents(cfg)
    except sources.SourceConnectionError as e:
        return jsonify({"status": "error", "message": str(e)}), 502

    own = _removal_ownership(cfg, rows, hashes)
    checked = not (own['unknown'] or own['bounded'])
    if not checked:
        log.warning("Triage resolve_groups: ownership not established — %d candidate listing(s) "
                    "unknown, search bounded: %s", own['unknown'], own['bounded'])

    # Enrich every unique group member with live details (seeding time, tracker
    # health) — one batched call across all resolved groups.
    seen, members_in = set(), []
    for g in own['groups'].values():
        for t in g:
            if t['hash'] not in seen:
                seen.add(t['hash'])
                members_in.append({'hash': t['hash'], 'instance_id': t.get('instance_id')})
    try:
        details = sources.fetch_torrent_details(cfg, members_in) if members_in else {}
    except Exception as e:
        log.warning("Triage resolve_groups: detail fetch failed: %s", type(e).__name__)
        details = {}

    out = {}
    for h in hashes:
        group = own['groups'].get(h)
        if not group:
            out[h] = []
            continue
        everyone = {g['hash'] for g in group}
        members = []
        for t in group:
            det = details.get(t['hash'], {})
            listing = own['paths'].get(t['hash']) or []
            shares_with = sorted({o for p in listing for o in own['holders'].get(p, ())} - {t['hash']})
            one = _removal_file_decision(own, t['hash'], {h}) if t['hash'] == h else (None, None)
            whole = _removal_file_decision(own, t['hash'], everyone)
            members.append({
                'hash':           t['hash'],
                'instance_id':    t.get('instance_id'),
                'instance_name':  t.get('instance_name'),
                'name':           t.get('name') or '',
                'tracker':        t.get('tracker') or '',
                'size':           t.get('size') or 0,
                'seeding_time':   det.get('seeding_time'),
                'uploaded':       det.get('uploaded'),
                'tracker_health': det.get('tracker_health', 'unknown'),
                'tracker_msg':    det.get('tracker_msg', ''),
                'shares_path':    bool(shares_with),
                'shares_with':    shares_with,
                'files':          {'one': one[0], 'all': whole[0]},
                'reason':         {'one': one[1], 'all': whole[1]},
            })
        members.sort(key=lambda m: m['name'])
        out[h] = members

    return jsonify({
        "status":           "success",
        "groups":           out,
        "missing":          [h for h in hashes if h not in own['by_hash']],
        "checked":          checked,
        "unknown_listings": own['unknown'],
        "bounded":          own['bounded'],
    })


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


# Client paths `search_release` will stat for one group. A season pack is a few
# dozen; this only bounds what a request can make the container stat.
_TRUMP_GROUP_PATHS_MAX = 2000


def _trump_same_item(a, b):
    """True when two arr rows or summaries name the same item on the same instance."""
    return bool(a and b) and ((a.get('service'), a.get('connection_id'), a.get('arr_id'))
                              == (b.get('service'), b.get('connection_id'), b.get('arr_id')))


def _trump_item_summary(row):
    return {k: row.get(k) for k in ('service', 'connection_id', 'connection_name', 'arr_id',
                                    'title', 'year', 'title_slug')}


def _trump_item_label(row):
    year = row.get('year')
    return f"{row.get('title') or '?'}{f' ({year})' if year else ''}"


def _trump_items_from_paths(cfg, group_paths, parsed):
    """The arr items holding a trump group's bytes, joined by inode (TR7).

    Group paths are torrent-tree paths and media-index rows are library paths;
    the arr renames on import, so the two share **inodes, not names**. The
    join, chosen over the two alternatives ROADMAP Phase 7 lists:

    * reading the audit's `linked_paths` would deserialize the full `torrents`
      `file_results` row — the known RAM hotspot — on a wizard step;
    * a compact persisted row would describe the last audit rather than the live
      group, and add a write path for one endpoint;
    * so: stat the group, then stat index rows — **only those whose recorded
      `size` matches a group file**, because a hardlink shares its size. That
      turns ~3,600 stats on `fuse.shfs` into a handful. A row whose size is
      stale (a file edited in place) is missed and the title match answers
      instead, which is the degraded direction, never a wrong hit.

    Returns `{status, items, errors}`. `status` is `matched`, `no_match`,
    `group_unreadable` (no group path could be stat'ed) or `index_unavailable`.
    Each item is a summary plus the `file_ids` of its hit rows, ranked by
    `_arr_candidate_score` against the parsed release — stable, and **not
    gated**: the files are authoritative, and a year or service guess from a
    release name must not overrule them.
    """
    inodes = {(st.st_dev, st.st_ino): st.st_size
              for st in _trump_stat_paths(cfg, group_paths).values()
              if st is not None and st.st_ino}
    if not inodes:
        return {'status': 'group_unreadable', 'items': [], 'errors': []}
    try:
        # Rows and errors from one snapshot (S11): the accessor reads whichever
        # fetch landed last, which under concurrent requests need not be this one.
        index, errors = fetch_arr_media_index_result(cfg)
    except Exception as e:
        log.warning("Trump: media index unavailable for the path lookup: %s", e)
        return {'status': 'index_unavailable', 'items': [], 'errors': []}
    sizes = set(inodes.values())
    hits = {}
    for row in index:
        size = row.get('size')
        if size is not None and size not in sizes:
            continue
        try:
            st = os.stat(row.get('path') or '')
        except (OSError, ValueError):
            continue
        if st.st_ino and (st.st_dev, st.st_ino) in inodes:
            hits.setdefault((row.get('service'), row.get('connection_id'), row.get('arr_id')), []).append(row)
    ranked = sorted(hits.values(),
                    key=lambda rows: -max(_arr_candidate_score(r, parsed) for r in rows))
    items = [{**_trump_item_summary(rows[0]),
              'file_ids': sorted({r['file_id'] for r in rows
                                  if isinstance(r.get('file_id'), int)}),
              'files': len(rows)}
             for rows in ranked]
    return {'status': 'matched' if items else 'no_match', 'items': items, 'errors': errors}


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


# Phase 2's pre-filter bounds how many torrents get a per-torrent file listing —
# a sequential round trip each, in both backends. It is a *cost* bound and never
# a membership rule (TR2). Past the bound it falls back to size-exact and marks
# the group partial rather than narrowing silently.
_TRUMP_CANDIDATE_BOUND = 150
# A shared-path sibling carrying one extra file (a tracker-required .nfo, a
# different sample) differs in total size by a sliver of its payload.
_TRUMP_SIZE_TOLERANCE  = 0.01


def _trump_content_root(row):
    """`save_path/name` in posix form, or None — where a torrent's files live."""
    sp = str(row.get('save_path') or '').replace('\\', '/').rstrip('/')
    name = str(row.get('name') or '')
    return f'{sp}/{name}' if sp and name else None


def _trump_candidates(rows, seeds):
    """(candidates, prefilter) — the torrents whose file lists phase 2 fetches.

    A torrent is a near neighbour of a seed when their **content roots overlap**
    (equal, or one inside the other) or their sizes are within
    `_TRUMP_SIZE_TOLERANCE`. The first rule is what "same save_path" stands in
    for, and it is deliberately not that: in a category layout every film
    shares `/data/torrents/movies`, so save_path equality admits the whole
    category and trips the bound on every trump. Two torrents can only share a
    file path if one's files sit under the other's root. The size rule catches
    a sibling whose root was renamed or laid out without a subfolder.

    Past `_TRUMP_CANDIDATE_BOUND` this falls back to size-exact — the old rule —
    and reports `bounded`, which makes the group partial: a narrower search is
    a smaller answer, and it has to say so.
    """
    bound = _TRUMP_CANDIDATE_BOUND
    seed_hashes = {s['hash'] for s in seeds}
    roots = [r for r in (_trump_content_root(s) for s in seeds) if r]

    def _near(r):
        if r['hash'] in seed_hashes:
            return True
        root = _trump_content_root(r)
        if root and any(root == sr or root.startswith(sr + '/') or sr.startswith(root + '/')
                        for sr in roots):
            return True
        size = r.get('size') or 0
        return any(abs(size - (s.get('size') or 0)) <= _TRUMP_SIZE_TOLERANCE * (s.get('size') or 0)
                   for s in seeds)

    widened = [r for r in rows if _near(r)]
    if len(widened) <= bound:
        return widened, {'candidates': len(widened), 'bound': bound, 'bounded': False}
    sizes = {s['size'] for s in seeds}
    exact = [r for r in rows if r['hash'] in seed_hashes or r['size'] in sizes]
    return exact, {'candidates': len(exact), 'widened': len(widened), 'bound': bound, 'bounded': True}


def _cross_seed_group(rows, paths_map, seeds):
    """(group, components, unknown) — everything a delete of `seeds` touches.

    **Membership is the transitive closure over shared file paths**, not "shares
    a path with the seed", and payload size plays no part (TR2). Every member is
    deleted *with its files*, so a torrent sharing a path with any member is
    harmed whether or not it shares one with the seed; and a sibling carrying
    one extra `.nfo` shares every file that matters while differing in size.

    `paths_map` is {hash: [paths] | None}. **`None` is "could not ask"** and a
    candidate in that state cannot be placed, so it is counted in `unknown` —
    the group may be missing it, which is TR1c. **`[]` is an answer**: the
    client says the torrent holds no files, so it has nothing on disk to share
    or to lose, and it is not counted. The caller refuses outright when a
    *seed's* own listing is unusable either way.

    `components` lists member hashes per connected payload, in seed order, so a
    caller can count each payload once. Each group row gains a sorted `paths`.
    """
    if isinstance(seeds, dict):
        seeds = [seeds]
    by_hash = {r['hash']: r for r in rows}
    holders = {}
    for r in rows:
        for p in paths_map.get(r['hash']) or []:
            holders.setdefault(p, []).append(r['hash'])
    seen, components = set(), []
    for s in seeds:
        if s['hash'] in seen or s['hash'] not in by_hash:
            continue
        seen.add(s['hash'])
        comp, stack = [], [s['hash']]
        while stack:
            h = stack.pop()
            comp.append(h)
            for p in paths_map.get(h) or []:
                for other in holders.get(p, ()):
                    if other not in seen:
                        seen.add(other)
                        stack.append(other)
        components.append(comp)
    group = [{**by_hash[h], 'paths': sorted(paths_map.get(h) or [])}
             for comp in components for h in comp]
    unknown = sum(1 for r in rows if r['hash'] not in seen and paths_map.get(r['hash']) is None)
    return group, components, unknown


def _trump_resolve_group(cfg, rows, seed_hashes):
    """Phase 2's expansion — shared by `resolve_group` and `execute`'s re-verify.

    Returns a dict whose `status` is `ok`, `no_seeds` (none of the seeds is in
    the client) or `seed_unknown` (a seed's own listing could not be used).

    A seed whose listing is `None` *or* `[]` refuses the whole request: there is
    no honest degraded answer for a seed, only a smaller one. Its group would
    collapse to the seed alone, and deleting that torrent's files leaves every
    cross-seed sibling registered and seeding on top of the hole — silently, in
    the default configuration, reported as a successful one-torrent group.

    Logs counts only, never names or hashes: the log ring reaches
    `/api/debug/report`.
    """
    by_hash = {r['hash']: r for r in rows}
    wanted = list(dict.fromkeys(seed_hashes))
    seeds = [by_hash[h] for h in wanted if h in by_hash]
    if not seeds:
        return {'status': 'no_seeds'}
    candidates, prefilter = _trump_candidates(rows, seeds)
    paths_map = sources.fetch_torrent_file_paths(cfg, candidates)
    unusable = [s for s in seeds if not paths_map.get(s['hash'])]
    if unusable:
        log.warning("Trump: refusing to resolve — no file listing for %d of %d seed(s)",
                    len(unusable), len(seeds))
        return {'status': 'seed_unknown', 'unknown_seeds': len(unusable), 'seeds': len(seeds)}

    group, components, unknown = _cross_seed_group(candidates, paths_map, seeds)
    by_member = {g['hash']: g for g in group}
    # One payload per connected component, however many registrations stand
    # on it; the largest member is the payload plus any extra sidecar.
    total_size = sum(max((by_member[h].get('size') or 0) for h in comp) for comp in components)
    partial = bool(unknown) or prefilter['bounded']
    if partial:
        log.warning("Trump: group resolved partial — %d of %d candidate listing(s) unknown, "
                    "pre-filter bounded: %s", unknown, len(candidates), prefilter['bounded'])
    link_check = _trump_link_state(cfg, group)
    return {
        'status':           'ok',
        'seeds':            seeds,
        'missing_seeds':    len(wanted) - len(seeds),
        'group':            group,
        'total_size':       total_size,
        'partial':          partial,
        'unknown_listings': unknown,
        'prefilter':        prefilter,
        'link_check':       link_check,
    }


def _trump_stat_paths(cfg, client_paths):
    """`{client path: os.stat_result | None}` for torrent-client file paths.

    The client names files in its own filesystem, and **neither source backend
    remaps a per-torrent file listing** — `_qbit` joins the client's raw
    `save_path`, `_qui` joins `_norm_torrent`'s, and only `fetch_file_map`
    applies `REMOTE_PATH` → `LOCAL_PATH`. So the swap happens here, exactly once:
    twice and every `stat` misses, never and every `stat` misses too. A path
    that cannot be stat'ed — not visible to the container, a mapping the user
    has not configured — is `None`, which is an unknown and never a safe answer.
    Read-only: `/data` is never written.
    """
    remote, local = cfg.get('REMOTE_PATH', ''), cfg.get('LOCAL_PATH', '')
    out = {}
    for p in client_paths:
        if p in out:
            continue
        try:
            out[p] = os.stat(sources.remap_path(p, remote, local))
        except (OSError, ValueError):
            out[p] = None
    return out


def _trump_link_state(cfg, group):
    """Does anything outside this group still hold each member's bytes? (TR3)

    The confirm screen's whole safety argument for `delete_files=True` was
    "your library hardlinks survive", and nothing checked it; M1 measured 3.5%
    of the reference box's torrent-tree files at `st_nlink == 1`. Every distinct
    path in the **whole group** is stat'ed and grouped by `(st_dev, st_ino)`:
    *own* is how many distinct group paths sit on an inode, *outside* is
    `st_nlink − own`. A file is True when outside > 0, False when it is 0 — this
    group holds the only links — and None when its `stat` failed or came back
    odd (no inode number, fewer links than the group's own paths).

    A member is False if any file is False; otherwise None if any is None or it
    has no paths; otherwise True. **False outranks None outranks True**: a known
    destruction beats an unknown, and an unknown never renders as safe.

    True means *a link outside this group exists*, not "the library copy
    exists" — a distinct-hardlink cross-seed counts, and correctly survives. It
    is also only as good as the group: a shared-path sibling the resolution
    missed adds no link, but a distinct-hardlink one it missed reads as outside.
    The inode grouping is sound on `fuse.shfs` because shfs synthesizes inodes
    consistently (M1, F8 dead); anywhere it is not, the odd `stat` degrades.

    Sets `hardlinked` and `only_copy_bytes` on each member in place and returns
    the group-wide summary, each inode counted once.
    """
    stats = _trump_stat_paths(cfg, [p for m in group for p in (m.get('paths') or [])])
    own = {}
    for st in stats.values():
        if st is not None and st.st_ino:
            key = (st.st_dev, st.st_ino)
            own[key] = own.get(key, 0) + 1

    def _file_state(st):
        if st is None or not st.st_ino:
            return None
        outside = st.st_nlink - own[(st.st_dev, st.st_ino)]
        return None if outside < 0 else outside > 0

    verdict = {p: _file_state(st) for p, st in stats.items()}
    only_copy = {}
    for p, v in verdict.items():
        if v is False:
            only_copy[(stats[p].st_dev, stats[p].st_ino)] = stats[p].st_size
    for m in group:
        states = [verdict[p] for p in (m.get('paths') or [])]
        if any(s is False for s in states):
            m['hardlinked'] = False
        elif not states or any(s is None for s in states):
            m['hardlinked'] = None
        else:
            m['hardlinked'] = True
        mine = {(stats[p].st_dev, stats[p].st_ino): stats[p].st_size
                for p in (m.get('paths') or []) if verdict[p] is False}
        m['only_copy_bytes'] = sum(mine.values())
    return {
        'only_copy_files': len(only_copy),
        'only_copy_bytes': sum(only_copy.values()),
        'unchecked_files': sum(1 for v in verdict.values() if v is None),
    }


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
        try:
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
        finally:
            # Every title after the first re-reads the same names from the cache
            # (TR11); nothing needs them once the request is answered.
            release_match_cache_clear()
        return jsonify({"status": "needs_pick", "picks": picks})

    # Phase 2 — expand the confirmed seeds into their full cross-seed groups.
    # A failed instance has already refused above, deliberately *not* as
    # `partial`: a sibling living on an instance that did not answer is
    # invisible rather than narrowed, and no acknowledgement makes that safe.
    res = _trump_resolve_group(cfg, rows, seed_hashes)
    if res['status'] == 'no_seeds':
        return jsonify({
            "status": "error",
            "message": "None of the selected torrents are still in the client — re-run the search.",
        }), 404
    if res['status'] == 'seed_unknown':
        return jsonify({
            "status": "error",
            "message": f"Could not read the file list for {res['unknown_seeds']} of the {res['seeds']} "
                       "selected torrent(s), so their cross-seed groups cannot be resolved. "
                       "Check that the torrent client is reachable and try again.",
        }), 502
    group = res['group']

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
        "status":           "success",
        "torrents":         group,
        "total_size":       res['total_size'],
        "partial":          res['partial'],
        "unknown_listings": res['unknown_listings'],
        "prefilter":        res['prefilter'],
        "link_check":       res['link_check'],
    })


@app.route('/api/workflows/trump/search_release', methods=['POST'])
@require_auth
def workflows_trump_search_release():
    """Find the replacement release in Sonarr/Radarr's release search.

    **Which arr item** is decided from the confirmed group's own files first
    (TR7, what `TRUMP.md` step 4 specified): `group_paths` are the client paths
    phase 2 returned, joined to the arrs' media index by inode. The title match
    is the fallback for a group that resolves to nothing. Where a path hit and a
    title hit disagree the endpoint says so (409 `arr_item_conflict`) rather
    than picking, and the wizard re-asks with the user's `arr_item`. A path hit
    also yields the library file ids a trump's import watch is scoped to (TR9).

    Then exact normalized title match (release names are effectively unique
    ids), optionally prioritising the indexer the PM came from. Always returns
    the arr deep link as a manual fallback.
    """
    data      = request.json or {}
    new_title = str(data.get('new_title') or '').strip()
    indexer   = str(data.get('indexer') or '').strip()
    if not new_title:
        return jsonify({"status": "error", "message": "new_title is required"}), 400
    cfg    = db_load_config()
    parsed = parse_release_info_for_path(new_title)
    # The titles and their errors from one snapshot (S11). "Fetch, then read the
    # accessor, in this request" was a sequential contract; another request's
    # refresh could land between the two and describe a list nobody received.
    titles, arr_errors = fetch_arr_all_titles_result(cfg)
    title_item = _trump_find_arr_item(cfg, parsed, titles=titles, name=new_title)

    group_paths = [p for p in (data.get('group_paths') or []) if isinstance(p, str) and p]
    lookup = (_trump_items_from_paths(cfg, group_paths[:_TRUMP_GROUP_PATHS_MAX], parsed)
              if group_paths else {'status': 'no_group', 'items': [], 'errors': []})
    for e in lookup['errors']:
        if not any((e.get('connection_id'), e.get('partial')) == (a.get('connection_id'), a.get('partial'))
                   for a in arr_errors):
            arr_errors.append(e)
    path_items = lookup['items']
    title_path = next((p for p in path_items if _trump_same_item(p, title_item)), None)
    choice     = data.get('arr_item') if isinstance(data.get('arr_item'), dict) else None

    item, resolved_by, library_file_ids = None, None, []
    if choice:
        chosen = next((p for p in path_items if _trump_same_item(p, choice)), None)
        if chosen is not None:
            item, library_file_ids = chosen, chosen['file_ids']
        elif _trump_same_item(title_item, choice):
            item = title_item
        else:
            return jsonify({"status": "error",
                            "message": "That Sonarr/Radarr item is not one of the matches for this group — "
                                       "search again."}), 400
        resolved_by = 'choice'
    elif path_items and title_item is not None and title_path is None:
        # Two independent answers to "which item is this", and they differ. The
        # files say one thing and the new release's name another — a renamed
        # title, a remake, a second instance. Picking either silently is how a
        # trump ends in the wrong grab, so the user decides.
        return jsonify({
            "status": "error", "code": "arr_item_conflict",
            "path_item":  path_items[0], "path_items": path_items,
            "title_item": _trump_item_summary(title_item),
            "arr_errors": arr_errors,
            "message": f"Your library files for this group belong to “{_trump_item_label(path_items[0])}”, "
                       f"but the new release's name matches “{_trump_item_label(title_item)}”. "
                       "Choose which one to search.",
        }), 409
    elif path_items:
        item = title_path or path_items[0]
        library_file_ids, resolved_by = item['file_ids'], 'path'
    elif title_item is not None:
        item, resolved_by = title_item, 'title'
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

    # The exact release on the PM's tracker leads and is pre-selected; the same
    # release elsewhere follows (grab there and cross-seed — an edge case, not
    # the default); then everything else. The PM's indexer is still a priority
    # and never a filter: a release not yet listed on that tracker is not a dead
    # end. Exactness gates the top, because the fuzzy score cannot tell a
    # REPACK from the release it trumped — see `rank_trump_replacements`.
    release, candidates = rank_trump_replacements(releases, new_title, indexer, limit=8)
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
        "resolved_by":     resolved_by,
        # The library files the trumped release was imported as — the scope a
        # trump's import watch may force-import over (TR9). Only ever from the
        # path join: a title match knows the item, not which of its files.
        "library_file_ids": library_file_ids,
        "path_lookup":     lookup['status'],
        "path_items":      path_items,
        # 4b's UI half, as a payload flag: more than one instance holds this
        # payload. Unreachable on the reference install (M4), so it is tested by
        # fixture rather than rendered blind.
        "arr_item_ambiguous": len(path_items) > 1,
    })


def _trump_reverify(cfg, items, data):
    """Re-resolve the posted group and compare. None to proceed, else a response.

    * **Identical** — proceed.
    * **Shrunk, or a hash the client no longer holds** — 409 `group_changed`.
    * **Grown** — also 409 `group_changed`. Decided by the user (2026-09-13):
      deleting only the posted hashes leaves the newcomer registered on top of
      deleted files (TR1's harm, recreated), and deleting it too removes a
      torrent nobody was shown. A grown group is new information about what
      will be touched, and TRUMPED §1 says that is seen before acting;
      re-resolving costs one click.
    * **Partial** — 409 `partial` unless `acknowledge_partial`.
    * **Any member holds the only copy** — 409 `only_copy` unless
      `acknowledge_only_copy`.

    A failed instance or an unusable seed listing is a 502, never a partial:
    those are not narrowed answers but missing ones. Seeds are the posted
    `seed_hashes` (the torrents the user confirmed in step 3) when they are part
    of the posted set, else every posted hash — with the confirmed seeds, a
    member that stopped sharing the payload drops out and is caught as shrunk.
    """
    posted = {i['hash'] for i in items}
    seeds = [h for h in (str(s) for s in (data.get('seed_hashes') or [])) if h in posted]
    try:
        rows = sources.list_torrents(cfg)
    except sources.SourceConnectionError as e:
        return jsonify({"status": "error", "message": str(e)}), 502
    res = _trump_resolve_group(cfg, rows, seeds or sorted(posted))
    if res['status'] == 'seed_unknown':
        return jsonify({
            "status": "error",
            "message": "Could not re-read the file list of the selected torrent(s) just before "
                       "removing them, so nothing was removed. Check that the torrent client is "
                       "reachable and try again.",
        }), 502

    group = {g['hash'] for g in res.get('group') or []}
    added, missing = len(group - posted), len(posted - group)
    if added or missing:
        log.warning("Trump: refusing execute — the group changed since it was confirmed "
                    "(%d added, %d gone)", added, missing)
        parts = ([f"{added} torrent{'s' if added != 1 else ''} joined it"] if added else []) + \
                ([f"{missing} {'are' if missing != 1 else 'is'} no longer part of it"] if missing else [])
        return jsonify({
            "status": "error", "code": "group_changed", "added": added, "missing": missing,
            "message": f"The cross-seed group changed since you confirmed it — {' and '.join(parts)}. "
                       "Nothing was removed. Go back to step 3 and confirm the group again.",
        }), 409

    if res['partial'] and not data.get('acknowledge_partial'):
        return jsonify({
            "status": "error", "code": "partial",
            "unknown_listings": res['unknown_listings'], "prefilter": res['prefilter'],
            "message": "The group could not be fully checked just now, so a cross-seed sharing "
                       "these files may be missing from it. Nothing was removed.",
        }), 409

    only = [g for g in res['group'] if g.get('hardlinked') is False]
    if only and not data.get('acknowledge_only_copy'):
        return jsonify({
            "status": "error", "code": "only_copy",
            "only_copy_bytes": res['link_check']['only_copy_bytes'],
            "only_copy_files": res['link_check']['only_copy_files'],
            "torrents": [{'hash': g['hash'], 'name': g.get('name') or '',
                          'only_copy_bytes': g.get('only_copy_bytes') or 0} for g in only],
            "message": "Nothing outside this group holds some of these files — removing it "
                       "destroys the only copy. Nothing was removed.",
        }), 409
    return None


@app.route('/api/workflows/trump/execute', methods=['POST'])
@require_auth
def workflows_trump_execute():
    """Remove the confirmed group via the client, and grab the replacement.

    Either half may be absent. With no release, this only removes and the user
    grabs through the arr deep link. **With no `hashes`, this is a grab** (TR4),
    and needs no ALLOW_CLIENT_DELETE: the flag gates the *removal*, not the
    request. It used to gate everything, so in the default configuration a user
    could run all four steps, watch the wizard find the exact release, and get
    a 403 — while Backfill grabs freely, because grabbing is not destructive.
    """
    cfg   = db_load_config()
    data  = request.json or {}
    items = [{'hash': str(i.get('hash') or ''), 'instance_id': i.get('instance_id')}
             for i in (data.get('hashes') or []) if i.get('hash')]
    release  = data.get('release') or {}
    grabbing = bool(release.get('guid') and data.get('service'))
    if not items and not grabbing:
        return jsonify({"status": "error",
                        "message": "Nothing to do — no torrents to remove and no release to grab."}), 400
    if items and not cfg.get('ALLOW_CLIENT_DELETE'):
        return jsonify({
            "status": "error",
            "message": "Client deletion is disabled — enable it in Config → Torrent Source first.",
        }), 403

    # TR5 — the last honest verification point is here, server-side, immediately
    # before the delete. The wizard's group can be minutes old: a tab left open,
    # a cross-seed script adding a registration, a torrent rechecked into a
    # different path. The expansion is re-run from the confirmed seeds and must
    # land on exactly the posted set.
    if items:
        refusal = _trump_reverify(cfg, items, data)
        if refusal is not None:
            return refusal

    # With the grab no longer behind the delete flag, a double submit is a real
    # second download. Backfill's advisory queue check (B12), not an idempotency
    # key: the replacement already sitting in the arr's queue — a second click,
    # or the arr having grabbed it from RSS on its own — is refused unless
    # `force`, and **before** anything is deleted. A queue that cannot be read
    # does not block; the answer says it was not checked.
    queue_checked = None
    arr_id = data.get('arr_id')
    if grabbing and isinstance(arr_id, int) and not isinstance(arr_id, bool) and not data.get('force'):
        season = data.get('season_number')
        queued = queue_records_for_item(
            cfg, data['service'], data.get('connection_id'), arr_id,
            season_number=season if isinstance(season, int) else None)
        queue_checked = queued is not None
        if queued:
            titles = [q.get('title') for q in queued if q.get('title')]
            name = 'Sonarr' if data['service'] == 'sonarr' else 'Radarr'
            return jsonify({"status": "error", "code": "already_queued", "titles": titles[:5],
                            "message": f"Already in {name}'s queue: {titles[0] if titles else 'this item'}. "
                                       "Nothing was removed or grabbed."}), 409

    removed = 0
    if items:
        try:
            removed = sources.remove_torrents(cfg, items, delete_files=True)
        except sources.SourceConnectionError as e:
            return jsonify({"status": "error", "message": str(e)}), 502

    grabbed, grab_error = None, ''
    if grabbing:
        try:
            grab_release(cfg, data['service'], data.get('connection_id'),
                         release['guid'], release.get('indexer_id'))
            grabbed = True
        except Exception as e:
            grabbed = False
            grab_error = str(e)

    # Credit the swap on the Rounds prize layer. Trumped is counted at execute
    # time: the swap trades one release for another, so the audit that follows
    # the import sees a library in much the same shape and has nothing to infer
    # the action from. A grab with nothing removed is not a swap and pays nothing.
    if removed:
        try:
            db_update_meta('ns_progress',
                           lambda p: rounds.record_trump(p, torrents=removed))
        except Exception as e:
            log.warning("Could not record trump on Rounds progress: %s", e)

    # TR9 — the old payload is gone before the new one exists, so the user is
    # unprotected for the whole download window, and the grab used to be fired
    # and abandoned. It is followed by the same import watch Backfill uses —
    # scoped to the library files the trumped release was imported as, which
    # only the path lookup knows (`library_file_ids`) — and the re-audit waits
    # for the import (TR10) instead of recording the hole left by the delete.
    watch_job_id = None
    arr_id = data.get('arr_id')
    if grabbed and isinstance(arr_id, int) and not isinstance(arr_id, bool) and data.get('connection_id'):
        watch_job_id = _start_import_watch(
            cfg, data['service'], data['connection_id'], arr_id,
            title=str(data.get('arr_title') or ''),
            file_ids=_int_list(data.get('library_file_ids')), source='trump')
    elif removed:
        # Nothing to follow: the watchdog's entry point, not a direct scan, so
        # the deferral and debounce every other client action gets apply here.
        nudge_watchdog('trumped torrents removed via the client')
    log.info("Trump execute: removed %d/%d torrent(s), grabbed=%s, watching import=%s",
             removed, len(items), grabbed, bool(watch_job_id))
    return jsonify({"status": "success", "removed": removed, "requested": len(items),
                    "grabbed": grabbed, "grab_error": grab_error,
                    "queue_checked": queue_checked, "watch_job_id": watch_job_id})


# T5's ordering. When a torrent's files earn different verdicts the row takes the
# one whose action deletes least: `library_unknown` says "do not act on this until
# the arr answers", `import_pending` says "rescan", `superseded` offers a delete for
# a copy the library already beats, and `not_in_library` is the bucket that invites
# one. A season pack with one episode the arr is waiting to import is not junk
# because its other nine are unmatched.
_TRIAGE_LEAST_DESTRUCTIVE = ('library_unknown', 'import_pending', 'superseded', 'not_in_library')


def _triage_pick_instance(rows, parsed):
    """`(winner, rivals)` among ranked arr rows for one release (T2).

    `rows` come ranked by `rank_arr_candidates`. The winner is the first, except
    where another **instance** ties it on that ranking and holds a file of the
    same quality as the release while the first does not: a 1080p and a 4K
    Sonarr both hold the episode, and the honest comparison — and the right
    place to send a rescan or a force import — is the instance whose file
    matches. That is TRIAGE T2's third ranking criterion ("prefer the instance
    whose file most closely matches"). Rows from the winner's own instance never
    reorder, so a single-instance install gets the ranker's answer unchanged.

    `rivals` is the first row from each *other* instance that also matched — the
    ambiguity the row says out loud rather than resolving silently.
    """
    if not rows:
        return None, []
    winner = rows[0]
    if 'file_quality_name' in winner and \
            compare_release_quality(parsed, winner.get('file_quality_name') or '') != 'same':
        top = _arr_candidate_score(winner, parsed)
        winner = next((r for r in rows[1:]
                       if r.get('connection_id') != winner.get('connection_id')
                       and _arr_candidate_score(r, parsed) == top
                       and compare_release_quality(parsed, r.get('file_quality_name') or '') == 'same'),
                      winner)
    rivals, seen = [], {winner.get('connection_id')}
    for r in rows:
        if r.get('connection_id') not in seen:
            seen.add(r.get('connection_id'))
            rivals.append(r)
    return winner, rivals


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

    # Dead registrations: torrents the tracker dropped whose payload is still
    # alive — on a working cross-seed sibling and/or the hardlinked library copy.
    # The audit merge keeps the healthy claimant per inode and stashes the dead
    # ones in `dead_siblings`; they are invisible everywhere else. Surface each
    # as its own removable registration. Collected before the cap so one cap
    # covers both kinds of row (T8), and against every listed hash rather than
    # only those the cap keeps — a torrent the cap cut is still not also a dead
    # registration, which is how `count_triage_items` counts.
    listed_hashes = {g['hash'] for g in groups.values() if g['hash']}
    dead_reg = {}
    for f in torrent_files:
        if f.get('excluded') or not f.get('dead_siblings'):
            continue
        for s in f['dead_siblings']:
            h = s.get('hash')
            if not h or h in listed_hashes:
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

    # T8 — one cap, on the combined list, largest first. It used to be applied to
    # the torrent rows and again to the dead registrations, so a page could hold
    # twice the cap while `truncated` reflected only the first, and the banner
    # said "500" whatever the cap was. The badge (`count_triage_items`) still
    # counts everything.
    listing = sorted([('torrent', g) for g in groups.values()] +
                     [('dead_registration', g) for g in dead_reg.values()],
                     key=lambda kind_group: -kind_group[1]['total_size'])
    total = len(listing)
    listing = listing[:_TRIAGE_GROUP_CAP]

    # S11 — each fetch hands back its rows and its errors from **one** snapshot.
    # This used to call the fetch and then its errors accessor, which reads
    # whichever fetch landed last: under eight gthreads another request's refresh
    # could land in between, leaving this request `[]` rows and no errors, and
    # the gate below reading that silence as a healthy arr.
    #
    # An arr that did not answer contributes no rows, which is the same empty
    # list as an arr that manages nothing — and Triage turns that silence into a
    # verdict with a delete button under it. `arr_configured` is derived from
    # the config rather than from the fetch, so it stays true and the existing
    # banner never fires.
    arr_errors = []
    try:
        media_index, index_errors = fetch_arr_media_index_result(cfg)
        arr_errors.extend(index_errors)
    except Exception as e:
        log.warning("Triage: media index fetch failed: %s", e)
        media_index = []
        # The per-connection loop inside has its own try, so reaching here means
        # the whole call failed and no snapshot was published. Synthesize.
        arr_errors.append({'connection_id': '', 'name': 'Sonarr/Radarr library',
                           'service': '', 'partial': False, 'message': str(e)})
    try:
        all_titles, title_errors = fetch_arr_all_titles_result(cfg)
        arr_errors.extend(title_errors)
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

    def _conn_name(row):
        return (row.get('connection_name')
                or (conn_by_id.get(row.get('connection_id')) or {}).get('name') or '')

    def _classify(path):
        """One file's library match and its verdict when the tracker has not dropped it.

        Library rows with files are gated to the service that fits the content
        type. A *gate*, not a tiebreak: this used to end `... or lib_rows`, which
        fell back to the wrong-type rows whenever the right type had none, so a
        TV episode of a same-titled series absent from Sonarr matched the film in
        Radarr (Fargo, Hannibal, Dune, Shōgun — the collisions are not exotic).
        That produced quality_cmp 'same', which is exactly what renders the Force
        import button, and force_import_files then posts replaceExistingFiles
        against the movie's id: one episode deliberately written over a library
        film, past every rejection spec (T1).

        Within the gate the rows are **ranked** (T2, `rank_arr_candidates`) —
        they are pooled across every connection under one title key, so `[0]`
        was whichever instance answered first — and the title list gets the
        same gate, which it never had. The year gate is the shared `arr_year_ok`
        (Radarr ±1, Sonarr one-sided), replacing an endpoint-local copy that was
        Radarr ±1 and blind to a title that is itself a year. Ranking happens
        within the rows a title match produced, aliases included; it never opens
        a second matching path.
        """
        parsed = parse_release_info_for_path(path)
        # Canonical keys first, then the arrs' alternate titles — an exact match
        # always wins, an alias only rescues what would otherwise match nothing.
        keys = with_title_aliases(title_match_keys(parsed['title']), title_aliases)
        is_episode = parsed['season'] is not None
        preferred = 'sonarr' if is_episode else 'radarr'
        lib_rows = rank_arr_candidates(
            next((lib_by_title[k] for k in keys if k in lib_by_title), []), parsed, service=preferred)
        if is_episode:
            # Same episode, or any episode of the same season for season packs
            se_tag = (f"s{parsed['season']:02d}e{parsed['episode']:02d}" if parsed['episode'] is not None
                      else f"s{parsed['season']:02d}e")
            matches = [r for r in lib_rows
                       if se_tag in os.path.basename(r.get('relative_path') or r.get('path') or '').lower()]
        else:
            matches = lib_rows
        library_match, library_rivals = _triage_pick_instance(matches, parsed)
        title_rows = next((ranked for ranked in
                           (rank_arr_candidates(titles_by_norm.get(k, []), parsed, service=preferred)
                            for k in keys)
                           if ranked), [])
        title_hit, title_rivals = _triage_pick_instance(title_rows, parsed)

        if library_match:
            verdict = 'superseded'
        elif title_hit or lib_rows:
            verdict = 'import_pending'
        elif arr_degraded:
            # `not_in_library` means "no arr has ever heard of this", and its
            # copy ends "junk can be deleted" — a not-imported torrent has no
            # hardlink anywhere by definition, so those files are the only copy.
            # That verdict must not be reachable when no arr answered: absence
            # of evidence is not evidence of absence (the same rule Phase 2's
            # source guard applies to the torrent client). `superseded` and
            # `import_pending` are positive matches and stand on their own; only
            # the verdict derived purely from silence is suppressed.
            verdict = 'library_unknown'
        else:
            verdict = 'not_in_library'
        return {'parsed': parsed, 'verdict': verdict,
                'match': library_match, 'match_rivals': library_rivals,
                'title': title_hit, 'title_rivals': title_rivals}

    def _library_payload(c):
        parsed = c['parsed']
        if c['match']:
            row, rivals = c['match'], c['match_rivals']
            quality = row.get('file_quality_name') or ''
            payload = {
                'title':        row.get('title') or '',
                'year':         row.get('year'),
                'service':      row.get('service') or '',
                'quality_name': quality,
                'hdr':          row.get('file_hdr') or '',
                'filename':     os.path.basename(row.get('path') or ''),
                'arr_url':      _arr_url(row),
                'quality_cmp':  compare_release_quality(parsed, quality),
                # Addressing for force-import: the arr already holds a file for
                # this title, so replacing it needs the item's own id, not a path.
                'arr_id':        row.get('arr_id'),
                'connection_id': row.get('connection_id'),
            }
        elif c['title']:
            row, rivals = c['title'], c['title_rivals']
            payload = {
                'title':        row.get('title') or '',
                'year':         row.get('year'),
                'service':      row.get('service') or '',
                'quality_name': '',
                'hdr':          '',
                'filename':     '',
                'arr_url':      _arr_url(row),
                'quality_cmp':  'unknown',
                'arr_id':        row.get('arr_id'),
                'connection_id': row.get('connection_id'),
            }
        else:
            return None
        # T2 — the instance a rescan and a force import go to, and every other
        # instance that also holds the title. Two instances holding one title is
        # a legitimate configuration (1080p + 4K), and "your library already has
        # this" is true of both, so the row says so rather than picking silently.
        payload['connection_name'] = _conn_name(row)
        payload['others'] = [{
            'connection_id': r.get('connection_id'),
            'name':          _conn_name(r),
            'quality_name':  r.get('file_quality_name') or '',
            'quality_cmp':   (compare_release_quality(parsed, r['file_quality_name'])
                              if r.get('file_quality_name') else 'unknown'),
        } for r in rivals]
        return payload

    def _dead_registration_item(g):
        videos = [f for f in g['files']
                  if os.path.splitext(f['path'])[1].lower() in _VIDEO_EXTS]
        rep = max(videos or g['files'], key=lambda f: f['size'])
        return {
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
            'verdict_spread': None,
            # Deliberately empty, and the UI renders no Exclude action for these
            # rows (T6). These `paths` are the **healthy carrier's** — a file a
            # working cross-seed is seeding right now. Excluding one hides a live
            # file from the walk while the dead registration it was meant to
            # address is still sitting in the client. Exclusion is not a
            # meaningful answer to this row at all; removing the registration is.
            'exclusion_patterns': [],
            'is_duplicate':   False,
            'parsed':         parse_release_info_for_path(rep['path']),
            'episodes':       None,
            'library':        None,
            'tracker_health': 'unregistered',
            'tracker_msg':    g['stored_msg'],
            'status':             rep.get('status') or '',
            'completion_unknown': bool(rep.get('completion_unknown')),
            # The files are the carrier's, so neither count describes this
            # registration's own torrent.
            'torrent_files':  None,
            'torrent_size':   None,
            'uploaded':       None,
            'ratio':          None,
            'seeding_time':   None,
            'added_on':       None,
            # The evidence the row's copy asserts — the page renders which (T9).
            'alive_library':  g['alive_library'],
            'alive_sibling':  g['alive_sibling'],
        }

    items = []
    for kind, g in listing:
        if kind == 'dead_registration':
            items.append(_dead_registration_item(g))
            continue
        # Largest first, so the representative is the largest video, as it was.
        videos = sorted((f for f in g['files'] if os.path.splitext(f['path'])[1].lower() in _VIDEO_EXTS),
                        key=lambda f: -f['size'])
        rep = videos[0] if videos else max(g['files'], key=lambda f: f['size'])

        # T5 — one verdict per torrent, earned by every video in it rather than
        # by the largest. A season pack is one decision, so rows stay per torrent,
        # but a pack where one episode is superseded and nine are not in the
        # library used to read as superseded-lower — the state that pre-selected
        # a delete of the whole cross-seed group. Every video is judged, the row
        # takes the verdict that deletes least, and a disagreement ships as
        # `verdict_spread`. An imported dead seed's verdict does not depend on the
        # library, so it is judged on its representative as before.
        voters = videos if videos and not g['imported'] else [rep]
        judged = [_classify(f['path']) for f in voters]
        spread = {}
        for c in judged:
            spread[c['verdict']] = spread.get(c['verdict'], 0) + 1
        fallback = min(spread, key=_TRIAGE_LEAST_DESTRUCTIVE.index)
        chosen = next(c for c in judged if c['verdict'] == fallback)
        parsed = chosen['parsed']
        # The episodes this row covers, for the rescan watch (T11): every video's,
        # when each names an episode of the row's season; otherwise the season.
        episodes = None
        if parsed['season'] is not None:
            numbers = {c['parsed']['episode'] for c in judged}
            if None not in numbers and all(c['parsed']['season'] == parsed['season'] for c in judged):
                episodes = sorted(numbers)

        tracker_health = g.get('stored_health') or 'unknown'
        # What this torrent is when the tracker doesn't say 'unregistered' —
        # health is the only live input to classification, so precomputing the
        # verdict under every health outcome lets the client apply the live
        # answer without re-running any of this.
        if g['imported']:
            # 'working' → None: a torrent the tracker answers for again has
            # recovered (re-registered) — the row disappears on live verify.
            alternatives = {'working': None, 'unregistered': 'dead_seed', 'other': 'dead_seed'}
        else:
            alternatives = {'working': fallback, 'unregistered': 'unregistered', 'other': fallback}

        verdict = _triage_verdict_under(alternatives, tracker_health)
        if verdict is None:
            continue

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
            'verdict_spread': spread if len(spread) > 1 else None,
            # Built here, not in the browser — and the safe folder comes off the
            # audit's own stamp, because deciding it needs the whole torrent,
            # every other torrent's paths and the media tree.
            'exclusion_patterns': _triage_exclusion_patterns(
                [f['path'] for f in g['files']], _excl_folder(g['files'])),
            'is_duplicate':   is_duplicate,
            'parsed':         parsed,
            'episodes':       episodes,
            'library':        _library_payload(chosen),
            'tracker_health': tracker_health,
            'tracker_msg':    g['stored_msg'],
            # What the client is doing, and whether the payload is whole. The
            # item carried neither, so the UI could not have told an in-flight
            # download from junk even if it wanted to (T4). Known-incomplete
            # torrents no longer reach this list at all; `completion_unknown`
            # ones do, and say so rather than vanishing.
            'status':             rep.get('status') or '',
            'completion_unknown': bool(rep.get('completion_unknown')),
            # T5 — what a delete touches is the whole torrent, and this row may
            # list a subset (a partly imported torrent, an excluded file). The
            # file count is the audit's (`_stamp_torrent_files`, sparse); the size
            # arrives from /triage/verify, which has the torrent's own row.
            'torrent_files':  max((f.get('torrent_files') or 0) for f in g['files']) or None,
            'torrent_size':   None,
            # Live-only fields — filled in by /triage/verify
            'uploaded':       None,
            'ratio':          None,
            'seeding_time':   None,
            'added_on':       None,
        })

    verdict_order = {'dead_seed': 0, 'dead_registration': 1, 'unregistered': 2,
                     'superseded': 3, 'import_pending': 4, 'library_unknown': 5,
                     'not_in_library': 6}
    items.sort(key=lambda i: (verdict_order.get(i['verdict'], 9), -i['total_size']))

    suggestions = _triage_exclusion_suggestions(items)

    # `counts` is gone (T9): nothing read it, and the badge and the Rounds card
    # read `details.triage_counts` off the audit. `shown` / `total` replace a
    # banner that hardcoded "500" (T8); `truncated` stays for an older bundle.
    return jsonify({
        "status":         "success",
        "items":          items,
        "shown":          len(listing),
        "total":          total,
        "truncated":      total > len(listing),
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


# Pile order on the page: the lossless pile first, where green belongs; the
# irreversible pile second; the one nothing can be done about until a clean scan
# last. CLEANUP Principle 5 — irreversibility outranks yield — decided by the
# user as option (a), 2026-09-13.
_CLEANUP_PILES = ('keeps_copy', 'only_copy', 'unverified')


@app.route('/api/workflows/cleanup')
@require_auth
def workflows_cleanup():
    """Orphaned files, one row per inode, grouped by release folder and split into piles.

    Built entirely from what the audit stamped — **nothing is stat'ed here**
    (C9). The page used to `os.path.getmtime` every orphan on every load, capped
    at 5,000, and above the cap every age silently vanished.

    Each row is an inode (C5): `path` (the first walked), `paths` (every
    torrent-tree path of it — the bytes go only when all of them go), `size`
    once, `state` (`_cleanup_state`) and `mtime` or `None` ("age unavailable" is
    a readout, never a missing chip).

    Each group ships `excl_folder` — the folder a rule may name, stamped by
    `audit._mark_cleanup_folders` — or `None` with `no_folder_rule` saying why:
    `root`, `media_root` (a category dir shared with the library by name),
    `live_torrent` (the folder also holds files a torrent claims — C16),
    `unverified`, or `not_established` (no stamp: a database whose last audit
    predates it). `loose` is kept as `excl_folder is None` for a stale browser
    bundle, which reads only that flag and would otherwise offer a folder rule
    for every group.

    `pile` is the group's most alarming state: any `unverified` file puts it in
    `unverified`, else any `last_copy` in `only_copy`, else `keeps_copy` — a
    group holding both kinds sits in the only-copy pile with per-file states.
    Groups sort by pile, then oldest first; rows oldest first.

    `freeable_size` is an **upper bound**: each last-copy inode counted once.
    What a run actually frees is the script's to report. `excluded_count` counts
    excluded orphan **records** (inodes), not paths.
    """
    records, excluded_count = _cleanup_records()

    folders = {}
    for rec in records:
        paths = _cleanup_paths(rec)
        dir_segs = paths[0].split('/')[:-1]
        top = '/'.join(dir_segs[:2]) if dir_segs else '(root)'
        g = folders.get(top)
        if g is None:
            g = folders[top] = {'folder': top, 'files': [], '_stamps': set(), '_refused': set()}
        g['_stamps'].add(str(rec.get('excl_folder') or ''))
        g['_refused'].add(str(rec.get('excl_refused') or ''))
        mtime = rec.get('mtime')
        g['files'].append({
            'path':  paths[0],
            'paths': paths,
            'size':  rec.get('size') or 0,
            'state': _cleanup_state(rec),
            'mtime': mtime if isinstance(mtime, int) and not isinstance(mtime, bool) else None,
        })

    states = {s: {'count': 0, 'size': 0}
              for s in ('library_copy', 'linked_elsewhere', 'last_copy', 'unverified')}
    groups = []
    for top, g in folders.items():
        stamps, refused = g.pop('_stamps'), g.pop('_refused')
        # Every record of a group carries the same stamp; disagreement means some
        # predate it, which is "not established", never "safe".
        if top == '(root)':
            excl, why = None, 'root'
        elif stamps == {top}:
            excl, why = top, None
        elif len(refused) == 1 and '' not in refused:
            excl, why = None, next(iter(refused))
        else:
            excl, why = None, 'not_established'
        present = {f['state'] for f in g['files']}
        pile = ('unverified' if 'unverified' in present
                else 'only_copy' if 'last_copy' in present else 'keeps_copy')
        for f in g['files']:
            states[f['state']]['count'] += 1
            states[f['state']]['size'] += f['size']
        g['files'].sort(key=lambda f: (f['mtime'] is None, f['mtime'] or 0, f['path']))
        ages = [f['mtime'] for f in g['files'] if f['mtime'] is not None]
        g.update({
            'excl_folder':    excl,
            'no_folder_rule': why,
            'loose':          excl is None,
            'pile':           pile,
            'oldest_mtime':   min(ages) if ages else None,
            'total_size':     sum(f['size'] for f in g['files']),
            'freeable_size':  sum(f['size'] for f in g['files'] if f['state'] == 'last_copy'),
        })
        groups.append(g)
    groups.sort(key=lambda g: (_CLEANUP_PILES.index(g['pile']), g['oldest_mtime'] is None,
                               g['oldest_mtime'] or 0, g['folder']))

    keeps = {'count': states['library_copy']['count'] + states['linked_elsewhere']['count'],
             'size':  states['library_copy']['size'] + states['linked_elsewhere']['size']}
    return jsonify({
        "status":         "success",
        "groups":         groups,
        "file_count":     len(records),
        "path_count":     sum(len(f['paths']) for g in groups for f in g['files']),
        "total_size":     sum(g['total_size'] for g in groups),
        "freeable_size":  sum(g['freeable_size'] for g in groups),
        "keeps_copy":     keeps,
        "only_copy":      states['last_copy'],
        "unverified":     states['unverified'],
        "states":         states,
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
    # Which workflow the grab belongs to. Anything but an explicit 'trump' is a
    # Backfill — that is what every bundle that predates the field sends.
    source        = 'trump' if data.get('source') == 'trump' else 'backfill'
    if not service or not connection_id or arr_id is None:
        return jsonify({'status': 'error', 'message': 'Missing parameters'}), 400

    # Scored here, at the grab, rather than when the watch confirms the import:
    # the watch is a best-effort helper that nurses the download into the
    # library, and tying the prize to its survival meant a container restart or
    # a stalled import erased credit for work the user had already done. See
    # `_record_backfill_credit`. **Backfill only**: a trump pays on Kingmaker,
    # at execute, and sharing this watch must not pay it out on Matchmaker too —
    # each workflow is paid in its own shape (TR9).
    if source == 'backfill':
        _record_backfill_credit(files)
    return jsonify({'job_id': _start_import_watch(db_load_config(), service, connection_id,
                                                  arr_id, title, file_ids, source)})


def _trump_rescan():
    """TR10 — the audit a trump used to start seconds after its delete.

    That scan recorded the one moment nobody wants recorded: the media file
    still there, its torrent-side link gone, so the hardlink ratio — 70 of the
    100 health points — dipped, and the dip landed in `audit_runs`, the health
    chart and the change log. It runs once the replacement has imported and
    re-hardlinked, which is the state worth measuring.
    """
    if try_start_scanning("trump"):
        threading.Thread(target=run_audit_process, args=("trump",), daemon=True).start()


def _start_import_watch(cfg, service, connection_id, arr_id, title, file_ids, source='backfill'):
    """Follow a grab into the library, on a daemon thread. Returns the job id.

    Shared by Backfill (through `/api/workflows/watch_import`) and Trumped
    (from `execute`, TR9), so both land in `_import_watches` and in the same
    bottom-right import panel every page polls. `file_ids` scopes a Sonarr force
    import to those library files' episodes — the files a backfill is for, or
    the ones a trumped release was imported as; with none, a Sonarr watch
    force-imports nothing (B11). A trump watch that confirms its import starts
    the re-audit (`_trump_rescan`); a Backfill one leaves that to the watchdog,
    as it always has.
    """
    arr_name = 'Sonarr' if service == 'sonarr' else 'Radarr'
    what     = 'trump' if source == 'trump' else 'backfill'
    job_id = secrets.token_hex(8)
    watch  = {
        'status':       'queued',
        'message':      'Queued — waiting for download client',
        'title':        title,
        'service':      service,
        'source':       source,
        'completed_at': None,
    }
    _import_watches[job_id] = watch

    def do_watch():
        def mark_done():
            watch['status']       = 'done'
            watch['message']      = 'Imported successfully'
            watch['completed_at'] = time.time()
            if source == 'trump':
                _trump_rescan()

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
                fail(f"Could not tell which episodes this {what} was for, so nothing was "
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
    return job_id


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
