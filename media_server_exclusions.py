_IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "tbn")


def _art(*names):
    patterns = []
    for name in names:
        patterns.extend(f"{name}.{ext}" for ext in _IMAGE_EXTENSIONS)
        patterns.extend(f"*-{name}.{ext}" for ext in _IMAGE_EXTENSIONS)
    return patterns


MEDIA_SERVER_EXCLUSION_PRESETS = {
    "plex": [
        "**/.plexmatch",
        "*.nfo",
        "movie.nfo",
        "tvshow.nfo",
        "season.nfo",
    ] + _art(
        "poster", "folder", "cover", "background", "fanart*", "art", "backdrop*", "logo",
        "clearlogo*", "square*", "squareArt*", "backgroundSquare*", "show",
    ),
    "jellyfin": [
        "*.nfo",
        "movie.nfo",
        "tvshow.nfo",
        "season.nfo",
        "artist.nfo",
        "album.nfo",
        "extrafanart",
    ] + _art(
        "poster", "folder", "cover", "default", "movie", "show", "jacket",
        "backdrop*", "fanart*", "background", "art", "banner", "logo",
        "clearlogo*", "landscape", "thumb",
    ),
    "emby": [
        "*.nfo",
        "movie.nfo",
        "tvshow.nfo",
        "season.nfo",
        "artist.nfo",
        "album.nfo",
    ] + _art(
        "poster", "folder", "cover", "fanart*", "backdrop*", "background",
        "banner", "logo", "clearlogo*", "landscape", "thumb",
    ),
    "kodi": [
        "*.nfo",
        "movie.nfo",
        "tvshow.nfo",
        "season.nfo",
        "artist.nfo",
        "album.nfo",
        "extrafanart",
    ] + _art(
        "poster", "folder", "cover", "fanart*", "backdrop*", "background",
        "banner", "logo", "clearlogo*", "landscape", "thumb",
    ),
    "ums": [
        "*.nfo",
    ] + _art("folder", "cover", "albumart", "poster", "fanart*", "background"),
}


def normalize_media_server_presets(values):
    if not isinstance(values, list):
        return []
    seen = set()
    normalized = []
    for value in values:
        key = str(value or "").strip().lower()
        if key in MEDIA_SERVER_EXCLUSION_PRESETS and key not in seen:
            seen.add(key)
            normalized.append(key)
    return normalized


def expand_exclusion_patterns(cfg):
    patterns = [
        pattern
        for pattern in cfg.get("EXCLUSION_PATTERNS", [])
        if isinstance(pattern, str) and pattern.strip()
    ]
    for preset in normalize_media_server_presets(cfg.get("MEDIA_SERVER_EXCLUSION_PRESETS", [])):
        patterns.extend(MEDIA_SERVER_EXCLUSION_PRESETS[preset])
    return _dedupe(patterns)


def _dedupe(patterns):
    seen = set()
    result = []
    for pattern in patterns:
        key = pattern.strip()
        lower_key = key.lower()
        if key and lower_key not in seen:
            seen.add(lower_key)
            result.append(key)
    return result
