// Read secret from localStorage (set on first load if AUDITORR_SECRET env is needed)
function getSecret() {
  return localStorage.getItem('auditorr_secret') || ''
}

async function req(path, opts = {}) {
  const secret = getSecret()
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  if (secret) headers['X-Auditorr-Secret'] = secret

  const res = await fetch('/api' + path, { ...opts, headers })

  if (res.status === 401) {
    // Prompt for secret if auth fails
    const s = window.prompt('auditorr requires an access key. Enter AUDITORR_SECRET:')
    if (s) {
      localStorage.setItem('auditorr_secret', s)
      return req(path, opts)   // retry once
    }
    throw new Error('Authentication required')
  }

  const data = await res.json()
  if (!res.ok) throw new Error(data.message || 'Request failed')
  return data
}

export const api = {
  results:        ()     => req('/results'),
  progress:       ()     => req('/progress'),
  changes:        ()     => req('/changes'),
  auditHistory:   ()     => req('/audit_history'),
  clearHistory:   ()     => req('/clear_history', { method: 'POST' }),
  startScan:      ()     => req('/start_scan', { method: 'POST' }),
  getConfig:      ()     => req('/config'),
  saveConfig:     (conf) => req('/config', { method: 'POST', body: JSON.stringify(conf) }),
  testConnection: (conf) => req('/test_connection', { method: 'POST', body: JSON.stringify(conf) }),
}
