import { Fragment, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { TimeChart } from "../chart";
import { Icon } from "../icons";
import { EventRec, useLive, useLiveEvent } from "../live";
import { useSession } from "../session";
import { AlarmsTab } from "./HostAdmin";
import { Btn, DataTable, ErrorBox, Loading, Panel, Pill, Tabs, TaskState, useAsync, VmState } from "../ui";
import { bps, bytes, mib, pct, ts } from "../util";

export function HostMonitor() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "performance";
  const { can } = useSession();
  const tabs: [string, string][] = [["performance", "Performance"], ["vms", "VM utilization"], ["hardware", "Hardware"], ["alarms", "Alarms"]];
  if (can("logs.view")) tabs.push(["events", "Events"]);
  tabs.push(["tasks", "Tasks"]);
  if (can("logs.view")) tabs.push(["logs", "System logs"], ["audit", "Audit log"]);
  return (
    <div className="page">
      <div className="page-head"><h1><Icon name="monitor" size={20} /> Monitor</h1></div>
      <Tabs tabs={tabs} value={tab} onChange={(t) => setParams({ tab: t })} />
      {tab === "performance" && <Performance />}
      {tab === "vms" && <VmUtilization />}
      {tab === "hardware" && <Hardware />}
      {tab === "alarms" && <AlarmsTab />}
      {tab === "events" && <Events />}
      {tab === "tasks" && <Tasks />}
      {tab === "logs" && <Logs />}
      {tab === "audit" && <Audit />}
    </div>
  );
}

function Performance() {
  const { samples } = useLive();
  const [range, setRange] = useState<"300" | "900" | "3600">("900");
  const data = useMemo(() => {
    const cutoff = Date.now() / 1000 - Number(range);
    return samples.filter((s) => s.ts >= cutoff);
  }, [samples, range]);
  const t = data.map((x) => x.ts);
  const nics = useMemo(() => Object.keys(data[data.length - 1]?.nics ?? {}), [data]);
  const last = data[data.length - 1];
  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <span className="muted">Time range</span>
        <Pill options={[["300", "5 min"], ["900", "15 min"], ["3600", "1 hour"]]} value={range} onChange={setRange} />
        <span className="muted small">Real-time, {data.length} samples</span>
      </div>
      <div className="grid c2">
        <Panel title="CPU utilization" icon="cpu" right={<span className="mono">{pct(last?.cpu, 1)}</span>}>
          <TimeChart times={t} series={[{ label: "Usage", color: "var(--chart-1)", values: data.map((x) => x.cpu) }]} max={100} fmt={(n) => `${n.toFixed(0)}%`} height={180} />
        </Panel>
        <Panel title="Memory" icon="memory" right={<span className="mono">{pct(last?.mem, 1)}</span>}>
          <TimeChart times={t} series={[{ label: "Consumed", color: "var(--chart-2)", values: data.map((x) => x.mem_used) }]}
            max={last?.mem_total} fmt={(n) => bytes(n, 0)} height={180} />
        </Panel>
        <Panel title="Network throughput (physical NICs)" icon="nic">
          <TimeChart times={t} series={[
            { label: "Received", color: "var(--chart-1)", values: data.map((x) => x.net_rx) },
            { label: "Transmitted", color: "var(--chart-3)", values: data.map((x) => x.net_tx) }]} fmt={(n) => bytes(n, 0) + "/s"} height={180} />
          {nics.length > 0 && (
            <table className="dt" style={{ marginTop: 8 }}>
              <thead><tr><th>Adapter</th><th className="num">Receive</th><th className="num">Transmit</th></tr></thead>
              <tbody>{nics.map((n) => <tr key={n}><td className="mono">{n}</td><td className="num">{bps(last?.nics[n]?.rx)}</td><td className="num">{bps(last?.nics[n]?.tx)}</td></tr>)}</tbody>
            </table>
          )}
        </Panel>
        <Panel title="Disk throughput" icon="disk">
          <TimeChart times={t} series={[
            { label: "Read", color: "var(--chart-2)", values: data.map((x) => x.disk_read) },
            { label: "Write", color: "var(--chart-4)", values: data.map((x) => x.disk_write) }]} fmt={(n) => bytes(n, 0) + "/s"} height={180} />
        </Panel>
      </div>
    </>
  );
}

function VmUtilization() {
  const { vmStats, vmVersion } = useLive();
  const { data, error, reload } = useAsync(() => api.get<any[]>("/api/v1/vms"), [vmVersion]);
  const nav = useNavigate();
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const rows = data.map((v) => ({ ...v, ...(vmStats[v.uuid] ?? {}) }));
  return (
    <Panel flush>
      <DataTable rowKey={(r: any) => r.uuid} rows={rows} onOpen={(r: any) => nav(`/vms/${r.uuid}`)}
        cols={[
          { key: "name", label: "Virtual machine", sort: (r: any) => r.name, render: (r: any) => <a onClick={() => nav(`/vms/${r.uuid}`)} style={{ cursor: "pointer" }}>{r.name}</a> },
          { key: "state", label: "State", render: (r: any) => <VmState state={r.state} /> },
          { key: "vcpus", label: "vCPUs", num: true, sort: (r: any) => r.vcpus },
          { key: "cpu_pct", label: "CPU usage", num: true, sort: (r: any) => r.cpu_pct ?? -1, render: (r: any) => r.state === "running" ? pct(r.cpu_pct, 1) : "—" },
          { key: "memory_mib", label: "Memory", num: true, sort: (r: any) => r.memory_mib, render: (r: any) => mib(r.memory_mib) },
          { key: "mem_guest_used", label: "Guest memory used", num: true, render: (r: any) => r.mem_guest_used ? bytes(r.mem_guest_used) : "—" },
          { key: "net", label: "Network", num: true, render: (r: any) => r.state === "running" ? `↓${bps(r.net_rx)} ↑${bps(r.net_tx)}` : "—" },
          { key: "disk", label: "Disk", num: true, render: (r: any) => r.state === "running" ? `R ${bps(r.disk_read)} W ${bps(r.disk_write)}` : "—" },
        ]} />
    </Panel>
  );
}

function Hardware() {
  const { data, error, reload } = useAsync(() => api.get("/api/v1/host/hardware"));
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <Loading />;
  return (
    <div className="grid">
      <Panel title="Sensors" icon="thermo" flush>
        <DataTable rowKey={(r: any) => r.chip + r.sensor + r.type} rows={data.sensors}
          empty="This hardware does not expose sensors to the operating system (common on virtual machines)."
          cols={[
            { key: "chip", label: "Device", sort: (r: any) => r.chip },
            { key: "sensor", label: "Sensor", sort: (r: any) => r.sensor },
            { key: "type", label: "Type" },
            { key: "value", label: "Reading", num: true, render: (r: any) => `${r.value} ${r.unit}` },
            { key: "limit", label: "Limit", num: true, render: (r: any) => (r.limit ? `${r.limit} ${r.unit}` : "—") },
            { key: "status", label: "Status", render: (r: any) => <span className={`status ${r.status}`}>{r.status}</span> },
          ]} />
      </Panel>
      <div className="grid c2">
        <Panel title="Platform" icon="host">
          <div className="kv">
            {Object.entries(data.dmi).filter(([, v]) => v).map(([k, v]) => <Fragment key={k}><div>{k.replace(/_/g, " ")}</div><div>{String(v)}</div></Fragment>)}
            <div>KVM</div><div>{data.kvm.dev_kvm ? `available (${data.kvm.module})` : "not available"}{data.kvm.nested ? ", nested enabled" : ""}</div>
            <div>IOMMU</div><div>{data.iommu ? "enabled" : "disabled"}</div>
          </div>
        </Panel>
        <Panel title="CPU" icon="cpu">
          <div className="kv">
            <div>Model</div><div>{data.cpu.model}</div>
            <div>Vendor</div><div>{data.cpu.vendor}</div>
            <div>Topology</div><div>{data.cpu.sockets} socket(s), {data.cpu.cores} cores, {data.cpu.threads} threads</div>
            <div>Frequency</div><div>{data.cpu.mhz ? `${data.cpu.mhz} MHz` : "—"}</div>
            <div>Features</div><div className="inline-list">{data.cpu.features.map((f: string) => <span key={f} className="badge">{f}</span>)}</div>
          </div>
        </Panel>
      </div>
      <Panel title="PCI devices" icon="cpu" flush>
        <DataTable rowKey={(r: any) => r.address} rows={data.pci}
          cols={[
            { key: "address", label: "Address", sort: (r: any) => r.address, render: (r: any) => <span className="mono">{r.address}</span> },
            { key: "class", label: "Class", sort: (r: any) => r.class },
            { key: "ids", label: "Vendor:Device", render: (r: any) => <span className="mono">{r.vendor_id}:{r.device_id}</span> },
            { key: "driver", label: "Driver" },
            { key: "iommu_group", label: "IOMMU group", render: (r: any) => r.iommu_group ?? "—" },
          ]} />
      </Panel>
    </div>
  );
}

function Events() {
  const [sev, setSev] = useState("");
  const [cat, setCat] = useState("");
  const [filter, setFilter] = useState("");
  const { data, error, reload, setData } = useAsync(() => api.get<EventRec[]>(`/api/v1/events?limit=1000${sev ? `&severity=${sev}` : ""}${cat ? `&category=${cat}` : ""}`), [sev, cat]);
  useLiveEvent((type, ev) => {
    if (type === "event" && (!sev || ev.severity === sev) && (!cat || ev.category === cat)) setData((xs) => [ev, ...(xs ?? [])].slice(0, 1000));
  }, [sev, cat]);
  return (
    <>
      <div className="toolbar">
        <select value={sev} onChange={(e) => setSev(e.target.value)} style={{ width: 150 }}>
          <option value="">All severities</option><option value="info">Info</option><option value="warning">Warning</option><option value="error">Error</option>
        </select>
        <select value={cat} onChange={(e) => setCat(e.target.value)} style={{ width: 160 }}>
          <option value="">All categories</option>
          {["vm", "storage", "network", "host", "auth"].map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <input type="search" placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 220 }} />
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <ErrorBox error={error} />
      <Panel flush>
        <DataTable rowKey={(r: EventRec) => String(r.id)} rows={data ?? []} filter={filter}
          cols={[
            { key: "ts", label: "Time", sort: (r) => r.ts, render: (r) => <span className="nowrap">{ts(r.ts)}</span> },
            { key: "severity", label: "Severity", render: (r) => <span className={`status ${r.severity === "info" ? "ok" : r.severity}`}>{r.severity}</span> },
            { key: "category", label: "Category", render: (r) => <span className="badge">{r.category}</span> },
            { key: "message", label: "Description" },
            { key: "target_name", label: "Target" },
            { key: "username", label: "User" },
          ]} />
      </Panel>
    </>
  );
}

function Tasks() {
  const [state, setState] = useState("");
  const { data, reload } = useAsync(() => api.get<any[]>(`/api/v1/tasks?limit=500${state ? `&state=${state}` : ""}`), [state]);
  const { tasks } = useLive();
  useEffect(reload, [tasks.length]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <>
      <div className="toolbar">
        <Pill options={[["", "All"], ["active", "Active"], ["success", "Completed"], ["error", "Failed"]]} value={state} onChange={setState} />
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <Panel flush>
        <DataTable rowKey={(r: any) => r.id} rows={data ?? []}
          cols={[
            { key: "description", label: "Task", sort: (r: any) => r.description },
            { key: "target_name", label: "Target" },
            { key: "username", label: "Initiator" },
            { key: "state", label: "Status", render: (r: any) => <TaskState state={r.state} /> },
            { key: "progress", label: "Progress", num: true, render: (r: any) => `${r.progress}%` },
            { key: "message", label: "Details" },
            { key: "created", label: "Queued", sort: (r: any) => r.created, render: (r: any) => <span className="nowrap">{ts(r.created)}</span> },
            { key: "finished", label: "Completed", render: (r: any) => <span className="nowrap">{ts(r.finished)}</span> },
          ]} />
      </Panel>
    </>
  );
}

function Logs() {
  const [unit, setUnit] = useState("");
  const [prio, setPrio] = useState("");
  const [grep, setGrep] = useState("");
  const [lines, setLines] = useState("300");
  const [q, setQ] = useState("");
  const { data, error, loading, reload } = useAsync(() => api.get<any[]>(`/api/v1/host/logs?${q}`), [q]);
  const apply = () => {
    const p = new URLSearchParams({ lines });
    if (unit) p.set("unit", unit);
    if (prio) p.set("priority", prio);
    if (grep) p.set("grep", grep);
    setQ(p.toString());
  };
  useEffect(apply, []); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <>
      <div className="toolbar">
        <select value={unit} onChange={(e) => setUnit(e.target.value)} style={{ width: 200 }}>
          <option value="">All units</option>
          {["lsxve-api", "lsxve-agent", "lsxve-init", "lsxve-dcui", "virtqemud", "virtnetworkd", "virtstoraged", "NetworkManager", "sshd", "chronyd", "firewalld", "kernel"].map((u) => <option key={u} value={u}>{u}</option>)}
        </select>
        <select value={prio} onChange={(e) => setPrio(e.target.value)} style={{ width: 140 }}>
          <option value="">Any priority</option>
          {["err", "warning", "notice", "info", "debug"].map((u) => <option key={u} value={u}>{u} and above</option>)}
        </select>
        <input type="search" placeholder="Search text" value={grep} onChange={(e) => setGrep(e.target.value)} style={{ width: 220 }} onKeyDown={(e) => e.key === "Enter" && apply()} />
        <select value={lines} onChange={(e) => setLines(e.target.value)} style={{ width: 110 }}>
          {["100", "300", "1000", "2000"].map((n) => <option key={n} value={n}>{n} lines</option>)}
        </select>
        <Btn icon="search" kind="outline" onClick={apply}>Apply</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={reload} disabled={loading}>Refresh</Btn>
      </div>
      <ErrorBox error={error} />
      <Panel flush>
        <div className="table-wrap" style={{ maxHeight: "calc(100vh - 330px)" }}>
          <table className="dt">
            <thead><tr><th>Time</th><th>Unit</th><th>Message</th></tr></thead>
            <tbody>
              {(data ?? []).slice().reverse().map((l, i) => (
                <tr key={i} className={`prio-${l.priority}`}>
                  <td className="nowrap log-line">{ts(l.ts)}</td>
                  <td className="nowrap log-line">{l.ident || l.unit}</td>
                  <td className="log-line" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{l.message}</td>
                </tr>
              ))}
              {data && data.length === 0 && <tr><td colSpan={3} className="empty">No matching log entries</td></tr>}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}

function Audit() {
  const [filter, setFilter] = useState("");
  const { data, error, reload } = useAsync(() => api.get<any[]>("/api/v1/audit?limit=2000"));
  return (
    <>
      <div className="toolbar">
        <input type="search" placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 260 }} />
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <ErrorBox error={error} />
      <Panel flush>
        <DataTable rowKey={(r: any) => String(r.id)} rows={data ?? []} filter={filter}
          cols={[
            { key: "ts", label: "Time", sort: (r: any) => r.ts, render: (r: any) => <span className="nowrap">{ts(r.ts)}</span> },
            { key: "username", label: "User", sort: (r: any) => r.username },
            { key: "ip", label: "Source", render: (r: any) => <span className="mono">{r.ip}</span> },
            { key: "action", label: "Action", sort: (r: any) => r.action, render: (r: any) => <span className="mono">{r.action}</span> },
            { key: "target", label: "Target" },
            { key: "outcome", label: "Outcome", render: (r: any) => <span className={`status ${r.outcome === "success" ? "ok" : r.outcome === "failure" || r.outcome === "locked" ? "error" : "warning"}`}>{r.outcome}</span> },
            { key: "details", label: "Details", render: (r: any) => <span className="mono small muted">{Object.keys(r.details ?? {}).length ? JSON.stringify(r.details) : ""}</span> },
          ]} />
      </Panel>
    </>
  );
}
