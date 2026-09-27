"""Type in the frontend comes from the app's scale, never from a number (Phase 11, R8).

TRIAGE T13 and TRUMPED TR17 were one finding filed twice: the workflow pages
sized their text with hardcoded numbers, most of them off index.css's `--font-*`
scale and 72 of them exactly halfway between two of its steps. A convention
written into each phase's brief did not hold the count, which went 177 → 220 →
223 across the phases that rewrote those pages. This does.

Every `fontSize` in a file in scope must be `'var(--font-<step>)'`, naming a step
index.css defines. The steps are read from index.css, never copied here, so a
change to the scale's values needs no edit to this test.

Scope is **every** `.jsx` file under `frontend/src`. It was the workflow pages,
App.jsx, ImportProgress.jsx and Rounds.jsx until the UI pass of 2026-09-22
converted the rest of the app — 329 literal sizes in 15 files, 29 of them off
the scale — alongside the controls it moved onto the shared kit.

A glyph that is not text (an emoji) or a display figure keeps its number, and
only through EXEMPTIONS, each naming its site and its count: an exemption that
matches a different number of sites fails, so the list cannot go stale or
quietly cover a second site.

No node: the files are read as text, so this runs in the CI gate as it stands.
"""
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'frontend', 'src')
INDEX_CSS = os.path.join(SRC, 'index.css')

# By glob, so a new page or component is in scope the day it is added.
SCOPE = sorted(glob.glob(os.path.join(SRC, '**', '*.jsx'), recursive=True))

# (file relative to frontend/src, text on the site's line, sites it must match, reason)
#
# The close × characters in App.jsx and ImportProgress.jsx were the exemptions
# here until the UI pass (2026-09-22) gave every close control one line icon,
# CloseButton; EmptyState's 🎉 went the same way the day before.
EXEMPTIONS = [
    # The Dashboard's numerals are display figures, not text: the scale stops at
    # --font-xl (20px, a page title), and forcing them onto it would erase the
    # "Instrument" number treatment the dashboard is built around.
    ('components/Dashboard.jsx', "fontSize: 42, fontWeight: 500", 1,
     "the health dial's score, the page's largest display figure"),
    ('components/Dashboard.jsx', "fontSize: size, fontWeight: 500", 1,
     "HeroNumber's numeral, a display figure sized by its caller (33px on the cards)"),
    ('components/Dashboard.jsx', "fontSize: Math.round(size * 0.55)", 1,
     "HeroNumber's unit caption, proportional to its numeral"),
    ('components/Dashboard.jsx', "{medals[i]}</span>", 1,
     "the leaderboard's medal emoji is a glyph sized as an icon"),
]

_VAR = re.compile(r"""^(['"])var\(--font-([a-z0-9-]+)\)\1$""")
_KEY = re.compile(r"""\bfontSize\s*:\s*('[^']*'|"[^"]*"|`[^`]*`|[^,}\n]+)""")
_ATTR = re.compile(r"""\bfontSize\s*=""")
_CSS = re.compile(r"""\bfont-size\s*:""")
_SHORTHAND = re.compile(r"""(?<![-\w])font\s*:\s*('[^']*'|"[^"]*"|[^,}\n]+)""")


def scale_steps(css_text):
    return set(re.findall(r'--font-([a-z0-9-]+)\s*:', css_text))


def check(text, steps, rel='<text>', exemptions=()):
    """Every size in `text` that is not from the scale, as (site, why) pairs, and
    how many sites each exemption matched."""
    bad, matched = [], {e: 0 for e in exemptions}
    for n, line in enumerate(text.splitlines(), 1):
        sites = []
        for m in _KEY.finditer(line):
            val = m.group(1).strip()
            v = _VAR.match(val)
            if not v:
                sites.append(f'fontSize: {val} is not a scale variable')
            elif v.group(2) not in steps:
                sites.append(f'fontSize: {val} names --font-{v.group(2)}, which index.css does not define')
            else:
                continue
        if _ATTR.search(line):
            sites.append('a fontSize= attribute — size text through style, from the scale')
        if _CSS.search(line):
            sites.append('a font-size declaration — size text through style, from the scale')
        for m in _SHORTHAND.finditer(line):
            if m.group(1).strip() not in ("'inherit'", '"inherit"'):
                sites.append(f'font: {m.group(1).strip()} sets a size outside the scale')
        if not sites:
            continue
        hit = [e for e in exemptions if e[0] == rel and e[1] in line]
        for e in hit:
            matched[e] += 1
        if not hit:
            bad.extend(f'{rel}:{n}: {why}' for why in sites)
    return bad, matched


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _rel(path):
    return os.path.relpath(path, SRC).replace(os.sep, '/')


def _run():
    steps = scale_steps(_read(INDEX_CSS))
    bad, matched = [], {e: 0 for e in EXEMPTIONS}
    for path in SCOPE:
        b, m = check(_read(path), steps, _rel(path), EXEMPTIONS)
        bad.extend(b)
        for e, n in m.items():
            matched[e] += n
    return steps, bad, matched


def test_the_scope_and_the_scale_are_where_this_test_looks():
    # A moved file or a renamed variable would otherwise pass by reading nothing.
    workflows = [p for p in SCOPE if os.sep + 'workflows' + os.sep in p]
    assert len(workflows) >= 6, workflows
    assert len(SCOPE) >= 25, f'scope collapsed to {len(SCOPE)} file(s)'
    for rel in ('App.jsx', 'components/Dashboard.jsx', 'components/Config.jsx', 'components/FileExplorer.jsx'):
        assert os.path.join(SRC, *rel.split('/')) in SCOPE, rel
    assert {'sm', 'base', 'md'} <= scale_steps(_read(INDEX_CSS))


def test_every_size_in_the_frontend_comes_from_the_scale():
    _, bad, _ = _run()
    assert not bad, (
        f'{len(bad)} font size(s) off the type scale. Use fontSize: \'var(--font-<step>)\' with the step '
        'for what the text is (CLAUDE.md, "Type on the workflow surfaces"):\n  ' + '\n  '.join(bad))


def test_every_exemption_matches_exactly_its_sites():
    _, _, matched = _run()
    wrong = [f'{f}: expected {count} site(s) containing {needle!r}, found {matched[(f, needle, count, why)]}'
             for (f, needle, count, why) in EXEMPTIONS if matched[(f, needle, count, why)] != count]
    assert not wrong, 'stale or widened exemption(s):\n  ' + '\n  '.join(wrong)


def test_the_check_catches_a_number_an_unknown_step_and_a_state_ternary():
    steps = {'xs', 'sm', 'base', 'md', 'lg', 'xl'}
    good = "<span style={{ fontSize: 'var(--font-sm)', color: 'x' }}>a</span>"
    assert check(good, steps) == ([], {})
    for line in ["<span style={{ fontSize: 10, fontFamily: 'var(--mono)' }}>",
                 "<span style={{ fontSize: 12.5 }}>",
                 "<span style={{ fontSize: 'var(--font-huge)' }}>",
                 "<span style={{ fontSize: compact ? 'var(--font-sm)' : 'var(--font-md)' }}>",
                 "<span style={{ fontSize: '11px' }}>",
                 '<text fontSize="9">',
                 "const s = { font: '12px var(--mono)' }"]:
        bad, _ = check(line, steps)
        assert bad, line
    assert check("{ font: 'inherit', cursor: 'pointer' }", steps) == ([], {})
