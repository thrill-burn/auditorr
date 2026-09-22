import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { api } from '../../api'
import { formatBytes } from '../../utils'
import { WATCH_ACTIVE, watchColor } from '../ImportProgress'
import {
  LabeledChips, IndexerChips, FolderChips, SortPicker, CountPicker,
  SectionLabel, WorkflowPage, WorkflowHeader, SpinKeyframes, Spinner, LoadingRow, WorkflowError, ArrErrorsWarning,
  Button, MatchChips, MATCH_COLOR, ITEM_TITLE, tint,
  QUALITY_RES_OPTIONS, QUALITY_SOURCE_OPTIONS, HDR_OPTIONS, HDR_STYLE,
  useAuditComplete,
} from './shared'

// ── Helpers ───────────────────────────────────────────────────────────────────
// Candidates arrive grouped, scoped and labelled with their root folder by the
// server (`_resolve_backfill`), from the same computation the search runs on.
// There is deliberately no grouping on this side any more (B7b): whether a
// Sonarr season is searched as one pack or as separate episodes turns on how
// many files the arr holds in that season, which this page never sees — so a
// copy here could only ever disagree with the search it is counting (B1).

const pad2 = n => String(n).padStart(2, '0')

// B6 — candidates whose grab the server accepted, by their stable key
// (`_resolve_backfill`'s `key`), until the audit lands. Module-level for the
// reason Triage's and Cleanup's `DISMISSED` sets are: the list is re-fetched
// from the last audit on every visit, so a grabbed candidate would otherwise come
// back on navigating away and returning. The keys also ride the next Generate
// request, because that run builds its candidates server-side and would search
// the grabbed one again. Cleared on `audit_complete`, when the server's own
// answer is correct — a scan that lands before the import can list it again, and
// B12's queue check then refuses the second grab.
const GRABBED = new Set()

// How a row says what became of its grab. Only `done` is an import auditorr
// watched happen; the rest are the other ways a watch can end (Phase 12, S07).
const IMPORT_LABEL = {
  queued: 'Queued…', downloading: 'Downloading…', importing: 'Importing…',
  done: '✓ Imported', error: 'Import failed', failed: 'Download failed',
  unreadable: 'Could not check', unobserved: 'Never queued',
  no_new_file: 'No new file', timed_out: 'Still downloading', unconfirmed: 'Unconfirmed',
}

// What the row searches for, said on the row. A pack and an episode are two
// different actions and must not share a label: "S01 · 10 ep" used to mean
// "ten unseeded episodes" and read as "a season pack" whichever it was.
function scopeLabel(item) {
  if (item.arr_service !== 'sonarr') return ''
  const s = item.season_number != null ? `S${pad2(item.season_number)}` : ''
  if (item.scope === 'season') return ` · ${s || 'season'} season pack · ${item.file_count} ep`
  const eps = (item.episode_numbers || []).map(e => `E${pad2(e)}`).join('')
  if (s && eps) return ` · ${s}${eps}`
  if (s) return ` · ${s} episode`
  return ' · episode'
}

// Why an episode row was not searched as part of its season.
function scopeReason(item) {
  if (item.scope !== 'episode' || item.season_files_held == null) return ''
  const seeded = item.season_files_held - (item.season_files_unseeded ?? 0)
  return seeded > 0
    ? `Searched on its own: ${seeded} of ${item.season_files_held} files in this season already have a torrent, and a season pack would replace them.`
    : 'Searched on its own: Sonarr has not numbered every file of this series, so no season can be shown to be fully unseeded.'
}

function kindsLine(groups) {
  const n = { season: 0, episode: 0, movie: 0 }
  for (const g of groups) n[g.scope] = (n[g.scope] || 0) + 1
  const parts = []
  if (n.season)  parts.push(`${n.season} season pack${n.season !== 1 ? 's' : ''}`)
  if (n.episode) parts.push(`${n.episode} single episode${n.episode !== 1 ? 's' : ''}`)
  if (n.movie)   parts.push(`${n.movie} movie${n.movie !== 1 ? 's' : ''}`)
  return parts.join(', ')
}

// The library-resolution readout, said with its noun in front of the numbers.
//
// Kept as its own sentence rather than folded into the candidate count beside
// it, because the two are not the same unit and cannot be divided into each
// other: a candidate is a *group* (one Sonarr season pack is one candidate
// however many episodes it holds) while both of these are *files*. Printed as
// "15 searchable candidates (2612 total unseeded)", that read as a catastrophic
// resolution gap on a healthy library and cost a real issue (#22) a round of
// back-and-forth to rule out.
//
// It also describes the whole library, where the candidate count narrows with
// the folder and title filters — another reason the two must not share a
// sentence that implies one is a fraction of the other.
function matchedFilesLine(matched, total) {
  if (!total) return ''
  const t = total.toLocaleString()
  if (matched <= 0)    return `None of your ${t} unseeded files matched your Sonarr/Radarr library.`
  if (matched >= total) return `All ${t} unseeded files matched your Sonarr/Radarr library.`
  return `Of ${t} unseeded files, ${matched.toLocaleString()} matched your Sonarr/Radarr library.`
}

// B9 — the files that did not match, split by what they are. The media walk
// indexes every file, so the gap always includes subtitles, artwork and .nfo
// files no arr will ever match, and that is fine. A *video* that did not match
// is the number worth acting on — a path-mapping mismatch is the usual reason —
// and it used to be buried in one undifferentiated count. Its own sentence, for
// the same reason as the one above.
//
// Which cause it names depends on how much of the library matched. A path
// mapping breaks every file on an instance, so it is the likely explanation only
// when matches are rare. Where most of the library matched, the unmatched videos
// are files the arr does not track at those paths. The first cut blamed a path
// mapping either way, and the reference box answered it: 797 files matched, and
// the unmatched videos sat together in one folder.
function unmatchedFilesLine(matched, total, video) {
  if (video == null || total - matched <= 0) return ''
  const other = total - matched - video
  const parts = []
  if (other === 1) parts.push('1 is a subtitle, artwork or other non-video file, which no arr indexes.')
  else if (other > 1) parts.push(`${other.toLocaleString()} are subtitles, artwork and other non-video files, which no arr indexes.`)
  if (video > 0) {
    const are = video === 1 ? '1 is a video file' : `${video.toLocaleString()} are video files`
    parts.push(matched < video
      ? `${are} that did not match — usually a path-mapping mismatch.`
      : `${are} Sonarr/Radarr ${video === 1 ? "doesn't" : "don't"} track at ${video === 1 ? 'that path' : 'those paths'} — a title they don't manage, or manage somewhere else.`)
  }
  return parts.join(' ')
}

function groupByFolder(candidates) {
  const map = {}
  for (const c of candidates) {
    const folder = c.folder || 'Other'
    if (!map[folder]) map[folder] = []
    map[folder].push(c)
  }
  return Object.entries(map).sort(([a], [b]) => a.localeCompare(b))
}

// ── Sort options ──────────────────────────────────────────────────────────────
const SORT_OPTIONS = [
  { value: 'largest',  label: 'Largest',    sub: 'biggest files first' },
  { value: 'smallest', label: 'Smallest',   sub: 'quickest wins first' },
  { value: 'random',   label: 'Random',     sub: 'shuffle the queue'   },
  { value: 'alpha',    label: 'A → Z',      sub: 'alphabetical'        },
]

// How each candidate's releases are ranked (B3). The arr's own order answers
// "what is the best copy of this?" — the upgrade question. Backfill asks "which
// of these is the file I already have?", so that is the default, and upgrading
// while backfilling is something you pick on purpose rather than get by accident.
const RANK_OPTIONS = [
  { value: 'closest', label: 'Closest to my file',       sub: 'size, then quality'  },
  { value: 'upgrade', label: 'Best available (upgrade)', sub: "the arr's own order" },
]

// ── Closeness to the file on disk (B3) ────────────────────────────────────────
// Hue as text only, never a fill — the treatment Trumped gives its candidates
// for the same question: is this the release I already have? (`MatchChips`,
// shared with Trumped, which asks its own fields.)
const MATCH_FIELDS = [['size', 'SIZE'], ['quality', 'QUAL'], ['hdr', 'HDR']]

function sizeDelta(delta) {
  if (delta == null) return '—'
  if (delta === 0) return '= exact'
  return `${delta > 0 ? '+' : '−'}${formatBytes(Math.abs(delta))}`
}

// ── Grab button (shared) ──────────────────────────────────────────────────────
// The idle Grab is a row chip in the accent; every state after it is a tag in
// the same slot at the same size (R8), and the two that take a second click —
// "anyway" and "Failed ↺" — are chips again.
function GrabButton({ state, onGrab, onReset, onForce, errorMsg }) {
  if (state === 'idle')        return <Button size="chip" tone="var(--accent)" onClick={onGrab}>Grab</Button>
  if (state === 'grabbing')    return <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', fontFamily: 'var(--mono)' }}>Grabbing…</span>
  if (state === 'refreshing')  return <span style={{ fontSize: 'var(--font-sm)', color: 'var(--accent)', fontFamily: 'var(--mono)' }}>Re-searching…</span>
  if (state === 'grabbed')     return <span style={{ fontSize: 'var(--font-sm)', color: 'var(--green)', fontFamily: 'var(--mono)', padding: '2px 8px' }}>✓ Grabbed</span>
  // Already in the arr's queue (B12): a second grab would download it twice, so
  // it takes a deliberate click.
  if (state === 'queued') return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span title={errorMsg} style={{ fontSize: 'var(--font-sm)', color: 'var(--yellow)', fontFamily: 'var(--mono)' }}>In queue</span>
      <Button size="chip" variant="ghost" onClick={onForce} title="Already downloading — grab another copy anyway">anyway</Button>
    </span>
  )
  return <Button size="chip" tone="var(--red)" onClick={onReset} title={`${errorMsg || 'Grab failed'} — click to reset`}>Failed ↺</Button>
}

// ── Result item ───────────────────────────────────────────────────────────────
const RELEASE_GRID = 'minmax(0,1fr) 100px 62px 72px 40px 90px 40px 44px 84px 72px'

function ResultItem({ item }) {
  // grabStates: { [guid]: 'idle' | 'grabbing' | 'refreshing' | 'grabbed' | 'queued' | 'error' }
  const [grabStates,    setGrabStates]    = useState({})
  const [grabErrors,    setGrabErrors]    = useState({})
  const [importStatus,  setImportStatus]  = useState(null)   // null | watching | importing | done | error
  const [importMessage, setImportMessage] = useState(null)
  const importPollRef    = useRef(null)
  const importStartedRef = useRef(false)
  const mountedRef       = useRef(true)
  const refreshPollRef   = useRef(null)

  useEffect(() => () => {
    mountedRef.current = false
    clearTimeout(importPollRef.current)
    clearTimeout(refreshPollRef.current)
  }, [])

  const setGrab = useCallback((key, state, message) => {
    setGrabStates(s => ({ ...s, [key]: state }))
    if (message !== undefined) setGrabErrors(s => ({ ...s, [key]: message }))
  }, [])

  const startImportWatch = useCallback(async (release) => {
    if (importStartedRef.current || !item.arr_id) return
    importStartedRef.current = true
    try {
      const resp = await api.watchImport({
        service:       item.arr_service,
        connection_id: item.arr_connection_id,
        arr_id:        item.arr_id,
        title:         item.arr_title || '',
        // Matchmaker (Rounds) counts files, and a season pack is one grab with
        // a dozen episodes behind it. Credited server-side when this call is
        // accepted — the watch it starts is a helper, not the scorekeeper.
        files:         item.file_count || 1,
        // The arr's ids for the library files this row is about. The watch
        // scopes a force import to their episodes, so a download can never be
        // forced in over files other torrents are hardlinked to (B11).
        file_ids:      item.file_ids || [],
        // The grabbed release's info hash, when its indexer gave one. It is how
        // the watch tells this download from another of the same title in the
        // arr's queue — the grab's own answer carries no download id (S07).
        info_hash:     release?.info_hash || undefined,
      })
      window.dispatchEvent(new CustomEvent('auditorr:import_started'))
      if (!mountedRef.current) return
      setImportStatus('queued')
      const poll = async () => {
        try {
          const data = await api.watchImportStatus(resp.job_id)
          if (!mountedRef.current) return
          setImportStatus(data.status)
          setImportMessage(data.message)
          if (WATCH_ACTIVE.includes(data.status)) {
            importPollRef.current = setTimeout(poll, 3000)
          }
        } catch (_) {}
      }
      poll()
    } catch (err) {
      if (mountedRef.current) {
        setImportStatus('error')
        setImportMessage(err.message)
      }
    }
  }, [item])

  // What the grab — and the queue check in front of it — is told about this row.
  const grabBody = useCallback((release, force) => ({
    service:       item.arr_service,
    connection_id: item.arr_connection_id,
    guid:          release.guid,
    indexer_id:    release.indexer_id,
    arr_id:        item.arr_id,
    episode_ids:   item.search?.episode_id ? [item.search.episode_id] : undefined,
    season_number: item.scope === 'season' ? item.season_number : undefined,
    force:         force || undefined,
  }), [item])

  const grabFailed = useCallback((key, err) => {
    if (err.code === 'already_queued') setGrab(key, 'queued', err.message)
    else setGrab(key, 'error', err.message)
  }, [setGrab])

  const doRefreshAndRetry = useCallback(async (originalRelease) => {
    const key = originalRelease.guid || originalRelease.title
    // The row's own search, exactly as the run issued it. Rebuilding the query
    // from season_number — which an episode row still carries, as a label —
    // turned the retry of an episode grab into a season-pack search.
    const params = { ...(item.search || {}) }

    const poll = async () => {
      if (!mountedRef.current) return
      try {
        const data = await api.acquireReleases(params)
        if (data.status === 'searching') {
          refreshPollRef.current = setTimeout(poll, 2000)
          return
        }
        if (data.status === 'done' && data.releases?.length) {
          const fresh = data.releases.find(r => r.indexer === originalRelease.indexer && r.title === originalRelease.title)
          if (!fresh) {
            setGrab(key, 'error', `Release not found on ${originalRelease.indexer} after re-search`)
            return
          }
          setGrab(key, 'grabbing')
          try {
            await api.grabRelease(grabBody(fresh, false))
            if (item.key) GRABBED.add(item.key)
            setGrab(key, 'grabbed')
            startImportWatch(fresh)
          } catch (err2) {
            // Never a second automatic retry.
            if (mountedRef.current) grabFailed(key, err2)
          }
        } else {
          setGrab(key, 'error', data.message || 'Re-search failed')
        }
      } catch (err) {
        if (mountedRef.current) setGrab(key, 'error', err.message)
      }
    }
    poll()
  }, [item, grabBody, grabFailed, setGrab, startImportWatch])

  const doGrab = useCallback(async (release, e, force = false) => {
    e.stopPropagation()
    const key = release.guid || release.title
    const current = grabStates[key]
    if (current && current !== 'error' && !(force && current === 'queued')) return
    setGrab(key, 'grabbing')
    try {
      await api.grabRelease(grabBody(release, force))
      // Only an accepted grab: a refusal (`already_queued`) or a failure keeps the row.
      if (item.key) GRABBED.add(item.key)
      setGrab(key, 'grabbed')
      startImportWatch(release)
    } catch (err) {
      // Only a stale guid is re-searched: it is the one failure a fresh search
      // fixes. Anything else is shown as it is — a timeout may be a grab the
      // arr did process, and retrying that downloads the release twice (B12).
      if (err.code === 'stale_release') {
        setGrab(key, 'refreshing')
        doRefreshAndRetry(release)
      } else {
        grabFailed(key, err)
      }
    }
  }, [item, grabStates, grabBody, grabFailed, setGrab, startImportWatch, doRefreshAndRetry])

  const resetGrab = useCallback((key, e) => {
    e.stopPropagation()
    setGrab(key, 'idle')
  }, [setGrab])

  const searching = item.status === 'searching'
  const found     = item.status === 'found'
  const notFound  = item.status === 'not_found'
  const errored   = item.status === 'error'

  const releases  = (found && item.releases) || (item.best_release ? [item.best_release] : [])
  const multi     = releases.length > 1

  const icon = searching ? (
    <Spinner size={10} />
  ) : found ? (
    <span style={{ color: 'var(--green)', fontSize: 'var(--font-sm)', fontWeight: 700 }}>✓</span>
  ) : notFound ? (
    <span style={{ color: 'var(--text-dim)', fontSize: 'var(--font-sm)' }}>—</span>
  ) : (
    <span style={{ color: 'var(--red)', fontSize: 'var(--font-sm)', fontWeight: 700 }}>✗</span>
  )

  const filename = (item.path || '').replace(/\\/g, '/').split('/').pop()

  // Single-release grab key
  const singleKey = item.best_release?.guid || item.best_release?.title

  const infoUrl = found ? (releases[0]?.info_url || '') : ''

  return (
    <div style={{ borderBottom: '1px solid var(--border)' }}>
      {/* Header row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: multi ? '10px 16px 6px' : '10px 16px' }}>
        <span style={{ width: 14, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          {icon}
        </span>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div title={scopeReason(item) || undefined} style={{
            ...ITEM_TITLE, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            color: notFound || errored ? 'var(--text-dim)' : 'var(--text)',
          }}>
            {item.arr_title}{scopeLabel(item)}
          </div>
          {/* Single-release info line */}
          {!multi && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--font-sm)', color: 'var(--text-dim)', fontFamily: 'var(--mono)', marginTop: 2 }}>
              {searching && 'Querying indexers…'}
              {found && item.best_release && (() => {
                const r = item.best_release
                const parts = [r.indexer, formatBytes(r.size), r.size_delta != null ? sizeDelta(r.size_delta) : null,
                  r.seeders != null ? `${r.seeders}S` : null, r.quality_name, r.hdr || null].filter(Boolean)
                if (r.custom_format_score) parts.push(`CF:${r.custom_format_score}`)
                return <><span>{parts.join(' · ')}</span><MatchChips match={r.match} fields={MATCH_FIELDS} titleSuffix=" against your file" /></>
              })()}
              {notFound  && 'No releases found'}
              {errored   && item.error}
            </div>
          )}
          {multi && (
            <div style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', fontFamily: 'var(--mono)', marginTop: 2 }}>
              {releases.length} releases found
            </div>
          )}
          {filename && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2, minWidth: 0 }}>
              {infoUrl ? (
                <a
                  href={infoUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={e => e.stopPropagation()}
                  title={filename}
                  style={{
                    fontSize: 'var(--font-sm)', color: 'var(--text-dim)', fontFamily: 'var(--mono)',
                    opacity: 0.75, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    flex: '1 1 0', minWidth: 0, textDecoration: 'underline',
                    textDecorationColor: 'var(--text-dim)', textUnderlineOffset: 2,
                  }}
                >
                  {filename}
                </a>
              ) : (
                <span
                  style={{
                    fontSize: 'var(--font-sm)', color: 'var(--text-dim)', fontFamily: 'var(--mono)',
                    opacity: 0.55, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    flex: '1 1 0', minWidth: 0,
                  }}
                  title={filename}
                >
                  {filename}
                </span>
              )}
              {item.file_quality && (
                <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', fontFamily: 'var(--mono)', opacity: 0.65, whiteSpace: 'nowrap', flexShrink: 0 }}>
                  {item.file_quality}
                </span>
              )}
              {item.file_hdr && HDR_STYLE[item.file_hdr] && (
                <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', fontWeight: 700, padding: '1px 4px', borderRadius: 3, whiteSpace: 'nowrap', flexShrink: 0, background: HDR_STYLE[item.file_hdr].bg, color: HDR_STYLE[item.file_hdr].color }}>
                  {item.file_hdr}
                </span>
              )}
              {item.total_size > 0 && (
                <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)', fontFamily: 'var(--mono)', opacity: 0.65, whiteSpace: 'nowrap', flexShrink: 0 }}>
                  {formatBytes(item.total_size)}
                </span>
              )}
            </div>
          )}
        </div>

        {/* Service link — the same chip Triage's rows carry. With no link
            address it is only a label, and says so by not being a button. */}
        {item.arr_service && (item.arr_url ? (
          <Button size="chip" tone={item.arr_service === 'radarr' ? 'var(--yellow)' : 'var(--blue)'}
            href={item.arr_url} target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()} title={`Open in ${item.arr_service}`}>
            {item.arr_service} ↗
          </Button>
        ) : (
          <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', flexShrink: 0,
            color: item.arr_service === 'radarr' ? 'var(--yellow)' : 'var(--blue)' }}>
            {item.arr_service}
          </span>
        ))}

        {/* Import status (auto-starts after any grab) */}
        {importStatus && (
          <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', flexShrink: 0, display: 'flex', alignItems: 'center', gap: 4 }}>
            {WATCH_ACTIVE.includes(importStatus) && <Spinner size={8} weight={1.5} />}
            <span style={{ color: watchColor(importStatus) }} title={importMessage}>
              {IMPORT_LABEL[importStatus] || importStatus}
            </span>
          </span>
        )}

        {/* Single-release grab button */}
        {found && !multi && item.best_release && (
          <div style={{ flexShrink: 0, display: 'flex', alignItems: 'center' }}>
            <GrabButton
              state={grabStates[singleKey] || 'idle'}
              errorMsg={grabErrors[singleKey]}
              onGrab={e => doGrab(item.best_release, e)}
              onForce={e => doGrab(item.best_release, e, true)}
              onReset={e => resetGrab(singleKey, e)}
            />
          </div>
        )}
      </div>

      {/* Multi-release picker */}
      {found && multi && (
        <div style={{ paddingLeft: 42, paddingRight: 16, paddingBottom: 10, display: 'flex', flexDirection: 'column', gap: 3 }}>
          {/* Column headers */}
          <div style={{ display: 'grid', gridTemplateColumns: RELEASE_GRID, gap: 8, padding: '2px 10px' }}>
            {['Title', 'Tracker', 'Size', 'vs file', 'Peers', 'Quality', 'Score', 'HDR', 'Match', ''].map((h, i) => (
              <span key={i} style={{
                fontSize: 'var(--font-sm)', fontFamily: 'var(--sans)', fontWeight: 600, letterSpacing: 0, textTransform: 'none',
                color: 'var(--text-dim)', opacity: 0.75,
                textAlign: i >= 2 && i <= 6 ? 'right' : 'left',
              }}>{h}</span>
            ))}
          </div>
          {releases.map((r, i) => {
            const key     = r.guid || r.title
            const gs      = grabStates[key] || 'idle'
            const isBest  = i === 0
            const hdrInfo = HDR_STYLE[r.hdr]
            return (
              <div key={key} style={{
                display: 'grid', gridTemplateColumns: RELEASE_GRID,
                alignItems: 'center', gap: 8, padding: '5px 10px', borderRadius: 6,
                background: isBest ? tint('var(--accent)', 3) : 'transparent',
                border: `1px solid ${isBest ? tint('var(--accent)', 15) : 'transparent'}`,
              }}>
                {r.info_url ? (
                  <a href={r.info_url} target="_blank" rel="noopener noreferrer"
                    onClick={e => e.stopPropagation()}
                    style={{ ...ITEM_TITLE, fontFamily: 'var(--mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textDecoration: 'underline', textDecorationColor: tint('var(--text)', 30), textUnderlineOffset: 2 }}
                    title={r.title}>
                    {r.title}
                  </a>
                ) : (
                  <span style={{ ...ITEM_TITLE, fontFamily: 'var(--mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                    title={r.title}>
                    {r.title}
                  </span>
                )}
                <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'right' }}
                  title={r.indexer}>
                  {r.indexer}
                </span>
                <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', textAlign: 'right' }}>
                  {formatBytes(r.size)}
                </span>
                <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', textAlign: 'right', whiteSpace: 'nowrap',
                  color: MATCH_COLOR[r.match?.size] || 'var(--text-dim)' }}>
                  {sizeDelta(r.size_delta)}
                </span>
                <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: r.seeders > 0 ? 'var(--green)' : 'var(--text-dim)', textAlign: 'right' }}>
                  {r.seeders != null ? `${r.seeders}S` : '—'}
                </span>
                <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                  title={r.quality_name}>
                  {r.quality_name || '—'}
                </span>
                <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', textAlign: 'right' }}>
                  {r.custom_format_score ?? 0}
                </span>
                <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
                  {hdrInfo ? (
                    <span style={{
                      fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', fontWeight: 700,
                      padding: '2px 5px', borderRadius: 4,
                      background: hdrInfo.bg, color: hdrInfo.color,
                      whiteSpace: 'nowrap',
                    }}>
                      {r.hdr}
                    </span>
                  ) : null}
                </div>
                <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
                  <MatchChips match={r.match} fields={MATCH_FIELDS} titleSuffix=" against your file" />
                </div>
                <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <GrabButton
                    state={gs}
                    errorMsg={grabErrors[key]}
                    onGrab={e => doGrab(r, e)}
                    onForce={e => doGrab(r, e, true)}
                    onReset={e => resetGrab(key, e)}
                  />
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── Prefs persistence ─────────────────────────────────────────────────────────
const PREFS_KEY = 'auditorr_backfill_prefs'
function loadPref(key, fallback) {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY) || '{}')[key] ?? fallback }
  catch { return fallback }
}

// The running search's id, per tab (B4). It lived only in component state, so
// navigating away lost the only handle to a run whose searches carried on
// against your indexers regardless. The server forgets a run an hour after it
// finishes, and stops one nobody has polled for ten minutes.
const JOB_KEY = 'auditorr_backfill_job'
function readJobId() {
  try { return sessionStorage.getItem(JOB_KEY) } catch { return null }
}
function saveJobId(id) {
  try { id ? sessionStorage.setItem(JOB_KEY, id) : sessionStorage.removeItem(JOB_KEY) } catch {}
}

// ── Main component ────────────────────────────────────────────────────────────
export default function Backfill({ onNavigate }) {
  const [phase, setPhase] = useState(() => (readJobId() ? 'running' : 'config'))  // config | running | done | stopped

  // Data loaded on mount
  const [indexers,      setIndexers]      = useState([])
  const [allGroups,     setAllGroups]     = useState([])   // candidates as the server grouped them
  // Both file counts, not candidate counts. The candidate figure below is
  // groups (a season pack is one candidate, however many episodes), so the
  // two can only be reported as separate claims — see the Search Depth copy.
  const [totalUnseeded, setTotalUnseeded] = useState(0)
  const [matchedFiles,  setMatchedFiles]  = useState(0)
  const [unmatchedVideo, setUnmatchedVideo] = useState(null)
  const [unmatchedExample, setUnmatchedExample] = useState(null)   // B9: one pair of paths, or null
  const [loading,       setLoading]       = useState(true)
  const [loadError,     setLoadError]     = useState(null)
  const [arrErrors,     setArrErrors]     = useState([])   // instances whose media index failed

  // Config — indexer strategy (persisted server-side via saveAcquirePrefs)
  const [downloadFrom,    setDownloadFrom]    = useState([])
  const [seedingOn,       setSeedingOn]       = useState([])
  const [saving,          setSaving]          = useState(false)
  // Config — quality / sort / depth (persisted in localStorage)
  const [selectedFolders, setSelectedFolders] = useState(() => loadPref('selectedFolders', []))
  const [titleSearch,     setTitleSearch]     = useState('')  // not persisted — ephemeral per-session
  const [searchCount,     setSearchCount]     = useState(() => loadPref('searchCount', 5))
  const [sort,            setSort]            = useState(() => loadPref('sort', 'largest'))
  const [releaseRank,     setReleaseRank]     = useState(() => loadPref('releaseRank', 'closest'))
  const [resFilter,       setResFilter]       = useState(() => loadPref('resFilter', []))
  const [sourceFilter,    setSourceFilter]    = useState(() => loadPref('sourceFilter', []))
  const [hdrFilter,       setHdrFilter]       = useState(() => loadPref('hdrFilter', []))

  // Job
  const [jobId,       setJobId]       = useState(() => readJobId())
  const [jobData,     setJobData]     = useState(null)
  const [reattaching, setReattaching] = useState(() => !!readJobId())
  const [notice,      setNotice]      = useState(null)
  const [pollError,   setPollError]   = useState(null)
  const pollRef     = useRef(null)
  const pollToken   = useRef(0)
  const sinceRef    = useRef(0)
  const failuresRef = useRef(0)
  const mountedRef  = useRef(true)

  useEffect(() => () => {
    // Deliberately no stop here: leaving the page is not abandoning the run.
    // The id is in sessionStorage and the page reattaches when it comes back.
    mountedRef.current = false
    clearTimeout(pollRef.current)
  }, [])

  // Persist filter prefs to localStorage whenever they change
  useEffect(() => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        sort, releaseRank, resFilter, sourceFilter, hdrFilter, searchCount, selectedFolders,
      }))
    } catch {}
  }, [sort, releaseRank, resFilter, sourceFilter, hdrFilter, searchCount, selectedFolders])

  const load = useCallback(() => {
    setLoading(true)
    setLoadError(null)
    Promise.all([api.acquireCandidates(), api.workflowIndexers(), api.getConfig()])
      .then(([cdata, idata, cfg]) => {
        setAllGroups((cdata.candidates || []).filter(c => !GRABBED.has(c.key)))
        setTotalUnseeded((cdata.resolved_count || 0) + (cdata.unresolved_count || 0))
        setMatchedFiles(cdata.resolved_count || 0)
        setUnmatchedVideo(cdata.unresolved_video_count ?? null)
        setUnmatchedExample(cdata.unmatched_example || null)
        setArrErrors(cdata.arr_errors || [])
        setIndexers(idata.indexers || [])
        setDownloadFrom(cfg.ACQUIRE_DOWNLOAD_FROM || [])
        setSeedingOn(cfg.ACQUIRE_SEEDING_ON || [])
      })
      .catch(e => setLoadError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])
  // Built from the last audit, like every other workflow page. Without this the
  // candidate list, the folder chips, the Search Depth readout and the Generate
  // count all stay pre-scan for as long as the page is open — including across
  // the scan the user's own backfill just caused, so a file that has been
  // successfully backfilled is still offered and grabbing it again is a plain
  // duplicate download. The config phase is the only thing this re-renders; a
  // running job lives in a different phase and is untouched. The audit's answer
  // is now the authoritative one, so the locally grabbed keys go (B6).
  useAuditComplete(() => { GRABBED.clear(); load() })

  const saveFilters = useCallback(async (df, so) => {
    setSaving(true)
    try { await api.saveAcquirePrefs({ ACQUIRE_DOWNLOAD_FROM: df, ACQUIRE_SEEDING_ON: so }) } catch (_) {}
    setSaving(false)
  }, [])

  const handleDownloadFromChange = v => { setDownloadFrom(v); saveFilters(v, seedingOn) }
  const handleSeedingOnChange    = v => { setSeedingOn(v);    saveFilters(downloadFrom, v) }

  // Filter raw groups by title search, then derive folder chips + count reactively
  const filteredGroups = useMemo(() => {
    if (!titleSearch.trim()) return allGroups
    const term = titleSearch.trim().toLowerCase()
    return allGroups.filter(g => (g.arr_title || '').toLowerCase().includes(term))
  }, [allGroups, titleSearch])

  const folderEntries = useMemo(() => groupByFolder(filteredGroups), [filteredGroups])
  const folders = useMemo(
    () => folderEntries.map(([name, items]) => ({ name, count: items.length })), [folderEntries])

  // A remembered selection that names no folder on the page selects nothing you
  // can see and would narrow the run to nothing. The labels changed from the
  // first path segment to the arrs' own root folders (B10), so every saved
  // selection from before is exactly that.
  const liveFolders = useMemo(
    () => selectedFolders.filter(f => folders.some(x => x.name === f)), [selectedFolders, folders])

  const selectedGroups = useMemo(() => (
    liveFolders.length === 0
      ? filteredGroups
      : folderEntries.filter(([name]) => liveFolders.includes(name)).flatMap(([, items]) => items)
  ), [folderEntries, filteredGroups, liveFolders])

  const availableCount = selectedGroups.length
  const willSearch = searchCount === null ? availableCount : Math.min(availableCount, searchCount)

  const matchedLine = useMemo(
    () => matchedFilesLine(matchedFiles, totalUnseeded), [matchedFiles, totalUnseeded])
  const unmatchedLine = useMemo(
    () => unmatchedFilesLine(matchedFiles, totalUnseeded, unmatchedVideo), [matchedFiles, totalUnseeded, unmatchedVideo])

  // Incremental (B5): each poll asks only for the rows that finished since the
  // last one and appends them. The in-flight row travels as `current`, apart
  // from the results, because it is still changing.
  const startPoll = useCallback((id) => {
    clearTimeout(pollRef.current)
    const token = ++pollToken.current
    sinceRef.current = 0
    failuresRef.current = 0
    const poll = async () => {
      try {
        const since = sinceRef.current
        const data = await api.generateStatus(id, since)
        if (!mountedRef.current || token !== pollToken.current) return
        failuresRef.current = 0
        sinceRef.current = data.next ?? since + (data.results?.length || 0)
        setReattaching(false)
        setPollError(null)
        setJobData(prev => ({
          status:     data.status,
          total:      data.total,
          completed:  data.completed,
          stopReason: data.stop_reason,
          current:    data.current || null,
          results:    since === 0 ? (data.results || []) : [...(prev?.results || []), ...(data.results || [])],
        }))
        if (data.arr_errors) setArrErrors(data.arr_errors)
        if (data.status === 'running') {
          pollRef.current = setTimeout(poll, 2000)
        } else {
          setPhase(data.status === 'done' ? 'done' : 'stopped')
        }
      } catch (err) {
        if (!mountedRef.current || token !== pollToken.current) return
        if (err.code === 'job_not_found') {
          // Swept, or from before a restart: nothing to go back to.
          saveJobId(null)
          setReattaching(false)
          setJobId(null)
          setJobData(null)
          setPhase('config')
          return
        }
        // A blip is not the end of a forty-minute run; keep asking for a while.
        failuresRef.current += 1
        if (failuresRef.current < 6) {
          pollRef.current = setTimeout(poll, 5000)
        } else {
          setReattaching(false)
          setPollError(`Lost contact with auditorr (${err.message}). The search may still be running — reopen Backfill to pick it up again.`)
          setPhase('stopped')
        }
      }
    }
    poll()
  }, [])

  const attach = useCallback((id) => {
    setJobId(id)
    saveJobId(id)
    setPhase('running')
    startPoll(id)
  }, [startPoll])

  // Reattach to this tab's run, if it has one.
  useEffect(() => {
    const id = readJobId()
    if (id) startPoll(id)
  }, [startPoll])

  async function handleGenerate() {
    setPhase('running')
    setJobData(null)
    setNotice(null)
    setPollError(null)
    try {
      const resp = await api.startGenerate({
        folders:       liveFolders,
        count:         searchCount ?? availableCount,
        sort,
        release_rank:  releaseRank,
        res_filter:    resFilter,
        source_filter: sourceFilter,
        hdr_filter:    hdrFilter,
        title_search:  titleSearch.trim() || undefined,
        download_from: downloadFrom,
        seeding_on:    seedingOn,
        // B6 — grabbed since the last audit; the run builds its own list.
        exclude_keys:  GRABBED.size ? [...GRABBED] : undefined,
      })
      // The run re-fetched the library (the index cache is 120s, and setting
      // filters takes longer than that), so this is the authoritative answer
      // about which instances answered — not the one the page loaded with.
      setArrErrors(resp.arr_errors || [])
      attach(resp.job_id)
    } catch (e) {
      if (e.code === 'job_running' && e.data?.job_id) {
        // Somebody's run — another tab's, or this one's from before. A new run
        // no longer stops it; this page shows it instead (B4).
        setNotice('A search was already running, so this is that search rather than a new one.')
        attach(e.data.job_id)
        return
      }
      setPhase('config')
      setLoadError(e.message)
    }
  }

  const handleStop = useCallback(async () => {
    if (jobId) { try { await api.stopGenerate(jobId) } catch (_) {} }
  }, [jobId])

  const handleReset = useCallback(() => {
    pollToken.current += 1
    clearTimeout(pollRef.current)
    saveJobId(null)
    // Back to the list the page loaded before the run, less what it grabbed (B6).
    setAllGroups(groups => groups.filter(c => !GRABBED.has(c.key)))
    setPhase('config')
    setJobId(null)
    setJobData(null)
    setReattaching(false)
    setNotice(null)
    setPollError(null)
    setLoadError(null)
  }, [])

  // ── Config phase ──────────────────────────────────────────────────────────────
  if (phase === 'config') {
    const kinds = kindsLine(selectedGroups)
    const exampleArr = unmatchedExample?.service === 'sonarr' ? 'Sonarr' : 'Radarr'
    return (
      <WorkflowPage gap={28}>
        <WorkflowHeader
          title="Backfill"
          accent="var(--blue)"
          blurb="Convert your orphaned media files into active seeds by grabbing matching releases from your trackers."
        />

        <WorkflowError message={loadError} />

        <ArrErrorsWarning
          errors={arrErrors}
          extra="Everything affected is missing from the candidates and folders below."
        />

        {loading ? (
          <LoadingRow label="Loading candidates…" />
        ) : (
          <>
            {indexers.length > 0 && (
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 14 }}>
                  <SectionLabel>Indexer Strategy</SectionLabel>
                  {saving && <span style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>Saving…</span>}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
                  <div>
                    <div style={{ fontSize: 'var(--font-md)', fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Download from</div>
                    <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginBottom: 8, lineHeight: 1.5 }}>Restrict to these indexers. <em>All</em> = no restriction.</div>
                    <IndexerChips options={indexers} value={downloadFrom} onChange={handleDownloadFromChange} />
                  </div>
                  <div>
                    <div style={{ fontSize: 'var(--font-md)', fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Must also be seeding on</div>
                    <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginBottom: 8, lineHeight: 1.5 }}>Release must also be listed on these — downloading it from one of them counts. <em>Any</em> = no restriction.</div>
                    <IndexerChips options={indexers} value={seedingOn} onChange={handleSeedingOnChange} allLabel="Any" />
                  </div>
                </div>
              </div>
            )}

            {folders.length > 0 && (
              <div>
                <SectionLabel>Root Folders</SectionLabel>
                <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginBottom: 10, lineHeight: 1.5 }}>
                  The root folders configured in Sonarr/Radarr. <em>All</em> = search everything.
                </div>
                <FolderChips folders={folders} selected={liveFolders} onChange={setSelectedFolders} />
              </div>
            )}

            <div>
              <SectionLabel>Quality Filter</SectionLabel>
              <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginBottom: 12, lineHeight: 1.5 }}>
                Restrict results by resolution and/or source, exactly like a Sonarr/Radarr quality profile. <em>Any</em> = no restriction.
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div>
                  <div style={{ fontSize: 'var(--font-md)', fontWeight: 600, color: 'var(--text)', marginBottom: 6 }}>Resolution</div>
                  <LabeledChips options={QUALITY_RES_OPTIONS} value={resFilter} onChange={setResFilter} />
                </div>
                <div>
                  <div style={{ fontSize: 'var(--font-md)', fontWeight: 600, color: 'var(--text)', marginBottom: 6 }}>Source</div>
                  <LabeledChips options={QUALITY_SOURCE_OPTIONS} value={sourceFilter} onChange={setSourceFilter} />
                </div>
                <div>
                  <div style={{ fontSize: 'var(--font-md)', fontWeight: 600, color: 'var(--text)', marginBottom: 6 }}>HDR</div>
                  <LabeledChips options={HDR_OPTIONS} value={hdrFilter} onChange={setHdrFilter} />
                </div>
              </div>
            </div>

            <div>
              <SectionLabel>Release Ranking</SectionLabel>
              <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginBottom: 10, lineHeight: 1.5 }}>
                Which release is offered first for each candidate.
              </div>
              <SortPicker options={RANK_OPTIONS} value={releaseRank} onChange={setReleaseRank} />
            </div>

            <div>
              <SectionLabel>Priority</SectionLabel>
              <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginBottom: 10, lineHeight: 1.5 }}>
                How to order candidates when there are more than the search limit.
              </div>
              <SortPicker options={SORT_OPTIONS} value={sort} onChange={setSort} />
              <div style={{ marginTop: 12 }}>
                <input
                  type="text"
                  value={titleSearch}
                  onChange={e => setTitleSearch(e.target.value)}
                  placeholder="Filter by title…"
                  style={{
                    padding: '6px 10px', borderRadius: 'var(--r)', fontSize: 'var(--font-base)',
                    border: '1px solid var(--border)',
                    background: 'var(--surface2)',
                    color: 'var(--text)',
                    fontFamily: 'inherit',
                    width: 220, outline: 'none',
                    opacity: titleSearch ? 1 : 0.7,
                  }}
                />
              </div>
            </div>

            <div>
              <SectionLabel>Search Depth</SectionLabel>
              <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginBottom: 12, lineHeight: 1.5 }}>
                Each candidate queries your indexers — expect 10–90s per search depending on your setup.
                {availableCount > 0 && ` ${availableCount.toLocaleString()} searchable candidate${availableCount !== 1 ? 's' : ''}${kinds ? ` — ${kinds}` : ''}.`}
                {/* Shown even at zero candidates: that is the case where a
                    resolution failure is most likely and the numbers matter
                    most. */}
                {matchedLine && ` ${matchedLine}`}
                {unmatchedLine && ` ${unmatchedLine}`}
              </div>
              {/* B9's optional half: the mismatch is usually obvious once the two
                  paths sit together. Only where the arr holds a file of the same
                  name — otherwise there is nothing true to put beside it. */}
              {unmatchedVideo > 0 && unmatchedExample && (
                <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginTop: -4, marginBottom: 12, lineHeight: 1.6 }}>
                  For example, one of those videos and the {exampleArr}{unmatchedExample.connection_name && unmatchedExample.connection_name.toLowerCase() !== exampleArr.toLowerCase() ? ` (${unmatchedExample.connection_name})` : ''} file of the same name:
                  <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', marginTop: 4, display: 'grid', gridTemplateColumns: 'auto 1fr', columnGap: 10, rowGap: 2, minWidth: 0 }}>
                    <span>your library</span>
                    <span style={{ color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={unmatchedExample.path}>{unmatchedExample.path}</span>
                    <span>{exampleArr}</span>
                    <span style={{ color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={unmatchedExample.arr_path}>{unmatchedExample.arr_path}</span>
                  </div>
                </div>
              )}
              <CountPicker value={searchCount} onChange={setSearchCount} max={availableCount || 999} />
            </div>

            <div>
              <Button variant="primary" onClick={handleGenerate} disabled={availableCount === 0}>
                Generate {willSearch > 0 ? `${willSearch} ` : ''}Releases →
              </Button>
              {availableCount === 0 && (
                <div style={{ marginTop: 8, fontSize: 'var(--font-base)', color: 'var(--text-dim)' }}>
                  No resolved candidates — check that Radarr/Sonarr is configured.
                </div>
              )}
            </div>
          </>
        )}

        <SpinKeyframes />
      </WorkflowPage>
    )
  }

  // ── Results phase (running | done | stopped) ──────────────────────────────────
  const results    = jobData?.results || []
  const current    = phase === 'running' ? jobData?.current : null
  const rows       = current ? [...results, current] : results
  const total      = jobData?.total ?? willSearch
  const completed  = jobData?.completed ?? 0
  const foundCount = results.filter(r => r.status === 'found').length
  const progress   = total > 0 ? (completed / total) * 100 : 0
  const found      = `${foundCount} release${foundCount !== 1 ? 's' : ''} found`

  const phaseLabel =
    reattaching                            ? 'Picking your search back up…' :
    phase === 'running'                    ? `Searching ${completed} of ${total}…` :
    phase === 'done'                       ? `Done — ${found}` :
    jobData?.stopReason === 'abandoned'    ? `Stopped with nobody watching — ${found}` :
                                             `Stopped — ${found}`

  return (
    <WorkflowPage gap={14}>
      {/* The shared header, as the config phase uses. This was a hand copy of
          it whose eyebrow carried `textAlign: 'center'`, so "Workflows" sat in
          the middle of the page above a left-aligned title. */}
      <WorkflowHeader
        title={phaseLabel}
        blurb={notice}
        right={phase === 'running'
          ? <Button onClick={handleStop}>Stop</Button>
          : <Button onClick={handleReset}>← New Search</Button>}
      />

      {/* Progress bar */}
      <div style={{ height: 3, background: 'var(--border)', borderRadius: 2, overflow: 'hidden' }}>
        <div style={{
          height: '100%', borderRadius: 2, transition: 'width 0.5s ease',
          background: phase === 'done' ? 'var(--green)' : 'var(--accent)',
          width: `${progress}%`,
        }} />
      </div>

      <WorkflowError message={pollError} />

      {/* The run's own answer about which instances were readable — the page
          may have been configured minutes ago, against a different one. */}
      <ArrErrorsWarning
        errors={arrErrors}
        extra="Candidates from those instances are missing from this run."
      />

      {/* Results list */}
      {rows.length > 0 && (
        <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', overflow: 'hidden' }}>
          {rows.map((item, i) => <ResultItem key={item.key ?? i} item={item} />)}
        </div>
      )}

      {rows.length === 0 && phase === 'running' && (
        <div style={{ padding: '48px 0', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, color: 'var(--text-dim)', fontSize: 'var(--font-base)' }}>
          <Spinner />
          {reattaching ? 'Picking your search back up…' : 'Starting search…'}
        </div>
      )}

      <SpinKeyframes />
    </WorkflowPage>
  )
}
