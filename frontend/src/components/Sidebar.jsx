import React, { useState, useEffect } from 'react'
import { scoreColor } from '../utils'

const NAV = [
  {
    id: 'dashboard', label: 'Dashboard',
    icon: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
        <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>
      </svg>
    ),
  },
  {
    id: 'media', label: 'Media',
    icon: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>
        <path d="M14 2v4a2 2 0 0 0 2 2h4"/>
      </svg>
    ),
  },
  {
    id: 'torrents', label: 'Torrents',
    icon: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="7 10 12 15 17 10"/>
        <line x1="12" y1="15" x2="12" y2="3"/>
      </svg>
    ),
  },
  {
    id: 'trackers', label: 'Trackers',
    icon: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10"/>
        <circle cx="12" cy="12" r="3"/>
        <line x1="12" y1="2" x2="12" y2="5"/>
        <line x1="12" y1="19" x2="12" y2="22"/>
        <line x1="2" y1="12" x2="5" y2="12"/>
        <line x1="19" y1="12" x2="22" y2="12"/>
      </svg>
    ),
  },
  {
    id: 'changes', label: 'Changes',
    icon: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10"/>
        <polyline points="12 6 12 12 16 14"/>
      </svg>
    ),
  },
  {
    id: 'workflows', label: 'Workflows',
    icon: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="5" cy="6" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="12" cy="18" r="2"/>
        <line x1="7" y1="6" x2="17" y2="6"/>
        <line x1="5.5" y1="7.8" x2="11" y2="16.2"/>
        <line x1="18.5" y1="7.8" x2="13" y2="16.2"/>
      </svg>
    ),
    children: [
      { id: 'workflows', label: 'Backfill' },
    ],
  },
  {
    id: 'config', label: 'Config',
    icon: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>
        <circle cx="12" cy="12" r="3"/>
      </svg>
    ),
  },
]

export default function Sidebar({ active, onChange, isScanning, progress, lastAuditTime, lastScanStatus, trigger, nextScanIn, statusMessage, score, crossSeedMultiplier }) {
  const scoreC = score != null ? scoreColor(score) : 'var(--text-dim)'
  const csDisplay = crossSeedMultiplier != null ? crossSeedMultiplier.toFixed(2) : null

  const [openGroups, setOpenGroups] = useState(() => {
    const s = new Set()
    if (active === 'workflows') s.add('workflows')
    return s
  })

  useEffect(() => {
    if (active === 'workflows') setOpenGroups(s => new Set([...s, 'workflows']))
  }, [active])

  function handleGroupClick(groupId) {
    const isOpen = openGroups.has(groupId)
    setOpenGroups(prev => {
      const next = new Set(prev)
      isOpen ? next.delete(groupId) : next.add(groupId)
      return next
    })
    if (!isOpen) onChange(groupId)
  }

  const triggerLabel = {
    startup:   '⚡ startup',
    watchdog:  '👁 watchdog',
    manual:    '▶ manual',
    scheduled: '⏰ scheduled',
    idle:      null,
  }[trigger] || null

  return (
    <aside style={{
      width: 220, flexShrink: 0,
      background: 'var(--surface)', borderRight: '1px solid var(--border)',
      display: 'flex', flexDirection: 'column',
      position: 'sticky', top: 0, height: '100vh', overflow: 'hidden',
    }}>
      {/* Logo */}
      <div style={{ padding: '20px 18px 16px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {/* Option J icon: magnifying glass with hub-and-spoke, broken lower-left node */}
          <div style={{ width: 30, height: 30, flexShrink: 0 }}>
            <svg width="30" height="30" viewBox="0 0 200 200" fill="none" xmlns="http://www.w3.org/2000/svg">
              {/* Glass circle */}
              <circle cx="90" cy="90" r="68" stroke="#f57c00" strokeWidth="14" strokeLinecap="round"/>
              {/* Handle — bottom-right, well clear of all spokes */}
              <line x1="138" y1="140" x2="174" y2="178" stroke="#f57c00" strokeWidth="16" strokeLinecap="round"/>
              {/* Hub node */}
              <circle cx="90" cy="90" r="14" fill="#f57c00"/>
              {/* Spoke 1 — ~12deg, top-right: healthy */}
              <line x1="91" y1="76" x2="96" y2="45" stroke="#f57c00" strokeWidth="6" strokeLinecap="round"/>
              <circle cx="97" cy="34" r="10" fill="#f57c00" opacity="0.85"/>
              {/* Spoke 2 — ~80deg, right: healthy */}
              <line x1="104" y1="90" x2="134" y2="83" stroke="#f57c00" strokeWidth="6" strokeLinecap="round"/>
              <circle cx="145" cy="80" r="10" fill="#f57c00" opacity="0.85"/>
              {/* Spoke 3 — ~150deg, lower-right: healthy (well away from handle at ~320deg) */}
              <line x1="97" y1="103" x2="116" y2="130" stroke="#f57c00" strokeWidth="6" strokeLinecap="round"/>
              <circle cx="122" cy="140" r="10" fill="#f57c00" opacity="0.85"/>
              {/* Spoke 4 — ~210deg, lower-left: BROKEN */}
              <line x1="83" y1="103" x2="66" y2="124" stroke="#f57c00" strokeWidth="6" strokeLinecap="round"/>
              <line x1="59" y1="133" x2="47" y2="150" stroke="#ef4444" strokeWidth="6" strokeLinecap="round" strokeDasharray="8 8"/>
              <circle cx="40" cy="160" r="10" fill="none" stroke="#ef4444" strokeWidth="6"/>
              <line x1="33" y1="153" x2="47" y2="167" stroke="#ef4444" strokeWidth="5" strokeLinecap="round"/>
              <line x1="47" y1="153" x2="33" y2="167" stroke="#ef4444" strokeWidth="5" strokeLinecap="round"/>
              {/* Spoke 5 — ~290deg, left: healthy */}
              <line x1="76" y1="90" x2="45" y2="97" stroke="#f57c00" strokeWidth="6" strokeLinecap="round"/>
              <circle cx="34" cy="100" r="10" fill="#f57c00" opacity="0.85"/>
            </svg>
          </div>
          <span style={{ fontFamily: 'var(--mono)', fontWeight: 700, fontSize: 15, color: 'var(--accent)', letterSpacing: '-0.3px' }}>auditorr</span>
        </div>
      </div>

      {/* Nav */}
      <nav style={{ flex: 1, padding: '10px 10px 0', display: 'flex', flexDirection: 'column', gap: 2 }}>
        {NAV.map(({ id, label, icon, children }) => {
          if (children) {
            const isGroupActive = active === id
            const isOpen = openGroups.has(id)
            return (
              <React.Fragment key={id}>
                <button onClick={() => handleGroupClick(id)} style={{
                  display: 'flex', alignItems: 'center', gap: 10,
                  padding: '9px 10px', borderRadius: 7, border: 'none',
                  background: isGroupActive ? 'var(--accent)18' : 'transparent',
                  color: isGroupActive ? 'var(--accent)' : 'var(--text-dim)',
                  fontSize: 13, fontWeight: isGroupActive ? 600 : 400,
                  cursor: 'pointer', transition: 'all 0.12s',
                  textAlign: 'left', width: '100%',
                  borderLeft: `2px solid ${isGroupActive ? 'var(--accent)' : 'transparent'}`,
                }}
                onMouseEnter={e => { if (!isGroupActive) e.currentTarget.style.color = 'var(--text)' }}
                onMouseLeave={e => { if (!isGroupActive) e.currentTarget.style.color = 'var(--text-dim)' }}>
                  <span style={{ flexShrink: 0, opacity: isGroupActive ? 1 : 0.7 }}>{icon}</span>
                  <span style={{ flex: 1 }}>{label}</span>
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
                    strokeLinecap="round" strokeLinejoin="round"
                    style={{ transform: isOpen ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', opacity: 0.45, flexShrink: 0 }}>
                    <polyline points="9 18 15 12 9 6"/>
                  </svg>
                </button>
                {isOpen && children.map(child => {
                  const childActive = active === child.id
                  return (
                    <button key={child.label} onClick={() => onChange(child.id)} style={{
                      display: 'flex', alignItems: 'center', gap: 8,
                      padding: '6px 10px 6px 34px', borderRadius: 7, border: 'none',
                      background: childActive ? 'var(--accent)12' : 'transparent',
                      color: childActive ? 'var(--accent)' : 'var(--text-dim)',
                      fontSize: 12, fontWeight: childActive ? 600 : 400,
                      cursor: 'pointer', transition: 'all 0.12s',
                      textAlign: 'left', width: '100%',
                      borderLeft: '2px solid transparent',
                    }}
                    onMouseEnter={e => { if (!childActive) e.currentTarget.style.color = 'var(--text)' }}
                    onMouseLeave={e => { if (!childActive) e.currentTarget.style.color = 'var(--text-dim)' }}>
                      <span style={{
                        width: 5, height: 5, borderRadius: '50%', flexShrink: 0,
                        background: childActive ? 'var(--accent)' : 'currentColor',
                        opacity: childActive ? 1 : 0.35,
                      }} />
                      {child.label}
                    </button>
                  )
                })}
              </React.Fragment>
            )
          }

          const isActive = active === id
          return (
            <button key={id} onClick={() => onChange(id)} style={{
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '9px 10px', borderRadius: 7, border: 'none',
              background: isActive ? 'var(--accent)18' : 'transparent',
              color: isActive ? 'var(--accent)' : 'var(--text-dim)',
              fontSize: 13, fontWeight: isActive ? 600 : 400,
              cursor: 'pointer', transition: 'all 0.12s',
              textAlign: 'left', width: '100%',
              borderLeft: `2px solid ${isActive ? 'var(--accent)' : 'transparent'}`,
            }}
            onMouseEnter={e => { if (!isActive) e.currentTarget.style.color = 'var(--text)' }}
            onMouseLeave={e => { if (!isActive) e.currentTarget.style.color = 'var(--text-dim)' }}>
              <span style={{ flexShrink: 0, opacity: isActive ? 1 : 0.7 }}>{icon}</span>
              <span>{label}</span>
            </button>
          )
        })}
      </nav>

      {/* Bottom stats */}
      <div style={{ padding: '12px 12px 16px', borderTop: '1px solid var(--border)', display: 'flex', flexDirection: 'column', gap: 8 }}>

        {/* Health + cross-seed scores */}
        {score != null && (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
            {/* Library health */}
            <div style={{
              padding: '8px 10px', borderRadius: 7,
              background: scoreC + '10', border: '1px solid ' + scoreC + '25',
              display: 'flex', flexDirection: 'column', gap: 2,
            }}>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: 1, textTransform: 'uppercase' }}>Health</span>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 16, fontWeight: 700, color: scoreC, lineHeight: 1 }}>
                {score}<span style={{ fontSize: 11, color: 'var(--text-dim)', fontWeight: 400 }}>/100</span>
              </span>
            </div>

            {/* Cross-seed multiplier */}
            {csDisplay != null && (
              <div style={{
                padding: '8px 10px', borderRadius: 7,
                background: 'var(--blue)10', border: '1px solid var(--blue)25',
                display: 'flex', flexDirection: 'column', gap: 2,
              }}>
                <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', letterSpacing: 0, textTransform: 'uppercase', whiteSpace: 'nowrap' }}>Cross-seed</span>
                <span style={{ fontFamily: 'var(--mono)', fontSize: 16, fontWeight: 700, color: 'var(--blue)', lineHeight: 1 }}>
                  {csDisplay}<span style={{ fontSize: 11, color: 'var(--text-dim)', fontWeight: 400 }}>×</span>
                </span>
              </div>
            )}
          </div>
        )}

        {/* Scan status */}
        {isScanning ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--accent)' }}>Scanning…</span>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--accent)' }}>{progress}%</span>
            </div>
            <div style={{ height: 3, background: 'var(--border2)', borderRadius: 99, overflow: 'hidden' }}>
              <div style={{ width: progress + '%', height: '100%', background: 'var(--accent)', borderRadius: 99, transition: 'width 0.4s ease' }} />
            </div>
            {statusMessage && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.4 }}>{statusMessage}</span>
            )}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {lastScanStatus === 'error' && statusMessage && (
              <div style={{ padding: '5px 8px', borderRadius: 5, background: 'var(--red)12', border: '1px solid var(--red)30', marginBottom: 2 }}>
                <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--red)', display: 'block', lineHeight: 1.4 }}>
                  ✗ {statusMessage}
                </span>
              </div>
            )}
            {lastAuditTime !== 'Never' && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-faint)' }}>last run {lastAuditTime}</span>
            )}
            {nextScanIn != null && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--yellow)' }}>⏱ next in {nextScanIn}s</span>
            )}
            {triggerLabel && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>{triggerLabel}</span>
            )}
          </div>
        )}
      </div>
    </aside>
  )
}
