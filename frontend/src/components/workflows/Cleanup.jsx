import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { api } from '../../api'
import { formatBytes } from '../../utils'
import { useToast } from '../Toast'
import {
  WorkflowPage, WorkflowHeader, EmptyState, LoadingRow, WorkflowError, WorkflowWarning, WorkflowCrossLink,
  Checkbox, ActionBar, Button, SpinKeyframes, useAuditComplete,
  ConfirmExcludeModal, SectionHeading, StatBox, Dot, MONO_TITLE, tint,
} from './shared'

// Exclusion rules are built from real paths, so they are written as `literal:`
// — release names contain `[`, `]`, `*` and `?`, and every other path rule in
// exclusions.py runs through fnmatch. A bracketed path became a character class
// that matched nothing; one containing `*` matched itself *and its neighbours*.
// Both were silent (C8).
const literal = (path, subtree = false) =>
  `literal:${String(path).replace(/\\/g, '/').replace(/^\/+|\/+$/g, '')}${subtree ? '/' : ''}`

// ── What survives a delete ────────────────────────────────────────────────────
//
// Cleanup is the only workflow whose output destroys data with no second copy
// anywhere, so the page is organised by whether anything survives, not by how
// much it frees (CLEANUP §1, Principle 5 — option (a), decided 2026-09-13). The
// state is computed server-side (`app._cleanup_state`) and never re-derived here.
//
// Ration colour: hue as a dot and its text, never a fill, and green only on the
// pile that loses nothing — never on a byte total. Trumped's link check shows the
// same two facts in the same hues (red "only copy", yellow for what could not be
// checked), so the two pages speak one vocabulary.
const STATE = {
  library_copy: {
    label: 'library copy', color: 'var(--green)',
    title: 'A hardlink to this file sits in your media library. Deleting the torrent-folder path loses nothing and frees nothing.',
  },
  linked_elsewhere: {
    label: 'linked elsewhere', color: 'var(--blue)',
    title: 'Another hardlink to this file exists beyond the paths listed here — a manual hardlink, a snapshot, another library root, or a copy you excluded. Deleting these paths loses nothing and frees nothing.',
  },
  last_copy: {
    label: 'only copy', color: 'var(--red)',
    title: 'Nothing else auditorr can see holds these bytes. Deleting is permanent.',
  },
  unverified: {
    label: 'could not check', color: 'var(--yellow)',
    title: 'auditorr could not fully ask your torrent client on the last scan, so this may belong to a live torrent. It cannot be selected until a scan reads every torrent’s file list.',
  },
}

const PILES = [
  {
    id: 'keeps_copy', title: 'Your library keeps a copy', color: 'var(--green)',
    blurb: 'Deleting these loses nothing and frees nothing — another link to the same data stays on disk. Clearing this pile is what tidies a torrent folder.',
  },
  {
    id: 'only_copy', title: 'This is the only copy', color: 'var(--red)',
    blurb: 'Deleting these is permanent. Oldest first: a file that has sat here for years is likelier junk than one from this week. A folder that also holds library copies sits here, with each file marked.',
  },
  {
    id: 'unverified', title: 'Could not check', color: 'var(--yellow)',
    blurb: 'auditorr could not fully ask your torrent client on the last scan, so these may belong to a live torrent. They become selectable after a scan that reads every torrent’s file list.',
  },
]

// Why a group offers no folder rule. The audit decides (`excl_folder`, or the
// reason it refused one); the page only says so.
const NO_FOLDER_RULE = {
  root: {
    chip: 'top level',
    title: 'These files sit at the top of your torrent folder, so there is no folder to exclude — excluding writes one rule per file.',
  },
  media_root: {
    chip: 'category folder',
    title: 'This folder shares its name with a folder at the top of your media library, so a folder rule would hide library files too. Excluding writes one rule per file.',
  },
  live_torrent: {
    chip: 'shared with a torrent',
    title: 'This folder also holds files a torrent in your client still uses, so a folder rule would hide them. Excluding writes one rule per file.',
  },
  unverified: {
    chip: 'unchecked files',
    title: 'This folder holds files auditorr could not check on the last scan. Excluding writes one rule per file.',
  },
  not_established: {
    chip: 'not yet checked',
    title: 'Whether a folder rule is safe here has not been checked — the last scan predates it. Excluding writes one rule per file until the next scan.',
  },
}

// "Age unavailable" is a readout, never a missing chip: absent used to render
// identically whether the stat failed, a cap tripped, or the file was new (C9).
function ageLabel(mtime) {
  if (mtime == null) return 'age unavailable'
  const days = Math.floor((Date.now() / 1000 - mtime) / 86400)
  if (days < 1) return 'today'
  if (days < 30) return `${days}d old`
  if (days < 365) return `${Math.floor(days / 30)}mo old`
  return `${Math.floor(days / 365)}y old`
}

const selectable = row => row.state !== 'unverified'
const plural = (n, word) => `${n} ${word}${n !== 1 ? 's' : ''}`

function StateMark({ state }) {
  const s = STATE[state] || STATE.last_copy
  return (
    <span title={s.title} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: s.color, flexShrink: 0, minWidth: 124, lineHeight: '16px' }}>
      <Dot color={s.color} />{s.label}
    </span>
  )
}

// A row is an inode (C5). A cross-seeded orphan lists every torrent-folder path
// it has, together — its bytes go only when all of them go.
function FileRow({ row, folder, checked, onToggle }) {
  const canSelect = selectable(row)
  const shown = p => (folder !== '(root)' && p.startsWith(folder + '/') ? p.slice(folder.length + 1) : p)
  return (
    <div
      onClick={canSelect ? () => onToggle(row.path) : undefined}
      style={{
        display: 'flex', alignItems: 'flex-start', gap: 10, padding: '7px 14px 7px 36px',
        borderTop: '1px solid var(--border)', cursor: canSelect ? 'pointer' : 'default',
        background: checked ? tint('var(--accent)', 2) : 'transparent',
      }}
    >
      {canSelect
        ? <Checkbox checked={checked} onChange={() => onToggle(row.path)} />
        : <span title={STATE.unverified.title} style={{ width: 15, height: 15, borderRadius: 'var(--r-sm)', border: '1.5px dashed var(--border2)', flexShrink: 0, cursor: 'not-allowed' }} />}
      <StateMark state={row.state} />
      <div style={{ flex: 1, minWidth: 0 }}>
        {row.paths.map(p => (
          <div key={p} title={p} style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', lineHeight: '16px' }}>
            {shown(p)}
          </div>
        ))}
        {row.paths.length > 1 && (
          <div style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginTop: 2 }}>
            One file at {row.paths.length} paths — its space is freed only when every one is deleted.
          </div>
        )}
      </div>
      <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0, lineHeight: '16px' }}>{ageLabel(row.mtime)}</span>
      <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0, minWidth: 60, textAlign: 'right', lineHeight: '16px' }}>
        {formatBytes(row.size)}
      </span>
    </div>
  )
}

function FolderGroup({ group, selected, onToggleFile, onToggleKeys }) {
  const [open, setOpen] = useState(group.files.length <= 6)
  const keys = group.files.filter(selectable).map(f => f.path)
  const allChecked  = keys.length > 0 && keys.every(k => selected.has(k))
  const someChecked = !allChecked && keys.some(k => selected.has(k))
  const reason = !group.excl_folder && NO_FOLDER_RULE[group.no_folder_rule || 'not_established']
  // A group holding more than one state says so on its header, so a folder in
  // the only-copy pile that is mostly library copies reads as exactly that.
  const tallies = Object.keys(STATE)
    .map(s => [s, group.files.filter(f => f.state === s).length])
    .filter(([, n]) => n > 0)

  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', overflow: 'hidden' }}>
      <div
        onClick={() => setOpen(o => !o)}
        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', cursor: 'pointer', background: 'var(--surface2)', flexWrap: 'wrap' }}
      >
        {keys.length > 0
          ? <Checkbox checked={allChecked} indeterminate={someChecked} onChange={() => onToggleKeys(keys)} />
          : <span style={{ width: 15, height: 15, flexShrink: 0 }} />}
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
          strokeLinecap="round" strokeLinejoin="round"
          style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', opacity: 0.45, flexShrink: 0, color: 'var(--text-dim)' }}>
          <polyline points="9 18 15 12 9 6" />
        </svg>
        <span title={group.folder} style={{ ...MONO_TITLE, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {group.folder}
        </span>
        {/* Why no folder rule is offered here. Saying so is what stops the
            header checkbox reading as "this whole folder". */}
        {reason && (
          <span title={reason.title} style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
            {reason.chip}
          </span>
        )}
        <span style={{ flex: 1 }} />
        {tallies.length > 1 && tallies.map(([s, n]) => (
          <span key={s} title={STATE[s].title} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: STATE[s].color, flexShrink: 0 }}>
            <Dot color={STATE[s].color} />{n} {STATE[s].label}
          </span>
        ))}
        <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
          {group.oldest_mtime != null ? `oldest ${ageLabel(group.oldest_mtime)}` : 'age unavailable'}
        </span>
        <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
          {plural(group.files.length, 'file')}
        </span>
        <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0, minWidth: 64, textAlign: 'right' }}>
          {formatBytes(group.total_size)}
        </span>
      </div>

      {open && group.files.map(row => (
        <FileRow key={row.path} row={row} folder={group.folder}
          checked={selected.has(row.path)} onToggle={onToggleFile} />
      ))}
    </div>
  )
}

function Pile({ pile, groups, selected, onToggleFile, onToggleKeys }) {
  const rows = groups.flatMap(g => g.files)
  const keys = rows.filter(selectable).map(f => f.path)
  const allChecked  = keys.length > 0 && keys.every(k => selected.has(k))
  const someChecked = !allChecked && keys.some(k => selected.has(k))
  const size = rows.reduce((s, f) => s + f.size, 0)
  return (
    <section style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {/* An unselectable pile keeps the checkbox's slot, so its dot sits in
          the same column as the others. */}
      <SectionHeading
        check={keys.length > 0 ? { checked: allChecked, indeterminate: someChecked, onChange: () => onToggleKeys(keys) } : null}
        dot={pile.color} title={pile.title}
        meta={`${plural(rows.length, 'file')} · ${formatBytes(size)}`}
        desc={pile.blurb}
      />
      {groups.map(g => (
        <FolderGroup key={g.folder} group={g} selected={selected}
          onToggleFile={onToggleFile} onToggleKeys={onToggleKeys} />
      ))}
    </section>
  )
}

// Rows excluded since the last audit, by primary path. Module-level so it
// outlives the component — see the same set in Triage.jsx for why.
const DISMISSED = new Set()

export default function Cleanup({ onNavigate, onScript, triageCount }) {
  const toast = useToast()
  const [report,   setReport]   = useState(null)
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState(null)
  // Row keys — a row's first path. No default selection, ever (§6): C2's failure
  // mode was one click from catastrophic, and the page is never pre-armed.
  const [selected, setSelected] = useState(() => new Set())
  const [busy,     setBusy]     = useState(null)
  const [confirmExclude, setConfirmExclude] = useState(false)

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
  const rowByKey = useMemo(() => {
    const m = {}
    for (const g of groups) for (const f of g.files) m[f.path] = f
    return m
  }, [groups])

  const selectedRows  = useMemo(() => [...selected].map(k => rowByKey[k]).filter(Boolean), [selected, rowByKey])
  const selectedPaths = useMemo(() => selectedRows.flatMap(r => r.paths), [selectedRows])
  const selKeeps = selectedRows.filter(r => r.state === 'library_copy' || r.state === 'linked_elsewhere').length
  const selOnly  = selectedRows.filter(r => r.state === 'last_copy')
  // A row selects every path of its inode, so an only-copy row's bytes are freed
  // at most once. The script reports what it actually frees.
  const selFreeable = selOnly.reduce((s, r) => s + r.size, 0)

  const toggleFile = useCallback(key => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }, [])

  // One toggle for a group header and a pile header: both compare against the
  // same selectable keys they then add or remove, so the label and the behaviour
  // can no longer use different denominators (the old "Select all" did).
  const toggleKeys = useCallback(keys => {
    setSelected(prev => {
      const allIn = keys.every(k => prev.has(k))
      const next = new Set(prev)
      keys.forEach(k => allIn ? next.delete(k) : next.add(k))
      return next
    })
  }, [])

  const handleDeleteScript = () => {
    onScript({
      scriptType: 'orphaned_torrents_delete',
      title: 'Orphaned Torrent Delete Script',
      // Replaced by the server's own count once the script arrives: the script
      // is built after a fresh check of the client, which can drop files.
      subtitle: `${plural(selectedPaths.length, 'file')} · up to ${formatBytes(selFreeable)} freed`,
      body: { paths: selectedPaths },
    })
  }

  // Fully-selected folders the audit established are safe become one literal
  // subtree rule; everything else gets an exact literal file rule — one per path,
  // so a cross-seeded row writes a rule for each of its paths.
  //
  // `excl_folder` is the audit's answer, not a check repeated here: a category
  // dir shared with the media library (C7), a folder that also holds a live
  // torrent's files (C16) and the root never get one. "Fully selected" means
  // every orphan path under the folder, whichever row it belongs to, so a folder
  // rule never hides a file nobody picked.
  const excludePatterns = useMemo(() => {
    const chosen = new Set(selectedPaths)
    const allPaths = groups.flatMap(g => g.files.flatMap(f => f.paths))
    const patterns = []
    const covered = new Set()
    for (const g of groups) {
      if (!g.excl_folder) continue
      const under = allPaths.filter(p => p.startsWith(g.excl_folder + '/'))
      if (under.length > 0 && under.every(p => chosen.has(p))) {
        patterns.push(literal(g.excl_folder, true))
        under.forEach(p => covered.add(p))
      }
    }
    for (const p of chosen) {
      if (!covered.has(p)) patterns.push(literal(p))
    }
    return patterns
  }, [groups, selectedPaths])

  const handleExclude = async () => {
    setBusy('exclude')
    try {
      const resp = await api.excludePatterns(excludePatterns)
      toast(resp.message || `Added ${resp.added} exclusion rules`,
            resp.refused ? 'warning' : 'success')
      selected.forEach(k => DISMISSED.add(k))
      setReport(r => ({
        ...r,
        groups: (r?.groups || [])
          .map(g => ({ ...g, files: g.files.filter(f => !selected.has(f.path)) }))
          .filter(g => g.files.length > 0),
      }))
      setSelected(new Set())
      setConfirmExclude(false)
    } catch (e) {
      toast(e.message, 'error')
    }
    setBusy(null)
  }

  const totalFiles = report?.file_count ?? 0
  const keeps = report?.keeps_copy || { count: 0, size: 0 }
  const only  = report?.only_copy  || { count: 0, size: 0 }
  const unchecked = report?.unverified || { count: 0, size: 0 }

  return (
    <WorkflowPage>
      <WorkflowHeader
        title="Cleanup"
        accent="var(--yellow)"
        blurb="Files in your torrent folder that no torrent in your client claims, split by whether anything else still holds the data and listed oldest first. Generate a delete script for your selection — your client is checked again before it is built — or exclude the ones you put there on purpose."
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
          sub="Every file in your torrent folder belongs to a torrent your client knows about. Nothing to clean up."
        />
      )}

      {!loading && totalFiles > 0 && (
        <>
          {/* The split leads; the total and the most a delete could free follow.
              Green sits on the pile that loses nothing, never on a byte total. */}
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <StatBox dot="var(--green)" label="Keeps a copy" value={keeps.count}
              sub={`${formatBytes(keeps.size)} · deleting loses nothing`} />
            <StatBox dot="var(--red)" label="Only copy" value={only.count}
              sub={`${formatBytes(only.size)} · deleting is permanent`} />
            <StatBox label="Total" value={formatBytes(report.total_size)}
              sub={`${plural(totalFiles, 'file')}${report.path_count > totalFiles ? ` at ${report.path_count} paths` : ''} · ${plural(groups.length, 'folder')}`} />
            <StatBox label="Freed at most" value={formatBytes(report.freeable_size)}
              sub="an upper bound — the script reports what it frees" />
            {unchecked.count > 0 && (
              <StatBox dot="var(--yellow)" label="Could not check" value={unchecked.count}
                sub={`${formatBytes(unchecked.size)} · not selectable`} />
            )}
            {report.excluded_count > 0 && (
              <StatBox label="Excluded" value={report.excluded_count} sub="hidden by your exclusion rules" />
            )}
          </div>

          {unchecked.count > 0 && (
            <WorkflowWarning>
              {plural(unchecked.count, 'file')} could not be checked on the last scan — your torrent
              client did not fully answer, so they may belong to a live torrent. They are listed under
              Could not check and cannot be selected until a scan reads every torrent’s file list.
            </WorkflowWarning>
          )}

          {PILES.map(pile => {
            const inPile = groups.filter(g => g.pile === pile.id)
            return inPile.length > 0 && (
              <Pile key={pile.id} pile={pile} groups={inPile} selected={selected}
                onToggleFile={toggleFile} onToggleKeys={toggleKeys} />
            )
          })}

          {selected.size > 0 && (
            <ActionBar summary={`${plural(selectedRows.length, 'file')} selected · ${selKeeps} keep a copy · ${selOnly.length} only copy · up to ${formatBytes(selFreeable)} freed`}>
              <Button onClick={() => setConfirmExclude(true)} disabled={busy != null} title="Add exclusion rules so auditorr stops flagging these">
                {busy === 'exclude' ? 'Excluding…' : 'Exclude'}
              </Button>
              <Button variant="danger" onClick={handleDeleteScript} disabled={busy != null}>
                Generate Delete Script
              </Button>
            </ActionBar>
          )}

          {confirmExclude && excludePatterns.length > 0 && (
            <ConfirmExcludeModal
              patterns={excludePatterns}
              subtitle={`Built from the ${plural(selectedRows.length, 'file')} you selected.`}
              busy={busy === 'exclude'}
              onCancel={() => setConfirmExclude(false)}
              onConfirm={handleExclude}
            />
          )}
        </>
      )}
      <SpinKeyframes />
    </WorkflowPage>
  )
}
