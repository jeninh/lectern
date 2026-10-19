import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Check, Copy, Devices as DevicesIcon } from "../components/Icons";
import { PageTitle } from "../components/Shell";
import { get, post } from "../lib/api";
import { relativeTime } from "../lib/format";
import { useInterval } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { SyncKeyStatus } from "../lib/types";

type Kind = "koreader" | "opds" | "both";
type Step = 1 | 2 | 3;

interface Created {
  syncSecret?: string;
  syncKeyId?: number;
  opdsSecret?: string;
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const { toast } = useStore();
  const [done, setDone] = useState(false);
  return (
    <button
      className="btn sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
          toast(`${label} copied`);
          window.setTimeout(() => setDone(false), 1500);
        } catch {
          toast("Copy failed; select the text instead", "error");
        }
      }}
      aria-label={`Copy ${label}`}
    >
      {done ? <Check /> : <Copy />} {done ? "Copied" : "Copy"}
    </button>
  );
}

export function AddDevicePage() {
  const { me, toast } = useStore();
  const nav = useNavigate();
  const [step, setStep] = useState<Step>(1);
  const [kind, setKind] = useState<Kind>("both");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<Created | null>(null);
  const [status, setStatus] = useState<SyncKeyStatus | null>(null);
  const labelRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (step === 2) labelRef.current?.focus();
  }, [step]);

  // Live first-sync confirmation: poll the new key until KOReader uses it.
  const polling = step === 3 && !!created?.syncKeyId && !(status?.connected && status?.latest_sync);
  useInterval(
    () => {
      if (!created?.syncKeyId) return;
      get<SyncKeyStatus>(`/api/devices/sync-keys/${created.syncKeyId}`)
        .then((s) => {
          setStatus((prev) => {
            if (!prev?.connected && s.connected) toast(`${label || "Device"} connected`);
            return s;
          });
        })
        .catch(() => {});
    },
    polling ? 3000 : null,
  );

  const create = async () => {
    if (!label.trim()) return;
    setBusy(true);
    try {
      const out: Created = {};
      if (kind !== "opds") {
        const r = await post<{ sync_key: { id: number }; secret: string }>("/api/devices/sync-keys", { label: label.trim() });
        out.syncSecret = r.secret;
        out.syncKeyId = r.sync_key.id;
      }
      if (kind !== "koreader") {
        const r = await post<{ secret: string }>("/api/devices/app-passwords", { label: label.trim() });
        out.opdsSecret = r.secret;
      }
      setCreated(out);
      setStep(3);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const steps: { n: Step; label: string }[] = [
    { n: 1, label: "What to connect" },
    { n: 2, label: "Name the device" },
    { n: 3, label: "Set up & confirm" },
  ];

  return (
    <>
      <PageTitle>Add a device</PageTitle>
      <ol className="steps" aria-label="Progress">
        {steps.map((s) => (
          <li key={s.n} className="step" data-state={s.n === step ? "active" : s.n < step ? "done" : "todo"} aria-current={s.n === step ? "step" : undefined}>
            <i aria-hidden="true">{s.n < step ? "✓" : s.n}</i>
            {s.label}
          </li>
        ))}
      </ol>

      {step === 1 && (
        <div className="card">
          <h2>What should this device do?</h2>
          <p className="sub">You can always add the other half later from the Devices page.</p>
          <div className="choice-grid" role="radiogroup" aria-label="Device capability">
            {(
              [
                ["both", "Sync and download", "KOReader: reading positions plus the OPDS catalogue."],
                ["koreader", "Sync only", "Progress sync across devices, nothing else."],
                ["opds", "Catalogue only", "An app password for any OPDS reader (KOReader, Thorium, Moon+)."],
              ] as [Kind, string, string][]
            ).map(([k, t, d]) => (
              <button key={k} className="choice" role="radio" aria-checked={kind === k} aria-pressed={kind === k} onClick={() => setKind(k)}>
                <strong>{t}</strong>
                <span>{d}</span>
              </button>
            ))}
          </div>
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 18 }}>
            <button className="btn primary" onClick={() => setStep(2)}>Continue</button>
          </div>
        </div>
      )}

      {step === 2 && (
        <form
          className="card"
          onSubmit={(e) => {
            e.preventDefault();
            create();
          }}
        >
          <h2>Name the device</h2>
          <p className="sub">Shown in activity and on Continue cards, so pick something you will recognise.</p>
          <label className="field">
            <span>Device name</span>
            <input ref={labelRef} className="input" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Kobo Clara, Boox Page, Pixel…" maxLength={128} required />
          </label>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <button type="button" className="btn ghost" onClick={() => setStep(1)}>Back</button>
            <button className="btn primary" disabled={busy || !label.trim()}>{busy ? "Creating…" : "Create credentials"}</button>
          </div>
        </form>
      )}

      {step === 3 && created && (
        <div className="stack">
          <div className="alert warn" role="status">These secrets are shown once. Copy them into the device now; you can revoke and reissue later.</div>

          {created.syncSecret && (
            <section className="card" aria-labelledby="ko-h">
              <h2 id="ko-h">KOReader progress sync</h2>
              <p className="sub">Tools ▸ Progress sync ▸ Custom sync server, then <strong>Login</strong> (not Register).</p>
              <dl className="kv">
                <dt>Server</dt>
                <dd className="row"><code style={{ fontFamily: "var(--font-mono)" }}>{me?.sync_url}</code><CopyButton value={me?.sync_url ?? ""} label="server URL" /></dd>
                <dt>Username</dt>
                <dd className="row"><code style={{ fontFamily: "var(--font-mono)" }}>{me?.user.username}</code><CopyButton value={me?.user.username ?? ""} label="username" /></dd>
                <dt>Password</dt>
                <dd><div className="secret"><code>{created.syncSecret}</code><CopyButton value={created.syncSecret} label="sync key" /></div></dd>
              </dl>
              <div style={{ marginTop: 16 }}>
                {status?.connected && status.latest_sync ? (
                  <div className="connected" role="status">
                    <div className="cover"><div className="fallback"><strong>{status.latest_sync.title.slice(0, 18)}</strong></div></div>
                    <div>
                      <strong><Check width={14} height={14} /> {label} is syncing</strong>
                      <div className="hint">
                        {Math.round(status.latest_sync.percentage * 100)}% in <Link to={`/app/library/${status.latest_sync.work_id}`}>{status.latest_sync.title}</Link> · {relativeTime(status.latest_sync.at)}
                      </div>
                    </div>
                  </div>
                ) : status?.connected ? (
                  <div className="connected" role="status">
                    <div className="cover" aria-hidden="true" />
                    <div>
                      <strong><Check width={14} height={14} /> {label} signed in</strong>
                      <div className="hint">Open any book on it; the position will show here and on Home.</div>
                    </div>
                  </div>
                ) : (
                  <div className="wait" role="status" aria-live="polite">
                    <span className="spinner" aria-hidden="true" />
                    <div>
                      <strong>Waiting for the first sync…</strong>
                      <div className="hint">Press Login in KOReader. This updates by itself.</div>
                    </div>
                  </div>
                )}
              </div>
            </section>
          )}

          {created.opdsSecret && (
            <section className="card" aria-labelledby="opds-h">
              <h2 id="opds-h">Catalogue (OPDS)</h2>
              <p className="sub">KOReader: Search ▸ OPDS catalog ▸ + Add catalog. Other readers: add an OPDS feed.</p>
              <dl className="kv">
                <dt>Catalogue URL</dt>
                <dd className="row"><code style={{ fontFamily: "var(--font-mono)" }}>{me?.opds_url}</code><CopyButton value={me?.opds_url ?? ""} label="catalogue URL" /></dd>
                <dt>Username</dt>
                <dd className="row"><code style={{ fontFamily: "var(--font-mono)" }}>{me?.user.username}</code><CopyButton value={me?.user.username ?? ""} label="username" /></dd>
                <dt>Password</dt>
                <dd><div className="secret"><code>{created.opdsSecret}</code><CopyButton value={created.opdsSecret} label="app password" /></div></dd>
              </dl>
            </section>
          )}

          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button className="btn" onClick={() => nav("/app/devices")}><DevicesIcon /> Done</button>
          </div>
        </div>
      )}
    </>
  );
}
