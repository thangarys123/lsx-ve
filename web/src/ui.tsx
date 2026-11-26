// Shared UI building blocks.
import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "./api";
import { Icon } from "./icons";
import { cls, STATE_LABEL } from "./util";

/* ------------------------------------------------------------------ data loading */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const reload = useCallback(() => {
    const my = ++seq.current;
    setLoading(true);
    fn()
      .then((d) => {
        if (my === seq.current) {
          setData(d);
          setError(null);
        }
      })
      .catch((e) => {
        if (my === seq.current) setError(e instanceof ApiError ? e : new ApiError(0, "error", String(e)));
      })
      .finally(() => my === seq.current && setLoading(false));
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}

/* ------------------------------------------------------------------ toasts */
interface Toast {
  id: number;
  kind: "info" | "success" | "error" | "warning";
  title: string;
  detail?: string;
}
const ToastCtx = createContext<(t: Omit<Toast, "id">) => void>(() => {});
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs.slice(-4), { ...t, id }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), t.kind === "error" ? 9000 : 5000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status">
        {items.map((t) => (
          <div key={t.id} className={cls("toast", t.kind)} onClick={() => setItems((xs) => xs.filter((x) => x.id !== t.id))}>
            <div className="t">{t.title}</div>
            {t.detail && <div className="d">{t.detail}</div>}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
export const useToast = () => useContext(ToastCtx);

/** Run an action, toast failures, return success. */
export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = useCallback(
    async <T,>(fn: () => Promise<T>, success?: string): Promise<T | undefined> => {
      setBusy(true);
      try {
        const r = await fn();
        if (success) toast({ kind: "success", title: success });
        return r;
      } catch (e: any) {
        toast({ kind: "error", title: "Operation failed", detail: e?.message ?? String(e) });
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [toast],
  );
  return { run, busy };
}

/* ------------------------------------------------------------------ layout pieces */
export function Panel({ title, right, children, flush, icon }: { title?: ReactNode; right?: ReactNode; children: ReactNode; flush?: boolean; icon?: string }) {
  return (
    <section className="panel">
      {title !== undefined && (
        <div className="ph">
          {icon && <Icon name={icon} size={14} />}
          {title}
          {right && <div className="right">{right}</div>}
        </div>
      )}
      <div className={cls("pb", flush && "flush")}>{children}</div>
    </section>
  );
}

export function Tabs({ tabs, value, onChange }: { tabs: [string, string][]; value: string; onChange: (k: string) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map(([k, label]) => (
        <button key={k} role="tab" aria-selected={value === k} className={cls(value === k && "active")} onClick={() => onChange(k)}>
          {label}
        </button>
      ))}
    </div>
  );
}

export function Btn({ icon, children, onClick, disabled, kind, title, size, type }: {
  icon?: string; children?: ReactNode; onClick?: () => void; disabled?: boolean;
  kind?: "primary" | "danger" | "outline" | "danger solid"; title?: string; size?: "sm"; type?: "submit" | "button";
}) {
  return (
    <button type={type ?? "button"} className={cls("btn", kind, size, !children && "icon")} onClick={onClick} disabled={disabled} title={title}>
      {icon && <Icon name={icon} size={15} />}
      {children}
    </button>
  );
}

export function Dropdown({ label, icon, items, align, disabled }: {
  label: ReactNode; icon?: string; disabled?: boolean; align?: "right";
  items: ({ label: string; icon?: string; onClick: () => void; disabled?: boolean; danger?: boolean } | "sep")[];
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);
  return (
    <div className="dropdown" ref={ref}>
      <button type="button" className="btn" disabled={disabled} onClick={() => setOpen(!open)}>
        {icon && <Icon name={icon} size={15} />}
        {label}
        <Icon name="down" size={12} />
      </button>
      {open && (
        <div className={cls("menu", align)}>
          {items.map((it, i) =>
            it === "sep" ? (
              <hr key={i} />
            ) : (
              <button key={i} disabled={it.disabled} className={cls(it.danger && "danger")} onClick={() => { setOpen(false); it.onClick(); }}>
                {it.icon && <Icon name={it.icon} size={14} />}
                {it.label}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  );
}

export function Modal({ title, onClose, children, footer, size, icon }: {
  title: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; size?: "wide" | "mid"; icon?: string;
}) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={cls("modal", size)} role="dialog" aria-modal="true">
        <div className="mh">
          {icon && <Icon name={icon} />}
          {title}
          <button className="btn icon x" onClick={onClose} aria-label="Close">
            <Icon name="close" />
          </button>
        </div>
        <div className="mb">{children}</div>
        {footer && <div className="mf">{footer}</div>}
      </div>
    </div>
  );
}

/** Confirmation dialog. If `typed` is set the user must type that exact text. */
export function Confirm({ title, message, confirmLabel, danger, typed, onConfirm, onClose, children }: {
  title: string; message: ReactNode; confirmLabel?: string; danger?: boolean; typed?: string;
  onConfirm: () => Promise<unknown> | void; onClose: () => void; children?: ReactNode;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const ok = !typed || text === typed;
  return (
    <Modal title={title} onClose={onClose} icon={danger ? "warn" : "info"}
      footer={<>
        <Btn onClick={onClose}>Cancel</Btn>
        <Btn kind={danger ? "danger solid" : "primary"} disabled={!ok || busy} onClick={async () => {
          setBusy(true);
          try { await onConfirm(); onClose(); } finally { setBusy(false); }
        }}>{confirmLabel ?? "OK"}</Btn>
      </>}>
      <div className="form">
        <div>{message}</div>
        {children}
        {typed && (
          <div className="field">
            <label>Type <code>{typed}</code> to confirm</label>
            <input type="text" autoFocus value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
          </div>
        )}
      </div>
    </Modal>
  );
}

export function Field({ label, help, error, children }: { label: ReactNode; help?: ReactNode; error?: string | null; children: ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {error ? <div className="err">{error}</div> : help ? <div className="help">{help}</div> : null}
    </div>
  );
}

export function Meter({ label, used, total, fmt, icon }: { label: string; used: number; total: number; fmt: (n: number) => string; icon?: string }) {
  const p = total > 0 ? Math.min(100, (used / total) * 100) : 0;
  return (
    <div className="meter">
      <div className="top">
        <span>{icon && <Icon name={icon} size={12} style={{ verticalAlign: -2, marginRight: 4 }} />}{label}</span>
        <span>{p.toFixed(0)}%</span>
      </div>
      <div className={cls("bar", p >= 90 ? "err" : p >= 75 ? "warn" : "")}>
        <i style={{ width: `${p}%` }} />
      </div>
      <div className="val">
        <span>Used {fmt(used)}</span>
        <span className="muted">Free {fmt(Math.max(0, total - used))}</span>
      </div>
    </div>
  );
}

export function VmState({ state }: { state: string }) {
  return <span className={cls("status", state)}>{STATE_LABEL[state] ?? state}</span>;
}

export function TaskState({ state }: { state: string }) {
  const label = { queued: "Queued", running: "Running", success: "Completed", error: "Failed" }[state] ?? state;
  return <span className={cls("status", state === "running" ? "running-task" : state)}>{label}</span>;
}

export function ErrorBox({ error, onRetry }: { error: ApiError | null; onRetry?: () => void }) {
  if (!error) return null;
  return (
    <div className="alert err">
      <Icon name="error" />
      <div className="grow">{error.message}</div>
      {onRetry && <Btn size="sm" icon="refresh" onClick={onRetry}>Retry</Btn>}
    </div>
  );
}

export function Loading() {
  return (
    <div className="empty-state">
      <Icon name="refresh" className="spin" />
    </div>
  );
}

/* ------------------------------------------------------------------ data table */
export interface Col<T> {
  key: string;
  label: ReactNode;
  render?: (row: T) => ReactNode;
  sort?: (row: T) => string | number;
  num?: boolean;
  width?: number | string;
}

export function DataTable<T>({ cols, rows, rowKey, selected, onSelect, onOpen, empty, multi, filter }: {
  cols: Col<T>[]; rows: T[]; rowKey: (r: T) => string; selected?: string[]; onSelect?: (keys: string[]) => void;
  onOpen?: (r: T) => void; empty?: ReactNode; multi?: boolean; filter?: string;
}) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [asc, setAsc] = useState(true);
  const sorted = useMemo(() => {
    let out = rows;
    if (filter) {
      const f = filter.toLowerCase();
      out = out.filter((r) => JSON.stringify(r).toLowerCase().includes(f));
    }
    const c = cols.find((x) => x.key === sortKey);
    if (c?.sort) {
      const s = c.sort;
      out = [...out].sort((a, b) => {
        const va = s(a), vb = s(b);
        return (va < vb ? -1 : va > vb ? 1 : 0) * (asc ? 1 : -1);
      });
    }
    return out;
  }, [rows, cols, sortKey, asc, filter]);
  const sel = new Set(selected ?? []);
  const toggle = (k: string, additive: boolean) => {
    if (!onSelect) return;
    if (multi && additive) {
      const n = new Set(sel);
      n.has(k) ? n.delete(k) : n.add(k);
      onSelect([...n]);
    } else onSelect(sel.has(k) && sel.size === 1 ? [] : [k]);
  };
  return (
    <div className="table-wrap">
      <table className="dt">
        <thead>
          <tr>
            {onSelect && multi && (
              <th style={{ width: 28 }}>
                <input type="checkbox" aria-label="Select all" checked={sorted.length > 0 && sorted.every((r) => sel.has(rowKey(r)))}
                  onChange={(e) => onSelect(e.target.checked ? sorted.map(rowKey) : [])} />
              </th>
            )}
            {cols.map((c) => (
              <th key={c.key} className={cls(c.sort && "sortable", c.num && "num")} style={{ width: c.width }}
                onClick={() => { if (!c.sort) return; if (sortKey === c.key) setAsc(!asc); else { setSortKey(c.key); setAsc(true); } }}>
                {c.label}
                {sortKey === c.key && <Icon name={asc ? "down" : "right"} size={10} style={{ marginLeft: 4, transform: asc ? undefined : "rotate(-90deg)" }} />}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 && (
            <tr>
              <td className="empty" colSpan={cols.length + (onSelect && multi ? 1 : 0)}>{empty ?? "No items"}</td>
            </tr>
          )}
          {sorted.map((r) => {
            const k = rowKey(r);
            return (
              <tr key={k} className={cls(sel.has(k) && "sel", (onSelect || onOpen) && "clickable")}
                onClick={(e) => toggle(k, e.ctrlKey || e.metaKey || (e.target as HTMLElement).tagName === "INPUT")}
                onDoubleClick={() => onOpen?.(r)}>
                {onSelect && multi && (
                  <td><input type="checkbox" aria-label="Select row" checked={sel.has(k)} onChange={() => {}} /></td>
                )}
                {cols.map((c) => (
                  <td key={c.key} className={cls(c.num && "num")}>{c.render ? c.render(r) : (r as any)[c.key]}</td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="table-foot">
        <span>{sorted.length} item{sorted.length === 1 ? "" : "s"}</span>
        {selected && selected.length > 0 && <span>{selected.length} selected</span>}
      </div>
    </div>
  );
}

export function Pill<T extends string>({ options, value, onChange }: { options: [T, string][]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="pill-select">
      {options.map(([k, l]) => (
        <button key={k} className={cls(value === k && "on")} onClick={() => onChange(k)}>{l}</button>
      ))}
    </div>
  );
}
