"""No two frontend modules in one folder share a name apart from case or extension (PR #28).

An import names a module without its extension, and Vite tries `.mjs`, `.js`,
`.mts`, `.ts`, `.jsx`, `.tsx` and `.json` in that order. PR #28 added
`ListFilters.jsx` beside `listFilters.js`. On Linux, `./ListFilters` found no
`ListFilters.js` and went on to the `.jsx`, so the author's build, CI and the
Docker image were all fine. On Windows and macOS, whose disks ignore case, it
found `listFilters.js` first and the build failed: "useListFilters is not
exported". CI runs on Linux, so it can only see this as a test.

Two names that differ only in extension (`api.js` beside `api.jsx`) are refused
too: on every system the import gets whichever Vite tries first.
"""
import collections
import os

from backend_tests.test_type_scale import SRC

# Vite's default `resolve.extensions`, which `vite.config.js` doesn't change.
EXTENSIONS = ('.mjs', '.js', '.mts', '.ts', '.jsx', '.tsx', '.json')


def clashes(names):
    """Groups of file names one extensionless import could mean."""
    by_stem = collections.defaultdict(list)
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext in EXTENSIONS:
            by_stem[stem.lower()].append(name)
    return sorted(sorted(group) for group in by_stem.values() if len(group) > 1)


def test_no_two_modules_in_a_folder_could_be_one_import():
    seen = 0
    bad = []
    for root, _dirs, files in os.walk(SRC):
        seen += len(files)
        for group in clashes(files):
            bad.append(f'{os.path.relpath(root, SRC).replace(os.sep, "/")}: {", ".join(group)}')
    # A walk of the wrong folder would pass without reading a thing.
    assert seen > 10, f'walked {seen} file(s) — is {SRC} right?'
    assert not bad, (
        'An import without an extension could mean more than one of these, and on Windows and macOS '
        'case doesn\'t tell them apart. Rename one:\n  ' + '\n  '.join(bad))


def test_the_check_reads_case_and_extension():
    assert clashes(['ListFilters.jsx', 'listFilters.js'])
    assert clashes(['api.js', 'api.jsx'])
    assert not clashes(['ListFilters.jsx', 'listFilterRules.js'])
    assert not clashes(['index.css', 'index.js'])
