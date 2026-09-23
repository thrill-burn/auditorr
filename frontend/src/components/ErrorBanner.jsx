import React from 'react'
import { tint } from '../utils'

export default function ErrorBanner({ message }) {
  if (!message || message === 'ok' || message === 'No audit run yet.') return null
  return (
    <div style={{
      background: tint('var(--red)', 7), borderBottom: `1px solid ${tint('var(--red)', 27)}`,
      padding: '10px 24px', display: 'flex', alignItems: 'center', gap: 10,
    }}>
      <span style={{ color: 'var(--red)', fontSize: 'var(--font-md)' }}>⚠</span>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 'var(--font-base)', color: 'var(--red)' }}>
        {message}
      </span>
    </div>
  )
}
