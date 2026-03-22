# auditorr Roadmap

## v1.2 — Actions Page + Sonarr/Radarr Integration

### Philosophy

auditorr remains **read-only** against the filesystem at all times.

Two categories of action:
- **Direct API calls** — anything that talks to another trusted app (Sonarr, Radarr, qBittorrent) or is purely read-only. Executed directly from auditorr. No script needed.
- **Generated scripts** — anything that writes, moves, deletes, or modifies files on disk. User reviews the script and runs it manually. auditorr never executes these.

---

### Sonarr/Radarr Integration — Media File Explorer

Interactive search buttons appear on individual file rows in the Media explorer for orphaned files. See existing implementation in FileExplorer.jsx and Config.jsx (SONARR_URL, SONARR_API_KEY, RADARR_URL, RADARR_API_KEY already in config).

---

### Actions Page

A fifth navigation tab in the sidebar, between Torrents and Config. Icon: wrench.

**Top summary banner:**
- Shows total recoverable GB (orphaned torrents + duplicates)
- Shows time since last scan
- If nothing to action: "No actions needed — library looks clean ✓" in green

**Four section cards** in this order, styled like Dashboard metric cards (color strip at top, monospace uppercase label, large bold number, subtitle, description, action buttons at bottom):

---

#### 1. Orphaned Media — var(--red)

**Impact:** count of orphaned media files + total size

**Content:**
- Informational only — no direct actions from this card
- Helper text: "Use the Sonarr / Radarr interactive search buttons in the Media explorer to find seeding versions for these files."
- Button: "View Orphaned Media →" — navigates to Media tab filtered by Orphaned status

**Empty state:** "All media files are seeding ✓"

---

#### 2. Orphaned Torrents — var(--yellow)

**Impact:** count + total size

**Action 1: Open in qBittorrent**
- Direct navigation links — one per orphaned torrent
- Format: `{QB_HOST}/torrents?hash={torrent_hash}`
- Opens in new tab
- Rendered as a collapsible list (show first 5, "Show X more" toggle)
- No script needed — pure navigation to trusted app

**Action 2: Generate Delete Script**
- Opens script modal with bash rm commands
- One rm command per file with filename and size in comment
- Summary header with total file count and size
- echo confirmation at end

Script format:
```bash
#!/bin/bash
# auditorr — Orphaned Torrent Cleanup Script
# Generated: {date}
# WARNING: Review carefully before running. This permanently deletes files.
# {count} files — {total_size}

# {filename} — {size}
rm "{full_path}"

# ...

echo "Done. {total_size} freed."
```

**Empty state:** "No orphaned torrents ✓"

---

#### 3. Not Imported — var(--red)

**Impact:** count + total size

**Action 1: Trigger Sonarr Rescan**
- Direct API call — auditorr calls Sonarr `POST /api/v3/command` with `DownloadedEpisodesScan` for each not-imported torrent path
- Button shows loading spinner while running
- Toast on completion: "Sonarr rescan triggered for {count} paths — check Sonarr for import results"
- Toast on error: show error message
- Button disabled with "Configure Sonarr in Settings →" if SONARR_URL not configured

**Action 2: Trigger Radarr Rescan**
- Same pattern but `DownloadedMoviesScan`
- Button disabled with "Configure Radarr in Settings →" if RADARR_URL not configured

**Navigation:** "View Not Imported →" — navigates to Torrents tab filtered by Not Imported

**Empty state:** "All torrents have been imported ✓"

---

#### 4. Duplicate Files — var(--purple)

**Impact:** Recoverable GB shown EXTRA LARGE (fontSize 48) — this is the headline feature
- Label above: "recoverable"
- Subtitle: "{count} duplicate groups" + note if any skipped due to cross-filesystem

**Action: Generate Dedupe Script**
- Opens script modal with bash ln -f commands
- Torrent file is always canonical, media file becomes the hardlink
- Cross-filesystem groups are skipped with explanatory comment
- Dry run summary header

Script format:
```bash
#!/bin/bash
# auditorr — Dedupe Script
# Generated: {date}
#
# SUMMARY
# {count} duplicate groups found
# {total_recoverable} recoverable
# {skipped} groups skipped (cross-filesystem — cannot hardlink across mounts)
#
# This script replaces duplicate files with hardlinks.
# All file paths will continue to exist after running.
# All torrents will continue seeding normally.
# Review each group carefully before running.

# Group {n}: {filename} — {size} recoverable
# Canonical: {torrent_path}
ln -f "{torrent_path}" \
      "{media_path}"

echo "Dedupe complete. Verify with: df -h"
```

**Empty state:** "No duplicates found ✓"

---

### Script Modal (shared component)

Triggered by any "Generate ... Script" button.

- Full-screen overlay (rgba(0,0,0,0.7), z-index 500)
- Modal box: max-width 700px, centered, var(--surface), var(--border), var(--rl) border-radius
- Header: title + subtitle + X close button
- Warning banner: yellow/amber, "⚠ Review this script carefully before running. auditorr does not execute scripts — you run this manually in your terminal."
- Script body: scrollable pre block, max-height 400px, var(--surface2), var(--mono), fontSize 11px
- Footer buttons:
  - "Copy to clipboard" — textarea fallback for HTTP (same pattern as FileExplorer.jsx copyPaths)
  - "Download as .sh" — Blob download
  - "Close"
- Close on X click or clicking overlay background
- Loading spinner while fetching script from backend

---

### Backend requirements

**New route: GET /api/actions**
Returns actionable summary from last scan results:
```python
{
  "orphaned_media": {"files": [{"path", "size"}], "total_size": int},
  "orphaned_torrents": {"files": [{"path", "size", "hash"}], "total_size": int},
  "not_imported": {"files": [{"path", "size"}], "total_size": int},
  "duplicates": {
    "groups": [{"files": [{"path", "size", "inode", "canonical", "same_fs"}], "recoverable_size": int, "skipped": bool}],
    "total_recoverable": int
  },
  "total_recoverable": int
}
```

**New route: GET /api/actions/script/<type>**
Returns bash script as plain text. Types: `orphaned_torrents_delete`, `dedupe`

**New route: POST /api/actions/sonarr_rescan**
Body: `{"paths": [str]}` — calls Sonarr DownloadedEpisodesScan for each path
Returns: `{"status": "success", "count": int}` or error

**New route: POST /api/actions/radarr_rescan**
Same but DownloadedMoviesScan for Radarr

All routes require @require_auth.

---

## v1.3 — Future Ideas

- Per-file "Trigger Rescan" button in Torrents explorer for Not Imported files
- Lidarr / Readarr support
- Cross-seed integration — feed orphaned media paths to cross-seed CLI
- Unraid Community Applications template
