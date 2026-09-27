import React from 'react'
import { reportClientError } from '../api'
import { Button, WorkflowPage, MONO_TITLE, tint } from './workflows/shared'

// A render error used to unmount the whole app and leave the dark background,
// with nothing on screen and nothing in the server log (2026-09-25: Segmented's
// All state threw whenever a scan recorded changes). Two of these now catch it.
//
//  - scope="page" wraps the page area in App.jsx, keyed by tab, so the sidebar
//    survives and switching page clears the error. It also clears on
//    `auditorr:audit_complete`, because new data is the likeliest fix.
//  - scope="app" wraps everything in main.jsx, for the shell outside a page:
//    the sidebar, overlays and modals.
//
// Each error is shown and sent to the server log once per page load. The log
// line reaches `docker logs` and /api/debug/report.

// React's component stack, innermost first, one frame per line: "    at
// Segmented (http://…/index.js:1:2)" in Chromium, "Segmented@http://…" in
// Firefox. DOM elements ("at div") are lowercase and left out. Names survive a
// production build only because vite.config.js sets `keepNames`.
function componentChain(stack) {
  const names = []
  for (const line of (stack || '').split('\n')) {
    const m = line.trim().match(/^(?:(?:at|in)\s+)?(?:new\s+)?([A-Z][\w$]*)/)
    if (!m) continue
    if (m[1] === 'ErrorBoundary') break
    if (names[names.length - 1] !== m[1]) names.push(m[1])
  }
  return names.slice(0, 6).reverse().join(' › ')
}

function describe(error) {
  if (error instanceof Error) return { name: error.name || 'Error', message: error.message || '(no message)' }
  return { name: 'Error', message: String(error) }
}

// key → Promise<boolean>, so a page that crashes again after Try again or a scan
// shows the same logged state without a second log line. A report that did not
// get through is forgotten, so the next crash tries again.
const reported = new Map()

function report(page, error, where) {
  const { name, message } = describe(error)
  const key = [page, name, message, where].join('\u0000')
  if (!reported.has(key)) {
    reported.set(key, reportClientError({ page, name, message, where }).then(ok => {
      if (!ok) reported.delete(key)
      return ok
    }))
  }
  return reported.get(key)
}

function currentPage() {
  return window.location.hash.replace('#', '') || 'dashboard'
}

function logLine(logged) {
  if (logged === null) return 'Sending it to the server log…'
  if (logged) return 'It was also written to the server log, so it shows in docker logs and the debug report.'
  return 'It could not be sent to the server log. The browser console has the full error.'
}

function ErrorDetail({ error, where }) {
  const { name, message } = describe(error)
  return (
    <>
      <div style={{ ...MONO_TITLE, overflowWrap: 'anywhere' }}>{name}: {message}</div>
      {where && (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-sm)', color: 'var(--text-dim)', overflowWrap: 'anywhere' }}>
          in {where}
        </div>
      )}
    </>
  )
}

const PROSE = { fontSize: 'var(--font-base)', color: 'var(--text-dim)', lineHeight: 1.6, margin: 0 }

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null, where: '', logged: null }
    this.reset = () => this.setState({ error: null, where: '', logged: null })
    this.onAuditComplete = () => { if (this.state.error) this.reset() }
  }

  static getDerivedStateFromError(error) {
    return { error, where: '', logged: null }
  }

  componentDidMount() {
    window.addEventListener('auditorr:audit_complete', this.onAuditComplete)
  }

  componentWillUnmount() {
    window.removeEventListener('auditorr:audit_complete', this.onAuditComplete)
  }

  componentDidCatch(error, info) {
    const where = componentChain(info?.componentStack)
    this.setState({ where })
    if (this.props.scope === 'app') {
      // The app applies the saved theme in an effect, which never ran if the
      // first render is what threw.
      try {
        if (localStorage.getItem('auditorr_theme') === 'light') document.documentElement.setAttribute('data-theme', 'light')
      } catch (_) {}
    }
    report(this.props.page || currentPage(), error, where).then(logged => {
      if (this.state.error === error) this.setState({ logged })
    })
  }

  render() {
    const { error, where, logged } = this.state
    if (!error) return this.props.children

    const actions = (
      <div style={{ display: 'flex', gap: 8 }}>
        <Button onClick={this.reset}>Try again</Button>
        <Button variant="ghost" onClick={() => window.location.reload()}>Reload page</Button>
      </div>
    )

    if (this.props.scope === 'app') {
      return (
        <div style={{ minHeight: '100vh', background: 'var(--bg)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
          <div role="alert" style={{
            background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12,
            boxShadow: 'var(--elev-1)', padding: '28px 32px', maxWidth: 600, width: '100%',
            display: 'flex', flexDirection: 'column', gap: 10,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 2 }}>
              <span style={{ width: 8, height: 8, borderRadius: 99, background: 'var(--red)', flexShrink: 0 }} />
              <span style={{ fontSize: 'var(--font-lg)', fontWeight: 600, color: 'var(--text)' }}>auditorr stopped with an error</span>
            </div>
            <ErrorDetail error={error} where={where} />
            <p style={{ ...PROSE, marginTop: 4 }}>{logLine(logged)}</p>
            <div style={{ marginTop: 6 }}>{actions}</div>
          </div>
        </div>
      )
    }

    return (
      <WorkflowPage gap={16} maxWidth={760}>
        <div role="alert" style={{
          padding: '14px 16px', background: tint('var(--red)', 6), border: `1px solid ${tint('var(--red)', 19)}`,
          borderRadius: 'var(--r)', display: 'flex', flexDirection: 'column', gap: 6,
        }}>
          <div style={{ fontSize: 'var(--font-md)', fontWeight: 600, color: 'var(--red)' }}>This page stopped with an error</div>
          <ErrorDetail error={error} where={where} />
        </div>
        <p style={PROSE}>The rest of auditorr still works, so you can pick another page from the sidebar. {logLine(logged)}</p>
        {actions}
      </WorkflowPage>
    )
  }
}
