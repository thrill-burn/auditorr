import React, { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import { FixedSizeList } from 'react-window'
import AutoSizer from 'react-virtualized-auto-sizer'
import { formatBytes } from '../utils'
import { api } from '../api'
import { useToast } from './Toast'

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

// ─── Primitives ──────────────────────────────────────────────────────────────

function Tag({ color, children }) {
  return (
    <span style={{
      padding: '1px 7px', borderRadius: 99, fontSize: 10, fontWeight: 600,
      fontFamily: 'var(--mono)', background: color + '22', color,
      border: '1px solid ' + color + '44', whiteSpace: 'nowrap', flexShrink: 0,
    }}>{children}</span>
  )
}

function Chip({ active, color, onClick, children, style }) {
  color = color || 'var(--accent)'
  style = style || {}
  return (
    <button onClick={onClick} style={Object.assign({
      padding: '4px 12px', borderRadius: 99, fontSize: 12, fontWeight: 500,
      border: '1px solid ' + (active ? color : 'var(--border2)'),
      background: active ? color + '22' : 'transparent',
      color: active ? color : 'var(--text-dim)',
      cursor: 'pointer', transition: 'all 0.12s', whiteSpace: 'nowrap',
    }, style)}>{children}</button>
  )
}

function FilterInput({ value, onChange, placeholder, width = 160 }) {
  const [focused, setFocused] = useState(false)
  return (
    <input
      type="text"
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      style={{
        width, height: 28, padding: '0 10px',
        borderRadius: 99, fontSize: 12,
        border: `1px solid ${focused ? 'var(--accent)' : value ? 'var(--accent)66' : 'var(--border2)'}`,
        background: focused || value ? 'var(--surface2)' : 'transparent',
        color: 'var(--text)', fontFamily: 'var(--mono)',
        outline: 'none', transition: 'all 0.12s',
      }}
    />
  )
}

function SizeInput({ value, onChange, placeholder }) {
  const [focused, setFocused] = useState(false)
  return (
    <input
      type="number"
      min="0"
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      style={{
        width: 80, height: 28, padding: '0 8px',
        borderRadius: 99, fontSize: 12,
        border: `1px solid ${focused ? 'var(--accent)' : value ? 'var(--accent)66' : 'var(--border2)'}`,
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

  const pathStyle = { fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)', wordBreak: 'break-all', lineHeight: 1.65, padding: '4px 0' }

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
          <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text)', fontWeight: 600, wordBreak: 'break-all', lineHeight: 1.5 }}>
            {name}
          </span>
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-dim)', fontSize: 20, lineHeight: 1, padding: '0 2px', flexShrink: 0 }}
          >×</button>
        </div>

        {linkedPaths?.length > 0 && (
          <div style={{ marginBottom: duplicatePaths?.length > 0 ? 16 : 0 }}>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 1.5, textTransform: 'uppercase', color: 'var(--text-dim)', marginBottom: 6 }}>
              Hardlinks ({linkedPaths.length})
            </div>
            {linkedPaths.map((p, i) => (
              <div key={i} style={{ ...pathStyle, borderBottom: i < linkedPaths.length - 1 ? '1px solid var(--border)' : 'none' }}>{p}</div>
            ))}
          </div>
        )}

        {duplicatePaths?.length > 0 && (
          <div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 1.5, textTransform: 'uppercase', color: 'var(--purple)', marginBottom: 6 }}>
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
          <div key={i} style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--r)', padding: '12px 14px' }}>
            <div className="skeleton" style={{ width: 60, height: 10, marginBottom: 8 }} />
            <div className="skeleton" style={{ width: 40, height: 24, marginBottom: 4 }} />
            <div className="skeleton" style={{ width: 80, height: 10 }} />
          </div>
        ))}
      </div>
      <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)' }}>
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
      <span style={{ fontFamily: 'var(--mono)', fontSize: 12, fontWeight: 700, color: 'var(--text)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', flexShrink: 0 }}>{formatBytes(node.size)}</span>
    </div>
  )
}

function FileRow({ name, node, depth, tab, sonarrConfigured, radarrConfigured, torrentSource, qbHost, quiHost, onOpenPopup }) {
  const indent      = (depth * 20) + 14
  const isDupe      = node.duplicate_paths?.length > 0
  const isOrphan    = node.status === 'Orphaned'
  const notImported = !node.imported && tab === 'torrents'
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
      window.open(data.url, '_blank')
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
      window.open(data.url, '_blank')
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
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
        {hasPaths && (
          <button
            onClick={e => { e.stopPropagation(); onOpenPopup({ name, linkedPaths: node.linked_paths, duplicatePaths: node.duplicate_paths, anchorRect: e.currentTarget.getBoundingClientRect() }) }}
            title="Show hardlinks & duplicates"
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-dim)', fontSize: 13, lineHeight: 1, padding: '0 2px', flexShrink: 0, opacity: 0.7 }}
            onMouseEnter={e => e.currentTarget.style.opacity = '1'}
            onMouseLeave={e => e.currentTarget.style.opacity = '0.7'}
          >ⓘ</button>
        )}
        {node.excluded && <Tag color="var(--text-dim)">excluded</Tag>}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
        {showSearchButtons && showSonarr && (
          <button
            title="Search in Sonarr"
            onClick={handleSonarrSearch}
            style={{
              background: 'var(--blue)18', border: '1px solid var(--blue)44', borderRadius: 99,
              color: 'var(--blue)', fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
              padding: '1px 8px', cursor: 'pointer', flexShrink: 0, transition: 'background 0.1s',
            }}
            onMouseEnter={e => e.currentTarget.style.background = 'var(--blue)30'}
            onMouseLeave={e => e.currentTarget.style.background = 'var(--blue)18'}
          >
            {sonarrState === 'loading' ? 'Opening…' : sonarrState === 'success' ? '✓ Opened' : sonarrState === 'error' ? '✗ Failed' : 'Open in Sonarr'}
          </button>
        )}
        {showSearchButtons && showRadarr && (
          <button
            title="Search in Radarr"
            onClick={handleRadarrSearch}
            style={{
              background: 'var(--yellow)18', border: '1px solid var(--yellow)44', borderRadius: 99,
              color: 'var(--yellow)', fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
              padding: '1px 8px', cursor: 'pointer', flexShrink: 0, transition: 'background 0.1s',
            }}
            onMouseEnter={e => e.currentTarget.style.background = 'var(--yellow)30'}
            onMouseLeave={e => e.currentTarget.style.background = 'var(--yellow)18'}
          >
            {radarrState === 'loading' ? 'Opening…' : radarrState === 'success' ? '✓ Opened' : radarrState === 'error' ? '✗ Failed' : 'Open in Radarr'}
          </button>
        )}
        <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', minWidth: 64, textAlign: 'right' }}>{formatBytes(node.size)}</span>
        {isDupe      && <Tag color="var(--purple)">dupe</Tag>}
        {notImported && <Tag color="var(--red)">not imported</Tag>}
        <Tag color={isOrphan ? 'var(--yellow)' : node.status === 'Seeding' ? 'var(--green)' : 'var(--blue)'}>{(node.status||'').toLowerCase()}</Tag>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', width: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'right' }}>
          {(node.trackers||[]).join(' · ')}
        </span>
        {showSourceLink && (
          <button
            title={torrentSource === 'qui' ? 'Open in qui' : 'Open in qBittorrent'}
            onClick={e => {
              e.stopPropagation()
              const url = torrentSource === 'qui'
                ? (node.instance_id ? `${quiHost}/instances/${node.instance_id}` : quiHost)
                : qbHost
              window.open(url, '_blank')
            }}
            style={{
              background: 'var(--surface2)', border: '1px solid var(--border2)', borderRadius: 99,
              color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 10,
              padding: '1px 8px', cursor: 'pointer', flexShrink: 0, transition: 'border-color 0.1s',
            }}
            onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--accent)'}
            onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border2)'}
          >{torrentSource === 'qui' ? 'qui ↗' : 'qBit ↗'}</button>
        )}
        <button
          title="Copy full path"
          onClick={e => {
            e.stopPropagation()
            const ta = document.createElement('textarea')
            ta.value = node.path || ''
            ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none'
            document.body.appendChild(ta)
            ta.focus(); ta.select()
            try { document.execCommand('copy') } catch (_) {
              navigator.clipboard?.writeText(node.path || '').catch(() => {})
            }
            document.body.removeChild(ta)
          }}
          style={{
            background: 'none', border: 'none', cursor: 'pointer', padding: '2px 4px',
            color: 'var(--text-faint)', fontSize: 11, lineHeight: 1, flexShrink: 0,
            borderRadius: 3, transition: 'color 0.1s',
          }}
          onMouseEnter={e => e.currentTarget.style.color = 'var(--text-dim)'}
          onMouseLeave={e => e.currentTarget.style.color = 'var(--text-faint)'}
        >⎘</button>
      </div>
    </div>
  )
}

function FlatFileRow({ node, tab, sonarrConfigured, radarrConfigured, torrentSource, qbHost, quiHost, isRevealed, onOpenPopup }) {
  const basename    = node.path.replace(/\\/g, '/').split('/').pop()
  const dirname     = node.path.replace(/\\/g, '/').split('/').slice(0, -1).join('/')
  const isDupe      = node.duplicate_paths?.length > 0
  const isOrphan    = node.status === 'Orphaned'
  const notImported = !node.imported && tab === 'torrents'
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
      window.open(data.url, '_blank')
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
      window.open(data.url, '_blank')
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
      background: isRevealed ? 'var(--accent)08' : 'var(--surface)',
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
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{basename}</span>
          {hasPaths && (
            <button
              onClick={e => { e.stopPropagation(); onOpenPopup({ name: basename, linkedPaths: node.linked_paths, duplicatePaths: node.duplicate_paths, anchorRect: e.currentTarget.getBoundingClientRect() }) }}
              title="Show hardlinks & duplicates"
              style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-dim)', fontSize: 13, lineHeight: 1, padding: '0 2px', flexShrink: 0, opacity: 0.7 }}
              onMouseEnter={e => e.currentTarget.style.opacity = '1'}
              onMouseLeave={e => e.currentTarget.style.opacity = '0.7'}
            >ⓘ</button>
          )}
          {node.excluded && <Tag color="var(--text-dim)">excluded</Tag>}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          {showSearchButtons && showSonarr && (
            <button title="Search in Sonarr" onClick={handleSonarrSearch} style={{
              background: 'var(--blue)18', border: '1px solid var(--blue)44', borderRadius: 99,
              color: 'var(--blue)', fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
              padding: '1px 8px', cursor: 'pointer', flexShrink: 0, transition: 'background 0.1s',
            }}
            onMouseEnter={e => e.currentTarget.style.background = 'var(--blue)30'}
            onMouseLeave={e => e.currentTarget.style.background = 'var(--blue)18'}>
              {sonarrState === 'loading' ? 'Opening…' : sonarrState === 'success' ? '✓ Opened' : sonarrState === 'error' ? '✗ Failed' : 'Open in Sonarr'}
            </button>
          )}
          {showSearchButtons && showRadarr && (
            <button title="Search in Radarr" onClick={handleRadarrSearch} style={{
              background: 'var(--yellow)18', border: '1px solid var(--yellow)44', borderRadius: 99,
              color: 'var(--yellow)', fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
              padding: '1px 8px', cursor: 'pointer', flexShrink: 0, transition: 'background 0.1s',
            }}
            onMouseEnter={e => e.currentTarget.style.background = 'var(--yellow)30'}
            onMouseLeave={e => e.currentTarget.style.background = 'var(--yellow)18'}>
              {radarrState === 'loading' ? 'Opening…' : radarrState === 'success' ? '✓ Opened' : radarrState === 'error' ? '✗ Failed' : 'Open in Radarr'}
            </button>
          )}
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', minWidth: 64, textAlign: 'right' }}>{formatBytes(node.size)}</span>
          {isDupe      && <Tag color="var(--purple)">dupe</Tag>}
          {notImported && <Tag color="var(--red)">not imported</Tag>}
          <Tag color={isOrphan ? 'var(--yellow)' : node.status === 'Seeding' ? 'var(--green)' : 'var(--blue)'}>{(node.status||'').toLowerCase()}</Tag>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', width: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'right' }}>
            {(node.trackers||[]).join(' · ')}
          </span>
          {showSourceLink && (
            <button
              title={torrentSource === 'qui' ? 'Open in qui' : 'Open in qBittorrent'}
              onClick={e => {
                e.stopPropagation()
                const url = torrentSource === 'qui'
                  ? (node.instance_id ? `${quiHost}/instances/${node.instance_id}` : quiHost)
                  : qbHost
                window.open(url, '_blank')
              }}
              style={{
                background: 'var(--surface2)', border: '1px solid var(--border2)', borderRadius: 99,
                color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 10,
                padding: '1px 8px', cursor: 'pointer', flexShrink: 0, transition: 'border-color 0.1s',
              }}
              onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--accent)'}
              onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border2)'}
            >{torrentSource === 'qui' ? 'qui ↗' : 'qBit ↗'}</button>
          )}
          <button
            title="Copy full path"
            onClick={e => {
              e.stopPropagation()
              const ta = document.createElement('textarea')
              ta.value = node.path || ''
              ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none'
              document.body.appendChild(ta)
              ta.focus(); ta.select()
              try { document.execCommand('copy') } catch (_) {
                navigator.clipboard?.writeText(node.path || '').catch(() => {})
              }
              document.body.removeChild(ta)
            }}
            style={{
              background: 'none', border: 'none', cursor: 'pointer', padding: '2px 4px',
              color: 'var(--text-faint)', fontSize: 11, lineHeight: 1, flexShrink: 0,
              borderRadius: 3, transition: 'color 0.1s',
            }}
            onMouseEnter={e => e.currentTarget.style.color = 'var(--text-dim)'}
            onMouseLeave={e => e.currentTarget.style.color = 'var(--text-faint)'}
          >⎘</button>
        </div>
      </div>
      {/* Line 2: directory */}
      <div style={{ paddingLeft: 24, fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-faint)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
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

function SizeRangeFilter({ minVal, minUnit, maxVal, maxUnit, onMinVal, onMinUnit, onMaxVal, onMaxUnit, onClear }) {
  const hasValue = minVal || maxVal
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', whiteSpace: 'nowrap' }}>size:</span>
      <SizeInput value={minVal} onChange={onMinVal} placeholder="min" />
      <UnitSelect value={minUnit} onChange={onMinUnit} />
      <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>–</span>
      <SizeInput value={maxVal} onChange={onMaxVal} placeholder="max" />
      <UnitSelect value={maxUnit} onChange={onMaxUnit} />
      {hasValue && (
        <button onClick={onClear} style={{
          padding: '2px 8px', borderRadius: 99, fontSize: 11,
          border: '1px solid var(--border2)', background: 'transparent',
          color: 'var(--text-dim)', cursor: 'pointer',
        }}>✕</button>
      )}
    </div>
  )
}

function UnitSelect({ value, onChange }) {
  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      style={{
        height: 28, padding: '0 6px', borderRadius: 99, fontSize: 11,
        border: '1px solid var(--border2)', background: 'var(--surface2)',
        color: 'var(--text-dim)', fontFamily: 'var(--mono)', cursor: 'pointer',
        outline: 'none',
      }}
    >
      {SIZE_UNITS.map(u => <option key={u} value={u}>{u}</option>)}
    </select>
  )
}

// ─── Main ─────────────────────────────────────────────────────────────────────

const STATUS_FILTERS = [
  { id: 'all',       label: 'All' },
  { id: 'Seeding',   label: 'Seeding',    color: 'var(--green)' },
  { id: 'Orphaned',  label: 'Orphaned',   color: 'var(--yellow)' },
  { id: 'Duplicate', label: 'Duplicates', color: 'var(--purple)' },
  { id: 'Excluded',  label: 'Excluded',   color: 'var(--text-dim)' },
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
      setSonarrConfigured(!!c.SONARR_URL)
      setRadarrConfigured(!!c.RADARR_URL)
      setTorrentSource(c.TORRENT_SOURCE || 'qbit')
      setQbHost(c.QB_HOST || '')
      setQuiHost(c.QUI_HOST || '')
      setHideExcluded(!!c.EXCLUSION_HIDE_FROM_EXPLORER)
    }).catch(() => {})
  }, [])

  const [statusFilter, setStatusFilter] = useState(initialStatus || 'all')
  const [importFilter, setImportFilter] = useState(initialImportFilter || 'all')
  const [trackerInc,   setTrackerInc]   = useState(initialTracker ? [initialTracker] : [])
  const [trackerExc,   setTrackerExc]   = useState([])
  const [showTrackers, setShowTrackers] = useState(!!initialTracker)
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

  const [sizeMinVal,  setSizeMinVal]  = useState('')
  const [sizeMinUnit, setSizeMinUnit] = useState('GB')
  const [sizeMaxVal,  setSizeMaxVal]  = useState('')
  const [sizeMaxUnit, setSizeMaxUnit] = useState('GB')

  const [popup, setPopup] = useState(null)
  const openPopup = useCallback((data) => setPopup(data), [])

  const openRef = useRef(new Set())
  const [tick, setTick] = useState(0)
  const onToggle = useCallback((path) => {
    if (openRef.current.has(path)) openRef.current.delete(path)
    else openRef.current.add(path)
    setTick(t => t + 1)
  }, [])

  const toggleTracker = useCallback((type, t) => {
    if (type === 'inc') {
      setTrackerInc(p => p.includes(t) ? p.filter(x => x !== t) : [...p, t])
      setTrackerExc(p => p.filter(x => x !== t))
    } else {
      setTrackerExc(p => p.includes(t) ? p.filter(x => x !== t) : [...p, t])
      setTrackerInc(p => p.filter(x => x !== t))
    }
  }, [])

  const sizeMinBytes = useMemo(() => toBytes(sizeMinVal, sizeMinUnit), [sizeMinVal, sizeMinUnit])
  const sizeMaxBytes = useMemo(() => toBytes(sizeMaxVal, sizeMaxUnit), [sizeMaxVal, sizeMaxUnit])
  const nameLower    = debouncedNameQuery.trim().toLowerCase()
  const isFlat       = !!debouncedNameQuery.trim() || !!revealPath || userFlat

  const filtered = useMemo(() => (files || []).filter(f => {
    if (hideExcluded && f.excluded === true && statusFilter !== 'Excluded') return false

    let sMatch
    if      (statusFilter === 'all')         sMatch = true
    else if (statusFilter === 'Duplicate')   sMatch = (f.duplicate_paths||[]).length > 0
    else if (statusFilter === 'NotImported') sMatch = !f.imported && f.status !== 'Orphaned'
    else if (statusFilter === 'Excluded')    sMatch = f.excluded === true
    else                                     sMatch = f.status === statusFilter

    const iMatch = importFilter === 'all' || (importFilter === 'notImported' && !f.imported)

    const tMatch =
      (trackerInc.length === 0 || trackerInc.some(t => (f.trackers||[]).includes(t))) &&
      (trackerExc.length === 0 || !trackerExc.some(t => (f.trackers||[]).includes(t)))

    const scMatch = seedCount === null || (() => {
      const n = (f.trackers||[]).filter(t => t !== 'None').length
      return n === seedCount
    })()

    const nMatch = !nameLower || f.path.toLowerCase().includes(nameLower)

    const szMin = sizeMinBytes === null || f.size >= sizeMinBytes
    const szMax = sizeMaxBytes === null || f.size <= sizeMaxBytes

    return sMatch && iMatch && tMatch && scMatch && nMatch && szMin && szMax
  }), [files, hideExcluded, statusFilter, importFilter, trackerInc, trackerExc, seedCount, nameLower, sizeMinBytes, sizeMaxBytes])

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
    let total = 0, totalSize = 0, seeding = 0, seedingSize = 0, orphaned = 0, orphanedSize = 0
    for (const f of filtered) {
      total++
      totalSize += f.size
      if (f.status === 'Seeding') { seeding++; seedingSize += f.size }
      if (f.status === 'Orphaned') { orphaned++; orphanedSize += f.size }
    }
    return { total, totalSize, seeding, seedingSize, orphaned, orphanedSize }
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
    <div style={{ padding:40, textAlign:'center', color:'var(--text-dim)', fontFamily:'var(--mono)', fontSize:12 }}>
      No files match the current filters.
    </div>
  )

  return (
    <div style={{ padding: '0 24px 24px', height: '100%', display: 'flex', flexDirection: 'column', boxSizing: 'border-box' }}>

      {/* ── Summary cards ── */}
      <div style={{ padding: '16px 0 14px', display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:10, flexShrink: 0 }}>
        {[
          { label:'Total Files', val:stats.total,    size:stats.totalSize,    color:'var(--text)' },
          { label:'Seeding',     val:stats.seeding,  size:stats.seedingSize,  color:'var(--green)' },
          { label:'Orphaned',    val:stats.orphaned, size:stats.orphanedSize, color:'var(--yellow)' },
        ].map(c => (
          <div key={c.label} style={{ background:'var(--surface)', border:'1px solid var(--border)', borderRadius:'var(--r)', padding:'10px 14px' }}>
            <div style={{ fontFamily:'var(--mono)', fontSize:9, color:'var(--text-dim)', textTransform:'uppercase', letterSpacing:1 }}>{c.label}</div>
            <div style={{ fontFamily:'var(--mono)', fontSize:22, fontWeight:700, color:c.color }}>{c.val.toLocaleString()}</div>
            <div style={{ fontSize:11, color:'var(--text-dim)' }}>{formatBytes(c.size)}</div>
          </div>
        ))}
      </div>

      {/* ── Toolbar ── */}
      <div style={{
        background: 'var(--bg)',
        borderBottom: '1px solid var(--border)',
        marginBottom: 14,
        flexShrink: 0,
      }}>
        {/* Row 1: chips */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', padding: '8px 0 6px' }}>
          {STATUS_FILTERS.map(({ id, label, color }) => (
            <Chip key={id} active={statusFilter===id} color={color}
              onClick={() => setStatusFilter(id)}>{label}</Chip>
          ))}
          {trackers.length > 0 && (
            <Chip active={showTrackers} color="var(--blue)"
              onClick={() => setShowTrackers(s => !s)}>
              {'🔍 Trackers' + (activeTrackerCount > 0 ? ' (' + activeTrackerCount + ')' : '')}
            </Chip>
          )}
          {tab === 'torrents' && <div style={{ width:1, height:18, background:'var(--border2)', margin:'0 2px' }} />}
          {tab === 'torrents' && <>
            <Chip active={importFilter==='all'} onClick={() => setImportFilter('all')}>All</Chip>
            <Chip active={importFilter==='notImported'} color="var(--red)"
              onClick={() => setImportFilter('notImported')}>Not Imported</Chip>
          </>}
          <div style={{ flex: 1 }} />

          {seedCount !== null && (
            <Chip active color="var(--blue)" onClick={() => setSeedCount(null)}>
              {seedCount === 0 ? '0× (orphaned)' : `${seedCount}× seeded`} ✕
            </Chip>
          )}

          {/* View toggle */}
          {(() => {
            const forced = !!debouncedNameQuery.trim() || !!revealPath
            return (
              <div style={{ display: 'flex', flexShrink: 0 }}>
                <button
                  onClick={() => { if (!forced) { setUserFlat(false); localStorage.setItem('auditorr_view_flat', '0') } }}
                  style={{
                    padding: '4px 10px', borderRadius: '99px 0 0 99px', fontSize: 11,
                    border: `1px solid ${!isFlat ? 'var(--accent)' : 'var(--border2)'}`,
                    borderRight: 'none',
                    background: !isFlat ? 'var(--accent)22' : 'transparent',
                    color: !isFlat ? 'var(--accent)' : 'var(--text-dim)',
                    cursor: forced ? 'default' : 'pointer',
                    opacity: forced ? 0.45 : 1,
                  }}
                >⊟ Tree</button>
                <button
                  onClick={() => { if (!forced) { setUserFlat(true); localStorage.setItem('auditorr_view_flat', '1') } }}
                  style={{
                    padding: '4px 10px', borderRadius: '0 99px 99px 0', fontSize: 11,
                    border: `1px solid ${isFlat ? 'var(--accent)' : 'var(--border2)'}`,
                    background: isFlat ? 'var(--accent)22' : 'transparent',
                    color: isFlat ? 'var(--accent)' : 'var(--text-dim)',
                    cursor: forced ? 'default' : 'pointer',
                    opacity: forced ? 0.45 : 1,
                  }}
                >⊞ Flat</button>
              </div>
            )
          })()}

          {isFlat && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>Sort:</span>
              <button
                onClick={() => setSortBy('name')}
                style={{
                  padding: '4px 8px', borderRadius: '99px 0 0 99px', fontSize: 11,
                  border: `1px solid ${sortBy === 'name' ? 'var(--accent)' : 'var(--border2)'}`,
                  borderRight: 'none',
                  background: sortBy === 'name' ? 'var(--accent)22' : 'transparent',
                  color: sortBy === 'name' ? 'var(--accent)' : 'var(--text-dim)',
                  cursor: 'pointer',
                }}
              >Name</button>
              <button
                onClick={() => setSortBy('size')}
                style={{
                  padding: '4px 8px', borderRadius: '0 99px 99px 0', fontSize: 11,
                  border: `1px solid ${sortBy === 'size' ? 'var(--accent)' : 'var(--border2)'}`,
                  background: sortBy === 'size' ? 'var(--accent)22' : 'transparent',
                  color: sortBy === 'size' ? 'var(--accent)' : 'var(--text-dim)',
                  cursor: 'pointer',
                }}
              >Size</button>
            </div>
          )}

          <button onClick={copyPaths} title={`Copy ${filtered.length} paths to clipboard`} style={{
            padding: '4px 12px', borderRadius: 99, fontSize: 12, flexShrink: 0,
            border: `1px solid ${copied ? 'var(--green)' : 'var(--border2)'}`,
            background: copied ? 'var(--green)18' : 'transparent',
            color: copied ? 'var(--green)' : 'var(--text-dim)',
            cursor: 'pointer', transition: 'all 0.15s',
          }}>{copied ? '✓ Copied!' : 'Copy Paths'}</button>

          <button onClick={exportCSV} style={{
            padding: '4px 12px', borderRadius: 99, fontSize: 12, flexShrink: 0,
            border: '1px solid var(--border2)', background: 'transparent',
            color: 'var(--text-dim)', cursor: 'pointer',
          }}>Export CSV</button>
        </div>

        {/* Row 2: search + size range */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '0 0 8px', flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <FilterInput value={nameQuery} onChange={setNameQuery} placeholder="🔎 search filename…" width={200} />
            {nameQuery && (
              <button onClick={() => setNameQuery('')} style={{
                padding: '2px 7px', borderRadius: 99, fontSize: 11,
                border: '1px solid var(--border2)', background: 'transparent',
                color: 'var(--text-dim)', cursor: 'pointer',
              }}>✕</button>
            )}
          </div>

          <div style={{ width: 1, height: 18, background: 'var(--border2)' }} />

          <SizeRangeFilter
            minVal={sizeMinVal}  minUnit={sizeMinUnit}
            maxVal={sizeMaxVal}  maxUnit={sizeMaxUnit}
            onMinVal={setSizeMinVal}   onMinUnit={setSizeMinUnit}
            onMaxVal={setSizeMaxVal}   onMaxUnit={setSizeMaxUnit}
            onClear={() => { setSizeMinVal(''); setSizeMaxVal('') }}
          />

          {(nameQuery || hasSizeFilter) && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--accent)' }}>
              {filtered.length.toLocaleString()} match{filtered.length !== 1 ? 'es' : ''}
            </span>
          )}
        </div>
      </div>

      {/* ── Tracker panel ── */}
      {showTrackers && trackers.length > 0 && (
        <div style={{
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 'var(--r)', padding: '12px 16px', marginBottom: 14, flexShrink: 0,
        }}>
          <div style={{ fontFamily:'var(--mono)', fontSize:9, color:'var(--text-dim)', letterSpacing:2, textTransform:'uppercase', marginBottom:10 }}>
            + include &nbsp;/&nbsp; − exclude
          </div>
          <div style={{ display:'flex', flexWrap:'wrap', gap:6 }}>
            {trackers.map(t => (
              <div key={t} style={{ display:'flex' }}>
                <Chip active={trackerInc.includes(t)} color="var(--green)"
                  onClick={() => toggleTracker('inc', t)}
                  style={{ borderRadius:'99px 0 0 99px', borderRight:'none' }}>+ {t}</Chip>
                <Chip active={trackerExc.includes(t)} color="var(--red)"
                  onClick={() => toggleTracker('exc', t)}
                  style={{ borderRadius:'0 99px 99px 0', padding:'4px 10px' }}>−</Chip>
              </div>
            ))}
            {activeTrackerCount > 0 && (
              <button onClick={() => { setTrackerInc([]); setTrackerExc([]) }} style={{
                padding:'4px 10px', borderRadius:99, fontSize:11,
                border:'1px solid var(--border2)', background:'transparent',
                color:'var(--text-dim)', cursor:'pointer',
              }}>clear</button>
            )}
          </div>
        </div>
      )}

      {/* ── Virtualized file list ── */}
      <div style={{
        background:'var(--surface)', border:'1px solid var(--border)',
        borderRadius:'var(--rl)', overflow:'hidden',
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

      <div style={{ marginTop:8, fontFamily:'var(--mono)', fontSize:10, color:'var(--text-dim)', textAlign:'right' }}>
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
