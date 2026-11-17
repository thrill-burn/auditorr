"""The exclusion matcher's short way for the walk's own paths (SCAN SC2).

`CompiledExclusions.match` runs once per file on disk. Under the reference
box's rules (five media-server presets) it was most of the two walks: 32.4 s at
650,000 files, 16.0 s with this.

Two things changed, and neither may change an answer:

1. What a folder's own names answer is worked out once and kept until the
   folder changes (`_dir_memo`). Only for a path spelled as the walk spells it:
   plain strings, no backslash, no doubled slash, the name the last segment of
   both paths. Anything else takes the code that was there before.
2. A `*literal` glob is one `endswith` and a `literal*` glob one `startswith`
   (`_glob_hit`). Only the other globs keep the regex.
3. The rules that read whole paths (`contains:`, `literal:`, path rules) are
   answered by the folder, once, and the file's name (SCAN SC13). Every path
   they try is a spelling of the folder followed by the name, so a rule is cut
   at its last `/`; a `contains:` needle holding a `/` can span the two, so it,
   and a path glob that doesn't end in a plain name, is still tried whole.
4. A glob the regex still answers (a star in the middle: the presets'
   `*-fanart*.jpg`) is skipped for a name ending in none of those globs'
   literal tails (SCAN SC14), so a video, a subtitle or a folder never reaches
   it. One glob with no tail turns the skip off.

An exclusion verdict is on every record, so it reaches every list and count.
Each rule set below is answered three ways for every path: the short way, the
long way (the same matcher handed a `str` subclass, which the short way
refuses) and `is_excluded`, the second implementation. The strings are spelled
with `/` whatever the platform, so Windows runs the short way too.
"""

import fnmatch
import itertools
import os
import pathlib
import random

import pytest

from audit import _walk_directory
import exclusions as exclusions_module
from exclusions import compile_exclusions, is_excluded
from media_server_exclusions import (
    DISC_RIP_EXCLUSION_PRESETS,
    MEDIA_SERVER_EXCLUSION_PRESETS,
    expand_exclusion_patterns,
)

EVERYTHING = 'rules that match everything'
RULESETS = {
    'default install': {},
    'plex preset': {'MEDIA_SERVER_EXCLUSION_PRESETS': ['plex']},
    'every preset': {'MEDIA_SERVER_EXCLUSION_PRESETS': list(MEDIA_SERVER_EXCLUSION_PRESETS),
                     'DISC_RIP_EXCLUSION_PRESETS': list(DISC_RIP_EXCLUSION_PRESETS)},
    'the box': {'MEDIA_SERVER_EXCLUSION_PRESETS': ['ums', 'kodi', 'emby', 'jellyfin', 'plex'],
                'EXCLUSION_PATTERNS': ['contains:sample', '*.srr', '*.sfv', '*.srt', '*.txt',
                                       'movies/Some Film (2020)/Some.Film.avi',
                                       'tv/Show/Show.S01E01.mkv']},
    'every rule type': {
        'MEDIA_SERVER_EXCLUSION_PRESETS': ['plex', 'jellyfin'],
        'DISC_RIP_EXCLUSION_PRESETS': ['bluray'],
        'EXCLUSION_PATTERNS': [
            'literal:tv/Show [x]/', 'literal:tv/Show/b.mkv', 'contains:sample',
            'movies/*/extras/*', 'Featurettes', 'ext:sfv', 'name:@eaDir', 'data/torrents/games',
            '/mnt/user/data/torrents/books/**', 'tv/Show/', 'a*b', '?x', '[ab]*', '*]',
            'fan*', '*.JPG', 'x*', '*y', '*.*.bak']},
    'odd globs': {'EXCLUSION_PATTERNS': ['*a', 'a*', 'a*a', '?', '*[', ']*', '*.', '.*']},
    # SC14: every glob the regex answers has a literal tail, so the skip is on.
    'mid-star globs': {'EXCLUSION_PATTERNS': [
        'fanart*.jpg', '*-fanart*.jpg', 'back*drop?.png', 'a*b[cd].tbn', '*x*y.webp',
        'Sample*.mkv', '*S01*.mkv', 'th?mb*.JPG', '*[]]*.nfo']},
    # SC13: every kind of rule the whole-path branch reads. Needles inside a
    # folder, inside a name and across the two (`/x`, `/.`, `show/s`); path
    # globs ending in a plain name and ones that don't; brackets around the
    # last `/`; literal files and subtrees; plain paths as files and folders.
    'whole-path rules': {'MEDIA_SERVER_EXCLUSION_PRESETS': ['plex'], 'EXCLUSION_PATTERNS': [
        'contains:show/s', 'contains:/x', 'contains:a b/', 'contains:x/', 'contains:é',
        'contains:torrents/tv/show/sea', 'contains:w/sa', 'contains:/.', 'contains:recycle/t',
        '**/poster.jpg', 'tv/*/Season 01/x', '*/extras/Movie.mkv', 'data/*/Sample.mkv', '*/a/ab',
        '[ab]/y', 'movies/Film/extras', '/data/torrents/a', 'torrents/@eaDir', 'Show/Movie.nfo',
        '/Movie.mkv', 'x/y/z/w', 'tv/Show/Season 01/Movie.mkv', 'relative/root/a/x',
        'literal:tv/Show [x]/Movie.mkv', 'literal:/data/torrents/a/x', 'literal:Movie.mkv',
        'literal:data/torrents/games/', 'literal:sample/x/', 'literal:/a/ab', 'literal:books/x/a b',
        'tv/Show/Season 01/', 'x/y/z/**', '*/x]', '?/b.mkv', '**/x', '**/[x', '*[/]y', 'a[/]x/y',
        '[!]/x', 'tv/*/*', 'Featurettes/*.nfo', '*/Sample', 'Movie.mkv/', 'books/*/a',
        'movies/Some Film (2020)/Some.Film.avi']},
    # Rules that leave nothing: every answer is "excluded", either way. A rule
    # is stripped before it's read, so `* ` is `*`.
    EVERYTHING: {'EXCLUSION_PATTERNS': ['*', '**', '*a*', '[!a]*', '* ', ' *']},
}

NAMES = [
    '.DS_Store', '.ds_store', 'Thumbs.db', 'THUMBS.DB', 'desktop.ini', '._Movie.mkv',
    '.goutputstream-AB12', '.#notes', '~$doc.docx', '.fuse_hidden0001', '.nfs0002', 'Movie.mkv',
    'poster.jpg', 'show-poster.JPG', 'fanart1.jpg', 'Movie.nfo', 'x.sfv', 'Sample.mkv',
    '...And Justice for All.flac', '.plexmatch', 'a.parts', '.abc123.parts', 'b.mkv.!qB',
    '..data', 'a', 'ab', 'aa', 'y', 'x', 'folder.jpg', 'Movie-poster.jpg', 'movie-fanart.png',
    'a.b.bak', 'Show.S01E01.mkv', 'Some.Film.avi', 'sample', 'a b', ' ', 'x]', '[x',
    'new\nline.mkv', 'é.mkv', 'a\udcffb.mkv']
DIRS = [
    'tv/Show', '@eaDir/Movie.mkv', '#recycle/tv', '@Recycle', '.Trash-1000/files', 'lost+found',
    '.zfs/snapshot/daily', '#snapshot/x', '.@__thumb', 'System Volume Information',
    '$RECYCLE.BIN', 'tv/Show [x]', 'movies/Film/extras', 'BDMV/STREAM',
    'music/...And Justice for All (1988)', 'Featurettes', '.snapshots/1/snapshot', '..data',
    '.recyclebin', 'eaDir', 'games', 'books/x', 'movies/Some Film (2020)', 'a', 'sample/x',
    'Sample', 'tv/Show/Season 01', 'x/y/z/w/v', 'extrafanart', 'tv/Show/extrafanart',
    'metadata', 'tv/poster.jpg']
# The last one has a doubled slash, which the short way refuses.
ROOTS = ['/data/torrents', '/mnt/user/data/torrents', '', 'relative/root', '/data//torrents']
BS = chr(92)


def _triples():
    """Every (full, rel, name) the walk builds, then the spellings other callers use."""
    out = []
    for root in ROOTS:
        for d, f in itertools.product(DIRS, NAMES):
            rel = f'{d}/{f}'
            out.append((f'{root}/{rel}', rel, f))
        for f in NAMES:                                 # a file at the root: rel is its name
            out.append((f'{root}/{f}', f, f))
    base = '/data/torrents'
    for d, f in itertools.product(DIRS[:12], NAMES[:16]):
        rel = f'{d}/{f}'
        full = f'{base}/{rel}'
        out += [
            (full, full, f),                            # scripts.hidden: nothing to be relative to
            (full, rel, os.path.basename(full)),
            (full.replace('/', BS), rel.replace('/', BS), f),          # Windows spellings
            (full, rel.replace('/', BS), f),
            (f'{base}//{rel}', rel, f), (full, f'{d}//{f}', f), (full + '/', rel + '/', f),
            (full, rel, ''), (full, rel, rel), (full, '', f), ('', '', ''),
            (full, 'other/place/' + f, f), (full, rel, 'different.name'),
            (pathlib.PurePosixPath(full), rel, f), (full, None, f), (None, rel, f),
            (os.fspath(pathlib.PurePosixPath(base, d)), d, os.path.basename(d)),   # _dir_excluded
            # One folder under two relative folders, then one relative folder
            # under two folders, an excluded name in one of each pair and asked
            # in both orders: what's kept for a folder is for both its spellings.
            (full, f'@eaDir/{f}', f), (full, rel, f), (full, f'@eaDir/{f}', f),
            (f'/data/@eaDir/{rel}', rel, f), (full, rel, f), (f'/data/@eaDir/{rel}', rel, f),
        ]
    return out


class _Long(str):
    """A name the short way won't take: it reads plain `str` only."""


def _long_way(matcher, full, rel, name):
    return bool(matcher.match(full, rel, _Long(name)))


@pytest.mark.parametrize('label', list(RULESETS))
def test_the_short_way_answers_as_the_long_way_and_is_excluded(label):
    patterns = expand_exclusion_patterns(RULESETS[label])
    short, reference = compile_exclusions(patterns), compile_exclusions(patterns)
    walk = _triples()
    expected = [_long_way(reference, *t) for t in walk]
    # The reference never took the short way, or this compares it with itself.
    assert reference._dir_memo == (None, None, False, None, None)
    # The corpus reaches the rules: some of it is excluded and some isn't.
    assert sum(expected) > 0
    assert sum(expected) < len(expected) or label == EVERYTHING

    shuffled = list(range(len(walk)))
    random.Random(5).shuffle(shuffled)
    wrong = []
    # Walk order keeps a folder's answer across its files; shuffled throws it
    # away at nearly every call; walk order again starts from a stale one.
    for order, indexes in (('walk', range(len(walk))), ('shuffled', shuffled),
                           ('walk again', range(len(walk)))):
        for i in indexes:
            if bool(short.match(*walk[i])) != expected[i]:
                wrong.append((order, walk[i], expected[i]))
    assert not wrong, f'{len(wrong)} answers differ, first: {wrong[:5]!r}'
    assert short._dir_memo[0] is not None          # and the short way did run

    # `is_excluded` loops every rule for every path, so it answers a slice.
    for i in range(0, len(walk), 7):
        full, rel, name = walk[i]
        assert bool(is_excluded(full, rel, name, patterns)) == expected[i], repr(walk[i])


def test_only_the_walks_own_spelling_takes_the_short_way():
    matcher = compile_exclusions(['*-poster.jpg', 'contains:sample'])

    matcher.match('/data/torrents/tv/Show/e1.mkv', 'tv/Show/e1.mkv', 'e1.mkv')
    assert matcher._dir_memo[:3] == ('/data/torrents/tv/Show', 'tv/Show', False)
    # A file at the root has no relative folder.
    matcher.match('/data/torrents/top.mkv', 'top.mkv', 'top.mkv')
    assert matcher._dir_memo[:3] == ('/data/torrents', None, False)

    kept = matcher._dir_memo
    full, rel, name = '/data/torrents/tv/Show/e1.mkv', 'tv/Show/e1.mkv', 'e1.mkv'
    for odd in [
        (full.replace('/', BS), rel.replace('/', BS), name),     # Windows separators
        (full, rel.replace('/', BS), name),
        ('/data//torrents/tv/Show/e1.mkv', rel, name),           # a doubled slash
        (full, 'tv//Show/e1.mkv', name),
        (full, rel, 'Show/e1.mkv'),                              # the name isn't one segment
        (full, rel, 'other.mkv'),                                # nor the last one
        (full, 'tv/Show/other.mkv', name),
        (full, rel, ''),
        (pathlib.PurePosixPath(full), rel, name),                # not a plain string
        (full, None, name),
        (None, rel, name),
    ]:
        matcher.match(*odd)
        assert matcher._dir_memo is kept, repr(odd)


def test_a_whole_path_rule_is_answered_by_the_folder_and_the_name():
    """SC13, with the answers written out: the parity above can't say the long way is right."""
    matcher = compile_exclusions(['contains:show/s', '**/.plexmatch', 'tv/*/extras/trailer.mkv',
                                  'literal:tv/Show/b.mkv', 'tv/Other/', 'movies/Film',
                                  'literal:/data/torrents/tv/Old/'])
    cases = [
        ('tv/Show/Sample.mkv', True),           # a needle across the folder and the name
        ('tv/Show/e1.mkv', False),
        ('tv/Show/Season 01/e1.mkv', True),     # the same needle inside the folder
        ('tv/Show/b.mkv', True),                # a literal file: its folder, then its name
        ('tv/Shows/b.mkv', False),
        ('tv/Show/x/b.mkv', False),
        ('b.mkv', False),
        ('a/.plexmatch', True),                 # a path glob ending in a plain name
        ('.plexmatch', True),                   # under the root, which `**` matches
        ('a/.plexmatch2', False),
        ('tv/Show/extras/trailer.mkv', True),
        ('tv/A/B/extras/trailer.mkv', True),    # fnmatch's `*` crosses a `/`
        ('tv/Show/extras/other.mkv', False),
        ('tv/Other/e1.mkv', True),              # a subtree
        ('tv/Otherwise/e1.mkv', False),
        ('movies/Film/f.mkv', True),            # a plain path as a folder
        ('movies/Film', True),                  # as a file
        ('old/movies/Film', True),
        ('movies/Film2/f.mkv', False),
        # A subtree's own path, as a file: only the full spelling reaches it, and
        # only by being equal (its folder is the whole spelling, nothing before).
        ('tv/Old', True),
        ('tv/Old/e1.mkv', True),
        ('tv/Older', False),
    ]
    for _ in range(2):                          # the second pass reads kept folders
        for rel, expected in cases:
            full, name = f'/data/torrents/{rel}', rel.rpartition('/')[2]
            assert bool(matcher.match(full, rel, name)) is expected, rel
            assert _long_way(matcher, full, rel, name) is expected, rel
    matcher.match('/data/torrents/tv/Show/b.mkv', 'tv/Show/b.mkv', 'b.mkv')
    matcher.match('/data/torrents/tv/Show/c.mkv', 'tv/Show/c.mkv', 'c.mkv')
    assert matcher._dir_memo[:3] == ('/data/torrents/tv/Show', 'tv/Show', False)
    assert 'b.mkv' in matcher._dir_memo[3]


def test_a_name_glob_by_its_ends_is_the_regex():
    """`_glob_hit` against the one regex of every glob, over random names and globs."""
    rnd = random.Random(9)
    alphabet = 'ab.-* ?[]!xyJPGjpg_\n'
    checked, wrong = 0, []
    for _ in range(400):
        globs = [''.join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 7)))
                 for _ in range(rnd.randint(1, 6))]
        globs = [g for g in globs if any(ch in g for ch in '*?[]') and g.strip() == g]
        matcher = compile_exclusions(globs)
        if matcher._seg_glob_rx is None:
            continue
        for _ in range(300):
            name = ''.join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 9)))
            checked += 1
            if bool(matcher._seg_glob_rx.match(os.path.normcase(name))) != matcher._glob_hit(name):
                wrong.append((globs, name))
    assert checked > 50_000
    assert not wrong, f'{len(wrong)} of {checked} differ, first: {wrong[:5]!r}'


@pytest.mark.skipif(os.sep != '/', reason="the walk spells a path with the platform's separator")
def test_the_walk_spells_its_paths_the_short_ways_way(tmp_path):
    """The real walk's paths are ones the short way takes, and its answers are `is_excluded`'s."""
    layout = ('top.mkv', 'tv/Show/e1.mkv', 'tv/Show/Show-poster.jpg', 'tv/Show/Subs/e1.en.srt',
              '@eaDir/x.mkv', 'movies/Film/Sample/film-sample.mkv', 'movies/Film/film.mkv')
    for rel in layout:
        path = tmp_path.joinpath(*rel.split('/'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    patterns = expand_exclusion_patterns({'MEDIA_SERVER_EXCLUSION_PRESETS': ['plex'],
                                          'EXCLUSION_PATTERNS': ['*.srt', 'contains:sample']})
    compiled = compile_exclusions(patterns)
    asked = []

    class _Listening:
        def match(self, full_path, rel_path, filename):
            answer = compiled.match(full_path, rel_path, filename)
            # The folder kept after the call is this path's own, so the short way took it.
            asked.append((rel_path, compiled._dir_memo[0] == os.path.dirname(full_path)))
            return answer

    inode_map = {}
    _walk_directory(str(tmp_path), 'Torrent', inode_map, {}, 0, 0, exclusion_patterns=patterns,
                    total_ref=[0], compiled_exclusions=_Listening())

    assert {rel for rel, _ in asked} >= set(layout)
    assert all(short for _, short in asked), asked
    stored = {info['torrent_rel_path']: bool(info['torrent_excluded'])
              for info in inode_map.values()}
    assert stored == {rel: bool(is_excluded(str(tmp_path / rel), rel, os.path.basename(rel),
                                            patterns)) for rel in layout}
    assert sorted(rel for rel, excluded in stored.items() if excluded) == [
        '@eaDir/x.mkv', 'movies/Film/Sample/film-sample.mkv', 'tv/Show/Show-poster.jpg',
        'tv/Show/Subs/e1.en.srt']


# ── SC14: a glob the regex answers is skipped for a name that can't end its way ──

@pytest.mark.parametrize('glob, tail', [
    ('fanart*.jpg', '.jpg'), ('*-fanart*.jpg', '.jpg'), ('a?b', 'b'), ('*b?c', 'c'),
    ('x[ab]', ''), ('*', ''), ('a*', ''), ('[!a]c', 'c'), ('a[b', 'b'), ('a]b*c.d', 'c.d'),
    ('*x*y.webp', 'y.webp'), ('[]x]*.tbn', '.tbn'), ('?', '')])
def test_a_globs_literal_tail(glob, tail):
    assert exclusions_module._literal_tail(glob) == tail
    # Every name the glob matches ends with it.
    for name in ('fanart-1.jpg', 'xx-fanart.jpg', 'ab', 'axb', 'zbqc', 'xa', 'bc', 'a[b',
                 'a]bQc.d', 'xQy.webp', ']x.tbn', 'q', 'a.mkv'):
        if fnmatch.fnmatchcase(name, glob):
            assert name.endswith(tail), (glob, name)


def _ends_rule(q):
    """A `*literal` or `literal*` glob, answered by `endswith`/`startswith`."""
    return len(q) > 1 and ((q[0] == '*' and not exclusions_module._has_glob(q[1:]))
                           or (q[-1] == '*' and not exclusions_module._has_glob(q[:-1])))


def test_a_mid_star_glob_is_skipped_only_for_a_name_it_cant_match():
    """`_glob_hit` against the one regex, over rule sets of globs the regex
    answers (none `*literal` or `literal*`), most of them with a literal tail and
    names that often end in one, so the skip is taken and missed both ways."""
    rnd = random.Random(14)
    heads = ['fanart', '-fanart', 'back', 'a', 'x?', '[ab]', '[!x]', '']
    tails = ['.jpg', '.jpeg', '.png', 'g', 'pg', '.tbn', 'y.webp', '?.png', '[ab].tbn', '*', '']
    checked = gated = ungated = 0
    wrong = []
    for _ in range(300):
        globs = []
        for _ in range(rnd.randint(1, 8)):
            g = rnd.choice(heads) + '*' + rnd.choice(['', 'mid', '-', '?']) + rnd.choice(tails)
            globs.append(g)
        if rnd.random() < 0.6:              # mostly every glob with a tail, so the skip is on
            globs = [g for g in globs if exclusions_module._literal_tail(g) or _ends_rule(g)]
        if not globs:
            continue
        matcher = compile_exclusions(globs)
        if matcher._glob_rest_rx is None:
            continue
        on = matcher._glob_rest_tails is not None
        assert on == all(exclusions_module._literal_tail(q) for q in map(os.path.normcase, globs)
                         if not _ends_rule(q))
        gated += on
        ungated += not on
        for _ in range(200):
            name = (rnd.choice(heads + ['Show.S01E01', 'poster', '']) + rnd.choice(['', 'mid', '-x'])
                    + rnd.choice(tails + ['.mkv', '.srt', '', '.JPG']))
            checked += 1
            if bool(matcher._seg_glob_rx.match(os.path.normcase(name))) != matcher._glob_hit(name):
                wrong.append((globs, name))
    assert checked > 40_000 and gated > 100 and ungated > 30
    assert not wrong, f'{len(wrong)} of {checked} differ, first: {wrong[:5]!r}'


def test_the_box_s_art_globs_are_skipped_for_a_video():
    """Under the presets every glob the regex answers has a tail, so a video,
    a subtitle and a folder's name never reach it, and an image still does."""
    matcher = compile_exclusions(expand_exclusion_patterns(RULESETS['the box']))
    assert matcher._glob_rest_rx is not None and matcher._glob_rest_tails is not None

    class _Watched:
        def __init__(self, rx):
            self.rx, self.asked = rx, []

        def match(self, s):
            self.asked.append(s)
            return self.rx.match(s)
    watched = _Watched(matcher._glob_rest_rx)
    matcher._glob_rest_rx = watched
    for name in ('Show.S01E01.mkv', 'Show.S01E01.en.srt', 'Season 01', 'clip.mp4'):
        assert matcher._glob_hit(name) is False
    assert watched.asked == []
    assert matcher._glob_hit('backdrop2.jpg') is True
    assert matcher._glob_hit('summer.jpg') is False
    assert len(watched.asked) == 2
