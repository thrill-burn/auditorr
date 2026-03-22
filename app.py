import os
import json
import time
import sqlite3
import threading
import hashlib
import logging
import secrets
import functools
import re
import fnmatch
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, g
from flask_cors import CORS
import qbittorrentapi
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

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

DATA_DIR    = os.environ.get('DATA_DIR', '/app/data')
CONFIG_FILE  = os.path.join(DATA_DIR, 'config.json')
RESULTS_FILE = os.path.join(DATA_DIR, 'results.json')
HISTORY_FILE = os.path.join(DATA_DIR, 'history.json')
DB_FILE      = os.path.join(DATA_DIR, 'auditorr.db')

# Ensure data dir exists — called here AND in startup() so both gunicorn workers are covered
def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

_ensure_data_dir()

AUDITORR_PORT   = int(os.environ.get('AUDITORR_PORT', 8677))
AUDITORR_SECRET = os.environ.get('AUDITORR_SECRET', '').strip()

_state_lock  = threading.Lock()
_config_lock = threading.Lock()

audit_state = {
    "is_scanning":      False,
    "progress":         0,
    "last_audit_time":  "Never",
    "total_files":      0,
    "scanned_files":    0,
    "status_message":   "",
    "last_scan_status": "never",   # "ok" | "error" | "never"
    "trigger":          "startup",
    "next_scan_in":     None,
}

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
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    'QB_HOST':            '',
    'QB_USER':            '',
    'QB_PASS':            '',
    'MEDIA_PATH':         '/data/media',
    'REMOTE_PATH':        '/data/torrents',
    'LOCAL_PATH':         '/data/torrents',
    'WATCHDOG_COOLDOWN':  60,
    'SCHEDULED_INTERVAL': 360,
    'OR_RATIO':           0.01,
    'NI_RATIO':           0.01,
    'DUP_RATIO':          0.01,
    'EXCLUSION_PATTERNS': [],
    'SONARR_URL':         '',
    'SONARR_API_KEY':     '',
    'RADARR_URL':         '',
    'RADARR_API_KEY':     '',
    'SONARR_REMOTE_PATH': '',  # Path as Sonarr sees it (inside its container)
    'RADARR_REMOTE_PATH': '',  # Path as Radarr sees it (inside its container)
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return dict(DEFAULT_CONFIG)

def save_config(conf):
    _atomic_write(CONFIG_FILE, conf)

with _config_lock:
    config = load_config()

# ---------------------------------------------------------------------------
# Atomic I/O
# ---------------------------------------------------------------------------

def _atomic_write(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    # Write to a tmp file in the same directory, then rename.
    # On volume mounts os.replace can fail with cross-device errors,
    # so we write directly to the target path as a fallback.
    tmp = filepath + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, filepath)
    except OSError:
        # Cross-device or other filesystem issue — write directly
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        with open(filepath, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())

def _safe_read(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Could not read {filepath}: {e}")
    return default

# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------

def _db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = _db_conn()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS audit_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at        TEXT    NOT NULL,
                trigger       TEXT    NOT NULL,
                health_score  REAL,
                status        TEXT    NOT NULL DEFAULT 'ok',
                error_message TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_run_id  INTEGER NOT NULL REFERENCES audit_runs(id),
                snapshot_json TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_ran_at ON audit_runs(ran_at);
        ''')
        conn.commit()
    finally:
        conn.close()

init_db()

def db_save_audit(trigger, health_score, status, error_message, snapshot):
    conn = _db_conn()
    try:
        cur = conn.execute(
            'INSERT INTO audit_runs (ran_at, trigger, health_score, status, error_message) VALUES (?,?,?,?,?)',
            (datetime.now().isoformat(), trigger, health_score, status, error_message)
        )
        run_id = cur.lastrowid
        conn.execute(
            'INSERT INTO audit_snapshots (audit_run_id, snapshot_json) VALUES (?,?)',
            (run_id, json.dumps(snapshot))
        )
        # Keep only last 10 full snapshots to bound disk usage
        conn.execute('''
            DELETE FROM audit_snapshots WHERE id NOT IN (
                SELECT id FROM audit_snapshots ORDER BY id DESC LIMIT 10
            )
        ''')
        conn.commit()
        return run_id
    finally:
        conn.close()

def db_get_last_two_snapshots():
    conn = _db_conn()
    try:
        rows = conn.execute('''
            SELECT s.snapshot_json, r.ran_at, r.id
            FROM audit_snapshots s
            JOIN audit_runs r ON r.id = s.audit_run_id
            WHERE r.status = 'ok'
            ORDER BY r.ran_at DESC LIMIT 2
        ''').fetchall()
        return [{'snapshot': json.loads(r['snapshot_json']), 'ran_at': r['ran_at'], 'id': r['id']} for r in rows]
    finally:
        conn.close()

def db_get_recent_runs(limit=90):
    conn = _db_conn()
    try:
        rows = conn.execute(
            'SELECT id, ran_at, trigger, health_score, status, error_message FROM audit_runs ORDER BY ran_at DESC LIMIT ?',
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------

def compute_diff(prev_snap, curr_snap):
    if not prev_snap or not curr_snap:
        return None

    def fset(snap, key):
        return {f['path']: f for f in snap.get(key, [])}

    changes = {
        'newly_orphaned':      [],
        'newly_imported':      [],
        'new_duplicates':      [],
        'resolved_duplicates': [],
        'new_files':           [],
        'removed_files':       [],
        'score_delta':         None,
    }

    ps = prev_snap.get('dashboard', {}).get('score')
    cs = curr_snap.get('dashboard', {}).get('score')
    if ps is not None and cs is not None:
        changes['score_delta'] = round(cs - ps, 1)

    for tab, key in [('media', 'media_files'), ('torrents', 'torrent_files')]:
        prev = fset(prev_snap, key)
        curr = fset(curr_snap, key)

        for path, f in curr.items():
            if path not in prev:
                changes['new_files'].append({'path': path, 'size': f['size'], 'tab': tab})
            else:
                pf = prev[path]
                if pf.get('status') != 'Orphaned' and f.get('status') == 'Orphaned':
                    changes['newly_orphaned'].append({'path': path, 'size': f['size'], 'tab': tab})
                if tab == 'torrents' and not pf.get('imported') and f.get('imported'):
                    changes['newly_imported'].append({'path': path, 'size': f['size'], 'tab': tab})
                if not pf.get('duplicate_paths') and f.get('duplicate_paths'):
                    changes['new_duplicates'].append({'path': path, 'size': f['size'], 'tab': tab})
                if pf.get('duplicate_paths') and not f.get('duplicate_paths'):
                    changes['resolved_duplicates'].append({'path': path, 'size': f['size'], 'tab': tab})

        for path in prev:
            if path not in curr:
                changes['removed_files'].append({'path': path, 'tab': tab})

    for k in changes:
        if isinstance(changes[k], list):
            changes[k] = changes[k][:50]

    has_changes = any(isinstance(v, list) and v for v in changes.values())
    return changes if has_changes else None

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_results():
    return _safe_read(RESULTS_FILE, {
        "media_files": [], "torrent_files": [], "trackers": [],
        "status": "No audit run yet.", "dashboard": None,
    })

def save_results(results):
    _atomic_write(RESULTS_FILE, results)

def load_history():
    return _safe_read(HISTORY_FILE, {"hourly_stats": [], "daily_stats": []})

def save_history(hist):
    _atomic_write(HISTORY_FILE, hist)

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def get_state():
    with _state_lock:
        return dict(audit_state)

def set_state(**kwargs):
    with _state_lock:
        audit_state.update(kwargs)

def update_progress(scanned, total):
    with _state_lock:
        audit_state["scanned_files"] = scanned
        audit_state["progress"] = min(100, int((scanned / total) * 100)) if total > 0 else 0

# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

class AuditDebounceHandler(FileSystemEventHandler):
    def __init__(self, cooldown_fn):
        super().__init__()
        self._cooldown_fn = cooldown_fn
        self._timer = None
        self._lock  = threading.Lock()

    def _reset_timer(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            cooldown = self._cooldown_fn()
            set_state(next_scan_in=cooldown)
            self._timer = threading.Timer(cooldown, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        set_state(next_scan_in=None)
        if not get_state()["is_scanning"]:
            log.info("Watchdog: cooldown elapsed, triggering audit.")
            set_state(trigger="watchdog")
            threading.Thread(target=run_audit_process, args=("watchdog",), daemon=True).start()

    def on_created(self, event): self._reset_timer()
    def on_deleted(self, event): self._reset_timer()
    def on_moved(self,   event): self._reset_timer()

_observer = None

def start_watchdog():
    global _observer
    with _config_lock:
        cfg = dict(config)
    paths = {p for p in {cfg.get('LOCAL_PATH',''), cfg.get('MEDIA_PATH','')} if p and os.path.exists(p)}
    if not paths:
        log.warning("Watchdog: no valid paths to watch.")
        return
    def get_cooldown():
        with _config_lock:
            return int(config.get('WATCHDOG_COOLDOWN', 60))
    handler = AuditDebounceHandler(get_cooldown)
    _observer = Observer()
    for path in paths:
        _observer.schedule(handler, path, recursive=True)
        log.info(f"Watchdog: watching {path}")
    _observer.start()

def restart_watchdog():
    global _observer
    if _observer:
        _observer.stop()
        _observer.join()
    start_watchdog()

# ---------------------------------------------------------------------------
# Scheduled fallback audit
# ---------------------------------------------------------------------------

def _scheduled_audit_loop():
    while True:
        time.sleep(60)
        with _config_lock:
            interval_minutes = int(config.get('SCHEDULED_INTERVAL', 360))
        last = get_state().get('last_audit_time', 'Never')
        if last == 'Never':
            continue
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        elapsed = (datetime.now() - last_dt).total_seconds() / 60
        if elapsed >= interval_minutes and not get_state()['is_scanning']:
            log.info(f"Scheduled audit: {elapsed:.0f}m since last run, triggering.")
            set_state(trigger="scheduled")
            threading.Thread(target=run_audit_process, args=("scheduled",), daemon=True).start()

threading.Thread(target=_scheduled_audit_loop, daemon=True).start()

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def count_files(directory):
    count = 0
    if os.path.exists(directory):
        for _, _, files in os.walk(directory):
            count += len(files)
    return count

def get_fast_hash(filepath, size, chunk_size=65536):
    try:
        hasher = hashlib.md5()
        with open(filepath, 'rb') as f:
            if size <= chunk_size * 2:
                hasher.update(f.read())
            else:
                hasher.update(f.read(chunk_size))
                f.seek(-chunk_size, 2)
                hasher.update(f.read(chunk_size))
        return hasher.hexdigest()
    except Exception as e:
        log.warning(f"Hash failed for {filepath}: {e}")
        return None

# ---------------------------------------------------------------------------
# Audit stages
# ---------------------------------------------------------------------------

def _fetch_qbit_file_map(cfg):
    qbt = qbittorrentapi.Client(
        host=cfg.get('QB_HOST'),
        username=cfg.get('QB_USER'),
        password=cfg.get('QB_PASS'),
    )
    qbt.auth_log_in()
    qbit_file_map = {}
    trackers_set  = set()
    remote_path   = cfg.get('REMOTE_PATH', '')
    local_path    = cfg.get('LOCAL_PATH', '')
    for torrent in qbt.torrents_info():
        raw = [t.url for t in qbt.torrents_trackers(torrent_hash=torrent.hash)
               if t.url.startswith('http') or t.url.startswith('udp')]
        hosts = [t.split('/')[2] for t in raw if len(t.split('/')) > 2] or ['Unknown']
        for h in hosts:
            trackers_set.add(h)
        save_path = torrent.save_path
        if remote_path and save_path.startswith(remote_path):
            save_path = save_path.replace(remote_path, local_path, 1)
        if torrent.state in ('uploading', 'stalledUP', 'forcedUP'):
            status = 'Seeding'
        elif torrent.state in ('downloading', 'stalledDL'):
            status = 'Downloading'
        else:
            status = 'Paused'
        for f in qbt.torrents_files(torrent_hash=torrent.hash):
            full_path = os.path.join(save_path, f.name)
            entry = qbit_file_map.setdefault(full_path, {"status": status, "trackers": set(), "hash": torrent.hash})
            entry["trackers"].update(hosts)
            if status == 'Seeding' or entry["status"] == 'Seeding':
                entry["status"] = 'Seeding'
            elif entry["status"] == 'Paused':
                entry["status"] = status
    return qbit_file_map, sorted(trackers_set)


def _is_excluded(rel_path, filename, patterns):
    """Return True if the file matches any exclusion glob pattern."""
    if not patterns:
        return False
    norm = rel_path.replace('\\', '/')
    parts = norm.split('/')
    for pat in patterns:
        if fnmatch.fnmatch(filename, pat):
            return True
        if fnmatch.fnmatch(norm, pat):
            return True
        # Check each parent directory component
        for part in parts[:-1]:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def _walk_directory(base_path, source_label, inode_map, qbit_file_map, scanned_so_far, total_files, exclusion_patterns=None):
    records = []
    scanned = scanned_so_far
    if not os.path.exists(base_path):
        log.warning(f"Path does not exist, skipping: {base_path}")
        return records, scanned
    for root, _, files in os.walk(base_path):
        for filename in files:
            full_path = os.path.join(root, filename)
            try:
                st       = os.stat(full_path)
                inode    = st.st_ino
                size     = st.st_size
                nlink    = st.st_nlink
                rel_path = os.path.relpath(full_path, base_path)
                inode_map.setdefault(inode, {
                    'trackers': set(), 'status': 'Orphaned',
                    'torrent_paths': [], 'media_paths': [], 'hash': '',
                })
                if source_label == 'Torrent':
                    inode_map[inode]['torrent_paths'].append(full_path)
                    qbit_info = qbit_file_map.get(full_path)
                    if qbit_info:
                        inode_map[inode]['trackers'].update(qbit_info['trackers'])
                        inode_map[inode]['hash'] = qbit_info.get('hash', '')
                        cur = inode_map[inode]['status']
                        if qbit_info['status'] == 'Seeding' or cur == 'Seeding':
                            inode_map[inode]['status'] = 'Seeding'
                        elif cur == 'Orphaned':
                            inode_map[inode]['status'] = qbit_info['status']
                else:
                    inode_map[inode]['media_paths'].append(full_path)
                records.append({
                    "full_path": full_path, "rel_path": rel_path,
                    "size": size, "inode": inode, "nlink": nlink, "source": source_label,
                    "excluded": _is_excluded(rel_path, filename, exclusion_patterns),
                })
            except Exception as e:
                log.warning(f"Could not stat {full_path}: {e}")
            scanned += 1
            update_progress(scanned, total_files)
    return records, scanned


def _build_duplicate_map(all_records):
    """O(n) duplicate detection: group by size, then inode, then hash representatives only."""
    size_groups = {}
    for f in all_records:
        if f['size'] > 0:
            size_groups.setdefault(f['size'], []).append(f)

    duplicate_map = {}
    for size, items in size_groups.items():
        inode_to_rep = {}
        for item in items:
            if item['inode'] not in inode_to_rep:
                inode_to_rep[item['inode']] = item
        if len(inode_to_rep) <= 1:
            continue
        hash_to_inodes = {}
        for inode, rep in inode_to_rep.items():
            fh = get_fast_hash(rep['full_path'], size)
            if fh:
                hash_to_inodes.setdefault(fh, []).append(inode)
        for fh, inodes in hash_to_inodes.items():
            if len(inodes) <= 1:
                continue
            for inode in inodes:
                others = [inode_to_rep[o]['full_path'] for o in inodes if o != inode]
                duplicate_map.setdefault(inode, []).extend(others)
    return duplicate_map


def _assemble_records(torrent_records, media_records, inode_map, duplicate_map):
    torrent_files_data = []
    for item in torrent_records:
        inode = item['inode']
        info  = inode_map[inode]
        torrent_files_data.append({
            "path": item['rel_path'], "size": item['size'], "inode": inode,
            "status": info['status'],
            "imported": item['nlink'] > 1 or len(info['media_paths']) > 0,
            "trackers": list(info['trackers']) or ["None"],
            "linked_paths": info['media_paths'],
            "duplicate_paths": duplicate_map.get(inode, []),
            "excluded": item.get('excluded', False),
            "hash": info.get('hash', ''),
        })
    media_files_data = []
    for item in media_records:
        inode = item['inode']
        info  = inode_map[inode]
        media_files_data.append({
            "path": item['rel_path'], "size": item['size'], "inode": inode,
            "status": info['status'], "imported": True,
            "trackers": list(info['trackers']) or ["None"],
            "linked_paths": info['torrent_paths'],
            "duplicate_paths": duplicate_map.get(inode, []),
            "excluded": item.get('excluded', False),
        })
    return torrent_files_data, media_files_data


def run_audit_process(trigger=None):
    with _config_lock:
        cfg = dict(config)
    # Accept trigger as parameter so callers can pass it explicitly,
    # avoiding a race between set_state(trigger=...) and reading it back
    if trigger is None:
        trigger = get_state().get('trigger', 'manual')
    set_state(is_scanning=True, progress=0, scanned_files=0, total_files=0,
              status_message="Connecting to qBittorrent...", last_scan_status="running")
    try:
        qbit_file_map, trackers = _fetch_qbit_file_map(cfg)
        set_state(status_message="Counting files...")
        total = count_files(cfg.get('MEDIA_PATH','')) + count_files(cfg.get('LOCAL_PATH',''))
        set_state(total_files=total, status_message="Scanning torrent directory...")
        inode_map = {}
        exclusion_patterns = cfg.get('EXCLUSION_PATTERNS', [])
        torrent_records, scanned = _walk_directory(
            cfg.get('LOCAL_PATH',''), 'Torrent', inode_map, qbit_file_map, 0, total,
            exclusion_patterns=exclusion_patterns)
        set_state(status_message="Scanning media directory...")
        media_records, _ = _walk_directory(
            cfg.get('MEDIA_PATH',''), 'Media', inode_map, qbit_file_map, scanned, total,
            exclusion_patterns=exclusion_patterns)
        set_state(status_message="Detecting duplicates...")
        duplicate_map = _build_duplicate_map(torrent_records + media_records)
        torrent_files_data, media_files_data = _assemble_records(
            torrent_records, media_records, inode_map, duplicate_map)
        set_state(status_message="Computing health metrics...")
        dashboard_stats = process_health_metrics(media_files_data, torrent_files_data, cfg)
        result = {
            "media_files": media_files_data, "torrent_files": torrent_files_data,
            "trackers": trackers, "status": "ok", "dashboard": dashboard_stats,
        }
        save_results(result)
        snapshot = {"media_files": media_files_data, "torrent_files": torrent_files_data,
                    "dashboard": dashboard_stats}
        db_save_audit(trigger, dashboard_stats['score'], 'ok', None, snapshot)
        log.info("Audit complete.")
        set_state(status_message="Audit complete.", last_scan_status="ok")
    except (qbittorrentapi.LoginFailed, qbittorrentapi.APIConnectionError) as e:
        msg = f"qBittorrent error: {e}"
        log.error(msg); _save_error_status(msg)
        db_save_audit(trigger, None, 'error', msg, {})
        set_state(status_message=msg, last_scan_status="error")
    except Exception as e:
        msg = f"Audit error: {e}"
        log.exception("Unexpected error during audit")
        _save_error_status(msg)
        db_save_audit(trigger, None, 'error', msg, {})
        set_state(status_message=msg, last_scan_status="error")
    finally:
        set_state(progress=100, is_scanning=False,
                  last_audit_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  trigger="idle")


def _save_error_status(message):
    curr = load_results()
    curr["status"] = message
    save_results(curr)

# ---------------------------------------------------------------------------
# Health metrics
# ---------------------------------------------------------------------------

def process_health_metrics(media_files, torrent_files, cfg):
    history = load_history()
    now     = datetime.now()
    or_ratio  = float(cfg.get('OR_RATIO',  0.01))
    ni_ratio  = float(cfg.get('NI_RATIO',  0.01))
    dup_ratio = float(cfg.get('DUP_RATIO', 0.01))
    # Exclude files marked as excluded from all scoring
    scoring_media    = [f for f in media_files    if not f.get('excluded')]
    scoring_torrents = [f for f in torrent_files  if not f.get('excluded')]
    total_media_size      = sum(f['size'] for f in scoring_media)
    hardlinked_media_size = sum(f['size'] for f in scoring_media if f.get('linked_paths'))
    total_torrents_size   = sum(f['size'] for f in scoring_torrents)
    orphaned_torrent_size = sum(f['size'] for f in scoring_torrents if f['status'] == 'Orphaned')
    not_imported_size     = sum(f['size'] for f in scoring_torrents
                                if not f['imported'] and f['status'] != 'Orphaned')
    seen_inodes = set()
    dup_size = dup_count = 0
    for f in scoring_media + scoring_torrents:
        if f.get('duplicate_paths') and f.get('inode') not in seen_inodes:
            seen_inodes.add(f['inode']); dup_size += f['size']; dup_count += 1
    hl_ratio = (hardlinked_media_size / total_media_size) if total_media_size > 0 else 1.0
    hl_score = hl_ratio * 70
    or_limit   = total_torrents_size * or_ratio
    or_penalty = (orphaned_torrent_size / or_limit) * 10 if or_limit > 0 else (10 if orphaned_torrent_size > 0 else 0)
    or_score   = max(0, 10 - or_penalty)
    ni_limit   = total_torrents_size * ni_ratio
    ni_penalty = (not_imported_size / ni_limit) * 10 if ni_limit > 0 else (10 if not_imported_size > 0 else 0)
    ni_score   = max(0, 10 - ni_penalty)
    dup_limit   = total_torrents_size * dup_ratio
    dup_penalty = (dup_size / dup_limit) * 10 if dup_limit > 0 else (10 if dup_size > 0 else 0)
    dup_score   = max(0, 10 - dup_penalty)
    final_score = round(max(0, min(100, hl_score + or_score + ni_score + dup_score)), 1)
    if   final_score >= 90: status_text = "Excellent"
    elif final_score >= 75: status_text = "Good"
    elif final_score >= 50: status_text = "Fair"
    else:                   status_text = "Poor"
    current_stat = {
        "timestamp": now.isoformat(), "health_score": final_score,
        "details": {
            "total_media_size": total_media_size, "hardlinked_media_size": hardlinked_media_size,
            "total_torrents_size": total_torrents_size, "orphaned_torrent_size": orphaned_torrent_size,
            "not_imported_size": not_imported_size, "duplicate_size": dup_size,
            "orphaned_torrent_count": sum(1 for f in scoring_torrents if f['status'] == 'Orphaned'),
            "not_imported_count": sum(1 for f in scoring_torrents if not f['imported'] and f['status'] != 'Orphaned'),
            "duplicate_count": dup_count, "or_limit": or_limit, "ni_limit": ni_limit,
            "dup_limit": dup_limit, "hl_score": round(hl_score,1), "or_score": round(or_score,1),
            "ni_score": round(ni_score,1), "dup_score": round(dup_score,1),
        }
    }
    history['hourly_stats'].append(current_stat)
    cutoff   = now - timedelta(hours=48)
    to_daily = [s for s in history['hourly_stats'] if datetime.fromisoformat(s['timestamp']) < cutoff]
    history['hourly_stats'] = [s for s in history['hourly_stats'] if datetime.fromisoformat(s['timestamp']) >= cutoff]
    daily_groups = {}
    for s in to_daily:
        daily_groups.setdefault(s['timestamp'][:10], []).append(s['health_score'])
    for day, scores in daily_groups.items():
        if not any(d['date'] == day for d in history['daily_stats']):
            history['daily_stats'].append({"date": day, "avg_score": round(sum(scores)/len(scores),1),
                                           "min_score": min(scores), "max_score": max(scores)})
    history['daily_stats'] = history['daily_stats'][-90:]
    save_history(history)
    combined_chart = list(history['daily_stats'])
    recent_groups  = {}
    for s in history['hourly_stats']:
        day_str = s['timestamp'][:10]
        if not any(d['date'] == day_str for d in history['daily_stats']):
            recent_groups.setdefault(day_str, []).append(s['health_score'])
    for day in sorted(recent_groups):
        scores = recent_groups[day]
        combined_chart.append({"date": day, "avg_score": round(sum(scores)/len(scores),1),
                                "min_score": min(scores), "max_score": max(scores)})
    trend = None
    if len(combined_chart) >= 2:
        trend = round(combined_chart[-1]['avg_score'] - combined_chart[-2]['avg_score'], 1)
    return {"score": final_score, "status": status_text, "trend": trend,
            "current": current_stat, "history_chart": combined_chart}

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def startup():
    _ensure_data_dir()
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
            set_state(trigger="startup")
            run_audit_process("startup")
            start_watchdog()
    except ImportError:
        # fcntl not available (Windows) — just run without locking
        log.info("Running startup audit...")
        set_state(trigger="startup")
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
    return jsonify(load_results())

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
    """Delete all audit run history and snapshots from SQLite and reset the history JSON."""
    conn = _db_conn()
    try:
        conn.executescript('DELETE FROM audit_snapshots; DELETE FROM audit_runs;')
        conn.commit()
    finally:
        conn.close()
    save_history({"hourly_stats": [], "daily_stats": []})
    # Also clear the dashboard history chart from results
    curr = load_results()
    if curr.get('dashboard'):
        curr['dashboard']['history_chart'] = []
        curr['dashboard']['trend'] = None
        save_results(curr)
    return jsonify({"status": "success"})

@app.route('/api/start_scan', methods=['POST'])
@require_auth
def start_scan():
    if not get_state()["is_scanning"]:
        set_state(trigger="manual")
        threading.Thread(target=run_audit_process, args=("manual",), daemon=True).start()
    return jsonify({"status": "started"})

@app.route('/api/config', methods=['GET', 'POST'])
@require_auth
def handle_config():
    global config
    if request.method == 'POST':
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data"}), 400
        warnings = []
        for key, label in [('MEDIA_PATH','Media Path'), ('LOCAL_PATH','Local Torrent Path')]:
            p = str(data.get(key, ''))
            if p and not os.path.exists(p):
                warnings.append(f"{label} '{p}' does not exist inside the container")
        try:
            existing = load_config()
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
        save_config(new_conf)
        with _config_lock:
            config = new_conf
        threading.Thread(target=restart_watchdog, daemon=True).start()

        # Recompute health metrics immediately using existing scan results
        # so threshold changes are reflected on the dashboard without a full rescan
        try:
            curr = load_results()
            if curr.get('media_files') and curr.get('torrent_files'):
                new_dashboard = process_health_metrics(
                    curr['media_files'], curr['torrent_files'], new_conf)
                curr['dashboard'] = new_dashboard
                save_results(curr)
        except Exception as e:
            log.warning(f"Could not recompute health metrics after config save: {e}")

        return jsonify({"status": "success", "warnings": warnings})

    with _config_lock:
        safe = dict(config)
    if safe.get('QB_PASS'):
        safe['QB_PASS'] = '__stored__'
    return jsonify(safe)

@app.route('/api/test_connection', methods=['POST'])
@require_auth
def test_connection():
    data = request.json
    try:
        client = qbittorrentapi.Client(
            host=data.get('QB_HOST'), username=data.get('QB_USER'), password=data.get('QB_PASS'))
        client.auth_log_in()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

def _test_arr_connection(url, api_key):
    """Probe an *arr /api/v3/system/status endpoint. Returns (ok, message)."""
    if not url or not api_key:
        return False, "URL and API key are required"
    endpoint = url.rstrip('/') + '/api/v3/system/status'
    try:
        http_req = urllib.request.Request(endpoint, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(http_req, timeout=10) as resp:
            resp.read()
        return True, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, str(e)

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

def _human_size(n):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def _build_dup_groups(torrent_files, local_path):
    """Group torrent files with duplicate_paths into structured groups for the Actions page."""
    groups = []
    seen_inodes = set()
    for f in torrent_files:
        if not f.get('duplicate_paths'):
            continue
        inode = f['inode']
        if inode in seen_inodes:
            continue
        seen_inodes.add(inode)
        torrent_full = os.path.join(local_path, f['path']) if local_path else f['path']
        try:
            torrent_dev = os.stat(torrent_full).st_dev
        except OSError:
            torrent_dev = None
        group_files = [{"path": torrent_full, "size": f['size'], "inode": inode, "canonical": True, "same_fs": True}]
        is_cross_fs = False
        for dup_path in f.get('duplicate_paths', []):
            try:
                same_fs = (torrent_dev is not None and os.stat(dup_path).st_dev == torrent_dev)
            except OSError:
                same_fs = False
            if not same_fs:
                is_cross_fs = True
            group_files.append({"path": dup_path, "size": f['size'], "inode": 0, "canonical": False, "same_fs": same_fs})
        recoverable = 0 if is_cross_fs else f['size'] * len(f.get('duplicate_paths', []))
        groups.append({"files": group_files, "recoverable_size": recoverable, "skipped": is_cross_fs})
    return groups


@app.route('/api/actions/script/<script_type>')
@require_auth
def get_action_script(script_type):
    results = load_results()
    with _config_lock:
        cfg = dict(config)
    torrent_files = results.get('torrent_files', [])
    local_path    = cfg.get('LOCAL_PATH', '')
    now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if script_type == 'orphaned_torrents_delete':
        orphaned = [f for f in torrent_files if f.get('status') == 'Orphaned']
        total_size = sum(f['size'] for f in orphaned)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines = [
            '#!/bin/bash',
            '# auditorr — Orphaned Torrent Cleanup Script',
            f'# Generated: {now_str}',
            '# WARNING: Review carefully before running. This permanently deletes files.',
            f'# {len(orphaned)} files — {_human_size(total_size)} expected to be freed',
            '#',
            '# This script will:',
            '#   1. Record free disk space before deletions',
            '#   2. Delete each orphaned file with progress output',
            '#   3. Record free disk space after deletions',
            '#   4. Compare actual space freed vs expected',
            '',
            'set -euo pipefail',
            '',
            f'TOTAL={len(orphaned)}',
            'DONE=0',
            'ERRORS=0',
            f'EXPECTED_BYTES={total_size}',
            '',
            '# Get free space in bytes on the relevant filesystem',
            'FREE_BEFORE=$(df --output=avail -B1 "$(dirname "$0")" 2>/dev/null | tail -1 || df -k . | awk \'NR==2{print $4*1024}\')',
            '',
            'echo "================================================"',
            f'echo "auditorr Orphaned Torrent Cleanup"',
            f'echo "Files to delete: {len(orphaned)}"',
            f'echo "Expected to free: {_human_size(total_size)}"',
            'echo "================================================"',
            'echo ""',
        ]

        for i, f in enumerate(orphaned):
            full_path = os.path.join(local_path, f['path']) if local_path else f['path']
            filename = os.path.basename(full_path)
            lines += [
                f'# File {i+1}/{len(orphaned)}: {filename} — {_human_size(f["size"])}',
                f'echo "[{i+1}/{len(orphaned)}] Deleting: {filename} ({_human_size(f["size"])})"',
                f'if [ -f "{full_path}" ]; then',
                f'  rm "{full_path}"',
                f'  echo "  ✓ Deleted"',
                '  DONE=$((DONE+1))',
                'else',
                f'  echo "  ⚠ Not found, skipping: {full_path}"',
                '  ERRORS=$((ERRORS+1))',
                'fi',
                '',
            ]

        lines += [
            'echo ""',
            'echo "================================================"',
            'echo "Cleanup complete."',
            'echo "  Deleted:  $DONE / $TOTAL files"',
            'if [ "$ERRORS" -gt 0 ]; then',
            '  echo "  Warnings: $ERRORS file(s) not found (already deleted?)"',
            'fi',
            '',
            '# Measure actual space freed',
            'FREE_AFTER=$(df --output=avail -B1 "$(dirname "$0")" 2>/dev/null | tail -1 || df -k . | awk \'NR==2{print $4*1024}\')',
            'FREED=$((FREE_AFTER - FREE_BEFORE))',
            f'EXPECTED={total_size}',
            '',
            '# Format freed bytes for display',
            'if [ "$FREED" -gt 1073741824 ]; then',
            '  FREED_DISPLAY=$(echo "scale=1; $FREED/1073741824" | bc)GB',
            'elif [ "$FREED" -gt 1048576 ]; then',
            '  FREED_DISPLAY=$(echo "scale=1; $FREED/1048576" | bc)MB',
            'else',
            '  FREED_DISPLAY="${FREED}B"',
            'fi',
            '',
            f'echo "  Expected: {_human_size(total_size)}"',
            'echo "  Actual:   ${FREED_DISPLAY}"',
            '',
            '# Warn if actual differs significantly from expected (>10% variance)',
            'if [ "$FREED" -gt 0 ]; then',
            '  VARIANCE=$(( (FREED - EXPECTED) * 100 / EXPECTED ))',
            '  if [ "${VARIANCE#-}" -gt 10 ]; then',
            '    echo "  ⚠ Note: actual space freed differs from expected by ${VARIANCE}%"',
            '    echo "    This is normal if files were hardlinked (inode still referenced elsewhere)"',
            '  fi',
            'fi',
            'echo "================================================"',
        ]
        script = '\n'.join(lines)

    elif script_type == 'dedupe':
        groups = _build_dup_groups(torrent_files, local_path)
        total_recoverable  = sum(g['recoverable_size'] for g in groups)
        skipped_count      = sum(1 for g in groups if g['skipped'])
        non_skipped_groups = [g for g in groups if not g['skipped']]
        total_non_skipped  = len(non_skipped_groups)
        lines = [
            '#!/bin/bash',
            '# auditorr — Dedupe Script',
            f'# Generated: {now_str}',
            '#',
            '# SUMMARY',
            f'# {len(groups)} duplicate groups found',
            f'# {_human_size(total_recoverable)} recoverable',
            f'# {skipped_count} groups skipped (cross-filesystem — cannot hardlink across mounts)',
            '#',
            '# This script replaces duplicate files with hardlinks.',
            '# All file paths will continue to exist after running.',
            '# All torrents will continue seeding normally.',
            '# Review each group carefully before running.',
            '',
            f'TOTAL={total_non_skipped}',
            'DONE=0',
            'SKIPPED=0',
            'RECLAIMED=0',
            '',
        ]
        group_num = 0
        for g in groups:
            canonical     = next(f for f in g['files'] if f['canonical'])
            non_canonical = [f for f in g['files'] if not f['canonical']]
            filename      = os.path.basename(canonical['path'])
            if g['skipped']:
                lines.append(f'# SKIPPED Group: {filename} — cross-filesystem, cannot hardlink')
                lines.append('')
                continue
            group_num += 1
            canon_path = canonical['path']
            lines.append(f'# Group {group_num}: {filename} — {_human_size(g["recoverable_size"])} recoverable')
            lines.append(f'# Canonical: {canon_path}')
            for nc in non_canonical:
                nc_path    = nc['path']
                size_human = _human_size(nc['size'])
                size_bytes = nc['size']
                lines.append(f'# Duplicate: {nc_path}')
                lines.append(f'echo "[{group_num}/{total_non_skipped}] Verifying {filename}..."')
                lines.append(f"HASH_A=$(md5sum \"{canon_path}\" | cut -d' ' -f1)")
                lines.append(f"HASH_B=$(md5sum \"{nc_path}\" | cut -d' ' -f1)")
                lines.append('if [ "$HASH_A" != "$HASH_B" ]; then')
                lines.append('  echo "  SKIP: Hash mismatch — files differ, skipping this group"')
                lines.append('  SKIPPED=$((SKIPPED+1))')
                lines.append('else')
                lines.append('  echo "  Hash verified. Creating hardlink..."')
                lines.append(f'  ln -f "{canon_path}" "{nc_path}"')
                lines.append(f'  echo "  Done. {size_human} reclaimed."')
                lines.append(f'  RECLAIMED=$((RECLAIMED+{size_bytes}))')
                lines.append('fi')
                lines.append('echo ""')
            lines.append('DONE=$((DONE+1))')
            lines.append('')
        lines.extend([
            'echo "================================"',
            'echo "Dedupe complete."',
            'echo "Groups processed: $DONE / $TOTAL"',
            'echo "Groups skipped (hash mismatch): $SKIPPED"',
            'echo ""',
            "echo \"Run 'df -h' to verify space reclaimed.\"",
        ])
        script = '\n'.join(lines)

    else:
        return jsonify({"status": "error", "message": "Unknown script type"}), 400

    return app.response_class(script, mimetype='text/plain; charset=utf-8')


def _arr_command(base_url, api_key, command_name, path):
    """POST a command to a Sonarr/Radarr instance."""
    endpoint = base_url.rstrip('/') + '/api/v3/command'
    body = json.dumps({"name": command_name, "path": path}).encode()
    http_req = urllib.request.Request(
        endpoint, data=body,
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        method='POST',
    )
    with urllib.request.urlopen(http_req, timeout=10) as resp:
        resp.read()


@app.route('/api/actions/sonarr_rescan', methods=['POST'])
@require_auth
def actions_sonarr_rescan():
    data = request.json or {}
    paths = data.get('paths', [])
    with _config_lock:
        cfg = dict(config)
    url = cfg.get('SONARR_URL', '').strip()
    key = cfg.get('SONARR_API_KEY', '').strip()
    local_path = cfg.get('LOCAL_PATH', '').strip()
    if not url or not key:
        return jsonify({"status": "error", "message": "Sonarr not configured"}), 400
    sonarr_remote = cfg.get('SONARR_REMOTE_PATH', '').strip()
    try:
        for path in paths:
            abs_path = path if os.path.isabs(path) else (os.path.join(local_path, path) if local_path else path)
            if sonarr_remote and local_path and abs_path.startswith(local_path):
                arr_path = abs_path.replace(local_path, sonarr_remote, 1)
            else:
                arr_path = abs_path
            arr_path = os.path.dirname(arr_path)
            _arr_command(url, key, "DownloadedEpisodesScan", arr_path)
        return jsonify({"status": "success", "count": len(paths)})
    except Exception as e:
        log.exception("Error in sonarr_rescan")
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/actions/radarr_rescan', methods=['POST'])
@require_auth
def actions_radarr_rescan():
    data = request.json or {}
    paths = data.get('paths', [])
    with _config_lock:
        cfg = dict(config)
    url = cfg.get('RADARR_URL', '').strip()
    key = cfg.get('RADARR_API_KEY', '').strip()
    local_path = cfg.get('LOCAL_PATH', '').strip()
    if not url or not key:
        return jsonify({"status": "error", "message": "Radarr not configured"}), 400
    radarr_remote = cfg.get('RADARR_REMOTE_PATH', '').strip()
    try:
        for path in paths:
            abs_path = path if os.path.isabs(path) else (os.path.join(local_path, path) if local_path else path)
            if radarr_remote and local_path and abs_path.startswith(local_path):
                arr_path = abs_path.replace(local_path, radarr_remote, 1)
            else:
                arr_path = abs_path
            arr_path = os.path.dirname(arr_path)
            _arr_command(url, key, "DownloadedMoviesScan", arr_path)
        return jsonify({"status": "success", "count": len(paths)})
    except Exception as e:
        log.exception("Error in radarr_rescan")
        return jsonify({"status": "error", "message": str(e)}), 400


def _parse_title_from_filename(filename):
    """Parse a clean title from a media filename for *arr search."""
    name = os.path.splitext(os.path.basename(filename))[0]
    # For TV shows: strip everything from SxxExx onwards
    name = re.split(r'[Ss]\d{1,2}[Ee]\d{1,2}', name)[0]
    # For movies: strip year (4 digits) and everything after
    name = re.split(r'\b(19|20)\d{2}\b', name)[0]
    # Strip quality/format tags and everything after
    name = re.sub(
        r'\b(2160p|1080p|1080i|720p|480p|4K|BluRay|BDRip|BRRip|WEB-DL|WEBRip|HDTV|DVDRip|'
        r'AMZN|DSNP|NF|HULU|HBO|x264|x265|HEVC|HDR|DV|AAC|DDP|DTS|MA|FLAC|REMUX|PROPER|REPACK|INTERNAL)\b.*',
        '', name, flags=re.IGNORECASE,
    )
    # Replace dots, underscores, hyphens with spaces
    name = re.sub(r'[._\-]', ' ', name)
    # Collapse multiple spaces and strip
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def _normalize_title(title):
    """Lowercase and strip punctuation for fuzzy title matching."""
    t = title.lower()
    t = re.sub(r'[^\w\s]', ' ', t)  # replace punctuation with space
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _arr_get(base_url, api_key, path):
    """GET from a *arr instance and return parsed JSON."""
    endpoint = base_url.rstrip('/') + path
    http_req = urllib.request.Request(endpoint, headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(http_req, timeout=10) as resp:
        return json.loads(resp.read())


@app.route('/api/actions/sonarr_search', methods=['POST'])
@require_auth
def actions_sonarr_search():
    data = request.json or {}
    file_path = data.get('path', '')
    with _config_lock:
        cfg = dict(config)
    url = cfg.get('SONARR_URL', '').strip()
    key = cfg.get('SONARR_API_KEY', '').strip()
    if not url or not key:
        return jsonify({"status": "error", "message": "Sonarr not configured"}), 400
    try:
        filename = os.path.basename(file_path)
        title = _parse_title_from_filename(filename)
        parsed_normalized = _normalize_title(title)
        series_list = _arr_get(url, key, '/api/v3/series')
        best = None
        best_score = 0
        for s in series_list:
            candidate = _normalize_title(s.get('title', ''))
            alt = _normalize_title(s.get('cleanTitle', ''))
            if candidate == parsed_normalized or alt == parsed_normalized:
                best = s
                break
            if parsed_normalized in candidate or candidate in parsed_normalized:
                score = len(candidate)
                if score > best_score:
                    best = s
                    best_score = score
        if best is None:
            return jsonify({"status": "error", "message": f"'{title}' not found in Sonarr library. Make sure it is added and monitored in Sonarr first."}), 400
        sonarr_url = url.rstrip('/') + '/series/' + best['titleSlug']
        return jsonify({"status": "success", "url": sonarr_url, "title": best.get('title', title)})
    except urllib.error.HTTPError as e:
        log.exception("HTTP error in sonarr_search")
        return jsonify({"status": "error", "message": f"Sonarr returned HTTP {e.code}: {e.reason}"}), 400
    except Exception as e:
        log.exception("Error in sonarr_search")
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/actions/radarr_search', methods=['POST'])
@require_auth
def actions_radarr_search():
    data = request.json or {}
    file_path = data.get('path', '')
    with _config_lock:
        cfg = dict(config)
    url = cfg.get('RADARR_URL', '').strip()
    key = cfg.get('RADARR_API_KEY', '').strip()
    if not url or not key:
        return jsonify({"status": "error", "message": "Radarr not configured"}), 400
    try:
        filename = os.path.basename(file_path)
        title = _parse_title_from_filename(filename)
        parsed_normalized = _normalize_title(title)
        movie_list = _arr_get(url, key, '/api/v3/movie')
        best = None
        best_score = 0
        for m in movie_list:
            candidate = _normalize_title(m.get('title', ''))
            alt = _normalize_title(m.get('cleanTitle', ''))
            if candidate == parsed_normalized or alt == parsed_normalized:
                best = m
                break
            if parsed_normalized in candidate or candidate in parsed_normalized:
                score = len(candidate)
                if score > best_score:
                    best = m
                    best_score = score
        if best is None:
            return jsonify({"status": "error", "message": f"'{title}' not found in Radarr library. Make sure it is added and monitored in Radarr first."}), 400
        radarr_url = url.rstrip('/') + '/movie/' + best['titleSlug']
        return jsonify({"status": "success", "url": radarr_url, "title": best.get('title', title)})
    except urllib.error.HTTPError as e:
        log.exception("HTTP error in radarr_search")
        return jsonify({"status": "error", "message": f"Radarr returned HTTP {e.code}: {e.reason}"}), 400
    except Exception as e:
        log.exception("Error in radarr_search")
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    dist = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
    if path and os.path.exists(os.path.join(dist, path)):
        return send_from_directory(dist, path)
    return send_from_directory(dist, 'index.html')
