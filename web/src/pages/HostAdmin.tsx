// Host administration pages: alarms, maintenance mode, VM autostart, syslog / SNMP, power policy,
// lockdown mode, account lockout and support bundles (ESXi Host > Monitor / Manage equivalents).
import { ReactNode, useEffect, useState } from "react";
import { api, enc } from "../api";
import { Icon } from "../icons";
import { useLive, useLiveEvent } from "../live";
import { useSession } from "../session";
import { Btn, Confirm, DataTable, ErrorBox, Field, Loading, Panel, useAction, useAsync } from "../ui";
import { ago, bytes, ts } from "../util";

/* ------------------------------------------------------------------ shared live state */
export interface Alarm {
  id: string; definition: string; name: string; object: string; severity: "critical" | "warning" | "info";
  message: string; target: string; since: number; acknowledged: boolean; acknowledged_by?: string;
}

/** Active alarms + maintenance flag, kept current from the WebSocket. */
export function useHostState() {
  const { can } = useSession();
  const [alarms, setAlarms] = useState<Alarm[]>([]);
  const [maint, setMaint] = useState(false);
  const load = () => {
    if (!can("host.view")) return;
    api.get("/api/v1/alarms").then((r) => setAlarms(r?.alarms ?? [])).catch(() => {});
    api.get("/api/v1/host/maintenance").then((r) => setMaint(!!r?.enabled)).catch(() => {});
  };
  useEffect(load, []); // eslint-disable-line react-hooks/exhaustive-deps
  useLiveEvent((type, data) => {
    if (type === "alarm") setAlarms(data?.alarms ?? []);
    if (type === "maintenance") setMaint(!!data.enabled);
  }, []);
  const { connected } = useLive();
  useEffect(() => { if (connected) load(); }, [connected]); // eslint-disable-line react-hooks/exhaustive-deps
  return { alarms, maint };
}

const SEV_CLASS: Record<string, string> = { critical: "error", warning: "warning", info: "ok" };

export function AlarmBadge({ alarms, onClick }: { alarms: Alarm[]; onClick: () => void }) {
  const open = alarms.filter((a) => !a.acknowledged && a.severity !== "info");
  const crit = open.filter((a) => a.severity === "critical").length;
  if (!open.length) return null;
  return (
    <button className="host-chip" onClick={onClick} title="Triggered alarms" style={{ cursor: "pointer" }}>
      <Icon name="warn" size={14} style={{ color: crit ? "var(--err)" : "var(--warn)" }} />
      {crit > 0 && <span className="badge red">{crit} critical</span>}
      {open.length - crit > 0 && <span className="badge amber">{open.length - crit} warning</span>}
    </button>
  );
}

export function MaintenanceBanner({ on }: { on: boolean }) {
  const { can } = useSession();
  const { run, busy } = useAction();
  if (!on) return null;
  return (
    <div className="alert warn" style={{ margin: "10px 14px 0" }}>
      <Icon name="warn" />
      <div className="grow"><b>The host is in maintenance mode.</b> Virtual machines and containers cannot be powered on and VM autostart is skipped at boot.</div>
      {can("host.config") && <Btn size="sm" disabled={busy} onClick={() => run(() => api.put("/api/v1/host/maintenance", { enabled: false }), "Exited maintenance mode")}>Exit maintenance mode</Btn>}
    </div>
  );
}

/** "Enter maintenance mode" dialog (ESXi: Host > Actions > Enter maintenance mode). */
export function MaintenanceDialog({ onClose }: { onClose: () => void }) {
  const { data, error } = useAsync(() => api.get("/api/v1/host/maintenance"));
  const [shut, setShut] = useState(false);
  const { run } = useAction();
  const running: any[] = data?.running_vms ?? [];
  if (data?.enabled) {
    return <Confirm title="Exit maintenance mode" message="Allow virtual machines to be powered on again?" confirmLabel="Exit maintenance mode"
      onConfirm={() => run(() => api.put("/api/v1/host/maintenance", { enabled: false }), "Exited maintenance mode")} onClose={onClose} />;
  }
  return (
    <Confirm title="Enter maintenance mode" confirmLabel="Enter maintenance mode" onClose={onClose}
      onConfirm={() => run(() => api.put("/api/v1/host/maintenance", { enabled: true, shutdown_vms: shut }))}
      message={<>
        <p>In maintenance mode no virtual machine or container can be powered on and VM autostart is skipped. Use it before
          firmware upgrades, hardware changes or disk work. There is no live migration on a standalone host: running VMs must be shut down.</p>
        <ErrorBox error={error} />
        {running.length > 0 ? (
          <div className="alert warn"><Icon name="warn" /><div className="grow">
            <b>{running.length} virtual machine(s) are powered on:</b> {running.map((v) => v.name).join(", ")}
            <label className="check" style={{ marginTop: 6 }}><input type="checkbox" checked={shut} onChange={(e) => setShut(e.target.checked)} /> Shut down the guests (power off after 3 minutes if they do not respond)</label>
          </div></div>
        ) : data && <div className="small muted">No virtual machines are powered on.</div>}
      </>} />
  );
}

/* ------------------------------------------------------------------ Monitor > Alarms */
export function AlarmsTab() {
  const { can } = useSession();
  const { data, error, reload, setData } = useAsync(() => api.get("/api/v1/alarms"));
  const { run, busy } = useAction();
  const [sel, setSel] = useState<string[]>([]);
  const [st, setSt] = useState<any>(null);
  useEffect(() => { if (data && !st) setSt(data.settings); }, [data]); // eslint-disable-line react-hooks/exhaustive-deps
  useLiveEvent((type, d) => { if (type === "alarm") setData((x: any) => ({ ...(x ?? {}), alarms: d.alarms })); }, []);
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data || !st) return <Loading />;
  const cur = data.alarms.find((a: Alarm) => a.id === sel[0]);
  const ro = !can("host.config");
  const num = (k: string, f: string, label: string, unit = "%", max = 100) => (
    <Field label={label}>
      <span className="unit-input"><input type="number" min={1} max={max} value={st[k][f]} disabled={ro || !st[k].enabled}
        onChange={(e) => setSt({ ...st, [k]: { ...st[k], [f]: Number(e.target.value) } })} /><span>{unit}</span></span>
    </Field>
  );
  const def = (k: string, title: string, fields: ReactNode) => (
    <Panel title={<label className="check" style={{ margin: 0 }}><input type="checkbox" checked={st[k].enabled} disabled={ro}
      onChange={(e) => setSt({ ...st, [k]: { ...st[k], enabled: e.target.checked } })} /> {title}</label>}>
      <div className="grid c3">{fields}</div>
    </Panel>
  );
  return (
    <>
      <div className="toolbar">
        <Btn icon="check" disabled={ro || !cur || cur.acknowledged || busy} onClick={() => run(() => api.post(`/api/v1/alarms/${enc(cur.id)}/ack`), "Alarm acknowledged").then(reload)}>Acknowledge</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <Panel flush title="Triggered alarms" icon="warn">
        <DataTable rowKey={(r: Alarm) => r.id} rows={data.alarms} selected={sel} onSelect={setSel} empty="No alarms are triggered"
          cols={[
            { key: "severity", label: "Status", render: (r: Alarm) => <span className={`status ${SEV_CLASS[r.severity]}`}>{r.severity === "info" ? "Info" : r.severity === "critical" ? "Critical" : "Warning"}</span> },
            { key: "name", label: "Alarm" },
            { key: "message", label: "Description" },
            { key: "since", label: "Triggered", sort: (r: Alarm) => r.since, render: (r: Alarm) => <span className="nowrap" title={ts(r.since)}>{ago(r.since)}</span> },
            { key: "acknowledged", label: "Acknowledged", render: (r: Alarm) => r.acknowledged ? `Yes (${r.acknowledged_by})` : "No" },
          ]} />
      </Panel>
      <div className="page-head" style={{ marginTop: 14 }}><h2 style={{ fontSize: 15 }}>Alarm definitions</h2>
        <div className="grow" />
        {!ro && <Btn kind="primary" disabled={busy} onClick={() => run(() => api.put("/api/v1/alarms/settings", st), "Alarm definitions saved").then((r: any) => r && (setSt(r.settings), reload()))}>Save definitions</Btn>}
      </div>
      <div className="small muted" style={{ marginBottom: 8 }}>Alarms are evaluated every 30 seconds. A triggered or cleared alarm is written to the event log and to the system log (forwarded by remote syslog). Always-on alarms: datastore inaccessible, VM crashed, hypervisor unavailable, maintenance mode.</div>
      <div className="grid c2">
        {def("host_cpu", "Host CPU usage", <>{num("host_cpu", "warning", "Warning above")}{num("host_cpu", "critical", "Critical above")}{num("host_cpu", "minutes", "Sustained for", "min", 60)}</>)}
        {def("host_memory", "Host memory usage", <>{num("host_memory", "warning", "Warning above")}{num("host_memory", "critical", "Critical above")}</>)}
        {def("datastore_usage", "Datastore usage on disk", <>{num("datastore_usage", "warning", "Warning above")}{num("datastore_usage", "critical", "Critical above")}</>)}
        {def("vm_cpu", "Virtual machine CPU usage", <>{num("vm_cpu", "warning", "Warning above")}{num("vm_cpu", "critical", "Critical above")}{num("vm_cpu", "minutes", "Sustained for", "min", 60)}</>)}
        {def("uplink", "Network uplink connectivity", <div className="small muted">Critical when all uplinks of a virtual switch are down, warning when redundancy is lost.</div>)}
        {def("certificate", "Host certificate expiration", <>{num("certificate", "warning_days", "Warn days before expiry", "days", 365)}</>)}
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ Manage > Autostart */
const STOP_LABEL: Record<string, string> = { shutdown: "Shut down guest", poweroff: "Power off", suspend: "Suspend" };

export function AutostartTab() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get("/api/v1/host/autostart"));
  const { run, busy } = useAction();
  const [s, setS] = useState<any>(null);
  const [vms, setVms] = useState<any[]>([]);
  useEffect(() => { if (data) { setS(data.settings); setVms(data.vms); } }, [data]);
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data || !s) return <Loading />;
  const ro = !can("host.config");
  const enabled = vms.filter((v) => v.enabled);
  const manual = vms.filter((v) => !v.enabled);
  const move = (i: number, d: number) => {
    const xs = [...enabled];
    const j = i + d;
    if (j < 0 || j >= xs.length) return;
    [xs[i], xs[j]] = [xs[j], xs[i]];
    setVms([...xs.map((v, k) => ({ ...v, order: k + 1 })), ...manual]);
  };
  const toggle = (v: any, on: boolean) => {
    const rest = vms.filter((x) => x.uuid !== v.uuid);
    const en = rest.filter((x) => x.enabled);
    setVms(on ? [...en, { ...v, enabled: true, order: en.length + 1 }, ...rest.filter((x) => !x.enabled)]
              : [...en.map((x, k) => ({ ...x, order: k + 1 })), { ...v, enabled: false, order: 0 }, ...rest.filter((x) => !x.enabled)]);
  };
  const save = () => run(() => api.put("/api/v1/host/autostart", {
    ...s, vms: vms.map((v) => ({ uuid: v.uuid, enabled: v.enabled, order: v.enabled ? v.order : 0 })),
  }), "Autostart configuration saved").then(reload);
  return (
    <>
      <div className="grid c2">
        <Panel title="Autostart defaults" icon="power">
          <div className="form">
            <label className="check"><input type="checkbox" checked={s.enabled} disabled={ro} onChange={(e) => setS({ ...s, enabled: e.target.checked })} /> Start and stop virtual machines with the host</label>
            <div className="grid c2">
              <Field label="Start delay" help="Wait before powering on the next VM"><span className="unit-input"><input type="number" min={0} max={3600} value={s.start_delay} disabled={ro} onChange={(e) => setS({ ...s, start_delay: Number(e.target.value) })} /><span>s</span></span></Field>
              <Field label="Stop delay" help="Maximum wait for each guest to stop"><span className="unit-input"><input type="number" min={0} max={3600} value={s.stop_delay} disabled={ro} onChange={(e) => setS({ ...s, stop_delay: Number(e.target.value) })} /><span>s</span></span></Field>
            </div>
            <Field label="Stop action">
              <select value={s.stop_action} disabled={ro} onChange={(e) => setS({ ...s, stop_action: e.target.value })}>
                {data.stop_actions.map((a: string) => <option key={a} value={a}>{STOP_LABEL[a] ?? a}</option>)}
              </select>
            </Field>
            <label className="check"><input type="checkbox" checked={s.wait_heartbeat} disabled={ro} onChange={(e) => setS({ ...s, wait_heartbeat: e.target.checked })} /> Continue immediately when the guest agent (VMware Tools equivalent) is running</label>
          </div>
        </Panel>
        <Panel title="How it works" icon="info">
          <div className="small muted">
            At host boot <span className="mono">lsxve-vm-autostart</span> powers on the VMs below in order, waiting the start delay after each.
            At host shutdown the VMs are stopped in reverse order with the stop action, before the remaining guests are handled by libvirt-guests.
            Nothing is started while the host is in maintenance mode. Per-VM delays and stop action can be overridden in the VM's
            <i> Edit settings &gt; VM Options</i>.
          </div>
        </Panel>
      </div>
      <Panel flush title="Virtual machines" icon="vms" right={!ro && <Btn kind="primary" size="sm" disabled={busy} onClick={save}>Save</Btn>}>
        <table className="dt">
          <thead><tr><th style={{ width: 70 }}>Order</th><th>Virtual machine</th><th>State</th><th>Start delay</th><th>Stop action</th><th>Stop delay</th><th style={{ width: 200 }} /></tr></thead>
          <tbody>
            {vms.length === 0 && <tr><td className="empty" colSpan={7}>No virtual machines</td></tr>}
            {enabled.map((v, i) => (
              <tr key={v.uuid}>
                <td>{i + 1}</td><td><b>{v.name}</b></td><td><span className={`status ${v.state}`}>{v.state}</span></td>
                <td>{v.start_delay >= 0 ? `${v.start_delay} s` : `default (${s.start_delay} s)`}</td>
                <td>{v.stop_action ? STOP_LABEL[v.stop_action] : `default (${STOP_LABEL[s.stop_action]})`}</td>
                <td>{v.stop_delay >= 0 ? `${v.stop_delay} s` : `default (${s.stop_delay} s)`}</td>
                <td className="right">{!ro && <span className="btn-group">
                  <Btn size="sm" disabled={i === 0} onClick={() => move(i, -1)}>Move up</Btn>
                  <Btn size="sm" disabled={i === enabled.length - 1} onClick={() => move(i, 1)}>Move down</Btn>
                  <Btn size="sm" onClick={() => toggle(v, false)}>Disable</Btn></span>}</td>
              </tr>
            ))}
            {manual.map((v) => (
              <tr key={v.uuid} className="muted">
                <td>Manual</td><td>{v.name}</td><td><span className={`status ${v.state}`}>{v.state}</span></td><td colSpan={3}>Not started with the host</td>
                <td className="right">{!ro && <Btn size="sm" onClick={() => toggle(v, true)}>Enable</Btn>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </>
  );
}

/* ------------------------------------------------------------------ Manage > Logging & monitoring */
export function LoggingTab() {
  return <div className="grid c2"><SyslogPanel /><SnmpPanel /><PowerPanel /></div>;
}

function SyslogPanel() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get("/api/v1/host/syslog"));
  const { run, busy } = useAction();
  const [t, setT] = useState<any[]>([]);
  useEffect(() => { if (data) setT(data.targets); }, [data]);
  const ro = !can("host.config") || !data?.available;
  return (
    <Panel title="Remote syslog" icon="log">
      <ErrorBox error={error} onRetry={reload} />
      {data && <div className="form">
        {!data.available && <div className="alert warn"><Icon name="warn" /><div>rsyslog is not installed on this host.</div></div>}
        <div className="small muted">All host log messages (LSX-VE, hypervisor, kernel, alarms) are forwarded to these servers (ESXi Syslog.global.logHost). Up to 4 servers.</div>
        {t.map((x, i) => (
          <div key={i} className="row" style={{ gap: 6 }}>
            <input type="text" placeholder="syslog.example.com" value={x.host} disabled={ro} onChange={(e) => setT(t.map((y, j) => j === i ? { ...y, host: e.target.value } : y))} style={{ flex: 1 }} />
            <select value={x.protocol} disabled={ro} style={{ width: 80 }} onChange={(e) => setT(t.map((y, j) => j === i ? { ...y, protocol: e.target.value } : y))}><option value="udp">UDP</option><option value="tcp">TCP</option></select>
            <input type="number" value={x.port} min={1} max={65535} disabled={ro} style={{ width: 90 }} onChange={(e) => setT(t.map((y, j) => j === i ? { ...y, port: Number(e.target.value) } : y))} />
            <Btn icon="trash" disabled={ro} title="Remove" onClick={() => setT(t.filter((_, j) => j !== i))} />
          </div>
        ))}
        <div className="btn-group">
          <Btn icon="plus" disabled={ro || t.length >= 4} onClick={() => setT([...t, { host: "", port: 514, protocol: "udp" }])}>Add server</Btn>
          <Btn kind="primary" disabled={ro || busy} onClick={() => run(() => api.put("/api/v1/host/syslog", { targets: t }), "Remote syslog saved").then(reload)}>Save</Btn>
        </div>
      </div>}
    </Panel>
  );
}

function SnmpPanel() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get("/api/v1/host/snmp"));
  const { run, busy } = useAction();
  const [f, setF] = useState<any>({ enabled: false, community: "", sources: "", location: "", contact: "" });
  useEffect(() => { if (data) setF({ enabled: data.enabled, community: "", sources: (data.sources ?? []).join(", "), location: data.location ?? "", contact: data.contact ?? "" }); }, [data]);
  const ro = !can("host.config") || !data?.available;
  return (
    <Panel title="SNMP agent" icon="monitor">
      <ErrorBox error={error} onRetry={reload} />
      {data && <div className="form">
        {!data.available && <div className="alert warn"><Icon name="warn" /><div>net-snmp is not installed on this host.</div></div>}
        <div className="kv"><div>Agent</div><div><span className={`status ${data.running ? "running" : "shutoff"}`}>{data.running ? "Running" : "Stopped"}</span> <span className="small muted">SNMP v2c, read-only, UDP 161</span></div></div>
        <label className="check"><input type="checkbox" checked={f.enabled} disabled={ro} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> Enable SNMP agent</label>
        <Field label="Read-only community" help={data.community_set ? "A community is set. Leave empty to keep it." : "6–32 characters: letters, digits, . _ ~ -"}>
          <input type="password" autoComplete="off" value={f.community} disabled={ro} onChange={(e) => setF({ ...f, community: e.target.value })} />
        </Field>
        <Field label="Allowed source networks" help="Comma separated, e.g. 10.0.0.0/8, 192.168.1.10/32. The firewall opens UDP 161 only while the agent is enabled.">
          <input type="text" value={f.sources} disabled={ro} onChange={(e) => setF({ ...f, sources: e.target.value })} />
        </Field>
        <div className="grid c2">
          <Field label="Location"><input type="text" value={f.location} disabled={ro} onChange={(e) => setF({ ...f, location: e.target.value })} /></Field>
          <Field label="Contact"><input type="text" value={f.contact} disabled={ro} onChange={(e) => setF({ ...f, contact: e.target.value })} /></Field>
        </div>
        <div><Btn kind="primary" disabled={ro || busy} onClick={() => run(() => api.put("/api/v1/host/snmp", {
          ...f, sources: f.sources.split(/[\s,]+/).filter(Boolean), community: f.community || null,
        }), "SNMP configuration saved").then(reload)}>Save</Btn></div>
      </div>}
    </Panel>
  );
}

function PowerPanel() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get("/api/v1/host/power-policy"));
  const { run, busy } = useAction();
  const ro = !can("host.config") || !data?.available;
  return (
    <Panel title="Power management" icon="power">
      <ErrorBox error={error} onRetry={reload} />
      {data && <div className="form">
        {!data.available && <div className="alert warn"><Icon name="warn" /><div>tuned is not installed on this host.</div></div>}
        <div className="small muted">Power policy of the host CPUs (tuned profile). Active profile: <span className="mono">{data.active || "none"}</span></div>
        {data.policies.map((p: any) => (
          <label key={p.id} className="check" style={{ alignItems: "flex-start" }}>
            <input type="radio" name="pp" checked={data.active === p.id} disabled={ro || busy}
              onChange={() => run(() => api.put("/api/v1/host/power-policy", { policy: p.id }), `Power policy: ${p.label}`).then(reload)} />
            <span><b>{p.label}</b><br /><span className="small muted">{p.description}</span></span>
          </label>
        ))}
      </div>}
    </Panel>
  );
}

/* ------------------------------------------------------------------ Manage > Security additions */
export function LockdownPanels() {
  const { can } = useSession();
  const lk = useAsync(() => api.get("/api/v1/host/lockdown"));
  const pol = useAsync(() => api.get("/api/v1/host/security-policy"));
  const { run, busy } = useAction();
  const [mode, setMode] = useState<string | null>(null);
  const [p, setP] = useState<any>(null);
  useEffect(() => { if (pol.data) setP(pol.data); }, [pol.data]);
  const ro = !can("security.manage");
  const cur = lk.data?.mode ?? "disabled";
  const LBL: Record<string, string> = { disabled: "Disabled", normal: "Normal", strict: "Strict" };
  return (
    <div className="grid c2" style={{ marginTop: 10 }}>
      <Panel title="Lockdown mode" icon="shield">
        <ErrorBox error={lk.error} onRetry={lk.reload} />
        <div className="form">
          <div className="kv"><div>Current mode</div><div><span className={`status ${cur === "disabled" ? "shutoff" : "warning"}`}>{LBL[cur]}</span></div></div>
          <div className="small muted">
            <b>Normal</b>: SSH is stopped and cannot be enabled; the local console (DCUI) still works with the root password.<br />
            <b>Strict</b>: additionally the console customization menu, Shut Down/Restart and all local login terminals are disabled –
            the host can only be managed from this web interface. Keep at least one working web administrator.
          </div>
          <div className="btn-group">
            {["disabled", "normal", "strict"].map((m) => <Btn key={m} kind={m === cur ? "primary" : "outline"} disabled={ro || busy || m === cur} onClick={() => setMode(m)}>{LBL[m]}</Btn>)}
          </div>
        </div>
      </Panel>
      <Panel title="Account lockout" icon="key">
        <ErrorBox error={pol.error} onRetry={pol.reload} />
        {p && <div className="form">
          <div className="small muted">Applies to LSX-VE web users. A locked account unlocks automatically after the lockout time, or with <span className="mono">lsxve-admin unlock</span> on the console.</div>
          <div className="grid c2">
            <Field label="Failed attempts before lockout" help="0 = never lock"><input type="number" min={0} max={100} value={p.login_max_failures} disabled={ro} onChange={(e) => setP({ ...p, login_max_failures: Number(e.target.value) })} /></Field>
            <Field label="Lockout time"><span className="unit-input"><input type="number" min={0} max={86400} value={p.login_lockout_seconds} disabled={ro} onChange={(e) => setP({ ...p, login_lockout_seconds: Number(e.target.value) })} /><span>s</span></span></Field>
          </div>
          <div><Btn kind="primary" disabled={ro || busy} onClick={() => run(() => api.put("/api/v1/host/security-policy", { login_max_failures: p.login_max_failures, login_lockout_seconds: p.login_lockout_seconds }), "Account lockout policy saved").then(pol.reload)}>Save</Btn></div>
        </div>}
      </Panel>
      <Panel title="Directory services" icon="users">
        <div className="small muted">Active Directory / LDAP authentication is not implemented in this release. Users are local LSX-VE accounts (Manage &gt; Users).</div>
      </Panel>
      {mode && <Confirm title={`Lockdown mode: ${LBL[mode]}`} danger={mode === "strict"} confirmLabel={`Set ${LBL[mode]}`}
        typed={mode === "strict" ? "strict" : undefined}
        message={mode === "disabled" ? "SSH can be enabled again and the local console is fully available." :
          mode === "normal" ? "SSH will be stopped and cannot be enabled while lockdown mode is on." :
          "SSH, local login terminals and the console customization menu will be disabled. Recovery is only possible from this web interface."}
        onConfirm={() => run(() => api.put("/api/v1/host/lockdown", { mode }), `Lockdown mode set to ${LBL[mode]}`).then(lk.reload)}
        onClose={() => setMode(null)} />}
    </div>
  );
}

/* ------------------------------------------------------------------ Manage > Support */
export function SupportTab() {
  const { can } = useSession();
  const info = useAsync(() => api.get("/api/v1/host/support-info"));
  const list = useAsync(() => can("host.config") ? api.get<any[]>("/api/v1/host/support-bundles") : Promise.resolve([]));
  const { run, busy } = useAction();
  const { tasks } = useLive();
  const running = tasks.some((t) => t.kind === "host.support_bundle" && (t.state === "running" || t.state === "queued"));
  useLiveEvent((type, d) => { if (type === "task" && d.kind === "host.support_bundle" && d.state !== "running") list.reload(); }, []);
  const i = info.data;
  return (
    <div className="grid c2">
      <Panel title="Support information" icon="info">
        <ErrorBox error={info.error} onRetry={info.reload} />
        {i && <div className="kv">
          <div>Product</div><div>{i.product} {i.version}</div>
          <div>Host name</div><div>{i.hostname}</div>
          <div>Manufacturer</div><div>{i.manufacturer || "—"}</div>
          <div>Model</div><div>{i.model || "—"}</div>
          <div>Serial number</div><div className="mono">{i.serial || "—"}</div>
          <div>System UUID</div><div className="mono">{i.uuid || "—"}</div>
          <div>BIOS</div><div>{i.bios || "—"}</div>
          <div>Certificate SHA-256</div><div className="mono small" style={{ wordBreak: "break-all" }}>{i.cert_sha256 || "—"}</div>
        </div>}
      </Panel>
      <Panel title="Support bundle" icon="log" right={can("host.config") && <Btn size="sm" kind="primary" disabled={busy || running}
        onClick={() => run(() => api.post("/api/v1/host/support-bundles"), "Generating support bundle")}>{running ? "Generating…" : "Generate support bundle"}</Btn>}>
        <div className="small muted" style={{ marginBottom: 8 }}>A .tar.gz with logs of the current and previous boot, service state, network, storage, VM definitions and LSX-VE configuration.
          Passwords, keys and the SNMP community are left out. The 3 newest bundles are kept.</div>
        <ErrorBox error={list.error} onRetry={list.reload} />
        <table className="dt">
          <thead><tr><th>Bundle</th><th className="num">Size</th><th>Created</th><th /></tr></thead>
          <tbody>
            {(list.data ?? []).length === 0 && <tr><td className="empty" colSpan={4}>No support bundles</td></tr>}
            {(list.data ?? []).map((b) => <tr key={b.name}><td className="mono small">{b.name}</td><td className="num">{bytes(b.size)}</td><td className="nowrap">{ts(b.created)}</td>
              <td className="right"><a className="btn sm" href={`/api/v1/host/support-bundles/${enc(b.name)}`} download>Download</a></td></tr>)}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}
