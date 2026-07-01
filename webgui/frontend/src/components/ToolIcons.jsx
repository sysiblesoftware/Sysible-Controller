import React from "react";

// Inline SVG icons approximating the desktop's FontAwesome (fa5s.*) set used
// on the System Administration tiles. Stroke uses currentColor so the tint
// classes (.ico-*) color them.
const P = {
  users: <><circle cx="9" cy="8" r="3" /><path d="M3 20c0-3 3-5 6-5s6 2 6 5" /><circle cx="17" cy="9" r="2.2" /><path d="M16 15c3 0 5 2 5 5" /></>,
  heartbeat: <><path d="M3 12h4l2-5 3 9 2-4h7" /></>,
  cogs: <><circle cx="9" cy="10" r="3" /><path d="M9 4v2M9 14v2M3 10h2M13 10h2M5 6l1.5 1.5M11.5 12.5 13 14M13 6l-1.5 1.5M6.5 12.5 5 14" /><circle cx="17" cy="17" r="2" /></>,
  "shield-alt": <><path d="M12 3l7 3v6c0 5-3.5 7.5-7 9-3.5-1.5-7-4-7-9V6z" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  box: <><path d="M3 7l9-4 9 4-9 4z" /><path d="M3 7v10l9 4 9-4V7" /><path d="M12 11v10" /></>,
  "code-branch": <><circle cx="7" cy="6" r="2" /><circle cx="7" cy="18" r="2" /><circle cx="17" cy="8" r="2" /><path d="M7 8v8M7 13a8 8 0 0 0 8-3" /></>,
  "network-wired": <><rect x="9" y="3" width="6" height="5" rx="1" /><rect x="3" y="16" width="6" height="5" rx="1" /><rect x="15" y="16" width="6" height="5" rx="1" /><path d="M12 8v4M6 16v-2h12v2" /></>,
  hdd: <><rect x="3" y="6" width="18" height="12" rx="2" /><circle cx="17" cy="12" r="1.3" /><path d="M6 10h6" /></>,
  database: <><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" /></>,
  fire: <><path d="M12 3c1 3 4 4 4 8a4 4 0 0 1-8 0c0-2 1-3 1-3 .5 1.5 1.5 2 1.5 2 .5-3 1.5-5 1.5-7z" /></>,
  lock: <><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></>,
  save: <><path d="M5 3h11l3 3v15H5z" /><path d="M8 3v5h7V3M8 21v-6h8v6" /></>,
  certificate: <><circle cx="10" cy="9" r="5" /><path d="M7 13l-1 7 4-2 4 2-1-7" /><path d="M10 7v2l1.5 1" /></>,
  cube: <><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z" /><path d="M12 3v18M4 7.5l8 4.5 8-4.5" /></>,
  "users-cog": <><circle cx="8" cy="8" r="3" /><path d="M2 20c0-3 3-5 6-5 1 0 2 .2 3 .6" /><circle cx="17" cy="16" r="2.5" /><path d="M17 12v1.5M17 18.5V20M13 16h1.5M19.5 16H21" /></>,
  "id-card": <><rect x="3" y="5" width="18" height="14" rx="2" /><circle cx="8" cy="11" r="2" /><path d="M5 16c0-1.5 1.5-2.5 3-2.5s3 1 3 2.5M14 9h4M14 12h4M14 15h2" /></>,
  bolt: <><path d="M13 3L4 14h6l-1 7 9-11h-6z" /></>,
};

export default function ToolIcon({ name, size = 22 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      {P[name] || P.box}
    </svg>
  );
}
