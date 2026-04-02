import threading

_state_lock = threading.Lock()

audit_state = {
    "is_scanning":      False,
    "progress":         0,
    "last_audit_time":  "Never",
    "total_files":      0,
    "scanned_files":    0,
    "status_message":   "",
    "last_scan_status": "never",   # "ok" | "error" | "never"
    "trigger":          "startup",
    "next_scan_in":     None,
    "phase":            "idle",
}


def get_state():
    with _state_lock:
        return dict(audit_state)


def set_state(**kwargs):
    with _state_lock:
        audit_state.update(kwargs)


def update_progress(scanned, total):
    with _state_lock:
        audit_state["scanned_files"] = scanned
        audit_state["progress"] = min(100, int((scanned / total) * 100)) if total > 0 else 0


def try_start_scanning(trigger):
    """Atomically check-and-set is_scanning. Returns True if the scan was successfully
    claimed (was False before); False if an audit is already running. Setting the trigger
    in the same lock acquisition eliminates the TOCTOU race between the guard check and
    the subsequent Thread().start() call at every call site.
    """
    with _state_lock:
        if audit_state["is_scanning"]:
            return False
        audit_state["is_scanning"] = True
        audit_state["trigger"] = trigger
        return True
