import React, { useState, useEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../../api'
import { formatBytes } from '../../utils'
import { useToast } from '../Toast'
import { WATCH_ACTIVE, watchColor } from '../ImportProgress'
import {
  WorkflowHeader, WorkflowError, WorkflowWarning, ArrErrorsWarning, WorkflowCrossLink,
  Checkbox, Spinner, SpinKeyframes, ActionButton, HDR_STYLE,
} from './shared'

const ACCENT = 'var(--green)'

// Compact seeding duration — days are the unit that matters for hit-and-run
function formatDuration(secs) {
  if (secs == null) return null
  const d = secs / 86400
  if (d >= 1) return `${d >= 10 ? Math.round(d) : d.toFixed(1)}d`
  const h = secs / 3600
  if (h >= 1) return `${Math.round(h)}h`
  return `${Math.max(1, Math.round(secs / 60))}m`
}

const SAMPLE_PM = `The following torrent(s) have been trumped

    The General 1926 2160p UHD BluRay TrueHD 7.1 Atmos HDR x265-HQMUX

and will be replaced by
The General 1926 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-RandomBytes.

Reason: DV/HDR replacing HDR`

// One confirmation is one operation (S09): the server records this id before it
// acts, refuses a second request carrying it while the first runs, and replays
// the first one's answer afterwards rather than grabbing again. `randomUUID` is
// missing outside a secure context, which a LAN install over http is.
const newOperationId = () => (
  (typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`)
).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64)

// ── Step scaffold ─────────────────────────────────────────────────────────────
function StepShell({ n, active, done, title, children }) {
  const color = done ? ACCENT : active ? 'var(--text)' : 'var(--text-dim)'
  return (
    <div style={{ display: 'flex', gap: 14, opacity: active || done ? 1 : 0.5 }}>
      <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
        <span style={{
          width: 26, height: 26, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 12, fontWeight: 700, fontFamily: 'var(--mono)',
          border: `1.5px solid ${done || active ? ACCENT : 'var(--border2)'}`,
          background: done ? ACCENT : 'transparent',
          color: done ? '#fff' : active ? ACCENT : 'var(--text-dim)',
        }}>
          {done ? '✓' : n}
        </span>
        <span style={{ flex: 1, width: 1.5, background: 'var(--border2)' }} />
      </div>
      <div style={{ flex: 1, minWidth: 0, paddingBottom: 22 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color, marginBottom: 10 }}>{title}</div>
        {(active || done) && children}
      </div>
    </div>
  )
}

function Field({ label, value, onChange, mono }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, flex: 1, minWidth: 0 }}>
      <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>{label}</span>
      <input
        value={value} onChange={e => onChange(e.target.value)}
        style={{
          padding: '7px 10px', borderRadius: 'var(--r)', border: '1px solid var(--border2)',
          background: 'var(--surface2)', color: 'var(--text)',
          fontFamily: mono ? 'var(--mono)' : 'inherit', fontSize: 12,
        }}
      />
    </label>
  )
}

function QualityChip({ label, hdr }) {
  const hdrInfo = HDR_STYLE[hdr]
  if (!label && !hdrInfo) return null
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      {label && (
        <span style={{ fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4, background: 'var(--surface3)', border: '1px solid var(--border2)', color: 'var(--text)' }}>{label}</span>
      )}
      {hdrInfo && (
        <span style={{ fontSize: 9, fontFamily: 'var(--mono)', fontWeight: 700, padding: '1px 4px', borderRadius: 3, background: hdrInfo.bg, color: hdrInfo.color }}>{hdr}</span>
      )}
    </span>
  )
}

// ── Match feedback ────────────────────────────────────────────────────────────
// Hue as text only (never a fill): green agrees with the PM, red differs, amber
// is a partial title overlap. Lets the user see at a glance exactly why a
// candidate ranks where it does before committing to a delete or a grab.
const MATCH_FIELDS = [['year', 'YR'], ['res', 'RES'], ['source', 'SRC'], ['audio', 'AUD'], ['hdr', 'HDR'], ['group', 'GRP']]
const MATCH_COLOR  = { same: 'var(--green)', diff: 'var(--red)', partial: 'var(--yellow)' }
const MATCH_MARK   = { same: '✓', diff: '✗', partial: '~' }

function MatchChips({ match }) {
  if (!match) return null
  const items = MATCH_FIELDS.filter(([k]) => match[k])
  if (!items.length) return null
  return (
    <span style={{ display: 'inline-flex', gap: 7, flexShrink: 0 }}>
      {items.map(([k, label]) => (
        <span key={k} title={`${label}: ${match[k]}`} style={{
          fontSize: 9, fontFamily: 'var(--mono)', fontWeight: 700, letterSpacing: 0.3,
          color: MATCH_COLOR[match[k]] || 'var(--text-dim)',
        }}>{label}{MATCH_MARK[match[k]] || ''}</span>
      ))}
    </span>
  )
}

function ScoreBadge({ score }) {
  if (score == null) return null
  const pct = Math.round(score * 100)
  const color = score >= 0.8 ? 'var(--green)' : score >= 0.5 ? 'var(--text-dim)' : 'var(--red)'
  return <span style={{ fontSize: 10, fontFamily: 'var(--mono)', fontWeight: 700, color, flexShrink: 0, width: 34, textAlign: 'right' }}>{pct}%</span>
}

// A selectable candidate row — works for both client torrents (name/tracker) and
// arr releases (title/indexer/seeders/quality).
function CandidateRow({ cand, selected, onSelect }) {
  const name = cand.name || cand.title || ''
  const sub  = cand.tracker || cand.indexer || ''
  return (
    <div onClick={onSelect} style={{
      display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', cursor: 'pointer',
      borderBottom: '1px solid var(--border)',
      background: selected ? `${ACCENT}0e` : 'transparent',
      borderLeft: `2px solid ${selected ? ACCENT : 'transparent'}`,
    }}>
      <span style={{ width: 13, height: 13, borderRadius: '50%', flexShrink: 0, border: `1.5px solid ${selected ? ACCENT : 'var(--border2)'}`, background: selected ? ACCENT : 'transparent' }} />
      {/* Full name, wrapped — the release name is the thing being vetted, so it must never truncate */}
      <span style={{ flex: 1, minWidth: 0, fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text)', lineHeight: 1.45, overflowWrap: 'anywhere' }}>{name}</span>
      {cand.quality_name && <QualityChip label={cand.quality_name} hdr={cand.hdr} />}
      <MatchChips match={cand.match} />
      {sub && <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: cand.pm_tracker ? ACCENT : 'var(--text-dim)', flexShrink: 0 }}>{sub}</span>}
      {cand.seeders != null && <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: cand.seeders > 0 ? 'var(--green)' : 'var(--red)', flexShrink: 0 }}>{cand.seeders}S</span>}
      <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>{formatBytes(cand.size)}</span>
      <ScoreBadge score={cand.match_score} />
    </div>
  )
}

function NoneRow({ selected, onSelect, label }) {
  return (
    <div onClick={onSelect} style={{
      display: 'flex', alignItems: 'center', gap: 10, padding: '7px 12px', cursor: 'pointer',
      background: selected ? 'var(--surface3)' : 'transparent',
      borderLeft: `2px solid ${selected ? 'var(--text-dim)' : 'transparent'}`,
    }}>
      <span style={{ width: 13, height: 13, borderRadius: '50%', flexShrink: 0, border: `1.5px solid ${selected ? 'var(--text-dim)' : 'var(--border2)'}`, background: selected ? 'var(--text-dim)' : 'transparent' }} />
      <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>{label}</span>
    </div>
  )
}

// ── Step 4: the release to grab ───────────────────────────────────────────────
// The exact release on the tracker that sent the PM is what the user means to
// grab, so it is not a row in a list: it is the answer, stated. Anything else —
// the same release on another tracker, a near match — is an edge case, and the
// card says which it is when it is not the PM's tracker.
function RecommendedRelease({ cand, selected, onSelect, indexer }) {
  const where = cand.pm_tracker
    ? { text: `on ${indexer}, the tracker that sent the PM`, color: ACCENT }
    : indexer
      ? { text: `not listed on ${indexer} — grabbing from ${cand.indexer || 'another tracker'} means cross-seeding it there`, color: 'var(--yellow)' }
      : { text: 'exact match for the new release name', color: 'var(--text-dim)' }
  return (
    <div onClick={onSelect} style={{
      cursor: 'pointer', borderRadius: 10, padding: '14px 16px',
      background: selected ? `${ACCENT}0e` : 'var(--surface2)',
      border: `1px solid ${selected ? ACCENT : 'var(--border2)'}`,
      boxShadow: 'var(--elev-1)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <span style={{ width: 14, height: 14, borderRadius: '50%', flexShrink: 0, border: `1.5px solid ${selected ? ACCENT : 'var(--border2)'}`, background: selected ? ACCENT : 'transparent' }} />
        <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text)' }}>Grab this one</span>
        <span style={{ fontSize: 11, color: where.color }}>· {where.text}</span>
      </div>
      <div style={{ fontSize: 13, fontFamily: 'var(--mono)', color: 'var(--text)', lineHeight: 1.45, overflowWrap: 'anywhere', marginBottom: 8 }}>
        {cand.title}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        {cand.quality_name && <QualityChip label={cand.quality_name} hdr={cand.hdr} />}
        <MatchChips match={cand.match} />
        {cand.indexer && <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: cand.pm_tracker ? ACCENT : 'var(--text-dim)' }}>{cand.indexer}</span>}
        {cand.seeders != null && <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: cand.seeders > 0 ? 'var(--green)' : 'var(--red)' }}>{cand.seeders} seeders</span>}
        <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>{formatBytes(cand.size)}</span>
      </div>
    </div>
  )
}

// ── Step 3: the confirm table ─────────────────────────────────────────────────
// `hardlinked` is the most important column here: it is the only thing between
// "remove with files" and destroying the only copy. `null` is an unknown and
// never renders as a tick.
const LINK_STATE = {
  true:  { mark: '✓ linked', color: 'var(--green)', title: 'Every file of this torrent has a link outside this group — normally your library copy. Removing the group leaves that copy in place.' },
  false: { mark: '✗ only copy', color: 'var(--red)', title: 'Nothing outside this group links to at least one of these files. Removing the group destroys it.' },
  null:  { mark: '? unchecked', color: 'var(--yellow)', title: 'auditorr could not check these files — the path is not visible inside the container, or the torrent path mapping does not cover it. That is not the same as safe.' },
}

const HEALTH = {
  unregistered: { text: 'unregistered', color: 'var(--red)' },
  not_working:  { text: 'not responding', color: 'var(--yellow)' },
  working:      { text: 'working', color: 'var(--text-dim)' },
}

const cell = { fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }

function GroupTable({ torrents }) {
  const showInstance = torrents.some(t => t.instance_name)
  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '6px 12px', borderBottom: '1px solid var(--border)', background: 'var(--surface2)' }}>
        <span style={{ ...cell, width: 84 }}>library link</span>
        <span style={{ ...cell, flex: 1, minWidth: 0 }}>torrent</span>
        <span style={{ ...cell, width: 150, textAlign: 'right' }}>tracker</span>
        <span style={{ ...cell, width: 64, textAlign: 'right' }}>size</span>
        <span style={{ ...cell, width: 96, textAlign: 'right' }}>seeded · up</span>
      </div>
      {torrents.map(t => {
        const link = LINK_STATE[String(t.hardlinked ?? null)]
        const health = HEALTH[t.tracker_health]
        return (
          <div key={t.hash} style={{ display: 'flex', alignItems: 'flex-start', gap: 10, padding: '8px 12px', borderBottom: '1px solid var(--border)' }}>
            <span title={t.hardlinked === false && t.only_copy_bytes ? `${link.title} (${formatBytes(t.only_copy_bytes)})` : link.title}
              style={{ ...cell, width: 84, fontSize: 11, fontWeight: 700, color: link.color }}>
              {link.mark}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text)', lineHeight: 1.45, overflowWrap: 'anywhere' }}>{t.name}</div>
              <div style={{ display: 'flex', gap: 10, marginTop: 2, flexWrap: 'wrap' }}>
                <span title={t.hash} style={cell}>hash {String(t.hash).slice(0, 12)}…</span>
                {showInstance && t.instance_name && <span style={cell}>on {t.instance_name}</span>}
              </div>
            </div>
            <div style={{ width: 150, textAlign: 'right', flexShrink: 0 }}>
              <div style={{ ...cell, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.tracker || 'no tracker'}</div>
              {/* The tracker already dropping this registration is it agreeing, in
                  the client, that the PM is real (TR12). */}
              {health && (
                <div title={t.tracker_msg || undefined} style={{ ...cell, color: health.color, marginTop: 2 }}>
                  {health.text}{t.tracker_health === 'unregistered' ? ' ✓ trumped' : ''}
                </div>
              )}
            </div>
            <span style={{ ...cell, width: 64, textAlign: 'right', fontSize: 11 }}>{formatBytes(t.size)}</span>
            <div style={{ width: 96, textAlign: 'right', flexShrink: 0 }}>
              <div style={cell}>{t.seeding_time != null ? formatDuration(t.seeding_time) : '—'}</div>
              <div style={{ ...cell, marginTop: 2, color: t.uploaded > 0 ? 'var(--green)' : 'var(--text-dim)' }}>{t.uploaded != null ? `↑ ${formatBytes(t.uploaded)}` : '—'}</div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

// The group could not be established completely. Same idiom as Backfill's and
// Triage's unreachable-arr warning: an unexplained gap is not an acceptable way
// to report one, and here the gap is a torrent that would break.
function PartialWarning({ group, ack, onAck }) {
  if (!group?.partial) return null
  const reasons = []
  if (group.unknown_listings) reasons.push(`${group.unknown_listings} torrent${group.unknown_listings !== 1 ? 's' : ''} that could share these files did not return a file list`)
  if (group.prefilter?.bounded) reasons.push(`the search for cross-seeds was narrowed to exact-size matches (${group.prefilter.widened} near neighbours, over the ${group.prefilter.bound} limit)`)
  return (
    <WorkflowWarning>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>This group may be incomplete</div>
      <div>
        {reasons.join('; ')}. A cross-seed missing from this list still points at these files, and removing the group
        with its files would break it. Retrying often resolves a timed-out listing.
      </div>
      <label onClick={onAck} style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, cursor: 'pointer', color: 'var(--text)' }}>
        <Checkbox checked={ack} onChange={onAck} />
        <span>I understand a cross-seed may be missing, and want to remove this group anyway</span>
      </label>
    </WorkflowWarning>
  )
}

function OnlyCopyModal({ info, clientName, busy, onCancel, onConfirm }) {
  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  const bytes = formatBytes(info.only_copy_bytes || 0)
  // Portal to <body>, as ConfirmDeleteModal does: the page's fade-in leaves a
  // transform that would make position:fixed resolve against the page.
  return createPortal(
    <div onClick={onCancel} style={{ position: 'fixed', inset: 0, zIndex: 200, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.55)' }}>
      <div onClick={e => e.stopPropagation()} style={{
        width: 'min(560px, calc(100vw - 48px))', maxHeight: 'calc(100vh - 96px)', display: 'flex', flexDirection: 'column',
        background: 'var(--surface)', border: '1px solid var(--border2)', borderRadius: 12, boxShadow: '0 16px 60px rgba(0,0,0,0.5)',
      }}>
        <div style={{ padding: '18px 20px 0' }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--red)' }}>This destroys the only copy of {bytes}</div>
          <p style={{ fontSize: 12.5, color: 'var(--text)', lineHeight: 1.6, margin: '10px 0 0' }}>
            Nothing outside this group links to <b>{info.only_copy_files} file{info.only_copy_files !== 1 ? 's' : ''}</b> ({bytes}) —
            no library copy, no other cross-seed. Removing the group from {clientName} with its files deletes them for good.
            The replacement grab downloads a <b>different</b> release; it does not bring these back.
          </p>
          <p style={{ fontSize: 11.5, color: 'var(--text-dim)', margin: '8px 0 0', lineHeight: 1.5 }}>
            Usually this means the trumped release was never imported, was imported by copy, or has since been upgraded in
            Sonarr/Radarr. If you want to keep it, cancel and use <b>Grab only</b>.
          </p>
        </div>
        <div style={{ margin: '14px 20px 0', border: '1px solid var(--border)', borderRadius: 8, overflowY: 'auto', flex: '0 1 auto' }}>
          {(info.torrents || []).map(t => (
            <div key={t.hash} style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '6px 12px', borderBottom: '1px solid var(--border)' }}>
              <span style={{ flex: 1, minWidth: 0, fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text)', overflowWrap: 'anywhere' }}>{t.name}</span>
              <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--red)', flexShrink: 0 }}>{formatBytes(t.only_copy_bytes || 0)}</span>
            </div>
          ))}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '14px 20px 18px' }}>
          <span style={{ flex: 1 }} />
          <ActionButton onClick={onCancel} disabled={busy}>Cancel</ActionButton>
          <ActionButton danger onClick={onConfirm} disabled={busy}>
            {busy ? 'Removing…' : `Remove and destroy ${bytes}`}
          </ActionButton>
        </div>
      </div>
    </div>,
    document.body
  )
}

function LinkSummary({ group }) {
  const t = group.torrents
  const lc = group.link_check || {}
  if (t.some(m => m.hardlinked === false)) {
    return (
      <div style={{ fontSize: 12, color: 'var(--red)', lineHeight: 1.6 }}>
        <b>{lc.only_copy_files} file{lc.only_copy_files !== 1 ? 's' : ''} ({formatBytes(lc.only_copy_bytes || 0)})</b> ha{lc.only_copy_files !== 1 ? 've' : 's'} no
        link anywhere outside this group — removing it destroys the only copy.
      </div>
    )
  }
  if (t.some(m => m.hardlinked == null)) {
    return (
      <div style={{ fontSize: 12, color: 'var(--yellow)', lineHeight: 1.6 }}>
        auditorr could not check {lc.unchecked_files || 'some'} file{lc.unchecked_files !== 1 ? 's' : ''} — make sure your library holds a copy before removing.
      </div>
    )
  }
  return (
    <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
      Every file has a link outside this group — normally your library copy — so it survives until Sonarr/Radarr imports the replacement.
    </div>
  )
}

export default function Trumped({ onNavigate, initialOldTitle, triageDeadSeeds }) {
  const toast = useToast()

  // Arriving from a Triage dead seed pre-fills the trumped release (TR16).
  const [pmText, setPmText]       = useState('')
  const [oldTitles, setOldTitles] = useState(() => (initialOldTitle ? [initialOldTitle] : []))
  const [newTitle, setNewTitle]   = useState('')
  const [parsed, setParsed]       = useState(!!initialOldTitle)

  const [indexers, setIndexers] = useState([])
  const [indexer, setIndexer]   = useState('')

  const [picks, setPicks]       = useState(null)   // phase 1: [{title, auto, candidates:[…]}]
  const [selected, setSelected] = useState({})     // pick index -> chosen hash | null
  const [group, setGroup]       = useState(null)   // phase 2 response + seed_hashes
  const [ackPartial, setAckPartial] = useState(false)
  const [search, setSearch]     = useState(null)   // search_release response
  const [conflict, setConflict] = useState(null)   // 409 arr_item_conflict payload
  const [chosenRelease, setChosenRelease] = useState(null)
  const [showOthers, setShowOthers] = useState(false)
  const [clientDeleteAllowed, setClientDeleteAllowed] = useState(false)
  const [clientName, setClientName] = useState('the client')

  const [busy, setBusy]       = useState(null)   // 'parse'|'group'|'search'|'execute'
  const [error, setError]     = useState(null)
  const [result, setResult]   = useState(null)
  const [onlyCopy, setOnlyCopy] = useState(null) // info for the second confirmation
  const [queued, setQueued]   = useState(null)   // 409 already_queued
  const [watch, setWatch]     = useState(null)   // {status, message} of the import watch

  const mountedRef   = useRef(true)
  const watchPollRef = useRef(null)
  const operationRef = useRef(null)   // this confirmation's operation id (S09)
  useEffect(() => () => { mountedRef.current = false; clearTimeout(watchPollRef.current) }, [])

  useEffect(() => {
    api.workflowIndexers().then(d => setIndexers(d.indexers || d || [])).catch(() => {})
    api.getConfig().then(cfg => {
      setClientDeleteAllowed(!!cfg.ALLOW_CLIENT_DELETE)
      setClientName(cfg.TORRENT_SOURCE === 'qui' ? 'qui' : 'qBittorrent')
    }).catch(() => {})
  }, [])

  // TR14 — downstream state is only valid for the inputs it was computed from.
  // Editing a title or the indexer after phase 1 used to leave the old picks
  // and tie-break on screen; editing the new title left the old search.
  const titlesKey = oldTitles.map(t => t.trim()).filter(Boolean).join('\n')
  useEffect(() => { setPicks(null); setSelected({}); setGroup(null) }, [titlesKey, indexer])
  useEffect(() => { setSearch(null); setConflict(null); setChosenRelease(null) }, [newTitle, group])
  useEffect(() => { setAckPartial(false) }, [group])

  const reset = useCallback(() => {
    setPmText(''); setOldTitles([]); setNewTitle(''); setParsed(false)
    setIndexer(''); setPicks(null); setSelected({}); setGroup(null)
    setSearch(null); setConflict(null); setChosenRelease(null); setError(null); setResult(null)
    setOnlyCopy(null); setQueued(null); setWatch(null); clearTimeout(watchPollRef.current)
    operationRef.current = null
  }, [])

  const handleParse = async () => {
    setBusy('parse'); setError(null)
    try {
      const r = await api.trumpParse(pmText)
      setOldTitles(r.old_titles || []); setNewTitle(r.new_title); setParsed(true)
    } catch (e) {
      // Parse failure: drop into manual entry rather than blocking
      setParsed(true)
      toast(e.message, 'info')
    }
    setBusy(null)
  }

  // Phase 1 — fetch the ranked candidate torrents for each trumped title.
  const handleFindTorrents = async () => {
    setBusy('group'); setError(null)
    try {
      const r = await api.trumpResolveGroup(oldTitles.filter(t => t.trim()), null, indexer)
      setPicks(r.picks || [])
      // Keyed by position, not title: a PM listing one release twice — or two
      // lines that trim alike — used to highlight and deselect together (TR15).
      const sel = {}
      ;(r.picks || []).forEach((p, i) => { sel[i] = p.auto })
      setSelected(sel)
    } catch (e) {
      setError(e.message)
    }
    setBusy(null)
  }

  // Phase 2 — expand the confirmed seeds into their full cross-seed group.
  const handleExpandGroup = async () => {
    setBusy('group'); setError(null)
    try {
      const hashes = [...new Set((picks || []).map((_, i) => selected[i]).filter(Boolean))]
      const g = await api.trumpResolveGroup(oldTitles.filter(t => t.trim()), hashes)
      setGroup({ ...g, seed_hashes: hashes })
    } catch (e) {
      setError(e.message)
    }
    setBusy(null)
  }

  const groupPaths = group ? [...new Set(group.torrents.flatMap(t => t.paths || []))] : []

  const handleSearch = async (arrItem) => {
    setBusy('search'); setError(null); setConflict(null)
    try {
      const r = await api.trumpSearchRelease({
        new_title: newTitle, indexer, group_paths: groupPaths, arr_item: arrItem || undefined,
      })
      setSearch(r)
      // Only an exact release is pre-selected. A near match is offered, never
      // chosen for the user.
      setChosenRelease(r.release || null)
      setShowOthers(!r.release)
    } catch (e) {
      if (e.code === 'arr_item_conflict') {
        setConflict(e.data)
      } else {
        const d = e.data || {}
        setSearch(d.fallback_url || d.arr_errors?.length
          ? { release: null, candidates: [], fallback_url: d.fallback_url, arr_errors: d.arr_errors || [], error: e.message }
          : null)
        setChosenRelease(null)
        setError(e.message)
      }
    }
    setBusy(null)
  }

  // Follow the grab into the library — the same watch, status poll and
  // bottom-right panel Backfill uses.
  const followImport = useCallback((jobId) => {
    window.dispatchEvent(new CustomEvent('auditorr:import_started'))
    setWatch({ status: 'queued', message: 'Queued — waiting for download client' })
    const poll = async () => {
      try {
        const data = await api.watchImportStatus(jobId)
        if (!mountedRef.current) return
        setWatch({ status: data.status, message: data.message })
        if (WATCH_ACTIVE.includes(data.status)) watchPollRef.current = setTimeout(poll, 3000)
      } catch (_) {}
    }
    poll()
  }, [])

  const handleExecute = async ({ remove, ackOnlyCopy = false, force = false }) => {
    setBusy('execute'); setError(null); setQueued(null)
    // Minted on the confirmation and kept until an answer arrives, so a retry of
    // a request whose answer never came resends it rather than grabbing twice.
    if (!operationRef.current) operationRef.current = newOperationId()
    try {
      const r = await api.trumpExecute({
        operation_id: operationRef.current,
        hashes: remove ? group.torrents.map(t => ({ hash: t.hash, instance_id: t.instance_id })) : [],
        seed_hashes: remove ? group.seed_hashes : undefined,
        acknowledge_partial: remove && ackPartial,
        acknowledge_only_copy: remove && ackOnlyCopy,
        release: chosenRelease ? { guid: chosenRelease.guid, indexer_id: chosenRelease.indexer_id,
                                   info_hash: chosenRelease.info_hash || undefined } : null,
        service: search?.service,
        connection_id: search?.connection_id,
        arr_id: search?.arr_id,
        arr_title: search?.arr_title,
        library_file_ids: search?.library_file_ids || [],
        force: force || undefined,
      })
      operationRef.current = null
      setResult(r)
      setOnlyCopy(null)
      if (r.watch_job_id) followImport(r.watch_job_id)
      const parts = []
      if (r.grabbed === true) parts.push('Grabbed the replacement')
      if (r.grabbed === false) parts.push(`The grab failed (${r.grab_error}) — nothing was removed`)
      if (r.removed) parts.push(`removed ${r.removed} torrent${r.removed !== 1 ? 's' : ''}`)
      if (r.removal_error) parts.push('but the old torrents are still in your client')
      toast(parts.join(' and ') || 'Done',
            r.grabbed === false || r.removal_error ? 'error' : 'success')
    } catch (e) {
      // An answer arrived, whatever it said, so this confirmation is spent: the
      // next attempt is a new operation. Only a request that got no answer at
      // all keeps its id, which is what makes a retry safe.
      if (e.data) operationRef.current = null
      if (e.code === 'in_progress') {
        setError(e.message)
      } else if (e.code === 'only_copy') {
        setOnlyCopy(e.data)
      } else if (e.code === 'group_changed') {
        // Back to step 3: the picks stay, the group is re-resolved on one click.
        setOnlyCopy(null); setGroup(null); setError(e.message)
      } else if (e.code === 'partial') {
        setOnlyCopy(null)
        setGroup(g => g && { ...g, partial: true, unknown_listings: e.data?.unknown_listings, prefilter: e.data?.prefilter })
        setError(e.message)
      } else if (e.code === 'already_queued') {
        setOnlyCopy(null); setQueued({ message: e.message, remove })
      } else {
        setError(e.message)
      }
    }
    setBusy(null)
  }

  const handleRemove = () => {
    const only = group.torrents.filter(t => t.hardlinked === false)
    if (only.length) {
      setOnlyCopy({
        only_copy_bytes: group.link_check?.only_copy_bytes || 0,
        only_copy_files: group.link_check?.only_copy_files || only.length,
        torrents: only,
      })
    } else {
      handleExecute({ remove: true })
    }
  }

  const candidates  = search?.candidates || []
  const recommended = search?.release ? (candidates.find(c => c.guid === search.release.guid) || search.release) : null
  const others      = candidates.filter(c => c.guid !== recommended?.guid)
  const skippedTitles = picks ? picks.filter((_, i) => !selected[i]).map(p => p.title) : []
  const removeBlocked = !clientDeleteAllowed || (group?.partial && !ackPartial)

  // Step gating
  const step2 = parsed
  const step3 = parsed && picks != null
  const step4 = step3 && (search != null || conflict != null)
  const step5 = step3 && search != null

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 22, maxWidth: 980 }}>
      <WorkflowHeader
        title="Trumped"
        accent={ACCENT}
        blurb="When a tracker trumps one of your releases, paste the PM here: auditorr finds the whole hardlink group (every cross-seed), removes it from the client with its files, and grabs the replacement through Sonarr/Radarr — the manual multi-step swap, automated and confirmed at every step."
        right={(parsed || picks) && (
          <button onClick={reset} style={{ fontSize: 12, padding: '6px 16px', borderRadius: 7, cursor: 'pointer', border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)' }}>
            ↺ Start over
          </button>
        )}
      />

      <WorkflowCrossLink
        text="Missed a PM? Imported torrents your tracker has already dropped are listed as dead seeds:"
        linkLabel="Triage"
        count={triageDeadSeeds}
        onClick={() => onNavigate && onNavigate({ tab: 'triage' })}
      />

      <WorkflowError message={error} />

      <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 10, boxShadow: 'var(--elev-1)', padding: '20px 22px' }}>
        {/* Step 1 — paste PM */}
        <StepShell n={1} active done={parsed} title="Paste the trump PM">
          {!parsed ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <textarea
                value={pmText} onChange={e => setPmText(e.target.value)}
                placeholder={SAMPLE_PM}
                rows={7}
                style={{
                  width: '100%', boxSizing: 'border-box', padding: '10px 12px', borderRadius: 'var(--r)',
                  border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)',
                  fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 1.5, resize: 'vertical',
                }}
              />
              <div style={{ display: 'flex', gap: 8 }}>
                <ActionButton primary onClick={handleParse} disabled={busy != null || !pmText.trim()}>
                  {busy === 'parse' ? 'Parsing…' : 'Parse PM'}
                </ActionButton>
                <ActionButton onClick={() => setParsed(true)} disabled={busy != null}>
                  Enter titles manually
                </ActionButton>
              </div>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                  Trumped (old) release{oldTitles.length > 1 ? `s — ${oldTitles.length} torrents, one per line` : ' — one per line'}
                </span>
                <textarea
                  value={oldTitles.join('\n')}
                  onChange={e => setOldTitles(e.target.value.split('\n'))}
                  rows={Math.min(Math.max(oldTitles.length, 1), 12)}
                  style={{
                    width: '100%', boxSizing: 'border-box', padding: '7px 10px', borderRadius: 'var(--r)',
                    border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)',
                    fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 1.5, resize: 'vertical',
                  }}
                />
              </label>
              <Field label="Replacement (new) release" value={newTitle} onChange={setNewTitle} mono />
            </div>
          )}
        </StepShell>

        {/* Step 2 — select tracker */}
        <StepShell n={2} active={step2} done={picks != null} title="Which tracker sent the PM?">
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Indexer (optional — its torrents and its copy of the replacement come first)</span>
              <select
                value={indexer} onChange={e => setIndexer(e.target.value)}
                style={{ padding: '7px 10px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)', fontSize: 12, minWidth: 220 }}
              >
                <option value="">Any indexer</option>
                {indexers.map(name => <option key={name} value={name}>{name}</option>)}
              </select>
            </label>
            {picks == null && (
              <ActionButton primary onClick={handleFindTorrents} disabled={busy != null || !oldTitles.some(t => t.trim())}>
                {busy === 'group' ? 'Finding torrents…' : 'Find matching torrents →'}
              </ActionButton>
            )}
          </div>
        </StepShell>

        {/* Step 3 — pick the torrents, then confirm the expanded group */}
        <StepShell n={3} active={step3} done={search != null} title="Confirm the hardlink group to remove">
          {picks && !group && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
                Pick the torrent that matches each trumped release — the best match is pre-selected. Once you confirm, every cross-seed of the chosen torrents is added automatically.
              </div>
              {picks.map((p, i) => (
                <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <div style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', wordBreak: 'break-all' }}>{p.title}</div>
                  {p.candidates.length === 0 ? (
                    <div style={{ fontSize: 11, color: 'var(--yellow)' }}>No match found in {clientName} — this release will be skipped.</div>
                  ) : (
                    <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
                      {p.candidates.map(c => (
                        <CandidateRow key={c.hash} cand={c}
                          selected={selected[i] === c.hash}
                          onSelect={() => setSelected(s => ({ ...s, [i]: c.hash }))} />
                      ))}
                      <NoneRow label="None of these — skip this release"
                        selected={selected[i] == null}
                        onSelect={() => setSelected(s => ({ ...s, [i]: null }))} />
                    </div>
                  )}
                </div>
              ))}
              <ActionButton primary onClick={handleExpandGroup} disabled={busy != null || !Object.values(selected).some(Boolean)}>
                {busy === 'group' ? 'Expanding…' : 'Confirm & find cross-seeds →'}
              </ActionButton>
            </div>
          )}

          {group && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
                <b style={{ color: 'var(--text)' }}>{group.torrents.length} torrent{group.torrents.length !== 1 ? 's' : ''}</b>
                {' '}— the selected releases plus every cross-seed sharing their files — will be removed from {clientName} <b>with their files</b>.
                {' '}Cross-seeds sharing a path point at the same files, so the payload is <b style={{ color: 'var(--text)' }}>{formatBytes(group.total_size)}</b> once, not once per torrent.
              </div>
              <LinkSummary group={group} />
              <PartialWarning group={group} ack={ackPartial} onAck={() => setAckPartial(a => !a)} />
              {skippedTitles.length > 0 && (
                <div style={{ fontSize: 11, color: 'var(--yellow)', background: 'var(--yellow)10', border: '1px solid var(--yellow)30', borderRadius: 8, padding: '8px 12px', lineHeight: 1.6 }}>
                  Skipped (no torrent selected):
                  <div style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)', marginTop: 4 }}>
                    {skippedTitles.map((t, i) => <div key={i}>{t}</div>)}
                  </div>
                </div>
              )}
              <GroupTable torrents={group.torrents} />
              <div style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.6 }}>
                Rows can’t be deselected: a cross-seed sharing a path with this group uses the same files, so removing any member
                with its files breaks the rest. A cross-seed with its own hardlinks isn’t listed — it keeps its files. To keep a
                registration on a tracker that hasn’t trumped this release, remove the others by hand in {clientName} and use
                {' '}<b>Grab only</b> below.
              </div>
              {search == null && !conflict && (
                <div style={{ display: 'flex', gap: 8 }}>
                  <ActionButton primary onClick={() => handleSearch()} disabled={busy != null}>
                    {busy === 'search' ? 'Searching…' : 'Find replacement release →'}
                  </ActionButton>
                  <button onClick={() => setGroup(null)} disabled={busy != null} style={{ fontSize: 12, padding: '6px 14px', borderRadius: 7, cursor: 'pointer', border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text-dim)' }}>
                    ← Change selection
                  </button>
                </div>
              )}
            </div>
          )}
        </StepShell>

        {/* Step 4 — replacement release */}
        <StepShell n={4} active={step4} done={result != null} title="Replacement release">
          {conflict && (
            <WorkflowWarning>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>Two answers for which title this is</div>
              <div>{conflict.message}</div>
              <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                <ActionButton primary onClick={() => handleSearch(conflict.path_item)} disabled={busy != null}>
                  Search {conflict.path_item?.title}{conflict.path_item?.year ? ` (${conflict.path_item.year})` : ''} — your library files
                </ActionButton>
                <ActionButton onClick={() => handleSearch(conflict.title_item)} disabled={busy != null}>
                  Search {conflict.title_item?.title}{conflict.title_item?.year ? ` (${conflict.title_item.year})` : ''} — the new release name
                </ActionButton>
              </div>
            </WorkflowWarning>
          )}
          {search && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <ArrErrorsWarning errors={search.arr_errors}
                extra="A release or library item on an instance that did not answer is not shown here." />
              {search.arr_title && (
                <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                  Searching <span style={{ color: 'var(--text)' }}>{search.arr_title}{search.arr_year ? ` (${search.arr_year})` : ''}</span>
                  {search.resolved_by === 'path' && ' — matched by the library files this group is hardlinked to'}
                  {search.resolved_by === 'title' && ' — matched by title (no library file of this group was found in Sonarr/Radarr)'}
                </div>
              )}
              {recommended ? (
                <>
                  <RecommendedRelease cand={recommended} indexer={indexer}
                    selected={chosenRelease?.guid === recommended.guid}
                    onSelect={() => setChosenRelease(recommended)} />
                  <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
                    <NoneRow label="Don't grab — I'll handle the replacement myself"
                      selected={chosenRelease == null}
                      onSelect={() => setChosenRelease(null)} />
                  </div>
                </>
              ) : (
                <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
                  No exact match for the new release name in {search.candidate_count ?? 0} release{search.candidate_count !== 1 ? 's' : ''}
                  {others.length > 0 ? ' — the closest are below; nothing is selected for you.' : '.'}
                  {search.fallback_url && <> Or grab it manually in <a href={search.fallback_url} target="_blank" rel="noopener noreferrer" style={{ color: ACCENT }}>Sonarr/Radarr ↗</a>.</>}
                </div>
              )}
              {others.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {recommended && (
                    <button onClick={() => setShowOthers(s => !s)} style={{ alignSelf: 'flex-start', fontSize: 11, padding: 0, border: 'none', background: 'none', cursor: 'pointer', color: 'var(--text-dim)' }}>
                      {showOthers ? '▾' : '▸'} Other releases ({others.length}) — for edge cases
                    </button>
                  )}
                  {showOthers && (
                    <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
                      {others.map(r => (
                        <CandidateRow key={r.guid} cand={r}
                          selected={chosenRelease?.guid === r.guid}
                          onSelect={() => setChosenRelease(r)} />
                      ))}
                      {!recommended && (
                        <NoneRow label="Don't grab — I'll handle the replacement myself"
                          selected={chosenRelease == null}
                          onSelect={() => setChosenRelease(null)} />
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </StepShell>

        {/* Step 5 — execute */}
        <StepShell n={5} active={step5 && result == null} done={result != null} title="Grab the replacement & remove the group">
          {result ? (
            <div style={{ fontSize: 12, color: 'var(--text)', lineHeight: 1.7 }}>
              {/* Stage by stage, in the order they ran (S09) — so a swap that
                  stopped half way says which half, and what is left to do. */}
              {(result.stages || []).filter(s => s.status !== 'skipped').map(s => (
                <div key={s.stage} style={{ display: 'flex', gap: 6, alignItems: 'flex-start',
                                            color: s.status === 'failed' ? 'var(--red)' : 'var(--text)' }}>
                  <span style={{ flexShrink: 0 }}>{s.status === 'failed' ? '✗' : '✓'}</span>
                  <span>{s.message}</span>
                </div>
              ))}
              {!result.stages && <>
                {result.grabbed === true && <>✓ Replacement grabbed.{' '}</>}
                {result.removed > 0 && <>✓ Removed <b>{result.removed}</b> torrent{result.removed !== 1 ? 's' : ''} and their files.</>}
              </>}
              {result.grabbed === true && !result.removed && !result.removal_error && (
                <div style={{ color: 'var(--text-dim)' }}>The trumped torrents are still in {clientName} — remove them there.</div>
              )}
              {watch && (
                <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 8, color: watchColor(watch.status) }}>
                  {WATCH_ACTIVE.includes(watch.status) && <Spinner size={10} />}
                  <span>Import: {watch.message}</span>
                </div>
              )}
              <div style={{ color: 'var(--text-dim)', marginTop: 4 }}>
                {result.watch_job_id
                  ? 'auditorr follows the download into Sonarr/Radarr — it is in the import panel, bottom right — and re-audits once the import lands.'
                  : result.removed > 0 ? 'The watchdog picks up the change and re-audits.' : null}
              </div>
            </div>
          ) : step5 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {!clientDeleteAllowed && (
                <div style={{ padding: '10px 14px', background: 'var(--yellow)10', border: '1px solid var(--yellow)30', borderRadius: 8, color: 'var(--yellow)', fontSize: 12 }}>
                  Client deletion is disabled, so the group can’t be removed from here. Enable “Workflow torrent deletion” in <a onClick={() => onNavigate && onNavigate({ tab: 'config' })} style={{ color: 'var(--yellow)', cursor: 'pointer', textDecoration: 'underline' }}>Config → Torrent Source</a> — or grab the replacement only, and remove the torrents in {clientName} yourself.
                </div>
              )}
              {queued && (
                <WorkflowWarning>
                  <div>{queued.message}</div>
                  <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                    <ActionButton onClick={() => handleExecute({ remove: queued.remove, force: true })} disabled={busy != null}>Grab anyway</ActionButton>
                    <ActionButton onClick={() => { setChosenRelease(null); setQueued(null) }} disabled={busy != null}>Don’t grab</ActionButton>
                  </div>
                </WorkflowWarning>
              )}
              <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>
                {chosenRelease
                  ? <>This grabs the replacement first, and removes <b style={{ color: 'var(--text)' }}>{group.torrents.length} torrent{group.torrents.length !== 1 ? 's' : ''}</b> ({formatBytes(group.total_size)} payload) from {clientName} with their files only once Sonarr/Radarr has accepted it — so a grab that fails leaves your files alone.</>
                  : <>This removes <b style={{ color: 'var(--text)' }}>{group.torrents.length} torrent{group.torrents.length !== 1 ? 's' : ''}</b> ({formatBytes(group.total_size)} payload) from {clientName} with their files.</>}
                {' '}There is no undo.
              </div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <ActionButton danger onClick={handleRemove} disabled={busy != null || removeBlocked}
                  title={group?.partial && !ackPartial ? 'Acknowledge the incomplete group above first' : undefined}>
                  {busy === 'execute' ? 'Executing…' : (chosenRelease ? 'Grab replacement + remove group' : 'Remove group')}
                </ActionButton>
                {chosenRelease && (
                  <ActionButton onClick={() => handleExecute({ remove: false })} disabled={busy != null}>
                    Grab only
                  </ActionButton>
                )}
              </div>
            </div>
          )}
        </StepShell>
      </div>
      {busy && <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--text-dim)' }}><Spinner /> Working…</div>}
      {onlyCopy && (
        <OnlyCopyModal info={onlyCopy} clientName={clientName} busy={busy === 'execute'}
          onCancel={() => setOnlyCopy(null)}
          onConfirm={() => handleExecute({ remove: true, ackOnlyCopy: true })} />
      )}
      <SpinKeyframes />
    </div>
  )
}
