import time
import threading

_state_lock = threading.Lock()

audit_state = {
    "is_scanning":           False,
    "progress":              0,
    "last_audit_time":       "Never",
    "total_files":           0,
    "scanned_files":         0,
    "status_message":        "",
    "last_scan_status":      "never",   # "ok" | "error" | "never"
    "trigger":               "startup",
    "next_scan_in":          None,
    "phase":                 "idle",
    "last_scan_completed_at": 0.0,
    "last_scan_duration":    0.0,
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


# ---------------------------------------------------------------------------
# Workflow activity signal — lets background scans (watchdog / scheduled)
# defer while someone is actively using a workflow page, so scan RSS and
# workflow-request RSS don't stack. Manual scans ignore this: explicit intent.
# ---------------------------------------------------------------------------

# Grace after the last workflow request before background scans may run.
# Workflow usage is bursty (page load, then verify batches, then a delete) —
# in-flight-only checking would let a scan sneak into the gaps.
WORKFLOW_GRACE_S = 90

_workflow_inflight      = 0
_workflow_last_activity = 0.0


def note_workflow_request_start():
    global _workflow_inflight, _workflow_last_activity
    with _state_lock:
        _workflow_inflight += 1
        _workflow_last_activity = time.time()


def note_workflow_request_end():
    global _workflow_inflight, _workflow_last_activity
    with _state_lock:
        _workflow_inflight = max(0, _workflow_inflight - 1)
        _workflow_last_activity = time.time()


def workflow_active(grace_s=WORKFLOW_GRACE_S):
    """True while a workflow request is in flight or one ended < grace_s ago."""
    with _state_lock:
        if _workflow_inflight > 0:
            return True
        return (time.time() - _workflow_last_activity) < grace_s
