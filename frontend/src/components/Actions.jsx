import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api'
import { useToast } from './Toast'

// ─── Script Modal ────────────────────────────────────────────────────────────

function ScriptModal({ type, title, subtitle, onClose }) {
  const [script, setScript] = useState(null)
  const [loading, setLoading] = useState(true)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    api.actionScript(type)
      .then(text => { setScript(text); setLoading(false) })
      .catch(e => { setScript(`# Error loading script: ${e.message}`); setLoading(false) })
  }, [type])

  const handleCopy = () => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(script).then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000) })
    } else {
      // textarea fallback for HTTP
      const ta = document.createElement('textarea')
      ta.value = script
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  const handleDownload = () => {
    const blob = new Blob([script], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = type + '.sh'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)',
        zIndex: 500, display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: '100%', maxWidth: 700, margin: '0 16px',
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 'var(--rl, 10px)', display: 'flex', flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        {/* Header */}
        <div style={{
          padding: '16px 20px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12,
        }}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--text)' }}>{title}</div>
            {subtitle && <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 2 }}>{subtitle}</div>}
          </div>
          <button
            onClick={onClose}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--text-dim)', fontSize: 20, lineHeight: 1, padding: 0, flexShrink: 0,
            }}
          >×</button>
        </div>

        {/* Warning banner */}
        <div style={{
          padding: '10px 20px', background: 'var(--yellow)15',
          borderBottom: '1px solid var(--yellow)30',
          fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--yellow)',
        }}>
          ⚠ Review this script carefully before running. auditorr does not execute scripts — you run this manually in your terminal.
        </div>

        {/* Script body */}
        <div style={{ padding: 20 }}>
          {loading ? (
            <div style={{
              height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--text-dim)', fontSize: 13,
            }}>
              Loading…
            </div>
          ) : (
            <pre style={{
              margin: 0, maxHeight: 400, overflowY: 'auto',
              background: 'var(--surface2)', borderRadius: 6,
              padding: '12px 14px', fontFamily: 'var(--mono)', fontSize: 11,
              color: 'var(--text)', whiteSpace: 'pre', lineHeight: 1.5,
            }}>
              {script}
            </pre>
          )}
        </div>

        {/* Footer */}
        <div style={{
          padding: '12px 20px', borderTop: '1px solid var(--border)',
          display: 'flex', gap: 8, justifyContent: 'flex-end',
        }}>
          <button onClick={onClose} style={btnStyle('var(--surface2)', 'var(--text-dim)')}>
            Close
          </button>
          {!loading && (
            <>
              <button onClick={handleDownload} style={btnStyle('var(--surface2)', 'var(--text)')}>
                Download as .sh
              </button>
              <button onClick={handleCopy} style={btnStyle('var(--accent)', 'var(--bg)')}>
                {copied ? 'Copied!' : 'Copy to clipboard'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── Shared styles ────────────────────────────────────────────────────────────

function btnStyle(bg, color, extra = {}) {
  return {
    padding: '7px 14px', borderRadius: 6, border: 'none',
    background: bg, color, fontSize: 12, fontWeight: 500,
    cursor: 'pointer', ...extra,
  }
}

function ActionCard({ color, label, count, size, description, children, emptyState }) {
  if (count === 0) {
    return (
      <div style={{
        background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 10, overflow: 'hidden', display: 'flex', flexDirection: 'column',
      }}>
        <div style={{ height: 3, background: color }} />
        <div style={{
          padding: '20px 24px', flex: 1,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: 'var(--green)', fontFamily: 'var(--mono)', fontSize: 13,
        }}>
          {emptyState}
        </div>
      </div>
    )
  }

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 10, overflow: 'hidden', display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ height: 3, background: color }} />
      <div style={{ padding: '20px 24px', flex: 1, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 4 }}>
            {label}
          </div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 32, fontWeight: 700, color, lineHeight: 1 }}>
            {count}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 4 }}>{size}</div>
        </div>
        {description && (
          <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.5 }}>{description}</div>
        )}
        <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
          {children}
        </div>
      </div>
    </div>
  )
}

// ─── Skeleton ─────────────────────────────────────────────────────────────────

function ActionsSkeleton() {
  return (
    <div style={{ padding: 32, display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div className="skeleton" style={{ height: 48, borderRadius: 8 }} />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
        {[0, 1, 2, 3].map(i => (
          <div key={i} className="skeleton" style={{ height: 200, borderRadius: 10 }} />
        ))}
      </div>
    </div>
  )
}

function humanSize(bytes) {
  if (bytes == null) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let n = bytes
  for (const u of units) {
    if (n < 1024) return `${n.toFixed(1)} ${u}`
    n /= 1024
  }
  return `${n.toFixed(1)} PB`
}

// ─── Main Actions component ───────────────────────────────────────────────────

export default function Actions({ onNavigate }) {
  const [data, setData] = useState(null)
  const [config, setConfig] = useState(null)
  const [error, setError] = useState(null)
  const [modal, setModal] = useState(null) // { type, title, subtitle }
  const [showMoreTorrents, setShowMoreTorrents] = useState(false)
  const [sonarrLoading, setSonarrLoading] = useState(false)
  const [radarrLoading, setRadarrLoading] = useState(false)
  const toast = useToast()

  const load = useCallback(async () => {
    try {
      const [actData, cfgData] = await Promise.all([api.actions(), api.getConfig()])
      setData(actData)
      setConfig(cfgData)
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const handleSonarrRescan = async () => {
    setSonarrLoading(true)
    try {
      const paths = (data?.not_imported?.files || []).map(f => f.path)
      const res = await api.sonarrRescan(paths)
      toast(`Sonarr rescan triggered for ${res.count} path${res.count !== 1 ? 's' : ''} — check Sonarr for import results`, 'success')
    } catch (e) {
      toast(e.message || 'Sonarr rescan failed', 'error')
    } finally {
      setSonarrLoading(false)
    }
  }

  const handleRadarrRescan = async () => {
    setRadarrLoading(true)
    try {
      const paths = (data?.not_imported?.files || []).map(f => f.path)
      const res = await api.radarrRescan(paths)
      toast(`Radarr rescan triggered for ${res.count} path${res.count !== 1 ? 's' : ''} — check Radarr for import results`, 'success')
    } catch (e) {
      toast(e.message || 'Radarr rescan failed', 'error')
    } finally {
      setRadarrLoading(false)
    }
  }

  if (error) {
    return (
      <div style={{ padding: 32, color: 'var(--red)', fontFamily: 'var(--mono)', fontSize: 13 }}>
        Failed to load actions: {error}
      </div>
    )
  }

  if (!data || !config) return <ActionsSkeleton />

  const orphanedMedia    = data.orphaned_media   || { files: [], total_size: 0 }
  const orphanedTorrents = data.orphaned_torrents || { files: [], total_size: 0 }
  const notImported      = data.not_imported      || { files: [], total_size: 0 }
  const duplicates       = data.duplicates        || { groups: [], total_recoverable: 0 }
  const totalRecoverable = data.total_recoverable || 0

  const qbHost = config.QB_HOST || ''
  const sonarrConfigured = !!config.SONARR_URL
  const radarrConfigured = !!config.RADARR_URL

  const orphanedTorrentFiles = orphanedTorrents.files || []
  const visibleTorrents = showMoreTorrents ? orphanedTorrentFiles : orphanedTorrentFiles.slice(0, 5)
  const hiddenCount = orphanedTorrentFiles.length - 5

  const dupCount = (duplicates.groups || []).filter(g => !g.skipped).length
  const dupSkipped = (duplicates.groups || []).filter(g => g.skipped).length

  return (
    <div style={{ padding: 32, maxWidth: 1100, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 20 }}>

      {/* Summary banner */}
      {totalRecoverable > 0 ? (
        <div style={{
          padding: '14px 20px', borderRadius: 8,
          background: 'var(--accent)12', border: '1px solid var(--accent)30',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 13, color: 'var(--text)' }}>
            <span style={{ color: 'var(--accent)', fontWeight: 700 }}>{humanSize(totalRecoverable)}</span>
            {' '}recoverable space identified
          </span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>
            last scan results
          </span>
        </div>
      ) : (
        <div style={{
          padding: '14px 20px', borderRadius: 8,
          background: 'var(--green)12', border: '1px solid var(--green)30',
          fontFamily: 'var(--mono)', fontSize: 13, color: 'var(--green)',
        }}>
          No actions needed — library looks clean ✓
        </div>
      )}

      {/* 2×2 grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>

        {/* 1. Orphaned Media */}
        <ActionCard
          color="var(--red)"
          label="Orphaned Media"
          count={orphanedMedia.files.length}
          size={humanSize(orphanedMedia.total_size)}
          description="Use the Sonarr / Radarr interactive search buttons in the Media explorer to find seeding versions for these files."
          emptyState="All media files are seeding ✓"
        >
          <button
            onClick={() => onNavigate({ tab: 'media', status: 'Orphaned' })}
            style={btnStyle('var(--surface2)', 'var(--text)')}
          >
            View Orphaned Media →
          </button>
        </ActionCard>

        {/* 2. Orphaned Torrents */}
        <ActionCard
          color="var(--yellow)"
          label="Orphaned Torrents"
          count={orphanedTorrentFiles.length}
          size={humanSize(orphanedTorrents.total_size)}
          emptyState="No orphaned torrents ✓"
        >
          {/* qBittorrent links */}
          {qbHost && orphanedTorrentFiles.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', fontWeight: 500, marginBottom: 2 }}>Open in qBittorrent</div>
              {visibleTorrents.map(f => (
                <a
                  key={f.hash}
                  href={`${qbHost}/torrents?hash=${f.hash}`}
                  target="_blank"
                  rel="noreferrer"
                  style={{
                    fontSize: 11, color: 'var(--accent)',
                    textDecoration: 'none', fontFamily: 'var(--mono)',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    display: 'block',
                  }}
                >
                  ↗ {f.path.split('/').pop() || f.path}
                </a>
              ))}
              {hiddenCount > 0 && (
                <button
                  onClick={() => setShowMoreTorrents(v => !v)}
                  style={{ ...btnStyle('transparent', 'var(--text-dim)'), padding: '2px 0', textAlign: 'left', fontSize: 11 }}
                >
                  {showMoreTorrents ? 'Show less' : `Show ${hiddenCount} more`}
                </button>
              )}
            </div>
          )}
          <button
            onClick={() => setModal({ type: 'orphaned_torrents_delete', title: 'Orphaned Torrent Delete Script', subtitle: `${orphanedTorrentFiles.length} files — ${humanSize(orphanedTorrents.total_size)}` })}
            style={btnStyle('var(--yellow)20', 'var(--yellow)')}
          >
            Generate Delete Script
          </button>
        </ActionCard>

        {/* 3. Not Imported */}
        <ActionCard
          color="var(--red)"
          label="Not Imported"
          count={notImported.files.length}
          size={humanSize(notImported.total_size)}
          emptyState="All torrents have been imported ✓"
        >
          {sonarrConfigured ? (
            <button
              onClick={handleSonarrRescan}
              disabled={sonarrLoading}
              style={btnStyle('var(--blue)20', 'var(--blue)', { opacity: sonarrLoading ? 0.6 : 1 })}
            >
              {sonarrLoading ? 'Triggering…' : 'Trigger Sonarr Rescan'}
            </button>
          ) : (
            <button
              onClick={() => onNavigate({ tab: 'config' })}
              style={btnStyle('var(--surface2)', 'var(--text-dim)')}
            >
              Configure Sonarr in Settings →
            </button>
          )}
          {radarrConfigured ? (
            <button
              onClick={handleRadarrRescan}
              disabled={radarrLoading}
              style={btnStyle('var(--blue)20', 'var(--blue)', { opacity: radarrLoading ? 0.6 : 1 })}
            >
              {radarrLoading ? 'Triggering…' : 'Trigger Radarr Rescan'}
            </button>
          ) : (
            <button
              onClick={() => onNavigate({ tab: 'config' })}
              style={btnStyle('var(--surface2)', 'var(--text-dim)')}
            >
              Configure Radarr in Settings →
            </button>
          )}
          <button
            onClick={() => onNavigate({ tab: 'torrents', importFilter: 'not_imported' })}
            style={btnStyle('var(--surface2)', 'var(--text)')}
          >
            View Not Imported →
          </button>
        </ActionCard>

        {/* 4. Duplicate Files */}
        <div style={{
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 10, overflow: 'hidden', display: 'flex', flexDirection: 'column',
        }}>
          <div style={{ height: 3, background: 'var(--purple)' }} />
          {dupCount === 0 && dupSkipped === 0 ? (
            <div style={{
              padding: '20px 24px', flex: 1,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--green)', fontFamily: 'var(--mono)', fontSize: 13,
            }}>
              No duplicates found ✓
            </div>
          ) : (
            <div style={{ padding: '20px 24px', flex: 1, display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div>
                <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--text-dim)', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 4 }}>
                  recoverable
                </div>
                <div style={{ fontFamily: 'var(--mono)', fontSize: 48, fontWeight: 700, color: 'var(--purple)', lineHeight: 1 }}>
                  {humanSize(duplicates.total_recoverable)}
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6 }}>
                  {dupCount} duplicate group{dupCount !== 1 ? 's' : ''}
                  {dupSkipped > 0 && ` · ${dupSkipped} skipped (cross-filesystem)`}
                </div>
              </div>
              <div style={{ marginTop: 'auto' }}>
                <button
                  onClick={() => setModal({ type: 'dedupe', title: 'Dedupe Script', subtitle: `${dupCount} groups — ${humanSize(duplicates.total_recoverable)} recoverable` })}
                  style={btnStyle('var(--purple)20', 'var(--purple)')}
                >
                  Generate Dedupe Script
                </button>
              </div>
            </div>
          )}
        </div>

      </div>

      {modal && (
        <ScriptModal
          type={modal.type}
          title={modal.title}
          subtitle={modal.subtitle}
          onClose={() => setModal(null)}
        />
      )}
    </div>
  )
}
