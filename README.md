# auditorr

auditorr shows you exactly what’s happening inside your media library

It cross-references your hardlinked torrent and media directories with qBittorrent to generate a real-time health score, detecting orphaned files, duplicates, missing links, and calculating cross-seeding efficiency.

![Dashboard](docs/dashboard.png)

- **Health score (0–100)** — see how clean and efficient your library is  
- **Find wasted disk space** — duplicates, orphaned files, unlinked torrents  
- **Cross-seeding insights** — understand how well your data is seeded  
- **Tracker leaderboard** — see which trackers actually matter  
- **Powerful file explorer** — filter by status, tracker, seed count, size 

---

## Who is this for?

auditorr is built for self-hosted media setups using qBittorrent + Sonarr/Radarr with hardlinks, following the [TRaSH Guides](https://trash-guides.info/File-and-Folder-Structure/).

The health score reflects how well your library is actually connected and seeding.

---

## Installation

### Instant Quick Start

```bash
docker run -d \
  --name auditorr \
  -p 8677:8677 \
  -v /mnt/user/appdata/auditorr/data:/app/data \
  -v /mnt/user/data:/data:ro \
  ghcr.io/thrill-burn/auditorr:latest
```

Then open: `http://your-server-ip:8677` and configure qBitorrent.

### unRaid (Recommended)

Docker tab → Add Container button at the bottom and fill in the blanks:

- **Name:** `auditorr`
- **Repository:** `ghcr.io/thrill-burn/auditorr:latest`
- **Icon URL:** `https://raw.githubusercontent.com/thrill-burn/auditorr/main/docs/icon.png`
- **WebUI:** `http://[IP]:[PORT:8677]/`
- **App Path:** `/mnt/user/appdata/auditorr/data` → `/app/data`
- **Data Path:** `/mnt/user/data` → `/data`
- **Port Mapping:** `8677 → 8677`

Press the Apply button, let the container install, then open `http://your-server-ip:8677` and configure qBittorrent connection details in the Config tab.

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
      - /mnt/user/appdata/auditorr/data:/app/data
      - /mnt/user/data:/data:ro
      # TRaSH folder defaults, change if required
    environment:
      - AUDITORR_PORT=8677
      # Uncomment to enable authentication:
      # - AUDITORR_SECRET=your-secret-key
```

### Build from source

```bash
git clone https://github.com/thrill-burn/auditorr.git
cd auditorr
docker build -t auditorr .
docker run -d \
  --name auditorr \
  -p 8677:8677 \
  -v /mnt/user/appdata/auditorr/data:/app/data \
  -v /mnt/user/data:/data:ro \
  auditorr
```

---
## Important

auditorr assumes a **hardlink-based setup**.

If you are not using hardlinks, your health score will be low even if your library appears functional.

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
