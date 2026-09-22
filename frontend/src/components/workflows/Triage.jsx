import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../../api'
import { formatBytes, copyText } from '../../utils'
import { useToast } from '../Toast'
import {
  WorkflowPage, WorkflowHeader, EmptyState, LoadingRow, WorkflowError, WorkflowWarning, WorkflowCrossLink,
  ArrErrorsWarning, ActionBar, Button, Spinner, SpinKeyframes, SectionHeading, QualityChip, Checkbox,
  ITEM_TITLE, tint, useAuditComplete, ConfirmExcludeModal, regKey, RegistrationWarning,
} from './shared'

const VERDICTS = [
  {
    key: 'dead_seed', label: 'Dead Seeds — imported', color: 'var(--green)',
    desc: 'Tracker-dead (trumped, deleted, or nuked) but already imported: your library holds a hardlink to the same data, so deleting these via the client is completely lossless. The safest cleanup there is.',
  },
  {
    key: 'dead_registration', label: 'Dead Registration — payload alive', color: 'var(--green)',
    desc: 'The tracker dropped this torrent, but its data is still alive — seeding on a working cross-seed and/or hardlinked in your library, and each row says which. Remove just the dead registration: auditorr deletes its files only when nothing that stays in your client uses them, and keeps them whenever it cannot check — so a live seed is never broken.',
  },
  {
    key: 'unregistered', label: 'Unregistered — not imported', color: 'var(--red)',
    desc: 'Tracker-dead and NOT in your library — this torrent holds the only copy of the data. Seeding earns nothing, but deleting loses the files, so decide whether anything here is worth keeping first.',
  },
  {
    key: 'superseded', label: 'Superseded', color: 'var(--yellow)',
    desc: 'Your library already has this title, usually at a different quality. Keep seeding for ratio, or delete if the torrent is dead weight.',
  },
  {
    key: 'import_pending', label: 'Import Pending', color: 'var(--blue)',
    desc: 'Managed by Sonarr/Radarr but missing from the library — the import likely failed or was skipped. Trigger a rescan to retry.',
  },
  {
    key: 'library_unknown', label: 'Could Not Check', color: 'var(--yellow)',
    desc: 'A Sonarr/Radarr instance did not answer, so auditorr could not establish whether your library holds these. They are NOT “not in library” — that verdict means no arr has ever heard of a title, and no arr answered. Fix the connection above and reload before acting on anything here.',
  },
  {
    key: 'not_in_library', label: 'Not in Library', color: 'var(--text-dim)',
    desc: 'No matching title in any Sonarr/Radarr instance, alternate titles included. Nothing in your library holds this, so these files are the only copy — deleting loses them. Manual downloads belong in your exclusions.',
  },
]

const VERDICT_LABEL = Object.fromEntries(VERDICTS.map(v => [v.key, v.label]))

// Superseded sub-buckets — what to do depends on how the orphaned torrent's
// quality compares to the library file it duplicates. The 'duplicate' bucket
// is special: it's a byte-identical copy (not just same quality), so it goes
// first and points at Dedupe instead of being a keep/delete judgement call.
const QUALITY_BUCKETS = [
  {
    key: 'duplicate', label: 'Duplicate of an existing copy', color: 'var(--purple)',
    desc: 'A byte-identical copy already exists on disk but isn’t hardlinked — wasted space. Hardlink it in the Dedupe workflow to reclaim the space losslessly, no need to stop seeding.',
  },
  {
    key: 'higher', label: 'Higher quality than library', color: 'var(--green)',
    desc: 'Better than what you imported — consider a manual import in Sonarr/Radarr to upgrade your library copy before doing anything else.',
  },
  {
    key: 'same', label: 'Same quality as library', color: 'var(--blue)',
    desc: 'A separate release at the same quality (not a byte-identical copy) — pure ratio padding. Keep seeding or delete, nothing to upgrade.',
  },
  {
    key: 'lower', label: 'Lower quality than library', color: 'var(--yellow)',
    desc: 'Your library already has better. Safe to delete once the torrent has earned its keep.',
  },
  {
    key: 'unknown', label: 'Quality comparison unavailable', color: 'var(--text-dim)',
    desc: 'One side of the comparison could not be parsed — check the quality chips on each row before acting.',
  },
]

// A row is a torrent **registration** (S05): the same hash on two qui instances
// is two rows, verified and removed independently. Path-keyed rows have no hash.
function itemKey(item) {
  return item.hash ? regKey(item) : item.rep_path
}

// Live-verify batch size. Batches go out sequentially and stay under the
// server's per-request cap, so the tracker fan-out never runs wider than the
// 8-worker ceiling qui/qBittorrent are known to tolerate.
const VERIFY_CHUNK = 150

// Resolve an item's verdict for a live tracker health answer using the
// alternatives precomputed by phase 1 (null → recovered; the row drops).
function verdictUnder(item, health) {
  const alts = item.verdict_alternatives
  if (!alts) return item.verdict
  if (health === 'working') return alts.working
  if (health === 'unregistered') return alts.unregistered
  return alts.other
}

// Which superseded sub-bucket an item belongs to. A byte-identical duplicate
// always wins over its quality comparison — it's a Dedupe target, not a
// keep/delete call — so it never also shows up under same/higher/lower.
function qualityBucket(item) {
  if (item.is_duplicate) return 'duplicate'
  return item.library?.quality_cmp || 'unknown'
}

// Default delete scope: a superseded torrent whose library copy is the better
// one is dead weight → remove the whole cross-seed group. Everything else
// defaults to the single recorded torrent — and so does a row that is itself
// unsure what it is: a title two instances hold (T2 — a wrong-instance `lower`
// is exactly what used to pre-select a whole group) or a torrent whose files
// earned different verdicts (T5).
function defaultScope(item) {
  if (item.verdict !== 'superseded' || item.library?.quality_cmp !== 'lower') return 'one'
  if ((item.library.others || []).length > 0 || item.verdict_spread) return 'one'
  return 'all'
}

// When the client added the torrent, as an age — how long a row has been a
// problem is the cheapest context there is (T9). Live-only: verify fills it in.
function addedAgeLabel(addedOn) {
  if (!addedOn) return null
  const days = Math.floor((Date.now() / 1000 - addedOn) / 86400)
  if (days < 1) return 'added today'
  if (days < 30) return `added ${days}d ago`
  if (days < 365) return `added ${Math.floor(days / 30)}mo ago`
  return `added ${Math.floor(days / 365)}y ago`
}

// Why a removal keeps or deletes a torrent's files, in the server's terms.
const FILE_REASON = {
  requested:        'nothing that stays in the client uses these files',
  shared:           'a torrent that stays shares these files',
  unknown:          'auditorr could not check what else uses them',
  unusable_listing: 'auditorr could not read this torrent’s file list',
  not_in_client:    'it is no longer in the client',
}

// The file decision the server will make for one torrent of this removal —
// `_removal_file_decision` in app.py, applied to the removal set the modal is
// about to post, from what `resolve_groups` returned. The server resolves again
// at confirm and refuses (409 plan_changed) anything shown here keeping its
// files that it would now delete, so a disagreement between the two can only
// ever fail safe.
function fileDecision(member, removal, checked) {
  const own = member?.reason?.all
  if (own === 'unusable_listing' || own === 'not_in_client') return { files: 'keep', reason: own }
  if ((member?.shares_with || []).some(h => !removal.has(h))) return { files: 'keep', reason: 'shared' }
  if (!checked) return { files: 'keep', reason: 'unknown' }
  return { files: 'delete', reason: 'requested' }
}

// "10 of 18 files · 40 GB of 72 GB" — a row can list a subset of its torrent (a
// partly imported torrent, an excluded file) while a delete removes all of it
// (T5). The file total comes from the audit; the size arrives with verify.
function subsetLabel(item) {
  const filesDiffer = item.torrent_files > item.file_count
  const sizeDiffers = item.torrent_size > item.total_size
  if (!filesDiffer && !sizeDiffers) return item.file_count > 1 ? `${item.file_count} files` : null
  const files = filesDiffer
    ? `${item.file_count} of ${item.torrent_files} files`
    : `${item.file_count} file${item.file_count !== 1 ? 's' : ''}`
  return sizeDiffers ? `${files} · ${formatBytes(item.total_size)} of ${formatBytes(item.torrent_size)}` : files
}

// Compact duration for seeding time — the hit-and-run tiebreaker, so days
// are the unit that matters
function formatDuration(secs) {
  if (secs == null) return null
  const d = secs / 86400
  if (d >= 1) return `${d >= 10 ? Math.round(d) : d.toFixed(1)}d`
  const h = secs / 3600
  if (h >= 1) return `${Math.round(h)}h`
  return `${Math.max(1, Math.round(secs / 60))}m`
}

// Search term for prescreening the qBittorrent/qui search box: the parsed
// title ("The Show") — qBit filters per word, so it matches dotted release
// names AND surfaces every torrent of that title, not just this release.
// Falls back to the release folder / file name when parsing found nothing.
function torrentSearchName(item) {
  if (item.parsed?.title) return item.parsed.title
  const paths = (item.paths || []).map(p => p.replace(/\\/g, '/'))
  if (paths.length === 0) return ''
  const fileBase = () => (paths[0].split('/').pop() || '').replace(/\.[^.]+$/, '')
  if (paths.length === 1) return fileBase()
  const segLists = paths.map(p => p.split('/').slice(0, -1))
  if (segLists.some(s => s.length === 0)) return fileBase()
  let common = segLists[0]
  for (const segs of segLists.slice(1)) {
    let i = 0
    while (i < common.length && i < segs.length && common[i] === segs[i]) i++
    common = common.slice(0, i)
  }
  return common.length > 0 ? common[common.length - 1] : fileBase()
}

// qui can jump straight to a torrent by hash; qBittorrent cannot
function canDeepLink(client, item) {
  return client?.name === 'qui' && item.hash && item.instance_id != null
}

// Force import replaces the file the arr already holds, so it is only offered
// where auditorr scored this release as the *same* quality as that file. A
// downgrade stays blocked — Sonarr/Radarr are right to refuse those, and the
// rescan action now reports their reason rather than a blank success.
function canForceImport(item) {
  const lib = item.library
  return !!lib && lib.quality_cmp === 'same' && !!lib.arr_id && !!lib.connection_id
}

// The arr reports every scan command as completed even when its import
// decision refused every file, so the reasons are the only real signal.
function rejectionSummary(rejected) {
  const reasons = [...new Set(rejected.flatMap(r => r.rejections || []))]
  if (reasons.length === 0) return 'no reason given'
  return reasons.slice(0, 2).join(' · ') + (reasons.length > 2 ? ` (+${reasons.length - 2} more)` : '')
}

// Exclusion rules come from the server (`item.exclusion_patterns`), not from
// here. Deriving them client-side was wrong twice (T6): the rules were built
// from raw paths, so a release name containing `[`, `*` or `?` became a glob
// that matched nothing — or matched more than was selected (C8) — and the
// common folder was derived from `item.paths`, which for a **partially
// imported** torrent holds only the not-imported files while their common
// folder is the release folder that also holds the imported ones. Only the
// audit can see that, so only the server can answer it.
//
// `dead_registration` rows carry an empty list and render no Exclude action at
// all: those paths belong to the *healthy carrier* — a file a working
// cross-seed is seeding right now — so excluding one hides a live file while
// the dead registration it was meant to address stays in the client. Absent
// rather than disabled, because the row already carries its reason.
const canExclude = item => (item.exclusion_patterns || []).length > 0

function TriageRow({ item, color, checked, onToggle, client, onOpenClient, onNavigate, pending, rescanned, unconfirmed }) {
  const p = item.parsed || {}
  const seTag = p.season != null
    ? ` · S${String(p.season).padStart(2, '0')}${p.episode != null ? 'E' + String(p.episode).padStart(2, '0') : ' pack'}`
    : ''
  const filename = (item.rep_path || '').replace(/\\/g, '/').split('/').pop()
  const lib = item.library

  return (
    <div
      onClick={onToggle}
      style={{
        display: 'flex', alignItems: 'flex-start', gap: 12, padding: '10px 14px',
        borderBottom: '1px solid var(--border)', cursor: 'pointer',
        background: checked ? tint('var(--accent)', 2) : 'transparent',
      }}
    >
      <span style={{ paddingTop: 3 }}>
        <Checkbox checked={checked} onChange={onToggle} />
      </span>

      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, minWidth: 0 }}>
          <span style={{ ...ITEM_TITLE, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {p.title || filename}{p.year ? ` (${p.year})` : ''}{seTag}
          </span>
          {subsetLabel(item) && (
            <span
              title={item.torrent_files > item.file_count || item.torrent_size > item.total_size
                ? 'This row lists the files that need a verdict. Removing the torrent removes all of it — the second figure.'
                : undefined}
              style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}
            >
              {subsetLabel(item)}
            </span>
          )}
          {/* T5 — the files of this torrent did not agree. The row shows the
              verdict whose action deletes least, and says it is a summary. */}
          {item.verdict_spread && (
            <span
              title={`These files earned different verdicts — ${Object.entries(item.verdict_spread).map(([v, n]) => `${n} ${VERDICT_LABEL[v] || v}`).join(', ')}. The row takes the one whose action deletes least.`}
              style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--yellow)', flexShrink: 0 }}
            >
              {Object.values(item.verdict_spread).reduce((a, b) => a + b, 0)} files · {Object.keys(item.verdict_spread).length} verdicts
            </span>
          )}
          {unconfirmed && (
            <span
              title={unconfirmed === 'unknown'
                ? `The ${client?.name || 'client'} instance holding this torrent did not answer after the removal, so auditorr cannot say whether it left. It stays here until the next scan.`
                : `auditorr asked ${client?.name || 'the client'} to remove this torrent and it was still listed a moment later. It stays here until the next scan shows what happened.`}
              style={{
                fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--yellow)', flexShrink: 0,
                border: '1px solid var(--yellow)', borderRadius: 'var(--r-pill)', padding: '1px 7px',
              }}
            >
              removal unconfirmed
            </span>
          )}
          {rescanned && (
            <span
              title="Handed to Sonarr/Radarr. The arr imports on its own schedule, and this row is built from the last audit — it clears once a scan has seen the result."
              style={{
                fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--blue)', flexShrink: 0,
                border: '1px solid var(--blue)', borderRadius: 'var(--r-pill)', padding: '1px 7px',
              }}
            >
              rescan sent
            </span>
          )}
        </div>
        <div title={item.rep_path} style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', opacity: 0.7, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginTop: 2 }}>
          {filename}
        </div>

        {/* Evidence line */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 5, flexWrap: 'wrap' }}>
          <QualityChip label={p.quality_label} hdr={p.hdr} unknown />
          {lib && lib.quality_name && (
            <>
              <span
                title={`${lib.title}${lib.year ? ` (${lib.year})` : ''}${lib.filename ? ' — ' + lib.filename : ''}`}
                style={{ fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}
              >
                vs library{lib.year ? ` (${lib.year})` : ''}
              </span>
              <QualityChip label={lib.quality_name} hdr={lib.hdr} dim unknown />
            </>
          )}
          {/* T2 — more than one instance holds this title. The row names them and
              says which one a rescan or force import goes to, rather than
              picking silently. */}
          {lib?.others?.length > 0 && (
            <span
              title={`${lib.others.length + 1} ${lib.service} instances hold this title. Rescan and force import go to ${lib.connection_name || 'the best match'}${lib.quality_name ? ` (${lib.quality_name})` : ''}. ${lib.others.map(o => `${o.name || o.connection_id} ${o.quality_name ? `holds ${o.quality_name}` : 'holds it too'}`).join('; ')}.`}
              style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--yellow)', opacity: 0.9 }}
            >
              on {lib.connection_name || lib.service} · also {lib.others.map(o => o.name || o.connection_id).join(', ')}
            </span>
          )}
          {/* T9 — the evidence this verdict's copy asserts as a generality. */}
          {item.verdict === 'dead_registration' && (item.alive_library || item.alive_sibling) && (
            <span
              title="Where this registration's data is still alive — why removing just the registration loses nothing"
              style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--green)', opacity: 0.9 }}
            >
              alive in {[item.alive_library && 'your library', item.alive_sibling && 'a seeding cross-seed'].filter(Boolean).join(' and ')}
            </span>
          )}
          {item.tracker_msg && (
            <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--red)', opacity: 0.9 }}>
              “{item.tracker_msg}”
            </span>
          )}
          {item.tracker_health === 'not_working' && !item.tracker_msg && (
            <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--yellow)', opacity: 0.8 }}>tracker not responding</span>
          )}
          {/* An unfinished download is not-imported by definition, so it used
              to sit here as junk. Known-incomplete torrents are filtered out
              server-side; these two cases are the ones that still reach the
              page, and they say why rather than being hidden. */}
          {item.completion_unknown && (
            <span
              title="The torrent client exposed no completion field for this torrent, so auditorr cannot tell whether the payload is finished. Check it in the client before deleting anything."
              style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--yellow)', opacity: 0.9 }}
            >
              completion unknown
            </span>
          )}
          {item.status === 'Downloading' && !item.completion_unknown && (
            <span
              title="Still downloading — the files are incomplete and will grow. Nothing here needs a verdict yet."
              style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--blue)', opacity: 0.9 }}
            >
              downloading
            </span>
          )}
          {item.is_duplicate && (
            <Button size="chip" tone="var(--purple)"
              onClick={e => { e.stopPropagation(); onNavigate && onNavigate({ tab: 'dedupe' }) }}
              title="A byte-identical copy exists on disk — open the Dedupe workflow to hardlink it and reclaim the space (lossless)"
            >
              Dedupe ↗
            </Button>
          )}
          {/* The reactive counterpart to a trump PM: a tracker dropping an
              imported torrent is often a trump the user never saw (TR16). */}
          {item.verdict === 'dead_seed' && (
            <Button size="chip" tone="var(--green)"
              onClick={e => { e.stopPropagation(); onNavigate && onNavigate({ tab: 'trumped', oldTitle: item.name || torrentSearchName(item) }) }}
              title="If the tracker trumped this release, the Trumped workflow removes every cross-seed of it and grabs the replacement"
            >
              Trumped? ↗
            </Button>
          )}
        </div>
      </div>

      {/* Fixed-width trailing columns so every row lines up vertically —
          no minWidth stretching, and the arr-link slot is always rendered
          even when empty. */}
      <div style={{ flexShrink: 0, textAlign: 'right', width: 110 }}>
        {/* The number a delete touches: the whole torrent once verify knows it (T5). */}
        <div style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text)' }}>{formatBytes(item.torrent_size ?? item.total_size)}</div>
        <div title={(item.trackers || []).join(', ')} style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {(item.trackers || [])[0] || 'no tracker'}{item.trackers?.length > 1 ? ` +${item.trackers.length - 1}` : ''}
        </div>
      </div>

      {/* Earnings — the keep/delete tiebreaker. Seeding time matters more
          than per-torrent ratio on private trackers: it decides whether
          deleting now means a hit-and-run. Live-only data: pulses until
          this row's verification batch answers. */}
      <div style={{ flexShrink: 0, textAlign: 'right', width: 100 }}>
        {pending ? (
          <div title="Verifying with the torrent client…"
            style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', animation: 'triagePulse 1.4s ease-in-out infinite' }}>
            ···
          </div>
        ) : (
          <>
            <div style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: item.uploaded > 0 ? 'var(--green)' : 'var(--text-dim)' }}>
              {item.uploaded != null ? `↑ ${formatBytes(item.uploaded)}` : '—'}
            </div>
            {item.seeding_time != null && (
              <div title="Total time seeding — check your tracker's hit-and-run rules before deleting"
                style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', marginTop: 2 }}>
                seeded {formatDuration(item.seeding_time)}
              </div>
            )}
            {addedAgeLabel(item.added_on) && (
              <div title="When your client added this torrent"
                style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', marginTop: 2 }}>
                {addedAgeLabel(item.added_on)}
              </div>
            )}
          </>
        )}
      </div>

      {/* 72, the width "sonarr ↗" takes as a chip. At 58 it overflowed into the
          earnings column — invisibly, until the chip's hairline rendered. */}
      <span style={{ flexShrink: 0, width: 72, display: 'flex', justifyContent: 'flex-end' }}>
        {lib?.arr_url && (
          <Button size="chip" tone={lib.service === 'radarr' ? 'var(--yellow)' : 'var(--blue)'}
            href={lib.arr_url} target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            title={`Open in ${lib.service}`}
            style={{ marginTop: 2, alignSelf: 'flex-start' }}
          >
            {lib.service} ↗
          </Button>
        )}
      </span>

      {/* Jump to the client to inspect/delete — qui deep-links to the exact
          torrent; qBittorrent copies the title for a one-paste search. Red
          for unregistered (the verdict that begs for deletion). */}
      {client && (
        <Button size="chip" variant="subtle" tone={item.verdict === 'unregistered' ? 'var(--red)' : undefined}
          onClick={e => onOpenClient(item, e)}
          title={canDeepLink(client, item)
            ? 'Open this torrent in qui'
            : `Copy “${torrentSearchName(item)}” and open ${client.name} — paste into its search box to find this torrent`}
          style={{ marginTop: 2 }}
        >
          {client.name} {canDeepLink(client, item) ? '↗' : '⧉↗'}
        </Button>
      )}
    </div>
  )
}

// Rows already acted on, by item key. Module-level on purpose: it has to
// outlive the component. The report is rebuilt from the *last audit*, so
// leaving Triage and coming back re-fetched rows the user had just deleted or
// excluded — they reappeared, looking like the action had failed, and stayed
// until a scan. Cleared when an audit lands, which is the point the server's
// own answer becomes correct.
const DISMISSED = new Set()

export default function Triage({ onNavigate, cleanupCount, trumpedCount }) {
  const toast = useToast()
  const [report,   setReport]   = useState(null)
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [busy,     setBusy]     = useState(null)   // 'rescan' | 'exclude' | 'delete' | null
  // Items handed to Sonarr/Radarr this session. Deleting or excluding drops the
  // row immediately, but a rescan cannot: the arr imports on its own schedule
  // and tells us nothing, and the row is built from audit-time data anyway. So
  // the row stays and says so, rather than looking like the click did nothing.
  const [rescanned, setRescanned] = useState(() => new Set())
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [confirmExclude, setConfirmExclude] = useState(false)
  // Cross-seed groups resolved live when the delete modal opens, keyed by the
  // item's registration → [{reg, hash, instance_id, name, tracker, seeding_time…}].
  const [groups,    setGroups]    = useState({})
  const [scopes,    setScopes]    = useState({})   // itemKey → 'one' | 'all'
  const [resolving, setResolving] = useState(false)
  const [resolveError, setResolveError] = useState(null)
  // What resolve_groups said about the answer as a whole:
  // { checked, unknown_listings, bounded, missing }.
  const [groupsMeta, setGroupsMeta] = useState(null)
  // A 409 `registration_ambiguous` — a selected torrent registered on more than
  // one instance, which the server will not guess between (S05).
  const [ambiguity, setAmbiguity] = useState(null)
  // Rows a removal could not confirm left the client, by item key →
  // 'still_listed' | 'unknown' (S09). They stay, and say so, until the audit.
  const [unconfirmed, setUnconfirmed] = useState({})

  const [client, setClient] = useState(null)   // { name: 'qBittorrent'|'qui', url }
  const [clientDeleteAllowed, setClientDeleteAllowed] = useState(false)

  // Phase 2 — live tracker verification. The report renders instantly from
  // audit-time data; hashes are then re-checked against the torrent client in
  // sequential batches and the rows updated in place.
  // null | { running, done, total, removed, failed: string|null }
  const [verify, setVerify] = useState(null)
  const verifyGen = useRef(0)   // bumped to cancel a superseded verify loop

  // Fold one batch of live details into the report: recovered torrents drop,
  // verdicts re-resolve via their phase-1 alternatives, live stats fill in.
  // Returns the keys of dropped (recovered) rows — computed purely from the
  // batch inputs so the state updater stays side-effect free.
  //
  // T10 — `found: false` is the client saying the torrent is gone since the
  // audit, and the row drops. **No entry at all is "could not ask"**: the row
  // keeps its audit-time verdict, re-resolved as it always was, but is not
  // marked verified — it used to be, which told the user a torrent the client
  // no longer held had been checked.
  const applyDetails = useCallback((batchItems, details) => {
    // Live details are keyed by registration (S05).
    const goneKeys = new Set(batchItems.filter(it => details[regKey(it)]?.found === false).map(itemKey))
    const recoveredKeys = new Set(
      batchItems
        .filter(it => !goneKeys.has(itemKey(it))
          && verdictUnder(it, (details[regKey(it)] || {}).tracker_health || 'unknown') == null)
        .map(itemKey)
    )
    const batchKeys = new Set(batchItems.map(regKey))
    setReport(r => {
      if (!r) return r
      const items = []
      for (const it of r.items) {
        if (!it.hash || !batchKeys.has(regKey(it))) { items.push(it); continue }
        const det = details[regKey(it)]
        if (det?.found === false) continue   // gone from the client since the audit
        const health = det?.tracker_health || 'unknown'
        const verdict = verdictUnder(it, health)
        if (verdict == null) continue   // recovered — re-registered on its tracker
        items.push({
          ...it,
          verdict,
          verified:       !!det,
          tracker_health: health,
          tracker_msg:    det?.tracker_msg || it.tracker_msg,
          uploaded:       det?.uploaded ?? null,
          ratio:          det?.ratio ?? null,
          seeding_time:   det?.seeding_time ?? null,
          added_on:       det?.added_on ?? null,
          torrent_size:   det?.size ?? it.torrent_size ?? null,
        })
      }
      return { ...r, items }
    })
    const droppedKeys = new Set([...goneKeys, ...recoveredKeys])
    if (droppedKeys.size > 0) {
      setSelected(prev => {
        if (![...droppedKeys].some(k => prev.has(k))) return prev
        const next = new Set(prev)
        droppedKeys.forEach(k => next.delete(k))
        return next
      })
    }
    return { recovered: recoveredKeys.size, gone: goneKeys.size }
  }, [])

  const runVerify = useCallback(async (targets) => {
    const gen = ++verifyGen.current
    targets = (targets || []).filter(i => i.hash)
    if (targets.length === 0) { setVerify(null); return }
    setVerify({ running: true, done: 0, total: targets.length, removed: 0, failed: null })
    let removed = 0, gone = 0
    for (let off = 0; off < targets.length; off += VERIFY_CHUNK) {
      const batch = targets.slice(off, off + VERIFY_CHUNK)
      let resp
      try {
        resp = await api.triageVerify(batch.map(i => ({ hash: i.hash, instance_id: i.instance_id })))
      } catch (e) {
        if (gen !== verifyGen.current) return
        setVerify(v => ({ ...(v || {}), running: false, failed: e.message }))
        return
      }
      if (gen !== verifyGen.current) return   // superseded by a refresh/unmount
      const outcome = applyDetails(batch, resp.details || {})
      removed += outcome.recovered
      gone += outcome.gone
      const done = Math.min(off + batch.length, targets.length)
      setVerify({ running: done < targets.length, done, total: targets.length, removed, failed: null })
    }
    if (removed > 0) {
      toast(`${removed} torrent${removed !== 1 ? 's' : ''} re-registered on its tracker since the last audit — removed from the list`, 'info')
    }
    if (gone > 0) {
      toast(`${gone} torrent${gone !== 1 ? 's are' : ' is'} no longer in your client since the last audit — removed from the list`, 'info')
    }
  }, [applyDetails, toast])

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    setSelected(new Set())
    setVerify(null)
    setRescanned(new Set())
    setUnconfirmed({})
    verifyGen.current++   // cancel any in-flight verification
    api.triageReport()
      .then(r => {
        // Drop anything acted on since the last audit — the server cannot know
        // yet, because its answer is that audit.
        const kept = (r?.items || []).filter(i => !DISMISSED.has(itemKey(i)))
        const rep  = { ...r, items: kept }
        setReport(rep)
        runVerify(kept)
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
    api.getConfig().then(cfg => {
      const isQui = cfg.TORRENT_SOURCE === 'qui'
      // Link address, not the API address — prefer the external URL when the
      // client is reached through a reverse proxy. Nothing here fetches it.
      const url = (isQui ? (cfg.QUI_EXTERNAL_URL || cfg.QUI_HOST)
                         : (cfg.QB_EXTERNAL_URL  || cfg.QB_HOST)) || ''
      setClient(url ? { name: isQui ? 'qui' : 'qBittorrent', url } : null)
      setClientDeleteAllowed(!!cfg.ALLOW_CLIENT_DELETE)
    }).catch(() => {})
  }, [runVerify])

  const openInClient = useCallback((item, e) => {
    e.stopPropagation()
    if (!client) return
    // qui deep-links straight to the torrent: /instances/{id}?torrent={hash}
    // selects it and opens the details pane. Stock qBittorrent's WebUI reads
    // no URL params, so it gets the copy-title-and-paste flow instead.
    if (canDeepLink(client, item)) {
      window.open(`${client.url.replace(/\/+$/, '')}/instances/${item.instance_id}?torrent=${item.hash}`, '_blank', 'noopener')
      return
    }
    const term = torrentSearchName(item)
    copyText(term)
    window.open(client.url, '_blank', 'noopener')
    toast(`“${term}” copied — paste it into the ${client.name} search box to find this torrent`, 'info')
  }, [client, toast])

  useEffect(() => {
    load()
    return () => { verifyGen.current++; importGen.current++ }   // stop polling after unmount
  }, [load])

  // Phase 1 answers from the *last audit's* stored rows, so a fix that changes
  // the filesystem — a rescan that imports, a delete — cannot show up until a
  // scan has run. Hooking the audit-complete event is therefore the only thing
  // that actually clears those rows; the actions themselves nudge the watchdog
  // so that scan comes within a cooldown rather than at the next scheduled one.
  useAuditComplete(useCallback(() => { DISMISSED.clear(); load() }, [load]))

  const items = report?.items || []
  const byVerdict = useMemo(() => {
    const m = {}
    for (const v of VERDICTS) m[v.key] = []
    for (const item of items) (m[item.verdict] || (m[item.verdict] = [])).push(item)
    // Largest-first within each verdict — keeps ordering stable when live
    // verification moves an item into a different bucket.
    for (const k of Object.keys(m)) m[k].sort((a, b) => b.total_size - a.total_size)
    return m
  }, [items])

  const selectedItems = useMemo(() => items.filter(i => selected.has(itemKey(i))), [items, selected])
  // What an action on the selection touches: each torrent in full where the row
  // knows it (T5) — `torrent_size` arrives with verify, `torrent_files` with the audit.
  const selectedSize  = selectedItems.reduce((s, i) => s + (i.torrent_size ?? i.total_size), 0)
  const selectedFiles = selectedItems.reduce((s, i) => s + Math.max(i.torrent_files || 0, i.file_count || 0), 0)

  const toggle = useCallback(key => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }, [])

  const toggleSection = useCallback((sectionItems) => {
    setSelected(prev => {
      const keys = sectionItems.map(itemKey)
      const allIn = keys.every(k => prev.has(k))
      const next = new Set(prev)
      keys.forEach(k => allIn ? next.delete(k) : next.add(k))
      return next
    })
  }, [])

  // Items deletable through the client need a torrent hash; path-keyed
  // entries (hash unknown) can only be handled manually.
  const deletableItems = selectedItems.filter(i => i.hash)

  // Superseded items are the only ones a force import applies to — the rest
  // have no library file to replace. Of those, only same-quality ones qualify;
  // the action is absent rather than disabled for the others, because the rows
  // already sit under a "lower than library" heading that says why.
  const forceImportItems = selectedItems.filter(i => i.verdict === 'superseded' && canForceImport(i))

  // Same shape as forceImportItems: the action applies to the rows it makes
  // sense for, and is absent when the selection holds none of them.
  const excludableItems = selectedItems.filter(canExclude)
  const excludePatterns = useMemo(
    () => [...new Set(excludableItems.flatMap(i => i.exclusion_patterns))],
    [excludableItems])

  // A mixed selection says what it left out and why — a row that silently
  // drops out of an action is the thing this page is built not to do.
  const excludeSkippedNote = useMemo(() => {
    const skipped = selectedItems.filter(i => !canExclude(i))
    if (skipped.length === 0) return null
    const lead = `${skipped.length} selected row${skipped.length !== 1 ? 's are' : ' is'} not included`
    return skipped.every(i => i.verdict === 'dead_registration')
      ? `${lead}: a dead registration is one your tracker dropped, but the files under it belong to a cross-seed that is still seeding. Hiding the file would not retire the registration — removing it from your client is what does.`
      : `${lead}.`
  }, [selectedItems])

  // Resolve each selected torrent's live cross-seed group — everything removing
  // it touches, and who else holds each file — so the modal can offer "this
  // torrent only" or the whole group and show, per torrent, the file decision
  // the server will hold the removal to.
  const resolveGroups = useCallback(async () => {
    setResolveError(null)
    setAmbiguity(null)
    setResolving(true)
    try {
      // Registrations, not hashes: the server refuses a hash two instances hold
      // unless it is told which.
      const resp = await api.triageResolveGroups(
        deletableItems.map(i => ({ hash: i.hash, instance_id: i.instance_id })))
      setGroups(resp.groups || {})
      setGroupsMeta({ checked: resp.checked !== false, unknown_listings: resp.unknown_listings || 0,
                      bounded: !!resp.bounded, missing: resp.missing || [] })
    } catch (e) {
      if (e.code === 'registration_ambiguous') setAmbiguity(e.data)
      setResolveError(e.message)
      setGroups({})
      setGroupsMeta(null)
    }
    setResolving(false)
  }, [deletableItems])

  const openConfirm = useCallback(() => {
    setConfirmOpen(true)
    setGroups({})
    setGroupsMeta(null)
    setScopes(() => {
      const m = {}
      for (const i of deletableItems) m[itemKey(i)] = defaultScope(i)
      return m
    })
    resolveGroups()
  }, [deletableItems, resolveGroups])

  const setItemScope = useCallback((key, scope) => {
    setScopes(prev => ({ ...prev, [key]: scope }))
  }, [])

  const setAllScopes = useCallback((scope) => {
    setScopes(() => {
      const m = {}
      for (const i of deletableItems) m[itemKey(i)] = scope
      return m
    })
  }, [deletableItems])

  // The removal the modal is about to post, and everything it shows: the deduped
  // torrents honouring each row's scope ('all' pulls the whole group, 'one' the
  // recorded torrent), each one's file decision, and the groups as shown — which
  // the server binds the removal to. A torrent no longer in the client has
  // nothing to remove. A resolve that failed keeps every file.
  const removalPlan = useMemo(() => {
    const checked = !resolveError && groupsMeta?.checked !== false
    // Keyed by registration throughout (S05) — `shares_with` names registrations,
    // so the removal set it is tested against has to as well.
    const byReg = new Map(), members = new Map(), shown = {}
    for (const item of deletableItems) {
      const grp = groups[regKey(item)] || []
      if (!resolveError && grp.length === 0) continue
      shown[regKey(item)] = grp.map(regKey)
      grp.forEach(m => members.set(regKey(m), m))
      if ((scopes[itemKey(item)] || 'one') === 'all' && grp.length > 0) {
        for (const m of grp) byReg.set(regKey(m), { hash: m.hash, instance_id: m.instance_id })
      } else {
        byReg.set(regKey(item), { hash: item.hash, instance_id: item.instance_id })
      }
    }
    const removal = new Set(byReg.keys())
    const files = {}
    for (const k of removal) {
      files[k] = resolveError ? { files: 'keep', reason: 'unknown' } : fileDecision(members.get(k), removal, checked)
    }
    return { items: [...byReg.values()], removal, files, groups: shown, checked }
  }, [deletableItems, groups, groupsMeta, scopes, resolveError])

  // ── Rescan follow-through ──────────────────────────────────────────────────
  //
  // A rescan is the one action whose result arrives from outside auditorr: the
  // command hands the file to Sonarr/Radarr, which imports on its own schedule
  // and tells us nothing. The arr's own file id is the honest signal that it
  // landed — the same one force_import confirms with, and for the same reason
  // (the command status is not trustworthy). So: snapshot, then watch it move.
  //
  // Cheap by construction — one arr call per selected row, a handful of polls,
  // and it stops the moment every row has answered.
  const importGen = useRef(0)

  //
  // T11 — a Sonarr row is watched by its own episodes, not the whole series: the
  // series' file ids move whenever Sonarr imports any episode of it, so a busy
  // series used to retire rows whose file never landed. `episodes` is every
  // video's episode when they all name one; without it the row's season is watched.
  const descriptorsFor = items => items.map(i => {
    const d = { key: itemKey(i), service: i.library.service,
                connection_id: i.library.connection_id, arr_id: i.library.arr_id }
    if (i.library.service === 'sonarr' && i.parsed?.season != null) {
      d.season = i.parsed.season
      if (i.episodes?.length) d.episodes = i.episodes
    }
    return d
  })

  const snapshotFileIds = useCallback(async items => {
    try {
      const resp = await api.importCheck(descriptorsFor(items))
      const m = {}
      for (const r of resp.results || []) if (r.checked) m[r.key] = JSON.stringify(r.file_id ?? null)
      return m
    } catch (_) {
      return null   // no baseline, so no watch — the audit still clears the row
    }
  }, [])

  const watchForImport = useCallback(async (items, baseline) => {
    const gen = ++importGen.current
    let waiting = items.filter(i => itemKey(i) in baseline)
    for (let attempt = 0; attempt < 20 && waiting.length; attempt++) {
      await new Promise(r => setTimeout(r, 6000))
      if (gen !== importGen.current) return          // superseded or unmounted
      let resp
      try { resp = await api.importCheck(descriptorsFor(waiting)) } catch (_) { return }
      if (gen !== importGen.current) return
      // Only a *checked* answer that differs from the baseline counts. An arr
      // that could not be reached reports checked:false, which must never read
      // as "the file changed" and retire a row that is still a problem.
      const landed = new Set((resp.results || [])
        .filter(r => r.checked && JSON.stringify(r.file_id ?? null) !== baseline[r.key])
        .map(r => r.key))
      if (landed.size) {
        landed.forEach(k => DISMISSED.add(k))
        setReport(r => ({ ...r, items: (r?.items || []).filter(i => !landed.has(itemKey(i))) }))
        toast(`${landed.size} import${landed.size === 1 ? '' : 's'} confirmed by Sonarr/Radarr`, 'success')
        waiting = waiting.filter(i => !landed.has(itemKey(i)))
      }
    }
  }, [toast])

  // S09 — only rows whose own torrent the client no longer lists leave the page.
  // A removal reports what it *submitted*; the server looks afterwards and says
  // per torrent `removed`, `already_gone`, `still_listed`, or `unknown` (its
  // instance did not answer). The rest stay, with a chip, until the audit lands.
  // A row removed as another row's cross-seed leaves too.
  // Outcomes are per **registration** (S05): the same torrent still registered
  // on another instance no longer reads as this row's removal being unconfirmed.
  const finishRemoval = (resp, failure) => {
    const outcomes = resp.outcomes || {}
    const left = new Set(Object.keys(outcomes).filter(k => outcomes[k] === 'removed' || outcomes[k] === 'already_gone'))
    const keys = new Set((report?.items || []).filter(i => i.hash && left.has(regKey(i))).map(itemKey))
    keys.forEach(k => DISMISSED.add(k))
    setReport(r => ({ ...r, items: (r?.items || []).filter(i => !keys.has(itemKey(i))) }))
    setSelected(prev => new Set([...prev].filter(k => !keys.has(k))))
    const stuck = deletableItems.filter(i => outcomes[regKey(i)] === 'still_listed' || outcomes[regKey(i)] === 'unknown')
    if (stuck.length) {
      setUnconfirmed(prev => ({ ...prev, ...Object.fromEntries(stuck.map(i => [itemKey(i), outcomes[regKey(i)]])) }))
    }
    const parts = [`${resp.removed ?? 0} torrent${resp.removed !== 1 ? 's' : ''} removed from ${client?.name || 'the client'}`]
    if (resp.files_deleted) parts.push(`${resp.files_deleted} with files deleted`)
    if (resp.files_kept)    parts.push(`${resp.files_kept} with files kept`)
    if (stuck.length)       parts.push(`${stuck.length} not confirmed`)
    toast(failure ? `${failure} — ${parts.join(' · ')}` : parts.join(' · '),
          failure ? 'error' : stuck.length ? 'warning' : 'success')
  }

  const handleClientDelete = async () => {
    setBusy('delete')
    const plan = removalPlan
    try {
      // A resolve that failed could not check what else uses these files, so the
      // removal keeps every one of them, as the modal said before confirm.
      // Otherwise the plan binds: the server resolves again and refuses
      // (409 plan_changed) if a group or a file decision moved towards deleting.
      const resp = resolveError
        ? await api.removeTorrents(plan.items, false)
        : await api.removeTorrents(plan.items, 'auto', {
            seeds:  Object.keys(plan.groups),
            groups: plan.groups,
            files:  Object.fromEntries(Object.entries(plan.files).map(([h, d]) => [h, d.files])),
          })
      finishRemoval(resp)
      setConfirmOpen(false)
    } catch (e) {
      if (e.code === 'plan_changed') {
        // Re-show: the modal stays open on a fresh answer, with the scopes kept.
        toast(e.message, 'warning')
        setBusy(null)
        resolveGroups()
        return
      }
      if (e.code === 'registration_ambiguous') {
        // Nothing was removed; the modal says which instances and why.
        setAmbiguity(e.data)
        setBusy(null)
        return
      }
      if (e.data?.outcomes) finishRemoval(e.data, e.message)
      else toast(e.message, 'error')
    }
    setBusy(null)
  }

  const handleRescan = async () => {
    setBusy('rescan')
    const sonarrPaths = []
    const radarrPaths = []
    for (const item of selectedItems) {
      const svc = item.library?.service
      if (svc === 'radarr') radarrPaths.push(item.rep_path)
      else if (svc === 'sonarr') sonarrPaths.push(item.rep_path)
      else { sonarrPaths.push(item.rep_path); radarrPaths.push(item.rep_path) }
    }
    // Snapshot the arr's file ids *before* the scan command, so the watcher
    // below has something to compare against. Only items matched to a library
    // entry can be watched; the rest fall back to clearing on the next audit.
    const watchable = selectedItems.filter(
      i => i.library?.arr_id != null && i.library?.connection_id && i.library?.service)
    const baseline = watchable.length ? await snapshotFileIds(watchable) : null
    let ok = 0
    const results = []
    const collect = r => { results.push(...(r?.results || [])); ok++ }
    try { if (sonarrPaths.length) { collect(await api.sonarrRescan(sonarrPaths)) } } catch (e) { toast(e.message, 'error') }
    try { if (radarrPaths.length) { collect(await api.radarrRescan(radarrPaths)) } } catch (e) { toast(e.message, 'error') }
    if (ok > 0) {
      const rejected = results.filter(r => (r.rejections || []).length > 0)
      if (rejected.length === 0) {
        // Mark the whole submitted batch rather than matching results back to
        // rows: the arr reports per *scan target*, which is the release folder
        // or the file itself, not the rep_path that was sent.
        const keys = selectedItems.map(itemKey)
        setRescanned(prev => new Set([...prev, ...keys]))
        toast('Rescan sent to Sonarr/Radarr — watching for the import', 'success')
        if (baseline) watchForImport(watchable, baseline)
      } else if (rejected.length === results.length) {
        toast(`Nothing will import — ${rejectionSummary(rejected)}`, 'error')
      } else {
        toast(`${results.length - rejected.length} scanning · ${rejected.length} refused — ${rejectionSummary(rejected)}`, 'warning')
      }
    }
    setBusy(null)
  }

  // "Import Anyway" — the only way past the arr's upgrade/revision specs, which
  // a same-quality trump replacement can never satisfy. Replaces the library
  // file, so it is deliberately restricted to same-quality items.
  const handleForceImport = async () => {
    setBusy('force')
    try {
      const resp = await api.forceImport(forceImportItems.map(i => ({
        key:           itemKey(i),
        service:       i.library.service,
        connection_id: i.library.connection_id,
        arr_id:        i.library.arr_id,
        paths:         i.paths,
      })))
      const done   = new Set((resp.results || []).filter(r => r.imported).map(r => r.key))
      const failed = (resp.results || []).filter(r => !r.imported)
      if (done.size) {
        toast(`Imported ${done.size} of ${resp.requested} — the library file was replaced`, 'success')
        done.forEach(k => DISMISSED.add(k))
        setReport(r => ({ ...r, items: (r?.items || []).filter(i => !done.has(itemKey(i))) }))
        setSelected(new Set())
      }
      if (failed.length) toast(failed[0].message || 'Import did not complete', 'error')
    } catch (e) {
      toast(e.message, 'error')
    }
    setBusy(null)
  }

  const handleExclude = async () => {
    setBusy('exclude')
    try {
      const resp = await api.excludePatterns(excludePatterns)
      toast(resp.message || `Added ${resp.added} exclusion rules`,
            resp.refused ? 'warning' : 'success')
      // Only the rows an exclusion actually covers leave; a dead registration
      // in the same selection was never part of this action.
      const keys = new Set(excludableItems.map(itemKey))
      keys.forEach(k => DISMISSED.add(k))
      setReport(r => ({ ...r, items: (r?.items || []).filter(i => !keys.has(itemKey(i))) }))
      setSelected(prev => new Set([...prev].filter(k => !keys.has(k))))
      setConfirmExclude(false)
    } catch (e) {
      toast(e.message, 'error')
    }
    setBusy(null)
  }

  // One-click exclude for a suggested junk category. The real exclusion
  // applies on the next audit, so drop the matching rows now (by the
  // suggestion's lowercase match substrings) to clear the reminder.
  //
  // No confirm step here, deliberately: the chip *is* the pattern — it shows
  // `ext:sfv` / `contains:sample` on its face before you click it, which is
  // what the confirm dialog exists to do for a constructed path. These are
  // typed rules, never raw paths, so no metacharacter can break them
  // (measured — ROADMAP §0.5); the caps still apply, hence the refusal path.
  const handleSuggestionExclude = async (sugg) => {
    setBusy('suggest')
    try {
      const resp = await api.excludePatterns(sugg.patterns)
      toast(resp.refused
        ? resp.message
        : `Excluding ${sugg.patterns.join(', ')} — added ${resp.added} rule${resp.added !== 1 ? 's' : ''}, visible in Config → Excluded Files`,
        resp.refused ? 'warning' : 'success')
      const hit = p => { const lp = p.toLowerCase(); return (sugg.match || []).some(m => lp.includes(m)) }
      for (const i of (report?.items || [])) if (hit(i.rep_path)) DISMISSED.add(itemKey(i))
      setReport(r => ({
        ...r,
        items: (r?.items || []).filter(i => !hit(i.rep_path)),
        suggestions: (r?.suggestions || []).filter(s => s.id !== sugg.id),
      }))
    } catch (e) {
      toast(e.message, 'error')
    }
    setBusy(null)
  }

  return (
    <WorkflowPage>
      <WorkflowHeader
        title="Triage"
        accent="var(--red)"
        blurb="Every torrent that needs your attention: dead on the tracker (imported or not), quality superseded, import failures, or not in your library at all — and what to do about each."
        /* No Refresh button: the page re-reads itself when an audit lands
           (`useAuditComplete`), which is the only event that can change what it
           shows. A button here would have been a stale page with a button. */
      />

      <WorkflowError message={error} />

      {!loading && (
        <WorkflowCrossLink
          text="Everything here has an active torrent. Files with no torrent attached:"
          linkLabel="Cleanup"
          count={cleanupCount}
          onClick={() => onNavigate && onNavigate({ tab: 'cleanup' })}
        />
      )}
      {!loading && (
        <WorkflowCrossLink
          text="Got a trump PM for a dead seed? Swap the whole cross-seed group for the replacement:"
          linkLabel="Trumped"
          count={trumpedCount}
          onClick={() => onNavigate && onNavigate({ tab: 'trumped' })}
        />
      )}

      {loading && <LoadingRow label="Reading the last audit and matching against your Sonarr/Radarr libraries…" />}

      {!loading && verify && (verify.running || verify.failed) && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, padding: '9px 14px', flexWrap: 'wrap',
          background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--r)',
        }}>
          {verify.running ? (
            <>
              <Spinner />
              <span style={{ fontSize: 'var(--font-base)', color: 'var(--text)' }}>Verifying live tracker status…</span>
              <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
                {verify.done}/{verify.total} checked{verify.removed > 0 ? ` · ${verify.removed} recovered` : ''}
              </span>
              <span style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', marginLeft: 'auto' }}>
                Showing audit-time data meanwhile — rows may move or drop as trackers answer
              </span>
            </>
          ) : (
            <>
              <span style={{ fontSize: 'var(--font-base)', color: 'var(--yellow)' }}>
                Live tracker verification failed ({verify.failed}) — showing audit-time tracker data
                {verify.done > 0 ? ` (${verify.done}/${verify.total} verified before the error)` : ''}.
              </span>
              <Button size="sm" onClick={() => runVerify(items.filter(i => i.hash && !i.verified))}>
                ↻ Retry
              </Button>
            </>
          )}
        </div>
      )}

      {!loading && !error && items.length === 0 && (
        <EmptyState
          title="Nothing to triage"
          sub="Every seeding torrent is imported and registered on its tracker. All clear."
        />
      )}

      {!loading && items.length > 0 && (
        <>
          {report?.truncated && (
            <div style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', fontFamily: 'var(--mono)' }}>
              {report.total != null
                ? `Showing the ${report.shown} largest of ${report.total} torrents — resolve some to see the rest.`
                : 'Showing the largest torrents — resolve some to see the rest.'}
            </div>
          )}
          {!report?.arr_configured && (
            <WorkflowWarning>
              No Sonarr/Radarr connection configured — library matching is disabled, so most items fall into “Not in Library”.
            </WorkflowWarning>
          )}

          <ArrErrorsWarning
            errors={report?.arr_errors}
            extra={'Library matching is incomplete, so “Could Not Check” replaces “Not in Library” for anything that matched nothing, '
                 + 'and some rows below may read “Import Pending” for files that are in fact imported.'}
          />

          {report?.suggestions?.length > 0 && (
            <div style={{ padding: '12px 14px', background: 'var(--surface)', border: '1px dashed var(--border2)', borderRadius: 'var(--r)', display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ fontSize: 'var(--font-base)', color: 'var(--text)' }}>
                <span style={{ fontWeight: 600 }}>Tidy up Triage</span>
                <span style={{ color: 'var(--text-dim)' }}> — some torrents only linger here because of files Sonarr/Radarr never import. Exclude them in one click (saved to Config → Excluded Files, editable there):</span>
              </div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {report.suggestions.map(s => (
                  <Button
                    key={s.id} size="sm"
                    onClick={() => handleSuggestionExclude(s)}
                    disabled={busy != null}
                    title={`${s.detail} — adds ${s.patterns.join(', ')}`}
                  >
                    <span>Exclude <span style={{ fontFamily: 'var(--mono)', color: 'var(--accent)' }}>{s.label}</span></span>
                    <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', fontWeight: 400 }}>
                      {s.count} · {formatBytes(s.size)}
                    </span>
                  </Button>
                ))}
              </div>
            </div>
          )}

          {VERDICTS.map(v => {
            const sectionItems = byVerdict[v.key] || []
            if (sectionItems.length === 0) return null
            const sectionSize = sectionItems.reduce((s, i) => s + i.total_size, 0)
            const keys = sectionItems.map(itemKey)
            const allChecked  = keys.every(k => selected.has(k))
            const someChecked = !allChecked && keys.some(k => selected.has(k))

            const renderRows = (rowItems) => (
              <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)', overflow: 'hidden' }}>
                {rowItems.map(item => (
                  <TriageRow
                    key={itemKey(item)}
                    item={item}
                    color={v.color}
                    checked={selected.has(itemKey(item))}
                    onToggle={() => toggle(itemKey(item))}
                    client={client}
                    onOpenClient={openInClient}
                    onNavigate={onNavigate}
                    pending={!!verify?.running && !!item.hash && !item.verified}
                    rescanned={rescanned.has(itemKey(item))}
                    unconfirmed={unconfirmed[itemKey(item)]}
                  />
                ))}
              </div>
            )

            return (
              <div key={v.key} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <SectionHeading
                  check={{ checked: allChecked, indeterminate: someChecked, onChange: () => toggleSection(sectionItems) }}
                  dot={v.color} title={v.label}
                  meta={`${sectionItems.length} · ${formatBytes(sectionSize)}`}
                  desc={v.desc}
                />
                {v.key !== 'superseded' ? renderRows(sectionItems) : (
                  QUALITY_BUCKETS.map(b => {
                    const bucketItems = sectionItems.filter(i => qualityBucket(i) === b.key)
                    if (bucketItems.length === 0) return null
                    const bKeys = bucketItems.map(itemKey)
                    const bAll  = bKeys.every(k => selected.has(k))
                    const bSome = !bAll && bKeys.some(k => selected.has(k))
                    const bSize = bucketItems.reduce((s, i) => s + i.total_size, 0)
                    // Indented to the verdict's title, so a bucket's checkbox sits
                    // under the heading it belongs to.
                    return (
                      <div key={b.key} style={{ marginLeft: 42, marginBottom: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
                        <SectionHeading sub
                          check={{ checked: bAll, indeterminate: bSome, onChange: () => toggleSection(bucketItems) }}
                          color={b.color} title={b.label}
                          meta={`${bucketItems.length} · ${formatBytes(bSize)}`}
                          desc={b.desc}
                        />
                        {renderRows(bucketItems)}
                      </div>
                    )
                  })
                )}
              </div>
            )
          })}

          {selectedItems.length > 0 && (
            <ActionBar summary={`${selectedItems.length} torrent${selectedItems.length !== 1 ? 's' : ''} selected · ${selectedFiles} file${selectedFiles !== 1 ? 's' : ''} · ${formatBytes(selectedSize)}`}>
              <Button onClick={handleRescan} disabled={busy != null} title="Tell Sonarr/Radarr to rescan these folders and retry the import">
                {busy === 'rescan' ? 'Rescanning…' : 'Trigger Rescan'}
              </Button>
              {forceImportItems.length > 0 && (
                <Button onClick={handleForceImport} disabled={busy != null}
                  title={`Replace the library file with this release via Sonarr/Radarr's "Import Anyway" — the only way past the upgrade check a same-quality swap can never pass`}>
                  {busy === 'force' ? 'Importing…' : `Force import (${forceImportItems.length})`}
                </Button>
              )}
              {excludableItems.length > 0 && (
                <Button onClick={() => setConfirmExclude(true)} disabled={busy != null} title="Add exclusion rules so auditorr stops flagging these">
                  {busy === 'exclude' ? 'Excluding…' : 'Exclude'}
                </Button>
              )}
              {client && (clientDeleteAllowed ? (
                <Button variant="danger" onClick={openConfirm} disabled={busy != null || deletableItems.length === 0}
                  title={deletableItems.length === 0
                    ? 'None of the selected items have a torrent hash'
                    : `Remove the selected torrents via ${client.name} — files deleted only where no live seed shares them`}>
                  Remove from {client.name}
                </Button>
              ) : (
                // Deletion is off: show the action greyed out (so users know it
                // exists) with a one-click path to enable it, mirroring Trumped.
                <>
                  <span style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', alignSelf: 'center' }}>
                    Deleting via {client.name} is off —{' '}
                    <a onClick={() => onNavigate && onNavigate({ tab: 'config' })}
                      style={{ color: 'var(--yellow)', cursor: 'pointer', textDecoration: 'underline' }}>enable in Config</a>
                  </span>
                  <Button variant="danger" disabled
                    title={`Client deletion is disabled — enable “Workflow torrent deletion” in Config → Torrent Source to remove torrents via ${client.name}`}>
                    Remove from {client.name}
                  </Button>
                </>
              ))}
            </ActionBar>
          )}

          {confirmExclude && excludePatterns.length > 0 && (
            <ConfirmExcludeModal
              patterns={excludePatterns}
              subtitle={`Built from ${excludableItems.length} selected torrent${excludableItems.length !== 1 ? 's' : ''}.`}
              note={excludeSkippedNote}
              busy={busy === 'exclude'}
              onCancel={() => setConfirmExclude(false)}
              onConfirm={handleExclude}
            />
          )}

          {confirmOpen && (
            <ConfirmDeleteModal
              items={deletableItems}
              groups={groups}
              plan={removalPlan}
              meta={groupsMeta}
              scopes={scopes}
              resolving={resolving}
              resolveError={resolveError}
              ambiguity={ambiguity}
              onSetItemScope={setItemScope}
              onSetAllScopes={setAllScopes}
              skippedCount={selectedItems.length - deletableItems.length}
              clientName={client?.name || 'client'}
              busy={busy === 'delete'}
              onCancel={() => setConfirmOpen(false)}
              onConfirm={handleClientDelete}
            />
          )}
        </>
      )}
      <SpinKeyframes />
      <style>{`@keyframes triagePulse { 0%, 100% { opacity: .25 } 50% { opacity: .75 } }`}</style>
    </WorkflowPage>
  )
}

// A small two-way scope switch: delete just the recorded torrent, or the whole
// hardlinked cross-seed group.
function ScopeSwitch({ scope, groupSize, onChange }) {
  const opts = [
    { key: 'one', label: 'This torrent only' },
    { key: 'all', label: `All ${groupSize} cross-seeds` },
  ]
  return (
    <div style={{ display: 'inline-flex', border: '1px solid var(--border2)', borderRadius: 6, overflow: 'hidden' }}>
      {opts.map((o, idx) => {
        const active = scope === o.key
        return (
          <button
            key={o.key}
            onClick={() => onChange(o.key)}
            style={{
              fontSize: 'var(--font-base)', fontFamily: 'var(--mono)', padding: '3px 9px', cursor: 'pointer',
              border: 'none', borderLeft: idx ? '1px solid var(--border2)' : 'none',
              background: active ? (o.key === 'all' ? tint('var(--red)', 13) : 'var(--surface3)') : 'transparent',
              color: active ? (o.key === 'all' ? 'var(--red)' : 'var(--text)') : 'var(--text-dim)',
              fontWeight: active ? 700 : 400,
            }}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

function ConfirmDeleteModal({
  items, groups, plan, meta, scopes, resolving, resolveError, ambiguity,
  onSetItemScope, onSetAllScopes, skippedCount, clientName, busy, onCancel, onConfirm,
}) {
  const settled   = !resolving && !resolveError
  const missing   = settled ? items.filter(i => (groups[regKey(i)] || []).length === 0) : []
  const totalSize = items.filter(i => !missing.includes(i))
                         .reduce((s, i) => s + (i.torrent_size ?? i.total_size), 0)
  const anyGroups = items.some(i => (groups[regKey(i)] || []).length > 1)
  const unchecked = settled && meta && !meta.checked

  const torrentCount = plan.items.length
  const deleting = Object.values(plan.files).filter(d => d.files === 'delete').length
  // Remove's label states the file outcome, so the button is never the first
  // place a user learns which files go.
  const confirmLabel = busy ? 'Removing…'
    : `Remove ${torrentCount} torrent${torrentCount !== 1 ? 's' : ''}${
        deleting === 0 ? ' · keep files'
          : deleting === torrentCount ? ' and their files'
          : ` · delete files of ${deleting}`}`

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  // One torrent's file decision, with the reason on hover.
  const decisionTag = h => {
    const d = plan.files[h]
    if (!d) return null
    const kept = d.files === 'keep'
    const unsure = d.reason === 'unknown' || d.reason === 'unusable_listing'
    return (
      <span title={`${kept ? 'Files kept' : 'Files deleted'} — ${FILE_REASON[d.reason] || d.reason}`}
            style={{ flexShrink: 0, color: kept ? (unsure ? 'var(--yellow)' : 'var(--text-dim)') : 'var(--red)' }}>
        {kept ? (unsure ? 'files kept — not checked' : 'files kept') : 'files deleted'}
      </span>
    )
  }

  // Portal to <body>: the page container's fade-in animation leaves a
  // persistent transform, which makes position:fixed resolve against the
  // (very tall) page instead of the viewport — the dialog would center
  // thousands of pixels off-screen, leaving only the dimmed backdrop.
  return createPortal(
    <div
      onClick={onCancel}
      style={{
        position: 'fixed', inset: 0, zIndex: 200, display: 'flex',
        alignItems: 'center', justifyContent: 'center',
        background: 'rgba(0,0,0,0.55)',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: 'min(560px, calc(100vw - 48px))', maxHeight: 'calc(100vh - 96px)',
          display: 'flex', flexDirection: 'column',
          background: 'var(--surface)', border: '1px solid var(--border2)',
          borderRadius: 12, boxShadow: '0 16px 60px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ padding: '18px 20px 0' }}>
          <div style={{ fontSize: 'var(--font-lg)', fontWeight: 700, color: 'var(--red)' }}>Remove from {clientName}</div>
          <p style={{ fontSize: 'var(--font-base)', color: 'var(--text)', lineHeight: 1.6, margin: '10px 0 0' }}>
            Removes <b>{torrentCount} torrent{torrentCount !== 1 ? 's' : ''}</b> (<b>{formatBytes(totalSize)}</b>) from {clientName}.
            A torrent’s files are deleted <b>only</b> where auditorr established that nothing staying in {clientName} uses
            them; files a remaining torrent shares, and files it could not check, are kept. Each torrent below says which. There is no undo.
          </p>
          {skippedCount > 0 && (
            <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: '8px 0 0' }}>
              {skippedCount} selected item{skippedCount !== 1 ? 's have' : ' has'} no torrent hash and will be skipped.
            </p>
          )}
          {/* A failed resolve never reads as "no cross-seeds": it is "could not
              check", and removing then keeps every file (S01, decision a). */}
          {ambiguity && (
            <div style={{ margin: '8px 0 0' }}><RegistrationWarning refusal={ambiguity} /></div>
          )}
          {resolveError && !ambiguity && (
            <p style={{ fontSize: 'var(--font-base)', color: 'var(--yellow)', margin: '8px 0 0', lineHeight: 1.5 }}>
              Couldn’t check for cross-seeds ({resolveError}), so auditorr can’t tell what else uses these files.
              Removing keeps every file: the torrents leave {clientName} and their files stay on disk, where Cleanup can check them later.
            </p>
          )}
          {unchecked && (
            <p style={{ fontSize: 'var(--font-base)', color: 'var(--yellow)', margin: '8px 0 0', lineHeight: 1.5 }}>
              {meta.unknown_listings > 0
                ? `${meta.unknown_listings} torrent${meta.unknown_listings !== 1 ? 's' : ''} in ${clientName} did not return a file list${meta.bounded ? ', and the search stopped at its limit' : ''}`
                : `${clientName} holds more near matches than auditorr checks at once`}
              , so any of them could be using these files. Files are kept for every torrent here.
            </p>
          )}
          {missing.length > 0 && (
            <p style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', margin: '8px 0 0' }}>
              {missing.length} selected torrent{missing.length !== 1 ? 's are' : ' is'} no longer in {clientName} — nothing to remove.
            </p>
          )}
          {anyGroups && !resolving && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '12px 0 0', fontSize: 'var(--font-base)', color: 'var(--text-dim)' }}>
              <span>Choose how much of each cross-seed group to remove. Apply to all:</span>
              <Button size="sm" onClick={() => onSetAllScopes('one')}>This torrent only</Button>
              <Button size="sm" onClick={() => onSetAllScopes('all')}>All cross-seeds</Button>
            </div>
          )}
        </div>
        <div style={{ margin: '14px 20px 0', border: '1px solid var(--border)', borderRadius: 8, overflowY: 'auto', flex: '0 1 auto' }}>
          {resolving && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 14px', fontSize: 'var(--font-base)', fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
              <Spinner /> Finding cross-seeds in {clientName}…
            </div>
          )}
          {items.map(item => {
            const grp   = groups[regKey(item)] || []
            // Two instances holding one torrent are two members with one name;
            // the instance is what tells them apart (S05).
            const multiInstance = new Set(grp.map(m => m.instance_id)).size > 1
            const scope = scopes[itemKey(item)] || 'one'
            const hasGroup = grp.length > 1
            const gone = settled && grp.length === 0
            const paths = (item.paths || []).map(p => p.replace(/\\/g, '/'))
            const shownPaths = paths.slice(0, 6)
            return (
              <div key={itemKey(item)} style={{ padding: '8px 12px', borderBottom: '1px solid var(--border)', opacity: gone ? 0.6 : 1 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                  <span style={{ ...ITEM_TITLE, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {item.parsed?.title || (item.rep_path || '').replace(/\\/g, '/').split('/').pop()}
                    {item.parsed?.year ? ` (${item.parsed.year})` : ''}
                  </span>
                  {item.seeding_time != null && (
                    <span title="Total time seeding" style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', flexShrink: 0 }}>
                      seeded {formatDuration(item.seeding_time)}
                    </span>
                  )}
                  <span style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text)', flexShrink: 0 }}>
                    {formatBytes(item.torrent_size ?? item.total_size)}
                  </span>
                </div>

                {hasGroup ? (
                  <>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
                      <ScopeSwitch scope={scope} groupSize={grp.length} onChange={s => onSetItemScope(itemKey(item), s)} />
                    </div>
                    <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 3 }}>
                      {grp.map(m => {
                        // Straight off the plan the page will post: a member another
                        // selected row removes is removed here too.
                        const willRemove = plan.removal.has(regKey(m))
                        return (
                          <div key={regKey(m)} title={m.name} style={{ display: 'flex', alignItems: 'baseline', gap: 8, fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)' }}>
                            <span style={{
                              flexShrink: 0, width: 48, color: willRemove ? 'var(--red)' : 'var(--text-dim)',
                              fontWeight: willRemove ? 700 : 400,
                            }}>
                              {willRemove ? 'remove' : 'keep'}
                            </span>
                            <span style={{ flex: 1, minWidth: 0, color: willRemove ? 'var(--text)' : 'var(--text-dim)', opacity: willRemove ? 1 : 0.7, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {m.name}
                            </span>
                            {willRemove && decisionTag(regKey(m))}
                            {multiInstance && m.instance_name && <span style={{ flexShrink: 0, color: 'var(--text-dim)' }}>{m.instance_name}</span>}
                            {m.tracker && <span style={{ flexShrink: 0, color: 'var(--text-dim)' }}>{m.tracker}</span>}
                            {m.seeding_time != null && <span style={{ flexShrink: 0, color: 'var(--text-dim)' }}>{formatDuration(m.seeding_time)}</span>}
                          </div>
                        )
                      })}
                    </div>
                  </>
                ) : (
                  <>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginTop: 3, fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
                      <span title={item.hash} style={{ flexShrink: 0 }}>hash {String(item.hash).slice(0, 12)}…</span>
                      {(item.trackers || [])[0] && <span style={{ flexShrink: 0 }}>{item.trackers[0]}</span>}
                      {!resolving && (
                        <span style={{ flexShrink: 0 }}>
                          {gone ? `no longer in ${clientName}`
                            : resolveError ? 'cross-seeds not checked'
                            : unchecked ? 'no cross-seeds found'
                            : 'no cross-seeds'}
                        </span>
                      )}
                      {!resolving && plan.removal.has(regKey(item)) && decisionTag(regKey(item))}
                    </div>
                    <div style={{ marginTop: 4 }}>
                      {shownPaths.map(p => (
                        <div key={p} title={p} style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', opacity: 0.75, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {p}
                        </div>
                      ))}
                      {paths.length > shownPaths.length && (
                        <div style={{ fontSize: 'var(--font-sm)', fontFamily: 'var(--mono)', color: 'var(--text-dim)', opacity: 0.6 }}>
                          +{paths.length - shownPaths.length} more files
                        </div>
                      )}
                    </div>
                  </>
                )}
              </div>
            )
          })}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '14px 20px 18px' }}>
          {busy && (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontSize: 'var(--font-base)', fontFamily: 'var(--mono)', color: 'var(--text-dim)' }}>
              <Spinner /> Removing via {clientName}…
            </span>
          )}
          <span style={{ flex: 1 }} />
          <Button onClick={onCancel}>{busy ? 'Continue in background' : 'Cancel'}</Button>
          <Button variant="danger" onClick={onConfirm} disabled={busy || resolving || torrentCount === 0}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>,
    document.body
  )
}
