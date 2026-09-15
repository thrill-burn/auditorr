import os
import re
import json
import logging
import time
import functools
import unicodedata
from types import MappingProxyType
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

# One immutable snapshot, `(ts, rows, errors, roots)`, published in a single
# assignment — never four (S11). See `fetch_arr_media_index_result`.
_arr_media_index_cache = {'snapshot': None}
_ARR_MEDIA_INDEX_TTL = 120
# Bulk list endpoints (/api/v3/movie, /api/v3/series) return the entire library
# in one response — seconds of JSON on a large instance, where the 10s default
# that suits per-item calls is thin. Timing out here is not a visible error: the
# connection simply contributes no rows, and every file it manages stops
# resolving as though the instance were never configured.
_ARR_LIST_TIMEOUT = 60

# Service config map: url_key, api_key_key, remote_path_key, command, list_path, slug_prefix, display_name
_SERVICE_MAP = {
    'sonarr': {
        'url_key':      'SONARR_URL',
        'external_key': 'SONARR_EXTERNAL_URL',
        'key_key':      'SONARR_API_KEY',
        'remote_key':   'SONARR_REMOTE_PATH',
        'command':      'DownloadedEpisodesScan',
        'list_path':    '/api/v3/series',
        'slug_prefix':  '/series/',
        'name':         'Sonarr',
    },
    'radarr': {
        'url_key':      'RADARR_URL',
        'external_key': 'RADARR_EXTERNAL_URL',
        'key_key':      'RADARR_API_KEY',
        'remote_key':   'RADARR_REMOTE_PATH',
        'command':      'DownloadedMoviesScan',
        'list_path':    '/api/v3/movie',
        'slug_prefix':  '/movie/',
        'name':         'Radarr',
    },
}


def link_base(conn):
    """The address to put in front of a *browser* link for this connection.

    Presentation only. This is the reverse-proxy-facing address when one is
    configured, which the server must never fetch — an auth proxy or an
    external-only DNS name in front of it would break API calls that the
    internal address serves fine.

    That separation is kept structural rather than by convention: _arr_get and
    _arr_command take a base_url *string* positionally and are never handed a
    connection dict, so there is no call site where this value could be
    mistaken for the API address. Keep it that way.
    """
    if not isinstance(conn, dict):
        return ''
    external = str(conn.get('external_url') or '').strip()
    return (external or str(conn.get('base_url') or '')).rstrip('/')


def _endpoint_key(base_url):
    """Comparison key for "the same server" — scheme, case and trailing slash
    folded, so http/https against one host:port is one instance, not two."""
    return re.sub(r'^https?://', '', str(base_url or '').strip().rstrip('/').lower())


def normalize_arr_connections(cfg, service=None):
    """Return normalized Sonarr/Radarr connection records.

    The legacy singleton keys (SONARR_URL / RADARR_URL) and the ARR_CONNECTIONS
    list are **merged, not chosen between**. ARR_CONNECTIONS used to *replace*
    the singletons: adding a single extra instance silently retired the primary
    Sonarr and Radarr, while their fields stayed on the Config page, kept
    saving, and kept passing their own connection test. Nothing read them, so
    an entire library became invisible to every consumer of this function —
    Backfill candidates, Triage's library comparison, indexers, deep links,
    rescan, force import, trump (#22). The UI has always called the list
    "Additional Sonarr/Radarr instances", which is what they now are.

    A singleton is dropped when an explicit entry already covers it — the same
    id, or the same service pointed at the same base_url — so an install that
    migrated by copying its primary into the list does not index it twice.
    """
    explicit = []
    for raw in (cfg.get('ARR_CONNECTIONS') or []):
        if not isinstance(raw, dict):
            continue
        conn = _normalize_arr_connection(raw)
        if conn and (service is None or conn['service'] == service):
            explicit.append(conn)

    # Only user-authored ids can genuinely collide, and a collision there is a
    # config error worth refusing. A singleton that clashes is ours to resolve,
    # and is skipped below rather than raising.
    seen_ids = set()
    for conn in explicit:
        if conn['id'] in seen_ids:
            raise ValueError(f"Duplicate Arr connection id: {conn['id']}")
        seen_ids.add(conn['id'])
    covered = {(c['service'], _endpoint_key(c['base_url'])) for c in explicit}

    legacy = []
    for svc_name, svc in _SERVICE_MAP.items():
        if service is not None and svc_name != service:
            continue
        url = str(cfg.get(svc['url_key'], '')).strip()
        api_key = str(cfg.get(svc['key_key'], '')).strip()
        if not url or not api_key:
            continue
        conn = _normalize_arr_connection({
            'id': f'{svc_name}-default',
            'service': svc_name,
            'name': svc['name'],
            'base_url': url,
            'external_url': cfg.get(svc['external_key'], ''),
            'api_key': api_key,
            'remote_path': cfg.get(svc['remote_key'], ''),
        })
        if conn is None:
            continue
        if conn['id'] in seen_ids or (conn['service'], _endpoint_key(conn['base_url'])) in covered:
            continue
        seen_ids.add(conn['id'])
        legacy.append(conn)

    # Primary first, matching the Config page's own order. Callers that fall
    # back to "the first connection" for a link base (acquire_candidates' /add/new
    # search) should land on the main instance, not whichever extra library
    # happens to sit at the top of the list.
    return legacy + explicit


def _fetch_root_folders(conn):
    """One arr's configured root folders — arr-side paths, longest first — or None.

    None is "could not read them", kept apart from `[]` ("none configured"). A
    failure here never fails the media index: the chips it feeds are a filter,
    and a missing filter must not cost the library it filters.
    """
    try:
        rows = _arr_get(conn['base_url'], conn['api_key'], '/api/v3/rootfolder')
    except Exception as e:
        log.warning("Could not fetch root folders from %s: %s", conn['id'], e)
        return None
    if not isinstance(rows, list):
        return None
    paths = {_path_norm(r.get('path')).rstrip('/') for r in rows
             if isinstance(r, dict) and r.get('path')}
    return sorted((p for p in paths if p), key=lambda p: (-len(p), p))


def _arr_media_index_snapshot(cfg, force=False):
    """`(ts, rows, errors, roots)` — the media index as one immutable snapshot (S11).

    Built from every configured arr and published with **one** assignment.
    Results are cached for _ARR_MEDIA_INDEX_TTL seconds; force=True bypasses.
    Each instance's root folders are read alongside (`arr_root_folders`).
    """
    now = time.monotonic()
    snap = _arr_media_index_cache.get('snapshot')
    if not force and snap is not None and (now - snap[0]) < _ARR_MEDIA_INDEX_TTL:
        return snap
    media = []
    errors = []
    roots = {}
    for conn in normalize_arr_connections(cfg):
        try:
            if conn['service'] == 'radarr':
                rows, partial = _fetch_radarr_media(conn)
            else:
                rows, partial = _fetch_sonarr_media(conn)
            media.extend(_apply_arr_media_path_mapping(rows, conn, cfg))
            # Only for an instance that answered — a dead one would only add a
            # second timeout. Per connection, never per row: one small list.
            roots[conn['id']] = _fetch_root_folders(conn)
            if partial:
                # Some of the library came back. Reported on the same channel as
                # a total failure because the consequence is the same shape — an
                # unexplained gap the caller would otherwise read as "manages
                # nothing" — and `partial` lets the UI say which it was.
                errors.append({'connection_id': conn['id'], 'name': conn['name'],
                               'service': conn['service'], 'partial': True,
                               'failed': partial['failed'], 'total': partial['total'],
                               'message': f"{partial['failed']} of {partial['total']} "
                                          f"series could not be read"})
        except Exception as e:
            log.warning("Could not fetch %s media from %s: %s", conn['service'], conn['id'], e)
            errors.append({'connection_id': conn['id'], 'name': conn['name'],
                           'service': conn['service'], 'partial': False,
                           'message': str(e)})
    snap = (now, media, tuple(errors), roots)
    _arr_media_index_cache['snapshot'] = snap
    return snap


def fetch_arr_media_index(cfg, force=False):
    """Managed media-file rows from every configured Arr instance (cached 120s).

    Rows only. **A caller that reports this fetch's failures uses
    `fetch_arr_media_index_result`**, which returns the rows and their errors
    from one snapshot.
    """
    return _arr_media_index_snapshot(cfg, force)[1]


def fetch_arr_media_index_result(cfg, force=False):
    """`(rows, errors)` for the media index, **from one snapshot** (S11).

    The accessors below read the *most recent* snapshot, which is not
    necessarily the one a caller received. auditorr runs one gunicorn worker
    with eight threads, so another request's successful refresh can land
    between a failed fetch and its `arr_media_index_errors()` read. That request
    then holds `[]` rows and **no errors** — the state Triage's `library_unknown`
    gate exists to catch, read as healthy, and `not_in_library` reachable from
    silence again. The old contract ("call the fetch, then the accessor, in the
    same request") was sequential, and said nothing about another request. The
    snapshot is a tuple published in one assignment, so rows and errors taken
    from it cannot come from two fetches.

    Errors carry `partial`: False is "this instance answered with nothing",
    True is "this instance answered with some of its library" (`failed`/`total`
    series). Both leave the same hole; only the second is recoverable by
    retrying a moment later.
    """
    snap = _arr_media_index_snapshot(cfg, force)
    return snap[1], list(snap[2])


def arr_root_folders():
    """`{connection_id: [root, ...] | None}` from the most recent index snapshot (B10).

    Arr-side paths as configured in Sonarr/Radarr, longest first. `None` for an
    instance whose root folders could not be read; an instance whose media index
    failed has no entry at all. **The most recent snapshot**, which under
    concurrent requests need not be the one the caller's own fetch returned —
    see `fetch_arr_media_index_result`. Backfill still reads it straight after
    its fetch; an interleaving costs it a folder chip, never a verdict.
    """
    snap = _arr_media_index_cache.get('snapshot')
    return dict(snap[3]) if snap else {}


def arr_media_index_errors():
    """Connections whose media index failed or came back partial, **most recent snapshot**.

    The index is a flat list of rows, so an instance that errored is
    indistinguishable from one that manages nothing — its files just stop
    resolving. Anything that presents resolution results reads the failures so
    they are reported rather than inferred from an unexplained gap.

    **This reads whichever fetch landed last**, and under concurrent requests
    that need not be the caller's (S11). Anything that turns these errors into
    a verdict takes them from `fetch_arr_media_index_result` instead — Triage
    and Trumped do. Backfill still reads this straight after its fetch, where
    an interleaving can hide a warning banner and nothing more; switching it was
    not mechanical (the fetch sits inside `_resolve_backfill`, read by two
    callers) and is recorded rather than done.
    """
    snap = _arr_media_index_cache.get('snapshot')
    return list(snap[2]) if snap else []


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


def season_episodes_from_name(name):
    """(season, [episode numbers]) parsed from a release basename, or (None, []).

    Two things the old two-digit pattern got wrong, both silently:

    * **Three-digit episodes.** `[Ee](\\d{1,2})` against `S01E120` matched `E12`
      and returned episode **12** — a confident wrong answer rather than no
      answer, which then resolved to a real but unrelated episode id.
      `(?!\\d)` makes the match refuse a truncation instead.
    * **Multi-episode files.** `S01E01E02` / `S01E01-E02` is one file holding
      two episodes; only the first was ever seen, so a lookup that failed on it
      failed outright.

    The bare `S01E01-02` form is deliberately **not** parsed: without a literal
    `E` the trailing number is indistinguishable from a quality token
    (`S01E01-720p`), and inventing episode 720 is worse than missing one.
    """
    base = os.path.basename(str(name or '').replace('\\', '/').rstrip('/'))
    m = re.search(r'[Ss](\d{1,2})[Ee](\d{1,3})(?!\d)', base)
    if not m:
        return None, []
    nums = [int(m.group(2))]
    tail = base[m.end():]
    while True:
        more = re.match(r'[-._ ]*[Ee](\d{1,3})(?!\d)', tail)
        if not more:
            break
        nums.append(int(more.group(1)))
        tail = tail[more.end():]
    return int(m.group(1)), nums


def _episode_id_from_path(conn, arr_id, file_path):
    """Derive a Sonarr episode ID by parsing SxxExx from file_path and matching against the series."""
    season, ep_nums = season_episodes_from_name(file_path)
    if season is None:
        return None
    try:
        episodes = _arr_get(conn['base_url'], conn['api_key'], f'/api/v3/episode?seriesId={arr_id}')
    except Exception as e:
        log.warning("Could not look up episode from path %s: %s", file_path, e)
        return None
    # First episode of the file that the series actually knows about — a
    # multi-episode file searches on whichever of its episodes resolves.
    by_num = {ep.get('episodeNumber'): ep.get('id') for ep in episodes
              if ep.get('seasonNumber') == season}
    return next((by_num[n] for n in ep_nums if by_num.get(n) is not None), None)


def sonarr_episodes_by_file(cfg, connection_id, series_id):
    """`{episode_file_id: [(episode_id, season, episode), ...]}` for one series, or None.

    The only authoritative route from a library file to the episodes it holds.
    `/api/v3/episodefile` does not carry them — Sonarr's `EpisodeFileResource`
    has a `seasonNumber` and **no** episode ids or numbers — so the
    `episode_ids` / `episode_numbers` `_fetch_sonarr_media` reads off that record
    are always empty against a real Sonarr, and every fixture that populated
    them was testing a field that does not exist. `/api/v3/episode?seriesId=`
    does carry `episodeFileId`, and that join also works for daily and
    absolute-numbered series, where parsing the filename does not.

    `None` is "could not ask", kept distinct from `{}` ("this series holds no
    files") for the reason R1 records: both Backfill consumers narrow a search or
    an import by this answer, and an unknown read as "no episodes" would narrow
    it to nothing silently.
    """
    conns = normalize_arr_connections(cfg, service='sonarr')
    conn = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        return None
    try:
        episodes = _arr_get(conn['base_url'], conn['api_key'],
                            f'/api/v3/episode?seriesId={series_id}')
    except Exception as e:
        log.warning("Could not list episodes for series %s on %s: %s", series_id, connection_id, e)
        return None
    out = {}
    for ep in episodes or []:
        file_id = ep.get('episodeFileId')
        if file_id and ep.get('id') is not None:
            out.setdefault(file_id, []).append(
                (ep['id'], ep.get('seasonNumber'), ep.get('episodeNumber')))
    for eps in out.values():
        eps.sort(key=lambda e: (e[1] if e[1] is not None else -1, e[2] if e[2] is not None else -1))
    return out


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
            # Sonarr only: what the release covers. An episode search can still
            # return a season pack or a multi-episode file — interactive search
            # lists rejected releases alongside approved ones, and a grab
            # through /api/v3/release bypasses the rejection — so Backfill's
            # scope gate has to see this to keep a pack off an episode row (B1).
            'full_season':         bool(r.get('fullSeason')),
            # Mapped first: `episodeNumbers` is parsed off the title in scene
            # numbering, `mappedEpisodeNumbers` is the series' own numbering —
            # the one an episode-file join yields.
            'episode_numbers':     list(r.get('mappedEpisodeNumbers') or r.get('episodeNumbers') or []),
        })
    return result


# The phrase separating the trumped release(s) from the replacement. "(and) will
# be replaced by" is the one layout on record from the field — three real PMs,
# one tracker's automated template (QA-5, 2026-09-13). The others are in
# circulation on other trackers and returned ([], '') here, dropping the user
# into manual entry for want of an alternation; none of them is backed by a real
# PM yet. Leftmost match wins, so "will be replaced by" is never cut short to
# its trailing "replaced by".
_TRUMP_DELIMITER_RE = re.compile(
    r'(?i)\b(?:(?:and\s+)?will\s+be\s+replaced\s+by'
    r'|has\s+been\s+trumped\s+by'
    r'|(?:has\s+been\s+)?superseded\s+by'
    r'|(?:has\s+been\s+)?replaced\s+(?:with|by))\b')


def parse_trump_pm(pm_text):
    """Extract (old_titles, new_title) from a tracker trump PM.

    A PM lists one or more trumped releases between the "...trumped" /
    "following torrent" header and the delimiter phrase, then the single
    replacement (typically a season pack when several episodes are trumped
    together). Returns ([], '') when no delimiter phrase is present — the UI
    falls back to manual fields. The old side is a list to cover season-pack
    trumps (N episodes → 1 pack); a single-release trump just yields a
    one-element list.

    **A release name is one line** (TR6). The new title is the first non-empty
    line after the delimiter, and nothing after it: the only terminator used to
    be a literal `Reason:` line, so a PM without one — or with a sign-off on the
    next line — had its boilerplate joined into the title, across blank lines.
    That title then fails the exact release match by construction, and its junk
    tokens drag every real candidate's title similarity below the gate, so a
    PM that parsed "perfectly" produced an empty candidate list.
    """
    text = re.sub(r'\r\n?', '\n', str(pm_text or ''))
    halves = _TRUMP_DELIMITER_RE.split(text, maxsplit=1)
    if len(halves) != 2:
        return [], ''
    before, after = halves

    def _clean(line):
        # Trailing sentence periods (PMs end the phrase with "."); scene names
        # never end in a bare dot, so stripping one is safe.
        return line.strip().rstrip('.').strip()

    # Old titles are every release line after the header. Anchor on the last
    # header line so any greeting above it is ignored; "trumped" / "following
    # torrent" never appear inside a release name, so the match is unambiguous.
    lines = before.split('\n')
    header_idx = -1
    for i, line in enumerate(lines):
        if re.search(r'(?i)trumped|following\s+torrent', line):
            header_idx = i
    if header_idx >= 0:
        old_titles = [_clean(l) for l in lines[header_idx + 1:] if l.strip()]
    else:
        # No header: only the line immediately above the delimiter. Taking every
        # line made "Hi there," a trumped release that phase 1 then ranked every
        # torrent in the client against.
        tail = [l for l in lines if l.strip()]
        old_titles = [_clean(tail[-1])] if tail else []

    new = next((l.strip().lstrip(':').strip() for l in after.split('\n')
                if l.strip().lstrip(':').strip()), '')
    if re.match(r'(?i)reason\s*:', new):
        new = ''
    return old_titles, _clean(new)


_SEASON_EP_RE = re.compile(r'\bs\d{1,2}(?:e\d{1,4})?\b')


def _season_ep_anchor(norm_name):
    """Season/episode anchor token of a normalized release name, or '' — the
    full 's04e01' when an episode is present, else the bare 's04' for a season
    pack, else '' for a movie. Used as a hard gate so a trump match never crosses
    to a different episode or to the season pack itself."""
    m = _SEASON_EP_RE.search(norm_name or '')
    return m.group(0) if m else ''


# A real file extension, for stripping one off a release name.
#
# Deliberately an explicit list rather than `os.path.splitext`, which takes
# everything after the final dot: `splitext('Some.Movie.2020')` returns
# ('Some.Movie', '.2020'), so a dot-separated release name loses its **year** to
# an extension that does not exist. Release names are dot-separated by
# convention, so this is the normal case, not an edge one.
_FILE_EXT_RE = re.compile(
    r'\.(mkv|mp4|m4v|avi|mov|wmv|webm|ts|m2ts|mpg|mpeg|iso'
    r'|srt|sub|idx|ass|ssa|vtt|nfo)$', re.I)


def _strip_file_ext(name):
    """Drop a trailing real file extension from a release file or folder name."""
    return _FILE_EXT_RE.sub('', str(name or '').strip())


# Container extensions an arr imports as video — dotted, lowercase. What tells
# an unmatched *video* (usually a path-mapping mismatch, and worth acting on)
# from an unmatched sidecar (subtitles, artwork, .nfo — which no arr indexes)
# in Backfill's resolution readout (B9). Triage's narrower `_VIDEO_EXTS` in
# app.py predates this and is Phase 9's to reconcile.
VIDEO_EXTENSIONS = frozenset({
    '.mkv', '.mk3d', '.mp4', '.m4v', '.avi', '.mov', '.qt', '.wmv', '.asf',
    '.mpg', '.mpeg', '.m2v', '.ts', '.m2ts', '.mts', '.wtv', '.vob', '.iso',
    '.webm', '.flv', '.ogm', '.ogv', '.divx', '.xvid', '.rm', '.rmvb', '.3gp',
    '.dvr-ms',
})


def _release_group_tag(name):
    """Release-group tag (lowercased, the token after the final hyphen), or '' —
    'A.Movie.2020-GRP' → 'grp'. The encode identity that distinguishes two
    same-episode releases; rejects sentence fragments so a hyphen inside a title
    can't be mistaken for a group.

    A trailing hyphen is not proof of a group: the two commonest source tokens
    carry one. 'Show.S01E01.1080p.AMZN.WEB-DL' yielded 'dl' and
    'Movie 2020 2160p Blu-Ray' yielded 'ray', which is why quality tokens are
    rejected here. In `match_trumped_torrent`'s overlap tier a mismatched group
    is a hard `continue`, so a PM that named its group disqualified the client's
    copy of the same payload outright whenever that copy was named without one.
    The inverse was quieter and worse: two groupless names both reduced to 'dl',
    which `score_release_match` then scored as a group *agreement* they never
    had.
    """
    s = _strip_file_ext(name)
    if '-' not in s:
        return ''
    tag = s.rsplit('-', 1)[-1].strip()
    if not tag or ' ' in tag or len(tag) > 20:
        return ''
    tag = re.sub(r'[^a-z0-9]', '', tag.lower())
    return '' if tag in _QUALITY_NOISE else tag


def match_trumped_torrent(rows, title):
    """Find the client torrent matching a trumped release name from the PM.

    Tiered, strongest first: exact normalized match, then PM-tokens-⊆-torrent
    (both inherently can't cross episodes/groups), then a strong-overlap
    fallback for PMs whose rendering differs from the torrent name (e.g. the
    tracker prints "DD+ 5.1" where the torrent says "DDP5.1"). The fallback is
    gated hard on the season/episode anchor, the release group, the title core
    and the year, then ranked by token overlap (≥0.6) — it tolerates cosmetic
    token differences but never a different episode, encode, or title. Used
    only for resolving the delete group (always user-confirmed), never for the
    grab.

    The title gate is the same one `score_release_match` applies, and it is the
    load-bearing one: two unrelated releases in the same format share almost
    all of their *quality* tokens ("2160p UHD BluRay TrueHD 7.1 Atmos x265"
    from one group), which on its own sails past a 0.6 overlap bar and offers a
    stranger's movie up for deletion.
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
    t_core   = _title_core_tokens(target, t_group)
    t_year   = _parse_year_from_name(target)
    if not t_core:
        return None
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
        # Title must actually overlap — quality tokens are not identity
        r_core = _title_core_tokens(rn, r_group)
        if not r_core or len(t_core & r_core) / len(t_core | r_core) < _MIN_TITLE_SIM:
            continue
        # Year must agree within ±1 (premiere vs wide-release rendering), so a
        # same-title remake from the same group can't be swapped in
        r_year = _parse_year_from_name(rn)
        if t_year and r_year and abs(t_year - r_year) > 1:
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

    The *indexer* comparison is the one place fuzziness is right, and it is the
    same `tracker_matches_indexer` the rest of the flow uses: indexer names are
    pooled across every arr by `fetch_arr_indexers`, so one tracker can arrive
    as "Aither (API) (Prowlarr)" on one instance and "Aither" on another, and
    exact equality fell through to the any-indexer retry.
    """
    target = _norm_release_name(new_title)
    if not target:
        return None
    pool = [r for r in releases
            if not indexer or tracker_matches_indexer(r.get('indexer'), indexer)]
    exact = [r for r in pool if _norm_release_name(r.get('title')) == target]
    if not exact:
        return None
    return max(exact, key=lambda r: r.get('seeders') or 0)


# ── Graded release matching (Trumped candidate lists) ───────────────────────
# match_trumped_torrent / match_trump_release above pick one confident result or
# nothing. When nothing matches — a PM that renders a token differently than the
# client/indexer (DD+ vs DDP), a typo, a missing release — the Trumped wizard
# shows a ranked candidate list the user picks from instead of dead-ending.
#
# The title is a REQUIRED soft match, then quality fields rank within it. The
# title must actually overlap and the year (±1) / episode must agree; only then
# do resolution/source/group/audio/HDR refine the ranking. Quality agreement alone
# never makes a match — otherwise two unrelated 1080p WEB-DLs look like siblings
# (the "Flow 2019" shown for "Obsession 2026" bug). When the real torrent isn't
# in the client the list comes back empty, which the picker reports honestly,
# rather than offering a confident wrong answer.

_AUDIO_PATTERNS = [
    ('truehd', r'true ?hd'),
    ('dtshd',  r'dts[ .]?hd'),
    ('dts',    r'\bdts'),
    ('ddp',    r'\b(?:ddp|dd\+|e[ -]?ac3|eac3)'),
    ('dd',     r'\b(?:dd|ac3)\b'),
    ('aac',    r'\baac'),
    ('flac',   r'\bflac'),
    ('opus',   r'\bopus'),
]


def _audio_codec(name):
    """Primary audio-codec family of a release name, normalized across the many
    renderings of one codec (DD+/DDP/E-AC3 → 'ddp', DTS-HD → 'dtshd'), or '' when
    none is detected. Channel layout (5.1/7.1) and Atmos are ignored — only the
    base codec, the part that actually distinguishes two encodes."""
    s = re.sub(r'[._\-]', ' ', str(name or '').lower())
    for fam, pat in _AUDIO_PATTERNS:
        if re.search(pat, s):
            return fam
    return ''


_QUALITY_NOISE = {
    '2160p', '1080p', '1080i', '720p', '480p', '4k', 'uhd', 'sdtv',
    'web', 'dl', 'webdl', 'webrip', 'bluray', 'blu', 'ray', 'bdrip', 'brrip',
    'remux', 'hdtv', 'dvd', 'dvdrip', 'bd',
    'hdr', 'hdr10', 'sdr', 'dv', 'dovi', 'dolby', 'vision', 'hlg', 'plus',
    'atmos', 'hd', 'ma',
    'x', 'h', 'hevc', 'avc',
    'amzn', 'nf', 'dsnp', 'atvp', 'hmax', 'max', 'hulu', 'pcok', 'stan', 'ip', 'itunes',
    'repack', 'proper', 'internal', 'extended', 'remastered', 'remaster',
    'imax', 'real', 'uncut', 'directors', 'cut',
    'mkv', 'mp4', 'avi', 'ts', 'm2ts', 'iso',
}

# Articles carry no discriminating power and cause spurious title overlap ("The
# Matrix" vs "The Thing" share 'the'); dropped from the title core.
_TITLE_STOPWORDS = {'the', 'of', 'a', 'an', 'and'}

# Tokens that are noise regardless of any trailing digits (fused channel layout,
# codec versions): ddp5, dd+5, x265, h264, eac3, dts5, the bare 7/1 of "7.1".
_CORE_DROP_RE = re.compile(
    r'^(?:'
    r'(?:19|20)\d{2}'                                      # year
    r'|s\d{1,2}(?:e\d{1,4})?'                              # season/episode anchor
    r'|\d{1,2}'                                            # stray channel/disc digits
    r'|(?:x|h)?26[45]'                                     # codec
    r'|(?:dd\+?|ddp|dts|e?ac3|aac|flac|opus|truehd|atmos)\d*'  # audio
    r')$'
)


def _title_core_tokens(norm, group=''):
    """Title-only tokens of a normalized release name — quality/source/audio/
    codec/year/episode/group/article noise stripped, punctuation folded — for
    the soft title match.

    Punctuation is stripped before the noise lookup, not after: a quality token
    that carries a symbol ("HDR10+", "DD+") would otherwise miss the noise set
    and land in the title core, where it reads as *shared title words* between
    two unrelated releases. One such token is enough to clear the similarity
    gate — that was the "Weapons 2025 offered for The Drama 2026" bug, where
    both names contributed 'hdr10+' and nothing else overlapped. \\W (not
    [^a-z0-9]) so accented title words survive intact.
    """
    out = set()
    for raw in re.split(r'[\s\-]+', norm):
        tok = re.sub(r'\W+', '', raw)   # director's → directors, hdr10+ → hdr10
        if not tok or tok == group or tok in _QUALITY_NOISE or tok in _TITLE_STOPWORDS:
            continue
        if _CORE_DROP_RE.match(tok):
            continue
        out.add(tok)
    return out


@functools.lru_cache(maxsize=32768)
def _release_match_features(name):
    """Match features of one release name, cached by name (TR11).

    Phase 1 ranks every torrent in the client against every trumped title, and
    a season-pack PM lists one title per episode, so the same few thousand names
    were re-parsed once per title — ~27s of single-threaded CPU in one request
    for 20 titles over 15k torrents, ~2s cached. **What is cached is read-only**:
    a cached dict or set that one caller edited would corrupt every later match
    in the process, so the mapping is a proxy and the title core a frozenset.
    `release_match_cache_clear` releases the entries when a phase-1 request ends.
    """
    norm  = _norm_release_name(name)
    info  = parse_release_info(name)
    group = _release_group_tag(name)
    return MappingProxyType({
        'core':   frozenset(_title_core_tokens(norm, group)),
        'res':    info['resolution'],
        'source': info['source'],
        'hdr':    info['hdr'],
        'audio':  _audio_codec(name),
        'group':  group,
        'year':   info['year'],
        'anchor': _season_ep_anchor(norm),
    })


def release_match_cache_clear():
    """Drop cached release-match features — the cache pays for itself inside one
    request, and a client of 15k torrents should not leave 15k entries resident
    on a process whose memory is already the thing to watch."""
    _release_match_features.cache_clear()


_MIN_TITLE_SIM = 0.3


def score_release_match(query, cand_name):
    """Graded similarity (0..1) of a candidate release name to a query title,
    with a per-field agreement breakdown for the UI.

    The title is a REQUIRED soft match: the two title cores must actually
    overlap (Jaccard ≥ _MIN_TITLE_SIM) and the year (within ±1) / episode
    anchor must agree — otherwise the candidate is disqualified (score 0,
    dropped by the ranker), no matter how well its resolution/source/audio line
    up. Quality agreement only *refines the ranking among real title matches*;
    it never manufactures one. This is what stops two unrelated 1080p WEB-DLs
    from looking like siblings.

    Returns (score, breakdown) where breakdown maps title/year/res/source/
    group/audio/hdr/anchor to 'same' | 'diff' | 'partial' | '' (missing on a
    side).
    """
    return _score_match_features(_release_match_features(query),
                                 _release_match_features(cand_name))


def _score_match_features(q, c):
    """`score_release_match` over features already extracted — see there.

    Split out so a ranking loop extracts the query's features once rather than
    once per candidate (TR11): the query is invariant across the loop, and
    re-parsing it was half the cost of a phase-1 pass.
    """
    b = {'title': '', 'year': '', 'res': '', 'source': '', 'group': '', 'audio': '', 'hdr': '', 'anchor': ''}

    # Title gate — both sides must have parseable title words that overlap.
    if not q['core'] or not c['core']:
        return 0.0, b
    inter = len(q['core'] & c['core'])
    union = len(q['core'] | c['core'])
    title_sim = inter / union if union else 0.0
    b['title'] = 'same' if q['core'] == c['core'] else ('partial' if inter else 'diff')
    if inter == 0 or title_sim < _MIN_TITLE_SIM:
        return 0.0, b

    # Year gate — a declared year off by more than one is a different release
    # (remake). Exactly one year of drift is the same film: premiere year vs
    # wide-release year renders one movie under either ("Snow White and the
    # Seven Dwarfs" is 1937 or 1938 depending on who typed it), so PMs, torrent
    # names, and indexer records routinely disagree by one. Same ±1 tolerance
    # `arr_year_ok` applies to Radarr; flagged 'partial' so the user sees the
    # drift. (Both sides here are *release names*, so this is symmetric — unlike
    # `arr_year_ok`, whose Sonarr half compares a release against a series' first
    # air year and can only be one-sided.)
    if q['year'] and c['year']:
        if abs(q['year'] - c['year']) > 1:
            return 0.0, b
        b['year'] = 'same' if q['year'] == c['year'] else 'partial'
    # Episode gate — a declared season/episode that disagrees is a different
    # payload.
    if q['anchor'] and c['anchor'] and q['anchor'] != c['anchor']:
        b['anchor'] = 'diff'
        return 0.0, b
    b['anchor'] = 'same' if (q['anchor'] and c['anchor']) else ''

    score = title_sim   # title dominates; quality only refines below

    def field(key, w_same, w_diff):
        qv, cv = q[key], c[key]
        if not qv or not cv:
            b[key] = ''
            return 0.0
        if qv == cv:
            b[key] = 'same'
            return w_same
        b[key] = 'diff'
        return -w_diff

    score += field('res',    0.12, 0.20)
    score += field('source', 0.10, 0.15)
    score += field('group',  0.18, 0.18)
    score += field('audio',  0.08, 0.05)
    score += field('hdr',    0.06, 0.05)

    # A title-gated candidate always stays visible (min 0.05) so the user can
    # vet it; only true gate failures return 0.
    score = max(0.05, min(1.0, score))
    if b['year'] == 'partial':
        # After the cap, so an exact-year twin outranks the ±1 rendering even
        # when both saturate at 1.0.
        score = max(0.05, score - 0.02)
    return score, b


def rank_release_matches(items, query, name_key='name', limit=8, min_score=0.0):
    """Rank `items` by name similarity to `query`, best first.

    Each returned item is a shallow copy with 'match_score' (0..1) and 'match'
    (the field breakdown). Only real title matches survive the gate in
    `score_release_match`; ties break on seeders. Returns at most `limit`, and
    **empty when nothing genuinely matches** — an honest empty list beats a
    confident wrong answer (the caller reports "no match", offering manual entry
    or the arr deep link).
    """
    scored = []
    q = _release_match_features(query)
    for it in items:
        s, brk = _score_match_features(q, _release_match_features(it.get(name_key) or ''))
        if s <= 0 or s < min_score:
            continue
        scored.append({**it, 'match_score': round(s, 3), 'match': brk})
    scored.sort(key=lambda x: (x['match_score'], x.get('seeders') or 0), reverse=True)
    return scored[:limit]


def rank_trump_replacements(releases, new_title, indexer='', limit=8):
    """(release, candidates) — Trumped step 4's replacement, best first.

    Three tiers, in this order:

    1. **The exact release on the tracker that sent the PM.** That is the copy
       the user means to grab — it is the one carrying the PM's freeleech, and
       seeding the replacement where the trump happened is the point of
       complying. It leads and is pre-selected.
    2. **The exact release on another tracker.** Grabbing there and
       cross-seeding is a legitimate edge case (the PM's tracker has not listed
       it yet, or the user prefers it), never the default.
    3. Everything else that clears the title gate, by score; ties broken by
       fewer disagreeing fields, then the PM's tracker, then seeders.

    **Exactness is required for the top two tiers, and it is load-bearing.**
    `repack` and `proper` are quality noise to the fuzzy score, so a trump that
    replaces a release with its own REPACK scores the trumped original — still
    cached on an indexer — exactly as high as the replacement, and the score
    saturates at 1.0 for most same-title releases anyway. Ranking by tracker
    over that score would pre-select the release that was just trumped. Within
    tiers 1 and 2 every copy is the same release, so seeders decide.

    `release` is the head of tier 1 or 2, else None — no confident match. Each
    candidate carries `pm_tracker` and `exact`.
    """
    target = _norm_release_name(new_title)
    q = _release_match_features(new_title)
    rows = []
    for r in releases:
        exact = bool(target) and _norm_release_name(r.get('title')) == target
        s, brk = _score_match_features(q, _release_match_features(r.get('title') or ''))
        if s <= 0 and not exact:
            continue
        rows.append({**r, 'match_score': 1.0 if exact else round(s, 3), 'match': brk, 'exact': exact,
                     'pm_tracker': bool(indexer) and tracker_matches_indexer(r.get('indexer'), indexer)})

    def _key(r):
        tier = 0 if (r['exact'] and r['pm_tracker']) else (1 if r['exact'] else 2)
        diffs = sum(1 for v in r['match'].values() if v == 'diff')
        return (tier, -r['match_score'], diffs, not r['pm_tracker'], -(r.get('seeders') or 0))

    rows.sort(key=_key)
    release = rows[0] if rows and rows[0]['exact'] else None
    return release, rows[:limit]


def title_soft_match(query_title, candidate_title):
    """Soft title-only similarity (0..1) between two titles — the shared title
    core over their union, ignoring quality/year/episode/article noise.

    For matching a release name to a managed arr title when exact keys differ: a
    stray season token ('… Rides Again S01'), extra scene tokens, or punctuation
    that would defeat `title_match_keys`. Returns 0 when either side has no
    parseable title words or they don't overlap at all.
    """
    q = _title_core_tokens(_norm_release_name(query_title))
    c = _title_core_tokens(_norm_release_name(candidate_title))
    if not q or not c:
        return 0.0
    inter = len(q & c)
    if not inter:
        return 0.0
    return inter / len(q | c)


# Host labels that identify the announce endpoint, not the tracker itself.
_TRACKER_HOST_PREFIXES = {'www', 'tracker', 'announce', 'tr', 't', 'private', 'secure'}


def indexer_key(name):
    """Comparable identity for an indexer name or a tracker host.

    The Trumped wizard knows the PM's tracker as an *arr indexer name* ("Aither
    (API) (Prowlarr)") while client torrents carry a *host* ("aither.cc"); both
    reduce to 'aither'. Parenthetical suffixes, URL scheme/path/port, announce
    subdomains and punctuation are dropped.
    """
    s = re.sub(r'\([^)]*\)', ' ', str(name or '').strip().lower())
    s = re.sub(r'^[a-z]+://', '', s.strip()).split('/')[0].split(':')[0].strip()
    if '.' in s:
        labels = [l for l in s.split('.') if l]
        while len(labels) > 1 and labels[0] in _TRACKER_HOST_PREFIXES:
            labels.pop(0)
        s = labels[0] if labels else s
    return re.sub(r'[^a-z0-9]', '', s)


def tracker_matches_indexer(tracker, indexer):
    """True when a torrent's tracker is (most likely) the indexer the PM came from.

    Deliberately fuzzy — the two names come from different systems and only ever
    agree by convention. Safe because this is a **ranking tie-break only**: a
    wrong answer reorders equally-scored candidates, it never drops one or
    promotes a worse title match. Containment needs 4+ chars so short names
    ('HD') can't swallow unrelated trackers.
    """
    a, b = indexer_key(tracker), indexer_key(indexer)
    if not a or not b:
        return False
    return a == b or (len(a) >= 4 and len(b) >= 4 and (a in b or b in a))


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


def queue_records_for_item(cfg, service, connection_id, arr_id, episode_ids=None, season_number=None):
    """The arr's live queue entries for one Backfill candidate, or None if unreadable.

    Sonarr: entries on this series carrying one of `episode_ids`; failing those,
    entries in `season_number`; failing both, any entry on the series. Radarr:
    entries on this movie. An entry in a terminal failed state is not a download
    in progress and is not returned — the same reading `poll_queue_until_clear`
    takes.

    Keeps a candidate from being grabbed a second time (B12): by a retry after a
    timeout the arr had in fact processed, by a second run, or by a click on a
    row that reported failure. `None` is "could not ask", distinct from `[]`.
    """
    conns = normalize_arr_connections(cfg, service=service)
    conn = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        return None
    try:
        result = _arr_get(conn['base_url'], conn['api_key'], '/api/v3/queue?pageSize=500', timeout=10)
    except Exception as e:
        log.warning("Could not read the %s queue on %s: %s", service, connection_id, e)
        return None
    records = result.get('records', []) if isinstance(result, dict) else (result or [])
    live = [r for r in records if isinstance(r, dict) and r.get('status') not in ('failed', 'error')]
    if service == 'radarr':
        return [r for r in live if r.get('movieId') == arr_id]
    mine = [r for r in live if r.get('seriesId') == arr_id]
    if episode_ids:
        wanted = set(episode_ids)
        return [r for r in mine if r.get('episodeId') in wanted]
    if season_number is not None:
        return [r for r in mine if r.get('seasonNumber') == season_number]
    return mine


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


def force_manual_import_by_id(cfg, service, connection_id, arr_id, download_id=None,
                              download_folder=None, only_paths=None, import_mode='Auto',
                              only_episode_ids=None, media_folder_fallback=True):
    """Force manual import of a movie or series, bypassing quality cutoff.

    download_id:     the downloadId from the Radarr/Sonarr queue record (qBittorrent hash).
                     When provided it is used for the GET query so the download-client-
                     tracked file is found directly.  This is the correct path for same-
                     quality grabs that sit in importPending state.
    download_folder: fallback — parent directory of outputPath from the queue record.
                     Used when download_id is unavailable.  Falls back further to the
                     media folder unless `media_folder_fallback` is False.
    only_paths:      restrict the import to these exact arr-side file paths. A folder
                     lookup returns every file in the folder, and a single-file torrent's
                     folder is the shared category dir — without this, importing one
                     torrent would sweep in every unrelated file sitting beside it.
    only_episode_ids: Sonarr only — keep a row only if every episode it names is in
                     this set, and drop a row that names none. The path form above
                     cannot scope a *grab*: the files to import do not exist until the
                     download does, while the library paths a caller knows are the
                     files being replaced, not imported. Episodes are the unit both
                     sides share. Without it a season pack grabbed for one episode
                     replaces every episode it carries (BACKFILL B1/B11).
    media_folder_fallback: the arr's own library folder as a last resort. Its listing
                     is the library file itself, so without a path scope a force
                     import from it re-imports the very file it is replacing. A caller
                     that has nothing to scope it with must pass False and report the
                     missing download location rather than reach for this.
    import_mode:     'Auto' is honoured **only when the rows came from the downloadId
                     lookup**: the arr then sees CanMoveFiles=false on a seeding torrent
                     and hardlinks. Every folder branch imports with 'Copy' whatever was
                     asked for — with no tracked download Auto means move, which pulls
                     the payload out from under the seed, and the branch is decided in
                     here, so a caller cannot know which one it will get. 'Copy' is
                     HardLinkOrCopy when the arr has hardlinks enabled.
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

    def _keep(rows):
        rows = rows or []
        if only_paths is not None:
            wanted = set(only_paths)
            rows = [r for r in rows if (r.get('path') or '') in wanted]
        if only_episode_ids is not None:
            allowed = set(only_episode_ids)

            def _in_scope(row):
                ids = {ep.get('id') for ep in (row.get('episodes') or []) if ep.get('id') is not None}
                return bool(ids) and ids <= allowed
            rows = [r for r in rows if _in_scope(r)]
        return rows

    def _query_by_download_id(dl_id):
        try:
            encoded = urllib.parse.quote(dl_id, safe='')
            return _keep(_arr_get(conn['base_url'], conn['api_key'],
                                  f'/api/v3/manualimport?downloadId={encoded}&{id_param}',
                                  timeout=30))
        except urllib.error.HTTPError as e:
            log.warning("manualimport GET by downloadId returned HTTP %s — falling back to folder", e.code)
            return []

    def _query_by_folder(folder):
        # No movieId/seriesId here, deliberately. It does not scope the folder
        # listing to that item — it *replaces* it, returning the arr's existing
        # library file (with a movieFileId) and ignoring `folder` entirely. That
        # silently defeated every folder lookup: the rows came back describing
        # the file we are trying to replace, never the one we want to import.
        try:
            encoded = urllib.parse.quote(folder, safe='')
            return _keep(_arr_get(conn['base_url'], conn['api_key'],
                                  f'/api/v3/manualimport?folder={encoded}&filterExistingFiles=false',
                                  timeout=30))
        except urllib.error.HTTPError as e:
            log.warning("manualimport GET by folder returned HTTP %s", e.code)
            return []

    # Try downloadId first (finds download-client-tracked files), then folder fallbacks.
    folders_to_try = [f for f in [download_folder, media_folder if media_folder_fallback else None] if f]
    files = []
    via_download = False
    if download_id:
        files = _query_by_download_id(download_id)
        via_download = bool(files)
        if files:
            log.info("Found %d importable file(s) for %s %s via downloadId", len(files), service, arr_id)

    if not files:
        for folder in folders_to_try:
            files = _query_by_folder(folder)
            if files:
                log.info("Found %d importable file(s) for %s %s in %s", len(files), service, arr_id, folder)
                break

    if not files and (download_id or folders_to_try):
        # Retry once after a delay — handles timing race where qBit finishes but files
        # aren't importable yet from Sonarr/Radarr's perspective
        log.info("No importable files for %s %s on first attempt, retrying in 15s…", service, arr_id)
        time.sleep(15)
        if download_id:
            files = _query_by_download_id(download_id)
            via_download = bool(files)
        if not files:
            for folder in folders_to_try:
                files = _query_by_folder(folder)
                if files:
                    break

    if not files:
        if not download_id and not folders_to_try:
            raise ValueError("Nothing to import from — no download id or folder was given")
        if only_paths or only_episode_ids is not None:
            raise ValueError(
                f"{_SERVICE_MAP[service]['name']} does not list the selected file(s) as importable — "
                "they may have already been imported, or moved")
        raise ValueError(f"No importable files found — check {service} queue manually")

    # Auto survives only where a tracked download stands behind the rows; every
    # folder branch copies (see the docstring). Decided here because only here
    # is the branch known.
    if not via_download:
        import_mode = 'Copy'

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
        'importMode':           import_mode,
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


def read_arr_file_id(cfg, service, connection_id, arr_id):
    """The current file id for a Radarr movie, or a Sonarr series' episode file ids.

    **Raises when the read fails** — an unknown connection, a timeout, an error
    status. That is the whole difference from `get_arr_file_id` below, and it is
    load-bearing for `/api/workflows/import_check` (S08): that helper swallowed
    every exception and answered `None`, so a timed-out read reached the page as
    `checked: true, file_id: null`, which differs from any baseline holding a
    file and was taken for a landed import. "Asked, and it holds no file" is a
    return value here; "could not ask" is an exception.

    Sonarr's answer is a *sorted list*, not a set: every consumer only ever
    compares two readings with `!=`, and sorting makes that comparison as exact
    as a set's while staying JSON-serializable. It used to be a `frozenset`,
    which `/api/workflows/import_check` put straight into `jsonify` — so that
    endpoint 500'd on every Sonarr item, silently taking any Radarr items in the
    same request down with it and leaving the rescan follow-through watching
    nothing.
    """
    conns = normalize_arr_connections(cfg, service=service)
    conn  = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        raise LookupError(f"Arr connection '{connection_id}' not found for service '{service}'")
    if service == 'radarr':
        info = _arr_get(conn['base_url'], conn['api_key'], f'/api/v3/movie/{arr_id}', timeout=10)
        return info.get('movieFileId')
    # For Sonarr track the episode file IDs as a sorted snapshot
    eps = _arr_get(conn['base_url'], conn['api_key'],
                   f'/api/v3/episodefile?seriesId={arr_id}', timeout=10)
    return sorted({e['id'] for e in eps if e.get('id')})


def get_arr_file_id(cfg, service, connection_id, arr_id):
    """`read_arr_file_id`, or None when it could not be read.

    Used to detect whether a ManualImport command actually replaced the file,
    since the command endpoint may report status='failed' even on success.
    `force_import_files` treats a `None` baseline as "could not verify", which
    is right for it — it compares a reading taken before its own command with
    one taken after, and says so when either is missing. A caller that has to
    tell a failed read from an empty one uses `read_arr_file_id`.
    """
    try:
        return read_arr_file_id(cfg, service, connection_id, arr_id)
    except Exception:
        return None


def force_import_files(cfg, service, connection_id, arr_id, paths, wait=20):
    """Import specific torrent files over the arr's existing file ("Import Anyway").

    A scan command can never do this: Sonarr/Radarr run the upgrade and revision
    specs against the file they already hold and reject anything that is not
    strictly better — a same-quality trump replacement always is. ManualImport
    with replaceExistingFiles is the API form of the UI's "Import Anyway", and
    the only override there is.

    Success is confirmed by watching the arr's own file id, not the command
    status, which reports 'failed' on replacements that in fact succeeded.
    Returns {'imported': bool, 'files': int, 'message': str}.
    """
    conns = normalize_arr_connections(cfg, service=service)
    conn  = next((c for c in conns if c['id'] == connection_id), None)
    if conn is None:
        raise ValueError(f"Arr connection '{connection_id}' not found for service '{service}'")
    if not paths:
        raise ValueError("No files to import")

    local_path  = cfg.get('LOCAL_PATH', '').strip()
    remote_path = conn.get('remote_path', '').strip()
    targets     = [arr_import_target(p, local_path, remote_path) for p in paths]
    arr_files   = [t[1] for t in targets]
    # The scan target, not the containing folder: for a single-file torrent the
    # folder is the shared category dir, whose listing identifies nothing —
    # every row comes back with movie null and quality "Unknown", which would
    # import this file into the library under an unknown quality.
    lookup      = targets[0][0]

    before = get_arr_file_id(cfg, service, connection_id, arr_id)
    # Copy, not Auto: there is no tracked download behind a Triage item — the arr
    # never grabbed it — so Auto would resolve to a move and break the seed.
    force_manual_import_by_id(cfg, service, connection_id, arr_id,
                              download_folder=lookup, only_paths=arr_files,
                              import_mode='Copy')

    name = _SERVICE_MAP[service]['name']
    if before is None:
        # Without a baseline a changed file id proves nothing — 0 ("no file") and
        # a real id both differ from None. Say so rather than claim either way.
        return {'imported': False, 'files': len(arr_files),
                'message': f"Import sent, but {name} did not report its current file — verify there"}

    # The command is queued, not synchronous, so poll for the file id to move.
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(2)
        after = get_arr_file_id(cfg, service, connection_id, arr_id)
        if after is not None and after != before:
            return {'imported': True, 'files': len(arr_files), 'message': 'Imported'}
    return {
        'imported': False,
        'files':    len(arr_files),
        'message':  f"{name} accepted the import but its library file has not changed — "
                    "check Activity → Queue there",
    }


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
            fetch = _fetch_radarr_media if conn['service'] == 'radarr' else _fetch_sonarr_media
            media, partial = fetch(conn)
            media = _apply_arr_media_path_mapping(media, conn, cfg)
            item['ok'] = True
            item['managed_file_count'] = len(media)
            item['sample_paths'] = [m['path'] for m in media if m.get('path')][:5]
            if partial:
                # Still `ok` — the connection works — but a file count that
                # silently omits a tenth of the library is the same misreport
                # the index errors channel exists to stop.
                item['partial'] = True
                item['message'] = (f"Connected, but {partial['failed']} of {partial['total']} "
                                   f"series could not be read — the file count below is incomplete")
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
    # Deliberately single-key, unlike base_url above: that alias is why
    # validate_config has to length-check both 'base_url' and 'url', and a second
    # alias here would let the validator check one name while this reads another.
    external_url = str(raw.get('external_url') or '').strip().rstrip('/')
    conn_id = str(raw.get('id') or f"{service}-{_slug(raw.get('name') or service)}").strip()
    return {
        'id': conn_id,
        'service': service,
        'name': str(raw.get('name') or _SERVICE_MAP[service]['name']).strip(),
        'base_url': base_url,
        'external_url': external_url,
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
    """(rows, partial) — Radarr needs one call, so `partial` is always None.

    The tuple exists for symmetry with `_fetch_sonarr_media`, which needs one
    call per series and therefore has a third outcome between "worked" and
    "raised": some of the library came back.
    """
    rows = []
    for movie in _arr_get(conn['base_url'], conn['api_key'], '/api/v3/movie', timeout=_ARR_LIST_TIMEOUT):
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
            # MovieFileResource.Size (long). Lets Trumped narrow its inode join
            # to rows that could be the same file before stat'ing any (TR7):
            # a hardlink shares its size.
            'size': movie_file.get('size'),
            'arr_id': movie.get('id'),
            'file_id': movie_file.get('id'),
            'title_slug': movie.get('titleSlug') or '',
            'file_quality_name': q_inner.get('name', ''),
            # Basename, not the full path: a library root or category directory
            # containing DV, HDR or HLG as a segment ("/data/media/HDR/…")
            # otherwise labels every file beneath it.
            'file_hdr': _detect_hdr(os.path.basename(path)),
        })
    return rows, None


def _fetch_sonarr_media(conn):
    """(rows, partial) — episode-file records for one Sonarr instance.

    `partial` is None when every series was read, else
    `{'failed': n, 'total': m}`. One unreachable series must not discard the
    instance (one timeout in a library of hundreds used to read to every caller
    as "this Sonarr manages nothing"), but the survivors are a flat list, so
    without this count a 40-of-400 gap is indistinguishable from a library that
    simply has no files there — and every missing episode then reads as
    `import_pending` or `not_in_library` in Triage. Partial failure is both
    likelier than total failure and, until this was returned, completely silent.
    """
    series_list = _arr_get(conn['base_url'], conn['api_key'], '/api/v3/series', timeout=_ARR_LIST_TIMEOUT)
    valid_series = [(s, s['id']) for s in series_list if s.get('id') is not None]
    if not valid_series:
        return [], None

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
                # EpisodeFileResource.Size (long) — see _fetch_radarr_media.
                'size': episode_file.get('size'),
                'arr_id': series_id,
                'file_id': episode_file.get('id'),
                'episode_ids': episode_file.get('episodeIds') or [],
                # Sonarr's own season/episode numbers, off the same response.
                # Regex-parsing them back out of the filename misses daily
                # series ("Show - 2024-01-05"), anime absolute numbering
                # ("Show - 087"), "S01.E02" and any non-standard renamer — and
                # an unparseable episode does not merely lose its season, it
                # merges with every other unparseable episode of the series
                # into one candidate keyed `..._SNone`. Zero extra API cost.
                'season_number': episode_file.get('seasonNumber'),
                'episode_numbers': episode_file.get('episodeNumbers') or [],
                'title_slug': series.get('titleSlug') or '',
                'file_quality_name': q_inner.get('name', ''),
                # Basename, not the full path — see _fetch_radarr_media.
                'file_hdr': _detect_hdr(os.path.basename(path)),
            })
        return rows

    all_rows = []
    failed = []
    max_workers = min(8, len(valid_series))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_episode_files, s, sid): sid for s, sid in valid_series}
        for future in as_completed(futures):
            try:
                all_rows.extend(future.result())
            except Exception as e:
                # One unreachable series must not discard the instance. Sonarr
                # needs a call per series where Radarr needs one in total, so
                # re-raising here made a single timeout anywhere in a library of
                # hundreds read to every caller as "this Sonarr manages nothing"
                # — every one of its files silently unresolvable.
                failed.append(futures[future])
                log.warning("Could not fetch episode files for series %s on %s: %s",
                            futures[future], conn['id'], e)
    if failed:
        log.error("%s: %d of %d series could not be read — their episodes will not "
                  "resolve to library items", conn['id'], len(failed), len(valid_series))
        return all_rows, {'failed': len(failed), 'total': len(valid_series)}
    return all_rows, None


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
    # Not splitext: it takes everything after the final dot, so the dot-separated
    # release name 'Some.Movie.2020' parsed its own year off as an extension and
    # `_parse_year_from_name` below then found none. Release names are
    # dot-separated by convention, and this function is also handed *folder*
    # names, which have no extension at all to take.
    stem = _strip_file_ext(base)
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


# One immutable snapshot, `(ts, rows, errors)`, published in one assignment (S11).
_arr_titles_cache = {'snapshot': None}
_ARR_TITLES_TTL = 120


def _arr_titles_snapshot(cfg, force=False):
    """`(ts, rows, errors)` — every managed title, as one immutable snapshot (S11).

    The media index only contains items that HAVE files — this list is what lets
    Triage distinguish "in the library but never imported" from "not in the
    library at all". Rows are [{service, connection_id, arr_id, title,
    title_slug, year, has_file, alt_titles}].

    `alt_titles` is the arr's own `alternateTitles` — see `title_alias_keys`.
    It rides the listing both services already return, so it costs no extra
    call; it is capped per item because Radarr can carry a translation for
    every region it knows about.
    """
    now = time.monotonic()
    snap = _arr_titles_cache.get('snapshot')
    if not force and snap is not None and (now - snap[0]) < _ARR_TITLES_TTL:
        return snap
    rows = []
    errors = []
    for conn in normalize_arr_connections(cfg):
        try:
            if conn['service'] == 'radarr':
                for m in _arr_get(conn['base_url'], conn['api_key'], '/api/v3/movie', timeout=_ARR_LIST_TIMEOUT):
                    rows.append({
                        'service':       'radarr',
                        'connection_id': conn['id'],
                        'arr_id':        m.get('id'),
                        'title':         m.get('title') or '',
                        'title_slug':    m.get('titleSlug') or '',
                        'year':          m.get('year'),
                        'has_file':      bool(m.get('hasFile')),
                        'alt_titles':    _alt_titles(m),
                    })
            else:
                for s in _arr_get(conn['base_url'], conn['api_key'], '/api/v3/series', timeout=_ARR_LIST_TIMEOUT):
                    stats = s.get('statistics') or {}
                    rows.append({
                        'service':       'sonarr',
                        'connection_id': conn['id'],
                        'arr_id':        s.get('id'),
                        'title':         s.get('title') or '',
                        'title_slug':    s.get('titleSlug') or '',
                        'year':          s.get('year'),
                        'has_file':      (stats.get('episodeFileCount') or 0) > 0,
                        'alt_titles':    _alt_titles(s),
                    })
        except Exception as e:
            log.warning("Could not fetch %s titles from %s: %s", conn['service'], conn['id'], e)
            errors.append({'connection_id': conn['id'], 'name': conn['name'],
                           'service': conn['service'], 'partial': False,
                           'message': str(e)})
    snap = (now, rows, tuple(errors))
    _arr_titles_cache['snapshot'] = snap
    return snap


def fetch_arr_all_titles(cfg, force=False):
    """Every managed title across all Arr instances (cached 120s) — rows only.

    See `_arr_titles_snapshot` for the row shape. A caller that reports this
    fetch's failures uses `fetch_arr_all_titles_result`.
    """
    return _arr_titles_snapshot(cfg, force)[1]


def fetch_arr_all_titles_result(cfg, force=False):
    """`(rows, errors)` for the title list, from one snapshot — see `fetch_arr_media_index_result` (S11)."""
    snap = _arr_titles_snapshot(cfg, force)
    return snap[1], list(snap[2])


def arr_titles_errors():
    """Connections whose title list failed, **most recent snapshot**.

    The mirror of `arr_media_index_errors`, and it matters for the same reason:
    this list is what answers "does any arr know this title at all", so an
    instance that is merely unreachable would otherwise be indistinguishable
    from one that has never heard of the release. It reads whichever fetch
    landed last, so a caller that turns it into a verdict takes the errors from
    `fetch_arr_all_titles_result` instead.
    """
    snap = _arr_titles_cache.get('snapshot')
    return list(snap[2]) if snap else []


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


def _arr_command(base_url, api_key, command_name, path, import_mode='Copy'):
    """POST a scan command to a Sonarr/Radarr instance.

    importMode is sent explicitly. The default, Auto, means "move" whenever no
    download-client item is attached to the scan — and one never is here, since
    the whole point of a Triage rescan is a payload the arr never grabbed. A
    move pulls the file out from under a seeding torrent. Copy leaves the source
    in place, and with "Use Hardlinks instead of Copy" on (the hardlink setup
    this app assumes) it costs no extra disk.
    """
    endpoint = base_url.rstrip('/') + '/api/v3/command'
    body = json.dumps({"name": command_name, "path": path, "importMode": import_mode}).encode()
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


# Radarr carries a translation for nearly every region it knows about, and this
# list is held in memory for the whole cache TTL, so it is bounded per item.
_MAX_ALT_TITLES = 25


def _alt_titles(item):
    """The `alternateTitles` strings on a Sonarr series or Radarr movie."""
    out, seen = [], set()
    for a in (item.get('alternateTitles') or ()):
        t = (a.get('title') or '').strip() if isinstance(a, dict) else str(a or '').strip()
        low = t.lower()
        if t and low not in seen:
            seen.add(low)
            out.append(t)
            if len(out) >= _MAX_ALT_TITLES:
                break
    return out


def title_alias_keys(all_titles):
    """Map each alternate-title key an arr knows onto that item's canonical keys.

    Sonarr and Radarr both carry `alternateTitles` — the AKA and translated
    names TheTVDB/TMDB hold for an item — and for non-English content the scene
    release is named in the **original language** while the arr stores the
    English title. `No.tengo.miedo.S01E01…` against a series Sonarr calls
    "I'm Not Afraid" matches on nothing, so Triage returned `not_in_library`
    ("no arr has ever heard of this — junk can be deleted") for a series Sonarr
    was actively managing and had already grabbed. That copy invited a delete
    on the only copy of the data.

    This is authoritative metadata auditorr simply never asked for, not a fuzzy
    guess: the arr could only have matched the grab in the first place *because*
    it consults this same field. It rides a listing both services already
    return, so reading it costs no extra API call.

    Returns {alias_key: {canonical_key, ...}}. Callers append the results
    **after** their canonical keys (`with_title_aliases`), so an exact title
    match always wins and an alias can only ever add a candidate that would
    otherwise have been "nothing". A key that is already canonical for the same
    item is skipped, so nothing aliases to itself.
    """
    alias = {}
    for t in all_titles or ():
        canon = title_match_keys(t.get('title') or '')
        if not canon:
            continue
        for a in (t.get('alt_titles') or ()):
            for k in title_match_keys(a):
                if k not in canon:
                    alias.setdefault(k, set()).update(canon)
    return alias


def with_title_aliases(keys, aliases):
    """Canonical keys first, then any the arrs' alternate titles point to.

    Order is the whole point: every consumer resolves with `next(...)` over
    these keys, so putting aliases last keeps an exact title match strictly
    ahead of a translated one.
    """
    keys = set(keys)
    if not aliases:
        return list(keys)
    extra = set()
    for k in keys:
        extra |= aliases.get(k) or set()
    return list(keys) + sorted(extra - keys)


def arr_year_ok(parsed, row):
    """Year gate for matching a parsed release name against an arr item.

    Three rules, and each of the three is load-bearing:

    * **A title that *is* a year is not a release year.** "1923", "1883",
      "1899", "2012" parse their own name as the year, while the arr stores the
      year the show or film actually came out (1923 → 2022). Skipping the check
      when the token sits inside the parsed title is what stops the gate
      disqualifying the one title it was handed.
    * **Radarr: ±1.** A premiere-vs-wide-release year renders one film either
      way (Snow White 1937/1938) — the same tolerance `rank_release_matches`
      already applies.
    * **Sonarr: one-sided, and deliberately not ±1.** The arr stores a series'
      *first air* year while a TV release name usually carries the episode's
      **air date**, so a legitimate match differs by the length of the show's
      run — ±1 would disqualify every daily series and everything past season
      two. What is still sound is the one direction: nothing can air more than a
      year before the series began, which is exactly the same-title-remake case
      this gate exists for (a release labelled 1990 against a 2019 reboot).

    Unknown on either side passes. A missing year is not evidence of a mismatch.
    """
    p_year = parsed.get('year')
    r_year = row.get('year')
    if not p_year or not r_year:
        return True
    if str(p_year) in str(parsed.get('title') or ''):
        return True
    if row.get('service') == 'radarr':
        return abs(int(r_year) - p_year) <= 1
    return p_year >= int(r_year) - 1


def _arr_candidate_score(row, parsed):
    """How well one arr row answers a parsed release. Higher is better."""
    score = 0
    season = parsed.get('season')
    if season is not None:
        row_season = row.get('season_number')
        row_eps    = row.get('episode_numbers') or []
        rel = os.path.basename(row.get('relative_path') or row.get('path') or '')
        if row_season is None:
            # Title-list rows have no per-file season, and media-index rows
            # written before Sonarr's own numbers were carried have none either
            # — fall back to the filename the way the rest of the app does.
            row_season, row_eps = season_episodes_from_name(rel)
        elif not row_eps:
            # A media-index row carries Sonarr's own season but never its
            # episode numbers — `/api/v3/episodefile` has none (Phase 6) — so
            # the episode anchor below was inert on every such row. The filename
            # supplies the episode half only, and only when it agrees with
            # Sonarr about the season.
            name_season, name_eps = season_episodes_from_name(rel)
            if name_season == row_season:
                row_eps = name_eps
        if row_season is not None:
            score += 4 if row_season == season else -4
            episode = parsed.get('episode')
            if episode is not None and row_eps:
                score += 4 if episode in row_eps else -2
    p_year, r_year = parsed.get('year'), row.get('year')
    if p_year and r_year:
        delta = abs(int(r_year) - p_year)
        score += 3 if delta == 0 else (1 if delta <= 1 else 0)
    if row.get('has_file'):
        score += 1
    return score


def rank_arr_candidates(rows, parsed, service=None):
    """Gate and rank arr rows for a parsed release — best first, `[]` for none.

    The replacement for `rows[0]`, which is what auditorr took everywhere a
    title lookup returned more than one row. Rows from every connection are
    pooled under one title key, so `[0]` meant "whichever instance
    `normalize_arr_connections` emitted first" — an answer with no relationship
    to the release being matched. Two Sonarrs holding the same series at 1080p
    and 4K is a legitimate configuration, and the wrong one produces the wrong
    quality comparison, the wrong pre-selected delete scope, and commands
    dispatched to an instance that cannot confirm them.

    Gates first: `service` when given (a *gate*, never a sort key — see
    TRIAGE T1) and `arr_year_ok`. Then ranks on the season/episode anchor, year
    agreement, and whether the item holds a file. **The sort is stable**, so
    rows that nothing distinguishes keep their input order and the answer is
    byte-identical to the old `[0]` on a single-instance install.

    Pure: rows in, rows out, no I/O — which is what makes it testable on an
    install that has only one instance of each service to offer it.
    """
    gated = [r for r in rows
             if (service is None or r.get('service') == service) and arr_year_ok(parsed, r)]
    return sorted(gated, key=lambda r: -_arr_candidate_score(r, parsed))


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


def _under_root(path, root):
    """True when path is root itself or sits inside it, on a separator boundary.

    Guards against /data/media-extra reading as a sub-path of /data/media.
    """
    return bool(root) and path.startswith(root) and path[len(root):][:1] in ('/', '')


def _scan_target(abs_path, local_path):
    """The path to hand Sonarr/Radarr for a downloaded file.

    A release folder is the better scan target than a bare file — it lets the
    arr import a whole season pack from one command — but only the release
    folder: both arrs parse the folder's own name to identify the title, so any
    other folder imports nothing. Torrent layouts put a category dir first
    (torrents/radarr/Film.mkv), so a blind dirname() on a single-file torrent
    hands over the download root and the arr parses "radarr" as the release
    name; on a nested pack it hands over "Season 1", which parses no better.
    The release folder is the second segment below local_path — the same
    "never a category dir" rule Triage uses for its exclusion granularity — and
    anything shallower scans the file itself, which both arrs accept and parse
    from its own filename.
    """
    if not _under_root(abs_path, local_path):
        return abs_path
    segments = [seg for seg in abs_path[len(local_path):].split('/') if seg]
    if len(segments) < 3:
        return abs_path
    return local_path.rstrip('/') + '/' + '/'.join(segments[:2])


def _local_to_abs(path, local_path):
    """Absolute container-side path for a stored (root-relative) file path.

    Posix all the way down, not os.path: these are container-side paths whatever
    platform the process runs on, and os.path.isabs answers False for "/data/…"
    on Windows (3.13+ reads a rooted path with no drive as relative).
    """
    if path.startswith('/'):
        return path
    return (local_path.rstrip('/') + '/' + path) if local_path else path


def _remote_path_for(abs_path, local_path, remote_path):
    """Translate an auditorr-local path to the path the arr container sees."""
    if remote_path and _under_root(abs_path, local_path):
        return remote_path + abs_path[len(local_path):]
    return abs_path


def arr_import_target(path, local_path, remote_path):
    """(scan_path, file_path) as the arr sees them, for one stored path.

    scan_path is what both a scan command and a manualimport lookup should be
    pointed at (see _scan_target); file_path is the specific file.
    """
    abs_path  = _local_to_abs(path, local_path)
    arr_file  = _remote_path_for(abs_path, local_path, remote_path)
    scan_path = _remote_path_for(_scan_target(abs_path, local_path), local_path, remote_path)
    return scan_path, arr_file


def import_rejections(conn, target, timeout=30):
    """What the arr would say about importing this target, without importing it.

    /api/v3/manualimport runs the same decision specs the scan command does and
    reports them per file, which is the only way to tell a rescan that imported
    nothing from one that imported everything — the command endpoint reports
    'completed' either way. Returns [{path, rejections: [reason, …]}].

    `target` is a _scan_target result: a release folder, or a bare file. Both are
    accepted by the `folder` parameter, and passing the file is what makes a
    single-file torrent work — a category-dir listing identifies nothing, coming
    back with movie null and quality "Unknown" for every row (and 384 rows deep
    on a real library), while the file itself parses cleanly. Never narrow the
    lookup with movieId/seriesId: that does not scope the folder, it *replaces*
    it with the arr's existing library file for that item.

    Advisory only: ManualImport with replaceExistingFiles overrides every
    rejection listed here, which is what the force-import path relies on.
    """
    try:
        encoded = urllib.parse.quote(target, safe='')
        rows = _arr_get(conn['base_url'], conn['api_key'],
                        f'/api/v3/manualimport?folder={encoded}&filterExistingFiles=false',
                        timeout=timeout)
    except Exception as e:
        log.warning("manualimport probe failed for %s: %s", target, e)
        return None
    out = []
    for row in (rows or []):
        reasons = [r.get('reason') for r in (row.get('rejections') or []) if r.get('reason')]
        # A row the arr could not attach to a title carries no rejections at all
        # — there was nothing to evaluate the specs against. Left unsaid that
        # reads as "no objections", which is the failure mode this whole probe
        # exists to remove.
        if not (row.get('movie') or row.get('series')):
            reasons.append('No matching title — the file could not be identified')
        out.append({'path': row.get('path') or '', 'rejections': reasons})
    return out


# Distinct targets probed per rescan request. Each probe is a manualimport parse,
# so an unbounded selection would stall the request. Results are cached per
# target, so a season pack's files all share one probe of their release folder.
_PROBE_FOLDER_LIMIT = 20


def arr_rescan(cfg, service, paths):
    """Shared rescan logic for sonarr and radarr.

    Returns {'count': commands issued, 'results': [{path, scanned, rejections,
    checked}]}. The rejections are what made this look like a silent failure:
    the arr runs its upgrade/revision specs against the file it already holds
    and refuses anything that isn't strictly better, while the command endpoint
    reports 'completed' either way.

    Raises ValueError if the service is not configured, or re-raises network errors.
    """
    svc = _SERVICE_MAP[service]
    connections = normalize_arr_connections(cfg, service=service)
    local_path = cfg.get('LOCAL_PATH', '').strip()
    if not connections:
        raise ValueError(f"{svc['name']} not configured")

    command_count = 0
    results = []
    probes = {}

    def _probe(conn, target):
        key = (conn['id'], target)
        if key not in probes:
            if len(probes) >= _PROBE_FOLDER_LIMIT:
                return None
            probes[key] = import_rejections(conn, target)
        return probes[key]

    for conn in connections:
        remote_path = conn.get('remote_path', '').strip()
        for path in paths:
            scan_path, arr_file = arr_import_target(path, local_path, remote_path)
            rows = _probe(conn, scan_path)

            reasons = []
            if rows is not None:
                # A folder scan acts on everything under it; a file scan on one row.
                matched = rows if scan_path != arr_file else [r for r in rows if r['path'] == arr_file]
                if not matched:
                    reasons = [f"{svc['name']} does not list this file as importable"]
                elif all(r['rejections'] for r in matched):
                    reasons = list(dict.fromkeys(r for m in matched for r in m['rejections']))

            _arr_command(conn['base_url'], conn['api_key'], svc['command'], scan_path)
            command_count += 1
            results.append({
                'path':       path,
                'scanned':    scan_path,
                'rejections': reasons,
                'checked':    rows is not None,
            })
    return {'count': command_count, 'results': results}


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
    result_url = link_base(best_conn) + svc['slug_prefix'] + best['titleSlug']
    return {"url": result_url, "title": best.get('title', title), "connection_id": best_conn['id']}
