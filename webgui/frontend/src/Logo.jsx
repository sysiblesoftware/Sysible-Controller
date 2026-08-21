import React from 'react'

// The Sysible Controller mark — a sibling of the Sysible Linux Engineering
// Platform mark. Same family tile (dark rounded square + thin brand-green ring),
// but its own glyph: a central hub wired to six nodes — the Controller
// orchestrating a fleet of agents. Distinct from SLEP's code-bracket + run
// triangle. Inlined so it stays crisp at any size and needs no network.
// Source of truth: /public/logo.svg.
export default function Logo({ size = 34 }) {
  // Six satellite nodes on a hexagon around the hub (center 64,64, radius 36).
  const spokes = [[64, 28], [95.2, 46], [95.2, 82], [64, 100], [32.8, 82], [32.8, 46]]
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
      {/* Spokes from the hub out to each node */}
      <g stroke="#4c9e5a" strokeWidth="3" strokeLinecap="round">
        {spokes.map(([x, y], i) => <line key={i} x1="64" y1="64" x2={x} y2={y} />)}
      </g>
      {/* Satellite nodes (brand blue) */}
      <g fill="#7aa2ff">
        {spokes.map(([x, y], i) => <circle key={i} cx={x} cy={y} r="7" />)}
      </g>
      {/* Central hub (brand green) */}
      <circle cx="64" cy="64" r="12.5" fill="#6ddb73" />
    </svg>
  )
}
