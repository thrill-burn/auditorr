// Read secret from localStorage (set on first load if AUDITORR_SECRET env is needed)
function getSecret() {
  return localStorage.getItem('auditorr_secret') || ''
}

async function req(path, opts = {}, retried = false) {
  const secret = getSecret()
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  if (secret) headers['X-Auditorr-Secret'] = secret

  const res = await fetch('/api' + path, { ...opts, headers })

  if (res.status === 401) {
    if (retried) throw new Error('Authentication required')
    // Prompt for secret if auth fails
    const s = window.prompt('auditorr requires an access key. Enter AUDITORR_SECRET:')
    if (s) {
      localStorage.setItem('auditorr_secret', s)
      return req(path, opts, true)
    }
    throw new Error('Authentication required')
  }

  const data = await res.json()
  if (!res.ok) {
    const err = new Error(data.message || data.error || 'Request failed')
    err.code = data.code
    throw err
  }
  return data
}

async function reqText(path, opts = {}, retried = false) {
  const secret = getSecret()
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  if (secret) headers['X-Auditorr-Secret'] = secret

  const res = await fetch('/api' + path, { ...opts, headers })

  if (res.status === 401) {
    if (retried) throw new Error('Authentication required')
    const s = window.prompt('auditorr requires an access key. Enter AUDITORR_SECRET:')
    if (s) {
      localStorage.setItem('auditorr_secret', s)
      return reqText(path, opts, true)
    }
    throw new Error('Authentication required')
  }

  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    const err = new Error(data.message || data.error || 'Request failed')
    err.code = data.code
    throw err
  }
  return res.text()
}

export const api = {
  results:        ()     => req('/results'),
  nextSteps:      ()     => req('/next_steps'),
  files:          (tab)  => req('/files?tab=' + tab),
  progress:       ()     => req('/progress'),
  changes:        ()     => req('/changes'),
  changeLog:      ()     => req('/change_log'),
  auditHistory:   ()     => req('/audit_history'),
  clearHistory:   ()     => req('/clear_history', { method: 'POST' }),
  startScan:      ()     => req('/start_scan', { method: 'POST' }),
  getConfig:      ()     => req('/config'),
  saveConfig:     (conf) => req('/config', { method: 'POST', body: JSON.stringify(conf) }),
  testConnection: (conf) => req('/test_connection', { method: 'POST', body: JSON.stringify(conf) }),
  testPaths:      (conf) => req('/test_paths',      { method: 'POST', body: JSON.stringify(conf) }),
  testSonarr: (url, apiKey) => req('/test_sonarr', { method: 'POST', body: JSON.stringify({ url, api_key: apiKey }) }),
  testRadarr: (url, apiKey) => req('/test_radarr', { method: 'POST', body: JSON.stringify({ url, api_key: apiKey }) }),
  testArrConnections: (conf) => req('/test_arr_connections', { method: 'POST', body: JSON.stringify(conf) }),
  actionScript: (type, body) => body
    ? reqText('/actions/script/' + type, { method: 'POST', body: JSON.stringify(body) })
    : reqText('/actions/script/' + type),
  sonarrRescan: (paths)   => req('/actions/sonarr_rescan', { method: 'POST', body: JSON.stringify({ paths }) }),
  radarrRescan: (paths)   => req('/actions/radarr_rescan', { method: 'POST', body: JSON.stringify({ paths }) }),
  forceImport: (items)    => req('/workflows/force_import', { method: 'POST', body: JSON.stringify({ items }) }),
  sonarrSearch: (path)    => req('/actions/sonarr_search', { method: 'POST', body: JSON.stringify({ path }) }),
  radarrSearch: (path)    => req('/actions/radarr_search', { method: 'POST', body: JSON.stringify({ path }) }),
  uploadStats: (params) => {
    if (params && typeof params === 'object') {
      const q = new URLSearchParams()
      if (params.from) q.set('from', params.from)
      if (params.to)   q.set('to',   params.to)
      return req('/upload_stats?' + q.toString())
    }
    return req('/upload_stats?days=' + params)
  },
  uploadSnapshotCounts: ()               => req('/upload_snapshots/source_counts'),
  retagUploadSnapshots:  (from, to, source) => req('/upload_snapshots/retag',  { method: 'POST', body: JSON.stringify({ from, to: to || undefined, source }) }),
  deleteUploadSnapshots: (from, to)         => req('/upload_snapshots/delete', { method: 'POST', body: JSON.stringify({ from, to: to || undefined }) }),
  acquireCandidates: ()   => req('/workflows/acquire_candidates'),
  acquireReleases: (params) => req('/workflows/acquire_releases?' + new URLSearchParams(params)),
  saveAcquirePrefs: (prefs) => req('/workflows/acquire_prefs', { method: 'POST', body: JSON.stringify(prefs) }),
  grabRelease:        (params) => req('/workflows/grab_release',      { method: 'POST', body: JSON.stringify(params) }),
  watchImport:        (params) => req('/workflows/watch_import',      { method: 'POST', body: JSON.stringify(params) }),
  watchImportStatus:  (jobId)  => req('/workflows/watch_import/status?job_id=' + jobId),
  watchImportActive:  ()       => req('/workflows/watch_import/active'),
  startGenerate:  (params) => req('/workflows/generate',        { method: 'POST', body: JSON.stringify(params) }),
  generateStatus: (jobId)  => req('/workflows/generate/status?job_id=' + jobId),
  stopGenerate:   (jobId)  => req('/workflows/generate/stop',   { method: 'POST', body: JSON.stringify({ job_id: jobId }) }),
  workflowIndexers: ()   => req('/workflows/indexers'),
  triageReport:   ()         => req('/workflows/triage'),
  triageVerify:   (items)    => req('/workflows/triage/verify', { method: 'POST', body: JSON.stringify({ items }) }),
  cleanupReport:  ()         => req('/workflows/cleanup'),
  dedupeReport:   ()         => req('/workflows/dedupe'),
  excludePatterns: (patterns) => req('/workflows/exclude', { method: 'POST', body: JSON.stringify({ patterns }) }),
  removeTorrents: (items, deleteFiles = true) => req('/workflows/remove_torrents', { method: 'POST', body: JSON.stringify({ items, delete_files: deleteFiles }) }),
  triageResolveGroups: (hashes) => req('/workflows/triage/resolve_groups', { method: 'POST', body: JSON.stringify({ hashes }) }),
  trumpParse:        (pmText)  => req('/workflows/trump/parse',          { method: 'POST', body: JSON.stringify({ pm_text: pmText }) }),
  trumpResolveGroup: (oldTitles, seedHashes, indexer) => req('/workflows/trump/resolve_group', { method: 'POST', body: JSON.stringify({ old_titles: oldTitles, seed_hashes: seedHashes, indexer }) }),
  trumpSearchRelease: (params) => req('/workflows/trump/search_release', { method: 'POST', body: JSON.stringify(params) }),
  trumpExecute:      (params)  => req('/workflows/trump/execute',        { method: 'POST', body: JSON.stringify(params) }),
  browseData:    ()       => req('/browse_data'),
  sourceInfo:    ()       => req('/source_info'),
  fetchSavePath: (creds)  => req('/source_save_path', { method: 'POST', body: JSON.stringify(creds) }),
}
