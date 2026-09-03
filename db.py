import os
import json
import zlib
import sqlite3
import logging
import threading
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DATA_DIR = os.environ.get('DATA_DIR', '/app/data')
DB_FILE  = os.path.join(DATA_DIR, 'auditorr.db')

DEFAULT_CONFIG = {
    'TORRENT_SOURCE':     'qbit',   # 'qbit' | 'qui'
    'QB_HOST':            '',
    'QB_USER':            '',
    'QB_PASS':            '',
    'QUI_HOST':           '',
    'QUI_API_KEY':        '',
    # Browser-facing link addresses, for setups reached through a reverse proxy.
    # Blank means "same as the host above" — see link_base() in arr.py. These are
    # never fetched by the server; the *_HOST keys stay the API address so that an
    # auth proxy in front of the UI can't break scanning.
    'QB_EXTERNAL_URL':    '',
    'QUI_EXTERNAL_URL':   '',
    'ALLOW_CLIENT_DELETE': False,  # Workflows may delete torrents+files via the client (opt-in)
    'MEDIA_PATH':         '/data/media',
    'REMOTE_PATH':        '/data/torrents',
    'LOCAL_PATH':         '/data/torrents',
    'WATCHDOG_ENABLED':   True,
    'WATCHDOG_COOLDOWN':  60,
    'SCHEDULED_INTERVAL': 360,
    'OR_RATIO':           0.01,
    'NI_RATIO':           0.01,
    'DUP_RATIO':          0.01,
    # Relative importance of each score component. Normalized to 100 points at
    # scoring time, so any non-negative numbers are valid — the defaults happen
    # to sum to 100 already, which keeps them readable as point values.
    'WEIGHT_HARDLINKED':  70,
    'WEIGHT_ORPHANED':    10,
    'WEIGHT_NOT_IMPORTED': 10,
    'WEIGHT_DUPLICATES':  10,
    'EXCLUSION_PATTERNS':          [],
    'DISC_RIP_EXCLUSION_PRESETS':  [],
    'MEDIA_SERVER_EXCLUSION_PRESETS': [],
    'EXCLUSION_HIDE_FROM_EXPLORER': False,
    'SONARR_URL':         '',
    'SONARR_API_KEY':     '',
    'RADARR_URL':         '',
    'RADARR_API_KEY':     '',
    'SONARR_EXTERNAL_URL': '',  # Browser-facing link address; blank = SONARR_URL
    'RADARR_EXTERNAL_URL': '',  # Browser-facing link address; blank = RADARR_URL
    'SONARR_REMOTE_PATH': '',  # Path as Sonarr sees it (inside its container)
    'RADARR_REMOTE_PATH': '',  # Path as Radarr sees it (inside its container)
    'ARR_CONNECTIONS':    [],  # Optional multi-instance Sonarr/Radarr connections
    'ACQUIRE_DOWNLOAD_FROM': [],  # Indexer names to download from (Workflows)
    'ACQUIRE_SEEDING_ON':    [],  # Indexer names the file must also be on (Workflows)
}

# Ordered to match the four dashboard cards (and the config donut's segments).
SCORE_WEIGHT_KEYS = ('WEIGHT_HARDLINKED', 'WEIGHT_ORPHANED',
                     'WEIGHT_NOT_IMPORTED', 'WEIGHT_DUPLICATES')

# The browser-facing link addresses, paired with the API address each falls back
# to when blank. Order is the order they appear in the Config UI.
EXTERNAL_URL_KEYS = (
    ('QB_EXTERNAL_URL',     'QB_HOST',     'qBittorrent External URL'),
    ('QUI_EXTERNAL_URL',    'QUI_HOST',    'qui External URL'),
    ('SONARR_EXTERNAL_URL', 'SONARR_URL',  'Sonarr External URL'),
    ('RADARR_EXTERNAL_URL', 'RADARR_URL',  'Radarr External URL'),
)

# The API addresses. Checked on save too, but only as a warning — see
# url_problem() for why an existing install must never be locked out of Config.
API_URL_KEYS = (
    ('QB_HOST',    'qBittorrent Host URL'),
    ('QUI_HOST',   'qui Host URL'),
    ('SONARR_URL', 'Sonarr URL'),
    ('RADARR_URL', 'Radarr URL'),
)


def url_problem(label, value, max_len=300):
    """Describe what's wrong with a configured URL, or None if it's usable.

    http/https only. An API address with any other scheme can't be fetched, and
    a *link* address with one (javascript:, data:) would execute in the page
    instead of navigating. A missing scheme is rejected as well: "media.example
    .com" renders as a *relative* link into auditorr, so it fails silently and
    looks like the feature is broken rather than the value.

    Callers decide the severity. New keys treat this as an error — nothing is
    stored there yet, so no install can be locked out. The pre-existing API keys
    treat it as a warning: an install that has held an unschemed URL for months
    must still be able to save unrelated settings.
    """
    text = str(value or '').strip()
    if not text:
        return None
    if len(text) > max_len:
        return f"{label} is too long"
    if not text.lower().startswith(('http://', 'https://')):
        return f"{label} must start with http:// or https://"
    return None


def score_weight_points(cfg):
    """Resolve the four score weights into points that sum to 100.

    Weights are relative: only their ratio matters, so users can type whatever
    numbers express their priorities and never see an invalid total. A category
    weighted 0 earns 0 points and is reported as unscored rather than failed.
    """
    raw = []
    for key in SCORE_WEIGHT_KEYS:
        try:
            val = float(cfg.get(key, DEFAULT_CONFIG[key]))
        except (ValueError, TypeError):
            val = float(DEFAULT_CONFIG[key])
        raw.append(max(0.0, val))
    total = sum(raw)
    if total <= 0:
        # Guarded at save time, but config can also arrive from an older DB row
        # or a hand-edited file — fall back rather than divide by zero.
        raw   = [float(DEFAULT_CONFIG[k]) for k in SCORE_WEIGHT_KEYS]
        total = sum(raw)
    return {key: (val / total) * 100 for key, val in zip(SCORE_WEIGHT_KEYS, raw)}


def _db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    # WAL mode allows concurrent reads during writes — better for Flask threads
    # reading results while the audit thread writes them.
    conn.execute("PRAGMA journal_mode=WAL")
    # Enforce declared foreign key constraints (SQLite ignores them by default).
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
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
            CREATE TABLE IF NOT EXISTS latest_results (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                results_json TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                hourly_stats TEXT    NOT NULL DEFAULT '[]',
                daily_stats  TEXT    NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS config (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                config_json TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS upload_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                taken_at    TEXT    NOT NULL,
                snapshot    TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_upload_taken_at ON upload_snapshots(taken_at);
            CREATE TABLE IF NOT EXISTS change_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at       TEXT NOT NULL,
                health_score REAL,
                trigger      TEXT,
                source       TEXT NOT NULL DEFAULT 'qbit',
                diff_json    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_change_log_ran_at ON change_log(ran_at);
            CREATE TABLE IF NOT EXISTS file_results (
                tab        TEXT PRIMARY KEY,
                files_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        ''')
        conn.commit()
        # Migrations: add columns that didn't exist in earlier schema versions
        for migration in (
            "ALTER TABLE upload_snapshots ADD COLUMN source TEXT NOT NULL DEFAULT 'qbit'",
            "ALTER TABLE audit_runs ADD COLUMN source TEXT NOT NULL DEFAULT 'qbit'",
            "ALTER TABLE audit_runs ADD COLUMN duration_seconds REAL",
            "ALTER TABLE audit_runs ADD COLUMN peak_rss_mb INTEGER",
        ):
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        # Strip full file lists from any existing audit_snapshots rows — they were previously
        # stored there for diff computation but are now handled via file_results + change_log.
        # json_remove operates on the raw SQLite blob without Python deserializing it.
        try:
            conn.execute(
                "UPDATE audit_snapshots SET snapshot_json = "
                "json_remove(snapshot_json, '$.media_files', '$.torrent_files') "
                "WHERE json_type(snapshot_json, '$.media_files') IS NOT NULL "
                "   OR json_type(snapshot_json, '$.torrent_files') IS NOT NULL"
            )
            conn.commit()
        except Exception as e:
            log.warning(f"Could not strip file lists from audit_snapshots: {e}")
    finally:
        conn.close()
    _migrate_json_files()


def _migrate_json_files():
    """Migrate legacy JSON files to SQLite on first run after upgrade from v1.1.

    Safe to call from multiple workers simultaneously: FileNotFoundError on open
    means another worker already completed the migration, so we skip silently.
    All other errors are logged as warnings so real problems remain visible.
    """
    results_file = os.path.join(DATA_DIR, 'results.json')
    history_file = os.path.join(DATA_DIR, 'history.json')
    config_file  = os.path.join(DATA_DIR, 'config.json')

    for filepath, migrate_fn, label in [
        (results_file, lambda d: db_save_results(d),                          'results.json'),
        (history_file, lambda d: db_save_history(d),                          'history.json'),
        (config_file,  lambda d: db_save_config({**DEFAULT_CONFIG, **d}),     'config.json'),
    ]:
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            migrate_fn(data)
            try:
                os.remove(filepath)
            except FileNotFoundError:
                pass  # Another worker deleted it first — migration already done
            log.info(f"Migrated {label} to SQLite and removed the file.")
        except FileNotFoundError:
            pass  # File never existed or already migrated — nothing to do
        except Exception as e:
            log.warning(f"Could not migrate {label}: {e}")


# ---------------------------------------------------------------------------
# Audit runs + snapshots
# ---------------------------------------------------------------------------

def db_save_audit(trigger, health_score, status, error_message, snapshot, source='qbit', duration_seconds=None, ran_at=None, peak_rss_mb=None):
    if ran_at is None:
        ran_at = datetime.now().isoformat()
    conn = _db_conn()
    try:
        cur = conn.execute(
            'INSERT INTO audit_runs (ran_at, trigger, health_score, status, error_message, source, duration_seconds, peak_rss_mb) VALUES (?,?,?,?,?,?,?,?)',
            (ran_at, trigger, health_score, status, error_message, source, duration_seconds, peak_rss_mb)
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


def db_get_recent_runs(limit=None):
    conn = _db_conn()
    try:
        if limit is not None:
            rows = conn.execute(
                'SELECT id, ran_at, trigger, health_score, status, error_message, source, duration_seconds, peak_rss_mb FROM audit_runs ORDER BY ran_at DESC LIMIT ?',
                (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT id, ran_at, trigger, health_score, status, error_message, source, duration_seconds, peak_rss_mb FROM audit_runs ORDER BY ran_at DESC'
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def db_clear_audit_history():
    conn = _db_conn()
    try:
        # executescript() issues an implicit COMMIT before running, so no
        # explicit conn.commit() is needed afterwards.
        conn.executescript('DELETE FROM audit_snapshots; DELETE FROM audit_runs;')
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Latest results
# ---------------------------------------------------------------------------

def db_save_results(results):
    # Strip file lists — they are stored separately in file_results to keep
    # this row small so every db_load_results call deserializes only summary data.
    summary = {k: v for k, v in results.items() if k not in ('media_files', 'torrent_files')}
    conn = _db_conn()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO latest_results (id, results_json) VALUES (1, ?)',
            (json.dumps(summary),)
        )
        conn.commit()
    finally:
        conn.close()


def db_load_results():
    conn = _db_conn()
    try:
        row = conn.execute('SELECT results_json FROM latest_results WHERE id = 1').fetchone()
        if row:
            data = json.loads(row['results_json'])
            # Migrate any file lists present in old-format rows into file_results,
            # then rewrite the row without them so future reads stay fast.
            needs_rewrite = False
            for tab_key, db_tab in [('media_files', 'media'), ('torrent_files', 'torrents')]:
                if tab_key in data:
                    files = data.pop(tab_key)
                    needs_rewrite = True
                    existing = conn.execute(
                        'SELECT 1 FROM file_results WHERE tab = ?', (db_tab,)
                    ).fetchone()
                    if not existing:
                        compressed = zlib.compress(json.dumps(files).encode(), level=1)
                        conn.execute(
                            'INSERT INTO file_results (tab, files_json) VALUES (?, ?)',
                            (db_tab, compressed)
                        )
            if needs_rewrite:
                conn.execute(
                    'UPDATE latest_results SET results_json = ? WHERE id = 1',
                    (json.dumps(data),)
                )
                conn.commit()
            return data
        return {"trackers": [], "status": "No audit run yet.", "dashboard": None}
    finally:
        conn.close()


def db_save_file_results(tab, files):
    # Stream JSON through zlib rather than materializing the full string + bytes
    # simultaneously. For 650K-file libraries this avoids a ~1 GB peak where
    # json.dumps() string, .encode() bytes, and compressed output all coexist.
    cobj = zlib.compressobj(level=1)
    chunks = [cobj.compress(chunk.encode('utf-8', errors='replace'))
              for chunk in json.JSONEncoder().iterencode(files)]
    chunks.append(cobj.flush())
    compressed = b''.join(chunks)
    conn = _db_conn()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO file_results (tab, files_json) VALUES (?, ?)',
            (tab, compressed)
        )
        # Cheap stats so other code paths (config-save recompute guard, debug
        # report) can learn the library size without deserializing the blob.
        conn.execute(
            'INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)',
            (f'file_results_{tab}_stats', json.dumps({
                'count':            len(files),
                'compressed_bytes': len(compressed),
                'saved_at':         datetime.now().isoformat(),
            }))
        )
        conn.commit()
    finally:
        conn.close()


def db_load_file_results(tab):
    conn = _db_conn()
    try:
        row = conn.execute('SELECT files_json FROM file_results WHERE tab = ?', (tab,)).fetchone()
        if not row:
            return []
        data = row['files_json']
        # Decompress if stored as bytes (compressed), fall back to plain JSON for
        # rows written by older versions of the app.
        if isinstance(data, (bytes, bytearray)):
            return json.loads(zlib.decompress(data).decode())
        return json.loads(data)
    finally:
        conn.close()


def db_has_file_results(tab):
    """Whether a stored row exists for this tab — distinct from an empty list.

    Lets callers with a legacy fallback (e.g. the 'triage' subset, absent
    until the first post-upgrade audit) tell "no row yet" apart from a
    legitimately empty result.
    """
    conn = _db_conn()
    try:
        return conn.execute(
            'SELECT 1 FROM file_results WHERE tab = ?', (tab,)
        ).fetchone() is not None
    finally:
        conn.close()


def db_stream_file_results(tab, chunk_size=1 << 20):
    """Yield the stored file list as raw JSON byte chunks, without parsing it.

    For very large libraries the decoded JSON is ~1 GB and the parsed object
    graph several times that; serving the stored JSON straight through keeps the
    peak at roughly the compressed blob size plus one chunk.
    """
    conn = _db_conn()
    try:
        row = conn.execute('SELECT files_json FROM file_results WHERE tab = ?', (tab,)).fetchone()
    finally:
        conn.close()
    if not row:
        yield b'[]'
        return
    data = row['files_json']
    if isinstance(data, (bytes, bytearray)):
        dobj = zlib.decompressobj()
        for i in range(0, len(data), chunk_size):
            yield dobj.decompress(data[i:i + chunk_size])
        yield dobj.flush()
    else:
        yield data.encode('utf-8', errors='replace')


def db_save_file_signatures(tab, sigs):
    """Persist the compact per-file diff signature map ({path: bitmask}).

    Stored separately from the full file lists so the next audit can diff
    against the previous scan without deserializing two multi-GB record lists.
    """
    compressed = zlib.compress(json.dumps(sigs).encode('utf-8', errors='replace'), 1)
    conn = _db_conn()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO file_results (tab, files_json) VALUES (?, ?)',
            (f'{tab}_sigs', compressed)
        )
        conn.commit()
    finally:
        conn.close()


def db_load_file_signatures(tab):
    conn = _db_conn()
    try:
        row = conn.execute('SELECT files_json FROM file_results WHERE tab = ?', (f'{tab}_sigs',)).fetchone()
        if not row:
            return {}
        data = row['files_json']
        if isinstance(data, (bytes, bytearray)):
            return json.loads(zlib.decompress(data).decode())
        return json.loads(data)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# App meta (small key/value rows: scan markers, counters, stats)
# ---------------------------------------------------------------------------

def db_get_meta(key, default=None):
    conn = _db_conn()
    try:
        row = conn.execute('SELECT value FROM app_meta WHERE key = ?', (key,)).fetchone()
        return json.loads(row['value']) if row else default
    finally:
        conn.close()


def db_set_meta(key, value):
    conn = _db_conn()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)',
            (key, json.dumps(value))
        )
        conn.commit()
    finally:
        conn.close()


def db_delete_meta(key):
    conn = _db_conn()
    try:
        conn.execute('DELETE FROM app_meta WHERE key = ?', (key,))
        conn.commit()
    finally:
        conn.close()


# Serializes read-modify-write on a single app_meta row. One process, several
# threads: the audit advances `ns_progress` once per scan while the workflow
# endpoints credit trumps and backfills from their own request threads.
_meta_lock = threading.Lock()


def db_update_meta(key, fn, default=None):
    """Read-modify-write one app_meta row atomically. Returns the stored value.

    `fn` receives the current value and returns the new one. The lock matters
    because `ns_progress` is advanced from two directions — the audit, once per
    scan, and the event endpoints (a trump swap, a confirmed backfill) on their
    own threads — so a plain get/modify/set pair drops whichever write lost the
    race, silently and permanently: these counters are cumulative, so a lost
    increment is never recovered by the next one.
    """
    with _meta_lock:
        value = fn(db_get_meta(key, default))
        db_set_meta(key, value)
        return value


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def db_save_history(hist):
    conn = _db_conn()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO history (id, hourly_stats, daily_stats) VALUES (1, ?, ?)',
            (json.dumps(hist.get('hourly_stats', [])), json.dumps(hist.get('daily_stats', [])))
        )
        conn.commit()
    finally:
        conn.close()


def db_load_history():
    conn = _db_conn()
    try:
        row = conn.execute('SELECT hourly_stats, daily_stats FROM history WHERE id = 1').fetchone()
        if row:
            return {
                'hourly_stats': json.loads(row['hourly_stats']),
                'daily_stats':  json.loads(row['daily_stats']),
            }
        return {"hourly_stats": [], "daily_stats": []}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def db_load_config():
    conn = _db_conn()
    try:
        row = conn.execute('SELECT config_json FROM config WHERE id = 1').fetchone()
        if row:
            return {**DEFAULT_CONFIG, **json.loads(row['config_json'])}
        return dict(DEFAULT_CONFIG)
    finally:
        conn.close()


def db_save_config(conf):
    conn = _db_conn()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO config (id, config_json) VALUES (1, ?)',
            (json.dumps(conf),)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def validate_config(data):
    """Validate config POST data. Returns a list of error strings (empty = valid)."""
    errors = []

    ts = data.get('TORRENT_SOURCE')
    if ts is not None and ts not in ('qbit', 'qui'):
        errors.append("TORRENT_SOURCE must be 'qbit' or 'qui'")

    wd = data.get('WATCHDOG_COOLDOWN')
    if wd is not None:
        try:
            if int(wd) < 10:
                errors.append("WATCHDOG_COOLDOWN must be at least 10")
        except (ValueError, TypeError):
            errors.append("WATCHDOG_COOLDOWN must be an integer")

    si = data.get('SCHEDULED_INTERVAL')
    if si is not None:
        try:
            if int(si) < 10:
                errors.append("SCHEDULED_INTERVAL must be at least 10")
        except (ValueError, TypeError):
            errors.append("SCHEDULED_INTERVAL must be an integer")

    for key in ('OR_RATIO', 'NI_RATIO', 'DUP_RATIO'):
        val = data.get(key)
        if val is not None:
            try:
                fval = float(val)
                if not (0.001 <= fval <= 1.0):
                    errors.append(f"{key} must be between 0.001 and 1.0")
            except (ValueError, TypeError):
                errors.append(f"{key} must be a number")

    # Score weights are relative, so only the ratio between them matters — any
    # non-negative number is valid. The one illegal state is all four at zero,
    # which would leave nothing to score. Missing keys fall back to their
    # non-zero defaults on save, so that state needs all four sent as zero.
    supplied_weights = []
    for key in SCORE_WEIGHT_KEYS:
        val = data.get(key)
        if val is None:
            continue
        try:
            fval = float(val)
        except (ValueError, TypeError):
            errors.append(f"{key} must be a number")
            continue
        if fval < 0:
            errors.append(f"{key} must not be negative")
        elif fval > 1000:
            errors.append(f"{key} must not exceed 1000")
        else:
            supplied_weights.append(fval)
    if len(supplied_weights) == len(SCORE_WEIGHT_KEYS) and sum(supplied_weights) <= 0:
        errors.append("At least one score category must have a weight above zero")

    qb_host = data.get('QB_HOST')
    if qb_host is not None and str(qb_host) and not str(qb_host).strip():
        errors.append("QB_HOST must not be blank whitespace (use empty string to leave unconfigured)")

    patterns = data.get('EXCLUSION_PATTERNS')
    if patterns is not None:
        if not isinstance(patterns, list):
            errors.append("EXCLUSION_PATTERNS must be a list")
        else:
            if len(patterns) > 100:
                errors.append("EXCLUSION_PATTERNS must not exceed 100 patterns")
            for i, p in enumerate(patterns):
                if not isinstance(p, str):
                    errors.append(f"EXCLUSION_PATTERNS[{i}] must be a string")
                elif len(p) > 200:
                    errors.append(f"EXCLUSION_PATTERNS[{i}] must not exceed 200 characters")

    presets = data.get('MEDIA_SERVER_EXCLUSION_PRESETS')
    if presets is not None:
        allowed_presets = {'plex', 'jellyfin', 'emby', 'kodi', 'ums'}
        if not isinstance(presets, list):
            errors.append("MEDIA_SERVER_EXCLUSION_PRESETS must be a list")
        elif len(presets) > len(allowed_presets):
            errors.append("MEDIA_SERVER_EXCLUSION_PRESETS contains too many entries")
        else:
            for i, preset in enumerate(presets):
                if str(preset).lower() not in allowed_presets:
                    errors.append(f"MEDIA_SERVER_EXCLUSION_PRESETS[{i}] must be one of plex, jellyfin, emby, kodi, ums")

    disc_presets = data.get('DISC_RIP_EXCLUSION_PRESETS')
    if disc_presets is not None:
        allowed_disc_presets = {'bluray', 'dvd'}
        if not isinstance(disc_presets, list):
            errors.append("DISC_RIP_EXCLUSION_PRESETS must be a list")
        elif len(disc_presets) > len(allowed_disc_presets):
            errors.append("DISC_RIP_EXCLUSION_PRESETS contains too many entries")
        else:
            for i, preset in enumerate(disc_presets):
                if str(preset).lower() not in allowed_disc_presets:
                    errors.append(f"DISC_RIP_EXCLUSION_PRESETS[{i}] must be one of bluray, dvd")

    # Hard error: these keys are new, so nothing can already be stored in one and
    # no existing install can be locked out of its own settings page by this.
    for key, _api_key, label in EXTERNAL_URL_KEYS:
        if key in data:
            problem = url_problem(label, data.get(key))
            if problem:
                errors.append(problem)

    arr_connections = data.get('ARR_CONNECTIONS')
    if arr_connections is not None:
        if not isinstance(arr_connections, list):
            errors.append("ARR_CONNECTIONS must be a list")
        elif len(arr_connections) > 50:
            errors.append("ARR_CONNECTIONS must not exceed 50 connections")
        else:
            seen_ids = set()
            for i, conn in enumerate(arr_connections):
                if not isinstance(conn, dict):
                    errors.append(f"ARR_CONNECTIONS[{i}] must be an object")
                    continue
                service = str(conn.get('service', '')).lower()
                if service not in ('sonarr', 'radarr'):
                    errors.append(f"ARR_CONNECTIONS[{i}].service must be 'sonarr' or 'radarr'")
                conn_id = str(conn.get('id', '')).strip()
                if not conn_id:
                    errors.append(f"ARR_CONNECTIONS[{i}].id is required")
                elif conn_id in seen_ids:
                    errors.append(f"ARR_CONNECTIONS[{i}].id must be unique")
                seen_ids.add(conn_id)
                if conn.get('base_url') and len(str(conn.get('base_url'))) > 300:
                    errors.append(f"ARR_CONNECTIONS[{i}].base_url is too long")
                if conn.get('url') and len(str(conn.get('url'))) > 300:
                    errors.append(f"ARR_CONNECTIONS[{i}].url is too long")
                # _merge_arr_connection_secrets copies every incoming key
                # verbatim, so a field with no validator here is stored entirely
                # unchecked. This one goes straight into a browser address bar.
                ext_problem = url_problem(
                    f"ARR_CONNECTIONS[{i}].external_url", conn.get('external_url'))
                if ext_problem:
                    errors.append(ext_problem)
                if conn.get('media_path') and len(str(conn.get('media_path'))) > 300:
                    errors.append(f"ARR_CONNECTIONS[{i}].media_path is too long")
                if conn.get('local_media_path') and len(str(conn.get('local_media_path'))) > 300:
                    errors.append(f"ARR_CONNECTIONS[{i}].local_media_path is too long")

    return errors


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------

def db_save_change_log_entry(ran_at, health_score, trigger, source, diff):
    conn = _db_conn()
    try:
        conn.execute(
            'INSERT INTO change_log (ran_at, health_score, trigger, source, diff_json) VALUES (?,?,?,?,?)',
            (ran_at, health_score, trigger, source, json.dumps(diff))
        )
        conn.commit()
    finally:
        conn.close()


def db_get_change_log(limit=500):
    conn = _db_conn()
    try:
        rows = conn.execute(
            '''SELECT cl.id, cl.ran_at, cl.health_score, cl.trigger, cl.source, cl.diff_json, ar.duration_seconds
               FROM change_log cl
               LEFT JOIN audit_runs ar ON ar.ran_at = cl.ran_at
               ORDER BY cl.ran_at DESC LIMIT ?''',
            (limit,)
        ).fetchall()
        return [
            {**{k: row[k] for k in ('id', 'ran_at', 'health_score', 'trigger', 'source')},
             'duration_seconds': row['duration_seconds'],
             'diff': json.loads(row['diff_json'])}
            for row in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Upload snapshots
# ---------------------------------------------------------------------------

def db_save_upload_snapshot(snapshot_dict, source='qbit'):
    conn = _db_conn()
    try:
        conn.execute(
            'INSERT INTO upload_snapshots (taken_at, snapshot, source) VALUES (?, ?, ?)',
            (datetime.now().isoformat(), json.dumps(snapshot_dict), source)
        )
        # Keep at most 1000 rows — delete oldest beyond that
        conn.execute('''
            DELETE FROM upload_snapshots WHERE id NOT IN (
                SELECT id FROM upload_snapshots ORDER BY id DESC LIMIT 1000
            )
        ''')
        conn.commit()
    finally:
        conn.close()


def db_get_upload_snapshots(since_days=90, from_date=None, to_date=None):
    conn = _db_conn()
    try:
        if from_date or to_date:
            conditions, params = [], []
            if from_date:
                conditions.append('taken_at >= ?')
                params.append(from_date)
            if to_date:
                to_ceil = to_date if len(to_date) > 10 else to_date + 'T23:59:59.999999'
                conditions.append('taken_at <= ?')
                params.append(to_ceil)
            where = ' AND '.join(conditions)
            rows = conn.execute(
                f'SELECT taken_at, snapshot, source FROM upload_snapshots WHERE {where} ORDER BY taken_at ASC',
                params
            ).fetchall()
        elif since_days == 0:
            rows = conn.execute(
                'SELECT taken_at, snapshot, source FROM upload_snapshots ORDER BY taken_at ASC'
            ).fetchall()
        else:
            cutoff = (datetime.now() - timedelta(days=since_days)).isoformat()
            rows = conn.execute(
                'SELECT taken_at, snapshot, source FROM upload_snapshots WHERE taken_at >= ? ORDER BY taken_at ASC',
                (cutoff,)
            ).fetchall()
        return [{'taken_at': r['taken_at'], 'snapshot': json.loads(r['snapshot']), 'source': r['source']} for r in rows]
    finally:
        conn.close()


def db_delete_upload_snapshots(from_date_str, to_date_str=None):
    """Delete upload_snapshots (and audit_runs) within a datetime range.

    Either bound may be None/'' for open-ended queries.
    Returns (0,0) if both bounds are empty (safety guard).
    """
    conn = _db_conn()
    try:
        to_ceil = None
        if to_date_str:
            to_ceil = to_date_str if len(to_date_str) > 16 else to_date_str + ':59.999999'

        if from_date_str and to_ceil:
            c1 = conn.execute(
                "DELETE FROM upload_snapshots WHERE taken_at >= ? AND taken_at <= ?",
                (from_date_str, to_ceil)
            )
            c2 = conn.execute(
                "DELETE FROM audit_runs WHERE ran_at >= ? AND ran_at <= ?",
                (from_date_str, to_ceil)
            )
        elif from_date_str:
            c1 = conn.execute(
                "DELETE FROM upload_snapshots WHERE taken_at >= ?",
                (from_date_str,)
            )
            c2 = conn.execute(
                "DELETE FROM audit_runs WHERE ran_at >= ?",
                (from_date_str,)
            )
        elif to_ceil:
            c1 = conn.execute(
                "DELETE FROM upload_snapshots WHERE taken_at <= ?",
                (to_ceil,)
            )
            c2 = conn.execute(
                "DELETE FROM audit_runs WHERE ran_at <= ?",
                (to_ceil,)
            )
        else:
            return 0, 0
        conn.commit()
        return c1.rowcount, c2.rowcount
    finally:
        conn.close()


def db_retag_upload_snapshots(from_date_str, source, to_date_str=None):
    """Set source on upload_snapshots AND audit_runs within a datetime range.

    Either bound may be None/'' for open-ended queries (e.g. "everything up to to_date").
    to_date_str is inclusive to the end of the specified minute — appends :59.999999
    for HH:MM-precision values.
    Returns (upload_snapshots_updated, audit_runs_updated). Returns (0,0) if both bounds empty.
    """
    conn = _db_conn()
    try:
        to_ceil = None
        if to_date_str:
            to_ceil = to_date_str if len(to_date_str) > 16 else to_date_str + ':59.999999'

        if from_date_str and to_ceil:
            c1 = conn.execute(
                "UPDATE upload_snapshots SET source = ? WHERE taken_at >= ? AND taken_at <= ?",
                (source, from_date_str, to_ceil)
            )
            c2 = conn.execute(
                "UPDATE audit_runs SET source = ? WHERE ran_at >= ? AND ran_at <= ?",
                (source, from_date_str, to_ceil)
            )
        elif from_date_str:
            c1 = conn.execute(
                "UPDATE upload_snapshots SET source = ? WHERE taken_at >= ?",
                (source, from_date_str)
            )
            c2 = conn.execute(
                "UPDATE audit_runs SET source = ? WHERE ran_at >= ?",
                (source, from_date_str)
            )
        elif to_ceil:
            c1 = conn.execute(
                "UPDATE upload_snapshots SET source = ? WHERE taken_at <= ?",
                (source, to_ceil)
            )
            c2 = conn.execute(
                "UPDATE audit_runs SET source = ? WHERE ran_at <= ?",
                (source, to_ceil)
            )
        else:
            return 0, 0
        conn.commit()
        return c1.rowcount, c2.rowcount
    finally:
        conn.close()


def db_count_upload_snapshots_by_source():
    """Return {source: count} for all upload snapshots."""
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM upload_snapshots GROUP BY source"
        ).fetchall()
        return {r['source']: r['cnt'] for r in rows}
    finally:
        conn.close()


def db_get_latest_upload_snapshot():
    conn = _db_conn()
    try:
        row = conn.execute(
            'SELECT snapshot FROM upload_snapshots ORDER BY taken_at DESC LIMIT 1'
        ).fetchone()
        return json.loads(row['snapshot']) if row else None
    finally:
        conn.close()
