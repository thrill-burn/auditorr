import React, { useState, useRef, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { tint } from '../../utils'

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
// `tint()` moved to utils.js once the rest of the app needed it too; it is
// re-exported here because every workflow page imports it from the kit.
export { tint }

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

// ── Segmented ─────────────────────────────────────────────────────────────────
//
// Every filter row in the app, whether it takes one choice or several (UI pass,
// 2026-09-22). There were fifteen control shapes doing five jobs — pills,
// bordered pairs, separate rectangles, two inset tracks at different radii —
// and once tint() drew them properly a pill and a button looked equally
// clickable. The rule now: **a pill is never interactive**. Filters are this
// track; actions are `Button`; a single choice that needs a detail line is an
// OptionCard; the any/only/hide flag is `FlagToggle`.
//
// Multi-select is the same track with several segments lit — by the user's
// choice over a row of separate toggle buttons, which retired CLAUDE.md's old
// "multi-select filters stay pill Chips" rule. `allLabel` puts a leading
// All/Any segment that is lit when nothing else is and clears the rest.
//
// options: [{ value, label, icon?, title?, disabled?, tone? }]. `icon` renders
// before the label (a status Dot, a chart swatch). `tone` colours the label of
// a selected segment — Triage's destructive "All cross-seeds" is red.
// size: 'sm' is var(--control-h) (dense toolbars), 'lg' is var(--control-h-lg).
// mono: for readouts (7d/30d/90d); words are sans. Styling lives in index.css's
// `.seg`, because the hover and the selected hairline cannot be inline.
export function Segmented({
  options, value, onChange, multiple = false, allLabel, size = 'sm', mono = false, disabled = false, label,
}) {
  const chosen = multiple ? (value || []) : value
  const isOn = v => (multiple ? chosen.includes(v) : chosen === v)
  const pick = v => {
    if (!multiple) onChange(v)
    else onChange(isOn(v) ? chosen.filter(x => x !== v) : [...chosen, v])
  }
  const cls = 'seg' + (size === 'lg' ? ' seg-lg' : '') + (mono ? ' seg-mono' : '')
  return (
    <div role="group" aria-label={label} className={cls}>
      {multiple && allLabel != null && (
        <button type="button" className="seg-opt" aria-pressed={chosen.length === 0} disabled={disabled}
          onClick={() => onChange([])}>
          {allLabel}
        </button>
      )}
      {options.map(opt => (
        <button key={String(opt.value)} type="button" className="seg-opt"
          aria-pressed={isOn(opt.value)} disabled={disabled || opt.disabled} title={opt.title}
          style={opt.tone ? { '--seg-fg': opt.tone } : undefined}
          onClick={() => pick(opt.value)}>
          {opt.icon}
          {opt.label}
        </button>
      ))}
    </div>
  )
}

// Backfill's filter rows. `value` is a list; [] means no restriction.
export function OptionFilter({ options, value, onChange, allLabel = 'Any' }) {
  return <Segmented multiple allLabel={allLabel} options={options} value={value} onChange={onChange} />
}

export function IndexerFilter({ options, value, onChange, allLabel = 'All' }) {
  return (
    <Segmented multiple allLabel={allLabel} value={value} onChange={onChange}
      options={options.map(o => ({ value: o, label: o }))} />
  )
}

export function FolderFilter({ folders, selected, onChange }) {
  return (
    <Segmented multiple allLabel="All" value={selected} onChange={onChange}
      options={folders.map(({ name, count }) => ({
        value: name,
        label: <>{name} <span style={{ opacity: 0.55, fontWeight: 400 }}>({count})</span></>,
      }))} />
  )
}

// ── Flag toggle ───────────────────────────────────────────────────────────────
// A tri-state filter for an orthogonal boolean: 'any' (neither pressed — the
// flag does not constrain), 'only' (+), 'hide' (−). File Explorer's Duplicates
// and Excluded, and its per-tracker include/exclude. It is a pair and not a
// Segmented because it is not a choice among options: "orphaned but not
// excluded" is a status *and* a flag, which is why issue #23 split them.
const FLAG = {
  minHeight: 'var(--control-h)', padding: '4px 10px', fontFamily: 'var(--sans)',
  fontSize: 'var(--font-base)', lineHeight: 1.25,
}
const flagLook = (on, color) => ({
  fontWeight: on ? 600 : 500,
  '--btn-bg': on ? 'var(--surface2)' : 'transparent',
  '--btn-border': on ? color : 'var(--border2)',
  '--btn-fg': on ? color : 'var(--text-dim)',
  '--btn-bg-hover': on ? 'var(--surface3)' : 'var(--surface2)',
  '--btn-fg-hover': on ? color : 'var(--text)',
  '--btn-filter-hover': 'none',
})

export function FlagToggle({ label, value, onChange, onlyTitle, hideTitle }) {
  const only = value === 'only'
  const hide = value === 'hide'
  return (
    <div role="group" style={{ display: 'inline-flex', flexShrink: 0 }}>
      <button type="button" className="wf-btn" aria-pressed={only} title={onlyTitle}
        onClick={() => onChange(only ? 'any' : 'only')}
        style={{ ...FLAG, ...flagLook(only, 'var(--green)'), borderRadius: 'var(--r) 0 0 var(--r)', borderRight: 'none' }}>
        + {label}
      </button>
      <button type="button" className="wf-btn" aria-pressed={hide} title={hideTitle}
        onClick={() => onChange(hide ? 'any' : 'hide')}
        style={{ ...FLAG, ...flagLook(hide, 'var(--red)'), borderRadius: '0 var(--r) var(--r) 0' }}>
        −
      </button>
    </div>
  )
}

// ── Icon buttons ──────────────────────────────────────────────────────────────
// A quiet square for a line icon. Styling is index.css's `.icon-btn`.
export function IconButton({ onClick, title, pressed, children }) {
  return (
    <button type="button" className="icon-btn" onClick={onClick} title={title} aria-label={title} aria-pressed={pressed}>
      {children}
    </button>
  )
}

// One close control for every modal, popover and panel. It was a × character at
// 20px in three places, 16px in two and a 13px icon in Rounds — and the two
// characters inside the type guard's scope each needed an exemption.
export function CloseButton({ onClick, title = 'Close', size = 14 }) {
  return (
    <IconButton onClick={onClick} title={title}>
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="2.25" strokeLinecap="round" aria-hidden="true">
        <path d="M18 6 6 18M6 6l12 12" />
      </svg>
    </IconButton>
  )
}

// ── Option cards ──────────────────────────────────────────────────────────────
// A single-choice row of cards: Backfill's Release Ranking, Priority and Search
// Depth. Every card is one shape — a sans name over a mono detail — at one
// minimum width, so the three rows line up in columns instead of each card
// being as wide as its own words. Search Depth was a different control (a
// centred 20px number in a taller box) until R9 made it these cards.
//
// The line heights are fixed, not `normal`: a <button> resets line-height to
// normal, and under normal a glyph drawn from a fallback font (the → of A → Z)
// makes its line, and so its card, taller than the ones beside it.
// Colours ride `.wf-btn`'s custom properties, which is what gives them a hover.
const OPTION_ROW  = { display: 'flex', gap: 8, flexWrap: 'wrap' }
const OPTION_CARD = {
  flexDirection: 'column', alignItems: 'flex-start', justifyContent: 'center', gap: 2,
  minWidth: 170, padding: '8px 14px', borderRadius: 'var(--r)', boxShadow: 'var(--elev-1)',
  fontFamily: 'var(--sans)', textAlign: 'left',
}
const OPTION_NAME = { fontSize: 'var(--font-base)', fontWeight: 600, lineHeight: '16px' }
const OPTION_SUB  = { fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', lineHeight: '14px' }
const optionColors = active => ({
  '--btn-bg': active ? 'var(--surface3)' : 'var(--surface)',
  '--btn-bg-hover': active ? 'var(--surface3)' : 'var(--surface2)',
  '--btn-border': active ? 'var(--accent)' : 'var(--border)',
  '--btn-fg': 'var(--text)', '--btn-fg-hover': 'var(--text)', '--btn-filter-hover': 'none',
})

function OptionCard({ active, onClick, disabled, name, sub }) {
  return (
    <button type="button" className="wf-btn" aria-pressed={active} onClick={onClick} disabled={disabled}
      style={{ ...OPTION_CARD, ...optionColors(active) }}>
      <span style={OPTION_NAME}>{name}</span>
      <span style={OPTION_SUB}>{sub}</span>
    </button>
  )
}

// ── Sort picker ───────────────────────────────────────────────────────────────
export function SortPicker({ options, value, onChange }) {
  return (
    <div style={OPTION_ROW}>
      {options.map(opt => (
        <OptionCard key={opt.value} active={value === opt.value} onClick={() => onChange(opt.value)}
          name={opt.label} sub={opt.sub} />
      ))}
    </div>
  )
}

// ── Count picker ──────────────────────────────────────────────────────────────
// null means "all available". `max` is the real candidate count: at 0 the All
// card is disabled rather than offering a number that is not there.
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

  // The custom card holds an input, so it is a div in the card's clothes rather
  // than an OptionCard (an input may not sit inside a button). Its underline is
  // an inset shadow, not a border, so it adds no height to the row.
  return (
    <div style={OPTION_ROW}>
      <OptionCard active={fiveActive} onClick={handleFive} name="Quick" sub="5 candidates" />

      <div className="wf-btn" style={{ ...OPTION_CARD, ...optionColors(customActive), cursor: 'text' }}
        onClick={() => inputRef.current?.focus()}>
        <span style={OPTION_NAME}>Custom</span>
        <span style={{ ...OPTION_SUB, display: 'flex', alignItems: 'baseline', gap: 6 }}>
          <input
            ref={inputRef}
            type="number"
            min={1}
            max={max || undefined}
            value={inputVal}
            onChange={handleInput}
            placeholder="—"
            aria-label="Custom number of candidates"
            style={{
              ...OPTION_SUB, color: 'var(--text)', width: '5ch', height: 14,
              padding: 0, margin: 0, background: 'none', border: 'none', outline: 'none',
              boxShadow: 'inset 0 -1px 0 var(--border2)',
            }}
          />
          candidates
        </span>
      </div>

      <OptionCard active={allActive} onClick={handleAll} disabled={max === 0}
        name="All" sub={max > 0 ? `${max.toLocaleString()} candidates` : 'none available'} />
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
//
// md and sm are exactly index.css's two control heights (UI pass, 2026-09-22),
// so a button sits level with the inputs and segmented tracks in any bar. They
// rendered 33 and 25px before, one step short of each; the user chose to grow
// them everywhere over fencing the heights inside filter bars, which would have
// left two different "small" buttons in the app.
//
// `square` makes an icon-only button as wide as it is tall (the ✕ that clears a
// search, the changes panel's collapse); give it an `ariaLabel`. `pressed` marks
// a toggle (File Explorer's Trackers panel) for assistive tech — the caller
// picks the variant that shows it.
const BUTTON_SIZE = {
  md:   { fontSize: 'var(--font-base)', fontWeight: 600, padding: '8px 16px', borderRadius: 'var(--r)', minHeight: 'var(--control-h-lg)' },
  sm:   { fontSize: 'var(--font-base)', fontWeight: 500, padding: '4px 10px', borderRadius: 'var(--r)', minHeight: 'var(--control-h)' },
  chip: { fontSize: 'var(--font-sm)', fontWeight: 500, padding: '1px 7px', borderRadius: 'var(--r-sm)', fontFamily: 'var(--mono)' },
}
const SQUARE = { md: 'var(--control-h-lg)', sm: 'var(--control-h)' }

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
  onClick, disabled, title, ariaLabel, pressed, square = false, style, children,
}) {
  const look = tone ? toneLook(tone) : (BUTTON_LOOK[variant] || BUTTON_LOOK.secondary)
  const css = {
    fontFamily: 'var(--sans)', lineHeight: 1.25, ...(BUTTON_SIZE[size] || BUTTON_SIZE.md),
    ...(square && SQUARE[size] ? { padding: 0, width: SQUARE[size] } : null),
    '--btn-bg': look.bg, '--btn-border': look.border, '--btn-fg': look.fg,
    '--btn-bg-hover': look.bgHover || look.bg, '--btn-fg-hover': look.fgHover || look.fg,
    '--btn-filter-hover': look.filter || 'none',
    ...style,
  }
  if (href) {
    return <a className="wf-btn" href={href} target={target} rel={rel} onClick={onClick} title={title} aria-label={ariaLabel} style={css}>{children}</a>
  }
  return (
    <button className="wf-btn" type="button" onClick={onClick} disabled={disabled} title={title}
      aria-label={ariaLabel} aria-pressed={pressed} style={css}>
      {children}
    </button>
  )
}
