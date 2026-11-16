import fnmatch
import os
import re

_LITERAL_PREFIX = "literal:"
# A stand-in file name, for reading a folder's spellings off `_variants`.
_PROBE = "x"
# True where `fnmatch.fnmatch` compares a path as it's written (POSIX). On
# Windows it folds case and reads "/" as "\\".
_CASE_EXACT = os.path.normcase("A/b") == "A/b"

_LITERAL_DOC = """literal:<path> — a path that is a path, not a glob (CLEANUP C8).

Every other path rule here runs through fnmatch, which reads `*`, `?`, `[` and
`]` as syntax. Real release names contain all four, and the two halves fail in
opposite directions:

    anime/[SubsPlease] Show - 01 [1080p].mkv   -> a character class; matches NOTHING
    movies/Film (2020)/Film*.mkv               -> matches itself AND Film2.mkv

Both are silent: the Exclude button reports success either way, and the second
hides files the user never selected. `literal:` is what the Cleanup and Triage
Exclude actions write, so a path built from a real file is matched as that file.

Two sub-forms:
    literal:movies/Some Release/Some.Release.mkv   exact file
    literal:movies/Some Release/                   subtree (trailing slash)

Both keep the container-path tolerance every other rule has (`_variants`), so a
pattern written from a relative path still matches the absolute one — without
that, every exclusion written from a workflow page would silently stop working.

Escaping at construction was the alternative and was rejected: fnmatch's escape
for a literal `[` is `[[]`, so it would write
`anime/[[]SubsPlease[]] Show - 01 [[]1080p[]].mkv` into a textarea the user is
meant to read and edit — which defeats the "show the pattern before writing it"
confirm step that ships alongside this. It also cannot express the `*`/`?`
over-match any more legibly.

Pre-existing broken patterns are deliberately NOT migrated: rewriting somebody's
exclusion list without being asked is worse than leaving a rule that matches
nothing, and the rewrite would be invisible.
"""


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
        "_seg_glob_rx",
        "_needs_candidates",
        "_subtree_prefixes",
        "_path_glob_variants",
        "_plain_path_variants",
        "_literal_files",
        "_literal_prefixes",
        "_glob_suffixes",
        "_glob_suffix_len",
        "_glob_suffix_tails",
        "_glob_prefixes",
        "_glob_rest_rx",
        "_dir_memo",
        "_name_needles",
        "_slash_needles",
        "_names_by_folder",
        "_prefix_rules",
        "_name_globs",
        "_whole_globs",
        "_whole_paths",
    )

    def __init__(self, patterns):
        self._extensions = set()        # from ext: rules and pure *.<ext> globs
        self._exact_names = set()       # from name:, barewords, exact names (lowercased)
        self._contains_needles = []     # from contains: rules
        self._seg_globs = []            # glob patterns with no "/" (e.g. fanart*.jpg)
        self._subtree_prefixes = []     # precomputed prefix tuples (/** and trailing /)
        self._path_glob_variants = []   # precomputed variant tuples for path globs
        self._plain_path_variants = []  # precomputed variant tuples for plain path+prefix
        self._literal_files = set()     # from literal:<path>   — exact, never a glob
        self._literal_prefixes = []     # from literal:<path>/  — subtree, never a glob

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

            # literal: — a path that is a path, not a glob. See _LITERAL_DOC.
            # The value is taken off `pat`, not `pat_lower`: everything else in
            # this loop is case-folded, but a filesystem path is not.
            if pat_lower.startswith(_LITERAL_PREFIX):
                value = pat[len(_LITERAL_PREFIX):].strip()
                if not value:
                    continue
                variants = _variants([value])
                if value.endswith("/"):
                    self._literal_prefixes.append(
                        tuple(dict.fromkeys(p.rstrip("/") for p in variants)))
                else:
                    self._literal_files.update(variants)
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

        # Every segment glob as one regex, matched once per segment. It's
        # `fnmatch.fnmatch` exactly — `fnmatchcase(normcase(name), normcase(pat))`
        # — so `is_excluded`, which still loops, gives the same answer. Looping
        # cost one fnmatch per glob per segment: with the Plex preset's ~130 art
        # globs, 200,000 files took 29.4 s on Linux, and 4.7 s this way.
        self._seg_glob_rx = (
            re.compile("|".join(f"(?:{fnmatch.translate(os.path.normcase(p))})"
                                for p in self._seg_globs))
            if self._seg_globs else None)
        # The same globs split by shape, for `_glob_hit`. `*literal` is
        # `endswith` and `literal*` is `startswith`, which is what their
        # `fnmatch.translate` says (`(?s:.*literal)\\Z`, `(?s:literal.*)\\Z`);
        # only the rest need the regex. A preset's art globs are nearly all
        # `*-poster.jpg`, and a regex alternation tries each one against the
        # whole name, where a video's last characters rule them all out at once.
        suffixes, prefixes, rest = [], [], []
        for p in self._seg_globs:
            q = os.path.normcase(p)
            if len(q) > 1 and q[0] == "*" and not _has_glob(q[1:]):
                suffixes.append(q[1:])
            elif len(q) > 1 and q[-1] == "*" and not _has_glob(q[:-1]):
                prefixes.append(q[:-1])
            else:
                rest.append(q)
        self._glob_suffixes = tuple(suffixes)
        self._glob_suffix_len = min(map(len, suffixes)) if suffixes else 0
        self._glob_suffix_tails = frozenset(s[-self._glob_suffix_len:] for s in suffixes)
        self._glob_prefixes = tuple(prefixes)
        self._glob_rest_rx = (
            re.compile("|".join(f"(?:{fnmatch.translate(p)})" for p in rest)) if rest else None)
        # The last folder asked about and its answer: `(full dir, rel dir, hit,
        # names, prefixes)`, the last two from `_folder_paths`.
        self._dir_memo = (None, None, False, None, None)
        # The buckets that read the path variants. Segment globs don't, so a
        # rule set without these never builds them — which is every install
        # with no hand-written path rule.
        self._needs_candidates = bool(
            self._contains_needles
            or self._subtree_prefixes
            or self._path_glob_variants
            or self._plain_path_variants
            or self._literal_files
            or self._literal_prefixes
        )
        # Those same rules as the short way reads them. Every spelling of a
        # file's path that they try is a spelling of its folder followed by its
        # name (`_folder_paths`), so a rule is answered by the folder, once, or
        # comes down to the name:
        #   contains:      the needle is in a folder spelling or in the name. One
        #                  holding a "/" can also span the two, so it's tried
        #                  against each whole spelling, per file.
        #   literal: file  the rule up to its last "/" is the folder, the rest
        #                  is the name. A plain path too, where it's an equal.
        #   subtree and    `_matches_prefix`, split at the rule's last "/" the
        #   plain path     same way (see `_folder_paths`).
        #   path glob      one whose last segment is a plain name: the folder
        #                  matches what's before it and the name is that name.
        #                  Any other glob is matched whole, per file.
        # A plain path and a glob go through `fnmatch.fnmatch`, which folds case
        # and separators where the platform does. Splitting at a "/" is the same
        # test only where it folds nothing; elsewhere those two stay whole.
        self._name_needles = tuple(n for n in self._contains_needles if "/" not in n)
        self._slash_needles = tuple(n for n in self._contains_needles if "/" in n)
        names_by_folder, prefix_rules, name_globs, whole_globs = {}, {}, [], []

        def _file(path):
            name = path.rpartition("/")[2]
            names_by_folder.setdefault(path[:len(path) - len(name)], set()).add(name)

        def _prefix(prefix):
            prefix = prefix.lstrip("/")
            name = prefix.rpartition("/")[2]
            folder = prefix[:len(prefix) - len(name)]
            prefix_rules[prefix] = (f"{prefix}/", f"/{prefix}/", folder, f"/{folder}", name)

        for path in self._literal_files:
            _file(path)
        for prefixes in self._literal_prefixes + self._subtree_prefixes:
            for prefix in prefixes:
                _prefix(prefix)
        for variants in self._plain_path_variants:
            for path in variants:
                _prefix(path)
                if _CASE_EXACT:
                    _file(path)
                else:
                    whole_globs.append(path)
        for variants in self._path_glob_variants:
            for pattern in variants:
                folder, slash, name = pattern.rpartition("/")
                if _CASE_EXACT and slash and name and not _has_glob(name):
                    name_globs.append((folder, name))
                else:
                    whole_globs.append(pattern)
        self._names_by_folder = names_by_folder
        self._prefix_rules = tuple(prefix_rules.values())
        self._name_globs = tuple(dict.fromkeys(name_globs))
        self._whole_globs = tuple(dict.fromkeys(whole_globs))
        self._whole_paths = bool(self._slash_needles or self._whole_globs)

    def match_names(self, path):
        """`match` for a rule set of names and name globs only, one pass over `path`.

        For `media_server_exclusions.is_always_excluded_path`, which runs per
        stored record. Those rules read only file and folder names, so this
        gives `match`'s answer without its three path spellings. Refuses a rule
        set that reads anything else.
        """
        if self._needs_candidates or self._extensions:
            raise ValueError("match_names is only for name and name-glob rules")
        names, rx, normcase = self._exact_names, self._seg_glob_rx, os.path.normcase
        for s in _norm(path).split("/"):
            if s.lower() in names or (rx is not None and rx.match(normcase(s))):
                return True
        return False

    def _glob_hit(self, name):
        """`self._seg_glob_rx.match(normcase(name))`, without trying every glob."""
        s = os.path.normcase(name)
        if self._glob_suffixes and s[-self._glob_suffix_len:] in self._glob_suffix_tails \
                and s.endswith(self._glob_suffixes):
            return True
        if self._glob_prefixes and s.startswith(self._glob_prefixes):
            return True
        return self._glob_rest_rx is not None and self._glob_rest_rx.match(s) is not None

    def match(self, full_path, rel_path, filename):
        """Return True when the file matches any compiled exclusion rule."""
        # The walk asks about a folder's files one after another, and everything
        # a rule can read off the folder's own names is the same for all of
        # them, so it's worked out once and kept until the folder changes. Only
        # for the spelling the walk produces (no backslash, no doubled slash,
        # the name the last segment of both paths), where `_norm` changes
        # nothing; anything else takes the long way below.
        if (type(full_path) is str and type(rel_path) is str and type(filename) is str
                and filename and "/" not in filename
                and "\\" not in full_path and "//" not in full_path
                and "\\" not in rel_path and "//" not in rel_path):
            tail = "/" + filename
            if full_path.endswith(tail) and (rel_path == filename or rel_path.endswith(tail)):
                dir_full = full_path[:-len(tail)]
                dir_rel = None if rel_path == filename else rel_path[:-len(tail)]
                memo = self._dir_memo
                if memo[0] != dir_full or memo[1] != dir_rel:
                    segs = set() if dir_rel is None else set(dir_rel.split("/"))
                    rest = dir_full.lstrip("/")
                    if rest:
                        segs.update(rest.split("/"))
                    hit = bool(
                        (self._exact_names and any(s.lower() in self._exact_names for s in segs))
                        or (self._seg_globs and any(self._glob_hit(s) for s in segs)))
                    names = prefixes = None
                    if self._needs_candidates and not hit:
                        hit, names, prefixes = self._folder_paths(dir_full, dir_rel)
                    memo = self._dir_memo = (dir_full, dir_rel, hit, names, prefixes)
                if memo[2]:
                    return True
                if self._extensions and os.path.splitext(filename)[1].lower() in self._extensions:
                    return True
                if self._exact_names and filename.lower() in self._exact_names:
                    return True
                if self._seg_globs and self._glob_hit(filename):
                    return True
                if not self._needs_candidates:
                    return False
                # The whole-path rules. What they read off the folder is in the
                # memo, and what's left for the file is its own name.
                if filename in memo[3]:
                    return True
                if self._name_needles:
                    low = filename.lower()
                    for needle in self._name_needles:
                        if needle in low:
                            return True
                return self._whole_paths and self._whole_path_hit(memo[4], filename)
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

        rx = self._seg_glob_rx
        if rx is not None:
            normcase = os.path.normcase
            if rx.match(normcase(filename_norm)) or any(
                    rx.match(normcase(s)) for s in segments):
                return True

        # Remaining buckets need the full candidates list
        if not self._needs_candidates:
            return False

        return self._candidates_hit(_variants([filename_norm, rel_norm, full_norm, full_no_root]))

    def _folder_paths(self, dir_full, dir_rel):
        """What the whole-path rules read off a folder: `(hit, names, prefixes)`.

        `_candidates_hit` tries a file's name and each spelling `_variants`
        gives its relative and full paths. A name holds no "/", so `_variants`
        (strip the leading slashes; cut at the first "/data/") only ever
        changes the folder's part: each spelling is one of the folder's
        `prefixes`, ending in "/" or empty, followed by the name. They're read
        off `_variants` itself with a stand-in name, so they can't drift.

        `hit` is a rule the folder answers for every file in it. `names` are
        the file names in this folder that a rule names. For `_matches_prefix`,
        with a spelling `q + name` and a rule `r` cut at its last "/" into
        `folder` and `name`: `r/` at the start and `/r/` inside end in a "/",
        so they sit inside `q`; equal to `r`, or ending in `/r`, needs the same
        name, and `q` equal to `folder` or ending in `/folder`.
        """
        probe = f"{dir_full}/{_PROBE}"
        spellings = [_PROBE, probe, probe.lstrip("/")]
        if dir_rel is not None:
            spellings.insert(1, f"{dir_rel}/{_PROBE}")
        prefixes = tuple(c[:-len(_PROBE)] for c in _variants(spellings))
        if self._contains_needles:
            for prefix in prefixes:
                low = prefix.lower()
                for needle in self._contains_needles:
                    if needle in low:
                        return True, None, None
        names = set()
        by_folder = self._names_by_folder
        if by_folder:
            for prefix in prefixes:
                if prefix in by_folder:
                    names.update(by_folder[prefix])
        if self._prefix_rules:
            rooted = tuple(dict.fromkeys(p.lstrip("/") for p in prefixes))
            for inside, between, folder, under, name in self._prefix_rules:
                for q in rooted:
                    if q.startswith(inside) or between in q:
                        return True, None, None
                    if q == folder or q.endswith(under):
                        names.add(name)
        for folder_glob, name in self._name_globs:
            for prefix in prefixes:
                if prefix and fnmatch.fnmatchcase(prefix[:-1], folder_glob):
                    names.add(name)
                    break
        return False, names, prefixes

    def _whole_path_hit(self, prefixes, filename):
        """The rules a name alone can't answer, against each spelling of the file's path."""
        for prefix in prefixes:
            candidate = prefix + filename
            if self._slash_needles:
                low = candidate.lower()
                for needle in self._slash_needles:
                    if needle in low:
                        return True
            for pattern in self._whole_globs:
                if fnmatch.fnmatch(candidate, pattern):
                    return True
        return False

    def _candidates_hit(self, candidates):
        """The rules that read whole paths: contains:, literal: and path rules."""
        for needle in self._contains_needles:
            if any(needle in c.lower() for c in candidates):
                return True

        if self._literal_files and any(c in self._literal_files for c in candidates):
            return True

        for prefixes in self._literal_prefixes:
            for prefix in prefixes:
                if _matches_prefix(prefix, candidates):
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


def reads_bracket_as_glob(pattern):
    """True when a path-shaped rule carries a `[` that both matchers read as glob syntax.

    CLEANUP C8's optional half (decision 4 (b), Phase 14). Since Phase 5 every
    rule auditorr builds from a path is `literal:`; what is left is a hand-typed
    or pre-Phase-5 rule, where `[SubsPlease]` is a character class and the rule
    silently matches nothing. The Config page warns on save and never blocks it.

    Mirrors how `CompiledExclusions` routes a rule, so it warns only where the
    rule really reaches `fnmatch`: not a typed rule (`literal:`, `ext:`, `name:`,
    `contains:`), not a bareword, and not a subtree (`…/` or `…/**`), which both
    matchers take as a plain prefix. Only `[` — a lone `]` is literal to
    `fnmatch`, and `*`/`?` in a hand-typed rule are usually meant.
    """
    pat = _norm(str(pattern or "").strip())
    if not pat or pat.startswith("#") or "/" not in pat or "[" not in pat:
        return False
    if pat.lower().startswith(("ext:", "name:", "contains:", _LITERAL_PREFIX)):
        return False
    return not (pat.endswith("/") or pat.endswith("/**"))


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
      - literal:movies/Film [2020].mkv excludes that exact path, glob chars and all

    NOTE: this is the second of **two** implementations of these rules —
    CompiledExclusions.match above is the one the audit uses. They are held in
    agreement by
    test_exclusions_and_relink.py::test_compiled_matcher_agrees_with_is_excluded_across_all_pattern_types,
    so a new rule type goes in both and in that test's corpus.
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

        # literal: — never reaches fnmatch. See _LITERAL_DOC.
        if pat_lower.startswith(_LITERAL_PREFIX):
            value = pat[len(_LITERAL_PREFIX):].strip()
            if not value:
                continue
            lit_variants = _variants([value])
            if value.endswith("/"):
                if any(_matches_prefix(p.rstrip("/"), candidates) for p in lit_variants):
                    return True
            elif any(c in set(lit_variants) for c in candidates):
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
