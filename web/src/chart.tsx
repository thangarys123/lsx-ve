// Lightweight SVG time-series chart (no external chart library).
import { useEffect, useMemo, useRef, useState } from "react";

/** Width of an element in CSS pixels, kept current with a ResizeObserver. */
function useWidth<T extends HTMLElement>(fallback = 600): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => { const cw = Math.floor(el.clientWidth); if (cw > 0) setW(cw); };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function axisTime(t: number, span: number): string {
  return new Date(t * 1000).toLocaleTimeString(undefined, span < 600
    ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
}

export interface Series {
  label: string;
  color: string;
  values: number[];
}

export function TimeChart({ times, series, height = 150, max, fmt, title, now }: {
  times: number[]; series: Series[]; height?: number; max?: number; fmt: (n: number) => string; title?: string; now?: string;
}) {
  const [box, W] = useWidth<HTMLDivElement>();
  const H = height;
  const padL = 46, padR = 8, padT = 8, padB = 18;
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const top = useMemo(() => {
    if (max !== undefined && max > 0 && isFinite(max)) return max;
    const m = Math.max(1, ...series.flatMap((s) => s.values));
    const mag = Math.pow(10, Math.floor(Math.log10(m)));
    return Math.ceil(m / mag) * mag;
  }, [series, max]);
  const n = times.length;
  const x = (i: number) => padL + (n <= 1 ? 0 : (i / (n - 1)) * (W - padL - padR));
  const y = (v: number) => padT + (1 - Math.min(v || 0, top) / top) * (H - padT - padB);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * top);
  const span = n > 1 ? times[n - 1] - times[0] : 0;
  const nTicks = Math.max(2, Math.min(8, Math.floor((W - padL - padR) / 110) + 1));
  const xticks = n > 1 ? Array.from(new Set(Array.from({ length: nTicks }, (_, k) => Math.round((k * (n - 1)) / (nTicks - 1))))) : [];
  const onMove = (e: React.MouseEvent) => {
    const r = ref.current?.getBoundingClientRect();
    if (!r || n < 2) return;
    const px = e.clientX - r.left;
    const i = Math.round(((px - padL) / (W - padL - padR)) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  };
  return (
    <div className="chart" ref={box}>
      <div className="legend">
        {title && <span style={{ color: "var(--text)", fontWeight: 600, marginRight: "auto" }}>{title}</span>}
        {series.map((s) => (
          <span key={s.label}><i style={{ background: s.color }} />{s.label}</span>
        ))}
        {now && <span className="mono" style={{ color: "var(--text)" }}>{now}</span>}
      </div>
      <svg ref={ref} width={W} height={H} viewBox={`0 0 ${W} ${H}`} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={padL} x2={W - padR} y1={y(t)} y2={y(t)} stroke="#1d2a40" strokeWidth={1} vectorEffect="non-scaling-stroke" />
            <text x={padL - 6} y={y(t) + 3} textAnchor="end" fontSize={9.5} fill="#7d8fa9">{fmt(t)}</text>
          </g>
        ))}
        {xticks.map((i) => (
          <text key={i} x={x(i)} y={H - 4} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"} fontSize={9.5} fill="#7d8fa9">{axisTime(times[i], span)}</text>
        ))}
        {n > 1 && series.map((s) => {
          const pts = s.values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
          return (
            <g key={s.label}>
              <polygon points={`${x(0)},${y(0)} ${pts} ${x(n - 1)},${y(0)}`} fill={s.color} opacity={0.12} />
              <polyline points={pts} fill="none" stroke={s.color} strokeWidth={1.6} vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
            </g>
          );
        })}
        {hover !== null && n > 1 && <line x1={x(hover)} x2={x(hover)} y1={padT} y2={H - padB} stroke="#5d7395" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />}
        {n <= 1 && <text x={W / 2} y={H / 2} textAnchor="middle" fill="#7d8fa9" fontSize={11}>Collecting data…</text>}
      </svg>
      {hover !== null && n > 1 && (
        <div className="tip" style={x(hover) > W * 0.6 ? { right: W - x(hover) + 8, top: 24 } : { left: x(hover) + 8, top: 24 }}>
          <div className="muted">{new Date(times[hover] * 1000).toLocaleTimeString()}</div>
          {series.map((s) => (
            <div key={s.label}><span style={{ color: s.color }}>■</span> {s.label}: {fmt(s.values[hover] ?? 0)}</div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Spark({ values, color = "var(--chart-1)", max = 100, width = 90, height = 22 }: { values: number[]; color?: string; max?: number; width?: number; height?: number }) {
  if (values.length < 2) return <svg width={width} height={height} />;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * width},${height - (Math.min(v, max) / max) * (height - 2) - 1}`).join(" ");
  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <polyline points={pts} fill="none" stroke={color} strokeWidth={1.4} />
    </svg>
  );
}
