import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { api } from '../api'
import { LoadingRow, WorkflowError, useAuditComplete } from './workflows/shared'

// Rounds — "this is your prioritized (and rewarded) workflows page."
//
// The page is called Rounds, not Next steps. "Next steps" is wizard language:
// it promises a finite list you work through and are then done with, which is
// the exact thing this page refuses to be (see the `nature` strings — "Keeps
// coming back", "Never really finishes"). Rounds get walked in a fixed order,
// forever, and the word casts the user as the person in charge of the library
// rather than someone being handed homework — which is the same posture the
// ladder names already take (Custodian, Sentinel, Watcher, Nightwatch).
// "Chores" was the other finalist and was cut for building the do-chores-get-
// useless-prizes joke out loud; the shelf lands harder against a flat frame,
// for the same reason its own subhead was cut.
//
// This file and its component are named for the page. Everything that is a
// compatibility surface is not: the tab id (`next-steps`), the route, the
// endpoint (`/api/next_steps`), `api.nextSteps`, the backend module
// (`next_steps.py`) and the config key all keep the old name, because
// bookmarks and stored config match on those. Renaming any of them breaks a
// user's saved link; renaming this file breaks nothing, which is exactly why
// it is the one that moved.
//
// Layout, top to bottom:
//   1. Baseline        — Cleanup + Dedupe. Collapses to one line once clear.
//   2. Ongoing         — Triage, then Backfill.
//   3. On demand       — Trumped.
//   4. Useless prizes  — the trophy shelf, at the foot of the page.
//
// The shelf used to lead, on the theory that opening with a wall of chores was
// unwelcoming. In use it just buried the thing the page is named after: the
// prizes are tall (five themed shelves, thirty medallions) and pushed every
// actual next step below the fold. A single column with the shelf last reads
// better than the obvious alternative of floating it into a second column —
// the medallion grid wants the full width, and a narrow rail would squeeze it
// to two tiles across. The workflow cards carry `next_prize` anyway, so the
// reward layer still shows up next to the work; the shelf at the bottom is
// where you go to browse it, which is a different intent from doing a chore.
//
// Sections are fixed and labelled, so the sequence is self-evident and nothing
// reshuffles between visits. Each workflow card also shows the prize it feeds
// ("do this, get that") — an abstract shelf does not motivate on its own.
//
// Cheese lives in the language and in restrained motion, never in pigment: no
// gradients, no gold, no confetti. Rings use a plain var() paint plus
// strokeOpacity; locked tiles are dashed and --text-faint. Do NOT reach for the
// `var(--x)55` alpha-suffix idiom here — it does not survive substitution inside
// a property value, which is what made these fills invisible the first time.
// See prompts/NEXT_STEPS.md.
//
// Everything here is contained to this page. Nothing about Rounds appears
// on the dashboard, in toasts, or as a sidebar badge.

const STATE_META = {
  fix:      { label: 'Needs you',     color: 'var(--red)' },
  blocked:  { label: 'Blocked',       color: 'var(--text-faint)' },
  optimize: { label: 'Could improve', color: 'var(--yellow)' },
  maintain: { label: 'Clear',         color: 'var(--green)' },
  standby:  { label: 'On standby',    color: 'var(--text-faint)' },
}

// Lucide-style line icons, 24×24, stroke currentColor. One per ladder.
const I = {
  hoard:        <><line x1="22" y1="12" x2="2" y2="12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" y1="16" x2="6.01" y2="16"/><line x1="10" y1="16" x2="10.01" y2="16"/></>,
  seedbearer:   <><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></>,
  benefactor:   <><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/></>,
  auditor:      <><rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="m9 14 2 2 4-4"/></>,
  custodian:    <><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></>,
  lapidary:     <><path d="M6 3h12l4 6-10 13L2 9Z"/><path d="M11 3 8 9l4 13 4-13-3-6"/><path d="M2 9h20"/></>,
  shoveler:     <><path d="M2 22v-5l5-5 5 5-5 5z"/><path d="M9.5 14.5 16 8"/><path d="m17 2 5 5-4 4-5-5z"/></>,
  matchmaker:   <><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></>,
  exterminator: <><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><path d="M8 20v2h8v-2"/><path d="M20 12a8 8 0 1 0-16 0c0 2.5 1 4 2 5v3h12v-3c1-1 2-2.5 2-5Z"/></>,
  clonehunter:  <><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></>,
  kingmaker:    <><path d="m2 6 4 4 6-7 6 7 4-4-2 12H4z"/><path d="M4 21h16"/></>,
  sentinel:     <><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></>,
  // One square, and an X where its copy would be.
  singleton:    <><rect x="2" y="3" width="12" height="12" rx="2"/><path d="m15 15 6 6"/><path d="m21 15-6 6"/></>,
  alchemist:    <><path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/></>,
  diplomat:     <><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></>,
  steady:       <><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></>,
  chronicler:   <><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></>,
  archivist:    <><rect x="2" y="4" width="20" height="5" rx="1"/><path d="M4 9v10a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V9"/><path d="M10 13h4"/></>,
  trophy:       <><path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"/><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/></>,
  packrat:      <><path d="M20 12v9H4v-9"/><rect x="2" y="7" width="20" height="5" rx="1"/><path d="M12 21V7"/><path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7Z"/><path d="M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7Z"/></>,
  usurer:       <><circle cx="12" cy="12" r="10"/><path d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/><path d="M12 6v2m0 8v2"/></>,
  seedling:     <><path d="M7 20h10"/><path d="M10 20c5.5-2.5.8-6.4 3-10"/><path d="M9.5 9.4c1.1.8 1.8 2.2 2.3 3.7-2 .4-3.5.4-4.8-.3-1.2-.6-2.3-1.9-3-4.2 2.8-.5 4.4 0 5.5.8z"/><path d="M14.1 6a7 7 0 0 0-1.1 4c1.9-.1 3.3-.6 4.3-1.4 1-1 1.6-2.3 1.7-4.6-2.7.1-4 1-4.9 2z"/></>,
  vaultkeeper:  <><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/><circle cx="12" cy="16" r="1"/></>,
  pollinator:   <><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></>,
  marathoner:   <><line x1="10" y1="2" x2="14" y2="2"/><line x1="12" y1="14" x2="15" y2="11"/><circle cx="12" cy="14" r="8"/></>,
  watcher:      <><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/><path d="m9 16 2 2 4-4"/></>,
  nightwatch:   <><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></>,
  clockwork:    <><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2"/><path d="M5 3 2 6"/><path d="m22 6-3-3"/></>,
  handson:      <><path d="M18 11V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2"/><path d="M14 10V4a2 2 0 0 0-2-2a2 2 0 0 0-2 2v2"/><path d="M10 10.5V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"/></>,
  highwater:    <><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></>,
  tidiness:     <><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></>,
  purity:       <><path d="M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5s-3.5-4-4-6.5c-.5 2.5-2 4.9-4 6.5C6 11.1 5 13 5 15a7 7 0 0 0 7 7Z"/></>,
  conservator:  <><line x1="3" y1="22" x2="21" y2="22"/><line x1="6" y1="18" x2="6" y2="11"/><line x1="10" y1="18" x2="10" y2="11"/><line x1="14" y1="18" x2="14" y2="11"/><line x1="18" y1="18" x2="18" y2="11"/><polygon points="12 2 20 7 4 7"/></>,
  librarian:    <><path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/></>,
  videophile:   <><rect x="2" y="7" width="20" height="15" rx="2"/><polyline points="17 2 12 7 7 2"/></>,
  provenance:   <><path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.17a2 2 0 0 0-.59-1.41L12 12l-4.41 4.42A2 2 0 0 0 7 17.83V22"/><path d="M7 2v4.17a2 2 0 0 0 .59 1.41L12 12l4.41-4.42A2 2 0 0 0 17 6.17V2"/></>,
  atlas:        <><circle cx="12" cy="5" r="3"/><path d="M6.5 8h11l1.7 12.1a1 1 0 0 1-1 1.1H5.8a1 1 0 0 1-1-1.1z"/></>,
  oldfaithful:  <><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></>,
  unblemished:  <><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></>,
  flawless:     <><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/><path d="M5 3v4"/><path d="M3 5h4"/></>,
  firebrigade:  <><path d="M15 6.5V3a1 1 0 0 0-1-1h-2a1 1 0 0 0-1 1v3.5"/><path d="M9 18h8"/><path d="M18 3h-3"/><path d="M11 3a6 6 0 0 0-6 6v11a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V9a6 6 0 0 0-6-6"/></>,
  completionist:<><circle cx="12" cy="8" r="6"/><path d="M15.477 12.89 17 22l-5-3-5 3 1.523-9.11"/></>,
  // Not a ladder — the achievement record's own header. Deliberately not the
  // clock-with-arrow: Old Faithful already wears that, and the two would sit in
  // the same card the moment an Old Faithful rung landed.
  record:       <><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></>,
  trophyhunter: <><path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"/><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/></>,
}

function Icon({ name, size = 20, style }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={style}>
      {I[name] || I.trophy}
    </svg>
  )
}

function Dot({ color, size = 7 }) {
  return <span className="ui-status-dot" style={{ width: size, height: size, background: color, flexShrink: 0 }} />
}

function Track({ pct, color = 'var(--accent)', height = 4 }) {
  return (
    <div style={{ height, background: 'var(--surface3)', borderRadius: 'var(--r-pill)', overflow: 'hidden' }}>
      {/* Plain var() fill, like the sidebar scan bar. The `var(--accent)55`
          alpha-suffix idiom does not survive substitution inside a single
          property value, so the fill silently resolved to nothing. */}
      <div style={{
        width: `${Math.max(0, Math.min(100, pct))}%`, height: '100%',
        background: color, opacity: 0.85, borderRadius: 'var(--r-pill)',
        transition: 'width 0.6s cubic-bezier(.4,0,.2,1)',
      }} />
    </div>
  )
}

// Count-up readout, matching the HealthDial's 0.9s cubic ease-out.
function useCountUp(target, ms = 900) {
  const [val, setVal] = useState(0)
  const fromRef = useRef(0)
  useEffect(() => {
    const from = fromRef.current
    const delta = (target || 0) - from
    if (!delta) { setVal(target || 0); return }
    let raf, start
    const step = t => {
      if (!start) start = t
      const p = Math.min(1, (t - start) / ms)
      setVal(Math.round(from + delta * (1 - Math.pow(1 - p, 3))))
      if (p < 1) raf = requestAnimationFrame(step)
      else fromRef.current = target || 0
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target, ms])
  return val
}

// One header shape for both halves of the prize section — themed ladder groups
// and feat groups. Same component, so the two read as one system rather than
// two lists that happen to sit near each other.
function GroupHeader({ label, count, total, blurb }) {
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
      <span style={{ fontSize: 'var(--font-base)', fontWeight: 700, color: 'var(--text)' }}>{label}</span>
      <span style={{
        fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)',
        color: count === total ? 'var(--green)' : 'var(--text-dim)',
      }}>{count}/{total}</span>
      {blurb && <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-faint)' }}>{blurb}</span>}
      <span style={{ flex: 1, height: 1, background: 'var(--border)', minWidth: 12 }} />
    </div>
  )
}

// ── Medallion — icon in a progress ring showing distance to the next tier ────
//
// The x/y in the corner is load-bearing: a mock-heroic band name ("Pack Rat")
// tells you nothing about how far up the ladder you are, and half the appeal of
// this layer is seeing that there are sixteen more rungs above you. Clicking
// opens the full rung list, which is the only place the names get their actual
// thresholds attached to them.
function Medallion({ l, selected, onSelect }) {
  const earned = l.tier > 0
  const R = 22, C = 2 * Math.PI * R
  const pct = l.maxed ? 100 : l.pct
  const ring = l.maxed ? 'var(--green)' : 'var(--accent)'
  return (
    <button
      onClick={() => onSelect(selected ? null : l.id)}
      title={`${l.name} — ${l.blurb}\n${l.value_label} ${l.measures || ''}${l.maxed ? ' · maxed' : ` · next: ${l.next_label} at ${l.next_at_label}`}\nClick for every rung.`}
      style={{
        position: 'relative', textAlign: 'inherit', font: 'inherit', cursor: 'pointer',
        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6,
        padding: '12px 6px 10px', borderRadius: 'var(--rl)',
        background: selected ? 'var(--surface2)' : earned ? 'var(--surface)' : 'transparent',
        border: selected ? '1px solid var(--accent)'
          : earned ? '1px solid var(--border)' : '1px dashed var(--border2)',
        boxShadow: earned ? 'var(--elev-1)' : 'none',
      }}
    >
      {/* Where you are out of how many there are. Mono, faint, out of the way. */}
      <span style={{
        position: 'absolute', top: 6, right: 8,
        fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', lineHeight: 1,
        color: l.maxed ? 'var(--green)' : 'var(--text-faint)',
      }}>
        {l.tier}/{l.tiers_total}
      </span>
      <div style={{ position: 'relative', width: 52, height: 52, flexShrink: 0 }}>
        <svg width="52" height="52" viewBox="0 0 52 52" style={{ position: 'absolute', inset: 0, transform: 'rotate(-90deg)' }}>
          <circle cx="26" cy="26" r={R} fill="none" stroke="var(--surface3)" strokeWidth="3" />
          {/* strokeOpacity, not an alpha-suffixed colour — SVG paint attributes
              take a plain var(), the same way the HealthDial draws its track. */}
          <circle cx="26" cy="26" r={R} fill="none" stroke={ring} strokeOpacity={0.9} strokeWidth="3"
            strokeLinecap="round" strokeDasharray={C}
            strokeDashoffset={C - (C * pct) / 100}
            style={{ transition: 'stroke-dashoffset 0.7s cubic-bezier(.4,0,.2,1)' }} />
        </svg>
        <div style={{
          position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: earned ? 'var(--text)' : 'var(--text-faint)',
        }}>
          <Icon name={l.id} size={20} />
        </div>
      </div>
      {/* The band title is the joke, so it leads; the ladder name stays below
          in mono so you can still tell which ladder you are looking at. */}
      <div style={{ textAlign: 'center', minWidth: 0, width: '100%' }}>
        <div title={l.tier_label || `${l.name} — not started`} style={{
          fontSize: 'var(--font-base)', fontWeight: 600, lineHeight: 1.25,
          color: earned ? 'var(--text)' : 'var(--text-faint)',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {l.tier_label || '—'}
        </div>
        <div style={{
          fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)', marginTop: 3,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {l.name}
        </div>
        {/* The value with the thing it measures. Without the second word,
            eight byte ladders all read "12.4 TB" and are indistinguishable.
            "maxed" is no longer needed here — the ring is green and the corner
            reads n/n. */}
        <div style={{
          fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', marginTop: 2,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {l.value_label}
          {l.measures && <span style={{ color: 'var(--text-faint)' }}> {l.measures}</span>}
        </div>
      </div>
    </button>
  )
}

// ── Ladder detail — every rung, named, with the number it actually wants ─────
//
// The names are the fun part but they are opaque on their own: "Geological
// Layer" is only funny once you can see it means 25 TB of torrent directory.
// This is where a title gets its threshold, its points, and its position.
function LadderDetail({ l, onClose }) {
  return (
    <div style={{
      background: 'var(--surface2)', border: '1px solid var(--border2)',
      borderRadius: 'var(--rl)', padding: '14px 16px',
      display: 'flex', flexDirection: 'column', gap: 12,
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 11 }}>
        <span style={{ color: 'var(--accent)', display: 'flex', marginTop: 1 }}>
          <Icon name={l.id} size={18} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 'var(--font-md)', fontWeight: 700, color: 'var(--text)' }}>{l.name}</span>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
              {l.tier} / {l.tiers_total} rungs
            </span>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)' }}>
              now {l.value_label}{l.measures ? ` ${l.measures}` : ''}
            </span>
          </div>
          <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: '5px 0 0', lineHeight: 1.55, maxWidth: 620 }}>
            {l.blurb}
          </p>
        </div>
        <button onClick={onClose} title="Close"
          style={{
            background: 'none', border: 'none', padding: 4, cursor: 'pointer',
            color: 'var(--text-faint)', display: 'flex', flexShrink: 0,
          }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2.5" strokeLinecap="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(216px, 1fr))', gap: 4 }}>
        {l.tiers.map(t => {
          const isNext = !t.earned && t.n === l.next_n
          return (
            <div key={t.n} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '5px 8px',
              borderRadius: 'var(--r-sm)',
              background: isNext ? 'var(--surface)' : 'transparent',
              border: `1px solid ${isNext ? 'var(--accent)' : 'transparent'}`,
            }}>
              <span style={{
                fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)',
                minWidth: 16, textAlign: 'right', flexShrink: 0,
              }}>{t.n}</span>
              {t.earned
                ? <Dot color="var(--green)" size={5} />
                : <span style={{
                    width: 5, height: 5, borderRadius: '50%', flexShrink: 0,
                    border: '1px solid var(--border2)',
                  }} />}
              <span style={{
                fontSize: 'var(--font-sm)', flex: 1, minWidth: 0,
                color: t.earned ? 'var(--text)' : isNext ? 'var(--accent)' : 'var(--text-faint)',
                fontWeight: isNext ? 600 : 400,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>{t.label}</span>
              <span style={{
                fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', flexShrink: 0,
                color: t.earned ? 'var(--text-dim)' : 'var(--text-faint)',
              }}>{t.at_label}</span>
            </div>
          )
        })}
      </div>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)' }}>
        {l.maxed
          ? 'Maxed. There is nothing above this one.'
          : `Next rung ${l.next_n} of ${l.tiers_total} — ${l.next_label} at ${l.next_at_label}.`}
      </span>
    </div>
  )
}

// ── Rank readout — page header, right of the title ───────────────────────────
//
// Lives up top rather than on the prize card. Moving the shelf to the foot of
// the page put the one line that recognises a long-running install ("Keeper of
// Inodes · 46,440 pts") below everything, and the title block left the whole
// right-hand side of the header empty. Sitting here it fills that space and
// leaves the shelf's own header as a single left-aligned row.
function RankReadout({ rank }) {
  const points = useCountUp(rank.points)
  return (
    <div style={{ textAlign: 'right', minWidth: 190, flexShrink: 0 }}>
      <div style={{ fontSize: 'var(--font-base)', fontWeight: 600, color: 'var(--text)' }}>{rank.name}</div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-xl)', fontWeight: 700, color: 'var(--text)', lineHeight: 1.15 }}>
        {points.toLocaleString()}<span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', fontWeight: 400 }}> pts</span>
      </div>
      <div style={{ marginTop: 6 }}><Track pct={rank.pct} /></div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)', marginTop: 5 }}>
        {rank.next_name
          ? `${rank.next_name} at ${rank.next_at.toLocaleString()}`
          : 'nothing left to become'}
        {rank.total_ranks ? ` · rank ${rank.index + 1} of ${rank.total_ranks}` : ''}
      </div>
    </div>
  )
}

// ── Useless prizes — the shelf, at the foot of the page ──────────────────────
function Prizes({ data }) {
  const [showAll, setShowAll] = useState(false)
  const [openLadder, setOpenLadder] = useState(null)
  const { ladders, ladder_groups, feats, feat_groups, prizes } = data

  // Three nearest unlocks — the "you are almost there" nudge.
  const closest = ladders.filter(l => !l.maxed).sort((a, b) => b.pct - a.pct).slice(0, 3)
  const selected = ladders.find(l => l.id === openLadder) || null

  // Both halves ship grouped by the server; each falls back to one unnamed
  // bucket so an older payload still renders rather than dropping the section.
  const shelves = (ladder_groups && ladder_groups.length)
    ? ladder_groups
    : [{ id: null, label: 'Prizes', blurb: '', earned: prizes.earned, total: prizes.total }]
  const groups = (feat_groups && feat_groups.length)
    ? feat_groups
    : [{ id: null, label: 'Feats', blurb: '', earned: feats.filter(f => f.earned).length, total: feats.length }]

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)',
      boxShadow: 'var(--elev-1)', padding: '18px 20px',
      display: 'flex', flexDirection: 'column', gap: 16,
    }}>
      {/* One row, left-aligned. The rank block that used to sit opposite this
          now lives in the page header — the split left a lot of air across the
          middle and read as two columns that were not really a pair. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ color: 'var(--accent)', display: 'flex' }}><Icon name="trophy" size={17} /></span>
        {/* Same rung as "Baseline" / "Ongoing" / "On demand". It is a peer of
            those sections, not a bigger thing than them — and it sits last
            precisely so it does not outrank the work. */}
        <span style={{ fontSize: 'var(--font-md)', fontWeight: 700, color: 'var(--text)' }}>Useless prizes</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
          {prizes.earned} / {prizes.total}
        </span>
      </div>

      {/* The shelf, themed. Thirty undifferentiated medallions is a wall; four
          named shelves plus a meta one say what each region is about before you
          read a single tile. The detail panel opens under the shelf you clicked
          in, not under the whole grid, so it stays near the medallion. */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {shelves.map(g => {
          const items = ladders.filter(l => (l.group || null) === g.id)
          if (!items.length) return null
          const showDetail = selected && (selected.group || null) === g.id
          return (
            <div key={g.id || 'all'} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <GroupHeader label={g.label} count={g.earned} total={g.total} blurb={g.blurb} />
              {/* 120, not 108: the rung labels moved up to the 12px rung of the
                  scale, and the tile is where the name has to survive. */}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))', gap: 8 }}>
                {items.map(l => (
                  <Medallion key={l.id} l={l} selected={l.id === openLadder} onSelect={setOpenLadder} />
                ))}
              </div>
              {showDetail && <LadderDetail l={selected} onClose={() => setOpenLadder(null)} />}
            </div>
          )
        })}
      </div>

      {/* Almost there. Naming the ladder and the rung number is the whole point:
          "Pack Rat" alone is a punchline with no setup — "Hoarder, rung 9 of 21"
          says what you are climbing and how far up it sits. */}
      {closest.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{ fontSize: 'var(--font-base)', fontWeight: 600, color: 'var(--text-dim)' }}>Closest to unlocking</span>
          {closest.map(l => (
            <button
              key={l.id}
              onClick={() => setOpenLadder(openLadder === l.id ? null : l.id)}
              style={{
                display: 'flex', alignItems: 'center', gap: 10, width: '100%',
                background: 'none', border: 'none', padding: '3px 0', cursor: 'pointer',
                font: 'inherit', textAlign: 'left',
              }}
            >
              <span style={{ color: 'var(--text-faint)', display: 'flex', flexShrink: 0 }}>
                <Icon name={l.id} size={14} />
              </span>
              <span style={{ minWidth: 180, display: 'flex', flexDirection: 'column', gap: 1 }}>
                <span style={{ fontSize: 'var(--font-base)', color: 'var(--text)', fontWeight: 600 }}>{l.next_label}</span>
                <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)' }}>
                  {l.name} · rung {l.next_n} of {l.tiers_total}
                </span>
              </span>
              <span style={{ flex: 1, minWidth: 60 }}><Track pct={l.pct} height={3} /></span>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)', minWidth: 110, textAlign: 'right' }}>
                {l.value_label} / {l.next_at_label}
              </span>
            </button>
          ))}
        </div>
      )}

      <button
        onClick={() => setShowAll(s => !s)}
        style={{
          alignSelf: 'flex-start', background: 'none', border: 'none', padding: 0, cursor: 'pointer',
          fontSize: 'var(--font-base)', color: 'var(--text-dim)', display: 'flex', alignItems: 'center', gap: 6,
        }}
      >
        {showAll ? 'Hide feats' : `Show all ${feats.length} feats`}
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
          strokeLinecap="round" strokeLinejoin="round"
          style={{ transform: showAll ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', opacity: 0.5 }}>
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </button>

      {/* Grouped, in ascending difficulty within each group. Eighty unsorted
          one-offs read as a wall you cannot get a foothold in; named sections
          with their own x/y show at a glance which axis you have neglected. */}
      {showAll && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {groups.map(g => {
            const items = feats.filter(f => (f.group || null) === g.id)
            if (!items.length) return null
            return (
              <div key={g.id || 'all'} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <GroupHeader label={g.label} count={g.earned} total={g.total} blurb={g.blurb} />
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 8 }}>
                  {items.map(f => (
                    <div key={f.id} style={{
                      background: f.earned ? 'var(--surface2)' : 'transparent',
                      border: f.earned ? '1px solid var(--border)' : '1px dashed var(--border2)',
                      borderRadius: 'var(--r)', padding: '9px 12px',
                      display: 'flex', flexDirection: 'column', gap: 3,
                    }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                        {f.earned && <Dot color="var(--green)" size={6} />}
                        <span style={{ fontSize: 'var(--font-base)', fontWeight: 600, color: f.earned ? 'var(--text)' : 'var(--text-faint)' }}>{f.label}</span>
                        <span style={{ flex: 1 }} />
                        <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)' }}>+{f.points}</span>
                      </div>
                      <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-faint)', lineHeight: 1.45 }}>{f.desc}</span>
                    </div>
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── The record — the dated achievement log, under the shelf ──────────────────
//
// Headed "The record" and not "History": Config already has an *Audit History*
// section and the Changes page is the audit history, so a third history on the
// page that is partly *built* from the audit log (`history_from_runs`) would be
// a coin flip for what a reader expects to find inside. "Record" is the word
// every other part of this feature already uses — down to the header icon.
//
// The shelf answers "what do I have". It cannot answer "what did I actually do,
// and when" — a medallion reading *Sisyphus* says nothing about the evening it
// was earned, and a wall of thirty of them says nothing about the order any of
// it happened in. This is the same data on its other axis.
//
// Entries arrive unresolved (`{at, kind, id, n}`) and are named here from the
// ladders and feats already in the payload, so the history never carries a
// second frozen copy of every label and a renamed rung reads correctly.
//
// Naming is not enough on its own: a row reading "Geological Layer · Hoarder ·
// rung 12 of 21" says when and where, and never *for what*. Each row therefore
// also carries the terms it was earned on — the rung's threshold followed by
// the ladder's `terms` phrase ("25.0 TB in the media library", "30 days without
// an orphan", "500 audits by the watchdog"), and for a feat, which has no
// threshold, its condition sentence. Deliberately not the tile's `measures`:
// that word is written to sit under a medallion with its ladder name and blurb
// around it, and "3 days unbroken" alone is unbroken at what.
//
// Preview then expand, not a bare toggle: a collapsed section that shows
// nothing has to be opened before it can justify itself, and the most recent
// few are the part anyone actually came for. Same shape either way — the date
// gutter only prints on the first row of its day, so one repeated date does not
// become a column of noise on a day when twenty rungs landed at once.
const HISTORY_PREVIEW = 6

// A rung threshold is frozen at its number, and every ladder's rung 1 fires on
// a new install's first audit — so without this the record opens on a screenful
// of "1 audits completed", "1 torrents seeding", "1 swaps complied with". Only
// ever touches the first word of a bare "1", which is why `terms` is written
// noun-first (see LADDER_FACET), and leaves -ss alone so "1 spotless" survives.
function singularise(value, phrase) {
  if (value !== '1' || !phrase) return phrase
  const [head, ...rest] = phrase.split(' ')
  const one = /(x|s|z|ch|sh)es$/.test(head) ? head.slice(0, -2)
    : /ss$/.test(head) ? head
      : head.replace(/s$/, '')
  return [one, ...rest].join(' ')
}

function fmtDay(iso) {
  const d = new Date(iso)
  if (isNaN(d)) return String(iso || '').slice(0, 10)
  return d.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric' })
}

function Timeline({ data }) {
  const [showAll, setShowAll] = useState(false)
  const { history, ladders, feats, rank } = data

  // Resolve once, not per render row. `prior` entries carry their own counts and
  // resolve to nothing — they are the "earned before auditorr kept dates" marker
  // an upgraded install opens with.
  const byLadder = useMemo(() => Object.fromEntries((ladders || []).map(l => [l.id, l])), [ladders])
  const byFeat   = useMemo(() => Object.fromEntries((feats   || []).map(f => [f.id, f])), [feats])

  const named = useMemo(() => (history || []).map((e, i) => {
    if (e.kind === 'prior') {
      const bits = []
      if (e.rungs) bits.push(`${e.rungs} rung${e.rungs === 1 ? '' : 's'}`)
      if (e.feats) bits.push(`${e.feats} feat${e.feats === 1 ? '' : 's'}`)
      return {
        key: `p${i}`, at: e.at, icon: 'trophy', prior: true,
        label: `${bits.join(' and ')} earned before this`,
        meta: 'auditorr only started dating these at this upgrade',
        detail: '', points: null,
      }
    }
    if (e.kind === 'feat') {
      const f = byFeat[e.id]
      return {
        key: `f${i}-${e.id}`, at: e.at, icon: 'trophy',
        label: f ? f.label : e.id,
        // The feat's own condition, because the name never carries it: "It Was
        // Always Here" and "Overqualified" say nothing about what was actually
        // done. Sans, exactly as the same sentence is set on the shelf — it is
        // prose, not a readout, and typeface follows kind.
        meta: f && f.desc ? `feat · ${f.desc}` : 'feat',
        detail: '', points: f ? f.points : null,
      }
    }
    const l = byLadder[e.id]
    const t = l && (l.tiers || [])[e.n - 1]
    return {
      key: `r${i}-${e.id}-${e.n}`, at: e.at, icon: e.id,
      label: t ? t.label : e.id,
      meta: l ? `${l.name} · rung ${e.n} of ${l.tiers_total}` : `rung ${e.n}`,
      metaMono: true,
      // What the rung actually took. The name is a punchline and the rung
      // number only says where it sits — neither says 25 TB, so a record built
      // from the two answered "when" and never "for what". `terms` rather than
      // the tile's `measures`: the tile has its medallion, its ladder name and
      // a blurb around it, so "unbroken" lands there and is unbroken at *what*
      // here. Falls back to `measures` so a payload from before the field
      // still says something.
      detail: t ? [t.at_label, singularise(t.at_label, l.terms || l.measures)]
        .filter(Boolean).join(' ') : '',
      points: t ? t.points : null,
    }
  }), [history, byLadder, byFeat])

  // The running total is walked **backwards from the rank readout**, not
  // forwards from zero. Forwards would disagree with the number at the top of
  // the page: that total also carries the setup steps and, on an upgraded
  // install, everything earned before the record started keeping dates —
  // neither of which is a dated entry. Anchoring at the top makes the newest
  // row equal the header exactly, and drops the residue where it belongs, on
  // the oldest rows and on the `prior` marker, which is the row whose whole job
  // is to say there was something before this.
  const entries = useMemo(() => {
    let running = (rank && rank.points) || 0
    return named.map(e => {
      const total = Math.max(0, running)
      running -= e.points || 0
      return { ...e, total }
    })
  }, [named, rank])

  if (!entries.length) return null

  const shown = showAll ? entries : entries.slice(0, HISTORY_PREVIEW)

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)',
      boxShadow: 'var(--elev-1)', padding: '18px 20px',
      display: 'flex', flexDirection: 'column', gap: 14,
    }}>
      {/* Peer of "Useless prizes" and of the workflow sections — same rung of
          the scale, same shape of header row. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ color: 'var(--accent)', display: 'flex' }}><Icon name="record" size={17} /></span>
        <span style={{ fontSize: 'var(--font-md)', fontWeight: 700, color: 'var(--text)' }}>The record</span>
        {/* The points figure is what makes the right-hand column legible: it is
            the same number the rank readout shows at the top of the page, and
            the newest row's running total equals it, so the column explains
            itself without a table header. */}
        <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
          {entries.length} entries · {((rank && rank.points) || 0).toLocaleString()} pts
        </span>
      </div>

      {/* Scrolls in place once expanded rather than growing the page by twenty
          thousand pixels. Not paginated and not virtualized: the list is
          bounded at every rung of every ladder plus every feat — under 800,
          ever — so there is nothing here for either to solve. */}
      <div style={{
        display: 'flex', flexDirection: 'column',
        ...(showAll ? { maxHeight: '60vh', overflowY: 'auto' } : null),
      }}>
        {shown.map((e, i) => {
          const day = fmtDay(e.at)
          // Only the first row of a day prints its date. Twenty rungs cross on
          // the first audit of a new install, and twenty identical dates in a
          // column is a wall, not a timeline.
          const newDay = i === 0 || fmtDay(shown[i - 1].at) !== day
          return (
            <div key={e.key} style={{ display: 'flex', alignItems: 'stretch', gap: 10 }}>
              <span style={{
                fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)',
                minWidth: 82, flexShrink: 0, paddingTop: 7, textAlign: 'right',
              }}>
                {newDay ? day : ''}
              </span>
              {/* The spine. A rule rather than a dot per row: the icons are
                  already the per-row marker, and a second marker beside them
                  reads as decoration. */}
              <span style={{
                width: 1, background: 'var(--border)', flexShrink: 0,
                marginTop: newDay ? 6 : 0,
                marginBottom: i === shown.length - 1 ? 8 : 0,
              }} />
              <div style={{
                display: 'flex', alignItems: 'center', gap: 9,
                flex: 1, minWidth: 0, padding: '5px 0 5px 2px',
              }}>
                <span style={{ color: e.prior ? 'var(--text-faint)' : 'var(--text-dim)', display: 'flex', flexShrink: 0 }}>
                  <Icon name={e.icon} size={14} />
                </span>
                {/* flexShrink 0: the rung's own name is the row, and it is
                    short. When space runs out it is the ladder position beside
                    it that gives way, not this. */}
                <span style={{
                  fontSize: 'var(--font-base)', fontWeight: e.prior ? 400 : 600,
                  color: e.prior ? 'var(--text-dim)' : 'var(--text)',
                  flexShrink: e.prior ? 1 : 0,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{e.label}</span>
                <span style={{
                  ...(e.metaMono ? { fontFamily: 'var(--mono)' } : null),
                  fontSize: 'var(--font-sm)', color: 'var(--text-faint)',
                  flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{e.meta}</span>
                {/* What it took, in its own column rather than tacked onto the
                    meta above: the meta is the flexible one and ellipsizes
                    first, which would drop precisely the figure this column
                    exists to show. Dropped entirely on feats rather than left
                    empty — they have no threshold, that being what makes them
                    feats, and the 88px would come straight out of the sentence
                    beside it, which is the longest thing in the card. The two
                    number columns after it are fixed-width, so the right-hand
                    edge stays put either way. */}
                {e.detail && (
                  <span style={{
                    fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)',
                    color: 'var(--text-dim)', flexShrink: 0,
                    minWidth: 88, textAlign: 'right', whiteSpace: 'nowrap',
                  }}>{e.detail}</span>
                )}
                {/* Two figures, and the order is the point: what this one was
                    worth, then what you were on once you had it. The award is
                    the fainter of the two — the running total is the column you
                    read down, and it is the one that ties back to the rank at
                    the top of the page. Fixed widths so both columns line up
                    whatever the digit count. */}
                <span style={{
                  fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)',
                  color: 'var(--text-faint)', flexShrink: 0,
                  minWidth: 46, textAlign: 'right',
                }}>{e.points != null ? `+${e.points}` : ''}</span>
                <span style={{
                  fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)',
                  color: 'var(--text-dim)', flexShrink: 0,
                  minWidth: 68, textAlign: 'right',
                }}>{e.total.toLocaleString()}</span>
              </div>
            </div>
          )
        })}
      </div>

      {entries.length > HISTORY_PREVIEW && (
        <button
          onClick={() => setShowAll(s => !s)}
          style={{
            alignSelf: 'flex-start', background: 'none', border: 'none', padding: 0, cursor: 'pointer',
            fontSize: 'var(--font-base)', color: 'var(--text-dim)', display: 'flex', alignItems: 'center', gap: 6,
          }}
        >
          {showAll ? 'Show recent only' : `Show all ${entries.length}`}
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
            strokeLinecap="round" strokeLinejoin="round"
            style={{ transform: showAll ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', opacity: 0.5 }}>
            <polyline points="9 18 15 12 9 6" />
          </svg>
        </button>
      )}
    </div>
  )
}

// One rule, every section. Density follows the row's *state*, never which
// section it happens to sit in — Ongoing used to force every card expanded and
// On demand keyed off the Kingmaker tally, so three identically-cleared rows
// rendered three different ways and the page read as though its own sections
// disagreed about what "clear" looks like. A row that wants something carries
// its stat line and its button; a row that is fine is a quiet line.
//
// Density is the *only* thing state changes. It does not change type size:
// every workflow card titles at --font-md whatever its state, because a bigger
// title on a busy row made Backfill loom over Triage for no reason a reader
// could name. The state pill, its colour and the presence of a stat line all
// already say which rows want something.
const isCompact = row => !['fix', 'optimize', 'blocked'].includes(row.state)

// ── Workflow card ────────────────────────────────────────────────────────────
//
// Fixed shape, top to bottom, so every card is read the same way:
//   title · state · nature      →  one prose line  →  [stats · payout · prize]
// Numbers live at the foot and nowhere else. When the readout sat directly
// under the title it competed with it, and the same figure could then appear
// again in the payout line below — which is exactly what Backfill did, printing
// its hardlink ratio twice on one card.
//
// The foot is at most three lines, and only a row that genuinely has three
// things to say uses all three. Which slots a row fills is the server's call
// (`_stat` / `_reward_line`): Backfill fills one, the sans payout line, so it
// reads as the same single line Triage and Trumped show beside it.
function WorkflowCard({ row, onNavigate, compact }) {
  const meta = STATE_META[row.state] || STATE_META.maintain
  const actionable = row.state === 'fix' || row.state === 'optimize'

  // Bottom-left, on the card's own alignment spine — the same left edge as the
  // dot, the title, the prose, the rule and the readout.
  //
  // It was briefly right-aligned at the end of the foot row, to close a gap
  // under a one-line foot. That was the wrong read of the gap: the bottom-right
  // of the card is empty either way, because the prize box is *top*-right, so
  // moving the button there filled nothing and cost two things. It stranded the
  // button ~440px from the readout it acts on, and it parked it directly under
  // the prize box, which reads as though "Open Backfill" belongs to Ratio
  // Alchemist rather than to the workflow. Left-aligned also gives a stronger
  // consistency guarantee than right-aligned did: the left edge is the same x
  // on every card whatever the copy says, where the right edge moved the moment
  // the column's width cap came off.
  const button = (actionable || row.state === 'blocked') ? (
    <button
      onClick={() => onNavigate(row.state === 'blocked' ? 'config' : row.id)}
      style={{
        alignSelf: 'flex-start', flexShrink: 0, marginTop: 10,
        padding: compact ? '7px 14px' : '9px 18px', borderRadius: 'var(--r)', cursor: 'pointer',
        border: `1px solid ${actionable ? 'var(--accent)' : 'var(--border2)'}`,
        background: actionable ? 'var(--accent)' : 'var(--surface2)',
        color: actionable ? '#0a0a0a' : 'var(--text)',
        fontSize: 'var(--font-base)', fontWeight: 600,
      }}
    >
      {row.state === 'blocked' ? 'Open Config →' : `Open ${row.label} →`}
    </button>
  ) : null

  return (
    // Two real columns: everything the card says on the left, the prize on the
    // right, top-aligned and level with the title.
    //
    // It used to be one vertical stack whose last element happened to hold two
    // things side by side, which put the prize box *below* the prose and made
    // the card as tall as prose + box stacked. It also drew a full-width rule
    // across a layout that was already two columns, so the rule cut through the
    // prize box — which carries its own border — leaving two separators doing
    // one job on that half. The rule is kept, because after Backfill's foot
    // became a sans sentence only brightness separates it from the prose above,
    // and without a divider that card reads as three paragraphs. It is just
    // scoped to the column whose content it actually divides.
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)',
      boxShadow: 'var(--elev-1)', padding: compact ? '12px 16px' : '16px 20px',
      display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
      gap: 20, flexWrap: 'wrap',
    }}>
      {/* Runs to the prize box and stops — no width of its own. It briefly
          carried the prose's old 620px cap so the rule would not overhang the
          text, but that made the column narrower than the card actually
          allows: prose wrapped early with 200px of empty card beside it, and
          copy got measured against an invented limit rather than the real one.
          The rule matching the text is now the *copy's* job, not the column's —
          see `teaching` in next_steps.py, which must fit on one line. */}
      <div style={{
        flex: '1 1 320px',
        display: 'flex', flexDirection: 'column', gap: compact ? 6 : 10,
      }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap' }}>
        <Dot color={row.accent} />
        <span style={{ fontSize: 'var(--font-md)', fontWeight: 700, color: 'var(--text)' }}>{row.label}</span>
        <span style={{
          fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', padding: '2px 8px',
          borderRadius: 'var(--r-pill)', border: `1px solid ${meta.color}`, color: meta.color,
        }}>{meta.label}</span>
        {/* Prose ("Clear once, then watch"), so sans. The pill beside it stays
            mono: a status label is closer to data than to a sentence, and it
            matches the chips on the workflow pages. */}
        <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-faint)' }}>{row.nature}</span>
      </div>

      {/* One line of prose under the title, always — what the workflow does on
          a row that wants something, what "clear" means on one that doesn't.
          620px, because 780 at this size ran ~110 characters a line, twice what
          the rest of the app sets prose at. */}
      <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', lineHeight: 1.55, margin: 0 }}>
        {row.summary}
      </p>

      {/* The foot: the numbers, then the way in — both on the card's left
          edge, in reading order. */}
      {(row.stat || row.reward) && (
        <div style={{
          borderTop: '1px solid var(--border)', paddingTop: compact ? 7 : 10,
          display: 'flex', flexDirection: 'column', gap: 3,
        }}>
          {/* The numbers. Mono and --text so it reads as a readout, but on
              the body rung of the scale — a rung up it became the loudest
              thing on the card and out-shouted the title above it. */}
          {row.stat && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-base)', fontWeight: 600, color: 'var(--text)' }}>
              {row.stat}
            </span>
          )}
          {/* Sans, like every other sentence on this page. "Clean for 25
              days · no kills yet" is prose with a number in it, not a
              readout; the mono it used to carry read as a third typeface. */}
          {row.reward && (
            <span style={{ fontSize: 'var(--font-base)', color: row.stat ? 'var(--text-dim)' : 'var(--text)' }}>
              {row.reward.headline}
            </span>
          )}
          {/* Optional, and empty on purpose for Backfill: its payout line
              already carries the ratio and the idle bytes, and a second
              sentence under it put a three-line foot next to Triage's one. */}
          {row.reward?.detail && !compact && (
            <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-faint)', lineHeight: 1.5 }}>
              {row.reward.detail}
            </span>
          )}
          {button}
        </div>
      )}

      {/* Fallback only. Every row carries a `reward`, so the foot above always
          renders and always hosts the button; this keeps an actionable row
          clickable if a payload ever arrives without one. */}
      {!(row.stat || row.reward) && button}
      </div>

      {/* The right column. Top-aligned and level with the title on every card,
          which is what the header's "N of M pts lost" readout used to occupy —
          it was removed so this slot is the same slot on all five rows.

          `width`, not `minWidth`. As a floor it only held the narrow boxes
          level and let anything wider grow to fit, so a real page rendered
          210 / 230 / 250 and the right edge jittered as you scrolled. 260 is
          the widest box actually measured (250, set by "Ratio Alchemist · rung
          6 of 14") plus one character, because that ladder's own rung count
          reaches "rung 10 of 14" and two digits would otherwise wrap. Sized off
          what renders, not off the longest tier name in the table — a box wide
          enough for "Nothing Continues To Happen" would be wide on every card
          for a label almost nobody sees. That label wraps to a second line
          instead, which costs one card some height and keeps the name intact;
          truncating it would eat the joke. */}
      {row.next_prize && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0,
          padding: '7px 12px', borderRadius: 'var(--r)',
          background: 'var(--surface2)', border: '1px solid var(--border2)',
          width: 260, maxWidth: '100%',
        }}>
          <span style={{ color: 'var(--accent)', display: 'flex', flexShrink: 0 }}>
            {/* The ladder the *next* rung belongs to — prizes[0] can be a
                maxed ladder, which would put the wrong icon here. */}
            <Icon name={row.next_prize.ladder_id} size={16} />
          </span>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3, flex: 1, minWidth: 0 }}>
            <span style={{ fontSize: 'var(--font-base)', fontWeight: 600, color: 'var(--text)' }}>
              Next: {row.next_prize.label}
            </span>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)' }}>
              {row.next_prize.ladder}
              {row.next_prize.n ? ` · rung ${row.next_prize.n} of ${row.next_prize.of}` : ''}
            </span>
            <Track pct={row.next_prize.pct} height={3} />
            <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)' }}>
              {row.next_prize.value_label} / {row.next_prize.at}
            </span>
          </div>
        </div>
      )}
    </div>
  )
}

// ── A labelled, collapsible section ──────────────────────────────────────────
function Section({ title, sub, done, doneLine, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer',
          background: 'none', border: 'none', padding: 0, textAlign: 'left', width: '100%',
        }}
      >
        {done && <Dot color="var(--green)" size={7} />}
        <span style={{ fontSize: 'var(--font-md)', fontWeight: 700, color: 'var(--text)' }}>{title}</span>
        <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-faint)' }}>{done ? doneLine : sub}</span>
        <span style={{ flex: 1, height: 1, background: 'var(--border)', minWidth: 12 }} />
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
          strokeLinecap="round" strokeLinejoin="round"
          style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', opacity: 0.45, color: 'var(--text-dim)', flexShrink: 0 }}>
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </button>
      {open && children}
    </div>
  )
}

// ── Setup checklist ──────────────────────────────────────────────────────────
function SetupPanel({ setup, onNavigate }) {
  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border2)', borderRadius: 'var(--rl)',
      boxShadow: 'var(--elev-1)', padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: 10,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
        <Dot color="var(--accent)" />
        <span style={{ fontSize: 'var(--font-md)', fontWeight: 700, color: 'var(--text)' }}>Finish setting up</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>
          {setup.done} / {setup.total}
        </span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {setup.steps.map(s => (
          <div key={s.id}
            onClick={() => !s.done && onNavigate(s.tab)}
            style={{
              display: 'flex', alignItems: 'flex-start', gap: 10, padding: '7px 6px',
              borderRadius: 'var(--r)', cursor: s.done ? 'default' : 'pointer',
            }}
            onMouseEnter={e => { if (!s.done) e.currentTarget.style.background = 'var(--surface2)' }}
            onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}
          >
            <span style={{
              width: 15, height: 15, borderRadius: 'var(--r-sm)', marginTop: 1, flexShrink: 0,
              border: `1.5px solid ${s.done ? 'var(--green)' : 'var(--border2)'}`,
              background: s.done ? 'var(--green)' : 'transparent',
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            }}>
              {s.done && (
                <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#0a0a0a" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              )}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 'var(--font-base)', color: s.done ? 'var(--text-dim)' : 'var(--text)', fontWeight: s.done ? 400 : 600 }}>{s.label}</div>
              <div style={{ fontSize: 'var(--font-sm)', color: 'var(--text-faint)', marginTop: 2, lineHeight: 1.5 }}>{s.hint}</div>
            </div>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: s.done ? 'var(--green)' : 'var(--text-faint)', flexShrink: 0 }}>
              +{s.points}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
export default function Rounds({ onNavigate }) {
  const [data, setData]   = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    api.nextSteps()
      .then(d => setData(d))
      .catch(e => setError(e.message))
  }, [])

  useEffect(() => { load() }, [load])
  // Every number here — states, counts, ladder peaks, feats — is written by an
  // audit, so that is the one event that can change this page. Without this it
  // sat on pre-scan data for as long as it was left open.
  useAuditComplete(load)

  if (error) return <div style={{ padding: 'var(--page-gutter)' }}><WorkflowError message={error} /></div>
  if (!data)  return <div style={{ padding: 'var(--page-gutter)' }}><LoadingRow label="Working out what you should be doing…" /></div>
  const { setup, rows, health } = data
  const byId = Object.fromEntries((rows || []).map(r => [r.id, r]))
  const baseline = ['cleanup', 'dedupe'].map(id => byId[id]).filter(Boolean)
  const baselineClear = baseline.length > 0 && baseline.every(r => !['fix', 'optimize'].includes(r.state))

  return (
    <div style={{ padding: 'var(--page-gutter)', display: 'flex', flexDirection: 'column', gap: 'var(--card-gap)', maxWidth: 1180 }}>

      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <div style={{ fontSize: 'var(--font-xl)', fontWeight: 700, color: 'var(--text)', lineHeight: 1.2 }}>Rounds</div>
          {/* Only the setup-stage blurb survives: with no audit on record the
              sections below are empty and the page needs to say why. Once they
              fill in, the section headers state the plan better than a paragraph
              restating it above them did. Same rung and same measure as every
              other paragraph on the page — it was a rung up and 140px wider,
              which made the one sentence a first-run user sees the odd one out. */}
          {data.stage === 'setup' && (
            <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.55, maxWidth: 620 }}>
              auditorr has nothing to audit yet. Finish the checklist and the rest of this page fills in.
            </p>
          )}
        </div>
        {data.stage !== 'setup' && <RankReadout rank={data.rank} />}
      </div>

      {!setup.complete && <SetupPanel setup={setup} onNavigate={onNavigate} />}

      {rows && rows.length > 0 && (
        <>
          <Section
            title="Baseline"
            sub="Clear these first. They should stay clear."
            done={baselineClear}
            doneLine="Clear. auditorr is watching."
            defaultOpen={!baselineClear}
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {baseline.map(r => (
                <WorkflowCard key={r.id} row={r} onNavigate={onNavigate} compact={isCompact(r)} />
              ))}
            </div>
          </Section>

          <Section title="Ongoing" sub="These never have a last item.">
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {['triage', 'backfill'].map(id => byId[id]).filter(Boolean).map(r => (
                <WorkflowCard key={r.id} row={r} onNavigate={onNavigate} compact={isCompact(r)} />
              ))}
            </div>
          </Section>

          {byId.trumped && (
            <Section
              title="On demand"
              sub="Only when a tracker asks."
              /* Collapsed until you have actually done one. Once there is a
                 Kingmaker tally to show, hiding it behind a closed section
                 means the only user who earned it never sees it. */
              defaultOpen={(byId.trumped.reward?.tally || 0) > 0}
            >
              {/* The tally still decides whether the *section* opens — a user
                  who has earned a Kingmaker streak must not have it hidden
                  behind a closed section — but the card itself follows the same
                  state rule as every other card. A compact card still renders
                  its reward line, so nothing is lost by collapsing it. */}
              <WorkflowCard row={byId.trumped} onNavigate={onNavigate}
                compact={isCompact(byId.trumped)} />
            </Section>
          )}
        </>
      )}

      {/* The shelf, last. Browsing prizes is a different intent from doing the
          work, and it was burying the work when it led the page. */}
      {data.stage !== 'setup' && <Prizes data={data} />}

      {/* And the record of it, under the shelf: the shelf is what you have, this
          is what you did. It sits after rather than before because it is the
          rarer intent of the two — you come to the shelf to see what is next
          and to this to look back. Renders nothing until there is something to
          look back on. */}
      {data.stage !== 'setup' && <Timeline data={data} />}

      {health?.score != null && (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-faint)' }}>
          Library health {health.score} · {health.status} · {data.audits} audit{data.audits === 1 ? '' : 's'} on record
          {data.streak_90 > 0 && ` · ${data.streak_90} day${data.streak_90 === 1 ? '' : 's'} at 90+`}
        </div>
      )}
    </div>
  )
}
