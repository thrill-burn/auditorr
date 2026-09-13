"""A Backfill run survives the page, and its status poll sends only what changed.

B4 — the generate job was one process-wide in-memory slot. Navigating away lost
the only handle to it (the id lived in React state) while its searches kept
running against the user's indexers; a second tab's Generate set `stop_flag` on
the first tab's half-finished run with no notice at either end; and a finished
run's full result set stayed resident until the next run replaced it.

B5 — `/generate/status` re-sent the whole growing result set, every candidate's
complete release list, every two seconds for a run that can last hours.
Completed rows never change, so the poll can be incremental.
"""
import threading
import time
from types import SimpleNamespace

import pytest

import app

_ENV = {'REMOTE_ADDR': '127.0.0.1'}


class _Gate:
    """`fetch_release_matrix`, held until the test lets a search through."""

    def __init__(self):
        self.sem = threading.Semaphore(0)
        self.calls = 0

    def __call__(self, *_a, **_kw):
        self.calls += 1
        self.sem.acquire(timeout=5)
        return []

    def release(self, n=1):
        for _ in range(n):
            self.sem.release()


def _candidate(n):
    return {'key': f'radarr-mv_{n}', 'scope': 'movie', 'arr_service': 'radarr',
            'arr_connection_id': 'radarr-mv', 'arr_id': n, 'arr_title': f'Film {n}',
            'arr_url': '', 'rep_path': f'movies/Film {n}.mkv', 'path': f'movies/Film {n}.mkv',
            'season_number': None, 'episode_numbers': [], 'file_count': 1,
            'total_size': 100 - n, 'file_quality': '', 'file_hdr': '', 'file_ids': [n],
            'search': {'service': 'radarr', 'connection_id': 'radarr-mv', 'arr_id': n}}


@pytest.fixture
def gen(monkeypatch):
    gate = _Gate()
    monkeypatch.setattr(app, 'AUDITORR_SECRET', '')
    monkeypatch.setattr(app, 'AUDITORR_REQUIRE_AUTH', False)
    monkeypatch.setattr(app, 'db_load_config', lambda: {})
    monkeypatch.setattr(app, '_build_generate_candidates',
                        lambda cfg, **kw: [_candidate(n) for n in (1, 2, 3)])
    monkeypatch.setattr(app, 'arr_media_index_errors', lambda: [])
    monkeypatch.setattr(app, 'fetch_release_matrix', gate)
    app._gen_jobs.clear()
    client = app.app.test_client()

    def start(**body):
        return client.post('/api/workflows/generate', json={'count': 10, **body}, environ_base=_ENV)

    def status(job_id, since=None):
        q = f'/api/workflows/generate/status?job_id={job_id}'
        if since is not None:
            q += f'&since={since}'
        return client.get(q, environ_base=_ENV)

    def wait(job_id, pred):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            body = status(job_id).get_json()
            if pred(body):
                return body
            time.sleep(0.01)
        raise AssertionError(f'job never reached the expected state: {body}')

    yield SimpleNamespace(client=client, gate=gate, start=start, status=status, wait=wait)

    # Let every thread finish before monkeypatch restores the real search.
    for job in list(app._gen_jobs.values()):
        job['stop_flag'] = True
    gate.release(50)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
            not j.get('finished_at') for j in list(app._gen_jobs.values())):
        time.sleep(0.01)
    app._gen_jobs.clear()


# ── B5: the poll is incremental ──────────────────────────────────────────────

def test_generate_status_is_incremental(gen):
    job_id = gen.start().get_json()['job_id']

    gen.gate.release(2)
    first = gen.wait(job_id, lambda b: b['completed'] == 2)
    first = gen.status(job_id, since=0).get_json()
    assert [r['arr_title'] for r in first['results']] == ['Film 1', 'Film 2']
    assert first['next'] == 2
    # The in-flight row travels separately: it is still changing, so it is not a
    # result the client may append.
    assert first['current']['arr_title'] == 'Film 3'
    assert first['current']['status'] == 'searching'

    gen.gate.release(1)
    gen.wait(job_id, lambda b: b['status'] == 'done')
    second = gen.status(job_id, since=first['next']).get_json()
    assert [r['arr_title'] for r in second['results']] == ['Film 3']
    assert (second['next'], second['current']) == (3, None)


def test_a_poll_with_nothing_new_sends_no_rows(gen):
    job_id = gen.start().get_json()['job_id']
    gen.gate.release(3)
    gen.wait(job_id, lambda b: b['status'] == 'done')

    assert gen.status(job_id, since=3).get_json()['results'] == []


def test_a_since_past_the_end_is_clamped_not_an_error(gen):
    job_id = gen.start().get_json()['job_id']
    gen.gate.release(3)
    gen.wait(job_id, lambda b: b['status'] == 'done')

    body = gen.status(job_id, since=99).get_json()
    assert (body['results'], body['next']) == ([], 3)


# ── B4: a run is durable and nobody else's ───────────────────────────────────

def test_a_second_run_is_refused_not_a_silent_stop(gen):
    first = gen.start().get_json()['job_id']

    res = gen.start()

    assert res.status_code == 409
    assert res.get_json()['code'] == 'job_running'
    assert res.get_json()['job_id'] == first
    assert app._gen_jobs[first]['stop_flag'] is False


def test_a_page_that_navigated_away_can_reattach_to_its_run(gen):
    """No `since`: everything completed so far, which is how a remount catches up."""
    job_id = gen.start().get_json()['job_id']
    gen.gate.release(3)
    gen.wait(job_id, lambda b: b['status'] == 'done')

    body = gen.status(job_id).get_json()
    assert [r['arr_title'] for r in body['results']] == ['Film 1', 'Film 2', 'Film 3']


def test_an_unknown_run_is_a_404_the_page_can_act_on(gen):
    res = gen.status('nope')

    assert res.status_code == 404
    assert res.get_json()['code'] == 'job_not_found'


def test_a_stopped_run_says_it_was_stopped(gen):
    job_id = gen.start().get_json()['job_id']
    gen.client.post('/api/workflows/generate/stop', json={'job_id': job_id}, environ_base=_ENV)
    gen.gate.release(3)

    body = gen.wait(job_id, lambda b: b['status'] != 'running')
    assert (body['status'], body['stop_reason']) == ('stopped', 'stopped')


def test_a_run_nobody_polls_stops_itself(gen, monkeypatch):
    """The tab is gone: stop querying indexers for results no one will see."""
    monkeypatch.setattr(app, '_GEN_ABANDON_SECS', -1)
    job_id = gen.start().get_json()['job_id']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not app._gen_jobs[job_id].get('finished_at'):
        time.sleep(0.01)

    job = app._gen_jobs[job_id]
    assert (job['status'], job['stop_reason']) == ('stopped', 'abandoned')
    assert gen.gate.calls == 0


def test_a_run_that_crashes_does_not_block_every_later_run(gen, monkeypatch):
    monkeypatch.setattr(app, '_rank_releases', lambda *a, **k: 1 / 0)
    job_id = gen.start().get_json()['job_id']
    gen.gate.release(3)
    gen.wait(job_id, lambda b: b['status'] != 'running')

    assert gen.start().status_code == 200


def test_finished_runs_are_swept_by_age_and_by_count():
    now = time.time()
    app._gen_jobs.clear()
    for n, age in enumerate([10, 20, 30, 40, 7200]):
        app._gen_jobs[f'j{n}'] = {'id': f'j{n}', 'status': 'done', 'finished_at': now - age}
    app._gen_jobs['live'] = {'id': 'live', 'status': 'running', 'finished_at': None}

    with app._gen_jobs_lock:
        app._sweep_gen_jobs(now)

    assert sorted(app._gen_jobs) == ['j0', 'j1', 'j2', 'live']
    app._gen_jobs.clear()
