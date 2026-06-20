import React, { useState, useRef, useEffect } from 'react'

const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December']
const DOW    = ['Su','Mo','Tu','We','Th','Fr','Sa']

function parseYMD(str) {
  if (!str) return null
  const [y, m, d] = str.split('-').map(Number)
  return (y && m && d) ? new Date(y, m - 1, d) : null
}

function toYMD(y, m, d) {
  return `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`
}

function fmtDisplay(str) {
  const d = parseYMD(str)
  if (!d) return null
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

export default function DatePicker({ value, onChange, placeholder = 'Pick a date' }) {
  const [open, setOpen] = useState(false)
  const ref  = useRef(null)

  const today    = new Date()
  const selected = parseYMD(value)

  const [viewYear,  setViewYear]  = useState(() => selected?.getFullYear() ?? today.getFullYear())
  const [viewMonth, setViewMonth] = useState(() => selected?.getMonth()    ?? today.getMonth())

  useEffect(() => {
    if (!open) return
    const handler = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const handleOpen = () => {
    if (selected) { setViewYear(selected.getFullYear()); setViewMonth(selected.getMonth()) }
    setOpen(o => !o)
  }

  const prevMonth = () => {
    if (viewMonth === 0) { setViewMonth(11); setViewYear(y => y - 1) }
    else setViewMonth(m => m - 1)
  }
  const nextMonth = () => {
    if (viewMonth === 11) { setViewMonth(0); setViewYear(y => y + 1) }
    else setViewMonth(m => m + 1)
  }

  const firstDow    = new Date(viewYear, viewMonth, 1).getDay()
  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate()
  const cells = [...Array(firstDow).fill(null), ...Array.from({ length: daysInMonth }, (_, i) => i + 1)]
  const todayYMD = toYMD(today.getFullYear(), today.getMonth(), today.getDate())

  const handleSelect = day => {
    onChange(toYMD(viewYear, viewMonth, day))
    setOpen(false)
  }

  const btnBase = {
    background: 'none', border: 'none', cursor: 'pointer',
    fontFamily: 'var(--sans)', borderRadius: 'var(--r-sm)', transition: 'background 0.1s',
  }

  return (
    <div ref={ref} style={{ position: 'relative', display: 'inline-block' }}>
      <button
        onClick={handleOpen}
        style={{
          height: 26, padding: '0 10px', borderRadius: 'var(--r)', fontSize: 11,
          border: `1px solid ${value ? 'var(--accent)66' : 'var(--border2)'}`,
          background: value ? 'var(--surface2)' : 'transparent',
          color: value ? 'var(--text)' : 'var(--text-dim)',
          fontFamily: 'var(--sans)', cursor: 'pointer', whiteSpace: 'nowrap',
          transition: 'border-color 0.12s',
        }}
      >
        {fmtDisplay(value) ?? placeholder}
      </button>

      {open && (
        <div style={{
          position: 'absolute', top: 'calc(100% + 6px)', left: 0, zIndex: 300,
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 'var(--rl)', padding: '12px 10px',
          boxShadow: '0 8px 28px rgba(0,0,0,0.45)',
          width: 220,
        }}>
          {/* Month / year nav */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
            <button onClick={prevMonth} style={{ ...btnBase, fontSize: 16, padding: '0 8px', color: 'var(--text-dim)' }}
              onMouseEnter={e => e.currentTarget.style.background = 'var(--surface2)'}
              onMouseLeave={e => e.currentTarget.style.background = 'none'}>‹</button>
            <span style={{ fontFamily: 'var(--sans)', fontSize: 12, fontWeight: 600, color: 'var(--text)' }}>
              {MONTHS[viewMonth]} {viewYear}
            </span>
            <button onClick={nextMonth} style={{ ...btnBase, fontSize: 16, padding: '0 8px', color: 'var(--text-dim)' }}
              onMouseEnter={e => e.currentTarget.style.background = 'var(--surface2)'}
              onMouseLeave={e => e.currentTarget.style.background = 'none'}>›</button>
          </div>

          {/* Day-of-week headers */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', marginBottom: 4 }}>
            {DOW.map(d => (
              <div key={d} style={{ fontFamily: 'var(--sans)', fontSize: 11, color: 'var(--text-dim)', textAlign: 'center', padding: '2px 0' }}>{d}</div>
            ))}
          </div>

          {/* Day cells */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 2 }}>
            {cells.map((day, i) => {
              if (!day) return <div key={i} />
              const ymd        = toYMD(viewYear, viewMonth, day)
              const isSelected = ymd === value
              const isToday    = ymd === todayYMD
              return (
                <button
                  key={i}
                  onClick={() => handleSelect(day)}
                  style={{
                    ...btnBase,
                    fontSize: 11, textAlign: 'center', padding: '5px 0',
                    border: isToday && !isSelected ? '1px solid var(--border2)' : '1px solid transparent',
                    background: isSelected ? 'var(--accent)' : 'transparent',
                    color: isSelected ? '#0a0a0a' : isToday ? 'var(--accent)' : 'var(--text)',
                    fontWeight: isSelected || isToday ? 600 : 400,
                  }}
                  onMouseEnter={e => { if (!isSelected) e.currentTarget.style.background = 'var(--surface2)' }}
                  onMouseLeave={e => { if (!isSelected) e.currentTarget.style.background = 'transparent' }}
                >
                  {day}
                </button>
              )
            })}
          </div>

          {/* Clear */}
          {value && (
            <button
              onClick={() => { onChange(''); setOpen(false) }}
              style={{
                ...btnBase, marginTop: 10, width: '100%', padding: '5px 0',
                border: '1px solid var(--border2)', fontSize: 11, color: 'var(--text-dim)',
              }}
              onMouseEnter={e => e.currentTarget.style.background = 'var(--surface2)'}
              onMouseLeave={e => e.currentTarget.style.background = 'none'}
            >
              Clear
            </button>
          )}
        </div>
      )}
    </div>
  )
}
