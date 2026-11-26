// Browser VM console: noVNC (RFB over WebSocket) via the authenticated /api/v1/console proxy.
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import RFB from "@novnc/novnc/lib/rfb";
import { api } from "../api";
import { Logo } from "../icons";
import { useSession } from "../session";
import { Btn, Dropdown } from "../ui";

export function ConsolePage() {
  const { uuid = "" } = useParams();
  const { can } = useSession();
  const screen = useRef<HTMLDivElement>(null);
  const rfb = useRef<any>(null);
  const [status, setStatus] = useState("Connecting…");
  const [name, setName] = useState("");
  const [connected, setConnected] = useState(false);
  const [scale, setScale] = useState(true);

  const connect = useCallback(async () => {
    rfb.current?.disconnect();
    setStatus("Requesting console ticket…");
    setConnected(false);
    try {
      const t = await api.post(`/api/v1/vms/${uuid}/console-ticket`);
      setName(t.name);
      document.title = `${t.name} — LSX-VE console`;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const r = new RFB(screen.current!, `${proto}://${location.host}${t.path}`, {
        credentials: { password: t.password },
        wsProtocols: ["binary"],
      });
      r.scaleViewport = scale;
      r.resizeSession = false;
      r.focusOnClick = true;
      r.background = "#000";
      r.addEventListener("connect", () => { setConnected(true); setStatus(""); r.focus(); });
      r.addEventListener("disconnect", (e: any) => { setConnected(false); setStatus(e.detail?.clean ? "Console disconnected." : "Connection to the console was lost."); });
      r.addEventListener("credentialsrequired", () => r.sendCredentials({ password: t.password }));
      r.addEventListener("securityfailure", (e: any) => setStatus(`Console authentication failed: ${e.detail?.reason ?? ""}`));
      r.addEventListener("desktopname", (e: any) => e.detail?.name && setName(e.detail.name));
      rfb.current = r;
      setStatus("Connecting to console…");
    } catch (e: any) {
      setStatus(e.message);
    }
  }, [uuid]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    connect();
    return () => rfb.current?.disconnect();
  }, [connect]);
  useEffect(() => { if (rfb.current) rfb.current.scaleViewport = scale; }, [scale]);

  const power = (a: string) => api.post(`/api/v1/vms/${uuid}/power/${a}`).catch((e) => setStatus(e.message));
  const keys = (syms: [number, string][]) => {
    const r = rfb.current;
    if (!r) return;
    syms.forEach(([k, c]) => r.sendKey(k, c, true));
    [...syms].reverse().forEach(([k, c]) => r.sendKey(k, c, false));
  };
  return (
    <div className="console-page">
      <div className="console-bar">
        <Logo size={20} />
        <span className="title">{name || "Console"}</span>
        <span className={`status ${connected ? "running" : "shutoff"}`}>{connected ? "Connected" : "Not connected"}</span>
        <div style={{ flex: 1 }} />
        <Btn size="sm" icon="ctrlaltdel" disabled={!connected} onClick={() => rfb.current?.sendCtrlAltDel()}>Ctrl+Alt+Del</Btn>
        <Dropdown label="Send keys" disabled={!connected} align="right" items={[
          { label: "Ctrl+Alt+F1", onClick: () => keys([[0xffe3, "ControlLeft"], [0xffe9, "AltLeft"], [0xffbe, "F1"]]) },
          { label: "Ctrl+Alt+F2", onClick: () => keys([[0xffe3, "ControlLeft"], [0xffe9, "AltLeft"], [0xffbf, "F2"]]) },
          { label: "Ctrl+Alt+Backspace", onClick: () => keys([[0xffe3, "ControlLeft"], [0xffe9, "AltLeft"], [0xff08, "Backspace"]]) },
          { label: "Windows key", onClick: () => keys([[0xffeb, "MetaLeft"]]) },
          { label: "Print Screen", onClick: () => keys([[0xff61, "PrintScreen"]]) },
        ]} />
        <Btn size="sm" icon="fullscreen" onClick={() => document.documentElement.requestFullscreen?.()}>Full screen</Btn>
        <label className="check small"><input type="checkbox" checked={scale} onChange={(e) => setScale(e.target.checked)} /> Fit to window</label>
        {can("vm.power") && (
          <Dropdown label="Power" icon="power" align="right" items={[
            { label: "Power on", icon: "play", onClick: () => power("start").then(() => setTimeout(connect, 2500)) },
            { label: "Shut down guest", icon: "power", onClick: () => power("shutdown") },
            { label: "Restart guest", icon: "restart", onClick: () => power("reboot") },
            { label: "Reset", icon: "restart", onClick: () => window.confirm("Reset the VM without shutting down the guest?") && power("reset") },
            { label: "Power off", icon: "stop", onClick: () => window.confirm("Power off immediately? Unsaved data may be lost.") && power("poweroff") },
          ]} />
        )}
        <Btn size="sm" icon="refresh" onClick={connect}>Reconnect</Btn>
      </div>
      <div className="console-screen"><div ref={screen} /></div>
      {status && <div className="console-msg">{status}</div>}
    </div>
  );
}
