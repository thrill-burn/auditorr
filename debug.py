"""Debug report support: in-memory log ring buffer, privacy sanitizers, and
the /api/debug/report payload builder.

Everything in the generated report must be safe to paste on a public forum:
no credentials, no hostnames/IPs, and no media file or folder names. Path
segments that could identify media are replaced with short stable hashes so
repeated mentions of the same file can still be correlated.
"""
import os
import re
import sys
import time
import hashlib
import logging
import platform
import sqlite3
import threading
import traceback
from collections import deque
from datetime import datetime, timedelta

from db import DATA_DIR, DB_FILE, db_load_config, db_load_results, db_get_meta
from state import get_state

log = logging.getLogger(__name__)

_PROCESS_START = time.time()

# ---------------------------------------------------------------------------
# Log ring buffer
# ---------------------------------------------------------------------------

_RING_CAPACITY = 400
_ring_lock = threading.Lock()
_ring_handler = None


class _RingBufferHandler(logging.Handler):
    def __init__(self, capacity=_RING_CAPACITY):
        super().__init__()
        self.records = deque(maxlen=capacity)

    def emit(self, record):
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += '\n' + ''.join(traceback.format_exception(*record.exc_info))
            with _ring_lock:
                self.records.append({
                    'ts':     datetime.fromtimestamp(record.created).isoformat(timespec='seconds'),
                    'level':  record.levelname,
                    'logger': record.name,
                    'msg':    msg,
                })
        except Exception:
            pass  # logging must never take the app down


def install_ring_buffer():
    """Attach the ring buffer to the root logger (idempotent)."""
    global _ring_handler
    if _ring_handler is None:
        _ring_handler = _RingBufferHandler()
        _ring_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(_ring_handler)
    return _ring_handler


def _recent_logs():
    if _ring_handler is None:
        return []
    with _ring_lock:
        return list(_ring_handler.records)


# ---------------------------------------------------------------------------
# Sanitizers
# ---------------------------------------------------------------------------

# Generic structural directory names that reveal setup shape, not identity.
_SAFE_SEGMENTS = {
    'data', 'media', 'medias', 'torrents', 'torrent', 'movies', 'movie', 'tv',
    'shows', 'show', 'series', 'anime', 'music', 'books', 'audiobooks', 'comics',
    'downloads', 'download', 'complete', 'incomplete', 'seeding', 'usenet',
    'mnt', 'user', 'user0', 'srv', 'app', 'config', 'cache', 'pool', 'library',
    'video', 'videos', 'films', 'film', '4k', 'uhd', 'remux', 'remuxes',
    'volume1', 'volume2', 'volume3', 'disk1', 'disk2', 'disk3', 'storage',
    'home', 'docker', 'appdata', 'bdmv', 'backup', 'stream', 'playlist',
    'season', 'specials', 'featurettes', 'extras', 'sample', 'subs', 'proper',
}


def _seg_hash(s):
    return hashlib.md5(s.encode('utf-8', errors='replace')).hexdigest()[:6]


# Real file extensions worth keeping for debuggability ("~a1b2c3.mkv").
# Anything else after a dot is part of a release name and gets hashed away.
_SAFE_EXTENSIONS = {
    '.mkv', '.mp4', '.avi', '.m2ts', '.ts', '.iso', '.wmv', '.mov', '.webm',
    '.srt', '.sub', '.idx', '.ass', '.sup', '.nfo', '.txt', '.json', '.xml',
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.bdmv', '.clpi', '.mpls', '.bup',
    '.ifo', '.vob', '.flac', '.mp3', '.m4a', '.m4b', '.ogg', '.wav', '.opus',
    '.epub', '.mobi', '.pdf', '.cbz', '.cbr', '.rar', '.zip', '.7z', '.par2',
    '.exe', '.bin', '.dat', '.db', '.log', '.torrent', '.part', '.tmp',
}


def sanitize_segment(seg):
    """Keep generic segments; replace identifying ones with a stable hash."""
    if not seg:
        return seg
    base, ext = os.path.splitext(seg)
    core = re.sub(r'[\s_\-0-9]+', '', base).lower()
    if not core or core in _SAFE_SEGMENTS or base.lower() in _SAFE_SEGMENTS:
        return seg
    # Keep recognized file extensions so file types stay debuggable
    if ext.lower() in _SAFE_EXTENSIONS:
        return f'~{_seg_hash(seg)}{ext}'
    return f'~{_seg_hash(seg)}'


def sanitize_path(p):
    if not p:
        return p
    norm = str(p).replace('\\', '/')
    parts = norm.split('/')
    return '/'.join(sanitize_segment(seg) for seg in parts)


_URL_RE      = re.compile(r'(https?://)([^\s/\'"]+)')
_IP_RE       = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
_TOKEN_RE    = re.compile(r'\b[0-9a-fA-F]{24,}\b')
_NIX_PATH_RE = re.compile(r'(?:/[^/\s\'",;]+){2,}/?')
_WIN_PATH_RE = re.compile(r'[A-Za-z]:\\[^\s\'",;]+')


def sanitize_text(text):
    """Scrub log/error text: paths hashed per-segment, hosts/IPs/tokens redacted."""
    if not text:
        return text
    s = str(text)
    s = _URL_RE.sub(lambda m: m.group(1) + '<host>', s)
    s = _IP_RE.sub('<ip>', s)
    s = _TOKEN_RE.sub('<token>', s)
    s = _NIX_PATH_RE.sub(lambda m: sanitize_path(m.group(0)), s)
    s = _WIN_PATH_RE.sub(lambda m: sanitize_path(m.group(0)), s)
    return s


def sanitize_exclusion_pattern(pat):
    """Keep structural rule syntax; hash anything that could be a media name."""
    p = str(pat).strip()
    lower = p.lower()
    for prefix in ('ext:', 'name:', 'contains:'):
        if lower.startswith(prefix):
            body = p[len(prefix):]
            if prefix == 'ext:':
                return p  # extensions are not identifying
            return f'{prefix}~{_seg_hash(body)}'
    # Pure extension globs (*.nfo) are safe; anything else gets path-sanitized
    if re.fullmatch(r'\*\.[A-Za-z0-9]{1,6}', p):
        return p
    return sanitize_path(p)


# ---------------------------------------------------------------------------
# Process / container memory
# ---------------------------------------------------------------------------

def _proc_status_kb(field):
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith(field + ':'):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def process_rss_mb():
    kb = _proc_status_kb('VmRSS')
    return round(kb / 1024) if kb is not None else None


def process_peak_rss_mb():
    kb = _proc_status_kb('VmHWM')
    return round(kb / 1024) if kb is not None else None


def _read_int_file(path):
    try:
        with open(path) as f:
            raw = f.read().strip()
        return None if raw == 'max' else int(raw)
    except (OSError, ValueError):
        return None


def container_memory():
    """Read cgroup memory limit/usage (v2 then v1). None when unavailable."""
    limit   = _read_int_file('/sys/fs/cgroup/memory.max')
    current = _read_int_file('/sys/fs/cgroup/memory.current')
    if limit is None and current is None:
        limit   = _read_int_file('/sys/fs/cgroup/memory/memory.limit_in_bytes')
        current = _read_int_file('/sys/fs/cgroup/memory/memory.usage_in_bytes')
        # v1 reports a huge number when unconstrained
        if limit is not None and limit > (1 << 60):
            limit = None
    to_mb = lambda v: round(v / (1024 * 1024)) if v is not None else None
    return {'limit_mb': to_mb(limit), 'current_mb': to_mb(current)}


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _sanitized_config(cfg):
    arr_conns = cfg.get('ARR_CONNECTIONS') or []
    return {
        'torrent_source':       cfg.get('TORRENT_SOURCE'),
        'qb_host_configured':   bool(cfg.get('QB_HOST')),
        'qb_credentials_set':   bool(cfg.get('QB_USER') or cfg.get('QB_PASS')),
        'qui_host_configured':  bool(cfg.get('QUI_HOST')),
        'qui_api_key_set':      bool(cfg.get('QUI_API_KEY')),
        'media_path':           sanitize_path(cfg.get('MEDIA_PATH')),
        'local_path':           sanitize_path(cfg.get('LOCAL_PATH')),
        'remote_path':          sanitize_path(cfg.get('REMOTE_PATH')),
        'paths_identical':      cfg.get('LOCAL_PATH') == cfg.get('REMOTE_PATH'),
        'watchdog_enabled':     cfg.get('WATCHDOG_ENABLED'),
        'watchdog_cooldown_s':  cfg.get('WATCHDOG_COOLDOWN'),
        'scheduled_interval_m': cfg.get('SCHEDULED_INTERVAL'),
        'ratios': {k.lower(): cfg.get(k) for k in ('OR_RATIO', 'NI_RATIO', 'DUP_RATIO')},
        'exclusion_hide_from_explorer': cfg.get('EXCLUSION_HIDE_FROM_EXPLORER'),
        'media_server_exclusion_presets': cfg.get('MEDIA_SERVER_EXCLUSION_PRESETS'),
        'exclusion_patterns':   [sanitize_exclusion_pattern(p) for p in (cfg.get('EXCLUSION_PATTERNS') or [])],
        'sonarr_configured':    bool(cfg.get('SONARR_URL')),
        'radarr_configured':    bool(cfg.get('RADARR_URL')),
        'arr_connections':      [
            {'service': str(c.get('service', '')), 'api_key_set': bool(c.get('api_key'))}
            for c in arr_conns if isinstance(c, dict)
        ],
    }


def _db_stats():
    stats = {}
    try:
        size = os.path.getsize(DB_FILE)
        wal  = DB_FILE + '-wal'
        if os.path.exists(wal):
            size += os.path.getsize(wal)
        stats['db_size_mb'] = round(size / (1024 * 1024), 1)
    except OSError:
        stats['db_size_mb'] = None
    try:
        conn = sqlite3.connect(DB_FILE)
        try:
            for table in ('audit_runs', 'audit_snapshots', 'upload_snapshots',
                          'change_log', 'file_results', 'app_meta'):
                try:
                    stats[f'{table}_rows'] = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                except sqlite3.OperationalError:
                    stats[f'{table}_rows'] = None
            blob_sizes = {}
            for row in conn.execute('SELECT tab, LENGTH(files_json) AS n FROM file_results'):
                blob_sizes[row[0]] = round(row[1] / (1024 * 1024), 1)
            stats['file_results_blob_mb'] = blob_sizes
        finally:
            conn.close()
    except sqlite3.Error as e:
        stats['error'] = str(e)
    return stats


def _library_stats():
    out = {}
    for tab in ('media', 'torrents'):
        out[tab] = db_get_meta(f'file_results_{tab}_stats')
    try:
        results = db_load_results()
        dash = results.get('dashboard') or {}
        out['health_score']  = dash.get('score')
        out['status']        = sanitize_text(results.get('status'))
        out['tracker_count'] = len(results.get('trackers') or [])
        details = dash.get('current', {}).get('details')
        if details:
            out['summary'] = details  # numbers only: sizes, counts, score components
    except Exception as e:
        out['error'] = sanitize_text(str(e))
    return out


def _recent_runs_sanitized(limit=20):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                'SELECT ran_at, trigger, health_score, status, error_message, source, duration_seconds '
                'FROM audit_runs ORDER BY ran_at DESC LIMIT ?', (limit,)
            ).fetchall()
            runs = [{**dict(r), 'error_message': sanitize_text(r['error_message'])} for r in rows]
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            trig = conn.execute(
                "SELECT trigger, COUNT(*) FROM audit_runs WHERE ran_at >= ? GROUP BY trigger",
                (cutoff,)
            ).fetchall()
            return runs, {r[0]: r[1] for r in trig}
        finally:
            conn.close()
    except sqlite3.Error:
        return [], {}


def build_debug_report(version):
    state = get_state()
    runs, runs_last_24h = _recent_runs_sanitized()
    report = {
        '_readme': (
            'auditorr debug report. Credentials are omitted; hosts, IPs and API '
            'tokens are redacted; media file/folder names are replaced with '
            'stable 6-char hashes (~abc123) so they can be correlated but not read.'
        ),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'app_version':  version,
        'runtime': {
            'python':            sys.version.split()[0],
            'platform':          platform.platform(),
            'pid':               os.getpid(),
            'process_uptime_s':  round(time.time() - _PROCESS_START),
            'cpu_count':         os.cpu_count(),
            'rss_mb':            process_rss_mb(),
            'peak_rss_mb':       process_peak_rss_mb(),
            'container_memory':  container_memory(),
            'data_dir_set':      bool(os.environ.get('DATA_DIR')),
            'auth_enabled':      bool(os.environ.get('AUDITORR_SECRET', '').strip()),
        },
        'config': _sanitized_config(db_load_config()),
        'scan_state': {
            **{k: state.get(k) for k in (
                'is_scanning', 'progress', 'phase', 'last_audit_time',
                'total_files', 'scanned_files', 'last_scan_status',
                'trigger', 'next_scan_in', 'last_scan_duration',
            )},
            'status_message': sanitize_text(state.get('status_message')),
            'phase_history':  [
                {**h, 'message': sanitize_text(h.get('message'))}
                for h in (state.get('phase_history') or [])
            ],
        },
        'crash_evidence': {
            'consecutive_aborted_scans': db_get_meta('consecutive_aborted_scans', 0),
            'last_aborted_scan': (lambda m: {
                **m, 'phase': sanitize_text(m.get('phase')),
            } if m else None)(db_get_meta('last_aborted_scan')),
            'in_progress_scan_marker': db_get_meta('scan_marker'),
            'runs_last_24h_by_trigger': runs_last_24h,
        },
        'library':       _library_stats(),
        'database':      _db_stats(),
        'audit_history': runs,
        'recent_logs': [
            {**r, 'msg': sanitize_text(r['msg'])}
            for r in _recent_logs()
        ],
    }
    return report
