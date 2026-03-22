import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api'
import { useToast } from './Toast'
import { formatBytes } from '../utils'

// ─── Script Modal ────────────────────────────────────────────────────────────

function ScriptModal({ scriptType, title, subtitle, onClose }) {
  const [script, setScript] = useState(null)
  const [loading, setLoading] = useState(true)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    api.actionScript(scriptType)
      .then(text => { setScript(text); setLoading(false) })
      .catch(e => { setScript(`# Error loading script: ${e.message}`); setLoading(false) })
  }, [scriptType])

  const handleCopy = () => {
    const ta = document.createElement('textarea')
    ta.value = script
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    try { document.execCommand('copy') } catch (_) {}
    document.body.removeChild(ta)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleDownload = () => {
    const blob = new Blob([script], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = scriptType + '.sh'
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
          width: '90%', maxWidth: 700, maxHeight: '90vh',
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 'var(--rl)', display: 'flex', flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        {/* Header */}
        <div style={{
          padding: '16px 20px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12,
          flexShrink: 0,
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
          padding: '10px 16px',
          background: 'rgba(234,179,8,0.13)',
          borderLeft: '3px solid var(--yellow)',
          margin: '12px 16px 0',
          fontSize: 11, color: 'var(--text-dim)', flexShrink: 0,
        }}>
          ⚠ Review this script carefully before running. auditorr does not execute scripts — you run this manually in your terminal.
        </div>

        {/* Script area */}
        <div style={{ flex: 1, overflow: 'auto', padding: 16, margin: '0 16px' }}>
          {loading ? (
            <div style={{
              height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--text-dim)', fontSize: 13,
            }}>
              Loading…
            </div>
          ) : (
            <pre style={{
              margin: 0,
              fontFamily: 'var(--mono)', fontSize: 11,
              color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
              lineHeight: 1.5,
            }}>
              {script}
            </pre>
          )}
        </div>

        {/* Footer */}
        <div style={{
          padding: '14px 16px', borderTop: '1px solid var(--border)',
          display: 'flex', gap: 10, justifyContent: 'flex-end', flexShrink: 0,
        }}>
          <button onClick={onClose} style={btnStyle('var(--surface2)', 'var(--text-dim)')}>
            Close
          </button>
          {!loading && script && (
            <>
              <button onClick={handleDownload} style={btnStyle('var(--surface2)', 'var(--text)')}>
                Download .sh
              </button>
              <button onClick={handleCopy} style={btnStyle('var(--accent)', 'var(--bg)')}>
                {copied ? '✓ Copied!' : 'Copy to clipboard'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function btnStyle(bg, color, extra = {}) {
  return {
    padding: '7px 14px', borderRadius: 6, border: 'none',
    background: bg, color, fontSize: 12, fontWeight: 500,
    cursor: 'pointer', ...extra,
  }
}

// ─── ActionCard ───────────────────────────────────────────────────────────────

function ActionCard({ color, label, number, subtitle, description, emptyMessage, isEmpty, children }) {
  return (
    <div style={{
      position: 'relative', overflow: 'hidden',
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 12, padding: '18px 18px 16px',
      display: 'flex', flexDirection: 'column',
    }}>
      {/* Color strip */}
      <div style={{
        position: 'absolute', top: 0, left: 0, right: 0, height: 3,
        background: color, borderRadius: '12px 12px 0 0',
      }} />

      {/* Label */}
      <div style={{
        fontFamily: 'var(--mono)', fontSize: 9, fontWeight: 600,
        color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase',
        marginTop: 4,
      }}>
        {label}
      </div>

      {/* Number */}
      <div style={{
        fontFamily: 'var(--mono)', fontSize: 34, fontWeight: 700,
        color: isEmpty ? 'var(--text-dim)' : color,
        lineHeight: 1, marginTop: 10,
      }}>
        {number}
      </div>

      {/* Subtitle */}
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>
        {isEmpty ? `✓ ${emptyMessage}` : subtitle}
      </div>

      {/* Description */}
      {description && !isEmpty && (
        <div style={{
          fontSize: 11.5, color: 'var(--text-dim)', marginTop: 10,
          lineHeight: 1.6, flexGrow: 1,
        }}>
          {description}
        </div>
      )}

      {/* Button area */}
      {!isEmpty && (
        <div style={{ marginTop: 14, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {children}
        </div>
      )}
    </div>
  )
}

// ─── Skeleton ─────────────────────────────────────────────────────────────────

function ActionsSkeleton() {
  return (
    <div style={{ padding: 28 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 16 }}>
        {[0, 1, 2, 3].map(i => (
          <div key={i} className="skeleton" style={{ height: 220, borderRadius: 12 }} />
        ))}
      </div>
    </div>
  )
}

// ─── Main Actions component ───────────────────────────────────────────────────

export default function Actions({ onNavigate }) {
  const [data, setData] = useState(null)
  const [config, setConfig] = useState(null)
  const [error, setError] = useState(null)
  const [scriptModal, setScriptModal] = useState(null) // { scriptType, title, subtitle }
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
      <div style={{ padding: 28, color: 'var(--red)', fontFamily: 'var(--mono)', fontSize: 13 }}>
        Failed to load actions: {error}
      </div>
    )
  }

  if (!data || !config) return <ActionsSkeleton />

  const orphanedMedia    = data.orphaned_media   || { files: [], total_size: 0 }
  const orphanedTorrents = data.orphaned_torrents || { files: [], total_size: 0 }
  const notImported      = data.not_imported      || { files: [], total_size: 0 }
  const duplicates       = data.duplicates        || { groups: [], total_recoverable: 0 }

  const qbHost           = config.QB_HOST || ''
  const sonarrConfigured = !!config.SONARR_URL
  const radarrConfigured = !!config.RADARR_URL

  const orphanedTorrentFiles = orphanedTorrents.files || []
  const visibleTorrents = showMoreTorrents ? orphanedTorrentFiles : orphanedTorrentFiles.slice(0, 5)
  const hiddenCount = Math.max(0, orphanedTorrentFiles.length - 5)

  const nonSkippedGroups = (duplicates.groups || []).filter(g => !g.skipped)
  const dupCount   = nonSkippedGroups.length
  const dupSkipped = (duplicates.groups || []).filter(g => g.skipped).length
  const dupEmpty   = dupCount === 0 && dupSkipped === 0

  return (
    <div style={{ padding: 28 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 16 }}>

        {/* 1. Orphaned Media */}
        <ActionCard
          color="var(--red)"
          label="Orphaned Media"
          number={orphanedMedia.files.length}
          subtitle={`${formatBytes(orphanedMedia.total_size)} unseeded`}
          description={'Filter the Media explorer by "Orphaned", then click the orange Sonarr or yellow Radarr pill buttons on each file row to trigger an interactive search. Pick your preferred release in Sonarr/Radarr and the file will be replaced with a seeding version.'}
          isEmpty={orphanedMedia.files.length === 0}
          emptyMessage="All media files are seeding"
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
          number={orphanedTorrentFiles.length}
          subtitle={formatBytes(orphanedTorrents.total_size)}
          description="Files in your torrent folder that qBittorrent has no record of."
          isEmpty={orphanedTorrentFiles.length === 0}
          emptyMessage="No orphaned torrents"
        >
          {qbHost && orphanedTorrentFiles.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', fontWeight: 500, marginBottom: 2 }}>
                Open in qBittorrent
              </div>
              {visibleTorrents.map((f, i) => (
                <a
                  key={f.hash || i}
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
                  ↗ {(f.path || '').split('/').pop() || f.path}
                </a>
              ))}
              {hiddenCount > 0 && (
                <button
                  onClick={() => setShowMoreTorrents(v => !v)}
                  style={{
                    background: 'none', border: 'none', cursor: 'pointer',
                    padding: 0, fontSize: 11, color: 'var(--text-dim)', textAlign: 'left',
                  }}
                >
                  {showMoreTorrents ? 'Show less' : `Show ${hiddenCount} more…`}
                </button>
              )}
            </div>
          )}
          <button
            onClick={() => setScriptModal({
              scriptType: 'orphaned_torrents_delete',
              title: 'Orphaned Torrent Delete Script',
              subtitle: `${orphanedTorrentFiles.length} files — ${formatBytes(orphanedTorrents.total_size)}`,
            })}
            style={btnStyle('var(--surface2)', 'var(--text)')}
          >
            Generate Delete Script
          </button>
        </ActionCard>

        {/* 3. Not Imported */}
        <ActionCard
          color="var(--red)"
          label="Not Imported"
          number={notImported.files.length}
          subtitle={`${formatBytes(notImported.total_size)} sitting idle`}
          description="Torrents seeding in qBittorrent with no matching media file. Sonarr/Radarr may have failed to import these silently."
          isEmpty={notImported.files.length === 0}
          emptyMessage="All torrents have been imported"
        >
          {sonarrConfigured ? (
            <button
              onClick={handleSonarrRescan}
              disabled={sonarrLoading}
              style={btnStyle('var(--surface2)', 'var(--text)', { opacity: sonarrLoading ? 0.6 : 1 })}
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
              style={btnStyle('var(--surface2)', 'var(--text)', { opacity: radarrLoading ? 0.6 : 1 })}
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
            onClick={() => onNavigate({ tab: 'torrents', importFilter: 'notImported' })}
            style={btnStyle('var(--surface2)', 'var(--text-dim)')}
          >
            View Not Imported →
          </button>
        </ActionCard>

        {/* 4. Duplicate Files — custom layout for extra-large recoverable size */}
        <div style={{
          position: 'relative', overflow: 'hidden',
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 12, padding: '18px 18px 16px',
          display: 'flex', flexDirection: 'column',
        }}>
          <div style={{
            position: 'absolute', top: 0, left: 0, right: 0, height: 3,
            background: 'var(--purple)', borderRadius: '12px 12px 0 0',
          }} />

          {/* Card label */}
          <div style={{
            fontFamily: 'var(--mono)', fontSize: 9, fontWeight: 600,
            color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase',
            marginTop: 4,
          }}>
            Duplicate Files
          </div>

          {/* "RECOVERABLE" sub-label above the big number */}
          <div style={{
            fontFamily: 'var(--mono)', fontSize: 9, fontWeight: 600,
            color: 'var(--text-dim)', letterSpacing: 2, textTransform: 'uppercase',
            marginTop: 14,
          }}>
            Recoverable
          </div>

          {/* Extra-large recoverable size */}
          <div style={{
            fontFamily: 'var(--mono)', fontSize: 48, fontWeight: 700,
            color: dupEmpty ? 'var(--text-dim)' : 'var(--purple)',
            lineHeight: 1,
          }}>
            {formatBytes(duplicates.total_recoverable)}
          </div>

          {/* Subtitle */}
          <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>
            {dupEmpty
              ? '✓ No duplicates found'
              : `${dupCount} duplicate group${dupCount !== 1 ? 's' : ''}${dupSkipped > 0 ? ` · ${dupSkipped} skipped (cross-filesystem)` : ''}`
            }
          </div>

          {/* Description + button */}
          {!dupEmpty && (
            <>
              <div style={{
                fontSize: 11.5, color: 'var(--text-dim)', marginTop: 10,
                lineHeight: 1.6, flexGrow: 1,
              }}>
                Bit-for-bit identical files wasting disk space. Running this script replaces duplicates with hardlinks — all paths and torrents continue working normally.
              </div>
              <div style={{ marginTop: 14 }}>
                <button
                  onClick={() => setScriptModal({
                    scriptType: 'dedupe',
                    title: 'Dedupe Script',
                    subtitle: `${dupCount} groups — ${formatBytes(duplicates.total_recoverable)} recoverable`,
                  })}
                  style={btnStyle('var(--surface2)', 'var(--text)')}
                >
                  Generate Dedupe Script
                </button>
              </div>
            </>
          )}
        </div>

      </div>

      {scriptModal && (
        <ScriptModal
          scriptType={scriptModal.scriptType}
          title={scriptModal.title}
          subtitle={scriptModal.subtitle}
          onClose={() => setScriptModal(null)}
        />
      )}
    </div>
  )
}
