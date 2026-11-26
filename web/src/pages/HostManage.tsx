import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, enc } from "../api";
import { Icon } from "../icons";
import { useSession } from "../session";
import { AutostartTab, LockdownPanels, LoggingTab, SupportTab } from "./HostAdmin";
import { Btn, Confirm, DataTable, ErrorBox, Field, Loading, Modal, Panel, Tabs, useAction, useAsync } from "../ui";
import { ago, ts } from "../util";

export function HostManage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "system";
  const { can } = useSession();
  const tabs: [string, string][] = [["system", "System"], ["autostart", "Autostart"], ["logging", "Syslog, SNMP & power"], ["services", "Services"], ["security", "Security & SSH"], ["firewall", "Firewall"], ["certificate", "Certificate"], ["support", "Support"]];
  if (can("security.manage")) tabs.push(["users", "Users"], ["roles", "Roles"]);
  return (
    <div className="page">
      <div className="page-head"><h1><Icon name="manage" size={20} /> Manage host</h1></div>
      <Tabs tabs={tabs} value={tab} onChange={(t) => setParams({ tab: t })} />
      {tab === "system" && <System />}
      {tab === "autostart" && <AutostartTab />}
      {tab === "logging" && <LoggingTab />}
      {tab === "support" && <SupportTab />}
      {tab === "services" && <Services />}
      {tab === "security" && <Security />}
      {tab === "firewall" && <Firewall />}
      {tab === "certificate" && <Certificate />}
      {tab === "users" && <Users />}
      {tab === "roles" && <Roles />}
    </div>
  );
}

const lines = (s: string) => s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

function System() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get("/api/v1/host/config"));
  const tz = useAsync(() => api.get<string[]>("/api/v1/host/timezones").catch(() => []));
  const { run, busy } = useAction();
  const [hostname, setHostname] = useState("");
  const [dns, setDns] = useState("");
  const [search, setSearch] = useState("");
  const [ntp, setNtp] = useState("");
  const [ntpOn, setNtpOn] = useState(true);
  const [zone, setZone] = useState("");
  useEffect(() => {
    if (!data) return;
    setHostname(data.hostname);
    setDns((data.dns?.servers ?? []).join(", "));
    setSearch((data.dns?.search ?? []).join(", "));
    setNtp((data.time?.ntp_servers ?? []).join(", "));
    setNtpOn(data.time?.ntp_enabled ?? true);
    setZone(data.time?.timezone ?? "");
  }, [data]);
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const ro = !can("host.config");
  return (
    <>
      {!data.agent && <div className="alert err"><Icon name="error" /><div>Host agent unavailable: {data.agent_error}. Configuration changes are not possible.</div></div>}
      {data.agent && Object.entries(data.errors ?? {}).map(([k, msg]) => <div key={k} className="alert warn"><Icon name="warn" /><div>Could not read {k} settings: {String(msg)}</div></div>)}
      <div className="grid c2">
        <Panel title="Identity" icon="host">
          <div className="form">
            <Field label="Host name" help="Fully qualified name recommended (e.g. lsx01.example.com). The TLS certificate is not regenerated automatically.">
              <input type="text" value={hostname} disabled={ro} onChange={(e) => setHostname(e.target.value)} />
            </Field>
            <div><Btn kind="primary" disabled={ro || busy || hostname === data.hostname} onClick={() => run(() => api.put("/api/v1/host/hostname", { hostname }), "Host name updated").then(reload)}>Save host name</Btn></div>
          </div>
        </Panel>
        <Panel title="DNS" icon="globe">
          <div className="form">
            <div className="small muted">Current resolvers: <span className="mono">{data.dns?.effective?.join(", ") || "none"}</span> ({data.dns?.mode === "static" ? "set by LSX-VE" : "from network connections / DHCP"})</div>
            <Field label="DNS servers" help="Comma separated IP addresses. Leave empty to use the servers provided by DHCP or the connection configuration.">
              <input type="text" value={dns} disabled={ro} onChange={(e) => setDns(e.target.value)} />
            </Field>
            <Field label="Search domains"><input type="text" value={search} disabled={ro} onChange={(e) => setSearch(e.target.value)} /></Field>
            <div><Btn kind="primary" disabled={ro || busy} onClick={() => run(() => api.put("/api/v1/host/dns", { servers: lines(dns), search: lines(search) }), "DNS updated").then(reload)}>Save DNS</Btn></div>
          </div>
        </Panel>
        <Panel title="Time & date" icon="clock">
          <div className="form">
            <div className="kv">
              <div>Host time</div><div>{data.time ? new Date(data.time.time * 1000).toUTCString() : "—"}</div>
              <div>Synchronized</div><div>{data.time?.ntp_synchronized ? <span className="status ok">Yes{data.time.reference ? ` (${data.time.reference})` : ""}</span> : <span className="status warning">No</span>}</div>
            </div>
            <Field label="Time zone">
              <select value={zone} disabled={ro} onChange={(e) => setZone(e.target.value)}>
                {!zone && <option value="">(unknown)</option>}
                {(tz.data && tz.data.length ? tz.data : zone ? [zone] : []).map((z) => <option key={z} value={z}>{z}</option>)}
              </select>
            </Field>
            <div><Btn kind="primary" disabled={ro || busy || !zone || zone === data.time?.timezone} onClick={() => run(() => api.put("/api/v1/host/timezone", { timezone: zone }), "Time zone updated").then(reload)}>Save time zone</Btn></div>
          </div>
        </Panel>
        <Panel title="NTP" icon="clock">
          <div className="form">
            <label className="check"><input type="checkbox" checked={ntpOn} disabled={ro} onChange={(e) => setNtpOn(e.target.checked)} /> Synchronize time with NTP servers</label>
            <Field label="NTP servers" help="Host names or IP addresses, comma separated.">
              <input type="text" value={ntp} disabled={ro} onChange={(e) => setNtp(e.target.value)} />
            </Field>
            <div><Btn kind="primary" disabled={ro || busy} onClick={() => run(() => api.put("/api/v1/host/ntp", { servers: lines(ntp), enabled: ntpOn }), "NTP configuration saved").then(reload)}>Save NTP</Btn></div>
            {(data.time?.ntp_sources ?? []).length > 0 && (
              <table className="dt">
                <thead><tr><th>Source</th><th>State</th><th>Stratum</th><th className="num">Offset</th></tr></thead>
                <tbody>{data.time.ntp_sources.map((s: any) => <tr key={s.address}><td className="mono">{s.address}</td><td>{s.state === "*" ? "selected" : s.state === "+" ? "candidate" : s.state}</td><td>{s.stratum}</td><td className="num mono">{s.offset}</td></tr>)}</tbody>
              </table>
            )}
          </div>
        </Panel>
      </div>
    </>
  );
}

function Services() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get<any[]>("/api/v1/host/services"));
  const { run, busy } = useAction();
  const [sel, setSel] = useState<string[]>([]);
  const cur = data?.find((s) => s.name === sel[0]);
  const act = (a: string) => cur && run(() => api.post(`/api/v1/host/services/${enc(cur.name)}/${a}`), `${cur.name}: ${a}`).then(reload);
  const allowed = (a: string) => !!cur && can("host.config") && cur.actions.includes(a) && !busy;
  return (
    <>
      <div className="toolbar">
        <Btn icon="play" disabled={!allowed("start") || cur?.active === "active"} onClick={() => act("start")}>Start</Btn>
        <Btn icon="stop" disabled={!allowed("stop") || cur?.active !== "active"} onClick={() => act("stop")}>Stop</Btn>
        <Btn icon="restart" disabled={!allowed("restart")} onClick={() => act("restart")}>Restart</Btn>
        <div className="sep" />
        <Btn icon="check" disabled={!allowed("enable") || cur?.enabled === "enabled"} onClick={() => act("enable")}>Start with host</Btn>
        <Btn icon="close" disabled={!allowed("disable") || cur?.enabled !== "enabled"} onClick={() => act("disable")}>Do not start with host</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <ErrorBox error={error} onRetry={reload} />
      <Panel flush>
        <DataTable rowKey={(r: any) => r.name} rows={(data ?? []).filter((s) => s.installed)} selected={sel} onSelect={setSel}
          cols={[
            { key: "name", label: "Service", sort: (r: any) => r.name, render: (r: any) => <span className="mono">{r.name}</span> },
            { key: "description", label: "Description" },
            { key: "active", label: "Status", render: (r: any) => <span className={`status ${r.active === "active" ? "running" : r.active === "failed" ? "error" : "shutoff"}`}>{r.active} ({r.sub})</span> },
            { key: "enabled", label: "Startup policy", render: (r: any) => r.enabled === "enabled" ? "Start with host" : r.enabled === "static" ? "Static / on demand" : r.enabled },
            { key: "actions", label: "Permitted actions", render: (r: any) => <span className="small muted">{r.actions.join(", ")}</span> },
          ]} />
      </Panel>
    </>
  );
}

function Security() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get("/api/v1/host/config"));
  const { run, busy } = useAction();
  const [prl, setPrl] = useState("prohibit-password");
  const [pw1, setPw1] = useState("");
  const [pw2, setPw2] = useState("");
  useEffect(() => data?.ssh && setPrl(data.ssh.permit_root_login), [data]);
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const ssh = data.ssh;
  const on = ssh?.service?.active === "active";
  return (
    <>
    <div className="grid c2">
      <Panel title="SSH" icon="ssh">
        <div className="form">
          <div className="kv">
            <div>Service</div><div>{ssh?.service ? <span className={`status ${on ? "running" : "shutoff"}`}>{on ? "Running" : "Stopped"}</span> : "—"}</div>
            <div>Start with host</div><div>{ssh?.service?.enabled === "enabled" ? "Yes" : "No"}</div>
          </div>
          <Field label="Root login over SSH">
            <select value={prl} disabled={!can("host.config")} onChange={(e) => setPrl(e.target.value)}>
              <option value="prohibit-password">Key-based only</option>
              <option value="yes">Allowed (password or key)</option>
              <option value="no">Not allowed</option>
            </select>
          </Field>
          <div className="btn-group">
            <Btn kind="primary" disabled={!can("host.config") || busy} onClick={() => run(() => api.put("/api/v1/host/ssh", { enabled: true, permit_root_login: prl }), "SSH enabled").then(reload)}>{on ? "Apply" : "Enable SSH"}</Btn>
            <Btn kind="outline" disabled={!can("host.config") || busy || !on} onClick={() => run(() => api.put("/api/v1/host/ssh", { enabled: false, permit_root_login: prl }), "SSH disabled").then(reload)}>Disable SSH</Btn>
          </div>
        </div>
      </Panel>
      <Panel title="Host root password" icon="key">
        <div className="form">
          <div className="small muted">The root account is used for the local console shell and SSH. It is independent of LSX-VE web users.</div>
          <Field label="New password"><input type="password" autoComplete="new-password" value={pw1} disabled={!can("security.manage")} onChange={(e) => setPw1(e.target.value)} /></Field>
          <Field label="Confirm" error={pw2 && pw1 !== pw2 ? "Passwords do not match" : null}><input type="password" autoComplete="new-password" value={pw2} disabled={!can("security.manage")} onChange={(e) => setPw2(e.target.value)} /></Field>
          <div><Btn kind="primary" disabled={!can("security.manage") || busy || !pw1 || pw1 !== pw2} onClick={() => run(() => api.put("/api/v1/host/root-password", { password: pw1 }), "Root password changed").then(() => { setPw1(""); setPw2(""); })}>Change root password</Btn></div>
        </div>
      </Panel>
    </div>
    <LockdownPanels />
    </>
  );
}

function Firewall() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get("/api/v1/host/firewall"));
  const { run, busy } = useAction();
  const [svc, setSvc] = useState("");
  const [port, setPort] = useState("");
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <Loading />;
  if (!data.running) return <div className="alert warn"><Icon name="warn" /><div>The firewall service is not running.</div></div>;
  const change = (op: string, kind: string, value: string) => run(() => api.post("/api/v1/host/firewall", { op, kind, value }), `Firewall rule ${op === "add" ? "added" : "removed"}`).then(reload);
  const rows = [...data.services.map((s: string) => ({ kind: "service", value: s })), ...data.ports.map((p: string) => ({ kind: "port", value: p }))];
  const ro = !can("host.config");
  return (
    <>
      <div className="alert info"><Icon name="info" /><div>Incoming rules for zone <b>{data.zone}</b>. The LSX-VE management service (HTTPS) is protected and cannot be removed.</div></div>
      <div className="grid c2">
        <Panel title="Allowed incoming traffic" icon="fire" flush>
          <DataTable rowKey={(r: any) => r.kind + r.value} rows={rows}
            cols={[
              { key: "kind", label: "Type", render: (r: any) => <span className="badge">{r.kind}</span> },
              { key: "value", label: "Rule", render: (r: any) => <span className="mono">{r.value}</span> },
              { key: "x", label: "", render: (r: any) => data.protected.includes(r.value) ? <span className="badge blue">protected</span> :
                <Btn size="sm" kind="danger" icon="trash" disabled={ro || busy} onClick={() => change("remove", r.kind, r.value)}>Remove</Btn> },
            ]} />
        </Panel>
        <Panel title="Add rule" icon="plus">
          <div className="form">
            <Field label="Predefined service">
              <div className="row">
                <select value={svc} disabled={ro} onChange={(e) => setSvc(e.target.value)} style={{ flex: 1 }}>
                  <option value="">Select a service…</option>
                  {data.available_services.filter((s: string) => !data.services.includes(s)).map((s: string) => <option key={s} value={s}>{s}</option>)}
                </select>
                <Btn kind="primary" disabled={ro || !svc || busy} onClick={() => change("add", "service", svc).then(() => setSvc(""))}>Allow</Btn>
              </div>
            </Field>
            <Field label="Port" help="Format: 8080/tcp or 5000-5010/udp">
              <div className="row">
                <input type="text" value={port} disabled={ro} onChange={(e) => setPort(e.target.value)} placeholder="8080/tcp" style={{ flex: 1 }} />
                <Btn kind="primary" disabled={ro || !/^\d{1,5}(-\d{1,5})?\/(tcp|udp|sctp)$/.test(port) || busy} onClick={() => change("add", "port", port).then(() => setPort(""))}>Allow</Btn>
              </div>
            </Field>
          </div>
        </Panel>
      </div>
    </>
  );
}

function Certificate() {
  const { can } = useSession();
  const { data, error, reload } = useAsync(() => api.get("/api/v1/host/certificate"));
  const { run, busy } = useAction();
  const [imp, setImp] = useState(false);
  const [regen, setRegen] = useState(false);
  const [cert, setCert] = useState("");
  const [key, setKey] = useState("");
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <Loading />;
  return (
    <>
      <div className="toolbar">
        <Btn icon="upload" disabled={!can("host.config")} onClick={() => setImp(true)}>Import certificate</Btn>
        <Btn icon="refresh" disabled={!can("host.config")} onClick={() => setRegen(true)}>Regenerate self-signed</Btn>
      </div>
      <Panel title="Management interface certificate" icon="cert">
        {data.present === false ? <div className="muted">No certificate (TLS disabled on this listener).</div> : (
          <div className="kv">
            <div>Subject</div><div className="mono">{data.subject}</div>
            <div>Issuer</div><div className="mono">{data.issuer}{data.self_signed && <span className="badge amber" style={{ marginLeft: 8 }}>self-signed</span>}</div>
            <div>Valid from</div><div>{ts(data.not_before)}</div>
            <div>Valid until</div><div>{ts(data.not_after)} {data.expired && <span className="badge red">expired</span>}</div>
            <div>Subject alternative names</div><div className="mono">{(data.san ?? []).join(", ")}</div>
            <div>SHA-256 fingerprint</div><div className="mono small">{data.sha256_fingerprint}</div>
          </div>
        )}
      </Panel>
      {imp && (
        <Modal title="Import certificate" icon="cert" size="mid" onClose={() => setImp(false)}
          footer={<><Btn onClick={() => setImp(false)}>Cancel</Btn><Btn kind="primary" disabled={busy || !cert || !key}
            onClick={() => run(() => api.post("/api/v1/host/certificate", { certificate: cert, private_key: key }), "Certificate installed — reload the page to use it").then((r) => { if (r) { setImp(false); setKey(""); reload(); } })}>Import</Btn></>}>
          <div className="form">
            <div className="small muted">PEM format. Include intermediate certificates after the server certificate. The new certificate is applied to new connections immediately.</div>
            <Field label="Certificate (chain)"><textarea rows={8} value={cert} onChange={(e) => setCert(e.target.value)} placeholder="-----BEGIN CERTIFICATE-----" spellCheck={false} /></Field>
            <Field label="Private key"><textarea rows={6} value={key} onChange={(e) => setKey(e.target.value)} placeholder="-----BEGIN PRIVATE KEY-----" spellCheck={false} /></Field>
          </div>
        </Modal>
      )}
      {regen && <Confirm title="Regenerate self-signed certificate" message="A new self-signed certificate is created for the current host name and management addresses. Browsers will show a new certificate warning."
        confirmLabel="Regenerate" onClose={() => setRegen(false)} onConfirm={() => run(() => api.post("/api/v1/host/certificate/regenerate"), "Certificate regenerated").then(reload)} />}
    </>
  );
}

function Users() {
  const { user: me } = useSession();
  const { data, error, reload } = useAsync(() => api.get<any[]>("/api/v1/users"));
  const roles = useAsync(() => api.get("/api/v1/roles"));
  const { run } = useAction();
  const [sel, setSel] = useState<string[]>([]);
  const [edit, setEdit] = useState<any | null>(null);
  const [del, setDel] = useState<any | null>(null);
  const cur = data?.find((u) => String(u.id) === sel[0]);
  return (
    <>
      <div className="toolbar">
        <Btn icon="plus" onClick={() => setEdit({})}>Add user</Btn>
        <Btn icon="edit" disabled={!cur} onClick={() => setEdit(cur)}>Edit</Btn>
        <Btn icon="trash" kind="danger" disabled={!cur || cur.id === me.id} onClick={() => setDel(cur)}>Remove</Btn>
        <div className="grow" />
        <Btn icon="refresh" onClick={reload}>Refresh</Btn>
      </div>
      <ErrorBox error={error} />
      <Panel flush>
        <DataTable rowKey={(r: any) => String(r.id)} rows={data ?? []} selected={sel} onSelect={setSel} onOpen={(r) => setEdit(r)}
          cols={[
            { key: "username", label: "User name", sort: (r: any) => r.username, render: (r: any) => <b>{r.username}</b> },
            { key: "full_name", label: "Description" },
            { key: "role", label: "Role", render: (r: any) => <span className="badge blue">{r.role}</span> },
            { key: "enabled", label: "Status", render: (r: any) => <span className={`status ${r.enabled ? "ok" : "shutoff"}`}>{r.enabled ? "Enabled" : "Disabled"}</span> },
            { key: "must_change_password", label: "Password", render: (r: any) => r.must_change_password ? <span className="badge amber">change required</span> : "OK" },
            { key: "last_login", label: "Last login", render: (r: any) => ago(r.last_login) },
          ]} />
      </Panel>
      {edit && <UserDialog user={edit} roles={roles.data?.roles ?? []} onClose={() => setEdit(null)} onSaved={reload} />}
      {del && <Confirm danger title={`Remove user ${del.username}`} message="The account and its sessions are removed. This cannot be undone." confirmLabel="Remove"
        onClose={() => setDel(null)} onConfirm={() => run(() => api.del(`/api/v1/users/${del.id}`), "User removed").then(() => { setSel([]); reload(); })} />}
    </>
  );
}

function UserDialog({ user, roles, onClose, onSaved }: { user: any; roles: any[]; onClose: () => void; onSaved: () => void }) {
  const isNew = !user.id;
  const [username, setUsername] = useState(user.username ?? "");
  const [full, setFull] = useState(user.full_name ?? "");
  const [role, setRole] = useState(user.role ?? "ReadOnly");
  const [enabled, setEnabled] = useState(user.enabled ?? true);
  const [pw, setPw] = useState("");
  const [mustChange, setMustChange] = useState(true);
  const { run, busy } = useAction();
  const save = async () => {
    const body: any = isNew ? { username, full_name: full, role, password: pw, must_change_password: mustChange }
      : { full_name: full, role, enabled, ...(pw ? { password: pw, must_change_password: mustChange } : {}) };
    const r = await run(() => (isNew ? api.post("/api/v1/users", body) : api.patch(`/api/v1/users/${user.id}`, body)), isNew ? "User created" : "User updated");
    if (r) { onSaved(); onClose(); }
  };
  return (
    <Modal title={isNew ? "Add user" : `Edit user ${user.username}`} icon="user" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || (isNew && (!username || !pw))} onClick={save}>{isNew ? "Add" : "Save"}</Btn></>}>
      <div className="form">
        <Field label="User name" help="Lowercase letters, digits, '.', '_' or '-'"><input type="text" value={username} disabled={!isNew} onChange={(e) => setUsername(e.target.value)} /></Field>
        <Field label="Description"><input type="text" value={full} onChange={(e) => setFull(e.target.value)} /></Field>
        <Field label="Role">
          <select value={role} onChange={(e) => setRole(e.target.value)}>{roles.map((r) => <option key={r.name} value={r.name}>{r.name} — {r.description}</option>)}</select>
        </Field>
        <Field label={isNew ? "Password" : "Reset password (optional)"} help="At least 8 characters with 3 of: lowercase, uppercase, digit, symbol.">
          <input type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} />
        </Field>
        {(isNew || pw) && <label className="check"><input type="checkbox" checked={mustChange} onChange={(e) => setMustChange(e.target.checked)} /> Require password change at next login</label>}
        {!isNew && <label className="check"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Account enabled</label>}
      </div>
    </Modal>
  );
}

function Roles() {
  const { data, error, reload } = useAsync(() => api.get("/api/v1/roles"));
  const { run } = useAction();
  const [sel, setSel] = useState<string[]>([]);
  const [edit, setEdit] = useState<any | null>(null);
  const [del, setDel] = useState<any | null>(null);
  if (error) return <ErrorBox error={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const cur = data.roles.find((r: any) => r.name === sel[0]);
  return (
    <>
      <div className="toolbar">
        <Btn icon="plus" onClick={() => setEdit({ permissions: [] })}>Add role</Btn>
        <Btn icon="edit" disabled={!cur || cur.builtin} onClick={() => setEdit(cur)}>Edit</Btn>
        <Btn icon="clone" disabled={!cur} onClick={() => setEdit({ ...cur, name: `${cur.name}-copy`, builtin: false, _copy: true })}>Duplicate</Btn>
        <Btn icon="trash" kind="danger" disabled={!cur || cur.builtin || cur.users > 0} onClick={() => setDel(cur)}>Remove</Btn>
      </div>
      <Panel flush>
        <DataTable rowKey={(r: any) => r.name} rows={data.roles} selected={sel} onSelect={setSel}
          cols={[
            { key: "name", label: "Role", render: (r: any) => <><b>{r.name}</b> {r.builtin && <span className="badge">built-in</span>}</> },
            { key: "description", label: "Description" },
            { key: "users", label: "Users", num: true },
            { key: "permissions", label: "Permissions", render: (r: any) => <span className="small muted">{r.permissions.length} of {Object.keys(data.permissions).length}</span> },
          ]} />
      </Panel>
      {edit && <RoleDialog role={edit} all={data.permissions} onClose={() => setEdit(null)} onSaved={reload} />}
      {del && <Confirm danger title={`Remove role ${del.name}`} message="The role definition is deleted." confirmLabel="Remove" onClose={() => setDel(null)}
        onConfirm={() => run(() => api.del(`/api/v1/roles/${enc(del.name)}`), "Role removed").then(reload)} />}
    </>
  );
}

function RoleDialog({ role, all, onClose, onSaved }: { role: any; all: Record<string, string>; onClose: () => void; onSaved: () => void }) {
  const isNew = !role.name || role._copy;
  const [name, setName] = useState(role.name ?? "");
  const [desc, setDesc] = useState(role.description ?? "");
  const [perms, setPerms] = useState<string[]>(role.permissions ?? []);
  const { run, busy } = useAction();
  const save = async () => {
    const body = { name, description: desc, permissions: perms };
    const r = await run(() => (isNew ? api.post("/api/v1/roles", body) : api.put(`/api/v1/roles/${enc(role.name)}`, body)), "Role saved");
    if (r) { onSaved(); onClose(); }
  };
  return (
    <Modal title={isNew ? "Add role" : `Edit role ${role.name}`} icon="shield" size="mid" onClose={onClose}
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="primary" disabled={busy || !name} onClick={save}>Save</Btn></>}>
      <div className="form">
        <div className="row">
          <Field label="Name"><input type="text" value={name} disabled={!isNew} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="Description"><input type="text" value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
        </div>
        <Field label="Privileges">
          <div style={{ display: "grid", gap: 6 }}>
            {Object.entries(all).map(([p, d]) => (
              <label key={p} className="check"><input type="checkbox" checked={perms.includes(p)} onChange={(e) => setPerms(e.target.checked ? [...perms, p] : perms.filter((x) => x !== p))} />
                <span className="mono" style={{ minWidth: 130 }}>{p}</span><span className="muted small">{d}</span></label>
            ))}
          </div>
        </Field>
      </div>
    </Modal>
  );
}
