# Changelog

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
