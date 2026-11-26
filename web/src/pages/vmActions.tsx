// Power / lifecycle actions shared by the VM list and VM detail views.
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { useSession } from "../session";
import { Btn, Confirm, Dropdown, Field, Modal, useAction } from "../ui";

export interface VmLite {
  uuid: string;
  name: string;
  state: string;
  template: boolean;
  autostart: boolean;
}

export function openConsole(uuid: string) {
  window.open(`/console/${uuid}`, `lsxve-console-${uuid}`, "width=1100,height=820,noopener");
}

export function useVmActions(onChanged?: () => void) {
  const { run } = useAction();
  const power = (vm: VmLite, action: string) => run(() => api.post(`/api/v1/vms/${vm.uuid}/power/${action}`)).then(onChanged);
  return { power };
}

export function VmPowerButtons({ vms, onChanged }: { vms: VmLite[]; onChanged?: () => void }) {
  const { can } = useSession();
  const { power } = useVmActions(onChanged);
  const [confirm, setConfirm] = useState<string | null>(null);
  const all = (pred: (v: VmLite) => boolean) => vms.length > 0 && vms.every(pred);
  const each = (a: string) => vms.forEach((v) => power(v, a));
  const label = vms.length === 1 ? vms[0].name : `${vms.length} virtual machines`;
  const p = can("vm.power");
  return (
    <>
      {can("vm.console") && <Btn icon="console" disabled={vms.length !== 1 || vms[0].state !== "running" && vms[0].state !== "paused"} onClick={() => openConsole(vms[0].uuid)}>Console</Btn>}
      <div className="sep" />
      <Btn icon="play" disabled={!p || !all((v) => ["shutoff", "crashed", "paused", "suspended"].includes(v.state) && !v.template)} onClick={() => each("start")}>Power on</Btn>
      <Btn icon="power" disabled={!p || !all((v) => v.state === "running")} onClick={() => each("shutdown")}>Shut down</Btn>
      <Btn icon="stop" disabled={!p || !all((v) => ["running", "paused", "shutting-down", "crashed"].includes(v.state))} onClick={() => setConfirm("poweroff")}>Power off</Btn>
      <Btn icon="pause" disabled={!p || !all((v) => v.state === "running")} onClick={() => each("suspend")}>Pause</Btn>
      <Dropdown label="More power" disabled={!p || vms.length === 0} items={[
        { label: "Restart guest OS", icon: "restart", disabled: !all((v) => v.state === "running"), onClick: () => each("reboot") },
        { label: "Reset (hard)", icon: "restart", disabled: !all((v) => v.state === "running"), onClick: () => setConfirm("reset") },
        { label: "Resume", icon: "play", disabled: !all((v) => v.state === "paused"), onClick: () => each("resume") },
      ]} />
      {confirm && (
        <Confirm danger title={confirm === "poweroff" ? `Power off ${label}` : `Reset ${label}`}
          message={confirm === "poweroff" ? "This immediately cuts power, like pulling the power cord. Unsaved guest data may be lost. Use Shut down for a graceful shutdown." : "This immediately resets the virtual machine without shutting down the guest OS."}
          confirmLabel={confirm === "poweroff" ? "Power off" : "Reset"} onClose={() => setConfirm(null)} onConfirm={() => each(confirm)} />
      )}
    </>
  );
}

export function CloneDialog({ vm, onClose, mode }: { vm: VmLite; onClose: () => void; mode: "clone" | "template" | "deploy" }) {
  const [name, setName] = useState(mode === "template" ? `${vm.name}-template` : mode === "deploy" ? "" : `${vm.name}-clone`);
  const [powerOn, setPowerOn] = useState(false);
  const { run, busy } = useAction();
  const title = mode === "deploy" ? `Deploy VM from template ${vm.name}` : mode === "template" ? `Clone ${vm.name} to template` : `Clone ${vm.name}`;
  return (
    <Modal title={title} icon={mode === "clone" ? "clone" : "template"} onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || !name}
        onClick={() => run(() => api.post(`/api/v1/vms/${vm.uuid}/clone`, { name, template: mode === "template", power_on: powerOn && mode !== "template" }), "Task submitted").then((r) => r && onClose())}>
        {mode === "deploy" ? "Deploy" : "Clone"}</Btn></>}>
      <div className="form">
        <div className="small muted">All virtual disks are fully copied into the same datastore. New MAC addresses and a new UUID are generated. {vm.state !== "shutoff" && <b style={{ color: "var(--warn)" }}>The source must be powered off.</b>}</div>
        <Field label="Name of the new virtual machine"><input type="text" autoFocus value={name} onChange={(e) => setName(e.target.value)} /></Field>
        {mode !== "template" && <label className="check"><input type="checkbox" checked={powerOn} onChange={(e) => setPowerOn(e.target.checked)} /> Power on after creation</label>}
      </div>
    </Modal>
  );
}

export function DeleteDialog({ vm, onClose }: { vm: VmLite; onClose: () => void }) {
  const [disks, setDisks] = useState(true);
  const { run } = useAction();
  const nav = useNavigate();
  return (
    <Confirm danger title={`Delete ${vm.name}`} confirmLabel="Delete" typed={disks ? vm.name : undefined} onClose={onClose}
      message={vm.state !== "shutoff" ? <b>The virtual machine must be powered off before it can be deleted.</b> :
        "The virtual machine is unregistered, including its snapshots and UEFI variable store."}
      onConfirm={() => run(() => api.del(`/api/v1/vms/${vm.uuid}?delete_disks=${disks ? 1 : 0}`), "Delete task submitted").then(() => nav("/vms"))}>
      <label className="check"><input type="checkbox" checked={disks} onChange={(e) => setDisks(e.target.checked)} /> Also delete its virtual disks (disks shared with other VMs are kept)</label>
    </Confirm>
  );
}
