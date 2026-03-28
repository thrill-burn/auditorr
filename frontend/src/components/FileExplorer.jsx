import React, { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import { formatBytes } from '../utils'
import { api } from '../api'
import { useToast } from './Toast'

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

// Compact text input for the toolbar
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

// Compact number input for size range
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

function LinkedPathsPopover({ name, linkedPaths, duplicatePaths }) {
  const [visible, setVisible] = useState(false)
  const [clampedLeft, setClampedLeft] = useState(null)
  const anchorRef = useRef(null)
  const popoverRef = useRef(null)

  const rect = visible && anchorRef.current ? anchorRef.current.getBoundingClientRect() : null
  const left = clampedLeft !== null ? clampedLeft : (rect?.left ?? 0)

  useEffect(() => {
    if (!visible) { setClampedLeft(null); return }
    const popoverWidth = popoverRef.current?.offsetWidth ?? 0
    if (!popoverWidth) return
    const r = anchorRef.current?.getBoundingClientRect()
    if (!r) return
    setClampedLeft(Math.min(r.left, window.innerWidth - popoverWidth - 16))
  }, [visible])

  const pathStyle = { fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)', wordBreak: 'break-all', whiteSpace: 'normal' }

  return (
    <>
      <span
        ref={anchorRef}
        onMouseEnter={() => setVisible(true)}
        onMouseLeave={() => setVisible(false)}
        style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
      >
        {name}
      </span>
      {visible && rect && (
        <div ref={popoverRef} style={{
          position: 'fixed',
          left,
          top: rect.bottom + window.scrollY + 4,
          background: '#151515',
          border: '1px solid #2a2a2a',
          borderRadius: 6,
          padding: '10px 14px',
          boxShadow: '0 8px 24px rgba(0,0,0,0.5)',
          zIndex: 9999,
          maxWidth: 'min(600px, calc(100vw - 32px))',
          pointerEvents: 'none',
        }}>
          {linkedPaths?.length > 0 && (
            <div>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 1.5, textTransform: 'uppercase', color: 'var(--text-dim)', marginBottom: 4 }}>Hardlinks</div>
              {linkedPaths.slice(0, 3).map((p, i) => (
                <div key={i} style={pathStyle}>{p}</div>
              ))}
              {linkedPaths.length > 3 && (
                <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>+{linkedPaths.length - 3} more</div>
              )}
            </div>
          )}
          {duplicatePaths?.length > 0 && (
            <div style={linkedPaths?.length > 0 ? { marginTop: 10 } : {}}>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 1.5, textTransform: 'uppercase', color: 'var(--purple)', marginBottom: 4 }}>Duplicates</div>
              {duplicatePaths.slice(0, 3).map((p, i) => (
                <div key={i} style={pathStyle}>{p}</div>
              ))}
              {duplicatePaths.length > 3 && (
                <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>+{duplicatePaths.length - 3} more</div>
              )}
            </div>
          )}
        </div>
      )}
    </>
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

function FolderRow({ name, node, depth, tab, openRef, onToggle, path, sonarrConfigured, radarrConfigured }) {
  const open = openRef.current.has(path)
  const indent = (depth * 20) + 14
  return (
    <div>
      <div
        onClick={(e) => { e.stopPropagation(); onToggle(path) }}
        style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '8px 16px 8px ' + indent + 'px',
          borderBottom: '1px solid var(--border)',
          background: open ? 'var(--surface2)' : 'var(--surface)',
          cursor: 'pointer', userSelect: 'none',
        }}
      >
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="var(--text-dim)" strokeWidth="3">
          {open ? <polyline points="6 9 12 15 18 9"/> : <polyline points="9 18 15 12 9 6"/>}
        </svg>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
        </svg>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 12, fontWeight: 700, color: 'var(--text)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', flexShrink: 0 }}>{formatBytes(node.size)}</span>
      </div>
      {open && sortedKeys(node.children).map(k =>
        node.children[k]._isDir
          ? <FolderRow key={k} name={k} node={node.children[k]} depth={depth+1} tab={tab}
              openRef={openRef} onToggle={onToggle} path={path + '/' + k}
              sonarrConfigured={sonarrConfigured} radarrConfigured={radarrConfigured} />
          : <FileRow   key={k} name={k} node={node.children[k]} depth={depth+1} tab={tab}
              sonarrConfigured={sonarrConfigured} radarrConfigured={radarrConfigured} />
      )}
    </div>
  )
}

function FileRow({ name, node, depth, tab, sonarrConfigured, radarrConfigured }) {
  const indent      = (depth * 20) + 14
  const isDupe      = node.duplicate_paths?.length > 0
  const isOrphan    = node.status === 'Orphaned'
  const notImported = !node.imported && tab === 'torrents'
  const showSearchButtons = tab === 'media' && isOrphan
  const mediaType  = detectMediaType(node.path)
  const showSonarr = sonarrConfigured && (mediaType === 'tv'    || mediaType === 'unknown')
  const showRadarr = radarrConfigured && (mediaType === 'movie' || mediaType === 'unknown')

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
      padding: '7px 16px 7px ' + indent + 'px',
      borderBottom: '1px solid var(--border)',
      background: 'var(--surface)', gap: 12,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0, flex: 1 }}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" strokeWidth="2" style={{ flexShrink: 0 }}>
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
        {(node.linked_paths?.length > 0 || node.duplicate_paths?.length > 0) ? (
          <LinkedPathsPopover name={name} linkedPaths={node.linked_paths} duplicatePaths={node.duplicate_paths} />
        ) : (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
        )}
        {node.excluded && <Tag color="var(--text-dim)">excluded</Tag>}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
        {showSearchButtons && showSonarr && (
          <button
            title="Search in Sonarr"
            onClick={handleSonarrSearch}
            style={{
              background: 'var(--blue)18',
              border: '1px solid var(--blue)44',
              borderRadius: 99,
              color: 'var(--blue)',
              fontFamily: 'var(--mono)',
              fontSize: 10,
              fontWeight: 600,
              padding: '1px 8px',
              cursor: 'pointer',
              flexShrink: 0,
              transition: 'background 0.1s',
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
              background: 'var(--yellow)18',
              border: '1px solid var(--yellow)44',
              borderRadius: 99,
              color: 'var(--yellow)',
              fontFamily: 'var(--mono)',
              fontSize: 10,
              fontWeight: 600,
              padding: '1px 8px',
              cursor: 'pointer',
              flexShrink: 0,
              transition: 'background 0.1s',
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

// ─── Flat file row (used in flat/search mode) ────────────────────────────────

function FlatFileRow({ node, tab, sonarrConfigured, radarrConfigured, isRevealed }) {
  const basename    = node.path.replace(/\\/g, '/').split('/').pop()
  const dirname     = node.path.replace(/\\/g, '/').split('/').slice(0, -1).join('/')
  const isDupe      = node.duplicate_paths?.length > 0
  const isOrphan    = node.status === 'Orphaned'
  const notImported = !node.imported && tab === 'torrents'
  const showSearchButtons = tab === 'media' && isOrphan
  const mediaType  = detectMediaType(node.path)
  const showSonarr = sonarrConfigured && (mediaType === 'tv'    || mediaType === 'unknown')
  const showRadarr = radarrConfigured && (mediaType === 'movie' || mediaType === 'unknown')

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
      padding: '6px 16px',
      borderBottom: '1px solid var(--border)',
      background: isRevealed ? 'var(--accent)08' : 'var(--surface)',
      borderLeft: isRevealed ? '2px solid var(--accent)' : 'none',
    }}>
      {/* Line 1 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0, flex: 1 }}>
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" strokeWidth="2" style={{ flexShrink: 0 }}>
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
          </svg>
          {(node.linked_paths?.length > 0 || node.duplicate_paths?.length > 0) ? (
            <LinkedPathsPopover name={basename} linkedPaths={node.linked_paths} duplicatePaths={node.duplicate_paths} />
          ) : (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{basename}</span>
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
      <div style={{ paddingLeft: 24, fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-faint)' }}>
        {dirname}
      </div>
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

  const [sonarrConfigured, setSonarrConfigured] = useState(false)
  const [radarrConfigured, setRadarrConfigured] = useState(false)

  useEffect(() => {
    api.getConfig().then(c => {
      setSonarrConfigured(!!c.SONARR_URL)
      setRadarrConfigured(!!c.RADARR_URL)
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

  // Name search
  const [nameQuery, setNameQuery] = useState('')

  useEffect(() => {
    if (revealPath) {
      const base = revealPath.replace(/\\/g, '/').split('/').pop()
      setNameQuery(base)
    }
  }, [revealPath])

  // Size range
  const [sizeMinVal,  setSizeMinVal]  = useState('')
  const [sizeMinUnit, setSizeMinUnit] = useState('GB')
  const [sizeMaxVal,  setSizeMaxVal]  = useState('')
  const [sizeMaxUnit, setSizeMaxUnit] = useState('GB')

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
  const nameLower    = nameQuery.trim().toLowerCase()
  const isFlat       = !!nameQuery.trim() || !!revealPath || userFlat

  const filtered = useMemo(() => (files || []).filter(f => {
    // Status
    let sMatch
    if      (statusFilter === 'all')         sMatch = true
    else if (statusFilter === 'Duplicate')   sMatch = (f.duplicate_paths||[]).length > 0
    else if (statusFilter === 'NotImported') sMatch = !f.imported && f.status !== 'Orphaned'
    else if (statusFilter === 'Excluded')    sMatch = f.excluded === true
    else                                     sMatch = f.status === statusFilter

    // Import
    const iMatch = importFilter === 'all' || (importFilter === 'notImported' && !f.imported)

    // Trackers
    const tMatch =
      (trackerInc.length === 0 || trackerInc.some(t => (f.trackers||[]).includes(t))) &&
      (trackerExc.length === 0 || !trackerExc.some(t => (f.trackers||[]).includes(t)))

    // Seed count (from cross-seed bar navigation)
    const scMatch = seedCount === null || (() => {
      const n = (f.trackers||[]).filter(t => t !== 'None').length
      return n === seedCount
    })()

    // Name search
    const nMatch = !nameLower || f.path.toLowerCase().includes(nameLower)

    // Size range
    const szMin = sizeMinBytes === null || f.size >= sizeMinBytes
    const szMax = sizeMaxBytes === null || f.size <= sizeMaxBytes

    return sMatch && iMatch && tMatch && scMatch && nMatch && szMin && szMax
  }), [files, statusFilter, importFilter, trackerInc, trackerExc, seedCount, nameLower, sizeMinBytes, sizeMaxBytes])

  const sortedFiltered = useMemo(() => {
    if (sortBy === 'size') return [...filtered].sort((a, b) => b.size - a.size)
    return [...filtered].sort((a, b) => {
      const nameA = a.path.replace(/\\/g, '/').split('/').pop()
      const nameB = b.path.replace(/\\/g, '/').split('/').pop()
      return nameA.localeCompare(nameB, undefined, { numeric: true })
    })
  }, [filtered, sortBy])

  const stats = useMemo(() => ({
    total:        filtered.length,
    totalSize:    filtered.reduce((a,f) => a+f.size, 0),
    seeding:      filtered.filter(f => f.status==='Seeding').length,
    seedingSize:  filtered.filter(f => f.status==='Seeding').reduce((a,f) => a+f.size, 0),
    orphaned:     filtered.filter(f => f.status==='Orphaned').length,
    orphanedSize: filtered.filter(f => f.status==='Orphaned').reduce((a,f) => a+f.size, 0),
  }), [filtered])

  const tree     = useMemo(() => buildTree(filtered), [filtered])
  const rootKeys = sortedKeys(tree.children)

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
  const [copied, setCopied] = useState(false)

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

  return (
    <div style={{ padding: '0 24px 24px' }}>

      {/* ── Summary cards ── */}
      <div style={{ padding: '16px 0 14px', display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:10 }}>
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

      {/* ── Toolbar: two rows, fully sticky ── */}
      <div style={{
        position: 'sticky', top: 0, zIndex: 90,
        background: 'var(--bg)',
        borderBottom: '1px solid var(--border)',
        marginBottom: 14,
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

          {/* Active seed-count filter badge */}
          {seedCount !== null && (
            <Chip active color="var(--blue)" onClick={() => setSeedCount(null)}>
              {seedCount === 0 ? '0× (orphaned)' : `${seedCount}× seeded`} ✕
            </Chip>
          )}

          {/* View toggle */}
          {(() => {
            const forced = !!nameQuery.trim() || !!revealPath
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

          {/* Sort toggle (flat mode only) */}
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

          {/* Copy paths button */}
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
          {/* Name search */}
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

          {/* Divider */}
          <div style={{ width: 1, height: 18, background: 'var(--border2)' }} />

          {/* Size range */}
          <SizeRangeFilter
            minVal={sizeMinVal}  minUnit={sizeMinUnit}
            maxVal={sizeMaxVal}  maxUnit={sizeMaxUnit}
            onMinVal={setSizeMinVal}   onMinUnit={setSizeMinUnit}
            onMaxVal={setSizeMaxVal}   onMaxUnit={setSizeMaxUnit}
            onClear={() => { setSizeMinVal(''); setSizeMaxVal('') }}
          />

          {/* Live match count */}
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
          borderRadius: 'var(--r)', padding: '12px 16px', marginBottom: 14,
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

      {/* ── File tree / flat list ── */}
      <div style={{
        background:'var(--surface)', border:'1px solid var(--border)',
        borderRadius:'var(--rl)', overflow:'hidden',
        minHeight: 'calc(100vh - 360px)',
      }}>
        {isFlat ? (
          filtered.length === 0 ? (
            <div style={{ padding:40, textAlign:'center', color:'var(--text-dim)', fontFamily:'var(--mono)', fontSize:12 }}>
              No files match the current filters.
            </div>
          ) : sortedFiltered.map(node => (
            <FlatFileRow
              key={node.path}
              node={node}
              tab={tab}
              sonarrConfigured={sonarrConfigured}
              radarrConfigured={radarrConfigured}
              isRevealed={!!revealPath && node.path === revealPath}
            />
          ))
        ) : (
          rootKeys.length === 0 ? (
            <div style={{ padding:40, textAlign:'center', color:'var(--text-dim)', fontFamily:'var(--mono)', fontSize:12 }}>
              No files match the current filters.
            </div>
          ) : rootKeys.map(k =>
            tree.children[k]._isDir
              ? <FolderRow key={k} name={k} node={tree.children[k]} depth={0} tab={tab}
                  openRef={openRef} onToggle={onToggle} path={k}
                  sonarrConfigured={sonarrConfigured} radarrConfigured={radarrConfigured} />
              : <FileRow   key={k} name={k} node={tree.children[k]} depth={0} tab={tab}
                  sonarrConfigured={sonarrConfigured} radarrConfigured={radarrConfigured} />
          )
        )}
      </div>

      <div style={{ marginTop:8, fontFamily:'var(--mono)', fontSize:10, color:'var(--text-dim)', textAlign:'right' }}>
        {filtered.length.toLocaleString()} files · {formatBytes(stats.totalSize)}
      </div>
    </div>
  )
}
