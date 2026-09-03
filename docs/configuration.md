# Configuration

Almost everything is configured in the **Config** tab of the web UI and stored
in auditorr's database — not in environment variables. The handful of variables
that do exist are listed at the [bottom of this page](#environment-variables).

Settings are validated before they save; if something is rejected the UI tells
you which field and why.

---

## Torrent Source

auditorr reads your torrents from one of two backends.

| Setting | Notes |
| --- | --- |
| **Source** | `qBittorrent` (direct) or `qui` (aggregates several clients). |
| **Host** | Full URL including scheme and port, e.g. `http://192.168.1.10:8080`. |
| **Username / Password** | qBittorrent only. |
| **API key** | qui only. |
| **Workflow torrent deletion** | Opt-in. Off by default — see below. |
| **External URL** | Optional. Only for reverse-proxy setups — see below. |

Use **Test Connection** after filling these in. On success auditorr reports the
client version, torrent count, and total seeding size, which is the quickest
confirmation that it's talking to the right instance.

Stored credentials are never returned by the API — they read back as
`__stored__` and are only overwritten when you type a new value.

### External URLs (reverse proxy)

**If the "open in qui ↗" and Sonarr/Radarr links already take you to the right
place, skip this section and leave these fields blank.** Blank means "same as
the host above", which is what auditorr has always done.

The **Host** field above is the address auditorr *connects to*. It's also, by
default, the address your browser is sent to when you click one of the `↗`
buttons. Those are usually the same. They aren't if you reach your apps through
a reverse proxy or a Tailscale domain, and there's no single value that works
for both: the internal address gives you links your browser can't open, and the
public one routes every scan through the proxy — or fails entirely if there's an
SSO layer in front of it.

So they're two separate settings. Find **External URLs (reverse proxy)** at the
bottom of Torrent Source and Integrations:

| Field | What it's for |
| --- | --- |
| **Host / URL** | The address **auditorr** connects to. Keep this internal. |
| **External URL** | The address **your browser** uses. Only what `↗` links point at. |

Notes:

- Must start with `http://` or `https://`. A bare `media.example.com` is
  rejected, because a browser reads it as a path inside auditorr rather than
  another host.
- Subpaths work: `https://media.example.com/sonarr` is fine.
- auditorr **never** fetches the external URL. It's a link target and nothing
  more — no API call, no connection test, no scan traffic.
- There's no Test button for it, because the only meaningful test is whether
  *your* browser can reach it. Fill it in and use the **open ↗** link beside the
  field.
- Additional Sonarr/Radarr instances each get their own **External URL** field
  on their card.

### Workflow torrent deletion (`ALLOW_CLIENT_DELETE`)

Default **Disallowed**. While disallowed, no workflow can remove a torrent from
your client; the Triage delete button isn't shown at all.

When allowed, Triage and Trumped may call the client's delete endpoint on
torrents you have explicitly selected, behind a confirmation dialog that lists
every hash, tracker, and file path involved. See
[Workflows](workflows.md#deleting-torrents-safely) for how file deletion
interacts with cross-seeding.

---

## Path Mappings

auditorr, your torrent client, and Sonarr/Radarr may each see the same files at
different paths. These settings reconcile that.

| Setting | Default | Meaning |
| --- | --- | --- |
| **Media path** | `/data/media` | Your library, as auditorr sees it. |
| **Remote torrent path** | `/data/torrents` | Your torrent folder as **the torrent client** reports it. |
| **Local torrent path** | `/data/torrents` | The same folder as **auditorr** sees it. |

If your client runs in another container with different mounts, remote and
local will differ — auditorr substitutes one prefix for the other when matching
torrents to files on disk. If both containers use the TRaSH layout under
`/data`, they are the same and you can leave them alone.

Both paths must be mounted into the auditorr container. Read-only is
recommended and is what the published templates use.

---

## Watchdog & Scheduled Audits

| Setting | Default | Meaning |
| --- | --- | --- |
| **Watchdog enabled** | On | Re-audit automatically when the filesystem changes. |
| **Watchdog cooldown** | 60s | Quiet period after a change before scanning. Minimum 10. |
| **Scheduled interval** | 360 min | Periodic audit regardless of filesystem activity. Minimum 10. |

The cooldown exists so that an import writing hundreds of files triggers one
audit rather than hundreds. On busy libraries, raise it.

Both automatic triggers defer while you are actively using a workflow page, so
a background scan can't land in the middle of a Triage session. Manual scans
and the startup scan ignore that — explicit intent always wins.

> On Unraid, filesystem events over NFS or certain bind mounts can be
> unreliable. If the watchdog never seems to fire, lean on the scheduled
> interval instead — see
> [Troubleshooting](troubleshooting.md#changes-dont-trigger-a-re-scan).

### Large libraries: turn the watchdog down, or off

This is the setting most worth revisiting once your library gets big. Two costs
scale with size:

- **One inotify watch per directory.** Both trees are watched recursively, so
  the watch count tracks your directory count — consuming kernel watch slots
  (`fs.inotify.max_user_watches`) and the memory behind them. The debug report's
  `runtime.inotify` shows your current count against the kernel limit.
- **Every trigger is a full re-walk.** There is no incremental scan. A change
  anywhere means both trees are walked again, and a scan is also auditorr's
  peak-memory moment.

The debounce restarts on each new event, so a long import or download run keeps
pushing the scan out rather than firing repeatedly. But once activity settles
the full audit runs, and after it finishes further triggers are suppressed for
whichever is longer — your cooldown, or the duration of the scan that just ran.
On a library where a scan takes several minutes, sustained write activity can
therefore produce a re-audit every few minutes, all day.

**Recommendation:** on large libraries, raise the cooldown into the tens of
minutes (1800 = 30 min is a reasonable starting point), or turn the watchdog off
entirely. The scheduled audit already runs every 6 hours by default — four full
audits a day is plenty for a health dashboard, and **Scan Now** is always there
when you want fresh numbers immediately.

If you keep it on, the cooldown is the dial that matters: it is the minimum
quiet period before a scan fires, so a larger value both batches more changes
into a single audit and puts a floor under how often scanning can happen at all.

---

## Integrations

Sonarr and Radarr are optional. Without them auditorr still audits, scores, and
generates scripts; what you lose is import awareness, quality comparison, and
the search-and-grab half of the workflows.

| Setting | Meaning |
| --- | --- |
| **Sonarr / Radarr URL** | Base URL, e.g. `http://192.168.1.10:8989`. |
| **API key** | From Settings → General in the respective app. |
| **Remote path** | The library path *as Sonarr/Radarr sees it*, if it differs from auditorr's. |
| **External URL** | Optional. Where *your browser* reaches it, if that differs — see [External URLs](#external-urls-reverse-proxy). |

Multiple instances are supported — add them under **Additional Sonarr/Radarr
instances** when you run, say, a 4K Radarr alongside a 1080p one. Those are read
*alongside* the primary Sonarr/Radarr fields above, not instead of them, so the
primary pair stays in use. Listing the same server in both places is harmless —
it's read once. Each connection carries its own Remote path and External URL, so
a split library behind a proxy links correctly per instance.

> **In v1.7.2 and earlier** the additional-instances list replaced the primary fields
> rather than adding to them, so filling it in switched your main Sonarr and
> Radarr off while their settings stayed on screen and still passed their test
> button. If you run more than one instance, upgrade — there is nothing to
> re-enter, and your counts will move on the first scan afterwards.

---

## Health Score

The score is out of 100, split across four components:

| Component | Default points | How it's earned |
| --- | --- | --- |
| **Hardlinked Media** | 70 | Directly proportional: the share of your library size that is hardlinked to a torrent. |
| **Orphaned Torrents** | 10 | Full marks at zero; falls to zero when orphaned size reaches the threshold below. |
| **Not Imported** | 10 | Same shape, on torrent data with no library file. |
| **Duplicate Files** | 10 | Same shape, on bit-identical files that don't share an inode. |

### Weighting

The donut at the top of the Health Score settings controls how the 100 points
are divided. The numbers are **relative** — only their proportions matter, so
you can type whatever expresses your priorities and the points column always
totals 100. The defaults happen to sum to 100 already, so an untouched install
reads its weights and its points as the same four numbers.

Set a category to `0` to stop scoring it. It still appears on the dashboard with
its size, count and trend — the points figure just reads **Not scored** instead.
At least one category has to stay above zero.

Scores read as **Great** (≥ 90), **Good** (≥ 75), **Fair** (≥ 50), **Poor**
below that, whatever weighting you choose.

> Changing the weighting changes what the number means, so your score history
> will step on the day you change it. The chart is not re-computed retroactively.

### If you remove torrents once seeding finishes

A common setup has Sonarr/Radarr or qBittorrent delete a torrent once it meets
its seeding requirement, and the torrent-folder hardlink goes with it. The media
file stays healthy, but it no longer has a torrent partner — so **Hardlinked
Media**, 70 of the 100 points by default, collapses.

If that's deliberate, turn **Hardlinked Media** down or off. The remaining
categories carry the score.

Doing that does **not** blind auditorr to broken imports, which is the usual
worry. The failure that actually costs you disk — an arr that *copied* instead
of hardlinking — leaves a second, bit-identical file with a different inode in
your torrent folder, and that is exactly what **Duplicate Files** detects. What
you give up is only the signal that a file has no torrent partner at all, which
in this workflow is the normal, intended state.

There is no way for auditorr to detect this workflow on its own: once the
torrent and its hardlink are gone, a deliberately cleaned file and a failed
import look identical on disk. That's why it's a setting rather than something
auto-detected.

### Thresholds

The three thresholds (`OR_RATIO`, `NI_RATIO`, `DUP_RATIO`) are each a fraction
of your **total torrent size**, default `0.01` — one percent. So with 10 TB of
torrents, the default gives you a 100 GB allowance for orphans before that
component's points are fully gone. Valid range is `0.001` to `1.0`.

Raise a threshold if you knowingly keep something around and are tired of being
marked down for it. Lower it if you want the score to react earlier.

Hardlinked Media has no threshold — it scores in direct proportion to how much
of your library is hardlinked.

> At the default weighting, Hardlinked Media is 70 of the 100 points, so a
> library that doesn't use hardlinks scores near zero no matter how tidy it is.
> That's intentional — see
> [Troubleshooting](troubleshooting.md#hardlinked-media-shows-0). If it's
> deliberate rather than a misconfiguration, reweight it as described above.

---

## Excluded Files & Folders

Exclusions are auditorr's only filtering mechanism. An excluded file is left out
of scoring, workflows, and duplicate detection — nothing is ever hidden from you
silently.

Maximum 100 patterns, 200 characters each. Lines starting with `#` are comments.

### Pattern syntax

| Pattern | Matches |
| --- | --- |
| `Featurettes` | A bare word matches any **path segment** with that exact name, at any depth. |
| `data/torrents/games` | A path matches that subtree. Works against both container-root and host-style paths. |
| `*.srt` | Glob, matched against the filename and against individual path segments. |
| `ext:.nfo` | By file extension. The leading dot is optional. |
| `name:@eaDir` | An exact file or folder name — stricter than a bare word only in that it never partially matches. |
| `contains:sample` | The text appears anywhere in the normalized path. |

Bare words are the friendly default: `Extras` excludes every `Extras` folder
without you needing glob syntax. Use `contains:` when you need to match
mid-segment text like `Sample` inside a filename.

### Presets

Two preset groups save you from writing common rules by hand:

- **Disc rip structures** — `BDMV`, `VIDEO_TS` and friends, for full-disc rips
  whose thousands of structural files would otherwise dominate duplicate
  detection.
- **Media server files** — Plex, Jellyfin, Emby, Kodi and UMS metadata and
  artwork directories.

### Hide excluded files from the explorer

Off by default: excluded files still appear in File Explorer, marked as
excluded, so you can see what your rules are doing. Turn it on once you trust
them.

This sets the starting position of the **Excluded** filter in File Explorer, and
you can override it for a single visit from the toolbar there — `+` to see only
excluded files, `-` to hide them. The setting wins again the next time you open
the page.

### Where exclusions come from

Besides typing them here, exclusions are written by the **Exclude** buttons on
the Cleanup and Triage pages, and by Triage's one-click suggestions. Everything
those buttons add lands in this list, visible and editable.

---

## Appearance & Audit History

**Appearance** switches between dark and light themes.

**Audit History** lists recent runs with trigger, duration, resulting score, and
status. Runs marked `aborted` are scans that were killed mid-flight — usually
the container running out of memory. See
[Troubleshooting](troubleshooting.md#scans-keep-getting-killed-or-scanning-stopped-on-its-own).

---

## Environment variables

Set these on the container; they can't be changed from the UI.

| Variable | Default | Effect |
| --- | --- | --- |
| `AUDITORR_PORT` | `8677` | Port the app listens on inside the container. |
| `DATA_DIR` | `/app/data` | Where the SQLite database and config live. Must be persistent. |
| `AUDITORR_SECRET` | *(unset)* | Access key. Once set, required from every client. See [Remote access](remote-access.md). |
| `AUDITORR_TRUSTED_NETWORKS` | *(unset)* | Extra CIDRs treated as local, e.g. `100.64.0.0/10` for Tailscale. |
| `AUDITORR_REQUIRE_AUTH` | `false` | Require the access key even from local clients. |

`MALLOC_ARENA_MAX=2` is set in the image itself to limit how much memory the
allocator holds after a large scan. Don't override it unless you're
investigating a memory problem.
