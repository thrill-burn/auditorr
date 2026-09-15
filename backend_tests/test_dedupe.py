"""Dedupe: the code that decides what gets hardlinked (DEDUPE F17).

Before this file two tests in `backend_tests/` touched `_build_dup_groups`, and
both came in with the tombstone fix rather than with Dedupe. Dedupe is the one
workflow whose output *writes* — a script that replaces files with hardlinks —
and it was the least tested workflow in the repo.

Two kinds of test live here and are kept apart:

* **Characterisation** — behaviour that was already right before Phase 10 and
  must stay right through it.
* **Findings** — F1/F9, F2/F3, F5, F7, F10, F11, F12, F13, F14, F15, F16, S10,
  the selection rule and the outside review's amendment 4. Each was written
  before its fix and failed for the reason its finding names.

Assertions are on the endpoint response and on **what running the generated
script did to `tmp_path`** — never on a helper's return alone. `PATH` shims fake
what NTFS cannot: a second device, a link that fails, an interrupted rename,
another owner, a sparse file, a symlink (creating one needs a Windows privilege
the dev machine does not hold) and a `stat` with no `-c`.

The fixture rule, inherited from Phase 9: **a fixture is what the audit
writes.** Records come from a real walk through `_build_duplicate_map` and
`_assemble_records`, so the adjacency is truncated and directed exactly as the
audit truncates it. A hand-written `duplicate_paths` listing every partner is
symmetric and complete, which is precisely what hid F15. The exceptions are
named where they occur: a newline cannot be put in an NTFS file name, a path
outside the script root cannot exist under `tmp_path`, and F12 is unreachable
from the audit by construction.
"""
import json
import os
import re
import shlex
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

import app
import audit
import scripts
from audit import _assemble_records, _build_duplicate_map, _walk_directory
from backend_tests.test_cleanup import _bash, _run
from backend_tests.test_source_absence import _inode
from exclusions import compile_exclusions
from media_server_exclusions import expand_exclusion_patterns


# ── fixtures ──────────────────────────────────────────────────────────────────

def _bytes(size, seed=0):
    block = bytes((i * 7 + seed) % 256 for i in range(256))
    return (block * (size // 256 + 1))[:size]


def _posix(p):
    return str(p).replace('\\', '/')


class Lib:
    """A torrent tree and a media tree under one root, walked by the real audit.

    Paths are given relative to the root (`torrents/…`, `media/…`), which is
    also the script root — so a path here is exactly what the page shows and
    what the script acts on, and the script runs with the root as its cwd.
    """

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / 'data'
        self.torrents = self.root / 'torrents'
        self.media = self.root / 'media'
        self.torrents.mkdir(parents=True)
        self.media.mkdir()

    def cfg(self, **over):
        return {'LOCAL_PATH': _posix(self.torrents), 'MEDIA_PATH': _posix(self.media), **over}

    def file(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def link(self, src, dst):
        d = Path(dst) if os.path.isabs(str(dst)) else self.root / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        os.link(self.root / src, d)

    def ino(self, rel):
        return os.stat(self.root / rel).st_ino

    def audit(self, patterns=()):
        """Both walks, the duplicate map and the records — as `run_audit_process`.

        The records are then respelled with `/`, as the container writes them.
        On Windows the walk joins with `\\`, and the pre-Phase-10 code then
        built script paths relative to `LOCAL_PATH` alone — so its script failed
        its own working-directory guard here and every finding test would have
        failed on a Windows artefact rather than on its finding.
        """
        expanded = expand_exclusion_patterns({'EXCLUSION_PATTERNS': list(patterns)})
        compiled = compile_exclusions(expanded)
        inode_map = {}
        tko, _, _, _ = _walk_directory(str(self.torrents), 'Torrent', inode_map, {}, 0, 0,
                                       exclusion_patterns=expanded,
                                       compiled_exclusions=compiled)
        mko, _, _, _ = _walk_directory(str(self.media), 'Media', inode_map, {}, 0, 0,
                                       exclusion_patterns=expanded,
                                       compiled_exclusions=compiled)
        dup = _build_duplicate_map(inode_map)
        t, m = _assemble_records(tko, mko, inode_map, dup, compiled_exclusions=compiled)
        for rec in t + m:
            for key in ('path', 'excl_folder'):
                if rec.get(key):
                    rec[key] = _posix(rec[key])
            for key in ('linked_paths', 'duplicate_paths', 'other_paths'):
                if rec.get(key):
                    rec[key] = [_posix(p) for p in rec[key]]
        return t, m


def _hand_audit(inodes, order=None):
    """Records from a hand-built `inode_map`, for the three shapes a real walk
    under `tmp_path` cannot produce. The fast hash is stubbed, as
    `DuplicateMapCompletionTests` does; everything after it is the audit's."""
    with patch.object(audit, 'get_fast_hash', lambda p, s: 'same'):
        dup = _build_duplicate_map(inodes)
    keys = order or list(inodes)
    tko = [k for k in keys if inodes[k]['torrent_paths']]
    mko = [k for k in keys if inodes[k]['media_paths']]
    return _assemble_records(tko, mko, inodes, dup)


# F1's pair: A is torrent-only; B is imported, so its torrent path and its
# library path are one inode. A is walked first, and B is the member the old
# rule let seed a second group from its other role.
F1_A = 'torrents/movies/Film.A/Film.mkv'
F1_B = 'torrents/movies/Film.B/Film.mkv'
F1_M = 'media/Film (2020)/Film (2020).mkv'


def _f1(lib, size=2048):
    data = _bytes(size, 1)
    lib.file(F1_A, data)
    lib.file(F1_B, data)
    lib.link(F1_B, F1_M)


def _pack(lib, n, folder, size=1000, seed=9):
    data = _bytes(size, seed)
    paths = [f'torrents/{folder}/Copy.{i:02d}.bin' for i in range(n)]
    for p in paths:
        lib.file(p, data)
    return paths


# An imported partner beside a copy with more links: K is three cross-seed
# hardlinks in the torrent tree, so the script keeps K; P is a separate copy
# imported into the library, so both of P's paths must move onto K together.
K1 = 'torrents/xs/one/Keep.mkv'
K2 = 'torrents/xs/two/Keep.mkv'
K3 = 'torrents/xs/three/Keep.mkv'
P_T = 'torrents/movies/Part/Keep.mkv'
P_M = 'media/Keep (2021)/Keep (2021).mkv'


def _imported_partner(lib, size=4096):
    data = _bytes(size, 3)
    lib.file(K1, data)
    lib.link(K1, K2)
    lib.link(K1, K3)
    lib.file(P_T, data)
    lib.link(P_T, P_M)
    return data


# ── the endpoints ─────────────────────────────────────────────────────────────

def _call(method, url, torrent_files, media_files, cfg, tmp, body=None, mountinfo=None):
    stored = {'torrents': torrent_files, 'media': media_files}
    with ExitStack() as stack:
        stack.enter_context(patch.object(app, 'db_load_config', return_value=dict(cfg)))
        stack.enter_context(patch.object(app, 'db_load_results', return_value={}))
        stack.enter_context(patch.object(app, 'db_load_file_results',
                                         side_effect=lambda tab: list(stored.get(tab, []))))
        # Never the real /proc/self/mountinfo: on a Linux CI box its answer
        # would depend on where the temp directory lives.
        stack.enter_context(patch.object(
            scripts, 'MOUNTINFO_PATH', mountinfo or str(tmp / 'no-such-mountinfo'), create=True))
        client = app.app.test_client()
        if method == 'get':
            return client.get(url)
        return client.post(url, json=body)


def _report(torrent_files, media_files, cfg, tmp, mountinfo=None):
    resp = _call('get', '/api/workflows/dedupe', torrent_files, media_files, cfg, tmp,
                 mountinfo=mountinfo)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def _script(torrent_files, media_files, cfg, tmp, groups=None, body=None, method='post',
            mountinfo=None):
    if body is None and method == 'post':
        body = {'groups': list(groups or [])}
    return _call(method, '/api/actions/script/dedupe', torrent_files, media_files, cfg, tmp,
                 body=body, mountinfo=mountinfo)


def _ids(report):
    return [g['id'] for g in report['groups']]


def _text(resp):
    return resp.get_data(as_text=True)


def _group_paths(group):
    """Every path a group lists. Characterisation tests read both shapes: they
    passed before Phase 10 changed the payload and must keep passing after."""
    if 'members' in group:
        return [p['path'] for m in group['members'] for p in m['paths']]
    return [f['path'] for f in group['files']]


def _members(group):
    return sorted(sorted(p['path'] for p in m['paths']) for m in group['members'])


def _copy_paths(text):
    """The path arguments of the script's `copy` lines, as bash reads them."""
    out = []
    for line in text.splitlines():
        if line.startswith('copy '):
            out += [a[2:] if a.startswith('./') else a for a in shlex.split(line)[1:]]
    return out


def _fake_stat(prefixes, exact=None):
    """`os.stat` answering for container paths from real files under `tmp_path`.

    `prefixes` maps a container prefix to the host directory standing in for
    it; `exact` maps one container path to one host file. Anything else goes to
    the real `stat`, so Flask and the test's own file handling are untouched.
    """
    real = os.stat
    exact = exact or {}

    def fake(p, *a, **k):
        if isinstance(p, (str, os.PathLike)):
            s = _posix(p)
            if s in exact:
                return real(exact[s], *a, **k)
            for prefix, host in prefixes.items():
                if s == prefix or s.startswith(prefix + '/'):
                    return real(str(host) + s[len(prefix):], *a, **k)
        return real(p, *a, **k)
    return fake


# ── shims for what NTFS cannot do ─────────────────────────────────────────────

def _tool(name):
    """The real tool a shim wraps, as the bash on PATH resolves it."""
    proc = subprocess.run([_bash(), '-c', f'command -v {name}'], capture_output=True)
    found = proc.stdout.decode('utf-8', 'replace').strip()
    if proc.returncode or not found.startswith('/'):
        pytest.skip(f'no {name} to wrap')
    return found


def _shim(tmp_path, name, body):
    d = tmp_path / 'shim'
    d.mkdir(exist_ok=True)
    (d / name).write_bytes(('#!/bin/sh\n' + body).encode('utf-8'))
    os.chmod(d / name, 0o755)
    return d


def _stat_shim(tmp_path, *, dev=(), uid=(), blocks=(), symlink=(), no_c=False):
    """`stat` that rewrites one field of its answer for paths matching a glob.

    The script's per-path format is `%d %i %h %s %b %B %u %g %a %F`, so a
    rewrite names a field by position: `%d` the device, `%u` the owner, `%b`
    the allocated blocks, `%F` the file type. `no_c` is BSD/macOS stat.
    """
    real = _tool('stat')
    lines = []
    if no_c:
        lines.append('for a in "$@"; do if [ "$a" = "-c" ]; then '
                     'echo "stat: illegal option -- c" >&2; exit 1; fi; done')
    lines += [f'out=$("{real}" "$@") || exit $?',
              'last=""',
              'for a in "$@"; do last=$a; done']
    rules = ([(p, '{ $1 = 4242; print }') for p in dev]
             + [(p, 'NF >= 10 { $7 = 4343 } { print }') for p in uid]
             + [(p, 'NF >= 10 { $5 = 0 } { print }') for p in blocks]
             + [(p, 'NF >= 10 { $10 = "symbolic"; $11 = "link" } { print }') for p in symlink])
    for pat, prog in rules:
        lines.append(f'case "$last" in *{pat}*) '
                     f'out=$(printf \'%s\\n\' "$out" | awk \'{prog}\') ;; esac')
    lines.append('printf \'%s\\n\' "$out"')
    return _shim(tmp_path, 'stat', '\n'.join(lines) + '\n')


def _ln_shim(tmp_path, fail_for):
    """`ln` failing for a destination matching `fail_for`, the way F5 fails:
    with `-f` it unlinks the destination first, then cannot make the link."""
    real = _tool('ln')
    return _shim(tmp_path, 'ln', f'''last=""
for a in "$@"; do last=$a; done
case "$last" in
  *{fail_for}*)
    for a in "$@"; do if [ "$a" = "-f" ]; then rm -f -- "$last"; fi; done
    echo "ln: failed to create hard link: Invalid cross-device link" >&2
    exit 1 ;;
esac
exec "{real}" "$@"
''')


def _pooled_ln_shim(tmp_path):
    """A pooled mount: `stat` reports one device for everything, but a link
    between two backing branches (`diskA`, `diskB`, `diskC` in the path)
    fails — and with `-f`, unlinks its destination first."""
    real = _tool('ln')
    return _shim(tmp_path, 'ln', f'''prev=""
last=""
for a in "$@"; do prev=$last; last=$a; done
br() {{ case "$1" in *diskB*) echo B ;; *diskC*) echo C ;; *) echo A ;; esac; }}
if [ "$(br "$prev")" != "$(br "$last")" ]; then
  for a in "$@"; do if [ "$a" = "-f" ]; then rm -f -- "$last"; fi; done
  echo "ln: failed to create hard link: Invalid cross-device link" >&2
  exit 1
fi
exec "{real}" "$@"
''')


def _mv_shim(tmp_path, fail_on):
    """`mv` failing on its Nth call — an interruption between two renames."""
    real = _tool('mv')
    counter = _posix(tmp_path / 'mv_calls')
    return _shim(tmp_path, 'mv', f'''n=$(cat "{counter}" 2>/dev/null || echo 0)
n=$((n+1))
echo "$n" > "{counter}"
if [ "$n" -eq {fail_on} ]; then
  echo "mv: cannot move: Input/output error" >&2
  exit 1
fi
exec "{real}" "$@"
''')


def _freed(out):
    m = re.search(r'Space freed:\s+(\S+ \S+)', out)
    assert m, out
    return m.group(1)


def _every_file(root):
    return [p for p in Path(root).rglob('*') if p.is_file() or p.is_symlink()]


# ═════════════════════════════════════════════════════════════════════════════
# Characterisation — already right, must stay right
# ═════════════════════════════════════════════════════════════════════════════

class TestAlreadyRight:

    def test_an_excluded_path_is_never_in_a_group_and_is_counted(self, tmp_path):
        """#14. B's library path is excluded; B's torrent path is not, so B is
        still a duplicate — but the excluded path is never offered or scripted."""
        lib = Lib(tmp_path)
        _f1(lib)
        pattern = 'literal:Film (2020)/Film (2020).mkv'
        t, m = lib.audit(patterns=[pattern])
        assert m[0]['excluded'] is True and m[0]['duplicate_paths']
        cfg = lib.cfg(EXCLUSION_PATTERNS=[pattern])

        report = _report(t, m, cfg, tmp_path)
        assert report['excluded_count'] == 1
        assert report['groups']
        assert not any(F1_M in _group_paths(g) for g in report['groups'])

        resp = _script(t, m, cfg, tmp_path, groups=_ids(report))
        assert resp.status_code == 200
        assert 'Film (2020)' not in _text(resp)

    def test_an_unknown_script_type_is_refused(self, tmp_path):
        resp = _call('post', '/api/actions/script/rm_everything', [], [], {}, tmp_path,
                     body={'groups': ['x']})
        assert resp.status_code == 400

    def test_a_wrong_directory_is_an_error_and_changes_nothing(self, tmp_path):
        lib = Lib(tmp_path)
        paths = _pack(lib, 2, 'two', size=3000)
        t, m = lib.audit()
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        wrong = tmp_path / 'elsewhere'
        wrong.mkdir()
        before = [lib.ino(p) for p in paths]
        code, out = _run(text, wrong)
        assert code != 0 and 'ERROR' in out, out
        assert [lib.ino(p) for p in paths] == before

    def test_the_health_scores_duplicate_count_is_untouched(self, tmp_path):
        """DEDUPE §8: the grouping work is page-and-script only. The dashboard,
        the change log, Singleton and Clone Hunter read `duplicate_count`, which
        `process_health_metrics` counts from `duplicate_paths` by `file_id`."""
        lib = Lib(tmp_path)
        _f1(lib)
        _pack(lib, 15, 'pack')
        t, m = lib.audit()
        with patch.object(audit, 'db_load_history',
                          return_value={'hourly_stats': [], 'daily_stats': []}):
            det = audit.process_health_metrics(m, t, {}, update_history=False)['current']['details']
        assert det['duplicate_count'] == 17
        assert det['duplicate_size'] == 2 * 2048 + 15 * 1000


class TestCompatibility:

    def test_a_stale_bundles_canonical_path_still_selects_its_group(self, tmp_path):
        """An open tab from before Phase 10 posts the old canonical path — the
        first walked record's. It is a member path, so it selects its group,
        and only its group."""
        lib = Lib(tmp_path)
        _f1(lib)
        other = _pack(lib, 2, 'other', size=3000)
        t, m = lib.audit()
        resp = _script(t, m, lib.cfg(), tmp_path, groups=[F1_A])
        assert resp.status_code == 200, _text(resp)
        scripted = _copy_paths(_text(resp))
        assert F1_A in scripted and F1_B in scripted
        assert not set(other) & set(scripted)


# ═════════════════════════════════════════════════════════════════════════════
# F1 / F9 / F2 / F3 / F10 / F13 / F15 — grouping around the inode
# ═════════════════════════════════════════════════════════════════════════════

class TestGrouping:

    def test_one_physical_pair_is_one_group(self, tmp_path):
        lib = Lib(tmp_path)
        _f1(lib)
        t, m = lib.audit()
        report = _report(t, m, lib.cfg(), tmp_path)
        assert len(report['groups']) == 1, report['groups']
        assert _members(report['groups'][0]) == [[F1_M, F1_B], [F1_A]]
        assert '"canonical"' not in json.dumps(report)

    def test_fifteen_identical_files_are_one_group_of_fifteen(self, tmp_path):
        lib = Lib(tmp_path)
        paths = _pack(lib, 15, 'pack')
        t, m = lib.audit()
        # The fixture is the audit's truncated, directed adjacency.
        assert max(len(r['duplicate_paths']) for r in t) == audit.DUP_PATHS_PER_FILE
        report = _report(t, m, lib.cfg(), tmp_path)
        assert len(report['groups']) == 1, [len(g.get('members', g.get('files'))) for g in report['groups']]
        assert sorted(_group_paths(report['groups'][0])) == paths

    def test_frees_up_to_counts_each_inode_once(self, tmp_path):
        lib = Lib(tmp_path)
        _f1(lib, size=2048)
        _pack(lib, 15, 'pack', size=1000)
        t, m = lib.audit()
        report = _report(t, m, lib.cfg(), tmp_path)
        by_size = {g['size']: g for g in report['groups']}
        assert by_size[2048]['frees_up_to'] == 2048
        assert by_size[1000]['frees_up_to'] == 14 * 1000
        assert report['frees_up_to'] == 2048 + 14 * 1000

    def test_the_group_id_is_its_smallest_path_whatever_the_record_order(self, tmp_path):
        lib = Lib(tmp_path)
        data = _bytes(1500, 4)
        lib.file('torrents/movies/Alpha/z.mkv', data)
        lib.file('torrents/movies/Beta/y.mkv', data)
        t, m = lib.audit()
        forward = _report(t, m, lib.cfg(), tmp_path)
        backward = _report(list(reversed(t)), list(reversed(m)), lib.cfg(), tmp_path)
        assert _ids(forward) == _ids(backward) == ['torrents/movies/Alpha/z.mkv']

    def test_the_report_and_the_script_build_the_same_groups(self, tmp_path):
        lib = Lib(tmp_path)
        _f1(lib)
        _pack(lib, 3, 'three', size=3000)
        t, m = lib.audit()
        report = _report(t, m, lib.cfg(), tmp_path)
        resp = _script(t, m, lib.cfg(), tmp_path, groups=_ids(report))
        assert resp.status_code == 200
        assert int(resp.headers['X-Auditorr-Groups']) == len(report['groups'])
        assert int(resp.headers['X-Auditorr-Files']) == report['file_count']
        assert int(resp.headers['X-Auditorr-Frees-Up-To']) == report['frees_up_to']
        page_paths = sorted(p for g in report['groups'] for p in _group_paths(g))
        assert sorted(_copy_paths(_text(resp))) == page_paths

        code, out = _run(_text(resp), lib.root, '--dry-run')
        assert code == 0, out
        partners = sum(len(g['members']) - 1 for g in report['groups'])
        assert len(re.findall(r'Would link', out)) == partners, out


# ═════════════════════════════════════════════════════════════════════════════
# The script contract — F1 at run time, F3, F5, F11, F14, F16, amendment 4
# ═════════════════════════════════════════════════════════════════════════════

class TestScriptRun:

    def test_no_path_is_linked_in_two_directions(self, tmp_path):
        lib = Lib(tmp_path)
        _f1(lib)
        t, m = lib.audit()
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        code, out = _run(text, lib.root)
        assert code == 0, out
        assert lib.ino(F1_A) == lib.ino(F1_B) == lib.ino(F1_M), out

    def test_an_imported_partner_has_every_path_replaced(self, tmp_path):
        lib = Lib(tmp_path)
        _imported_partner(lib, size=4096)
        t, m = lib.audit()
        report = _report(t, m, lib.cfg(), tmp_path)
        assert len(report['groups']) == 1
        code, out = _run(_text(_script(t, m, lib.cfg(), tmp_path, groups=_ids(report))),
                         lib.root)
        assert code == 0, out
        assert lib.ino(P_T) == lib.ino(P_M) == lib.ino(K1) == lib.ino(K2), out
        assert os.stat(lib.root / K1).st_nlink == 5
        assert _freed(out) == '4.0 KB'

    def test_a_partner_with_links_the_script_does_not_know_is_skipped(self, tmp_path):
        """Refuse partial replacement (§5.4 step 4). Q has a hardlink outside
        both trees; replacing Q's one listed path would split Q's inode and free
        nothing."""
        lib = Lib(tmp_path)
        _imported_partner(lib, size=4096)
        q = 'torrents/movies/Other/Keep.mkv'
        lib.file(q, _bytes(4096, 3))
        lib.link(q, tmp_path / 'outside' / 'Keep.mkv')
        before = lib.ino(q)
        t, m = lib.audit()
        report = _report(t, m, lib.cfg(), tmp_path)
        code, out = _run(_text(_script(t, m, lib.cfg(), tmp_path, groups=_ids(report))),
                         lib.root)
        assert code == 0, out
        assert lib.ino(q) == before, out
        assert os.stat(tmp_path / 'outside' / 'Keep.mkv').st_ino == before
        assert 'more hardlink' in out
        assert lib.ino(P_T) == lib.ino(K1)

    def test_reclaimed_bytes_count_only_when_the_last_link_is_replaced(self, tmp_path):
        lib = Lib(tmp_path)
        data = _imported_partner(lib, size=4096)
        lib.file('torrents/movies/Solo/Keep.mkv', data)           # frees once
        lib.file('torrents/movies/Held/Keep.mkv', data)           # an unknown link:
        lib.link('torrents/movies/Held/Keep.mkv', tmp_path / 'snap' / 'Keep.mkv')  # frees nothing
        t, m = lib.audit()
        report = _report(t, m, lib.cfg(), tmp_path)
        code, out = _run(_text(_script(t, m, lib.cfg(), tmp_path, groups=_ids(report))),
                         lib.root)
        assert code == 0, out
        # P's two paths free one copy; Solo frees one; Held frees nothing.
        assert _freed(out) == '8.0 KB'
        assert re.search(r'Linked:\s+2 file', out), out

    def test_a_failed_link_never_removes_the_destination(self, tmp_path):
        """F5 — theirs (essentrix83). `ln -f` unlinks the destination before it
        finds out the link cannot be made."""
        lib = Lib(tmp_path)
        data = _bytes(2048, 6)
        lib.file('torrents/diskA/a.bin', data)
        target = lib.file('torrents/diskB/b.bin', data)
        before = lib.ino('torrents/diskB/b.bin')
        t, m = lib.audit()
        shim = _ln_shim(tmp_path, 'diskB')
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        code, out = _run(text, lib.root, path_prefix=shim)
        assert target.exists(), out
        assert target.read_bytes() == data
        assert lib.ino('torrents/diskB/b.bin') == before
        assert 'could not be linked' in out
        assert _freed(out) == '0 B'
        assert not [p for p in _every_file(lib.root) if '.auditorr' in p.name]

    def test_a_group_across_two_devices_links_within_each_device(self, tmp_path):
        lib = Lib(tmp_path)
        data = _bytes(2048, 7)
        for p in ('diskA/1.bin', 'diskA/2.bin', 'diskB/3.bin', 'diskB/4.bin'):
            lib.file(f'torrents/{p}', data)
        t, m = lib.audit()
        shim = _stat_shim(tmp_path, dev=['diskB'])
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        code, out = _run(text, lib.root, path_prefix=shim)
        assert code == 0, out
        a1, a2 = lib.ino('torrents/diskA/1.bin'), lib.ino('torrents/diskA/2.bin')
        b3, b4 = lib.ino('torrents/diskB/3.bin'), lib.ino('torrents/diskB/4.bin')
        assert a1 == a2 and b3 == b4 and a1 != b3, out
        assert _freed(out) == '4.0 KB'

    def test_a_link_that_fails_on_a_pooled_mount_is_retried_or_skipped(self, tmp_path):
        """The outside review's amendment 4: one reported `st_dev` for every
        branch, so device buckets cannot separate them and the link itself is
        the first thing that can tell."""
        lib = Lib(tmp_path)
        data = _bytes(2048, 8)
        for p in ('diskA/1.bin', 'diskA/2.bin', 'diskB/3.bin', 'diskB/4.bin', 'diskC/5.bin'):
            lib.file(f'torrents/{p}', data)
        before_c = lib.ino('torrents/diskC/5.bin')
        t, m = lib.audit()
        shim = _pooled_ln_shim(tmp_path)
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        code, out = _run(text, lib.root, path_prefix=shim)
        assert code == 0, out
        a1, a2 = lib.ino('torrents/diskA/1.bin'), lib.ino('torrents/diskA/2.bin')
        b3, b4 = lib.ino('torrents/diskB/3.bin'), lib.ino('torrents/diskB/4.bin')
        assert a1 == a2 and b3 == b4 and a1 != b3, out
        assert lib.ino('torrents/diskC/5.bin') == before_c
        assert 'could not be linked' in out
        assert _freed(out) == '4.0 KB'

    def test_an_interrupted_run_leaves_every_path_a_file_and_resumes(self, tmp_path):
        """QA-7 / amendment 4. One path's rename is atomic; an inode's path set
        is not. Interrupted between P's two renames: nothing is missing, no
        temporary name is left, no bytes are claimed — and a re-run finishes."""
        lib = Lib(tmp_path)
        _imported_partner(lib, size=4096)
        t, m = lib.audit()
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        shim = _mv_shim(tmp_path, fail_on=2)
        code, out = _run(text, lib.root, path_prefix=shim)
        assert code != 0, out
        assert 'FAILED' in out and 'again' in out
        assert _freed(out) == '0 B'
        assert not [p for p in _every_file(lib.root) if '.auditorr' in p.name], out
        for rel in (K1, K2, K3, P_T, P_M):
            assert (lib.root / rel).is_file()
        assert sorted([lib.ino(P_T) == lib.ino(K1), lib.ino(P_M) == lib.ino(K1)]) == [False, True]

        code, out = _run(text, lib.root)
        assert code == 0, out
        assert lib.ino(P_T) == lib.ino(P_M) == lib.ino(K1), out
        assert _freed(out) == '4.0 KB'

    def test_a_second_run_changes_nothing(self, tmp_path):
        """QA-6."""
        lib = Lib(tmp_path)
        paths = _pack(lib, 3, 'three', size=3000)
        t, m = lib.audit()
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        code, out = _run(text, lib.root)
        assert code == 0, out
        inodes = [lib.ino(p) for p in paths]
        assert len(set(inodes)) == 1
        code, out = _run(text, lib.root)
        assert code == 0, out
        assert [lib.ino(p) for p in paths] == inodes
        assert re.search(r'Already linked:\s+2', out), out
        assert _freed(out) == '0 B'

    def test_members_with_different_owners_are_reported_not_relinked(self, tmp_path):
        """F11 / QA-8. Every path of a replaced file takes the kept copy's
        owner and mode — never silently."""
        lib = Lib(tmp_path)
        data = _bytes(2048, 10)
        lib.file('torrents/o/1.bin', data)
        lib.file('torrents/o/2.bin', data)
        lib.file('torrents/nobody/3.bin', data)
        before = lib.ino('torrents/nobody/3.bin')
        t, m = lib.audit()
        shim = _stat_shim(tmp_path, uid=['nobody'])
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        code, out = _run(text, lib.root, path_prefix=shim)
        assert code == 0, out
        assert lib.ino('torrents/o/1.bin') == lib.ino('torrents/o/2.bin')
        assert lib.ino('torrents/nobody/3.bin') == before
        assert 'owner' in out

    def test_a_symlink_is_never_replaced(self, tmp_path):
        """F16. `os.stat` follows a symlinked file, so the walk records the
        link's path as another path of its target. The shim reports that path
        as a symlink; the dev machine cannot create a real one."""
        lib = Lib(tmp_path)
        data = _bytes(2048, 11)
        lib.file('torrents/movies/A/a.mkv', data)
        lib.file('torrents/movies/B/link.mkv', data)
        before = lib.ino('torrents/movies/B/link.mkv')
        t, m = lib.audit()
        shim = _stat_shim(tmp_path, symlink=['link.mkv'])
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        code, out = _run(text, lib.root, path_prefix=shim)
        assert code == 0, out
        assert lib.ino('torrents/movies/B/link.mkv') == before
        assert 'symlink' in out

    def test_a_sparse_member_is_skipped(self, tmp_path):
        """§5.4 step 5 — a second F6 guard that needs no torrent client."""
        lib = Lib(tmp_path)
        data = _bytes(2 * 1024 * 1024, 12)
        # The whole copy sorts first, so it is the kept copy under either rule
        # and the sparse one is always the file that would be replaced.
        lib.file('torrents/s/a_whole.bin', data)
        lib.file('torrents/s/b_sparse.bin', data)
        before = lib.ino('torrents/s/b_sparse.bin')
        t, m = lib.audit()
        shim = _stat_shim(tmp_path, blocks=['b_sparse'])
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        code, out = _run(text, lib.root, path_prefix=shim)
        assert code == 0, out
        assert lib.ino('torrents/s/b_sparse.bin') == before, out
        assert 'looks unfinished' in out

    def test_dry_run_changes_nothing_and_reports_the_plan(self, tmp_path):
        lib = Lib(tmp_path)
        paths = _pack(lib, 3, 'three', size=3000)
        t, m = lib.audit()
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        before = [lib.ino(p) for p in paths]
        code, out = _run(text, lib.root, '--dry-run')
        assert code == 0, out
        assert [lib.ino(p) for p in paths] == before
        assert len(re.findall(r'Would link', out)) == 2, out
        assert 'dry run' in out.lower()

    def test_the_script_refuses_without_gnu_stat(self, tmp_path):
        lib = Lib(tmp_path)
        paths = _pack(lib, 2, 'two', size=3000)
        t, m = lib.audit()
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        before = [lib.ino(p) for p in paths]
        shim = _stat_shim(tmp_path, no_c=True)
        code, out = _run(text, lib.root, path_prefix=shim)
        assert code != 0, out
        assert 'GNU stat' in out
        assert [lib.ino(p) for p in paths] == before

    def test_a_file_named_like_a_flag_is_linked_not_parsed(self, tmp_path):
        """C12's parity. With no media path the script root is the torrent
        folder, so these reach the script as `-f.bin` and `-n.bin`."""
        lib = Lib(tmp_path)
        data = _bytes(2048, 13)
        lib.file('torrents/-f.bin', data)
        lib.file('torrents/-n.bin', data)
        t, m = lib.audit()
        cfg = lib.cfg(MEDIA_PATH='')
        text = _text(_script(t, m, cfg, tmp_path, groups=_ids(_report(t, m, cfg, tmp_path))))
        code, out = _run(text, lib.torrents)
        assert code == 0, out
        assert lib.ino('torrents/-f.bin') == lib.ino('torrents/-n.bin'), out

    def test_an_old_script_warns_and_still_runs(self, tmp_path):
        lib = Lib(tmp_path)
        paths = _pack(lib, 2, 'two', size=3000)
        t, m = lib.audit()
        text = _text(_script(t, m, lib.cfg(), tmp_path,
                             groups=_ids(_report(t, m, lib.cfg(), tmp_path))))
        assert re.search(r'^GENERATED_AT=\d+$', text, re.M)
        text = re.sub(r'^GENERATED_AT=\d+$', 'GENERATED_AT=1000000000', text, flags=re.M)
        code, out = _run(text, lib.root)
        assert code == 0, out
        assert 'hours ago' in out
        assert lib.ino(paths[0]) == lib.ino(paths[1])

    def test_the_script_parses_under_bash_n(self, tmp_path):
        lib = Lib(tmp_path)
        data = _bytes(2048, 14)
        lib.file("torrents/Ocean's 11 [1960]/Ocean's 11 & more; $(x).mkv", data)
        lib.file('torrents/Show $HOME/S01E01 `x`.mkv', data)
        t, m = lib.audit()
        resp = _script(t, m, lib.cfg(), tmp_path, groups=_ids(_report(t, m, lib.cfg(), tmp_path)))
        assert resp.status_code == 200
        proc = subprocess.run([_bash(), '-n'], input=resp.get_data(), capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert 'ln -f' not in _text(resp)


# ═════════════════════════════════════════════════════════════════════════════
# S10 and the selection rule
# ═════════════════════════════════════════════════════════════════════════════

class TestScriptInputs:

    def test_a_newline_in_a_name_never_reaches_a_comment_line(self, tmp_path):
        """A newline ends a comment, and the rest of the name runs — after the
        working-directory guard has passed, which is the normal case. NTFS
        cannot hold the name, so the record is built by hand and `os.stat` is
        answered for it."""
        run = tmp_path / 'run'
        good = run / 'movies' / 'A' / 'a.mkv'
        good.parent.mkdir(parents=True)
        good.write_bytes(_bytes(64))
        evil = '/data/torrents/movies/B/x\ntouch PWNED\n.mkv'
        t, m = _hand_audit({
            (9, 1): _inode(size=64, torrent_paths=['/data/torrents/movies/A/a.mkv'],
                           torrent_rel_path='movies/A/a.mkv'),
            (9, 2): _inode(size=64, torrent_paths=[evil],
                           torrent_rel_path=evil[len('/data/torrents/'):]),
        })
        cfg = {'LOCAL_PATH': '/data/torrents', 'MEDIA_PATH': ''}
        with patch('os.stat', _fake_stat({'/data/torrents': run}, exact={evil: str(good)})):
            ids = _ids(_report(t, m, cfg, tmp_path))
            resp = _script(t, m, cfg, tmp_path, groups=ids)
        assert resp.status_code == 200, _text(resp)
        code, out = _run(_text(resp), run)
        assert not list(tmp_path.rglob('PWNED')), out
        # The name's second line is still in the script — inside the quoted
        # argument that starts on the line before it, where it is inert.
        lines = _text(resp).splitlines()
        for i, line in enumerate(lines):
            if line == 'touch PWNED':
                assert lines[i - 1].startswith("copy './movies/B/x"), lines[i - 1]

    def test_script_root_from_config_never_reaches_a_comment_raw(self, tmp_path):
        run = tmp_path / 'run'
        root = '/data/tor\ntouch PWNED\nrents'
        data = _bytes(64, 2)
        for rel in ('movies/A/a.mkv', 'movies/B/b.mkv'):
            (run / rel).parent.mkdir(parents=True, exist_ok=True)
            (run / rel).write_bytes(data)
        t, m = _hand_audit({
            (9, 1): _inode(size=64, torrent_paths=[f'{root}/movies/A/a.mkv'],
                           torrent_rel_path='movies/A/a.mkv'),
            (9, 2): _inode(size=64, torrent_paths=[f'{root}/movies/B/b.mkv'],
                           torrent_rel_path='movies/B/b.mkv'),
        })
        cfg = {'LOCAL_PATH': root, 'MEDIA_PATH': ''}
        with patch('os.stat', _fake_stat({root: run})):
            ids = _ids(_report(t, m, cfg, tmp_path))
            resp = _script(t, m, cfg, tmp_path, groups=ids)
        assert resp.status_code == 200, _text(resp)
        code, out = _run(_text(resp), run)
        assert not list(tmp_path.rglob('PWNED')), out

    def test_an_empty_or_unmatched_selection_scripts_nothing(self, tmp_path):
        """C14's precedent. `if selection.get('groups')` read `[]` as no filter,
        so an empty selection — and a GET — scripted every group."""
        lib = Lib(tmp_path)
        _f1(lib)
        t, m = lib.audit()
        cfg = lib.cfg()
        for resp in (_script(t, m, cfg, tmp_path, method='get'),
                     _script(t, m, cfg, tmp_path, body={}),
                     _script(t, m, cfg, tmp_path, body={'groups': []}),
                     _script(t, m, cfg, tmp_path, body={'groups': 'all'})):
            assert resp.status_code == 400, _text(resp)
            assert resp.get_json()['code'] == 'selection_required'
            assert '#!/bin/bash' not in _text(resp)
        resp = _script(t, m, cfg, tmp_path, groups=['torrents/nothing/here.mkv'])
        assert resp.status_code == 409, _text(resp)
        assert resp.get_json()['code'] == 'nothing_selected'

    def test_a_member_outside_the_script_root_is_never_scripted(self, tmp_path):
        """§10. With no common ancestor the script root falls back to
        `LOCAL_PATH`, and a library path became `../../data/media/…` — a path
        outside the folder the user was told to `cd` into."""
        host = tmp_path / 'host'
        data = _bytes(64, 3)
        for rel in ('srv/torrents/movies/A/a.bin', 'srv/torrents/movies/B/b.bin',
                    'data/media/Film/c.bin'):
            (host / rel).parent.mkdir(parents=True, exist_ok=True)
            (host / rel).write_bytes(data)
        t, m = _hand_audit({
            (9, 1): _inode(size=64, torrent_paths=['/srv/torrents/movies/A/a.bin'],
                           torrent_rel_path='movies/A/a.bin'),
            (9, 2): _inode(size=64, torrent_paths=['/srv/torrents/movies/B/b.bin'],
                           torrent_rel_path='movies/B/b.bin'),
            (9, 3): _inode(size=64, media_paths=['/data/media/Film/c.bin'],
                           media_rel_path='Film/c.bin'),
        })
        cfg = {'LOCAL_PATH': '/srv/torrents', 'MEDIA_PATH': '/data/media'}
        stat = _fake_stat({'/srv/torrents': host / 'srv' / 'torrents',
                           '/data/media': host / 'data' / 'media'})
        with patch('os.stat', stat):
            report = _report(t, m, cfg, tmp_path)
            resp = _script(t, m, cfg, tmp_path, groups=_ids(report))
        g = report['groups'][0]
        assert g['status'] == 'unverifiable' and g['reason'] == 'outside_script_root'
        assert resp.status_code == 200, _text(resp)
        assert not any(p.startswith('..') or '/../' in p for p in _copy_paths(_text(resp)))

        c_before = os.stat(host / 'data' / 'media' / 'Film' / 'c.bin').st_ino
        code, out = _run(_text(resp), host / 'srv' / 'torrents')
        assert code == 0, out
        assert (os.stat(host / 'srv/torrents/movies/A/a.bin').st_ino
                == os.stat(host / 'srv/torrents/movies/B/b.bin').st_ino), out
        assert os.stat(host / 'data' / 'media' / 'Film' / 'c.bin').st_ino == c_before


# ═════════════════════════════════════════════════════════════════════════════
# Classification — §5.3, F4, F12
# ═════════════════════════════════════════════════════════════════════════════

def _mountinfo(tmp_path, *lines):
    p = tmp_path / 'mountinfo'
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(p)


def _mount_point(path):
    return _posix(path).replace('\\', '\\134').replace(' ', '\\040')


class TestClassification:

    def test_a_fuse_mount_reads_unverifiable(self, tmp_path):
        """§5.3 / F4 (their half: essentrix83). SHFS and mergerfs report one
        `st_dev` for every branch, so a device comparison says nothing there."""
        lib = Lib(tmp_path)
        _f1(lib)
        t, m = lib.audit()
        root = _mount_point(lib.root)
        ext4_root = '22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw'
        # A mount whose path is a string prefix of the data root but not a
        # parent of it must never match.
        decoy = f'97 22 0:43 / {root}-decoy rw shared:39 - fuse.mergerfs pool rw'
        for fstype in ('fuse.shfs', 'fuse.mergerfs'):
            info = _mountinfo(tmp_path, ext4_root, decoy,
                              f'98 22 0:44 / {root} rw,nosuid,nodev shared:40 - {fstype} shfs rw')
            g = _report(t, m, lib.cfg(), tmp_path, mountinfo=info)['groups'][0]
            assert (g['status'], g['reason']) == ('unverifiable', 'pooled_mount'), g
            assert g['facts']['fstype'] == fstype

        info = _mountinfo(tmp_path, ext4_root, decoy)
        report = _report(t, m, lib.cfg(), tmp_path, mountinfo=info)
        g = report['groups'][0]
        assert g['status'] == 'linkable' and g['facts']['fstype'] == 'ext4', g
        assert report['mount']['checked'] is True

        report = _report(t, m, lib.cfg(), tmp_path)             # unreadable
        assert report['groups'][0]['status'] == 'linkable'
        assert report['mount']['checked'] is False
        assert report['groups'][0]['facts']['fstype'] is None

    def test_a_group_whose_copies_report_two_devices_is_cross_device(self, tmp_path):
        lib = Lib(tmp_path)
        data = _bytes(2048, 15)
        lib.file('torrents/diskA/a.bin', data)
        lib.file('torrents/diskB/b.bin', data)
        t, m = lib.audit()
        real = os.stat

        def two_devices(p, *a, **k):
            st = real(p, *a, **k)
            if isinstance(p, (str, os.PathLike)) and 'diskB' in _posix(p):
                return os.stat_result((st.st_mode, st.st_ino, 4242) + tuple(st)[3:])
            return st
        with patch('os.stat', two_devices):
            g = _report(t, m, lib.cfg(), tmp_path)['groups'][0]
        assert g['status'] == 'cross_device' and g['selectable'] is True, g
        assert g['facts']['devices'] == 2

    def test_a_missing_member_is_stale_and_unselectable(self, tmp_path):
        lib = Lib(tmp_path)
        paths = _pack(lib, 3, 'three', size=3000)
        t, m = lib.audit()
        os.remove(lib.root / paths[2])
        report = _report(t, m, lib.cfg(), tmp_path)
        g = report['groups'][0]
        assert (g['status'], g['reason'], g['selectable']) == ('stale', 'missing', False), g
        resp = _script(t, m, lib.cfg(), tmp_path, groups=_ids(report))
        assert resp.status_code == 409
        assert resp.get_json()['code'] == 'nothing_selected'

    def test_groups_sort_by_status_then_bytes(self, tmp_path):
        lib = Lib(tmp_path)
        small = _pack(lib, 2, 'small', size=1000, seed=1)
        _pack(lib, 2, 'big', size=5000, seed=2)
        gone = _pack(lib, 2, 'gone', size=9000, seed=3)
        t, m = lib.audit()
        os.remove(lib.root / gone[1])
        report = _report(t, m, lib.cfg(), tmp_path)
        assert [(g['status'], g['size']) for g in report['groups']] == [
            ('linkable', 5000), ('linkable', 1000), ('stale', 9000)]
        assert small[0] in _ids(report)

    def test_each_group_states_its_cause(self, tmp_path):
        """Decision 2 (a): the cause comes from the group's own paths. A torrent
        copy and a library copy on different inodes is a hardlink that should
        exist; copies within one tree are plain duplicates."""
        lib = Lib(tmp_path)
        arr = _bytes(2048, 16)
        lib.file('torrents/movies/Rel/rel.mkv', arr)
        lib.file('media/Rel (2020)/Rel (2020).mkv', arr)          # the arr copied
        _pack(lib, 2, 'twice', size=3000)
        t, m = lib.audit()
        report = _report(t, m, lib.cfg(), tmp_path)
        causes = {g['size']: g['cause'] for g in report['groups']}
        assert causes == {2048: 'missing_hardlink', 3000: 'copies'}
        assert report['missing_hardlink_count'] == 1
        assert report['file_count'] == 4

    def test_already_hardlinked_is_logged_never_rendered(self, tmp_path, caplog):
        """F12. Two members of one group resolving to one inode cannot come out
        of `_build_duplicate_map`, which iterates an inode-keyed map — so this
        fixture is written by hand, deliberately. It is logged as a count and
        merged; it is never a state on the page."""
        host = tmp_path / 'host'
        data = _bytes(64, 4)
        for rel in ('torrents/a/x.mkv', 'torrents/b/y.mkv', 'media/X/x.mkv'):
            (host / rel).parent.mkdir(parents=True, exist_ok=True)
            (host / rel).write_bytes(data)
        inodes = {
            (9, 1): _inode(size=64, torrent_paths=['/data/torrents/a/x.mkv'],
                           torrent_rel_path='a/x.mkv',
                           media_paths=['/data/media/X/x.mkv'], media_rel_path='X/x.mkv'),
            (9, 2): _inode(size=64, torrent_paths=['/data/torrents/b/y.mkv'],
                           torrent_rel_path='b/y.mkv'),
        }
        dup = {(9, 1): ['/data/torrents/b/y.mkv', '/data/media/X/x.mkv'],
               (9, 2): ['/data/torrents/a/x.mkv']}
        t, m = _assemble_records([(9, 1), (9, 2)], [(9, 1)], inodes, dup)
        cfg = {'LOCAL_PATH': '/data/torrents', 'MEDIA_PATH': '/data/media'}
        caplog.set_level('INFO')
        with patch('os.stat', _fake_stat({'/data': host})):
            report = _report(t, m, cfg, tmp_path)
        assert len(report['groups']) == 1
        assert _members(report['groups'][0]) == [['media/X/x.mkv', 'torrents/a/x.mkv'],
                                                 ['torrents/b/y.mkv']]
        assert 'already_hardlinked' not in json.dumps(report)
        messages = [r.getMessage() for r in caplog.records]
        assert any('same file' in msg and ' 1 ' in f' {msg} ' for msg in messages), messages
        assert not any('x.mkv' in msg for msg in messages)


# ═════════════════════════════════════════════════════════════════════════════
# F7 — the fast hash
# ═════════════════════════════════════════════════════════════════════════════

class TestFastHash:

    def test_the_fast_hash_reads_the_middle(self, tmp_path):
        """Two encodes sharing a container header and trailer collide on a
        head+tail hash. `cmp` would catch it in the script, after reading the
        whole file; the candidate should never be offered at all."""
        lib = Lib(tmp_path)
        size = 300_000
        a = bytearray(_bytes(size, 5))
        b = bytearray(a)
        b[size // 2] ^= 0xFF
        lib.file('torrents/x/a.bin', bytes(a))
        lib.file('torrents/y/b.bin', bytes(b))
        t, m = lib.audit()
        assert all(not r['duplicate_paths'] for r in t + m)
        assert _report(t, m, lib.cfg(), tmp_path)['groups'] == []
