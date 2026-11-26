// LSX-VE icon set (original line icons, 24x24 grid, currentColor).
import type { CSSProperties } from "react";

const P: Record<string, string> = {
  host: "M3 5h18v5H3zM3 14h18v5H3zM7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6",
  vm: "M3 4h18v12H3zM8 20h8M12 16v4M7 8l3 2.5L7 13M12 13h4",
  vms: "M2 6h14v10H2zM6 20h6M8 3h14v10M9 16v4",
  container: "M3 7l9-4 9 4v10l-9 4-9-4zM3 7l9 4 9-4M12 11v10M7.5 5l9 4",
  containers: "M2 9l6-3 6 3v7l-6 3-6-3zM2 9l6 3 6-3M8 12v7M10 4l6-3 6 3v7l-4 2",
  freeze: "M12 2v20M4 6l16 12M20 6L4 18M9 3l3 3 3-3M9 21l3-3 3 3",
  storage: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  network: "M12 3v6M5 21v-4h14v4M12 9v8M9 3h6v6H9zM2 21h6M16 21h6",
  monitor: "M3 3v18h18M7 15l4-5 3 3 6-7",
  manage: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z",
  power: "M12 2v9M6.3 5.6a8 8 0 1 0 11.4 0",
  play: "M7 4l13 8-13 8z",
  stop: "M6 6h12v12H6z",
  pause: "M7 5h3v14H7zM14 5h3v14h-3z",
  restart: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5",
  console: "M3 4h18v16H3zM7 9l3 3-3 3M13 15h4",
  plus: "M12 5v14M5 12h14",
  trash: "M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3",
  edit: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4",
  refresh: "M20 11a8 8 0 0 0-14.7-4.3L4 8M4 4v4h4M4 13a8 8 0 0 0 14.7 4.3L20 16M20 20v-4h-4",
  snapshot: "M4 8h3l2-3h6l2 3h3v11H4zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  clone: "M8 8h12v12H8zM4 16V4h12",
  template: "M4 4h16v5H4zM4 13h7v7H4zM15 13h5v7h-5z",
  disk: "M4 5h16v14H4zM8 15h.01M12 15h4",
  nic: "M4 8h16v9H4zM8 17v3M12 17v3M16 17v3M8 11h8",
  cpu: "M7 7h10v10H7zM10 10h4v4h-4zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4",
  memory: "M3 8h18v8H3zM7 8v8M11 8v8M15 8v8M5 16v3M19 16v3",
  iso: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  upload: "M12 16V4M7 9l5-5 5 5M4 20h16",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  users: "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21a7 7 0 0 1 14 0M16 3.5a4 4 0 0 1 0 7.5M18 14a6 6 0 0 1 4 7",
  shield: "M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z",
  cert: "M4 4h16v12H4zM8 8h8M8 12h4M15 14a3 3 0 1 0 0 .01M13.5 16.5L13 21l2-1 2 1-.5-4.5",
  fire: "M12 21c4 0 7-2.7 7-6.5 0-3.5-2.5-5.5-4-8-.5 2-1.5 3-3 3.5.5-3-1-6-3-7 0 4-4 6-4 11.5C5 18.3 8 21 12 21z",
  logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10",
  right: "M9 6l6 6-6 6",
  down: "M6 9l6 6 6-6",
  close: "M6 6l12 12M18 6L6 18",
  check: "M5 12l4.5 4.5L19 7",
  warn: "M12 3l10 18H2zM12 10v5M12 18h.01",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v6M12 7.5h.01",
  error: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9 9l6 6M15 9l-6 6",
  task: "M9 6h11M9 12h11M9 18h11M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2",
  event: "M4 5h16v15H4zM4 10h16M9 3v4M15 3v4",
  key: "M15 9a4 4 0 1 1-1.2 2.8L4 21H2v-3l9.2-9.2A4 4 0 0 1 15 9zM16 8h.01",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  globe: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3c2.5 2.5 3.8 5.5 3.8 9s-1.3 6.5-3.8 9c-2.5-2.5-3.8-5.5-3.8-9S9.5 5.5 12 3z",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  service: "M4 6h16M4 12h16M4 18h16M8 4v4M16 10v4M11 16v4",
  log: "M6 3h9l5 5v13H6zM14 3v6h6M9 13h7M9 17h7",
  bridge: "M3 17h18M6 17V9M18 17V9M3 9c3 0 6-3 9-3s6 3 9 3M10 17v-5M14 17v-5",
  vlan: "M4 4h6v6H4zM14 14h6v6h-6zM10 7h4v10",
  bond: "M7 7a4 4 0 0 0 0 10M17 7a4 4 0 0 1 0 10M8 12h8",
  folder: "M3 6h6l2 2h10v11H3z",
  ssh: "M3 5h18v14H3zM7 10l2 2-2 2M12 14h3",
  health: "M3 12h4l2-5 4 10 2-5h6",
  ctrlaltdel: "M3 7h6v6H3zM10.5 7h5v6h-5zM17 7h4v6h-4zM6 17h12",
  fullscreen: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  thermo: "M10 14V5a2 2 0 1 1 4 0v9a4 4 0 1 1-4 0z",
};

export function Icon({ name, size = 16, style, className }: { name: string; size?: number; style?: CSSProperties; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7}
      strokeLinecap="round" strokeLinejoin="round" style={style} className={className} aria-hidden="true">
      <path d={P[name] ?? P.info} />
    </svg>
  );
}

export function Logo({ size = 26 }: { size?: number }) {
  return (
    <svg className="logo" width={size} height={size} viewBox="0 0 64 64" aria-label="LSX-VE">
      <defs>
        <linearGradient id="lsxg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#4c97ff" />
          <stop offset="1" stopColor="#1f5fc8" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="12" fill="#0f1b2e" stroke="#2b4670" />
      <path d="M13 16h7v24h13v7H13z" fill="url(#lsxg)" />
      <path d="M38 16h6.5l5 9.4 5-9.4H61l-8.2 15.5L61 47h-6.5l-5-9.4-5 9.4H38l8.2-15.5z" fill="#e6edf7" />
    </svg>
  );
}
