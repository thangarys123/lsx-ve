import { useCallback, useEffect, useMemo, useState } from "react";
import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { AlarmBadge, MaintenanceBanner, useHostState } from "./pages/HostAdmin";
import { api, setCsrf, setUnauthorizedHandler } from "./api";
import { Icon, Logo } from "./icons";
import { live, useLive, useLiveEvent } from "./live";
import { Session, SessionCtx, User, useSession } from "./session";
import { Btn, Dropdown, Field, Modal, TaskState, useToast } from "./ui";
import { cls, ts } from "./util";
import { Login } from "./pages/Login";
import { HostSummary } from "./pages/HostSummary";
import { HostMonitor } from "./pages/HostMonitor";
import { HostManage } from "./pages/HostManage";
import { VMList } from "./pages/VMList";
import { VMDetail } from "./pages/VMDetail";
import { StoragePage } from "./pages/Storage";
import { NetworkingPage } from "./pages/Networking";
import { ConsolePage } from "./pages/Console";
import { CTConsolePage } from "./pages/CTConsole";
import { ContainerDetail, ContainerList } from "./pages/Containers";

export function App() {
  const [state, setState] = useState<"loading" | "anon" | "auth">("loading");
  const [sess, setSess] = useState<{ user: User; csrf: string; version: string; idle_timeout: number } | null>(null);
  const [hostname, setHostname] = useState("");
  const nav = useNavigate();

  const loadSession = useCallback(async () => {
    try {
      const s = await api.get("/api/v1/auth/session");
      setCsrf(s.csrf);
      setSess(s);
      setState("auth");
    } catch {
      setState("anon");
    }
  }, []);

  useEffect(() => {
    api.get("/api/v1/public/info").then((i) => setHostname(i.hostname)).catch(() => {});
    setUnauthorizedHandler(() => {
      live.stop();
      setSess(null);
      setState("anon");
    });
    loadSession();
  }, [loadSession]);

  const logout = useCallback(async () => {
    try {
      await api.post("/api/v1/auth/logout");
    } catch {
      /* already gone */
    }
    live.stop();
    setSess(null);
    setState("anon");
    nav("/");
  }, [nav]);

  const session: Session | null = useMemo(() => sess && {
    ...sess,
    hostname,
    logout,
    can: (p: string) => sess.user.permissions.includes(p),
  }, [sess, hostname, logout]);

  if (state === "loading") return <div className="login-bg" />;
  if (state === "anon" || !session) return <Login hostname={hostname} onLogin={loadSession} />;
  if (session.user.must_change_password)
    return <SessionCtx.Provider value={session}><ForcedPassword onDone={loadSession} /></SessionCtx.Provider>;
  return (
    <SessionCtx.Provider value={session}>
      <Routes>
        <Route path="/console/:uuid" element={<ConsolePage />} />
        <Route path="/ct-console/:name" element={<CTConsolePage />} />
        <Route path="*" element={<Shell />} />
      </Routes>
    </SessionCtx.Provider>
  );
}

function useIdleLogout(seconds: number, logout: () => void) {
  useEffect(() => {
    let last = Date.now();
    const bump = () => (last = Date.now());
    const evs = ["mousemove", "keydown", "mousedown", "wheel", "touchstart"];
    evs.forEach((e) => window.addEventListener(e, bump, { passive: true }));
    const t = setInterval(() => {
      if (Date.now() - last > seconds * 1000) logout();
    }, 15000);
    return () => {
      clearInterval(t);
      evs.forEach((e) => window.removeEventListener(e, bump));
    };
  }, [seconds, logout]);
}

function Shell() {
  const { user, hostname, logout, can, version, idle_timeout } = useSession();
  const { connected, vmVersion, ctVersion } = useLive();
  const [vms, setVms] = useState<any[]>([]);
  const [cts, setCts] = useState<any[]>([]);
  const [ctOpen, setCtOpen] = useState(true);
  const [pools, setPools] = useState<number | null>(null);
  const [nets, setNets] = useState<number | null>(null);
  const [vmOpen, setVmOpen] = useState(true);
  const [pw, setPw] = useState(false);
  const toast = useToast();
  const loc = useLocation();
  const navigate = useNavigate();
  const { alarms, maint } = useHostState();
  useIdleLogout(idle_timeout, logout);

  useEffect(() => {
    live.start();
    Promise.all([
      can("host.view") ? api.get("/api/v1/host/metrics") : Promise.resolve({ samples: [] }),
      can("host.view") ? api.get("/api/v1/tasks?limit=50") : Promise.resolve([]),
    ]).then(([m, t]) => live.seed(m.samples, t)).catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (can("vm.view")) api.get("/api/v1/vms").then(setVms).catch(() => setVms([]));
  }, [vmVersion]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (can("ct.view")) api.get("/api/v1/containers").then(setCts).catch(() => setCts([]));
  }, [ctVersion]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (can("storage.view")) api.get("/api/v1/storage/datastores").then((p) => setPools(p.length)).catch(() => setPools(null));
    if (can("network.view")) api.get("/api/v1/network/virtual").then((n) => setNets(n.length)).catch(() => setNets(null));
  }, [loc.pathname]); // eslint-disable-line react-hooks/exhaustive-deps

  useLiveEvent((type, data) => {
    if (type === "task" && (data.state === "success" || data.state === "error") && data.username === user.username) {
      toast({ kind: data.state === "success" ? "success" : "error", title: data.description, detail: data.message });
    }
  }, [user.username]);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <Logo />
          <span className="name">LSX-VE</span>
          <span className="sub">Host Client</span>
        </div>
        <div className="host-chip">
          <Icon name="host" size={14} />
          <span className="mono">{hostname}</span>
        </div>
        {maint && <div className="host-chip" title="The host is in maintenance mode"><Icon name="manage" size={14} style={{ color: "var(--warn)" }} /> Maintenance mode</div>}
        <div className="spacer" />
        <AlarmBadge alarms={alarms} onClick={() => navigate("/host/monitor?tab=alarms")} />
        <div className="host-chip" title={connected ? "Live updates connected" : "Live updates disconnected — reconnecting"}>
          <span className={cls("conn-dot", !connected && "off")} />
          {connected ? "Live" : "Reconnecting"}
        </div>
        <Dropdown align="right" label={<><Icon name="user" size={14} /> {user.username} <span className="muted small">({user.role})</span></>}
          items={[
            { label: "Change password", icon: "key", onClick: () => setPw(true) },
            "sep",
            { label: `LSX-VE ${version}`, icon: "info", onClick: () => {}, disabled: true },
            { label: "Log out", icon: "logout", onClick: logout },
          ]} />
      </header>
      <nav className="nav" aria-label="Navigator">
        <div className="section">Navigator</div>
        <NavLink end to="/" className={({ isActive }) => cls("item", isActive && "active")}>
          <Icon name="host" /> <span className="label">Host</span>
        </NavLink>
        {can("host.view") && <NavLink to="/host/monitor" className={({ isActive }) => cls("item sub", isActive && "active")}>
          <Icon name="monitor" size={14} /> <span className="label">Monitor</span>
        </NavLink>}
        {can("host.view") && <NavLink to="/host/manage" className={({ isActive }) => cls("item sub", isActive && "active")}>
          <Icon name="manage" size={14} /> <span className="label">Manage</span>
        </NavLink>}
        {can("vm.view") && <>
          <NavLink end to="/vms" className={({ isActive }) => cls("item", isActive && "active")}>
            <span className="twisty" onClick={(e) => { e.preventDefault(); setVmOpen(!vmOpen); }}>
              <Icon name={vmOpen ? "down" : "right"} size={11} />
            </span>
            <Icon name="vms" /> <span className="label">Virtual Machines</span>
            <span className="count">{vms.length}</span>
          </NavLink>
          {vmOpen && vms.map((v) => (
            <NavLink key={v.uuid} to={`/vms/${v.uuid}`} className={({ isActive }) => cls("item sub2", isActive && "active")} title={v.name}>
              <Icon name={v.template ? "template" : "vm"} size={13}
                style={{ color: v.state === "running" ? "var(--ok)" : v.state === "paused" ? "var(--paused)" : "var(--muted)" }} />
              <span className="label" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{v.name}</span>
            </NavLink>
          ))}
        </>}
        {can("ct.view") && <>
          <NavLink end to="/containers" className={({ isActive }) => cls("item", isActive && "active")}>
            <span className="twisty" onClick={(e) => { e.preventDefault(); setCtOpen(!ctOpen); }}>
              <Icon name={ctOpen ? "down" : "right"} size={11} />
            </span>
            <Icon name="containers" /> <span className="label">Containers</span>
            <span className="count">{cts.length}</span>
          </NavLink>
          {ctOpen && cts.map((c) => (
            <NavLink key={c.name} to={`/containers/${encodeURIComponent(c.name)}`} className={({ isActive }) => cls("item sub2", isActive && "active")} title={c.name}>
              <Icon name="container" size={13}
                style={{ color: c.state === "running" ? "var(--ok)" : c.state === "frozen" ? "var(--paused)" : "var(--muted)" }} />
              <span className="label" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.name}</span>
            </NavLink>
          ))}
        </>}
        {can("storage.view") && <NavLink to="/storage" className={({ isActive }) => cls("item", isActive && "active")}>
          <Icon name="storage" /> <span className="label">Storage</span>
          {pools !== null && <span className="count">{pools}</span>}
        </NavLink>}
        {can("network.view") && <NavLink to="/networking" className={({ isActive }) => cls("item", isActive && "active")}>
          <Icon name="network" /> <span className="label">Networking</span>
          {nets !== null && <span className="count">{nets}</span>}
        </NavLink>}
      </nav>
      <main className="main">
        <MaintenanceBanner on={maint} />
        <Routes>
          <Route path="/" element={<HostSummary />} />
          <Route path="/host/monitor" element={<HostMonitor />} />
          <Route path="/host/manage" element={<HostManage />} />
          <Route path="/vms" element={<VMList />} />
          <Route path="/vms/:uuid" element={<VMDetail />} />
          <Route path="/containers" element={<ContainerList />} />
          <Route path="/containers/:name" element={<ContainerDetail />} />
          <Route path="/storage" element={<StoragePage />} />
          <Route path="/networking" element={<NetworkingPage />} />
          <Route path="*" element={<div className="page"><div className="empty-state"><h3>Page not found</h3></div></div>} />
        </Routes>
      </main>
      {can("host.view") && <TasksPane />}
      {pw && <ChangePassword onClose={() => setPw(false)} />}
    </div>
  );
}

function TasksPane() {
  const { tasks } = useLive();
  const [open, setOpen] = useState(true);
  const active = tasks.filter((t) => t.state === "running" || t.state === "queued").length;
  const failed = tasks.filter((t) => t.state === "error" && t.finished && Date.now() / 1000 - t.finished < 3600).length;
  return (
    <div className="tasks-pane">
      <div className="th" onClick={() => setOpen(!open)}>
        <Icon name={open ? "down" : "right"} size={12} />
        <b>Recent tasks</b>
        {active > 0 && <span className="badge blue">{active} running</span>}
        {failed > 0 && <span className="badge red">{failed} failed (last hour)</span>}
      </div>
      {open && (
        <div className="body">
          <table className="dt">
            <thead>
              <tr><th>Task</th><th>Target</th><th>Initiator</th><th>Queued</th><th>Started</th><th>Result</th><th>Completed</th></tr>
            </thead>
            <tbody>
              {tasks.length === 0 && <tr><td className="empty" colSpan={7}>No recent tasks</td></tr>}
              {tasks.slice(0, 30).map((t) => (
                <tr key={t.id}>
                  <td>{t.description}</td>
                  <td>{t.target_name || "—"}</td>
                  <td>{t.username}</td>
                  <td className="nowrap">{ts(t.created)}</td>
                  <td className="nowrap">{ts(t.started)}</td>
                  <td>
                    {t.state === "running" ? (
                      <span className="row" style={{ gap: 8 }}>
                        <span className="progress"><i style={{ width: `${t.progress}%` }} /></span>
                        <span className="small muted">{t.progress}% {t.message}</span>
                      </span>
                    ) : (
                      <span title={t.message}><TaskState state={t.state} /> <span className="small muted">{t.state === "error" ? t.message : ""}</span></span>
                    )}
                  </td>
                  <td className="nowrap">{ts(t.finished)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function ChangePassword({ onClose, forced }: { onClose: () => void; forced?: boolean }) {
  const [cur, setCur] = useState("");
  const [n1, setN1] = useState("");
  const [n2, setN2] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const submit = async () => {
    if (n1 !== n2) return setErr("The new passwords do not match");
    setBusy(true);
    try {
      await api.post("/api/v1/auth/password", { current_password: cur, new_password: n1 });
      toast({ kind: "success", title: "Password changed" });
      onClose();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={forced ? "Password change required" : "Change password"} icon="key" onClose={forced ? () => {} : onClose}
      footer={<>{!forced && <Btn onClick={onClose}>Cancel</Btn>}<Btn kind="primary" disabled={busy || !cur || !n1} onClick={submit}>Change password</Btn></>}>
      <form className="form" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {forced && <div className="alert warn"><Icon name="warn" /><div>You must choose a new password before continuing.</div></div>}
        <Field label="Current password"><input type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} /></Field>
        <Field label="New password" help="At least 8 characters with 3 of: lowercase, uppercase, digit, symbol.">
          <input type="password" autoComplete="new-password" value={n1} onChange={(e) => setN1(e.target.value)} />
        </Field>
        <Field label="Confirm new password" error={err}><input type="password" autoComplete="new-password" value={n2} onChange={(e) => setN2(e.target.value)} /></Field>
        <button type="submit" hidden />
      </form>
    </Modal>
  );
}

function ForcedPassword({ onDone }: { onDone: () => void }) {
  return <div className="login-bg"><ChangePassword forced onClose={onDone} /></div>;
}

