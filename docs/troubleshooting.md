# Troubleshooting

Headings are the symptom, not the subsystem. Find the one that sounds like your
problem.

If nothing here fits, [file an issue](#reporting-a-problem) — there's a
one-command way to include everything needed to diagnose it.

---

## The UI won't load, or everything returns 401

**Most likely:** you reach auditorr from outside its own network, and you
upgraded to 1.7.1.

Before 1.7.1, leaving `AUDITORR_SECRET` unset disabled authentication entirely
and served anyone who could reach the port. Now, with no key set, only local
clients are served and everything else is refused.

Full explanation and fixes — including Tailscale, VPNs, and reverse proxies — in
[Remote access](remote-access.md#everything-returns-401-after-upgrading). Short
version: set `AUDITORR_SECRET`, or add your tunnel's range to
`AUDITORR_TRUSTED_NETWORKS`.

---

## Hardlinked Media shows 0%

**Cause:** your library and torrent folder don't share files. Either you're not
using hardlinks, or auditorr can't see that you are.

Hardlinked Media is 70 of the 100 health points by default, so this one number
sinks the whole score.

> **Removing torrents once seeding finishes?** Then this is expected, not a
> fault — the media file outlives its torrent, so it has nothing left to be
> hardlinked to. Turn the component down or off in
> [Health Score → Weighting](configuration.md#weighting) rather than working
> through the checks below.

**Check, in order:**

1. **Are you actually hardlinking?** This requires your downloads and library to
   be on the **same filesystem**, with your client and arrs configured for
   hardlinks rather than copy — the
   [TRaSH Guides layout](https://trash-guides.info/File-and-Folder-Structure/).
   If your media and torrents are on different disks or shares, hardlinks are
   impossible and this score cannot rise.
2. **Is both trees mounted into the container?** auditorr can only detect a link
   between files it can both see. Mount the shared parent (`/data`), not the two
   subfolders separately from different sources.
3. **Do the path mappings match reality?** If your client reports save paths
   that don't correspond to what auditorr sees, check
   [Path Mappings](configuration.md#path-mappings).

A useful confirmation: open **Media**, click any file you know is seeding. If
its hardlink siblings are listed, detection works and the number is telling you
the truth about the rest of the library.

---

## Everything shows as Orphaned or Not Imported

**Cause:** path mapping, almost always.

auditorr matches torrents to files by path. If your client reports
`/downloads/torrents/…` and auditorr sees the same files at `/data/torrents/…`,
nothing lines up, so every torrent looks like it has no file and every file
looks like it has no torrent.

**Fix:** set **Remote torrent path** to the prefix your client reports and
**Local torrent path** to what auditorr sees — see
[Path Mappings](configuration.md#path-mappings). The **Fetch from qBittorrent**
button fills in the remote side by inspecting your actual torrents.

---

## Scans keep getting killed, or scanning stopped on its own

**Symptom:** Audit History shows runs marked `aborted`, or automatic scanning
has quietly stopped happening.

**Cause:** the container ran out of memory during a scan and was killed. A
killed process can't log anything, so auditorr detects it after the fact: it
writes a marker at scan start and removes it on exit, and a marker still present
at boot means the previous scan died.

After **two consecutive** killed scans auditorr stops scanning automatically —
both the startup audit and the watchdog — so it doesn't sit in a restart loop
forever. It stays paused until a **manual scan completes successfully**.

**Fix:**

1. Raise the container's memory limit. Large libraries genuinely need it, and
   the peak is during a scan, not at rest.
2. Exclude what you don't need audited. Disc rips are the usual culprit — turn
   on the disc-rip preset in
   [Excluded Files](configuration.md#excluded-files--folders).
3. **Scan less often.** A scan is the peak-memory moment, so the fix for
   repeated kills is often fewer scans rather than more headroom. On a large
   library the watchdog can trigger a full re-walk every few minutes during
   sustained downloading — raise its cooldown substantially or turn it off and
   let the 6-hourly scheduled audit do the work. See
   [Large libraries](configuration.md#large-libraries-turn-the-watchdog-down-or-off).
4. Trigger a manual scan to clear the pause once you've made room.

The debug report's memory section records peak RSS per scan and which phase each
aborted scan died in, which tells you whether you're close to the limit or far
over it.

---

## Memory usage stays high after a scan

This is expected and is not a leak.

Large scans allocate a lot, and the C allocator does not return freed pages to
the operating system afterwards — so the container's reported memory stays at
its high-water mark even though the data is gone. auditorr explicitly asks the
allocator to release memory after every scan and after each heavy page load,
which helps, but does not always bring the number all the way down.

The way to tell the difference: the debug report's memory timeline samples
allocated *blocks* alongside RSS. Climbing blocks means a real leak. Flat blocks
under high RSS is the allocator holding pages, which is harmless.

Worth reporting if allocated blocks climb steadily across hours.

---

## Changes don't trigger a re-scan

**Check first:** the watchdog is enabled, and you've waited out the cooldown
(default 60 seconds after the *last* change).

**If it never fires:** filesystem event delivery is unreliable over NFS and some
bind-mount configurations, which is common on Unraid. auditorr can't receive
events the kernel doesn't send.

**Workaround:** lower the **scheduled interval** so periodic audits cover you,
and treat the watchdog as a bonus. See
[Watchdog & Scheduled Audits](configuration.md#watchdog--scheduled-audits).

**Also normal:** automatic scans defer while you're actively using a workflow
page, up to about ten minutes. A scan you expected during a long Triage session
may simply be waiting for you to finish.

---

## Can't connect to qBittorrent or qui

**Test Connection** distinguishes the common causes — bad credentials, host
unreachable, and timeout are separate messages.

- **Host format:** include scheme and port, e.g. `http://192.168.1.10:8080`.
  Not just the IP.
- **`localhost` won't work** from inside the container — it means the container
  itself. Use the host's LAN address, or the container name if they share a
  Docker network.
- **qBittorrent:** if you've enabled "Bypass authentication for clients on
  localhost", that doesn't apply to auditorr; it needs real credentials.
- **qui:** the API key comes from qui, and only instances that are connected,
  have local filesystem access, and use hardlinks or reflinks are eligible.

---

## File Explorer or Trackers is empty

File lists are built during an audit and stored separately from the dashboard
summary. If you've just installed or just upgraded, **run one audit** and they
populate.

If they're still empty afterwards, the audit is finding no files at all — that's
a path problem, not a display problem. See
[Everything shows as Orphaned](#everything-shows-as-orphaned-or-not-imported).

---

## Duplicate Files says 0, but I know I have duplicates

Duplicate detection is deliberately capped, because uncapped it becomes
quadratic on disc-heavy libraries and exhausts memory:

- Excluded files are skipped entirely.
- Groups of more than 200 same-size files are skipped — this is what disc rips
  produce.
- Each file records at most 10 sibling paths.

Also note the definition: duplicates are identical files that **don't share an
inode**. Two paths that are already hardlinks to one file are not duplicates —
they're the thing you want.

---

## Triage says "verification failed"

Triage renders from audit-time data and then confirms live tracker health in
background batches. That message means the batches couldn't reach your torrent
client — the rows shown are real but their health is as of the last audit.

Press **Retry**. If it keeps failing, your client is unreachable or slow to
respond; check it directly and use **Test Connection** in Config.

The page never guesses: it will tell you it's showing stale data rather than
silently present it as current.

---

## Trigger Rescan doesn't import anything

Read the toast — it now carries Sonarr/Radarr's actual answer rather than a
blanket "rescan triggered". Two answers are common:

**"Not an upgrade for existing movie file"** or **"Not a quality revision
upgrade"** — the arr already has a file for this title and won't replace it with
something equal or worse. Rescanning again will never change that. If the
release really is equivalent (a trump replacement, typically), use
[Force import](workflows.md#rescan-vs-force-import) instead. If it's genuinely
lower quality, the arr is right and there's nothing to fix.

**"No matching title — the file could not be identified"** — the arr couldn't
tell what the release is from its name. Import it by hand from the arr's own
Manual Import, or fix the naming.

If nothing happens at all and no reason is given, auditorr couldn't reach the
arr to ask; check **Test Connection** in Config.

> A rescan imports by hardlink and leaves the torrent seeding. It will never
> move your file out from under the client.

---

## The score dropped and I didn't change anything

Open **Changes**. Every audit records a file-level diff against the previous
one, kept indefinitely, so you can see exactly which files appeared, vanished,
or changed status between any two runs.

Common innocent causes: an import completed (files move), a tracker went down
(torrents become unregistered), or a large download finished and hasn't been
imported yet.

---

## Sonarr/Radarr buttons open the wrong thing, or nothing

auditorr deep-links into your arrs by matching the file's path to their library.
If the arr sees the library at a different path than auditorr does, the lookup
fails.

Set **Remote path** for that integration to the path *as Sonarr/Radarr sees it*
— see [Integrations](configuration.md#integrations).

---

## Links open an internal IP I can't reach

You access your apps through a reverse proxy or Tailscale, but the `↗` buttons
send you to `http://192.168.x.x:8989` — the address auditorr itself connects on.

Set an **External URL** for that service. It's the bottom section of Config →
Torrent Source and Integrations, collapsed by default:

- **Host / URL** stays internal — that's what auditorr connects to, and routing
  it through the proxy would push every scan through it too (and break entirely
  if the proxy enforces SSO).
- **External URL** is your public address, and is used only to build links.

Must include the scheme: `https://sonarr.example.com`, not
`sonarr.example.com`. There's no Test button — use the **open ↗** link beside
the field, since the only thing worth testing is whether *your* browser reaches
it. Full detail in
[External URLs](configuration.md#external-urls-reverse-proxy).

---

## Reporting a problem

auditorr can produce a diagnostic report designed to be pasted publicly:

```
http://your-host:8677/api/debug/report
```

It includes runtime and memory stats, sanitized configuration, scan state and
crash evidence, recent audit runs, and the last few hundred log lines.

It deliberately does **not** include credentials. Hostnames, IP addresses and
API tokens are redacted, and your media file and folder names are replaced with
stable short hashes — so files can be correlated through the report without
being readable. Generic structure (`/data/torrents`, `Season 01`, real file
extensions) is preserved so the report is still useful.

Attach it to a
[GitHub issue](https://github.com/thrill-burn/auditorr/issues) along with what
you expected to happen. That's usually enough to skip the entire first round of
back-and-forth.
