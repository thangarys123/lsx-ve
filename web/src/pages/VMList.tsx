import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Spark } from "../chart";
import { Icon } from "../icons";
import { useLive } from "../live";
import { useSession } from "../session";
import { Btn, DataTable, Dropdown, ErrorBox, Panel, useAction, useAsync, VmState } from "../ui";
import { mib, OS_LABEL, pct } from "../util";
import { CloneDialog, DeleteDialog, VmPowerButtons } from "./vmActions";
import { VMWizard } from "./VMWizard";

export function VMList() {
  const { can } = useSession();
  const { vmStats, vmVersion } = useLive();
  const { data, error, reload } = useAsync(() => api.get<any[]>("/api/v1/vms"), [vmVersion]);
  const hv = useAsync(() => api.get("/api/v1/hypervisor"));
  const [sel, setSel] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [wizard, setWizard] = useState(false);
  const [dlg, setDlg] = useState<null | "clone" | "template" | "deploy" | "delete">(null);
  const [history] = useState<Record<string, number[]>>({});
  const { run } = useAction();
  const nav = useNavigate();
  const rows = (data ?? []).map((v) => {
    const st = vmStats[v.uuid];
    if (st) history[v.uuid] = [...(history[v.uuid] ?? []).slice(-29), st.cpu_pct];
    return { ...v, stats: st };
  });
  const selected = rows.filter((r) => sel.includes(r.uuid));
  const one = selected.length === 1 ? selected[0] : null;
  return (
    <div className="page">
      <div className="page-head">
        <h1><Icon name="vms" size={20} /> Virtual machines</h1>
        <span className="meta">{rows.filter((r) => r.state === "running").length} running · {rows.length} total</span>
      </div>
      {hv.data && !hv.data.available && <div className="alert err"><Icon name="error" /><div>Hypervisor unavailable: {hv.data.error}</div></div>}
      <div className="toolbar">
        {can("vm.manage") && <Btn icon="plus" onClick={() => setWizard(true)} disabled={!hv.data?.available}>Create VM</Btn>}
        <VmPowerButtons vms={selected} onChanged={reload} />
        <div className="sep" />
        {can("vm.manage") && (
          <Dropdown label="Actions" icon="more" disabled={!one} items={[
            { label: "Edit settings", icon: "edit", onClick: () => nav(`/vms/${one!.uuid}?edit=1`) },
            { label: one?.autostart ? "Disable autostart" : "Enable autostart", icon: "power", disabled: one?.template,
              onClick: () => run(() => api.put(`/api/v1/vms/${one!.uuid}/autostart`, { enabled: !one!.autostart }), "Autostart updated").then(reload) },
            "sep",
            ...(one?.template ? [
              { label: "Deploy VM from template", icon: "template", onClick: () => setDlg("deploy") },
              { label: "Convert to virtual machine", icon: "vm", onClick: () => run(() => api.put(`/api/v1/vms/${one!.uuid}/template`, { template: false }), "Converted").then(reload) },
            ] : [
              { label: "Clone", icon: "clone", disabled: one?.state !== "shutoff", onClick: () => setDlg("clone") },
              { label: "Clone to template", icon: "template", disabled: one?.state !== "shutoff", onClick: () => setDlg("template") },
              { label: "Convert to template", icon: "template", disabled: one?.state !== "shutoff", onClick: () => run(() => api.put(`/api/v1/vms/${one!.uuid}/template`, { template: true }), "Converted to template").then(reload) },
            ]),
            "sep",
            { label: "Delete", icon: "trash", danger: true, disabled: one?.state !== "shutoff", onClick: () => setDlg("delete") },
          ]} />
        )}
        <div className="grow" />
        <input type="search" placeholder="Search" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 200 }} />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <ErrorBox error={error} onRetry={reload} />
      <Panel flush>
        <DataTable multi rowKey={(r: any) => r.uuid} rows={rows} selected={sel} onSelect={setSel} filter={filter}
          onOpen={(r: any) => nav(`/vms/${r.uuid}`)}
          empty={<div className="empty-state"><h3>No virtual machines</h3>{can("vm.manage") && <Btn kind="primary" icon="plus" onClick={() => setWizard(true)}>Create a virtual machine</Btn>}</div>}
          cols={[
            { key: "name", label: "Name", sort: (r: any) => r.name.toLowerCase(), render: (r: any) => (
              <a onClick={(e) => { e.stopPropagation(); nav(`/vms/${r.uuid}`); }} style={{ cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6 }}>
                <Icon name={r.template ? "template" : "vm"} size={14} />{r.name}{r.template && <span className="badge">template</span>}
              </a>) },
            { key: "state", label: "Status", sort: (r: any) => r.state, render: (r: any) => <VmState state={r.state} /> },
            { key: "os_type", label: "Guest OS", sort: (r: any) => r.os_type, render: (r: any) => r.os_variant || OS_LABEL[r.os_type] || r.os_type },
            { key: "vcpus", label: "vCPUs", num: true, sort: (r: any) => r.vcpus },
            { key: "memory_mib", label: "Memory", num: true, sort: (r: any) => r.memory_mib, render: (r: any) => mib(r.memory_mib) },
            { key: "cpu", label: "CPU usage", num: true, sort: (r: any) => r.stats?.cpu_pct ?? -1, render: (r: any) => r.stats ? (
              <span className="row" style={{ justifyContent: "flex-end", gap: 8 }}><Spark values={history[r.uuid] ?? []} width={60} />{pct(r.stats.cpu_pct, 1)}</span>) : "—" },
            { key: "firmware", label: "Firmware", render: (r: any) => r.firmware.toUpperCase() },
            { key: "autostart", label: "Autostart", render: (r: any) => r.autostart ? <span className="badge green">Yes</span> : <span className="muted">No</span> },
            { key: "has_snapshots", label: "Snapshots", render: (r: any) => r.has_snapshots ? <Icon name="snapshot" size={14} /> : "" },
          ]} />
      </Panel>
      {wizard && <VMWizard onClose={() => { setWizard(false); reload(); }} />}
      {one && (dlg === "clone" || dlg === "template" || dlg === "deploy") && <CloneDialog vm={one} mode={dlg} onClose={() => setDlg(null)} />}
      {one && dlg === "delete" && <DeleteDialog vm={one} onClose={() => { setDlg(null); setSel([]); }} />}
    </div>
  );
}
