import os
import time
import threading
import logging
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from db import db_load_config
from state import get_state, set_state, try_start_scanning
from audit import run_audit_process

log = logging.getLogger(__name__)

_observer = None


class AuditDebounceHandler(FileSystemEventHandler):
    def __init__(self, cooldown_fn):
        super().__init__()
        self._cooldown_fn = cooldown_fn
        self._timer       = None
        self._lock        = threading.Lock()

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
        if try_start_scanning("watchdog"):
            log.info("Watchdog: cooldown elapsed, triggering audit.")
            threading.Thread(target=run_audit_process, args=("watchdog",), daemon=True).start()

    def on_created(self, event): self._reset_timer()
    def on_deleted(self, event): self._reset_timer()
    def on_moved(self,   event): self._reset_timer()


def start_watchdog():
    global _observer
    cfg   = db_load_config()
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
        if elapsed >= interval_minutes and try_start_scanning("scheduled"):
            log.info(f"Scheduled audit: {elapsed:.0f}m since last run, triggering.")
            threading.Thread(target=run_audit_process, args=("scheduled",), daemon=True).start()
