// How a new library video arrived (2026-11-20): `arrived` on a library
// `new_files` entry, in the New in the library card's words and colours. A
// moved or renamed file is not new, and the card doesn't count it. Entries from
// before carry none, nor do the files the card doesn't count (anything but a
// video, or excluded), and those read "New media".
export const ARRIVALS = {
  linked:   { label: 'New · linked',     color: 'var(--map-linked)' },
  unseeded: { label: 'New · not seeded', color: 'var(--blue)' },
  copies:   { label: 'New · copy',       color: 'var(--purple)' },
  moved:    { label: 'Moved or renamed', color: 'var(--text-dim)' },
}

export const CHANGE_CATEGORIES = [
  {
    key: 'newly_orphaned',
    diffKey: 'newly_orphaned',
    tab: null,
    label: 'Orphaned',
    color: 'var(--yellow)',
    navigable: true,
  },
  {
    key: 'new_duplicates',
    diffKey: 'new_duplicates',
    tab: null,
    label: 'New dupe',
    color: 'var(--purple)',
    navigable: true,
  },
  {
    key: 'newly_imported',
    diffKey: 'newly_imported',
    tab: null,
    label: 'Imported',
    color: 'var(--green)',
    navigable: true,
  },
  {
    key: 'resolved_duplicates',
    diffKey: 'resolved_duplicates',
    tab: null,
    label: 'Dupe resolved',
    color: 'var(--blue)',
    navigable: true,
  },
  {
    key: 'new_torrent',
    diffKey: 'new_files',
    tab: 'torrents',
    label: 'New torrent',
    color: 'var(--blue)',
    navigable: true,
  },
  {
    key: 'new_media',
    diffKey: 'new_files',
    tab: 'media',
    label: 'New media',
    color: 'var(--cyan)',
    navigable: true,
  },
  {
    key: 'removed_torrent',
    diffKey: 'removed_files',
    tab: 'torrents',
    label: 'Removed torrent',
    color: 'var(--red)',
    navigable: false,
  },
  {
    key: 'removed_media',
    diffKey: 'removed_files',
    tab: 'media',
    label: 'Removed media',
    color: 'var(--yellow)',
    navigable: false,
  },
]
