import React from 'react'

export function Card({ title, headerRight, hover = false, children, style, className = '' }) {
  return (
    <section className={`ui-card${hover ? ' ui-card-hover' : ''}${className ? ` ${className}` : ''}`} style={style}>
      {(title || headerRight) && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 14 }}>
          {title && <SectionLabel>{title}</SectionLabel>}
          {headerRight}
        </div>
      )}
      {children}
    </section>
  )
}

export function SectionLabel({ children, eyebrow = false, style }) {
  return (
    <div className={eyebrow ? 'ui-table-header' : 'ui-section-title'} style={{ textAlign: 'left', ...style }}>
      {children}
    </div>
  )
}

export function Button({ variant = 'default', children, className = '', style, ...props }) {
  const variantClass = variant === 'primary' ? ' ui-button-primary' : variant === 'ghost' ? ' ui-button-ghost' : ''
  return (
    <button className={`ui-button${variantClass}${className ? ` ${className}` : ''}`} style={style} {...props}>
      {children}
    </button>
  )
}

export function Chip({ active = false, color, children, style, className = '', ...props }) {
  return (
    <button
      className={`ui-chip${active ? ' ui-chip-active' : ''}${className ? ` ${className}` : ''}`}
      style={{ ...(color ? { borderColor: active ? `${color}66` : 'var(--border2)' } : null), ...style }}
      {...props}
    >
      {color && <span className="ui-status-dot" style={{ background: color, width: 6, height: 6 }} />}
      {children}
    </button>
  )
}

export function StatTile({ label, value, sub, color, style }) {
  return (
    <div className="ui-card" style={{ padding: '12px 16px', minWidth: 140, ...style }}>
      <div className="ui-metric-label" style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 5 }}>
        {color && <span className="ui-status-dot" style={{ background: color }} />}
        {label}
      </div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 20, fontWeight: 700, color: 'var(--text)', lineHeight: 1 }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

export function SegmentedControl({ options, value, onChange, getLabel = x => x, getValue = x => x, style }) {
  return (
    <div style={{ display: 'inline-flex', gap: 4, alignItems: 'center', ...style }}>
      {options.map(opt => {
        const v = getValue(opt)
        const active = v === value
        return (
          <button
            key={String(v)}
            onClick={() => onChange(v)}
            className={`ui-chip${active ? ' ui-chip-active' : ''}`}
            style={{ padding: '4px 10px', borderRadius: 6 }}
          >
            {getLabel(opt)}
          </button>
        )
      })}
    </div>
  )
}
