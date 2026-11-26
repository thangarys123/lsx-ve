// LXC containers: list, create, detail (summary, snapshots, configuration), clone, delete.
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, enc } from "../api";
import { Icon } from "../icons";
import { useLive } from "../live";
import { useSession } from "../session";
import { Btn, Confirm, DataTable, Dropdown, ErrorBox, Field, Loading, Modal, Panel, Pill, Tabs, useAction, useAsync } from "../ui";
import { bytes, cls, mib, ts } from "../util";

const CT_LABEL: Record<string, string> = {
  running: "Running", stopped: "Stopped", frozen: "Frozen", starting: "Starting", stopping: "Stopping", unknown: "Unknown",
};

export function CtState({ state }: { state: string }) {
  return <span className={cls("status", state)}>{CT_LABEL[state] ?? state}</span>;
}

export function openCtConsole(name: string) {
  window.open(`/ct-console/${enc(name)}`, `lsxve-ct-console-${name}`, "width=1000,height=700,noopener");
}

type Ct = {
  name: string; state: string; ips: string[]; memory_mib: number; memory_used: number | null; cpus: number;
  autostart: boolean; unprivileged: boolean; bridge: string; os: string; description: string; snapshots: number;
  ipv4: string; gateway: string; mac: string; disk_gib: number | null; created: number | null; created_by: string; source: string; error?: string;
};

function useCtStatus() {
  return useAsync(() => api.get("/api/v1/containers/status"));
}

/* ------------------------------------------------------------------ power buttons */
function CtPowerButtons({ ct, onChanged }: { ct: Ct | null; onChanged: () => void }) {
  const { can } = useSession();
  const { run } = useAction();
  const [confirm, setConfirm] = useState<null | "stop">(null);
  if (!can("ct.power") && !can("ct.console")) return null;
  const power = (a: string) => run(() => api.post(`/api/v1/containers/${enc(ct!.name)}/power/${a}`)).then(onChanged);
  const st = ct?.state;
  return (
    <>
      {can("ct.power") && <>
        <Btn icon="play" disabled={!ct || st !== "stopped"} onClick={() => power("start")}>Start</Btn>
        <Btn icon="power" disabled={!ct || st !== "running"} onClick={() => power("shutdown")}>Shut down</Btn>
        <Btn icon="restart" disabled={!ct || st !== "running"} onClick={() => power("reboot")}>Restart</Btn>
        <Dropdown label="Power" icon="more" disabled={!ct || st === "stopped"} items={[
          { label: "Freeze", icon: "pause", disabled: st !== "running", onClick: () => power("freeze") },
          { label: "Resume", icon: "play", disabled: st !== "frozen", onClick: () => power("unfreeze") },
          "sep",
          { label: "Stop (kill immediately)", icon: "stop", danger: true, onClick: () => setConfirm("stop") },
        ]} />
      </>}
      {can("ct.console") && <Btn icon="console" disabled={!ct || st !== "running"} onClick={() => openCtConsole(ct!.name)}>Console</Btn>}
      {confirm && ct && <Confirm title={`Stop ${ct.name}`} danger confirmLabel="Stop"
        message="All processes in the container are killed immediately. Unsaved data may be lost."
        onConfirm={() => power("stop")} onClose={() => setConfirm(null)} />}
    </>
  );
}

/* ------------------------------------------------------------------ list */
export function ContainerList() {
  const { can } = useSession();
  const { ctVersion } = useLive();
  const status = useCtStatus();
  const { data, error, reload } = useAsync(() => api.get<Ct[]>("/api/v1/containers"), [ctVersion]);
  const [sel, setSel] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [dlg, setDlg] = useState<null | "create" | "clone" | "delete">(null);
  const nav = useNavigate();
  useEffect(() => {
    const t = setInterval(reload, 10000);
    return () => clearInterval(t);
  }, [reload]);
  const rows = data ?? [];
  const one = rows.find((r) => sel.length === 1 && r.name === sel[0]) ?? null;
  const st = status.data;
  return (
    <div className="page">
      <div className="page-head">
        <h1><Icon name="containers" size={20} /> Containers</h1>
        <span className="meta">{rows.filter((r) => r.state === "running").length} running · {rows.length} total{st?.version ? ` · LXC ${st.version}` : ""}</span>
      </div>
      {st && !st.available && <div className="alert err"><Icon name="error" /><div>LXC is not installed on this host.</div></div>}
      <ErrorBox error={status.error} onRetry={status.reload} />
      <div className="toolbar">
        {can("ct.manage") && <Btn icon="plus" disabled={!st?.available} onClick={() => setDlg("create")}>Create container</Btn>}
        <CtPowerButtons ct={one} onChanged={reload} />
        <div className="sep" />
        {can("ct.manage") && <Dropdown label="Actions" icon="more" disabled={!one} items={[
          { label: "Edit settings", icon: "edit", onClick: () => nav(`/containers/${enc(one!.name)}?tab=config`) },
          { label: "Clone", icon: "clone", disabled: one?.state !== "stopped", onClick: () => setDlg("clone") },
          "sep",
          { label: "Delete", icon: "trash", danger: true, disabled: one?.state !== "stopped", onClick: () => setDlg("delete") },
        ]} />}
        <div className="grow" />
        <input type="search" placeholder="Search" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 200 }} />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <ErrorBox error={error} onRetry={reload} />
      <Panel flush>
        <DataTable rowKey={(r: Ct) => r.name} rows={rows} selected={sel} onSelect={setSel} filter={filter}
          onOpen={(r: Ct) => nav(`/containers/${enc(r.name)}`)}
          empty={<div className="empty-state"><h3>No containers</h3>
            {can("ct.manage") && st?.available && <Btn kind="primary" icon="plus" onClick={() => setDlg("create")}>Create a container</Btn>}</div>}
          cols={[
            { key: "name", label: "Name", sort: (r: Ct) => r.name.toLowerCase(), render: (r: Ct) => (
              <a onClick={(e) => { e.stopPropagation(); nav(`/containers/${enc(r.name)}`); }} style={{ cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6 }}>
                <Icon name="container" size={14} />{r.name}</a>) },
            { key: "state", label: "Status", sort: (r: Ct) => r.state, render: (r: Ct) => <CtState state={r.state} /> },
            { key: "os", label: "Operating system", sort: (r: Ct) => r.os, render: (r: Ct) => r.os || <span className="muted">—</span> },
            { key: "ips", label: "IP addresses", render: (r: Ct) => <span className="mono">{r.ips?.join(", ") || "—"}</span> },
            { key: "cpus", label: "CPU limit", num: true, sort: (r: Ct) => r.cpus, render: (r: Ct) => r.cpus ? `${r.cpus}` : "host" },
            { key: "memory_mib", label: "Memory", num: true, sort: (r: Ct) => r.memory_mib, render: (r: Ct) => (
              <span>{r.memory_used != null ? `${bytes(r.memory_used)} / ` : ""}{r.memory_mib ? mib(r.memory_mib) : "unlimited"}</span>) },
            { key: "disk_gib", label: "Disk", num: true, sort: (r: Ct) => r.disk_gib ?? 0, render: (r: Ct) => r.disk_gib != null ? `${r.disk_gib} GiB` : <span className="muted">no limit</span> },
            { key: "unprivileged", label: "Type", render: (r: Ct) => r.unprivileged ? "Unprivileged" : <span className="badge">privileged</span> },
            { key: "autostart", label: "Autostart", render: (r: Ct) => r.autostart ? <span className="badge green">Yes</span> : <span className="muted">No</span> },
            { key: "snapshots", label: "Snapshots", num: true, render: (r: Ct) => r.snapshots ? <span><Icon name="snapshot" size={13} /> {r.snapshots}</span> : "" },
          ]} />
      </Panel>
      {dlg === "create" && st && <CreateContainer status={st} onClose={() => { setDlg(null); reload(); }} />}
      {dlg === "clone" && one && <CloneCt ct={one} onClose={() => setDlg(null)} />}
      {dlg === "delete" && one && <DeleteCt ct={one} onClose={() => { setDlg(null); setSel([]); }} />}
    </div>
  );
}

/* ------------------------------------------------------------------ create */
function BridgeSelect({ bridges, value, onChange }: { bridges: { name: string; network: string }[]; value: string; onChange: (v: string) => void }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      {bridges.length === 0 && <option value="">No bridge available</option>}
      {bridges.map((b) => <option key={b.name} value={b.name}>{b.name}{b.network === "default" ? " — physical network (bridged)" : b.network ? ` — virtual network "${b.network}"` : ""}</option>)}
    </select>
  );
}

function CreateContainer({ status, onClose }: { status: any; onClose: () => void }) {
  const { run, busy } = useAction();
  const bridges: { name: string; network: string }[] = status.bridges ?? [];
  const tarballs: { name: string; size: number }[] = status.templates ?? [];
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [srcType, setSrcType] = useState<"download" | "tarball">("download");
  const images = useAsync(() => srcType === "download" ? api.get("/api/v1/containers/images") : Promise.resolve(null), [srcType]);
  const [distro, setDistro] = useState("");
  const [release, setRelease] = useState("");
  const [variant, setVariant] = useState("default");
  const [file, setFile] = useState(tarballs[0]?.name ?? "");
  const [cpus, setCpus] = useState(1);
  const [memMiB, setMemMiB] = useState(512);
  const [diskGiB, setDiskGiB] = useState(8);
  const [bridge, setBridge] = useState(bridges.find((b) => b.network === "default")?.name ?? bridges[0]?.name ?? "");
  const [ipMode, setIpMode] = useState<"dhcp" | "static">("dhcp");
  const [ipv4, setIpv4] = useState("");
  const [gw, setGw] = useState("");
  const [unpriv, setUnpriv] = useState(true);
  const [autostart, setAutostart] = useState(false);
  const [start, setStart] = useState(true);
  const [pw1, setPw1] = useState("");
  const [pw2, setPw2] = useState("");

  const imgs: { distro: string; release: string; variant: string; build: string }[] = images.data?.images ?? [];
  const distros = useMemo(() => [...new Set(imgs.map((i) => i.distro))], [imgs]);
  const releases = useMemo(() => [...new Set(imgs.filter((i) => i.distro === distro).map((i) => i.release))], [imgs, distro]);
  const variants = useMemo(() => imgs.filter((i) => i.distro === distro && i.release === release), [imgs, distro, release]);
  useEffect(() => {
    if (!distro && distros.length) setDistro(distros.includes("debian") ? "debian" : distros[0]);
  }, [distros]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (releases.length && !releases.includes(release)) setRelease(releases[releases.length - 1]);
  }, [releases]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (variants.length && !variants.some((x) => x.variant === variant))
      setVariant(variants.some((x) => x.variant === "default") ? "default" : variants[0].variant);
  }, [variants]); // eslint-disable-line react-hooks/exhaustive-deps

  const errs: string[] = [];
  if (!/^[A-Za-z0-9][A-Za-z0-9-]{0,62}$/.test(name)) errs.push("Name: letters, digits and '-', at most 63 characters, starting with a letter or digit.");
  if (srcType === "download" && !(distro && release && variant)) errs.push("Choose an image.");
  if (srcType === "tarball" && !file) errs.push("Upload a rootfs archive to the ISO library first (Storage → ISO library).");
  if (!bridge) errs.push("A network bridge is required.");
  if (ipMode === "static" && !/^\d+\.\d+\.\d+\.\d+\/\d+$/.test(ipv4)) errs.push("Static address must include the prefix, e.g. 192.168.1.20/24.");
  if (pw1 && pw1 !== pw2) errs.push("Root passwords do not match.");

  const submit = async () => {
    const body: any = {
      name, description: desc, cpus, memory_mib: memMiB, disk_gib: diskGiB, bridge, unprivileged: unpriv, autostart, start,
      source: srcType === "download" ? { type: "download", distro, release, variant } : { type: "tarball", file },
      ipv4: ipMode === "dhcp" ? "dhcp" : ipv4, gateway: ipMode === "static" ? gw : "",
    };
    if (pw1) body.root_password = pw1;
    const r = await run(() => api.post("/api/v1/containers", body), `Creating container ${name}`);
    if (r) onClose();
  };

  return (
    <Modal title="Create container" icon="container" size="wide" onClose={onClose}
      footer={<>
        {errs.length > 0 && <span className="small muted grow">{errs[0]}</span>}
        <Btn onClick={onClose}>Cancel</Btn>
        <Btn kind="primary" disabled={busy || errs.length > 0} onClick={submit}>Create</Btn>
      </>}>
      <div className="grid c2">
        <div className="form">
          <h3>Name and image</h3>
          <Field label="Container name" help="Also used as the container's host name.">
            <input type="text" autoFocus value={name} maxLength={63} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Source">
            <Pill options={[["download", "Image server"], ["tarball", "Rootfs archive"]]} value={srcType} onChange={setSrcType} />
          </Field>
          {srcType === "download" && <>
            {images.error && <div className="alert err"><Icon name="error" /><div>{images.error.message}</div></div>}
            {images.loading && <div className="small muted">Loading the image list from {status.image_server}…</div>}
            {imgs.length > 0 && <div className="row">
              <Field label="Distribution">
                <select value={distro} onChange={(e) => { setDistro(e.target.value); setRelease(""); }}>
                  {distros.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
              </Field>
              <Field label="Release">
                <select value={release} onChange={(e) => setRelease(e.target.value)}>
                  {releases.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </Field>
              <Field label="Variant">
                <select value={variant} onChange={(e) => setVariant(e.target.value)}>
                  {variants.map((x) => <option key={x.variant} value={x.variant}>{x.variant}</option>)}
                </select>
              </Field>
            </div>}
            <div className="small muted">Images come from {status.image_server} ({status.arch}). The host downloads the image over HTTPS, so it needs Internet access.</div>
          </>}
          {srcType === "tarball" && <>
            <Field label="Rootfs archive" help="A .tar / .tar.gz / .tar.xz / .tar.zst / .tar.bz2 of a root filesystem, uploaded to the ISO library.">
              <select value={file} onChange={(e) => setFile(e.target.value)}>
                {tarballs.length === 0 && <option value="">No archive in the ISO library</option>}
                {tarballs.map((t) => <option key={t.name} value={t.name}>{t.name} ({bytes(t.size)})</option>)}
              </select>
            </Field>
          </>}
          <Field label="Notes"><textarea value={desc} maxLength={1000} onChange={(e) => setDesc(e.target.value)} style={{ fontFamily: "var(--sans)" }} /></Field>
        </div>
        <div className="form">
          <h3>Resources and network</h3>
          <div className="row">
            <Field label="CPU limit" help="0 = no limit (all host CPUs).">
              <input type="number" min={0} max={1024} value={cpus} onChange={(e) => setCpus(Math.max(0, Number(e.target.value) | 0))} />
            </Field>
            <Field label="Memory limit" help="0 = no limit.">
              <div className="unit-input"><input type="number" min={0} step={128} value={memMiB} onChange={(e) => setMemMiB(Math.max(0, Number(e.target.value) | 0))} /><span>MiB</span></div>
            </Field>
          </div>
          <Field label="Disk size" help="The root file system is an ext4 disk image of this size on the datastore (thin: space is used as files are written). 0 = no limit, a directory on the datastore.">
            <div className="unit-input"><input type="number" min={0} max={65536} value={diskGiB} onChange={(e) => setDiskGiB(Math.max(0, Number(e.target.value) | 0))} /><span>GiB</span></div>
          </Field>
          <Field label="Network bridge" help="eth0 of the container is connected to this bridge.">
            <BridgeSelect bridges={bridges} value={bridge} onChange={setBridge} />
          </Field>
          <Field label="IPv4">
            <Pill options={[["dhcp", "DHCP / from the image"], ["static", "Static"]]} value={ipMode} onChange={setIpMode} />
          </Field>
          {ipMode === "static" && <div className="row">
            <Field label="Address/prefix"><input type="text" className="mono" placeholder="192.168.1.50/24" value={ipv4} onChange={(e) => setIpv4(e.target.value)} /></Field>
            <Field label="Gateway"><input type="text" className="mono" placeholder="192.168.1.1" value={gw} onChange={(e) => setGw(e.target.value)} /></Field>
          </div>}
          <Field label="Root password" help="Optional. Sets the root password inside the container, so you can log in on the console.">
            <input type="password" autoComplete="new-password" value={pw1} onChange={(e) => setPw1(e.target.value)} />
          </Field>
          {pw1 && <Field label="Confirm root password"><input type="password" autoComplete="new-password" value={pw2} onChange={(e) => setPw2(e.target.value)} /></Field>}
          <label className="check"><input type="checkbox" checked={unpriv} onChange={(e) => setUnpriv(e.target.checked)} /> Unprivileged container (recommended)</label>
          {!unpriv && <div className="alert warn"><Icon name="warn" /><div>In a privileged container, root inside the container is root on the host. Use it only for trusted workloads.</div></div>}
          <label className="check"><input type="checkbox" checked={autostart} onChange={(e) => setAutostart(e.target.checked)} /> Start automatically with the host</label>
          <label className="check"><input type="checkbox" checked={start} onChange={(e) => setStart(e.target.checked)} /> Start after creation</label>
        </div>
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------ dialogs */
function CloneCt({ ct, onClose }: { ct: Ct; onClose: () => void }) {
  const [name, setName] = useState(`${ct.name}-clone`.slice(0, 63));
  const { run, busy } = useAction();
  const ok = /^[A-Za-z0-9][A-Za-z0-9-]{0,62}$/.test(name);
  return (
    <Modal title={`Clone ${ct.name}`} icon="clone" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={!ok || busy}
        onClick={() => run(() => api.post(`/api/v1/containers/${enc(ct.name)}/clone`, { name }), "Clone started").then((r) => r && onClose())}>Clone</Btn></>}>
      <div className="form">
        <Field label="New container name" help="The copy gets a new MAC address. Autostart is off for the copy.">
          <input type="text" autoFocus value={name} maxLength={63} onChange={(e) => setName(e.target.value)} />
        </Field>
      </div>
    </Modal>
  );
}

function DeleteCt({ ct, onClose, after }: { ct: Ct; onClose: () => void; after?: () => void }) {
  const { run } = useAction();
  return (
    <Confirm title={`Delete ${ct.name}`} danger typed={ct.name} confirmLabel="Delete"
      message={<>The container <b>{ct.name}</b>, its root filesystem and all {ct.snapshots} snapshot(s) are deleted permanently.</>}
      onConfirm={() => run(() => api.del(`/api/v1/containers/${enc(ct.name)}`), "Delete started").then(() => after?.())} onClose={onClose} />
  );
}

/* ------------------------------------------------------------------ detail */
export function ContainerDetail() {
  const { name = "" } = useParams();
  const { can } = useSession();
  const { ctVersion } = useLive();
  const nav = useNavigate();
  const { data: ct, error, reload } = useAsync(() => api.get(`/api/v1/containers/${enc(name)}`), [name, ctVersion]);
  const [tab, setTab] = useState(new URLSearchParams(location.search).get("tab") ?? "summary");
  const [dlg, setDlg] = useState<null | "clone" | "delete" | "snapshot">(null);
  useEffect(() => {
    const t = setInterval(reload, 10000);
    return () => clearInterval(t);
  }, [reload]);
  if (error) return <div className="page"><ErrorBox error={error} onRetry={reload} /></div>;
  if (!ct) return <Loading />;
  return (
    <div className="page">
      <div className="toolbar">
        <CtPowerButtons ct={ct} onChanged={reload} />
        <div className="sep" />
        {can("ct.snapshot") && <Btn icon="snapshot" disabled={ct.state !== "stopped"} title={ct.state !== "stopped" ? "Stop the container to take a snapshot" : undefined} onClick={() => setDlg("snapshot")}>Take snapshot</Btn>}
        {can("ct.manage") && <Dropdown label="Actions" icon="more" items={[
          { label: "Clone", icon: "clone", disabled: ct.state !== "stopped", onClick: () => setDlg("clone") },
          "sep",
          { label: "Delete", icon: "trash", danger: true, disabled: ct.state !== "stopped", onClick: () => setDlg("delete") },
        ]} />}
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <div className="hero">
        <div className="big-icon"><Icon name="container" size={26} /></div>
        <div>
          <h2>{ct.name}</h2>
          <div className="facts">
            <span><b>State</b><CtState state={ct.state} /></span>
            <span><b>Operating system</b>{ct.os || "—"}</span>
            <span><b>IP addresses</b><span className="mono">{ct.ips.join(", ") || "—"}</span></span>
            <span><b>Autostart</b>{ct.autostart ? "Yes" : "No"}</span>
          </div>
        </div>
        <div className="right" style={{ alignItems: "center" }}>
          <div className="stat"><div className="label">Memory</div><div className="value" style={{ fontSize: 18 }}>{ct.memory_used != null ? bytes(ct.memory_used) : "—"}</div><div className="hint">{ct.memory_mib ? `limit ${mib(ct.memory_mib)}` : "no limit"}</div></div>
          <div className="stat"><div className="label">CPU</div><div className="value" style={{ fontSize: 18 }}>{ct.cpus || "host"}</div><div className="hint">{ct.cpus ? "CPU limit" : "no limit"}</div></div>
        </div>
      </div>
      <Tabs tabs={[["summary", "Summary"], ["snapshots", `Snapshots (${ct.snapshot_list.length})`], ...(can("ct.manage") ? [["config", "Configuration"] as [string, string]] : [])]} value={tab} onChange={setTab} />
      {tab === "summary" && (
        <div className="grid c2">
          <Panel title="General" icon="info">
            <div className="kv">
              <div>Type</div><div>{ct.unprivileged ? "Unprivileged (uid/gid mapped to 100000+)" : "Privileged"}</div>
              <div>Source</div><div>{ct.source || "—"}</div>
              <div>Created</div><div>{ct.created ? `${ts(ct.created)}${ct.created_by ? ` by ${ct.created_by}` : ""}` : "—"}</div>
              <div>Disk</div><div>{ct.disk_gib != null ? `${ct.disk_gib} GiB (ext4 disk image)` : "no limit (directory on the datastore)"}</div>
              <div>Root filesystem</div><div className="mono small">{ct.rootfs}</div>
              <div>systemd unit</div><div className="mono small">{ct.unit}</div>
              <div>Notes</div><div style={{ whiteSpace: "pre-wrap" }}>{ct.description || <span className="muted">—</span>}</div>
            </div>
          </Panel>
          <Panel title="Network" icon="network">
            <div className="kv">
              <div>Bridge</div><div className="mono">{ct.bridge || "—"}</div>
              <div>MAC address</div><div className="mono">{ct.mac || "—"}</div>
              <div>IPv4 (configured)</div><div className="mono">{ct.ipv4 ? `${ct.ipv4}${ct.gateway ? ` via ${ct.gateway}` : ""}` : "DHCP / from the image"}</div>
              <div>IP addresses (live)</div><div className="mono">{ct.ips.join(", ") || "—"}</div>
            </div>
          </Panel>
          <div style={{ gridColumn: "1 / -1" }}>
            <Panel title="Console" icon="console">
              <div className="row">
                <span className="small muted grow">The console attaches to the container's /dev/console. Log in with the container's own accounts. Leave it with Ctrl+a q or close the window.</span>
                {can("ct.console") && <Btn icon="console" kind="outline" disabled={ct.state !== "running"} onClick={() => openCtConsole(ct.name)}>Open console</Btn>}
              </div>
            </Panel>
          </div>
        </div>
      )}
      {tab === "snapshots" && <CtSnapshots ct={ct} onChanged={reload} />}
      {tab === "config" && can("ct.manage") && <CtConfig ct={ct} onSaved={reload} />}
      {dlg === "clone" && <CloneCt ct={ct} onClose={() => setDlg(null)} />}
      {dlg === "delete" && <DeleteCt ct={ct} onClose={() => setDlg(null)} after={() => nav("/containers")} />}
      {dlg === "snapshot" && <SnapshotDialog ct={ct} onClose={() => setDlg(null)} />}
    </div>
  );
}

function SnapshotDialog({ ct, onClose }: { ct: Ct; onClose: () => void }) {
  const [comment, setComment] = useState("");
  const { run, busy } = useAction();
  return (
    <Modal title={`Take snapshot of ${ct.name}`} icon="snapshot" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy}
        onClick={() => run(() => api.post(`/api/v1/containers/${enc(ct.name)}/snapshots`, { comment }), "Snapshot started").then((r) => r && onClose())}>Take snapshot</Btn></>}>
      <div className="form">
        <div className="small muted">A snapshot is a full copy of the container's root filesystem (directory backend), so it needs as much free space as the container uses.</div>
        <Field label="Comment"><textarea value={comment} maxLength={500} onChange={(e) => setComment(e.target.value)} style={{ fontFamily: "var(--sans)" }} /></Field>
      </div>
    </Modal>
  );
}

function CtSnapshots({ ct, onChanged }: { ct: any; onChanged: () => void }) {
  const { can } = useSession();
  const { run } = useAction();
  const [confirm, setConfirm] = useState<null | { kind: "restore" | "delete"; snap: string }>(null);
  const stopped = ct.state === "stopped";
  return (
    <Panel flush title="Snapshots" icon="snapshot">
      {!stopped && <div className="alert info" style={{ margin: 10 }}><Icon name="info" /><div>Stop the container to take or restore a snapshot.</div></div>}
      <DataTable rowKey={(r: any) => r.name} rows={ct.snapshot_list}
        empty={<div className="empty-state"><h3>No snapshots</h3></div>}
        cols={[
          { key: "name", label: "Snapshot", render: (r: any) => <span className="mono">{r.name}</span> },
          { key: "created", label: "Taken", render: (r: any) => <span className="mono small">{r.created || "—"}</span> },
          { key: "comment", label: "Comment", render: (r: any) => r.comment || <span className="muted">—</span> },
          { key: "act", label: "", render: (r: any) => can("ct.snapshot") ? (
            <span className="row" style={{ justifyContent: "flex-end", gap: 6 }}>
              <Btn size="sm" icon="restart" disabled={!stopped} onClick={() => setConfirm({ kind: "restore", snap: r.name })}>Restore</Btn>
              <Btn size="sm" icon="trash" kind="danger" onClick={() => setConfirm({ kind: "delete", snap: r.name })}>Delete</Btn>
            </span>) : null },
        ]} />
      {confirm?.kind === "restore" && <Confirm title={`Restore ${confirm.snap}`} danger confirmLabel="Restore"
        message={<>The current root filesystem of <b>{ct.name}</b> is replaced by snapshot {confirm.snap}. Changes made since the snapshot are lost.</>}
        onConfirm={() => run(() => api.post(`/api/v1/containers/${enc(ct.name)}/snapshots/${confirm.snap}/restore`), "Restore started").then(onChanged)}
        onClose={() => setConfirm(null)} />}
      {confirm?.kind === "delete" && <Confirm title={`Delete ${confirm.snap}`} danger confirmLabel="Delete"
        message={<>Snapshot {confirm.snap} of <b>{ct.name}</b> is deleted permanently.</>}
        onConfirm={() => run(() => api.del(`/api/v1/containers/${enc(ct.name)}/snapshots/${confirm.snap}`), "Delete started").then(onChanged)}
        onClose={() => setConfirm(null)} />}
    </Panel>
  );
}

function CtConfig({ ct, onSaved }: { ct: any; onSaved: () => void }) {
  const status = useCtStatus();
  const { run, busy } = useAction();
  const [cpus, setCpus] = useState<number>(Math.round(ct.cpus));
  const [memMiB, setMemMiB] = useState<number>(ct.memory_mib);
  const [bridge, setBridge] = useState<string>(ct.bridge);
  const [ipMode, setIpMode] = useState<"dhcp" | "static">(ct.ipv4 ? "static" : "dhcp");
  const [ipv4, setIpv4] = useState<string>(ct.ipv4);
  const [gw, setGw] = useState<string>(ct.gateway);
  const [autostart, setAutostart] = useState<boolean>(ct.autostart);
  const [desc, setDesc] = useState<string>(ct.description);
  const [pw1, setPw1] = useState("");
  const [pw2, setPw2] = useState("");
  const save = async () => {
    const body: any = { cpus, memory_mib: memMiB, bridge, autostart, description: desc,
      ipv4: ipMode === "dhcp" ? "dhcp" : ipv4, gateway: ipMode === "static" ? gw : "" };
    if (pw1) body.root_password = pw1;
    const r: any = await run(() => api.patch(`/api/v1/containers/${enc(ct.name)}`, body), "Container settings saved");
    if (r) { setPw1(""); setPw2(""); onSaved(); }
    return r;
  };
  const badIp = ipMode === "static" && !/^\d+\.\d+\.\d+\.\d+\/\d+$/.test(ipv4);
  const running = ct.state !== "stopped";
  return (
    <div className="grid c2">
      <Panel title="Settings" icon="edit">
        <div className="form">
          {running && <div className="alert info"><Icon name="info" /><div>Resource and network changes apply at the next start of the container.</div></div>}
          <div className="row">
            <Field label="CPU limit" help="0 = no limit."><input type="number" min={0} max={1024} value={cpus} onChange={(e) => setCpus(Math.max(0, Number(e.target.value) | 0))} /></Field>
            <Field label="Memory limit" help="0 = no limit."><div className="unit-input"><input type="number" min={0} step={128} value={memMiB} onChange={(e) => setMemMiB(Math.max(0, Number(e.target.value) | 0))} /><span>MiB</span></div></Field>
          </div>
          <Field label="Network bridge">
            <BridgeSelect bridges={status.data?.bridges ?? [{ name: ct.bridge, network: "" }]} value={bridge} onChange={setBridge} />
          </Field>
          <Field label="IPv4"><Pill options={[["dhcp", "DHCP / from the image"], ["static", "Static"]]} value={ipMode} onChange={setIpMode} /></Field>
          {ipMode === "static" && <div className="row">
            <Field label="Address/prefix" error={badIp ? "e.g. 192.168.1.50/24" : null}><input type="text" className="mono" value={ipv4} onChange={(e) => setIpv4(e.target.value)} /></Field>
            <Field label="Gateway"><input type="text" className="mono" value={gw} onChange={(e) => setGw(e.target.value)} /></Field>
          </div>}
          <label className="check"><input type="checkbox" checked={autostart} onChange={(e) => setAutostart(e.target.checked)} /> Start automatically with the host</label>
          <Field label="Notes"><textarea value={desc} maxLength={1000} onChange={(e) => setDesc(e.target.value)} style={{ fontFamily: "var(--sans)" }} /></Field>
          <Field label="New root password" help={running ? "Stop the container to change the root password." : "Leave empty to keep the current password."}>
            <input type="password" autoComplete="new-password" disabled={running} value={pw1} onChange={(e) => setPw1(e.target.value)} />
          </Field>
          {pw1 && <Field label="Confirm root password" error={pw2 && pw1 !== pw2 ? "Passwords do not match" : null}><input type="password" autoComplete="new-password" value={pw2} onChange={(e) => setPw2(e.target.value)} /></Field>}
          <div><Btn kind="primary" disabled={busy || badIp || (!!pw1 && pw1 !== pw2)} onClick={save}>Save</Btn></div>
        </div>
      </Panel>
      <Panel title="LXC configuration file (read-only)" icon="log">
        <pre className="mono small" style={{ whiteSpace: "pre-wrap", margin: 0, maxHeight: 520, overflow: "auto" }}>{ct.config}</pre>
      </Panel>
    </div>
  );
}
