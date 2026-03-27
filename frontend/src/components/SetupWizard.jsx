import React, { useState } from 'react'
import { api } from '../api'

function Field({ label, hint, type = 'text', value, onChange, placeholder, style = {} }) {
  const [focused, setFocused] = useState(false)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5, ...style }}>
      <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)' }}>{label}</label>
      {hint && <span style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.45 }}>{hint}</span>}
      <input
        type={type} value={value ?? ''} placeholder={placeholder}
        onChange={e => onChange(e.target.value)}
        onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
        style={{
          padding: '8px 11px', borderRadius: 'var(--r)',
          border: `1px solid ${focused ? 'var(--accent)' : 'var(--border2)'}`,
          background: 'var(--surface2)', color: 'var(--text)',
          fontFamily: 'var(--mono)', fontSize: 13, outline: 'none',
          transition: 'border 0.12s', width: '100%', boxSizing: 'border-box',
        }}
      />
    </div>
  )
}

function StepIndicator({ current }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 0, marginBottom: 28 }}>
      {[1, 2, 3].map((n, i) => (
        <React.Fragment key={n}>
          <div style={{
            width: 28, height: 28, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 700,
            background: current === n ? 'var(--accent)' : current > n ? 'var(--accent)30' : 'var(--surface2)',
            color: current === n ? '#000' : current > n ? 'var(--accent)' : 'var(--text-dim)',
            border: `1px solid ${current >= n ? 'var(--accent)' : 'var(--border2)'}`,
            transition: 'all 0.2s',
          }}>{n}</div>
          {i < 2 && (
            <div style={{
              width: 40, height: 1,
              background: current > n ? 'var(--accent)' : 'var(--border2)',
              transition: 'background 0.2s',
            }} />
          )}
        </React.Fragment>
      ))}
    </div>
  )
}

function btnPrimary(disabled) {
  return {
    padding: '8px 20px', borderRadius: 'var(--r)', border: 'none',
    background: disabled ? 'var(--border2)' : 'var(--accent)',
    color: disabled ? 'var(--text-dim)' : '#000',
    fontWeight: 700, fontSize: 12, cursor: disabled ? 'default' : 'pointer',
    transition: 'all 0.12s',
  }
}

function btnSecondary() {
  return {
    padding: '8px 16px', borderRadius: 'var(--r)',
    border: '1px solid var(--border2)', background: 'transparent',
    color: 'var(--text-dim)', fontSize: 12, cursor: 'pointer',
  }
}

// ── Step 1: qBittorrent ───────────────────────────────────────────────────────
function Step1({ data, onChange, onNext, onSkip }) {
  const [testStatus, setTestStatus] = useState(null)
  const [advancing, setAdvancing] = useState(false)

  const handleTest = async () => {
    setTestStatus({ loading: true })
    try {
      await api.testConnection({ QB_HOST: data.QB_HOST, QB_USER: data.QB_USER, QB_PASS: data.QB_PASS })
      setTestStatus({ ok: true, msg: 'Connected!' })
    } catch (e) {
      setTestStatus({ ok: false, msg: e.message })
    }
  }

  const handleNext = () => {
    if (testStatus?.ok) { onNext(); return }
    setAdvancing(true)
    setTimeout(() => { setAdvancing(false); onNext() }, 1500)
  }

  return (
    <>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 2, textTransform: 'uppercase', color: 'var(--text-dim)', marginBottom: 6 }}>Step 1 of 3</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--text)', marginBottom: 4 }}>qBittorrent Connection</div>
      <p style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 20, lineHeight: 1.55 }}>
        Connect auditorr to your qBittorrent instance. You'll need the host URL and your login credentials.
      </p>
      <Field label="Host URL" placeholder="http://192.168.1.x:8080" value={data.QB_HOST} onChange={v => { onChange('QB_HOST', v); setTestStatus(null) }} style={{ marginBottom: 14 }} />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 20 }}>
        <Field label="Username" placeholder="admin" value={data.QB_USER} onChange={v => { onChange('QB_USER', v); setTestStatus(null) }} />
        <Field label="Password" type="password" placeholder="password" value={data.QB_PASS} onChange={v => { onChange('QB_PASS', v); setTestStatus(null) }} />
      </div>

      {advancing && (
        <div style={{ marginBottom: 14, padding: '10px 14px', borderRadius: 'var(--r)', border: '1px solid #f59e0b', background: '#f59e0b12', fontSize: 12, color: '#f59e0b' }}>
          ⚠ qBittorrent is required for auditorr to function. You can finish configuring it later in Settings.
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button onClick={handleTest} style={btnSecondary()}>Test Connection</button>
          {testStatus && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: testStatus.loading ? 'var(--text-dim)' : testStatus.ok ? 'var(--green)' : 'var(--red)' }}>
              {testStatus.loading ? 'Testing…' : (testStatus.ok ? '✓ ' : '✗ ') + testStatus.msg}
            </span>
          )}
        </div>
        <button onClick={handleNext} style={btnPrimary(false)}>Next →</button>
      </div>

      <SkipLink onSkip={onSkip} />
    </>
  )
}

// ── Step 2: Data Paths ────────────────────────────────────────────────────────
function Step2({ data, onChange, onNext, onBack, onSkip }) {
  const [pathStatus, setPathStatus] = useState(null)

  const handleTestPaths = async () => {
    setPathStatus({ loading: true })
    try {
      const result = await api.testPaths({ MEDIA_PATH: data.MEDIA_PATH, LOCAL_PATH: data.LOCAL_PATH })
      setPathStatus(result)
    } catch (e) {
      setPathStatus({ error: e.message })
    }
  }

  const clearStatus = () => setPathStatus(null)

  const mediaOk  = pathStatus?.paths?.MEDIA_PATH?.ok
  const localOk  = pathStatus?.paths?.LOCAL_PATH?.ok

  return (
    <>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 2, textTransform: 'uppercase', color: 'var(--text-dim)', marginBottom: 6 }}>Step 2 of 3</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--text)', marginBottom: 4 }}>Data Paths</div>
      <p style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 20, lineHeight: 1.55 }}>
        Tell auditorr where your media library and torrent downloads live inside this container.
      </p>

      <Field label="Media Path"
        hint="Where your final media library lives inside this container — e.g. /data/media"
        placeholder="/data/media" value={data.MEDIA_PATH}
        onChange={v => { onChange('MEDIA_PATH', v); clearStatus() }}
        style={{ marginBottom: 6 }}
      />
      {pathStatus && !pathStatus.loading && pathStatus.paths && (
        <div style={{ marginBottom: 12, fontFamily: 'var(--mono)', fontSize: 11, color: mediaOk ? 'var(--green)' : 'var(--red)' }}>
          {mediaOk ? '✓ Path exists' : '✗ ' + (pathStatus.paths.MEDIA_PATH?.message || 'Path not found')}
        </div>
      )}

      <Field label="Local Torrent Path"
        hint="Where torrent files live on disk from this container's perspective — e.g. /data/torrents"
        placeholder="/data/torrents" value={data.LOCAL_PATH}
        onChange={v => { onChange('LOCAL_PATH', v); clearStatus() }}
        style={{ marginBottom: 6 }}
      />
      {pathStatus && !pathStatus.loading && pathStatus.paths && (
        <div style={{ marginBottom: 12, fontFamily: 'var(--mono)', fontSize: 11, color: localOk ? 'var(--green)' : 'var(--red)' }}>
          {localOk ? '✓ Path exists' : '✗ ' + (pathStatus.paths.LOCAL_PATH?.message || 'Path not found')}
        </div>
      )}

      {pathStatus?.error && (
        <div style={{ marginBottom: 12, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--red)' }}>✗ {pathStatus.error}</div>
      )}

      <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button onClick={onBack} style={btnSecondary()}>← Back</button>
          <button onClick={handleTestPaths} style={btnSecondary()}>
            {pathStatus?.loading ? 'Testing…' : 'Test Paths'}
          </button>
        </div>
        <button onClick={onNext} style={btnPrimary(false)}>Next →</button>
      </div>

      <SkipLink onSkip={onSkip} />
    </>
  )
}

// ── Step 3: Sonarr & Radarr ───────────────────────────────────────────────────
function Step3({ data, onChange, onBack, onComplete, onSkip }) {
  const [sonarrStatus, setSonarrStatus] = useState(null)
  const [radarrStatus, setRadarrStatus] = useState(null)

  const handleTestSonarr = async () => {
    setSonarrStatus({ loading: true })
    try {
      await api.testSonarr(data.SONARR_URL, data.SONARR_API_KEY)
      setSonarrStatus({ ok: true, msg: 'Connected!' })
    } catch (e) { setSonarrStatus({ ok: false, msg: e.message }) }
  }

  const handleTestRadarr = async () => {
    setRadarrStatus({ loading: true })
    try {
      await api.testRadarr(data.RADARR_URL, data.RADARR_API_KEY)
      setRadarrStatus({ ok: true, msg: 'Connected!' })
    } catch (e) { setRadarrStatus({ ok: false, msg: e.message }) }
  }

  return (
    <>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 9, letterSpacing: 2, textTransform: 'uppercase', color: 'var(--text-dim)', marginBottom: 6 }}>Step 3 of 3</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--text)', marginBottom: 4 }}>Sonarr & Radarr</div>
      <p style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 20, lineHeight: 1.55 }}>
        Optional. Required for interactive search in the Media explorer. API keys are in each app under Settings → General.
      </p>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, marginBottom: 8 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Field label="Sonarr URL" placeholder="http://192.168.1.x:8989" value={data.SONARR_URL}
            onChange={v => { onChange('SONARR_URL', v); setSonarrStatus(null) }} />
          <Field label="Sonarr API Key" type="password" placeholder="paste API key…" value={data.SONARR_API_KEY}
            onChange={v => { onChange('SONARR_API_KEY', v); setSonarrStatus(null) }} />
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <button onClick={handleTestSonarr} style={btnSecondary()}>Test Sonarr</button>
            {sonarrStatus && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: sonarrStatus.loading ? 'var(--text-dim)' : sonarrStatus.ok ? 'var(--green)' : 'var(--red)' }}>
                {sonarrStatus.loading ? 'Testing…' : (sonarrStatus.ok ? '✓ ' : '✗ ') + sonarrStatus.msg}
              </span>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Field label="Radarr URL" placeholder="http://192.168.1.x:7878" value={data.RADARR_URL}
            onChange={v => { onChange('RADARR_URL', v); setRadarrStatus(null) }} />
          <Field label="Radarr API Key" type="password" placeholder="paste API key…" value={data.RADARR_API_KEY}
            onChange={v => { onChange('RADARR_API_KEY', v); setRadarrStatus(null) }} />
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <button onClick={handleTestRadarr} style={btnSecondary()}>Test Radarr</button>
            {radarrStatus && (
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: radarrStatus.loading ? 'var(--text-dim)' : radarrStatus.ok ? 'var(--green)' : 'var(--red)' }}>
                {radarrStatus.loading ? 'Testing…' : (radarrStatus.ok ? '✓ ' : '✗ ') + radarrStatus.msg}
              </span>
            )}
          </div>
        </div>
      </div>

      <div style={{ marginTop: 20, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <button onClick={onBack} style={btnSecondary()}>← Back</button>
        <button onClick={onComplete} style={btnPrimary(false)}>Finish Setup</button>
      </div>

      <SkipLink onSkip={onSkip} />
    </>
  )
}

function SkipLink({ onSkip }) {
  return (
    <div style={{ textAlign: 'center', marginTop: 18 }}>
      <button onClick={onSkip} style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 11, color: 'var(--text-dim)', padding: 0, textDecoration: 'underline' }}>
        Skip setup
      </button>
    </div>
  )
}

// ── Wizard shell ──────────────────────────────────────────────────────────────
export default function SetupWizard({ onComplete, onSkip }) {
  const [step, setStep] = useState(1)
  const [wizardData, setWizardData] = useState({
    QB_HOST: '', QB_USER: '', QB_PASS: '',
    MEDIA_PATH: '/data/media', LOCAL_PATH: '/data/torrents', REMOTE_PATH: '/data/torrents',
    SONARR_URL: '', SONARR_API_KEY: '',
    RADARR_URL: '', RADARR_API_KEY: '',
  })

  const set = (key, val) => setWizardData(d => ({ ...d, [key]: val }))

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 1000,
      background: 'rgba(0,0,0,0.75)',
      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
      padding: 24,
    }}>
      <div style={{
        width: '100%', maxWidth: 560,
        background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--rl)', padding: 32,
        maxHeight: '90vh', overflowY: 'auto',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 24 }}>
          <img src="/favicon.ico" alt="" style={{ width: 22, height: 22, opacity: 0.85 }} onError={e => { e.currentTarget.style.display = 'none' }} />
          <span style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 700, color: 'var(--text)', letterSpacing: 0.5 }}>auditorr</span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-dim)', marginLeft: 4 }}>setup</span>
        </div>

        <StepIndicator current={step} />

        {step === 1 && (
          <Step1
            data={wizardData}
            onChange={set}
            onNext={() => setStep(2)}
            onSkip={onSkip}
          />
        )}
        {step === 2 && (
          <Step2
            data={wizardData}
            onChange={set}
            onNext={() => setStep(3)}
            onBack={() => setStep(1)}
            onSkip={onSkip}
          />
        )}
        {step === 3 && (
          <Step3
            data={wizardData}
            onChange={set}
            onBack={() => setStep(2)}
            onComplete={() => onComplete(wizardData)}
            onSkip={onSkip}
          />
        )}
      </div>
    </div>
  )
}
