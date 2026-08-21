import React from 'react'

// The Sysible Controller mark — a sibling of the Sysible Linux Engineering
// Platform mark (dark tile + green ring family). Inlined so it stays crisp at
// any size and needs no network. Source of truth: /public/logo.svg.
export default function Logo({ size = 34 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 128 128" role="img" aria-label="Sysible Controller" style={{ display: 'block', borderRadius: size * 0.22 }}>
      <defs>
        <linearGradient id="ctrl-tile" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#161d29" />
          <stop offset="1" stopColor="#0a0d14" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="124" height="124" rx="28" ry="28" fill="url(#ctrl-tile)" />
      <rect x="3.5" y="3.5" width="121" height="121" rx="26.5" ry="26.5" fill="none" stroke="#6ddb73" strokeWidth="2" />
      <g fill="none" stroke="#7aa2ff" strokeWidth="6.5" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="50,44 33,64 50,84" />
        <polyline points="78,44 95,64 78,84" />
      </g>
      <path d="M56 50 L56 78 L75 64 Z" fill="#6ddb73" />
    </svg>
  )
}
