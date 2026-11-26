// Live updates over the authenticated WebSocket (/api/v1/ws): metrics, tasks, events, VM state.
import { useEffect, useSyncExternalStore } from "react";

export interface HostSample {
  ts: number;
  cpu: number;
  mem: number;
  mem_used: number;
  mem_total: number;
  net_rx: number;
  net_tx: number;
  disk_read: number;
  disk_write: number;
  nics: Record<string, { rx: number; tx: number }>;
  load: number[];
}
export interface VmStat {
  cpu_pct: number;
  mem_assigned: number;
  mem_guest_used: number | null;
  net_rx: number;
  net_tx: number;
  disk_read: number;
  disk_write: number;
}
export interface Task {
  id: string;
  kind: string;
  description: string;
  target: string;
  target_name: string;
  username: string;
  state: "queued" | "running" | "success" | "error";
  progress: number;
  message: string;
  created: number;
  started: number | null;
  finished: number | null;
  result?: any;
}
export interface EventRec {
  id: number;
  ts: number;
  severity: "info" | "warning" | "error";
  category: string;
  message: string;
  target: string;
  target_name: string;
  username: string;
}

type Listener = () => void;
type Handler = (type: string, data: any) => void;

class LiveStore {
  connected = false;
  samples: HostSample[] = [];
  vmStats: Record<string, VmStat> = {};
  tasks: Task[] = [];
  vmVersion = 0; // bumps when any VM changes state / config
  ctVersion = 0; // bumps when any container changes
  private listeners = new Set<Listener>();
  private handlers = new Set<Handler>();
  private ws: WebSocket | null = null;
  private retry = 1000;
  private stopped = true;
  private snapshot = { connected: false, samples: this.samples, vmStats: this.vmStats, tasks: this.tasks, vmVersion: 0, ctVersion: 0 };

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }
  stop() {
    this.stopped = true;
    this.ws?.close();
    this.ws = null;
    this.samples = [];
    this.tasks = [];
  }
  seed(samples: HostSample[], tasks: Task[]) {
    this.samples = samples.slice(-1200);
    this.tasks = tasks;
    this.emit();
  }
  private connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/v1/ws`);
    this.ws = ws;
    ws.onopen = () => {
      this.connected = true;
      this.retry = 1000;
      this.emit();
    };
    ws.onclose = () => {
      this.connected = false;
      this.emit();
      if (!this.stopped) {
        setTimeout(() => this.connect(), this.retry);
        this.retry = Math.min(this.retry * 2, 15000);
      }
    };
    ws.onmessage = (ev) => {
      let msg: any;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      this.dispatch(msg.type, msg.data);
    };
  }
  private dispatch(type: string, data: any) {
    if (type === "metrics") {
      if (data.host) this.samples = [...this.samples.slice(-1199), data.host];
      this.vmStats = data.vms || {};
    } else if (type === "task") {
      const others = this.tasks.filter((t) => t.id !== data.id);
      this.tasks = [data, ...others].sort((a, b) => b.created - a.created).slice(0, 200);
      if (data.state === "success" && data.kind.startsWith("vm.")) this.vmVersion++;
      if ((data.state === "success" || data.state === "error") && data.kind.startsWith("ct.")) this.ctVersion++;
    } else if (type === "vm") {
      this.vmVersion++;
    } else if (type === "ct") {
      this.ctVersion++;
    } else if (type === "session_expired") {
      this.stop();
    }
    this.handlers.forEach((h) => h(type, data));
    this.emit();
  }
  private emit() {
    this.snapshot = { connected: this.connected, samples: this.samples, vmStats: this.vmStats, tasks: this.tasks, vmVersion: this.vmVersion, ctVersion: this.ctVersion };
    this.listeners.forEach((l) => l());
  }
  subscribe = (l: Listener) => {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  };
  getSnapshot = () => this.snapshot;
  on(h: Handler) {
    this.handlers.add(h);
    return () => {
      this.handlers.delete(h);
    };
  }
}

export const live = new LiveStore();

export function useLive() {
  return useSyncExternalStore(live.subscribe, live.getSnapshot);
}

export function useLiveEvent(handler: Handler, deps: unknown[] = []) {
  useEffect(() => live.on(handler), deps); // eslint-disable-line react-hooks/exhaustive-deps
}
