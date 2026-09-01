import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { api } from '../../api'
import { formatBytes } from '../../utils'
import { useToast } from '../Toast'
import {
  WorkflowHeader, EmptyState, LoadingRow, WorkflowError, WorkflowCrossLink,
  Checkbox, ActionBar, ActionButton, SpinKeyframes, useAuditComplete,
} from './shared'

function ageLabel(mtime) {
  if (!mtime) return null
  const days = Math.floor((Date.now() / 1000 - mtime) / 86400)
  if (days < 1) return 'today'
  if (days < 30) return `${days}d old`
  if (days < 365) return `${Math.floor(days / 30)}mo old`
  return `${Math.floor(days / 365)}y old`
}

function StatBox({ label, value, sub, color }) {
  return (
    <div style={{
      padding: '12px 16px', borderRadius: 'var(--rl)', flex: 1, minWidth: 140,
      background: 'var(--surface)', border: '1px solid var(--border)', boxShadow: 'var(--elev-1)',
    }}>
      <div style={{ fontFamily: 'var(--sans)', fontSize: 12, fontWeight: 600, letterSpacing: 0, textTransform: 'none', color: 'var(--text)', marginBottom: 5, display: 'flex', alignItems: 'center', gap: 7 }}>
        {label}
      </div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 20, fontWeight: 700, color: 'var(--text)', lineHeight: 1 }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

function FolderGroup({ group, selected, onToggleFile, onToggleGroup }) {
  const [open, setOpen] = useState(group.files.length <= 6)
  const keys = group.files.map(f => f.path)
  const allChecked  = keys.every(k => selected.has(k))
  const someChecked = !allChecked && keys.some(k => selected.has(k))
  const oldest = group.files.reduce((m, f) => (f.mtime && (!m || f.mtime < m) ? f.mtime : m), null)

  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', overflow: 'hidden' }}>
      {/* Group header */}
      <div
        onClick={() => setOpen(o => !o)}
        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', cursor: 'pointer', background: 'var(--surface2)' }}
      >
        <Checkbox checked={allChecked} indeterminate={someChecked} onChange={() => onToggleGroup(group)} />
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
          strokeLinecap="round" strokeLinejoin="round"
          style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', opacity: 0.45, flexShrink: 0, color: 'var(--text-dim)' }}>
          <polyline points="9 18 15 12 9 6" />
        </svg>
        <span title={group.folder} style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: 600, color: 'var(--text)', fontFamily: 'var(--mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {group.folder}
        </span>
        {oldest && (
          <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>{ageLabel(oldest)}</span>
        )}
        {group.hardlinked_size > 0 && (
          <span title="Part of this folder is hardlinked elsewhere — deleting those files frees nothing"
            style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--blue)', flexShrink: 0 }}>
            ⧉ {formatBytes(group.hardlinked_size)} linked
          </span>
        )}
        <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
          {group.files.length} file{group.files.length !== 1 ? 's' : ''}
        </span>
        <span style={{ fontSize: 12, fontFamily: 'var(--mono)', fontWeight: 600, color: 'var(--text)', flexShrink: 0, minWidth: 64, textAlign: 'right' }}>
          {formatBytes(group.total_size)}
        </span>
      </div>

      {/* Files */}
      {open && group.files.map(f => {
        const checked = selected.has(f.path)
        const name = f.path.replace(/\\/g, '/')
        const display = group.folder !== '(root)' && name.startsWith(group.folder + '/')
          ? name.slice(group.folder.length + 1)
          : name
        return (
          <div
            key={f.path}
            onClick={() => onToggleFile(f.path)}
            style={{
              display: 'flex', alignItems: 'center', gap: 10, padding: '7px 14px 7px 36px',
              borderTop: '1px solid var(--border)', cursor: 'pointer',
              background: checked ? 'var(--accent)06' : 'transparent',
            }}
          >
            <Checkbox checked={checked} onChange={() => onToggleFile(f.path)} />
            <span title={f.path} style={{ flex: 1, minWidth: 0, fontSize: 11.5, fontFamily: 'var(--mono)', color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {display}
            </span>
            {f.hardlinked && (
              <span title="Hardlinked elsewhere — deleting frees no space" style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--blue)', flexShrink: 0 }}>⧉ linked</span>
            )}
            {f.mtime && (
              <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>{ageLabel(f.mtime)}</span>
            )}
            <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0, minWidth: 60, textAlign: 'right' }}>
              {formatBytes(f.size)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

// Paths excluded since the last audit, by rel path. Module-level so it outlives
// the component — see the same set in Triage.jsx for why.
const DISMISSED = new Set()

export default function Cleanup({ onNavigate, onScript, triageCount }) {
  const toast = useToast()
  const [report,   setReport]   = useState(null)
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState(null)
  const [selected, setSelected] = useState(() => new Set())   // file rel paths
  const [busy,     setBusy]     = useState(null)

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    setSelected(new Set())
    api.cleanupReport()
      .then(r => setReport({
        ...r,
        // Excluded since the last audit — the server's answer is that audit, so
        // without this they reappear the moment you navigate back.
        groups: (r?.groups || [])
          .map(g => ({ ...g, files: (g.files || []).filter(f => !DISMISSED.has(f.path)) }))
          .filter(g => g.files.length > 0),
      }))
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])
  // The report is built from the last audit, so running a delete script changes
  // nothing here until a scan has seen it. This is what clears the rows.
  useAuditComplete(useCallback(() => { DISMISSED.clear(); load() }, [load]))

  const groups = report?.groups || []
  const fileByPath = useMemo(() => {
    const m = {}
    for (const g of groups) for (const f of g.files) m[f.path] = f
    return m
  }, [groups])

  const selectedFiles = useMemo(() => [...selected].map(p => fileByPath[p]).filter(Boolean), [selected, fileByPath])
  const selectedSize     = selectedFiles.reduce((s, f) => s + f.size, 0)
  const selectedFreeable = selectedFiles.reduce((s, f) => s + (f.hardlinked ? 0 : f.size), 0)

  const toggleFile = useCallback(path => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(path) ? next.delete(path) : next.add(path)
      return next
    })
  }, [])

  const toggleGroup = useCallback(group => {
    setSelected(prev => {
      const keys = group.files.map(f => f.path)
      const allIn = keys.every(k => prev.has(k))
      const next = new Set(prev)
      keys.forEach(k => allIn ? next.delete(k) : next.add(k))
      return next
    })
  }, [])

  const selectAll = useCallback(() => {
    setSelected(prev => {
      const all = groups.flatMap(g => g.files.map(f => f.path))
      return prev.size === all.length ? new Set() : new Set(all)
    })
  }, [groups])

  const handleDeleteScript = () => {
    onScript({
      scriptType: 'orphaned_torrents_delete',
      title: 'Orphaned Torrent Delete Script',
      subtitle: `${selectedFiles.length} files · ${formatBytes(selectedFreeable)} freed now`,
      body: { paths: [...selected] },
    })
  }

  const handleExclude = async () => {
    setBusy('exclude')
    // Fully-selected release folders become one subtree rule; loose files
    // get exact-path rules.
    const patterns = []
    const covered = new Set()
    for (const g of groups) {
      if (g.folder !== '(root)' && g.files.length > 0 && g.files.every(f => selected.has(f.path))) {
        patterns.push(g.folder + '/')
        g.files.forEach(f => covered.add(f.path))
      }
    }
    for (const p of selected) {
      if (!covered.has(p)) patterns.push(p.replace(/\\/g, '/'))
    }
    try {
      const resp = await api.excludePatterns(patterns)
      toast(`Added ${resp.added} exclusion rule${resp.added !== 1 ? 's' : ''} — applies from the next audit`, 'success')
      selected.forEach(p => DISMISSED.add(p))
      setReport(r => ({
        ...r,
        groups: (r?.groups || [])
          .map(g => ({ ...g, files: g.files.filter(f => !selected.has(f.path)) }))
          .filter(g => g.files.length > 0),
      }))
      setSelected(new Set())
    } catch (e) {
      toast(e.message, 'error')
    }
    setBusy(null)
  }

  const totalFiles = report?.file_count ?? 0

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 22 }}>
      <WorkflowHeader
        title="Cleanup"
        accent="var(--yellow)"
        blurb="Files in your torrent folder that qBittorrent has no knowledge of. Review them by release folder, then generate a delete script for your selection — or exclude the ones you put there on purpose."
        /* Re-reads itself when an audit lands — see `useAuditComplete`. */
      />

      <WorkflowError message={error} />

      {!loading && (
        <WorkflowCrossLink
          text="Everything here has no torrent attached. Problem torrents the client still knows about:"
          linkLabel="Triage"
          count={triageCount}
          onClick={() => onNavigate && onNavigate({ tab: 'triage' })}
        />
      )}

      {loading && <LoadingRow label="Loading orphaned files…" />}

      {!loading && !error && totalFiles === 0 && (
        <EmptyState
          title="No orphaned torrents"
          sub="Every file in your torrent folder is known to qBittorrent. Nothing to clean up."
        />
      )}

      {!loading && totalFiles > 0 && (
        <>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <StatBox label="Orphaned Files" value={totalFiles} sub={`${groups.length} folder${groups.length !== 1 ? 's' : ''}`} />
            <StatBox label="Total Size" value={formatBytes(report.total_size)} color="var(--yellow)" />
            <StatBox
              label="Freed If Deleted" value={formatBytes(report.freeable_size)} color="var(--green)"
              sub={report.total_size > report.freeable_size ? `${formatBytes(report.total_size - report.freeable_size)} hardlinked — frees nothing` : 'no hardlinks'}
            />
            {report.excluded_count > 0 && (
              <StatBox label="Excluded" value={report.excluded_count} sub="hidden by your exclusion rules" />
            )}
          </div>

          <div>
            <button onClick={selectAll} style={{
              fontSize: 12, padding: '5px 14px', borderRadius: 'var(--r-pill)', cursor: 'pointer',
              border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)',
            }}>
              {selected.size === totalFiles ? 'Deselect all' : 'Select all'}
            </button>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {groups.map(g => (
              <FolderGroup
                key={g.folder}
                group={g}
                selected={selected}
                onToggleFile={toggleFile}
                onToggleGroup={toggleGroup}
              />
            ))}
          </div>

          {selected.size > 0 && (
            <ActionBar summary={`${selected.size} file${selected.size !== 1 ? 's' : ''} selected · ${formatBytes(selectedSize)} (${formatBytes(selectedFreeable)} freed now)`}>
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
