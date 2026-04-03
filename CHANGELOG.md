# Changelog
## v1.3.3 — 2026-04-02

### Performance
- **Per-thread qBittorrent login** — worker threads now log in once per thread via `threading.local()` instead of once per torrent. On large libraries with thousands of torrents this was causing 10–16 minute scan startup times; logins are now capped at 16 (one per worker) regardless of library size.

### Bug Fixes
- **Informative test connection errors** — `/api/test_connection` now returns distinct messages for the three real failure modes: wrong credentials (login failed), unreachable host (connection refused / DNS failure), and timeout. Previously all failures returned a raw library exception string. Successful connections now also return the qBittorrent version.

## v1.3.2 — 2026-04-01

### Performance & Large Library Improvements
- **Scan progress card** — floating card (max-width 860px, bottom-right) replaces the small sidebar progress bar during active scans. Shows two phase bars (Torrents / Disk) with live status labels, a file counter, and a status message line. Automatically dismisses when results finish loading.
- **Results loading state** — card stays visible after the scan completes while results are being fetched, showing an animated indeterminate bar with an ease-out fill that approaches 85% over ~8 seconds then snaps to 100% on completion.
- **Faster progress polling** — poll rate drops to 500ms while a scan is active, returning to 5s at idle.
- **Eliminated count_files pre-pass** — file total is now accumulated incrementally during the directory walk rather than via a separate full `os.walk` pre-pass. Removes a redundant full directory traversal on every scan, improving startup time on large libraries.
- **Phase tracking** — audit state now includes a `phase` field (`connecting` / `torrents` / `disk` / `post` / `idle`) exposed via `/api/progress`, driving the two-phase card UI.
- **Gunicorn worker timeout** — bumped from 120s to 300s to accommodate large result payload serialization on libraries with many files.

## v1.3.1 — 2026-03-31

### Bug Fixes
- **Config test connection** — fixed `InvalidURL: No host supplied` error shown on page reload or first setup before saving config.

## v1.3.0 — 2026-03-28

### Setup & Onboarding
- **Setup wizard** — 3-step wizard shown on first launch when no config exists. Step 1: qBittorrent connection with post-test info card. Step 2: data paths with container filesystem browser and "Fetch from qBittorrent" save-path button. Step 3: Sonarr/Radarr. An early-start button on Step 2 lets users trigger a first audit before completing optional integrations.

### qBittorrent
- **Connection info card** — after a successful test connection, shows qBittorrent version, torrent count, and total seeding size inline in the qBit card
- **Save path auto-fetch** — "Fetch from qBittorrent" button computes the common prefix of the first 50 torrent save paths and fills the qBit Save Path field
- **Thread-safe connection timeout** — `test_connection` route uses `threading.Thread` + `t.join(timeout=10)` so a hanging qBit instance cannot block the gunicorn worker indefinitely; `_fetch_qbit_file_map` wraps the full scan in `socket.setdefaulttimeout(30)` / `finally: socket.setdefaulttimeout(None)`
- **`/api/qbit_save_path`** — POST endpoint now returns `version`, `torrent_count`, and `seeding_size` alongside `save_path`, enabling the wizard to populate the info card from a single credentials-in-body call

### Config
- **Inline test buttons** — Test Connection moved into the qBittorrent card; Media Path and Local Torrent Path each show per-field ✓/✗ feedback after Test Paths
- **Unsaved changes indicator** — amber dot on the Save Settings button appears whenever a field has been modified since last save; clears on successful save
- **Path warnings persistence** — path warnings from config save are stored in `localStorage` and survive page reload
- **Container filesystem browser** — collapsible `/data` directory browser in the Path Mappings card with click-to-fill for Media Path and Local Torrent Path

### Dashboard & Explorer
- **Flat/tree view toggle** — ⊟ Tree / ⊞ Flat segmented control in the file explorer toolbar, persisted to `localStorage`; search and path-reveal force flat mode regardless of toggle state
- **Size sort** — Sort: Name | Size inline toggle in flat mode; sorts by file size descending
- **Hardlinks/dupes custom popover** — replaces native browser title tooltip with a styled `position: fixed` overlay below the filename, showing HARDLINKS and DUPLICATES sections with horizontal viewport clamping and word-break paths
- **Changes panel collapse** — ▶/▼ toggle in the changes panel header collapses the category list; state persisted to `localStorage` as `auditorr_changes_collapsed`

### Bug Fixes
- **Title parser** — `\s+` anchor in `_parse_title_from_filename` prevents quality tags from matching mid-word (e.g. `Internal` in "Internal Affairs" is no longer stripped; `INTERNAL` only matches as a standalone token)
- **Path boundary substitution** — `arr_rescan` boundary check `abs_path[len(local_path):][:1] in ('/', '')` prevents `/data/media-extra` from being treated as a sub-path of `/data/media`
- **Infinite 401 retry loop** — `api.js` `req()`/`reqText()` now pass a `retried` flag to prevent prompting for the secret key on every request after a second failure
- **Hook ordering violation** — `computeCrossSeedStats` `useMemo` in `Dashboard.jsx` moved before the `if (!data)` early return
- **Notification API guard** — `'Notification' in window` check added in `App.jsx` before all `Notification` calls, fixing crashes in browsers that don't implement the API
- **Delete script abort** — `set -euo pipefail` removed from the orphaned torrents delete script; per-file existence checks handle errors without aborting on first missing file

### Tests
- **pytest suite** — `tests/` directory with 71 tests covering `compute_diff`, `_parse_title_from_filename`, `process_health_metrics`, `validate_config`, `generate_script`, `_normalize_title`, and `arr_rescan` path boundary substitution. No external dependencies beyond `requirements.txt`; all qBittorrent and SQLite I/O mocked with `unittest.mock`.
- **xfail tests** — 2 tests document the known `4K`-in-title regex bug (`strict=True` so they will automatically surface as passes once fixed)

# v1.2.1 — 2026-03-25

### Security
- **Mask Sonarr/Radarr API keys** — `SONARR_API_KEY` and `RADARR_API_KEY` are now masked as `__stored__` in the `/api/config` GET response, consistent with existing `QB_PASS` behaviour. Keys are preserved on save when the masked placeholder is submitted.

## v1.2.0 — 2026-03-24

### Backend
- **Modular refactor** — `app.py` split into `audit.py`, `arr.py`, `db.py`, `scripts.py`, `state.py`, and `watchdog_handler.py`. Routes remain in `app.py`. Strict module layering eliminates circular imports.
- **SQLite persistence for all data** — `results.json` and `history.json` migrated to SQLite with automatic startup migration. Config migrated from `config.json` to a `config` table; all modules read config directly from the database with no in-memory cache or global lock.
- **Config validation** — `validate_config()` in `db.py` enforces integer minimums, float ranges for health thresholds, and list constraints on exclusion patterns. Called before every config save.
- **Per-tracker upload snapshots** — each successful audit snapshots cumulative uploaded bytes and seeding size per tracker. Stored in a new `upload_snapshots` table (capped at 1000 rows). Deltas between snapshots produce upload-per-day stats and yield metrics.
- **Yield metrics** — `compute_upload_stats()` computes upload deltas and yield per tracker. Yield = total uploaded / seeding size over a rolling window. Counter resets (qBit restarts) are detected and skipped. A lightweight `yield_summary` key is appended to `/api/results` after each audit.
- **TOCTOU fix on scan state** — `try_start_scanning(trigger)` atomically checks and sets `is_scanning` in a single lock acquisition, eliminating the race across all trigger paths (manual, watchdog, scheduled, startup).
- **Shell injection fix** — all path interpolations in generated bash scripts now use `shlex.quote()`. `echo` with interpolated paths replaced with `printf '%s'`.
- **WAL mode and foreign keys** — SQLite connections now enable WAL journal mode and foreign key enforcement on every connection.

### Frontend
- **Disk size as primary metric** — dashboard metric cards show total size as the headline value rather than file count.
- **Aligned button rows** — action button rows in metric cards use consistent height so buttons align across all four cards regardless of how many are visible.

### Scripts
- **Relative paths in generated scripts** — delete and dedupe scripts now use paths relative to the torrent/data directory instead of absolute container paths. Both scripts include a usage header and a working-directory guard that verifies the first file exists before proceeding.

## v1.1.0

### New Features
- **Sonarr/Radarr integration** — configure URLs and API keys in Config. Orphaned media files in the Media explorer show "Open in Sonarr" or "Open in Radarr" pill buttons that deep-link directly to the correct series/movie page for interactive search. Button shown is determined automatically from the file path (tv/television folders → Sonarr, movie folders → Radarr).
- **Dashboard action buttons** — each metric card now has inline action buttons. Orphaned Torrents: Generate Delete Script. Not Imported: Trigger Sonarr Rescan / Trigger Radarr Rescan. Duplicate Files: Generate Dedupe Script. Hardlinked Media: View Orphaned Media.
- **Delete script** — generates a reviewed bash script for orphaned torrent cleanup with per-file progress output, pre/post disk space measurement, and actual vs expected space freed comparison.
- **Dedupe script** — generates a bash script that replaces duplicate files with hardlinks. Runs full md5sum verification on each file pair before hardlinking. Includes progress output and skips cross-filesystem groups that cannot be hardlinked.
- **Light mode** — toggle in Config, persisted in localStorage.
- **Exclusion patterns** — glob-based patterns in Config exclude files from health scoring while keeping them visible in the explorer with an "excluded" tag.
- **Rescan on config save** — saving settings triggers an immediate background audit.
- **Sonarr/Radarr remote path config** — separate path translation for Sonarr and Radarr containers that may see torrent paths differently than auditorr.

### Backend
- **Parallelized tracker fetching** — tracker API calls now run concurrently with ThreadPoolExecutor (16 workers), eliminating sequential per-torrent qBittorrent API calls and significantly reducing scan time on large libraries.
- **Torrent hash stored per file** — hash is now captured from qBittorrent and stored in results, enabling future deep-link and per-torrent features.
- **Normalized title matching** — Sonarr/Radarr library search strips punctuation before comparing, fixing matches for titles with colons, dashes, and other special characters.
- **Rescan path translation** — Sonarr/Radarr rescan commands translate auditorr-local paths to the correct path as seen inside the arr container.
- **set -euo pipefail removed from delete script** — per-file existence checks handle errors gracefully without aborting the entire script on first failure.

### Frontend
- **Script modal** — full-screen overlay for reviewing generated bash scripts before running. Shows warning banner, scrollable monospace script, copy to clipboard, and download as .sh.
- **Smart Sonarr/Radarr button visibility** — buttons only shown for relevant file types based on path detection, not on every orphaned file.
- **Action button rows** — consistent two-row button layout across all dashboard cards, aligned left-to-right.
- **Recoverable size** — orphaned torrents card shows total recoverable GB in the same style as duplicate files.

## v1.0.1

### New Features
- **Exclusion patterns** — glob-based exclusions configurable in Config → Exclusion Patterns. One pattern per line. Matched files are excluded from all health scoring (orphaned, duplicate, not-imported) but remain visible in the file explorer with an "excluded" tag. Supports standard globs: `*.srt`, `@eaDir`, `Featurettes`.
- **Rescan on config save** — saving settings now automatically triggers a background audit so changes take effect immediately without manually running a scan.

## v1.0.0 — Initial Release

### Features
- Library health score (0–100) with color-coded arc dial
- Score history chart with smart trend delta (vs yesterday or last week)
- Cross-seed effectiveness panel — weighted average seed multiplier, segmented disk bar, tracker leaderboard
- File explorer with tree view for media and torrent directories
- Filters: status, import state, tracker include/exclude, seed count, filename search, size range
- "What changed" panel — diff between last two scans showing newly orphaned, imported, duplicate, and removed files
- Threshold alerts on dashboard when categories significantly exceed configured limits
- Audit history table in Config — last 50 runs with time, trigger, score, and status
- Light and dark mode — toggle in Config, persisted in localStorage
- Hash-based URL routing — tabs are bookmarkable, browser back/forward works
- Copy Paths button — copies all filtered file paths to clipboard
- Per-row path copy icon on every file row
- Export CSV for any filtered view

### Backend
- Flask + gunicorn (single worker, 120s timeout)
- SQLite persistence — every audit stored with full snapshot, survives restarts
- Watchdog filesystem observer — debounced inotify-based audit triggering
- Scheduled fallback audit — configurable interval, catches missed watchdog events on NFS/bind mounts
- O(n) duplicate detection — group by size → group by inode → hash one representative per inode
- Atomic file writes with cross-filesystem fallback (handles Docker volume mounts)
- Startup lock — prevents duplicate audits on container start
- Optional API authentication via AUDITORR_SECRET env var
- Health recomputed immediately on config save — no rescan needed to see threshold changes
- Config path validation — warns if container paths don't exist without blocking save
- /health endpoint for Docker/Traefik healthchecks

### Configuration
- qBittorrent connection (host, user, password)
- Path mappings with remote→local path translation for split-container setups
- Watchdog cooldown and scheduled interval
- Per-category health thresholds (orphaned, not imported, duplicates)
- Default port: 8677 (t=8, o=6, r=7, r=7 on a phone keypad)
