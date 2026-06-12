import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { api } from '../../api'
import { formatBytes, copyText } from '../../utils'
import { useToast } from '../Toast'
import {
  WorkflowHeader, EmptyState, LoadingRow, WorkflowError, WorkflowCrossLink,
  Checkbox, ActionBar, ActionButton, SpinKeyframes, HDR_STYLE,
} from './shared'

const VERDICTS = [
  {
    key: 'dead_seed', label: 'Dead Seeds — imported', color: 'var(--green)',
    desc: 'Tracker-dead (trumped, deleted, or nuked) but already imported: your library holds a hardlink to the same data, so deleting these via the client is completely lossless. The safest cleanup there is.',
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
// quality compares to the library file it duplicates.
const QUALITY_BUCKETS = [
  {
    key: 'higher', label: 'Higher quality than library', color: 'var(--green)',
    desc: 'Better than what you imported — consider a manual import in Sonarr/Radarr to upgrade your library copy before doing anything else.',
  },
  {
    key: 'same', label: 'Same quality as library', color: 'var(--blue)',
    desc: 'An alternate copy at the same quality — pure ratio padding. Keep seeding or delete, nothing to upgrade.',
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

function TriageRow({ item, color, checked, onToggle, client, onOpenClient }) {
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
        </div>
      </div>

      {/* Earnings — the keep/delete tiebreaker */}
      <div style={{ flexShrink: 0, textAlign: 'right', minWidth: 86 }}>
        <div style={{ fontSize: 12, fontFamily: 'var(--mono)', color: item.uploaded > 0 ? 'var(--green)' : 'var(--text-dim)' }}>
          {item.uploaded != null ? `↑ ${formatBytes(item.uploaded)}` : '—'}
        </div>
        {item.ratio != null && (
          <div style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', marginTop: 2 }}>
            ratio {item.ratio.toFixed(2)}
          </div>
        )}
      </div>

      <div style={{ flexShrink: 0, textAlign: 'right', minWidth: 64 }}>
        <div style={{ fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text)' }}>{formatBytes(item.total_size)}</div>
        <div title={(item.trackers || []).join(', ')} style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 110 }}>
          {(item.trackers || [])[0] || 'no tracker'}{item.trackers?.length > 1 ? ` +${item.trackers.length - 1}` : ''}
        </div>
      </div>

      {lib?.arr_url && (
        <a
          href={lib.arr_url} target="_blank" rel="noopener noreferrer"
          onClick={e => e.stopPropagation()}
          title={`Open in ${lib.service}`}
          style={{
            fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4, flexShrink: 0,
            textDecoration: 'none', marginTop: 2,
            background: lib.service === 'radarr' ? 'var(--yellow)18' : 'var(--blue)18',
            color:      lib.service === 'radarr' ? 'var(--yellow)'   : 'var(--blue)',
            border:     `1px solid ${lib.service === 'radarr' ? 'var(--yellow)' : 'var(--blue)'}40`,
          }}
        >
          {lib.service} ↗
        </a>
      )}

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

  const handleClientDelete = async () => {
    setBusy('delete')
    try {
      const resp = await api.removeTorrents(
        deletableItems.map(i => ({ hash: i.hash, instance_id: i.instance_id }))
      )
      toast(`Removed ${resp.removed} torrent${resp.removed !== 1 ? 's' : ''} and their files via ${client?.name || 'the client'}`, 'success')
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

          {VERDICTS.map(v => {
            const sectionItems = byVerdict[v.key] || []
            if (sectionItems.length === 0) return null
            const sectionSize = sectionItems.reduce((s, i) => s + i.total_size, 0)
            const keys = sectionItems.map(itemKey)
            const allChecked  = keys.every(k => selected.has(k))
            const someChecked = !allChecked && keys.some(k => selected.has(k))

            const renderRows = (rowItems) => (
              <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 9, overflow: 'hidden' }}>
                {rowItems.map(item => (
                  <TriageRow
                    key={itemKey(item)}
                    item={item}
                    color={v.color}
                    checked={selected.has(itemKey(item))}
                    onToggle={() => toggle(itemKey(item))}
                    client={client}
                    onOpenClient={openInClient}
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
                <p style={{ fontSize: 12, color: 'var(--text-dim)', margin: '0 0 10px 34px', lineHeight: 1.5, maxWidth: 640 }}>
                  {v.desc}
                </p>
                {v.key !== 'superseded' ? renderRows(sectionItems) : (
                  QUALITY_BUCKETS.map(b => {
                    const bucketItems = sectionItems.filter(i => (i.library?.quality_cmp || 'unknown') === b.key)
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
                        <p style={{ fontSize: 11.5, color: 'var(--text-dim)', margin: '0 0 8px 25px', lineHeight: 1.5, maxWidth: 620 }}>
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
                <ActionButton danger onClick={() => setConfirmOpen(true)} disabled={busy != null || deletableItems.length === 0}
                  title={deletableItems.length === 0
                    ? 'None of the selected items have a torrent hash'
                    : `Remove the selected torrents AND their downloaded files via ${client.name}`}>
                  Delete from {client.name}
                </ActionButton>
              )}
            </ActionBar>
          )}

          {confirmOpen && (
            <ConfirmDeleteModal
              items={deletableItems}
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

function ConfirmDeleteModal({ items, skippedCount, clientName, busy, onCancel, onConfirm }) {
  const totalSize  = items.reduce((s, i) => s + i.total_size, 0)
  const totalFiles = items.reduce((s, i) => s + i.file_count, 0)
  return (
    <div
      onClick={busy ? undefined : onCancel}
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
          <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--red)' }}>Delete from {clientName}</div>
          <p style={{ fontSize: 12.5, color: 'var(--text)', lineHeight: 1.6, margin: '10px 0 0' }}>
            This permanently removes <b>{items.length} torrent{items.length !== 1 ? 's' : ''}</b> from {clientName} and
            deletes <b>{totalFiles} file{totalFiles !== 1 ? 's' : ''} · {formatBytes(totalSize)}</b> from disk. There is no undo.
          </p>
          {skippedCount > 0 && (
            <p style={{ fontSize: 11.5, color: 'var(--text-dim)', margin: '8px 0 0' }}>
              {skippedCount} selected item{skippedCount !== 1 ? 's have' : ' has'} no torrent hash and will be skipped.
            </p>
          )}
        </div>
        <div style={{ margin: '14px 20px 0', border: '1px solid var(--border)', borderRadius: 8, overflowY: 'auto', flex: '0 1 auto' }}>
          {items.map(item => (
            <div key={itemKey(item)} style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '7px 12px', borderBottom: '1px solid var(--border)' }}>
              <span style={{ flex: 1, minWidth: 0, fontSize: 12, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {item.parsed?.title || (item.rep_path || '').replace(/\\/g, '/').split('/').pop()}
              </span>
              <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
                {formatBytes(item.total_size)}
              </span>
            </div>
          ))}
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, padding: '14px 20px 18px' }}>
          <ActionButton onClick={onCancel} disabled={busy}>Cancel</ActionButton>
          <ActionButton danger onClick={onConfirm} disabled={busy}>
            {busy ? 'Deleting…' : `Delete ${items.length} torrent${items.length !== 1 ? 's' : ''} + files`}
          </ActionButton>
        </div>
      </div>
    </div>
  )
}
