# auditorr

A media library audit tool for qBittorrent + Sonarr/Radarr setups.

auditorr scans your torrent and media directories, cross-references them against qBittorrent, and gives you a health score for your library. It checks hardlinks, surfaces orphaned files, unimported torrents, duplicates, and determines your cross-seeding effectiveness.

![Dashboard](docs/dashboard.png)

---

## Who is this for?

auditorr is built for people running a self-hosted media stack with hardlinks and an organised folder structure — particularly those following the [TRaSH Guides](https://trash-guides.info/File-and-Folder-Structure/) setup. If you use TRaSH-recommended paths with qBittorrent, Sonarr, and Radarr, the hardlink-based health score will reflect exactly how well your library is connected and seeding.

---

## Features

- **Library health score** — 0–100 score with a color-coded arc dial, trending vs yesterday or last week
- **Cross-seed effectiveness** — weighted multiplier showing how many trackers each byte of media is seeded on, with a segmented disk bar
- **Tracker leaderboard** — top trackers by disk space, clickable to filter the torrent explorer
- **File explorer** — browse media and torrent directories with filters for status, tracker, seed count, filename search, and file size range
- **"What changed" panel** — diff between the last two scans: newly orphaned, newly imported, new duplicates, resolved duplicates
- **Threshold alerts** — banners when a category significantly exceeds its configured threshold
- **Audit history** — every scan logged to SQLite, survives container restarts
- **Light/dark mode** — toggle in Config
- **Auth** — optional shared secret via `AUDITORR_SECRET` env var
- **Watchdog** — inotify-based filesystem watcher triggers audits automatically on file changes
- **Scheduled fallback** — periodic audit catches missed watchdog events on NFS/bind mounts

---

## Quick Start

### Unraid (Recommended)

Docker tab -> Add Container button at the bottom

- **Name:** `auditorr`
- **Repository:** `ghcr.io/thrill-burn/auditorr:latest`
- **Icon URL:** `https://raw.githubusercontent.com/thrill-burn/auditorr/refs/heads/main/docs/icon.png`
- **WebUI:** `http://[IP]:[PORT:8677]/`
- **Path 1:** `/mnt/user/appdata/auditorr/data` → `/app/data`
- **Path 2:** `/mnt/user/data` → `/data`
- **Port mapping:** `8677 → 8677`

### Docker Compose

```yaml
services:
  auditorr:
    image: ghcr.io/thrill-burn/auditorr:latest
    container_name: auditorr
    restart: unless-stopped
    ports:
      - "8677:8677"
    volumes:
      - /path/to/appdata:/app/data
      - /path/to/data:/data:ro
      # Your media and torrent folders need to be subfolders of your data folder
      # for unRaid with TRaSH folders this is:
      # - /mnt/user/appdata/auditorr/data:/app/data
      # - /mnt/user/data:/data:ro
    environment:
      - AUDITORR_PORT=8677
      # Uncomment to enable authentication:
      # - AUDITORR_SECRET=your-secret-key
```

Then open `http://your-server-ip:8677` and configure qBittorrent connection details in the Config tab.

### Build from source

```bash
git clone https://github.com/thrill-burn/auditorr.git
cd auditorr
docker build -t auditorr .
docker run -d \
  --name auditorr \
  -p 8677:8677 \
  -v /path/to/appdata/auditorr:/app/data \
  -v /path/to/data:/data:ro \
  auditorr
```

---

## Configuration

All configuration is done through the **Config** tab in the UI.

| Setting | Description |
|---|---|
| **qBittorrent Host** | URL of your qBittorrent instance, e.g. `http://192.168.1.x:8080` |
| **qBit Save Path** | The path qBittorrent reports via its API |
| **Local Torrent Path** | Where those files actually live from auditorr's perspective (may differ if qBit runs in its own container) |
| **Media Path** | Your final media library directory |
| **Watchdog Cooldown** | Seconds to wait after a filesystem change before running an audit (default: 60) |
| **Scheduled Interval** | Fallback audit interval in minutes (default: 360) |
| **Thresholds** | Percentage of library at which each category loses all its points |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `AUDITORR_PORT` | `8677` | Port to listen on |
| `AUDITORR_SECRET` | *(unset)* | If set, all API routes require `X-Auditorr-Secret: <value>` header |
| `DATA_DIR` | `/app/data` | Where config, history, and SQLite db are stored |

---

## Health Score

| Component | Max Points | Description |
|---|---|---|
| Hardlinked Media | 70 | % of media library hardlinked back to a torrent file |
| Orphaned Torrents | 10 | Files in torrent folder unknown to qBittorrent |
| Not Imported | 10 | Seeding torrents with no matching media file |
| Duplicate Files | 10 | Bit-for-bit identical files sharing no inode |

For the 10-point categories, points are lost linearly as problem data grows toward the configured threshold. At the threshold, all 10 points are gone.

The Hardlinked Media score assumes you are using hardlinks between your torrent download folder and your media library — as recommended by the [TRaSH Guides folder structure](https://trash-guides.info/File-and-Folder-Structure/). Without hardlinks, this score will be 0 regardless of how healthy your library is.

---

## License

MIT — see [LICENSE](LICENSE)
