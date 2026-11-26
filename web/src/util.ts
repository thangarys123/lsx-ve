export function bytes(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || isNaN(n)) return "—";
  const u = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let i = 0;
  let v = Math.abs(n);
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${n < 0 ? "-" : ""}${v.toFixed(i === 0 ? 0 : digits)} ${u[i]}`;
}

export function rate(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const bits = n * 8;
  const u = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"];
  let i = 0;
  let v = bits;
  while (v >= 1000 && i < u.length - 1) {
    v /= 1000;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

export function bps(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${bytes(n)}/s`;
}

export function mib(n: number): string {
  return n >= 1024 ? `${(n / 1024).toFixed(n % 1024 === 0 ? 0 : 1)} GiB` : `${n} MiB`;
}

export function pct(n: number | null | undefined, d = 0): string {
  return n === null || n === undefined || isNaN(n) ? "—" : `${n.toFixed(d)}%`;
}

export function duration(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d} day${d > 1 ? "s" : ""}, ${h} hour${h !== 1 ? "s" : ""}`;
  if (h) return `${h} hour${h !== 1 ? "s" : ""}, ${m} min`;
  return `${m} min`;
}

export function ts(t: number | null | undefined): string {
  if (!t) return "—";
  const d = new Date(t * 1000);
  return d.toLocaleString(undefined, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function timeShort(t: number): string {
  return new Date(t * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function ago(t: number | null | undefined): string {
  if (!t) return "never";
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function cls(...xs: (string | false | null | undefined)[]): string {
  return xs.filter(Boolean).join(" ");
}

export function usageClass(p: number): string {
  return p >= 90 ? "err" : p >= 75 ? "warn" : "";
}

export const STATE_LABEL: Record<string, string> = {
  running: "Powered on",
  shutoff: "Powered off",
  paused: "Paused",
  suspended: "Suspended",
  crashed: "Crashed",
  "shutting-down": "Shutting down",
  nostate: "Unknown",
  stopped: "Stopped",
  frozen: "Frozen",
  starting: "Starting",
  stopping: "Stopping",
  unknown: "Unknown",
};

export const OS_LABEL: Record<string, string> = {
  linux: "Linux",
  windows: "Microsoft Windows",
  bsd: "BSD",
  other: "Other",
};
