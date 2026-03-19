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
            background: 'var(--surface2)', border: `1px solid ${colors[t.type]}44`,
            borderLeft: `3px solid ${colors[t.type]}`,
            borderRadius: 'var(--r)', padding: '10px 16px',
            fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text)',
            boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
            maxWidth: 340, animation: 'slideDown 0.2s ease both',
          }}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

export const useToast = () => useContext(ToastCtx)
