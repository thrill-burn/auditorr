import React from 'react'

// An import watch has more ways to end than done and error (Phase 12, S07).
// `done` is the only success, and the server reaches it only when the arr's file
// for the target changed, so it is the only green. A failure is red. A result
// that is not a success and not a failure either — never queued, left the queue
// with no new file, still downloading when the watch stopped, not confirmable —
// is dim text with its message.
export const WATCH_ACTIVE = ['queued', 'downloading', 'importing']
export const WATCH_FAILED = ['error', 'failed', 'unreadable']

export function watchColor(status) {
  if (status === 'done') return 'var(--green)'
  if (WATCH_FAILED.includes(status)) return 'var(--red)'
  return WATCH_ACTIVE.includes(status) ? 'var(--accent)' : 'var(--text-dim)'
}

const STAGE_CONFIG = {
  queued:      { label: 'Queued',            color: 'var(--text-dim)', icon: 'pulse'   },
  downloading: { label: 'Downloading',       color: 'var(--accent)',   icon: 'spinner' },
  importing:   { label: 'Importing',         color: 'var(--accent)',   icon: 'spinner' },
  done:        { label: 'Done',              color: 'var(--green)',    icon: 'check'   },
  error:       { label: 'Failed',            color: 'var(--red)',      icon: 'x'       },
  failed:      { label: 'Download failed',   color: 'var(--red)',      icon: 'x'       },
  unreadable:  { label: 'Could not check',   color: 'var(--red)',      icon: 'x'       },
  unobserved:  { label: 'Never queued',      color: 'var(--text-dim)', icon: 'dash'    },
  no_new_file: { label: 'No new file',       color: 'var(--text-dim)', icon: 'dash'    },
  timed_out:   { label: 'Still downloading', color: 'var(--text-dim)', icon: 'dash'    },
  unconfirmed: { label: 'Unconfirmed',       color: 'var(--text-dim)', icon: 'dash'    },
}

function StageIcon({ type, color }) {
  if (type === 'spinner') return (
    <span style={{
      display: 'inline-block', width: 9, height: 9, borderRadius: '50%', flexShrink: 0,
      border: `1.5px solid ${color}`, borderTopColor: 'transparent',
      animation: 'importSpin 0.8s linear infinite',
    }} />
  )
  if (type === 'pulse') return (
    <span style={{
      display: 'inline-block', width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
      background: color, animation: 'importPulse 1.4s ease-in-out infinite',
    }} />
  )
  if (type === 'check') return <span style={{ color, fontSize: 'var(--font-sm)', lineHeight: 1, flexShrink: 0 }}>✓</span>
  if (type === 'dash') return <span style={{ color, fontSize: 'var(--font-sm)', lineHeight: 1, flexShrink: 0 }}>–</span>
  return <span style={{ color, fontSize: 'var(--font-sm)', lineHeight: 1, flexShrink: 0 }}>✗</span>
}

export default function ImportProgress({ open, jobs, onClose }) {
  if (!open) return null

  const activeCount = jobs.filter(j => WATCH_ACTIVE.includes(j.status)).length

  // Sort: active first, then the ones that have ended
  const sorted = [...jobs].sort((a, b) => {
    const aActive = WATCH_ACTIVE.includes(a.status)
    const bActive = WATCH_ACTIVE.includes(b.status)
    if (aActive && !bActive) return -1
    if (!aActive && bActive) return 1
    return 0
  })

  return (
    <div style={{
      position: 'fixed', bottom: 24, right: 24, zIndex: 350,
      width: 320, maxHeight: 400,
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 10, boxShadow: '0 8px 32px rgba(0,0,0,0.35)',
      display: 'flex', flexDirection: 'column', overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '10px 14px', borderBottom: '1px solid var(--border)', flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {/* Backfill and Trumped grabs share one watch and one panel. */}
          <span style={{ fontSize: 'var(--font-md)', fontWeight: 600, color: 'var(--text)' }}>Import Jobs</span>
          {activeCount > 0 && (
            <span style={{
              fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 99,
              background: 'var(--accent)18', color: 'var(--accent)', border: '1px solid var(--accent)30',
            }}>
              {activeCount} active
            </span>
          )}
        </div>
        <button onClick={onClose} style={{
          background: 'none', border: 'none', cursor: 'pointer',
          color: 'var(--text-dim)', fontSize: 16, lineHeight: 1, padding: '0 2px',
        }}>×</button>
      </div>

      {/* Job list */}
      <div style={{ overflowY: 'auto', flex: 1 }}>
        {jobs.length === 0 && (
          <div style={{ padding: '20px 14px', textAlign: 'center', color: 'var(--text-dim)', fontSize: 'var(--font-base)', fontFamily: 'var(--mono)' }}>
            No active import jobs
          </div>
        )}
        {sorted.map(job => {
          const cfg = STAGE_CONFIG[job.status] || STAGE_CONFIG.queued
          // Every ending but an observed import says what happened, in the arr's words or ours.
          const ended = !WATCH_ACTIVE.includes(job.status) && job.status !== 'done' && job.message
          return (
            <div key={job.job_id} style={{
              display: 'flex', alignItems: 'flex-start', gap: 10,
              padding: '9px 14px', borderBottom: '1px solid var(--border)',
            }}>
              {/* Service badge */}
              <span style={{
                fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', padding: '2px 5px', borderRadius: 3,
                flexShrink: 0, marginTop: 1,
                background: job.service === 'radarr' ? 'var(--yellow)18' : 'var(--blue)18',
                color:      job.service === 'radarr' ? 'var(--yellow)'   : 'var(--blue)',
                border:     `1px solid ${job.service === 'radarr' ? 'var(--yellow)' : 'var(--blue)'}35`,
              }}>
                {job.service || '—'}
              </span>

              {/* Title + stage */}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{
                  fontSize: 'var(--font-md)', fontWeight: 500, color: 'var(--text)',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }} title={job.title}>
                  {job.title || 'Unknown'}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginTop: 3 }}>
                  <StageIcon type={cfg.icon} color={cfg.color} />
                  <span style={{
                    fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: cfg.color,
                  }} title={ended ? job.message : undefined}>
                    {cfg.label}
                    {ended && ' — ' + job.message.slice(0, 40)}
                  </span>
                </div>
              </div>
            </div>
          )
        })}
      </div>

      <style>{`
        @keyframes importSpin  { to { transform: rotate(360deg); } }
        @keyframes importPulse { 0%,100% { opacity:0.35 } 50% { opacity:1 } }
      `}</style>
    </div>
  )
}
