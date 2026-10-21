import { useEffect, useState } from "react";
import { get, post } from "../lib/api";
import { relativeTime } from "../lib/format";
import { countdown, downloadPlugin, pluginSetupText } from "../lib/koplugin";
import { useStore } from "../lib/store";
import type { Device, PairingCode, PluginDefaults } from "../lib/types";
import { Download, Key } from "./Icons";

/** Download lectern.koplugin for one of your devices, see its setup link, show a pairing code. */
export function KOReaderPlugin({ device, onChanged }: { device: Device; onChanged: () => void }) {
  const { toast, now } = useStore();
  const [defaults, setDefaults] = useState<PluginDefaults | null>(null);
  const [server, setServer] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [code, setCode] = useState<PairingCode | null>(null);
  const [tick, setTick] = useState(Date.now());

  useEffect(() => {
    get<PluginDefaults>("/api/devices/koplugin")
      .then((d) => {
        setDefaults(d);
        setServer((s) => s || device.plugin?.server_url || d.server_url || "");
      })
      .catch(() => {});
  }, [device.plugin?.server_url]);
  useEffect(() => {
    if (!code) return;
    const t = window.setInterval(() => setTick(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [code]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const download = () =>
    run(async () => {
      await downloadPlugin(device.id, server.trim() || null);
      toast("Plugin downloaded. Copy the lectern.koplugin folder to koreader/plugins.");
      onChanged();
    });
  const revoke = () =>
    run(async () => {
      await post(`/api/devices/${device.id}/koplugin/revoke`);
      toast("Setup link revoked");
      onChanged();
    });
  const showCode = () =>
    run(async () => {
      setCode(await post<PairingCode>(`/api/devices/${device.id}/pairing-code`));
      setTick(Date.now());
    });

  const state = pluginSetupText(device.plugin, now);
  const codeLeft = code ? countdown(code.expires_at, tick) : null;
  const codeExpired = codeLeft === "0:00";

  return (
    <div className="koplugin">
      {err && <div className="alert error" role="alert">{err}</div>}
      <section className="config" aria-labelledby="kp-dl-h">
        <h4 id="kp-dl-h">1 · Download the plugin</h4>
        <p className="hint">
          The zip sets up progress sync and the Lectern catalogue on its first start, with automatic sync. It holds a one-time setup link,
          valid for 7 days. Downloading again replaces the previous link.
        </p>
        <label className="field">
          <span>Server address for the e-reader</span>
          <input className="input" value={server} onChange={(e) => setServer(e.target.value)} placeholder="http://100.101.102.103:8080" autoComplete="off" spellCheck={false} />
        </label>
        <p className="hint">
          {defaults?.source === "tailscale"
            ? "Lectern's Tailscale address, found automatically. E-readers use plain http on port 8080 because MagicDNS is off on them."
            : defaults?.source === "config"
              ? "From LECTERN_DEVICE_URL."
              : "Use Lectern's Tailscale IP from the Tailscale admin console, on port 8080: MagicDNS is off on e-readers."}
        </p>
        {state && (
          <p className={`status ${state.tone}`} role="status">
            <span className={`status-dot ${state.tone}`} aria-hidden="true" />
            {state.text}
            {device.plugin?.set_up_at && <span className="hint"> · {relativeTime(device.plugin.set_up_at, now)}</span>}
          </p>
        )}
        <div className="row">
          <button className="btn primary" onClick={download} disabled={busy || !server.trim()}><Download /> Download lectern.koplugin.zip</button>
          {device.plugin?.setup_state === "pending" && <button className="btn ghost" onClick={revoke} disabled={busy}>Revoke setup link</button>}
        </div>
      </section>
      <section className="config" aria-labelledby="kp-install-h">
        <h4 id="kp-install-h">2 · Install it</h4>
        <ol className="hint install-steps">
          <li>Unzip it and connect the e-reader over USB.</li>
          <li>Copy the <code>lectern.koplugin</code> folder into <code>koreader/plugins/</code> (Kindle: <code>/mnt/us/koreader/plugins/</code>, Kobo: <code>.adds/koreader/plugins/</code>).</li>
          <li>Eject, restart KOReader, and turn on Wi-Fi and Tailscale. Lectern appears in the Tools menu.</li>
        </ol>
      </section>
      <section className="config" aria-labelledby="kp-pair-h">
        <h4 id="kp-pair-h">Re-pairing after a reset</h4>
        <p className="hint">In KOReader choose Tools → Lectern → Pair with a code, then enter this code. It lasts 5 minutes, works once, and allows 5 tries.</p>
        {code && !codeExpired ? (
          <div className="pair-code" role="status" aria-live="polite">
            <code aria-label={`Pairing code ${code.code.split("").join(" ")}`}>{code.code.slice(0, 3)} {code.code.slice(3)}</code>
            <span className="hint">Username <strong>{code.username}</strong> · expires in {codeLeft}</span>
          </div>
        ) : (
          <button className="btn" onClick={showCode} disabled={busy}><Key /> {code ? "Code expired. Show a new code" : "Show pairing code"}</button>
        )}
      </section>
    </div>
  );
}
