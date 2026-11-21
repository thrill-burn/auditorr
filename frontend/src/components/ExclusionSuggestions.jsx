import React from 'react'
import { formatBytes } from '../utils'
import { Button, MONO_TITLE } from './workflows/shared'
import { fmtWhen } from './ScanCards'

// Settings → Excluded Files & Folders: what the last scan suggests excluding
// (`audit.exclusion_suggestions`, 2026-11-20). A new install has the
// always-on rules and nothing else; the folders a user would exclude are their
// own (a games category, a home-videos folder), so no fixed list can name them
// and the scan does, from what it found. The user's pick from a mockup.
//
// Every action here is the form's: *Add rule* puts the rule in the list below
// (a preset's *Turn on* lights it below), *Not this* marks it dismissed, and
// nothing is written until Save, like everything else on the page. Each row's
// done state is read off the form itself (the rule in the list, the preset on,
// the id dismissed), so editing the list by hand can't leave a row that says
// "Added" when it isn't. Once saved, the server leaves a dismissed suggestion,
// a rule already in the list and a preset already on out of what it sends.

const CAPTION = { fontFamily: 'var(--sans)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }
const READOUT = { fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }
const PRESET_LABEL = { bluray: 'Blu-ray Disc', dvd: 'DVD Disc' }
const PRESET_FOLDER = { bluray: 'BDMV', dvd: 'VIDEO_TS' }

function plural(n, one, many = one + 's') { return `${n.toLocaleString()} ${n === 1 ? one : many}` }

function kindOf(s) {
  if (s.kind === 'library') return 'Library folder'
  if (s.kind === 'preset') return 'Full disc rips'
  if (!s.category) return 'Torrent folder'
  if (s.category === s.folder) return 'Torrent category'
  return <>Torrent folder · category <span style={{ fontFamily: 'var(--mono)' }}>{s.category}</span></>
}

function whyOf(s) {
  if (s.kind === 'library') {
    return 'No Sonarr or Radarr folder is in it and nothing in it is seeded, so it counts against Hardlinked and no workflow can act on it.'
  }
  if (s.kind === 'preset') {
    return `Full ${s.preset === 'dvd' ? 'DVD' : 'Blu-ray'} rips hold hundreds of structure files that read as copies and as not imported. The preset ignores the structure, and standalone video files stay visible.`
  }
  const base = 'Nothing in it is in your library, so every file in it counts as not imported and is a Triage row.'
  return s.orphaned_size > 0
    ? `${base} Excluding it also takes its ${formatBytes(s.orphaned_size)} of orphaned files out of Cleanup.`
    : base
}

function figuresOf(s) {
  if (s.kind === 'torrents') return <span style={READOUT}>{plural(s.torrents, 'torrent')} · {formatBytes(s.size)}</span>
  if (s.kind === 'library') return <span style={READOUT}>{plural(s.files, 'file')} · {formatBytes(s.size)}</span>
  const d = s.discs || {}
  const where = [d.torrents && `${d.torrents.toLocaleString()} in the torrent folder`,
                 d.library && `${d.library.toLocaleString()} in the library`].filter(Boolean)
  const n = (d.torrents || 0) + (d.library || 0)
  // Words in it, so Geist, numbers and all.
  return <span style={CAPTION}>{plural(n, 'disc folder')}{where.length ? ` · ${where.join(', ')}` : ''}</span>
}

export default function ExclusionSuggestions({ suggestions, ruleLines, discPresets, dismissed, lastAuditTime,
                                               onAdd, onDismiss, onUndo }) {
  if (!suggestions?.length) return null
  return (
    <div style={{ marginBottom: 20 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', alignItems: 'baseline', gap: '4px 12px' }}>
        <span style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-md)', fontWeight: 600, color: 'var(--text)' }}>
          Suggested from your last scan
        </span>
        {/* As Last scan writes it. The state's time is "YYYY-MM-DD HH:MM:SS",
            which only some browsers parse with the space. */}
        {lastAuditTime && <span style={READOUT}>{fmtWhen(String(lastAuditTime).replace(' ', 'T'))}</span>}
      </div>
      <p style={{ ...CAPTION, lineHeight: 1.45, margin: '4px 0 10px', maxWidth: '70ch' }}>
        Folders that look like they aren't yours to audit. Each one is checked against the whole scan. Adding one
        puts its rule in the list below, and nothing changes until you save.
      </p>
      <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
        {suggestions.map((s, i) => {
          const added = s.rule ? ruleLines.includes(s.rule) : discPresets.includes(s.preset)
          const hidden = !added && dismissed.includes(s.id)
          const done = added || hidden
          const name = s.kind === 'preset' ? PRESET_FOLDER[s.preset] : s.folder
          return (
            <div key={s.id} style={{
              display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: '6px 16px', alignItems: 'start',
              padding: '12px 14px', borderTop: i ? '1px solid var(--border)' : 'none',
            }}>
              <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
                <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: '2px 10px', opacity: done ? 0.5 : 1 }}>
                  <span style={{ ...MONO_TITLE, overflowWrap: 'anywhere' }}>{name}</span>
                  <span style={CAPTION}>{kindOf(s)}</span>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 3, opacity: done ? 0.5 : 1 }}>
                  <span style={{ fontSize: 'var(--font-base)', color: 'var(--text-dim)', lineHeight: 1.45 }}>{whyOf(s)}</span>
                  {figuresOf(s)}
                  <span style={{ ...CAPTION, overflowWrap: 'anywhere' }}>
                    {s.rule
                      ? <>Adds <code style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text)' }}>{s.rule}</code></>
                      : <>Turns on the <span style={{ color: 'var(--text)', fontWeight: 600 }}>{PRESET_LABEL[s.preset]}</span> preset below</>}
                  </span>
                </div>
                {done && (
                  <span style={CAPTION}>
                    {added
                      ? <><span style={{ color: 'var(--green)' }}>
                          {s.rule ? 'Added to the list below.' : `${PRESET_LABEL[s.preset]} preset turned on below.`}
                        </span> It applies when you save, from the next scan.</>
                      : "Won't be suggested again once you save. You can still add the rule by hand."}
                  </span>
                )}
              </div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                {done ? (
                  <Button size="sm" variant="ghost" onClick={() => onUndo(s, added)}>Undo</Button>
                ) : (
                  <>
                    <Button size="sm" onClick={() => onAdd(s)}>{s.rule ? 'Add rule' : 'Turn on'}</Button>
                    <Button size="sm" variant="ghost" onClick={() => onDismiss(s)}>Not this</Button>
                  </>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
