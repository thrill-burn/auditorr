import os
import re
import json
import logging
import time
import unicodedata
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
        guid = r.get('guid', '')
        result.append({
            'title':               r.get('title', ''),
            'indexer':             r.get('indexer', ''),
            'indexer_id':          r.get('indexerId', 0),
            'seeders':             r.get('seeders', 0),
            'leechers':            r.get('leechers', 0),
            'size':                r.get('size', 0),
            'guid':                guid,
            'info_url':            r.get('infoUrl') or (guid if str(guid).startswith('http') else ''),
            'quality_name':        q_inner.get('name', ''),
            'resolution':          q_inner.get('resolution', 0),
            'source':              q_inner.get('source', ''),
            'hdr':                 _detect_hdr(r.get('title', '')),
            'custom_format_score': r.get('customFormatScore', 0),
            'quality_weight':      r.get('qualityWeight', 0),
        })
    return result


def parse_trump_pm(pm_text):
    """Extract (old_titles, new_title) from a tracker trump PM.

    A PM lists one or more trumped releases between the "...trumped" /
    "following torrent" header and the "(and) will be replaced by" phrase, then
    the single replacement (typically a season pack when several episodes are
    trumped together), optionally terminated by a "Reason:" line. Returns
    ([], '') when the delimiter phrase is absent — the UI falls back to manual
    fields. The old side is a list to cover season-pack trumps (N episodes → 1
    pack); a single-release trump just yields a one-element list.
    """
    text = re.sub(r'\r\n?', '\n', str(pm_text or ''))
    halves = re.split(r'(?i)\b(?:and\s+)?will\s+be\s+replaced\s+by\b', text, maxsplit=1)
    if len(halves) != 2:
        return [], ''
    before, after = halves

    # Old titles are every release line after the header. Anchor on the last
    # header line so any greeting above it is ignored; "trumped" / "following
    # torrent" never appear inside a release name, so the match is unambiguous.
    lines = before.split('\n')
    header_idx = -1
    for i, line in enumerate(lines):
        if re.search(r'(?i)trumped|following\s+torrent', line):
            header_idx = i
    # Trailing sentence periods (PMs end the phrase with "."); scene names never
    # end in a bare dot, so stripping one is safe.
    old_titles = [l.strip().rstrip('.').strip()
                  for l in lines[header_idx + 1:] if l.strip()]

    after = re.split(r'(?i)\n\s*reason\s*:', after, maxsplit=1)[0]
    new = ' '.join(l.strip() for l in after.strip().split('\n') if l.strip())

    return old_titles, new.rstrip('.').strip()


_SEASON_EP_RE = re.compile(r'\bs\d{1,2}(?:e\d{1,4})?\b')


def _season_ep_anchor(norm_name):
    """Season/episode anchor token of a normalized release name, or '' — the
    full 's04e01' when an episode is present, else the bare 's04' for a season
    pack, else '' for a movie. Used as a hard gate so a trump match never crosses
    to a different episode or to the season pack itself."""
    m = _SEASON_EP_RE.search(norm_name or '')
    return m.group(0) if m else ''


def _release_group_tag(name):
    """Release-group tag (lowercased, the token after the final hyphen), or '' —
    'A.Movie.2020-GRP' → 'grp'. The encode identity that distinguishes two
    same-episode releases; rejects sentence fragments so a hyphen inside a title
    can't be mistaken for a group."""
    s = str(name or '').strip()
    if '-' not in s:
        return ''
    tag = s.rsplit('-', 1)[-1].strip()
    if not tag or ' ' in tag or len(tag) > 20:
        return ''
    return re.sub(r'[^a-z0-9]', '', tag.lower())


def match_trumped_torrent(rows, title):
    """Find the client torrent matching a trumped release name from the PM.

    Tiered, strongest first: exact normalized match, then PM-tokens-⊆-torrent
    (both inherently can't cross episodes/groups), then a strong-overlap
    fallback for PMs whose rendering differs from the torrent name (e.g. the
    tracker prints "DD+ 5.1" where the torrent says "DDP5.1"). The fallback is
    gated hard on the season/episode anchor and the release group, then ranked
    by token overlap (≥0.6) — it tolerates cosmetic token differences but never
    a different episode or a different encode. Used only for resolving the
    delete group (always user-confirmed), never for the grab.
    """
    target = _norm_release_name(title)
    if not target:
        return None
    t_tokens = set(target.split())

    exact = next((r for r in rows if _norm_release_name(r['name']) == target), None)
    if exact is not None:
        return exact

    subset = next((r for r in rows
                   if t_tokens.issubset(set(_norm_release_name(r['name']).split()))), None)
    if subset is not None:
        return subset

    t_anchor = _season_ep_anchor(target)
    t_group  = _release_group_tag(title)
    best, best_score = None, 0.0
    for r in rows:
        rn = _norm_release_name(r['name'])
        r_tokens = set(rn.split())
        if not r_tokens:
            continue
        # Episode/season must agree when either side declares one
        if (t_anchor or _season_ep_anchor(rn)) and t_anchor != _season_ep_anchor(rn):
            continue
        # Release group must agree when both declare one (the encode identity)
        r_group = _release_group_tag(r['name'])
        if t_group and r_group and t_group != r_group:
            continue
        score = len(t_tokens & r_tokens) / max(len(t_tokens), len(r_tokens))
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= 0.6 else None


def _norm_release_name(name):
    """Normalize a release name for exact comparison: dots/underscores to
    spaces, collapse whitespace, lowercase."""
    return re.sub(r'\s+', ' ', re.sub(r'[._]', ' ', str(name or '').lower())).strip()


def match_trump_release(releases, new_title, indexer=''):
    """Find the replacement release by exact normalized title match.

    Release names include the group tag, so the title is effectively a unique
    id — fuzzy matching would only invite grabbing the wrong release. When
    several indexers carry the same release, the optional indexer filter (or
    the highest seeder count) decides.
    """
    target = _norm_release_name(new_title)
    if not target:
        return None
    pool = [r for r in releases
            if not indexer or (r.get('indexer') or '').lower() == indexer.lower()]
    exact = [r for r in pool if _norm_release_name(r.get('title')) == target]
    if not exact:
        return None
    return max(exact, key=lambda r: r.get('seeders') or 0)


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
        raw = resp.read()
        return json.loads(raw) if raw.strip() else {}


def poll_queue_until_clear(cfg, service, connection_id, arr_id, timeout=300, on_downloading=None):
    """Poll Sonarr/Radarr queue for arr_id until the item clears or timeout (seconds).

    Returns the last seen list of active queue records so the caller can extract
    outputPath for a manual import when the timeout expires with items still present.
    Returns [] when the item cleared cleanly or was never seen.

    Returns early (before timeout) when all active items are in importPending state —
    the download is complete and Radarr is blocking the import; force import is needed
    immediately rather than after a full 300 s wait.
    """
    conns = normalize_arr_connections(cfg, service=service)
    conn  = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        return []
    # Fetch the full queue and filter client-side — the ?movieId= URL param is unreliable
    # across Radarr versions and may silently return empty rather than the full list
    id_field             = 'movieId' if service == 'radarr' else 'seriesId'
    deadline             = time.monotonic() + timeout
    notified             = False
    ever_seen            = False   # did we ever find this item in the queue?
    not_found_ticks      = 0       # consecutive polls with item absent
    import_pending_ticks = 0       # consecutive polls with all items importPending
    last_active          = []      # last snapshot of active records (for outputPath extraction)
    while time.monotonic() < deadline:
        try:
            result   = _arr_get(conn['base_url'], conn['api_key'], '/api/v3/queue?pageSize=500', timeout=10)
            records  = result.get('records', result) if isinstance(result, dict) else result
            relevant = [r for r in records if r.get(id_field) == arr_id]
            # 'completed' means download done but import not yet processed — keep polling
            # until the item fully disappears or hits a hard terminal state.
            # 'warning' is transient (download client temporarily unreachable, etc.)
            # and must NOT be treated as terminal.
            active   = [r for r in relevant if r.get('status') not in ('error', 'failed')]
            if active:
                ever_seen            = True
                last_active          = active
                not_found_ticks      = 0
                if not notified and on_downloading:
                    on_downloading()
                    notified = True
                # If all items are importPending the download is done but Radarr is
                # blocking the import — return early so force import fires immediately
                # instead of waiting the full timeout.
                if all(r.get('trackedDownloadState') == 'importPending' for r in active):
                    import_pending_ticks += 1
                    if import_pending_ticks >= 3:  # ~15 s of confirmed importPending
                        return last_active
                else:
                    import_pending_ticks = 0
            elif relevant:
                return []  # item is only in hard terminal states (error/failed)
            else:
                not_found_ticks += 1
                # Radarr's download-client check interval defaults to ~60 s, so the
                # queue entry may not appear until a full minute after the grab.
                # Wait up to 24 consecutive empty polls (~120 s) before concluding the
                # item was never registered.  Once seen, its absence means processed.
                if ever_seen or not_found_ticks >= 24:
                    return []
        except Exception:
            pass
        time.sleep(5)
    return last_active  # timeout — caller can use outputPath to locate the download


def force_manual_import_by_id(cfg, service, connection_id, arr_id, download_id=None, download_folder=None):
    """Force manual import of a movie or series, bypassing quality cutoff.

    download_id:     the downloadId from the Radarr/Sonarr queue record (qBittorrent hash).
                     When provided it is used for the GET query so the download-client-
                     tracked file is found directly.  This is the correct path for same-
                     quality grabs that sit in importPending state.
    download_folder: fallback — parent directory of outputPath from the queue record.
                     Used when download_id is unavailable.  Falls back further to the
                     media folder for hardlink/backfill cases where the file is already
                     at its final location.
    """
    conns = normalize_arr_connections(cfg, service=service)
    conn  = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        raise ValueError(f"Arr connection '{connection_id}' not found for service '{service}'")

    if service == 'radarr':
        info         = _arr_get(conn['base_url'], conn['api_key'], f'/api/v3/movie/{arr_id}')
        media_folder = info.get('path', '')
        id_param     = f'movieId={arr_id}'
    else:
        info         = _arr_get(conn['base_url'], conn['api_key'], f'/api/v3/series/{arr_id}')
        media_folder = info.get('path', '')
        id_param     = f'seriesId={arr_id}'

    def _query_by_download_id(dl_id):
        try:
            encoded = urllib.parse.quote(dl_id, safe='')
            return _arr_get(conn['base_url'], conn['api_key'],
                            f'/api/v3/manualimport?downloadId={encoded}&{id_param}',
                            timeout=30)
        except urllib.error.HTTPError as e:
            log.warning("manualimport GET by downloadId returned HTTP %s — falling back to folder", e.code)
            return []

    def _query_by_folder(folder):
        try:
            encoded = urllib.parse.quote(folder, safe='')
            return _arr_get(conn['base_url'], conn['api_key'],
                            f'/api/v3/manualimport?folder={encoded}&{id_param}&filterExistingFiles=false',
                            timeout=30)
        except urllib.error.HTTPError as e:
            log.warning("manualimport GET by folder returned HTTP %s", e.code)
            return []

    # Try downloadId first (finds download-client-tracked files), then folder fallbacks.
    files = []
    if download_id:
        files = _query_by_download_id(download_id)
        if files:
            log.info("Found %d importable file(s) for %s %s via downloadId", len(files), service, arr_id)

    if not files:
        folders_to_try = [f for f in [download_folder, media_folder] if f]
        for folder in folders_to_try:
            files = _query_by_folder(folder)
            if files:
                log.info("Found %d importable file(s) for %s %s in %s", len(files), service, arr_id, folder)
                break

    if not files:
        # Retry once after a delay — handles timing race where qBit finishes but files
        # aren't importable yet from Sonarr/Radarr's perspective
        log.info("No importable files for %s %s on first attempt, retrying in 15s…", service, arr_id)
        time.sleep(15)
        if download_id:
            files = _query_by_download_id(download_id)
        if not files:
            for folder in [f for f in [download_folder, media_folder] if f]:
                files = _query_by_folder(folder)
                if files:
                    break

    if not files:
        raise ValueError(f"No importable files found — check {service} queue manually")

    # Use the ManualImport command endpoint with replaceExistingFiles=True.
    # This mirrors what Radarr/Sonarr's "Import Anyway" UI button does and bypasses
    # quality revision checks (e.g. importing a non-RERIP over an existing RERIP),
    # which the /api/v3/manualimport POST endpoint cannot override server-side.
    id_key = 'movieId' if service == 'radarr' else 'seriesId'
    cmd_files = []
    for f in files:
        item = {
            'path':         f['path'],
            id_key:         arr_id,
            'quality':      f.get('quality') or {},
            'languages':    f.get('languages') or [],
            'releaseGroup': f.get('releaseGroup') or '',
            'downloadId':   f.get('downloadId') or download_id or '',
            'rejections':   [],
        }
        if service == 'sonarr':
            # Sonarr needs episode context so it can assign the file correctly
            episodes = f.get('episodes') or []
            item['episodeIds']    = [ep['id'] for ep in episodes if ep.get('id')]
            item['seasonNumber']  = f.get('seasonNumber', 0)
        cmd_files.append(item)
    cmd_body = {
        'name':                 'ManualImport',
        'files':                cmd_files,
        'replaceExistingFiles': True,
        'importMode':           'Auto',
    }
    req = urllib.request.Request(
        conn['base_url'].rstrip('/') + '/api/v3/command',
        data=json.dumps(cmd_body).encode(),
        headers={'X-Api-Key': conn['api_key'], 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            log.info("ManualImport command returned %s for %s %s — likely already imported", e.code, service, arr_id)
            return []
        raise


def get_arr_file_id(cfg, service, connection_id, arr_id):
    """Return the current file ID for a Radarr movie or Sonarr series episode files.

    Used to detect whether a ManualImport command actually replaced the file,
    since the command endpoint may report status='failed' even on success.
    Returns None if unavailable.
    """
    conns = normalize_arr_connections(cfg, service=service)
    conn  = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        return None
    try:
        if service == 'radarr':
            info = _arr_get(conn['base_url'], conn['api_key'], f'/api/v3/movie/{arr_id}', timeout=10)
            return info.get('movieFileId')
        else:
            # For Sonarr track the set of episode file IDs as a frozen snapshot
            eps = _arr_get(conn['base_url'], conn['api_key'],
                           f'/api/v3/episodefile?seriesId={arr_id}', timeout=10)
            return frozenset(e['id'] for e in eps if e.get('id'))
    except Exception:
        return None


def remove_from_arr_queue(cfg, service, connection_id, queue_id):
    """Delete a queue item from Arr without removing from the download client."""
    conns = normalize_arr_connections(cfg, service=service)
    conn  = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None or not queue_id:
        return
    req = urllib.request.Request(
        conn['base_url'].rstrip('/') + f'/api/v3/queue/{queue_id}?removeFromClient=false&blocklist=false',
        headers={'X-Api-Key': conn['api_key']},
        method='DELETE',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info("Removed queue item %s from %s queue", queue_id, service)
            return resp.status
    except Exception as e:
        log.warning("Failed to remove queue item %s from %s: %s", queue_id, service, e)


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


# Release-name quality detection — order matters (remux outranks bluray; the
# bare WEB tag is resolved to webdl). Patterns run against dot/underscore-
# normalized names so word boundaries are reliable.
_RES_NAME_PATTERNS = [
    ('2160p', r'\b(2160p|4k|uhd)\b'),
    ('1080p', r'\b1080[pi]\b'),
    ('720p',  r'\b720p\b'),
    ('480p',  r'\b(480p|sdtv)\b'),
]
_SOURCE_NAME_PATTERNS = [
    ('remux',  r'\bremux\b'),
    ('bluray', r'\b(blu[- ]?ray|bdrip|brrip|bd(25|50|66|100))\b'),
    ('webdl',  r'\bweb[- ]?dl\b'),
    ('webrip', r'\bweb[- ]?rip\b'),
    ('webdl',  r'\bweb\b'),
    ('hdtv',   r'\bhdtv\b'),
    ('dvd',    r'\b(dvdrip|dvd)\b'),
]
_SOURCE_DISPLAY = {
    'remux': 'Remux', 'bluray': 'Bluray', 'webdl': 'WEB-DL',
    'webrip': 'WEBRip', 'hdtv': 'HDTV', 'dvd': 'DVD',
}
_RES_RANK    = {'480p': 1, '720p': 2, '1080p': 3, '2160p': 4}
_SOURCE_RANK = {'dvd': 1, 'hdtv': 2, 'webrip': 3, 'webdl': 4, 'bluray': 5, 'remux': 6}


def parse_quality_name(quality_name):
    """Extract (resolution, source) labels from an arr quality name like 'Bluray-1080p'."""
    name = str(quality_name or '')
    resolution = next((label for label, pat in _RES_NAME_PATTERNS
                       if re.search(pat, name, re.IGNORECASE)), '')
    source = next((label for label, pat in _SOURCE_NAME_PATTERNS
                   if re.search(pat, name, re.IGNORECASE)), '')
    return resolution, source


def _effective_res_rank(resolution, source):
    """Resolution rank, inferring SD when only a DVD source tag is present —
    'DVDRip' releases and the arr quality name 'DVD' carry no resolution token
    but are 480p-class by definition."""
    rank = _RES_RANK.get(resolution, 0)
    if not rank and source == 'dvd':
        rank = _RES_RANK['480p']
    return rank


def compare_release_quality(parsed, lib_quality_name):
    """Compare a parsed torrent release against the library file's arr quality name.

    Resolution decides; source (Remux > Bluray > WEB-DL > WEBRip > HDTV > DVD)
    breaks resolution ties. Returns 'higher' | 'same' | 'lower' | 'unknown' —
    'unknown' whenever the deciding field is missing on either side, because a
    bucket that suggests "safe to delete" must not guess.
    """
    lib_res, lib_src = parse_quality_name(lib_quality_name)
    t_res = _effective_res_rank(parsed.get('resolution'), parsed.get('source'))
    l_res = _effective_res_rank(lib_res, lib_src)
    if not t_res or not l_res:
        return 'unknown'
    if t_res != l_res:
        return 'higher' if t_res > l_res else 'lower'
    t_src = _SOURCE_RANK.get(parsed.get('source'), 0)
    l_src = _SOURCE_RANK.get(lib_src, 0)
    if not t_src or not l_src:
        return 'same'
    if t_src != l_src:
        return 'higher' if t_src > l_src else 'lower'
    return 'same'


def _parse_year_from_name(name):
    """Release year from a space-normalized release name, or None.

    Takes the LAST plausible year token so titles that are themselves years
    survive ("2012 2009 1080p" → 2009). Tokens outside 1900..now+2 are
    ignored, which also skips title numbers like "Blade Runner 2049".
    """
    cutoff = time.gmtime().tm_year + 2
    years = [int(y) for y in re.findall(r'\b((?:19|20)\d{2})\b', name)
             if 1900 <= int(y) <= cutoff]
    return years[-1] if years else None


def parse_release_info(path):
    """Parse title, season/episode, year, and quality hints from a release file or folder name.

    Returns {'title', 'season', 'episode', 'year', 'resolution', 'source',
    'hdr', 'quality_label'} — empty strings / None for anything not detected.
    """
    base = os.path.basename(str(path or '').replace('\\', '/').rstrip('/'))
    stem = os.path.splitext(base)[0]
    name = re.sub(r'[._]', ' ', stem)

    se = re.search(r'[Ss](\d{1,2})[Ee](\d{1,3})', base)
    season  = int(se.group(1)) if se else None
    episode = int(se.group(2)) if se else None
    if season is None:
        # Season-pack folders: "Show S03 1080p ..." with no episode marker
        sp = re.search(r'\b[Ss](\d{1,2})\b(?!\s*[Ee])', name)
        if sp:
            season = int(sp.group(1))

    resolution = next((label for label, pat in _RES_NAME_PATTERNS
                       if re.search(pat, name, re.IGNORECASE)), '')
    source = next((label for label, pat in _SOURCE_NAME_PATTERNS
                   if re.search(pat, name, re.IGNORECASE)), '')
    hdr = _detect_hdr(base)
    quality_label = ' '.join(x for x in (resolution, _SOURCE_DISPLAY.get(source, '')) if x)
    return {
        'title':         _parse_title_from_filename(base),
        'season':        season,
        'episode':       episode,
        'year':          _parse_year_from_name(name),
        'resolution':    resolution,
        'source':        source,
        'hdr':           hdr,
        'quality_label': quality_label,
    }


def parse_release_info_for_path(rel_path):
    """Parse a relative torrent path, merging release-folder hints into the file name's.

    Episode files inside a season-pack folder often lack quality tags the
    folder name carries ("Show S03 1080p BluRay/Show S03E01.mkv") — fields the
    file name leaves blank are filled from the top-level folder.
    """
    norm = str(rel_path or '').replace('\\', '/')
    parsed = parse_release_info(norm)
    if '/' in norm:
        folder = parse_release_info(norm.split('/')[0])
        for key in ('resolution', 'source', 'hdr', 'title'):
            if not parsed[key]:
                parsed[key] = folder[key]
        if parsed['season'] is None:
            parsed['season'] = folder['season']
        if parsed['year'] is None:
            parsed['year'] = folder['year']
        parsed['quality_label'] = ' '.join(
            x for x in (parsed['resolution'], _SOURCE_DISPLAY.get(parsed['source'], '')) if x)
    return parsed


_arr_titles_cache = {'data': None, 'ts': 0}
_ARR_TITLES_TTL = 120


def fetch_arr_all_titles(cfg, force=False):
    """Every managed title across all Arr instances, including items without files.

    The media index only contains items that HAVE files — this list is what lets
    Triage distinguish "in the library but never imported" from "not in the
    library at all". Returns [{service, connection_id, arr_id, title,
    title_slug, year, has_file}].
    """
    now = time.monotonic()
    if not force and _arr_titles_cache['data'] is not None and (now - _arr_titles_cache['ts']) < _ARR_TITLES_TTL:
        return _arr_titles_cache['data']
    rows = []
    for conn in normalize_arr_connections(cfg):
        try:
            if conn['service'] == 'radarr':
                for m in _arr_get(conn['base_url'], conn['api_key'], '/api/v3/movie'):
                    rows.append({
                        'service':       'radarr',
                        'connection_id': conn['id'],
                        'arr_id':        m.get('id'),
                        'title':         m.get('title') or '',
                        'title_slug':    m.get('titleSlug') or '',
                        'year':          m.get('year'),
                        'has_file':      bool(m.get('hasFile')),
                    })
            else:
                for s in _arr_get(conn['base_url'], conn['api_key'], '/api/v3/series'):
                    stats = s.get('statistics') or {}
                    rows.append({
                        'service':       'sonarr',
                        'connection_id': conn['id'],
                        'arr_id':        s.get('id'),
                        'title':         s.get('title') or '',
                        'title_slug':    s.get('titleSlug') or '',
                        'year':          s.get('year'),
                        'has_file':      (stats.get('episodeFileCount') or 0) > 0,
                    })
        except Exception as e:
            log.warning("Could not fetch %s titles from %s: %s", conn['service'], conn['id'], e)
    _arr_titles_cache['data'] = rows
    _arr_titles_cache['ts'] = now
    return rows


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
    # For movies: strip the release year and everything after — but only a
    # plausible year (1900..now+2, so "Blade Runner 2049" keeps its number)
    # and never the leading token (year-titled films like "1917" or "2046")
    cutoff = time.gmtime().tm_year + 2
    for m in re.finditer(r'\b((?:19|20)\d{2})\b', name):
        if m.start() > 0 and 1900 <= int(m.group(1)) <= cutoff:
            name = name[:m.start()]
            break
    # Strip quality/format tags and everything after — \s+ anchor ensures the
    # tag is a standalone token, preventing mid-word matches (e.g. "Internal" in
    # "Internal Affairs" or "4K" in "The 4K Experience"); the lookahead requires
    # the tag to END at a token boundary too ("MA" must not eat "Machine")
    name = re.sub(
        r'\s+(2160p|1080p|1080i|720p|480p|4K|BluRay|BDRip|BRRip|WEB-DL|WEBRip|HDTV|DVDRip|'
        r'AMZN|DSNP|NF|HULU|HBO|x264|x265|HEVC|HDR|DV|AAC|DDP|DTS|MA|FLAC|REMUX|PROPER|REPACK|INTERNAL)'
        r'(?=[\s)\]]|$).*$',
        '', name, flags=re.IGNORECASE,
    )
    # Collapse multiple spaces and strip, then drop dangling separators left
    # by the year/episode splits — "The Lion King (1994)" splits to "The Lion King ("
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'[\s(\[\-–—]+$', '', name)
    return name


def _normalize_title(title):
    """Lowercase and strip punctuation for fuzzy title matching."""
    t = title.lower()
    t = re.sub(r'[^\w\s]', ' ', t)  # replace punctuation with space
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _ascii_fold(title):
    """Strip diacritics so 'Žižek' matches the ASCII 'Zizek' a release name uses."""
    return unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode()


def title_match_keys(title):
    """All normalized lookup keys a title should match under.

    Scene names handle apostrophes two ways — replaced by a separator
    (Widow.s.Bay) or dropped entirely (Widows.Bay) — so "Widow's Bay" must
    index/look up as both 'widow s bay' and 'widows bay'. Diacritics get the
    same treatment: arr titles keep them ("Žižek!") while release names are
    ASCII ("zizek"), so an ASCII-folded variant of every key is added too.
    """
    if not title:
        return set()
    variants = {title, re.sub(r"['’`]", '', title)}
    variants |= {_ascii_fold(v) for v in variants}
    keys = {_normalize_title(v) for v in variants}
    keys.discard('')
    return keys


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
