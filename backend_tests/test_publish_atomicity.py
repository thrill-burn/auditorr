"""One generation, published and read as one (S04).

From the 2026-09-10 outside review: an audit published in sixteen independently
committed pieces, so a reader could combine two scans and a publish interrupted
part-way persisted the mix. The Phase 13 brief's probe reproduced both against
`132849c` — media generation 2 against torrents generation 1, one file reading
as unseeded (Backfill offers a grab) and one as not imported (Triage offers a
delete); and an interrupted publish that left media generation 3 against
torrents generation 2 with generation-2 signatures, plus an upload snapshot and
a change-log entry for a scan whose inventory never landed.

**Every test here uses a real SQLite file in `tmp_path` and reads it back.**
Mocking `db_save_file_results` to prove a publish is atomic proves nothing, so
the publish tests drive `run_audit_process` end to end over real directories and
then open the database. The reader tests drive the real Flask endpoints against
a writer thread committing two rows separately — which is exactly what the audit
used to do, and what a reader must survive either way.

Two kinds of test, kept apart:

* **Characterisation** — a healthy scan persists the same figures it did before
  Phase 13, and a scan never deserializes the previous generation.
* **Findings** — the mixed read, the interrupted publish, the differenced series
  that outlived it, and the credit that must not be lost to a lock.
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

import app
import audit
import db
import rounds
import scripts
import sources


# ---------------------------------------------------------------------------
# A real database, real roots, a real scan
# ---------------------------------------------------------------------------

@contextmanager
def real_db(tmp_path):
    """Point every `db` function at a fresh SQLite file under `tmp_path`."""
    path = tmp_path / 'auditorr.db'
    with patch.object(db, 'DATA_DIR', str(tmp_path)), \
         patch.object(db, 'DB_FILE', str(path)):
        db.init_db()
        yield path


def _cfg(local, media):
    return {
        'TORRENT_SOURCE': 'qui', 'QUI_HOST': 'http://qui:7476', 'QUI_API_KEY': 'k',
        'LOCAL_PATH': local, 'MEDIA_PATH': media, 'REMOTE_PATH': local,
        'OR_RATIO': 0.01, 'NI_RATIO': 0.01, 'DUP_RATIO': 0.01,
        'EXCLUSION_PATTERNS': [],
    }


def _roots(tmp_path, torrents, media=0):
    """Two real roots holding `torrents` / `media` files in one release folder."""
    local, lib = tmp_path / 'torrents', tmp_path / 'media'
    for root, n, sub in ((local, torrents, 'movies'), (lib, media, 'Movies')):
        folder = root / sub / 'Rel'
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (folder / f'f{i}.mkv').write_bytes(b'x' * (10 + i))
    return str(local), str(lib)


def _answer(local, claimed):
    """A `fetch_file_map` answer claiming `claimed` files under the torrent root."""
    report = sources.new_source_report('qui')
    report['instances_total'] = 1
    report['instances_ok']    = 1
    rel = os.path.join(local, 'movies', 'Rel')
    file_map = {
        os.path.join(rel, f'f{i}.mkv'): {
            'status': 'Seeding', 'trackers': {'t.example'}, 'hash': f'h{i}',
            'instance_id': 1, 'tracker_health': 'working', 'tracker_msg': ''}
        for i in range(claimed)
    }
    report['torrent_count'] = claimed
    report['file_map_size'] = len(file_map)
    return file_map, ['t.example'], {'_instance_count': 1}, report


def run_scan(tmp_path, *, torrents, media=0, claimed=None, trigger='manual', extra=()):
    """Drive one whole audit against a real database and real directories."""
    local, lib = _roots(tmp_path, torrents, media)
    claimed = torrents if claimed is None else claimed
    with ExitStack() as stack:
        stack.enter_context(patch('audit.db_load_config', return_value=_cfg(local, lib)))
        stack.enter_context(patch('audit.sources.fetch_file_map',
                                  return_value=_answer(local, claimed)))
        for ctx in extra:
            stack.enter_context(ctx)
        audit.run_audit_process(trigger)
    return local, lib


# ---------------------------------------------------------------------------
# Reading the file back
# ---------------------------------------------------------------------------

def stored(path):
    """Everything a publish writes, read straight off the file."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        def meta(key):
            row = conn.execute('SELECT value FROM app_meta WHERE key = ?', (key,)).fetchone()
            return json.loads(row['value']) if row else None

        def blob(tab):
            row = conn.execute('SELECT files_json FROM file_results WHERE tab = ?',
                               (tab,)).fetchone()
            if not row:
                return None
            import zlib
            data = row['files_json']
            return json.loads(zlib.decompress(data).decode()
                              if isinstance(data, (bytes, bytearray)) else data)

        results = conn.execute('SELECT results_json FROM latest_results WHERE id = 1').fetchone()
        return {
            'media':      blob('media'),
            'torrents':   blob('torrents'),
            'triage':     blob('triage'),
            'cleanup':    blob('cleanup'),
            'media_sigs': blob('media_sigs'),
            'torrents_sigs': blob('torrents_sigs'),
            'results':    json.loads(results['results_json']) if results else None,
            'generation': meta('audit_generation'),
            'media_stats':    meta('file_results_media_stats'),
            'torrents_stats': meta('file_results_torrents_stats'),
            'ns_progress': meta('ns_progress'),
            'baseline':    meta('source_baseline'),
            'scan_marker': meta('scan_marker'),
            'aborted':     meta('consecutive_aborted_scans'),
            'snapshots':   conn.execute('SELECT COUNT(*) c FROM upload_snapshots').fetchone()['c'],
            'changes':     conn.execute('SELECT COUNT(*) c FROM change_log').fetchone()['c'],
            'runs':        [dict(r) for r in conn.execute(
                'SELECT ran_at, status, error_message FROM audit_runs ORDER BY id').fetchall()],
            'history':     conn.execute('SELECT COUNT(*) c FROM history').fetchone()['c'],
        }
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Characterisation — true before Phase 13 and after it
# ═════════════════════════════════════════════════════════════════════════════

def test_a_healthy_scan_persists_every_piece(tmp_path):
    """The publish still stores what it always stored, on a real file."""
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=3, claimed=2)
        out = stored(path)

    assert len(out['torrents']) == 3
    assert out['media'] == []
    assert out['results']['dashboard']['score'] is not None
    assert out['media_sigs'] == {}
    assert len(out['torrents_sigs']) == 3
    assert out['snapshots'] == 1
    assert [r['status'] for r in out['runs']] == ['ok']
    assert out['scan_marker'] is None
    assert out['aborted'] == 0
    assert out['baseline']['torrent_count'] == 2
    assert out['history'] == 1


def test_a_scan_never_deserializes_the_previous_generation(tmp_path):
    """R6's rule, and S04's hardest constraint: the publish must not become a
    reason to hold two inventories. The diff is computed off the compact
    signature maps, never off the previous file lists."""
    with real_db(tmp_path):
        run_scan(tmp_path, torrents=3)
        with patch.object(db, 'db_load_file_results',
                          side_effect=AssertionError('the scan read the previous generation')):
            run_scan(tmp_path, torrents=4)


def test_a_refused_scan_still_leaves_the_last_good_generation(tmp_path):
    """C2 / S03 on a real file: a refused scan writes nothing, exits normally,
    clears `scan_marker` and does not trip the crash-loop breaker."""
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=4)
        good = stored(path)
        # A client that forgot its torrents, on a trigger that cannot accept it.
        run_scan(tmp_path, torrents=4, claimed=0, trigger='watchdog')
        after = stored(path)

    assert after['torrents'] == good['torrents']
    assert after['torrents_sigs'] == good['torrents_sigs']
    assert after['snapshots'] == good['snapshots']
    assert after['baseline'] == good['baseline']
    assert after['scan_marker'] is None
    assert after['aborted'] == 0
    assert [r['status'] for r in after['runs']] == ['ok', 'anomaly']


# ═════════════════════════════════════════════════════════════════════════════
# S04 — the findings
# ═════════════════════════════════════════════════════════════════════════════

def _publish_halted(after_tab):
    """Pause a publish once `after_tab`'s file list is written.

    Returns `(patch, reached, release)` — wait on `reached`, do the reading, set
    `release`. The wrapper calls the real function, so the publish is a real
    publish; only its timing is under the test's control.
    """
    reached, release = threading.Event(), threading.Event()
    real = db.db_save_file_results

    def _save(tab, files, **kw):
        real(tab, files, **kw)
        if tab == after_tab:
            reached.set()
            release.wait(10)

    return patch('audit.db_save_file_results', _save), reached, release


def test_a_reader_between_two_saves_sees_one_generation(tmp_path):
    """S04, the reader half. A real reader thread against a real publish.

    The brief's probe read media generation 2 against torrents generation 1, and
    the resulting join read one file as unseeded (Backfill offers a grab) and
    one as not imported (Triage offers a delete). A reader must see the whole
    previous generation or the whole new one.

    Both roots change between the two scans, so the pair of lengths names the
    generation on its own — no new API is needed to observe the mix, which is
    what lets this fail on pre-Phase-13 code for its own reason.
    """
    with real_db(tmp_path):
        run_scan(tmp_path, torrents=2, media=2)

        halt, reached, release = _publish_halted('media')
        seen = {}

        def _read():
            reached.wait(10)
            # Pinned where pinning exists; plain reads where it does not.
            snapshot = getattr(db, 'db_read_snapshot', None)
            if snapshot is None:
                seen['media']    = db.db_load_file_results('media')
                seen['torrents'] = db.db_load_file_results('torrents')
            else:
                with snapshot() as snap:
                    seen['media']    = db.db_load_file_results('media', conn=snap)
                    seen['torrents'] = db.db_load_file_results('torrents', conn=snap)
            release.set()

        reader = threading.Thread(target=_read)
        reader.start()
        run_scan(tmp_path, torrents=5, media=5, extra=[halt])
        reader.join(15)

    assert (len(seen['media']), len(seen['torrents'])) in {(2, 2), (5, 5)}, \
        f"the reader joined two scans: {len(seen['media'])} media, " \
        f"{len(seen['torrents'])} torrents"


def _publish_between_reads(tmp_path, after_read, scan):
    """Patch `app.db_load_file_results` so a whole scan publishes between two reads.

    Deterministic and single-threaded: the publish commits entirely while the
    endpoint is part-way through its reads. A pinned reader sees the generation
    it started on; an unpinned one sees one row from each.
    """
    real, state = db.db_load_file_results, {'n': 0}

    def _load(tab, conn=None, **kw):
        # `conn` is forwarded only where the reader is pinned. Kept tolerant on
        # purpose: run against a pre-Phase-13 `db_load_file_results(tab)` this
        # must still read the row, or the endpoint 500s and the test passes for
        # the wrong reason instead of showing the mixed read (Phase 10's lesson).
        try:
            out = real(tab, conn=conn, **kw)
        except TypeError:
            out = real(tab)
        state['n'] += 1
        if state['n'] == after_read:
            scan()
        return out

    return patch.object(app, 'db_load_file_results', _load)


def _duplicate_generation(tmp_path):
    """A scan whose torrent file and media file are identical copies."""
    local, lib = _roots(tmp_path, 1, media=1)
    with patch('audit.db_load_config', return_value=_cfg(local, lib)),          patch('audit.sources.fetch_file_map', return_value=_answer(local, 1)):
        audit.run_audit_process('manual')
    return local, lib


def test_the_dedupe_endpoint_reads_both_file_lists_from_one_generation(tmp_path):
    """S04's named multi-row reader, and the brief's own example.

    Dedupe joins the two lists, so reading them from two generations builds a
    group across two scans. Here the second generation has no media file at all,
    so an unpinned reader takes the torrent list's partner from a library that
    no longer holds it and reports a group of one — an answer neither scan gives.
    Only the script's own re-verification kept this from being a data-loss
    finding; the report was wrong either way.
    """
    with real_db(tmp_path):
        local, lib = _duplicate_generation(tmp_path)
        assert len(_dedupe_groups(tmp_path)) == 1, "fixture: one duplicate group"

        def _second_generation():
            os.remove(os.path.join(lib, 'Movies', 'Rel', 'f0.mkv'))
            with patch('audit.db_load_config', return_value=_cfg(local, lib)),                  patch('audit.sources.fetch_file_map', return_value=_answer(local, 1)):
                audit.run_audit_process('manual')

        # `workflows_dedupe` reads torrents, then media.
        with _publish_between_reads(tmp_path, 1, _second_generation):
            mid = _dedupe_groups(tmp_path)

    assert len(mid) == 1, "the page joined the new media list to the old torrent list"
    assert len(mid[0]['members']) == 2


def _dedupe_groups(tmp_path):
    local, lib = str(tmp_path / 'torrents'), str(tmp_path / 'media')
    with patch.object(app, 'db_load_config', return_value=_cfg(local, lib)),          patch.object(app, 'AUDITORR_SECRET', ''),          patch.object(app, 'AUDITORR_REQUIRE_AUTH', False),          patch.object(scripts, 'MOUNTINFO_PATH', str(tmp_path / 'no-mountinfo'), create=True):
        resp = app.app.test_client().get('/api/workflows/dedupe')
    return resp.get_json()['groups']


def test_the_config_save_recompute_reads_one_generation(tmp_path):
    """S04's one reader that **writes**.

    The config-save health recompute reads three rows and persists a dashboard
    built from them, so a mixed read does not merely display a wrong health
    score — it stores one, and every page reads it until the next scan.
    """
    with real_db(tmp_path) as path:
        local, lib = _duplicate_generation(tmp_path)

        def _second_generation():
            _roots(tmp_path, 4, media=1)
            with patch('audit.db_load_config', return_value=_cfg(local, lib)),                  patch('audit.sources.fetch_file_map', return_value=_answer(local, 4)):
                audit.run_audit_process('manual')

        # The recompute reads media, then torrents.
        with _publish_between_reads(tmp_path, 1, _second_generation),              patch.object(app, 'db_load_config', return_value=_cfg(local, lib)),              patch.object(app, 'AUDITORR_SECRET', ''),              patch.object(app, 'AUDITORR_REQUIRE_AUTH', False),              patch.object(app, 'restart_watchdog', lambda: None):
            resp = app.app.test_client().post(
                '/api/config', json=dict(_cfg(local, lib), OR_RATIO=0.02))
        assert resp.status_code == 200
        out = stored(path)

    details = out['results']['dashboard']['current']['details']
    assert (details['media_file_count'], details['torrent_file_count']) == (1, 1),         "a mixed read persisted a health score built from two scans"


def test_a_publish_that_fails_part_way_publishes_nothing(tmp_path):
    """S04's write half. Killed between the media and torrents writes, the
    database used to hold media generation 3 against torrents generation 2."""
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=2)
        good = stored(path)

        boom = patch('audit.db_save_file_signatures',
                     side_effect=RuntimeError('disk full'))
        run_scan(tmp_path, torrents=6, extra=[boom])
        after = stored(path)

    assert after['media']    == good['media']
    assert after['torrents'] == good['torrents']
    assert after['triage']   == good['triage']
    assert after['cleanup']  == good['cleanup']
    assert after['generation'] == good['generation']
    # `latest_results` keeps its dashboard; only its `status` line changes, which
    # is the error path reporting the failure where the UI shows it.
    assert after['results']['dashboard'] == good['results']['dashboard']
    assert 'could not be saved' in after['results']['status']


def test_an_interrupted_publish_leaves_the_previous_scan_whole(tmp_path):
    """The file lists **and** the signatures. The brief's probe left the media
    signatures a generation behind the media list, so the next scan's diff
    spanned two generations — every file reported as coming back, and
    `count_pile_resolved` paying shovel credit for a recovery that never
    happened, which the monotonic Rounds layer can never take back."""
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=2)
        good = stored(path)
        # A streak left by an earlier killed scan, which the good scan above
        # cleared — so the failed publish below has something to reset, and
        # the assertion on it cannot pass merely because nothing changed.
        db.db_set_meta('consecutive_aborted_scans', 1)

        # Injected at the *last* write of the publish, so the file lists, the
        # signatures, the results row and the run record are all already
        # written inside the transaction when it fails.
        boom = patch('audit.db_update_meta', side_effect=RuntimeError('disk full'))
        run_scan(tmp_path, torrents=6, extra=[boom])
        after = stored(path)

    assert after['media_sigs']    == good['media_sigs']
    assert after['torrents_sigs'] == good['torrents_sigs']
    assert len(after['torrents_sigs']) == 2
    assert after['torrents'] == good['torrents']
    # The run is recorded as an error, the marker is cleared, and the crash-loop
    # streak is untouched: the process declined to write, it did not die.
    assert [r['status'] for r in after['runs']] == ['ok', 'error']
    assert 'could not be saved' in after['runs'][-1]['error_message']
    assert after['scan_marker'] is None
    assert after['aborted'] == 0


def test_no_upload_snapshot_survives_a_publish_that_did_not_complete(tmp_path):
    """Decision 2 (a), 2026-09-15: the differenced series join the publish.

    The upload snapshot and the change-log entry were written *before* the
    inventory, so a publish that failed afterwards left a snapshot row and a
    change-log entry for a scan whose file lists never landed. Upload snapshots
    are differenced, so a dip and its recovery become one enormous false upload
    day — CLEANUP §10's argument, arriving through a partial publish rather than
    a refused scan.
    """
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=2)
        run_scan(tmp_path, torrents=3)
        good = stored(path)
        assert good['snapshots'] == 2 and good['changes'] == 1, "fixture"

        boom = patch('audit.db_save_file_results', side_effect=RuntimeError('disk full'))
        run_scan(tmp_path, torrents=7, extra=[boom])
        after = stored(path)

    assert after['snapshots'] == good['snapshots']
    assert after['changes']   == good['changes']
    assert after['history']   == good['history']
    assert after['ns_progress'] == good['ns_progress']
    assert after['baseline'] == good['baseline']


def test_a_backfill_credit_during_a_publish_is_not_lost(tmp_path):
    """Rounds' first rule is that points are never taken away, so the publish
    must not be able to drop one to a lock. The credit lands while the publish
    holds the write lock; it waits on `_meta_lock` and then merges.

    **Passes on pre-Phase-13 code too, and is kept for that reason** — the risk
    this phase introduced is the new lock ordering, not the old one. The publish
    now takes `_meta_lock` *before* the SQLite write lock; the other order
    deadlocks until the busy timeout and then loses exactly this credit.
    """
    with real_db(tmp_path) as path:
        run_scan(tmp_path, torrents=2)

        halt, reached, release = _publish_halted('torrents')
        done = threading.Event()

        def _credit():
            reached.wait(10)
            # The real endpoint's write, verbatim (`app._record_backfill_credit`):
            # it starts while the publish holds the lock and blocks until commit.
            db.db_update_meta('ns_progress',
                              lambda p: rounds.record_backfill(p, files=3))
            done.set()

        crediting = threading.Thread(target=_credit)
        crediting.start()
        # Give the credit thread time to reach (and block on) the lock before
        # the publish is released.
        threading.Thread(target=lambda: (reached.wait(10), time.sleep(0.2),
                                         release.set())).start()
        run_scan(tmp_path, torrents=3, extra=[halt])
        crediting.join(15)
        assert done.is_set(), "the credit never completed"
        out = stored(path)

    progress = out['ns_progress'] or {}
    assert progress.get('backfilled', 0) >= 3, "a backfill credit was lost to the publish"
    assert progress.get('peaks'), "the audit's own progress was lost instead"


def test_the_publish_never_holds_two_inventories(tmp_path):
    """RAM is the hard constraint of this phase. The file lists reach the
    transaction as **compressed blobs**, prepared one at a time outside it — so
    the lock is held for the writes alone (measured 0.10 s against 21 s of
    compression at 650,000 files) and nothing materialises a second generation.
    """
    handed = []
    real = db.db_save_file_results

    def _save(tab, files, **kw):
        handed.append((tab, files))
        real(tab, files, **kw)

    with real_db(tmp_path):
        run_scan(tmp_path, torrents=3, extra=[patch('audit.db_save_file_results', _save)])

    assert [tab for tab, _ in handed] == ['media', 'torrents', 'triage', 'cleanup']
    for tab, files in handed:
        assert isinstance(files, db.PreparedFileResults), f"{tab} was handed a record list"
        assert isinstance(files.blob, bytes)


def test_a_publish_holds_the_write_lock_only_for_the_writes(tmp_path):
    """Decision 4, 2026-09-15. Compression is staged outside the transaction, so
    another writer waits for the writes rather than for the compression. The
    assertion is structural rather than a stopwatch: every blob is already
    compressed before `db_publish` is entered."""
    order = []
    real_prepare, real_publish = db.db_prepare_file_results, db.db_publish

    def _prepare(files):
        order.append('prepare')
        return real_prepare(files)

    @contextmanager
    def _publish():
        order.append('publish')
        with real_publish() as conn:
            yield conn

    with real_db(tmp_path):
        run_scan(tmp_path, torrents=3, extra=[
            patch('audit.db_prepare_file_results', _prepare),
            patch('audit.db_publish', _publish)])

    assert order.count('publish') == 1
    assert order.index('publish') == len(order) - 1, \
        "a file list was compressed while the write lock was held"
