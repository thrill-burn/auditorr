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

### In Progress (experimental) 🚧
- **Upload activity chart** — stacked bar chart on the dashboard showing daily upload volume per tracker
- **Library yield panel** — hero yield percentage with per-tracker yield table (uploaded / seeding size)
- **Tracker detail modal** — per-tracker overlay with seeding/orphaned/not-imported breakdown, upload trend chart, yield stat, and navigation buttons to the file explorer; reachable from the tracker leaderboard, tracker pills, and yield table

---

## v1.3 — Future Ideas

- Webhook / notification support — alert when health score drops below threshold (Discord, ntfy.sh, Gotify)
- Per-tracker import success rate — of files downloaded from each tracker, what % got imported by Sonarr/Radarr
- Lidarr / Readarr support
- One-click hardlink repair — for orphaned media with a matching torrent file, create the hardlink directly
- Unraid Community Applications template
