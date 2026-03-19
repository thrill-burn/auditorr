export function formatBytes(bytes) {
  if (!+bytes) return '0 B'
  const k = 1024, sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

export function scoreColor(score) {
  if (score >= 90) return 'var(--green)'
  if (score >= 75) return 'var(--yellow)'
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
