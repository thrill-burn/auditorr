import os
import re
import json
import logging
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

_arr_media_index_cache = {'data': None, 'ts': 0}
_ARR_MEDIA_INDEX_TTL = 120

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


def normalize_arr_connections(cfg, service=None):
    """Return normalized Sonarr/Radarr connection records.

    Supports the legacy singleton config keys and the new ARR_CONNECTIONS list.
    """
    raw_connections = cfg.get('ARR_CONNECTIONS')
    normalized = []

    if isinstance(raw_connections, list) and raw_connections:
        for raw in raw_connections:
            if not isinstance(raw, dict):
                continue
            conn = _normalize_arr_connection(raw)
            if conn and (service is None or conn['service'] == service):
                normalized.append(conn)
    else:
        for svc_name, svc in _SERVICE_MAP.items():
            url = str(cfg.get(svc['url_key'], '')).strip()
            api_key = str(cfg.get(svc['key_key'], '')).strip()
            if not url or not api_key:
                continue
            if service is not None and svc_name != service:
                continue
            normalized.append(_normalize_arr_connection({
                'id': f'{svc_name}-default',
                'service': svc_name,
                'name': svc['name'],
                'base_url': url,
                'api_key': api_key,
                'remote_path': cfg.get(svc['remote_key'], ''),
            }))

    seen = set()
    deduped = []
    for conn in normalized:
        if conn['id'] in seen:
            raise ValueError(f"Duplicate Arr connection id: {conn['id']}")
        seen.add(conn['id'])
        deduped.append(conn)
    return deduped


def fetch_arr_media_index(cfg, force=False):
    """Fetch managed media-file paths from every configured Arr instance.

    Results are cached for _ARR_MEDIA_INDEX_TTL seconds. Pass force=True to bypass.
    """
    now = time.monotonic()
    if not force and _arr_media_index_cache['data'] is not None and (now - _arr_media_index_cache['ts']) < _ARR_MEDIA_INDEX_TTL:
        return _arr_media_index_cache['data']
    media = []
    for conn in normalize_arr_connections(cfg):
        try:
            if conn['service'] == 'radarr':
                rows = _fetch_radarr_media(conn)
            else:
                rows = _fetch_sonarr_media(conn)
            media.extend(_apply_arr_media_path_mapping(rows, conn, cfg))
        except Exception as e:
            log.warning("Could not fetch %s media from %s: %s", conn['service'], conn['id'], e)
    _arr_media_index_cache['data'] = media
    _arr_media_index_cache['ts'] = now
    return media


def fetch_arr_indexers(cfg):
    """Return a deduped list of indexer names seen across all configured Arr instances."""
    names = []
    for conn in normalize_arr_connections(cfg):
        try:
            indexers = _arr_get(conn['base_url'], conn['api_key'], '/api/v3/indexer')
            for idx in indexers:
                name = idx.get('name')
                if name and name not in names:
                    names.append(name)
        except Exception as e:
            log.warning("Could not fetch indexers from %s: %s", conn['id'], e)
    return names


def _episode_id_from_path(conn, arr_id, file_path):
    """Derive a Sonarr episode ID by parsing SxxExx from file_path and matching against the series."""
    m = re.search(r'[Ss](\d{1,2})[Ee](\d{1,2})', os.path.basename(file_path))
    if not m:
        return None
    season, ep_num = int(m.group(1)), int(m.group(2))
    try:
        episodes = _arr_get(conn['base_url'], conn['api_key'], f'/api/v3/episode?seriesId={arr_id}')
        for ep in episodes:
            if ep.get('seasonNumber') == season and ep.get('episodeNumber') == ep_num:
                return ep.get('id')
    except Exception as e:
        log.warning("Could not look up episode from path %s: %s", file_path, e)
    return None


def fetch_release_matrix(cfg, service, connection_id, arr_id, episode_id=None, season_number=None, file_path=None):
    """Fetch the interactive release search for a single Arr item.

    Returns a list of {title, indexer, seeders, leechers, size, guid} dicts.
    Radarr:  /api/v3/release?movieId={arr_id}
    Sonarr season pack: /api/v3/release?seriesId={arr_id}&seasonNumber={season_number}
    Sonarr episode:     /api/v3/release?episodeId={episode_id}

    For Sonarr grouped (season) rows, pass season_number — this triggers Sonarr's
    native season pack search. For single-episode rows, episode_id or file_path is used.
    """
    conns = normalize_arr_connections(cfg, service=service)
    conn = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        raise ValueError(f"Arr connection '{connection_id}' not found for service '{service}'")
    if service == 'radarr':
        api_path = f'/api/v3/release?movieId={arr_id}'
    else:
        if season_number is not None:
            api_path = f'/api/v3/release?seriesId={arr_id}&seasonNumber={season_number}'
        else:
            if not episode_id and file_path:
                episode_id = _episode_id_from_path(conn, arr_id, file_path)
            if not episode_id:
                raise ValueError("Could not determine episode ID for Sonarr release search — open in Sonarr directly")
            api_path = f'/api/v3/release?episodeId={episode_id}'
    rows = _arr_get(conn['base_url'], conn['api_key'], api_path, timeout=90)
    result = []
    for r in rows:
        q_outer = r.get('quality') or {}
        q_inner = q_outer.get('quality') or {}
        result.append({
            'title':               r.get('title', ''),
            'indexer':             r.get('indexer', ''),
            'indexer_id':          r.get('indexerId', 0),
            'seeders':             r.get('seeders', 0),
            'leechers':            r.get('leechers', 0),
            'size':                r.get('size', 0),
            'guid':                r.get('guid', ''),
            'quality_name':        q_inner.get('name', ''),
            'resolution':          q_inner.get('resolution', 0),
            'source':              q_inner.get('source', ''),
            'hdr':                 _detect_hdr(r.get('title', '')),
            'custom_format_score': r.get('customFormatScore', 0),
            'quality_weight':      r.get('qualityWeight', 0),
        })
    return result


def grab_release(cfg, service, connection_id, guid, indexer_id):
    """Trigger a release grab on the given Arr instance (equivalent to clicking Grab in the UI)."""
    conns = normalize_arr_connections(cfg, service=service)
    conn = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        raise ValueError(f"Arr connection '{connection_id}' not found for service '{service}'")
    body = json.dumps({"guid": guid, "indexerId": indexer_id}).encode()
    http_req = urllib.request.Request(
        conn['base_url'].rstrip('/') + '/api/v3/release',
        data=body,
        headers={"X-Api-Key": conn['api_key'], "Content-Type": "application/json"},
        method='POST',
    )
    with urllib.request.urlopen(http_req, timeout=30) as resp:
        return json.loads(resp.read())


def poll_queue_until_clear(cfg, service, connection_id, arr_id, timeout=300, on_downloading=None):
    """Poll Sonarr/Radarr queue for arr_id until the item clears or timeout (seconds)."""
    conns = normalize_arr_connections(cfg, service=service)
    conn  = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        return
    queue_path = (
        f'/api/v3/queue?movieId={arr_id}&pageSize=50'
        if service == 'radarr' else
        f'/api/v3/queue?seriesId={arr_id}&pageSize=50'
    )
    deadline     = time.monotonic() + timeout
    notified     = False
    while time.monotonic() < deadline:
        try:
            result  = _arr_get(conn['base_url'], conn['api_key'], queue_path, timeout=10)
            records = result.get('records', result) if isinstance(result, dict) else result
            # 'completed' means download done but Radarr/Sonarr hasn't processed the import yet —
            # keep polling until the item fully disappears or reaches a terminal error state
            active  = [r for r in records if r.get('status') not in ('warning', 'error', 'failed')]
            if active:
                if not notified and on_downloading:
                    on_downloading()
                    notified = True
            else:
                return
        except Exception:
            pass
        time.sleep(5)


def force_manual_import_by_id(cfg, service, connection_id, arr_id):
    """Force manual import of a movie or series, bypassing quality cutoff."""
    conns = normalize_arr_connections(cfg, service=service)
    conn  = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        raise ValueError(f"Arr connection '{connection_id}' not found for service '{service}'")

    if service == 'radarr':
        info     = _arr_get(conn['base_url'], conn['api_key'], f'/api/v3/movie/{arr_id}')
        folder   = info.get('path', '')
        id_param = f'movieId={arr_id}'
    else:
        info     = _arr_get(conn['base_url'], conn['api_key'], f'/api/v3/series/{arr_id}')
        folder   = info.get('path', '')
        id_param = f'seriesId={arr_id}'

    if not folder:
        raise ValueError(f"No folder returned by {service} for id {arr_id}")

    encoded = urllib.parse.quote(folder, safe='')
    files   = _arr_get(conn['base_url'], conn['api_key'],
                       f'/api/v3/manualimport?folder={encoded}&{id_param}&filterExistingFiles=false',
                       timeout=30)
    if not files:
        # Retry once after a delay — handles timing race where qBit finishes but files
        # aren't importable yet from Sonarr/Radarr's perspective
        log.info("No importable files for %s %s on first attempt, retrying in 15s…", service, arr_id)
        time.sleep(15)
        files = _arr_get(conn['base_url'], conn['api_key'],
                         f'/api/v3/manualimport?folder={encoded}&{id_param}&filterExistingFiles=false',
                         timeout=30)
    if not files:
        raise ValueError(f"No importable files found — check {service} queue manually")

    body = json.dumps({'files': files, 'importMode': 'Auto'}).encode()
    req  = urllib.request.Request(
        conn['base_url'].rstrip('/') + '/api/v3/manualimport',
        data=body,
        headers={'X-Api-Key': conn['api_key'], 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            # Radarr/Sonarr returns 4xx when files are already imported via normal queue flow
            log.info("Manual import POST returned %s for %s %s — likely already imported", e.code, service, arr_id)
            return []
        raise


def test_arr_connections(cfg):
    """Probe configured Arr instances and confirm managed file metadata is readable."""
    connections = normalize_arr_connections(cfg)
    results = []

    for conn in connections:
        item = {
            'id': conn['id'],
            'name': conn['name'],
            'service': conn['service'],
            'base_url': conn['base_url'],
            'ok': False,
            'managed_file_count': 0,
            'sample_paths': [],
        }
        try:
            ok, message = _test_arr_connection(conn['base_url'], conn['api_key'])
        except Exception as e:
            ok, message = False, str(e)

        if not ok:
            item['message'] = message or 'Connection failed'
            results.append(item)
            continue

        try:
            media = _fetch_radarr_media(conn) if conn['service'] == 'radarr' else _fetch_sonarr_media(conn)
            media = _apply_arr_media_path_mapping(media, conn, cfg)
            item['ok'] = True
            item['managed_file_count'] = len(media)
            item['sample_paths'] = [m['path'] for m in media if m.get('path')][:5]
        except Exception as e:
            item['message'] = f'Connected, but media file metadata could not be read: {e}'
        results.append(item)

    return {
        'ok': bool(connections) and all(item['ok'] for item in results),
        'connection_count': len(connections),
        'connections': results,
    }


def _normalize_arr_connection(raw):
    service = str(raw.get('service', '')).strip().lower()
    if service not in _SERVICE_MAP:
        return None
    base_url = str(raw.get('base_url') or raw.get('url') or '').strip().rstrip('/')
    api_key = str(raw.get('api_key') or raw.get('apiKey') or '').strip()
    if not base_url or not api_key:
        return None
    conn_id = str(raw.get('id') or f"{service}-{_slug(raw.get('name') or service)}").strip()
    return {
        'id': conn_id,
        'service': service,
        'name': str(raw.get('name') or _SERVICE_MAP[service]['name']).strip(),
        'base_url': base_url,
        'api_key': api_key,
        'remote_path': str(raw.get('remote_path') or raw.get('remotePath') or '').strip(),
        'media_path': str(raw.get('media_path') or raw.get('mediaPath') or '').strip(),
        'local_media_path': str(raw.get('local_media_path') or raw.get('localMediaPath') or '').strip(),
    }


def _apply_arr_media_path_mapping(rows, conn, cfg):
    arr_root = conn.get('media_path', '')
    local_root = conn.get('local_media_path', '') or cfg.get('MEDIA_PATH', '')
    mapped = []
    for row in rows:
        item = dict(row)
        original_path = item.get('path', '')
        mapped_path = _replace_path_prefix(original_path, arr_root, local_root)
        if mapped_path != original_path:
            item['arr_path'] = original_path
            item['path'] = mapped_path
        mapped.append(item)
    return mapped


def _replace_path_prefix(path, source_root, target_root):
    if not path or not source_root or not target_root:
        return path
    path_norm = _path_norm(path)
    source_norm = _path_norm(source_root).rstrip('/')
    target_norm = _path_norm(target_root).rstrip('/')
    if not source_norm:
        return path
    if path_norm == source_norm:
        return target_norm
    if path_norm.startswith(f'{source_norm}/'):
        return f"{target_norm}/{path_norm[len(source_norm):].lstrip('/')}"
    return path


def _path_norm(path):
    return str(path or '').replace('\\', '/').replace('//', '/')


def _fetch_radarr_media(conn):
    rows = []
    for movie in _arr_get(conn['base_url'], conn['api_key'], '/api/v3/movie'):
        movie_file = movie.get('movieFile') or {}
        path = movie_file.get('path')
        if not path:
            continue
        q_outer = movie_file.get('quality') or {}
        q_inner = q_outer.get('quality') or {}
        rows.append({
            'connection_id': conn['id'],
            'connection_name': conn['name'],
            'service': 'radarr',
            'title': movie.get('title') or '',
            'year': movie.get('year'),
            'path': path,
            'relative_path': movie_file.get('relativePath'),
            'arr_id': movie.get('id'),
            'file_id': movie_file.get('id'),
            'title_slug': movie.get('titleSlug') or '',
            'file_quality_name': q_inner.get('name', ''),
            'file_hdr': _detect_hdr(path),
        })
    return rows


def _fetch_sonarr_media(conn):
    series_list = _arr_get(conn['base_url'], conn['api_key'], '/api/v3/series')
    valid_series = [(s, s['id']) for s in series_list if s.get('id') is not None]
    if not valid_series:
        return []

    base_url = conn['base_url']
    api_key = conn['api_key']

    def _fetch_episode_files(series, series_id):
        episode_files = _arr_get(base_url, api_key, f'/api/v3/episodefile?seriesId={series_id}')
        rows = []
        for episode_file in episode_files:
            path = episode_file.get('path')
            if not path:
                continue
            q_outer = episode_file.get('quality') or {}
            q_inner = q_outer.get('quality') or {}
            rows.append({
                'connection_id': conn['id'],
                'connection_name': conn['name'],
                'service': 'sonarr',
                'title': series.get('title') or '',
                'year': series.get('year'),
                'path': path,
                'relative_path': episode_file.get('relativePath'),
                'arr_id': series_id,
                'file_id': episode_file.get('id'),
                'episode_ids': episode_file.get('episodeIds') or [],
                'title_slug': series.get('titleSlug') or '',
                'file_quality_name': q_inner.get('name', ''),
                'file_hdr': _detect_hdr(path),
            })
        return rows

    all_rows = []
    max_workers = min(8, len(valid_series))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_episode_files, s, sid): sid for s, sid in valid_series}
        for future in as_completed(futures):
            all_rows.extend(future.result())
    return all_rows


def _detect_hdr(title):
    """Detect the highest HDR format present in a release title."""
    t = re.sub(r'[.\-_]', ' ', title.upper())
    if re.search(r'\bDOLBY\s+VISION\b|\bDOVI\b|\bDV\b', t):
        return 'DV'
    if re.search(r'\bHDR10\+|\bHDR10PLUS\b', t):
        return 'HDR10+'
    if re.search(r'\bHDR10\b', t):
        return 'HDR10'
    if re.search(r'\bHLG\b', t):
        return 'HLG'
    if re.search(r'\bHDR\b', t):
        return 'HDR'
    return ''


def _slug(value):
    return re.sub(r'[^a-z0-9]+', '-', str(value).lower()).strip('-') or 'default'


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


def _arr_get(base_url, api_key, path, timeout=10):
    """GET from a *arr instance and return parsed JSON."""
    endpoint = base_url.rstrip('/') + path
    http_req = urllib.request.Request(endpoint, headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(http_req, timeout=timeout) as resp:
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
    connections = normalize_arr_connections(cfg, service=service)
    local_path = cfg.get('LOCAL_PATH', '').strip()
    if not connections:
        raise ValueError(f"{svc['name']} not configured")
    command_count = 0
    for conn in connections:
        remote_path = conn.get('remote_path', '').strip()
        for path in paths:
            abs_path = path if os.path.isabs(path) else (os.path.join(local_path, path) if local_path else path)
            if remote_path and local_path and abs_path.startswith(local_path) and \
                    abs_path[len(local_path):][:1] in ('/', ''):
                arr_path = remote_path + abs_path[len(local_path):]
            else:
                arr_path = abs_path
            arr_path = os.path.dirname(arr_path)
            _arr_command(conn['base_url'], conn['api_key'], svc['command'], arr_path)
            command_count += 1
    return command_count


def arr_search(cfg, service, file_path):
    """Shared search logic for sonarr and radarr.

    Returns {"url": ..., "title": ...} or raises LookupError if not found,
    ValueError if not configured, or re-raises network errors.
    """
    svc = _SERVICE_MAP[service]
    connections = normalize_arr_connections(cfg, service=service)
    if not connections:
        raise ValueError(f"{svc['name']} not configured")
    filename         = os.path.basename(file_path)
    title            = _parse_title_from_filename(filename)
    parsed_normalized = _normalize_title(title)
    best             = None
    best_score       = 0
    best_conn        = None
    for conn in connections:
        try:
            items = _arr_get(conn['base_url'], conn['api_key'], svc['list_path'])
        except urllib.error.HTTPError as e:
            raise ConnectionError(f"{conn['name']} returned HTTP {e.code}: {e.reason}") from e
        for item in items:
            candidate = _normalize_title(item.get('title', ''))
            alt       = _normalize_title(item.get('cleanTitle', ''))
            if candidate == parsed_normalized or alt == parsed_normalized:
                best = item
                best_conn = conn
                best_score = float('inf')
                break
            if parsed_normalized in candidate or candidate in parsed_normalized:
                score = len(candidate)
                if score > best_score:
                    best       = item
                    best_conn  = conn
                    best_score = score
        if best_score == float('inf'):
            break
    if best is None:
        raise LookupError(
            f"'{title}' not found in {svc['name']} library. "
            f"Make sure it is added and monitored in {svc['name']} first."
        )
    result_url = best_conn['base_url'].rstrip('/') + svc['slug_prefix'] + best['titleSlug']
    return {"url": result_url, "title": best.get('title', title), "connection_id": best_conn['id']}
