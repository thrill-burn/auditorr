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

async function reqText(path, opts = {}) {
  const secret = getSecret()
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  if (secret) headers['X-Auditorr-Secret'] = secret

  const res = await fetch('/api' + path, { ...opts, headers })

  if (res.status === 401) {
    const s = window.prompt('auditorr requires an access key. Enter AUDITORR_SECRET:')
    if (s) {
      localStorage.setItem('auditorr_secret', s)
      return reqText(path, opts)
    }
    throw new Error('Authentication required')
  }

  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(data.message || 'Request failed')
  }
  return res.text()
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
  testSonarr: (url, apiKey) => req('/test_sonarr', { method: 'POST', body: JSON.stringify({ url, api_key: apiKey }) }),
  testRadarr: (url, apiKey) => req('/test_radarr', { method: 'POST', body: JSON.stringify({ url, api_key: apiKey }) }),
  actionScript: (type)    => reqText('/actions/script/' + type),
  sonarrRescan: (paths)   => req('/actions/sonarr_rescan', { method: 'POST', body: JSON.stringify({ paths }) }),
  radarrRescan: (paths)   => req('/actions/radarr_rescan', { method: 'POST', body: JSON.stringify({ paths }) }),
  sonarrSearch: (path)    => req('/actions/sonarr_search', { method: 'POST', body: JSON.stringify({ path }) }),
  radarrSearch: (path)    => req('/actions/radarr_search', { method: 'POST', body: JSON.stringify({ path }) }),
  uploadStats:  (days)    => req('/upload_stats' + (days ? '?days=' + days : '')),
}
