# auditorr documentation

| Page | What's in it |
| --- | --- |
| [Configuration](configuration.md) | Every setting in the Config tab, every environment variable, and the exclusion syntax. |
| [Workflows](workflows.md) | The five workflows in depth — what each one targets, what it does, and what it will never do. |
| [Remote access](remote-access.md) | Who is allowed to talk to auditorr, access keys, VPNs and reverse proxies. |
| [Troubleshooting](troubleshooting.md) | Symptom-first answers, and how to file a useful issue. |

New here? The [README](../README.md) covers installation.

## How auditorr works, in one page

auditorr walks two directory trees — your media library and your torrent
folder — and cross-references them against qBittorrent or qui.

Because a hardlink-based setup stores one copy of a file that appears in both
trees, auditorr can tell which of your seeding torrents actually back a library
file, which library files nothing is seeding, and which files exist twice on
disk for no reason. That produces four numbers, a health score, and five
workflows to fix what the numbers found.

Every audit is a full re-walk. Nothing is written to your library: auditorr
mounts your data read-only and its destructive actions are opt-in, reviewed,
and confirmed — either a script you read before running, or a torrent-client
call behind a confirmation dialog.

Results are stored in SQLite under `/app/data`, so history, per-tracker upload
snapshots, and the file-level change log survive restarts.
