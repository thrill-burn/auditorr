import React, { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import { FixedSizeList } from 'react-window'
import AutoSizer from 'react-virtualized-auto-sizer'
import { formatBytes, copyText, parseReleaseTitle, tint } from '../utils'
import { api } from '../api'
import { useToast } from './Toast'
import { Button, Segmented, FlagToggle, CloseButton, IconButton, SearchInput, Dot } from './workflows/shared'

// ─── Constants ────────────────────────────────────────────────────────────────

const FLAT_ITEM_HEIGHT = 50  // FlatFileRow: 2 lines + padding
const TREE_ITEM_HEIGHT = 36  // FolderRow / FileRow: 1 line + padding

// ─── Hooks ───────────────────────────────────────────────────────────────────

function useDebounce(value, delay) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(t)
  }, [value, delay])
  return debounced
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function detectMediaType(filePath) {
  const parts = filePath.toLowerCase().replace(/\\/g, '/').split('/')
  for (const part of parts) {
    if (/movie|film|radarr/.test(part)) return 'movie'
    if (/tv|television|show|series|sonarr/.test(part)) return 'tv'
  }
  return 'unknown'
}

// Open a torrent in the client. qui deep-links straight to the torrent by
// hash (/instances/{id}?torrent={hash} selects it with the details pane);
// qBittorrent's WebUI reads no URL params, so copy a searchable title for a
// one-paste search instead. The release folder (second path segment in TRaSH
// layouts) names the torrent better than a nested file's basename.
function openTorrentInClient(node, torrentSource, qbHost, quiHost, toast) {
  if (torrentSource === 'qui') {
    const base = (quiHost || '').replace(/\/+$/, '')
    const url = node.hash && node.instance_id != null
      ? `${base}/instances/${node.instance_id}?torrent=${node.hash}`
      : (node.instance_id != null ? `${base}/instances/${node.instance_id}` : base)
    window.open(url, '_blank', 'noopener')
    return
  }
  const segs = (node.path || '').replace(/\\/g, '/').split('/')
  const nameSource = segs.length >= 3 ? segs[1] : segs[segs.length - 1]
  const term = parseReleaseTitle(nameSource) || nameSource
  copyText(term)
  window.open(qbHost, '_blank', 'noopener')
  toast(`“${term}” copied — paste it into the qBittorrent search box to find this torrent`, 'info')
}

function clientLinkTitle(node, torrentSource) {
  if (torrentSource === 'qui') {
    return node.hash && node.instance_id != null ? 'Open this torrent in qui' : 'Open in qui'
  }
  return 'Copy the title and open qBittorrent — paste into its search box to find this torrent'
}

// ─── Primitives ──────────────────────────────────────────────────────────────

// A row's status word. It was written as a filled pill, but its background and
// hairline were glued-alpha tints the browser dropped, so for as long as it has
// existed it has rendered as coloured text — which is also what the
// ration-colour rule asks for on a list where every row carries one. Kept that
// way deliberately when the tints were fixed (R9); do not "restore" the fill.
function Tag({ color, children }) {
  return (
    <span style={{
      padding: '1px 7px', fontSize: 'var(--font-sm)', fontWeight: 600,
      fontFamily: 'var(--mono)', color, whiteSpace: 'nowrap', flexShrink: 0,
    }}>{children}</span>
  )
}

// A file row's two icon actions, shared by the tree and flat rows. They were
// the glyphs ⓘ and ⎘ in hand-rolled buttons, one copy per row renderer: fallback
// font glyphs at two sizes and two baselines, and the copy one in --text-faint,
// which on the light theme is #d6d3ce on white. Copying said nothing at all.
const ICON = { width: 12, height: 12, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true }

function PathsButton({ name, node, onOpenPopup }) {
  return (
    <IconButton size="sm" title="Show hardlinks & duplicates"
      onClick={e => { e.stopPropagation(); onOpenPopup({ name, linkedPaths: node.linked_paths, duplicatePaths: node.duplicate_paths, anchorRect: e.currentTarget.getBoundingClientRect() }) }}>
      <svg {...ICON}><circle cx="12" cy="12" r="10" /><path d="M12 16v-4M12 8h.01" /></svg>
    </IconButton>
  )
}

function CopyPathButton({ path, toast }) {
  return (
    <IconButton size="sm" title="Copy full path"
      onClick={e => { e.stopPropagation(); copyText(path || ''); toast('Path copied', 'success') }}>
      <svg {...ICON}><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
    </IconButton>
  )
}

// "Open in Sonarr/Radarr". Same story as Tag, and it was a hand-copy in both
// row renderers. Hover underlines, because the wash it used to set never drew.
function ArrSearchButton({ service, color, state, onClick }) {
  return (
    <button
      title={`Search in ${service}`}
      onClick={onClick}
      style={{
        background: 'none', border: 'none', padding: '1px 2px',
        color, fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', fontWeight: 600,
        cursor: 'pointer', flexShrink: 0, textDecoration: 'none',
      }}
      onMouseEnter={e => e.currentTarget.style.textDecoration = 'underline'}
      onMouseLeave={e => e.currentTarget.style.textDecoration = 'none'}
    >
      {state === 'loading' ? 'Opening…' : state === 'success' ? '✓ Opened' : state === 'error' ? '✗ Failed' : `Open in ${service}`}
    </button>
  )
}

// A dot before a filter option's label, the size the old chips drew it.
const optDot = color => <Dot color={color} size={6} />

function seedCountValue(file) {
  return (file.trackers || []).filter(t => t !== 'None').length
}

function seedCountMatches(selected, count) {
  if (selected === null) return true
  if (selected === '5plus' || selected >= 5) return count >= 5
  return count === selected
}

function seedCountLabel(value) {
  if (value === null) return 'All'
  if (value === '5plus' || value >= 5) return '5x+'
  return `${value}x`
}

function SeedCountMenu({ value, options, onChange }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const active = value !== null
  const label = seedCountLabel(value)

  useEffect(() => {
    if (!open) return
    const onDown = e => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  return (
    <div ref={ref} style={{ position: 'relative', flexShrink: 0 }}>
      {/* A menu trigger is a button; it has never looked like one. Raised when a
          count is chosen, quiet when it is not. */}
      <Button size="sm" variant={active ? 'secondary' : 'ghost'} pressed={open} onClick={() => setOpen(o => !o)}>
        {active && optDot(value === 0 ? 'var(--yellow)' : 'var(--blue)')}
        <span>Seed count: {label}</span>
        <span style={{ color: 'var(--text-dim)', fontSize: 'var(--font-sm)' }}>▾</span>
      </Button>
      {open && (
        <div style={{
          position: 'absolute',
          right: 0,
          top: '100%',
          marginTop: 6,
          minWidth: 150,
          zIndex: 120,
          background: 'var(--surface2)',
          border: '1px solid var(--border2)',
          borderRadius: 8,
          boxShadow: 'var(--shadow-pop)',
          padding: 4,
        }}>
          {options.map(opt => {
            const selected = opt.value === null
              ? value === null
              : opt.value === '5plus'
                ? value === '5plus' || value >= 5
                : value === opt.value
            return (
              <button
                key={String(opt.value)}
                type="button"
                onClick={() => { onChange(opt.value); setOpen(false) }}
                style={{
                  width: '100%',
                  padding: '6px 8px',
                  border: 'none',
                  borderRadius: 'var(--r)',
                  background: selected ? 'var(--surface3)' : 'transparent',
                  color: selected ? 'var(--text)' : 'var(--text-dim)',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 12,
                  fontFamily: 'var(--sans)',
                  fontSize: 'var(--font-base)',
                  fontWeight: selected ? 700 : 500,
                  textAlign: 'left',
                }}
              >
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7 }}>
                  {opt.value !== null && optDot(opt.value === 0 ? 'var(--yellow)' : 'var(--blue)')}
                  {opt.label}
                </span>
                <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>{opt.count.toLocaleString()}</span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

function SizeInput({ value, onChange, placeholder }) {
  const [focused, setFocused] = useState(false)
  return (
    <input
      type="number"
      min="0"
      step="any"
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={`${placeholder} size`}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      style={{
        width: 80, height: 'var(--control-h)', padding: '0 8px',
        borderRadius: 'var(--r)', fontSize: 'var(--font-base)',
        border: `1px solid ${focused ? 'var(--accent)' : value ? tint('var(--accent)', 40) : 'var(--border2)'}`,
        background: focused || value ? 'var(--surface2)' : 'transparent',
        color: 'var(--text)', fontFamily: 'var(--mono)',
        outline: 'none', transition: 'all 0.12s',
      }}
    />
  )
}

function PathsModal({ name, linkedPaths, duplicatePaths, onClose, anchorRect }) {
  useEffect(() => {
    const handler = e => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [onClose])

  const pathStyle = { fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text)', wordBreak: 'break-all', lineHeight: 1.65, padding: '4px 0' }

  const POPOVER_W = 580
  const popoverStyle = anchorRect ? (() => {
    const top = anchorRect.bottom + 8
    const left = Math.max(8, Math.min(anchorRect.left, window.innerWidth - POPOVER_W - 16))
    const maxH = Math.max(160, window.innerHeight - top - 16)
    return {
      position: 'fixed', top, left, width: POPOVER_W,
      maxHeight: maxH, overflowY: 'auto',
      background: 'var(--surface)',
      border: '1px solid var(--border2)',
      borderRadius: 10,
      padding: '18px 20px',
      boxShadow: '0 12px 40px rgba(0,0,0,0.6)',
      zIndex: 10001,
    }
  })() : {
    background: 'var(--surface)',
    border: '1px solid var(--border2)',
    borderRadius: 10,
    padding: '18px 20px',
    maxWidth: 620, width: '100%',
    maxHeight: '70vh', overflowY: 'auto',
    boxShadow: '0 12px 40px rgba(0,0,0,0.6)',
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 10000,
        background: anchorRect ? 'transparent' : 'rgba(0,0,0,0.55)',
        display: anchorRect ? 'block' : 'flex',
        alignItems: 'center', justifyContent: 'center',
        padding: anchorRect ? 0 : 24,
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={popoverStyle}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12, marginBottom: 16 }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-base)', color: 'var(--text)', fontWeight: 600, wordBreak: 'break-all', lineHeight: 1.5 }}>
            {name}
          </span>
          <CloseButton onClick={onClose} />
        </div>

        {linkedPaths?.length > 0 && (
          <div style={{ marginBottom: duplicatePaths?.length > 0 ? 16 : 0 }}>
            <div style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', fontWeight: 600, letterSpacing: 0, textTransform: 'none', color: 'var(--text)', marginBottom: 6 }}>
              Hardlinks ({linkedPaths.length})
            </div>
            {linkedPaths.map((p, i) => (
              <div key={i} style={{ ...pathStyle, borderBottom: i < linkedPaths.length - 1 ? '1px solid var(--border)' : 'none' }}>{p}</div>
            ))}
          </div>
        )}

        {duplicatePaths?.length > 0 && (
          <div>
            <div style={{ fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', fontWeight: 600, letterSpacing: 0, textTransform: 'none', color: 'var(--text)', marginBottom: 6 }}>
              Duplicates ({duplicatePaths.length})
            </div>
            {duplicatePaths.map((p, i) => (
              <div key={i} style={{ ...pathStyle, borderBottom: i < duplicatePaths.length - 1 ? '1px solid var(--border)' : 'none' }}>{p}</div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Skeleton ────────────────────────────────────────────────────────────────

function ExplorerSkeleton() {
  return (
    <div style={{ padding: '14px 24px 48px' }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 10, marginBottom: 14 }}>
        {[0,1,2].map(i => (
          <div key={i} style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--r)', boxShadow: 'var(--elev-1)', padding: '12px 14px' }}>
            <div className="skeleton" style={{ width: 60, height: 10, marginBottom: 8 }} />
            <div className="skeleton" style={{ width: 40, height: 24, marginBottom: 4 }} />
            <div className="skeleton" style={{ width: 80, height: 10 }} />
          </div>
        ))}
      </div>
      <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', boxShadow: 'var(--elev-1)' }}>
        {[...Array(10)].map((_, i) => (
          <div key={i} style={{ padding: '9px 16px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between' }}>
            <div className="skeleton" style={{ width: (30 + i*5%30) + '%', height: 11 }} />
            <div className="skeleton" style={{ width: 100, height: 11 }} />
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Tree ────────────────────────────────────────────────────────────────────

function buildTree(files) {
  const root = { _isDir: true, children: {}, size: 0 }
  for (const file of files) {
    let node = root
    node.size += file.size
    const parts = file.path.replace(/\\/g, '/').split('/').filter(Boolean)
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i]
      if (i === parts.length - 1) {
        node.children[part] = file
      } else {
        if (!node.children[part]) node.children[part] = { _isDir: true, children: {}, size: 0 }
        node = node.children[part]
        node.size += file.size
      }
    }
  }
  return root
}

function sortedKeys(children) {
  const keys = Object.keys(children)
  const dirs  = keys.filter(k => children[k]._isDir).sort((a,b) => a.localeCompare(b, undefined, { numeric: true }))
  const files = keys.filter(k => !children[k]._isDir).sort((a,b) => a.localeCompare(b, undefined, { numeric: true }))
  return [...dirs, ...files]
}

// Flatten the tree into a sorted array of visible rows, respecting open/closed state.
// Called in a useMemo that depends on [tree, tick] so it re-runs only when the tree
// or an open/close toggle changes.
function flattenVisible(children, openSet, depth = 0, parentPath = '') {
  const rows = []
  for (const k of sortedKeys(children)) {
    const node = children[k]
    const nodePath = parentPath ? `${parentPath}/${k}` : k
    if (node._isDir) {
      rows.push({ type: 'folder', name: k, node, depth, path: nodePath })
      if (openSet.has(nodePath)) {
        const nested = flattenVisible(node.children, openSet, depth + 1, nodePath)
        for (let i = 0; i < nested.length; i++) rows.push(nested[i])
      }
    } else {
      rows.push({ type: 'file', name: k, node, depth })
    }
  }
  return rows
}

// ─── Row components ──────────────────────────────────────────────────────────
// Each row fills exactly its slot height (boxSizing border-box) so react-window
// positions them correctly with no gaps.

function FolderRow({ name, node, depth, openRef, onToggle, path }) {
  const open = openRef.current.has(path)
  const indent = (depth * 20) + 14
  return (
    <div
      onClick={(e) => { e.stopPropagation(); onToggle(path) }}
      style={{
        display: 'flex', alignItems: 'center', gap: 8,
        height: TREE_ITEM_HEIGHT, boxSizing: 'border-box',
        paddingLeft: indent, paddingRight: 16,
        borderBottom: '1px solid var(--border)',
        background: open ? 'var(--surface2)' : 'var(--surface)',
        cursor: 'pointer', userSelect: 'none', overflow: 'hidden',
      }}
    >
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="var(--text-dim)" strokeWidth="3" style={{ flexShrink: 0 }}>
        {open ? <polyline points="6 9 12 15 18 9"/> : <polyline points="9 18 15 12 9 6"/>}
      </svg>
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" style={{ flexShrink: 0 }}>
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
      </svg>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-base)', fontWeight: 700, color: 'var(--text)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', flexShrink: 0 }}>{formatBytes(node.size)}</span>
    </div>
  )
}

function FileRow({ name, node, depth, tab, sonarrConfigured, radarrConfigured, torrentSource, qbHost, quiHost, onOpenPopup }) {
  const indent      = (depth * 20) + 14
  const isDupe      = node.duplicate_paths?.length > 0
  const isOrphan    = node.status === 'Orphaned'
  const notImported = !node.excluded && !node.imported && node.status !== 'Orphaned' && tab === 'torrents'
  const showSearchButtons = tab === 'media' && isOrphan
  const mediaType  = detectMediaType(node.path)
  const showSonarr = sonarrConfigured && (mediaType === 'tv'    || mediaType === 'unknown')
  const showRadarr = radarrConfigured && (mediaType === 'movie' || mediaType === 'unknown')
  const sourceHost = torrentSource === 'qui' ? quiHost : qbHost
  const showSourceLink = tab === 'torrents' && !!sourceHost
  const hasPaths   = node.linked_paths?.length > 0 || node.duplicate_paths?.length > 0

  const toast = useToast()
  const [sonarrState, setSonarrState] = useState('idle')
  const [radarrState, setRadarrState] = useState('idle')

  const handleSonarrSearch = async (e) => {
    e.stopPropagation()
    setSonarrState('loading')
    try {
      const data = await api.sonarrSearch(node.path)
      window.open(data.url, '_blank', 'noopener')
      setSonarrState('success')
      toast(`Opened ${data.title} in Sonarr — run Interactive Search to find a seeding version`, 'success')
      setTimeout(() => setSonarrState('idle'), 3000)
    } catch (err) {
      setSonarrState('error')
      toast(err.message || 'Sonarr search failed', 'error')
      setTimeout(() => setSonarrState('idle'), 3000)
    }
  }

  const handleRadarrSearch = async (e) => {
    e.stopPropagation()
    setRadarrState('loading')
    try {
      const data = await api.radarrSearch(node.path)
      window.open(data.url, '_blank', 'noopener')
      setRadarrState('success')
      toast(`Opened ${data.title} in Radarr — run Interactive Search to find a seeding version`, 'success')
      setTimeout(() => setRadarrState('idle'), 3000)
    } catch (err) {
      setRadarrState('error')
      toast(err.message || 'Radarr search failed', 'error')
      setTimeout(() => setRadarrState('idle'), 3000)
    }
  }

  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      height: TREE_ITEM_HEIGHT, boxSizing: 'border-box',
      paddingLeft: indent, paddingRight: 16,
      borderBottom: '1px solid var(--border)',
      background: 'var(--surface)', gap: 12, overflow: 'hidden',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0, flex: 1 }}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" strokeWidth="2" style={{ flexShrink: 0 }}>
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
        {hasPaths && <PathsButton name={name} node={node} onOpenPopup={onOpenPopup} />}
        {node.excluded && <Tag color="var(--text-dim)">excluded</Tag>}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
        {showSearchButtons && showSonarr && (
          <ArrSearchButton service="Sonarr" color="var(--blue)" state={sonarrState} onClick={handleSonarrSearch} />
        )}
        {showSearchButtons && showRadarr && (
          <ArrSearchButton service="Radarr" color="var(--yellow)" state={radarrState} onClick={handleRadarrSearch} />
        )}
        <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', minWidth: 64, textAlign: 'right' }}>{formatBytes(node.size)}</span>
        {isDupe      && <Tag color="var(--purple)">dupe</Tag>}
        {notImported && <Tag color="var(--red)">not imported</Tag>}
        <Tag color={isOrphan ? 'var(--yellow)' : node.status === 'Seeding' ? 'var(--green)' : 'var(--blue)'}>{(node.status||'').toLowerCase()}</Tag>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', width: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'right' }}>
          {(node.trackers||[]).join(' · ')}
        </span>
        {showSourceLink && (
          <Button size="chip" variant="subtle" title={clientLinkTitle(node, torrentSource)}
            onClick={e => { e.stopPropagation(); openTorrentInClient(node, torrentSource, qbHost, quiHost, toast) }}>
            {torrentSource === 'qui' ? 'qui ↗' : 'qBit ↗'}
          </Button>
        )}
        <CopyPathButton path={node.path} toast={toast} />
      </div>
    </div>
  )
}

function FlatFileRow({ node, tab, sonarrConfigured, radarrConfigured, torrentSource, qbHost, quiHost, isRevealed, onOpenPopup }) {
  const basename    = node.path.replace(/\\/g, '/').split('/').pop()
  const dirname     = node.path.replace(/\\/g, '/').split('/').slice(0, -1).join('/')
  const isDupe      = node.duplicate_paths?.length > 0
  const isOrphan    = node.status === 'Orphaned'
  const notImported = !node.excluded && !node.imported && node.status !== 'Orphaned' && tab === 'torrents'
  const showSearchButtons = tab === 'media' && isOrphan
  const mediaType  = detectMediaType(node.path)
  const showSonarr = sonarrConfigured && (mediaType === 'tv'    || mediaType === 'unknown')
  const showRadarr = radarrConfigured && (mediaType === 'movie' || mediaType === 'unknown')
  const sourceHost = torrentSource === 'qui' ? quiHost : qbHost
  const showSourceLink = tab === 'torrents' && !!sourceHost
  const hasPaths   = node.linked_paths?.length > 0 || node.duplicate_paths?.length > 0

  const toast = useToast()
  const [sonarrState, setSonarrState] = useState('idle')
  const [radarrState, setRadarrState] = useState('idle')

  const handleSonarrSearch = async (e) => {
    e.stopPropagation()
    setSonarrState('loading')
    try {
      const data = await api.sonarrSearch(node.path)
      window.open(data.url, '_blank', 'noopener')
      setSonarrState('success')
      toast(`Opened ${data.title} in Sonarr — run Interactive Search to find a seeding version`, 'success')
      setTimeout(() => setSonarrState('idle'), 3000)
    } catch (err) {
      setSonarrState('error')
      toast(err.message || 'Sonarr search failed', 'error')
      setTimeout(() => setSonarrState('idle'), 3000)
    }
  }

  const handleRadarrSearch = async (e) => {
    e.stopPropagation()
    setRadarrState('loading')
    try {
      const data = await api.radarrSearch(node.path)
      window.open(data.url, '_blank', 'noopener')
      setRadarrState('success')
      toast(`Opened ${data.title} in Radarr — run Interactive Search to find a seeding version`, 'success')
      setTimeout(() => setRadarrState('idle'), 3000)
    } catch (err) {
      setRadarrState('error')
      toast(err.message || 'Radarr search failed', 'error')
      setTimeout(() => setRadarrState('idle'), 3000)
    }
  }

  return (
    <div style={{
      height: FLAT_ITEM_HEIGHT, boxSizing: 'border-box',
      padding: '6px 16px',
      borderBottom: '1px solid var(--border)',
      background: isRevealed ? tint('var(--accent)', 3) : 'var(--surface)',
      borderLeft: isRevealed ? '2px solid var(--accent)' : 'none',
      overflow: 'hidden',
    }}>
      {/* Line 1 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0, flex: 1 }}>
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" strokeWidth="2" style={{ flexShrink: 0 }}>
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
          </svg>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{basename}</span>
          {hasPaths && <PathsButton name={basename} node={node} onOpenPopup={onOpenPopup} />}
          {node.excluded && <Tag color="var(--text-dim)">excluded</Tag>}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          {showSearchButtons && showSonarr && (
            <ArrSearchButton service="Sonarr" color="var(--blue)" state={sonarrState} onClick={handleSonarrSearch} />
          )}
          {showSearchButtons && showRadarr && (
            <ArrSearchButton service="Radarr" color="var(--yellow)" state={radarrState} onClick={handleRadarrSearch} />
          )}
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', minWidth: 64, textAlign: 'right' }}>{formatBytes(node.size)}</span>
          {isDupe      && <Tag color="var(--purple)">dupe</Tag>}
          {notImported && <Tag color="var(--red)">not imported</Tag>}
          <Tag color={isOrphan ? 'var(--yellow)' : node.status === 'Seeding' ? 'var(--green)' : 'var(--blue)'}>{(node.status||'').toLowerCase()}</Tag>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', width: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'right' }}>
            {(node.trackers||[]).join(' · ')}
          </span>
          {showSourceLink && (
            <Button size="chip" variant="subtle" title={clientLinkTitle(node, torrentSource)}
              onClick={e => { e.stopPropagation(); openTorrentInClient(node, torrentSource, qbHost, quiHost, toast) }}>
              {torrentSource === 'qui' ? 'qui ↗' : 'qBit ↗'}
            </Button>
          )}
          <CopyPathButton path={node.path} toast={toast} />
        </div>
      </div>
      {/* Line 2: directory. --text-dim, not --text-faint: faint is #d6d3ce on
          white in the light theme, and the folder is how two files of the same
          name are told apart. */}
      <div style={{ paddingLeft: 24, fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {dirname}
      </div>
    </div>
  )
}

// ─── react-window item renderers (defined at module level — stable references) ─

// Wraps each item with the absolute-position style from react-window.
// key={node.path} on the inner component forces remount when the node changes
// (e.g. after a filter change that shifts items in the list), resetting hook state.

const FlatRowRenderer = ({ index, style, data }) => {
  const { nodes, tab, sonarrConfigured, radarrConfigured, torrentSource, qbHost, quiHost, revealPath, onOpenPopup } = data
  const node = nodes[index]
  return (
    <div style={style}>
      <FlatFileRow
        key={node.path}
        node={node}
        tab={tab}
        sonarrConfigured={sonarrConfigured}
        radarrConfigured={radarrConfigured}
        torrentSource={torrentSource}
        qbHost={qbHost}
        quiHost={quiHost}
        isRevealed={!!revealPath && node.path === revealPath}
        onOpenPopup={onOpenPopup}
      />
    </div>
  )
}

const TreeRowRenderer = ({ index, style, data }) => {
  const { rows, tab, openRef, onToggle, sonarrConfigured, radarrConfigured, torrentSource, qbHost, quiHost, onOpenPopup } = data
  const row = rows[index]
  return (
    <div style={style}>
      {row.type === 'folder'
        ? <FolderRow
            key={row.path}
            name={row.name} node={row.node} depth={row.depth}
            openRef={openRef} onToggle={onToggle} path={row.path}
          />
        : <FileRow
            key={row.path || row.name}
            name={row.name} node={row.node} depth={row.depth} tab={tab}
            sonarrConfigured={sonarrConfigured} radarrConfigured={radarrConfigured}
            torrentSource={torrentSource} qbHost={qbHost} quiHost={quiHost}
            onOpenPopup={onOpenPopup}
          />
      }
    </div>
  )
}

// ─── Size unit helpers ────────────────────────────────────────────────────────

const SIZE_UNITS = ['MB', 'GB', 'TB']

function toBytes(val, unit) {
  const n = parseFloat(val)
  if (!val || isNaN(n)) return null
  const multipliers = { MB: 1024**2, GB: 1024**3, TB: 1024**4 }
  const multiplier = multipliers[unit]
  if (!multiplier) return null
  return n * multiplier
}

// One unit for the whole range, as a track after both boxes (the user's pick
// from `.internal/preview/kitchoices.html`, 2026-09-23). It was a native
// <select> after each box: the browser's own arrow and menu, at 11px beside
// 12px boxes, in a toolbar where every other choice is a Segmented. The boxes
// take decimals, so 500 MB – 2 GB is 0.5 – 2 in GB.
const SIZE_UNIT_OPTIONS = SIZE_UNITS.map(u => ({ value: u, label: u }))

function SizeRangeFilter({ minVal, maxVal, unit, onMinVal, onMaxVal, onUnit, onClear }) {
  const hasValue = minVal || maxVal
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', whiteSpace: 'nowrap' }}>size:</span>
      <SizeInput value={minVal} onChange={onMinVal} placeholder="min" />
      <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)' }}>–</span>
      <SizeInput value={maxVal} onChange={onMaxVal} placeholder="max" />
      <Segmented mono label="Size unit" options={SIZE_UNIT_OPTIONS} value={unit} onChange={onUnit} />
      {hasValue && (
        <Button size="sm" variant="ghost" square onClick={onClear} title="Clear size range" ariaLabel="Clear size range">✕</Button>
      )}
    </div>
  )
}

// ─── Main ─────────────────────────────────────────────────────────────────────

// Status is genuinely mutually exclusive — a file is seeding or it is orphaned.
// "Duplicate" and "Excluded" are orthogonal booleans and live in FlagToggles
// instead: as chips in this row they could only ever be selected *instead of* a
// status, so "orphaned but not excluded" was inexpressible (issue #23).
const DIVIDER = { width: 1, height: 18, background: 'var(--border2)', margin: '0 3px', flexShrink: 0 }

const STATUS_FILTERS = [
  { id: 'all',      label: 'All' },
  { id: 'Seeding',  label: 'Seeding',  color: 'var(--green)' },
  { id: 'Orphaned', label: 'Orphaned', color: 'var(--yellow)' },
]

export default function FileExplorer({ files, trackers, tab, initialStatus, initialImportFilter, initialTracker, initialSeedCount, revealPath }) {
  trackers = trackers || []

  const [sonarrConfigured,      setSonarrConfigured]      = useState(false)
  const [radarrConfigured,      setRadarrConfigured]      = useState(false)
  const [torrentSource,         setTorrentSource]         = useState('qbit')
  const [qbHost,                setQbHost]                = useState('')
  const [quiHost,               setQuiHost]               = useState('')
  const [hideExcluded,          setHideExcluded]          = useState(false)

  useEffect(() => {
    api.getConfig().then(c => {
      const arrConnections = Array.isArray(c.ARR_CONNECTIONS) ? c.ARR_CONNECTIONS : []
      setSonarrConfigured(!!c.SONARR_URL || arrConnections.some(conn => String(conn.service || '').toLowerCase() === 'sonarr'))
      setRadarrConfigured(!!c.RADARR_URL || arrConnections.some(conn => String(conn.service || '').toLowerCase() === 'radarr'))
      setTorrentSource(c.TORRENT_SOURCE || 'qbit')
      // These two are only ever used to build links the browser opens, so they
      // resolve to the external address when one is set. The API address stays
      // server-side — nothing here should ever fetch from these.
      setQbHost(c.QB_EXTERNAL_URL || c.QB_HOST || '')
      setQuiHost(c.QUI_EXTERNAL_URL || c.QUI_HOST || '')
      setHideExcluded(!!c.EXCLUSION_HIDE_FROM_EXPLORER)
    }).catch(() => {})
  }, [])

  // Deep links still arrive in the old single-axis vocabulary (Dashboard cards,
  // alert actions, hash routes), so they are mapped onto the split axes here
  // rather than changing pendingNav's shape in App.jsx.
  const [statusFilter, setStatusFilter] = useState(
    initialStatus === 'Seeding' || initialStatus === 'Orphaned' ? initialStatus : 'all')
  const [importFilter, setImportFilter] = useState(
    initialImportFilter === 'notImported' || initialStatus === 'NotImported' ? 'notImported' : 'all')
  const [dupFilter, setDupFilter] = useState(initialStatus === 'Duplicate' ? 'only' : 'any')
  // null = untouched, so the config default governs — including when the config
  // fetch lands after first paint. A click pins it for this visit only.
  const [exclChoice, setExclChoice] = useState(initialStatus === 'Excluded' ? 'only' : null)
  const exclFilter = exclChoice || (hideExcluded ? 'hide' : 'any')
  const [trackerInc,   setTrackerInc]   = useState(initialTracker ? [initialTracker] : [])
  const [trackerExc,   setTrackerExc]   = useState([])
  const [showTrackers, setShowTrackers] = useState(tab === 'torrents' || !!initialTracker)
  const [seedCount,    setSeedCount]    = useState(initialSeedCount != null ? initialSeedCount : null)
  const [userFlat, setUserFlat] = useState(() => localStorage.getItem('auditorr_view_flat') === '1')
  const [sortBy, setSortBy] = useState('name')

  // Raw name query drives the input; debounced value drives filtering
  const [nameQuery, setNameQuery] = useState('')
  const debouncedNameQuery = useDebounce(nameQuery, 150)

  useEffect(() => {
    if (revealPath) {
      const base = revealPath.replace(/\\/g, '/').split('/').pop()
      setNameQuery(base)
    }
  }, [revealPath])

  const [sizeMinVal, setSizeMinVal] = useState('')
  const [sizeMaxVal, setSizeMaxVal] = useState('')
  const [sizeUnit,   setSizeUnit]   = useState('GB')

  const [popup, setPopup] = useState(null)
  const openPopup = useCallback((data) => setPopup(data), [])

  const openRef = useRef(new Set())
  const [tick, setTick] = useState(0)
  const onToggle = useCallback((path) => {
    if (openRef.current.has(path)) openRef.current.delete(path)
    else openRef.current.add(path)
    setTick(t => t + 1)
  }, [])

  // A tracker is included, excluded or neither — never both.
  const setTrackerFlag = useCallback((t, v) => {
    setTrackerInc(p => v === 'only' ? (p.includes(t) ? p : [...p, t]) : p.filter(x => x !== t))
    setTrackerExc(p => v === 'hide' ? (p.includes(t) ? p : [...p, t]) : p.filter(x => x !== t))
  }, [])

  const sizeMinBytes = useMemo(() => toBytes(sizeMinVal, sizeUnit), [sizeMinVal, sizeUnit])
  const sizeMaxBytes = useMemo(() => toBytes(sizeMaxVal, sizeUnit), [sizeMaxVal, sizeUnit])
  const nameLower    = debouncedNameQuery.trim().toLowerCase()
  const isFlat       = !!debouncedNameQuery.trim() || !!revealPath || userFlat

  const filtered = useMemo(() => (files || []).filter(f => {
    const sMatch = statusFilter === 'all' || f.status === statusFilter

    const isDup = (f.duplicate_paths||[]).length > 0
    const dMatch = dupFilter === 'any' || (dupFilter === 'only' ? isDup : !isDup)

    const isExcl = f.excluded === true
    const eMatch = exclFilter === 'any' || (exclFilter === 'only' ? isExcl : !isExcl)

    const iMatch = importFilter === 'all' || (importFilter === 'notImported' && !f.excluded && !f.imported && f.status !== 'Orphaned')

    const tMatch =
      (trackerInc.length === 0 || trackerInc.some(t => (f.trackers||[]).includes(t))) &&
      (trackerExc.length === 0 || !trackerExc.some(t => (f.trackers||[]).includes(t)))

    const scMatch = seedCountMatches(seedCount, seedCountValue(f))

    const nMatch = !nameLower || f.path.toLowerCase().includes(nameLower)

    const szMin = sizeMinBytes === null || f.size >= sizeMinBytes
    const szMax = sizeMaxBytes === null || f.size <= sizeMaxBytes

    return sMatch && dMatch && eMatch && iMatch && tMatch && scMatch && nMatch && szMin && szMax
  }), [files, statusFilter, dupFilter, exclFilter, importFilter, trackerInc, trackerExc, seedCount, nameLower, sizeMinBytes, sizeMaxBytes])

  const sortedFiltered = useMemo(() => {
    if (sortBy === 'size') return [...filtered].sort((a, b) => b.size - a.size)
    return [...filtered].sort((a, b) => {
      const nameA = a.path.replace(/\\/g, '/').split('/').pop()
      const nameB = b.path.replace(/\\/g, '/').split('/').pop()
      return nameA.localeCompare(nameB, undefined, { numeric: true })
    })
  }, [filtered, sortBy])

  // Single-pass stats computation
  const stats = useMemo(() => {
    let total = 0, totalSize = 0, seeding = 0, seedingSize = 0, orphaned = 0, orphanedSize = 0, excluded = 0
    for (const f of filtered) {
      total++
      totalSize += f.size
      if (f.status === 'Seeding') { seeding++; seedingSize += f.size }
      if (f.status === 'Orphaned') { orphaned++; orphanedSize += f.size }
      if (f.excluded === true) excluded++
    }
    return { total, totalSize, seeding, seedingSize, orphaned, orphanedSize, excluded }
  }, [filtered])

  const tree = useMemo(() => buildTree(filtered), [filtered])

  // Flat array of visible tree rows — recomputed when tree changes or a folder is toggled
  const treeRows = useMemo(() => flattenVisible(tree.children, openRef.current), [tree, tick])

  // Stable itemData objects for react-window (avoids forcing re-renders of all visible rows)
  const flatItemData = useMemo(() => ({
    nodes: sortedFiltered, tab, sonarrConfigured, radarrConfigured, torrentSource, qbHost, quiHost, revealPath, onOpenPopup: openPopup,
  }), [sortedFiltered, tab, sonarrConfigured, radarrConfigured, torrentSource, qbHost, quiHost, revealPath, openPopup])

  const treeItemData = useMemo(() => ({
    rows: treeRows, tab, openRef, onToggle, sonarrConfigured, radarrConfigured, torrentSource, qbHost, quiHost, onOpenPopup: openPopup,
  }), [treeRows, tab, openRef, onToggle, sonarrConfigured, radarrConfigured, torrentSource, qbHost, quiHost, openPopup])

  const [copied, setCopied] = useState(false)

  const seedCountOptions = useMemo(() => {
    const counts = new Map()
    for (const f of files || []) {
      if (exclFilter !== 'any' && (f.excluded === true) !== (exclFilter === 'only')) continue
      const n = seedCountValue(f)
      const bucket = n >= 5 ? '5plus' : n
      counts.set(bucket, (counts.get(bucket) || 0) + 1)
    }

    const options = [{ value: null, label: 'All', count: Array.from(counts.values()).reduce((s, n) => s + n, 0) }]
    for (let n = 0; n <= 4; n++) {
      if (counts.has(n) || seedCount === n) options.push({ value: n, label: `${n}x`, count: counts.get(n) || 0 })
    }
    if (counts.has('5plus') || seedCount === '5plus' || seedCount >= 5) {
      options.push({ value: '5plus', label: '5x+', count: counts.get('5plus') || 0 })
    }
    return options
  }, [files, exclFilter, seedCount])

  const exportCSV = () => {
    const rows = ['RelativePath,Size,Status,Imported,Trackers,LinkedPaths,DuplicatePaths',
      ...filtered.map(f =>
        '"'+f.path+'",'+f.size+','+f.status+','+f.imported+
        ',"'+(f.trackers||[]).join('|')+'","'+(f.linked_paths||[]).join('|')+
        '","'+(f.duplicate_paths||[]).join('|')+'"'
      )
    ].join('\n')
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([rows], { type: 'text/csv' }))
    a.download = 'auditorr_'+tab+'.csv'
    a.click()
  }

  if (!files || !files.length) return <ExplorerSkeleton />

  const activeTrackerCount = trackerInc.length + trackerExc.length
  const trackerPanelOpen = showTrackers
  const hasSizeFilter = sizeMinVal || sizeMaxVal

  const copyPaths = () => {
    const paths = filtered.map(f => f.path).join('\n')
    const ta = document.createElement('textarea')
    ta.value = paths
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none'
    document.body.appendChild(ta)
    ta.focus(); ta.select()
    try { document.execCommand('copy') } catch (_) {
      navigator.clipboard?.writeText(paths).catch(() => {})
    }
    document.body.removeChild(ta)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const emptyMsg = (
      <div style={{ padding:40, textAlign:'center', color:'var(--text-dim)', fontFamily:'var(--sans)', fontSize:'var(--font-base)' }}>
      No files match the current filters.
    </div>
  )

  return (
    <div style={{ padding: '0 24px 24px', height: '100%', display: 'flex', flexDirection: 'column', boxSizing: 'border-box' }}>

      {/* ── Summary cards ── */}
      <div style={{ padding: '16px 0 14px', display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:10, flexShrink: 0 }}>
        {[
          { label:'Total files', val:stats.total,    size:stats.totalSize,    color:'var(--text)' },
          { label:'Seeding',     val:stats.seeding,  size:stats.seedingSize,  color:'var(--green)' },
          { label:'Orphaned',    val:stats.orphaned, size:stats.orphanedSize, color:'var(--yellow)' },
        ].map(c => (
          <div key={c.label} style={{ background:'var(--surface)', border:'1px solid var(--border)', borderRadius:'var(--r)', boxShadow:'var(--elev-1)', padding:'10px 14px' }}>
            <div style={{ fontFamily:'var(--sans)', fontSize:'var(--font-base)', fontWeight:600, color:'var(--text)', textTransform:'none', letterSpacing:0, display:'flex', alignItems:'center', gap:7 }}>
              {c.color !== 'var(--text)' && <span className="ui-status-dot" style={{ background:c.color }} />}
              {c.label}
            </div>
            <div style={{ fontFamily:'var(--mono)', fontSize:'var(--font-xl)', fontWeight:700, color:'var(--text)' }}>{c.val.toLocaleString()}</div>
            <div style={{ fontSize:'var(--font-sm)', color:'var(--text-dim)' }}>{formatBytes(c.size)}</div>
          </div>
        ))}
      </div>

      {/* ── Toolbar ──
          A dense bar: every control in it is var(--control-h), 6px apart inside
          a group and a divider between groups. Status, Import, View and Sort are
          one-of-N, so they are Segmented; Duplicates and Excluded are flags on a
          separate axis from Status (issue #23), so they stay FlagToggles. */}
      <div style={{
        background: 'var(--bg)',
        borderBottom: '1px solid var(--border)',
        marginBottom: 14,
        flexShrink: 0,
      }}>
        {/* Row 1: filter groups */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', padding: '8px 0 6px' }}>
          <span className="ui-field-label" style={{ color: 'var(--text-dim)' }}>Status</span>
          <Segmented label="Status" value={statusFilter} onChange={setStatusFilter}
            options={STATUS_FILTERS.map(({ id, label, color }) => ({ value: id, label, icon: color && optDot(color) }))} />
          <div style={DIVIDER} />
          <FlagToggle label="Duplicates" value={dupFilter} onChange={setDupFilter}
            onlyTitle="Show only duplicate files" hideTitle="Hide duplicate files" />
          <FlagToggle
            label={'Excluded' + (exclFilter === 'any' && stats.excluded > 0 ? ` (${stats.excluded.toLocaleString()})` : '')}
            value={exclFilter} onChange={setExclChoice}
            onlyTitle="Show only excluded files" hideTitle="Hide excluded files" />
          {trackers.length > 0 && (
            <Button size="sm" variant={trackerPanelOpen ? 'secondary' : 'ghost'} pressed={trackerPanelOpen}
              onClick={() => setShowTrackers(s => !s)}>
              {optDot('var(--blue)')}
              {'Trackers' + (activeTrackerCount > 0 ? ' (' + activeTrackerCount + ')' : '')}
            </Button>
          )}
          {tab === 'torrents' && <>
            <div style={DIVIDER} />
            <span className="ui-field-label" style={{ color: 'var(--text-dim)' }}>Import</span>
            <Segmented label="Import" value={importFilter} onChange={setImportFilter} options={[
              { value: 'all', label: 'All' },
              { value: 'notImported', label: 'Not imported', icon: optDot('var(--red)') },
            ]} />
          </>}
          <div style={DIVIDER} />
          <SeedCountMenu value={seedCount} options={seedCountOptions} onChange={setSeedCount} />
          <div style={{ flex: 1 }} />

          {/* View toggle. A name search or a reveal forces the flat view, so the
              control shows Flat and is disabled rather than lying about it. */}
          {(() => {
            const forced = !!debouncedNameQuery.trim() || !!revealPath
            return (
              <div style={{ display: 'flex', flexShrink: 0, alignItems: 'center', gap: 6 }}>
                <span className="ui-field-label" style={{ color: 'var(--text-dim)' }}>View</span>
                <Segmented label="View" value={isFlat ? 'flat' : 'tree'} disabled={forced}
                  onChange={v => { setUserFlat(v === 'flat'); localStorage.setItem('auditorr_view_flat', v === 'flat' ? '1' : '0') }}
                  options={[{ value: 'tree', label: '⊟ Tree' }, { value: 'flat', label: '⊞ Flat' }]} />
              </div>
            )
          })()}

          {isFlat && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0, marginLeft: 6 }}>
              <span className="ui-field-label" style={{ color: 'var(--text-dim)' }}>Sort</span>
              <Segmented label="Sort" value={sortBy} onChange={setSortBy}
                options={[{ value: 'name', label: 'Name' }, { value: 'size', label: 'Size' }]} />
            </div>
          )}
        </div>

        {/* Row 2: search + size range, then the two actions */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '0 0 8px', flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <SearchInput value={nameQuery} onChange={setNameQuery} placeholder="Search filename…" width={200} mono />
            {nameQuery && (
              <Button size="sm" variant="ghost" square onClick={() => setNameQuery('')} title="Clear search" ariaLabel="Clear search">✕</Button>
            )}
          </div>

          <div style={{ ...DIVIDER, margin: 0 }} />

          <SizeRangeFilter
            minVal={sizeMinVal} maxVal={sizeMaxVal} unit={sizeUnit}
            onMinVal={setSizeMinVal} onMaxVal={setSizeMaxVal} onUnit={setSizeUnit}
            onClear={() => { setSizeMinVal(''); setSizeMaxVal('') }}
          />

          {(nameQuery || hasSizeFilter) && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--accent)' }}>
              {filtered.length.toLocaleString()} match{filtered.length !== 1 ? 'es' : ''}
            </span>
          )}

          {/* Actions, not filters — they sit on this row so the filter row above
              has room for the status track and both flag toggles without wrapping. */}
          <div style={{ flex: 1 }} />

          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Button size="sm" variant={copied ? undefined : 'subtle'} tone={copied ? 'var(--green)' : undefined}
              onClick={copyPaths} title={`Copy ${filtered.length} paths to clipboard`}>
              {copied ? '✓ Copied!' : 'Copy Paths'}
            </Button>
            <Button size="sm" variant="subtle" onClick={exportCSV}>Export CSV</Button>
          </div>
        </div>
      </div>

      {/* ── Tracker panel ── */}
      {trackerPanelOpen && trackers.length > 0 && (
        <div style={{
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 'var(--r)', boxShadow: 'var(--elev-1)', padding: '12px 16px', marginBottom: 14, flexShrink: 0,
        }}>
          <div style={{ fontFamily:'var(--sans)', fontSize:'var(--font-base)', fontWeight:600, color:'var(--text)', letterSpacing:0, textTransform:'none', marginBottom:10 }}>
            + include / − exclude
          </div>
          <div style={{ display:'flex', flexWrap:'wrap', gap:6 }}>
            {trackers.map(t => (
              <FlagToggle key={t} label={t}
                value={trackerInc.includes(t) ? 'only' : trackerExc.includes(t) ? 'hide' : 'any'}
                onChange={v => setTrackerFlag(t, v)}
                onlyTitle={`Show only files on ${t}`} hideTitle={`Hide files on ${t}`} />
            ))}
            {activeTrackerCount > 0 && (
              <Button size="sm" variant="ghost" onClick={() => { setTrackerInc([]); setTrackerExc([]) }}>Clear</Button>
            )}
          </div>
        </div>
      )}

      {/* ── Virtualized file list ── */}
      <div style={{
        background:'var(--surface)', border:'1px solid var(--border)',
        borderRadius:'var(--rl)', boxShadow:'var(--elev-1)', overflow:'hidden',
        flex: 1, minHeight: 0,
      }}>
        {isFlat ? (
          sortedFiltered.length === 0 ? emptyMsg : (
            <AutoSizer>
              {({ height, width }) => (
                <FixedSizeList
                  height={height}
                  width={width}
                  itemCount={sortedFiltered.length}
                  itemSize={FLAT_ITEM_HEIGHT}
                  itemData={flatItemData}
                  overscanCount={10}
                >
                  {FlatRowRenderer}
                </FixedSizeList>
              )}
            </AutoSizer>
          )
        ) : (
          treeRows.length === 0 ? emptyMsg : (
            <AutoSizer>
              {({ height, width }) => (
                <FixedSizeList
                  height={height}
                  width={width}
                  itemCount={treeRows.length}
                  itemSize={TREE_ITEM_HEIGHT}
                  itemData={treeItemData}
                  overscanCount={10}
                >
                  {TreeRowRenderer}
                </FixedSizeList>
              )}
            </AutoSizer>
          )
        )}
      </div>

      <div style={{ marginTop:8, fontFamily:'var(--mono)', fontSize:'var(--font-sm)', color:'var(--text-dim)', textAlign:'right' }}>
        {filtered.length.toLocaleString()} files · {formatBytes(stats.totalSize)}
      </div>

      {popup && (
        <PathsModal
          name={popup.name}
          linkedPaths={popup.linkedPaths}
          duplicatePaths={popup.duplicatePaths}
          anchorRect={popup.anchorRect}
          onClose={() => setPopup(null)}
        />
      )}
    </div>
  )
}
