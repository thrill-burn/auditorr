# auditorr Roadmap

## v1.2 — Tracker Yield & Upload Analytics

### Philosophy
Surface upload analytics that qBittorrent has but doesn't present in a useful way. Track upload generated per GB of disk space per tracker — like a yield or interest rate — so users can understand which trackers are actually worth seeding on.

### Tracker Yield Panel
- Capture `uploaded` bytes per torrent hash on each scan
- Store lightweight upload snapshots in SQLite keyed by hash and timestamp
- Compute upload deltas between scans to derive period-based stats (daily, weekly)
- Calculate yield per tracker: `total_uploaded / size` (all-time) and `uploaded_delta / size / days` (annualized rate)
- Dashboard panel showing tracker yield leaderboard with trend sparklines
- "Best performing tracker" and "lowest yield tracker" callouts

### Supporting Backend
- New SQLite table: `torrent_upload_snapshots (hash, scanned_at, uploaded_bytes)`
- Prune snapshots older than 90 days
- New `/api/tracker_yield` endpoint returning per-tracker yield stats

---

## v1.3 — Future Ideas

- Webhook / notification support — alert when health score drops below threshold (Discord, ntfy.sh, Gotify)
- Per-tracker import success rate — of files downloaded from each tracker, what % got imported by Sonarr/Radarr
- Lidarr / Readarr support
- One-click hardlink repair — for orphaned media with a matching torrent file, create the hardlink directly
- Unraid Community Applications template
