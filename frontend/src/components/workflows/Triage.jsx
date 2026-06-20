import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../../api'
import { formatBytes, copyText } from '../../utils'
import { useToast } from '../Toast'
import {
  WorkflowHeader, EmptyState, LoadingRow, WorkflowError, WorkflowCrossLink,
  Checkbox, ActionBar, ActionButton, Spinner, SpinKeyframes, HDR_STYLE,
} from './shared'

const VERDICTS = [
  {
    key: 'dead_seed', label: 'Dead Seeds — imported', color: 'var(--green)',
    desc: 'Tracker-dead (trumped, deleted, or nuked) but already imported: your library holds a hardlink to the same data, so deleting these via the client is completely lossless. The safest cleanup there is.',
  },
  {
    key: 'dead_registration', label: 'Dead Registration — payload alive', color: 'var(--green)',
    desc: 'The tracker dropped this torrent, but its data is still alive — seeding on a working cross-seed and/or hardlinked in your library. Remove just the dead registration: auditorr keeps the file when a live seed shares it, and deletes it only when it is this torrent’s own hardlink, so nothing is ever orphaned or broken.',
  },
  {
    key: 'unregistered', label: 'Unregistered — not imported', color: 'var(--red)',
    desc: 'Tracker-dead and NOT in your library — this torrent holds the only copy of the data. Seeding earns nothing, but deleting loses the files, so decide whether anything here is worth keeping first.',
  },
  {
    key: 'superseded', label: 'Superseded', color: 'var(--yellow)',
    desc: 'Your library already has this title, usually at a different quality. Keep seeding for ratio, or delete if the torrent is dead weight.',
  },
  {
    key: 'import_pending', label: 'Import Pending', color: 'var(--blue)',
    desc: 'Managed by Sonarr/Radarr but missing from the library — the import likely failed or was skipped. Trigger a rescan to retry.',
  },
  {
    key: 'not_in_library', label: 'Not in Library', color: 'var(--text-dim)',
    desc: 'No matching title in any Sonarr/Radarr instance. Manual downloads belong in your exclusions; junk can be deleted.',
  },
]

// Superseded sub-buckets — what to do depends on how the orphaned torrent's
// quality compares to the library file it duplicates. The 'duplicate' bucket
// is special: it's a byte-identical copy (not just same quality), so it goes
// first and points at Dedupe instead of being a keep/delete judgement call.
const QUALITY_BUCKETS = [
  {
    key: 'duplicate', label: 'Duplicate of an existing copy', color: 'var(--purple)',
    desc: 'A byte-identical copy already exists on disk but isn’t hardlinked — wasted space. Hardlink it in the Dedupe workflow to reclaim the space losslessly, no need to stop seeding.',
  },
  {
    key: 'higher', label: 'Higher quality than library', color: 'var(--green)',
    desc: 'Better than what you imported — consider a manual import in Sonarr/Radarr to upgrade your library copy before doing anything else.',
  },
  {
    key: 'same', label: 'Same quality as library', color: 'var(--blue)',
    desc: 'A separate release at the same quality (not a byte-identical copy) — pure ratio padding. Keep seeding or delete, nothing to upgrade.',
  },
  {
    key: 'lower', label: 'Lower quality than library', color: 'var(--yellow)',
    desc: 'Your library already has better. Safe to delete once the torrent has earned its keep.',
  },
  {
    key: 'unknown', label: 'Quality comparison unavailable', color: 'var(--text-dim)',
    desc: 'One side of the comparison could not be parsed — check the quality chips on each row before acting.',
  },
]

function itemKey(item) {
  return item.hash || item.rep_path
}

// Which superseded sub-bucket an item belongs to. A byte-identical duplicate
// always wins over its quality comparison — it's a Dedupe target, not a
// keep/delete call — so it never also shows up under same/higher/lower.
function qualityBucket(item) {
  if (item.is_duplicate) return 'duplicate'
  return item.library?.quality_cmp || 'unknown'
}

// Default delete scope: a superseded torrent whose library copy is the better
// one is dead weight → remove the whole cross-seed group. Everything else
// defaults to the single recorded torrent.
function defaultScope(item) {
  return (item.verdict === 'superseded' && item.library?.quality_cmp === 'lower') ? 'all' : 'one'
}

// Compact duration for seeding time — the hit-and-run tiebreaker, so days
// are the unit that matters
function formatDuration(secs) {
  if (secs == null) return null
  const d = secs / 86400
  if (d >= 1) return `${d >= 10 ? Math.round(d) : d.toFixed(1)}d`
  const h = secs / 3600
  if (h >= 1) return `${Math.round(h)}h`
  return `${Math.max(1, Math.round(secs / 60))}m`
}

// Search term for prescreening the qBittorrent/qui search box: the parsed
// title ("The Show") — qBit filters per word, so it matches dotted release
// names AND surfaces every torrent of that title, not just this release.
// Falls back to the release folder / file name when parsing found nothing.
function torrentSearchName(item) {
  if (item.parsed?.title) return item.parsed.title
  const paths = (item.paths || []).map(p => p.replace(/\\/g, '/'))
  if (paths.length === 0) return ''
  const fileBase = () => (paths[0].split('/').pop() || '').replace(/\.[^.]+$/, '')
  if (paths.length === 1) return fileBase()
  const segLists = paths.map(p => p.split('/').slice(0, -1))
  if (segLists.some(s => s.length === 0)) return fileBase()
  let common = segLists[0]
  for (const segs of segLists.slice(1)) {
    let i = 0
    while (i < common.length && i < segs.length && common[i] === segs[i]) i++
    common = common.slice(0, i)
  }
  return common.length > 0 ? common[common.length - 1] : fileBase()
}

// qui can jump straight to a torrent by hash; qBittorrent cannot
function canDeepLink(client, item) {
  return client?.name === 'qui' && item.hash && item.instance_id != null
}

// Exclusion rules for a torrent. Single-file torrents always get an exact
// file-path rule — their parent is often a shared category dir (tv-sonarr)
// that must never be excluded wholesale. Multi-file torrents get their
// common folder only when it sits at least two segments deep
// (category/release-folder); a flatter layout means the common folder IS the
// category dir, so those fall back to per-file rules too.
function exclusionPatterns(item) {
  const paths = (item.paths || []).map(p => p.replace(/\\/g, '/'))
  if (paths.length <= 1) return paths
  const segLists = paths.map(p => p.split('/').slice(0, -1))
  if (segLists.some(s => s.length === 0)) return paths
  let common = segLists[0]
  for (const segs of segLists.slice(1)) {
    let i = 0
    while (i < common.length && i < segs.length && common[i] === segs[i]) i++
    common = common.slice(0, i)
  }
  return common.length >= 2 ? [common.join('/') + '/'] : paths
}

function QualityChip({ label, hdr, dim }) {
  if (!label && !hdr) return <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', opacity: 0.5 }}>unknown</span>
  const hdrInfo = HDR_STYLE[hdr]
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      {label && (
        <span style={{
          fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4,
          background: dim ? 'var(--surface2)' : 'var(--surface3)',
          border: '1px solid var(--border2)',
          color: dim ? 'var(--text-dim)' : 'var(--text)', whiteSpace: 'nowrap',
        }}>
          {label}
        </span>
      )}
      {hdrInfo && (
        <span style={{ fontSize: 9, fontFamily: 'var(--mono)', fontWeight: 700, padding: '1px 4px', borderRadius: 3, background: hdrInfo.bg, color: hdrInfo.color, whiteSpace: 'nowrap' }}>
          {hdr}
        </span>
      )}
    </span>
  )
}

function TriageRow({ item, color, checked, onToggle, client, onOpenClient, onNavigate }) {
  const p = item.parsed || {}
  const seTag = p.season != null
    ? ` · S${String(p.season).padStart(2, '0')}${p.episode != null ? 'E' + String(p.episode).padStart(2, '0') : ' pack'}`
    : ''
  const filename = (item.rep_path || '').replace(/\\/g, '/').split('/').pop()
  const lib = item.library

  return (
    <div
      onClick={onToggle}
      style={{
        display: 'flex', alignItems: 'flex-start', gap: 12, padding: '10px 14px',
        borderBottom: '1px solid var(--border)', cursor: 'pointer',
        background: checked ? 'var(--accent)06' : 'transparent',
      }}
    >
      <span style={{ paddingTop: 3 }}>
        <Checkbox checked={checked} onChange={onToggle} />
      </span>

      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, minWidth: 0 }}>
          <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {p.title || filename}{p.year ? ` (${p.year})` : ''}{seTag}
          </span>
          {item.file_count > 1 && (
            <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
              {item.file_count} files
            </span>
          )}
        </div>
        <div title={item.rep_path} style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', opacity: 0.7, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginTop: 2 }}>
          {filename}
        </div>

        {/* Evidence line */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 5, flexWrap: 'wrap' }}>
          <QualityChip label={p.quality_label} hdr={p.hdr} />
          {lib && lib.quality_name && (
            <>
              <span
                title={`${lib.title}${lib.year ? ` (${lib.year})` : ''}${lib.filename ? ' — ' + lib.filename : ''}`}
                style={{ fontSize: 10, color: 'var(--text-dim)' }}
              >
                vs library{lib.year ? ` (${lib.year})` : ''}
              </span>
              <QualityChip label={lib.quality_name} hdr={lib.hdr} dim />
            </>
          )}
          {item.tracker_msg && (
            <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--red)', opacity: 0.9 }}>
              “{item.tracker_msg}”
            </span>
          )}
          {item.tracker_health === 'not_working' && !item.tracker_msg && (
            <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--yellow)', opacity: 0.8 }}>tracker not responding</span>
          )}
          {item.is_duplicate && (
            <button
              onClick={e => { e.stopPropagation(); onNavigate && onNavigate({ tab: 'dedupe' }) }}
              title="A byte-identical copy exists on disk — open the Dedupe workflow to hardlink it and reclaim the space (lossless)"
              style={{
                fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4, cursor: 'pointer',
                background: 'var(--purple)18', color: 'var(--purple)', border: '1px solid var(--purple)40',
              }}
            >
              Dedupe ↗
            </button>
          )}
        </div>
      </div>

      {/* Fixed-width trailing columns so every row lines up vertically —
          no minWidth stretching, and the arr-link slot is always rendered
          even when empty. */}
      <div style={{ flexShrink: 0, textAlign: 'right', width: 110 }}>
        <div style={{ fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text)' }}>{formatBytes(item.total_size)}</div>
        <div title={(item.trackers || []).join(', ')} style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {(item.trackers || [])[0] || 'no tracker'}{item.trackers?.length > 1 ? ` +${item.trackers.length - 1}` : ''}
        </div>
      </div>

      {/* Earnings — the keep/delete tiebreaker. Seeding time matters more
          than per-torrent ratio on private trackers: it decides whether
          deleting now means a hit-and-run. */}
      <div style={{ flexShrink: 0, textAlign: 'right', width: 100 }}>
        <div style={{ fontSize: 12, fontFamily: 'var(--mono)', color: item.uploaded > 0 ? 'var(--green)' : 'var(--text-dim)' }}>
          {item.uploaded != null ? `↑ ${formatBytes(item.uploaded)}` : '—'}
        </div>
        {item.seeding_time != null && (
          <div title="Total time seeding — check your tracker's hit-and-run rules before deleting"
            style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', marginTop: 2 }}>
            seeded {formatDuration(item.seeding_time)}
          </div>
        )}
      </div>

      <span style={{ flexShrink: 0, width: 58, display: 'flex', justifyContent: 'flex-end' }}>
        {lib?.arr_url && (
          <a
            href={lib.arr_url} target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            title={`Open in ${lib.service}`}
            style={{
              fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4, flexShrink: 0,
              textDecoration: 'none', marginTop: 2, alignSelf: 'flex-start',
              background: lib.service === 'radarr' ? 'var(--yellow)18' : 'var(--blue)18',
              color:      lib.service === 'radarr' ? 'var(--yellow)'   : 'var(--blue)',
              border:     `1px solid ${lib.service === 'radarr' ? 'var(--yellow)' : 'var(--blue)'}40`,
            }}
          >
            {lib.service} ↗
          </a>
        )}
      </span>

      {/* Jump to the client to inspect/delete — qui deep-links to the exact
          torrent; qBittorrent copies the title for a one-paste search. Red
          for unregistered (the verdict that begs for deletion). */}
      {client && (
        <button
          onClick={e => onOpenClient(item, e)}
          title={canDeepLink(client, item)
            ? 'Open this torrent in qui'
            : `Copy “${torrentSearchName(item)}” and open ${client.name} — paste into its search box to find this torrent`}
          style={{
            fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4, flexShrink: 0,
            cursor: 'pointer', marginTop: 2,
            background: item.verdict === 'unregistered' ? 'var(--red)18' : 'var(--surface2)',
            color:      item.verdict === 'unregistered' ? 'var(--red)'   : 'var(--text-dim)',
            border:     `1px solid ${item.verdict === 'unregistered' ? 'var(--red)40' : 'var(--border2)'}`,
          }}
        >
          {client.name} {canDeepLink(client, item) ? '↗' : '⧉↗'}
        </button>
      )}
    </div>
  )
}

export default function Triage({ onNavigate, cleanupCount }) {
  const toast = useToast()
  const [report,   setReport]   = useState(null)
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [busy,     setBusy]     = useState(null)   // 'rescan' | 'exclude' | 'delete' | null
  const [confirmOpen, setConfirmOpen] = useState(false)
  // Cross-seed groups resolved live when the delete modal opens, keyed by the
  // item's recorded hash → [{hash, instance_id, name, tracker, seeding_time…}].
  const [groups,    setGroups]    = useState({})
  const [scopes,    setScopes]    = useState({})   // itemKey → 'one' | 'all'
  const [resolving, setResolving] = useState(false)
  const [resolveError, setResolveError] = useState(null)

  const [client, setClient] = useState(null)   // { name: 'qBittorrent'|'qui', url }
  const [clientDeleteAllowed, setClientDeleteAllowed] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    setSelected(new Set())
    api.triageReport()
      .then(setReport)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
    api.getConfig().then(cfg => {
      const isQui = cfg.TORRENT_SOURCE === 'qui'
      const url = (isQui ? cfg.QUI_HOST : cfg.QB_HOST) || ''
      setClient(url ? { name: isQui ? 'qui' : 'qBittorrent', url } : null)
      setClientDeleteAllowed(!!cfg.ALLOW_CLIENT_DELETE)
    }).catch(() => {})
  }, [])

  const openInClient = useCallback((item, e) => {
    e.stopPropagation()
    if (!client) return
    // qui deep-links straight to the torrent: /instances/{id}?torrent={hash}
    // selects it and opens the details pane. Stock qBittorrent's WebUI reads
    // no URL params, so it gets the copy-title-and-paste flow instead.
    if (canDeepLink(client, item)) {
      window.open(`${client.url.replace(/\/+$/, '')}/instances/${item.instance_id}?torrent=${item.hash}`, '_blank', 'noopener')
      return
    }
    const term = torrentSearchName(item)
    copyText(term)
    window.open(client.url, '_blank', 'noopener')
    toast(`“${term}” copied — paste it into the ${client.name} search box to find this torrent`, 'info')
  }, [client, toast])

  useEffect(() => { load() }, [load])

  const items = report?.items || []
  const byVerdict = useMemo(() => {
    const m = {}
    for (const v of VERDICTS) m[v.key] = []
    for (const item of items) (m[item.verdict] || (m[item.verdict] = [])).push(item)
    return m
  }, [items])

  const selectedItems = useMemo(() => items.filter(i => selected.has(itemKey(i))), [items, selected])
  const selectedSize  = selectedItems.reduce((s, i) => s + i.total_size, 0)
  const selectedPaths = selectedItems.flatMap(i => i.paths || [])

  const toggle = useCallback(key => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }, [])

  const toggleSection = useCallback((sectionItems) => {
    setSelected(prev => {
      const keys = sectionItems.map(itemKey)
      const allIn = keys.every(k => prev.has(k))
      const next = new Set(prev)
      keys.forEach(k => allIn ? next.delete(k) : next.add(k))
      return next
    })
  }, [])

  // Items deletable through the client need a torrent hash; path-keyed
  // entries (hash unknown) can only be handled manually.
  const deletableItems = selectedItems.filter(i => i.hash)

  // Open the confirm modal and resolve each selected torrent's live cross-seed
  // group so the user can choose per-item: delete just this torrent, or the
  // whole hardlinked group. Deletion is hardlink-safe either way.
  const openConfirm = useCallback(async () => {
    setConfirmOpen(true)
    setResolveError(null)
    setResolving(true)
    setScopes(() => {
      const m = {}
      for (const i of deletableItems) m[itemKey(i)] = defaultScope(i)
      return m
    })
    try {
      const resp = await api.triageResolveGroups(deletableItems.map(i => i.hash))
      setGroups(resp.groups || {})
    } catch (e) {
      setResolveError(e.message)
      setGroups({})
    }
    setResolving(false)
  }, [deletableItems])

  const setItemScope = useCallback((key, scope) => {
    setScopes(prev => ({ ...prev, [key]: scope }))
  }, [])

  const setAllScopes = useCallback((scope) => {
    setScopes(() => {
      const m = {}
      for (const i of deletableItems) m[itemKey(i)] = scope
      return m
    })
  }, [deletableItems])

  // Flatten the selection into a deduped removal set, honoring each item's
  // scope: 'all' pulls every hash in its cross-seed group, 'one' just the
  // recorded torrent.
  const buildRemovalItems = useCallback(() => {
    const byHash = new Map()
    for (const item of deletableItems) {
      const grp = groups[item.hash] || []
      if ((scopes[itemKey(item)] || 'one') === 'all' && grp.length > 0) {
        for (const m of grp) byHash.set(m.hash, { hash: m.hash, instance_id: m.instance_id })
      } else {
        byHash.set(item.hash, { hash: item.hash, instance_id: item.instance_id })
      }
    }
    return [...byHash.values()]
  }, [deletableItems, groups, scopes])

  const handleClientDelete = async () => {
    setBusy('delete')
    try {
      // 'auto': the server deletes each torrent's files only when no surviving
      // torrent shares them, so a live cross-seed sibling is never broken and a
      // distinct hardlink is never left orphaned — safe across every topology.
      const resp = await api.removeTorrents(buildRemovalItems(), 'auto')
      const parts = [`Removed ${resp.removed} torrent${resp.removed !== 1 ? 's' : ''} via ${client?.name || 'the client'}`]
      if (resp.files_deleted) parts.push(`${resp.files_deleted} with files deleted`)
      if (resp.files_kept)    parts.push(`${resp.files_kept} kept (still seeded elsewhere)`)
      toast(parts.join(' · '), 'success')
      const keys = new Set(deletableItems.map(itemKey))
      setReport(r => ({ ...r, items: (r?.items || []).filter(i => !keys.has(itemKey(i))) }))
      setSelected(new Set())
      setConfirmOpen(false)
    } catch (e) {
      toast(e.message, 'error')
    }
    setBusy(null)
  }

  const handleRescan = async () => {
    setBusy('rescan')
    const sonarrPaths = []
    const radarrPaths = []
    for (const item of selectedItems) {
      const svc = item.library?.service
      if (svc === 'radarr') radarrPaths.push(item.rep_path)
      else if (svc === 'sonarr') sonarrPaths.push(item.rep_path)
      else { sonarrPaths.push(item.rep_path); radarrPaths.push(item.rep_path) }
    }
    let ok = 0
    try { if (sonarrPaths.length) { await api.sonarrRescan(sonarrPaths); ok++ } } catch (e) { toast(e.message, 'error') }
    try { if (radarrPaths.length) { await api.radarrRescan(radarrPaths); ok++ } } catch (e) { toast(e.message, 'error') }
    if (ok > 0) toast('Rescan triggered — check Sonarr/Radarr for import results', 'success')
    setBusy(null)
  }

  const handleExclude = async () => {
    setBusy('exclude')
    const patterns = [...new Set(selectedItems.flatMap(exclusionPatterns))]
    try {
      const resp = await api.excludePatterns(patterns)
      toast(`Added ${resp.added} exclusion rule${resp.added !== 1 ? 's' : ''} — applies from the next audit`, 'success')
      const keys = new Set(selectedItems.map(itemKey))
      setReport(r => ({ ...r, items: (r?.items || []).filter(i => !keys.has(itemKey(i))) }))
      setSelected(new Set())
    } catch (e) {
      toast(e.message, 'error')
    }
    setBusy(null)
  }

  // One-click exclude for a suggested junk category. The real exclusion
  // applies on the next audit, so drop the matching rows now (by the
  // suggestion's lowercase match substrings) to clear the reminder.
  const handleSuggestionExclude = async (sugg) => {
    setBusy('suggest')
    try {
      const resp = await api.excludePatterns(sugg.patterns)
      toast(`Excluding ${sugg.patterns.join(', ')} — added ${resp.added} rule${resp.added !== 1 ? 's' : ''}, visible in Config → Excluded Files`, 'success')
      const hit = p => { const lp = p.toLowerCase(); return (sugg.match || []).some(m => lp.includes(m)) }
      setReport(r => ({
        ...r,
        items: (r?.items || []).filter(i => !hit(i.rep_path)),
        suggestions: (r?.suggestions || []).filter(s => s.id !== sugg.id),
      }))
    } catch (e) {
      toast(e.message, 'error')
    }
    setBusy(null)
  }

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 22 }}>
      <WorkflowHeader
        title="Triage"
        accent="var(--red)"
        blurb="Every torrent that needs your attention: dead on the tracker (imported or not), quality superseded, import failures, or not in your library at all — and what to do about each."
        right={!loading && (
          <button onClick={load} style={{
            fontSize: 12, padding: '6px 16px', borderRadius: 7, cursor: 'pointer',
            border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)',
          }}>↻ Refresh</button>
        )}
      />

      <WorkflowError message={error} />

      {!loading && (
        <WorkflowCrossLink
          text="Everything here has an active torrent. Files with no torrent attached:"
          linkLabel="Cleanup"
          count={cleanupCount}
          onClick={() => onNavigate && onNavigate({ tab: 'cleanup' })}
        />
      )}

      {loading && <LoadingRow label="Inspecting torrents — querying tracker status and your Sonarr/Radarr libraries…" />}

      {!loading && !error && items.length === 0 && (
        <EmptyState
          title="Nothing to triage"
          sub="Every seeding torrent is imported and registered on its tracker. All clear."
        />
      )}

      {!loading && items.length > 0 && (
        <>
          {report?.truncated && (
            <div style={{ fontSize: 12, color: 'var(--text-dim)', fontFamily: 'var(--mono)' }}>
              Showing the 500 largest torrents — resolve some to see the rest.
            </div>
          )}
          {!report?.arr_configured && (
            <div style={{ padding: '10px 14px', background: 'var(--yellow)10', border: '1px solid var(--yellow)30', borderRadius: 8, color: 'var(--yellow)', fontSize: 12.5 }}>
              No Sonarr/Radarr connection configured — library matching is disabled, so most items fall into “Not in Library”.
            </div>
          )}

          {report?.suggestions?.length > 0 && (
            <div style={{ padding: '12px 14px', background: 'var(--surface)', border: '1px dashed var(--border2)', borderRadius: 8, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ fontSize: 12.5, color: 'var(--text)' }}>
                <span style={{ fontWeight: 600 }}>Tidy up Triage</span>
                <span style={{ color: 'var(--text-dim)' }}> — some torrents only linger here because of files Sonarr/Radarr never import. Exclude them in one click (saved to Config → Excluded Files, editable there):</span>
              </div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {report.suggestions.map(s => (
                  <button
                    key={s.id}
                    onClick={() => handleSuggestionExclude(s)}
                    disabled={busy != null}
                    title={`${s.detail} — adds ${s.patterns.join(', ')}`}
                    style={{
                      display: 'inline-flex', alignItems: 'center', gap: 7, padding: '5px 11px', borderRadius: 7,
                      fontSize: 12, cursor: busy != null ? 'not-allowed' : 'pointer', opacity: busy != null ? 0.5 : 1,
                      border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)',
                    }}
                  >
                    <span>Exclude <span style={{ fontFamily: 'var(--mono)', color: 'var(--accent)' }}>{s.label}</span></span>
                    <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
                      {s.count} · {formatBytes(s.size)}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {VERDICTS.map(v => {
            const sectionItems = byVerdict[v.key] || []
            if (sectionItems.length === 0) return null
            const sectionSize = sectionItems.reduce((s, i) => s + i.total_size, 0)
            const keys = sectionItems.map(itemKey)
            const allChecked  = keys.every(k => selected.has(k))
            const someChecked = !allChecked && keys.some(k => selected.has(k))

            const renderRows = (rowItems) => (
              <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 9, boxShadow: 'var(--elev-1)', overflow: 'hidden' }}>
                {rowItems.map(item => (
                  <TriageRow
                    key={itemKey(item)}
                    item={item}
                    color={v.color}
                    checked={selected.has(itemKey(item))}
                    onToggle={() => toggle(itemKey(item))}
                    client={client}
                    onOpenClient={openInClient}
                    onNavigate={onNavigate}
                  />
                ))}
              </div>
            )

            return (
              <div key={v.key}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                  <Checkbox checked={allChecked} indeterminate={someChecked} onChange={() => toggleSection(sectionItems)} />
                  <span style={{ width: 9, height: 9, borderRadius: 3, background: v.color, flexShrink: 0 }} />
                  <span style={{ fontSize: 14, fontWeight: 700, color: 'var(--text)' }}>{v.label}</span>
                  <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
                    {sectionItems.length} · {formatBytes(sectionSize)}
                  </span>
                </div>
                <p style={{ fontSize: 12, color: 'var(--text-dim)', margin: '0 0 10px 34px', lineHeight: 1.5, maxWidth: 960 }}>
                  {v.desc}
                </p>
                {v.key !== 'superseded' ? renderRows(sectionItems) : (
                  QUALITY_BUCKETS.map(b => {
                    const bucketItems = sectionItems.filter(i => qualityBucket(i) === b.key)
                    if (bucketItems.length === 0) return null
                    const bKeys = bucketItems.map(itemKey)
                    const bAll  = bKeys.every(k => selected.has(k))
                    const bSome = !bAll && bKeys.some(k => selected.has(k))
                    const bSize = bucketItems.reduce((s, i) => s + i.total_size, 0)
                    return (
                      <div key={b.key} style={{ marginLeft: 34, marginBottom: 16 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
                          <Checkbox checked={bAll} indeterminate={bSome} onChange={() => toggleSection(bucketItems)} />
                          <span style={{ fontSize: 12.5, fontWeight: 600, color: b.color }}>{b.label}</span>
                          <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
                            {bucketItems.length} · {formatBytes(bSize)}
                          </span>
                        </div>
                        <p style={{ fontSize: 11.5, color: 'var(--text-dim)', margin: '0 0 8px 25px', lineHeight: 1.5, maxWidth: 960 }}>
                          {b.desc}
                        </p>
                        {renderRows(bucketItems)}
                      </div>
                    )
                  })
                )}
              </div>
            )
          })}

          {selectedItems.length > 0 && (
            <ActionBar summary={`${selectedItems.length} torrent${selectedItems.length !== 1 ? 's' : ''} selected · ${selectedPaths.length} file${selectedPaths.length !== 1 ? 's' : ''} · ${formatBytes(selectedSize)}`}>
              <ActionButton onClick={handleRescan} disabled={busy != null} title="Tell Sonarr/Radarr to rescan these folders and retry the import">
                {busy === 'rescan' ? 'Rescanning…' : 'Trigger Rescan'}
              </ActionButton>
              <ActionButton onClick={handleExclude} disabled={busy != null} title="Add exclusion rules so auditorr stops flagging these">
                {busy === 'exclude' ? 'Excluding…' : 'Exclude'}
              </ActionButton>
              {clientDeleteAllowed && client && (
                <ActionButton danger onClick={openConfirm} disabled={busy != null || deletableItems.length === 0}
                  title={deletableItems.length === 0
                    ? 'None of the selected items have a torrent hash'
                    : `Remove the selected torrents via ${client.name} — files deleted only where no live seed shares them`}>
                  Remove from {client.name}
                </ActionButton>
              )}
            </ActionBar>
          )}

          {confirmOpen && (
            <ConfirmDeleteModal
              items={deletableItems}
              groups={groups}
              scopes={scopes}
              resolving={resolving}
              resolveError={resolveError}
              onSetItemScope={setItemScope}
              onSetAllScopes={setAllScopes}
              skippedCount={selectedItems.length - deletableItems.length}
              clientName={client?.name || 'client'}
              busy={busy === 'delete'}
              onCancel={() => setConfirmOpen(false)}
              onConfirm={handleClientDelete}
            />
          )}
        </>
      )}
      <SpinKeyframes />
    </div>
  )
}

// A small two-way scope switch: delete just the recorded torrent, or the whole
// hardlinked cross-seed group.
function ScopeSwitch({ scope, groupSize, onChange }) {
  const opts = [
    { key: 'one', label: 'This torrent only' },
    { key: 'all', label: `All ${groupSize} cross-seeds` },
  ]
  return (
    <div style={{ display: 'inline-flex', border: '1px solid var(--border2)', borderRadius: 6, overflow: 'hidden' }}>
      {opts.map((o, idx) => {
        const active = scope === o.key
        return (
          <button
            key={o.key}
            onClick={() => onChange(o.key)}
            style={{
              fontSize: 10.5, fontFamily: 'var(--mono)', padding: '3px 9px', cursor: 'pointer',
              border: 'none', borderLeft: idx ? '1px solid var(--border2)' : 'none',
              background: active ? (o.key === 'all' ? 'var(--red)20' : 'var(--surface3)') : 'transparent',
              color: active ? (o.key === 'all' ? 'var(--red)' : 'var(--text)') : 'var(--text-dim)',
              fontWeight: active ? 700 : 400,
            }}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

function ConfirmDeleteModal({
  items, groups, scopes, resolving, resolveError,
  onSetItemScope, onSetAllScopes, skippedCount, clientName, busy, onCancel, onConfirm,
}) {
  const totalSize = items.reduce((s, i) => s + i.total_size, 0)
  const anyGroups = items.some(i => (groups[i.hash] || []).length > 1)

  // Deduped set of every torrent hash that will actually be removed.
  const removalHashes = new Set()
  for (const item of items) {
    const grp = groups[item.hash] || []
    if ((scopes[itemKey(item)] || 'one') === 'all' && grp.length > 0) {
      grp.forEach(m => removalHashes.add(m.hash))
    } else {
      removalHashes.add(item.hash)
    }
  }
  const torrentCount = removalHashes.size

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  // Portal to <body>: the page container's fade-in animation leaves a
  // persistent transform, which makes position:fixed resolve against the
  // (very tall) page instead of the viewport — the dialog would center
  // thousands of pixels off-screen, leaving only the dimmed backdrop.
  return createPortal(
    <div
      onClick={onCancel}
      style={{
        position: 'fixed', inset: 0, zIndex: 200, display: 'flex',
        alignItems: 'center', justifyContent: 'center',
        background: 'rgba(0,0,0,0.55)',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: 'min(560px, calc(100vw - 48px))', maxHeight: 'calc(100vh - 96px)',
          display: 'flex', flexDirection: 'column',
          background: 'var(--surface)', border: '1px solid var(--border2)',
          borderRadius: 12, boxShadow: '0 16px 60px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ padding: '18px 20px 0' }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--red)' }}>Remove from {clientName}</div>
          <p style={{ fontSize: 12.5, color: 'var(--text)', lineHeight: 1.6, margin: '10px 0 0' }}>
            Removes <b>{torrentCount} torrent{torrentCount !== 1 ? 's' : ''}</b> (<b>{formatBytes(totalSize)}</b>) from {clientName}.
            Each torrent’s files are deleted <b>only</b> where no surviving torrent still shares them — any file still
            seeded by a cross-seed sibling is kept, so no live seed is broken. {clientName} reports the split afterwards. There is no undo.
          </p>
          {skippedCount > 0 && (
            <p style={{ fontSize: 11.5, color: 'var(--text-dim)', margin: '8px 0 0' }}>
              {skippedCount} selected item{skippedCount !== 1 ? 's have' : ' has'} no torrent hash and will be skipped.
            </p>
          )}
          {resolveError && (
            <p style={{ fontSize: 11.5, color: 'var(--yellow)', margin: '8px 0 0' }}>
              Couldn’t resolve cross-seed groups ({resolveError}) — only the listed torrents will be deleted.
            </p>
          )}
          {anyGroups && !resolving && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '12px 0 0', fontSize: 11.5, color: 'var(--text-dim)' }}>
              <span>Choose how much of each cross-seed group to remove (files are kept or dropped automatically per torrent). Apply to all:</span>
              <button onClick={() => onSetAllScopes('one')} style={miniBtn}>This torrent only</button>
              <button onClick={() => onSetAllScopes('all')} style={miniBtn}>All cross-seeds</button>
            </div>
          )}
        </div>
        <div style={{ margin: '14px 20px 0', border: '1px solid var(--border)', borderRadius: 8, overflowY: 'auto', flex: '0 1 auto' }}>
          {resolving && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 14px', fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
              <Spinner /> Finding cross-seeds in {clientName}…
            </div>
          )}
          {items.map(item => {
            const grp   = groups[item.hash] || []
            const scope = scopes[itemKey(item)] || 'one'
            const hasGroup = grp.length > 1
            const paths = (item.paths || []).map(p => p.replace(/\\/g, '/'))
            const shownPaths = paths.slice(0, 6)
            return (
              <div key={itemKey(item)} style={{ padding: '8px 12px', borderBottom: '1px solid var(--border)' }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                  <span style={{ flex: 1, minWidth: 0, fontSize: 12, fontWeight: 600, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {item.parsed?.title || (item.rep_path || '').replace(/\\/g, '/').split('/').pop()}
                    {item.parsed?.year ? ` (${item.parsed.year})` : ''}
                  </span>
                  {item.seeding_time != null && (
                    <span title="Total time seeding" style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
                      seeded {formatDuration(item.seeding_time)}
                    </span>
                  )}
                  <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text)', flexShrink: 0 }}>
                    {formatBytes(item.total_size)}
                  </span>
                </div>

                {hasGroup ? (
                  <>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
                      <ScopeSwitch scope={scope} groupSize={grp.length} onChange={s => onSetItemScope(itemKey(item), s)} />
                    </div>
                    <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 3 }}>
                      {grp.map(m => {
                        const willRemove = scope === 'all' || m.hash === item.hash
                        // Files survive removal when a kept sibling shares them.
                        // scope 'all' leaves no kept member → files are deleted;
                        // scope 'one' keeps the rest → a shared path is kept.
                        const fileKept = willRemove && scope === 'one' && m.shares_path
                        return (
                          <div key={m.hash} title={m.name} style={{ display: 'flex', alignItems: 'baseline', gap: 8, fontSize: 10, fontFamily: 'var(--mono)' }}>
                            <span style={{
                              flexShrink: 0, width: 48, color: willRemove ? 'var(--red)' : 'var(--text-dim)',
                              fontWeight: willRemove ? 700 : 400,
                            }}>
                              {willRemove ? 'remove' : 'keep'}
                            </span>
                            <span style={{ flex: 1, minWidth: 0, color: willRemove ? 'var(--text)' : 'var(--text-dim)', opacity: willRemove ? 1 : 0.7, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {m.name}
                            </span>
                            {willRemove && (
                              <span title={fileKept ? 'A surviving cross-seed shares this file — it is kept' : 'No surviving torrent shares this file — it is deleted'}
                                    style={{ flexShrink: 0, color: fileKept ? 'var(--text-dim)' : 'var(--red)' }}>
                                {fileKept ? 'file kept' : 'file deleted'}
                              </span>
                            )}
                            {m.tracker && <span style={{ flexShrink: 0, color: 'var(--text-dim)' }}>{m.tracker}</span>}
                            {m.seeding_time != null && <span style={{ flexShrink: 0, color: 'var(--text-dim)' }}>{formatDuration(m.seeding_time)}</span>}
                          </div>
                        )
                      })}
                    </div>
                  </>
                ) : (
                  <>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginTop: 3, fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
                      <span title={item.hash} style={{ flexShrink: 0 }}>hash {String(item.hash).slice(0, 12)}…</span>
                      {(item.trackers || [])[0] && <span style={{ flexShrink: 0 }}>{item.trackers[0]}</span>}
                      {!resolving && <span style={{ flexShrink: 0 }}>no cross-seeds</span>}
                    </div>
                    <div style={{ marginTop: 4 }}>
                      {shownPaths.map(p => (
                        <div key={p} title={p} style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', opacity: 0.75, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {p}
                        </div>
                      ))}
                      {paths.length > shownPaths.length && (
                        <div style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', opacity: 0.6 }}>
                          +{paths.length - shownPaths.length} more files
                        </div>
                      )}
                    </div>
                  </>
                )}
              </div>
            )
          })}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '14px 20px 18px' }}>
          {busy && (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
              <Spinner /> Removing via {clientName}…
            </span>
          )}
          <span style={{ flex: 1 }} />
          <ActionButton onClick={onCancel}>{busy ? 'Continue in background' : 'Cancel'}</ActionButton>
          <ActionButton danger onClick={onConfirm} disabled={busy || resolving}>
            {busy ? 'Removing…' : `Remove ${torrentCount} torrent${torrentCount !== 1 ? 's' : ''}`}
          </ActionButton>
        </div>
      </div>
    </div>,
    document.body
  )
}

const miniBtn = {
  fontSize: 10.5, fontFamily: 'var(--mono)', padding: '2px 8px', borderRadius: 5, cursor: 'pointer',
  border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)',
}
