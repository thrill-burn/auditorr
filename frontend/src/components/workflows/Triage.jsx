import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { api } from '../../api'
import { formatBytes } from '../../utils'
import { useToast } from '../Toast'
import {
  WorkflowHeader, EmptyState, LoadingRow, WorkflowError,
  Checkbox, ActionBar, ActionButton, SpinKeyframes, HDR_STYLE,
} from './shared'

const VERDICTS = [
  {
    key: 'unregistered', label: 'Unregistered', color: 'var(--red)',
    desc: 'The tracker no longer recognizes these torrents — trumped, deleted, or nuked. Seeding them earns nothing. Safe to delete.',
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

function itemKey(item) {
  return item.hash || item.rep_path
}

// Deepest common directory of a torrent's files — what an exclusion rule
// should cover. Falls back to the exact file path for single rootless files.
function exclusionPattern(item) {
  const paths = (item.paths || []).map(p => p.replace(/\\/g, '/'))
  if (paths.length === 0) return null
  const segLists = paths.map(p => p.split('/').slice(0, -1))
  if (segLists.some(s => s.length === 0)) return paths[0]
  let common = segLists[0]
  for (const segs of segLists.slice(1)) {
    let i = 0
    while (i < common.length && i < segs.length && common[i] === segs[i]) i++
    common = common.slice(0, i)
  }
  return common.length > 0 ? common.join('/') + '/' : paths[0]
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

function TriageRow({ item, color, checked, onToggle }) {
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
            {p.title || filename}{seTag}
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
              <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>vs library</span>
              <QualityChip label={lib.quality_name} hdr={lib.hdr} dim />
              {lib.same_quality && (
                <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', opacity: 0.7 }}>same quality — alternate copy</span>
              )}
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
    </div>
  )
}

export default function Triage({ onNavigate, onScript }) {
  const toast = useToast()
  const [report,   setReport]   = useState(null)
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [busy,     setBusy]     = useState(null)   // 'rescan' | 'exclude' | null

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    setSelected(new Set())
    api.triageReport()
      .then(setReport)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

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

  const handleDeleteScript = () => {
    onScript({
      scriptType: 'delete_selected',
      title: 'Delete Selected Files',
      subtitle: `${selectedItems.length} torrent${selectedItems.length !== 1 ? 's' : ''} · ${formatBytes(selectedSize)}`,
      body: { paths: selectedPaths },
    })
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
    const patterns = [...new Set(selectedItems.map(exclusionPattern).filter(Boolean))]
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
        blurb="Explains why each seeding torrent was never imported — quality superseded, dead on the tracker, import failure, or not in your library at all — and what to do about it."
        right={!loading && (
          <button onClick={load} style={{
            fontSize: 12, padding: '6px 16px', borderRadius: 7, cursor: 'pointer',
            border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)',
          }}>↻ Refresh</button>
        )}
      />

      <WorkflowError message={error} />

      {loading && <LoadingRow label="Inspecting torrents — querying tracker status and your Sonarr/Radarr libraries…" />}

      {!loading && !error && items.length === 0 && (
        <EmptyState
          title="Everything is imported"
          sub="Every seeding torrent has a matching file in your media library. Nothing to triage."
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
                <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 9, overflow: 'hidden' }}>
                  {sectionItems.map(item => (
                    <TriageRow
                      key={itemKey(item)}
                      item={item}
                      color={v.color}
                      checked={selected.has(itemKey(item))}
                      onToggle={() => toggle(itemKey(item))}
                    />
                  ))}
                </div>
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
              <ActionButton danger onClick={handleDeleteScript} disabled={busy != null}>
                Generate Delete Script
              </ActionButton>
            </ActionBar>
          )}
        </>
      )}
      <SpinKeyframes />
    </div>
  )
}
