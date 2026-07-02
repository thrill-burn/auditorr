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

from db import DATA_DIR, DB_FILE, db_load_config, db_load_results, db_get_meta, db_set_meta
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


# An already-sanitized segment ("~a1b2c3" / "~a1b2c3.mkv") — must pass through
# unchanged so running the sanitizer twice never re-hashes (hash stability).
_HASHED_SEG_RE = re.compile(r'~[0-9a-f]{6}(\.[A-Za-z0-9]{1,5})?')


def sanitize_segment(seg):
    """Keep generic segments; replace identifying ones with a stable hash."""
    if not seg:
        return seg
    if _HASHED_SEG_RE.fullmatch(seg):
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
# Quoted values in structured log lines (file_name='...', folder="..."). Quotes
# delimit the full value, so this is the only place paths/names containing
# spaces can be caught reliably.
_QUOTED_RE   = re.compile(r"'([^'\n]{3,})'|\"([^\"\n]{3,})\"")
# Bare filenames outside quotes: dotted release names ending in a real extension
_EXT_ALT     = '|'.join(e.lstrip('.') for e in sorted(_SAFE_EXTENSIONS))
_FILENAME_RE = re.compile(r'[^\s/\\\'",;]+\.(?:' + _EXT_ALT + r')\b', re.IGNORECASE)
# Media-release fingerprints: year in parens, SxxExx, resolution, source tags —
# used to decide that a quoted value without slashes/extension is still a title
_MEDIA_HINT_RE = re.compile(
    r'\((?:19|20)\d\d\)|\bS\d{1,2}E\d{2,3}\b|\b\d{3,4}p\b'
    r'|\b(?:WEB-?DL|WEBRip|BluRay|Blu-Ray|REMUX|HDTV|DVDRip|x26[45]|H\.?26[45])\b',
    re.IGNORECASE,
)


def _sanitize_quoted(m):
    quote = m.group(0)[0]
    inner = m.group(1) if m.group(1) is not None else m.group(2)
    if '/' in inner or '\\' in inner:
        return f'{quote}{sanitize_path(inner)}{quote}'
    _, ext = os.path.splitext(inner)
    if ext.lower() in _SAFE_EXTENSIONS or _MEDIA_HINT_RE.search(inner):
        return f'{quote}{sanitize_segment(inner)}{quote}'
    return m.group(0)


def sanitize_text(text):
    """Scrub log/error text: paths hashed per-segment, hosts/IPs/tokens redacted."""
    if not text:
        return text
    s = str(text)
    s = _URL_RE.sub(lambda m: m.group(1) + '<host>', s)
    s = _IP_RE.sub('<ip>', s)
    s = _TOKEN_RE.sub('<token>', s)
    # Quoted spans first — quotes are the only reliable delimiter for paths and
    # filenames containing spaces ("The Lion King (1994) ... .mkv")
    s = _QUOTED_RE.sub(_sanitize_quoted, s)
    s = _NIX_PATH_RE.sub(lambda m: sanitize_path(m.group(0)), s)
    s = _WIN_PATH_RE.sub(lambda m: sanitize_path(m.group(0)), s)
    # Bare unquoted filenames (file_name=Some.Release.2025.2160p.GRP.mkv)
    s = _FILENAME_RE.sub(lambda m: sanitize_segment(m.group(0)), s)
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


def _read_keyed_file(path):
    """Parse 'key value' lines (cgroup events files). None when unreadable."""
    try:
        with open(path) as f:
            out = {}
            for line in f:
                parts = line.split()
                if len(parts) == 2:
                    try:
                        out[parts[0]] = int(parts[1])
                    except ValueError:
                        pass
            return out
    except OSError:
        return None


# Module constants so tests can point these at fixture files
_CGROUP_V2_EVENTS = '/sys/fs/cgroup/memory.events'
_CGROUP_V1_OOM    = '/sys/fs/cgroup/memory/memory.oom_control'


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


def cgroup_oom_events():
    """Kernel OOM counters for this container's cgroup, or None when unavailable.

    The OOM killer SIGKILLs the process — nothing can be logged from inside at
    the moment of death — but the cgroup's oom_kill counter survives a worker
    restart (it resets only when the whole container is recreated), so a counter
    increment seen at next boot is hard evidence the previous death was an OOM.
    """
    vals = _read_keyed_file(_CGROUP_V2_EVENTS)
    if vals is not None:
        return {'oom': vals.get('oom', 0), 'oom_kill': vals.get('oom_kill', 0)}
    vals = _read_keyed_file(_CGROUP_V1_OOM)
    if vals is not None and 'oom_kill' in vals:
        return {'oom_kill': vals['oom_kill']}
    return None


def host_available_mb():
    """Host MemAvailable — relevant when there is no container memory limit and
    the host's OOM killer is the thing doing the killing."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return round(int(line.split()[1]) / 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


def memory_pressure():
    """One-call snapshot of everything relevant to diagnosing memory deaths."""
    return {
        'rss_mb':            process_rss_mb(),
        'peak_rss_mb':       process_peak_rss_mb(),
        'container':         container_memory(),
        'cgroup_oom_events': cgroup_oom_events(),
        'host_available_mb': host_available_mb(),
    }


# ---------------------------------------------------------------------------
# Heap trimming
# ---------------------------------------------------------------------------

try:
    import ctypes
    _libc = ctypes.CDLL('libc.so.6')
except Exception:
    _libc = None


def malloc_trim():
    """Ask glibc to hand freed heap pages back to the OS.

    CPython frees scan/request structures internally, but glibc keeps the pages
    resident, so RSS stays at the worst peak ever reached for the container's
    lifetime — the 'ratchet' very large libraries report as a leak. Returns
    False (no-op) on musl/Windows/macOS."""
    if _libc is None:
        return False
    try:
        _libc.malloc_trim(0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Memory timeline (always-on) + heavy-request attribution
# ---------------------------------------------------------------------------
# The in-scan sampler (audit._memory_sampler) only covers crashes; this covers
# the slow-growth complaint: is RSS a staircase that jumps at scans, a
# staircase that jumps at workflow page visits, or a continuous climb (a real
# leak)? `blocks` is sys.getallocatedblocks() — flat blocks under high RSS
# means allocator ratchet, monotonically climbing blocks means a genuine leak.

_MEM_SAMPLE_INTERVAL = 60      # seconds between samples
_MEM_RECENT_SAMPLES  = 180     # fine-grained ring: last ~3 hours (lost on restart)
_MEM_HOURLY_KEPT     = 336     # persisted hourly aggregates: 14 days

_mem_lock   = threading.Lock()
_mem_recent = deque(maxlen=_MEM_RECENT_SAMPLES)
_heavy_ring = deque(maxlen=50)
_mem_monitor_started = False


def record_heavy_request(path, rss_before, rss_after, rss_trimmed, duration_ms):
    """Ring-buffer one heavy request's memory footprint for the debug report."""
    with _mem_lock:
        _heavy_ring.append({
            'ts':          datetime.now().isoformat(timespec='seconds'),
            'path':        path,
            'rss_before':  rss_before,
            'rss_after':   rss_after,
            'rss_trimmed': rss_trimmed,
            'duration_ms': duration_ms,
        })


def _memory_snapshot():
    with _mem_lock:
        return {'timeline_recent': list(_mem_recent), 'heavy_requests': list(_heavy_ring)}


def _persist_hour(hour, agg):
    hist = db_get_meta('memory_history') or []
    hist.append({'hour': hour, **agg})
    db_set_meta('memory_history', hist[-_MEM_HOURLY_KEPT:])


def _memory_monitor():
    hour, agg = None, None
    while True:
        time.sleep(_MEM_SAMPLE_INTERVAL)
        try:
            rss   = process_rss_mb()
            state = get_state()
            sample = {
                'ts':     datetime.now().isoformat(timespec='seconds'),
                'rss_mb': rss,
                'blocks': sys.getallocatedblocks(),
                'phase':  state.get('phase') if state.get('is_scanning') else None,
            }
            with _mem_lock:
                _mem_recent.append(sample)
            if rss is None:
                continue  # non-Linux dev machine — nothing worth aggregating
            hk = sample['ts'][:13]  # YYYY-MM-DDTHH
            if agg is not None and hk != hour:
                _persist_hour(hour, agg)
                agg = None
            if agg is None:
                hour = hk
                agg  = {'rss_min': rss, 'rss_max': rss, 'rss_end': rss,
                        'blocks_end': sample['blocks'], 'scanned': False}
            agg['rss_min']    = min(agg['rss_min'], rss)
            agg['rss_max']    = max(agg['rss_max'], rss)
            agg['rss_end']    = rss
            agg['blocks_end'] = sample['blocks']
            agg['scanned']    = agg['scanned'] or bool(state.get('is_scanning'))
        except Exception:
            pass  # the monitor must never die or take the app down


def start_memory_monitor():
    """Start the process-lifetime memory sampler (idempotent)."""
    global _mem_monitor_started
    if _mem_monitor_started:
        return
    _mem_monitor_started = True
    threading.Thread(target=_memory_monitor, daemon=True, name='memory-monitor').start()


def inotify_watch_stats():
    """This process's inotify watch count vs the kernel limit (Linux only).

    The filesystem watchdog holds one watch per directory, so on very large
    libraries this both consumes kernel slots and measures watch-table RAM."""
    try:
        watches = 0
        for fd in os.listdir('/proc/self/fdinfo'):
            try:
                with open(f'/proc/self/fdinfo/{fd}') as f:
                    watches += sum(1 for line in f if line.startswith('inotify wd:'))
            except OSError:
                continue
        limit = None
        try:
            with open('/proc/sys/fs/inotify/max_user_watches') as f:
                limit = int(f.read().strip())
        except (OSError, ValueError):
            pass
        return {'watches': watches, 'max_user_watches': limit}
    except OSError:
        return None


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
        import shutil
        usage = shutil.disk_usage(DATA_DIR)
        stats['data_dir_free_mb'] = round(usage.free / (1024 * 1024))
    except OSError:
        stats['data_dir_free_mb'] = None
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
                'SELECT ran_at, trigger, health_score, status, error_message, source, duration_seconds, peak_rss_mb '
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
            'allocated_blocks':  sys.getallocatedblocks(),
            'container_memory':  container_memory(),
            'cgroup_oom_events': cgroup_oom_events(),
            'host_available_mb': host_available_mb(),
            'malloc_trim_available': _libc is not None,
            'malloc_arena_max':  os.environ.get('MALLOC_ARENA_MAX'),
            'thread_count':      threading.active_count(),
            'thread_names':      sorted(t.name for t in threading.enumerate())[:40],
            'inotify':           inotify_watch_stats(),
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
            'oom_kills_attributed':    db_get_meta('oom_kills_attributed', 0),
            'runs_last_24h_by_trigger': runs_last_24h,
            'last_scan_phases': [
                {**h, 'message': sanitize_text(h.get('message'))}
                for h in (db_get_meta('last_scan_phases') or [])
            ],
        },
        'memory': {
            '_readme': (
                'timeline_recent: 60s samples, last ~3h, lost on restart. '
                'timeline_hourly: persisted hourly aggregates, 14 days. '
                'heavy_requests: RSS around endpoints that deserialize the full '
                'file lists (rss_trimmed = after handing freed pages back to the OS). '
                'blocks = sys.getallocatedblocks(): flat blocks under high RSS means '
                'allocator ratchet, climbing blocks means a genuine object leak.'
            ),
            **_memory_snapshot(),
            'timeline_hourly': db_get_meta('memory_history') or [],
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
