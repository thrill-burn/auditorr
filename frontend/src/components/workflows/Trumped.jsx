import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../../api'
import { formatBytes } from '../../utils'
import { useToast } from '../Toast'
import {
  WorkflowHeader, WorkflowError, Spinner, SpinKeyframes, ActionButton, HDR_STYLE,
} from './shared'

const ACCENT = 'var(--green)'

// Compact seeding duration — days are the unit that matters for hit-and-run
function formatDuration(secs) {
  if (secs == null) return null
  const d = secs / 86400
  if (d >= 1) return `${d >= 10 ? Math.round(d) : d.toFixed(1)}d`
  const h = secs / 3600
  if (h >= 1) return `${Math.round(h)}h`
  return `${Math.max(1, Math.round(secs / 60))}m`
}

const SAMPLE_PM = `The following torrent(s) have been trumped

    Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos HDR x265-HQMUX

and will be replaced by
Jumanji 1995 2160p UHD BluRay TrueHD 7.1 Atmos DV HDR x265-RandomBytes.

Reason: DV/HDR replacing HDR`

// ── Step scaffold ─────────────────────────────────────────────────────────────
function StepShell({ n, active, done, title, children }) {
  const color = done ? ACCENT : active ? 'var(--text)' : 'var(--text-dim)'
  return (
    <div style={{ display: 'flex', gap: 14, opacity: active || done ? 1 : 0.5 }}>
      <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
        <span style={{
          width: 26, height: 26, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 12, fontWeight: 700, fontFamily: 'var(--mono)',
          border: `1.5px solid ${done || active ? ACCENT : 'var(--border2)'}`,
          background: done ? ACCENT : 'transparent',
          color: done ? '#fff' : active ? ACCENT : 'var(--text-dim)',
        }}>
          {done ? '✓' : n}
        </span>
        <span style={{ flex: 1, width: 1.5, background: 'var(--border2)' }} />
      </div>
      <div style={{ flex: 1, minWidth: 0, paddingBottom: 22 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color, marginBottom: 10 }}>{title}</div>
        {(active || done) && children}
      </div>
    </div>
  )
}

function Field({ label, value, onChange, mono }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, flex: 1, minWidth: 0 }}>
      <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>{label}</span>
      <input
        value={value} onChange={e => onChange(e.target.value)}
        style={{
          padding: '7px 10px', borderRadius: 6, border: '1px solid var(--border2)',
          background: 'var(--surface2)', color: 'var(--text)',
          fontFamily: mono ? 'var(--mono)' : 'inherit', fontSize: 12,
        }}
      />
    </label>
  )
}

function QualityChip({ label, hdr }) {
  const hdrInfo = HDR_STYLE[hdr]
  if (!label && !hdrInfo) return null
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      {label && (
        <span style={{ fontSize: 10, fontFamily: 'var(--mono)', padding: '1px 6px', borderRadius: 4, background: 'var(--surface3)', border: '1px solid var(--border2)', color: 'var(--text)' }}>{label}</span>
      )}
      {hdrInfo && (
        <span style={{ fontSize: 9, fontFamily: 'var(--mono)', fontWeight: 700, padding: '1px 4px', borderRadius: 3, background: hdrInfo.bg, color: hdrInfo.color }}>{hdr}</span>
      )}
    </span>
  )
}

export default function Trumped({ onNavigate }) {
  const toast = useToast()

  const [pmText, setPmText]     = useState('')
  const [oldTitle, setOldTitle] = useState('')
  const [newTitle, setNewTitle] = useState('')
  const [parsed, setParsed]     = useState(false)

  const [indexers, setIndexers] = useState([])
  const [indexer, setIndexer]   = useState('')

  const [group, setGroup]       = useState(null)   // {torrents, total_size, matched_name}
  const [search, setSearch]     = useState(null)   // search_release response
  const [clientDeleteAllowed, setClientDeleteAllowed] = useState(false)
  const [clientName, setClientName] = useState('the client')

  const [busy, setBusy]   = useState(null)   // 'parse'|'group'|'search'|'execute'
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)

  useEffect(() => {
    api.workflowIndexers().then(d => setIndexers(d.indexers || d || [])).catch(() => {})
    api.getConfig().then(cfg => {
      setClientDeleteAllowed(!!cfg.ALLOW_CLIENT_DELETE)
      setClientName(cfg.TORRENT_SOURCE === 'qui' ? 'qui' : 'qBittorrent')
    }).catch(() => {})
  }, [])

  const reset = useCallback(() => {
    setPmText(''); setOldTitle(''); setNewTitle(''); setParsed(false)
    setIndexer(''); setGroup(null); setSearch(null); setError(null); setResult(null)
  }, [])

  const handleParse = async () => {
    setBusy('parse'); setError(null)
    try {
      const r = await api.trumpParse(pmText)
      setOldTitle(r.old_title); setNewTitle(r.new_title); setParsed(true)
    } catch (e) {
      // Parse failure: drop into manual entry rather than blocking
      setParsed(true)
      toast(e.message, 'info')
    }
    setBusy(null)
  }

  const handleResolveGroup = async () => {
    setBusy('group'); setError(null)
    try {
      setGroup(await api.trumpResolveGroup(oldTitle))
    } catch (e) {
      setError(e.message)
    }
    setBusy(null)
  }

  const handleSearch = async () => {
    setBusy('search'); setError(null)
    try {
      setSearch(await api.trumpSearchRelease({ new_title: newTitle, indexer }))
    } catch (e) {
      setSearch(e.fallback_url ? { release: null, fallback_url: e.fallback_url, error: e.message } : null)
      setError(e.message)
    }
    setBusy(null)
  }

  const handleExecute = async () => {
    setBusy('execute'); setError(null)
    try {
      const r = await api.trumpExecute({
        hashes: group.torrents.map(t => ({ hash: t.hash, instance_id: t.instance_id })),
        release: search?.release ? {
          guid: search.release.guid,
          indexer_id: search.release.indexer_id,
        } : null,
        service: search?.service,
        connection_id: search?.connection_id,
      })
      setResult(r)
      const grabMsg = r.grabbed === true ? ' and grabbed the replacement'
        : r.grabbed === false ? ` but the grab failed (${r.grab_error})` : ''
      toast(`Removed ${r.removed} torrent${r.removed !== 1 ? 's' : ''}${grabMsg}`,
        r.grabbed === false ? 'error' : 'success')
    } catch (e) {
      setError(e.message)
    }
    setBusy(null)
  }

  // Step gating
  const step2 = parsed
  const step3 = step2 && group != null
  const step4 = step3
  const step5 = step4 && search != null

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 22, maxWidth: 980 }}>
      <WorkflowHeader
        title="Trumped"
        accent={ACCENT}
        blurb="When a tracker trumps one of your releases, paste the PM here: auditorr finds the whole hardlink group (every cross-seed), removes it from the client with its files, and grabs the replacement through Sonarr/Radarr — the manual multi-step swap, automated and confirmed at every step."
        right={(parsed || group) && (
          <button onClick={reset} style={{ fontSize: 12, padding: '6px 16px', borderRadius: 7, cursor: 'pointer', border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)' }}>
            ↺ Start over
          </button>
        )}
      />

      <WorkflowError message={error} />

      <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 10, boxShadow: 'var(--elev-1)', padding: '20px 22px' }}>
        {/* Step 1 — paste PM */}
        <StepShell n={1} active done={parsed} title="Paste the trump PM">
          {!parsed ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <textarea
                value={pmText} onChange={e => setPmText(e.target.value)}
                placeholder={SAMPLE_PM}
                rows={7}
                style={{
                  width: '100%', boxSizing: 'border-box', padding: '10px 12px', borderRadius: 7,
                  border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)',
                  fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 1.5, resize: 'vertical',
                }}
              />
              <div style={{ display: 'flex', gap: 8 }}>
                <ActionButton primary onClick={handleParse} disabled={busy != null || !pmText.trim()}>
                  {busy === 'parse' ? 'Parsing…' : 'Parse PM'}
                </ActionButton>
                <ActionButton onClick={() => setParsed(true)} disabled={busy != null}>
                  Enter titles manually
                </ActionButton>
              </div>
            </div>
          ) : (
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              <Field label="Trumped (old) release" value={oldTitle} onChange={setOldTitle} mono />
              <Field label="Replacement (new) release" value={newTitle} onChange={setNewTitle} mono />
            </div>
          )}
        </StepShell>

        {/* Step 2 — select tracker */}
        <StepShell n={2} active={step2} done={group != null} title="Which tracker sent the PM?">
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Indexer (optional — narrows the release search)</span>
              <select
                value={indexer} onChange={e => setIndexer(e.target.value)}
                style={{ padding: '7px 10px', borderRadius: 6, border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)', fontSize: 12, minWidth: 220 }}
              >
                <option value="">Any indexer</option>
                {indexers.map(name => <option key={name} value={name}>{name}</option>)}
              </select>
            </label>
            {group == null && (
              <ActionButton primary onClick={handleResolveGroup} disabled={busy != null || !oldTitle.trim()}>
                {busy === 'group' ? 'Finding torrents…' : 'Find hardlink group →'}
              </ActionButton>
            )}
          </div>
        </StepShell>

        {/* Step 3 — confirm the group */}
        <StepShell n={3} active={step3} done={search != null} title="Confirm the hardlink group to remove">
          {group && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>
                <b style={{ color: 'var(--text)' }}>{group.torrents.length} torrent{group.torrents.length !== 1 ? 's' : ''}</b> share this content
                ({formatBytes(group.total_size)}). All of them will be removed from {clientName} <b>with their files</b> — your library hardlink
                survives until Sonarr/Radarr imports the replacement.
              </div>
              <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
                {group.torrents.map(t => (
                  <div key={t.hash} style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '8px 12px', borderBottom: '1px solid var(--border)' }}>
                    <span style={{ flex: 1, minWidth: 0, fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.name}</span>
                    <span title={t.hash} style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>{t.tracker || 'no tracker'}</span>
                    {t.seeding_time != null && <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>seeded {formatDuration(t.seeding_time)}</span>}
                    <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>{formatBytes(t.size)}</span>
                  </div>
                ))}
              </div>
              {search == null && (
                <ActionButton primary onClick={handleSearch} disabled={busy != null}>
                  {busy === 'search' ? 'Searching…' : 'Find replacement release →'}
                </ActionButton>
              )}
            </div>
          )}
        </StepShell>

        {/* Step 4 — replacement release */}
        <StepShell n={4} active={step4 && search != null} done={result != null} title="Replacement release">
          {search && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {search.release ? (
                <div style={{ border: `1px solid ${ACCENT}40`, borderRadius: 8, padding: '10px 12px', background: `${ACCENT}08` }}>
                  <div style={{ fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text)', wordBreak: 'break-all' }}>{search.release.title}</div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 6, flexWrap: 'wrap', fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
                    <QualityChip label={search.release.quality_name} hdr={search.release.hdr} />
                    <span>{search.release.indexer}</span>
                    <span>{formatBytes(search.release.size)}</span>
                    <span style={{ color: search.release.seeders > 0 ? 'var(--green)' : 'var(--red)' }}>{search.release.seeders} seeders</span>
                  </div>
                </div>
              ) : (
                <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
                  No exact match found in {search.candidates ?? 0} release{search.candidates !== 1 ? 's' : ''}.
                  {search.fallback_url && (
                    <> Grab it manually in <a href={search.fallback_url} target="_blank" rel="noopener noreferrer" style={{ color: ACCENT }}>Sonarr/Radarr ↗</a>, then run the removal below.</>
                  )}
                </div>
              )}
            </div>
          )}
        </StepShell>

        {/* Step 5 — execute */}
        <StepShell n={5} active={step5 && result == null} done={result != null} title="Remove the group & grab the replacement">
          {result ? (
            <div style={{ fontSize: 12, color: 'var(--text)', lineHeight: 1.7 }}>
              ✓ Removed <b>{result.removed}</b> torrent{result.removed !== 1 ? 's' : ''} and their files.{' '}
              {result.grabbed === true && <>Replacement grabbed — Sonarr/Radarr will import and re-hardlink it.</>}
              {result.grabbed === false && <span style={{ color: 'var(--red)' }}>Grab failed: {result.grab_error}. Grab manually in the arr.</span>}
              {result.grabbed == null && <>No release was grabbed (grab it manually if you haven't).</>}
              {result.rescan_started && <div style={{ color: 'var(--text-dim)', marginTop: 4 }}>A re-audit is running; the dashboard will converge once the import completes.</div>}
            </div>
          ) : step5 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {!clientDeleteAllowed && (
                <div style={{ padding: '10px 14px', background: 'var(--yellow)10', border: '1px solid var(--yellow)30', borderRadius: 8, color: 'var(--yellow)', fontSize: 12 }}>
                  Client deletion is disabled. Enable “Workflow torrent deletion” in <a onClick={() => onNavigate && onNavigate({ tab: 'config' })} style={{ color: 'var(--yellow)', cursor: 'pointer', textDecoration: 'underline' }}>Config → Torrent Source</a> to run this step.
                </div>
              )}
              <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>
                This removes <b style={{ color: 'var(--text)' }}>{group.torrents.length} torrent{group.torrents.length !== 1 ? 's' : ''}</b> ({formatBytes(group.total_size)}) from {clientName} with their files
                {search?.release ? ', then grabs the replacement.' : '.'} There is no undo.
              </div>
              <ActionButton danger onClick={handleExecute} disabled={busy != null || !clientDeleteAllowed}>
                {busy === 'execute' ? 'Executing…' : (search?.release ? 'Remove group + grab replacement' : 'Remove group')}
              </ActionButton>
            </div>
          )}
        </StepShell>
      </div>
      {busy && <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--text-dim)' }}><Spinner /> Working…</div>}
      <SpinKeyframes />
    </div>
  )
}
