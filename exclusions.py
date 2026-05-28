import fnmatch
import os


def is_excluded(full_path, rel_path, filename, patterns):
    """Return True when a file matches a path-aware exclusion rule.

    Rules are intentionally friendlier than raw globs:
      - data/torrents/games excludes a whole subtree from container-root paths
      - /mnt/user/data/torrents/books also matches the same TRaSH path in Docker
      - Featurettes excludes a matching path segment anywhere
      - *.srt keeps legacy glob behavior
      - ext:.nfo excludes by extension
      - name:@eaDir excludes an exact file/folder name
      - contains:sample excludes when text appears anywhere in the normalized path
    """
    if not patterns:
        return False

    rel_norm = _norm(rel_path)
    full_norm = _norm(full_path)
    full_no_root = full_norm.lstrip("/")
    filename_norm = _norm(filename)
    segments = set(rel_norm.split("/") + full_no_root.split("/"))
    candidates = _variants([filename_norm, rel_norm, full_norm, full_no_root])

    for raw in patterns:
        pat = _norm(str(raw).strip())
        if not pat or pat.startswith("#"):
            continue

        pat_lower = pat.lower()

        if pat_lower.startswith("ext:"):
            ext = pat_lower[4:].strip()
            if ext and not ext.startswith("."):
                ext = f".{ext}"
            if os.path.splitext(filename_norm)[1].lower() == ext:
                return True
            continue

        if pat_lower.startswith("name:"):
            name = pat_lower[5:].strip("/")
            if name and name in {s.lower() for s in segments}:
                return True
            continue

        if pat_lower.startswith("contains:"):
            needle = pat_lower[9:]
            if needle and any(needle in c.lower() for c in candidates):
                return True
            continue

        pat_variants = _variants([pat])

        if any(_matches_subtree(p, candidates) for p in pat_variants):
            return True

        if "/" in pat:
            if any(fnmatch.fnmatch(c, p) for c in candidates for p in pat_variants):
                return True
            # Plain path entries are treated as subtree prefixes.
            if not _has_glob(pat) and any(_matches_prefix(p, candidates) for p in pat_variants):
                return True
            continue

        if _has_glob(pat):
            if fnmatch.fnmatch(filename_norm, pat) or any(fnmatch.fnmatch(s, pat) for s in segments):
                return True
            continue

        # Plain words match exact path segments. This handles entries like
        # Featurettes and @eaDir without forcing users to write glob syntax.
        if pat_lower in {s.lower() for s in segments}:
            return True

    return False


def _matches_subtree(pattern, candidates):
    if pattern.endswith("/**"):
        return _matches_prefix(pattern[:-3].rstrip("/"), candidates)
    if pattern.endswith("/"):
        return _matches_prefix(pattern.rstrip("/"), candidates)
    return False


def _matches_prefix(prefix, candidates):
    prefix = prefix.lstrip("/")
    for candidate in candidates:
        normalized = candidate.lstrip("/")
        if (
            normalized == prefix
            or normalized.startswith(f"{prefix}/")
            or normalized.endswith(f"/{prefix}")
            or f"/{prefix}/" in normalized
        ):
            return True
    return False


def _has_glob(pattern):
    return any(ch in pattern for ch in "*?[]")


def _norm(path):
    return str(path or "").replace("\\", "/").replace("//", "/")


def _variants(paths):
    values = []
    for path in paths:
        norm = _norm(path)
        if not norm:
            continue
        values.append(norm)
        no_root = norm.lstrip("/")
        values.append(no_root)
        marker = "/data/"
        if marker in f"/{no_root}":
            tail = f"/{no_root}".split(marker, 1)[1]
            values.append(f"data/{tail}".strip("/"))
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
