"""Every test session gets its own data directory.

`db.DATA_DIR` defaults to `/app/data`, the container's volume, and `app.py` calls
`init_db()` at import — so collecting any test module that imports `app` created
that directory and a real `auditorr.db` in it. On the dev machine that was
`C:\\app\\data`, silently, on every run. On a Linux CI runner, which runs as a
non-root user, `/app` cannot be created at all, and every such module would
error at collection (found writing Phase 14's CI gate, before its first run).

Set here, before any test module imports `db`, and **unconditionally**: a
developer's own `DATA_DIR` pointing at a real install must never be the one the
suite writes to. Tests that need a database of their own still patch
`db.DATA_DIR` / `db.DB_FILE` to a file under `tmp_path`, as before.
"""
import atexit
import os
import shutil
import tempfile

_DATA_DIR = tempfile.mkdtemp(prefix='auditorr-tests-')
os.environ['DATA_DIR'] = _DATA_DIR
atexit.register(shutil.rmtree, _DATA_DIR, ignore_errors=True)
