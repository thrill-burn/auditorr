import React, { useState, useRef, useEffect } from 'react'
import { createPortal } from 'react-dom'

// ── Staying current ───────────────────────────────────────────────────────────
//
// Every workflow page fetches its own report — those live outside App's
// `results`, so App refreshing after an audit did nothing for them and a page
// left open across a scan showed pre-scan data until you navigated away and
// back. App dispatches `auditorr:audit_complete` when a scan finishes; this
// subscribes a page's own loader to it.
//
// **This replaces the per-page Refresh buttons, which were a stale page with a
// button on it.** Polling was considered and rejected, on cost and on logic:
// every one of these reports is built from the *last audit's* stored rows, so
// a poll between audits re-fetches identical bytes — and not cheap ones.
// Cleanup and Dedupe deserialize the full file list (the known RAM hotspot),
// and Triage's verify phase fans out to the torrent client's per-torrent
// tracker endpoint on a deliberately capped 8-worker pool. There is exactly
// one event that can change what these pages show, so listen for that one.
//
// The chain closes without the user doing anything: act on an item, the
// watchdog sees the filesystem change, defers while the session is still
// active, scans, and this fires.
export function useAuditComplete(onComplete) {
  const ref = useRef(onComplete)
  ref.current = onComplete
  useEffect(() => {
    const h = () => ref.current && ref.current()
    window.addEventListener('auditorr:audit_complete', h)
    return () => window.removeEventListener('auditorr:audit_complete', h)
  }, [])
}

// ── Shared option lists ───────────────────────────────────────────────────────
export const QUALITY_RES_OPTIONS = [
  { value: '2160p', label: '2160p / 4K' },
  { value: '1080p', label: '1080p'      },
  { value: '720p',  label: '720p'       },
  // SD: 480p and 576p, and a DVD with no resolution of its own (Radarr reports 0).
  { value: '480p',  label: '480p / SD'  },
]
export const QUALITY_SOURCE_OPTIONS = [
  { value: 'remux',  label: 'Remux'  },
  { value: 'bluray', label: 'Bluray' },
  { value: 'webdl',  label: 'WEB-DL' },
  { value: 'webrip', label: 'WEBRip' },
  { value: 'hdtv',   label: 'HDTV'   },
  { value: 'dvd',    label: 'DVD'    },
]
export const HDR_OPTIONS = [
  { value: 'DV',     label: 'Dolby Vision' },
  { value: 'HDR10+', label: 'HDR10+' },
  { value: 'HDR10',  label: 'HDR10'  },
  { value: 'HDR',    label: 'HDR'    },
  { value: 'HLG',    label: 'HLG'    },
  { value: 'SDR',    label: 'SDR'    },
]
export const HDR_STYLE = {
  'DV':     { bg: '#7c3aed20', color: '#a78bfa' },
  'HDR10+': { bg: '#1d4ed820', color: '#60a5fa' },
  'HDR10':  { bg: '#0e749820', color: '#38bdf8' },
  'HDR':    { bg: '#05966920', color: '#34d399' },
  'HLG':    { bg: '#0f766e20', color: '#2dd4bf' },
}

// ── Chip ──────────────────────────────────────────────────────────────────────
export function Chip({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '3px 10px', borderRadius: 'var(--r-pill)', fontSize: 12, cursor: 'pointer',
        border: active ? '1px solid var(--accent)' : '1px solid var(--border2)',
        background: active ? 'var(--accent)18' : 'transparent',
        color: active ? 'var(--accent)' : 'var(--text-dim)',
        fontWeight: active ? 600 : 400,
      }}
    >
      {children}
    </button>
  )
}

// ── Labeled chips (options with separate display labels) ─────────────────────
export function LabeledChips({ options, value, onChange, allLabel = 'Any' }) {
  const noneSelected = value.length === 0
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      <Chip active={noneSelected} onClick={() => onChange([])}>{allLabel}</Chip>
      {options.map(opt => {
        const active = value.includes(opt.value)
        return (
          <Chip key={opt.value} active={active}
            onClick={() => onChange(active ? value.filter(v => v !== opt.value) : [...value, opt.value])}>
            {opt.label}
          </Chip>
        )
      })}
    </div>
  )
}

// ── Indexer chips ─────────────────────────────────────────────────────────────
export function IndexerChips({ options, value, onChange, allLabel = 'All' }) {
  const noneSelected = value.length === 0
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      <Chip active={noneSelected} onClick={() => onChange([])}>{allLabel}</Chip>
      {options.map(opt => {
        const active = value.includes(opt)
        return (
          <Chip key={opt} active={active}
            onClick={() => onChange(active ? value.filter(v => v !== opt) : [...value, opt])}>
            {opt}
          </Chip>
        )
      })}
    </div>
  )
}

// ── Folder chips ──────────────────────────────────────────────────────────────
export function FolderChips({ folders, selected, onChange }) {
  const noneSelected = selected.length === 0
  const toggle = name => onChange(selected.includes(name) ? selected.filter(f => f !== name) : [...selected, name])
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      <Chip active={noneSelected} onClick={() => onChange([])}>All</Chip>
      {folders.map(({ name, count }) => (
        <Chip key={name} active={selected.includes(name)} onClick={() => toggle(name)}>
          {name} <span style={{ opacity: 0.55 }}>({count})</span>
        </Chip>
      ))}
    </div>
  )
}

// ── Sort picker ───────────────────────────────────────────────────────────────
export function SortPicker({ options, value, onChange }) {
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      {options.map(opt => {
        const active = value === opt.value
        return (
          <button
            key={opt.value}
            onClick={() => onChange(opt.value)}
            style={{
              display: 'flex', flexDirection: 'column', alignItems: 'flex-start',
              padding: '8px 14px', borderRadius: 'var(--r)', cursor: 'pointer', minWidth: 110,
              border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
              background: active ? 'var(--surface3)' : 'var(--surface)',
              color: 'var(--text)',
              boxShadow: 'var(--elev-1)',
            }}
          >
            <span style={{ fontSize: 13, fontWeight: 600 }}>{opt.label}</span>
            <span style={{ fontSize: 11, marginTop: 2, opacity: 0.6, fontFamily: 'var(--mono)' }}>{opt.sub}</span>
          </button>
        )
      })}
    </div>
  )
}

// ── Count picker ──────────────────────────────────────────────────────────────
// null means "all available"
export function CountPicker({ value, onChange, max }) {
  const inputRef = useRef(null)
  const [inputVal, setInputVal] = useState(() =>
    value !== null && value !== 5 ? String(value) : ''
  )

  const customActive = value !== null && value !== 5
  const allActive    = value === null
  const fiveActive   = value === 5

  function handleFive() {
    setInputVal('')
    onChange(5)
  }

  function handleAll() {
    setInputVal('')
    onChange(null)
  }

  function handleInput(e) {
    const raw = e.target.value
    setInputVal(raw)
    if (raw === '') { onChange(5); return }
    const n = parseInt(raw, 10)
    if (!isNaN(n) && n >= 1) onChange(max > 0 ? Math.min(n, max) : n)
  }

  const cardStyle = (active) => ({
    display: 'flex', flexDirection: 'column', alignItems: 'center',
    padding: '10px 22px', borderRadius: 'var(--r)', minWidth: 90,
    border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
    background: active ? 'var(--surface3)' : 'var(--surface)',
    color: 'var(--text)',
    boxShadow: 'var(--elev-1)',
  })

  const numStyle = (active) => ({
    fontSize: 22, fontWeight: 700, fontFamily: 'var(--mono)', lineHeight: '26px',
    height: 26, display: 'flex', alignItems: 'center',
    color: 'var(--text)',
  })

  const subStyle = { fontSize: 11, marginTop: 3, fontFamily: 'var(--mono)', opacity: 0.7 }

  return (
    <div style={{ display: 'flex', gap: 10 }}>
      <button onClick={handleFive} style={{ ...cardStyle(fiveActive), cursor: 'pointer', border: 'none', borderWidth: 1, borderStyle: 'solid', borderColor: fiveActive ? 'var(--accent)' : 'var(--border)' }}>
        <span style={numStyle(fiveActive)}>5</span>
        <span style={subStyle}>quick</span>
      </button>

      <div style={{ ...cardStyle(customActive), cursor: 'text' }} onClick={() => inputRef.current?.focus()}>
        <input
          ref={inputRef}
          type="number"
          min={1}
          max={max || undefined}
          value={inputVal}
          onChange={handleInput}
          placeholder="—"
          style={{
            ...numStyle(customActive),
            width: 54, textAlign: 'center',
            background: 'none', border: 'none', outline: 'none',
            padding: 0, margin: 0, fontWeight: 700,
          }}
        />
        <span style={subStyle}>custom</span>
      </div>

      <button
        onClick={handleAll}
        disabled={max === 0}
        style={{ ...cardStyle(allActive), cursor: max === 0 ? 'not-allowed' : 'pointer', opacity: max === 0 ? 0.4 : 1, border: 'none', borderWidth: 1, borderStyle: 'solid', borderColor: allActive ? 'var(--accent)' : 'var(--border)' }}
      >
        <span style={numStyle(allActive)}>{max > 0 ? max : '—'}</span>
        <span style={subStyle}>all</span>
      </button>

      <style>{`
        input[type=number]::-webkit-inner-spin-button,
        input[type=number]::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
        input[type=number] { -moz-appearance: textfield; }
      `}</style>
    </div>
  )
}

// ── Section label ─────────────────────────────────────────────────────────────
export function SectionLabel({ children }) {
  return (
    <div style={{ fontFamily: 'var(--sans)', fontSize: 13, fontWeight: 600, letterSpacing: 0, textTransform: 'none', textAlign: 'left', color: 'var(--text)', marginBottom: 8 }}>
      {children}
    </div>
  )
}

// ── Workflow page header ──────────────────────────────────────────────────────
export function WorkflowHeader({ title, blurb, accent, right }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontFamily: 'var(--sans)', fontSize: 13, fontWeight: 600, color: 'var(--text)', letterSpacing: 0, textTransform: 'none', textAlign: 'left', marginBottom: 4 }}>Workflows</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 20, fontWeight: 700, color: 'var(--text)', lineHeight: 1.2 }}>{title}</span>
        </div>
        {blurb && (
          <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.6, maxWidth: 960 }}>
            {blurb}
          </p>
        )}
      </div>
      {right && <div style={{ flexShrink: 0 }}>{right}</div>}
    </div>
  )
}

// ── Cross-link between sibling workflows ──────────────────────────────────────
export function WorkflowCrossLink({ text, linkLabel, count, onClick }) {
  if (!count) return null
  return (
    <button
      onClick={onClick}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 6, alignSelf: 'flex-start',
        padding: '6px 12px', borderRadius: 'var(--r)', fontSize: 12, cursor: 'pointer',
        border: '1px dashed var(--border2)', background: 'transparent', color: 'var(--text-dim)',
        transition: 'all 0.12s',
      }}
      onMouseEnter={e => { e.currentTarget.style.color = 'var(--text)'; e.currentTarget.style.borderColor = 'var(--accent)' }}
      onMouseLeave={e => { e.currentTarget.style.color = 'var(--text-dim)'; e.currentTarget.style.borderColor = 'var(--border2)' }}
    >
      {text}
      <span style={{ color: 'var(--accent)', fontWeight: 600 }}>{linkLabel} ({count}) →</span>
    </button>
  )
}

// ── Empty / success state ─────────────────────────────────────────────────────
export function EmptyState({ emoji = '🎉', title, sub }) {
  return (
    <div style={{ padding: '64px 0', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, textAlign: 'center' }}>
      <div style={{ fontSize: 40, lineHeight: 1 }}>{emoji}</div>
      <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--text)', marginTop: 6 }}>{title}</div>
      {sub && <div style={{ fontSize: 12.5, color: 'var(--text-dim)', maxWidth: 420, lineHeight: 1.6 }}>{sub}</div>}
    </div>
  )
}

// ── Loading spinner row ───────────────────────────────────────────────────────
export function LoadingRow({ label = 'Loading…' }) {
  return (
    <div style={{ padding: '48px 0', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, color: 'var(--text-dim)', fontSize: 13 }}>
      <Spinner />
      {label}
      <SpinKeyframes />
    </div>
  )
}

export function Spinner({ size = 12 }) {
  return (
    <span style={{ display: 'inline-block', width: size, height: size, borderRadius: '50%', border: '2px solid var(--accent)', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />
  )
}

export function SpinKeyframes() {
  return <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
}

// ── Error banner ──────────────────────────────────────────────────────────────
export function WorkflowError({ message }) {
  if (!message) return null
  return (
    <div style={{ padding: '10px 14px', background: 'var(--red)10', border: '1px solid var(--red)30', borderRadius: 'var(--r)', color: 'var(--red)', fontSize: 13 }}>
      {message}
    </div>
  )
}

// Something is degraded but the page still works — distinct from WorkflowError,
// which means the page has nothing to show.
export function WorkflowWarning({ children }) {
  if (!children) return null
  return (
    <div style={{ padding: '10px 14px', background: 'var(--yellow)10', border: '1px solid var(--yellow)30', borderRadius: 'var(--r)', color: 'var(--yellow)', fontSize: 13, lineHeight: 1.5 }}>
      {children}
    </div>
  )
}

// Sonarr/Radarr instances that did not answer, or answered with only part of
// their library. Shared because the consequence is the same on every page that
// resolves anything against an arr: the rows that instance manages are simply
// absent, which is indistinguishable from an instance that manages nothing.
// `extra` is the per-page sentence about what that absence does *here*.
export function ArrErrorsWarning({ errors, extra }) {
  if (!errors?.length) return null
  const nPartial = errors.filter(e => e.partial).length
  const nDown    = errors.length - nPartial
  const clauses = []
  if (nDown)    clauses.push(`${nDown} Sonarr/Radarr instance${nDown !== 1 ? 's' : ''} could not be read`)
  if (nPartial) clauses.push(`${nPartial} ${nDown ? '' : `Sonarr/Radarr instance${nPartial !== 1 ? 's' : ''} `}`
                           + `answered with only part of ${nPartial !== 1 ? 'their libraries' : 'its library'}`)
  return (
    <WorkflowWarning>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{clauses.join(', ')}</div>
      <div>
        {extra}{extra ? ' ' : ''}
        {errors.map(e => `${e.name || e.connection_id || 'unnamed'}: ${e.message}`).join(' · ')}
      </div>
    </WorkflowWarning>
  )
}

// A torrent registration's key — the same string `sources.registration_key`
// builds server-side (S05). A hash is not an identity: the same torrent can be
// registered on two qui instances at two save paths, and every map keyed by hash
// alone kept whichever came first. The bare hash where there is no instance
// (qBittorrent), which is also what every server answer is keyed by there.
export function regKey(t) {
  if (!t) return ''
  if (t.reg) return t.reg
  return t.instance_id == null ? String(t.hash || '') : `${t.instance_id}:${t.hash}`
}

// A request refused because a torrent is registered on more than one instance
// and the request did not say which (409 `registration_ambiguous`). Nothing was
// done; the sentence names the instances, since that is what the user acts on.
export function RegistrationWarning({ refusal }) {
  const ambiguous = refusal?.ambiguous
  if (!ambiguous?.length) return null
  const names = [...new Set(ambiguous.flatMap(a => a.instances || []))]
  return (
    <WorkflowWarning>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        {ambiguous.length} torrent{ambiguous.length !== 1 ? 's are' : ' is'} registered on more than one instance
      </div>
      <div>
        {names.join(', ')} each hold {ambiguous.length !== 1 ? 'these torrents' : 'this torrent'}, and auditorr will
        not pick one of them for you — nothing was done. Remove the extra registration in your client, or act on
        the row that belongs to the instance you mean.
      </div>
    </WorkflowWarning>
  )
}

// ── Checkbox ──────────────────────────────────────────────────────────────────
export function Checkbox({ checked, indeterminate, onChange }) {
  return (
    <span
      onClick={e => { e.stopPropagation(); onChange() }}
      style={{
        width: 15, height: 15, borderRadius: 'var(--r-sm)', flexShrink: 0, cursor: 'pointer',
        border: `1.5px solid ${checked || indeterminate ? 'var(--accent)' : 'var(--border2)'}`,
        background: checked || indeterminate ? 'var(--accent)' : 'transparent',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        transition: 'all 0.1s',
      }}
    >
      {checked && (
        <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      )}
      {!checked && indeterminate && (
        <span style={{ width: 7, height: 2, background: '#fff', borderRadius: 1 }} />
      )}
    </span>
  )
}

// ── Selection action bar (sticky bottom) ──────────────────────────────────────
export function ActionBar({ children, summary }) {
  return (
    <div style={{
      position: 'sticky', bottom: 16, zIndex: 50,
      background: 'var(--surface)', border: '1px solid var(--border2)', borderRadius: 'var(--rl)',
      padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 12,
      boxShadow: 'var(--shadow-pop)',
    }}>
      <div style={{ flex: 1, minWidth: 0, fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-dim)' }}>{summary}</div>
      <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>{children}</div>
    </div>
  )
}

// ── Exclude confirmation ──────────────────────────────────────────────────────
//
// No exclusion is written without the user seeing the exact string first.
// Cleanup and Triage both build patterns from real paths, and a construction
// bug there is invisible by nature: the toast says "added" whether the rule
// matches the file, matches nothing, or matches half the folder. This is the
// one part of that fix that generalises — it also covers the residual the
// ≥2-segment folder rule leaves behind (an install whose library folders carry
// the release name gets both trees from a release-folder pattern too).
//
// Shared because the two pages need the same dialog, following the
// ConfirmDeleteModal idiom next door rather than inventing a second one.

// Mirrors db.EXCLUSION_PATTERN_MAX_CHARS. The server is authoritative and
// refuses over-long patterns with a count; this only warns before the round trip.
const MAX_PATTERN_CHARS = 200

export function ConfirmExcludeModal({ patterns, subtitle, note, busy, onCancel, onConfirm }) {
  const tooLong = patterns.filter(p => p.length > MAX_PATTERN_CHARS)

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  // Portal to <body> for the same reason ConfirmDeleteModal does: the page's
  // fade-in leaves a transform, which makes position:fixed resolve against the
  // page instead of the viewport.
  return createPortal(
    <div
      onClick={onCancel}
      style={{
        position: 'fixed', inset: 0, zIndex: 200, display: 'flex',
        alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.55)',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: 'min(620px, calc(100vw - 48px))', maxHeight: 'calc(100vh - 96px)',
          display: 'flex', flexDirection: 'column',
          background: 'var(--surface)', border: '1px solid var(--border2)',
          borderRadius: 12, boxShadow: '0 16px 60px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ padding: '18px 20px 0' }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text)' }}>
            Add {patterns.length} exclusion rule{patterns.length !== 1 ? 's' : ''}
          </div>
          <p style={{ fontSize: 12.5, color: 'var(--text)', lineHeight: 1.6, margin: '10px 0 0' }}>
            {subtitle} Excluded files are left out of scoring, workflows and duplicate
            detection from the next audit on — nothing is deleted. These land in
            <b> Config → Excluded Files &amp; Folders</b>, where you can edit or remove them.
          </p>
          {note && (
            <p style={{ fontSize: 11.5, color: 'var(--text-dim)', margin: '8px 0 0', lineHeight: 1.5 }}>
              {note}
            </p>
          )}
          {/* Says what will happen and offers a route that exists. It used to
              read "select the whole release folder instead" — which is wrong
              whenever auditorr has already declined to use that folder, and
              those are exactly the rows that end up here. A refusal message
              that recommends an impossible action is worse than none. */}
          {tooLong.length > 0 && (
            <p style={{ fontSize: 11.5, color: 'var(--yellow)', margin: '8px 0 0', lineHeight: 1.5 }}>
              {tooLong.length} rule{tooLong.length !== 1 ? 's are' : ' is'} longer than {MAX_PATTERN_CHARS} characters
              and will be refused. auditorr uses one rule for the whole release folder
              wherever that is safe; these are the files where it is not, so they need a
              rule per file and the path itself is too long. Add a shorter rule by hand in
              Config → Excluded Files &amp; Folders — a <span style={{ fontFamily: 'var(--mono)' }}>contains:</span> rule
              on a distinctive part of the name is usually enough.
            </p>
          )}
        </div>
        <div style={{ margin: '14px 20px 0', border: '1px solid var(--border)', borderRadius: 8, overflowY: 'auto', flex: '0 1 auto' }}>
          {patterns.map((p, i) => {
            const over = p.length > MAX_PATTERN_CHARS
            return (
              <div key={`${p}-${i}`} title={p} style={{
                padding: '6px 12px', borderBottom: i < patterns.length - 1 ? '1px solid var(--border)' : 'none',
                fontSize: 11, fontFamily: 'var(--mono)', wordBreak: 'break-all',
                color: over ? 'var(--yellow)' : 'var(--text)',
              }}>
                {p}
                {over && <span style={{ opacity: 0.8 }}> · {p.length} chars</span>}
              </div>
            )
          })}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '14px 20px 18px' }}>
          <span style={{ flex: 1 }} />
          <ActionButton onClick={onCancel} disabled={busy}>Cancel</ActionButton>
          <ActionButton primary onClick={onConfirm} disabled={busy}>
            {busy ? 'Excluding…' : `Add ${patterns.length} rule${patterns.length !== 1 ? 's' : ''}`}
          </ActionButton>
        </div>
      </div>
    </div>,
    document.body
  )
}

export function ActionButton({ onClick, disabled, danger, primary, children, title }) {
  const color = danger ? 'var(--red)' : primary ? 'var(--accent)' : 'var(--text)'
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        fontSize: 12, fontWeight: 600, padding: '8px 16px', borderRadius: 'var(--r)',
        cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.45 : 1,
        border: `1px solid ${danger ? 'var(--red)40' : primary ? 'var(--accent)' : 'var(--border2)'}`,
        background: danger ? 'var(--surface2)' : primary ? 'var(--accent)' : 'var(--surface2)',
        color: primary ? '#0a0a0a' : color,
      }}
    >
      {children}
    </button>
  )
}
