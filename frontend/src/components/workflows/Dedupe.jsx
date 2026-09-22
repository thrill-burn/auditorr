import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { api } from '../../api'
import { formatBytes } from '../../utils'
import {
  WorkflowPage, WorkflowHeader, EmptyState, LoadingRow, WorkflowError,
  Checkbox, ActionBar, Button, SpinKeyframes, useAuditComplete, StatBox, Dot, ITEM_TITLE, tint,
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
// fill; the cause is plain dim text. A group with nothing to say carries no
// status at all.
const STATUS = {
  // No label. On a pooled share (Unraid, mergerfs) every copy reports one disk
  // whichever drive it is on, so "same disk" would be a claim — and the script
  // checks each link itself when it runs. A caution here was on every group of
  // the commonest install, for normal operation (removed 2026-09-15).
  linkable: { label: null },
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
  stat_failed: () => 'auditorr could not read one of these files just now. The script checks every file itself before touching it.',
  outside_script_root: () => 'One copy is outside the folder the script runs from, so the script leaves that copy out and links the rest.',
}

// Decision 2 (a), 2026-09-15: the cause is stated from the group's own paths,
// and the page makes no claim about which kind dominates a library (QA-11 is
// unmeasured). The paths say where the copies sit, never who made them: a copy
// made by hand for an upload has the same shape as an import that copied.
const CAUSE = {
  missing_hardlink: {
    label: 'missing hardlink',
    title: 'A torrent file and a library file holding the same bytes, never hardlinked — an import that copied, or a copy made by hand. Linking them frees the space and makes the torrent read as imported.',
  },
  copies: {
    label: 'duplicate copies',
    title: 'The same bytes stored more than once. Linking them frees the space.',
  },
}

const plural = (n, word) => `${n} ${word}${n !== 1 ? 's' : ''}`
const selectable = g => g.selectable !== false && g.status !== 'stale'

function DupGroup({ group, checked, onToggle }) {
  const canSelect = selectable(group)
  const status = STATUS[group.status] || STATUS.unverifiable
  const note = status.note ? status.note(group) : null
  const cause = CAUSE[group.cause] || CAUSE.copies
  const name = (group.id || '').split('/').pop()

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)',
      overflow: 'hidden',
    }}>
      <div
        onClick={canSelect ? onToggle : undefined}
        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', flexWrap: 'wrap', cursor: canSelect ? 'pointer' : 'default', background: checked ? tint('var(--accent)', 2) : 'var(--surface2)' }}
      >
        {canSelect ? (
          <Checkbox checked={checked} onChange={onToggle} />
        ) : (
          <span title={note || ''} style={{ width: 15, height: 15, borderRadius: 'var(--r-sm)', border: '1.5px dashed var(--border2)', flexShrink: 0, cursor: 'not-allowed' }} />
        )}
        <span title={group.id} style={{ ...ITEM_TITLE, minWidth: 0, fontFamily: 'var(--mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {name}
        </span>
        <span title={cause.title} style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', flexShrink: 0 }}>
          {cause.label}
        </span>
        <span style={{ flex: 1 }} />
        {status.label && (
          <span title={note || ''} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: status.color, flexShrink: 0 }}>
            <Dot color={status.color} />{status.label}
          </span>
        )}
        <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
          {group.file_count} copies
        </span>
        <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0, minWidth: 96, textAlign: 'right' }}>
          frees up to {formatBytes(group.frees_up_to)}
        </span>
      </div>

      {/* Every group that is not plainly linkable says what to expect, inline. */}
      {note && (
        <div style={{ padding: '6px 14px 6px 36px', borderTop: '1px solid var(--border)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', lineHeight: 1.5 }}>
          {note}
        </div>
      )}

      {group.members.map(m => (
        <div key={m.key} style={{ display: 'flex', alignItems: 'flex-start', gap: 10, padding: '6px 14px 6px 36px', borderTop: '1px solid var(--border)' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            {m.paths.map(p => (
              <div key={p.path} style={{ display: 'flex', alignItems: 'center', gap: 8, lineHeight: '16px' }}>
                <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0, minWidth: 48 }}>
                  {p.tree === 'media' ? 'library' : 'torrent'}
                </span>
                <span title={p.path} style={{ flex: 1, minWidth: 0, fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {p.path}
                </span>
              </div>
            ))}
            {m.paths.length > 1 && (
              <div style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginTop: 2 }}>
                One file at {m.paths.length} paths — the script replaces all of them together, or none.
              </div>
            )}
          </div>
          <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0, lineHeight: '16px' }}>
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
    <WorkflowPage>
      <WorkflowHeader
        title="Dedupe"
        accent="var(--purple)"
        blurb="Identical files stored as separate copies. Where one sits in your torrent folder and the other in your library, linking them also makes the torrent read as imported. The script checks every file again before touching it — same disk, same bytes, no hardlinks it can't see — links only what passes, and chooses the copy to keep when it runs."
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

          {choosable.length > 0 && (
            <div>
              <Button size="sm" variant="ghost" onClick={toggleAll}>
                {allChosen ? 'Deselect all' : `Select all (${choosable.length})`}
              </Button>
            </div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {groups.map(g => (
              <DupGroup key={g.id} group={g} checked={selected.has(g.id)} onToggle={() => toggle(g.id)} />
            ))}
          </div>

          {selectedGroups.length > 0 && (
            <ActionBar summary={`${selectionLine} selected`}>
              <Button variant="primary" onClick={handleScript}>
                Generate Dedupe Script
              </Button>
            </ActionBar>
          )}
        </>
      )}
      <SpinKeyframes />
    </WorkflowPage>
  )
}
