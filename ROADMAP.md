# auditorr Roadmap

## v1.2 — Tracker Yield & Upload Analytics

### Philosophy
Surface upload analytics that qBittorrent has but doesn't present in a useful way. Track upload generated per GB of disk space per tracker — like a yield or interest rate — so users can understand which trackers are actually worth seeding on.

### Shipped in v1.2.0 ✅
- **Backend refactor** — modular structure (`audit.py`, `arr.py`, `db.py`, `scripts.py`, `state.py`, `watchdog_handler.py`)
- **SQLite migration** — `results.json`, `history.json`, and `config.json` all migrated to SQLite with automatic startup migration
- **Config validation** — enforced before every save; integer minimums, float ranges, list constraints
- **Relative paths in generated scripts** — delete and dedupe scripts use paths relative to the torrent/data directory; includes usage header and working-directory guard
- **Dashboard UI** — disk size as primary metric card value; action button rows aligned across all cards
- **Per-tracker upload snapshots** — cumulative uploaded bytes and seeding size captured per tracker on every successful audit, stored in `upload_snapshots` table (capped at 1000 rows)
- **Yield metrics backend** — `compute_upload_stats()` computes upload deltas, daily buckets, and yield per tracker; counter-reset detection; `yield_summary` in `/api/results`; `/api/upload_stats` and `/api/upload_snapshots` endpoints
- **Upload activity chart** — stacked bar chart on the dashboard showing daily upload volume per tracker
- **Library yield panel** — Upload/Yield tab switcher; hero yield %; per-tracker upload and yield tables; shared control bar with day-range pills and tracker filter dropdown persisted to localStorage
- **Trackers page** — dedicated route with per-tracker cards, sort by name/seeding size/uploaded/yield, tracker include/exclude filter
- **TrackerCard** — extracted from TrackerDetailModal, exported from Dashboard.jsx; modal is now a thin wrapper

---

## v1.3 — Setup & UX Polish

### Philosophy
Reduce time-to-first-scan for new users and surface qBittorrent metadata that was already available but not shown. Improve daily-driver UX with smarter view persistence and better inline feedback.

### Shipped in v1.3.3 ✅
- Per-thread qBittorrent login via `threading.local()` — 16 logins max regardless of library size
- Informative test connection errors (distinct messages for bad credentials, unreachable host, and timeout)
- Scan progress card (floating, phase-aware two-bar layout, auto-dismisses)
- Results loading state (card stays visible post-scan during fetchResults, indeterminate bar)
- 500ms active poll rate (drops from 5s while is_scanning is true)
- Eliminated count_files pre-pass (file total accumulated incrementally during walk)
- Phase tracking (`phase` field in audit state: connecting / torrents / disk / post / idle)
- Gunicorn timeout bump (120s → 300s)

### Shipped in v1.3.0 ✅
- **Setup wizard** — 3-step first-run wizard (qBittorrent connection → data paths → Sonarr/Radarr); early-start button on Step 2 to trigger a first audit before optional integrations are complete
- **qBittorrent connection info card** — post-test display of qBittorrent version, torrent count, and seeding size in both Config and wizard Step 1
- **Save path auto-fetch** — "Fetch from qBittorrent" computes the common prefix of the first 50 torrent save paths; `/api/qbit_save_path` now returns `version`, `torrent_count`, `seeding_size` alongside `save_path`
- **Thread-safe connection timeout** — `test_connection` uses `threading.Thread` + `t.join(timeout=10)`; `_fetch_qbit_file_map` wraps full scan in `socket.setdefaulttimeout(30)` / `finally: socket.setdefaulttimeout(None)`
- **Container filesystem browser** — `/api/browse_data` lists `/data` subdirs; collapsible browser in Config and always-expanded in wizard Step 2 with click-to-fill for Media Path and Local Torrent Path
- **Inline test buttons** — Test Connection inside the qBit card; per-field ✓/✗ path feedback after Test Paths
- **Unsaved changes indicator** — amber dot on Save Settings button when fields are dirty
- **Path warnings persistence** — saved to localStorage, survive page reload
- **Flat/tree view toggle** — ⊟ Tree / ⊞ Flat persisted to localStorage; search and path-reveal force flat mode
- **Size sort** — Sort: Name | Size toggle in flat mode; sorts by file size descending
- **Hardlinks/dupes custom popover** — replaces native title tooltip; `position: fixed` overlay below filename with viewport clamping and word-break paths
- **Changes panel collapse** — ▶/▼ toggle persisted to localStorage as `auditorr_changes_collapsed`
- **Title parser fix** — `\s+` anchor prevents quality tags from matching mid-word (e.g. "Internal Affairs" no longer truncated)
- **Path boundary fix** — `arr_rescan` boundary check prevents `/data/media-extra` matching `/data/media` prefix
- **Bug fixes** — infinite 401 retry loop in `api.js`; `useMemo` hook ordering in Dashboard; `Notification` API guard; `set -euo pipefail` removed from delete script
- **pytest suite** — 71 tests in `tests/` covering core audit logic, config validation, script generation, title parsing, and path boundary substitution; all I/O mocked

---

## v1.4 — Future Ideas

- **Webhook / notification support** — alert when health score drops below threshold (Discord, ntfy.sh, Gotify)
- **Per-tracker import success rate** — of files downloaded from each tracker, what % got imported by Sonarr/Radarr
- **Lidarr / Readarr support** — extend `_SERVICE_MAP` in `arr.py` with Lidarr/Readarr endpoints
- **One-click hardlink repair** — for orphaned media with a matching torrent file, create the missing hardlink directly from the UI
- **Unraid Community Applications template** — publish a CA template for one-click install
- **Title parser 4K fix** — `4K` token in the quality-tag regex strips mid-title occurrences (e.g. "The 4K Experience"); needs a lookahead or post-tag word boundary (2 xfail tests already cover this)
- **Export to arr** — bulk-add orphaned media files to Sonarr/Radarr monitored list from the file explorer
- **Torrent re-announce** — trigger a re-announce via qBittorrent API for all seeding torrents on a selected tracker directly from the Trackers page
- **Multi-instance qBittorrent** — support scanning from more than one qBittorrent instance and merging results
- **Score history export** — download the full score history as CSV from the Config → Audit History section
- **Dark/light mode system preference** — auto-detect `prefers-color-scheme` and default to it instead of always starting on dark
