import fnmatch

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


DISC_RIP_EXCLUSION_PRESETS = {
    "bluray": [
        "BDMV",
        "CERTIFICATE",
    ],
    "dvd": [
        "VIDEO_TS",
        "AUDIO_TS",
    ],
}


def normalize_media_server_presets(values):
    return _normalize_presets(values, MEDIA_SERVER_EXCLUSION_PRESETS)


def normalize_disc_rip_presets(values):
    return _normalize_presets(values, DISC_RIP_EXCLUSION_PRESETS)


def _normalize_presets(values, allowed):
    if not isinstance(values, list):
        return []
    seen = set()
    normalized = []
    for value in values:
        key = str(value or "").strip().lower()
        if key in allowed and key not in seen:
            seen.add(key)
            normalized.append(key)
    return normalized


# Filesystem tombstones — always excluded, on every install, not a preset.
#
# Unlink a file a process still has open and the filesystem cannot free it, so
# it renames it out of the way and drops that name when the last handle closes:
# FUSE (Unraid's shfs) uses `.fuse_hiddenXXXXXXXXXXXXXXXX`, NFS uses `.nfsXXXX`.
# **The file has already been deleted. What is left is the filesystem's record
# of a delete it has not finished.**
#
# Observed on the reference box, and it reached every workflow that offers an
# action. Unraid's mover copied a 3.5 GB episode from cache to array and could
# not unlink the source because qBittorrent held it open, so one file mid-move
# appeared as two:
#
#   Dedupe   grouped the tombstone with the live episode and picked the
#            TOMBSTONE as the canonical copy — the group's canonical is its
#            smallest path and `.` sorts ahead of every release name — so the
#            script would have replaced a real file with a hardlink to a name
#            the filesystem is in the middle of removing, and reported
#            reclaiming 3.5 GB that frees itself. `cmp` cannot catch it: the two
#            are genuinely identical, being the same file.
#   Cleanup  offered it as an orphan with 3.5 GB "freed if deleted". `rm` on it
#            frees nothing — the handle owns the inode, not the name.
#   Triage   it is an unclaimed file inside a live torrent's release folder, so
#            it blocks that torrent's folder-level exclusion (T6).
#   Score    3.5 GB of orphaned bytes against the health score, and a broken
#            Sentinel streak, for a file the user already deleted.
#
# None of those actions is available to fix it either: a tombstone goes away
# when the process closes the handle, and nothing auditorr can generate will
# hurry that along. So it is not a workflow row — it is noise from a delete in
# progress.
#
# **Always on rather than a preset, and not silent.** Excluded files still show
# in File Explorer marked excluded and still count in Cleanup's Excluded box, so
# this hides nothing the user cannot see; it is simply not something to opt into,
# because it is filesystem bookkeeping rather than anybody's media.
TOMBSTONE_PATTERNS = [".fuse_hidden*", ".nfs*"]


def is_tombstone_path(path):
    """True for a path whose filename is a filesystem tombstone.

    Shares TOMBSTONE_PATTERNS with the exclusion list so there is one definition
    of what a tombstone is. Used where a stored record may predate the exclusion
    (a scan has not run since the upgrade) and the next step is destructive.
    """
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) for p in TOMBSTONE_PATTERNS)


def expand_exclusion_patterns(cfg):
    patterns = [
        pattern
        for pattern in cfg.get("EXCLUSION_PATTERNS", [])
        if isinstance(pattern, str) and pattern.strip()
    ]
    patterns.extend(TOMBSTONE_PATTERNS)
    for preset in normalize_disc_rip_presets(cfg.get("DISC_RIP_EXCLUSION_PRESETS", [])):
        patterns.extend(DISC_RIP_EXCLUSION_PRESETS[preset])
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
