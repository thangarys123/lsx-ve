import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, enc } from "../api";
import { Icon } from "../icons";
import { useSession } from "../session";
import { Btn, Confirm, DataTable, ErrorBox, Field, Loading, Modal, Panel, Tabs, useAction, useAsync, useToast } from "../ui";

export function NetworkingPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "portgroups";
  const host = useAsync(() => api.get("/api/v1/network/host"));
  return (
    <div className="page">
      <div className="page-head"><h1><Icon name="network" size={20} /> Networking</h1></div>
      <PendingBanner pending={host.data?.pending} onDone={host.reload} />
      <VmBridgeBanner st={host.data?.vm_bridge} onDone={host.reload} />
      <Tabs tabs={[["portgroups", "Port groups"], ["vswitches", "Virtual switches"], ["nics", "Physical adapters"], ["connections", "Host interfaces"], ["virtual", "Virtual networks"]]} value={tab} onChange={(t) => setParams({ tab: t })} />
      {tab === "portgroups" && <PortGroups />}
      {tab === "vswitches" && <VSwitches />}
      {tab === "nics" && <Nics host={host} />}
      {tab === "connections" && <Connections host={host} />}
      {tab === "virtual" && <VirtualNetworks host={host.data} />}
    </div>
  );
}

/* ------------------------------------------------------------------ bridged VM network */
function VmBridgeBanner({ st, onDone }: { st: any; onDone: () => void }) {
  const { can } = useSession();
  const { run, busy } = useAction();
  if (!st) return null;
  if (st.status === "done" && st.bridge_present)
    return <div className="alert info"><Icon name="bridge" /><div>VMs and containers on the virtual network <b>default</b> are bridged to <span className="mono">{st.bridge}</span>{st.port ? <> (physical port <span className="mono">{st.port}</span>)</> : null} and get their addresses from the physical network. There is no NAT.</div></div>;
  if (st.status === "disabled") return null;
  const failed = st.status === "failed" || st.status === "skipped";
  return (
    <div className={`alert ${failed ? "err" : "warn"}`}>
      <Icon name={failed ? "error" : "warn"} />
      <div className="grow">{failed
        ? <>The management interface could not be moved onto bridge <span className="mono">{st.bridge || "br0"}</span>: {st.reason}. VMs on network <b>default</b> have no connection until this works.</>
        : <>Setting up the bridged VM network…</>}</div>
      {failed && can("network.manage") && <Btn size="sm" icon="refresh" disabled={busy}
        onClick={() => run(() => api.post("/api/v1/network/host/vm-bridge/retry"), "Retrying — the connection drops for a few seconds").then(() => setTimeout(onDone, 15000))}>Retry</Btn>}
    </div>
  );
}

/* ------------------------------------------------------------------ rollback banner */
function PendingBanner({ pending, onDone }: { pending: any; onDone: () => void }) {
  const [now, setNow] = useState(Date.now() / 1000);
  const [reach, setReach] = useState(true);
  const { run, busy } = useAction();
  useEffect(() => {
    if (!pending || pending.state === "rolled_back") return;
    const t = setInterval(async () => {
      setNow(Date.now() / 1000);
      try {
        await api.get("/api/v1/network/host/pending");
        setReach(true);
      } catch {
        setReach(false);
      }
    }, 2000);
    return () => clearInterval(t);
  }, [pending]);
  useEffect(() => {
    if (pending && pending.state !== "rolled_back" && now > pending.expires + 3) onDone();
  }, [now, pending, onDone]);
  if (!pending) return null;
  if (pending.state === "rolled_back")
    return <div className="alert warn"><Icon name="warn" /><div className="grow">The last network change (<b>{pending.summary}</b>) was not confirmed and has been <b>rolled back automatically</b>.</div></div>;
  const left = Math.max(0, Math.round(pending.expires - now));
  return (
    <div className="alert warn" role="alert">
      <Icon name="clock" />
      <div className="grow">
        <b>Network change awaiting confirmation:</b> {pending.summary}
        <div className="small">{reach ? "The management interface is still reachable. Confirm to keep the change; otherwise it is rolled back automatically."
          : "This browser can no longer reach the host at this address. If the management address changed, open the new address and confirm there — otherwise the change is rolled back automatically."}</div>
      </div>
      <span className="countdown">{left}s</span>
      <Btn kind="primary" disabled={busy || !reach} onClick={() => run(() => api.post("/api/v1/network/host/pending/confirm", { checkpoint: pending.checkpoint }), "Network change confirmed").then(onDone)}>Keep change</Btn>
      <Btn kind="outline" disabled={busy || !reach} onClick={() => run(() => api.post("/api/v1/network/host/pending/rollback", { checkpoint: pending.checkpoint }), "Network change rolled back").then(onDone)}>Roll back now</Btn>
    </div>
  );
}

function useApplyChange(onDone: () => void) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const apply = async (change: any) => {
    setBusy(true);
    try {
      const r = await api.post("/api/v1/network/host/changes", change);
      toast({ kind: "warning", title: "Network change applied", detail: `${r.summary}. Confirm within ${Math.round(r.expires - Date.now() / 1000)} s or it will be rolled back.` });
      onDone();
      return true;
    } catch (e: any) {
      toast({ kind: "error", title: "Network change failed", detail: e.status === 0 ? "Connection lost while applying. If the change broke connectivity it will be rolled back automatically." : e.message });
      onDone();
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { apply, busy };
}

/* ------------------------------------------------------------------ physical NICs */
function Nics({ host }: { host: ReturnType<typeof useAsync<any>> }) {
  if (host.error) return <ErrorBox error={host.error} onRetry={host.reload} />;
  if (!host.data) return <Loading />;
  const phys = host.data.nics.filter((n: any) => n.kind === "physical");
  return (
    <Panel flush>
      <DataTable rowKey={(r: any) => r.name} rows={phys} cols={[
        { key: "name", label: "Adapter", sort: (r: any) => r.name, render: (r: any) => <><span className="mono">{r.name}</span>{r.name === host.data.management.interface && <span className="badge blue" style={{ marginLeft: 6 }}>management</span>}</> },
        { key: "carrier", label: "Link", render: (r: any) => <span className={`status ${r.carrier ? "ok" : "error"}`}>{r.carrier ? "Up" : "Down"}</span> },
        { key: "speed_mbps", label: "Speed", render: (r: any) => r.speed_mbps ? `${r.speed_mbps >= 1000 ? r.speed_mbps / 1000 + " Gbit/s" : r.speed_mbps + " Mbit/s"} ${r.duplex ?? ""}` : "—" },
        { key: "mac", label: "MAC address", render: (r: any) => <span className="mono">{r.mac}</span> },
        { key: "driver", label: "Driver" },
        { key: "mtu", label: "MTU", num: true },
        { key: "master", label: "Member of", render: (r: any) => r.master ?? "—" },
        { key: "addrs", label: "Addresses", render: (r: any) => <span className="mono small">{(host.data.addresses?.[r.name] ?? []).join(", ") || "—"}</span> },
      ]} />
    </Panel>
  );
}

/* ------------------------------------------------------------------ NM connections */
function Connections({ host }: { host: ReturnType<typeof useAsync<any>> }) {
  const { can } = useSession();
  const [sel, setSel] = useState<string[]>([]);
  const [dlg, setDlg] = useState<null | "edit" | "bridge" | "vlan" | "bond" | "delete">(null);
  const { apply } = useApplyChange(host.reload);
  if (host.error) return <ErrorBox error={host.error} onRetry={host.reload} />;
  if (!host.data) return <Loading />;
  const d = host.data;
  if (!d.agent) return <div className="alert err"><Icon name="error" /><div>Host agent unavailable: {d.agent_error}</div></div>;
  const conns = d.connections ?? [];
  const cur = conns.find((c: any) => c.uuid === sel[0]);
  const mgmt = d.management.interface;
  const m = can("network.manage") && !d.pending;
  const phys = d.nics.filter((n: any) => n.kind === "physical").map((n: any) => n.name);
  return (
    <>
      <div className="toolbar">
        <Btn icon="edit" disabled={!m || !cur || !!cur.slave_type} onClick={() => setDlg("edit")}>Edit settings</Btn>
        <div className="sep" />
        <Btn icon="bridge" disabled={!m} onClick={() => setDlg("bridge")}>Add bridge</Btn>
        <Btn icon="vlan" disabled={!m} onClick={() => setDlg("vlan")}>Add VLAN</Btn>
        <Btn icon="bond" disabled={!m} onClick={() => setDlg("bond")}>Add bond</Btn>
        <div className="sep" />
        <Btn icon="trash" kind="danger" disabled={!m || !cur} onClick={() => setDlg("delete")}>Delete</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={host.reload}>Refresh</Btn>
      </div>
      {d.pending && <div className="small muted" style={{ marginBottom: 8 }}>Further changes are blocked until the pending change is confirmed or rolled back.</div>}
      <Panel flush>
        <DataTable rowKey={(r: any) => r.uuid} rows={conns} selected={sel} onSelect={setSel} onOpen={() => m && setDlg("edit")}
          cols={[
            { key: "name", label: "Connection", sort: (r: any) => r.name, render: (r: any) => <><b>{r.name}</b>{r.device && r.device === mgmt && <span className="badge blue" style={{ marginLeft: 6 }}>management</span>}</> },
            { key: "type", label: "Type", render: (r: any) => <span className="badge">{r.type.replace("802-3-ethernet", "ethernet")}</span> },
            { key: "device", label: "Device", render: (r: any) => <span className="mono">{r.device ?? r.interface ?? "—"}</span> },
            { key: "active", label: "State", render: (r: any) => <span className={`status ${r.active ? "ok" : "shutoff"}`}>{r.active ? "Active" : "Inactive"}</span> },
            { key: "ipv4", label: "IPv4", render: (r: any) => r.slave_type ? <span className="muted">port of {r.master}</span> : <span className="small"><span className="badge">{r.ipv4.method}</span> <span className="mono">{r.ipv4.addresses.join(", ") || (r.device ? (d.addresses?.[r.device] ?? []).filter((a: string) => !a.includes(":")).join(", ") : "")}</span></span> },
            { key: "ipv6", label: "IPv6", render: (r: any) => r.slave_type ? "" : <span className="small"><span className="badge">{r.ipv6.method}</span> <span className="mono">{r.ipv6.addresses.join(", ")}</span></span> },
            { key: "gw", label: "Gateway", render: (r: any) => <span className="mono small">{r.ipv4.gateway ?? ""}</span> },
            { key: "mtu", label: "MTU", num: true, render: (r: any) => r.mtu ?? "auto" },
            { key: "auto", label: "Autoconnect", render: (r: any) => r.autoconnect ? "Yes" : "No" },
          ]} />
      </Panel>
      {dlg === "edit" && cur && <EditConnection conn={cur} mgmt={cur.device === mgmt} onClose={() => setDlg(null)} apply={apply} />}
      {(dlg === "bridge" || dlg === "vlan" || dlg === "bond") && <CreateIface kind={dlg} phys={phys} mgmt={mgmt} conns={conns} onClose={() => setDlg(null)} apply={apply} />}
      {dlg === "delete" && cur && <Confirm danger title={`Delete connection ${cur.name}`} confirmLabel="Delete" onClose={() => setDlg(null)}
        message={<>{cur.device === mgmt && <div className="alert err"><Icon name="warn" /><div>This connection carries the management network. If deleting it breaks connectivity, the change is rolled back automatically.</div></div>}The connection profile is deleted. You must confirm the change afterwards.</>}
        onConfirm={() => apply({ op: "delete", uuid: cur.uuid }).then(() => setSel([]))} />}
    </>
  );
}

interface IpState { m4: string; a4: string; g4: string; d4: string; m6: string; a6: string; g6: string; }
const split = (s: string) => s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

function IpFields({ s, set }: { s: IpState; set: (p: Partial<IpState>) => void }) {
  return (
    <div className="grid c2">
      <div className="form">
        <h4>IPv4</h4>
        <Field label="Method">
          <select value={s.m4} onChange={(e) => set({ m4: e.target.value })}>
            <option value="auto">DHCP</option><option value="manual">Static</option><option value="disabled">Disabled</option>
          </select>
        </Field>
        {s.m4 === "manual" && <>
          <Field label="Addresses" help="CIDR, comma separated (e.g. 10.0.0.10/24)"><input type="text" value={s.a4} onChange={(e) => set({ a4: e.target.value })} /></Field>
          <Field label="Default gateway"><input type="text" value={s.g4} onChange={(e) => set({ g4: e.target.value })} /></Field>
        </>}
        {s.m4 !== "disabled" && <Field label="DNS servers (this connection)"><input type="text" value={s.d4} onChange={(e) => set({ d4: e.target.value })} /></Field>}
      </div>
      <div className="form">
        <h4>IPv6</h4>
        <Field label="Method">
          <select value={s.m6} onChange={(e) => set({ m6: e.target.value })}>
            <option value="auto">Automatic (SLAAC + DHCPv6)</option><option value="dhcp">DHCPv6 only</option><option value="manual">Static</option><option value="ignore">Ignore</option><option value="disabled">Disabled</option>
          </select>
        </Field>
        {s.m6 === "manual" && <>
          <Field label="Addresses" help="e.g. 2001:db8::10/64"><input type="text" value={s.a6} onChange={(e) => set({ a6: e.target.value })} /></Field>
          <Field label="Default gateway"><input type="text" value={s.g6} onChange={(e) => set({ g6: e.target.value })} /></Field>
        </>}
      </div>
    </div>
  );
}

function ipBody(s: IpState) {
  return {
    ipv4: { method: s.m4, ...(s.m4 === "manual" ? { addresses: split(s.a4), gateway: s.g4 || null } : {}), ...(s.m4 !== "disabled" ? { dns: split(s.d4) } : {}) },
    ipv6: { method: s.m6, ...(s.m6 === "manual" ? { addresses: split(s.a6), gateway: s.g6 || null } : {}) },
  };
}

function EditConnection({ conn, mgmt, onClose, apply }: { conn: any; mgmt: boolean; onClose: () => void; apply: (c: any) => Promise<boolean> }) {
  const [s, setS] = useState<IpState>({
    m4: conn.ipv4.method === "manual" ? "manual" : conn.ipv4.method === "disabled" ? "disabled" : "auto",
    a4: conn.ipv4.addresses.join(", "), g4: conn.ipv4.gateway ?? "", d4: conn.ipv4.dns.join(", "),
    m6: ["auto", "dhcp", "manual", "ignore", "disabled"].includes(conn.ipv6.method) ? conn.ipv6.method : "auto",
    a6: conn.ipv6.addresses.join(", "), g6: conn.ipv6.gateway ?? "",
  });
  const [mtu, setMtu] = useState<string>(conn.mtu ? String(conn.mtu) : "");
  const [auto, setAuto] = useState(conn.autoconnect);
  const [busy, setBusy] = useState(false);
  return (
    <Modal title={`Edit ${conn.name}`} icon="nic" size="mid" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy} onClick={async () => {
        setBusy(true);
        const ok = await apply({ op: "configure", uuid: conn.uuid, ...ipBody(s), ...(mtu ? { mtu: Number(mtu) } : {}), autoconnect: auto });
        setBusy(false);
        if (ok) onClose();
      }}>Apply</Btn></>}>
      <div className="form">
        {mgmt && <div className="alert warn"><Icon name="warn" /><div>This is the <b>management connection</b>. The change is applied with an automatic rollback: if you cannot confirm it from the browser within the timeout, the previous configuration is restored.</div></div>}
        <IpFields s={s} set={(p) => setS({ ...s, ...p })} />
        <div className="row">
          <Field label="MTU" help="576–9216; empty keeps the current setting"><input type="number" value={mtu} placeholder="auto" onChange={(e) => setMtu(e.target.value)} /></Field>
          <label className="check" style={{ marginTop: 18 }}><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> Connect automatically at boot</label>
        </div>
      </div>
    </Modal>
  );
}

function CreateIface({ kind, phys, mgmt, conns, onClose, apply }: { kind: "bridge" | "vlan" | "bond"; phys: string[]; mgmt: string | null; conns: any[]; onClose: () => void; apply: (c: any) => Promise<boolean> }) {
  const [name, setName] = useState(kind === "bridge" ? "br0" : kind === "bond" ? "bond0" : "");
  const [ports, setPorts] = useState<string[]>([]);
  const [parent, setParent] = useState(phys[0] ?? "");
  const [vid, setVid] = useState(100);
  const [mode, setMode] = useState("active-backup");
  const [stp, setStp] = useState(false);
  const [mtu, setMtu] = useState("");
  const mgConn = conns.find((c) => c.device === mgmt);
  const takesMgmt = (kind !== "vlan") && mgmt !== null && ports.includes(mgmt);
  const [s, setS] = useState<IpState>({ m4: "disabled", a4: "", g4: "", d4: "", m6: "ignore", a6: "", g6: "" });
  useEffect(() => {
    // moving the management NIC into a bridge/bond: carry its IP configuration over
    if (takesMgmt && mgConn) setS({
      m4: mgConn.ipv4.method === "manual" ? "manual" : "auto", a4: mgConn.ipv4.addresses.join(", "), g4: mgConn.ipv4.gateway ?? "", d4: mgConn.ipv4.dns.join(", "),
      m6: ["auto", "dhcp", "manual", "ignore", "disabled"].includes(mgConn.ipv6.method) ? mgConn.ipv6.method : "auto", a6: mgConn.ipv6.addresses.join(", "), g6: mgConn.ipv6.gateway ?? "",
    });
  }, [takesMgmt]); // eslint-disable-line react-hooks/exhaustive-deps
  const vlanName = name || `${parent}.${vid}`;
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    const body: any = { op: `create_${kind}`, name: kind === "vlan" ? vlanName : name, ...ipBody(s), ...(mtu ? { mtu: Number(mtu) } : {}) };
    if (kind === "bridge") Object.assign(body, { ports, stp });
    if (kind === "bond") Object.assign(body, { members: ports, mode });
    if (kind === "vlan") Object.assign(body, { parent, vlan_id: vid });
    setBusy(true);
    const ok = await apply(body);
    setBusy(false);
    if (ok) onClose();
  };
  const title = { bridge: "Add bridge", vlan: "Add VLAN interface", bond: "Add bond (NIC team)" }[kind];
  return (
    <Modal title={title} icon={kind} size="mid" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || (kind !== "vlan" && !name) || (kind === "bond" && ports.length === 0)} onClick={submit}>Apply</Btn></>}>
      <div className="form">
        {kind === "bridge" && <div className="small muted">A host bridge lets virtual machines attach directly to the physical network. Select the uplink adapter(s).</div>}
        <div className="row">
          <Field label="Interface name" help={kind === "vlan" ? `Default: ${parent}.${vid}` : "max 15 characters"}>
            <input type="text" value={name} placeholder={kind === "vlan" ? `${parent}.${vid}` : ""} onChange={(e) => setName(e.target.value)} maxLength={15} />
          </Field>
          {kind === "vlan" && <>
            <Field label="Parent interface"><select value={parent} onChange={(e) => setParent(e.target.value)}>{[...phys, ...conns.filter((c) => c.type === "bridge" || c.type === "bond").map((c) => c.interface ?? c.name)].map((p) => <option key={p}>{p}</option>)}</select></Field>
            <Field label="VLAN ID"><input type="number" min={1} max={4094} value={vid} onChange={(e) => setVid(Number(e.target.value))} /></Field>
          </>}
          {kind === "bond" && <Field label="Mode"><select value={mode} onChange={(e) => setMode(e.target.value)}>
            {["active-backup", "802.3ad", "balance-alb", "balance-tlb", "balance-xor", "balance-rr", "broadcast"].map((m) => <option key={m}>{m}</option>)}</select></Field>}
        </div>
        {kind !== "vlan" && (
          <Field label={kind === "bridge" ? "Uplink ports" : "Member adapters"}>
            <div className="inline-list">{phys.map((p) => (
              <label key={p} className="check" style={{ marginRight: 14 }}><input type="checkbox" checked={ports.includes(p)} onChange={(e) => setPorts(e.target.checked ? [...ports, p] : ports.filter((x) => x !== p))} />
                <span className="mono">{p}</span>{p === mgmt && <span className="badge blue">management</span>}</label>))}</div>
          </Field>
        )}
        {takesMgmt && <div className="alert warn"><Icon name="warn" /><div>The management adapter <b>{mgmt}</b> will become a port of <b>{name}</b>. Its IP configuration has been copied below so the host stays reachable. The change rolls back automatically unless confirmed.</div></div>}
        {kind === "bridge" && <label className="check"><input type="checkbox" checked={stp} onChange={(e) => setStp(e.target.checked)} /> Enable Spanning Tree Protocol</label>}
        <IpFields s={s} set={(p) => setS({ ...s, ...p })} />
        <Field label="MTU"><input type="number" value={mtu} placeholder="auto" onChange={(e) => setMtu(e.target.value)} style={{ width: 140 }} /></Field>
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------ libvirt networks */
function VirtualNetworks({ host }: { host: any }) {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get<any[]>("/api/v1/network/virtual"));
  const [sel, setSel] = useState<string[]>([]);
  const [create, setCreate] = useState(false);
  const [del, setDel] = useState(false);
  const { run, busy } = useAction();
  const cur = data?.find((n) => n.name === sel[0]);
  const act = (a: string) => cur && run(() => api.post(`/api/v1/network/virtual/${enc(cur.name)}/${a}`)).then(reload);
  const m = can("network.manage");
  return (
    <>
      <div className="toolbar">
        <Btn icon="plus" disabled={!m} onClick={() => setCreate(true)}>Add virtual network</Btn>
        <Btn icon="play" disabled={!m || !cur || cur.active || busy} onClick={() => act("start")}>Start</Btn>
        <Btn icon="stop" disabled={!m || !cur || !cur.active || busy} onClick={() => act("stop")}>Stop</Btn>
        <Btn icon="power" disabled={!m || !cur} onClick={() => act(cur?.autostart ? "autostart_off" : "autostart_on")}>{cur?.autostart ? "Disable autostart" : "Enable autostart"}</Btn>
        <Btn icon="trash" kind="danger" disabled={!m || !cur || cur.name === "default" || cur.used_by.length > 0} onClick={() => setDel(true)}>Delete</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <ErrorBox error={error} onRetry={reload} />
      <Panel flush>
        <DataTable rowKey={(r: any) => r.name} rows={data ?? []} selected={sel} onSelect={setSel} cols={[
          { key: "name", label: "Name", sort: (r: any) => r.name, render: (r: any) => <b>{r.name}</b> },
          { key: "active", label: "State", render: (r: any) => <span className={`status ${r.active ? "active" : "inactive"}`}>{r.active ? "Active" : "Inactive"}</span> },
          { key: "mode", label: "Mode", render: (r: any) => ({ nat: "NAT", isolated: "Isolated", route: "Routed", bridge: "Host bridge" } as any)[r.mode] ?? r.mode },
          { key: "bridge", label: "Bridge", render: (r: any) => <span className="mono">{r.bridge ?? "—"}</span> },
          { key: "ipv4", label: "Subnet", render: (r: any) => <span className="mono">{r.ipv4 ?? "—"}</span> },
          { key: "dhcp", label: "DHCP range", render: (r: any) => r.dhcp ? <span className="mono small">{r.dhcp.start} – {r.dhcp.end}</span> : "—" },
          { key: "used_by", label: "VMs", render: (r: any) => r.used_by.length ? r.used_by.join(", ") : <span className="muted">—</span> },
          { key: "autostart", label: "Autostart", render: (r: any) => r.autostart ? "Yes" : "No" },
        ]} />
      </Panel>
      {create && <CreateVnet bridges={(host?.nics ?? []).filter((n: any) => n.kind === "bridge" && !n.name.startsWith("virbr")).map((n: any) => n.name)} onClose={() => { setCreate(false); reload(); }} />}
      {del && cur && <Confirm danger title={`Delete ${cur.name}`} confirmLabel="Delete" onClose={() => setDel(false)} message="The virtual network is stopped and removed."
        onConfirm={() => run(() => api.post(`/api/v1/network/virtual/${enc(cur.name)}/delete`), "Virtual network deleted").then(() => { setSel([]); reload(); })} />}
    </>
  );
}

function CreateVnet({ bridges, onClose }: { bridges: string[]; onClose: () => void }) {
  const [name, setName] = useState("");
  const [mode, setMode] = useState("nat");
  const [ipv4, setIpv4] = useState("192.168.150.1/24");
  const [dhcp, setDhcp] = useState(true);
  const [ds, setDs] = useState("192.168.150.100");
  const [de, setDe] = useState("192.168.150.200");
  const [br, setBr] = useState(bridges[0] ?? "");
  const { run, busy } = useAction();
  const submit = () => {
    const body: any = { name, mode };
    if (mode === "bridge") body.bridge = br;
    else Object.assign(body, { ipv4, ...(dhcp ? { dhcp_start: ds, dhcp_end: de } : {}) });
    run(() => api.post("/api/v1/network/virtual", body), "Virtual network created").then((r) => r && onClose());
  };
  return (
    <Modal title="Add virtual network" icon="network" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || !name || (mode === "bridge" && !br)} onClick={submit}>Create</Btn></>}>
      <div className="form">
        <Field label="Name"><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Mode">
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="nat">NAT — VMs reach outside through the host</option>
            <option value="route">Routed — VMs reachable via a route on your network</option>
            <option value="isolated">Isolated — VMs and host only</option>
            <option value="bridge">Host bridge — attach to an existing bridge</option>
          </select>
        </Field>
        {mode === "bridge" ? (
          <Field label="Host bridge" help={bridges.length === 0 ? "No bridges exist yet; create one under Host interfaces." : undefined}>
            <select value={br} onChange={(e) => setBr(e.target.value)}>{bridges.map((b) => <option key={b}>{b}</option>)}</select>
          </Field>
        ) : <>
          <Field label="Gateway address / prefix"><input type="text" value={ipv4} onChange={(e) => setIpv4(e.target.value)} /></Field>
          <label className="check"><input type="checkbox" checked={dhcp} onChange={(e) => setDhcp(e.target.checked)} /> DHCP server</label>
          {dhcp && <div className="row"><Field label="DHCP start"><input type="text" value={ds} onChange={(e) => setDs(e.target.value)} /></Field><Field label="DHCP end"><input type="text" value={de} onChange={(e) => setDe(e.target.value)} /></Field></div>}
        </>}
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------ port groups / virtual switches */
function PortGroups() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get<any[]>("/api/v1/network/portgroups"));
  const sw = useAsync(() => api.get<any[]>("/api/v1/network/vswitches"));
  const [sel, setSel] = useState<string[]>([]);
  const [add, setAdd] = useState(false);
  const [del, setDel] = useState(false);
  const { run } = useAction();
  const m = can("network.manage");
  const cur = data?.find((n) => n.name === sel[0]);
  const rows = (data ?? []).filter((n) => n.kind === "portgroup");
  return (
    <>
      <div className="toolbar">
        <Btn icon="plus" disabled={!m || !(sw.data ?? []).length} onClick={() => setAdd(true)}>Add port group</Btn>
        <Btn icon="trash" kind="danger" disabled={!m || !cur || cur.name === "default" || cur.used_by.length > 0} onClick={() => setDel(true)}>Remove</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={() => { reload(); sw.reload(); }}>Refresh</Btn>
      </div>
      <div className="small muted" style={{ marginBottom: 8 }}>A port group connects VM network adapters to a virtual switch. With a VLAN ID the VMs' traffic is tagged with that 802.1Q VLAN on the switch's physical uplink (the physical switch port must be a trunk). VLAN ID 0 = untagged.</div>
      <ErrorBox error={error} onRetry={reload} />
      <Panel flush>
        <DataTable rowKey={(r: any) => r.name} rows={rows} selected={sel} onSelect={setSel} empty="No port groups" cols={[
          { key: "name", label: "Name", sort: (r: any) => r.name, render: (r: any) => <><Icon name="network" size={13} /> <b>{r.name}</b></> },
          { key: "active", label: "State", render: (r: any) => <span className={`status ${r.active ? "active" : "inactive"}`}>{r.active ? "Active" : "Inactive"}</span> },
          { key: "vlan", label: "VLAN ID", sort: (r: any) => r.vlan, render: (r: any) => r.vlan ? r.vlan : <span className="muted">0 (none)</span> },
          { key: "vswitch", label: "Virtual switch", render: (r: any) => <span className="mono">{r.vswitch}</span> },
          { key: "used_by", label: "VMs", render: (r: any) => r.used_by.length ? r.used_by.join(", ") : <span className="muted">—</span> },
        ]} />
      </Panel>
      {add && <AddPortGroup switches={sw.data ?? []} existing={data ?? []} onClose={() => { setAdd(false); reload(); }} />}
      {del && cur && <Confirm danger title={`Remove port group ${cur.name}`} confirmLabel="Remove" onClose={() => setDel(false)}
        message={cur.vlan ? `The port group and, if no other port group uses it, VLAN ${cur.vlan} on ${cur.vswitch} are removed.` : "The port group is removed."}
        onConfirm={() => run(() => api.del(`/api/v1/network/portgroups/${enc(cur.name)}`)).then(() => { setSel([]); setTimeout(reload, 2500); })} />}
    </>
  );
}

function AddPortGroup({ switches, existing, onClose }: { switches: any[]; existing: any[]; onClose: () => void }) {
  const [name, setName] = useState("");
  const [vlan, setVlan] = useState(0);
  const [sw, setSw] = useState(switches[0]?.name ?? "");
  const { run, busy } = useAction();
  const sel = switches.find((x) => x.name === sw);
  const noUplink = vlan > 0 && sel && !sel.uplinks.some((u: any) => u.kind === "physical" || u.kind === "bond");
  const dup = existing.some((p) => p.name === name);
  return (
    <Modal title="Add port group" icon="network" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || !name || dup || !sw || noUplink || vlan < 0 || vlan > 4094}
        onClick={() => run(() => api.post("/api/v1/network/portgroups", { name, vswitch: sw, vlan_id: vlan }), `Adding port group ${name}`).then((r) => r && onClose())}>Add</Btn></>}>
      <div className="form">
        <Field label="Name" error={dup ? "A network with this name exists" : null}><input type="text" value={name} autoFocus onChange={(e) => setName(e.target.value)} placeholder="VM Network 20" /></Field>
        <Field label="VLAN ID" help="0 – 4094. 0 means untagged traffic."><input type="number" min={0} max={4094} value={vlan} onChange={(e) => setVlan(Number(e.target.value))} /></Field>
        <Field label="Virtual switch" error={noUplink ? "This virtual switch has no physical uplink to carry tagged VLAN traffic" : null}>
          <select value={sw} onChange={(e) => setSw(e.target.value)}>{switches.map((x) => <option key={x.name} value={x.name}>{x.name}{x.management ? " (management)" : ""}</option>)}</select>
        </Field>
        {vlan > 0 && sel?.management && <div className="small muted">The VLAN sub-interface is created inside a network checkpoint: if the management address is lost, the change is rolled back automatically.</div>}
      </div>
    </Modal>
  );
}

function VSwitches() {
  const { data, error, reload } = useAsync(() => api.get<any[]>("/api/v1/network/vswitches"));
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <Loading />;
  return (
    <>
      <div className="toolbar">
        <div className="small muted">Virtual switches are host bridges with physical uplinks. Create additional switches (bridges) or bonds under <i>Host interfaces</i>.</div>
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      {data.length === 0 && <div className="empty-state"><h3>No virtual switches</h3></div>}
      <div className="grid c2">
        {data.map((s) => (
          <Panel key={s.name} title={<>{s.name}{s.management && <span className="badge blue" style={{ marginLeft: 6 }}>management</span>}</>} icon="bridge">
            <div className="kv">
              <div>MTU</div><div>{s.mtu}</div>
              <div>Spanning tree</div><div>{s.stp ? "On" : "Off"}</div>
              <div>Uplinks</div><div>{s.uplinks.length ? s.uplinks.map((u: any) => (
                <div key={u.name}><span className={`status ${u.carrier ? "running" : "error"}`}><span className="mono">{u.name}</span></span> <span className="small muted">{u.kind}{u.speed_mbps ? `, ${u.speed_mbps} Mb/s` : ""}{u.carrier ? "" : ", link down"}</span></div>
              )) : <span className="muted">none (internal only)</span>}</div>
              <div>Port groups</div><div>{s.portgroups.length ? s.portgroups.join(", ") : <span className="muted">—</span>}</div>
              <div>VM ports in use</div><div>{s.vm_ports}</div>
            </div>
          </Panel>
        ))}
      </div>
    </>
  );
}
