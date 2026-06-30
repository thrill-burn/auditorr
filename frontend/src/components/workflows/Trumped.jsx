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
          padding: '7px 10px', borderRadius: 'var(--r)', border: '1px solid var(--border2)',
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

// ── Match feedback ────────────────────────────────────────────────────────────
// Hue as text only (never a fill): green agrees with the PM, red differs, amber
// is a partial title overlap. Lets the user see at a glance exactly why a
// candidate ranks where it does before committing to a delete or a grab.
const MATCH_FIELDS = [['res', 'RES'], ['source', 'SRC'], ['audio', 'AUD'], ['hdr', 'HDR'], ['group', 'GRP']]
const MATCH_COLOR  = { same: 'var(--green)', diff: 'var(--red)', partial: 'var(--yellow)' }
const MATCH_MARK   = { same: '✓', diff: '✗', partial: '~' }

function MatchChips({ match }) {
  if (!match) return null
  const items = MATCH_FIELDS.filter(([k]) => match[k])
  if (!items.length) return null
  return (
    <span style={{ display: 'inline-flex', gap: 7, flexShrink: 0 }}>
      {items.map(([k, label]) => (
        <span key={k} title={`${label}: ${match[k]}`} style={{
          fontSize: 9, fontFamily: 'var(--mono)', fontWeight: 700, letterSpacing: 0.3,
          color: MATCH_COLOR[match[k]] || 'var(--text-dim)',
        }}>{label}{MATCH_MARK[match[k]] || ''}</span>
      ))}
    </span>
  )
}

function ScoreBadge({ score }) {
  if (score == null) return null
  const pct = Math.round(score * 100)
  const color = score >= 0.8 ? 'var(--green)' : score >= 0.5 ? 'var(--text-dim)' : 'var(--red)'
  return <span style={{ fontSize: 10, fontFamily: 'var(--mono)', fontWeight: 700, color, flexShrink: 0, width: 34, textAlign: 'right' }}>{pct}%</span>
}

// A selectable candidate row — works for both client torrents (name/tracker) and
// arr releases (title/indexer/seeders/quality).
function CandidateRow({ cand, selected, onSelect }) {
  const name = cand.name || cand.title || ''
  const sub  = cand.tracker || cand.indexer || ''
  return (
    <div onClick={onSelect} style={{
      display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', cursor: 'pointer',
      borderBottom: '1px solid var(--border)',
      background: selected ? `${ACCENT}0e` : 'transparent',
      borderLeft: `2px solid ${selected ? ACCENT : 'transparent'}`,
    }}>
      <span style={{ width: 13, height: 13, borderRadius: '50%', flexShrink: 0, border: `1.5px solid ${selected ? ACCENT : 'var(--border2)'}`, background: selected ? ACCENT : 'transparent' }} />
      <span style={{ flex: 1, minWidth: 0, fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
      {cand.quality_name && <QualityChip label={cand.quality_name} hdr={cand.hdr} />}
      <MatchChips match={cand.match} />
      {sub && <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>{sub}</span>}
      {cand.seeders != null && <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: cand.seeders > 0 ? 'var(--green)' : 'var(--red)', flexShrink: 0 }}>{cand.seeders}S</span>}
      <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>{formatBytes(cand.size)}</span>
      <ScoreBadge score={cand.match_score} />
    </div>
  )
}

function NoneRow({ selected, onSelect, label }) {
  return (
    <div onClick={onSelect} style={{
      display: 'flex', alignItems: 'center', gap: 10, padding: '7px 12px', cursor: 'pointer',
      background: selected ? 'var(--surface3)' : 'transparent',
      borderLeft: `2px solid ${selected ? 'var(--text-dim)' : 'transparent'}`,
    }}>
      <span style={{ width: 13, height: 13, borderRadius: '50%', flexShrink: 0, border: `1.5px solid ${selected ? 'var(--text-dim)' : 'var(--border2)'}`, background: selected ? 'var(--text-dim)' : 'transparent' }} />
      <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>{label}</span>
    </div>
  )
}

export default function Trumped({ onNavigate }) {
  const toast = useToast()

  const [pmText, setPmText]       = useState('')
  const [oldTitles, setOldTitles] = useState([])   // one per trumped release (a season pack lists many)
  const [newTitle, setNewTitle]   = useState('')
  const [parsed, setParsed]       = useState(false)

  const [indexers, setIndexers] = useState([])
  const [indexer, setIndexer]   = useState('')

  const [picks, setPicks]       = useState(null)   // phase 1: [{title, auto, candidates:[…]}]
  const [selected, setSelected] = useState({})     // title -> chosen hash | null
  const [group, setGroup]       = useState(null)   // phase 2: {torrents, total_size, matched_name}
  const [search, setSearch]     = useState(null)   // search_release response
  const [chosenRelease, setChosenRelease] = useState(null)
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
    setPmText(''); setOldTitles([]); setNewTitle(''); setParsed(false)
    setIndexer(''); setPicks(null); setSelected({}); setGroup(null)
    setSearch(null); setChosenRelease(null); setError(null); setResult(null)
  }, [])

  const handleParse = async () => {
    setBusy('parse'); setError(null)
    try {
      const r = await api.trumpParse(pmText)
      setOldTitles(r.old_titles || []); setNewTitle(r.new_title); setParsed(true)
    } catch (e) {
      // Parse failure: drop into manual entry rather than blocking
      setParsed(true)
      toast(e.message, 'info')
    }
    setBusy(null)
  }

  // Phase 1 — fetch the ranked candidate torrents for each trumped title.
  const handleFindTorrents = async () => {
    setBusy('group'); setError(null)
    try {
      const r = await api.trumpResolveGroup(oldTitles.filter(t => t.trim()))
      setPicks(r.picks || [])
      const sel = {}
      ;(r.picks || []).forEach(p => { sel[p.title] = p.auto })
      setSelected(sel)
    } catch (e) {
      setError(e.message)
    }
    setBusy(null)
  }

  // Phase 2 — expand the confirmed seeds into their full cross-seed group.
  const handleExpandGroup = async () => {
    setBusy('group'); setError(null)
    try {
      const hashes = Object.values(selected).filter(Boolean)
      setGroup(await api.trumpResolveGroup(oldTitles.filter(t => t.trim()), hashes))
    } catch (e) {
      setError(e.message)
    }
    setBusy(null)
  }

  const handleSearch = async () => {
    setBusy('search'); setError(null)
    try {
      const r = await api.trumpSearchRelease({ new_title: newTitle, indexer })
      setSearch(r)
      setChosenRelease(r.release || (r.candidates && r.candidates[0]) || null)
    } catch (e) {
      setSearch(e.fallback_url ? { release: null, candidates: [], fallback_url: e.fallback_url, error: e.message } : null)
      setChosenRelease(null)
      setError(e.message)
    }
    setBusy(null)
  }

  const handleExecute = async () => {
    setBusy('execute'); setError(null)
    try {
      const r = await api.trumpExecute({
        hashes: group.torrents.map(t => ({ hash: t.hash, instance_id: t.instance_id })),
        release: chosenRelease ? {
          guid: chosenRelease.guid,
          indexer_id: chosenRelease.indexer_id,
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

  // Releases to choose from: ranked candidates, with the exact auto-match pinned
  // to the top if the ranker didn't already include it.
  const releaseList = (() => {
    if (!search) return []
    const list = [...(search.candidates || [])]
    if (search.release && !list.some(c => c.guid === search.release.guid)) list.unshift(search.release)
    return list
  })()
  const skippedTitles = picks ? picks.filter(p => !selected[p.title]).map(p => p.title) : []

  // Step gating
  const step2 = parsed
  const step3 = parsed && picks != null
  const step4 = step3 && search != null
  const step5 = step4

  return (
    <div className="fade-in" style={{ padding: '28px 28px 48px', display: 'flex', flexDirection: 'column', gap: 22, maxWidth: 980 }}>
      <WorkflowHeader
        title="Trumped"
        accent={ACCENT}
        blurb="When a tracker trumps one of your releases, paste the PM here: auditorr finds the whole hardlink group (every cross-seed), removes it from the client with its files, and grabs the replacement through Sonarr/Radarr — the manual multi-step swap, automated and confirmed at every step."
        right={(parsed || picks) && (
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
                  width: '100%', boxSizing: 'border-box', padding: '10px 12px', borderRadius: 'var(--r)',
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
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                  Trumped (old) release{oldTitles.length > 1 ? `s — ${oldTitles.length} torrents, one per line` : ' — one per line'}
                </span>
                <textarea
                  value={oldTitles.join('\n')}
                  onChange={e => setOldTitles(e.target.value.split('\n'))}
                  rows={Math.min(Math.max(oldTitles.length, 1), 12)}
                  style={{
                    width: '100%', boxSizing: 'border-box', padding: '7px 10px', borderRadius: 'var(--r)',
                    border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)',
                    fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 1.5, resize: 'vertical',
                  }}
                />
              </label>
              <Field label="Replacement (new) release" value={newTitle} onChange={setNewTitle} mono />
            </div>
          )}
        </StepShell>

        {/* Step 2 — select tracker */}
        <StepShell n={2} active={step2} done={picks != null} title="Which tracker sent the PM?">
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Indexer (optional — narrows the release search)</span>
              <select
                value={indexer} onChange={e => setIndexer(e.target.value)}
                style={{ padding: '7px 10px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text)', fontSize: 12, minWidth: 220 }}
              >
                <option value="">Any indexer</option>
                {indexers.map(name => <option key={name} value={name}>{name}</option>)}
              </select>
            </label>
            {picks == null && (
              <ActionButton primary onClick={handleFindTorrents} disabled={busy != null || !oldTitles.some(t => t.trim())}>
                {busy === 'group' ? 'Finding torrents…' : 'Find matching torrents →'}
              </ActionButton>
            )}
          </div>
        </StepShell>

        {/* Step 3 — pick the torrents, then confirm the expanded group */}
        <StepShell n={3} active={step3} done={search != null} title="Confirm the hardlink group to remove">
          {picks && !group && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
                Pick the torrent that matches each trumped release — the best match is pre-selected. Once you confirm, every cross-seed of the chosen torrents is added automatically.
              </div>
              {picks.map(p => (
                <div key={p.title} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <div style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-dim)', wordBreak: 'break-all' }}>{p.title}</div>
                  {p.candidates.length === 0 ? (
                    <div style={{ fontSize: 11, color: 'var(--yellow)' }}>No match found in {clientName} — this release will be skipped.</div>
                  ) : (
                    <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
                      {p.candidates.map(c => (
                        <CandidateRow key={c.hash} cand={c}
                          selected={selected[p.title] === c.hash}
                          onSelect={() => setSelected(s => ({ ...s, [p.title]: c.hash }))} />
                      ))}
                      <NoneRow label="None of these — skip this release"
                        selected={selected[p.title] == null}
                        onSelect={() => setSelected(s => ({ ...s, [p.title]: null }))} />
                    </div>
                  )}
                </div>
              ))}
              <ActionButton primary onClick={handleExpandGroup} disabled={busy != null || !Object.values(selected).some(Boolean)}>
                {busy === 'group' ? 'Expanding…' : 'Confirm & find cross-seeds →'}
              </ActionButton>
            </div>
          )}

          {group && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>
                <b style={{ color: 'var(--text)' }}>{group.torrents.length} torrent{group.torrents.length !== 1 ? 's' : ''}</b>
                {' '}(the selected releases plus every cross-seed, {formatBytes(group.total_size)}) will be removed from {clientName} <b>with their files</b> — your library hardlinks survive until Sonarr/Radarr imports the replacement.
              </div>
              {skippedTitles.length > 0 && (
                <div style={{ fontSize: 11, color: 'var(--yellow)', background: 'var(--yellow)10', border: '1px solid var(--yellow)30', borderRadius: 8, padding: '8px 12px', lineHeight: 1.6 }}>
                  Skipped (no torrent selected):
                  <div style={{ fontFamily: 'var(--mono)', color: 'var(--text-dim)', marginTop: 4 }}>
                    {skippedTitles.map(t => <div key={t}>{t}</div>)}
                  </div>
                </div>
              )}
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
                <div style={{ display: 'flex', gap: 8 }}>
                  <ActionButton primary onClick={handleSearch} disabled={busy != null}>
                    {busy === 'search' ? 'Searching…' : 'Find replacement release →'}
                  </ActionButton>
                  <button onClick={() => setGroup(null)} disabled={busy != null} style={{ fontSize: 12, padding: '6px 14px', borderRadius: 7, cursor: 'pointer', border: '1px solid var(--border2)', background: 'var(--surface2)', color: 'var(--text-dim)' }}>
                    ← Change selection
                  </button>
                </div>
              )}
            </div>
          )}
        </StepShell>

        {/* Step 4 — replacement release */}
        <StepShell n={4} active={step4} done={result != null} title="Replacement release">
          {search && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
                {search.release
                  ? <>Exact match found — confirm it below, or pick another release.</>
                  : <>No exact-title match in {search.candidate_count ?? 0} release{search.candidate_count !== 1 ? 's' : ''}. Pick the closest{search.fallback_url && <>, or grab it manually in <a href={search.fallback_url} target="_blank" rel="noopener noreferrer" style={{ color: ACCENT }}>Sonarr/Radarr ↗</a></>}.</>}
              </div>
              {releaseList.length > 0 ? (
                <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
                  {releaseList.map(r => (
                    <CandidateRow key={r.guid} cand={r}
                      selected={chosenRelease?.guid === r.guid}
                      onSelect={() => setChosenRelease(r)} />
                  ))}
                  <NoneRow label="Don't grab — I'll handle the replacement myself"
                    selected={chosenRelease == null}
                    onSelect={() => setChosenRelease(null)} />
                </div>
              ) : (
                <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>
                  No releases returned. {search.fallback_url && <>Grab manually in <a href={search.fallback_url} target="_blank" rel="noopener noreferrer" style={{ color: ACCENT }}>Sonarr/Radarr ↗</a>, then run the removal below.</>}
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
                {chosenRelease ? ', then grabs the replacement.' : '.'} There is no undo.
              </div>
              <ActionButton danger onClick={handleExecute} disabled={busy != null || !clientDeleteAllowed}>
                {busy === 'execute' ? 'Executing…' : (chosenRelease ? 'Remove group + grab replacement' : 'Remove group')}
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
