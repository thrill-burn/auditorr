import logging
import os
import re
import shlex


log = logging.getLogger(__name__)

RELINK_MAX_PAIRS = 25_000_000

QUALITY_TOKENS = {
    "2160p", "1080p", "1080i", "720p", "480p", "bluray", "bdrip",
    "brrip", "web", "webdl", "web-dl", "webrip", "hdtv", "dvdrip",
    "remux", "x264", "x265", "hevc", "hdr", "dv", "proper", "repack",
}


def find_relink_candidates(media_files, torrent_files, arr_media, cfg, min_confidence=0.65):
    media_root = cfg.get("MEDIA_PATH", "")
    torrent_root = cfg.get("LOCAL_PATH", "")
    arr_by_path = {_norm_path(item.get("path")): item for item in arr_media if item.get("path")}
    candidates = []

    # Pre-build eligible torrents with scoring data computed once, not per media file.
    eligible_torrents = []
    for torrent in torrent_files:
        if torrent.get("excluded") or torrent.get("linked_paths"):
            continue
        if torrent.get("status") == "Orphaned":
            continue
        haystack = _token_text(torrent.get("path", ""))
        token_set = set(_tokens(haystack))
        eligible_torrents.append((torrent, haystack, token_set))

    eligible_media = [
        m for m in media_files
        if not m.get("excluded") and not m.get("linked_paths")
    ]

    pair_count = len(eligible_media) * len(eligible_torrents)
    max_pairs = cfg.get("RELINK_MAX_PAIRS", RELINK_MAX_PAIRS)
    if pair_count > max_pairs:
        log.warning(
            "Relink scan skipped: %d media × %d torrents = %d pairs exceeds "
            "RELINK_MAX_PAIRS (%d). Library is too large for a synchronous relink scan.",
            len(eligible_media), len(eligible_torrents), pair_count, max_pairs,
        )
        return []

    for media in eligible_media:
        media_abs = _abs_path(media_root, media.get("path", ""))
        arr_item = arr_by_path.get(_norm_path(media_abs))
        descriptor = _descriptor_for_media(media, media_abs, arr_item)
        ranked = []

        for torrent, haystack, token_set in eligible_torrents:
            score, reasons = _score_candidate(media, descriptor, torrent, haystack, token_set)
            if score >= min_confidence:
                ranked.append((score, torrent, reasons))

        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            continue

        score, torrent, reasons = ranked[0]
        torrent_abs = _abs_path(torrent_root, torrent.get("path", ""))
        candidates.append({
            "media_path": media_abs,
            "torrent_path": torrent_abs,
            "confidence": round(score, 2),
            "reasons": reasons,
            "arr_connection_id": descriptor.get("connection_id"),
            "arr_service": descriptor.get("service"),
            "arr_title": descriptor.get("title"),
            "torrent_hash": torrent.get("hash", ""),
            "torrent_instance_id": torrent.get("instance_id"),
            "torrent_instance_name": torrent.get("instance_name"),
            "size": media.get("size", 0),
        })

    return candidates


def generate_relink_script(candidates):
    lines = [
        "#!/bin/bash",
        "# auditorr — Media Relink Script",
        "# Review carefully before running. auditorr never executes this script.",
        "# For each candidate, content is compared before the media file is replaced",
        "# with a hardlink to the matching torrent/download file.",
        "set -u",
        "",
    ]

    for i, candidate in enumerate(candidates, start=1):
        media = shlex.quote(candidate["media_path"])
        torrent = shlex.quote(candidate["torrent_path"])
        lines += [
            f'echo "[{i}/{len(candidates)}] {candidate["arr_title"] or candidate["media_path"]}"',
            f"MEDIA={media}",
            f"TORRENT={torrent}",
            'if [ ! -f "$MEDIA" ]; then',
            '  echo "  skip: media missing"',
            'elif [ ! -f "$TORRENT" ]; then',
            '  echo "  skip: torrent file missing"',
            'elif ! cmp -s "$MEDIA" "$TORRENT"; then',
            '  echo "  skip: files differ"',
            'else',
            '  TMP="${MEDIA}.auditorr-unlinked-backup"',
            '  mv "$MEDIA" "$TMP"',
            '  if ln "$TORRENT" "$MEDIA"; then',
            '    rm "$TMP"',
            '    echo "  linked"',
            '  else',
            '    mv "$TMP" "$MEDIA"',
            '    echo "  failed: restored original media file"',
            '  fi',
            'fi',
            "",
        ]

    return "\n".join(lines)


def _descriptor_for_media(media, media_abs, arr_item):
    if arr_item:
        title = arr_item.get("title") or ""
        return {
            "connection_id": arr_item.get("connection_id"),
            "service": arr_item.get("service"),
            "title": title,
            "year": arr_item.get("year"),
            "tokens": _tokens(title),
        }

    fallback = os.path.splitext(os.path.basename(media_abs))[0]
    return {
        "connection_id": None,
        "service": None,
        "title": fallback,
        "year": _year(media_abs),
        "tokens": _tokens(fallback),
    }


def _score_candidate(media, descriptor, torrent, haystack=None, token_set=None):
    if haystack is None:
        haystack = _token_text(torrent.get("path", ""))
    if token_set is None:
        token_set = set(_tokens(haystack))
    torrent_tokens = token_set
    reasons = []
    score = 0.0

    media_size = media.get("size")
    torrent_size = torrent.get("size")
    if media_size and torrent_size:
        delta = abs(media_size - torrent_size) / max(media_size, torrent_size)
        if delta == 0:
            score += 0.25
            reasons.append("exact size match")
        elif delta <= 0.01:
            score += 0.15
            reasons.append("near size match")

    title_tokens = [t for t in descriptor.get("tokens", []) if t not in QUALITY_TOKENS]
    if title_tokens and set(title_tokens).issubset(torrent_tokens):
        score += 0.35
        reasons.append(f"arr title match: {descriptor.get('title')}")
    elif title_tokens:
        overlap = len(set(title_tokens) & torrent_tokens) / len(set(title_tokens))
        if overlap >= 0.6:
            score += 0.2
            reasons.append(f"partial arr title match: {descriptor.get('title')}")

    year = descriptor.get("year")
    if year and str(year) in haystack:
        score += 0.15
        reasons.append(f"year match: {year}")

    service = descriptor.get("service")
    category = (torrent.get("category") or "").lower()
    path_text = (torrent.get("path") or "").lower()
    if service and (service == category or service in path_text):
        score += 0.1
        reasons.append(f"service/category match: {service}")

    if torrent.get("status") == "Seeding":
        score += 0.1
        reasons.append("torrent is seeding")

    if descriptor.get("connection_id"):
        score += 0.1
        reasons.append(f"matched via {descriptor['connection_id']}")

    return min(score, 1.0), reasons


def _tokens(value):
    return [
        t for t in re.split(r"[^a-z0-9]+", str(value).lower())
        if len(t) > 1 and t not in {"the", "and", "of", "a", "an"}
    ]


def _token_text(value):
    return str(value).replace(".", " ").replace("_", " ").replace("-", " ").lower()


def _year(value):
    match = re.search(r"\b(19|20)\d{2}\b", str(value))
    return int(match.group(0)) if match else None


def _abs_path(root, path):
    if not path:
        return _norm_path(root)
    if os.path.isabs(path):
        return _norm_path(path)
    return _norm_path(os.path.join(root, path))


def _norm_path(path):
    return os.path.normpath(str(path or "")).replace("\\", "/")
