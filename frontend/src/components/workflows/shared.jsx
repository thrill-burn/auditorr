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

// ── Tints ─────────────────────────────────────────────────────────────────────
//
// An alpha tint of a theme colour, as `color-mix` — **never `var(--x)NN`**. CSS
// substitutes var() as tokens and does not re-parse the result, so
// `var(--red)40` is a colour followed by a stray number: invalid, and the
// browser drops the whole declaration. `border: 1px solid var(--red)40` draws
// no border at all. That is how every warning box lost its box, every danger
// button its hairline and every coloured row chip its border, for as long as
// the idiom was in use (UI pass, 2026-09-21). `backend_tests/test_tint_idiom.py`
// fails the build on the old spelling.
export const tint = (color, pct) => `color-mix(in srgb, ${color} ${pct}%, transparent)`

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
        padding: '3px 10px', borderRadius: 'var(--r-pill)', fontSize: 'var(--font-base)', cursor: 'pointer',
        border: active ? '1px solid var(--accent)' : '1px solid var(--border2)',
        background: active ? tint('var(--accent)', 9) : 'transparent',
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
            <span style={{ fontSize: 'var(--font-base)', fontWeight: 600 }}>{opt.label}</span>
            <span style={{ fontSize: 'var(--font-sm)', marginTop: 2, opacity: 0.6, fontFamily: 'var(--mono)' }}>{opt.sub}</span>
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
    fontSize: 'var(--font-xl)', fontWeight: 700, fontFamily: 'var(--mono)', lineHeight: '24px',
    height: 24, display: 'flex', alignItems: 'center',
    color: 'var(--text)',
  })

  const subStyle = { fontSize: 'var(--font-sm)', marginTop: 3, fontFamily: 'var(--mono)', opacity: 0.7 }

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

// ── Headings ──────────────────────────────────────────────────────────────────
//
// Three weights, one per role, taken from Triage (UI pass, 2026-09-21): a
// section heading is 700, a sub-heading 600, an item's title 500 — all at
// --font-md, so the hierarchy is weight alone and R8's sizes do not move.
// Cleanup's piles and Backfill's section labels were 600, level with their own
// sub-labels; Cleanup's and Dedupe's item titles were a mono 600 heavier than
// the heading above them. Rounds and Trumped were already here.
//
// Item titles pick their typeface by kind, as Rounds does: sans for a title
// auditorr knows ("Heat (1995)"), mono for a raw path or release name.
export const ITEM_TITLE = { fontSize: 'var(--font-md)', fontWeight: 500, color: 'var(--text)' }

export function SectionLabel({ children }) {
  return (
    <div style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-md)', fontWeight: 700, letterSpacing: 0, textTransform: 'none', textAlign: 'left', color: 'var(--text)', marginBottom: 8 }}>
      {children}
    </div>
  )
}

export function Dot({ color, size = 7 }) {
  return <span className="ui-status-dot" style={{ width: size, height: size, background: color }} />
}

// A pile / verdict / bucket heading over a list: [checkbox] [dot] title meta,
// then its description set under the title rather than under the checkbox.
//
// `check` is `{ checked, indeterminate, onChange }`, or `null` for a section
// with nothing selectable — which still reserves the slot, so the dots of every
// section on a page sit in one column. `sub` is the second level: no dot, the
// title in its hue at 600.
export function SectionHeading({ title, dot, color, meta, desc, check, sub = false }) {
  const indent = (check !== undefined ? 25 : 0) + (dot ? 17 : 0)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: sub ? 4 : 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        {check ? <Checkbox checked={check.checked} indeterminate={check.indeterminate} onChange={check.onChange} />
          : check === null ? <span style={{ width: 15, height: 15, flexShrink: 0 }} /> : null}
        {dot && <Dot color={dot} />}
        <span style={{ fontSize: 'var(--font-md)', fontWeight: sub ? 600 : 700, color: color || 'var(--text)' }}>{title}</span>
        {meta != null && (
          <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>{meta}</span>
        )}
      </div>
      {desc && (
        <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: `0 0 ${sub ? 4 : 2}px ${indent}px`, lineHeight: 1.5, maxWidth: 960 }}>
          {desc}
        </p>
      )}
    </div>
  )
}

// ── Stat box ──────────────────────────────────────────────────────────────────
// Cleanup's and Dedupe's summary tiles were two copies that had drifted (12 vs
// 9 radius). Hue, where there is one, is the dot beside the label.
export function StatBox({ label, value, sub, dot }) {
  return (
    <div style={{
      padding: '12px 16px', borderRadius: 'var(--rl)', flex: 1, minWidth: 140,
      background: 'var(--surface)', border: '1px solid var(--border)', boxShadow: 'var(--elev-1)',
    }}>
      <div style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-md)', fontWeight: 600, letterSpacing: 0, textTransform: 'none', color: 'var(--text)', marginBottom: 5, display: 'flex', alignItems: 'center', gap: 7 }}>
        {dot && <Dot color={dot} />}
        {label}
      </div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-xl)', fontWeight: 700, color: 'var(--text)', lineHeight: 1 }}>{value}</div>
      {sub && <div style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

// ── Release evidence ──────────────────────────────────────────────────────────
// A quality label and its HDR tag. `unknown` renders the absence as a readout
// (Triage's rows say "unknown") rather than as nothing (Trumped's candidates).
export function QualityChip({ label, hdr, dim, unknown = false }) {
  const hdrInfo = HDR_STYLE[hdr]
  if (!label && !hdrInfo) {
    return unknown
      ? <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', opacity: 0.5 }}>unknown</span>
      : null
  }
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      {label && (
        <span style={{
          fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 'var(--r-sm)',
          background: dim ? 'var(--surface2)' : 'var(--surface3)',
          border: '1px solid var(--border2)',
          color: dim ? 'var(--text-dim)' : 'var(--text)', whiteSpace: 'nowrap',
        }}>
          {label}
        </span>
      )}
      {hdrInfo && (
        <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', fontWeight: 700, padding: '1px 4px', borderRadius: 3, background: hdrInfo.bg, color: hdrInfo.color, whiteSpace: 'nowrap' }}>
          {hdr}
        </span>
      )}
    </span>
  )
}

// How a release compares with the file it would replace or match, field by
// field. Hue as text only, never a fill: green agrees, red differs, amber is a
// partial overlap. `fields` is [[key, LABEL], …] — each page asks its own
// question (Backfill: size/quality/HDR; Trumped: the PM's title fields).
export const MATCH_COLOR = { same: 'var(--green)', diff: 'var(--red)', partial: 'var(--yellow)' }
const MATCH_MARK = { same: '✓', diff: '✗', partial: '~' }

export function MatchChips({ match, fields, titleSuffix = '' }) {
  if (!match) return null
  const items = fields.filter(([k]) => match[k])
  if (!items.length) return null
  return (
    <span style={{ display: 'inline-flex', gap: 6, flexShrink: 0 }}>
      {items.map(([k, label]) => (
        <span key={k} title={`${label}: ${match[k]}${titleSuffix}`} style={{
          fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', fontWeight: 700, letterSpacing: 0.3,
          color: MATCH_COLOR[match[k]] || 'var(--text-dim)',
        }}>{label}{MATCH_MARK[match[k]] || ''}</span>
      ))}
    </span>
  )
}

// ── Disclosure ────────────────────────────────────────────────────────────────
// A text toggle for a folded list, chevron after the label — Rounds' "Show all
// feats" shape, which Trumped's "Other releases ▸" now shares.
export function Disclosure({ open, onClick, children }) {
  return (
    <button
      onClick={onClick}
      aria-expanded={open}
      style={{
        alignSelf: 'flex-start', background: 'none', border: 'none', padding: 0, cursor: 'pointer',
        fontSize: 'var(--font-base)', color: 'var(--text-dim)', display: 'flex', alignItems: 'center', gap: 6,
      }}
    >
      {children}
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
        strokeLinecap="round" strokeLinejoin="round"
        style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', opacity: 0.5 }}>
        <polyline points="9 18 15 12 9 6" />
      </svg>
    </button>
  )
}

// ── Workflow page ─────────────────────────────────────────────────────────────
// The frame every workflow page sits in: the app's page gutter (Rounds' too),
// and room at the foot for the sticky action bar.
export function WorkflowPage({ gap = 22, maxWidth, children }) {
  return (
    <div className="fade-in" style={{
      padding: 'var(--page-gutter) var(--page-gutter) 48px', display: 'flex', flexDirection: 'column', gap,
      ...(maxWidth ? { maxWidth } : null),
    }}>
      {children}
    </div>
  )
}

// ── Workflow page header ──────────────────────────────────────────────────────
export function WorkflowHeader({ title, blurb, accent, right }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-md)', fontWeight: 600, color: 'var(--text)', letterSpacing: 0, textTransform: 'none', textAlign: 'left', marginBottom: 4 }}>Workflows</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 'var(--font-xl)', fontWeight: 700, color: 'var(--text)', lineHeight: 1.2 }}>{title}</span>
        </div>
        {blurb && (
          <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.6, maxWidth: 960 }}>
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
        padding: '6px 12px', borderRadius: 'var(--r)', fontSize: 'var(--font-base)', cursor: 'pointer',
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
// A line icon, not the 🎉 it used to be: the design system allows emoji only as
// functional status glyphs in dense rows, never as decoration.
export function EmptyState({ title, sub }) {
  return (
    <div style={{ padding: '64px 0', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, textAlign: 'center' }}>
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--green)" strokeWidth="1.75"
        strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="10" /><path d="m9 12 2 2 4-4" />
      </svg>
      <div style={{ fontSize: 'var(--font-lg)', fontWeight: 700, color: 'var(--text)', marginTop: 6 }}>{title}</div>
      {sub && <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', maxWidth: 420, lineHeight: 1.6 }}>{sub}</div>}
    </div>
  )
}

// ── Loading spinner row ───────────────────────────────────────────────────────
export function LoadingRow({ label = 'Loading…' }) {
  return (
    <div style={{ padding: '48px 0', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, color: 'var(--text-dim)', fontSize: 'var(--font-base)' }}>
      <Spinner />
      {label}
      <SpinKeyframes />
    </div>
  )
}

export function Spinner({ size = 12, weight = 2 }) {
  return (
    <span style={{ display: 'inline-block', flexShrink: 0, width: size, height: size, borderRadius: '50%', border: `${weight}px solid var(--accent)`, borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />
  )
}

export function SpinKeyframes() {
  return <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
}

// ── Error banner ──────────────────────────────────────────────────────────────
export function WorkflowError({ message }) {
  if (!message) return null
  return (
    <div style={{ padding: '10px 14px', background: tint('var(--red)', 6), border: `1px solid ${tint('var(--red)', 19)}`, borderRadius: 'var(--r)', color: 'var(--red)', fontSize: 'var(--font-base)' }}>
      {message}
    </div>
  )
}

// Something is degraded but the page still works — distinct from WorkflowError,
// which means the page has nothing to show.
export function WorkflowWarning({ children }) {
  if (!children) return null
  return (
    <div style={{ padding: '10px 14px', background: tint('var(--yellow)', 6), border: `1px solid ${tint('var(--yellow)', 19)}`, borderRadius: 'var(--r)', color: 'var(--yellow)', fontSize: 'var(--font-base)', lineHeight: 1.5 }}>
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
      <div style={{ flex: 1, minWidth: 0, fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>{summary}</div>
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
          <div style={{ fontSize: 'var(--font-lg)', fontWeight: 700, color: 'var(--text)' }}>
            Add {patterns.length} exclusion rule{patterns.length !== 1 ? 's' : ''}
          </div>
          <p style={{ fontSize: 'var(--font-base)', color: 'var(--text)', lineHeight: 1.6, margin: '10px 0 0' }}>
            {subtitle} Excluded files are left out of scoring, workflows and duplicate
            detection from the next audit on — nothing is deleted. These land in
            <b> Config → Excluded Files &amp; Folders</b>, where you can edit or remove them.
          </p>
          {note && (
            <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: '8px 0 0', lineHeight: 1.5 }}>
              {note}
            </p>
          )}
          {/* Says what will happen and offers a route that exists. It used to
              read "select the whole release folder instead" — which is wrong
              whenever auditorr has already declined to use that folder, and
              those are exactly the rows that end up here. A refusal message
              that recommends an impossible action is worse than none. */}
          {tooLong.length > 0 && (
            <p style={{ fontSize: 'var(--font-base)', color: 'var(--yellow)', margin: '8px 0 0', lineHeight: 1.5 }}>
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
                fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', wordBreak: 'break-all',
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
          <Button onClick={onCancel} disabled={busy}>Cancel</Button>
          <Button variant="primary" onClick={onConfirm} disabled={busy}>
            {busy ? 'Excluding…' : `Add ${patterns.length} rule${patterns.length !== 1 ? 's' : ''}`}
          </Button>
        </div>
      </div>
    </div>,
    document.body
  )
}

// ── Button ────────────────────────────────────────────────────────────────────
//
// Every button on the workflow surfaces, the script modal and Rounds (UI pass,
// 2026-09-21). There were fourteen hand-rolled styles — seven paddings, five
// radii, two weights, white and near-black text on the same orange — and none
// had a hover. The design system's `Button` is the model.
//
// Variants:
//   primary    orange fill, dark text. At most one per view: the forward action,
//              never a destructive one.
//   secondary  the raised surface. The default.
//   danger     red text on a faint red wash with a red hairline — quiet, per the
//              ration-colour rule, and the hairline it always meant to have.
//   ghost      transparent, dim text: a quiet control beside a louder one.
//   subtle     a neutral row chip: raised, dim text.
//   `tone`     any theme colour: its text and hairline, on an 8% wash. Row chips
//              that carry a hue (Grab, radarr ↗, Dedupe ↗, Failed ↺).
//
// Sizes:
//   md    every standalone button — action bars, modal footers, page headers,
//         wizard steps, Rounds' "Open …".
//   sm    a small control inside a panel or a sentence (Retry, Select all).
//   chip  an action inside a row — R8's "action chip", at --font-sm like the
//         tags it sits beside, so a slot that swaps a chip for a tag keeps its size.
//
// Colours arrive as custom properties and index.css's `.wf-btn` applies them,
// because an inline `background` would outrank the `:hover` rule. Pass `href`
// for a link that looks like a button (the arr and client chips).
const BUTTON_SIZE = {
  md:   { fontSize: 'var(--font-base)', fontWeight: 600, padding: '8px 16px', borderRadius: 'var(--r)' },
  sm:   { fontSize: 'var(--font-base)', fontWeight: 500, padding: '4px 10px', borderRadius: 'var(--r)' },
  chip: { fontSize: 'var(--font-sm)', fontWeight: 500, padding: '1px 7px', borderRadius: 'var(--r-sm)', fontFamily: 'var(--mono)' },
}

const BUTTON_LOOK = {
  primary:   { bg: 'var(--accent)', border: 'var(--accent)', fg: '#0a0a0a', filter: 'brightness(1.08)' },
  secondary: { bg: 'var(--surface2)', border: 'var(--border2)', fg: 'var(--text)', bgHover: 'var(--surface3)' },
  danger:    { bg: tint('var(--red)', 7), border: tint('var(--red)', 25), fg: 'var(--red)', bgHover: tint('var(--red)', 12) },
  ghost:     { bg: 'transparent', border: 'var(--border2)', fg: 'var(--text-dim)', bgHover: 'var(--surface2)', fgHover: 'var(--text)' },
  subtle:    { bg: 'var(--surface2)', border: 'var(--border2)', fg: 'var(--text-dim)', bgHover: 'var(--surface3)', fgHover: 'var(--text)' },
}

const toneLook = c => ({ bg: tint(c, 8), border: tint(c, 30), fg: c, bgHover: tint(c, 14) })

export function Button({
  variant = 'secondary', size = 'md', tone, href, target, rel,
  onClick, disabled, title, style, children,
}) {
  const look = tone ? toneLook(tone) : (BUTTON_LOOK[variant] || BUTTON_LOOK.secondary)
  const css = {
    fontFamily: 'var(--sans)', lineHeight: 1.25, ...(BUTTON_SIZE[size] || BUTTON_SIZE.md),
    '--btn-bg': look.bg, '--btn-border': look.border, '--btn-fg': look.fg,
    '--btn-bg-hover': look.bgHover || look.bg, '--btn-fg-hover': look.fgHover || look.fg,
    '--btn-filter-hover': look.filter || 'none',
    ...style,
  }
  if (href) {
    return <a className="wf-btn" href={href} target={target} rel={rel} onClick={onClick} title={title} style={css}>{children}</a>
  }
  return (
    <button className="wf-btn" type="button" onClick={onClick} disabled={disabled} title={title} style={css}>
      {children}
    </button>
  )
}
