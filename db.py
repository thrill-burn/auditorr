import os
import json
import sqlite3
import logging
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
        ''')
        conn.commit()
        # Migrations: add columns that didn't exist in earlier schema versions
        for migration in (
            "ALTER TABLE upload_snapshots ADD COLUMN source TEXT NOT NULL DEFAULT 'qbit'",
            "ALTER TABLE audit_runs ADD COLUMN source TEXT NOT NULL DEFAULT 'qbit'",
        ):
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
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

def db_save_audit(trigger, health_score, status, error_message, snapshot, source='qbit'):
    conn = _db_conn()
    try:
        cur = conn.execute(
            'INSERT INTO audit_runs (ran_at, trigger, health_score, status, error_message, source) VALUES (?,?,?,?,?,?)',
            (datetime.now().isoformat(), trigger, health_score, status, error_message, source)
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
            'SELECT id, ran_at, trigger, health_score, status, error_message, source FROM audit_runs ORDER BY ran_at DESC LIMIT ?',
            (limit,)
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
    conn = _db_conn()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO latest_results (id, results_json) VALUES (1, ?)',
            (json.dumps(results),)
        )
        conn.commit()
    finally:
        conn.close()


def db_load_results():
    conn = _db_conn()
    try:
        row = conn.execute('SELECT results_json FROM latest_results WHERE id = 1').fetchone()
        if row:
            return json.loads(row['results_json'])
        return {
            "media_files": [], "torrent_files": [], "trackers": [],
            "status": "No audit run yet.", "dashboard": None,
        }
    finally:
        conn.close()


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

    return errors


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


def db_get_upload_snapshots(since_days=90):
    conn = _db_conn()
    try:
        if since_days == 0:
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
