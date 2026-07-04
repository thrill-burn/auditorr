import os
import time
import threading
import logging
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from db import db_load_config
from state import get_state, set_state, try_start_scanning, workflow_active
from audit import run_audit_process

log = logging.getLogger(__name__)

_observer = None

# Background scans defer while a workflow session is active (scan RSS and
# workflow-request RSS stack — the 751MB field peak was a watchdog scan under
# a Triage session). Re-check every minute, but never hold a triggered audit
# back more than 10 minutes so a long session can't starve scan freshness.
_WORKFLOW_DEFER_RETRY_S = 60
_MAX_WORKFLOW_DEFER_S   = 600


class AuditDebounceHandler(FileSystemEventHandler):
    def __init__(self, cooldown_fn):
        super().__init__()
        self._cooldown_fn    = cooldown_fn
        self._timer          = None
        self._lock           = threading.Lock()
        self._deferred_since = None

    def _reset_timer(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            cooldown = self._cooldown_fn()
            set_state(next_scan_in=cooldown)
            self._timer = threading.Timer(cooldown, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        set_state(next_scan_in=None)
        state = get_state()
        cooldown = self._cooldown_fn()
        last_ended   = state.get('last_scan_completed_at', 0.0)
        last_duration = state.get('last_scan_duration', 0.0)
        if last_ended:
            # Don't re-trigger immediately from events that fired during the scan —
            # suppress for max(cooldown, scan_duration) seconds after the scan ended.
            suppression = max(cooldown, last_duration)
            elapsed = time.time() - last_ended
            if elapsed < suppression:
                remaining = suppression - elapsed
                log.info(f"Watchdog: post-scan suppression, rescheduling in {remaining:.0f}s.")
                with self._lock:
                    self._timer = threading.Timer(remaining, self._fire)
                    self._timer.daemon = True
                    self._timer.start()
                set_state(next_scan_in=int(remaining))
                return
        # Someone is using a workflow page — defer so the scan doesn't stack
        # its memory peak on top of the session's (and so the FS events from a
        # session's deletions coalesce into one audit once the user is done).
        if workflow_active():
            with self._lock:
                if self._deferred_since is None:
                    self._deferred_since = time.time()
                if time.time() - self._deferred_since < _MAX_WORKFLOW_DEFER_S:
                    log.info("Watchdog: workflow in use, deferring audit %ds.",
                             _WORKFLOW_DEFER_RETRY_S)
                    self._timer = threading.Timer(_WORKFLOW_DEFER_RETRY_S, self._fire)
                    self._timer.daemon = True
                    self._timer.start()
                    set_state(next_scan_in=_WORKFLOW_DEFER_RETRY_S)
                    return
                log.info("Watchdog: max workflow deferral (%ds) reached, scanning anyway.",
                         _MAX_WORKFLOW_DEFER_S)
        with self._lock:
            self._deferred_since = None
        if try_start_scanning("watchdog"):
            log.info("Watchdog: cooldown elapsed, triggering audit.")
            threading.Thread(target=run_audit_process, args=("watchdog",), daemon=True).start()

    def on_created(self, event): self._reset_timer()
    def on_deleted(self, event): self._reset_timer()
    def on_moved(self,   event): self._reset_timer()


def start_watchdog():
    global _observer
    cfg   = db_load_config()
    if not cfg.get('WATCHDOG_ENABLED', True):
        log.info("Watchdog: disabled in config, not starting.")
        return
    paths = {p for p in {cfg.get('LOCAL_PATH',''), cfg.get('MEDIA_PATH','')} if p and os.path.exists(p)}
    if not paths:
        log.warning("Watchdog: no valid paths to watch.")
        return

    def get_cooldown():
        return int(db_load_config().get('WATCHDOG_COOLDOWN', 60))

    handler   = AuditDebounceHandler(get_cooldown)
    _observer = Observer()
    for path in paths:
        _observer.schedule(handler, path, recursive=True)
        log.info(f"Watchdog: watching {path}")
    _observer.start()


def restart_watchdog():
    global _observer
    if _observer:
        _observer.stop()
        _observer.join()
    start_watchdog()


def _scheduled_audit_loop():
    defer_logged = False
    while True:
        time.sleep(60)
        cfg              = db_load_config()
        interval_minutes = int(cfg.get('SCHEDULED_INTERVAL', 360))
        last             = get_state().get('last_audit_time', 'Never')
        if last == 'Never':
            continue
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        elapsed = (datetime.now() - last_dt).total_seconds() / 60
        if elapsed < interval_minutes:
            continue
        # Due — but wait for any active workflow session to go quiet first.
        # The loop re-checks every minute; no cap needed because the activity
        # signal lapses 90s after the last real workflow request (polls are
        # excluded), and the interval is measured in hours.
        if workflow_active():
            if not defer_logged:
                log.info("Scheduled audit due, but a workflow is in use — deferring until quiet.")
                defer_logged = True
            continue
        if try_start_scanning("scheduled"):
            defer_logged = False
            log.info(f"Scheduled audit: {elapsed:.0f}m since last run, triggering.")
            threading.Thread(target=run_audit_process, args=("scheduled",), daemon=True).start()
