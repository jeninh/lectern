import { useEffect, useState } from "react";
import { absoluteTime } from "../lib/format";
import { elevenLabsStatus, revokeElevenLabs, saveElevenLabsKey, testElevenLabs, type ElevenLabsStatus } from "../lib/audiobook";
import { useStore } from "../lib/store";

/** Admin → Integrations: the owner's own ElevenLabs key. Write-only: never shown again. */
export function ElevenLabsCard() {
  const { toast } = useStore();
  const [st, setSt] = useState<ElevenLabsStatus | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    elevenLabsStatus().then(setSt).catch(() => {});
  }, []);

  const run = async (fn: () => Promise<ElevenLabsStatus>, ok: string) => {
    setBusy(true);
    try {
      setSt(await fn());
      setKey("");
      toast(ok);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card" id="integrations" aria-labelledby="el-h" style={{ marginTop: 18 }}>
      <h2 id="el-h">ElevenLabs (optional)</h2>
      <p className="sub">
        Use your own ElevenLabs account for “Make an audiobook”. Your key is encrypted on this server and never shown again. Lectern contacts only {st?.endpoint ?? "https://api.elevenlabs.io"}, and only when you test the key, list voices, play a sample, estimate, or start a narration you confirmed. A narration sends the chosen chapters’ text to ElevenLabs.
      </p>
      {st?.offline && (
        <div className="alert warn" style={{ marginBottom: 10 }}>
          Unavailable: Lectern is in offline mode (Admin → Offline mode), so nothing is sent to ElevenLabs.
        </div>
      )}
      {st?.configured ? (
        <div className="stack" style={{ gap: 8 }}>
          <div>
            Key <code className="mono">{st.key_hint}</code>
            {st.undecryptable && <span className="error"> · cannot be decrypted (the server's secret key changed): enter it again</span>}
            {st.last_tested_at && <> · tested {absoluteTime(st.last_tested_at)}: {st.last_test_ok ? st.last_test_detail : `failed (${st.last_test_detail})`}</>}
          </div>
          <div className="hint">Last 30 days: {st.usage.characters.toLocaleString()} characters, {Math.round(st.usage.credits).toLocaleString()} credits{st.last_used_at ? ` · last used ${absoluteTime(st.last_used_at)}` : ""}.</div>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn sm" disabled={busy || st.offline} onClick={() => run(testElevenLabs, "Key tested.")}>Test</button>
            <button className="btn sm danger" disabled={busy} onClick={() => run(revokeElevenLabs, "ElevenLabs key removed.")}>Remove key</button>
          </div>
        </div>
      ) : null}
      <form
        className="row"
        style={{ gap: 8, marginTop: 10 }}
        onSubmit={(e) => {
          e.preventDefault();
          if (key.trim()) run(() => saveElevenLabsKey(key.trim()), "ElevenLabs key saved and tested.");
        }}
      >
        <label className="field" style={{ flex: 1 }}>
          <span>{st?.configured ? "Replace key" : "API key"}</span>
          <input type="password" autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)} placeholder="sk_…" />
        </label>
        <button className="btn" disabled={busy || key.trim().length < 8}>Save and test</button>
      </form>
    </section>
  );
}
