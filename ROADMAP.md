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

## v1.4 — Shipped ✅

- **qui torrent source** — connect to [qui](https://github.com/autobrr/qui) instead of a direct qBittorrent instance for multi-instance / mergerfs setups

---

## v1.5 — Shipped ✅

### v1.5.0 — Large-library memory optimizations & tracker analytics

- **Early release of scan intermediates** — `torrent_records`, `media_records`, `inode_map`, and `duplicate_map` are explicitly deleted after `_assemble_records()` so Python's GC can collect them before subsequent allocations. For a 500k-file library these four structures together can reach 600–700 MB.
- **Early release of raw qBittorrent objects** — `tracker_map` and `files_map` are released immediately after the normalized `file_map` is built in `sources/_qbit.py`.
- **Lazy file endpoint** — `media_files` and `torrent_files` removed from `/api/results` and the `latest_results` row. A new `/api/files?tab=media|torrents` endpoint serves them on demand. The frontend fetches file lists only when navigating to File Explorer or Trackers; Dashboard, Sidebar, and Trackers derive display values from pre-computed summary stats in `/api/results`.
- **`audit_snapshots` no longer stores file lists** — snapshots now contain only dashboard stats (a few KB per row). Diff computation uses in-memory file data before overwriting `file_results`. `/api/changes` reads from `change_log`. A one-time startup migration strips file lists from existing rows via SQLite `json_remove`.
- **Per-tracker trend charts** — TrackerCard has 5 chart tabs: Seeding, Uploaded, Yield (daily uploaded ÷ seeding %), Orphaned, Not Imported. Click any stat box to switch the chart; active box gets a colored outline. Seeding chart auto-scales the y-axis so small changes are visible.
- **Daily health stats in upload snapshots** — each audit augments the upload snapshot with per-tracker `orphaned_size`, `orphaned_count`, `not_imported_size`, `not_imported_count` from the file walk. `compute_upload_stats` returns a new `daily_tracker_stats` field: per-day point-in-time stats per tracker (last snapshot of each day).
- **Trackers date range** — Trackers page owns independent `dateFrom`/`dateTo` state, initialized from the Dashboard's current lookback on first mount, then fully decoupled. FilterBar renders date pickers when in date-range mode and preset buttons when in lookback mode.
- **`/api/upload_stats` date range params** — endpoint now accepts `from=` and `to=` ISO date strings alongside the existing `days=` param. `db_get_upload_snapshots` supports the same.
- **Change Log date range filter** — replaced the preset day buttons (7d / 30d / 90d) with a from/to date range picker.
- **Custom DatePicker component** — replaces native `<input type="date">` across the app. Calendar popover styled with CSS variables; shows month navigation, today highlight, selected day accent fill, and a Clear button. Used identically in FilterBar and ChangeLog.

---

## v1.6 — Workflows / Backfill ✅

### Shipped in v1.6.0
- **Workflows sidebar group** — expandable nav group with chevron; future workflows slot in as additional children
- **Backfill workflow** — surfaces unseeded media files matched to the Sonarr/Radarr library and queries interactive release search for each candidate
- **Quality filtering** — Resolution / Source / HDR multi-select chips matching Sonarr/Radarr quality profile terminology; filter persistence in `localStorage`
- **Interactive release table** — Title / Tracker / Size / Peers / Quality / Score / HDR columns; HDR auto-detected from release title with coloured badges; sorted by custom format score → quality weight → seeders
- **One-click grab** — Grab button POSTs directly to Sonarr/Radarr `/api/v3/release`
- **Season-pack grouping** — Sonarr episode files grouped by (series, season) and searched as a season pack
- **Sort options** — Largest / Smallest / Random (OS-entropy) / A → Z
- **Search depth** — 5 / 15 / All with estimated time; backend cap raised to 500

### Shipped in v1.6.1 (large-library hardening)
- **Crash-restart loop fix** — duplicate detection caps (skip excluded files, skip size groups > 200, ≤ 10 sibling paths per file) eliminate the quadratic memory blowup on BDMV/disc-heavy libraries
- **Signature-based diff** — change log diffs against a compact `{path: bitmask}` map instead of deserializing previous full file lists
- **Aborted-scan detection** — scan marker + per-phase RSS tracking; killed scans appear as `aborted` rows in Audit History with the exact phase of death
- **Crash-loop breaker** — automatic scanning pauses after 2 consecutive killed scans instead of restarting forever
- **`/api/debug/report`** — privacy-scrubbed diagnostic dump (sanitized config, memory/cgroup stats, crash evidence, recent logs) safe to paste publicly
- **gthread gunicorn worker** — slow requests no longer kill the worker or block progress polls; `/api/files` streams stored JSON without parsing it

---

## v1.7 — Triage & Client Actions ✅

### Shipped in v1.7.0
- **Dead Seeds** — audit-time tracker-health classification (zero extra API cost) stored on every torrent file record; new Triage section for imported + tracker-dead torrents (lossless deletes), live re-verified on page load; counted in the sidebar badge
- **Opt-in client-side deletion** — `ALLOW_CLIENT_DELETE` config toggle (default off); Triage deletes torrents + files via qBittorrent `torrents/delete` or qui bulk-action, behind a confirmation dialog listing hashes, trackers, seeding time, and every file path; replaces Triage's delete script (which caused redownloads)
- **Superseded quality buckets** — higher / same / lower than library / unavailable, via resolution + source ranking; DVD implies SD
- **Seeding time per row** — replaces per-torrent ratio as the hit-and-run tiebreaker
- **Matching fixes** — year-aware radarr matching (same-title remakes), diacritic folding (Žižek!), year-titled films (2046 / 1917 / 2012), qBittorrent 5.x tracker status codes
- **Cross-seed delete safety** — per-path records keep the healthiest claimant torrent; a path with any live torrent can never be flagged as a dead seed
- **Exclusion granularity fix** — exact file paths for single-file torrents; category dirs can never become exclusion rules
- **qui deep links** — Triage + File Explorer chips open the exact torrent (`?torrent={hash}`); qBittorrent chips copy a searchable title
- **Cleanup ↔ Triage cross-links** with live counts
- **Trumped workflow** — PM-driven wizard: paste the tracker's trump PM, confirm the hardlink group (resolved live for cross-seed siblings), nuke it everywhere via the client, grab the replacement through Sonarr/Radarr. Design in `prompts/TRUMP.md`
- **Dead Registration bucket** — tracker dropped the registration but the payload lives on via a cross-seed sibling or the library hardlink; topology-aware `delete_files='auto'` deletes a torrent's files only when no surviving torrent shares that path
- **Two-phase Triage** — the page answers instantly from an audit-time snapshot (zero torrent-client calls), then confirms live tracker health in batched background requests
- **Background scan deferral** — the watchdog and scheduled audit defer to an active Triage/Cleanup/Dedupe/Backfill/Trumped session instead of scanning underneath it
- **Memory ratchet mitigations** — `malloc_trim()` after scans and heavy endpoints, `MALLOC_ARENA_MAX=2`, and a leak-vs-ratchet-distinguishing allocated-blocks timeline in the debug report
- **Trackers comparison view** — per-tracker card grid replaced with an overlaid multi-line chart (solo/toggle per tracker) and a sortable comparison table
- **Trumped soft-matching refinements** — near-year title disambiguation no longer misfires on adjacent scene tokens

### Shipped in v1.7.1 (security hardening)
- **Closed by default off-LAN (#18)** — with no `AUDITORR_SECRET` configured, only local clients (loopback + private ranges) are served; everything else gets a generic 401. A port-forwarded instance no longer runs open. Locality comes from the connection's source address, never the spoofable `X-Forwarded-For`
- **Header-only access key** — the `?secret=` query-string fallback was removed; query strings leak into proxy logs and browser history
- **`AUDITORR_TRUSTED_NETWORKS`** — extra CIDRs treated as local, e.g. Tailscale's `100.64.0.0/10` (CGNAT, so not covered by the RFC1918 check)
- **`AUDITORR_REQUIRE_AUTH`** — strict mode with no local exemption; set without a secret it fails closed (503 `auth_not_configured`) behind a self-recovering full-page setup notice

---

## Future Ideas
- **Dead-since tracking** — persist when each torrent was first seen tracker-dead across audits, so Dead Seeds can show tracker-credited seed time (seeded minus dead duration) for hit-and-run decisions
- **Webhook / notification support** — alert when health score drops below threshold (Discord, ntfy.sh, Gotify)
- **Per-tracker import success rate** — of files downloaded from each tracker, what % got imported by Sonarr/Radarr
- **Lidarr / Readarr support** — extend `_SERVICE_MAP` in `arr.py` with Lidarr/Readarr endpoints
- **One-click hardlink repair** — for orphaned media with a matching torrent file, create the missing hardlink directly from the UI
- **Title parser 4K fix** — `4K` token in the quality-tag regex strips mid-title occurrences (e.g. "The 4K Experience"); needs a lookahead or post-tag word boundary (2 xfail tests already cover this)
- **Export to arr** — bulk-add orphaned media files to Sonarr/Radarr monitored list from the file explorer
- **Torrent re-announce** — trigger a re-announce via qBittorrent API for all seeding torrents on a selected tracker directly from the Trackers page
- **Score history export** — download the full score history as CSV from the Config → Audit History section
- **Dark/light mode system preference** — auto-detect `prefers-color-scheme` and default to it instead of always starting on dark
