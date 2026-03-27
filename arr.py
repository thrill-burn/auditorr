import os
import re
import json
import logging
import urllib.request
import urllib.error

log = logging.getLogger(__name__)

# Service config map: url_key, api_key_key, remote_path_key, command, list_path, slug_prefix, display_name
_SERVICE_MAP = {
    'sonarr': {
        'url_key':      'SONARR_URL',
        'key_key':      'SONARR_API_KEY',
        'remote_key':   'SONARR_REMOTE_PATH',
        'command':      'DownloadedEpisodesScan',
        'list_path':    '/api/v3/series',
        'slug_prefix':  '/series/',
        'name':         'Sonarr',
    },
    'radarr': {
        'url_key':      'RADARR_URL',
        'key_key':      'RADARR_API_KEY',
        'remote_key':   'RADARR_REMOTE_PATH',
        'command':      'DownloadedMoviesScan',
        'list_path':    '/api/v3/movie',
        'slug_prefix':  '/movie/',
        'name':         'Radarr',
    },
}


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


def _arr_get(base_url, api_key, path):
    """GET from a *arr instance and return parsed JSON."""
    endpoint = base_url.rstrip('/') + path
    http_req = urllib.request.Request(endpoint, headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(http_req, timeout=10) as resp:
        return json.loads(resp.read())


def _parse_title_from_filename(filename):
    """Parse a clean title from a media filename for *arr search."""
    name = os.path.splitext(os.path.basename(filename))[0]
    # Replace dots, underscores, hyphens with spaces first so later regexes
    # operate on space-separated tokens with consistent word boundaries
    name = re.sub(r'[._\-]', ' ', name)
    # For TV shows: strip everything from SxxExx onwards
    name = re.split(r'[Ss]\d{1,2}[Ee]\d{1,2}', name)[0]
    # For movies: strip year (4 digits) and everything after
    name = re.split(r'\b(19|20)\d{2}\b', name)[0]
    # Strip quality/format tags and everything after — \s+ anchor ensures the
    # tag is a standalone token, preventing mid-word matches (e.g. "Internal" in
    # "Internal Affairs" or "4K" in "The 4K Experience")
    name = re.sub(
        r'\s+(2160p|1080p|1080i|720p|480p|4K|BluRay|BDRip|BRRip|WEB-DL|WEBRip|HDTV|DVDRip|'
        r'AMZN|DSNP|NF|HULU|HBO|x264|x265|HEVC|HDR|DV|AAC|DDP|DTS|MA|FLAC|REMUX|PROPER|REPACK|INTERNAL)'
        r'.*$',
        '', name, flags=re.IGNORECASE,
    )
    # Collapse multiple spaces and strip
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def _normalize_title(title):
    """Lowercase and strip punctuation for fuzzy title matching."""
    t = title.lower()
    t = re.sub(r'[^\w\s]', ' ', t)  # replace punctuation with space
    t = re.sub(r'\s+', ' ', t).strip()
    return t


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


def arr_rescan(cfg, service, paths):
    """Shared rescan logic for sonarr and radarr.

    service is 'sonarr' or 'radarr'. Returns the number of paths rescanned.
    Raises ValueError if the service is not configured, or re-raises network errors.
    """
    svc = _SERVICE_MAP[service]
    url        = cfg.get(svc['url_key'], '').strip()
    key        = cfg.get(svc['key_key'], '').strip()
    local_path = cfg.get('LOCAL_PATH', '').strip()
    if not url or not key:
        raise ValueError(f"{svc['name']} not configured")
    remote_path = cfg.get(svc['remote_key'], '').strip()
    for path in paths:
        abs_path = path if os.path.isabs(path) else (os.path.join(local_path, path) if local_path else path)
        if remote_path and local_path and abs_path.startswith(local_path) and \
                abs_path[len(local_path):][:1] in ('/', ''):
            arr_path = remote_path + abs_path[len(local_path):]
        else:
            arr_path = abs_path
        arr_path = os.path.dirname(arr_path)
        _arr_command(url, key, svc['command'], arr_path)
    return len(paths)


def arr_search(cfg, service, file_path):
    """Shared search logic for sonarr and radarr.

    Returns {"url": ..., "title": ...} or raises LookupError if not found,
    ValueError if not configured, or re-raises network errors.
    """
    svc = _SERVICE_MAP[service]
    url = cfg.get(svc['url_key'], '').strip()
    key = cfg.get(svc['key_key'], '').strip()
    if not url or not key:
        raise ValueError(f"{svc['name']} not configured")
    filename         = os.path.basename(file_path)
    title            = _parse_title_from_filename(filename)
    parsed_normalized = _normalize_title(title)
    try:
        items = _arr_get(url, key, svc['list_path'])
    except urllib.error.HTTPError as e:
        raise ConnectionError(f"{svc['name']} returned HTTP {e.code}: {e.reason}") from e
    best             = None
    best_score       = 0
    for item in items:
        candidate = _normalize_title(item.get('title', ''))
        alt       = _normalize_title(item.get('cleanTitle', ''))
        if candidate == parsed_normalized or alt == parsed_normalized:
            best = item
            break
        if parsed_normalized in candidate or candidate in parsed_normalized:
            score = len(candidate)
            if score > best_score:
                best       = item
                best_score = score
    if best is None:
        raise LookupError(
            f"'{title}' not found in {svc['name']} library. "
            f"Make sure it is added and monitored in {svc['name']} first."
        )
    result_url = url.rstrip('/') + svc['slug_prefix'] + best['titleSlug']
    return {"url": result_url, "title": best.get('title', title)}
