import React from 'react'
import { tint } from '../utils'

export default function ErrorBanner({ message }) {
  if (!message || message === 'ok' || message === 'No audit run yet.') return null
  return (
    <div style={{
      background: tint('var(--red)', 7), borderBottom: `1px solid ${tint('var(--red)', 27)}`,
      padding: '10px 24px', display: 'flex', alignItems: 'center', gap: 10,
    }}>
      <span style={{ color: 'var(--red)', fontSize: 14 }}>⚠</span>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--red)' }}>
        {message}
      </span>
    </div>
  )
}
