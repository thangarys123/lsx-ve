import { Fragment, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, enc, uploadFile, uploadIso } from "../api";
import { Icon } from "../icons";
import { useSession } from "../session";
import { Btn, Confirm, DataTable, ErrorBox, Field, Loading, Modal, Panel, Tabs, useAction, useAsync, useToast } from "../ui";
import { bytes, pct } from "../util";
import { VMWizard } from "./VMWizard";

export function StoragePage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "datastores";
  return (
    <div className="page">
      <div className="page-head"><h1><Icon name="storage" size={20} /> Storage</h1></div>
      <Tabs tabs={[["datastores", "Datastores"], ["browser", "Datastore browser"], ["isos", "ISO library"], ["devices", "Devices"], ["filesystems", "File systems & LVM"]]}
        value={tab} onChange={(t) => setParams({ tab: t })} />
      {tab === "datastores" && <Datastores onBrowse={(n) => setParams({ tab: "browser", ds: n })} />}
      {tab === "browser" && <Browser initial={params.get("ds") ?? "default"} />}
      {tab === "isos" && <IsoLibrary />}
      {tab === "devices" && <Devices />}
      {tab === "filesystems" && <Filesystems />}
    </div>
  );
}

const TYPE_LABEL: Record<string, string> = { dir: "Local directory", netfs: "NFS", logical: "LVM volume group", iscsi: "iSCSI", disk: "Disk", fs: "File system" };

function Datastores({ onBrowse }: { onBrowse: (n: string) => void }) {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get<any[]>("/api/v1/storage/datastores"));
  const [sel, setSel] = useState<string[]>([]);
  const [create, setCreate] = useState(false);
  const [remove, setRemove] = useState(false);
  const { run, busy } = useAction();
  const cur = data?.find((p) => p.name === sel[0]);
  const act = (a: string) => cur && run(() => api.post(`/api/v1/storage/datastores/${enc(cur.name)}/actions/${a}`)).then(reload);
  const m = can("storage.manage");
  return (
    <>
      <div className="toolbar">
        <Btn icon="plus" disabled={!m} onClick={() => setCreate(true)}>New datastore</Btn>
        <Btn icon="folder" disabled={!cur} onClick={() => cur && onBrowse(cur.name)}>Browse</Btn>
        <div className="sep" />
        <Btn icon="play" disabled={!m || !cur || cur.active || busy} onClick={() => act("start")}>Activate</Btn>
        <Btn icon="stop" disabled={!m || !cur || !cur.active || cur.is_iso_library || busy} onClick={() => act("stop")}>Deactivate</Btn>
        <Btn icon="refresh" disabled={!m || !cur || !cur.active} onClick={() => act("refresh")}>Rescan</Btn>
        <Btn icon="power" disabled={!m || !cur} onClick={() => act(cur?.autostart ? "autostart_off" : "autostart_on")}>{cur?.autostart ? "Disable autostart" : "Enable autostart"}</Btn>
        <Btn icon="trash" kind="danger" disabled={!m || !cur || cur.name === "default" || cur.is_iso_library} onClick={() => setRemove(true)}>Remove</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <ErrorBox error={error} onRetry={reload} />
      <Panel flush>
        <DataTable rowKey={(r: any) => r.name} rows={data ?? []} selected={sel} onSelect={setSel} onOpen={(r: any) => onBrowse(r.name)}
          cols={[
            { key: "name", label: "Name", sort: (r: any) => r.name, render: (r: any) => <><Icon name={r.is_iso_library ? "iso" : "storage"} size={14} style={{ verticalAlign: -2, marginRight: 6 }} /><b>{r.name}</b>{r.is_iso_library && <span className="badge" style={{ marginLeft: 6 }}>ISO library</span>}</> },
            { key: "state", label: "State", render: (r: any) => <span className={`status ${r.active ? "active" : "inactive"}`}>{r.state}</span> },
            { key: "type", label: "Type", render: (r: any) => TYPE_LABEL[r.type] ?? r.type },
            { key: "capacity", label: "Capacity", num: true, sort: (r: any) => r.capacity, render: (r: any) => bytes(r.capacity) },
            { key: "available", label: "Free", num: true, sort: (r: any) => r.available, render: (r: any) => bytes(r.available) },
            { key: "usage", label: "Used", render: (r: any) => {
              const p = r.capacity ? (r.allocation / r.capacity) * 100 : 0;
              return <span className="row" style={{ gap: 8 }}><span className={`bar ${p > 90 ? "err" : p > 75 ? "warn" : ""}`} style={{ width: 90, marginTop: 0 }}><i style={{ width: `${p}%` }} /></span>{pct(p)}</span>;
            } },
            { key: "volumes", label: "Items", num: true },
            { key: "source", label: "Location", render: (r: any) => <span className="mono small">{r.type === "netfs" ? `${r.host}:${r.export}` : r.type === "iscsi" ? `${r.host} ${r.device}` : r.type === "logical" ? `VG ${r.vg}` : r.path}</span> },
            { key: "autostart", label: "Autostart", render: (r: any) => r.autostart ? "Yes" : "No" },
          ]} />
      </Panel>
      {create && <NewDatastore onClose={() => { setCreate(false); setTimeout(reload, 1500); }} />}
      {remove && cur && <Confirm danger title={`Remove datastore ${cur.name}`} confirmLabel="Remove" onClose={() => setRemove(false)}
        message="The datastore is unregistered from LSX-VE. Files on the underlying storage are NOT erased and can be re-added later. Datastores used by VMs cannot be removed."
        onConfirm={() => run(() => api.del(`/api/v1/storage/datastores/${enc(cur.name)}`), "Datastore removed").then(() => { setSel([]); reload(); })} />}
    </>
  );
}

function NewDatastore({ onClose }: { onClose: () => void }) {
  const [type, setType] = useState("dir");
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [host, setHost] = useState("");
  const [exp, setExp] = useState("");
  const [vg, setVg] = useState("");
  const [iqn, setIqn] = useState("");
  const [port, setPort] = useState(3260);
  const { run, busy } = useAction();
  const submit = () => {
    const body: any = { type, name };
    if (type === "dir" || type === "netfs") body.path = path || `/lsxve/datastores/${name}`;
    if (type === "netfs") Object.assign(body, { host, export: exp });
    if (type === "logical") body.vg = vg;
    if (type === "iscsi") Object.assign(body, { host, iqn, port });
    run(() => api.post("/api/v1/storage/datastores", body), "Create datastore task submitted").then((r) => r && onClose());
  };
  return (
    <Modal title="New datastore" icon="storage" size="mid" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || !name} onClick={submit}>Create</Btn></>}>
      <div className="form">
        <Field label="Type">
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="dir">Local directory (on an existing mounted file system)</option>
            <option value="netfs">NFS share</option>
            <option value="iscsi">iSCSI target (LUNs exposed as raw volumes)</option>
            <option value="logical">Existing LVM volume group</option>
          </select>
        </Field>
        <div className="small muted">To create a datastore on a blank physical disk, use Storage → Devices → "Create datastore on disk".</div>
        <Field label="Datastore name"><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        {(type === "dir" || type === "netfs") && (
          <Field label={type === "netfs" ? "Local mount point" : "Directory"} help="Must be under /lsxve/, /var/lib/libvirt/images, /srv/, /mnt/ or /data/">
            <input type="text" value={path} placeholder={`/lsxve/datastores/${name || "<name>"}`} onChange={(e) => setPath(e.target.value)} />
          </Field>
        )}
        {(type === "netfs" || type === "iscsi") && <Field label={type === "netfs" ? "NFS server" : "iSCSI portal host"}><input type="text" value={host} onChange={(e) => setHost(e.target.value)} /></Field>}
        {type === "netfs" && <Field label="Export path"><input type="text" value={exp} placeholder="/exports/vms" onChange={(e) => setExp(e.target.value)} /></Field>}
        {type === "iscsi" && <div className="row">
          <Field label="Target IQN"><input type="text" value={iqn} placeholder="iqn.2003-01.org.example:target1" onChange={(e) => setIqn(e.target.value)} /></Field>
          <Field label="Port"><input type="number" value={port} onChange={(e) => setPort(Number(e.target.value))} style={{ width: 100 }} /></Field>
        </div>}
        {type === "logical" && <Field label="Volume group name" help="See File systems & LVM for existing groups."><input type="text" value={vg} onChange={(e) => setVg(e.target.value)} /></Field>}
      </div>
    </Modal>
  );
}

function Browser({ initial }: { initial: string }) {
  const { can } = useSession();
  const pools = useAsync(() => api.get<any[]>("/api/v1/storage/datastores"));
  const [pool, setPool] = useState(initial);
  const { data, error, reload } = useAsync(() => api.get<any[]>(`/api/v1/storage/datastores/${enc(pool)}/volumes`), [pool]);
  const [sel, setSel] = useState<string[]>([]);
  const [create, setCreate] = useState(false);
  const [resize, setResize] = useState(false);
  const [del, setDel] = useState(false);
  const [imp, setImp] = useState(false);
  const [reg, setReg] = useState(false);
  const [up, setUp] = useState<{ name: string; loaded: number; total: number } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const { run } = useAction();
  const cur = data?.find((v) => v.name === sel[0]);
  const m = can("storage.manage");
  const vmm = can("vm.manage");
  const lname = (cur?.name ?? "").toLowerCase();
  const importable = !!cur && /\.(ova|ovf|vmdk|qcow2|img|raw|vhd|vhdx|vdi)$/.test(lname);
  const registrable = !!cur && cur.used_by.length === 0 && (cur.format === "qcow2" || cur.format === "raw") && !/\.(iso|ova|ovf)$/.test(lname);
  const upload = async (f: File) => {
    if (!/^[A-Za-z0-9][A-Za-z0-9 _.()+-]{0,200}$/.test(f.name) || !/\.(ova|ovf|vmdk|qcow2|img|raw|vhd|vhdx|vdi|iso|mf|nvram)$/i.test(f.name)) {
      toast({ kind: "error", title: "Invalid file", detail: "Allowed: .ova .ovf .vmdk .qcow2 .img .raw .vhd(x) .vdi .iso .mf; names with letters, digits, spaces and . _ - ( ) +" });
      return;
    }
    setUp({ name: f.name, loaded: 0, total: f.size });
    try {
      await uploadFile(`/api/v1/storage/datastores/${enc(pool)}/files/${enc(f.name)}`, f, (l, t) => setUp({ name: f.name, loaded: l, total: t }));
      toast({ kind: "success", title: "Upload complete", detail: f.name });
      reload();
    } catch (e: any) {
      toast({ kind: "error", title: "Upload failed", detail: e.message });
    } finally {
      setUp(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  };
  return (
    <>
      <div className="toolbar">
        <select value={pool} onChange={(e) => { setPool(e.target.value); setSel([]); }} style={{ width: 200 }}>
          {(pools.data ?? []).map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
        </select>
        <div className="sep" />
        <Btn icon="upload" disabled={!m || !!up} onClick={() => fileRef.current?.click()}>Upload</Btn>
        <input ref={fileRef} type="file" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
        <Btn icon="down" disabled={!cur} onClick={() => cur && window.open(`/api/v1/storage/datastores/${enc(pool)}/files/${enc(cur.name)}`, "_blank")}>Download</Btn>
        <div className="sep" />
        <Btn icon="plus" disabled={!m} onClick={() => setCreate(true)}>New virtual disk</Btn>
        <Btn icon="edit" disabled={!m || !cur || cur.used_by.length > 0} onClick={() => setResize(true)}>Grow</Btn>
        <Btn icon="trash" kind="danger" disabled={!m || !cur || cur.used_by.length > 0} onClick={() => setDel(true)}>Delete</Btn>
        <div className="sep" />
        <Btn icon="vm" disabled={!vmm || !importable} onClick={() => setImp(true)} title="Create a VM from an OVA / OVF package or a VMDK / VHD / VDI / qcow2 image (disks are converted to qcow2)">Import VM</Btn>
        <Btn icon="vm" disabled={!vmm || !registrable} onClick={() => setReg(true)} title="Create a VM that uses this qcow2 / raw disk in place">Register VM</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      {up && (
        <div className="alert info"><Icon name="upload" /><div className="grow">
          Uploading <b>{up.name}</b> to {pool} — {bytes(up.loaded)} of {bytes(up.total)} ({pct((up.loaded / Math.max(1, up.total)) * 100)})
          <div className="bar"><i style={{ width: `${(up.loaded / Math.max(1, up.total)) * 100}%` }} /></div>
          <div className="small muted">Keep this page open until the upload completes. For an OVF package upload the .ovf and every .vmdk it references.</div>
        </div></div>
      )}
      <ErrorBox error={error} onRetry={reload} />
      <Panel flush>
        <DataTable rowKey={(r: any) => r.name} rows={data ?? []} selected={sel} onSelect={setSel} empty="Empty datastore"
          cols={[
            { key: "name", label: "Name", sort: (r: any) => r.name, render: (r: any) => <span className="mono">{r.name}</span> },
            { key: "format", label: "Format" },
            { key: "capacity", label: "Provisioned size", num: true, sort: (r: any) => r.capacity, render: (r: any) => bytes(r.capacity) },
            { key: "allocation", label: "Space used", num: true, sort: (r: any) => r.allocation, render: (r: any) => bytes(r.allocation) },
            { key: "thin", label: "Provisioning", render: (r: any) => r.allocation < r.capacity * 0.98 ? <span className="badge blue">thin</span> : <span className="badge">thick</span> },
            { key: "used_by", label: "Used by", render: (r: any) => r.used_by.length ? r.used_by.join(", ") : <span className="muted">—</span> },
            { key: "path", label: "Path", render: (r: any) => <span className="mono small muted">{r.path}</span> },
          ]} />
      </Panel>
      {create && <NewVolume pool={pool} onClose={() => { setCreate(false); setTimeout(reload, 1500); }} />}
      {imp && cur && <ImportVm pool={pool} file={cur.name} pools={pools.data ?? []} onClose={() => setImp(false)} />}
      {reg && cur && <VMWizard initialDisk={{ datastore: pool, existing: cur.name, name: cur.name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_-]/g, "-").slice(0, 60) }} onClose={() => { setReg(false); reload(); }} />}
      {resize && cur && <ResizeVolume pool={pool} vol={cur} onClose={() => { setResize(false); setTimeout(reload, 1500); }} />}
      {del && cur && <Confirm danger typed={cur.name} title={`Delete ${cur.name}`} confirmLabel="Delete" onClose={() => setDel(false)}
        message="The file and all data in it are permanently deleted." onConfirm={() => run(() => api.del(`/api/v1/storage/datastores/${enc(pool)}/volumes/${enc(cur.name)}`), "Virtual disk deleted").then(() => { setSel([]); reload(); })} />}
    </>
  );
}

function NewVolume({ pool, onClose }: { pool: string; onClose: () => void }) {
  const [name, setName] = useState("");
  const [size, setSize] = useState(20);
  const [fmt, setFmt] = useState("qcow2");
  const [prov, setProv] = useState("thin");
  const { run, busy } = useAction();
  return (
    <Modal title={`New virtual disk in ${pool}`} icon="disk" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || !name}
        onClick={() => run(() => api.post(`/api/v1/storage/datastores/${enc(pool)}/volumes`, { name, size_gib: size, format: fmt, provisioning: prov }), "Task submitted").then((r) => r && onClose())}>Create</Btn></>}>
      <div className="form">
        <Field label="File name"><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <div className="row">
          <Field label="Size"><div className="unit-input"><input type="number" min={1} value={size} onChange={(e) => setSize(Number(e.target.value))} /><span>GiB</span></div></Field>
          <Field label="Format"><select value={fmt} onChange={(e) => setFmt(e.target.value)}><option value="qcow2">qcow2 (snapshots, thin)</option><option value="raw">raw</option></select></Field>
          <Field label="Provisioning"><select value={prov} onChange={(e) => setProv(e.target.value)}><option value="thin">Thin</option><option value="thick">Thick</option></select></Field>
        </div>
      </div>
    </Modal>
  );
}

function ResizeVolume({ pool, vol, onClose }: { pool: string; vol: any; onClose: () => void }) {
  const curG = Math.ceil(vol.capacity / 1024 ** 3);
  const [size, setSize] = useState(curG + 10);
  const { run, busy } = useAction();
  return (
    <Modal title={`Grow ${vol.name}`} icon="disk" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || size <= curG}
        onClick={() => run(() => api.post(`/api/v1/storage/datastores/${enc(pool)}/volumes/${enc(vol.name)}/resize`, { size_gib: size }), "Task submitted").then((r) => r && onClose())}>Grow</Btn></>}>
      <div className="form">
        <div className="small muted">Current size {bytes(vol.capacity)}. Disks can only be grown; extend the partition and file system inside the guest afterwards.</div>
        <Field label="New size"><div className="unit-input"><input type="number" min={curG + 1} value={size} onChange={(e) => setSize(Number(e.target.value))} /><span>GiB</span></div></Field>
      </div>
    </Modal>
  );
}

function IsoLibrary() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get<any[]>("/api/v1/storage/isos"));
  const [sel, setSel] = useState<string[]>([]);
  const [up, setUp] = useState<{ name: string; loaded: number; total: number } | null>(null);
  const [del, setDel] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const { run } = useAction();
  const cur = data?.find((v) => v.name === sel[0]);
  const upload = async (f: File) => {
    if (!/^[A-Za-z0-9][A-Za-z0-9 _.()+-]{0,200}$/.test(f.name) || !/\.(iso|img)$/i.test(f.name)) {
      toast({ kind: "error", title: "Invalid file name", detail: "Use letters, digits, spaces and . _ - ( ) + ; extension .iso or .img" });
      return;
    }
    setUp({ name: f.name, loaded: 0, total: f.size });
    try {
      await uploadIso(f, (l, t) => setUp({ name: f.name, loaded: l, total: t }));
      toast({ kind: "success", title: "Upload complete", detail: f.name });
      reload();
    } catch (e: any) {
      toast({ kind: "error", title: "Upload failed", detail: e.message });
    } finally {
      setUp(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  };
  return (
    <>
      <div className="toolbar">
        <Btn icon="upload" disabled={!can("storage.manage") || !!up} onClick={() => fileRef.current?.click()}>Upload ISO image</Btn>
        <input ref={fileRef} type="file" accept=".iso,.img,.tar,.tar.gz,.tgz,.tar.xz,.txz,.tar.zst,.tar.bz2" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
        <Btn icon="trash" kind="danger" disabled={!can("storage.manage") || !cur || cur.used_by.length > 0} onClick={() => setDel(true)}>Delete</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      {up && (
        <div className="alert info"><Icon name="upload" /><div className="grow">
          Uploading <b>{up.name}</b> — {bytes(up.loaded)} of {bytes(up.total)} ({pct((up.loaded / Math.max(1, up.total)) * 100)})
          <div className="bar"><i style={{ width: `${(up.loaded / Math.max(1, up.total)) * 100}%` }} /></div>
          <div className="small muted">Keep this page open until the upload completes.</div>
        </div></div>
      )}
      <ErrorBox error={error} onRetry={reload} />
      <Panel flush>
        <DataTable rowKey={(r: any) => r.name} rows={data ?? []} selected={sel} onSelect={setSel} empty="No ISO images. Upload installation media to create virtual machines."
          cols={[
            { key: "name", label: "Image", sort: (r: any) => r.name, render: (r: any) => <><Icon name="iso" size={14} style={{ verticalAlign: -2, marginRight: 6 }} />{r.name}</> },
            { key: "capacity", label: "Size", num: true, sort: (r: any) => r.capacity, render: (r: any) => bytes(r.capacity) },
            { key: "used_by", label: "Mounted in", render: (r: any) => r.used_by.join(", ") || <span className="muted">—</span> },
            { key: "path", label: "Path", render: (r: any) => <span className="mono small muted">{r.path}</span> },
          ]} />
      </Panel>
      {del && cur && <Confirm danger title={`Delete ${cur.name}`} confirmLabel="Delete" onClose={() => setDel(false)} message="The image is permanently deleted from the ISO library."
        onConfirm={() => run(() => api.del(`/api/v1/storage/isos/${enc(cur.name)}`), "Image deleted").then(() => { setSel([]); reload(); })} />}
    </>
  );
}

function Devices() {
  const { can } = useSession();
  const { data, error, reload, loading } = useAsync(() => api.get<any[]>("/api/v1/storage/disks"));
  const [sel, setSel] = useState<string[]>([]);
  const [smart, setSmart] = useState<string | null>(null);
  const [fmt, setFmt] = useState(false);
  const cur = data?.find((d) => d.name === sel[0]);
  return (
    <>
      <div className="toolbar">
        <Btn icon="health" disabled={!cur} onClick={() => cur && setSmart(cur.name)}>SMART health</Btn>
        <Btn icon="plus" kind="danger" disabled={!can("storage.format") || !cur || cur.check?.in_use} onClick={() => setFmt(true)}>Create datastore on disk</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={reload} disabled={loading}>Rescan</Btn>
      </div>
      <ErrorBox error={error} onRetry={reload} />
      <Panel flush>
        <DataTable rowKey={(r: any) => r.name} rows={data ?? []} selected={sel} onSelect={setSel}
          cols={[
            { key: "name", label: "Device", sort: (r: any) => r.name, render: (r: any) => <span className="mono">{r.path}</span> },
            { key: "model", label: "Model", render: (r: any) => [r.vendor, r.model].filter(Boolean).join(" ").trim() || "—" },
            { key: "serial", label: "Serial", render: (r: any) => <span className="mono small">{r.serial ?? "—"}</span> },
            { key: "size", label: "Capacity", num: true, sort: (r: any) => r.size, render: (r: any) => bytes(r.size) },
            { key: "tran", label: "Transport", render: (r: any) => (r.tran ?? "—").toUpperCase() },
            { key: "rota", label: "Type", render: (r: any) => r.rota ? "HDD" : "SSD / flash" },
            { key: "use", label: "In use", render: (r: any) => r.check?.in_use ? <span title={r.check.reasons.join("\n")} className="badge amber">{r.check.reasons[0]}</span> : <span className="badge green">available</span> },
            { key: "parts", label: "Contents", render: (r: any) => <span className="small muted">{(r.children ?? []).map((c: any) => `${c.name}${c.fstype ? ` (${c.fstype})` : ""}${c.mountpoint ? ` → ${c.mountpoint}` : ""}`).join(", ") || (r.fstype ?? "empty")}</span> },
          ]} />
      </Panel>
      {smart && <SmartDialog dev={smart} onClose={() => setSmart(null)} />}
      {fmt && cur && <FormatDialog disk={cur} onClose={() => { setFmt(false); reload(); }} />}
    </>
  );
}

function SmartDialog({ dev, onClose }: { dev: string; onClose: () => void }) {
  const { data, error } = useAsync(() => api.get(`/api/v1/storage/disks/${enc(dev)}/smart`));
  return (
    <Modal title={`SMART health — /dev/${dev}`} icon="health" size="mid" onClose={onClose} footer={<Btn onClick={onClose}>Close</Btn>}>
      <ErrorBox error={error} />
      {!data ? <Loading /> : !data.available ? <div className="alert warn"><Icon name="warn" /><div>SMART data is not available for this device{data.messages?.length ? `: ${data.messages.join("; ")}` : " (virtual disks and some RAID controllers do not expose SMART)."}</div></div> : (
        <div className="form">
          <div className="kv">
            <div>Overall assessment</div><div>{data.passed === true ? <span className="status ok">PASSED</span> : data.passed === false ? <span className="status error">FAILED</span> : "—"}</div>
            <div>Model / serial</div><div>{data.model} · <span className="mono">{data.serial}</span></div>
            <div>Firmware</div><div>{data.firmware}</div>
            <div>Temperature</div><div>{data.temperature != null ? `${data.temperature} °C` : "—"}</div>
            <div>Power-on hours</div><div>{data.power_on_hours ?? "—"}</div>
            <div>Power cycles</div><div>{data.power_cycles ?? "—"}</div>
          </div>
          {data.attributes.length > 0 && (
            <table className="dt"><thead><tr><th>ID</th><th>Attribute</th><th className="num">Value</th><th className="num">Worst</th><th className="num">Threshold</th><th>Raw</th></tr></thead>
              <tbody>{data.attributes.map((a: any) => <tr key={a.id} style={a.failing ? { color: "#ff9b94" } : undefined}><td>{a.id}</td><td>{a.name}</td><td className="num">{a.value}</td><td className="num">{a.worst}</td><td className="num">{a.thresh}</td><td className="mono small">{a.raw}</td></tr>)}</tbody></table>
          )}
          {data.nvme && <div className="kv">{Object.entries(data.nvme).map(([k, v]) => <Fragment key={k}><div>{k.replace(/_/g, " ")}</div><div className="mono">{String(v)}</div></Fragment>)}</div>}
        </div>
      )}
    </Modal>
  );
}

function FormatDialog({ disk, onClose }: { disk: any; onClose: () => void }) {
  const [kind, setKind] = useState("xfs");
  const [name, setName] = useState("");
  const [confirm, setConfirm] = useState("");
  const { run, busy } = useAction();
  const path = disk.path;
  const existing: string[] = disk.check?.existing_data ?? [];
  return (
    <Modal title={`Create datastore on ${path}`} icon="warn" size="mid" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="danger solid" disabled={busy || !name || confirm !== path}
        onClick={() => run(() => api.post(`/api/v1/storage/disks/${enc(disk.name)}/format`, { kind, name, confirm }), "Format task submitted").then((r) => r && onClose())}>Erase disk and create datastore</Btn></>}>
      <div className="form">
        <div className="alert err"><Icon name="warn" /><div><b>All data on {path} will be permanently destroyed.</b><br />
          {[disk.vendor, disk.model].filter(Boolean).join(" ")} · serial <span className="mono">{disk.serial ?? "unknown"}</span> · {bytes(disk.size)}
          {existing.length > 0 && <div style={{ marginTop: 6 }}>Existing signatures found: <span className="mono">{existing.join(", ")}</span></div>}
        </div></div>
        <Field label="Datastore name"><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Format">
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="xfs">XFS file system (recommended; qcow2 thin provisioning and snapshots)</option>
            <option value="ext4">ext4 file system</option>
            <option value="lvm">LVM volume group (raw logical volumes; no qcow2 snapshots)</option>
          </select>
        </Field>
        <Field label={<>Type <code>{path}</code> to confirm</>}><input type="text" value={confirm} onChange={(e) => setConfirm(e.target.value)} spellCheck={false} /></Field>
      </div>
    </Modal>
  );
}

function Filesystems() {
  const { data, error, reload } = useAsync(() => api.get("/api/v1/storage/filesystems"));
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <Loading />;
  return (
    <div className="grid">
      {data.error && <div className="alert warn"><Icon name="warn" /><div>{data.error}</div></div>}
      <Panel title="Mounted file systems" icon="folder" flush>
        <DataTable rowKey={(r: any) => r.target} rows={data.mounts} cols={[
          { key: "target", label: "Mount point", render: (r: any) => <span className="mono">{r.target}</span> },
          { key: "source", label: "Device", render: (r: any) => <span className="mono small">{r.source}</span> },
          { key: "fstype", label: "Type" },
          { key: "size", label: "Size", num: true, render: (r: any) => bytes(r.size) },
          { key: "used", label: "Used", num: true, render: (r: any) => bytes(r.used) },
          { key: "avail", label: "Free", num: true, render: (r: any) => bytes(r.avail) },
          { key: "pct", label: "Usage", render: (r: any) => { const p = r.size ? (r.used / r.size) * 100 : 0; return <span className="row" style={{ gap: 8 }}><span className={`bar ${p > 90 ? "err" : p > 75 ? "warn" : ""}`} style={{ width: 80, marginTop: 0 }}><i style={{ width: `${p}%` }} /></span>{pct(p)}</span>; } },
        ]} />
      </Panel>
      <div className="grid c2">
        <Panel title="LVM volume groups" icon="storage" flush>
          <DataTable rowKey={(r: any) => r.vg_name} rows={data.lvm.vgs} cols={[
            { key: "vg_name", label: "Volume group" }, { key: "vg_size", label: "Size", num: true, render: (r: any) => bytes(Number(r.vg_size)) },
            { key: "vg_free", label: "Free", num: true, render: (r: any) => bytes(Number(r.vg_free)) }, { key: "pv_count", label: "PVs", num: true }, { key: "lv_count", label: "LVs", num: true },
          ]} />
        </Panel>
        <Panel title="LVM logical volumes" icon="disk" flush>
          <DataTable rowKey={(r: any) => r.vg_name + r.lv_name} rows={data.lvm.lvs} cols={[
            { key: "lv_name", label: "Logical volume" }, { key: "vg_name", label: "VG" },
            { key: "lv_size", label: "Size", num: true, render: (r: any) => bytes(Number(r.lv_size)) },
            { key: "pool_lv", label: "Thin pool", render: (r: any) => r.pool_lv || "—" },
            { key: "data_percent", label: "Data %", num: true, render: (r: any) => r.data_percent || "—" },
          ]} />
        </Panel>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ import OVF / OVA / VMDK */
function ImportVm({ pool, file, pools, onClose }: { pool: string; file: string; pools: any[]; onClose: () => void }) {
  const info = useAsync(() => api.post("/api/v1/vms/import/inspect", { datastore: pool, file }));
  const nets = useAsync(() => api.get<any[]>("/api/v1/network/virtual").catch(() => []));
  const hv = useAsync(() => api.get("/api/v1/hypervisor"));
  const [f, setF] = useState<any>(null);
  const { run, busy } = useAction();
  const d = info.data;
  if (d && !f) {
    const win = d.os_family === "windows";
    setF({
      name: d.name, vcpus: d.vcpus, memory_mib: d.memory_mib, firmware: d.firmware, os_type: d.os_family in (hv.data?.os_types ?? { linux: 1 }) ? d.os_family : "linux",
      os_variant: d.os_label || "", network: "default", nic_model: win ? "e1000e" : (d.nics?.[0]?.model || "virtio"),
      nic_count: Math.max(d.nics?.length ?? 1, 1), disk_bus: "sata", target_datastore: pool, power_on: false,
    });
  }
  const set = (p: any) => setF({ ...f, ...p });
  const targets = pools.filter((p) => p.active && !p.is_iso_library);
  const need = (d?.disks ?? []).reduce((a: number, x: any) => a + (x.capacity || 0), 0);
  const tgt = targets.find((p) => p.name === f?.target_datastore);
  return (
    <Modal title={`Import virtual machine from ${file}`} icon="vm" size="mid" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={!f || busy || !!d?.missing?.length || !f.name}
        onClick={() => run(() => api.post("/api/v1/vms/import", { ...f, datastore: pool, file, description: `Imported from ${file}` }), "Import started — see Recent tasks").then((r) => r && onClose())}>Import</Btn></>}>
      {info.error ? <ErrorBox error={info.error} /> : !f ? <Loading /> : <div className="form">
        {d.missing?.length > 0 && <div className="alert err"><Icon name="error" /><div>Disk files referenced by the descriptor are missing: <span className="mono">{d.missing.join(", ")}</span>. Upload them to this datastore first.</div></div>}
        <div className="small muted">{d.kind === "ovf" ? `OVF descriptor: ${d.disks.length} disk(s), ${d.nics?.length ?? 0} network adapter(s)${d.os_label ? `, guest OS "${d.os_label}"` : ""}.` : `Disk image (${d.disks[0]?.format ?? "unknown"} format).`}
          {" "}Disks are converted to qcow2 ({bytes(need)} provisioned) — the source file is not modified.</div>
        <div className="grid c2">
          <Field label="VM name"><input type="text" value={f.name} onChange={(e) => set({ name: e.target.value })} /></Field>
          <Field label="Target datastore" error={tgt && need > tgt.available ? "Not enough free space for thick disks (thin qcow2 usually needs much less)" : null}>
            <select value={f.target_datastore} onChange={(e) => set({ target_datastore: e.target.value })}>{targets.map((p) => <option key={p.name} value={p.name}>{p.name} ({bytes(p.available)} free)</option>)}</select>
          </Field>
          <Field label="vCPUs"><input type="number" min={1} max={hv.data?.host_threads ?? 64} value={f.vcpus} onChange={(e) => set({ vcpus: Number(e.target.value) })} /></Field>
          <Field label="Memory"><span className="unit-input"><input type="number" min={128} value={f.memory_mib} onChange={(e) => set({ memory_mib: Number(e.target.value) })} /><span>MiB</span></span></Field>
          <Field label="Guest OS family">
            <select value={f.os_type} onChange={(e) => set({ os_type: e.target.value })}>{Object.entries(hv.data?.os_types ?? { linux: "Linux" }).map(([k, l]) => <option key={k} value={k}>{String(l)}</option>)}</select>
          </Field>
          <Field label="Firmware"><select value={f.firmware} onChange={(e) => set({ firmware: e.target.value })}><option value="bios">BIOS</option><option value="uefi">UEFI</option></select></Field>
          <Field label="Disk controller" help="SATA boots any guest; VirtIO needs drivers (LSX-VE Guest Tools on Windows)">
            <select value={f.disk_bus} onChange={(e) => set({ disk_bus: e.target.value })}>{(hv.data?.disk_buses ?? ["sata"]).map((b: string) => <option key={b} value={b}>{b}</option>)}</select>
          </Field>
          <Field label="Network adapters">
            <div className="row" style={{ gap: 6 }}>
              <input type="number" min={0} max={8} value={f.nic_count} style={{ width: 70 }} onChange={(e) => set({ nic_count: Number(e.target.value) })} />
              <select value={f.nic_model} onChange={(e) => set({ nic_model: e.target.value })}>{(hv.data?.nic_models ?? ["virtio"]).map((m: string) => <option key={m} value={m}>{m}</option>)}</select>
              <select value={f.network} onChange={(e) => set({ network: e.target.value })}>{(nets.data ?? []).map((n: any) => <option key={n.name} value={n.name}>{n.name}</option>)}</select>
            </div>
          </Field>
        </div>
        <label className="check"><input type="checkbox" checked={f.power_on} onChange={(e) => set({ power_on: e.target.checked })} /> Power on after import</label>
      </div>}
    </Modal>
  );
}
