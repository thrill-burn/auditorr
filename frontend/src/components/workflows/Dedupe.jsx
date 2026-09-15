import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { api } from '../../api'
import { formatBytes } from '../../utils'
import {
  WorkflowHeader, EmptyState, LoadingRow, WorkflowError,
  Checkbox, ActionBar, ActionButton, SpinKeyframes, useAuditComplete,
} from './shared'

// ── What each group is ────────────────────────────────────────────────────────
//
// A group is a set of identical files — one row per file (inode), every path of
// it listed — and no copy is marked as the one kept. The script decides that
// when it runs, per disk, by link count, and checks every file again before it
// touches any (DEDUPE §5.4). So there is no KEEP/LINK chip here any more, and
// nothing is selected for you: which copy survives, and whose owner and
// permissions every path ends up with, is a run-time decision you choose into.
//
// Status and cause are computed server-side (`scripts.build_dedupe_report`) and
// never re-derived here. Ration colour: status is a dot and its text, never a
// fill; the cause is plain dim text.
const STATUS = {
  linkable: {
    label: 'same disk', color: 'var(--green)',
    title: 'Every copy reports the same disk. The script still checks each one before linking it.',
  },
  cross_device: {
    label: 'different disks', color: 'var(--blue)',
    note: () => 'These copies report different disks. The script links the copies that share a disk and leaves the rest alone.',
  },
  unverifiable: {
    label: 'could not check', color: 'var(--yellow)',
    note: g => (UNVERIFIABLE[g.reason] || UNVERIFIABLE.stat_failed)(g),
  },
  stale: {
    label: 'changed since the scan', color: 'var(--text-dim)',
    note: () => 'A copy has gone since the last scan, so this group cannot be selected. It clears on the next scan.',
  },
}

const UNVERIFIABLE = {
  pooled_mount: g => `These files are on a pooled filesystem (${g.facts?.fstype || 'FUSE'}), which reports one disk for all of its drives, so auditorr cannot tell whether they share one. The script tries each link and leaves alone any that cross drives.`,
  stat_failed: () => 'auditorr could not read one of these files just now. The script checks every file itself before touching it.',
  outside_script_root: () => 'One copy is outside the folder the script runs from, so the script leaves that copy out and links the rest.',
}

// Decision 2 (a), 2026-09-15: the cause is stated from the group's own paths,
// and the page makes no claim about which kind dominates a library (QA-11 is
// unmeasured).
const CAUSE = {
  missing_hardlink: {
    label: 'missing hardlink',
    title: 'A torrent file and a library file holding the same bytes, never hardlinked — the arr copied instead. Linking them repairs the import as well as freeing the space.',
  },
  copies: {
    label: 'duplicate copies',
    title: 'The same bytes stored more than once. Linking them frees the space.',
  },
}

const plural = (n, word) => `${n} ${word}${n !== 1 ? 's' : ''}`
const selectable = g => g.selectable !== false && g.status !== 'stale'

function Dot({ color }) {
  return <span style={{ width: 7, height: 7, borderRadius: '50%', background: color, flexShrink: 0, display: 'inline-block' }} />
}

function StatBox({ label, value, sub }) {
  return (
    <div style={{
      padding: '12px 16px', borderRadius: 9, flex: 1, minWidth: 140,
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

function DupGroup({ group, checked, onToggle }) {
  const canSelect = selectable(group)
  const status = STATUS[group.status] || STATUS.unverifiable
  const note = status.note ? status.note(group) : null
  const cause = CAUSE[group.cause] || CAUSE.copies
  const name = (group.id || '').split('/').pop()

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 9, boxShadow: 'var(--elev-1)',
      overflow: 'hidden',
    }}>
      <div
        onClick={canSelect ? onToggle : undefined}
        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', flexWrap: 'wrap', cursor: canSelect ? 'pointer' : 'default', background: checked ? 'var(--accent)06' : 'var(--surface2)' }}
      >
        {canSelect ? (
          <Checkbox checked={checked} onChange={onToggle} />
        ) : (
          <span title={note || ''} style={{ width: 15, height: 15, borderRadius: 'var(--r-sm)', border: '1.5px dashed var(--border2)', flexShrink: 0, cursor: 'not-allowed' }} />
        )}
        <span title={group.id} style={{ minWidth: 0, fontSize: 13, fontWeight: 600, color: 'var(--text)', fontFamily: 'var(--mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {name}
        </span>
        <span title={cause.title} style={{ fontSize: 11, color: 'var(--text-dim)', flexShrink: 0 }}>
          {cause.label}
        </span>
        <span style={{ flex: 1 }} />
        <span title={status.title || note || ''} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11, fontFamily: 'var(--mono)', color: status.color, flexShrink: 0 }}>
          <Dot color={status.color} />{status.label}
        </span>
        <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
          {group.file_count} copies
        </span>
        <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0, minWidth: 96, textAlign: 'right' }}>
          frees up to {formatBytes(group.frees_up_to)}
        </span>
      </div>

      {/* Every group that is not plainly linkable says what to expect, inline. */}
      {note && (
        <div style={{ padding: '6px 14px 6px 36px', borderTop: '1px solid var(--border)', fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.5 }}>
          {note}
        </div>
      )}

      {group.members.map(m => (
        <div key={m.key} style={{ display: 'flex', alignItems: 'flex-start', gap: 10, padding: '6px 14px 6px 36px', borderTop: '1px solid var(--border)' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            {m.paths.map(p => (
              <div key={p.path} style={{ display: 'flex', alignItems: 'center', gap: 8, lineHeight: '16px' }}>
                <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0, minWidth: 48 }}>
                  {p.tree === 'media' ? 'library' : 'torrent'}
                </span>
                <span title={p.path} style={{ flex: 1, minWidth: 0, fontSize: 11.5, fontFamily: 'var(--mono)', color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {p.path}
                </span>
              </div>
            ))}
            {m.paths.length > 1 && (
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 2 }}>
                One file at {m.paths.length} paths — the script replaces all of them together, or none.
              </div>
            )}
          </div>
          <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0, lineHeight: '16px' }}>
            {formatBytes(m.size)}
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
  // Group ids. Never pre-selected (decision 2, 2026-09-15 — Cleanup's rule).
  const [selected, setSelected] = useState(() => new Set())

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    setSelected(new Set())
    api.dedupeReport()
      .then(setReport)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])
  // Built from the last audit, so running a dedupe script changes nothing here
  // until a scan has seen it. This is what clears the groups.
  useAuditComplete(load)

  const groups = report?.groups || []
  const choosable = useMemo(() => groups.filter(selectable), [groups])
  const staleCount = groups.length - choosable.length
  const selectedGroups = useMemo(() => choosable.filter(g => selected.has(g.id)), [choosable, selected])
  const selFiles = selectedGroups.reduce((s, g) => s + g.file_count, 0)
  const selFrees = selectedGroups.reduce((s, g) => s + g.frees_up_to, 0)

  const toggle = useCallback(id => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }, [])

  const allChosen = choosable.length > 0 && choosable.every(g => selected.has(g.id))
  const toggleAll = useCallback(() => {
    setSelected(allChosen ? new Set() : new Set(choosable.map(g => g.id)))
  }, [allChosen, choosable])

  const selectionLine = `${plural(selectedGroups.length, 'group')} · ${plural(selFiles, 'file')} · frees up to ${formatBytes(selFrees)}`

  const handleScript = () => {
    onScript({
      scriptType: 'dedupe',
      title: 'Dedupe Script',
      subtitle: selectionLine,
      body: { groups: selectedGroups.map(g => g.id) },
    })
  }

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 22 }}>
      <WorkflowHeader
        title="Dedupe"
        accent="var(--purple)"
        blurb="Identical files stored as separate copies. Where one sits in your torrent folder and the other in your library, the arr copied instead of hardlinking, and linking them repairs the import as well as freeing the space. The script checks every file again before touching it — same disk, same bytes, no hardlinks it can't see — links only what passes, and chooses the copy to keep when it runs."
        /* Re-reads itself when an audit lands — see `useAuditComplete`. */
      />

      <WorkflowError message={error} />

      {loading && <LoadingRow label="Loading duplicate groups…" />}

      {!loading && !error && groups.length === 0 && (
        <EmptyState
          title="No duplicates"
          sub="No identical files without a shared inode were found in your last audit."
        />
      )}

      {!loading && groups.length > 0 && (
        <>
          {/* Decision 2 (a): both numbers lead, and the bytes are always a maximum. */}
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <StatBox label="Would share a copy" value={plural(report.file_count, 'file')}
              sub={`in ${plural(choosable.length, 'group')}${staleCount ? ` · ${staleCount} changed since the scan` : ''}`} />
            <StatBox label="Frees up to" value={formatBytes(report.frees_up_to)}
              sub="a maximum — the script reports what it frees" />
            <StatBox label="Missing hardlinks" value={report.missing_hardlink_count}
              sub="groups pairing a torrent file with a library copy" />
            {report.excluded_count > 0 && (
              <StatBox label="Excluded" value={report.excluded_count} sub="hidden by your exclusion rules" />
            )}
          </div>

          {report.mount && report.mount.checked === false && (
            <p style={{ fontSize: 12, color: 'var(--text-dim)', margin: 0, lineHeight: 1.5 }}>
              auditorr could not read this system’s mount table, so a pooled filesystem (an Unraid share, mergerfs) would not be recognised here. The script checks every link itself either way.
            </p>
          )}

          {choosable.length > 0 && (
            <div>
              <button onClick={toggleAll} style={{
                fontSize: 12, padding: '5px 14px', borderRadius: 99, cursor: 'pointer',
                border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)',
              }}>
                {allChosen ? 'Deselect all' : `Select all (${choosable.length})`}
              </button>
            </div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {groups.map(g => (
              <DupGroup key={g.id} group={g} checked={selected.has(g.id)} onToggle={() => toggle(g.id)} />
            ))}
          </div>

          {selectedGroups.length > 0 && (
            <ActionBar summary={`${selectionLine} selected`}>
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
