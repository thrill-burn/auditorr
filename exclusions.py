import fnmatch
import os


class CompiledExclusions:
    """Pre-compiled exclusion rule set — do per-pattern work once, not per file.

    Pattern variants and type classification happen in __init__. match() only
    does the per-file work: normalise the three path forms, then check each
    precomputed bucket in cheapest-first order.
    """

    __slots__ = (
        "_extensions",
        "_exact_names",
        "_contains_needles",
        "_seg_globs",
        "_subtree_prefixes",
        "_path_glob_variants",
        "_plain_path_variants",
    )

    def __init__(self, patterns):
        self._extensions = set()        # from ext: rules and pure *.<ext> globs
        self._exact_names = set()       # from name:, barewords, exact names (lowercased)
        self._contains_needles = []     # from contains: rules
        self._seg_globs = []            # glob patterns with no "/" (e.g. fanart*.jpg)
        self._subtree_prefixes = []     # precomputed prefix tuples (/** and trailing /)
        self._path_glob_variants = []   # precomputed variant tuples for path globs
        self._plain_path_variants = []  # precomputed variant tuples for plain path+prefix

        for raw in patterns:
            pat = _norm(str(raw).strip())
            if not pat or pat.startswith("#"):
                continue
            pat_lower = pat.lower()

            if pat_lower.startswith("ext:"):
                ext = pat_lower[4:].strip()
                if ext and not ext.startswith("."):
                    ext = f".{ext}"
                if ext:
                    self._extensions.add(ext)
                continue

            if pat_lower.startswith("name:"):
                name = pat_lower[5:].strip("/")
                if name:
                    self._exact_names.add(name)
                continue

            if pat_lower.startswith("contains:"):
                needle = pat_lower[9:]
                if needle:
                    self._contains_needles.append(needle)
                continue

            # No "/" and no glob → bareword, exact segment match O(1)
            if "/" not in pat and not _has_glob(pat):
                self._exact_names.add(pat_lower)
                continue

            # No "/" with glob → pure *.<ext> into ext bucket, else seg_glob
            if "/" not in pat:
                if (
                    pat_lower.startswith("*.")
                    and pat_lower.count("*") == 1
                    and "?" not in pat
                    and "[" not in pat
                ):
                    self._extensions.add(pat_lower[1:])  # e.g. ".nfo"
                else:
                    self._seg_globs.append(pat)
                continue

            # Has "/": compute pattern variants once
            pat_variants = tuple(_variants([pat]))

            # Subtree patterns (/** or trailing /): extract prefix, precompute
            subtree_prefixes = []
            for p in pat_variants:
                if p.endswith("/**"):
                    subtree_prefixes.append(p[:-3].rstrip("/"))
                elif p.endswith("/"):
                    subtree_prefixes.append(p.rstrip("/"))
            if subtree_prefixes:
                # dict.fromkeys preserves order and deduplicates
                self._subtree_prefixes.append(tuple(dict.fromkeys(subtree_prefixes)))
                continue

            if _has_glob(pat):
                self._path_glob_variants.append(pat_variants)
            else:
                # Plain path: both fnmatch on variants AND subtree-prefix check
                self._plain_path_variants.append(pat_variants)

    def match(self, full_path, rel_path, filename):
        """Return True when the file matches any compiled exclusion rule."""
        rel_norm = _norm(rel_path)
        full_norm = _norm(full_path)
        full_no_root = full_norm.lstrip("/")
        filename_norm = _norm(filename)
        segments = set(rel_norm.split("/") + full_no_root.split("/"))

        # O(1) extension check
        if self._extensions:
            if os.path.splitext(filename_norm)[1].lower() in self._extensions:
                return True

        # O(1) exact segment check
        if self._exact_names and {s.lower() for s in segments} & self._exact_names:
            return True

        # Remaining buckets need the full candidates list
        if not (
            self._contains_needles
            or self._seg_globs
            or self._subtree_prefixes
            or self._path_glob_variants
            or self._plain_path_variants
        ):
            return False

        candidates = _variants([filename_norm, rel_norm, full_norm, full_no_root])

        for needle in self._contains_needles:
            if any(needle in c.lower() for c in candidates):
                return True

        for pat in self._seg_globs:
            if fnmatch.fnmatch(filename_norm, pat) or any(
                fnmatch.fnmatch(s, pat) for s in segments
            ):
                return True

        for prefixes in self._subtree_prefixes:
            for prefix in prefixes:
                if _matches_prefix(prefix, candidates):
                    return True

        for pat_variants in self._path_glob_variants:
            for c in candidates:
                for p in pat_variants:
                    if fnmatch.fnmatch(c, p):
                        return True

        for pat_variants in self._plain_path_variants:
            for c in candidates:
                for p in pat_variants:
                    if fnmatch.fnmatch(c, p):
                        return True
            for p in pat_variants:
                if _matches_prefix(p, candidates):
                    return True

        return False


def compile_exclusions(patterns):
    """Pre-compile a pattern list into a reusable matcher.

    Call once per audit (after expand_exclusion_patterns), then call
    compiled.match(full_path, rel_path, filename) for each file.
    """
    return CompiledExclusions(patterns)


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
