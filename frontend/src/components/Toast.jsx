import React, { createContext, useContext, useState, useCallback, useLayoutEffect, useMemo } from 'react'
import { flushSync } from 'react-dom'

const ToastCtx = createContext(null)
const CornerCtx = createContext(null)

// The panels fixed in the bottom-right corner, from the bottom up; toasts go
// above them all. They used to share one spot and cover each other: a Trumped
// grab opens Import Jobs and toasts in the same moment, and its re-audit's scan
// card lands on the panel. Import Jobs is lowest because it's the one you click
// into, so the scan card and the toasts come and go above it without moving it.
const CORNER_ORDER = ['imports', 'scan']
const EDGE = 24
const GAP = 8

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  // Each open panel's height, by name.
  const [heights, setHeights] = useState({})

  const add = useCallback((message, type = 'info', duration = 4000) => {
    const id = Date.now() + Math.random()
    setToasts(t => [...t, { id, message, type }])
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), duration)
  }, [])

  const report = useCallback((name, height) =>
    setHeights(h => (h[name] || 0) === height ? h : { ...h, [name]: height }), [])

  // Each panel's bottom edge: the corner, plus every open panel below it.
  const corner = useMemo(() => {
    const bottoms = {}
    let bottom = EDGE
    for (const name of CORNER_ORDER) {
      bottoms[name] = bottom
      if (heights[name]) bottom += heights[name] + GAP
    }
    return { report, bottoms, toasts: bottom }
  }, [report, heights])

  const colors = { info: 'var(--blue)', success: 'var(--green)', error: 'var(--red)', warning: 'var(--yellow)' }

  return (
    <ToastCtx.Provider value={add}>
      <CornerCtx.Provider value={corner}>
        {children}
      </CornerCtx.Provider>
      <div style={{ position: 'fixed', bottom: corner.toasts, right: EDGE, zIndex: 999, display: 'flex', flexDirection: 'column', gap: GAP }}>
        {toasts.map(t => (
          <div key={t.id} className="fade-in" style={{
            background: 'var(--surface)', border: '1px solid var(--border2)',
            borderRadius: 'var(--r)', padding: '10px 16px',
            fontFamily: 'var(--sans)', fontSize: 'var(--font-base)', color: 'var(--text)',
            boxShadow: 'var(--shadow-pop)',
            maxWidth: 340, animation: 'slideDown 0.2s ease both',
            display: 'flex', alignItems: 'center', gap: 8,
          }}>
            <span className="ui-status-dot" style={{ width: 7, height: 7, background: colors[t.type] }} />
            {t.message}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

export const useToast = () => useContext(ToastCtx)

// A panel fixed in the bottom-right corner, named in CORNER_ORDER. While `open`
// it reports its height, following it as it grows; it returns the `bottom` the
// panel sits at, above every open panel lower in the order.
export function useCornerStack(name, ref, open) {
  const corner = useContext(CornerCtx)
  const report = corner?.report
  useLayoutEffect(() => {
    const el = ref.current
    if (!open || !el || !report) return
    const measure = () => report(name, el.offsetHeight)
    measure()
    // An observer fires between layout and paint, and an update from it would
    // render a frame later, so the panels above would overlap a growing one for
    // that frame. flushSync moves them in the same one.
    const observer = new ResizeObserver(() => flushSync(measure))
    observer.observe(el)
    return () => { observer.disconnect(); report(name, 0) }
  }, [name, ref, open, report])
  return corner?.bottoms[name] ?? EDGE
}
