import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../../api'
import { formatBytes } from '../../utils'
import { Button, Spinner, SortPicker, Checkbox, SectionLabel, MONO_TITLE, tint } from './shared'

// ── T22: Replace with this pack ──────────────────────────────────────────────
// Sonarr fills a season as it airs, from whichever group has each episode, and
// upgrades episodes one at a time, so the library ends up spread over several
// torrents. A season pack hardlinked over the lot puts it back on one. Sonarr
// won't do that itself for a pack at the same quality or lower, which is where
// this comes in: every episode of the pack goes over Sonarr's file, a lower
// one only where the library file has another hardlink (the better bytes stay
// on disk in their own torrent). The server reads all of it live and refuses
// anything it couldn't check; this dialog shows each episode first.
//
// T24 — an episode whose library file nothing else links (`unseeded`) is kept
// unless the switch below the list is on. It's the user's trade: the library
// seeded at a lower quality over unseeded at a higher one. Off by default,
// because it's the one replace that deletes a file for good.
//
// Lives here, not in Triage, because three places open it: a Triage row, the
// season-pack dialog below, and a finished download in the Import Jobs panel.

const SKIP_REASON = {
  unchecked:     "couldn't check the current file for another hardlink",
  split:         'Sonarr holds these episodes in a different split of files',
  not_in_series: 'Sonarr lists no such episode',
  unparsed:      'no episode number in the file name',
}

function lowerWord(cmp) {
  return cmp === 'lower' ? 'Lower quality' : "Quality couldn't be compared"
}

function replaceNote(e, includeUnseeded) {
  if (e.action === 'add') return 'Sonarr holds no file for this episode.'
  if (e.action === 'skip') return `${SKIP_REASON[e.reason] || e.reason}.`.replace(/^./, c => c.toUpperCase())
  const holder = e.replaces?.group ? ` (${e.replaces.group})` : ''
  if (e.action === 'unseeded') {
    return `${lowerWord(e.cmp)}, and nothing seeds the current file${holder}: it's the only copy. `
      + (includeUnseeded ? 'Replacing deletes it, and the episode gains a seed.'
                         : 'Kept unless you replace unseeded episodes below.')
  }
  if (e.action !== 'replace') return null
  if (e.cmp === 'lower' || e.cmp === 'unknown') {
    return `${lowerWord(e.cmp)}. The current file${holder} has another hardlink, so it stays on disk.`
  }
  return e.replaces && !e.replaces.linked
    ? `The current file${holder} has no other copy. Sonarr deletes it, or moves it to its recycle bin.`
    : null
}

function replaceDetail(e) {
  if (e.action === 'already') return `${e.quality} · the library file is this pack’s`
  if (e.action === 'add') return `— → ${e.quality}`
  if (e.action === 'replace' || e.action === 'unseeded') return `${e.replaces?.quality} → ${e.quality}`
  return e.replaces?.quality || e.quality
}

// Consecutive episodes that say the same thing read as one line: E03–E06.
function replaceLines(entries, includeUnseeded) {
  const lines = []
  for (const e of entries) {
    const line = { ...e, detail: replaceDetail(e), note: replaceNote(e, includeUnseeded), labels: [e.label] }
    const last = lines[lines.length - 1]
    if (last && last.action === line.action && last.detail === line.detail && last.note === line.note) {
      last.labels.push(e.label)
      continue
    }
    lines.push(line)
  }
  return lines.map(l => ({
    ...l,
    label: l.labels.length > 1
      ? `${l.labels[0].split('–')[0]}–${l.labels[l.labels.length - 1].split('–').pop()}`
      : l.label,
  }))
}

const ACTION_LOOK = {
  replace: { word: 'replace', color: 'var(--text)', weight: 700 },
  add:     { word: 'add',     color: 'var(--text)', weight: 700 },
  already: { word: 'already', color: 'var(--text-dim)', weight: 400 },
  skip:    { word: 'keep',    color: 'var(--text-dim)', weight: 400 },
}

const pad2 = n => String(n ?? '').padStart(2, '0')

// The dialog frame both modals share: portalled to <body>, because the page's
// fade-in leaves a transform that makes position:fixed resolve against the page.
function ModalFrame({ width = 600, onCancel, children }) {
  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])
  return createPortal(
    <div onClick={onCancel} style={{
      position: 'fixed', inset: 0, zIndex: 400, display: 'flex',
      alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.55)',
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        width: `min(${width}px, calc(100vw - 48px))`, maxHeight: 'calc(100vh - 96px)',
        display: 'flex', flexDirection: 'column',
        background: 'var(--surface)', border: '1px solid var(--border2)',
        borderRadius: 12, boxShadow: '0 16px 60px rgba(0,0,0,0.5)',
      }}>
        {children}
      </div>
    </div>,
    document.body
  )
}

const MONO = { fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)' }
const PROSE = { fontSize: 'var(--font-base)', color: 'var(--text-dim)', lineHeight: 1.6, margin: '10px 0 0' }
const TITLE = { fontSize: 'var(--font-lg)', fontWeight: 700, color: 'var(--text)' }

// `packs`: [{ reg, params: { hash, instance_id, connection_id, arr_id, season }, name, group,
// quality, recommended }]. `initial` is the reg the dialog opens on; `pickNote` says why the
// recommended one is. `watchId` is set when a season-pack watch opened it, so the server
// can close that watch once the replace lands.
export function ReplaceModal({ title, season, packs, initial, pickNote, clientName = 'your client', watchId, onCancel, onDone }) {
  const [chosen, setChosen] = useState(initial || packs[0]?.reg)
  const pack = packs.find(s => s.reg === chosen) || packs[0]
  const [plan, setPlan] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [includeUnseeded, setIncludeUnseeded] = useState(false)

  useEffect(() => {
    let live = true
    setPlan(null)
    setError(null)
    setIncludeUnseeded(false)
    api.triageReplacePlan(pack.params)
      .then(r => { if (live) setPlan(r) })
      .catch(e => { if (live) setError(e.message) })
    return () => { live = false }
  }, [chosen])  // eslint-disable-line react-hooks/exhaustive-deps

  const unseeded = plan ? plan.entries.filter(e => e.action === 'unseeded') : []
  const doable = plan
    ? plan.entries.filter(e => e.action === 'replace' || e.action === 'add' || (includeUnseeded && e.action === 'unseeded'))
    : []
  const episodes = doable.reduce((n, e) => n + Math.max(1, e.episodes.length), 0)
  const others = packs.filter(s => s.reg !== pack.reg)
  // The torrents that keep a replaced file's other link, by group, less the
  // other packs, which are named on their own.
  const otherGroups = new Set(others.map(s => s.group).filter(Boolean))
  const linked = doable.filter(e => e.replaces?.linked)
  const holders = [...new Set(linked.map(e => e.replaces.group).filter(Boolean))].filter(g => !otherGroups.has(g))
  const leftover = [
    ...others.map(s => s.name),
    ...(holders.length ? [`the torrents still holding the old files (${holders.join(', ')})`]
      : linked.length > 0 && others.length === 0 ? ['the torrents still holding the old files'] : []),
  ]
  const downgrades = (plan?.downgrades || 0) + (includeUnseeded ? unseeded.length : 0)
  const unseededEpisodes = unseeded.reduce((n, e) => n + Math.max(1, e.episodes.length), 0)

  const confirm = async () => {
    setBusy(true)
    setError(null)
    try {
      onDone(await api.triageReplace({
        ...pack.params, paths: doable.map(e => e.path),
        include_unseeded: includeUnseeded || undefined, watch_id: watchId || undefined,
      }), pack)
    } catch (e) {
      setError(e.message)
      setBusy(false)
    }
  }

  return (
    <ModalFrame onCancel={onCancel}>
      <div style={{ padding: '18px 20px 0' }}>
        <div style={TITLE}>Replace {title || 'this season'} · S{pad2(season)} in your library</div>
        <p style={{ ...PROSE, color: 'var(--text)' }}>
          Imports a pack’s episodes over the files {plan?.connection_name || 'Sonarr'} holds now, as hardlinks,
          so one torrent seeds the season. A file is replaced with a lower-quality one <b>only</b> where it
          has another hardlink, so nothing is lost{unseeded.length > 0 ? ', unless you also replace the unseeded episodes below' : ''}.
          Each episode below says what happens.
        </p>
        {packs.length > 1 && (
          <div style={{ margin: '12px 0 0', display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)' }}>
              {packs.length} packs of this season are in {clientName}. Keep:
            </span>
            <SortPicker label="Pack" value={chosen} onChange={v => { if (!busy) setChosen(v) }}
              options={packs.map(s => ({
                value: s.reg,
                label: s.group || 'pack',
                sub: [s.quality, s.recommended && 'recommended'].filter(Boolean).join(' · '),
              }))} />
            {pickNote && (
              <span style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)' }}>{pickNote}</span>
            )}
          </div>
        )}
      </div>

      <div style={{ margin: '14px 20px 0', border: '1px solid var(--border)', borderRadius: 8, overflowY: 'auto', flex: '0 1 auto' }}>
        {!plan && !error && (
          <div style={{ ...MONO, display: 'flex', alignItems: 'center', gap: 8, padding: '12px 14px', color: 'var(--text-dim)' }}>
            <Spinner /> Checking {clientName} and Sonarr…
          </div>
        )}
        {plan && (
          <>
            <div title={plan.release} style={{ ...MONO, padding: '8px 12px', color: 'var(--text-dim)', borderBottom: '1px solid var(--border)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {plan.release}
            </div>
            {replaceLines(plan.entries, includeUnseeded).map(e => {
              const look = e.action === 'unseeded'
                ? (includeUnseeded ? ACTION_LOOK.replace : ACTION_LOOK.skip)
                : (ACTION_LOOK[e.action] || ACTION_LOOK.skip)
              const unsure = e.action === 'skip' && e.reason === 'unchecked'
              const { note, detail } = e
              return (
                <div key={e.path} style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '7px 12px', borderBottom: '1px solid var(--border)' }}>
                  <span style={{ ...MONO, width: 76, flexShrink: 0, color: 'var(--text)' }}>{e.label}</span>
                  <span style={{ ...MONO, width: 56, flexShrink: 0, color: unsure ? 'var(--yellow)' : look.color, fontWeight: look.weight }}>
                    {look.word}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div title={e.replaces ? `${e.replaces.file} → ${e.file}` : e.file}
                      style={{ ...MONO, color: 'var(--text-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {detail}
                    </div>
                    {note && (
                      <div style={{ fontSize: 'var(--font-base)', color: unsure ? 'var(--yellow)' : 'var(--text-dim)', marginTop: 2 }}>
                        {note}
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
          </>
        )}
        {error && (
          <p style={{ fontSize: 'var(--font-base)', color: 'var(--yellow)', lineHeight: 1.5, margin: 0, padding: '12px 14px' }}>
            {error}
          </p>
        )}
      </div>

      {plan && unseeded.length > 0 && (
        <div onClick={() => { if (!busy) setIncludeUnseeded(v => !v) }}
          style={{ display: 'flex', alignItems: 'flex-start', gap: 10, margin: '12px 20px 0', cursor: busy ? 'default' : 'pointer' }}>
          <span style={{ paddingTop: 2 }}>
            <Checkbox checked={includeUnseeded} onChange={() => { if (!busy) setIncludeUnseeded(v => !v) }} />
          </span>
          <span style={{ fontSize: 'var(--font-base)', color: 'var(--text)', lineHeight: 1.5 }}>
            Also replace {unseededEpisodes} unseeded episode{unseededEpisodes !== 1 ? 's' : ''}.
            <span style={{ color: 'var(--text-dim)' }}>
              {' '}Their current files are the only copies, so Sonarr deletes them (or moves them to its
              recycle bin), and each episode gains a seed at the pack’s quality.
            </span>
          </span>
        </div>
      )}

      {plan && doable.length > 0 && (
        <p style={{ ...PROSE, margin: '12px 20px 0' }}>
          Nothing leaves {clientName}.
          {leftover.length > 0 && ` After the next scan, Triage lists what no longer supplies your library: ${leftover.join(', and ')}.`}
          {downgrades > 0 && ` If your Sonarr profile still wants the higher quality, Sonarr may upgrade ${downgrades === 1 ? 'that episode' : 'those episodes'} again.`}
        </p>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '14px 20px 18px' }}>
        {busy && (
          <span style={{ ...MONO, fontSize: 'var(--font-base)', display: 'inline-flex', alignItems: 'center', gap: 8, color: 'var(--text-dim)' }}>
            <Spinner /> Importing in Sonarr…
          </span>
        )}
        <span style={{ flex: 1 }} />
        <Button onClick={onCancel}>{busy ? 'Continue in background' : 'Cancel'}</Button>
        <Button variant="primary" onClick={confirm} disabled={busy || !plan || doable.length === 0}>
          {busy ? 'Replacing…'
            : !plan ? 'Replace'
            : doable.length ? `Replace ${episodes} episode${episodes !== 1 ? 's' : ''}`
            : 'Nothing to replace'}
        </Button>
      </div>
    </ModalFrame>
  )
}

// How a replace's answer reads as one sentence, wherever it was opened from.
export function replaceOutcome(resp) {
  const parts = []
  if (resp.replaced?.length) parts.push(`Replaced ${resp.replaced.join(', ')}`)
  if (resp.pending?.length) parts.push(`${resp.pending.join(', ')} still importing in Sonarr`)
  if (resp.dropped?.length) parts.push(`${resp.dropped.join(', ')} no longer safe to replace, left as ${resp.dropped.length === 1 ? 'it was' : 'they were'}`)
  return parts.join(' · ') || 'Nothing changed'
}

// ── T25: Find the season pack ────────────────────────────────────────────────
// At the end of a season a good tracker kills its single episodes and posts a
// pack. From Triage's dead seeds or Backfill's unseeded episodes, this looks
// for that pack in the client, else searches Sonarr for it, and grabs it. The
// download is followed in Import Jobs, and once it's in the client the replace
// above opens from there. Nothing here changes the library; the replace does,
// and only after its own confirm.

// "E01–E02 · WEBDL-2160p · seeded": the season as it stands, a line per run of
// episodes that read the same.
function libraryRuns(library) {
  const state = e => !e.has_file ? 'no file' : e.linked === true ? 'seeded' : e.linked === false ? 'unseeded' : 'not checked'
  const runs = []
  for (const e of library) {
    const key = `${e.quality}|${state(e)}`
    const last = runs[runs.length - 1]
    if (last && last.key === key && e.episode === last.to + 1) { last.to = e.episode; continue }
    runs.push({ key, from: e.episode, to: e.episode, quality: e.quality, state: state(e) })
  }
  return runs.map(r => ({
    ...r,
    label: r.from === r.to ? `E${pad2(r.from)}` : `E${pad2(r.from)}–E${pad2(r.to)}`,
  }))
}

const STATE_COLOR = { seeded: 'var(--green)', unseeded: 'var(--yellow)', 'no file': 'var(--text-dim)', 'not checked': 'var(--text-dim)' }

// "same as 4 · lower than 2" — a release against the library's episodes.
function vsLibrary(v) {
  if (!v) return ''
  return [['higher', 'higher than'], ['same', 'same as'], ['lower', 'lower than'], ['unknown', "can't compare with"]]
    .filter(([k]) => v[k] > 0)
    .map(([k, words]) => `${words} ${v[k]}`)
    .join(' · ')
}

const RELEASE_GRID = 'minmax(0,1fr) 110px 64px 44px'

export function SeasonPackModal({ target, onCancel }) {
  const params = useMemo(() => ({
    connection_id: target.connection_id, arr_id: target.arr_id, season: target.season,
  }), [target])
  const [lookup, setLookup] = useState(null)
  const [lookupError, setLookupError] = useState(null)
  const [search, setSearch] = useState({ state: 'idle', releases: [], error: null, searched: 0 })
  const [chosen, setChosen] = useState(null)
  const [grab, setGrab] = useState({ state: 'idle', error: null })
  const [replacing, setReplacing] = useState(false)
  const [replaced, setReplaced] = useState(null)
  const mounted = useRef(true)
  useEffect(() => () => { mounted.current = false }, [])

  const runSearch = useCallback(async () => {
    setSearch({ state: 'searching', releases: [], error: null, searched: 0 })
    setChosen(null)
    try {
      const r = await api.seasonPackReleases({ ...params, trackers: target.trackers || [] })
      if (!mounted.current) return r
      setSearch({ state: 'done', releases: r.releases || [], error: null, searched: r.searched || 0 })
      // Only a release on the singles' own tracker is chosen for you: that's
      // where a pack that killed them is posted. Anything else is your pick.
      const lead = (r.releases || [])[0]
      setChosen(lead?.preferred ? lead.guid : null)
      return r
    } catch (e) {
      if (mounted.current) setSearch({ state: 'error', releases: [], error: e.message, searched: 0 })
      return null
    }
  }, [params, target.trackers])

  useEffect(() => {
    api.seasonPackLookup(params)
      .then(r => {
        if (!mounted.current) return
        setLookup(r)
        // A pack already in the client needs no download, so no indexer is
        // asked unless you ask.
        if (!(r.packs || []).some(p => p.complete === true)) runSearch()
      })
      .catch(e => { if (mounted.current) setLookupError(e.message) })
  }, [params])  // eslint-disable-line react-hooks/exhaustive-deps

  const client = lookup?.client_name || 'your client'
  const ready = (lookup?.packs || []).filter(p => p.complete === true)
  const unfinished = (lookup?.packs || []).filter(p => p.complete !== true)
  const release = search.releases.find(r => r.guid === chosen)
  const title = lookup?.title || target.title || 'This series'

  const doGrab = async (force = false, retried = false, pick = release) => {
    if (!pick) return
    setGrab({ state: 'grabbing', error: null })
    try {
      await api.seasonPackGrab({
        ...params, guid: pick.guid, indexer_id: pick.indexer_id, info_hash: pick.info_hash || undefined,
        series_title: title, force: force || undefined,
      })
      if (!mounted.current) return
      setGrab({ state: 'grabbed', error: null })
      window.dispatchEvent(new CustomEvent('auditorr:import_started'))
    } catch (e) {
      if (!mounted.current) return
      if (e.code === 'already_queued') { setGrab({ state: 'queued', error: e.message }); return }
      // The one failure a fresh search fixes, retried once with the same
      // release from it (B12). Anything else is shown as it is.
      if (e.code === 'stale_release' && !retried) {
        const r = await runSearch()
        const fresh = (r?.releases || []).find(x => x.title === pick.title && x.indexer === pick.indexer)
        if (fresh) { setChosen(fresh.guid); return doGrab(force, true, fresh) }
        setGrab({ state: 'error', error: `${pick.title} is no longer listed on ${pick.indexer}` })
        return
      }
      setGrab({ state: 'error', error: e.message })
    }
  }

  const replacePacks = ready.map(p => ({
    reg: p.reg, name: p.group ? `${p.group} · ${p.quality}` : p.name, group: p.group, quality: p.quality,
    params: { hash: p.hash, instance_id: p.instance_id, ...params },
  }))

  if (replacing) {
    return (
      <ReplaceModal title={title} season={target.season} packs={replacePacks} clientName={client}
        onCancel={() => setReplacing(false)}
        onDone={resp => { setReplacing(false); setReplaced(replaceOutcome(resp)) }} />
    )
  }

  const grabbed = grab.state === 'grabbed'
  const runs = lookup ? libraryRuns(lookup.library || []) : []

  return (
    <ModalFrame width={680} onCancel={onCancel}>
      <div style={{ padding: '18px 20px 0' }}>
        <div style={TITLE}>Season pack · {title} · S{pad2(target.season)}</div>
        <p style={{ ...PROSE, color: 'var(--text)' }}>
          Puts the season on one torrent. A pack already in {client} goes straight to the replace. Otherwise
          auditorr searches Sonarr for one, you grab it, and once it has downloaded the replace opens from
          Import Jobs. Your library changes only in the replace, after you’ve seen each episode.
        </p>
      </div>

      <div style={{ padding: '0 20px', overflowY: 'auto', flex: '0 1 auto' }}>
        {lookupError && (
          <p style={{ fontSize: 'var(--font-base)', color: 'var(--yellow)', lineHeight: 1.5, margin: '14px 0 0' }}>{lookupError}</p>
        )}
        {!lookup && !lookupError && (
          <div style={{ ...MONO, display: 'flex', alignItems: 'center', gap: 8, padding: '14px 0 0', color: 'var(--text-dim)' }}>
            <Spinner /> Checking Sonarr and {client}…
          </div>
        )}

        {lookup && (
          <>
            <div style={{ margin: '16px 0 6px' }}><SectionLabel>In your library</SectionLabel></div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
              {runs.length === 0 && <span style={{ ...MONO, color: 'var(--text-dim)' }}>Sonarr lists no episodes in this season.</span>}
              {runs.map(r => (
                <div key={r.label} style={{ ...MONO, display: 'flex', gap: 10 }}>
                  <span style={{ width: 76, flexShrink: 0, color: 'var(--text)' }}>{r.label}</span>
                  <span style={{ width: 120, flexShrink: 0, color: 'var(--text-dim)' }}>{r.quality || '—'}</span>
                  <span style={{ color: STATE_COLOR[r.state] }}>{r.state}</span>
                </div>
              ))}
            </div>

            <div style={{ margin: '18px 0 6px' }}><SectionLabel>In {client}</SectionLabel></div>
            {ready.length === 0 && unfinished.length === 0 && (
              <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: 0 }}>
                No pack of this season is in {client}.
                {!lookup.client_checked && ` An instance didn't answer, so one may be there.`}
              </p>
            )}
            {[...ready, ...unfinished].map(p => (
              <div key={p.reg} style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '3px 0' }}>
                <span title={p.name} style={{ ...MONO_TITLE, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.name}</span>
                <span style={{ ...MONO, color: 'var(--text-dim)', flexShrink: 0 }}>{formatBytes(p.size)}</span>
                <span style={{ ...MONO, flexShrink: 0, color: p.complete === true ? 'var(--green)' : 'var(--text-dim)' }}>
                  {p.complete === true ? 'complete' : p.complete === false ? 'downloading' : 'completion unknown'}
                </span>
              </div>
            ))}
            {replaced && (
              <p style={{ fontSize: 'var(--font-base)', color: 'var(--text)', margin: '8px 0 0' }}>{replaced}</p>
            )}

            <div style={{ margin: '18px 0 6px', display: 'flex', alignItems: 'center', gap: 10 }}>
              <SectionLabel>Search Sonarr</SectionLabel>
              {ready.length > 0 && search.state === 'idle' && (
                <Button size="sm" variant="ghost" onClick={runSearch}>Search indexers anyway</Button>
              )}
            </div>
            {search.state === 'searching' && (
              <div style={{ ...MONO, display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text-dim)' }}>
                <Spinner /> Searching Sonarr’s indexers for a season {target.season} pack…
              </div>
            )}
            {search.state === 'error' && (
              <p style={{ fontSize: 'var(--font-base)', color: 'var(--yellow)', margin: 0 }}>
                {search.error} <Button size="sm" variant="ghost" onClick={runSearch}>Retry</Button>
              </p>
            )}
            {search.state === 'done' && search.releases.length === 0 && (
              <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: 0 }}>
                No season pack found{search.searched ? `: Sonarr returned ${search.searched} release${search.searched !== 1 ? 's' : ''}, none of them a full season ${target.season}` : ''}.
              </p>
            )}
            {search.state === 'done' && search.releases.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3, paddingBottom: 4 }}>
                <div style={{ display: 'grid', gridTemplateColumns: RELEASE_GRID, gap: 8, padding: '2px 10px' }}>
                  {['Release', 'Tracker', 'Size', 'Peers'].map((h, i) => (
                    <span key={h} style={{ fontSize: 'var(--font-sm)', fontWeight: 600, color: 'var(--text-dim)', textAlign: i >= 2 ? 'right' : 'left' }}>{h}</span>
                  ))}
                </div>
                {search.releases.map(r => {
                  const on = r.guid === chosen
                  return (
                    <div key={r.guid} onClick={() => { if (!grabbed) setChosen(r.guid) }}
                      style={{
                        display: 'grid', gridTemplateColumns: RELEASE_GRID, gap: 8, alignItems: 'baseline',
                        padding: '6px 10px', borderRadius: 6, cursor: grabbed ? 'default' : 'pointer',
                        background: on ? tint('var(--accent)', 6) : 'transparent',
                        border: `1px solid ${on ? tint('var(--accent)', 35) : 'transparent'}`,
                      }}>
                      <div style={{ minWidth: 0 }}>
                        <div title={r.title} style={{ ...MONO_TITLE, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.title}</div>
                        <div style={{ ...MONO, color: 'var(--text-dim)', marginTop: 2 }}>
                          {[r.quality_name, vsLibrary(r.vs_library) && `${vsLibrary(r.vs_library)} in your library`]
                            .filter(Boolean).join(' · ')}
                        </div>
                        {r.preferred && (
                          <div style={{ fontSize: 'var(--font-sm)', color: 'var(--text)', marginTop: 2 }}>
                            On the tracker your episodes came from
                          </div>
                        )}
                      </div>
                      <span title={r.indexer}
                        style={{ ...MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: r.preferred ? 'var(--text)' : 'var(--text-dim)' }}>
                        {r.indexer}
                      </span>
                      <span style={{ ...MONO, color: 'var(--text-dim)', textAlign: 'right' }}>{formatBytes(r.size)}</span>
                      <span style={{ ...MONO, textAlign: 'right', color: r.seeders > 0 ? 'var(--green)' : 'var(--text-dim)' }}>
                        {r.seeders != null ? `${r.seeders}S` : '—'}
                      </span>
                    </div>
                  )
                })}
              </div>
            )}
          </>
        )}
      </div>

      {(grab.state === 'error' || grab.state === 'queued') && (
        <p style={{ fontSize: 'var(--font-base)', color: 'var(--yellow)', lineHeight: 1.5, margin: '12px 20px 0' }}>{grab.error}</p>
      )}
      {grabbed && (
        <p style={{ fontSize: 'var(--font-base)', color: 'var(--text)', lineHeight: 1.6, margin: '12px 20px 0' }}>
          Grabbed. auditorr follows the download in Import Jobs. When it’s in {client}, the job there
          opens the replace. If Sonarr imports the pack itself, because it’s an upgrade, there’s nothing left to do.
        </p>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '14px 20px 18px' }}>
        <span style={{ flex: 1 }} />
        <Button onClick={onCancel}>{grabbed || replaced ? 'Close' : 'Cancel'}</Button>
        {ready.length > 0 && !replaced && (
          <Button variant="primary" onClick={() => setReplacing(true)}>
            Replace with {ready.length === 1 ? 'this pack' : 'a pack'}…
          </Button>
        )}
        {grab.state === 'queued' && (
          <Button onClick={() => doGrab(true)}>Grab anyway</Button>
        )}
        {search.state === 'done' && search.releases.length > 0 && !grabbed && grab.state !== 'queued' && (
          <Button variant={ready.length > 0 && !replaced ? 'secondary' : 'primary'}
            onClick={() => doGrab(false)} disabled={!release || grab.state === 'grabbing'}>
            {grab.state === 'grabbing' ? 'Grabbing…' : release ? 'Grab this pack' : 'Choose a pack'}
          </Button>
        )}
      </div>
    </ModalFrame>
  )
}
