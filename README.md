# auditorr

**A media library audit tool for qBittorrent / qui + Sonarr / Radarr.**
Hardlink-based health score, per-tracker analytics, cross-seed
effectiveness, and a change log that tells you exactly what moved between
scans.

<p align="center">
  <a href="docs/dashboard.png">
    <img src="docs/dashboard.png" alt="auditorr dashboard" width="900" />
  </a>
</p>

---

<table>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/feature-media.png"><img src="docs/feature-media.png" alt="Media explorer" /></a>
      <p><b>Media explorer</b><br>
      Tree or flat view of your library. Click a file to see every hardlink
      sibling and which tracker is seeding it — answer <i>"is this episode
      actually linked to my downloads?"</i> in one click.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/feature-torrents.png"><img src="docs/feature-torrents.png" alt="Torrents" /></a>
      <p><b>Torrents</b><br>
      Every file qBittorrent / qui is seeding, with tracker, status, and
      reverse hardlink lookup. A <code>qbit/qui ↗</code> chip per row
      deep-links to that torrent in your client.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/feature-trackers.png"><img src="docs/feature-trackers.png" alt="Trackers" /></a>
      <p><b>Trackers</b><br>
      Per-tracker analytics: seeding TB, 30-day uploaded, <b>yield %</b>
      (uploaded ÷ seeding), orphaned, not imported, and an uploaded trend
      sparkline. Deep-link into the explorer pre-filtered by tracker.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/feature-changes.png"><img src="docs/feature-changes.png" alt="Change log" /></a>
      <p><b>Change log</b><br>
      File-level diff between every consecutive audit — kept indefinitely.
      Filter by type (orphaned / imported / new / removed), search by path,
      export CSV. The fastest way to see why your score moved.</p>
    </td>
  </tr>
</table>

---

## Install

### Unraid

auditorr is available on **Unraid Community Apps** — search for `auditorr`
in the Apps tab and install in one click. The template ships with sensible
defaults:

- **WebUI:** `http://[IP]:[PORT:8677]/`
- **Appdata:** `/mnt/user/appdata/auditorr/data` → `/app/data`
- **Data:** `/mnt/user/data` → `/data` (read-only)

### Docker Compose

```yaml
services:
  auditorr:
    build: .
    container_name: auditorr
    restart: unless-stopped
    ports:
      - "8677:8677"
    volumes:
      - ./data:/app/data
      - /path/to/media:/data/media:ro
      - /path/to/torrents:/data/torrents:ro
    environment:
      - AUDITORR_PORT=8677
```

Open `http://your-server-ip:8677` and point auditorr at your torrent source
from the in-app settings.

<details>
<summary><b>Build from source</b></summary>

```bash
git clone https://github.com/thrill-burn/auditorr.git
cd auditorr
docker build -t auditorr .
docker run -d \
  --name auditorr \
  -p 8677:8677 \
  -v /path/to/appdata:/app/data \
  -v /path/to/media:/data/media:ro \
  -v /path/to/torrents:/data/torrents:ro \
  auditorr
```

</details>

---

## Health score

| Component         | Max pts | What it measures                                                    |
| ----------------- | ------- | ------------------------------------------------------------------- |
| Hardlinked Media  |     70  | % of media library hardlinked back to a torrent file                |
| Orphaned Torrents |     10  | Files in your torrent folder that qBittorrent has no record of      |
| Not Imported      |     10  | Seeding torrents with no matching file in your media library        |
| Duplicate Files   |     10  | Bit-for-bit identical files that share no inode (true disk dupes)   |

The 10-point categories lose points **linearly** as problem data grows
toward the configured threshold. At the threshold, all 10 points are gone.

> [!IMPORTANT]
> The Hardlinked Media score assumes you use hardlinks between your torrent
> folder and your media library — the
> [TRaSH Guides](https://trash-guides.info/File-and-Folder-Structure/) folder
> structure. Without hardlinks, this score is 0 regardless of how healthy
> the rest of your library is.

---

## License

MIT — see [LICENSE](LICENSE).
