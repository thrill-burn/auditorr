# Changelog
## v1.5.1 — 2026-05-29

### Features
- **Media server exclusion presets** — new "Media Server" section in Config → Exclusion Patterns. Select one or more presets (Plex, Jellyfin, Emby, Kodi, UMS) to automatically exclude all common sidecar files for that server: `.nfo` files, artwork images (`poster.jpg`, `folder.jpg`, `fanart*.jpg`, `*-backdrop.png`, etc.), and server-specific metadata files (`.plexmatch`, `extrafanart`). Presets are applied on top of any custom exclusion patterns and are deduplicated before the scan.
- **Arr metadata relink workflow** — new "Relink" button in the dashboard for media files that are missing hardlinks. auditorr cross-references each unlinked media file against Sonarr/Radarr metadata and unlinked torrent files, scores each candidate by size match, title token overlap, year, service/category, and seeding status, then generates a reviewed bash script (`ln` with pre-flight `cmp` verification). The script never executes automatically — you review and run it manually. Supports multi-instance Arr connections; per-connection path mapping is applied before matching.
- **Human-friendly multi-Arr settings** — Config now uses editable Sonarr/Radarr instance rows instead of a raw JSON textarea. Add Sonarr or Radarr instances, choose the service, label them, set URL/API key, and optionally map per-instance media paths while preserving the same saved `ARR_CONNECTIONS` format.

### Bug Fixes
- **qui/qBittorrent connection race on server reboot** — startup audit now retries once after a 60-second delay when the first attempt fails with a source connection error (e.g. qBittorrent or qui not yet ready on container start). The first transient failure is not persisted to audit history; if the retry succeeds, only the successful audit appears in history. If the retry also fails, that final failure is recorded. Manual, watchdog, and scheduled scan errors are unchanged.

### Performance
- **Precompiled exclusion patterns** — `compile_exclusions(patterns)` pre-classifies every pattern into typed buckets at audit startup: O(1) extension set (`ext:` rules and pure `*.<ext>` globs), O(1) exact-name set (barewords and `name:` rules), precomputed subtree prefix tuples (`/**` and trailing-`/` patterns), and precomputed variant tuples for path globs and plain-path prefix rules. The compiled matcher's `match()` does only the per-file work. For a 100-pattern list including all media-server presets, this eliminates ~100 `_norm`/`_variants` calls per file, reducing exclusion checking from the dominant scan cost to near-zero.
- **Relink candidate scan scalability** — `find_relink_candidates` now pre-builds the eligible torrent list (filtering excluded/linked/orphaned torrents and precomputing `_token_text` and the token set once per torrent) before the media loop, eliminating the O(media × torrents) redundant work. Added a `RELINK_MAX_PAIRS` guard (default 25,000,000): if the eligible pair count exceeds the cap, the scan logs a warning and returns an empty list rather than pinning the single worker. Below the cap, output is identical.
- **Parallel Sonarr episode-file fetching** — `_fetch_sonarr_media` now fans the per-series `/api/v3/episodefile?seriesId=` requests over a bounded thread pool (≤ 8 workers) instead of issuing them sequentially. For Sonarr libraries with hundreds of series this eliminates the N+1 sequential API call pattern and cuts fetch time roughly proportional to series count.

## v1.5.0 — 2026-05-22

### Performance
- **Large-library memory usage** — four changes combine to reduce peak and sustained RAM for libraries with hundreds of thousands of files. Users previously reported 8 GB of Docker memory usage after 24 hours; the root causes were all in the backend:
  - **Early release of scan intermediates** — `torrent_records`, `media_records`, `inode_map`, and `duplicate_map` are explicitly deleted after `_assemble_records()` so Python's GC can collect them before subsequent allocations. For a 500k-file library these four structures together can reach 600–700 MB.
  - **Early release of raw qBittorrent objects** — `tracker_map` and `files_map` (the raw per-torrent dicts from the qBittorrent API) are released immediately after the normalized `file_map` is built in `sources/_qbit.py`.
  - **File lists removed from `/api/results`** — `media_files` and `torrent_files` are no longer embedded in the `latest_results` SQLite row or returned by `/api/results`. A new `/api/files?tab=media|torrents` endpoint and `file_results` table serve them on demand. The frontend fetches them lazily only when the user navigates to the File Explorer or Trackers tabs. Dashboard, Sidebar, and Trackers now derive all display values from pre-computed summary stats (`tracker_file_stats`, `not_imported_paths`, `cross_seed_stats`) stored directly in `/api/results`, so a typical page load and dashboard refresh never touches the file lists at all.
  - **`audit_snapshots` no longer stores file lists** — the per-audit snapshot previously included the full `media_files` and `torrent_files` arrays (300–500 MB of JSON per row on large libraries). At the end of every successful audit these two snapshots were immediately re-read and deserialized to compute the change log diff — a 600 MB–1 GB peak that repeated on every `/api/changes` request. Snapshots now store only `dashboard` stats (a few KB per row). Diff computation uses the in-memory file data *before* overwriting `file_results`, and `/api/changes` reads the pre-computed diff from `change_log` instead of deserializing snapshots. A one-time startup migration strips file lists from any existing rows using SQLite's `json_remove` (no Python deserialization required).

### Bug Fixes
- **Media/Torrents blank page after lazy-load refactor** — fixed a React hook ordering violation in the File Explorer that could crash the app after navigating to Media or Torrents. The lazy-loaded pages first rendered a loading skeleton with no file list, then re-rendered when `/api/files` returned data; a `useState` hook below that early skeleton return caused React to see a different hook order between renders.
- **Old-result migration for lazy file storage** — if an existing `latest_results` row still contains legacy `media_files` or `torrent_files` arrays, startup/read migration now moves those arrays into the new `file_results` table before stripping them from `/api/results`. This preserves Media/Torrents data across upgrade instead of leaving the lazy file endpoint empty until the next audit.
- **Local Torrent Path hint text typo** — corrected "youor" → "your" in the Config path mappings field hint.

## v1.4.4 — 2026-05-19

### Bug Fixes
- **qui seeding size inflated N× in multi-instance setups** — tracker seeding size and upload stats are now deduplicated by torrent hash across all qui instances. In setups where qui's per-instance torrent endpoint returns all managed torrents regardless of which instance is queried, the same hash was being counted once per eligible instance, causing `seeding_size` for a tracker to read as N× the correct value (e.g. 220 TB instead of 28 TB with ~8 instances).
- **qui upload and seeding stats verified against live API** — confirmed via the live qui OpenAPI spec and actual API responses that the torrent list does include `uploaded` (cumulative bytes) despite the spec's `Torrent` schema omitting it. The existing `_norm_torrent` field mapping was correct. The seeding size deduplication via `seen_hashes` (shared across all instance calls) is confirmed to correctly prevent N× inflation in multi-instance setups where qui returns all managed torrents from every per-instance endpoint.
- **Hardlink/duplicate popover off-screen and mispositioned** — replaced the hover-triggered filename popover with a click-triggered modal. The old implementation added `window.scrollY` to a `position: fixed` coordinate (double-counting scroll offset), causing the tooltip to appear far below its anchor when the page was scrolled. The popover also disappeared when moving the mouse toward it and rendered off-screen for rows near the bottom of the viewport. The new ⓘ button next to the filename opens a centered overlay showing all hardlink and duplicate paths; close with ✕, backdrop click, or Escape.

### Improvements
- **Media / Torrents and Change Log pages fill the viewport** — all three pages now use a flex-column layout that fills the available height exactly. The virtualized file list and change log table expand to the bottom of the window rather than stopping at a fixed `calc(100vh - 360px)` offset, eliminating the blank space below the table. The "N files · X TB" footer line on the Media/Torrents pages is preserved as a flex item below the list and cannot be overwritten.
- **Scan duration tracked and displayed** — audit wall-clock time is now captured for every scan (success and error) and stored in a new `duration_seconds` column in `audit_runs`. The Config audit history table shows a Duration column (e.g. `45s`, `2m 13s`). The Audit History page annotates each entry's date with the scan time for that audit (e.g. `May 19, 12:00 · 2m 13s`). Existing rows in the database show `—` until the next audit completes.

## v1.4.3 — 2026-05-18

### Bug Fixes
- **Test Sonarr / Test Radarr always failed** — the test endpoints now fall back to the stored API key (and URL) when the frontend submits a blank value. The config GET response masks keys as `__stored__` for security; the form cleared this to an empty string, causing the test to immediately return "URL and API key are required" even with a valid saved key.

## v1.4.2 — 2026-05-18

### Improvements
- **Audit History redesigned as a flat table** — the Changes page now renders all file-level diffs across all scans as a single virtualized table (react-window) instead of per-scan accordion cards. Columns: Date, Trigger, Type, Path, Size. A sticky filter bar provides category chips, date-range presets (24h / 7d / 30d / 90d), a debounced path search input, and a CSV export button for the current filtered view.
- **New File / Removed split by side** — "New File" and "Removed" change types are now split into "New Torrent" / "New Media" and "Removed Torrent" / "Removed Media", keyed on the existing `tab` field already present in diffs. Makes it clear when both sides of a hardlink arrived or left together, rather than showing duplicate-looking entries. Applied consistently in both the Audit History table and the dashboard Changes panel.

### Performance
- **Virtual scrolling in Media / Torrents explorer** — both flat list and tree view now use `react-window` `FixedSizeList` backed by `AutoSizer`. Only the visible rows (~20–30) are mounted in the DOM regardless of library size, eliminating the primary source of slowness on large libraries. The tree view pre-computes a flat ordered array of visible rows (respecting open/closed folder state) so virtualization applies to the full expanded tree, not just root-level folders.
- **Debounced search** — the filename search input now waits 150 ms after the last keystroke before filtering, preventing per-keystroke full-array scans on large libraries.
- **Single-pass summary stats** — the Total / Seeding / Orphaned counts and sizes are now computed in one loop instead of four separate filter/reduce passes over the filtered array.
- **Stable `itemData` references** — `itemData` objects passed to `react-window` are `useMemo`'d, preventing unnecessary re-renders of all visible rows on unrelated state changes.
- **Row state isolation** — virtual list item renderers key inner row components by `node.path`, forcing a clean remount (and resetting Sonarr/Radarr button state) whenever a list slot's file changes after a filter update.

### Bug Fixes
- **Duplicate detection false positives** - file identity now uses both device and inode (`st_dev`, `st_ino`) instead of inode alone. This prevents unrelated same-size files on different mounts or filesystems from being grouped together when their raw inode numbers collide, reducing false duplicate flags for small files and cross-device libraries. Dedupe script grouping and duplicate health counts now use the same device-aware identity.

## v1.4.1 — 2026-05-16

### Features
- **Change Log page** — new "Changes" sidebar page with a persistent, browsable history of file-level diffs between consecutive audits. Each entry shows timestamp, trigger, health score, and score delta, with expandable category chips (Became Orphaned / New Duplicates / Newly Imported / Duplicates Resolved / New Files / Removed Files) and file lists. A filter bar lets you narrow to entries containing a specific category. Diffs are stored in a new `change_log` SQLite table, pruned to 90 days, and computed automatically at the end of every successful audit.
- **Watchdog enable/disable toggle** — new "Filesystem Watchdog" toggle in Config → Watchdog & Scheduled Audits. Enabled by default; disable for large libraries with frequent downloads to prevent constant rescans. The cooldown and interval fields dim and become non-interactive when the watchdog is disabled.

### Bug Fixes
- **Startup audit skipped when unconfigured** — auditorr no longer fires an audit on startup when the configured torrent source has no host set (fresh install or new container). This prevented a spurious `qBittorrent error` from appearing in the UI before the user had completed setup. The first audit now starts correctly after step 2 of the setup wizard is submitted, using the source the user actually selected.
- **Scan progress panel dismissible** — the scanning card (bottom-right) can now be dismissed by clicking anywhere outside it or pressing the × button. The panel reappears automatically when the next scan starts.

## v1.4.0 — 2026-05-12

### Features
- **qui torrent source** — auditorr can now connect to [qui](https://github.com/autobrr/qui) as an alternative to a direct qBittorrent connection. qui aggregates multiple qBittorrent instances behind a single API endpoint, making it the right choice for multi-instance setups sharing a common filesystem (e.g. mergerfs). Select the source in Config or the setup wizard; all existing qBittorrent behaviour is unchanged.
- **`sources/` package** — torrent-source logic is now isolated in `sources/__init__.py`, `sources/_qbit.py`, and `sources/_qui.py` behind a common `fetch_file_map` / `test_connection` / `connection_info` / `fetch_save_path_hint` interface. Adding future sources requires only a new backend module and a one-line dispatcher update.
- **Source-aware deep links in file explorer** — the Torrents tab now shows a `qBit ↗` or `qui ↗` button on each file row that opens the torrent source web UI. For qui, the link navigates directly to the instance that owns that file.
- **Source-aware setup wizard** — Step 1 of the setup wizard now has a [qBittorrent] / [qui] segmented toggle with conditional credential fields and post-test info cards for both sources.

### Improvements
- **Exclusion patterns: hide from file explorer** — a new "File explorer visibility" toggle in Config → Exclusion Patterns controls whether excluded files appear in the file explorer. Default is Visible (existing behaviour). When set to Hidden, excluded files are filtered out of all views; switching the status filter to "Excluded" still shows them so they remain accessible on demand.

### Bug Fixes
- **Upload spike detection via instance count** — each upload snapshot now embeds `_instance_count` (the number of eligible qui instances contributing to that scan, always 1 for qBittorrent). `compute_upload_stats` skips delta pairs where the instance count differs between consecutive snapshots, preventing false upload spikes when a new qui instance is added or when a partial snapshot was captured while one instance was temporarily unreachable. Old snapshots without the field are treated as count 1 for backward compatibility.
- **Removed Upload Baseline card from Config** — the retag/delete UI panel exposed destructive database operations with insufficient safeguards. Backend endpoints remain auth-protected and available for direct API access if needed.

### API changes
- `/api/source_info` (renamed from `/api/qbit_info`) — returns connection info for whichever source is configured
- `/api/source_save_path` (renamed from `/api/qbit_save_path`) — fetches the common save-path prefix from whichever source is configured
- `/api/test_connection` — now source-aware; returns `instances`, `eligible_count`, and `skipped` for qui

## v1.3.6 — 2026-05-11

### Bug Fixes
- **Delete script: fix `df` fallback never running** — `FREE_BEFORE`/`FREE_AFTER` were captured via `df --output=avail -B1 | tail -1 || fallback`. Because `tail` exits 0 even on empty input, the `||` fallback never fired when `df --output=avail` wasn't supported, leaving both variables empty and reporting `Actual: 0B`. Fixed by capturing the primary output first and checking emptiness before falling back.
- **Delete script: tighten variance threshold** — acceptable variance between expected and actual space freed tightened from 10% to 2%.

### Improvements
- **Delete script: hardlink-aware space accounting** — the script now checks each file's inode link count before deletion (`stat -c '%h'` / `stat -f '%l'`). Hardlinked files (inode still referenced by the media directory) are tracked separately and excluded from the expected-vs-actual comparison, since deleting one link doesn't free space until the last reference is gone. The summary shows a breakdown of hardlinked vs standalone files and the correct `✓` confirmation when 0 bytes freed is expected.

## v1.3.5 — 2026-05-07

### Bug Fixes
- **Dedupe script path root** — the generated dedupe script now computes the common ancestor of `LOCAL_PATH` and `MEDIA_PATH` (e.g. `/data` when torrents are under `/data/torrents` and media under `/data/media`) and makes every path in the script relative to that root. Previously, canonical paths were relative to `LOCAL_PATH` and duplicate paths were relative to either `LOCAL_PATH` or `MEDIA_PATH`, so no single `cd` could resolve both — causing the working-directory guard and all `ln -f` commands to fail. The USAGE comment now names the in-container root so users know which host directory to `cd` into before running the script.

## v1.3.4 — 2026-04-30

### Bug Fixes
- **Clear stale qBittorrent error on successful test connection** — after a successful test connection, the persisted results status is reset to `ok` if it was a `qBittorrent error`. Previously, a qBit error from a prior run would persist in the ErrorBanner until the next full audit completed, even after fixing the connection.
- **Script modal z-index** — fixed script output modal being obscured by other UI elements.
- **Dedupe DONE counter accuracy** — dedupe script progress counter now correctly reflects completed operations.

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
