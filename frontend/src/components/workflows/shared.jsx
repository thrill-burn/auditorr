import React, { useState, useRef } from 'react'

// ── Shared option lists ───────────────────────────────────────────────────────
export const QUALITY_RES_OPTIONS = [
  { value: '2160p', label: '2160p / 4K' },
  { value: '1080p', label: '1080p'      },
  { value: '720p',  label: '720p'       },
]
export const QUALITY_SOURCE_OPTIONS = [
  { value: 'remux',  label: 'Remux'  },
  { value: 'bluray', label: 'Bluray' },
  { value: 'webdl',  label: 'WEB-DL' },
  { value: 'webrip', label: 'WEBRip' },
  { value: 'hdtv',   label: 'HDTV'   },
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
        padding: '3px 10px', borderRadius: 99, fontSize: 12, cursor: 'pointer',
        border: `1px solid ${active ? 'var(--accent)' : 'var(--border2)'}`,
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
              padding: '8px 14px', borderRadius: 8, cursor: 'pointer', minWidth: 110,
              border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
              background: active ? 'var(--accent)12' : 'var(--surface)',
              color: active ? 'var(--accent)' : 'var(--text)',
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
    padding: '10px 22px', borderRadius: 8, minWidth: 90,
    border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
    background: active ? 'var(--accent)12' : 'var(--surface)',
    color: active ? 'var(--accent)' : 'var(--text)',
  })

  const numStyle = (active) => ({
    fontSize: 22, fontWeight: 700, fontFamily: 'var(--mono)', lineHeight: '26px',
    height: 26, display: 'flex', alignItems: 'center',
    color: active ? 'var(--accent)' : 'inherit',
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
    <div style={{ fontFamily: 'var(--mono)', fontSize: 11, letterSpacing: 2, textTransform: 'uppercase', color: 'var(--text-dim)', marginBottom: 8 }}>
      {children}
    </div>
  )
}

// ── Workflow page header ──────────────────────────────────────────────────────
export function WorkflowHeader({ title, blurb, accent, right }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: 2.5, textTransform: 'uppercase', marginBottom: 4 }}>Workflows</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {accent && <span style={{ width: 10, height: 10, borderRadius: 3, background: accent, flexShrink: 0 }} />}
          <span style={{ fontSize: 20, fontWeight: 700, color: 'var(--text)', lineHeight: 1.2 }}>{title}</span>
        </div>
        {blurb && (
          <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.6, maxWidth: 640 }}>
            {blurb}
          </p>
        )}
      </div>
      {right && <div style={{ flexShrink: 0 }}>{right}</div>}
    </div>
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
    <div style={{ padding: '10px 14px', background: 'var(--red)10', border: '1px solid var(--red)30', borderRadius: 8, color: 'var(--red)', fontSize: 13 }}>
      {message}
    </div>
  )
}

// ── Checkbox ──────────────────────────────────────────────────────────────────
export function Checkbox({ checked, indeterminate, onChange }) {
  return (
    <span
      onClick={e => { e.stopPropagation(); onChange() }}
      style={{
        width: 15, height: 15, borderRadius: 4, flexShrink: 0, cursor: 'pointer',
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
      background: 'var(--surface)', border: '1px solid var(--border2)', borderRadius: 10,
      padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 12,
      boxShadow: '0 8px 30px rgba(0,0,0,0.35)',
    }}>
      <div style={{ flex: 1, minWidth: 0, fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-dim)' }}>{summary}</div>
      <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>{children}</div>
    </div>
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
        fontSize: 12, fontWeight: 600, padding: '8px 16px', borderRadius: 7,
        cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.45 : 1,
        border: `1px solid ${danger ? 'var(--red)40' : primary ? 'var(--accent)' : 'var(--border2)'}`,
        background: danger ? 'var(--red)12' : primary ? 'var(--accent)' : 'var(--surface2)',
        color: primary ? '#fff' : color,
      }}
    >
      {children}
    </button>
  )
}
