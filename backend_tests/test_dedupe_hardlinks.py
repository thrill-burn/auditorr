import os
import subprocess
import shlex
import shutil
import tempfile
from pathlib import Path

import pytest
from unittest.mock import patch

import audit
import scripts


def snapshot(root, excluded=()):
    inode_map = {}
    order, _, _, _ = audit._walk_directory(
        str(root), 'Torrent', inode_map, {}, 0, 0,
        exclusion_patterns=list(excluded))
    duplicates = audit._build_duplicate_map(inode_map)
    torrent, media = audit._assemble_records(order, [], inode_map, duplicates)
    return {'torrent_files': torrent, 'media_files': media}, inode_map


def generate(root, excluded=()):
    results, _ = snapshot(root, excluded)
    return scripts.generate_script('dedupe', results, {'LOCAL_PATH': str(root)})


def run_script(root, script, overrides=None):
    # Exercise the real generated Bash script with a PATH that has NO Python.
    # Failure injection wraps OS commands, without changing the script itself.
    with tempfile.TemporaryDirectory() as tool_dir:
        for command in ('cmp', 'stat', 'ln', 'mv', 'mktemp', 'rm', 'rmdir'):
            dest = Path(tool_dir) / command
            if command in (overrides or {}):
                dest.write_text('#!/bin/bash\n' + overrides[command])
                dest.chmod(0o755)
            else:
                dest.symlink_to(shutil.which(command))
        env = {**os.environ, 'PATH': tool_dir}
        env.pop('BASH_ENV', None)
        return subprocess.run(['/bin/bash'], input=script, text=True, cwd=root,
                              env=env, capture_output=True, check=False, timeout=20)


def files(root, siblings=2):
    a = root / 'A'
    b = root / 'Luminarr'
    a.write_bytes(b'same bytes')
    b.write_bytes(a.read_bytes())
    paths = [b]
    for index in range(siblings - 1):
        path = root / f'Darkpeers {index}'
        os.link(b, path)
        paths.append(path)
    return a, paths


def test_all_siblings_hash_once_and_rerun(tmp_path):
    a, siblings = files(tmp_path, 14)  # exceed the old path cap
    with patch('audit.get_fast_hash', wraps=audit.get_fast_hash) as hashed:
        results, inode_map = snapshot(tmp_path)
    assert hashed.call_count == 2
    results['torrent_files'].sort(key=lambda f: f['path'] != 'A')
    duplicate_map = audit._build_duplicate_map(inode_map)
    assert set(duplicate_map[(a.stat().st_dev, a.stat().st_ino)]) == set(map(str, siblings))
    script = scripts.generate_script('dedupe', results, {'LOCAL_PATH': str(tmp_path)})
    result = run_script(tmp_path, script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count('Verifying:') == 1
    assert 'Paths linked: 14' in result.stdout
    assert 'Reclaimed bytes (logical): 10' in result.stdout
    assert len({p.stat().st_ino for p in [a] + siblings}) == 1
    result = run_script(tmp_path, script)
    assert result.returncode == 0
    assert 'Paths linked: 0' in result.stdout
    assert 'Reclaimed bytes (logical): 0' in result.stdout


def test_content_changed_since_audit_is_rejected(tmp_path):
    a, siblings = files(tmp_path)
    script = generate(tmp_path)
    siblings[0].write_bytes(b'other data')
    result = run_script(tmp_path, script)
    assert result.returncode == 0
    assert 'SKIP: files differ' in result.stdout
    assert 'Reclaimed bytes (logical): 0' in result.stdout
    assert a.read_bytes() == b'same bytes'
    assert all(p.read_bytes() == b'other data' for p in siblings)
    results, _ = snapshot(tmp_path)
    assert not any(f['duplicate_paths'] for f in results['torrent_files'])


def test_excluded_sibling_not_touched_or_counted(tmp_path):
    a, siblings = files(tmp_path)
    # Force canonical order so the excluded link belongs to the replaced copy.
    results, _ = snapshot(tmp_path, ['contains:Darkpeers'])
    results['torrent_files'].sort(key=lambda f: f['path'] != 'A')
    script = scripts.generate_script('dedupe', results, {'LOCAL_PATH': str(tmp_path)})
    old_inode = siblings[1].stat().st_ino
    result = run_script(tmp_path, script)
    assert result.returncode == 0
    assert siblings[0].stat().st_ino == a.stat().st_ino
    assert siblings[1].stat().st_ino == old_inode
    assert 'Reclaimed bytes (logical): 0' in result.stdout
    assert '0.0 B potentially recoverable' in script


def test_link_failure_preserves_original_and_reports_error(tmp_path):
    a, siblings = files(tmp_path)
    script = generate(tmp_path)
    before = {p: p.stat().st_ino for p in [a] + siblings}
    result = run_script(tmp_path, script, {'ln': 'exit 1\n'})
    assert result.returncode == 1
    assert 'Cannot create replacement hardlink' in result.stderr
    assert 'Reclaimed bytes (logical): 0' in result.stdout
    assert before == {p: p.stat().st_ino for p in before}
    assert all(p.read_bytes() == b'same bytes' for p in before)
    assert not list(tmp_path.glob('.auditorr-dedupe-*'))


def test_overlapping_groups_existing_hardlinks_and_external_link(tmp_path):
    a, siblings = files(tmp_path)
    c = tmp_path / 'C'
    c.write_bytes(a.read_bytes())
    external = tmp_path / 'unscanned'
    os.link(c, external)
    results = {'torrent_files': [
        {'path': 'A', 'inode': a.stat().st_ino, 'size': 10,
         'duplicate_paths': [str(siblings[0]), str(siblings[1]),
                             str(tmp_path) + '/./' + siblings[0].name]},
        {'path': siblings[0].name, 'inode': siblings[0].stat().st_ino, 'size': 10,
         'duplicate_paths': [str(c), str(a)]},
    ]}
    cfg = {'LOCAL_PATH': str(tmp_path)}
    groups = scripts._build_dup_groups(results['torrent_files'], str(tmp_path))['groups']
    assert len(groups) == 1
    assert groups[0]['recoverable_size'] == 10
    result = run_script(tmp_path, scripts.generate_script('dedupe', results, cfg))
    assert result.returncode == 0
    assert result.stdout.count('Verifying:') == 2
    assert 'Reclaimed bytes (logical): 10' in result.stdout
    assert len({p.stat().st_ino for p in [a, c] + siblings}) == 1
    assert external.stat().st_ino != a.stat().st_ino


def test_cross_device_partition(tmp_path):
    a, siblings = files(tmp_path)
    remote = tmp_path / 'remote'
    remote.write_bytes(a.read_bytes())
    results, _ = snapshot(tmp_path)
    real_stat = os.lstat

    def stat_with_device(path):
        st = real_stat(path)
        if str(path) == str(remote):
            values = list(st)
            values[2] += 1
            return os.stat_result(values)
        return st

    with patch('scripts.os.lstat', side_effect=stat_with_device):
        groups = scripts._build_dup_groups(results['torrent_files'], str(tmp_path))['groups']
    assert len(groups) == 1
    assert {f['path'] for f in groups[0]['files']} == {p.name for p in [a] + siblings}


def test_replace_failure_preserves_target(tmp_path):
    a, siblings = files(tmp_path)
    script = generate(tmp_path)
    before = {p: p.stat().st_ino for p in [a] + siblings}
    result = run_script(tmp_path, script, {'mv': 'exit 1\n'})
    assert result.returncode == 1
    assert before == {p: p.stat().st_ino for p in before}
    assert not list(tmp_path.glob('.auditorr-dedupe-*'))


def test_partial_failure_then_resume(tmp_path):
    a, siblings = files(tmp_path)
    results, _ = snapshot(tmp_path)
    results['torrent_files'].sort(key=lambda f: f['path'] != 'A')
    script = scripts.generate_script('dedupe', results, {'LOCAL_PATH': str(tmp_path)})
    marker = shlex.quote(str(tmp_path / 'link-attempted'))
    fail_second = (f'if [ -e {marker} ]; then exit 1; fi\n'
                   f': > {marker}\n'
                   f'exec {shlex.quote(shutil.which("ln"))} "$@"\n')
    result = run_script(tmp_path, script, {'ln': fail_second})
    assert result.returncode == 1
    assert 'Paths linked: 1' in result.stdout
    assert 'Reclaimed bytes (logical): 0' in result.stdout
    result = run_script(tmp_path, script)
    assert result.returncode == 0
    assert 'Paths linked: 1' in result.stdout
    assert 'Reclaimed bytes (logical): 10' in result.stdout
    assert len({p.stat().st_ino for p in [a] + siblings}) == 1


def test_torrent_and_media_siblings_with_excluded_representative(tmp_path):
    torrents = tmp_path / 'torrents'
    media = tmp_path / 'media'
    torrents.mkdir()
    media.mkdir()
    a, siblings = files(torrents)
    library = media / 'Library'
    os.link(siblings[0], library)
    inode_map = {}
    torrent_order, _, _, _ = audit._walk_directory(
        str(torrents), 'Torrent', inode_map, {}, 0, 0,
        exclusion_patterns=['contains:Luminarr'])
    media_order, _, _, _ = audit._walk_directory(
        str(media), 'Media', inode_map, {}, 0, 0)
    # Model whichever excluded alias the scan encounters first.
    info = inode_map[(library.stat().st_dev, library.stat().st_ino)]
    info['torrent_rel_path'] = 'Luminarr'
    info['torrent_excluded'] = True
    duplicate_map = audit._build_duplicate_map(inode_map)
    torrent_files, media_files = audit._assemble_records(
        torrent_order, media_order, inode_map, duplicate_map)
    torrent_files.sort(key=lambda f: f['path'] != 'A')
    script = scripts.generate_script('dedupe',
        {'torrent_files': torrent_files, 'media_files': media_files},
        {'LOCAL_PATH': str(torrents), 'MEDIA_PATH': str(media)})
    old_inode = siblings[0].stat().st_ino
    result = run_script(tmp_path, script)
    assert result.returncode == 0
    assert a.stat().st_ino == library.stat().st_ino == siblings[1].stat().st_ino
    assert siblings[0].stat().st_ino == old_inode
    assert 'Reclaimed bytes (logical): 0' in result.stdout


def test_runtime_rechecks_device(tmp_path):
    a, siblings = files(tmp_path)
    results, _ = snapshot(tmp_path)
    results['torrent_files'].sort(key=lambda f: f['path'] != 'A')
    script = scripts.generate_script('dedupe', results, {'LOCAL_PATH': str(tmp_path)})
    # Simulate a device change through the stat command used on the host.
    real_stat = shlex.quote(shutil.which('stat'))
    remote_stat = (
        'if [[ "$*" == *"%d:%i"* && ( "${@: -1}" == ./Luminarr || "${@: -1}" == "./Darkpeers 0" ) ]]; then\n'
        f'  value=$({real_stat} "$@") || exit $?\n'
        '  printf "999999:%s\\n" "${value#*:}"\n'
        'else\n'
        f'  exec {real_stat} "$@"\n'
        'fi\n')
    result = run_script(tmp_path, script, {'stat': remote_stat})
    assert result.returncode == 0, result.stderr
    assert 'SKIP: different filesystem' in result.stdout
    assert a.stat().st_ino != siblings[0].stat().st_ino
    assert 'Reclaimed bytes (logical): 0' in result.stdout


@pytest.mark.parametrize('when', ['before', 'after'])
@pytest.mark.parametrize('signal', ['TERM', 'KILL'])
def test_interrupted_replace_keeps_every_path_and_can_resume(tmp_path, when, signal):
    a, siblings = files(tmp_path)
    results, _ = snapshot(tmp_path)
    results['torrent_files'].sort(key=lambda f: f['path'] != 'A')
    script = scripts.generate_script('dedupe', results, {'LOCAL_PATH': str(tmp_path)})
    real_mv = shlex.quote(shutil.which('mv'))
    interrupt = f'kill -{signal} "$PPID"\nexit 143\n'
    if when == 'after':
        interrupt = f'{real_mv} "$@" || exit $?\n' + interrupt
    result = run_script(tmp_path, script, {'mv': interrupt})
    assert result.returncode != 0
    assert all(p.is_file() and p.read_bytes() == b'same bytes' for p in [a] + siblings)
    if signal == 'TERM':
        assert not list(tmp_path.glob('.auditorr-dedupe-*'))
    result = run_script(tmp_path, script)
    assert result.returncode == 0, result.stderr
    assert len({p.stat().st_ino for p in [a] + siblings}) == 1
    assert 'Reclaimed bytes (logical): 10' in result.stdout


def test_compare_io_error_is_an_error_not_a_mismatch(tmp_path):
    a, siblings = files(tmp_path)
    script = generate(tmp_path)
    before = {p: p.stat().st_ino for p in [a] + siblings}
    result = run_script(tmp_path, script, {'cmp': 'exit 2\n'})
    assert result.returncode == 1
    assert 'Comparison failed' in result.stderr
    assert 'Copies skipped: 0' in result.stdout
    assert 'Reclaimed bytes (logical): 0' in result.stdout
    assert before == {p: p.stat().st_ino for p in before}


def test_quoted_and_option_like_paths(tmp_path):
    a = tmp_path / "-keep 'quoted'"
    b = tmp_path / 'copy\n$(false)'
    c = tmp_path / '-second link'
    a.write_bytes(b'hello')
    b.write_bytes(a.read_bytes())
    os.link(b, c)
    script = generate(tmp_path)
    assert 'python' not in script.lower()
    result = run_script(tmp_path, script)
    assert result.returncode == 0, result.stderr
    assert len({p.stat().st_ino for p in (a, b, c)}) == 1
    assert 'Reclaimed bytes (logical): 5' in result.stdout


def test_full_compare_rejects_sample_hash_collision(tmp_path):
    a = tmp_path / 'A'
    b = tmp_path / 'B'
    edge = b'x' * 65536
    a.write_bytes(edge + b'a' + edge)
    b.write_bytes(edge + b'b' + edge)
    results, _ = snapshot(tmp_path)
    assert all(f['duplicate_paths'] for f in results['torrent_files'])
    result = run_script(tmp_path, scripts.generate_script(
        'dedupe', results, {'LOCAL_PATH': str(tmp_path)}))
    assert result.returncode == 0, result.stderr
    assert 'SKIP: files differ' in result.stdout
    assert a.stat().st_ino != b.stat().st_ino
    assert 'Reclaimed bytes (logical): 0' in result.stdout


def test_more_copies_than_map_cap_are_merged(tmp_path):
    copies = [tmp_path / f'copy {i}' for i in range(13)]
    for path in copies:
        path.write_bytes(b'hello')
    script = generate(tmp_path)
    result = run_script(tmp_path, script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count('Verifying:') == 12
    assert 'Paths linked: 12' in result.stdout
    assert 'Reclaimed bytes (logical): 60' in result.stdout
    assert len({p.stat().st_ino for p in copies}) == 1
