import { ReactNode, useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, enc } from "../api";
import { TimeChart } from "../chart";
import { Icon } from "../icons";
import { EventRec, useLive } from "../live";
import { useSession } from "../session";
import { Btn, Confirm, DataTable, Dropdown, ErrorBox, Field, Loading, Modal, Panel, Tabs, useAction, useAsync, VmState } from "../ui";
import { bps, bytes, mib, OS_LABEL, pct, ts } from "../util";
import { CloneDialog, DeleteDialog, openConsole, useVmActions } from "./vmActions";

type Hist = { t: number; cpu: number; mem: number | null; rx: number; tx: number; rd: number; wr: number };

const TABS: [string, string][] = [["summary", "Summary"], ["monitor", "Monitor"], ["configure", "Configure"],
  ["datastores", "Datastores"], ["networks", "Networks"], ["snapshots", "Snapshots"]];

export function VMDetail() {
  const { uuid = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const { vmVersion, vmStats } = useLive();
  const { data: vm, error, reload } = useAsync(() => api.get(`/api/v1/vms/${uuid}`), [uuid, vmVersion]);
  const tab = TABS.some(([k]) => k === params.get("tab")) ? params.get("tab")! : "summary";
  const setTab = (t: string) => setParams(t === "summary" ? {} : { tab: t }, { replace: true });
  const [dlg, setDlg] = useState<null | "clone" | "template" | "deploy" | "delete" | "snapshot" | "notes" | "export">(null);
  const [edit, setEdit] = useState(params.get("edit") === "1");
  const [hist, setHist] = useState<Hist[]>([]);
  const st = vmStats[uuid];
  useEffect(() => {
    if (st) setHist((h) => [...h.slice(-299), { t: Date.now() / 1000, cpu: st.cpu_pct, mem: st.mem_guest_used ?? null, rx: st.net_rx, tx: st.net_tx, rd: st.disk_read ?? 0, wr: st.disk_write ?? 0 }]);
  }, [st]);
  useEffect(() => {
    setHist([]);
    api.get<any[]>(`/api/v1/vms/${uuid}/metrics`).then((xs) => setHist((h) => h.length > xs.length ? h : xs.slice(-300).map((x) => ({
      t: x.t, cpu: x.cpu_pct, mem: x.mem_guest_used ?? null, rx: x.net_rx, tx: x.net_tx, rd: x.disk_read ?? 0, wr: x.disk_write ?? 0,
    })))).catch(() => {});
  }, [uuid]);

  if (error) return <div className="page"><ErrorBox error={error} onRetry={reload} /></div>;
  if (!vm) return <Loading />;
  const closeEdit = () => { setEdit(false); if (params.get("edit")) setParams({}); reload(); };
  return (
    <div className="page">
      <VmHeader vm={vm} onChanged={reload} onDialog={setDlg} onEdit={() => setEdit(true)} />
      {vm.pending_restart && <div className="alert warn"><Icon name="warn" /><div>Configuration changes are pending: {vm.config.vcpus} vCPU / {mib(vm.config.memory_mib)} take effect at the next power on.</div></div>}
      <Tabs tabs={TABS.map(([k, l]) => [k, k === "snapshots" && vm.snapshots.length ? `${l} (${vm.snapshots.length})` : l])} value={tab} onChange={setTab} />

      {tab === "summary" && <Summary vm={vm} st={st} onTab={setTab} onEdit={() => setEdit(true)} onNotes={() => setDlg("notes")} />}
      {tab === "monitor" && <Monitor vm={vm} hist={hist} st={st} />}
      {tab === "configure" && <Configure vm={vm} onEdit={() => setEdit(true)} />}
      {tab === "datastores" && <VmDatastores vm={vm} />}
      {tab === "networks" && <VmNetworks vm={vm} />}
      {tab === "snapshots" && <Snapshots vm={vm} onChanged={reload} onTake={() => setDlg("snapshot")} />}

      {edit && <EditVM vm={vm} onClose={closeEdit} />}
      {(dlg === "clone" || dlg === "template" || dlg === "deploy") && <CloneDialog vm={vm} mode={dlg} onClose={() => setDlg(null)} />}
      {dlg === "delete" && <DeleteDialog vm={vm} onClose={() => setDlg(null)} />}
      {dlg === "snapshot" && <SnapshotDialog vm={vm} onClose={() => { setDlg(null); }} />}
      {dlg === "notes" && <NotesDialog vm={vm} onClose={() => { setDlg(null); reload(); }} />}
      {dlg === "export" && <ExportDialog vm={vm} onClose={() => setDlg(null)} />}
    </div>
  );
}

/* ------------------------------------------------------------------ header (vSphere style) */
function VmHeader({ vm, onChanged, onDialog, onEdit }: { vm: any; onChanged: () => void; onDialog: (d: any) => void; onEdit: () => void }) {
  const { can } = useSession();
  const { power } = useVmActions(onChanged);
  const { run } = useAction();
  const [confirm, setConfirm] = useState<null | "poweroff" | "reset">(null);
  const s = vm.state;
  const p = can("vm.power") && !vm.template;
  const off = ["shutoff", "crashed", "suspended"].includes(s);
  const on = s === "running";
  const live = on || s === "paused";
  const m = can("vm.manage");
  const uuid = vm.uuid;
  const toolsMounted = vm.hardware.cdroms.some((c: any) => (c.path || c.iso || "").includes("lsx-ve-guest-tools"));
  return (
    <div className="vm-head">
      <span className="vm-head-icon"><Icon name={vm.template ? "template" : "vm"} size={22} /></span>
      <h1>{vm.name}</h1>
      {vm.template && <span className="badge">template</span>}
      <span className="vbar" />
      <Btn icon="play" title="Power on" disabled={!p || !(off || s === "paused")} onClick={() => power(vm, s === "paused" ? "resume" : "start")} />
      <Btn icon="stop" title="Power off" disabled={!p || !(live || s === "shutting-down" || s === "crashed")} onClick={() => setConfirm("poweroff")} />
      <Btn icon="pause" title="Suspend (pause)" disabled={!p || !on} onClick={() => power(vm, "suspend")} />
      <Btn icon="console" title="Launch web console" disabled={!can("vm.console") || !live || !vm.console.available} onClick={() => openConsole(uuid)} />
      <Btn icon="snapshot" title="Take snapshot" disabled={!can("vm.snapshot") || vm.template} onClick={() => onDialog("snapshot")} />
      <Btn icon="edit" title="Edit settings" disabled={!m} onClick={onEdit} />
      <span className="vbar" />
      <Dropdown label="Actions" icon="more" items={[
        { label: "Power on", icon: "play", disabled: !p || !off, onClick: () => power(vm, "start") },
        { label: "Power off", icon: "stop", disabled: !p || !live, onClick: () => setConfirm("poweroff") },
        { label: "Suspend", icon: "pause", disabled: !p || !on, onClick: () => power(vm, "suspend") },
        { label: "Resume", icon: "play", disabled: !p || s !== "paused", onClick: () => power(vm, "resume") },
        { label: "Reset", icon: "restart", disabled: !p || !on, onClick: () => setConfirm("reset") },
        "sep",
        { label: "Shut down guest OS", icon: "power", disabled: !p || !on, onClick: () => power(vm, "shutdown") },
        { label: "Restart guest OS", icon: "restart", disabled: !p || !on, onClick: () => power(vm, "reboot") },
        "sep",
        { label: "Open web console", icon: "console", disabled: !can("vm.console") || !live || !vm.console.available, onClick: () => openConsole(uuid) },
        { label: "Take snapshot…", icon: "snapshot", disabled: !can("vm.snapshot") || vm.template, onClick: () => onDialog("snapshot") },
        { label: "Edit settings…", icon: "edit", disabled: !m, onClick: onEdit },
        { label: "Edit notes…", icon: "log", disabled: !m, onClick: () => onDialog("notes") },
        "sep",
        { label: toolsMounted ? "Unmount LSX-VE Guest Tools" : "Install LSX-VE Guest Tools…", icon: "iso", disabled: !m || vm.template,
          onClick: () => toolsMounted
            ? run(() => api.del(`/api/v1/vms/${uuid}/guest-tools`), "Guest Tools unmounted").then(onChanged)
            : run(() => api.post(`/api/v1/vms/${uuid}/guest-tools`), "Guest Tools ISO mounted — run the installer from the CD drive in the guest").then(onChanged) },
        { label: "Export to OVA…", icon: "upload", disabled: !m || s !== "shutoff", onClick: () => onDialog("export") },
        { label: vm.autostart ? "Disable autostart" : "Enable autostart", icon: "power", disabled: !m || vm.template,
          onClick: () => run(() => api.put(`/api/v1/vms/${uuid}/autostart`, { enabled: !vm.autostart }), "Autostart updated").then(onChanged) },
        "sep",
        ...(vm.template ? [
          { label: "Deploy VM from template…", icon: "template", disabled: !m, onClick: () => onDialog("deploy") },
          { label: "Convert to virtual machine", icon: "vm", disabled: !m, onClick: () => run(() => api.put(`/api/v1/vms/${uuid}/template`, { template: false }), "Converted").then(onChanged) },
        ] : [
          { label: "Clone…", icon: "clone", disabled: !m || s !== "shutoff", onClick: () => onDialog("clone") },
          { label: "Clone to template…", icon: "template", disabled: !m || s !== "shutoff", onClick: () => onDialog("template") },
          { label: "Convert to template", icon: "template", disabled: !m || s !== "shutoff", onClick: () => run(() => api.put(`/api/v1/vms/${uuid}/template`, { template: true }), "Converted to template").then(onChanged) },
        ]),
        "sep",
        { label: "Delete from disk…", icon: "trash", danger: true, disabled: !m || s !== "shutoff", onClick: () => onDialog("delete") },
      ]} />
      <span className="grow" />
      <Btn icon="refresh" title="Refresh" onClick={onChanged} />
      {confirm && (
        <Confirm danger title={confirm === "poweroff" ? `Power off ${vm.name}` : `Reset ${vm.name}`}
          message={confirm === "poweroff" ? "This immediately cuts power, like pulling the power cord. Unsaved guest data may be lost. Use Shut down guest OS for a graceful shutdown." : "This immediately resets the virtual machine without shutting down the guest OS."}
          confirmLabel={confirm === "poweroff" ? "Power off" : "Reset"} onClose={() => setConfirm(null)} onConfirm={() => power(vm, confirm)} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ summary tiles */
function Tile({ title, children, footer, className, right }: { title: string; children: ReactNode; footer?: ReactNode; className?: string; right?: ReactNode }) {
  return (
    <section className={`tile ${className ?? ""}`}>
      <div className="tile-h"><h3>{title}</h3>{right}</div>
      <div className="tile-b">{children}</div>
      {footer && <div className="tile-f">{footer}</div>}
    </section>
  );
}

function TileLink({ children, onClick, disabled }: { children: ReactNode; onClick: () => void; disabled?: boolean }) {
  return <button type="button" className="tile-link" disabled={disabled} onClick={onClick}>{children}</button>;
}

const gb = (n: number | null | undefined) => (n == null ? "—" : bytes(n));
const mhz = (n: number) => (n >= 10000 ? `${(n / 1000).toFixed(2)} GHz` : `${Math.round(n)} MHz`);

function agentLabel(g: any): [string, string] {
  if (!g || g.agent == null) return ["Not configured", "shutoff"];
  if (g.agent === "connected") return ["Running", "ok"];
  if (g.agent === "configured") return ["Not running (VM powered off)", "shutoff"];
  return ["Not running", "warning"];
}

function Summary({ vm, st, onTab, onEdit, onNotes }: { vm: any; st: any; onTab: (t: string) => void; onEdit: () => void; onNotes: () => void }) {
  const { can } = useSession();
  const hw = vm.hardware;
  const live = vm.state === "running" || vm.state === "paused";
  const [updated, setUpdated] = useState(new Date());
  useEffect(() => setUpdated(new Date()), [st]);
  const cpuMhz = st && vm.host?.cpu_mhz ? (st.cpu_pct / 100) * vm.vcpus * vm.host.cpu_mhz : null;
  const used = hw.disks.reduce((a: number, d: any) => a + (d.allocation ?? 0), 0);
  const prov = hw.disks.reduce((a: number, d: any) => a + (d.capacity ?? 0), 0);
  const [agent, agentCls] = agentLabel(vm.guest);
  const ips: any[] = vm.guest_ips ?? [];
  const datastores = Array.from(new Set(hw.disks.map((d: any) => d.datastore).filter(Boolean))) as string[];
  const networks = Array.from(new Set(hw.nics.map((n: any) => n.source).filter(Boolean))) as string[];
  const curSnap = vm.snapshots.find((s: any) => s.current);
  const lastSnap = [...vm.snapshots].sort((a: any, b: any) => b.created - a.created)[0];
  return (
    <div className="tiles">
      <Tile title="Guest OS" className="t-console" footer={can("vm.console") && <>
        <Btn kind="primary" icon="console" disabled={!live || !vm.console.available} onClick={() => openConsole(vm.uuid)}>Launch web console</Btn>
      </>}>
        {live ? <ScreenThumb uuid={vm.uuid} onOpen={() => can("vm.console") && vm.console.available && openConsole(vm.uuid)} />
          : <div className="thumb off"><Icon name="power" size={22} /><span>Powered off</span></div>}
      </Tile>

      <Tile title="Virtual Machine Details" className="span2">
        <div className="details">
          <div className="details-icon"><Icon name={vm.template ? "template" : "vm"} size={54} /></div>
          <div className="kv">
            <div>Power status</div><div><VmState state={vm.state} /></div>
            <div>Guest OS</div><div>{vm.os_variant || OS_LABEL[vm.os_type]}{vm.guest?.os_name && vm.guest.os_name !== vm.os_variant && <div className="small muted">guest reports: {vm.guest.os_name}{vm.guest.kernel ? ` (kernel ${vm.guest.kernel})` : ""}</div>}</div>
            <div>QEMU guest agent</div><div><span className={`status ${agentCls}`}>{agent}</span>{vm.guest?.agent && vm.guest.agent !== "connected" && vm.guest.agent !== "configured" && <div className="small muted">Install LSX-VE Guest Tools (Actions menu) for VirtIO drivers, hostname, IPs and clean shutdown</div>}</div>
            <div>DNS name</div><div className="mono">{vm.guest?.hostname || <span className="muted">—</span>}</div>
            <div>IP addresses{ips.length > 0 ? ` (${ips.length})` : ""}</div>
            <div className="mono">
              {ips.length ? ips.slice(0, 6).map((a, i) => <div key={i} className={a.link_local ? "muted" : ""}>{a.address}</div>) : <span className="muted">{live ? "Not reported yet" : "—"}</span>}
              {ips.length > 6 && <div className="muted small">+{ips.length - 6} more (Networks tab)</div>}
              {vm.guest_ip_source === "arp" && <div className="small muted" style={{ fontFamily: "var(--sans)" }}>from the host ARP table — install the guest agent for exact data</div>}
            </div>
            <div>Autostart</div><div>{vm.template ? "—" : vm.autostart ? `Enabled${vm.autostart_cfg?.order ? ` (start order ${vm.autostart_cfg.order})` : " (powers on with the host)"}` : "Disabled"}</div>
          </div>
        </div>
      </Tile>

      <Tile title="Usage" right={<span className="small muted">{live ? `Last updated: ${updated.toLocaleTimeString()}` : ""}</span>}
        footer={<TileLink onClick={() => onTab("monitor")}>View stats</TileLink>}>
        <div className="usage">
          <div className="u-label">CPU</div>
          <div className="u-big"><Icon name="cpu" size={20} />{cpuMhz != null ? mhz(cpuMhz) : "0 MHz"}<small>used</small></div>
          <div className="u-label">Memory</div>
          <div className="u-big"><Icon name="memory" size={20} />{st?.mem_guest_used ? bytes(st.mem_guest_used) : live ? mib(vm.memory_mib) : "0 B"}<small>{st?.mem_guest_used ? "used by guest" : live ? "assigned" : "used"}</small></div>
          <div className="u-label">Storage</div>
          <div className="u-big"><Icon name="disk" size={20} />{bytes(used)}<small>used of {bytes(prov)}</small></div>
        </div>
      </Tile>

      <Tile title="VM Hardware" className="span2" footer={can("vm.manage") && <TileLink onClick={onEdit}>Edit</TileLink>}>
        <div className="kv">
          <div>CPU</div><div>{vm.vcpus} CPU(s){cpuMhz != null ? `, ${mhz(cpuMhz)} used` : ""}</div>
          <div>Memory</div><div>{mib(vm.memory_mib)}{st?.mem_guest_used ? `, ${bytes(st.mem_guest_used)} memory active` : ""}</div>
          {hw.disks.map((d: any, i: number) => <HwRow key={d.target} label={`Hard disk ${i + 1}`}>
            {gb(d.capacity)} | {d.format || "raw"} · {d.bus} · {gb(d.allocation)} allocated{" "}
            {d.datastore ? <Link to="/storage">{d.datastore}</Link> : <span className="muted">external</span>}
          </HwRow>)}
          {hw.nics.map((n: any, i: number) => <HwRow key={n.mac} label={`Network adapter ${i + 1}`}>
            <Link to="/networking">{n.source}</Link> ({n.link === "up" ? "connected" : "disconnected"}) | <span className="mono">{n.mac}</span> · {n.model}
          </HwRow>)}
          {hw.cdroms.map((c: any, i: number) => <HwRow key={c.target} label={`CD/DVD drive ${i + 1}`}>{(c.path || "").includes("lsx-ve-guest-tools") ? "LSX-VE Guest Tools" : c.iso ?? <span className="muted">empty</span>}</HwRow>)}
          {(vm.hostdevs ?? []).map((h: any, i: number) => <HwRow key={h.id} label={`${h.type === "pci" ? "PCI" : "USB"} device ${i + 1}`}><span className="mono">{h.type === "pci" ? h.address : `${h.vendor}:${h.product}`}</span> (passthrough)</HwRow>)}
          {(vm.hardware.usb_controllers ?? []).map((u: string, i: number) => <HwRow key={`usb${i}`} label="USB controller">{u === "qemu-xhci" || u === "nec-xhci" ? "USB 3.1 (xHCI)" : u.includes("ehci") ? "USB 2.0 (EHCI)" : u}</HwRow>)}
          {(vm.hardware.serials ?? []).filter((x: any) => !x.console).map((x: any) => <HwRow key={`com${x.port}`} label={`Serial port ${x.port + 1}`}><span className="mono">{x.path ?? x.type}</span></HwRow>)}
          <div>Video card</div><div>{vm.resources?.video?.model === "virtio" || !vm.resources?.video?.model ? "VirtIO GPU" : `${vm.resources.video.model.toUpperCase()}${vm.resources.video.vram_mib ? `, ${vm.resources.video.vram_mib} MiB` : ""}`}</div>
          {(vm.resources?.cpu_quota_us > 0 || vm.resources?.mem_locked || vm.resources?.cpu_hotadd || vm.resources?.mem_hotadd) && <><div>Resources</div><div>
            {[vm.resources.cpu_quota_us > 0 && vm.host?.cpu_mhz ? `CPU limit ${mhz(vm.resources.cpu_quota_us / 100000 * vm.host.cpu_mhz)}` : null,
              vm.resources.mem_locked ? "all memory reserved" : null, vm.resources.cpu_hotadd ? `CPU hot add (max ${vm.resources.vcpus_max})` : null,
              vm.resources.mem_hotadd ? `memory hot add (max ${mib(vm.resources.mem_max_mib)})` : null].filter(Boolean).join(" · ")}
          </div></>}
          <div>Firmware</div><div>{hw.firmware.toUpperCase()}{hw.secure_boot ? " · Secure Boot" : ""}{hw.tpm ? " · vTPM 2.0" : ""}</div>
          <div>Compatibility</div><div>{hw.machine} ({hw.domain_type === "kvm" ? "KVM accelerated" : "software emulation (TCG)"})</div>
        </div>
      </Tile>

      <Tile title="Related Objects">
        <div className="related">
          <h4>Host</h4>
          <Link to="/"><Icon name="host" size={14} />{vm.host?.hostname ?? "this host"}</Link>
          <h4>Networks</h4>
          {networks.length ? networks.map((n) => <Link key={n} to="/networking"><Icon name="network" size={14} />{n}</Link>) : <span className="muted">none</span>}
          <h4>Storage</h4>
          {datastores.length ? datastores.map((d) => <Link key={d} to="/storage"><Icon name="storage" size={14} />{d}</Link>) : <span className="muted">none</span>}
        </div>
      </Tile>

      <Tile title="Notes" footer={can("vm.manage") && <TileLink onClick={onNotes}>Edit</TileLink>}>
        {vm.description ? <div className="notes">{vm.description}</div> : <div className="tile-empty"><Icon name="edit" size={30} /><span>No notes assigned</span></div>}
      </Tile>

      <Tile title="Snapshots" footer={<TileLink onClick={() => onTab("snapshots")}>Manage</TileLink>}>
        {vm.snapshots.length === 0 ? <div className="tile-empty"><Icon name="snapshot" size={30} /><span>No snapshot taken</span></div> : (
          <div className="kv">
            <div>Snapshots</div><div>{vm.snapshots.length}</div>
            <div>Current</div><div>{curSnap?.name ?? "—"}</div>
            <div>Latest</div><div>{lastSnap.name}<div className="small muted">{ts(lastSnap.created)}</div></div>
          </div>
        )}
      </Tile>

      <Tile title="General">
        <div className="kv">
          <div>UUID</div><div className="mono small">{vm.uuid}</div>
          <div>Boot order</div><div>{hw.boot_order.join(" → ") || "default"}</div>
          <div>Created by</div><div>{vm.created_by || <span className="muted">—</span>}</div>
          {vm.guest?.timezone && <><div>Guest time zone</div><div>{vm.guest.timezone}</div></>}
        </div>
      </Tile>
    </div>
  );
}

function HwRow({ label, children }: { label: string; children: ReactNode }) {
  return <><div>{label}</div><div>{children}</div></>;
}

function ScreenThumb({ uuid, onOpen }: { uuid: string; onOpen?: () => void }) {
  const [n, setN] = useState(0);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const t = setInterval(() => setN((x) => x + 1), 10000);
    return () => clearInterval(t);
  }, []);
  if (failed) return <div className="thumb off"><span>Preview unavailable</span></div>;
  return <img className="thumb clickable" title="Open web console" alt="Console preview" src={`/api/v1/vms/${uuid}/screenshot?n=${n}`} onClick={onOpen} onError={() => setFailed(true)} />;
}

/* ------------------------------------------------------------------ monitor */
function Monitor({ vm, hist, st }: { vm: any; hist: Hist[]; st: any }) {
  const t = hist.map((h) => h.t);
  const live = vm.state === "running" || vm.state === "paused";
  return (
    <div className="grid">
      {!live && <div className="alert info"><Icon name="info" /><div>The virtual machine is powered off; live performance charts appear while it runs.</div></div>}
      <div className="grid c2">
        <Panel title="CPU" icon="cpu" right={st ? <span className="mono">{pct(st.cpu_pct, 1)} of {vm.vcpus} vCPU</span> : null}>
          <TimeChart times={t} series={[{ label: "CPU", color: "var(--chart-1)", values: hist.map((h) => h.cpu) }]} max={100} fmt={(n) => `${n.toFixed(0)}%`} height={150} />
        </Panel>
        <Panel title="Memory (guest used)" icon="memory" right={st?.mem_guest_used ? <span className="mono">{bytes(st.mem_guest_used)} / {mib(vm.memory_mib)}</span> : <span className="small muted">needs the virtio balloon driver in the guest</span>}>
          <TimeChart times={t} series={[{ label: "Used", color: "var(--chart-4)", values: hist.map((h) => h.mem ?? 0) }]} max={vm.memory_mib * 1048576} fmt={(n) => bytes(n, 0)} height={150} />
        </Panel>
        <Panel title="Network" icon="nic" right={st ? <span className="mono">↓{bps(st.net_rx)} ↑{bps(st.net_tx)}</span> : null}>
          <TimeChart times={t} series={[{ label: "Rx", color: "var(--chart-2)", values: hist.map((h) => h.rx) }, { label: "Tx", color: "var(--chart-3)", values: hist.map((h) => h.tx) }]} fmt={(n) => bytes(n, 0) + "/s"} height={150} />
        </Panel>
        <Panel title="Disk I/O" icon="disk" right={st ? <span className="mono">R {bps(st.disk_read)} W {bps(st.disk_write)}</span> : null}>
          <TimeChart times={t} series={[{ label: "Read", color: "var(--chart-2)", values: hist.map((h) => h.rd) }, { label: "Write", color: "var(--chart-3)", values: hist.map((h) => h.wr) }]} fmt={(n) => bytes(n, 0) + "/s"} height={150} />
        </Panel>
      </div>
      <VmEvents uuid={vm.uuid} />
    </div>
  );
}

/* ------------------------------------------------------------------ configure / datastores / networks */
function Configure({ vm, onEdit }: { vm: any; onEdit: () => void }) {
  const { can } = useSession();
  const hw = vm.hardware;
  return (
    <div className="grid">
      <div className="toolbar"><Btn icon="edit" disabled={!can("vm.manage")} onClick={onEdit}>Edit settings</Btn></div>
      <div className="grid c2">
        <Panel title="VM options" icon="manage">
          <div className="kv">
            <div>Name</div><div>{vm.name}</div>
            <div>Guest OS</div><div>{vm.os_variant || OS_LABEL[vm.os_type]} <span className="muted">({OS_LABEL[vm.os_type] ?? vm.os_type})</span></div>
            <div>Firmware</div><div>{hw.firmware.toUpperCase()}{hw.secure_boot ? " · Secure Boot" : ""}</div>
            <div>vTPM</div><div>{hw.tpm ? "TPM 2.0 (emulated)" : "None"}</div>
            <div>Boot order</div><div>{hw.boot_order.join(" → ") || "default"}</div>
            <div>Autostart</div><div>{vm.autostart ? "Enabled" : "Disabled"}</div>
            <div>QEMU guest agent channel</div><div>{vm.guest?.agent == null ? "Not present" : "Present"}</div>
            <div>Machine type</div><div className="mono">{hw.machine}</div>
            <div>Execution</div><div>{hw.domain_type === "kvm" ? "KVM (hardware assisted)" : "QEMU TCG (software emulation)"}</div>
            <div>UUID</div><div className="mono small">{vm.uuid}</div>
          </div>
        </Panel>
        <Panel title="CPU and memory" icon="cpu">
          <div className="kv">
            <div>vCPUs</div><div>{vm.vcpus}{vm.config.vcpus !== vm.vcpus ? ` (${vm.config.vcpus} after restart)` : ""}</div>
            <div>CPU model</div><div>{hw.domain_type === "kvm" ? `host-passthrough (${vm.host?.cpu_model ?? "host CPU"})` : "emulated"}</div>
            <div>Memory</div><div>{mib(vm.memory_mib)}{vm.config.memory_mib !== vm.memory_mib ? ` (${mib(vm.config.memory_mib)} after restart)` : ""}</div>
            <div>Console</div><div>{(vm.console.type ?? "none").toUpperCase()} via web console</div>
          </div>
        </Panel>
      </div>
      <Panel title="Hard disks" icon="disk" flush>
        <DataTable rowKey={(r: any) => r.target} rows={hw.disks} empty="No hard disks." cols={[
          { key: "target", label: "Device", render: (r: any) => <span className="mono">{r.target}</span> },
          { key: "bus", label: "Controller" },
          { key: "capacity", label: "Capacity", num: true, render: (r: any) => bytes(r.capacity) },
          { key: "allocation", label: "Allocated", num: true, render: (r: any) => bytes(r.allocation) },
          { key: "format", label: "Format" },
          { key: "datastore", label: "Datastore", render: (r: any) => r.datastore ?? <span className="muted">external</span> },
          { key: "path", label: "Path", render: (r: any) => <span className="mono small">{r.path}</span> },
        ]} />
      </Panel>
      <div className="grid c2">
        <Panel title="CD/DVD drives" icon="iso" flush>
          <DataTable rowKey={(r: any) => r.target} rows={hw.cdroms} empty="No CD/DVD drive." cols={[
            { key: "target", label: "Device", render: (r: any) => <span className="mono">{r.target}</span> },
            { key: "iso", label: "Media", render: (r: any) => r.iso ?? <span className="muted">empty</span> },
          ]} />
        </Panel>
        <Panel title="Network adapters" icon="nic" flush><NicTable vm={vm} /></Panel>
      </div>
    </div>
  );
}

function NicTable({ vm }: { vm: any }) {
  return (
    <DataTable rowKey={(r: any) => r.mac} rows={vm.hardware.nics} empty="No network adapters." cols={[
      { key: "mac", label: "MAC", render: (r: any) => <span className="mono">{r.mac}</span> },
      { key: "source", label: "Connected to", render: (r: any) => `${r.source} (${r.type === "network" ? "virtual network" : "bridge"})` },
      { key: "model", label: "Type" },
      { key: "link", label: "Link", render: (r: any) => <span className={`status ${r.link === "up" ? "ok" : "shutoff"}`}>{r.link === "up" ? "connected" : "disconnected"}</span> },
      { key: "ips", label: "Guest IP addresses", render: (r: any) => <span className="mono small">{(vm.guest_ips ?? []).filter((a: any) => a.mac && r.mac && a.mac.toLowerCase() === r.mac.toLowerCase()).map((a: any) => a.address).join(", ") || "—"}</span> },
      { key: "device", label: "Host device", render: (r: any) => <span className="mono">{r.device ?? "—"}</span> },
    ]} />
  );
}

function VmDatastores({ vm }: { vm: any }) {
  const rows = Object.values(vm.hardware.disks.reduce((acc: Record<string, any>, d: any) => {
    const k = d.datastore ?? "(external)";
    acc[k] = acc[k] ?? { name: k, disks: [], capacity: 0, allocation: 0 };
    acc[k].disks.push(d.target); acc[k].capacity += d.capacity ?? 0; acc[k].allocation += d.allocation ?? 0;
    return acc;
  }, {}));
  return (
    <Panel flush>
      <DataTable rowKey={(r: any) => r.name} rows={rows} empty="This virtual machine uses no datastore." cols={[
        { key: "name", label: "Datastore", render: (r: any) => r.name === "(external)" ? <span className="muted">external path</span> : <Link to="/storage"><Icon name="storage" size={13} /> {r.name}</Link> },
        { key: "disks", label: "Virtual disks", render: (r: any) => <span className="mono">{r.disks.join(", ")}</span> },
        { key: "capacity", label: "Provisioned", num: true, render: (r: any) => bytes(r.capacity) },
        { key: "allocation", label: "Used", num: true, render: (r: any) => bytes(r.allocation) },
      ]} />
    </Panel>
  );
}

function VmNetworks({ vm }: { vm: any }) {
  const ips: any[] = vm.guest_ips ?? [];
  return (
    <div className="grid">
      <Panel title="Network adapters" icon="nic" flush><NicTable vm={vm} /></Panel>
      <Panel title="Guest IP addresses" icon="network" right={vm.guest_ip_source ? <span className="small muted">source: {{ agent: "QEMU guest agent", lease: "DHCP lease", arp: "host ARP table" }[vm.guest_ip_source as string]}</span> : null} flush>
        <DataTable rowKey={(r: any) => `${r.interface}-${r.address}`} rows={ips} empty={vm.state === "running" ? "No addresses reported. Install qemu-guest-agent in the guest, or wait until the guest has sent traffic (ARP)." : "The virtual machine is not running."} cols={[
          { key: "interface", label: "Interface", render: (r: any) => <span className="mono">{r.interface}</span> },
          { key: "address", label: "Address", render: (r: any) => <span className="mono">{r.address}{r.prefix != null ? `/${r.prefix}` : ""}</span> },
          { key: "mac", label: "MAC", render: (r: any) => <span className="mono">{r.mac ?? "—"}</span> },
        ]} />
      </Panel>
    </div>
  );
}

function NotesDialog({ vm, onClose }: { vm: any; onClose: () => void }) {
  const [text, setText] = useState(vm.description ?? "");
  const { run, busy } = useAction();
  return (
    <Modal title={`Edit notes — ${vm.name}`} icon="edit" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || text === (vm.description ?? "")}
        onClick={() => run(() => api.patch(`/api/v1/vms/${vm.uuid}`, { description: text }), "Notes saved").then((r) => r && onClose())}>Save</Btn></>}>
      <Field label="Notes" help={`${text.length} / 1000 characters`}><textarea autoFocus maxLength={1000} rows={8} value={text} onChange={(e) => setText(e.target.value)} style={{ fontFamily: "var(--sans)" }} /></Field>
    </Modal>
  );
}

function SnapshotDialog({ vm, onClose }: { vm: any; onClose: () => void }) {
  const [name, setName] = useState(`snap-${new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "")}`);
  const [desc, setDesc] = useState("");
  const { run, busy } = useAction();
  const nonQcow = vm.hardware.disks.filter((d: any) => d.format !== "qcow2");
  return (
    <Modal title={`Take snapshot of ${vm.name}`} icon="snapshot" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || !name || nonQcow.length > 0}
        onClick={() => run(() => api.post(`/api/v1/vms/${vm.uuid}/snapshots`, { name, description: desc }), "Snapshot task submitted").then((r) => r && onClose())}>Take snapshot</Btn></>}>
      <div className="form">
        {nonQcow.length > 0 && <div className="alert err"><Icon name="error" /><div>Snapshots need qcow2 disks. Not supported for: {nonQcow.map((d: any) => d.target).join(", ")}.</div></div>}
        {vm.hardware.firmware === "uefi" && <div className="alert info"><Icon name="info" /><div>For UEFI virtual machines the hypervisor may only accept snapshots while the VM is powered off; if it refuses, the task reports the reason.</div></div>}
        <div className="small muted">{vm.state === "running" ? "The snapshot includes the memory state of the running VM (internal qcow2 snapshot). The VM is briefly paused." : "Disk state snapshot (internal qcow2 snapshot)."}</div>
        <Field label="Name"><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Description"><textarea value={desc} onChange={(e) => setDesc(e.target.value)} style={{ fontFamily: "var(--sans)" }} /></Field>
      </div>
    </Modal>
  );
}

function Snapshots({ vm, onChanged, onTake }: { vm: any; onChanged: () => void; onTake: () => void }) {
  const { can } = useSession();
  const [sel, setSel] = useState<string[]>([]);
  const [confirm, setConfirm] = useState<null | "revert" | "delete">(null);
  const { run } = useAction();
  const cur = vm.snapshots.find((s: any) => s.name === sel[0]);
  return (
    <>
      <div className="toolbar">
        <Btn icon="snapshot" disabled={!can("vm.snapshot") || vm.template} onClick={onTake}>Take snapshot</Btn>
        <Btn icon="restart" disabled={!can("vm.snapshot") || !cur} onClick={() => setConfirm("revert")}>Restore snapshot</Btn>
        <Btn icon="trash" kind="danger" disabled={!can("vm.snapshot") || !cur} onClick={() => setConfirm("delete")}>Delete snapshot</Btn>
      </div>
      <Panel flush>
        <DataTable rowKey={(r: any) => r.name} rows={vm.snapshots} selected={sel} onSelect={setSel} empty="This virtual machine has no snapshots."
          cols={[
            { key: "name", label: "Snapshot", render: (r: any) => <><b>{r.name}</b> {r.current && <span className="badge blue">you are here</span>}</> },
            { key: "created", label: "Created", sort: (r: any) => r.created, render: (r: any) => ts(r.created) },
            { key: "state", label: "VM state at snapshot", render: (r: any) => <VmState state={r.state === "running" ? "running" : "shutoff"} /> },
            { key: "parent", label: "Parent", render: (r: any) => r.parent ?? "—" },
            { key: "description", label: "Description" },
          ]} />
      </Panel>
      {confirm === "revert" && cur && <Confirm danger title={`Restore ${cur.name}`} confirmLabel="Restore" onClose={() => setConfirm(null)}
        message={<>The current state of <b>{vm.name}</b> is discarded and replaced by snapshot <b>{cur.name}</b>. Changes since the snapshot are lost unless you take a new snapshot first.</>}
        onConfirm={() => run(() => api.post(`/api/v1/vms/${vm.uuid}/snapshots/${enc(cur.name)}/revert`), "Restore task submitted").then(onChanged)} />}
      {confirm === "delete" && cur && <Confirm danger title={`Delete snapshot ${cur.name}`} confirmLabel="Delete" onClose={() => setConfirm(null)}
        message="The snapshot is removed and its data merged; the current VM state is not changed."
        onConfirm={() => run(() => api.del(`/api/v1/vms/${vm.uuid}/snapshots/${enc(cur.name)}`), "Delete task submitted").then(() => { setSel([]); onChanged(); })} />}
    </>
  );
}

function VmEvents({ uuid }: { uuid: string }) {
  const { data } = useAsync(() => api.get<EventRec[]>(`/api/v1/events?target=${enc(uuid)}&limit=300`), [uuid]);
  return (
    <Panel title="Events" icon="event" flush>
      <DataTable rowKey={(r: EventRec) => String(r.id)} rows={data ?? []} cols={[
        { key: "ts", label: "Time", render: (r) => ts(r.ts) },
        { key: "severity", label: "Severity", render: (r) => <span className={`status ${r.severity === "info" ? "ok" : r.severity}`}>{r.severity}</span> },
        { key: "message", label: "Description" },
        { key: "username", label: "User" },
      ]} />
    </Panel>
  );
}

/* ------------------------------------------------------------------ edit settings */
function EditVM({ vm, onClose }: { vm: any; onClose: () => void }) {
  const ctx = useAsync(() => Promise.all([
    api.get("/api/v1/hypervisor"), api.get<any[]>("/api/v1/storage/datastores"), api.get<any[]>("/api/v1/storage/isos").catch(() => []),
    api.get<any[]>("/api/v1/network/virtual").catch(() => []), api.get("/api/v1/network/host").catch(() => ({ nics: [] })),
  ]));
  const running = vm.state !== "shutoff";
  const [name, setName] = useState(vm.name);
  const [desc, setDesc] = useState(vm.description ?? "");
  const [osType, setOsType] = useState(vm.os_type);
  const [osVariant, setOsVariant] = useState<string>(vm.os_variant ?? "");
  const [vcpus, setVcpus] = useState(vm.config.vcpus);
  const [mem, setMem] = useState(vm.config.memory_mib);
  const [cdrom, setCdrom] = useState(vm.hardware.cdroms[0]?.iso ?? "");
  const [boot, setBoot] = useState<string[]>(vm.config.boot_order.length ? Array.from(new Set([...vm.config.boot_order, "cdrom", "disk", "network"])) : ["disk", "cdrom", "network"]);
  const [removeDisks, setRemoveDisks] = useState<string[]>([]);
  const [deleteRemoved, setDeleteRemoved] = useState(false);
  const [addDisks, setAddDisks] = useState<any[]>([]);
  const [removeNics, setRemoveNics] = useState<string[]>([]);
  const [nicEdits, setNicEdits] = useState<Record<string, any>>({});
  const [addNics, setAddNics] = useState<any[]>([]);
  const res = vm.resources ?? {};
  const [hotadd, setHotadd] = useState<boolean>(!!res.cpu_hotadd);
  const [shares, setShares] = useState<number>(res.cpu_shares || 1000 * vm.config.vcpus);
  const [limitMhz, setLimitMhz] = useState<number>(0);
  const [memLock, setMemLock] = useState<boolean>(!!res.mem_locked);
  const [memHot, setMemHot] = useState<boolean>(!!res.mem_hotadd);
  const [addSer, setAddSer] = useState<{ datastore: string; file: string }[]>([]);
  const [rmSer, setRmSer] = useState<number[]>([]);
  const serials: any[] = vm.hardware.serials ?? [];
  const [video, setVideo] = useState<string>(res.video?.model || "virtio");
  const [vram, setVram] = useState<number>(res.video?.vram_mib || 16);
  const [sizes, setSizes] = useState<Record<string, number>>({});
  const [limits, setLimits] = useState<Record<string, { iops: number; mbps: number }>>({});
  const [addDevs, setAddDevs] = useState<any[]>([]);
  const [rmDevs, setRmDevs] = useState<string[]>([]);
  const ac = vm.autostart_cfg ?? {};
  const [aus, setAus] = useState<any>({ enabled: !!ac.enabled, order: ac.order || 0, start_delay: ac.start_delay ?? -1, stop_delay: ac.stop_delay ?? -1, stop_action: ac.stop_action || "", wait_heartbeat: !!ac.wait_heartbeat });
  const devs = useAsync(() => api.get("/api/v1/host-devices").catch(() => ({ pci: [], usb: [], iommu: false })));
  const { run, busy } = useAction();
  useEffect(() => {
    const hmhz = ctx.data?.[0]?.host_mhz;
    if (hmhz && res.cpu_quota_us) setLimitMhz(Math.round(res.cpu_quota_us / 100000 * hmhz));
  }, [ctx.data]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!ctx.data) return <Modal title={`Edit ${vm.name}`} onClose={onClose}>{ctx.error ? <ErrorBox error={ctx.error} /> : <Loading />}</Modal>;
  const [hv, pools, isos, vnets, hostnet] = ctx.data;
  const dsList = pools.filter((p: any) => p.active && !p.is_iso_library);
  const bridges = (hostnet.nics ?? []).filter((n: any) => n.kind === "bridge" && !n.name.startsWith("virbr"));
  const save = async () => {
    const b: any = {};
    if (name !== vm.name) b.name = name;
    if (desc !== (vm.description ?? "")) b.description = desc;
    if (osType !== vm.os_type) b.os_type = osType;
    if (osVariant !== (vm.os_variant ?? "")) b.os_variant = osVariant;
    if (vcpus !== vm.config.vcpus) b.vcpus = vcpus;
    if (mem !== vm.config.memory_mib) b.memory_mib = mem;
    if (cdrom !== (vm.hardware.cdroms[0]?.iso ?? "")) b.cdrom = cdrom || null;
    if (JSON.stringify(boot) !== JSON.stringify(vm.config.boot_order)) b.boot_order = boot;
    if (removeDisks.length) { b.remove_disks = removeDisks; b.delete_removed_disks = deleteRemoved; }
    if (addDisks.length) b.add_disks = addDisks;
    if (removeNics.length) b.remove_nics = removeNics;
    const upd = Object.values(nicEdits).filter((n: any) => !removeNics.includes(n.mac));
    if (upd.length) b.update_nics = upd;
    if (addNics.length) b.add_nics = addNics;
    if (hotadd !== !!res.cpu_hotadd) b.cpu_hotadd = hotadd;
    if (shares !== (res.cpu_shares || 1000 * vm.config.vcpus)) b.cpu_shares = shares;
    const oldLimit = res.cpu_quota_us && hv.host_mhz ? Math.round(res.cpu_quota_us / 100000 * hv.host_mhz) : 0;
    if (limitMhz !== oldLimit) b.cpu_limit_mhz = limitMhz;
    if (memLock !== !!res.mem_locked) b.mem_lock = memLock;
    if (memHot !== !!res.mem_hotadd) b.mem_hotadd = memHot;
    if (addSer.length) b.add_serials = addSer;
    if (rmSer.length) b.remove_serials = rmSer;
    if (video !== (res.video?.model || "virtio")) b.video_model = video;
    if (video !== "virtio" && vram !== (res.video?.vram_mib || 16)) b.video_vram_mib = vram;
    const rz = Object.entries(sizes).filter(([t, g]) => { const d = vm.hardware.disks.find((x: any) => x.target === t); return d && g > Math.ceil(d.capacity / 1073741824); });
    if (rz.length) b.resize_disks = rz.map(([target, size_gib]) => ({ target, size_gib }));
    const lm = Object.entries(limits);
    if (lm.length) b.disk_limits = lm.map(([target, l]) => ({ target, iops: l.iops || 0, mbps: l.mbps || 0 }));
    if (addDevs.length) b.add_hostdevs = addDevs;
    if (rmDevs.length) b.remove_hostdevs = rmDevs;
    const asChanged = JSON.stringify(aus) !== JSON.stringify({ enabled: !!ac.enabled, order: ac.order || 0, start_delay: ac.start_delay ?? -1, stop_delay: ac.stop_delay ?? -1, stop_action: ac.stop_action || "", wait_heartbeat: !!ac.wait_heartbeat });
    if (asChanged && !vm.template) {
      const ok = await run(() => api.put(`/api/v1/vms/${vm.uuid}/autostart`, aus));
      if (ok === undefined) return;
    }
    if (Object.keys(b).length === 0) return onClose();
    const r = await run(() => api.patch(`/api/v1/vms/${vm.uuid}`, b), "Reconfigure task submitted");
    if (r) onClose();
  };
  const mv = (i: number, d: number) => { const x = [...boot]; const j = i + d; if (j < 0 || j >= x.length) return; [x[i], x[j]] = [x[j], x[i]]; setBoot(x); };
  return (
    <Modal title={`Edit settings — ${vm.name}`} icon="edit" size="wide" onClose={onClose}
      footer={<><span className="left">{running && "VM is running: CPU, memory, boot order and SATA changes apply at the next power on; VirtIO/SCSI disks, NICs and CD media are changed live."}</span>
        <Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy} onClick={save}>Save</Btn></>}>
      <div className="grid c2">
        <Panel title="General">
          <div className="form">
            <Field label="Name" help={running ? "Power off the VM to rename it." : vm.snapshots.length ? "VMs with snapshots cannot be renamed." : undefined}>
              <input type="text" value={name} disabled={running || vm.snapshots.length > 0} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label="Guest operating system" help="Changing the family switches device models for new disks and adapters; existing devices are kept.">
              <select value={(hv.os_catalog ?? []).some((c: any) => c.label === osVariant) ? osVariant : "__custom"}
                onChange={(e) => {
                  if (e.target.value === "__custom") return;
                  const c = (hv.os_catalog ?? []).find((x: any) => x.label === e.target.value);
                  if (c) { setOsVariant(c.label); setOsType(c.family); }
                }}>
                {Object.entries(hv.os_types).map(([fam, famLabel]) => (
                  <optgroup key={fam} label={String(famLabel)}>
                    {(hv.os_catalog ?? []).filter((c: any) => c.family === fam).map((c: any) => <option key={c.id} value={c.label}>{c.label}</option>)}
                  </optgroup>
                ))}
                <option value="__custom">{osVariant && !(hv.os_catalog ?? []).some((c: any) => c.label === osVariant) ? `Other: ${osVariant}` : "Other (custom)"}</option>
              </select>
            </Field>
            <Field label="Notes"><textarea value={desc} onChange={(e) => setDesc(e.target.value)} style={{ fontFamily: "var(--sans)" }} /></Field>
            <div className="kv small"><div>Firmware</div><div>{vm.hardware.firmware.toUpperCase()}{vm.hardware.secure_boot ? " + Secure Boot" : ""} <span className="muted">(fixed at creation)</span></div></div>
          </div>
        </Panel>
        <Panel title="CPU, memory and boot">
          <div className="form">
            <div className="row">
              <Field label="vCPUs" help={running && res.cpu_hotadd ? `CPU hot add: up to ${res.vcpus_max} vCPUs live` : undefined}><input type="number" min={1} max={hv.host_threads} value={vcpus} onChange={(e) => setVcpus(Number(e.target.value))} /></Field>
              <Field label="Memory" help={running && res.mem_hotadd ? `Memory hot add: +128 MiB steps live, up to ${mib(res.mem_max_mib)} (${(res.mem_slots ?? 16) - (res.mem_dimms ?? 0)} of ${res.mem_slots ?? 16} slots free)` : undefined}><div className="unit-input"><input type="number" min={128} step={running && res.mem_hotadd ? 128 : 256} value={mem} onChange={(e) => setMem(Number(e.target.value))} /><span>MiB</span></div></Field>
            </div>
            <Field label="CD/DVD media">
              <select value={cdrom} onChange={(e) => setCdrom(e.target.value)} disabled={vm.hardware.cdroms.length === 0}>
                <option value="">Empty (eject)</option>
                {isos.map((x: any) => <option key={x.name} value={x.name}>{x.name}</option>)}
              </select>
            </Field>
            <Field label="Boot order">
              <ul className="list-reset order-list">{boot.map((b, i) => <li key={b}><span className="badge">{i + 1}</span>{b}<span style={{ flex: 1 }} /><Btn size="sm" disabled={i === 0} onClick={() => mv(i, -1)}>▲</Btn><Btn size="sm" disabled={i === boot.length - 1} onClick={() => mv(i, 1)}>▼</Btn></li>)}</ul>
            </Field>
          </div>
        </Panel>
        <Panel title="Hard disks">
          <div className="form">
            {vm.hardware.disks.map((d: any) => (
              <div key={d.target} style={{ borderBottom: "1px solid var(--border)", paddingBottom: 6 }}>
                <div className="row">
                  <span className="mono" style={{ width: 50 }}>{d.target}</span>
                  <span style={{ flex: 1 }}>{d.bus} · {d.format || "raw"} · {d.datastore ?? "external"}</span>
                  <div className="unit-input" style={{ width: 120 }} title={vm.snapshots.length ? "Disks of a VM with snapshots cannot be extended" : "Extend the disk (cannot shrink)"}>
                    <input type="number" min={Math.ceil(d.capacity / 1073741824)} disabled={vm.snapshots.length > 0 || removeDisks.includes(d.target)}
                      value={sizes[d.target] ?? Math.ceil(d.capacity / 1073741824)} onChange={(e) => setSizes({ ...sizes, [d.target]: Number(e.target.value) })} /><span>GiB</span>
                  </div>
                  <label className="check"><input type="checkbox" checked={removeDisks.includes(d.target)} onChange={(e) => setRemoveDisks(e.target.checked ? [...removeDisks, d.target] : removeDisks.filter((x) => x !== d.target))} /> Remove</label>
                </div>
                <div className="row small" style={{ marginTop: 4 }}>
                  <span className="muted" style={{ width: 50 }} />
                  <span className="muted">Limit</span>
                  <div className="unit-input" style={{ width: 120 }}><input type="number" min={0} value={limits[d.target]?.iops ?? d.iops_limit ?? 0}
                    onChange={(e) => setLimits({ ...limits, [d.target]: { iops: Number(e.target.value), mbps: limits[d.target]?.mbps ?? Math.round((d.bps_limit ?? 0) / 1048576) } })} /><span>IOPS</span></div>
                  <div className="unit-input" style={{ width: 120 }}><input type="number" min={0} value={limits[d.target]?.mbps ?? Math.round((d.bps_limit ?? 0) / 1048576)}
                    onChange={(e) => setLimits({ ...limits, [d.target]: { iops: limits[d.target]?.iops ?? d.iops_limit ?? 0, mbps: Number(e.target.value) } })} /><span>MB/s</span></div>
                  <span className="muted">0 = unlimited</span>
                </div>
              </div>
            ))}
            {Object.keys(sizes).length > 0 && <div className="small muted">Extending a disk does not grow the partition or file system inside the guest — extend them in the guest OS afterwards.</div>}
            {removeDisks.length > 0 && <label className="check" style={{ color: "#ffb4ae" }}><input type="checkbox" checked={deleteRemoved} onChange={(e) => setDeleteRemoved(e.target.checked)} /> Delete removed disk files from the datastore (irreversible)</label>}
            {addDisks.map((d, i) => (
              <div key={i} className="row">
                <select value={d.datastore} onChange={(e) => setAddDisks(addDisks.map((x, j) => j === i ? { ...x, datastore: e.target.value } : x))} style={{ width: 150 }}>{dsList.map((p: any) => <option key={p.name}>{p.name}</option>)}</select>
                <div className="unit-input" style={{ width: 120 }}><input type="number" min={1} value={d.size_gib} onChange={(e) => setAddDisks(addDisks.map((x, j) => j === i ? { ...x, size_gib: Number(e.target.value) } : x))} /><span>GiB</span></div>
                <select value={d.bus} onChange={(e) => setAddDisks(addDisks.map((x, j) => j === i ? { ...x, bus: e.target.value } : x))} style={{ width: 110 }}>{hv.disk_buses.map((b: string) => <option key={b}>{b}</option>)}</select>
                <select value={d.provisioning} onChange={(e) => setAddDisks(addDisks.map((x, j) => j === i ? { ...x, provisioning: e.target.value } : x))} style={{ width: 90 }}><option value="thin">thin</option><option value="thick">thick</option></select>
                <Btn icon="trash" kind="danger" onClick={() => setAddDisks(addDisks.filter((_, j) => j !== i))} />
              </div>
            ))}
            <div><Btn icon="plus" kind="outline" size="sm" onClick={() => setAddDisks([...addDisks, { datastore: dsList[0]?.name ?? "default", size_gib: 16, bus: "virtio", provisioning: "thin" }])}>Add hard disk</Btn></div>
          </div>
        </Panel>
        <Panel title="Network adapters">
          <div className="form">
            {vm.hardware.nics.map((n: any) => {
              const e = nicEdits[n.mac] ?? { mac: n.mac, type: n.type, source: n.source, model: n.model, connected: n.link === "up" };
              const set = (p: any) => setNicEdits({ ...nicEdits, [n.mac]: { ...e, ...p } });
              return (
                <div key={n.mac} className="row">
                  <span className="mono small" style={{ width: 120 }}>{n.mac}</span>
                  <select value={`${e.type}:${e.source}`} onChange={(ev) => { const [t, ...s] = ev.target.value.split(":"); set({ type: t, source: s.join(":") }); }} style={{ flex: 1 }}>
                    {vnets.map((v: any) => <option key={v.name} value={`network:${v.name}`}>{v.name} (virtual network)</option>)}
                    {bridges.map((b: any) => <option key={b.name} value={`bridge:${b.name}`}>{b.name} (bridge)</option>)}
                    {!vnets.find((v: any) => v.name === n.source) && !bridges.find((b: any) => b.name === n.source) && <option value={`${n.type}:${n.source}`}>{n.source}</option>}
                  </select>
                  <label className="check"><input type="checkbox" checked={e.connected} onChange={(ev) => set({ connected: ev.target.checked })} /> Connected</label>
                  <label className="check"><input type="checkbox" checked={removeNics.includes(n.mac)} onChange={(ev) => setRemoveNics(ev.target.checked ? [...removeNics, n.mac] : removeNics.filter((x) => x !== n.mac))} /> Remove</label>
                </div>
              );
            })}
            {addNics.map((n, i) => (
              <div key={i} className="row">
                <select value={`${n.type}:${n.source}`} onChange={(ev) => { const [t, ...s] = ev.target.value.split(":"); setAddNics(addNics.map((x, j) => j === i ? { ...x, type: t, source: s.join(":") } : x)); }} style={{ flex: 1 }}>
                  {vnets.map((v: any) => <option key={v.name} value={`network:${v.name}`}>{v.name} (virtual network)</option>)}
                  {bridges.map((b: any) => <option key={b.name} value={`bridge:${b.name}`}>{b.name} (bridge)</option>)}
                </select>
                <select value={n.model} onChange={(ev) => setAddNics(addNics.map((x, j) => j === i ? { ...x, model: ev.target.value } : x))} style={{ width: 110 }}>{hv.nic_models.map((m: string) => <option key={m}>{m}</option>)}</select>
                <Btn icon="trash" kind="danger" onClick={() => setAddNics(addNics.filter((_, j) => j !== i))} />
              </div>
            ))}
            <div><Btn icon="plus" kind="outline" size="sm" disabled={vnets.length + bridges.length === 0} onClick={() => setAddNics([...addNics, { type: vnets.length ? "network" : "bridge", source: vnets[0]?.name ?? bridges[0]?.name, model: "virtio" }])}>Add network adapter</Btn></div>
          </div>
        </Panel>
        <Panel title="Resource allocation">
          <div className="form">
            <label className="check"><input type="checkbox" checked={hotadd} onChange={(e) => setHotadd(e.target.checked)} /> Enable CPU hot add <span className="small muted">(add vCPUs while the VM runs; takes effect at the next power on)</span></label>
            <div className="row">
              <Field label="CPU shares" help="Relative priority when the host CPU is contended">
                <select value={[500, 1000, 2000].map((x) => x * vcpus).includes(shares) ? String(shares) : "custom"}
                  onChange={(e) => e.target.value !== "custom" && setShares(Number(e.target.value))}>
                  <option value={500 * vcpus}>Low ({500 * vcpus})</option>
                  <option value={1000 * vcpus}>Normal ({1000 * vcpus})</option>
                  <option value={2000 * vcpus}>High ({2000 * vcpus})</option>
                  <option value="custom">Custom</option>
                </select>
              </Field>
              <Field label="Shares value"><input type="number" min={2} max={262144} value={shares} onChange={(e) => setShares(Number(e.target.value))} /></Field>
            </div>
            <Field label="CPU limit" help={`0 = unlimited. Host CPU: ${hv.host_mhz || "?"} MHz per thread, VM maximum ${(hv.host_mhz || 0) * vcpus} MHz.`}>
              <div className="unit-input"><input type="number" min={0} step={100} value={limitMhz} onChange={(e) => setLimitMhz(Number(e.target.value))} /><span>MHz</span></div>
            </Field>
            <label className="check"><input type="checkbox" checked={memHot} onChange={(e) => setMemHot(e.target.checked)} /> Enable memory hot add <span className="small muted">(add memory while the VM runs, up to the host RAM; Linux and Windows Server guests; takes effect at the next power on)</span></label>
            <label className="check"><input type="checkbox" checked={memLock} onChange={(e) => setMemLock(e.target.checked)} /> Reserve all guest memory (locked in host RAM, never swapped) <span className="small muted">— at the next power on</span></label>
          </div>
        </Panel>
        <Panel title="Video card">
          <div className="form">
            <div className="row">
              <Field label="Model" help="virtio: best for Linux / guest tools; VGA / Bochs: widest compatibility">
                <select value={video} onChange={(e) => setVideo(e.target.value)}>{(hv.video_models ?? ["virtio", "vga", "bochs"]).map((m: string) => <option key={m} value={m}>{m === "virtio" ? "VirtIO GPU" : m === "vga" ? "Standard VGA" : "Bochs display"}</option>)}</select>
              </Field>
              <Field label="Video memory"><div className="unit-input"><input type="number" min={4} max={256} disabled={video === "virtio"} value={vram} onChange={(e) => setVram(Number(e.target.value))} /><span>MiB</span></div></Field>
            </div>
            {running && <div className="small muted">Video changes apply at the next power on.</div>}
          </div>
        </Panel>
        <Panel title="PCI and USB devices">
          <div className="form">
            {(devs.data && !devs.data.iommu) && <div className="small muted">IOMMU (intel_iommu / amd_iommu) is not active: PCI passthrough is unavailable. USB devices can still be attached.</div>}
            {(vm.hostdevs ?? []).map((h: any) => (
              <div key={h.id} className="row">
                <span className="badge">{h.type.toUpperCase()}</span>
                <span className="mono" style={{ flex: 1 }}>{h.type === "pci" ? h.address : `${h.vendor}:${h.product}`}</span>
                <label className="check"><input type="checkbox" checked={rmDevs.includes(h.id)} onChange={(e) => setRmDevs(e.target.checked ? [...rmDevs, h.id] : rmDevs.filter((x) => x !== h.id))} /> Remove</label>
              </div>
            ))}
            {addDevs.map((h, i) => (
              <div key={i} className="row"><span className="badge blue">new {h.type.toUpperCase()}</span><span className="mono" style={{ flex: 1 }}>{h.type === "pci" ? h.address : `${h.vendor}:${h.product}`}</span>
                <Btn icon="trash" kind="danger" onClick={() => setAddDevs(addDevs.filter((_, j) => j !== i))} /></div>
            ))}
            <div className="row">
              <select value="" onChange={(e) => {
                const [t, a, b2] = e.target.value.split("|");
                if (!t) return;
                setAddDevs([...addDevs, t === "pci" ? { type: "pci", address: a } : { type: "usb", vendor: a, product: b2 }]);
              }} style={{ flex: 1 }}>
                <option value="">Add PCI or USB device…</option>
                <optgroup label={running ? "PCI devices (power off the VM to add)" : "PCI devices"}>
                  {(devs.data?.pci ?? []).map((d: any) => <option key={d.id} value={`pci|${d.address}`} disabled={running || !!d.blocked || d.used_by.length > 0 || addDevs.some((x) => x.address === d.address)}>
                    {d.address} {d.vendor} {d.product}{d.blocked ? ` — ${d.blocked}` : d.used_by.length ? ` — used by ${d.used_by.join(", ")}` : ""}</option>)}
                </optgroup>
                <optgroup label="USB devices">
                  {(devs.data?.usb ?? []).map((d: any) => <option key={d.id} value={`usb|${d.vendor}|${d.product}`} disabled={d.used_by.length > 0 || addDevs.some((x) => x.vendor === d.vendor && x.product === d.product)}>
                    {d.vendor}:{d.product} {d.vendor_name} {d.product_name}{d.used_by.length ? ` — used by ${d.used_by.join(", ")}` : ""}</option>)}
                </optgroup>
              </select>
            </div>
            <div className="small muted">A passed-through device is used exclusively by this VM and is detached from the host while the VM runs. Devices the host needs (management NIC, boot disk controller, primary GPU) are blocked.</div>
          </div>
        </Panel>
        <Panel title="Serial ports">
          <div className="form">
            {serials.map((sp: any) => (
              <div key={sp.port} className="row">
                <span className="badge">COM{sp.port + 1}</span>
                <span style={{ flex: 1 }} className={sp.console ? "" : "mono small"}>{sp.console ? "Console (text console of the VM)" : sp.type === "file" ? sp.path : sp.type}</span>
                {sp.type === "file" && <label className="check"><input type="checkbox" checked={rmSer.includes(sp.port)} disabled={running}
                  onChange={(e) => setRmSer(e.target.checked ? [...rmSer, sp.port] : rmSer.filter((x) => x !== sp.port))} /> Remove</label>}
              </div>
            ))}
            {addSer.map((x, i) => (
              <div key={i} className="row">
                <span className="badge blue">new</span>
                <select value={x.datastore} style={{ width: 150 }} onChange={(e) => setAddSer(addSer.map((y, j) => j === i ? { ...y, datastore: e.target.value } : y))}>
                  {dsList.filter((p: any) => !p.type || p.type === "dir" || p.type === "netfs").map((p: any) => <option key={p.name}>{p.name}</option>)}
                </select>
                <input type="text" value={x.file} style={{ flex: 1 }} onChange={(e) => setAddSer(addSer.map((y, j) => j === i ? { ...y, file: e.target.value } : y))} />
                <Btn icon="trash" kind="danger" onClick={() => setAddSer(addSer.filter((_, j) => j !== i))} />
              </div>
            ))}
            <div><Btn icon="plus" kind="outline" size="sm" disabled={running || serials.length + addSer.length >= 4 || dsList.length === 0}
              onClick={() => setAddSer([...addSer, { datastore: dsList.find((p: any) => !p.type || p.type === "dir" || p.type === "netfs")?.name ?? dsList[0].name, file: `${vm.name}-com${serials.length + addSer.length + 1}.log` }])}>Add serial port</Btn></div>
            <div className="small muted">A serial port writes the guest's COM port output to a file on a directory or NFS datastore (download it from the datastore browser). {running ? "Power off the VM to add or remove serial ports." : ""}</div>
          </div>
        </Panel>
        {!vm.template && <Panel title="VM options — autostart">
          <div className="form">
            <label className="check"><input type="checkbox" checked={aus.enabled} onChange={(e) => setAus({ ...aus, enabled: e.target.checked })} /> Power on this VM with the host</label>
            <div className="row">
              <Field label="Start order" help="0 = any order (after ordered VMs)"><input type="number" min={0} max={999} value={aus.order} disabled={!aus.enabled} onChange={(e) => setAus({ ...aus, order: Number(e.target.value) })} /></Field>
              <Field label="Start delay" help="-1 = host default"><div className="unit-input"><input type="number" min={-1} max={3600} value={aus.start_delay} disabled={!aus.enabled} onChange={(e) => setAus({ ...aus, start_delay: Number(e.target.value) })} /><span>s</span></div></Field>
            </div>
            <div className="row">
              <Field label="Stop action">
                <select value={aus.stop_action} disabled={!aus.enabled} onChange={(e) => setAus({ ...aus, stop_action: e.target.value })}>
                  <option value="">Host default</option><option value="shutdown">Shut down guest</option><option value="poweroff">Power off</option><option value="suspend">Suspend</option>
                </select>
              </Field>
              <Field label="Stop delay" help="-1 = host default"><div className="unit-input"><input type="number" min={-1} max={3600} value={aus.stop_delay} disabled={!aus.enabled} onChange={(e) => setAus({ ...aus, stop_delay: Number(e.target.value) })} /><span>s</span></div></Field>
            </div>
            <label className="check"><input type="checkbox" checked={aus.wait_heartbeat} disabled={!aus.enabled} onChange={(e) => setAus({ ...aus, wait_heartbeat: e.target.checked })} /> Continue with the next VM as soon as the guest agent is running</label>
          </div>
        </Panel>}
      </div>
    </Modal>
  );
}

function ExportDialog({ vm, onClose }: { vm: any; onClose: () => void }) {
  const pools = useAsync(() => api.get<any[]>("/api/v1/storage/datastores"));
  const [ds, setDs] = useState("");
  const { run, busy } = useAction();
  const list = (pools.data ?? []).filter((p: any) => p.active && !p.is_iso_library);
  useEffect(() => { if (!ds && list.length) setDs(vm.hardware.disks[0]?.datastore && list.some((p: any) => p.name === vm.hardware.disks[0].datastore) ? vm.hardware.disks[0].datastore : list[0].name); }, [pools.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const used = vm.hardware.disks.reduce((a: number, d: any) => a + (d.allocation ?? 0), 0);
  return (
    <Modal title={`Export ${vm.name} to OVA`} icon="upload" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || !ds || vm.state !== "shutoff"}
        onClick={() => run(() => api.post(`/api/v1/vms/${vm.uuid}/export`, { datastore: ds }), "Export started — see Recent tasks").then((r) => r && onClose())}>Export</Btn></>}>
      <div className="form">
        <div className="small muted">Creates <span className="mono">{vm.name}.ova</span> (OVF 1.0 with stream-optimized VMDK disks) that VMware ESXi / Workstation, VirtualBox and other hypervisors can import.
          Download it afterwards from <i>Storage &gt; Datastore browser</i>. Snapshots, TPM state and passthrough devices are not exported.</div>
        {vm.state !== "shutoff" && <div className="alert warn"><Icon name="warn" /><div>Power off the virtual machine first.</div></div>}
        <Field label="Write the OVA to datastore" help={`Needs roughly ${bytes(used)} of free space (compressed disks are usually smaller).`}>
          <select value={ds} onChange={(e) => setDs(e.target.value)}>{list.map((p: any) => <option key={p.name} value={p.name}>{p.name} ({bytes(p.available)} free)</option>)}</select>
        </Field>
      </div>
    </Modal>
  );
}
