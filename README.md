# auditorr

**A media library audit tool for qBittorrent / qui + Sonarr / Radarr.**
Library health score, per-tracker analytics, cross-seed effectiveness, and
a file-level change log between audits.

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
      reverse hardlink lookup. A <code>qui ↗</code> chip per row deep-links
      straight to that torrent in qui; for qBittorrent the chip copies a
      searchable title and opens the WebUI for a one-paste search.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/feature-trackers.png"><img src="docs/feature-trackers.png" alt="Trackers" /></a>
      <p><b>Trackers</b><br>
      Per-tracker analytics: size seeded, 30-day uploaded, <b>yield %</b>
      (uploaded ÷ seeding), orphaned, not imported, and an uploaded trend
      sparkline. Deep-link into the explorer pre-filtered by tracker.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/feature-changes.png"><img src="docs/feature-changes.png" alt="Change log" /></a>
      <p><b>Change log</b><br>
      File-level diff between every consecutive audit, kept indefinitely.
      Filter by type (orphaned / imported / new / removed), search by path,
      export CSV. The fastest way to see why your score moved.</p>
    </td>
  </tr>
</table>

---

## Workflows

The dashboard tells you what's wrong; **workflows** fix it. One per
dashboard card, in the same order and colors.

**Backfill** targets *orphaned media*: files on disk with no hardlink to a
seeding torrent and no way to grow one (trumped, dead tracker, your own
rip). Search the same title on the tracker you want, grab a version that
actually seeds, and swap it in via Sonarr / Radarr — turning dead weight
into a properly seeding upgrade.

**Cleanup** targets *orphaned torrent files*: files in your torrent folder
the client has no torrent for. Review them grouped by release folder, then
generate a reviewed delete script — or exclude the ones you put there on
purpose.

**Triage** explains every problem torrent and what to do about it:
**Dead Seeds** (tracker-dead but already imported — your library hardlink
keeps the data, so deleting them via the client is lossless), Unregistered,
Superseded (bucketed by quality vs your library copy: higher / same /
lower), Import Pending, and Not in Library. Rows show uploaded and seeding
time (the hit-and-run tiebreaker), tracker messages, and quality chips.
Actions: trigger a Sonarr/Radarr rescan, exclude, or — **opt-in via
Config** — delete torrents *and their files* directly through
qBittorrent / qui, behind a confirmation dialog that lists every hash and
file path before anything is sent.

**Dedupe** targets *duplicate files*: review duplicate groups and generate
a script that replaces copies with hardlinks, verified with `cmp` before
linking.

**Trumped** automates the private-tracker trump swap. Paste the tracker's
"this release has been trumped" PM; auditorr parses the old and new release
names, finds the entire hardlink group (every cross-seed of the old
content), removes it from the client with its files, and grabs the exact
replacement through Sonarr/Radarr — wizard-style, confirmed at every step.
auditorr never contacts a tracker: everything goes through the client and
the arrs.

---

## Install

### Unraid

auditorr is on **Unraid Community Apps** — search for `auditorr` in the
Apps tab and install in one click.

- **WebUI:** `http://[IP]:[PORT:8677]/`
- **Appdata:** `/mnt/user/appdata/auditorr/data` → `/app/data`
- **Data:** `/mnt/user/data` → `/data` (read-only)

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
      - ./data:/app/data
      - /path/to/media:/data/media:ro
      - /path/to/torrents:/data/torrents:ro
```

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

> [!IMPORTANT]
> The Hardlinked Media score assumes you use hardlinks between your torrent
> folder and your media library — the
> [TRaSH Guides](https://trash-guides.info/File-and-Folder-Structure/) folder
> structure. Without hardlinks, this score is 0 regardless of how healthy
> the rest of your library is.

---

## Reporting issues

If a scan fails or behaves unexpectedly, open `http://<host>:8677/api/debug/report`
and paste the output into your issue or forum post. The report is
**privacy-scrubbed and safe to share publicly**: it contains no credentials,
hostnames, IPs, or API keys, and media file/folder names are replaced with
short hashes. It includes your (sanitized) configuration, library size stats,
memory usage, recent audit history, crash evidence, and recent log lines —
usually everything needed to diagnose a problem in one paste.

---

## License

MIT — see [LICENSE](LICENSE).
