"""Cleanup: the code that decides what gets deleted (CLEANUP C15).

Nothing in `backend_tests/` touched `workflows_cleanup`, the delete script, or
`generate_script('orphaned_torrents_delete', …)` before this file. Cleanup is
the one workflow whose output destroys data with no second copy anywhere, and
the one fact that decides everything — *no torrent claims this file* — is the
one fact the script cannot check.

Two kinds of test live here and are kept apart:

* **Characterisation** — behaviour that is already right. It passed before
  Phase 8 and must keep passing through it.
* **Findings** — C3/C4c, C5, C6, C9, C10, C11, C12, C13, C14, C16, idempotency,
  `--dry-run`. Each was written before its fix and failed for the reason its
  finding names.

Assertions are on the response, the script text, and **what running the
script did to `tmp_path`** — never on a helper's return value alone. The script
contract (C11–C13) has no other honest test: a string that looks like a correct
`rm` is not one.

Audit-side tests walk a real tree built with `os.link` (the `tree` idiom from
`test_trump_resolution.py`) rather than a hand-built `inode_map`, because C5
lives in the fold from walk to record.
"""
import os
import re
import shutil
import subprocess
import time
import unicodedata
from unittest.mock import MagicMock, patch

import pytest

import app
import audit
import sources
from audit import _assemble_records, _walk_directory
from exclusions import compile_exclusions
from media_server_exclusions import expand_exclusion_patterns

REMOTE = '/data/torrents'


# ── fixtures ──────────────────────────────────────────────────────────────────

def _orphan(path, size=100, **over):
    rec = {'path': path, 'size': size, 'status': 'Orphaned', 'imported': False,
           'excluded': False, 'linked_paths': [], 'hash': ''}
    rec.update(over)
    return rec


def _details(**over):
    return {'dashboard': {'current': {'details': dict(over)}}}


def _loader(records, compact=True, loaded=None, beside=None):
    """`db_load_file_results` / `db_has_file_results` over one stored list.

    With `compact`, only the `cleanup` row exists and loading the full
    `torrents` row is an error — C10's contract is that neither the page nor the
    script path deserializes it once the compact row exists. `beside` is the
    `cleanup_folders` row (C26, C27), absent unless given.
    """
    loaded = [] if loaded is None else loaded

    def load(tab, conn=None):
        if tab == 'cleanup_folders':
            return list(beside or [])
        loaded.append(tab)
        if compact and tab == 'torrents':
            raise AssertionError('the full torrents row was deserialized')
        return list(records)

    def has(tab, conn=None):
        if tab == 'cleanup_folders':
            return beside is not None
        return compact and tab == 'cleanup'
    return load, has, loaded


def _cleanup(records, cfg=None, compact=False, details=None):
    load, has, loaded = _loader(records, compact)
    with patch.object(app, 'db_load_config', return_value=dict(cfg or {'LOCAL_PATH': ''})), \
         patch.object(app, 'db_load_results', return_value=details or _details()), \
         patch.object(app, 'db_has_file_results', side_effect=has), \
         patch.object(app, 'db_load_file_results', side_effect=load):
        resp = app.app.test_client().get('/api/workflows/cleanup')
    resp.loaded = loaded
    return resp


def _row(h, name, save_path=REMOTE + '/movies', content_path=None, progress=1.0,
         completion_on=1789000000, inst=None):
    return {'hash': h, 'name': name, 'size': 1000, 'save_path': save_path,
            'content_path': content_path if content_path is not None else f'{save_path}/{name}',
            'progress': progress, 'completion_on': completion_on,
            'tracker': 't.example', 'instance_id': inst, 'instance_name': None}


def _report(failed=()):
    rep = sources.new_source_report('qbit')
    rep['instances_total'] = 1 + len(failed)
    rep['instances_ok'] = 1
    for name in failed:
        sources.report_instance_failure(rep, name, 'timed out')
    return rep


def _script(records, paths, *, rows=(), listing=None, cfg=None, compact=False,
            details=None, list_error=None, report=None, method='post', folders=None,
            empty=None):
    """POST a selection to the delete-script endpoint with a faked live client.

    Reads the full `torrents` row by default, which is the fallback path and
    goes through the same code; `test_cleanup_reads_the_compact_row` is the one
    that asserts the compact row is what gets read. Defaulting to compact would
    make every finding below fail on C10 rather than on its own finding.

    `empty` lists the empty folders the last scan found (C27) and `folders` is
    the folder half of the selection.
    """
    cfg = {'LOCAL_PATH': REMOTE, 'REMOTE_PATH': REMOTE, **(cfg or {})}
    beside = None
    if empty is not None:
        beside = [{'kind': 'empty', 'path': f, 'mtime': 1} for f in empty]
        details = details or _details(empty_folder_count=len(empty))
    load, has, loaded = _loader(records, compact, beside=beside)
    listing = listing or {}
    # Registration-keyed, as `sources.fetch_torrent_file_paths` answers (S05).
    fetch = MagicMock(side_effect=lambda _c, items: {app._reg(i): listing.get(i['hash'])
                                                     for i in items})
    detailed = MagicMock(side_effect=list_error) if list_error else \
        MagicMock(return_value=(list(rows), report or _report()))
    with patch.object(app, 'db_load_config', return_value=cfg), \
         patch.object(app, 'db_load_results', return_value=details or _details()), \
         patch.object(app, 'db_has_file_results', side_effect=has), \
         patch.object(app, 'db_load_file_results', side_effect=load), \
         patch.object(app.sources, 'list_torrents_detailed', detailed), \
         patch.object(app.sources, 'fetch_torrent_file_paths', fetch):
        client = app.app.test_client()
        url = '/api/actions/script/orphaned_torrents_delete'
        if method == 'get':
            resp = client.get(url)
        else:
            body = {} if paths is None else {'paths': list(paths)}
            if folders is not None:
                body['folders'] = list(folders)
            resp = client.post(url, json=body)
    resp.loaded, resp.fetch, resp.detailed = loaded, fetch, detailed
    return resp


def _text(resp):
    return resp.get_data(as_text=True)


# ── a real tree, walked by the real audit ─────────────────────────────────────

def _audit(torrents, media, file_map=None, patterns=(), **kw):
    """Walk both trees exactly as `run_audit_process` does and assemble records."""
    expanded = expand_exclusion_patterns({'EXCLUSION_PATTERNS': list(patterns)})
    compiled = compile_exclusions(expanded)
    inode_map = {}
    tko, _, _, _ = _walk_directory(str(torrents), 'Torrent', inode_map, file_map or {},
                                   0, 0, exclusion_patterns=expanded,
                                   compiled_exclusions=compiled)
    mko, _, _, _ = _walk_directory(str(media), 'Media', inode_map, file_map or {},
                                   0, 0, exclusion_patterns=expanded,
                                   compiled_exclusions=compiled)
    torrent_files, media_files = _assemble_records(tko, mko, inode_map, {},
                                                   compiled_exclusions=compiled, **kw)
    return torrent_files, media_files


def _write(path, data=b'x' * 64):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def trees(tmp_path):
    torrents, media = tmp_path / 'torrents', tmp_path / 'media'
    torrents.mkdir()
    media.mkdir()
    return torrents, media


def _posix(p):
    return str(p).replace('\\', '/')


def _walk_order(order):
    """Patch `os.walk` to visit sibling folders in a fixed order.

    NTFS lists alphabetically and ext4 by hash, so which of two hardlinked paths
    the walk reaches first is not something a test can leave to the filesystem
    when that is the thing under test. `os.walk` is top-down, so sorting the
    `dirs` it yields steers the descent.
    """
    real = os.walk

    def walk(top, *a, **kw):
        for root, dirs, files in real(top, *a, **kw):
            dirs.sort(reverse=(order == 'desc'))
            files.sort(reverse=(order == 'desc'))
            yield root, dirs, files
    return patch('os.walk', walk)


def _group(report, folder):
    return next(g for g in report['groups'] if g['folder'] == folder)


def _all_rows(report):
    return [f for g in report['groups'] for f in g['files']]


# ── running the generated script ──────────────────────────────────────────────

def _bash():
    """A bash that can see `tmp_path`, or skip.

    On the dev machine `shutil.which('bash')` resolves Git Bash. The System32
    `bash.exe` is the WSL launcher, which runs in a different filesystem
    namespace and cannot see a Windows temp directory at all.
    """
    found = shutil.which('bash')
    if not found or 'system32' in found.lower() or 'windowsapps' in found.lower():
        pytest.skip('no bash that can see tmp_path')
    return found


def _run(script_text, cwd, *args, path_prefix=None):
    script = cwd.parent / f'cleanup_{len(list(cwd.parent.glob("cleanup_*.sh")))}.sh'
    script.write_bytes(script_text.encode('utf-8'))
    env = dict(os.environ)
    if path_prefix:
        env['PATH'] = str(path_prefix) + os.pathsep + env.get('PATH', '')
    # No terminal on stdin, as from cron or a pipe: a script that would ask
    # (`[ -t 0 ]`) must not wait for an answer here.
    proc = subprocess.run([_bash(), str(script), *args], cwd=str(cwd),
                          capture_output=True, env=env, timeout=120,
                          stdin=subprocess.DEVNULL)
    out = proc.stdout.decode('utf-8', 'replace') + proc.stderr.decode('utf-8', 'replace')
    return proc.returncode, out


def _local_cfg(torrents):
    return {'LOCAL_PATH': str(torrents), 'REMOTE_PATH': REMOTE}


# ═════════════════════════════════════════════════════════════════════════════
# Characterisation — already right, must stay right
# ═════════════════════════════════════════════════════════════════════════════

class TestAlreadyRight:

    def test_excluded_orphans_are_never_emitted(self):
        records = [_orphan('movies/Rel/keep.mkv'),
                   _orphan('movies/Rel/hidden.nfo', excluded=True)]
        report = _cleanup(records).get_json()
        paths = [p for f in _all_rows(report) for p in f.get('paths', [f['path']])]
        assert paths == ['movies/Rel/keep.mkv']

        resp = _script(records, ['movies/Rel/keep.mkv', 'movies/Rel/hidden.nfo'],
                       compact=False)
        assert resp.status_code == 200
        assert 'keep.mkv' in _text(resp)
        assert 'hidden.nfo' not in _text(resp)

    def test_excluded_count(self):
        records = [_orphan('movies/A/a.mkv'),
                   _orphan('movies/B/b.nfo', excluded=True),
                   _orphan('movies/B/c.nfo', excluded=True),
                   {**_orphan('movies/C/seeding.mkv'), 'status': 'Seeding', 'excluded': True}]
        assert _cleanup(records).get_json()['excluded_count'] == 2

    def test_a_tombstone_is_never_emitted(self, trees):
        """Walked for real: the always-on tombstone rule excludes it at the walk."""
        torrents, media = trees
        _write(torrents / 'tv' / 'Show.S01' / 'Show.S01E01.mkv')
        _write(torrents / 'tv' / 'Show.S01' / '.fuse_hidden00314807000c8ea2')
        torrent_files, _ = _audit(torrents, media)
        tomb = next(r for r in torrent_files if '.fuse_hidden' in r['path'])
        assert tomb['status'] == 'Orphaned' and tomb['excluded'] is True

        report = _cleanup(torrent_files).get_json()
        assert not any('.fuse_hidden' in p for f in _all_rows(report)
                       for p in f.get('paths', [f['path']]))
        resp = _script(torrent_files, [_posix(r['path']) for r in torrent_files],
                       compact=False)
        assert resp.status_code == 200
        assert '.fuse_hidden' not in _text(resp)

    def test_os_and_nas_clutter_is_never_emitted(self, trees):
        """C24, walked for real: a Mac's `.DS_Store`, a Synology thumbnail
        folder and a recycle bin are excluded at the walk, and only the real
        stray file is offered."""
        torrents, media = trees
        _write(torrents / 'tv' / 'Show.S01' / '.DS_Store')
        _write(torrents / 'tv' / 'Show.S01' / '@eaDir' / 'Show.S01E01.mkv' / 'SYNOPHOTO_THUMB_M.jpg')
        _write(torrents / '#recycle' / 'Old.Release' / 'Old.Release.mkv')
        _write(torrents / 'tv' / 'Show.S01' / 'stray.txt')
        torrent_files, _ = _audit(torrents, media)
        clutter = [r for r in torrent_files if 'stray' not in r['path']]
        assert len(clutter) == 3 and all(r['status'] == 'Orphaned' and r['excluded'] is True
                                         for r in clutter)

        report = _cleanup(torrent_files).get_json()
        offered = [p for f in _all_rows(report) for p in f.get('paths', [f['path']])]
        assert [_posix(p) for p in offered] == ['tv/Show.S01/stray.txt']
        assert report['excluded_count'] == 3

    def test_the_script_skips_a_missing_file(self, tmp_path):
        torrents = tmp_path / 'torrents'
        _write(torrents / 'movies' / 'Rel' / 'a.mkv')
        records = [_orphan('movies/Rel/a.mkv', size=64), _orphan('movies/Rel/b.mkv', size=64)]
        resp = _script(records, ['movies/Rel/a.mkv', 'movies/Rel/b.mkv'],
                       cfg=_local_cfg(torrents), compact=False)
        code, out = _run(_text(resp), torrents)
        assert code == 0, out
        assert not (torrents / 'movies' / 'Rel' / 'a.mkv').exists()
        assert 'b.mkv' in out

    def test_the_nlink_accounting_lines(self, tmp_path):
        torrents = tmp_path / 'torrents'
        _write(torrents / 'movies' / 'Rel' / 'a.mkv')
        linked = _write(torrents / 'movies' / 'Rel' / 'b.mkv')
        (tmp_path / 'elsewhere').mkdir()
        os.link(linked, tmp_path / 'elsewhere' / 'b.mkv')
        records = [_orphan('movies/Rel/a.mkv', size=64), _orphan('movies/Rel/b.mkv', size=64)]
        resp = _script(records, ['movies/Rel/a.mkv', 'movies/Rel/b.mkv'],
                       cfg=_local_cfg(torrents), compact=False)
        code, out = _run(_text(resp), torrents)
        assert re.search(r'Hardlinked \(space not freed yet\):\s+1 file', out), out
        assert re.search(r'Standalone \(space freed\):\s+1 file', out), out
        assert (tmp_path / 'elsewhere' / 'b.mkv').exists()

    def test_a_wrong_directory_is_still_an_error(self, tmp_path):
        torrents = tmp_path / 'torrents'
        _write(torrents / 'movies' / 'Rel' / 'a.mkv')
        wrong = tmp_path / 'somewhere_else'
        wrong.mkdir()
        records = [_orphan('movies/Rel/a.mkv', size=64)]
        resp = _script(records, ['movies/Rel/a.mkv'], cfg=_local_cfg(torrents), compact=False)
        code, out = _run(_text(resp), wrong)
        assert code != 0
        assert 'ERROR' in out
        assert (torrents / 'movies' / 'Rel' / 'a.mkv').exists()

    def test_the_script_parses_under_bash_n(self):
        records = [_orphan("movies/Ocean's 11 [1960]/Ocean's 11 *.mkv", excl_folder="movies/Ocean's 11 [1960]"),
                   _orphan('tv/Show — Dash/S01E01 $HOME `x`.mkv')]
        resp = _script(records, [r['path'] for r in records])
        assert resp.status_code == 200
        proc = subprocess.run([_bash(), '-n'], input=resp.get_data(), capture_output=True)
        assert proc.returncode == 0, proc.stderr

    def test_the_full_row_is_the_fallback_until_the_first_scan(self):
        resp = _cleanup([_orphan('movies/Rel/a.mkv')], compact=False)
        assert resp.status_code == 200
        assert resp.get_json()['file_count'] == 1


# ═════════════════════════════════════════════════════════════════════════════
# C3 + C4c — the live re-verify at script generation
# ═════════════════════════════════════════════════════════════════════════════

class TestLiveReverify:

    def test_a_torrent_added_after_the_audit_drops_its_files_from_the_script(self):
        """The audit said orphan; a cross-seed script injected a torrent since."""
        records = [_orphan('movies/Rel/Rel.mkv'), _orphan('movies/Rel/Rel.nfo')]
        resp = _script(records, ['movies/Rel/Rel.mkv', 'movies/Rel/Rel.nfo'],
                       rows=[_row('aaa', 'Rel')],
                       listing={'aaa': [f'{REMOTE}/movies/Rel/Rel.mkv']})
        assert resp.status_code == 200
        text = _text(resp)
        assert 'Rel.mkv' not in text.replace('Rel.nfo', '')
        assert 'Rel.nfo' in text
        assert '# 1 file(s) are no longer orphaned and were removed from this script.' in text
        assert resp.headers['X-Auditorr-Dropped'] == '1'
        assert resp.headers['X-Auditorr-Files'] == '1'
        assert re.search(r'^VERIFIED_AT=\d+$', text, re.M)
        assert int(resp.headers['X-Auditorr-Verified-At']) > 0

    def test_an_unreachable_client_emits_no_script(self):
        resp = _script([_orphan('movies/Rel/Rel.mkv')], ['movies/Rel/Rel.mkv'],
                       list_error=sources.SourceConnectionError('refused'))
        assert resp.status_code == 502
        body = resp.get_json()
        assert body['code'] == 'client_unreachable'
        assert '#!/bin/bash' not in _text(resp)

    def test_a_failed_instance_emits_no_script(self):
        resp = _script([_orphan('movies/Rel/Rel.mkv')], ['movies/Rel/Rel.mkv'],
                       report=_report(failed=['second']))
        assert resp.status_code == 502
        assert resp.get_json()['code'] == 'instances_unavailable'

    def test_a_listing_that_fails_claims_what_is_on_disk(self, tmp_path):
        """`None` goes through the audit's own disk fallback, which over-claims."""
        torrents = tmp_path / 'torrents'
        _write(torrents / 'movies' / 'Rel' / 'Rel.mkv')
        records = [_orphan('movies/Rel/Rel.mkv'), _orphan('movies/Other/o.mkv')]
        resp = _script(records, ['movies/Rel/Rel.mkv', 'movies/Other/o.mkv'],
                       cfg=_local_cfg(torrents), rows=[_row('aaa', 'Rel')],
                       listing={'aaa': None})
        assert resp.status_code == 200
        assert 'Rel.mkv' not in _text(resp)
        assert resp.headers['X-Auditorr-Dropped'] == '1'

    def test_an_unfinished_torrent_claims_its_incomplete_spelling(self):
        records = [_orphan('movies/Rel/Rel.mkv.!qB'), _orphan('movies/Other/o.mkv')]
        resp = _script(records, ['movies/Rel/Rel.mkv.!qB', 'movies/Other/o.mkv'],
                       rows=[_row('aaa', 'Rel', progress=0.4)],
                       listing={'aaa': [f'{REMOTE}/movies/Rel/Rel.mkv']})
        assert resp.status_code == 200
        assert '.!qB' not in _text(resp)

    def test_nothing_left_is_a_refusal_not_an_empty_script(self):
        resp = _script([_orphan('movies/Rel/Rel.mkv')], ['movies/Rel/Rel.mkv'],
                       rows=[_row('aaa', 'Rel')],
                       listing={'aaa': [f'{REMOTE}/movies/Rel/Rel.mkv']})
        assert resp.status_code == 409
        assert resp.get_json()['code'] == 'nothing_left'

    def test_a_healthy_library_asks_for_no_file_listings(self):
        """An orphan has no torrent, so on a sane install nothing is a candidate."""
        rows = [_row(f'h{i}', f'Other.{i}') for i in range(50)]
        resp = _script([_orphan('movies/Rel/Rel.mkv')], ['movies/Rel/Rel.mkv'], rows=rows)
        assert resp.status_code == 200
        resp.fetch.assert_not_called()

    def test_a_selection_too_broad_to_verify_is_refused(self):
        rows = [_row(f'h{i}', f'Loose.{i}.mkv', content_path='') for i in range(400)]
        resp = _script([_orphan('movies/Rel/Rel.mkv')], ['movies/Rel/Rel.mkv'], rows=rows)
        assert resp.status_code == 409
        assert resp.get_json()['code'] == 'selection_too_broad'

    def test_no_local_path_is_refused(self):
        resp = _script([_orphan('movies/Rel/Rel.mkv')], ['movies/Rel/Rel.mkv'],
                       cfg={'LOCAL_PATH': ''})
        assert resp.status_code == 400
        assert resp.get_json()['code'] == 'local_path_unset'

    def test_an_unverified_path_is_refused_by_the_server(self):
        resp = _script([_orphan('movies/Rel/Rel.mkv', unverified=True)], ['movies/Rel/Rel.mkv'])
        assert resp.status_code == 409
        assert resp.get_json()['code'] == 'unverified'


# ═════════════════════════════════════════════════════════════════════════════
# C14 — no "clean everything"
# ═════════════════════════════════════════════════════════════════════════════

class TestNoCleanEverything:

    def test_the_unselected_script_is_refused(self):
        records = [_orphan('movies/Rel/Rel.mkv')]
        assert _script(records, None, method='get').status_code == 400
        assert _script(records, None).status_code == 400
        assert _script(records, []).status_code == 400

    def test_delete_selected_is_gone(self):
        with patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app, 'db_load_results', return_value={}), \
             patch.object(app, 'db_load_file_results', return_value=[_orphan('a/b/c.mkv')]):
            resp = app.app.test_client().post('/api/actions/script/delete_selected',
                                              json={'paths': ['a/b/c.mkv']})
        assert resp.status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
# C5 + C6 — the unit is the inode, and the state says whether anything survives
# ═════════════════════════════════════════════════════════════════════════════

class TestStates:

    def test_both_paths_of_a_cross_seeded_orphan_are_listed_and_its_bytes_counted_once(self, trees):
        torrents, media = trees
        first = _write(torrents / 'tv-a' / 'Rel' / 'x.mkv', b'v' * 500)
        (torrents / 'tv-b' / 'Rel').mkdir(parents=True)
        os.link(first, torrents / 'tv-b' / 'Rel' / 'x.mkv')
        torrent_files, _ = _audit(torrents, media)

        report = _cleanup(torrent_files).get_json()
        rows = _all_rows(report)
        assert len(rows) == 1
        assert sorted(rows[0]['paths']) == ['tv-a/Rel/x.mkv', 'tv-b/Rel/x.mkv']
        assert report['total_size'] == 500
        assert report['freeable_size'] == 500

        resp = _script(torrent_files, rows[0]['paths'], compact=False)
        assert resp.status_code == 200
        assert 'tv-a/Rel/x.mkv' in _text(resp) and 'tv-b/Rel/x.mkv' in _text(resp)

    def test_a_cross_seeded_orphan_with_no_library_copy_is_a_last_copy(self, trees):
        """§5.3's correction: two torrent-tree paths are not a second copy."""
        torrents, media = trees
        first = _write(torrents / 'tv-a' / 'Rel' / 'x.mkv')
        (torrents / 'tv-b' / 'Rel').mkdir(parents=True)
        os.link(first, torrents / 'tv-b' / 'Rel' / 'x.mkv')
        torrent_files, _ = _audit(torrents, media)
        row = _all_rows(_cleanup(torrent_files).get_json())[0]
        assert row['state'] == 'last_copy'

    def test_a_library_link_is_a_library_copy(self, trees):
        torrents, media = trees
        src = _write(torrents / 'movies' / 'Rel' / 'Rel.mkv')
        (media / 'Rel (2020)').mkdir()
        os.link(src, media / 'Rel (2020)' / 'Rel (2020).mkv')
        torrent_files, _ = _audit(torrents, media)
        report = _cleanup(torrent_files).get_json()
        assert _all_rows(report)[0]['state'] == 'library_copy'
        assert report['freeable_size'] == 0

    def test_a_link_outside_both_trees_is_linked_elsewhere(self, trees, tmp_path):
        torrents, media = trees
        src = _write(torrents / 'movies' / 'Rel' / 'Rel.mkv')
        (tmp_path / 'snapshot').mkdir()
        os.link(src, tmp_path / 'snapshot' / 'Rel.mkv')
        torrent_files, _ = _audit(torrents, media)
        report = _cleanup(torrent_files).get_json()
        assert _all_rows(report)[0]['state'] == 'linked_elsewhere'
        assert report['freeable_size'] == 0

    def test_an_unverified_orphan_never_renders_as_anything_else(self, trees):
        torrents, media = trees
        src = _write(torrents / 'movies' / 'Rel' / 'Rel.mkv')
        (media / 'Rel').mkdir()
        os.link(src, media / 'Rel' / 'Rel.mkv')                 # would be library_copy
        _write(torrents / 'tv' / 'Show' / 'e.mkv')
        torrent_files, _ = _audit(torrents, media, unverified={
            'all': False, 'roots': [str(torrents / 'movies')]})
        report = _cleanup(torrent_files).get_json()
        states = {f['path']: f['state'] for f in _all_rows(report)}
        assert states == {'movies/Rel/Rel.mkv': 'unverified', 'tv/Show/e.mkv': 'last_copy'}

    def test_a_scan_that_persisted_past_a_failed_instance_is_unverified_throughout(self, trees):
        torrents, media = trees
        _write(torrents / 'movies' / 'Rel' / 'Rel.mkv')
        torrent_files, _ = _audit(torrents, media, unverified={'all': True, 'roots': []})
        assert all(f['state'] == 'unverified'
                   for f in _all_rows(_cleanup(torrent_files).get_json()))

    def test_the_stamps_are_sparse(self, trees):
        """Only orphaned records pay for them — a field on every record grows files_json."""
        torrents, media = trees
        live = _write(torrents / 'tv' / 'Live' / 'e.mkv')
        _write(torrents / 'tv' / 'Gone' / 'e.mkv')
        file_map = {str(live): {'status': 'Seeding', 'trackers': set(), 'hash': 'aaa',
                                'tracker_health': 'working'}}
        torrent_files, _ = _audit(torrents, media, file_map=file_map)
        seeding = next(r for r in torrent_files if r['status'] == 'Seeding')
        for key in ('mtime', 'nlink', 'other_paths', 'unverified', 'excl_refused', 'leftover'):
            assert key not in seeding
        orphan = next(r for r in torrent_files if r['status'] == 'Orphaned')
        assert isinstance(orphan['mtime'], int) and orphan['nlink'] == 1

    def test_a_failed_stat_never_produces_a_safe_state(self):
        """No `nlink` stamp and no library link reads as the alarming state."""
        report = _cleanup([_orphan('movies/Rel/Rel.mkv')]).get_json()
        row = _all_rows(report)[0]
        assert row['state'] == 'last_copy'
        assert row['mtime'] is None


# ═════════════════════════════════════════════════════════════════════════════
# C9 + C10 — nothing is stat'ed per page load, and the page reads the compact row
# ═════════════════════════════════════════════════════════════════════════════

class TestCompactAndStatFree:

    def test_the_page_stats_nothing(self):
        marker = '/nonexistent/c9-marker'
        seen = []
        real_stat, real_getmtime = os.stat, os.path.getmtime

        def _stat(p, *a, **k):
            if marker in str(p):
                seen.append(p)
            return real_stat(p, *a, **k)

        def _getmtime(p):
            if marker in str(p):
                seen.append(p)
            return real_getmtime(p)
        with patch('os.stat', side_effect=_stat), \
             patch('os.path.getmtime', side_effect=_getmtime):
            resp = _cleanup([_orphan('movies/Rel/Rel.mkv')], cfg={'LOCAL_PATH': marker})
        assert resp.status_code == 200
        assert seen == []

    def test_cleanup_reads_the_compact_row(self):
        records = [_orphan('movies/Rel/Rel.mkv', mtime=1700000000, nlink=1)]
        resp = _cleanup(records, compact=True,
                        details=_details(orphaned_excluded_count=3))
        assert resp.status_code == 200
        assert resp.loaded == ['cleanup']
        assert resp.get_json()['excluded_count'] == 3

        resp = _script(records, ['movies/Rel/Rel.mkv'], compact=True)
        assert resp.status_code == 200
        assert 'torrents' not in resp.loaded


# ═════════════════════════════════════════════════════════════════════════════
# C16 + C7 — the folder a rule may name is stamped by the audit
# ═════════════════════════════════════════════════════════════════════════════

class TestFolderRules:

    def test_a_folder_holding_a_live_torrent_offers_no_folder_rule(self, trees):
        torrents, media = trees
        live = _write(torrents / 'tv' / 'Show.S01.1080p-GRP' / 'Show.S01E01.mkv')
        _write(torrents / 'tv' / 'Show.S01.1080p-GRP' / 'Show.S01E01.sample.mkv')
        file_map = {str(live): {'status': 'Seeding', 'trackers': set(), 'hash': 'aaa',
                                'tracker_health': 'working'}}
        torrent_files, _ = _audit(torrents, media, file_map=file_map)
        g = _group(_cleanup(torrent_files).get_json(), 'tv/Show.S01.1080p-GRP')
        assert g['excl_folder'] is None
        assert g['no_folder_rule'] == 'live_torrent'

    def test_a_live_torrents_second_hardlink_also_refuses_the_folder(self, trees):
        """Exclusivity is over every torrent-tree *path*, not every record: a
        distinct-hardlink cross-seed's second path is on no record at all."""
        torrents, media = trees
        live = _write(torrents / 'tv-a' / 'Show' / 'e.mkv')
        (torrents / 'tv-b' / 'Show').mkdir(parents=True)
        second = torrents / 'tv-b' / 'Show' / 'e.mkv'
        os.link(live, second)
        _write(torrents / 'tv-b' / 'Show' / 'stray.nfo')
        # Both registrations are live. A second path no torrent claims is a
        # leftover link instead (C17) — see TestLeftoverLinks.
        file_map = {str(live): {'status': 'Seeding', 'trackers': set(), 'hash': 'aaa',
                                'tracker_health': 'working'},
                    str(second): {'status': 'Seeding', 'trackers': set(), 'hash': 'bbb',
                                  'tracker_health': 'working'}}
        torrent_files, _ = _audit(torrents, media, file_map=file_map)
        g = _group(_cleanup(torrent_files).get_json(), 'tv-b/Show')
        assert g['excl_folder'] is None
        assert g['no_folder_rule'] == 'live_torrent'

    def test_a_one_segment_release_folder_is_offered_a_folder_rule(self, trees):
        """Phase 5 §7 item 6 — the depth rule refused these on the reference box."""
        torrents, media = trees
        folder = 'Dark Matter (2024) S01 (2160p WEBRip)[cTurtle]'
        _write(torrents / folder / 'S01E03.mkv')
        _write(torrents / folder / 'S01E06.mkv')
        _write(media / 'tv' / 'Dark Matter (2024)' / 'Season 01' / 'ep.mkv')
        torrent_files, _ = _audit(torrents, media)
        g = _group(_cleanup(torrent_files).get_json(), folder)
        assert g['excl_folder'] == folder
        assert g['no_folder_rule'] is None

    def test_a_folder_holding_an_unverified_file_offers_no_folder_rule(self, trees):
        torrents, media = trees
        _write(torrents / 'movies' / 'Rel' / 'a.mkv')
        torrent_files, _ = _audit(torrents, media, unverified={
            'all': False, 'roots': [str(torrents / 'movies' / 'Rel')]})
        g = _group(_cleanup(torrent_files).get_json(), 'movies/Rel')
        assert g['excl_folder'] is None
        assert g['no_folder_rule'] == 'unverified'

    def test_an_unstamped_group_says_so(self):
        g = _group(_cleanup([_orphan('movies/Rel/a.mkv')]).get_json(), 'movies/Rel')
        assert g['excl_folder'] is None
        assert g['no_folder_rule'] == 'not_established'


# ═════════════════════════════════════════════════════════════════════════════
# The script contract — C11, C12, C13, idempotency, --dry-run
# ═════════════════════════════════════════════════════════════════════════════

class TestScriptContract:

    def test_a_failing_rm_is_reported_failed_not_deleted(self, tmp_path):
        torrents = tmp_path / 'torrents'
        target = _write(torrents / 'movies' / 'Rel' / 'a.mkv')
        shim = tmp_path / 'shim'
        shim.mkdir()
        (shim / 'rm').write_bytes(b'#!/bin/sh\nexit 1\n')
        os.chmod(shim / 'rm', 0o755)
        records = [_orphan('movies/Rel/a.mkv', size=64, excl_folder='movies/Rel')]
        resp = _script(records, ['movies/Rel/a.mkv'], cfg=_local_cfg(torrents))
        code, out = _run(_text(resp), torrents, path_prefix=shim)
        assert target.exists()
        assert 'FAILED' in out, out
        assert '✓ Deleted' not in out
        assert 'concurrent activity' not in out
        assert code != 0

    def test_a_file_named_like_a_flag_is_deleted_not_parsed(self, tmp_path):
        torrents = tmp_path / 'torrents'
        target = _write(torrents / '-f')
        records = [_orphan('-f', size=64)]
        resp = _script(records, ['-f'], cfg=_local_cfg(torrents))
        code, out = _run(_text(resp), torrents)
        assert not target.exists(), out
        assert code == 0

    def test_a_second_run_exits_zero_and_reports_everything_already_gone(self, tmp_path):
        torrents = tmp_path / 'torrents'
        _write(torrents / 'movies' / 'Rel' / 'a.mkv')
        _write(torrents / 'movies' / 'Other' / 'keep.mkv')
        records = [_orphan('movies/Rel/a.mkv', size=64, excl_folder='movies/Rel')]
        text = _text(_script(records, ['movies/Rel/a.mkv'], cfg=_local_cfg(torrents)))
        first, _ = _run(text, torrents)
        assert first == 0
        second, out = _run(text, torrents)
        assert second == 0, out
        assert 'already gone' in out.lower()

    def test_an_emptied_release_folder_goes_and_nothing_above_or_nonempty_does(self, tmp_path):
        torrents = tmp_path / 'torrents'
        _write(torrents / 'movies' / 'Rel' / 'a.mkv')
        _write(torrents / 'movies' / 'Rel' / 'sub' / 'b.srt')
        _write(torrents / 'movies' / 'Keep' / 'x.mkv')
        _write(torrents / 'movies' / 'Keep' / 'keep.nfo')         # not selected
        _write(torrents / 'movies' / 'Film.mkv')                  # loose in the category
        records = [
            _orphan('movies/Rel/a.mkv', size=64, excl_folder='movies/Rel'),
            _orphan('movies/Rel/sub/b.srt', size=64, excl_folder='movies/Rel'),
            _orphan('movies/Keep/x.mkv', size=64, excl_folder='movies/Keep'),
            _orphan('movies/Film.mkv', size=64, excl_refused='media_root'),
        ]
        resp = _script(records, ['movies/Rel/a.mkv', 'movies/Rel/sub/b.srt',
                                 'movies/Keep/x.mkv', 'movies/Film.mkv'],
                       cfg=_local_cfg(torrents))
        code, out = _run(_text(resp), torrents)
        assert code == 0, out
        assert not (torrents / 'movies' / 'Rel').exists(), out
        assert (torrents / 'movies' / 'Keep' / 'keep.nfo').exists()
        assert (torrents / 'movies').is_dir()

    def test_dry_run_deletes_nothing(self, tmp_path):
        torrents = tmp_path / 'torrents'
        target = _write(torrents / 'movies' / 'Rel' / 'a.mkv')
        records = [_orphan('movies/Rel/a.mkv', size=64, excl_folder='movies/Rel')]
        resp = _script(records, ['movies/Rel/a.mkv'], cfg=_local_cfg(torrents))
        code, out = _run(_text(resp), torrents, '--dry-run')
        assert code == 0, out
        assert target.exists()
        assert 'dry run' in out.lower()

    def test_a_stale_script_warns_but_still_runs(self, tmp_path):
        torrents = tmp_path / 'torrents'
        target = _write(torrents / 'movies' / 'Rel' / 'a.mkv')
        records = [_orphan('movies/Rel/a.mkv', size=64, excl_folder='movies/Rel')]
        text = _text(_script(records, ['movies/Rel/a.mkv'], cfg=_local_cfg(torrents)))
        text = re.sub(r'^VERIFIED_AT=\d+$', 'VERIFIED_AT=1000000000', text, flags=re.M)
        code, out = _run(text, torrents)
        assert code == 0, out
        assert 'regenerate' in out.lower()
        assert not target.exists()

    def test_a_flat_layout_still_gets_a_working_directory_guard(self, tmp_path):
        """CODE_REVIEW_2026-09-27 CR5 (CLEANUP C20).

        With no category folder a release folder is one segment deep, so the
        anchor the guard used, its parent, is the torrent folder itself, and no
        guard was emitted. That is the reference box's layout. A live torrent
        beside it is there before the run and after it, so the guard names that.
        """
        torrents = tmp_path / 'torrents'
        target = _write(torrents / 'Some.Release.2020' / 'a.mkv')
        _write(torrents / 'Live.Release.2021' / 'l.mkv')
        records = [_orphan('Some.Release.2020/a.mkv', size=64, excl_folder='Some.Release.2020')]
        rows = [_row('lll', 'Live.Release.2021', save_path=REMOTE)]
        text = _text(_script(records, ['Some.Release.2020/a.mkv'], rows=rows,
                             cfg=_local_cfg(torrents)))
        assert 'Live.Release.2021' in text

        elsewhere = tmp_path / 'home'
        _write(elsewhere / 'notes.txt')
        code, out = _run(text, elsewhere)
        assert code == 1, out
        assert 'does not look like your torrent directory' in out

        first, out = _run(text, torrents)
        assert first == 0, out
        assert not target.exists()
        assert not (torrents / 'Some.Release.2020').exists(), "the emptied release folder stays"
        second, out = _run(text, torrents)
        assert second == 0, out
        assert 'already gone' in out.lower()

    def test_a_torrent_still_downloading_is_no_landmark(self):
        """Its final path need not exist yet."""
        rows = [_row('ddd', 'Downloading.2022', save_path=REMOTE, progress=0.4, completion_on=0),
                _row('lll', 'Live.Release.2021', save_path=REMOTE)]
        assert app._cleanup_landmarks(rows, REMOTE, '/srv/torrents') == ['Live.Release.2021']

    def test_a_category_folder_is_the_landmark_where_there_is_one(self):
        rows = [_row('lll', 'Live.Release.2021', save_path=REMOTE + '/movies'),
                _row('mmm', 'Other', save_path='/elsewhere/movies')]     # outside LOCAL_PATH
        assert app._cleanup_landmarks(rows, REMOTE, '/srv/torrents') == ['movies']

    def test_with_nothing_to_check_against_it_says_so_and_asks(self, tmp_path):
        """No category folder and no live torrent to name: the script cannot
        guard, so it says where it is about to delete, and asks where it can.
        From a pipe it cannot ask, and carries on as before."""
        torrents = tmp_path / 'torrents'
        target = _write(torrents / 'Some.Release.2020' / 'a.mkv')
        records = [_orphan('Some.Release.2020/a.mkv', size=64, excl_folder='Some.Release.2020')]
        text = _text(_script(records, ['Some.Release.2020/a.mkv'], cfg=_local_cfg(torrents)))
        assert 'Is this your torrent folder? [y/N]' in text
        code, out = _run(text, torrents)
        assert code == 0, out
        assert 'cannot check that it is running in your torrent directory' in out
        assert not target.exists()

    def test_a_newline_in_a_file_name_is_never_a_command(self, tmp_path):
        """Found writing C12, in no review. The old script put file names into
        `#` comment lines raw, and a newline ends a comment."""
        torrents = tmp_path / 'torrents'
        (torrents / 'movies' / 'Rel').mkdir(parents=True)
        evil = 'movies/Rel/x\ntouch PWNED\n.mkv'
        records = [_orphan(evil, excl_folder='movies/Rel')]
        resp = _script(records, [evil], cfg=_local_cfg(torrents))
        assert resp.status_code == 200
        code, out = _run(_text(resp), torrents)
        assert not (torrents / 'PWNED').exists(), out
        assert code == 0, out


# ═════════════════════════════════════════════════════════════════════════════
# C17 — leftover links: a path no torrent claims, to bytes a torrent still does
# ═════════════════════════════════════════════════════════════════════════════

LIVE     = 'cross-seed/Rel--aaaa'
LEFTOVER = 'cross-seed/Rel--cccc'


def _leftover_tree(trees):
    """A cross-seed removed with its files kept: its folder's file is a second
    hardlink of a live torrent's file (issue #26)."""
    torrents, media = trees
    live = _write(torrents / LIVE / 'Rel.mkv', b'v' * 500)
    (torrents / LEFTOVER).mkdir(parents=True)
    os.link(live, torrents / LEFTOVER / 'Rel.mkv')
    file_map = {str(live): {'status': 'Seeding', 'trackers': set(), 'hash': 'aaa',
                            'tracker_health': 'working'}}
    return torrents, media, file_map


class TestLeftoverLinks:

    @pytest.mark.parametrize('order', ['asc', 'desc'])
    def test_a_removed_cross_seeds_folder_is_listed_in_its_own_pile(self, trees, order):
        """Orphanhood is per inode, so one claimed path made the leftover read as
        seeding and it was in no report at all."""
        torrents, media, file_map = _leftover_tree(trees)
        with _walk_order(order):
            torrent_files, _ = _audit(torrents, media, file_map=file_map)
        [rec] = torrent_files
        assert rec['status'] == 'Seeding'
        assert _posix(rec['path']) == f'{LIVE}/Rel.mkv'
        assert rec['leftover']['path'] == f'{LEFTOVER}/Rel.mkv'

        report = _cleanup(torrent_files).get_json()
        g = _group(report, LEFTOVER)
        assert g['pile'] == 'leftover'
        assert g['excl_folder'] == LEFTOVER
        [row] = g['files']
        assert row['state'] == 'torrent_copy'
        assert row['paths'] == [f'{LEFTOVER}/Rel.mkv']
        assert isinstance(row['mtime'], int)
        assert report['freeable_size'] == 0
        assert report['leftover'] == {'count': 1, 'size': 500}
        assert report['keeps_copy'] == {'count': 1, 'size': 500}

    def test_the_live_path_can_never_be_selected(self, trees):
        torrents, media, file_map = _leftover_tree(trees)
        torrent_files, _ = _audit(torrents, media, file_map=file_map)
        resp = _script(torrent_files, [f'{LIVE}/Rel.mkv'])
        assert resp.status_code == 409
        assert resp.get_json()['code'] == 'nothing_left'

        resp = _script(torrent_files, [f'{LEFTOVER}/Rel.mkv', f'{LIVE}/Rel.mkv'])
        assert resp.status_code == 200
        text = _text(resp)
        assert f'{LEFTOVER}/Rel.mkv' in text
        assert 'Rel--aaaa' not in text
        assert 'a leftover link' in text
        assert resp.headers['X-Auditorr-Freeable'] == '0'

    @pytest.mark.parametrize('order', ['asc', 'desc'])
    def test_excluding_the_leftover_folder_leaves_the_live_file_alone(self, trees, order):
        """The rule the leftover pile offers. A record took its path, size and
        `excluded` from the first path walked, so where the leftover came first
        the rule hid the torrent's own file — C16's shape, from the other side."""
        torrents, media, file_map = _leftover_tree(trees)
        with _walk_order(order):
            torrent_files, _ = _audit(torrents, media, file_map=file_map,
                                      patterns=[f'literal:{LEFTOVER}/'])
        [rec] = torrent_files
        assert rec['excluded'] is False
        assert _posix(rec['path']) == f'{LIVE}/Rel.mkv'
        assert 'leftover' not in rec
        assert _all_rows(_cleanup(torrent_files).get_json()) == []

    def test_a_leftover_under_an_unresolved_root_cannot_be_selected(self, trees):
        """A torrent whose listing failed with nothing found may claim it."""
        torrents, media, file_map = _leftover_tree(trees)
        torrent_files, _ = _audit(torrents, media, file_map=file_map, unverified={
            'all': False, 'roots': [str(torrents / LEFTOVER)]})
        report = _cleanup(torrent_files).get_json()
        [row] = _all_rows(report)
        assert row['state'] == 'unverified'
        assert _group(report, LEFTOVER)['pile'] == 'unverified'
        assert _script(torrent_files, row['paths']).get_json()['code'] == 'unverified'

    def test_the_compact_row_and_the_badge_count_carry_leftovers(self, trees):
        torrents, media, file_map = _leftover_tree(trees)
        _write(torrents / 'movies' / 'Gone' / 'g.mkv')
        torrent_files, media_files = _audit(torrents, media, file_map=file_map)
        working = audit.cleanup_working_set(torrent_files)
        assert sorted(_posix(r['path']) for r in working) == \
            [f'{LEFTOVER}/Rel.mkv', 'movies/Gone/g.mkv']
        with patch.object(audit, 'db_load_history',
                          return_value={'hourly_stats': [], 'daily_stats': []}):
            det = audit.process_health_metrics(media_files, torrent_files, {},
                                               update_history=False)['current']['details']
        assert det['orphaned_torrent_count'] + det['leftover_count'] == len(working) == 2
        # Zero bytes freed, so nothing the score or Rounds reads moves.
        assert det['orphaned_torrent_size'] == 64
        assert _cleanup(working, compact=True).get_json()['file_count'] == 2

    def test_running_the_script_removes_the_leftover_and_nothing_else(self, trees):
        torrents, media, file_map = _leftover_tree(trees)
        torrent_files, _ = _audit(torrents, media, file_map=file_map)
        resp = _script(torrent_files, [f'{LEFTOVER}/Rel.mkv'], cfg=_local_cfg(torrents))
        code, out = _run(_text(resp), torrents)
        assert code == 0, out
        # Emptied, and its folder was stamped as one a rule may name, so pruned.
        assert not (torrents / LEFTOVER).exists(), out
        assert (torrents / LIVE / 'Rel.mkv').read_bytes() == b'v' * 500
        assert re.search(r'Hardlinked \(space not freed yet\):\s+1 file', out), out


# ═════════════════════════════════════════════════════════════════════════════
# C18 — a live torrent's libtorrent part file
# ═════════════════════════════════════════════════════════════════════════════

PART_HASH = 'ab' * 20
PART      = f'.{PART_HASH}.parts'


class TestPartFiles:

    def test_a_live_torrents_part_file_is_kept_out_of_every_workflow(self, trees):
        """Claimed by the source layer (`sources.part_file_claims`), so it is no
        orphan; excluded at the walk, so Triage, whose delete beside a row
        removes the torrent, never lists it as a file the arr did not import."""
        torrents, media = trees
        payload = _write(torrents / 'movies' / 'Rel' / 'Rel.mkv')
        part = _write(torrents / 'movies' / PART)
        claim = {'status': 'Seeding', 'trackers': set(), 'hash': PART_HASH,
                 'tracker_health': 'working'}
        torrent_files, _ = _audit(torrents, media,
                                  file_map={str(payload): dict(claim), str(part): dict(claim)})
        rec = next(r for r in torrent_files if PART in r['path'])
        assert rec['status'] == 'Seeding' and rec['excluded'] is True
        assert not audit._is_triage_relevant(rec)
        assert _all_rows(_cleanup(torrent_files).get_json()) == []

    def test_a_part_file_whose_torrent_is_gone_is_an_ordinary_orphan(self, trees):
        torrents, media = trees
        _write(torrents / 'movies' / PART)
        torrent_files, _ = _audit(torrents, media)
        [row] = _all_rows(_cleanup(torrent_files).get_json())
        assert row['path'] == f'movies/{PART}'
        assert row['state'] == 'last_copy'

    def test_the_reverify_drops_a_part_file_whose_torrent_is_back(self):
        """A finished torrent's `content_path` is its own folder, so the candidate
        rules written for a payload never reached the file beside it."""
        records = [_orphan(f'movies/{PART}'), _orphan('movies/Other/o.mkv')]
        resp = _script(records, [f'movies/{PART}', 'movies/Other/o.mkv'],
                       rows=[_row(PART_HASH, 'Rel')],
                       listing={PART_HASH: [f'{REMOTE}/movies/Rel/Rel.mkv']})
        assert resp.status_code == 200
        assert PART not in _text(resp)
        assert resp.headers['X-Auditorr-Dropped'] == '1'
        resp.fetch.assert_called_once()


# ── C25 — one name, two Unicode spellings ─────────────────────────────────────
#
# `é` is one code point in NFC and `e` plus a combining accent in NFD. A Mac
# writes NFD over SMB, so the disk and the client can spell one file two ways,
# and a raw string comparison read the file as an orphan and the torrent as
# missing it. Both spellings are real file names on NTFS and ext4 alike, which
# is the case that matters: there, only the disk's spelling can be opened.

_NFC = unicodedata.normalize('NFC', 'Amélie (2001)')
_NFD = unicodedata.normalize('NFD', _NFC)
assert _NFC != _NFD


class TestUnicodeSpellings:

    @pytest.mark.parametrize('disk, client', [(_NFD, _NFC), (_NFC, _NFD)],
                             ids=['disk NFD, client NFC', 'disk NFC, client NFD'])
    def test_a_torrents_file_spelled_the_other_way_is_still_its_own(self, trees, disk, client):
        torrents, media = trees
        _write(torrents / 'movies' / disk / f'{disk}.mkv')
        key = os.path.join(str(torrents), 'movies', client, f'{client}.mkv')
        file_map = {key: {'status': 'Seeding', 'trackers': set(), 'hash': 'aaa',
                          'tracker_health': 'working'}}
        torrent_files, _ = _audit(torrents, media, file_map=file_map)
        assert [r['status'] for r in torrent_files] == ['Seeding']
        # The record keeps the disk's spelling: that's the one that can be opened.
        assert _posix(torrent_files[0]['path']) == f'movies/{disk}/{disk}.mkv'
        assert not _all_rows(_cleanup(torrent_files).get_json())

    def test_an_ascii_miss_is_still_an_orphan(self, trees):
        """The fallback is for names that differ only in spelling, nothing else."""
        torrents, media = trees
        _write(torrents / 'movies' / 'Amelie (2001)' / 'Amelie (2001).mkv')
        key = os.path.join(str(torrents), 'movies', _NFC, f'{_NFC}.mkv')
        torrent_files, _ = _audit(torrents, media, file_map={
            key: {'status': 'Seeding', 'trackers': set(), 'hash': 'aaa',
                  'tracker_health': 'working'}})
        assert [r['status'] for r in torrent_files] == ['Orphaned']

    def test_an_orphan_under_a_failed_listing_is_unverified_in_either_spelling(self, trees):
        """The unsafe direction: a plain comparison marked nothing unverified."""
        torrents, media = trees
        _write(torrents / _NFD / 'stray.mkv')
        torrent_files, _ = _audit(torrents, media, unverified={
            'all': False, 'roots': [str(torrents / _NFC)]})
        assert torrent_files[0].get('unverified') is True

    def test_the_live_check_finds_a_claim_spelled_the_other_way(self):
        """Also the unsafe direction: the torrent was no candidate, so its claim
        on the selected file was never checked and the file went in the script."""
        rel = f'movies/{_NFD}/{_NFD}.mkv'
        records = [_orphan(rel), _orphan('movies/Other/o.mkv')]
        resp = _script(records, [rel, 'movies/Other/o.mkv'], rows=[_row('aaa', _NFC)],
                       listing={'aaa': [f'{REMOTE}/movies/{_NFC}/{_NFC}.mkv']})
        assert resp.status_code == 200
        assert resp.headers['X-Auditorr-Dropped'] == '1'
        assert 'o.mkv' in _text(resp)

    def test_an_ascii_landmark_is_preferred(self):
        """A landmark is the client's spelling, tested on disk with `[ -e ]`."""
        rows = [_row('a', _NFC, save_path=REMOTE), _row('b', 'Zulu.2020', save_path=REMOTE)]
        assert app._cleanup_landmarks(rows, REMOTE, REMOTE) == ['Zulu.2020', _NFC]


# ── C26 — folders whose file list didn't load ─────────────────────────────────

class TestUncheckedFolders:
    """A failed listing claims its torrent's folder whole, which is the safe
    direction, and hides a stray file in it. The page says which folders."""

    def _get(self, rows, details):
        with patch.object(app, 'db_load_config', return_value={'LOCAL_PATH': ''}), \
             patch.object(app, 'db_load_results', return_value=_details(**details)), \
             patch.object(app, 'db_has_file_results', side_effect=lambda tab, conn=None: tab in rows), \
             patch.object(app, 'db_load_file_results',
                          side_effect=lambda tab, conn=None: list(rows[tab])):
            return app.app.test_client().get('/api/workflows/cleanup').get_json()

    def test_the_page_names_the_folders_it_could_not_look_inside(self):
        report = self._get({'cleanup': [_orphan('movies/A/a.mkv')],
                            'cleanup_folders': [{'kind': 'unchecked', 'path': 'movies/B'}]},
                           {'cleanup_unchecked_count': 3})
        # The count is the whole number; the list may be capped.
        assert report['unchecked_folders'] == {'count': 3, 'folders': ['movies/B']}

    def test_with_no_orphans_at_all_it_still_says_so(self):
        """The empty page used to say every file was checked."""
        report = self._get({'cleanup': [],
                            'cleanup_folders': [{'kind': 'unchecked', 'path': 'movies/B'}]},
                           {'cleanup_unchecked_count': 1})
        assert report['file_count'] == 0
        assert report['unchecked_folders']['count'] == 1

    def test_a_scan_from_before_the_row_says_nothing(self):
        assert _cleanup([_orphan('movies/A/a.mkv')]).get_json()['unchecked_folders'] == \
            {'count': 0, 'folders': []}

    def test_only_folders_under_the_torrent_root_are_named(self):
        roots = ['/data/torrents/movies/B', '/data/torrents/movies/B/', '/data/torrents/A',
                 '/elsewhere/C', '/data/torrents', '/data/torrentsX/D']
        assert audit.cleanup_unchecked_folders(roots, '/data/torrents') == ['A', 'movies/B']
        assert audit.cleanup_unchecked_folders(roots, '') == []


# ── C27 — empty folders ───────────────────────────────────────────────────────
#
# qui's Orphan Scan rules, adopted: a folder with no file at any depth, never
# the torrent root, a folder the client saves into or one above it, never one
# an exclusion covers or one holding anything rmdir would refuse, and never one
# changed in the last ten minutes.

def _walk_empty(base, protected=(), patterns=(), label='Torrent'):
    expanded = expand_exclusion_patterns({'EXCLUSION_PATTERNS': list(patterns)})
    out = []
    _walk_directory(str(base), label, {}, {}, 0, 0, exclusion_patterns=expanded,
                    compiled_exclusions=compile_exclusions(expanded), total_ref=[0],
                    empty_dirs=out,
                    protected_dirs=audit.protected_dirs(protected, str(base)))
    return sorted(_posix(os.path.relpath(p, str(base))) for p, _ in out)


def _mkdirs(base, *rels, old=True):
    for rel in rels:
        (base / rel).mkdir(parents=True, exist_ok=True)
    if old:
        _age(base, *rels)


def _age(base, *rels):
    then = time.time() - 3600
    for rel in rels:
        parts = rel.split('/')
        for i in range(len(parts), 0, -1):
            os.utime(base.joinpath(*parts[:i]), (then, then))


class TestEmptyFolders:

    def test_an_empty_tree_is_offered_once_at_its_top(self, tmp_path):
        _mkdirs(tmp_path, 'Gone.Release/Sub/Deeper', 'Other.Gone')
        assert _walk_empty(tmp_path) == ['Gone.Release', 'Other.Gone']

    def test_a_folder_holding_anything_is_not_empty(self, tmp_path):
        """Anything rmdir would refuse: a file at any depth, an excluded folder
        (a Synology `@eaDir`, C24) and a folder the walk couldn't read."""
        _write(tmp_path / 'Has.File' / 'deep' / 'er' / 'note.txt')
        _mkdirs(tmp_path, 'Has.Clutter/@eaDir', 'Has.Unreadable/locked', 'Has.File/empty.kid')
        real = os.scandir
        locked = os.path.normcase(str(tmp_path / 'Has.Unreadable' / 'locked'))

        def scandir(path='.'):
            if os.path.normcase(os.fspath(path)) == locked:
                raise PermissionError(13, 'Permission denied', os.fspath(path))
            return real(path)
        with patch('os.scandir', scandir):
            found = _walk_empty(tmp_path)
        # The file's folder isn't empty, but an empty folder beside the file is.
        assert found == ['Has.File/empty.kid']

    def test_a_folder_the_client_saves_into_is_never_offered(self, tmp_path):
        """A category folder qBittorrent saves into again, and every folder
        above it, but an abandoned release inside one still is."""
        _mkdirs(tmp_path, 'books/Old.Release', 'cats/tv/hd', 'Gone')
        found = _walk_empty(tmp_path, protected=[str(tmp_path / 'books'),
                                                 str(tmp_path / 'cats' / 'tv' / 'hd')])
        assert found == ['Gone', 'books/Old.Release']

    def test_a_folder_changed_in_the_last_ten_minutes_is_left_for_now(self, tmp_path):
        _mkdirs(tmp_path, 'Just.Made', old=False)
        _mkdirs(tmp_path, 'Long.Gone')
        assert _walk_empty(tmp_path) == ['Long.Gone']

    def test_the_media_tree_is_never_looked_at(self, tmp_path):
        _mkdirs(tmp_path, 'Movies/Empty')
        assert _walk_empty(tmp_path, label='Media') == []


class TestCategoryFolders:
    """qBittorrent's own rule (`SessionImpl::categorySavePath`), written for
    auditorr: qui's is GPL and auditorr is MIT, so none of it is copied."""

    PREFS = {'save_path': '/data/torrents', 'use_subcategories': False}

    def test_each_kind_of_category_folder(self):
        cats = {'movies': {'name': 'movies', 'savePath': ''},
                'tv': {'name': 'tv', 'savePath': '/srv/tv'},
                'music': {'name': 'music', 'savePath': 'audio/music'},
                'what:hd?': {'name': 'what:hd?', 'savePath': ''}}
        assert sorted(sources.client_save_dirs(self.PREFS, cats)) == sorted([
            '/data/torrents/movies', '/srv/tv', '/data/torrents/audio/music',
            '/data/torrents/what hd ', '/data/torrents'])

    def test_a_subcategory_nests_under_its_parent_only_with_subcategories_on(self):
        cats = {'tv': {'savePath': '/srv/tv'}, 'tv/anime': {'savePath': ''}}
        on = sources.client_save_dirs({**self.PREFS, 'use_subcategories': True}, cats)
        off = sources.client_save_dirs(self.PREFS, cats)
        assert '/srv/tv/anime' in on
        assert '/data/torrents/tv/anime' in off

    def test_the_incomplete_folder_only_when_it_is_on(self):
        prefs = {**self.PREFS, 'temp_path': '/data/torrents/incomplete'}
        assert '/data/torrents/incomplete' not in sources.client_save_dirs(prefs, {})
        assert '/data/torrents/incomplete' in sources.client_save_dirs(
            {**prefs, 'temp_path_enabled': True}, {})

    def test_an_answer_that_is_not_a_dict_is_could_not_ask(self):
        assert sources.client_save_dirs(self.PREFS, []) is None
        assert sources.client_save_dirs(None, {}) is None


class TestEmptyFolderScript:

    def test_the_page_lists_them_apart_from_the_files(self):
        report = TestUncheckedFolders()._get(
            {'cleanup': [_orphan('movies/A/a.mkv')],
             'cleanup_folders': [{'kind': 'empty', 'path': 'Old.Release', 'mtime': 5}]},
            {'empty_folder_count': 1, 'empty_folders_unknown': False})
        assert report['empty_folders'] == {
            'count': 1, 'unknown': False, 'folders': [{'path': 'Old.Release', 'mtime': 5}]}
        assert report['file_count'] == 1          # folders aren't files: not in the badge

    def test_a_folders_only_selection_builds_a_script(self):
        resp = _script([], [], folders=['Old.Release'], empty=['Old.Release'])
        assert resp.status_code == 200
        assert 'remove_empty_folder ./Old.Release' in _text(resp)
        assert (resp.headers['X-Auditorr-Files'], resp.headers['X-Auditorr-Folders']) == ('0', '1')
        resp.fetch.assert_not_called()           # the listing is all a folder needs

    def test_a_folder_the_last_scan_did_not_list_is_refused(self):
        resp = _script([], [], folders=['Some.Live.Release'], empty=['Old.Release'])
        assert resp.status_code == 409 and resp.get_json()['code'] == 'nothing_left'

    def test_a_folder_a_torrent_saves_into_now_is_dropped(self):
        """A torrent added since the scan, or one missing its files."""
        rows = [_row('aaa', 'Old.Release', save_path=REMOTE)]
        resp = _script([], [], folders=['Old.Release', 'Gone'], empty=['Old.Release', 'Gone'],
                       rows=rows)
        assert resp.status_code == 200
        assert resp.headers['X-Auditorr-Folders-Dropped'] == '1'
        assert 'Old.Release' not in _text(resp).split('# ── Empty folders')[1]
        resp = _script([], [], folders=['Old.Release'], empty=['Old.Release'], rows=rows)
        assert resp.status_code == 409

    def test_no_selection_at_all_is_still_refused(self):
        assert _script([], [], folders=[]).status_code == 400

    def test_the_script_removes_only_what_is_still_empty(self, tmp_path):
        torrents = tmp_path / 'torrents'
        _write(torrents / 'Live.Release' / 'a.mkv')
        _mkdirs(torrents, 'Old.Release/Sub/Deeper', 'Refilled')
        text = _text(_script([], [], folders=['Old.Release', 'Refilled'],
                             empty=['Old.Release', 'Refilled'], cfg=_local_cfg(torrents),
                             rows=[_row('lll', 'Live.Release', save_path=REMOTE)]))
        _write(torrents / 'Refilled' / 'new.mkv')    # a download landed since the scan

        code, out = _run(text, torrents, '--dry-run')
        assert code == 0 and (torrents / 'Old.Release' / 'Sub' / 'Deeper').is_dir()
        assert 'Would remove empty folder: Old.Release' in out

        code, out = _run(text, torrents)
        assert code == 0, out
        assert not (torrents / 'Old.Release').exists()
        assert (torrents / 'Refilled' / 'new.mkv').exists()
        assert 'No longer empty, left alone: Refilled' in out
        assert (torrents / 'Live.Release' / 'a.mkv').exists()

        code, out = _run(text, torrents)                 # a finished script runs again
        assert code == 0 and 'Already gone: Old.Release' in out
