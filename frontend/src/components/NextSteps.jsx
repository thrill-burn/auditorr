import React, { useState, useEffect, useRef } from 'react'
import { api } from '../api'
import { LoadingRow, WorkflowError, SectionLabel } from './workflows/shared'

// Next steps — "this is your prioritized (and rewarded) workflows page."
//
// The spine is real: the five workflows ordered by recoverable health-score
// points per unit of effort. The prize layer is deliberately useless and says
// so — Progress Quest rules: many tiers, mock-heroic names, the next one
// always in sight. Cheese lives in the language, never in the pigment: no
// saturated fills, no gradients, no confetti. See prompts/OPS.md.
//
// Everything here is contained to this page. Nothing about Next steps appears
// on the dashboard, in toasts, or as a sidebar badge.

const STATE_META = {
  fix:      { label: 'Needs you',   color: 'var(--red)' },
  blocked:  { label: 'Blocked',     color: 'var(--text-faint)' },
  optimize: { label: 'Could improve', color: 'var(--yellow)' },
  maintain: { label: 'Clear',       color: 'var(--green)' },
  standby:  { label: 'On standby',  color: 'var(--text-faint)' },
}

// ── Count-up readout, matching the HealthDial's 0.9s cubic ease-out ──────────
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
      const eased = 1 - Math.pow(1 - p, 3)
      setVal(Math.round(from + delta * eased))
      if (p < 1) raf = requestAnimationFrame(step)
      else fromRef.current = target || 0
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target, ms])
  return val
}

function Track({ pct, color = 'var(--accent)', height = 4 }) {
  return (
    <div style={{ height, background: 'var(--surface3)', borderRadius: 'var(--r-pill)', overflow: 'hidden' }}>
      <div style={{
        width: `${Math.max(0, Math.min(100, pct))}%`, height: '100%',
        background: `${color}55`, borderRadius: 'var(--r-pill)',
        transition: 'width 0.6s cubic-bezier(.4,0,.2,1)',
      }} />
    </div>
  )
}

function Dot({ color, size = 7 }) {
  return <span className="ui-status-dot" style={{ width: size, height: size, background: color, flexShrink: 0 }} />
}

// ── Hero row — the answer to "what should I be doing" ────────────────────────
function HeroRow({ row, onNavigate }) {
  const meta = STATE_META[row.state] || STATE_META.maintain
  const actionable = row.state === 'fix' || row.state === 'optimize'
  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)',
      boxShadow: 'var(--elev-1)', padding: '20px 22px',
      display: 'flex', flexDirection: 'column', gap: 12,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <Dot color={row.accent} />
        <span style={{ fontSize: 17, fontWeight: 700, color: 'var(--text)' }}>{row.label}</span>
        <span style={{
          fontFamily: 'var(--mono)', fontSize: 10.5, padding: '2px 8px',
          borderRadius: 'var(--r-pill)', border: `1px solid ${meta.color}40`, color: meta.color,
        }}>{meta.label}</span>
        {row.nature && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--text-faint)' }}>
            {row.nature}
          </span>
        )}
        {row.score_lost > 0 && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--text-dim)', marginLeft: 'auto' }}>
            {row.score_lost} of {row.score_max} pts lost
          </span>
        )}
      </div>

      <div style={{ fontFamily: 'var(--mono)', fontSize: 19, fontWeight: 700, color: 'var(--text)', lineHeight: 1.25 }}>
        {row.headline}
      </div>

      <p style={{ fontSize: 13, color: 'var(--text-dim)', lineHeight: 1.65, margin: 0, maxWidth: 760 }}>
        {row.teaching}
      </p>

      {/* How this one pays out. Each workflow rewards in its own shape:
          a defended streak, a cumulative shovel count, or a ratcheting best. */}
      {row.reward && (
        <div style={{
          borderTop: '1px solid var(--border)', paddingTop: 10, marginTop: 2,
          display: 'flex', flexDirection: 'column', gap: 2,
        }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text)' }}>
            {row.reward.headline}
          </span>
          <span style={{ fontSize: 11.5, color: 'var(--text-faint)', lineHeight: 1.5 }}>
            {row.reward.detail}
          </span>
        </div>
      )}

      {actionable && (
        <button
          onClick={() => onNavigate(row.id)}
          style={{
            alignSelf: 'flex-start', marginTop: 2,
            padding: '9px 18px', borderRadius: 'var(--r)', cursor: 'pointer',
            border: '1px solid var(--accent)', background: 'var(--accent)',
            color: '#0a0a0a', fontSize: 13, fontWeight: 600,
          }}
        >
          Open {row.label} →
        </button>
      )}
      {row.state === 'blocked' && (
        <button
          onClick={() => onNavigate('config')}
          style={{
            alignSelf: 'flex-start', marginTop: 2,
            padding: '8px 16px', borderRadius: 'var(--r)', cursor: 'pointer',
            border: '1px solid var(--border2)', background: 'var(--surface2)',
            color: 'var(--text)', fontSize: 12.5, fontWeight: 600,
          }}
        >
          Open Config →
        </button>
      )}
    </div>
  )
}

// ── Compact row — the queue behind the hero ──────────────────────────────────
function QueueRow({ row, onNavigate }) {
  const meta = STATE_META[row.state] || STATE_META.maintain
  const clickable = row.state !== 'blocked'
  return (
    <button
      onClick={() => onNavigate(clickable ? row.id : 'config')}
      style={{
        width: '100%', textAlign: 'left', cursor: 'pointer',
        background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)',
        boxShadow: 'var(--elev-1)', padding: '13px 16px',
        display: 'flex', alignItems: 'center', gap: 12, transition: 'all 0.12s',
      }}
      onMouseEnter={e => { e.currentTarget.style.boxShadow = 'var(--elev-2)' }}
      onMouseLeave={e => { e.currentTarget.style.boxShadow = 'var(--elev-1)' }}
    >
      <Dot color={row.accent} />
      <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', minWidth: 74 }}>{row.label}</span>
      <span style={{
        fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-dim)',
        flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }}>
        {row.headline}
      </span>
      {row.reward && (
        <span style={{
          fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-faint)', flexShrink: 0,
          maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {row.reward.headline}
        </span>
      )}
      <span style={{
        fontFamily: 'var(--mono)', fontSize: 10.5, padding: '2px 8px', flexShrink: 0,
        borderRadius: 'var(--r-pill)', border: `1px solid ${meta.color}40`, color: meta.color,
      }}>{meta.label}</span>
    </button>
  )
}

// ── Setup checklist ──────────────────────────────────────────────────────────
function SetupPanel({ setup, onNavigate }) {
  const [open, setOpen] = useState(!setup.complete)
  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)',
      boxShadow: 'var(--elev-1)', padding: '14px 18px',
    }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          width: '100%', display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer',
          background: 'none', border: 'none', padding: 0, textAlign: 'left',
        }}
      >
        <Dot color={setup.complete ? 'var(--green)' : 'var(--accent)'} />
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>Setup</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--text-dim)' }}>
          {setup.done} / {setup.total}
        </span>
        <span style={{ flex: 1 }} />
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
          strokeLinecap="round" strokeLinejoin="round"
          style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', opacity: 0.45, color: 'var(--text-dim)' }}>
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </button>

      {open && (
        <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 2 }}>
          {setup.steps.map(s => (
            <div key={s.id}
              onClick={() => !s.done && onNavigate(s.tab)}
              style={{
                display: 'flex', alignItems: 'flex-start', gap: 10, padding: '8px 8px',
                borderRadius: 'var(--r)', cursor: s.done ? 'default' : 'pointer',
                transition: 'background 0.12s',
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
                <div style={{ fontSize: 12.5, color: s.done ? 'var(--text-dim)' : 'var(--text)', fontWeight: s.done ? 400 : 600 }}>
                  {s.label}
                </div>
                <div style={{ fontSize: 11.5, color: 'var(--text-faint)', marginTop: 2, lineHeight: 1.5 }}>{s.hint}</div>
              </div>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: s.done ? 'var(--green)' : 'var(--text-faint)', flexShrink: 0 }}>
                +{s.points}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── One prize ladder ─────────────────────────────────────────────────────────
function Ladder({ l }) {
  const [open, setOpen] = useState(false)
  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)',
      boxShadow: 'var(--elev-1)', padding: '13px 16px',
      display: 'flex', flexDirection: 'column', gap: 8,
    }}>
      <div onClick={() => setOpen(o => !o)} style={{ cursor: 'pointer', display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>
          {l.tier_label || l.name}
        </span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-faint)' }}>
          {l.tier} / {l.tiers_total}
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text)' }}>{l.value_label}</span>
      </div>

      <Track pct={l.pct} color={l.maxed ? 'var(--green)' : 'var(--accent)'} />

      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <span style={{ fontSize: 11.5, color: 'var(--text-faint)', flex: 1, minWidth: 0, lineHeight: 1.5 }}>
          {l.blurb}
        </span>
        {!l.maxed && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', flexShrink: 0 }}>
            {l.next_label} at {l.next_at_label}
          </span>
        )}
        {l.maxed && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--green)', flexShrink: 0 }}>maxed</span>
        )}
      </div>

      {open && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginTop: 2 }}>
          {l.tiers.map(t => (
            <span key={t.n} title={`${t.label} — ${t.at_label}`} style={{
              fontFamily: 'var(--mono)', fontSize: 10.5, padding: '2px 8px', borderRadius: 'var(--r-pill)',
              border: t.earned ? '1px solid var(--border2)' : '1px dashed var(--border2)',
              color: t.earned ? 'var(--text-dim)' : 'var(--text-faint)',
              background: t.earned ? 'var(--surface2)' : 'transparent',
            }}>
              {t.label} · {t.at_label}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function Feat({ f }) {
  return (
    <div style={{
      background: f.earned ? 'var(--surface)' : 'transparent',
      border: f.earned ? '1px solid var(--border)' : '1px dashed var(--border2)',
      borderRadius: 'var(--rl)', padding: '11px 14px',
      boxShadow: f.earned ? 'var(--elev-1)' : 'none',
      display: 'flex', flexDirection: 'column', gap: 3,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
        {f.earned && <Dot color="var(--green)" size={6} />}
        <span style={{ fontSize: 12.5, fontWeight: 600, color: f.earned ? 'var(--text)' : 'var(--text-faint)' }}>
          {f.label}
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ fontFamily: 'var(--mono)', fontSize: 10.5, color: f.earned ? 'var(--text-dim)' : 'var(--text-faint)' }}>
          +{f.points}
        </span>
      </div>
      <span style={{ fontSize: 11.5, color: 'var(--text-faint)', lineHeight: 1.5 }}>{f.desc}</span>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
export default function NextSteps({ onNavigate }) {
  const [data, setData]       = useState(null)
  const [error, setError]     = useState(null)
  const [showPrizes, setShow] = useState(false)

  useEffect(() => {
    let alive = true
    api.nextSteps()
      .then(d => { if (alive) setData(d) })
      .catch(e => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [])

  const points = useCountUp(data?.rank?.points || 0)

  if (error) return <div style={{ padding: 'var(--page-gutter)' }}><WorkflowError message={error} /></div>
  if (!data)  return <div style={{ padding: 'var(--page-gutter)' }}><LoadingRow label="Working out what you should be doing…" /></div>

  if (data.enabled === false) {
    return (
      <div style={{ padding: 'var(--page-gutter)', color: 'var(--text-dim)', fontSize: 13 }}>
        Next steps is turned off. Enable it in Config if you want it back.
      </div>
    )
  }

  const { rank, setup, rows, ladders, feats, prizes, health } = data
  const hero  = rows && rows.length ? rows[0] : null
  const queue = rows && rows.length ? rows.slice(1) : []

  return (
    <div style={{ padding: 'var(--page-gutter)', display: 'flex', flexDirection: 'column', gap: 'var(--card-gap)', maxWidth: 1180 }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 260 }}>
          <div style={{ fontFamily: 'var(--sans)', fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Workflows</div>
          <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--text)', lineHeight: 1.2 }}>
            {data.stage === 'setup' ? 'Finish setting up' : 'Do this next'}
          </div>
          <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.6, maxWidth: 720 }}>
            {data.stage === 'setup'
              ? 'auditorr has nothing to audit yet. Work down the list and the rest of this page fills in.'
              : 'Two steps. First the one-time cleanup — orphaned torrents and duplicates — which finishes and stays finished. Then the ongoing part: tend to what fails to import, and slowly hardlink more of what you own.'}
          </p>
        </div>

        {/* Rank readout — mono, near-white, count-up. Dressing, not the point. */}
        <div style={{
          background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)',
          boxShadow: 'var(--elev-1)', padding: '12px 16px', minWidth: 210,
        }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>{rank.name}</div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 22, fontWeight: 700, color: 'var(--text)', lineHeight: 1.1 }}>
            {points.toLocaleString()}
            <span style={{ fontSize: 11, color: 'var(--text-dim)', fontWeight: 400 }}> pts</span>
          </div>
          <div style={{ marginTop: 8 }}><Track pct={rank.pct} /></div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--text-faint)', marginTop: 6 }}>
            {rank.next_name ? `${rank.next_name} at ${rank.next_at.toLocaleString()}` : 'nothing left to become'}
          </div>
        </div>
      </div>

      {/* Setup — only while it matters */}
      {!setup.complete && <SetupPanel setup={setup} onNavigate={onNavigate} />}

      {/* The spine. Rows are grouped by stage so the sequence is legible:
          foundation is the sprint to a clean baseline, maintenance is the
          forever loop. Hardlinked media scores high because it is hard, not
          because it comes first. */}
      {hero && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <HeroRow row={hero} onNavigate={onNavigate} />
          {queue.map((r, i) => {
            const prev = i === 0 ? hero : queue[i - 1]
            const newStage = r.stage !== prev.stage
            return (
              <React.Fragment key={r.id}>
                {newStage && (
                  <div style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    marginTop: 6, paddingLeft: 2,
                  }}>
                    <span style={{ fontSize: 11.5, fontWeight: 600, color: 'var(--text-dim)' }}>
                      {r.stage_label}
                    </span>
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--text-faint)' }}>
                      {r.stage === 'oneoff' ? 'finishes' : r.stage === 'ongoing' ? 'never finishes' : ''}
                    </span>
                    <span style={{ flex: 1, height: 1, background: 'var(--border)' }} />
                  </div>
                )}
                <QueueRow row={r} onNavigate={onNavigate} />
              </React.Fragment>
            )
          })}
        </div>
      )}

      {data.baseline_clear && hero && hero.stage !== 'oneoff' && (
        <div style={{ fontSize: 12.5, color: 'var(--text-dim)', lineHeight: 1.6 }}>
          Orphans and duplicates are clear, and that part stays done — your library has a clean
          baseline. What's left never quite finishes: keep an eye on what fails to import, and
          hardlink more of what you already own.
        </div>
      )}

      {health?.score != null && (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--text-faint)' }}>
          Library health {health.score} · {health.status} · {data.audits} audit{data.audits === 1 ? '' : 's'} on record
          {data.streak_90 > 0 && ` · ${data.streak_90} day${data.streak_90 === 1 ? '' : 's'} at 90+`}
        </div>
      )}

      {/* Useless prizes — the side quest, and the page says so */}
      <div style={{ marginTop: 6 }}>
        <button
          onClick={() => setShow(s => !s)}
          style={{
            width: '100%', display: 'flex', alignItems: 'baseline', gap: 10, cursor: 'pointer',
            background: 'none', border: 'none', padding: '0 0 10px', textAlign: 'left',
          }}
        >
          <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>Useless prizes</span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--text-dim)' }}>
            {prizes.earned} / {prizes.total}
          </span>
          <span style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>
            None of this does anything. Your library does not care.
          </span>
          <span style={{ flex: 1 }} />
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
            strokeLinecap="round" strokeLinejoin="round"
            style={{ transform: showPrizes ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', opacity: 0.45, color: 'var(--text-dim)' }}>
            <polyline points="9 18 15 12 9 6" />
          </svg>
        </button>

        {showPrizes && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--card-gap)' }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 10 }}>
              {ladders.map(l => <Ladder key={l.id} l={l} />)}
            </div>
            <div>
              <SectionLabel>Feats</SectionLabel>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 10 }}>
                {feats.map(f => <Feat key={f.id} f={f} />)}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
