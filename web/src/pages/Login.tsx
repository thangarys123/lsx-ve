import { useState } from "react";
import { api, setCsrf } from "../api";
import { Icon, Logo } from "../icons";

export function Login({ hostname, onLogin }: { hostname: string; onLogin: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api.post("/api/v1/auth/login", { username, password });
      setCsrf(r.csrf);
      setPassword("");
      onLogin();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="login-bg">
      <form className="login form" onSubmit={submit}>
        <div className="brand">
          <Logo size={40} />
          <span className="name">LSX-VE</span>
        </div>
        <div className="tag">Linux Server eXtended · Virtualization Environment</div>
        {hostname && <div className="host"><Icon name="host" size={12} style={{ verticalAlign: -2 }} /> {hostname}</div>}
        <div className="field">
          <label htmlFor="u">User name</label>
          <input id="u" type="text" autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="p">Password</label>
          <input id="p" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error && <div className="alert err" role="alert"><Icon name="error" /><div>{error}</div></div>}
        <button className="btn primary" style={{ height: 34, justifyContent: "center" }} disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Log in"}
        </button>
        <div className="foot">Sessions expire after inactivity. All actions are audited.</div>
      </form>
    </div>
  );
}
