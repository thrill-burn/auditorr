export function formatBytes(bytes) {
  if (!+bytes) return '0 B'
  const k = 1024, sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

export function formatBytesCompact(bytes) {
  if (!+bytes) return '0 B'
  const k = 1024, sizes = ['B', 'K', 'M', 'G', 'T']
  const i = Math.min(Math.floor(Math.log(Math.abs(bytes)) / Math.log(k)), sizes.length - 1)
  const value = bytes / Math.pow(k, i)
  const precision = value >= 100 ? 0 : value >= 10 ? 1 : 2
  return `${value.toFixed(precision)}${sizes[i]}`
}

// Clipboard write that also works outside secure contexts (plain-http LAN)
export function copyText(text) {
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => {})
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none'
  document.body.appendChild(ta)
  ta.focus()
  ta.select()
  try { document.execCommand('copy') } catch (_) {}
  document.body.removeChild(ta)
}

// Clean title from a release file/folder name — for prescreening a torrent
// client's search box, which filters per word. Mirrors arr.py's
// _parse_title_from_filename in miniature.
export function parseReleaseTitle(name) {
  let t = String(name || '').replace(/\.[^.]+$/, '')
  t = t.replace(/[._]/g, ' ')
  t = t.split(/[Ss]\d{1,2}[Ee]\d{1,3}/)[0]
  t = t.split(/\b(?:19|20)\d{2}\b/)[0]
  t = t.replace(/\s+(2160p|1080p|1080i|720p|480p|4K|BluRay|BDRip|BRRip|WEB-DL|WEBRip|WEB|HDTV|DVDRip|DVD|REMUX|x264|x265|HEVC)(?=[\s)\]]|$).*$/i, '')
  t = t.replace(/\s+/g, ' ').trim()
  return t.replace(/[\s(\[\-–—]+$/, '')
}

export function scoreColor(score) {
  if (score >= 90) return 'var(--cyan)'
  if (score >= 75) return 'var(--green)'
  if (score >= 50) return 'var(--yellow)'
  return 'var(--red)'
}

export function buildFlatList(files) {
  // Returns a flat array of { type:'dir'|'file', depth, name, node, path }
  // suitable for react-window virtualisation
  const result = []

  function walk(children, depth) {
    const keys   = Object.keys(children)
    const dirs   = keys.filter(k => children[k]._isDir).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    const ffiles = keys.filter(k => !children[k]._isDir).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    for (const k of [...dirs, ...ffiles]) {
      const node = children[k]
      result.push({ type: node._isDir ? 'dir' : 'file', depth, name: k, node })
      if (node._isDir && node._open) walk(node.children, depth + 1)
    }
  }

  walk(files, 0)
  return result
}

export function buildTree(files) {
  const root = { _isDir: true, children: {}, size: 0, _open: false }
  for (const file of files) {
    let cur = root
    cur.size += file.size
    const parts = file.path.split('/')
    parts.forEach((part, i) => {
      if (i === parts.length - 1) {
        cur.children[part] = file
      } else {
        if (!cur.children[part]) cur.children[part] = { _isDir: true, children: {}, size: 0, _open: false }
        cur = cur.children[part]
        cur.size += file.size
      }
    })
  }
  return root
}
