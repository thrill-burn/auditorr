"""The Dedupe page reads a compact row, and its badge counts what the page lists (Phase 14).

Two items from ROADMAP Phase 14, and they are coupled:

* **R6 — a compact `dedupe` `file_results` row.** `/api/workflows/dedupe` and
  the dedupe script endpoint each deserialized **both** full file lists — the
  RAM hotspot C10 closed for Cleanup and v1.7.0 closed for Triage — and then
  kept only what `scripts.dup_group_inputs` keeps: records that are excluded or
  carry duplicate partners. The predicate existed; persisting it did not.
* **The sidebar badge** counted duplicate *files* (`duplicate_count`) beside a
  page of *groups*. `duplicate_count` feeds the health score, the change log,
  Singleton and Clone Hunter, so it cannot change; the audit stamps a group
  count instead, built by the page's own grouping over the row's inputs.

**Every test drives a real scan into a real SQLite file** and reads the
endpoints back, as `test_publish_atomicity.py` does. Mocking
`db_load_file_results` to show the page reads one row would prove nothing.

Characterisation (passes before and after): the figures with five consumers do
not move; the page and the script are identical built from either source; a
database whose last scan predates the row still serves both.
Findings (failed first, each for its own reason): the page and the script load
a full list; the badge's number is not the page's; the config-save recompute
drops the details only a scan can measure.
"""
import json
import os
import sqlite3
from contextlib import ExitStack
from unittest.mock import patch

import app
import audit
import db
import scripts
import sources
from backend_tests.test_publish_atomicity import real_db, stored


# ---------------------------------------------------------------------------
# A library with duplicates, scanned for real
# ---------------------------------------------------------------------------

def _bytes(n, seed):
    block = bytes((i * 7 + seed) % 256 for i in range(256))
    return (block * (n // 256 + 1))[:n]


def _cfg(local, media, **over):
    return {
        'TORRENT_SOURCE': 'qui', 'QUI_HOST': 'http://qui:7476', 'QUI_API_KEY': 'k',
        'LOCAL_PATH': local, 'MEDIA_PATH': media, 'REMOTE_PATH': local,
        'OR_RATIO': 0.01, 'NI_RATIO': 0.01, 'DUP_RATIO': 0.01,
        'EXCLUSION_PATTERNS': [], **over,
    }


# (tree, relative path, content seed, size). Two groups in the torrent tree, one
# in the library, one cross-tree pair, a lone file and an excluded copy.
_FILES = [
    ('torrents', 'movies/Rel.A/Film.mkv',        1, 4096),
    ('torrents', 'movies/Rel.B/Film.mkv',        1, 4096),
    ('torrents', 'movies/Rel.C/Film.mkv',        1, 4096),
    ('torrents', 'tv/Show.S01/Show.S01E01.mkv',  2, 3000),
    ('torrents', 'tv/Show.S01.x/Show.S01E01.mkv', 2, 3000),
    ('torrents', 'movies/Lone/Lone.mkv',         3, 5000),
    ('torrents', 'movies/Pair/Pair.mkv',         4, 2500),
    ('media',    'Movies/Pair (2020)/Pair.mkv',  4, 2500),
    ('media',    'Movies/Dup (2019)/Dup.mkv',    5, 2200),
    ('media',    'Movies/Dup (2019) copy/Dup.mkv', 5, 2200),
    ('media',    'Movies/Dup (2019) extra/Dup.mkv', 5, 2200),
]


def _library(tmp_path):
    local, media = tmp_path / 'torrents', tmp_path / 'media'
    for tree, rel, seed, size in _FILES:
        p = (local if tree == 'torrents' else media) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_bytes(size, seed))
    return str(local), str(media)


def _answer(local, seed_byte_secs=0):
    """Every torrent-tree file claimed by one live torrent each."""
    report = sources.new_source_report('qui')
    report['instances_total'] = report['instances_ok'] = 1
    file_map = {}
    for i, (tree, rel, _seed, _size) in enumerate(_FILES):
        if tree == 'torrents':
            file_map[os.path.join(local, *rel.split('/'))] = {
                'status': 'Seeding', 'trackers': {'t.example'}, 'hash': f'h{i:02d}',
                'instance_id': 1, 'tracker_health': 'working', 'tracker_msg': ''}
    report['torrent_count'] = report['file_map_size'] = len(file_map)
    snapshot = {'_instance_count': 1, '_seed_byte_secs': seed_byte_secs, '_max_seed_secs': 86400}
    return file_map, ['t.example'], snapshot, report


def _scan(local, media, cfg=None, **answer_kw):
    with patch('audit.db_load_config', return_value=cfg or _cfg(local, media)), \
         patch('audit.sources.fetch_file_map', return_value=_answer(local, **answer_kw)):
        audit.run_audit_process('manual')


class _Reads:
    """Which `file_results` tabs an endpoint asked for."""

    def __init__(self):
        self.tabs = []
        self._real = db.db_load_file_results

    def __call__(self, tab, conn=None, **kw):
        self.tabs.append(tab)
        return self._real(tab, conn=conn, **kw)


def _client(tmp_path, cfg, stack, reads=None):
    stack.enter_context(patch.object(app, 'db_load_config', return_value=dict(cfg)))
    stack.enter_context(patch.object(app, 'AUDITORR_SECRET', ''))
    stack.enter_context(patch.object(app, 'AUDITORR_REQUIRE_AUTH', False))
    stack.enter_context(patch.object(scripts, 'MOUNTINFO_PATH', str(tmp_path / 'no-mountinfo')))
    if reads is not None:
        stack.enter_context(patch.object(app, 'db_load_file_results', reads))
    return app.app.test_client()


def _page(tmp_path, cfg, reads=None):
    with ExitStack() as stack:
        resp = _client(tmp_path, cfg, stack, reads).get('/api/workflows/dedupe')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def _script(tmp_path, cfg, groups, reads=None):
    with ExitStack() as stack:
        stack.enter_context(patch('app.time.time', return_value=1_700_000_000))
        return _client(tmp_path, cfg, stack, reads).post(
            '/api/actions/script/dedupe', json={'groups': groups})


def _drop_row(path, tab):
    """What a database written before this row existed looks like."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute('DELETE FROM file_results WHERE tab = ?', (tab,))
        conn.execute('DELETE FROM app_meta WHERE key = ?', (f'file_results_{tab}_stats',))
        conn.commit()
    finally:
        conn.close()


def _details(path):
    return stored(path)['results']['dashboard']['current']['details']


# ═════════════════════════════════════════════════════════════════════════════
# Characterisation — true before Phase 14 and after it
# ═════════════════════════════════════════════════════════════════════════════

def test_the_figures_with_five_consumers_do_not_move(tmp_path):
    """`duplicate_count` feeds the health score, the change log, Singleton and
    Clone Hunter. Pinned here the way Phase 12 pinned `score == 33.3`: the
    values this library produced before Phase 14, read off a real scan."""
    with real_db(tmp_path) as path:
        local, media = _library(tmp_path)
        _scan(local, media)
        os.remove(os.path.join(media, 'Movies', 'Dup (2019) extra', 'Dup.mkv'))
        _scan(local, media)
        out = stored(path)
        conn = sqlite3.connect(str(path))
        try:
            change = conn.execute('SELECT health_score, diff_json FROM change_log').fetchone()
        finally:
            conn.close()

    det = out['results']['dashboard']['current']['details']
    assert det['duplicate_count'] == 9
    assert det['duplicate_size'] == 3 * 4096 + 2 * 3000 + 2 * 2500 + 2 * 2200
    assert out['results']['dashboard']['score'] == 10.0
    assert change[0] == 10.0
    diff = json.loads(change[1])
    assert {k: len(v) for k, v in diff.items() if isinstance(v, list)} == {
        'newly_orphaned': 0, 'newly_imported': 0, 'new_duplicates': 0,
        'resolved_duplicates': 0, 'new_files': 0, 'removed_files': 1}
    assert diff['score_delta'] == 0.0


def test_the_page_and_the_script_are_the_same_from_either_source(tmp_path):
    """Parity: built from the compact row, or from both full rows as every
    database before Phase 14 serves them, the page payload and the generated
    script's bytes are identical."""
    with real_db(tmp_path) as path:
        local, media = _library(tmp_path)
        cfg = _cfg(local, media)
        _scan(local, media, cfg)
        page_row = _page(tmp_path, cfg)
        ids = [g['id'] for g in page_row['groups']]
        script_row = _script(tmp_path, cfg, ids)

        _drop_row(path, 'dedupe')
        page_full = _page(tmp_path, cfg)
        script_full = _script(tmp_path, cfg, ids)

    assert len(page_row['groups']) == 4
    assert page_row == page_full
    assert script_row.status_code == script_full.status_code == 200
    assert script_row.get_data() == script_full.get_data()
    assert dict(script_row.headers)['X-Auditorr-Groups'] == dict(script_full.headers)['X-Auditorr-Groups']


def test_a_database_whose_last_scan_predates_the_row_still_serves_both(tmp_path):
    """No stored-data migration (C10's pattern): the full rows answer until the
    first scan after upgrade writes the compact one."""
    with real_db(tmp_path) as path:
        local, media = _library(tmp_path)
        cfg = _cfg(local, media)
        _scan(local, media, cfg)
        _drop_row(path, 'dedupe')

        reads = _Reads()
        page = _page(tmp_path, cfg, reads)
        script = _script(tmp_path, cfg, [g['id'] for g in page['groups']])

    assert len(page['groups']) == 4
    assert set(reads.tabs) >= {'media', 'torrents'}
    assert script.status_code == 200


def test_the_current_exclusion_rules_still_apply_to_a_path_with_no_record(tmp_path):
    """`_build_dup_groups` matches a path with no record of its own against the
    *current* rules. A rule added after the scan hides the path on the page
    whichever source the page was built from."""
    with real_db(tmp_path):
        local, media = _library(tmp_path)
        _scan(local, media)
        later = _cfg(local, media, EXCLUSION_PATTERNS=['literal:movies/Rel.C/Film.mkv'])
        page = _page(tmp_path, later)

    paths = [p['path'] for g in page['groups'] for m in g['members'] for p in m['paths']]
    assert not any(p.endswith('Rel.C/Film.mkv') for p in paths)


# ═════════════════════════════════════════════════════════════════════════════
# R6 — the findings
# ═════════════════════════════════════════════════════════════════════════════

def test_the_dedupe_page_loads_no_full_file_list(tmp_path):
    with real_db(tmp_path):
        local, media = _library(tmp_path)
        cfg = _cfg(local, media)
        _scan(local, media, cfg)
        reads = _Reads()
        page = _page(tmp_path, cfg, reads)

    assert page['groups'], 'fixture: the page lists groups'
    assert 'media' not in reads.tabs and 'torrents' not in reads.tabs, \
        f"the Dedupe page deserialized a full file list: {reads.tabs}"


def test_the_dedupe_script_loads_no_full_file_list(tmp_path):
    with real_db(tmp_path):
        local, media = _library(tmp_path)
        cfg = _cfg(local, media)
        _scan(local, media, cfg)
        ids = [g['id'] for g in _page(tmp_path, cfg)['groups']]
        reads = _Reads()
        resp = _script(tmp_path, cfg, ids, reads)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert 'media' not in reads.tabs and 'torrents' not in reads.tabs, \
        f"the dedupe script deserialized a full file list: {reads.tabs}"


def test_the_compact_row_holds_only_what_grouping_reads(tmp_path):
    """The row is `dup_group_inputs`' predicate, persisted: excluded records and
    records with duplicate partners, from both trees, told apart by tree."""
    with real_db(tmp_path) as path:
        local, media = _library(tmp_path)
        _scan(local, media, _cfg(local, media, EXCLUSION_PATTERNS=['literal:movies/Lone/Lone.mkv']))
        conn = db._db_conn()
        try:
            row = db.db_load_file_results('dedupe', conn=conn)
            stats = db.db_get_meta('file_results_dedupe_stats')
        finally:
            conn.close()
        full = stored(path)

    assert set(row) == {'torrents', 'media'}
    kept = row['torrents'] + row['media']
    assert all(r.get('excluded') or r.get('duplicate_paths') for r in kept)
    assert sorted(r['path'].replace('\\', '/') for r in row['torrents']) == sorted(
        r['path'].replace('\\', '/') for r in full['torrents']
        if r.get('excluded') or r.get('duplicate_paths'))
    assert stats['count'] == len(kept)


# ═════════════════════════════════════════════════════════════════════════════
# The badge
# ═════════════════════════════════════════════════════════════════════════════

def test_the_dedupe_badge_counts_the_groups_the_page_lists(tmp_path):
    """The sidebar read `duplicate_count` — 10 files here — beside a page of 4
    groups. The stamp is the page's own grouping over the scan's inputs; stale
    groups are listed on the page, so they count too."""
    with real_db(tmp_path) as path:
        local, media = _library(tmp_path)
        cfg = _cfg(local, media)
        _scan(local, media, cfg)
        # One copy gone since the scan: its group is listed as `stale`.
        os.remove(os.path.join(local, 'tv', 'Show.S01.x', 'Show.S01E01.mkv'))
        page = _page(tmp_path, cfg)
        det = _details(path)

    assert [g['status'] for g in page['groups']].count('stale') == 1
    assert det.get('dedupe_group_count') == len(page['groups']) == 4
    assert det['duplicate_count'] == 10, 'the file count a stale bundle reads is untouched'


def test_a_config_save_keeps_the_details_only_a_scan_can_measure(tmp_path):
    """Found writing Phase 14, in no list. The config-save recompute rebuilt the
    details from the stored lists and dropped what only the scan measures —
    seeding time from the client and the oldest media file from the walk — so
    Rounds' Atlas, Old Faithful and Provenance tiles read 0 until the next scan.
    The badge's group count would have gone the same way."""
    with real_db(tmp_path) as path:
        local, media = _library(tmp_path)
        cfg = _cfg(local, media)
        _scan(local, media, cfg, seed_byte_secs=123_456)
        before = _details(path)
        with ExitStack() as stack:
            client = _client(tmp_path, cfg, stack)
            stack.enter_context(patch.object(app, 'restart_watchdog', lambda: None))
            resp = client.post('/api/config', json=dict(cfg, OR_RATIO=0.02))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        after = _details(path)

    assert after['or_limit'] != before['or_limit'], 'fixture: the recompute ran'
    for key in ('seed_byte_secs', 'max_seed_secs', 'oldest_media_age_days', 'dedupe_group_count'):
        assert after.get(key) == before.get(key), key
    assert before['seed_byte_secs'] == 123_456
