import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { TimeChart } from "../chart";
import { Icon } from "../icons";
import { useLive } from "../live";
import { useSession } from "../session";
import { Btn, Confirm, ErrorBox, Field, Loading, Meter, Panel, useAsync, useToast } from "../ui";
import { bytes, bps, duration, pct } from "../util";
import { MaintenanceDialog, useHostState } from "./HostAdmin";
import { VMWizard } from "./VMWizard";

export function HostSummary() {
  const { can } = useSession();
  const { data: s, error, reload } = useAsync(() => api.get("/api/v1/host/summary"));
  const { samples, vmVersion } = useLive();
  const [wizard, setWizard] = useState(false);
  const [power, setPower] = useState<"reboot" | "poweroff" | null>(null);
  const [maintDlg, setMaint] = useState(false);
  const { maint } = useHostState();
  const nav = useNavigate();
  useEffect(() => {
    const t = setInterval(reload, 30000);
    return () => clearInterval(t);
  }, [reload]);
  useEffect(reload, [vmVersion]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <div className="page"><ErrorBox error={error} onRetry={reload} /></div>;
  if (!s) return <Loading />;
  const last = samples[samples.length - 1];
  const recent = samples.slice(-300);
  const hv = s.hypervisor;
  const cpuPct = last?.cpu ?? s.current?.cpu ?? 0;
  const mem = s.memory;
  const memUsed = last?.mem_used ?? mem.used;
  const st = s.storage;
  return (
    <div className="page">
      <div className="toolbar">
        {can("vm.manage") && <Btn icon="plus" onClick={() => setWizard(true)} disabled={!hv.available}>Create virtual machine</Btn>}
        <div className="sep" />
        {can("host.config") && <Btn icon="manage" onClick={() => setMaint(true)}>{maint ? "Exit maintenance mode" : "Enter maintenance mode"}</Btn>}
        {can("host.power") && <Btn icon="restart" onClick={() => setPower("reboot")}>Reboot host</Btn>}
        {can("host.power") && <Btn icon="power" onClick={() => setPower("poweroff")}>Shut down host</Btn>}
        <div className="sep" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
        <div className="grow" />
        {can("host.view") && <Btn icon="manage" onClick={() => nav("/host/manage")}>Manage</Btn>}
      </div>

      <div className="hero">
        <div className="big-icon"><Icon name="host" size={26} /></div>
        <div>
          <h2>{s.hostname}</h2>
          <div className="facts">
            <span><b>Version</b>LSX-VE {s.version}</span>
            <span><b>State</b><span className={`status ${hv.available ? "ok" : "error"}`}>{hv.available ? "Normal (connected to hypervisor)" : "Hypervisor unavailable"}</span></span>
            <span><b>Uptime</b>{duration(s.uptime)}</span>
            <span><b>Management IP</b><span className="mono">{s.management.ipv4[0]?.split("/")[0] ?? "—"}</span></span>
          </div>
        </div>
        <div className="right">
          <Meter label="CPU" icon="cpu" used={cpuPct} total={100} fmt={(n) => `${n.toFixed(0)}%`} />
          <Meter label="Memory" icon="memory" used={memUsed} total={mem.total} fmt={(n) => bytes(n)} />
          <Meter label="Storage" icon="storage" used={st.used} total={st.capacity} fmt={(n) => bytes(n)} />
        </div>
      </div>

      {!hv.available && (
        <div className="alert err"><Icon name="error" /><div className="grow"><b>Hypervisor not available.</b> {hv.error}</div></div>
      )}
      {hv.available && !hv.accelerated && (
        <div className="alert warn"><Icon name="warn" /><div className="grow">
          <b>Hardware virtualization is not available</b> (no /dev/kvm). Virtual machines will run with software emulation and be very slow.
          Enable Intel VT-x / AMD-V in the server firmware.
        </div></div>
      )}

      <div className="grid c3" style={{ marginBottom: 10 }}>
        <Panel title="Hardware" icon="cpu">
          <div className="kv">
            <div>Manufacturer</div><div>{s.dmi.sys_vendor || "—"}</div>
            <div>Model</div><div>{s.dmi.product_name || "—"}</div>
            <div>CPU</div><div>{s.cpu.model}</div>
            <div>Sockets / cores / threads</div><div>{s.cpu.sockets} / {s.cpu.cores} / {s.cpu.threads}</div>
            <div>Virtualization</div><div>{s.cpu.virtualization ?? <span className="badge red">not supported</span>}{s.cpu.nested_hypervisor && <span className="badge amber" style={{ marginLeft: 6 }}>nested</span>}</div>
            <div>Memory</div><div>{bytes(mem.total)}</div>
            <div>Firmware</div><div>{s.firmware}{s.secure_boot ? " (Secure Boot on)" : s.firmware === "UEFI" ? " (Secure Boot off)" : ""}</div>
            <div>BIOS</div><div>{[s.dmi.bios_vendor, s.dmi.bios_version, s.dmi.bios_date].filter(Boolean).join(" ") || "—"}</div>
            <div>IOMMU</div><div>{s.iommu ? "Enabled" : "Not enabled"}</div>
          </div>
        </Panel>
        <Panel title="Configuration" icon="manage">
          <div className="kv">
            <div>Product</div><div>LSX-VE {s.version}</div>
            <div>Base OS</div><div>{s.os.PRETTY_NAME ?? "—"}</div>
            <div>Kernel</div><div className="mono">{s.kernel.release}</div>
            <div>Hypervisor</div><div>{hv.available ? `QEMU ${hv.qemu_version} · libvirt ${hv.libvirt_version}` : "—"}</div>
            <div>Acceleration</div><div>{hv.available ? (hv.accelerated ? "KVM" : "Software (TCG)") : "—"}</div>
            <div>UEFI / Secure Boot / vTPM</div><div>{hv.available ? `${hv.uefi ? "Yes" : "No"} / ${hv.secure_boot ? "Yes" : "No"} / ${hv.tpm ? "Yes" : "No"}` : "—"}</div>
            <div>Management interface</div><div className="mono">{s.management.interface ?? "—"}</div>
            <div>IPv4</div><div className="mono">{s.management.ipv4.join(", ") || "—"}</div>
            <div>IPv6</div><div className="mono">{s.management.ipv6.join(", ") || "—"}</div>
            <div>Gateway</div><div className="mono">{s.management.gateway ?? "—"}</div>
          </div>
        </Panel>
        <div className="grid">
          <Panel title="Virtual machines" icon="vms" right={<a onClick={() => nav("/vms")} style={{ cursor: "pointer" }}>View all</a>}>
            <div className="stats" style={{ gridTemplateColumns: "repeat(2, 1fr)" }}>
              <div className="stat"><div className="label">Running</div><div className="value" style={{ color: "var(--ok)" }}>{s.vms.running}</div></div>
              <div className="stat"><div className="label">Stopped</div><div className="value">{s.vms.stopped}</div></div>
              <div className="stat"><div className="label">Paused</div><div className="value" style={{ color: "var(--paused)" }}>{s.vms.paused}</div></div>
              <div className="stat"><div className="label">Templates</div><div className="value">{s.vms.templates}</div></div>
            </div>
          </Panel>
          <Panel title="Health" icon="health" right={<a onClick={() => nav("/host/monitor?tab=hardware")} style={{ cursor: "pointer" }}>Sensors</a>}>
            <div className="kv">
              <div>Hardware sensors</div>
              <div>{s.sensors_count ? <span className={`status ${s.health}`}>{s.health} ({s.sensors_count} sensors)</span> : <span className="muted">No sensors exposed by this hardware</span>}</div>
              <div>Datastores</div><div>{st.datastores} · {bytes(st.free)} free</div>
              <div>Load average</div><div className="mono">{last?.load?.map((l) => l.toFixed(2)).join("  ") ?? "—"}</div>
            </div>
          </Panel>
        </div>
      </div>

      <div className="grid c2">
        <Panel title="CPU" icon="cpu" right={<span className="mono">{pct(cpuPct, 1)}</span>}>
          <TimeChart times={recent.map((x) => x.ts)} series={[{ label: "Host CPU", color: "var(--chart-1)", values: recent.map((x) => x.cpu) }]}
            max={100} fmt={(n) => `${n.toFixed(0)}%`} height={140} />
        </Panel>
        <Panel title="Memory" icon="memory" right={<span className="mono">{bytes(memUsed)} / {bytes(mem.total)}</span>}>
          <TimeChart times={recent.map((x) => x.ts)} series={[{ label: "Consumed", color: "var(--chart-2)", values: recent.map((x) => x.mem_used) }]}
            max={mem.total} fmt={(n) => bytes(n, 0)} height={140} />
        </Panel>
        <Panel title="Network" icon="nic" right={<span className="mono">↓ {bps(last?.net_rx)} ↑ {bps(last?.net_tx)}</span>}>
          <TimeChart times={recent.map((x) => x.ts)} series={[
            { label: "Received", color: "var(--chart-1)", values: recent.map((x) => x.net_rx) },
            { label: "Transmitted", color: "var(--chart-3)", values: recent.map((x) => x.net_tx) }]}
            fmt={(n) => bytes(n, 0) + "/s"} height={140} />
        </Panel>
        <Panel title="Disk" icon="disk" right={<span className="mono">R {bps(last?.disk_read)} W {bps(last?.disk_write)}</span>}>
          <TimeChart times={recent.map((x) => x.ts)} series={[
            { label: "Read", color: "var(--chart-2)", values: recent.map((x) => x.disk_read) },
            { label: "Write", color: "var(--chart-4)", values: recent.map((x) => x.disk_write) }]}
            fmt={(n) => bytes(n, 0) + "/s"} height={140} />
        </Panel>
      </div>

      {wizard && <VMWizard onClose={() => setWizard(false)} />}
      {maintDlg && <MaintenanceDialog onClose={() => setMaint(false)} />}
      {power && <PowerDialog action={power} hostname={s.hostname} running={s.vms.running} onClose={() => setPower(null)} />}
    </div>
  );
}

function PowerDialog({ action, hostname, running, onClose }: { action: "reboot" | "poweroff"; hostname: string; running: number; onClose: () => void }) {
  const [reason, setReason] = useState("");
  const toast = useToast();
  const verb = action === "reboot" ? "Reboot" : "Shut down";
  return (
    <Confirm danger title={`${verb} host ${hostname}`} typed={hostname} confirmLabel={verb} onClose={onClose}
      message={<>
        {running > 0 && <div className="alert warn"><Icon name="warn" /><div>{running} virtual machine(s) are running. They will be shut down gracefully by the guest shutdown service before the host {action === "reboot" ? "reboots" : "powers off"}; VMs configured for autostart start again after boot.</div></div>}
        The management interface will be unavailable until the host {action === "reboot" ? "is back online" : "is powered on again"}.
      </>}
      onConfirm={async () => {
        try {
          await api.post("/api/v1/host/power", { action, confirm: hostname, reason });
          toast({ kind: "warning", title: `${verb} initiated`, detail: "The connection will be lost shortly." });
        } catch (e: any) {
          toast({ kind: "error", title: `${verb} failed`, detail: e.message });
          throw e;
        }
      }}>
      <Field label="Reason (recorded in the event log)">
        <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={200} />
      </Field>
    </Confirm>
  );
}
