"""What /api/debug/report tells a field tester's reader (DEBUG_REPORT_BUILD, 2026-11-16).

The scan plan (SCAN SC1-SC14) was proven on a synthetic 650,000-file library,
and testers with large libraries are asked for two reports each: one from the
1.8.3 release and one from `:experimental`. These are the report's additions
for reading them:

1. `build`: which image sent it. Both report `app_version` 1.8.3.
2. `scan_state.peak_source`: whether the stored peak is the kernel's (SCAN
   SC7/E1) or the old 20 s sample, where the kernel refused the reset.
3. `phase_history`: each phase's start to 0.01 s (`t`) and the scan's peak so
   far (`hwm_mb`), with the client listing as the first phase.
4. `scan_state.arr_ask`: how long Sonarr and Radarr took to say which files
   they hold, asked while the disk is walked. The test library's arrs were
   fakes that answer at once, so this is the one input never timed at scale.
5. `runtime.filesystems`: the filesystem type each root is stored on, which a
   walk's speed depends on. The type only, never the mount or its source.

Every field here is counts, seconds, booleans, a filesystem type or a short
commit, never a path, host or infohash: the report is pasted in public.
"""

import json
import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest

import audit
import db
import debug
import scripts
from arr import ManagedPaths
from backend_tests.test_publish_atomicity import _answer, real_db, run_scan
from backend_tests.test_scan_peak_rss import _kernel

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True, scope='module')
def _session_db():
    # The session's own database (conftest's DATA_DIR), as `app`'s import would
    # make it: the report reads the config, the runs and app_meta.
    db.init_db()


def _report():
    return debug.build_debug_report('9.9.9')


# ---------------------------------------------------------------------------
# 1. Which build sent the report
# ---------------------------------------------------------------------------

_SHA = '0123456789abcdef0123456789abcdef01234567'


@pytest.mark.parametrize('env, build', [
    ({'AUDITORR_COMMIT': _SHA, 'AUDITORR_REF': 'experimental'},
     {'commit': '0123456789ab', 'ref': 'experimental'}),
    ({'AUDITORR_COMMIT': 'e1a48cd', 'AUDITORR_REF': 'v1.8.4'},
     {'commit': 'e1a48cd', 'ref': 'v1.8.4'}),
    ({}, {'commit': None, 'ref': None}),
    ({'AUDITORR_COMMIT': '', 'AUDITORR_REF': ''}, {'commit': None, 'ref': None}),
    ({'AUDITORR_COMMIT': '  ', 'AUDITORR_REF': 'main'}, {'commit': None, 'ref': 'main'}),
], ids=['ci', 'short', 'unset', 'empty', 'blank commit'])
def test_the_report_names_its_build(monkeypatch, env, build):
    for key in ('AUDITORR_COMMIT', 'AUDITORR_REF'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    report = _report()
    assert report['build'] == build
    assert report['app_version'] == '9.9.9'
    # A full SHA is a 40-hex token; only its first 12 characters go out.
    assert _SHA not in json.dumps(report)


def _lines(path):
    with open(os.path.join(REPO, path), encoding='utf-8') as f:
        return f.read().splitlines()


def test_ci_passes_the_commit_and_ref_to_the_image_build():
    lines = _lines('.github/workflows/docker.yml')
    start = next(i for i, l in enumerate(lines) if 'docker/build-push-action' in l)
    # The step runs to the next list item at the step's own indentation.
    step_indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
    assert lines[start - 1].lstrip().startswith('- name:')
    end = next((i for i in range(start, len(lines))
                if lines[i].strip().startswith('- ')
                and len(lines[i]) - len(lines[i].lstrip()) == step_indent), len(lines))
    step = lines[start:end]
    at = next(i for i, l in enumerate(step) if l.strip().startswith('build-args:'))
    indent = len(step[at]) - len(step[at].lstrip())
    args = []
    for line in step[at + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        if line.strip():
            args.append(line.strip())
    assert 'AUDITORR_COMMIT=${{ github.sha }}' in args
    assert 'AUDITORR_REF=${{ github.ref_name }}' in args


def test_the_dockerfile_takes_them_after_its_dependencies():
    lines = [l.split('#', 1)[0].strip() for l in _lines('Dockerfile')]
    stage = max(i for i, l in enumerate(lines) if l.upper().startswith('FROM '))
    pip = max(i for i, l in enumerate(lines) if re.match(r'RUN\b.*\bpip install\b', l))
    assert pip > stage
    for name in ('AUDITORR_COMMIT', 'AUDITORR_REF'):
        # Declared in the final stage after the dependency install, so a new
        # commit never invalidates the pip layer's cache.
        args = [i for i, l in enumerate(lines) if re.match(rf'ARG\s+{name}\b', l)]
        assert args and all(i > pip for i in args), name
        # And handed to the process as an environment variable.
        env = [i for i, l in enumerate(lines)
               if l.startswith('ENV ') and re.search(rf'\b{name}=\$\{{?{name}\}}?(\s|$)', l)]
        assert env and min(env) > max(args), name


# ---------------------------------------------------------------------------
# 2. Whether the stored peak is the kernel's
# ---------------------------------------------------------------------------

@contextmanager
def _scan_db(tmp_path):
    """`real_db`, with the report's own reads of the runs table pointed at it."""
    with real_db(tmp_path) as path, patch.object(debug, 'DB_FILE', str(path)):
        yield path


def _no_path(field, tmp_path):
    text = json.dumps(field)
    for spelling in (str(tmp_path), tmp_path.as_posix(), tmp_path.name):
        assert spelling not in text


@pytest.mark.parametrize('reset_ok, source, stored', [(True, 'kernel', 4321),
                                                       (False, 'sampled', 17)],
                         ids=['reset', 'refused'])
def test_the_report_says_where_the_stored_peak_came_from(tmp_path, reset_ok, source, stored):
    with _scan_db(tmp_path):
        run_scan(tmp_path, torrents=3, extra=_kernel(reset_ok, []))
        report = _report()
    assert report['scan_state']['peak_source'] == source
    # The row it describes: the kernel's 4,321 MB, or RSS at save time.
    assert report['audit_history'][0]['peak_rss_mb'] == stored
    _no_path(report['scan_state']['peak_source'], tmp_path)


# ---------------------------------------------------------------------------
# 3. Where the scan's time and memory go
# ---------------------------------------------------------------------------

_LISTING = 0.2


def _slow_client(tmp_path):
    """A client whose listing takes `_LISTING` seconds."""
    answer = _answer(str(tmp_path / 'torrents'), 3)

    def fetch(*args, **kwargs):
        time.sleep(_LISTING)
        return answer
    return patch('audit.sources.fetch_file_map', side_effect=fetch)


@pytest.mark.parametrize('reset_ok', [True, False], ids=['reset', 'refused'])
def test_each_phase_says_when_it_began_and_the_peak_so_far(tmp_path, reset_ok):
    with _scan_db(tmp_path):
        run_scan(tmp_path, torrents=3, media=2,
                 extra=(*_kernel(reset_ok, []), _slow_client(tmp_path),
                        # Where the reset worked, one read of /proc gives both.
                        # (`create`, so on the old code this fails on what the
                        # history holds, not on the patch.)
                        patch('audit.rss_and_peak_mb', return_value=(17, 4321),
                              create=True)))
        report = _report()
    phases = report['scan_state']['phase_history']
    # The listing is a phase of its own, from the scan's start.
    assert phases[0]['message'] == 'Connecting to torrent source...'
    assert phases[0]['t'] == 0
    assert phases[1]['message'] == 'Scanning torrent directory...'
    assert phases[1]['t'] >= _LISTING - 0.02
    assert phases[-1]['message'] == 'Saving audit results...'
    times = [p['t'] for p in phases]
    for t in times:
        assert isinstance(t, float)
        assert re.fullmatch(r'\d+\.\d{1,2}', json.dumps(t)), t
    assert times == sorted(times)
    # The last phase runs to the scan's duration.
    assert times[-1] <= report['scan_state']['last_scan_duration']
    for p in phases:
        # The scan's peak so far: the kernel's, or none where it can't be had.
        assert p['hwm_mb'] == (4321 if reset_ok else None)
        assert p['rss_mb'] == 17
        assert re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d', p['at'])
    # Persisted for after a restart, entry for entry.
    assert report['crash_evidence']['last_scan_phases'] == phases
    _no_path(phases, tmp_path)


_STATUS = """Name:\tgunicorn
VmPeak:\t 3012340 kB
VmSize:\t 2900000 kB
VmHWM:\t 1163264 kB
VmRSS:\t  532480 kB
RssAnon:\t  500000 kB
Threads:\t9
"""


def test_one_read_gives_rss_and_the_peak(tmp_path):
    status = tmp_path / 'status'
    status.write_text(_STATUS)
    with patch.object(debug, '_PROC_STATUS', str(status)):
        assert debug.rss_and_peak_mb() == (520, 1136)
        status.write_text('Name:\tx\nVmRSS:\t 2048 kB\n')
        assert debug.rss_and_peak_mb() == (2, None)
    with patch.object(debug, '_PROC_STATUS', str(tmp_path / 'missing')):
        assert debug.rss_and_peak_mb() == (None, None)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='needs /proc')
def test_one_read_agrees_with_the_two_it_replaces():
    with debug._hwm_lock:
        rss, hwm = debug.rss_and_peak_mb()
        assert hwm == debug.peak_rss_since_reset_mb()
        # RSS moves between two reads; within the kernel counters' slack.
        assert abs(rss - debug.process_rss_mb()) <= 8


# ---------------------------------------------------------------------------
# 4. How long Sonarr and Radarr took to answer
# ---------------------------------------------------------------------------

_HELD = 5


def _held():
    return ManagedPaths(f'/data/media/movies/T{i}/t{i}.mkv' for i in range(_HELD))


def _arrs(fetch):
    return patch('audit.fetch_arr_managed_paths', side_effect=fetch)


def _ask(tmp_path, *extra):
    """One scan; the report's `arr_ask`, and the report."""
    with _scan_db(tmp_path):
        run_scan(tmp_path, torrents=3, media=2, extra=extra)
        report = _report()
    ask = report['scan_state']['arr_ask']
    _no_path(ask, tmp_path)
    return ask, report


def test_the_report_times_the_arrs_answer(tmp_path, caplog):
    def fetch(cfg):
        time.sleep(0.2)
        return _held(), 'ok'
    caplog.set_level(logging.INFO, logger='audit')
    ask, _ = _ask(tmp_path, _arrs(fetch))
    assert ask['check'] == 'ok' and ask['files'] == _HELD and ask['timed_out'] is False
    assert ask['secs'] >= 0.19
    # The walks ran meanwhile, so the scan waited for less than the arrs took.
    assert 0 <= ask['waited_secs'] <= ask['secs'] + 0.05
    # One log line with the same figures, for recent_logs.
    lines = [r.getMessage() for r in caplog.records if 'Sonarr/Radarr file list' in r.getMessage()]
    assert lines == [f"Audit: Sonarr/Radarr file list: check=ok files={_HELD} "
                     f"secs={ask['secs']} waited_secs={ask['waited_secs']} timed_out=False"]


def test_an_answer_already_in_is_not_waited_for(tmp_path):
    answered = threading.Event()
    listing = _answer(str(tmp_path / 'torrents'), 3)

    def fetch(cfg):
        answered.set()
        return _held(), 'ok'

    def client(*args, **kwargs):
        # The client answers only once the arrs have.
        assert answered.wait(5)
        return listing
    ask, _ = _ask(tmp_path, _arrs(fetch), patch('audit.sources.fetch_file_map', side_effect=client))
    assert ask['check'] == 'ok' and ask['files'] == _HELD
    assert ask['waited_secs'] == 0


def test_an_arr_that_fails_is_unreachable(tmp_path):
    def fetch(cfg):
        raise RuntimeError("HTTPConnectionPool(host='sonarr.lan', port=8989): refused")
    ask, report = _ask(tmp_path, _arrs(fetch))
    assert ask['check'] == 'unreachable' and ask['files'] is None
    assert ask['timed_out'] is False and isinstance(ask['secs'], float)
    assert 'sonarr.lan' not in json.dumps(report)


def test_the_scan_gives_up_on_a_slow_arr(tmp_path):
    release = threading.Event()

    def fetch(cfg):
        release.wait(10)
        return _held(), 'ok'
    try:
        ask, _ = _ask(tmp_path, _arrs(fetch), patch.object(audit, '_ARR_PATHS_WAIT', 0.2))
    finally:
        release.set()
    assert ask['timed_out'] is True and ask['secs'] is None
    assert ask['check'] == 'unreachable' and ask['files'] is None
    assert 0.18 <= ask['waited_secs'] <= 2


def test_no_arr_is_none(tmp_path):
    # The harness configures no arr: the real fetch answers at once.
    ask, _ = _ask(tmp_path)
    assert ask['check'] == 'none' and ask['files'] is None and ask['timed_out'] is False
    assert ask['waited_secs'] <= ask['secs'] + 0.05


def test_a_scan_refused_before_the_answer_says_nothing_of_it(tmp_path):
    with _scan_db(tmp_path):
        run_scan(tmp_path, torrents=4)
        assert _report()['scan_state']['arr_ask']['check'] == 'none'
        # The client claims nothing: refused after the torrent walk, before
        # the scan reads the arrs' answer. Not the scan before's answer.
        run_scan(tmp_path, torrents=4, claimed=0, trigger='watchdog')
        report = _report()
    assert 'anomaly' in [r['status'] for r in report['audit_history']]
    assert report['scan_state']['arr_ask'] is None


# ---------------------------------------------------------------------------
# 5. What each root is stored on
# ---------------------------------------------------------------------------

# proc_pid_mountinfo(5): id, parent, device, root, mount point, options,
# optional fields up to `-`, then type, source and the superblock's options.
_MOUNTINFO = '\n'.join([
    # The container's own root; its options name the host's Docker folders.
    '22 1 0:21 / / rw,relatime master:1 - overlay overlay '
    'rw,lowerdir=/var/lib/docker/overlay2/l/SRCLOWER,upperdir=/var/lib/docker/srcupper/diff',
    # Unraid's user share, and a mount whose point only starts the same way.
    '30 22 0:40 /mnt/user /mnt/user rw,noatime master:2 - fuse.shfs shfs rw,user_id=0',
    '31 22 8:17 / /mnt/user2 rw - xfs /dev/sdq1 rw,attr2',
    # Nested in the share, from a host, and then mounted over.
    '32 30 0:50 / /mnt/user/vaultzeta rw - nfs4 nasbox.lan:/export/srcvault rw,vers=4.2',
    '33 30 0:51 / /mnt/user/vaultzeta rw - zfs srcpool/vault rw,xattr',
    # A space in a mount point, which the kernel writes as an octal escape.
    '34 22 0:52 / /srv/my\\040disk rw - btrfs /dev/sdr1 rw',
]) + '\n'


@pytest.fixture
def mountinfo(tmp_path):
    path = tmp_path / 'mountinfo'
    path.write_text(_MOUNTINFO)
    return str(path)


def test_each_root_reads_its_deepest_mount(mountinfo, tmp_path):
    assert scripts.filesystem_types({
        'share':   '/mnt/user/Movies',
        'beside':  '/mnt/user2/torrents',        # not /mnt/user's
        'over':    '/mnt/user/vaultzeta/TV/',     # the later of two lines
        'point':   '/mnt/user/vaultzeta',
        'escaped': '/srv/my disk/media',
        'root':    '/data/torrents',              # only / holds it
        'blank':   '  ',
        'unset':   None,
    }, mountinfo) == {
        'share': 'fuse.shfs', 'beside': 'xfs', 'over': 'zfs', 'point': 'zfs',
        'escaped': 'btrfs', 'root': 'overlay', 'blank': None, 'unset': None,
    }
    # No mountinfo (not Linux): nothing is known.
    assert scripts.filesystem_types({'torrents': '/mnt/user2/torrents'},
                                    str(tmp_path / 'missing')) == {'torrents': None}


def _report_with(local, media, mountinfo_path):
    cfg = {**db.db_load_config(), 'LOCAL_PATH': local, 'MEDIA_PATH': media}
    with patch.object(scripts, 'MOUNTINFO_PATH', mountinfo_path), \
         patch('debug.db_load_config', return_value=cfg):
        return _report()


def test_the_report_names_each_roots_filesystem_and_nothing_more(mountinfo):
    report = _report_with('/mnt/user2/torrents', '/mnt/user/vaultzeta/Movies', mountinfo)
    assert report['runtime']['filesystems'] == {'torrents': 'xfs', 'media': 'zfs'}
    # No mount point, source or option from the file. (`/mnt/user` and
    # `/mnt/user2` are generic segments the config block prints by design;
    # `vaultzeta` is one it hashes.)
    text = json.dumps(report)
    for leak in ('vaultzeta', 'SRCLOWER', 'srcupper', '/var/lib/docker', 'nasbox', 'srcvault',
                 'srcpool', '/dev/sdq1', '/dev/sdr1', 'my disk', 'my\\040disk', 'user_id'):
        assert leak not in text, leak


def test_without_mountinfo_or_a_root_the_type_is_null(mountinfo, tmp_path):
    report = _report_with('/mnt/user2/torrents', '/mnt/user/Movies', str(tmp_path / 'missing'))
    assert report['runtime']['filesystems'] == {'torrents': None, 'media': None}
    report = _report_with('', '/mnt/user/Movies', mountinfo)
    assert report['runtime']['filesystems'] == {'torrents': None, 'media': 'fuse.shfs'}
