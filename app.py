import os
import json
import time
import sqlite3
import threading
import hashlib
import logging
import secrets
import functools
import fnmatch
import urllib.request
import urllib.error
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
            entry = qbit_file_map.setdefault(full_path, {"status": status, "trackers": set()})
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
                    'torrent_paths': [], 'media_paths': [],
                })
                if source_label == 'Torrent':
                    inode_map[inode]['torrent_paths'].append(full_path)
                    qbit_info = qbit_file_map.get(full_path)
                    if qbit_info:
                        inode_map[inode]['trackers'].update(qbit_info['trackers'])
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
    with _config_lock:
        cfg = dict(config)
    ok, msg = _test_arr_connection(cfg.get('SONARR_URL', ''), cfg.get('SONARR_API_KEY', ''))
    if ok:
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": msg}), 400

@app.route('/api/test_radarr', methods=['POST'])
@require_auth
def test_radarr():
    with _config_lock:
        cfg = dict(config)
    ok, msg = _test_arr_connection(cfg.get('RADARR_URL', ''), cfg.get('RADARR_API_KEY', ''))
    if ok:
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": msg}), 400

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    dist = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
    if path and os.path.exists(os.path.join(dist, path)):
        return send_from_directory(dist, path)
    return send_from_directory(dist, 'index.html')
