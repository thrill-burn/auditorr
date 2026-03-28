import React, { useState, useEffect } from 'react'
import { api } from '../api'

function Field({ label, hint, type = 'text', value, onChange, placeholder, style = {}, prefix, suffix }) {
  const [focused, setFocused] = useState(false)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5, ...style }}>
      <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)' }}>{label}</label>
      {hint && <span style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45 }}>{hint}</span>}
      <div style={{ display: 'flex', alignItems: 'center', position: 'relative' }}>
        {prefix && (
          <span style={{
            position: 'absolute', left: 10, fontFamily: 'var(--mono)', fontSize: 13,
            color: 'var(--text-dim)', pointerEvents: 'none',
          }}>{prefix}</span>
        )}
        <input
          type={type} value={value ?? ''} placeholder={placeholder}
          onChange={e => onChange(e.target.value)}
          onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
          style={{
            padding: `8px ${suffix ? '32px' : '11px'} 8px ${prefix ? '24px' : '11px'}`,
            borderRadius: 'var(--r)',
            border: `1px solid ${focused ? 'var(--accent)' : 'var(--border2)'}`,
            background: 'var(--surface2)', color: 'var(--text)',
            fontFamily: 'var(--mono)', fontSize: 13, outline: 'none',
            transition: 'border 0.12s', width: '100%',
          }}
        />
        {suffix && (
          <span style={{
            position: 'absolute', right: 10, fontFamily: 'var(--mono)', fontSize: 13,
            color: 'var(--text-dim)', pointerEvents: 'none',
          }}>{suffix}</span>
        )}
      </div>
    </div>
  )
}

function Card({ title, children }) {
  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--rl)', padding: 24, marginBottom: 16 }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 2, textTransform: 'uppercase', color: 'var(--text-dim)', paddingBottom: 14, marginBottom: 18, borderBottom: '1px solid var(--border)' }}>{title}</div>
      {children}
    </div>
  )
}

export default function Config({ lastAuditTime, isScanning, onConfigSaved, theme, onThemeChange, onScan }) {
  const [conf,        setConf]        = useState(null)
  const [testStatus,        setTestStatus]        = useState(null)
  const [sonarrTestStatus,  setSonarrTestStatus]  = useState(null)
  const [radarrTestStatus,  setRadarrTestStatus]  = useState(null)
  const [saveStatus,        setSaveStatus]        = useState(null)
  const [saveWarnings,      setSaveWarnings]      = useState(() => JSON.parse(localStorage.getItem('auditorr_path_warnings') || '[]'))
  const [passChanged, setPassChanged] = useState(false)
  const [auditRuns,   setAuditRuns]   = useState(null)
  const [clearStatus, setClearStatus] = useState(null)
  const [pathTestStatus, setPathTestStatus] = useState(null)

  // We display ratios as percentages in the UI (0.01 → "1")
  // and convert back on save
  const [orPct,  setOrPct]  = useState('')
  const [niPct,  setNiPct]  = useState('')
  const [dupPct, setDupPct] = useState('')
  const [exclusionPatterns, setExclusionPatterns] = useState('')
  const [exclusionFocused,  setExclusionFocused]  = useState(false)

  const loadConfig = () => {
    api.getConfig().then(data => {
      const c = {
        ...data,
        QB_PASS:        data.QB_PASS        === '__stored__' ? '' : data.QB_PASS,
        SONARR_API_KEY: data.SONARR_API_KEY === '__stored__' ? '' : data.SONARR_API_KEY,
        RADARR_API_KEY: data.RADARR_API_KEY === '__stored__' ? '' : data.RADARR_API_KEY,
      }
      setConf(c)
      setOrPct( String(parseFloat((c.OR_RATIO  ?? 0.01) * 100)))
      setNiPct( String(parseFloat((c.NI_RATIO  ?? 0.01) * 100)))
      setDupPct(String(parseFloat((c.DUP_RATIO ?? 0.01) * 100)))
      setExclusionPatterns((c.EXCLUSION_PATTERNS || []).join('\n'))
      setPassChanged(false)
    })
  }

  useEffect(() => {
    loadConfig()
    api.auditHistory().then(data => setAuditRuns(data.runs || [])).catch(() => setAuditRuns([]))
  }, [])

  if (!conf) return <div style={{ padding: 40, color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 12 }}>Loading…</div>

  const set = key => val => setConf(c => ({ ...c, [key]: val }))

  const setPersistentWarnings = (warnings) => {
    setSaveWarnings(warnings)
    if (warnings.length) localStorage.setItem('auditorr_path_warnings', JSON.stringify(warnings))
    else localStorage.removeItem('auditorr_path_warnings')
  }

  const handleTest = async () => {
    setTestStatus({ loading: true })
    try {
      await api.testConnection({ QB_HOST: conf.QB_HOST, QB_USER: conf.QB_USER, QB_PASS: conf.QB_PASS })
      setTestStatus({ ok: true, msg: 'Connected!' })
    } catch (e) { setTestStatus({ ok: false, msg: e.message }) }
  }

  const handleTestPaths = async () => {
    setPathTestStatus('loading')
    try {
      const result = await api.testPaths({ MEDIA_PATH: conf.MEDIA_PATH, LOCAL_PATH: conf.LOCAL_PATH })
      setPathTestStatus(result)
    } catch (e) { setPathTestStatus({ error: e.message }) }
  }

  const handleTestSonarr = async () => {
    setSonarrTestStatus({ loading: true })
    try {
      await api.testSonarr(conf.SONARR_URL, conf.SONARR_API_KEY)
      await handleSave()
      setSonarrTestStatus({ ok: true, msg: 'Connected and saved!' })
    } catch (e) { setSonarrTestStatus({ ok: false, msg: e.message }) }
  }

  const handleTestRadarr = async () => {
    setRadarrTestStatus({ loading: true })
    try {
      await api.testRadarr(conf.RADARR_URL, conf.RADARR_API_KEY)
      await handleSave()
      setRadarrTestStatus({ ok: true, msg: 'Connected and saved!' })
    } catch (e) { setRadarrTestStatus({ ok: false, msg: e.message }) }
  }

  const handleSave = async () => {
    setPersistentWarnings([])
    const payload = {
      ...conf,
      OR_RATIO:  parseFloat(orPct)  / 100 || 0.01,
      NI_RATIO:  parseFloat(niPct)  / 100 || 0.01,
      DUP_RATIO: parseFloat(dupPct) / 100 || 0.01,
      EXCLUSION_PATTERNS: exclusionPatterns.split('\n').map(p => p.trim()).filter(Boolean),
    }
    if (!passChanged) delete payload.QB_PASS
    try {
      const result = await api.saveConfig(payload)
      if (result.warnings?.length) setPersistentWarnings(result.warnings)
      else setPersistentWarnings([])
      setSaveStatus({ ok: true, msg: 'Saved!' })
      setTimeout(() => setSaveStatus(null), 5000)
      // Re-fetch config so form shows server-confirmed values
      loadConfig()
      // Refresh dashboard so threshold changes are reflected immediately
      if (onConfigSaved) onConfigSaved()
    } catch (e) { setSaveStatus({ ok: false, msg: e.message }) }
  }

  const g2 = { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }
  const g3 = { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 }

  const handleClearHistory = async () => {
    if (!window.confirm('Clear all audit history? This will reset the score chart and run log. Cannot be undone.')) return
    try {
      await api.clearHistory()
      setAuditRuns([])
      setClearStatus({ ok: true, msg: 'History cleared.' })
      if (onConfigSaved) onConfigSaved()
      setTimeout(() => setClearStatus(null), 3000)
    } catch (e) { setClearStatus({ ok: false, msg: e.message }) }
  }

  const thresholdHint = (label) =>
    `All 10 pts lost when ${label} data reaches this % of your library. Points lost proportionally below that.`

  return (
    <div className="fade-in" style={{ padding: 24, maxWidth: 800 }}>

      <Card title="qBittorrent Connection">
        <Field label="Host URL" placeholder="http://192.168.1.x:8080" value={conf.QB_HOST} onChange={set('QB_HOST')} style={{ marginBottom: 14 }} />
        <div style={g2}>
          <Field label="Username" placeholder="admin" value={conf.QB_USER} onChange={set('QB_USER')} />
          <Field label="Password" type="password" placeholder="(unchanged — leave blank to keep current)"
            value={conf.QB_PASS} onChange={v => { setPassChanged(true); set('QB_PASS')(v) }} />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14 }}>
          {testStatus && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: testStatus.ok ? 'var(--green)' : 'var(--red)' }}>
              {testStatus.loading ? 'Testing…' : (testStatus.ok ? '✓ ' : '✗ ') + testStatus.msg}
            </span>
          )}
          <button onClick={handleTest} style={{ padding: '7px 14px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)', fontSize: 12, cursor: 'pointer' }}>
            Test Connection
          </button>
        </div>
      </Card>

      <Card title="Path Mappings">
        <Field label="qBit Save Path" style={{ marginBottom: 14 }}
          hint="The path qBittorrent reports via its API. May differ if qBit runs in its own container."
          placeholder="/data/torrents" value={conf.REMOTE_PATH} onChange={set('REMOTE_PATH')} />
        <div style={g2}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Field label="Media Path"
              hint="Where your final media library lives inside this container — e.g. /data/media"
              placeholder="/data/media" value={conf.MEDIA_PATH} onChange={v => { set('MEDIA_PATH')(v); setPersistentWarnings([]); setPathTestStatus(null) }} />
            {pathTestStatus && pathTestStatus !== 'loading' && !pathTestStatus.error && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: pathTestStatus.media_path?.ok ? 'var(--green)' : 'var(--red)' }}>
                {pathTestStatus.media_path?.ok ? '✓ Found' : '✗ Not found inside container'}
              </span>
            )}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Field label="Local Torrent Path"
              hint="Where those same torrent files are on disk from this container's perspective."
              placeholder="/data/torrents" value={conf.LOCAL_PATH} onChange={v => { set('LOCAL_PATH')(v); setPersistentWarnings([]); setPathTestStatus(null) }} />
            {pathTestStatus && pathTestStatus !== 'loading' && !pathTestStatus.error && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: pathTestStatus.local_path?.ok ? 'var(--green)' : 'var(--red)' }}>
                {pathTestStatus.local_path?.ok ? '✓ Found' : '✗ Not found inside container'}
              </span>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14 }}>
          {!pathTestStatus && (
            <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Click Test Paths to verify these are visible inside the container</span>
          )}
          {pathTestStatus === 'loading' && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)' }}>Testing…</span>
          )}
          {pathTestStatus?.error && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--red)' }}>✗ {pathTestStatus.error}</span>
          )}
          <button onClick={handleTestPaths} style={{ padding: '7px 14px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)', fontSize: 12, cursor: 'pointer' }}>
            Test Paths
          </button>
        </div>
      </Card>

      {saveWarnings.length > 0 && (
        <div style={{
          marginBottom: 16, padding: '12px 16px',
          borderRadius: 'var(--rl)', border: '1px solid #f59e0b',
          background: '#f59e0b12',
        }}>
          {saveWarnings.map((w, i) => (
            <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontFamily: 'var(--mono)', fontSize: 12, color: '#f59e0b', lineHeight: 1.5 }}>
              <span style={{ flexShrink: 0 }}>⚠</span>
              <span>Path warning — {w}</span>
            </div>
          ))}
        </div>
      )}

      <Card title="Watchdog & Scheduled Audits">
        <div style={g2}>
          <Field label="Watchdog Cooldown (seconds)" type="number"
            hint="After a filesystem change is detected, wait this many seconds before running an audit. Default: 60."
            placeholder="60" value={conf.WATCHDOG_COOLDOWN} onChange={set('WATCHDOG_COOLDOWN')} />
          <Field label="Scheduled Interval (minutes)" type="number"
            hint="Fallback: run an audit every N minutes even if the watchdog fires no events. Catches missed changes on NFS/bind mounts. Default: 360 (6h)."
            placeholder="360" value={conf.SCHEDULED_INTERVAL} onChange={set('SCHEDULED_INTERVAL')} />
        </div>
      </Card>

      <Card title="Integrations">
        <p style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.55, marginBottom: 18 }}>
          Required for interactive search in the Media explorer. API keys found in each app under Settings → General.
        </p>
        <div style={g2}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Field label="Sonarr URL" placeholder="http://192.168.1.x:8989" value={conf.SONARR_URL} onChange={set('SONARR_URL')} />
            <Field label="Sonarr API Key" type="password" placeholder="paste API key…" value={conf.SONARR_API_KEY} onChange={set('SONARR_API_KEY')} />
            <Field label="Sonarr Remote Path" type="text"
              hint="The path to your downloads folder as Sonarr sees it inside its container. Leave blank if Sonarr and auditorr share the same paths."
              placeholder="/downloads or /data/torrents"
              value={conf.SONARR_REMOTE_PATH} onChange={set('SONARR_REMOTE_PATH')} />
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              {sonarrTestStatus && (
                <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: sonarrTestStatus.ok ? 'var(--green)' : 'var(--red)' }}>
                  {sonarrTestStatus.loading ? 'Testing…' : (sonarrTestStatus.ok ? '✓ ' : '✗ ') + sonarrTestStatus.msg}
                </span>
              )}
              <button onClick={handleTestSonarr} style={{ padding: '7px 14px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)', fontSize: 12, cursor: 'pointer' }}>
                Test Sonarr
              </button>
            </div>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Field label="Radarr URL" placeholder="http://192.168.1.x:7878" value={conf.RADARR_URL} onChange={set('RADARR_URL')} />
            <Field label="Radarr API Key" type="password" placeholder="paste API key…" value={conf.RADARR_API_KEY} onChange={set('RADARR_API_KEY')} />
            <Field label="Radarr Remote Path" type="text"
              hint="The path to your downloads folder as Radarr sees it inside its container. Leave blank if Radarr and auditorr share the same paths."
              placeholder="/downloads or /data/torrents"
              value={conf.RADARR_REMOTE_PATH} onChange={set('RADARR_REMOTE_PATH')} />
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              {radarrTestStatus && (
                <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: radarrTestStatus.ok ? 'var(--green)' : 'var(--red)' }}>
                  {radarrTestStatus.loading ? 'Testing…' : (radarrTestStatus.ok ? '✓ ' : '✗ ') + radarrTestStatus.msg}
                </span>
              )}
              <button onClick={handleTestRadarr} style={{ padding: '7px 14px', borderRadius: 'var(--r)', border: '1px solid var(--border2)', background: 'transparent', color: 'var(--text-dim)', fontSize: 12, cursor: 'pointer' }}>
                Test Radarr
              </button>
            </div>
          </div>
        </div>
      </Card>

      <Card title="Health Score Thresholds">
        <p style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.55, marginBottom: 18 }}>
          Each threshold defines the size limit for that category relative to your total torrent library.
          Points are lost <em>linearly</em> as you approach the threshold — at exactly the threshold value all 10 points are gone.
          For example, a <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>1%</span> threshold means
          you start losing points immediately if any problem data exists, and lose all 10 points once it reaches 1% of your library.
          Lower = stricter. Hardlinked Media accounts for 70 pts; each category below accounts for 10 pts.
        </p>
        <div style={g3}>
          <Field label="Orphaned Torrent Threshold" type="number"
            suffix="%"
            hint={thresholdHint('orphaned torrent')}
            placeholder="1" value={orPct} onChange={setOrPct} />
          <Field label="Not Imported Threshold" type="number"
            suffix="%"
            hint={thresholdHint('unlinked seeding')}
            placeholder="1" value={niPct} onChange={setNiPct} />
          <Field label="Duplicate Files Threshold" type="number"
            suffix="%"
            hint={thresholdHint('duplicate file')}
            placeholder="1" value={dupPct} onChange={setDupPct} />
        </div>

        {/* Live score preview */}
        <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
          {[
            { label: 'Orphaned', pct: orPct,  size: null },
            { label: 'Not Imported', pct: niPct,  size: null },
            { label: 'Duplicates', pct: dupPct, size: null },
          ].map(({ label, pct }) => {
            const v = parseFloat(pct)
            if (!v || isNaN(v)) return null
            return (
              <div key={label} style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)', background: 'var(--surface2)', borderRadius: 'var(--r)', padding: '6px 10px' }}>
                {label}: all 10 pts lost at <span style={{ color: 'var(--accent)' }}>{v}%</span> of library
              </div>
            )
          })}
        </div>
      </Card>

      <Card title="Exclusion Patterns">
        <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', display: 'block', marginBottom: 5 }}>Patterns</label>
        <span style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45, display: 'block', marginBottom: 8 }}>
          One pattern per line. Supports globs: <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>*.srt</span>, <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>@eaDir</span>, <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text)' }}>Featurettes</span>. Matching files are excluded from health scoring but still visible in the file explorer.
        </span>
        <textarea
          value={exclusionPatterns}
          onChange={e => setExclusionPatterns(e.target.value)}
          onFocus={() => setExclusionFocused(true)}
          onBlur={() => setExclusionFocused(false)}
          placeholder={'@eaDir\n*.srt\nFeaturettes'}
          style={{
            width: '100%', height: 120, padding: '8px 11px',
            borderRadius: 'var(--r)',
            border: `1px solid ${exclusionFocused ? 'var(--accent)' : 'var(--border2)'}`,
            background: 'var(--surface2)', color: 'var(--text)',
            fontFamily: 'var(--mono)', fontSize: 12,
            outline: 'none', resize: 'vertical',
            transition: 'border 0.12s', boxSizing: 'border-box',
          }}
        />
      </Card>

      <Card title="Appearance">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>Theme</div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>Choose between dark and light mode. Dark is the default.</div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexShrink: 0, marginLeft: 24 }}>
            {['dark', 'light'].map(t => (
              <button
                key={t}
                onClick={() => onThemeChange && onThemeChange(t)}
                style={{
                  padding: '7px 18px', borderRadius: 'var(--r)', fontSize: 12, fontWeight: 500,
                  border: `1px solid ${theme === t ? 'var(--accent)' : 'var(--border2)'}`,
                  background: theme === t ? 'var(--accent)18' : 'transparent',
                  color: theme === t ? 'var(--accent)' : 'var(--text-dim)',
                  cursor: 'pointer', transition: 'all 0.12s',
                }}
              >
                {t === 'dark' ? '🌙 Dark' : '☀️ Light'}
              </button>
            ))}
          </div>
        </div>
      </Card>

      <Card title="Audit History">
        <div style={{ marginBottom: 14 }}>
          <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>
            Last {Math.min(auditRuns?.length || 0, 50)} audit runs. History is stored in SQLite and survives restarts.
          </span>
        </div>

        {!auditRuns ? (
          <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', padding: '12px 0' }}>Loading…</div>
        ) : auditRuns.length === 0 ? (
          <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', padding: '12px 0' }}>No audit runs recorded yet.</div>
        ) : (
          <div style={{ maxHeight: 320, overflowY: 'auto', border: '1px solid var(--border)', borderRadius: 'var(--r)' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11, fontFamily: 'var(--mono)' }}>
              <thead style={{ position: 'sticky', top: 0, zIndex: 1 }}>
                <tr style={{ background: 'var(--surface2)', borderBottom: '1px solid var(--border)' }}>
                  {['Time', 'Trigger', 'Score', 'Status'].map(h => (
                    <th key={h} style={{ padding: '8px 12px', textAlign: 'left', fontSize: 9, letterSpacing: 1.5, textTransform: 'uppercase', color: 'var(--text-dim)', fontWeight: 600 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(() => {
                  // Deduplicate consecutive runs with same timestamp+trigger (gunicorn dual-worker artifact)
                  const seen = new Set()
                  return auditRuns.filter(run => {
                    const key = run.ran_at.slice(0, 16) + run.trigger + run.status
                    if (seen.has(key)) return false
                    seen.add(key)
                    return true
                  }).slice(0, 50)
                })().map((run, i) => {
                  const isOk = run.status === 'ok'
                  const dt = new Date(run.ran_at)
                  const timeStr = dt.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
                  return (
                    <tr key={run.id} style={{ borderBottom: '1px solid var(--border)', background: i % 2 === 0 ? 'transparent' : 'var(--surface2)' }}>
                      <td style={{ padding: '7px 12px', color: 'var(--text-dim)' }}>{timeStr}</td>
                      <td style={{ padding: '7px 12px', color: 'var(--text-dim)' }}>{run.trigger}</td>
                      <td style={{ padding: '7px 12px', color: isOk ? 'var(--text)' : 'var(--text-dim)', fontWeight: isOk ? 600 : 400 }}>
                        {isOk && run.health_score != null ? run.health_score : '—'}
                      </td>
                      <td style={{ padding: '7px 12px' }}>
                        <span style={{
                          padding: '2px 8px', borderRadius: 99, fontSize: 10,
                          background: isOk ? 'var(--green)18' : 'var(--red)18',
                          color: isOk ? 'var(--green)' : 'var(--red)',
                          border: `1px solid ${isOk ? 'var(--green)' : 'var(--red)'}35`,
                        }}>
                          {isOk ? 'ok' : run.error_message?.split(':')[0] || 'error'}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingTop: 16, borderTop: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-dim)' }}>
            Last audit: {lastAuditTime}
          </span>
          <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            Audits run automatically via watchdog. Use the button below to trigger one manually.
          </span>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexShrink: 0, marginLeft: 20 }}>
          {saveStatus && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: saveStatus.ok ? 'var(--green)' : 'var(--red)' }}>
              {saveStatus.ok ? '✓ ' : '✗ '}{saveStatus.msg}
            </span>
          )}
          <button onClick={handleSave} style={{ padding: '7px 18px', borderRadius: 'var(--r)', border: 'none', background: 'var(--accent)', color: '#000', fontWeight: 700, fontSize: 12, cursor: 'pointer' }}>
            Save Settings
          </button>
          {onScan && (
            <button
              onClick={onScan}
              disabled={isScanning}
              style={{
                padding: '7px 18px', borderRadius: 'var(--r)',
                border: '1px solid var(--border2)',
                background: 'transparent',
                color: isScanning ? 'var(--text-dim)' : 'var(--text)',
                fontSize: 12, fontWeight: 500, cursor: isScanning ? 'default' : 'pointer',
                opacity: isScanning ? 0.5 : 1,
              }}
            >
              {isScanning ? 'Scanning…' : '▶ Run Audit'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
