from exclusions import compile_exclusions

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


# Operating-system and NAS clutter — always excluded too, for the same two
# reasons (CLEANUP C24). None of it is anybody's media or any torrent's, and
# each kind belongs to something that manages it: a desktop's folder settings
# and thumbnail caches, a NAS's thumbnail folders and recycle bin, a
# filesystem's snapshots, `fsck`'s lost+found. A Mac browsing a share writes
# `.DS_Store` into every folder it opens, which made each one an orphan in
# Cleanup and blocked that torrent's one-rule folder exclusion in Triage (T6).
# A snapshot folder's files are read-only copies a delete script can't remove.
#
# The list is qui's Orphan Scan default ignores (autobrr/qui,
# documentation/docs/_partials/_orphan-scan-default-ignores.mdx), with three
# differences, each deliberate:
#
#   * qui's `..*` folder prefix is left out. It's for Kubernetes volume
#     internals (`..data`), which never sit in a torrent folder, and it would
#     also hide a real release such as the album `...And Justice for All`.
#   * qui's `*.parts` and `*.!qB` are left out. auditorr claims both for a torrent
#     still in the client (C18, C4a), and with the torrent gone they're junk
#     worth listing.
#   * QNAP's `@Recycle` and `.@__thumb` and Synology's `#snapshot` are added.
#
# `name:` is an exact file or folder name anywhere in the path, case-insensitive,
# so everything under `@eaDir/` is excluded. `#recycle` has to be written with
# it: a rule starting with `#` is a comment.
SYSTEM_CLUTTER_PATTERNS = [
    # Files, by exact name.
    "name:.DS_Store", "name:.directory", "name:desktop.ini", "name:Thumbs.db",
    # Files, by prefix: AppleDouble, GNOME's half-written saves, editor and
    # Office lock files.
    "._*", ".goutputstream-*", ".#*", "~$*",
    # Folders, by exact name: trash and recycle bins, snapshots, thumbnail
    # caches, filesystem bookkeeping.
    "name:.AppleDB", "name:.AppleDouble", "name:.TemporaryItems", "name:.Trashes",
    "name:.Recycle.Bin", "name:.recycle", "name:#recycle", "name:@Recycle",
    "name:$RECYCLE.BIN", "name:.snapshot", "name:.snapshots", "name:#snapshot",
    "name:.zfs", "name:@eaDir", "name:.@__thumb", "name:lost+found",
    "name:System Volume Information",
    # Folders, by prefix: a desktop's per-user trash on a removable disk.
    ".Trash-*",
]

ALWAYS_EXCLUDED_PATTERNS = TOMBSTONE_PATTERNS + SYSTEM_CLUTTER_PATTERNS

_ALWAYS_MATCHER = compile_exclusions(ALWAYS_EXCLUDED_PATTERNS)


def is_always_excluded_path(path):
    """True for a path an always-on rule excludes: a tombstone, or OS/NAS clutter.

    Shares ALWAYS_EXCLUDED_PATTERNS with the exclusion list so there is one
    definition of each. Used where a stored record may predate the exclusion (a
    scan has not run since the upgrade) and the next step is destructive.
    """
    return _ALWAYS_MATCHER.match_names(path)


def expand_exclusion_patterns(cfg):
    patterns = [
        pattern
        for pattern in cfg.get("EXCLUSION_PATTERNS", [])
        if isinstance(pattern, str) and pattern.strip()
    ]
    patterns.extend(ALWAYS_EXCLUDED_PATTERNS)
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
