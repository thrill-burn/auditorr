import React, { useState, useEffect, useRef } from 'react'

const PULSE_STYLE = `
@keyframes auditPulse {
  0%, 100% { opacity: 0.4; }
  50%       { opacity: 1; }
}
.audit-pulse {
  animation: auditPulse 1.4s ease-in-out infinite;
}
`

const LOGO = (
  <div style={{ width: 30, height: 30, flexShrink: 0 }}>
    <svg width="30" height="30" viewBox="0 0 200 200" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="90" cy="90" r="68" stroke="#f57c00" strokeWidth="14" strokeLinecap="round"/>
      <line x1="138" y1="140" x2="174" y2="178" stroke="#f57c00" strokeWidth="16" strokeLinecap="round"/>
      <circle cx="90" cy="90" r="14" fill="#f57c00"/>
      <line x1="91" y1="76" x2="96" y2="45" stroke="#f57c00" strokeWidth="6" strokeLinecap="round"/>
      <circle cx="97" cy="34" r="10" fill="#f57c00" opacity="0.85"/>
      <line x1="104" y1="90" x2="134" y2="83" stroke="#f57c00" strokeWidth="6" strokeLinecap="round"/>
      <circle cx="145" cy="80" r="10" fill="#f57c00" opacity="0.85"/>
      <line x1="97" y1="103" x2="116" y2="130" stroke="#f57c00" strokeWidth="6" strokeLinecap="round"/>
      <circle cx="122" cy="140" r="10" fill="#f57c00" opacity="0.85"/>
      <line x1="83" y1="103" x2="66" y2="124" stroke="#f57c00" strokeWidth="6" strokeLinecap="round"/>
      <line x1="59" y1="133" x2="47" y2="150" stroke="#ef4444" strokeWidth="6" strokeLinecap="round" strokeDasharray="8 8"/>
      <circle cx="40" cy="160" r="10" fill="none" stroke="#ef4444" strokeWidth="6"/>
      <line x1="33" y1="153" x2="47" y2="167" stroke="#ef4444" strokeWidth="5" strokeLinecap="round"/>
      <line x1="47" y1="153" x2="33" y2="167" stroke="#ef4444" strokeWidth="5" strokeLinecap="round"/>
      <line x1="76" y1="90" x2="45" y2="97" stroke="#f57c00" strokeWidth="6" strokeLinecap="round"/>
      <circle cx="34" cy="100" r="10" fill="#f57c00" opacity="0.85"/>
    </svg>
  </div>
)

function PhaseBar({ label, fillPct, pulse, phaseStatus }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: 1.5, width: 56, flexShrink: 0 }}>{label}</span>
      <div style={{ flex: 1, height: 6, background: 'var(--border2)', borderRadius: 99, overflow: 'hidden' }}>
        <div
          className={pulse ? 'audit-pulse' : undefined}
          style={{ height: '100%', width: `${fillPct}%`, background: 'var(--accent)', borderRadius: 99, transition: 'width 0.3s ease' }}
        />
      </div>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', width: 130, flexShrink: 0, textAlign: 'right' }}>{phaseStatus}</span>
    </div>
  )
}

function LoadingResultsCard() {
  const [loadProgress, setLoadProgress] = useState(0)
  const startRef = useRef(Date.now())

  useEffect(() => {
    const id = setInterval(() => {
      const elapsed = Date.now() - startRef.current
      setLoadProgress(Math.min(85, 85 * (1 - Math.exp(-elapsed / 8000))))
    }, 100)
    return () => clearInterval(id)
  }, [])

  return (
    <>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {LOGO}
          <span style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 600, color: 'var(--accent)' }}>Loading results…</span>
        </div>
      </div>

      {/* Indeterminate bar */}
      <div style={{ height: 6, background: 'var(--border2)', borderRadius: 99, overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${loadProgress}%`, background: 'var(--accent)', borderRadius: 99, transition: 'width 0.1s ease-out' }} />
      </div>

      {/* Status */}
      <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-faint)' }}>
        Deserializing audit results…
      </div>
    </>
  )
}

export default function ScanProgress({ isScanning, progress, phase, statusMessage, scannedFiles, totalFiles, isLoadingResults }) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    if (isScanning || isLoadingResults) {
      setVisible(true)
    } else {
      const t = setTimeout(() => setVisible(false), 300)
      return () => clearTimeout(t)
    }
  }, [isScanning, isLoadingResults])

  if (!visible) return null

  const torrentsDone = phase === 'disk' || phase === 'post' || phase === 'idle'
  const torrentsActive = phase === 'connecting' || phase === 'torrents'
  const torrentsPulse = torrentsActive && progress === 0
  const torrentsFill = torrentsDone ? 100 : torrentsPulse ? 40 : progress

  const diskDone = phase === 'post' || phase === 'idle'
  const diskActive = phase === 'disk'
  const diskFill = diskDone ? 100 : diskActive ? progress : 0

  const torrentsStatus = phase === 'connecting' ? 'connecting…'
    : phase === 'torrents' ? 'fetching torrent list…'
    : 'done'

  const diskStatus = phase === 'connecting' || phase === 'torrents' ? 'waiting…'
    : phase === 'disk' ? 'scanning…'
    : 'done'

  return (
    <>
      <style>{PULSE_STYLE}</style>
      <div style={{
        position: 'fixed', bottom: 24, right: 24,
        maxWidth: 860, width: 'calc(100vw - 48px)',
        background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--rl)', boxShadow: '0 8px 32px rgba(0,0,0,0.5)',
        padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 16,
        zIndex: 600,
      }}>
        {isLoadingResults && !isScanning ? (
          <LoadingResultsCard />
        ) : (
          <>
            {/* Header */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                {LOGO}
                <span style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 600, color: 'var(--accent)' }}>Scanning library…</span>
              </div>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 13, color: 'var(--accent)' }}>{progress}%</span>
            </div>

            {/* Phase bars */}
            <PhaseBar label="Torrents" fillPct={torrentsFill} pulse={torrentsPulse} phaseStatus={torrentsStatus} />
            <PhaseBar label="Disk" fillPct={diskFill} pulse={false} phaseStatus={diskStatus} />

            {/* File counter */}
            {totalFiles > 0 && (
              <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', textAlign: 'right' }}>
                {scannedFiles.toLocaleString()} / {totalFiles.toLocaleString()} files
              </div>
            )}

            {/* Status message */}
            {statusMessage && (
              <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-faint)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {statusMessage}
              </div>
            )}
          </>
        )}
      </div>
    </>
  )
}
