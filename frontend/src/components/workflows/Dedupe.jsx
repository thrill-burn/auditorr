import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { api } from '../../api'
import { formatBytes } from '../../utils'
import {
  WorkflowHeader, EmptyState, LoadingRow, WorkflowError,
  Checkbox, ActionBar, ActionButton, SpinKeyframes,
} from './shared'

function StatBox({ label, value, sub, color }) {
  return (
    <div style={{
      padding: '12px 16px', borderRadius: 9, flex: 1, minWidth: 140,
      background: 'var(--surface)', border: '1px solid var(--border)',
    }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: 1.5, textTransform: 'uppercase', color: 'var(--text-dim)', marginBottom: 5 }}>{label}</div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 20, fontWeight: 700, color: color || 'var(--text)', lineHeight: 1 }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

function DupGroup({ group, checked, onToggle }) {
  const canonical  = group.files.find(f => f.canonical)
  const duplicates = group.files.filter(f => !f.canonical)
  const filename   = (canonical?.path || '').replace(/\\/g, '/').split('/').pop()

  return (
    <div
      onClick={() => !group.cross_fs && onToggle()}
      style={{
        background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 9,
        overflow: 'hidden', cursor: group.cross_fs ? 'default' : 'pointer',
        opacity: group.cross_fs ? 0.6 : 1,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', background: checked ? 'var(--accent)06' : 'var(--surface2)' }}>
        {group.cross_fs ? (
          <span title="Cannot hardlink across filesystems" style={{ width: 15, flexShrink: 0, textAlign: 'center', fontSize: 11, color: 'var(--text-dim)' }}>—</span>
        ) : (
          <Checkbox checked={checked} onChange={onToggle} />
        )}
        <span title={canonical?.path} style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: 600, color: 'var(--text)', fontFamily: 'var(--mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {filename}
        </span>
        {group.cross_fs ? (
          <span
            title="These copies live on different filesystems — hardlinks can't span mounts, so this group is skipped"
            style={{ fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 7px', borderRadius: 99, flexShrink: 0, border: '1px solid var(--border2)', color: 'var(--text-dim)' }}
          >
            cross-filesystem
          </span>
        ) : (
          <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--green)', flexShrink: 0 }}>
            +{formatBytes(group.recoverable_size)}
          </span>
        )}
        <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
          {group.files.length} copies
        </span>
      </div>

      {group.files.map((f, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 14px 6px 36px', borderTop: '1px solid var(--border)' }}>
          <span style={{
            fontSize: 9, fontFamily: 'var(--mono)', fontWeight: 700, padding: '1px 6px', borderRadius: 4, flexShrink: 0,
            background: f.canonical ? 'var(--green)18' : 'var(--surface3)',
            color: f.canonical ? 'var(--green)' : 'var(--text-dim)',
            border: `1px solid ${f.canonical ? 'var(--green)40' : 'var(--border2)'}`,
          }}>
            {f.canonical ? 'KEEP' : 'LINK'}
          </span>
          <span title={f.path} style={{ flex: 1, minWidth: 0, fontSize: 11.5, fontFamily: 'var(--mono)', color: f.canonical ? 'var(--text)' : 'var(--text-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {f.path}
          </span>
          <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
            {formatBytes(f.size)}
          </span>
        </div>
      ))}
    </div>
  )
}

export default function Dedupe({ onNavigate, onScript }) {
  const [report,   setReport]   = useState(null)
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState(null)
  const [selected, setSelected] = useState(() => new Set())   // group ids

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    api.dedupeReport()
      .then(data => {
        setReport(data)
        // All linkable groups selected by default — dedupe keeps every path alive
        setSelected(new Set((data.groups || []).filter(g => !g.cross_fs).map(g => g.id)))
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const groups = report?.groups || []
  const linkable = useMemo(() => groups.filter(g => !g.cross_fs), [groups])
  const selectedGroups = useMemo(() => linkable.filter(g => selected.has(g.id)), [linkable, selected])
  const selectedRecoverable = selectedGroups.reduce((s, g) => s + g.recoverable_size, 0)
  const crossFsCount = groups.length - linkable.length

  const toggle = useCallback(id => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }, [])

  const toggleAll = useCallback(() => {
    setSelected(prev => prev.size === linkable.length ? new Set() : new Set(linkable.map(g => g.id)))
  }, [linkable])

  const handleScript = () => {
    onScript({
      scriptType: 'dedupe',
      title: 'Dedupe Script',
      subtitle: `${selectedGroups.length} group${selectedGroups.length !== 1 ? 's' : ''} · ${formatBytes(selectedRecoverable)} recoverable`,
      body: { groups: [...selected] },
    })
  }

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 22 }}>
      <WorkflowHeader
        title="Dedupe"
        accent="var(--purple)"
        blurb="Bit-for-bit identical files that don't share an inode — true copies wasting disk space. The generated script verifies each pair with cmp, then replaces the copy with a hardlink: every path survives and every torrent keeps seeding."
        right={!loading && (
          <button onClick={load} style={{
            fontSize: 12, padding: '6px 16px', borderRadius: 7, cursor: 'pointer',
            border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)',
          }}>↻ Refresh</button>
        )}
      />

      <WorkflowError message={error} />

      {loading && <LoadingRow label="Loading duplicate groups…" />}

      {!loading && !error && groups.length === 0 && (
        <EmptyState
          title="No duplicates"
          sub="No identical files without a shared inode were found in your last audit. Your disk space is fully deduplicated."
        />
      )}

      {!loading && groups.length > 0 && (
        <>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <StatBox label="Duplicate Groups" value={groups.length} sub={crossFsCount > 0 ? `${crossFsCount} skipped (cross-filesystem)` : null} />
            <StatBox label="Recoverable" value={formatBytes(report.total_recoverable)} color="var(--green)" sub="if all groups are hardlinked" />
            {report.excluded_count > 0 && (
              <StatBox label="Excluded" value={report.excluded_count} sub="hidden by your exclusion rules" />
            )}
          </div>

          {linkable.length > 0 && (
            <div>
              <button onClick={toggleAll} style={{
                fontSize: 12, padding: '5px 14px', borderRadius: 99, cursor: 'pointer',
                border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)',
              }}>
                {selected.size === linkable.length ? 'Deselect all' : 'Select all'}
              </button>
            </div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {groups.map(g => (
              <DupGroup key={g.id} group={g} checked={selected.has(g.id)} onToggle={() => toggle(g.id)} />
            ))}
          </div>

          {selectedGroups.length > 0 && (
            <ActionBar summary={`${selectedGroups.length} group${selectedGroups.length !== 1 ? 's' : ''} selected · ${formatBytes(selectedRecoverable)} recoverable`}>
              <ActionButton primary onClick={handleScript}>
                Generate Dedupe Script
              </ActionButton>
            </ActionBar>
          )}
        </>
      )}
      <SpinKeyframes />
    </div>
  )
}
