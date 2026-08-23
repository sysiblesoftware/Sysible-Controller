import React from 'react'

// The Sysible Controller mark — the canonical spoked topology from the website:
// a dark tile + brand-green ring, a central GREEN hub wired out to six satellite
// nodes (alternating white / brand-blue) by thin grey connectors. Distinct from
// SLEP's code-bracket + run-triangle mark, and matched to the Enterprise edition.
// Inlined so it stays crisp at any size and needs no network.
// Source of truth: /public/logo.svg.
export default function Logo({ size = 34 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 128 128" role="img" aria-label="Sysible Controller" style={{ display: 'block', borderRadius: size * 0.22 }}>
      <defs>
        <linearGradient id="ctrl-tile" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#161d29" />
          <stop offset="1" stopColor="#0a0d13" />
        </linearGradient>
      </defs>
      <rect x="6" y="6" width="116" height="116" rx="28" ry="28" fill="url(#ctrl-tile)" />
      <rect x="8.5" y="8.5" width="111" height="111" rx="25.5" ry="25.5" fill="none" stroke="#6ddb73" strokeWidth="4" />
      <g stroke="#8aa0c8" strokeWidth="2.7" strokeLinecap="round" opacity="0.85">
        <line x1="64" y1="64" x2="100" y2="64" />
        <line x1="64" y1="64" x2="82" y2="95.18" />
        <line x1="64" y1="64" x2="46" y2="95.18" />
        <line x1="64" y1="64" x2="28" y2="64" />
        <line x1="64" y1="64" x2="46" y2="32.82" />
        <line x1="64" y1="64" x2="82" y2="32.82" />
      </g>
      <circle cx="100" cy="64" r="6.6" fill="#eceff3" />
      <circle cx="82" cy="95.18" r="6.6" fill="#7aa2ff" />
      <circle cx="46" cy="95.18" r="6.6" fill="#eceff3" />
      <circle cx="28" cy="64" r="6.6" fill="#7aa2ff" />
      <circle cx="46" cy="32.82" r="6.6" fill="#eceff3" />
      <circle cx="82" cy="32.82" r="6.6" fill="#7aa2ff" />
      <circle cx="64" cy="64" r="11" fill="#6ddb73" />
    </svg>
  )
}
