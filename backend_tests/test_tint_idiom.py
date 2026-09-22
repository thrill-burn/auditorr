"""No colour on the workflow surfaces is tinted by gluing a hex alpha onto a var() (UI pass, 2026-09-21).

The design system teaches `background: var(--accent)1a` as its alpha-tint idiom,
and the workflow pages used it in 42 places. It does not work. CSS substitutes
`var()` as tokens and never re-parses the result, so `var(--red)40` is a colour
followed by a separate number — an invalid value — and the browser drops the
**whole declaration**. `border: 1px solid var(--red)40` draws no border at all,
where the author expected a faint one. Measured in Chromium: `0px none` and a
transparent background, against `color-mix()`'s red at 25%.

That silently removed every warning box's box, every danger button's hairline,
and the border of every coloured row chip, which is most of why the buttons
looked like fourteen different styles. Rounds.jsx already said so in a comment;
the idiom kept spreading anyway, so this is a test, like the type-scale guard
beside it, rather than another comment.

Tint through `tint(color, pct)` in `workflows/shared.jsx`, which is
`color-mix(in srgb, <color> <pct>%, transparent)`. Three spellings are refused:
`var(--x)NN`, a template literal `${X}NN`, and a string concatenation `+ 'NN'`
— the last two produce the same broken value whenever the colour is a var().

Comments are skipped, so the explanation can quote the thing it forbids.
"""
import os
import re

from backend_tests.test_type_scale import SCOPE, SRC

_VAR_ALPHA = re.compile(r"var\(--[\w-]+\)[0-9a-fA-F]{2}(?![\w-])")
# Any template substitution, not only a bare name: `${a || 'var(--x)'}40` and
# `${ok ? 'var(--green)' : 'var(--red)'}35` both render `var(--x)NN`, and the
# first version of this pattern (`\$\{name\}NN`) let both through outside the
# guarded files (Sidebar's count badges, Config's run badges).
_TEMPLATE_ALPHA = re.compile(r"\}[0-9a-fA-F]{2}(?![\w-])")
_CONCAT_ALPHA = re.compile(r"""\+\s*(['"])[0-9a-fA-F]{2}\1""")
_LINE_COMMENT = re.compile(r"(?<![:\w])//.*$")


def code_lines(text):
    """(line number, code) for each line, with // and /* */ comments removed."""
    in_block = False
    for n, line in enumerate(text.splitlines(), 1):
        out = ''
        rest = line
        while rest:
            if in_block:
                end = rest.find('*/')
                if end < 0:
                    rest = ''
                    break
                rest = rest[end + 2:]
                in_block = False
            start = rest.find('/*')
            if start < 0:
                out += rest
                break
            out += rest[:start]
            rest = rest[start + 2:]
            in_block = True
        yield n, _LINE_COMMENT.sub('', out)


def check(text, rel='<text>'):
    bad = []
    for n, code in code_lines(text):
        for rx, what in ((_VAR_ALPHA, 'a hex alpha glued to a var()'),
                         (_TEMPLATE_ALPHA, 'a hex alpha glued to a template value'),
                         (_CONCAT_ALPHA, 'a hex alpha concatenated onto a colour')):
            for m in rx.finditer(code):
                bad.append(f'{rel}:{n}: {m.group(0)!r} is {what} — use tint(color, pct)')
    return bad


def _rel(path):
    return os.path.relpath(path, SRC).replace(os.sep, '/')


def test_no_colour_on_the_workflow_surfaces_is_tinted_with_a_glued_alpha():
    bad = []
    for path in SCOPE:
        with open(path, encoding='utf-8') as fh:
            bad.extend(check(fh.read(), _rel(path)))
    assert not bad, (
        f'{len(bad)} tint(s) the browser will drop. A hex alpha after a var() is invalid once the var() '
        'is substituted, and the whole declaration goes with it (CLAUDE.md, "Tints"):\n  ' + '\n  '.join(bad))


def test_the_check_catches_each_spelling_and_skips_comments():
    for line in ["background: 'var(--accent)18',",
                 "border: '1px solid var(--red)40',",
                 "border: `1px solid ${isBest ? 'var(--accent)25' : 'x'}`",
                 "background: selected ? `${ACCENT}0e` : 'transparent',",
                 "border: `1px solid ${child.accent || 'var(--border2)'}40`,",
                 "border: `1px solid ${isOk ? 'var(--green)' : 'var(--red)'}35`,",
                 "background: color + '14',"]:
        assert check(line), line
    for line in ["background: tint('var(--accent)', 9),",
                 "// `var(--red)40` is a colour followed by a stray number",
                 "{/* The `var(--accent)55` alpha-suffix idiom */}",
                 "color: 'var(--text)', padding: '1px 7px',",
                 "href: 'https://example.com/a1'",
                 "width: `${pad2(n)}px`",
                 "} else {",
                 "{items.map(x => <X key={x.id} />)}"]:
        assert not check(line), line
    assert not check("/* one\n var(--red)40\n */ x")
    assert check("/* one */ border: '1px solid var(--red)40'")
