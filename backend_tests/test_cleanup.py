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
from unittest.mock import MagicMock, patch

import pytest

import app
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


def _loader(records, compact=True, loaded=None):
    """`db_load_file_results` / `db_has_file_results` over one stored list.

    With `compact`, only the `cleanup` row exists and loading the full
    `torrents` row is an error — C10's contract is that neither the page nor the
    script path deserializes it once the compact row exists.
    """
    loaded = [] if loaded is None else loaded

    def load(tab, conn=None):
        loaded.append(tab)
        if compact and tab == 'torrents':
            raise AssertionError('the full torrents row was deserialized')
        return list(records)

    def has(tab, conn=None):
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
            details=None, list_error=None, report=None, method='post'):
    """POST a selection to the delete-script endpoint with a faked live client.

    Reads the full `torrents` row by default, which is the fallback path and
    goes through the same code; `test_cleanup_reads_the_compact_row` is the one
    that asserts the compact row is what gets read. Defaulting to compact would
    make every finding below fail on C10 rather than on its own finding.
    """
    cfg = {'LOCAL_PATH': REMOTE, 'REMOTE_PATH': REMOTE, **(cfg or {})}
    load, has, loaded = _loader(records, compact)
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
            resp = client.post(url, json={'paths': list(paths)} if paths is not None else {})
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
    torrent_files, media_files = _assemble_records(tko, mko, inode_map, {}, **kw)
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
    proc = subprocess.run([_bash(), str(script), *args], cwd=str(cwd),
                          capture_output=True, env=env, timeout=120)
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
        for key in ('mtime', 'nlink', 'other_paths', 'unverified', 'excl_refused'):
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
        os.link(live, torrents / 'tv-b' / 'Show' / 'e.mkv')
        _write(torrents / 'tv-b' / 'Show' / 'stray.nfo')
        file_map = {str(live): {'status': 'Seeding', 'trackers': set(), 'hash': 'aaa',
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
