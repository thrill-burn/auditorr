# Changelog

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
