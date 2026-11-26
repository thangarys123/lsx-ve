// Container console: xterm.js <-> /api/v1/ct-console (WebSocket) <-> pty running lxc-console on the host.
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api, enc } from "../api";
import { Logo } from "../icons";
import { useSession } from "../session";
import { Btn, Dropdown } from "../ui";

export function CTConsolePage() {
  const { name = "" } = useParams();
  const { can } = useSession();
  const host = useRef<HTMLDivElement>(null);
  const term = useRef<Terminal | null>(null);
  const fit = useRef<FitAddon | null>(null);
  const ws = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState("Connecting…");

  useEffect(() => {
    const t = new Terminal({
      cursorBlink: true, fontSize: 14, scrollback: 5000,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
      theme: { background: "#0b1220", foreground: "#e6edf7", cursor: "#6ea8ff" },
    });
    const f = new FitAddon();
    t.loadAddon(f);
    t.open(host.current!);
    f.fit();
    term.current = t;
    fit.current = f;
    const enc8 = new TextEncoder();
    const d1 = t.onData((d) => ws.current?.readyState === WebSocket.OPEN && ws.current.send(enc8.encode(d)));
    const d2 = t.onBinary((d) => {
      const b = new Uint8Array(d.length);
      for (let i = 0; i < d.length; i++) b[i] = d.charCodeAt(i) & 0xff;
      if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(b);
    });
    const d3 = t.onResize(({ cols, rows }) => {
      if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify({ type: "resize", cols, rows }));
    });
    const onWin = () => f.fit();
    window.addEventListener("resize", onWin);
    document.title = `${name} — LSX-VE container console`;
    return () => {
      window.removeEventListener("resize", onWin);
      d1.dispose(); d2.dispose(); d3.dispose();
      ws.current?.close();
      t.dispose();
    };
  }, [name]);

  const connect = useCallback(async () => {
    ws.current?.close();
    setConnected(false);
    setStatus("Requesting console ticket…");
    const t = term.current!;
    try {
      const tk = await api.post(`/api/v1/containers/${enc(name)}/console-ticket`);
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const s = new WebSocket(`${proto}://${location.host}${tk.path}&cols=${t.cols}&rows=${t.rows}`);
      s.binaryType = "arraybuffer";
      s.onopen = () => {
        setConnected(true);
        setStatus("");
        t.focus();
        t.write("\r\n\x1b[2m[connected to the console of " + name + " — press Enter for a login prompt; Ctrl+a q detaches]\x1b[0m\r\n");
      };
      s.onmessage = (ev) => {
        if (typeof ev.data === "string") t.write(ev.data);
        else t.write(new Uint8Array(ev.data));
      };
      s.onclose = (ev) => {
        setConnected(false);
        setStatus(ev.code === 4401 ? "Console ticket expired — reconnect." : ev.reason || "Console disconnected.");
      };
      ws.current = s;
      setStatus("Connecting to the console…");
    } catch (e: any) {
      setStatus(e.message);
    }
  }, [name]);

  useEffect(() => { connect(); }, [connect]);

  const power = (a: string) => api.post(`/api/v1/containers/${enc(name)}/power/${a}`).catch((e) => setStatus(e.message));
  const send = (s: string) => ws.current?.readyState === WebSocket.OPEN && ws.current.send(new TextEncoder().encode(s));
  return (
    <div className="console-page">
      <div className="console-bar">
        <Logo size={20} />
        <span className="title">{name}</span>
        <span className={`status ${connected ? "running" : "shutoff"}`}>{connected ? "Connected" : "Not connected"}</span>
        <div style={{ flex: 1 }} />
        <Dropdown label="Send keys" disabled={!connected} align="right" items={[
          { label: "Ctrl+C", onClick: () => send("\x03") },
          { label: "Ctrl+D", onClick: () => send("\x04") },
          { label: "Ctrl+Z", onClick: () => send("\x1a") },
          { label: "Ctrl+a (escape prefix)", onClick: () => send("\x01") },
        ]} />
        <Btn size="sm" icon="fullscreen" onClick={() => document.documentElement.requestFullscreen?.().then(() => fit.current?.fit())}>Full screen</Btn>
        {can("ct.power") && (
          <Dropdown label="Power" icon="power" align="right" items={[
            { label: "Start", icon: "play", onClick: () => power("start").then(() => setTimeout(connect, 2500)) },
            { label: "Shut down", icon: "power", onClick: () => power("shutdown") },
            { label: "Restart", icon: "restart", onClick: () => power("reboot").then(() => setTimeout(connect, 4000)) },
            { label: "Stop (kill)", icon: "stop", onClick: () => window.confirm("Kill all processes of the container immediately?") && power("stop") },
          ]} />
        )}
        <Btn size="sm" icon="refresh" onClick={connect}>Reconnect</Btn>
      </div>
      <div className="console-screen" style={{ padding: 6, background: "#0b1220" }}>
        <div ref={host} style={{ width: "100%", height: "100%" }} />
      </div>
      {status && <div className="console-msg">{status}</div>}
    </div>
  );
}
