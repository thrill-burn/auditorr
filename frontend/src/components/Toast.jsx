import React, { createContext, useContext, useState, useCallback } from 'react'

const ToastCtx = createContext(null)

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const add = useCallback((message, type = 'info', duration = 4000) => {
    const id = Date.now() + Math.random()
    setToasts(t => [...t, { id, message, type }])
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), duration)
  }, [])

  const colors = { info: 'var(--blue)', success: 'var(--green)', error: 'var(--red)', warning: 'var(--yellow)' }

  return (
    <ToastCtx.Provider value={add}>
      {children}
      <div style={{ position: 'fixed', bottom: 24, right: 24, zIndex: 999, display: 'flex', flexDirection: 'column', gap: 8 }}>
        {toasts.map(t => (
          <div key={t.id} className="fade-in" style={{
            background: 'var(--surface)', border: '1px solid var(--border2)',
            borderRadius: 'var(--r)', padding: '10px 16px',
            fontFamily: 'var(--sans)', fontSize: 12, color: 'var(--text)',
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
