// New virtual machine wizard. Every option maps to a field of POST /api/v1/vms.
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Icon } from "../icons";
import { Btn, Field, Loading, Modal, useAction, useAsync } from "../ui";
import { bytes, cls, mib } from "../util";

interface DiskSpec { datastore: string; size_gib: number; bus: string; provisioning: string; existing?: string }
interface NicSpec { type: "network" | "bridge"; source: string; model: string }

const STEPS = ["Name and guest OS", "Compute", "Storage", "Media and network", "Firmware and boot", "Ready to complete"];
const BOOT_LABEL: Record<string, string> = { cdrom: "CD/DVD drive", disk: "Hard disk", network: "Network (PXE)" };

export function VMWizard({ onClose, initialDisk }: { onClose: () => void; initialDisk?: { datastore: string; existing: string; name?: string } }) {
  const ctx = useAsync(() => Promise.all([
    api.get("/api/v1/hypervisor"),
    api.get<any[]>("/api/v1/storage/datastores"),
    api.get<any[]>("/api/v1/storage/isos").catch(() => []),
    api.get<any[]>("/api/v1/network/virtual").catch(() => []),
    api.get("/api/v1/network/host").catch(() => ({ nics: [] })),
  ]));
  const [step, setStep] = useState(0);
  const [name, setName] = useState(initialDisk?.name ?? "");
  const [desc, setDesc] = useState("");
  const [osType, setOsType] = useState("linux");
  const [osVariant, setOsVariant] = useState("");
  const [osId, setOsId] = useState("");
  const [vcpus, setVcpus] = useState(2);
  const [memGiB, setMemGiB] = useState(4);
  const [disks, setDisks] = useState<DiskSpec[]>([initialDisk
    ? { datastore: initialDisk.datastore, size_gib: 0, bus: "sata", provisioning: "thin", existing: initialDisk.existing }
    : { datastore: "", size_gib: 32, bus: "virtio", provisioning: "thin" }]);
  const [iso, setIso] = useState("");
  const [nics, setNics] = useState<NicSpec[]>([{ type: "network", source: "default", model: "virtio" }]);
  const [firmware, setFirmware] = useState("uefi");
  const [secureBoot, setSecureBoot] = useState(false);
  const [tpm, setTpm] = useState(false);
  const [boot, setBoot] = useState(["disk", "cdrom", "network"]);
  const [autostart, setAutostart] = useState(false);
  const [powerOn, setPowerOn] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const { run, busy } = useAction();

  const [hv, pools, isos, vnets, hostnet] = ctx.data ?? [null, [], [], [], { nics: [] }];
  const dsList = useMemo(() => (pools as any[]).filter((p) => p.active && !p.is_iso_library), [pools]);
  const bridges = useMemo(() => (hostnet.nics ?? []).filter((n: any) => n.kind === "bridge" && !n.name.startsWith("virbr")), [hostnet]);

  const catalog: any[] = hv?.os_catalog ?? [];
  // first available datastore as the default target (installer hosts have "datastore1", not "default")
  useEffect(() => {
    if (!dsList.length) return;
    setDisks((ds) => ds.map((d) => (d.datastore && dsList.find((p) => p.name === d.datastore)) ? d : { ...d, datastore: dsList[0].name }));
  }, [dsList]);
  // picking a guest OS version applies its family and recommended virtual hardware
  const pickOs = (id: string) => {
    setOsId(id);
    if (id === "custom") { setOsVariant(""); return; }
    const e = catalog.find((c) => c.id === id);
    if (!e) return;
    setOsVariant(e.label);
    setOsType(e.family);
    setVcpus(Math.max(1, Math.min(e.vcpus, hv.host_threads)));
    setMemGiB(Math.max(0.5, Math.min(e.memory_mib, Math.floor(hv.host_memory_mib * 0.8)) / 1024));
    setDisks((ds) => ds.map((d, i) => (i === 0 && !d.existing ? { ...d, size_gib: e.disk_gib } : d)));
    const uefi = e.firmware === "uefi" && hv.uefi;
    setFirmware(uefi ? "uefi" : "bios");
    setSecureBoot(uefi && e.secure_boot && !!hv.secure_boot);
    setTpm(e.tpm && !!hv.tpm);
  };
  useEffect(() => {
    if (!hv) return;
    if (!hv.uefi) setFirmware("bios");
    const d = hv.os_defaults?.[osType];
    if (d) {
      setDisks((ds) => ds.map((x) => ({ ...x, bus: d.disk_bus })));
      setNics((ns) => ns.map((x) => ({ ...x, model: d.nic_model })));
    }
    if (osType === "windows" && hv.tpm && hv.uefi) { setTpm(true); setFirmware("uefi"); if (hv.secure_boot) setSecureBoot(true); }
  }, [osType, hv]);
  useEffect(() => { if (iso && boot[0] !== "cdrom") setBoot(["cdrom", ...boot.filter((b) => b !== "cdrom")]); }, [iso]); // eslint-disable-line

  if (ctx.loading && !ctx.data) return <Modal title="New virtual machine" onClose={onClose}><Loading /></Modal>;
  if (ctx.error || !hv) return <Modal title="New virtual machine" onClose={onClose}><div className="alert err">{ctx.error?.message}</div></Modal>;

  const memMiB = Math.round(memGiB * 1024);
  const validate = (s: number): string | null => {
    if (s === 0 && !osId) return "Select the guest operating system.";
    if (s === 0 && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/.test(name)) return "Name must start with a letter or digit and contain only letters, digits, '.', '_' or '-' (max 63).";
    if (s === 1 && (vcpus < 1 || vcpus > hv.host_threads)) return `vCPUs must be between 1 and ${hv.host_threads}.`;
    if (s === 1 && (memMiB < 128 || memMiB > hv.host_memory_mib)) return `Memory must be between 128 MiB and ${mib(hv.host_memory_mib)}.`;
    if (s === 2) {
      for (const d of disks) {
        if (!dsList.find((p) => p.name === d.datastore)) return `Datastore ${d.datastore} is not available.`;
        if (d.existing === "") return "Select an existing disk image or switch to a new virtual disk.";
        if (!d.existing && (d.size_gib < 1 || d.size_gib > 65536)) return "Disk size must be 1–65536 GiB.";
      }
    }
    if (s === 3) for (const n of nics) if (!n.source) return "Select a network for every adapter.";
    return null;
  };
  const next = () => {
    const e = validate(step);
    setErr(e);
    if (!e) setStep(step + 1);
  };
  const submit = async () => {
    const body = {
      name, description: desc, os_type: osType, os_variant: osVariant, vcpus, memory_mib: memMiB,
      firmware, secure_boot: firmware === "uefi" && secureBoot, tpm, disks: disks.map((d) => d.existing ? { datastore: d.datastore, bus: d.bus, existing: d.existing } : d),
      iso: iso || undefined, nics, boot_order: boot, autostart, power_on: powerOn,
    };
    const r = await run(() => api.post("/api/v1/vms", body), "Create task submitted");
    if (r) onClose();
  };
  const moveBoot = (i: number, d: number) => {
    const b = [...boot];
    const j = i + d;
    if (j < 0 || j >= b.length) return;
    [b[i], b[j]] = [b[j], b[i]];
    setBoot(b);
  };
  const totalNew = disks.filter((d) => !d.existing).reduce((a, d) => a + d.size_gib, 0);

  return (
    <Modal title={initialDisk ? "Register virtual machine" : "New virtual machine"} icon="vm" size="wide" onClose={onClose}
      footer={<>
        <span className="left">{err && <span style={{ color: "#ff9b94" }}>{err}</span>}</span>
        <Btn onClick={onClose}>Cancel</Btn>
        <Btn disabled={step === 0} onClick={() => { setErr(null); setStep(step - 1); }}>Back</Btn>
        {step < STEPS.length - 1 ? <Btn kind="primary" onClick={next}>Next</Btn> : <Btn kind="primary" disabled={busy} onClick={submit}>Finish</Btn>}
      </>}>
      <div className="wizard" style={{ margin: "-14px -16px" }}>
        <div className="steps">
          {STEPS.map((s, i) => (
            <div key={s} className={cls(i === step && "cur", i < step && "done")}><span className="n">{i < step ? "✓" : i + 1}</span>{s}</div>
          ))}
        </div>
        <div className="body">
          {step === 0 && (
            <div className="form">
              <h3>Name and guest operating system</h3>
              <Field label="Virtual machine name"><input type="text" autoFocus value={name} onChange={(e) => setName(e.target.value)} maxLength={63} /></Field>
              <Field label="Guest operating system" help="Sets the device models (disk controller, network adapter, clock, display) and recommended CPU, memory, disk and firmware. You can change every value in the next steps.">
                <select value={osId} onChange={(e) => pickOs(e.target.value)}>
                  <option value="" disabled>Select the guest operating system…</option>
                  {Object.entries(hv.os_types).map(([fam, famLabel]) => {
                    const items = catalog.filter((c) => c.family === fam);
                    return items.length ? (
                      <optgroup key={fam} label={String(famLabel)}>
                        {items.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
                      </optgroup>
                    ) : null;
                  })}
                  <option value="custom">Other – enter a name…</option>
                </select>
              </Field>
              {osId && osId !== "custom" && (() => {
                const e = catalog.find((c) => c.id === osId);
                return e ? <div className="small muted">Recommended: {e.vcpus} vCPU · {mib(e.memory_mib)} memory · {e.disk_gib} GiB disk · {e.firmware === "uefi" ? "UEFI" : "BIOS"}{e.secure_boot ? " · Secure Boot" : ""}{e.tpm ? " · TPM 2.0" : ""}</div> : null;
              })()}
              {osId === "custom" && (
                <div className="row">
                  <Field label="Guest OS family" help="Selects compatible device models.">
                    <select value={osType} onChange={(e) => setOsType(e.target.value)}>
                      {Object.entries(hv.os_types).map(([k, l]) => <option key={k} value={k}>{String(l)}</option>)}
                    </select>
                  </Field>
                  <Field label="Guest OS name"><input type="text" value={osVariant} placeholder="e.g. Haiku R1" onChange={(e) => setOsVariant(e.target.value)} maxLength={64} /></Field>
                </div>
              )}
              <Field label="Notes"><textarea value={desc} onChange={(e) => setDesc(e.target.value)} maxLength={1000} style={{ fontFamily: "var(--sans)" }} /></Field>
            </div>
          )}
          {step === 1 && (
            <div className="form">
              <h3>Compute resources</h3>
              <Field label="Virtual CPUs" help={`Host has ${hv.host_threads} logical CPUs.${hv.accelerated ? "" : " Running without KVM acceleration."}`}>
                <div className="row">
                  <input type="range" min={1} max={hv.host_threads} value={vcpus} onChange={(e) => setVcpus(Number(e.target.value))} style={{ flex: 1 }} />
                  <input type="number" min={1} max={hv.host_threads} value={vcpus} onChange={(e) => setVcpus(Number(e.target.value))} style={{ width: 90 }} />
                </div>
              </Field>
              <Field label="Memory" help={`Host has ${mib(hv.host_memory_mib)} of physical memory.`}>
                <div className="row">
                  <input type="range" min={0.25} max={Math.max(1, Math.floor(hv.host_memory_mib / 1024))} step={0.25} value={memGiB} onChange={(e) => setMemGiB(Number(e.target.value))} style={{ flex: 1 }} />
                  <div className="unit-input" style={{ width: 140 }}><input type="number" min={0.125} step={0.25} value={memGiB} onChange={(e) => setMemGiB(Number(e.target.value))} /><span>GiB</span></div>
                </div>
              </Field>
            </div>
          )}
          {step === 2 && (
            <div className="form">
              <h3>Virtual disks</h3>
              {dsList.length === 0 && <div className="alert err"><Icon name="error" /><div>No active datastore is available.</div></div>}
              {disks.map((d, i) => {
                const ds = dsList.find((p) => p.name === d.datastore);
                return (
                  <div key={i} className="panel"><div className="pb">
                    <div className="row" style={{ marginBottom: 8 }}><b>Hard disk {i + 1}</b><div className="grow" />
                      <Btn size="sm" icon="trash" kind="danger" onClick={() => setDisks(disks.filter((_, j) => j !== i))}>Remove</Btn></div>
                    <div className="row">
                      <Field label="Datastore" help={ds ? `${bytes(ds.available)} free of ${bytes(ds.capacity)}` : undefined}>
                        <select value={d.datastore} onChange={(e) => setDisks(disks.map((x, j) => j === i ? { ...x, datastore: e.target.value, existing: undefined } : x))}>
                          {dsList.map((p) => <option key={p.name} value={p.name}>{p.name} ({p.type})</option>)}
                        </select>
                      </Field>
                      <Field label="Source">
                        <select value={d.existing ? "existing" : "new"} onChange={(e) => setDisks(disks.map((x, j) => j === i ? { ...x, existing: e.target.value === "existing" ? "" : undefined } : x))}>
                          <option value="new">New virtual disk</option><option value="existing">Existing disk image</option>
                        </select>
                      </Field>
                      <Field label="Controller">
                        <select value={d.bus} onChange={(e) => setDisks(disks.map((x, j) => j === i ? { ...x, bus: e.target.value } : x))}>
                          {hv.disk_buses.map((b: string) => <option key={b} value={b}>{b === "virtio" ? "VirtIO block" : b === "scsi" ? "VirtIO SCSI" : "SATA (AHCI)"}</option>)}
                        </select>
                      </Field>
                    </div>
                    {d.existing === undefined ? (
                      <div className="row">
                        <Field label="Size"><div className="unit-input"><input type="number" min={1} value={d.size_gib} onChange={(e) => setDisks(disks.map((x, j) => j === i ? { ...x, size_gib: Number(e.target.value) } : x))} /><span>GiB</span></div></Field>
                        <Field label="Provisioning" help={d.provisioning === "thin" ? "Space is allocated as the guest writes (qcow2)." : "Space is fully allocated now."}>
                          <select value={d.provisioning} onChange={(e) => setDisks(disks.map((x, j) => j === i ? { ...x, provisioning: e.target.value } : x))}>
                            <option value="thin">Thin provisioned</option><option value="thick">Thick provisioned</option>
                          </select>
                        </Field>
                      </div>
                    ) : <ExistingVolume pool={d.datastore} value={d.existing} onChange={(v) => setDisks(disks.map((x, j) => j === i ? { ...x, existing: v } : x))} />}
                  </div></div>
                );
              })}
              <div><Btn icon="plus" kind="outline" disabled={disks.length >= 8 || dsList.length === 0}
                onClick={() => setDisks([...disks, { datastore: dsList[0]?.name ?? "default", size_gib: 16, bus: hv.os_defaults?.[osType]?.disk_bus ?? "virtio", provisioning: "thin" }])}>Add hard disk</Btn></div>
            </div>
          )}
          {step === 3 && (
            <div className="form">
              <h3>Installation media</h3>
              <Field label="CD/DVD drive" help={isos.length === 0 ? "The ISO library is empty — upload images under Storage → ISO library." : "Images from the ISO library"}>
                <select value={iso} onChange={(e) => setIso(e.target.value)}>
                  <option value="">Empty drive</option>
                  {isos.map((x: any) => <option key={x.name} value={x.name}>{x.name} ({bytes(x.capacity)})</option>)}
                </select>
              </Field>
              <h3 style={{ marginTop: 8 }}>Network adapters</h3>
              {nics.map((n, i) => (
                <div key={i} className="row" style={{ alignItems: "flex-end" }}>
                  <Field label={`Adapter ${i + 1} connection`}>
                    <select value={n.type} onChange={(e) => setNics(nics.map((x, j) => j === i ? { ...x, type: e.target.value as any, source: e.target.value === "network" ? (vnets[0]?.name ?? "") : (bridges[0]?.name ?? "") } : x))}>
                      <option value="network">Virtual network</option><option value="bridge">Host bridge</option>
                    </select>
                  </Field>
                  <Field label="Network">
                    <select value={n.source} onChange={(e) => setNics(nics.map((x, j) => j === i ? { ...x, source: e.target.value } : x))}>
                      <option value="">Select…</option>
                      {n.type === "network" ? vnets.map((v: any) => <option key={v.name} value={v.name}>{v.name} ({v.mode === "bridge" ? `bridged to ${v.bridge} — physical network` : v.mode}{v.active ? "" : ", inactive"})</option>)
                        : bridges.map((b: any) => <option key={b.name} value={b.name}>{b.name}</option>)}
                    </select>
                  </Field>
                  <Field label="Adapter type">
                    <select value={n.model} onChange={(e) => setNics(nics.map((x, j) => j === i ? { ...x, model: e.target.value } : x))}>
                      {hv.nic_models.map((m: string) => <option key={m} value={m}>{m === "virtio" ? "VirtIO (paravirtual)" : m === "e1000e" ? "Intel E1000E" : "Realtek RTL8139"}</option>)}
                    </select>
                  </Field>
                  <Btn icon="trash" kind="danger" onClick={() => setNics(nics.filter((_, j) => j !== i))} />
                </div>
              ))}
              {n0Bridge(bridges) && <div className="small muted">No host bridges exist. Create one under Networking to put VMs directly on a physical network.</div>}
              <div><Btn icon="plus" kind="outline" disabled={nics.length >= 8} onClick={() => setNics([...nics, { type: "network", source: vnets[0]?.name ?? "", model: hv.os_defaults?.[osType]?.nic_model ?? "virtio" }])}>Add network adapter</Btn></div>
            </div>
          )}
          {step === 4 && (
            <div className="form">
              <h3>Firmware</h3>
              <div className="row">
                <label className="check"><input type="radio" checked={firmware === "bios"} onChange={() => { setFirmware("bios"); setSecureBoot(false); }} /> BIOS (SeaBIOS)</label>
                <label className="check" style={{ opacity: hv.uefi ? 1 : 0.4 }}><input type="radio" disabled={!hv.uefi} checked={firmware === "uefi"} onChange={() => setFirmware("uefi")} /> UEFI (OVMF){!hv.uefi && " — not available on this host"}</label>
              </div>
              <label className="check" style={{ opacity: firmware === "uefi" && hv.secure_boot ? 1 : 0.4 }}>
                <input type="checkbox" disabled={firmware !== "uefi" || !hv.secure_boot} checked={secureBoot} onChange={(e) => setSecureBoot(e.target.checked)} />
                Enable UEFI Secure Boot (Microsoft keys enrolled){!hv.secure_boot && " — not supported by this host's firmware"}
              </label>
              <label className="check" style={{ opacity: hv.tpm ? 1 : 0.4 }}>
                <input type="checkbox" disabled={!hv.tpm} checked={tpm} onChange={(e) => setTpm(e.target.checked)} />
                Add virtual TPM 2.0{!hv.tpm && " — swtpm not available"}
              </label>
              <h3 style={{ marginTop: 8 }}>Boot order</h3>
              <ul className="list-reset order-list" style={{ maxWidth: 380 }}>
                {boot.map((b, i) => (
                  <li key={b}><span className="badge">{i + 1}</span>{BOOT_LABEL[b]}<span className="grow" style={{ flex: 1 }} />
                    <Btn size="sm" disabled={i === 0} onClick={() => moveBoot(i, -1)}>▲</Btn>
                    <Btn size="sm" disabled={i === boot.length - 1} onClick={() => moveBoot(i, 1)}>▼</Btn></li>
                ))}
              </ul>
            </div>
          )}
          {step === 5 && (
            <div className="form">
              <h3>Ready to complete</h3>
              <div className="kv">
                <div>Name</div><div>{name}</div>
                <div>Guest OS</div><div>{hv.os_types[osType]}{osVariant ? ` — ${osVariant}` : ""}</div>
                <div>CPU / memory</div><div>{vcpus} vCPU · {mib(memMiB)}</div>
                <div>Disks</div><div>{disks.length === 0 ? "none" : disks.map((d, i) => <div key={i}>{d.existing ? `existing ${d.existing}` : `${d.size_gib} GiB ${d.provisioning}`} on {d.datastore} ({d.bus})</div>)}</div>
                <div>New storage</div><div>{totalNew} GiB {disks.some((d) => d.provisioning === "thin") ? "(thin — allocated on demand)" : ""}</div>
                <div>CD/DVD</div><div>{iso || "empty"}</div>
                <div>Network</div><div>{nics.length === 0 ? "none" : nics.map((n, i) => <div key={i}>{n.source} ({n.model})</div>)}</div>
                <div>Firmware</div><div>{firmware.toUpperCase()}{firmware === "uefi" && secureBoot ? " with Secure Boot" : ""}{tpm ? " · vTPM 2.0" : ""}</div>
                <div>Boot order</div><div>{boot.map((b) => BOOT_LABEL[b]).join(" → ")}</div>
                <div>Console</div><div>VNC (localhost only, proxied through the management interface)</div>
              </div>
              <label className="check"><input type="checkbox" checked={autostart} onChange={(e) => setAutostart(e.target.checked)} /> Start automatically when the host boots</label>
              <label className="check"><input type="checkbox" checked={powerOn} onChange={(e) => setPowerOn(e.target.checked)} /> Power on after creation</label>
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}

function n0Bridge(b: any[]) {
  return b.length === 0;
}

function ExistingVolume({ pool, value, onChange }: { pool: string; value?: string; onChange: (v: string) => void }) {
  const { data } = useAsync(() => api.get<any[]>(`/api/v1/storage/datastores/${encodeURIComponent(pool)}/volumes`), [pool]);
  const free = (data ?? []).filter((v) => v.used_by.length === 0 && !v.name.toLowerCase().endsWith(".iso"));
  return (
    <Field label="Disk image" help="Only images not attached to another VM are listed.">
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        <option value="">Select…</option>
        {free.map((v) => <option key={v.name} value={v.name}>{v.name} ({bytes(v.capacity)}, {v.format})</option>)}
      </select>
    </Field>
  );
}
