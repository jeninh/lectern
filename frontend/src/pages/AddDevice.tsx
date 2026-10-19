import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Copy } from "../components/Icons";
import { get, post } from "../lib/api";
import { percent, relativeTime } from "../lib/format";
import { useInterval } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { CreatedDevice, SyncKeyStatus } from "../lib/types";

type Step = 1 | 2 | 3;
const SUGGESTIONS = ["Kobo Clara", "Kindle (KOReader)", "Boox Page", "PocketBook", "Phone"];

function CopyButton({ value, label }: { value: string; label: string }) {
  const { toast } = useStore();
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn sm ghost copy"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
          toast(`${label[0].toUpperCase()}${label.slice(1)} copied`);
          window.setTimeout(() => setDone(false), 1600);
        } catch {
          toast("Copy failed; select the text instead", "error");
        }
      }}
      aria-label={`Copy ${label}`}
    >
      {done ? <Check /> : <Copy />} <span>{done ? "Copied" : "Copy"}</span>
    </button>
  );
}

function Value({ label, value, secret }: { label: string; value: string; secret?: boolean }) {
  return (
    <div className={`value-row${secret ? " secret" : ""}`}>
      <span className="value-label">{label}</span>
      <code>{value}</code>
      <CopyButton value={value} label={label.toLowerCase()} />
    </div>
  );
}

/** Stepped flow rendered inside the Devices sheet: name → credentials → first sync. */
export function AddDeviceFlow({ onDone, onCreated }: { onDone: () => void; onCreated: () => void }) {
  const { me, toast } = useStore();
  const [step, setStep] = useState<Step>(1);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedDevice | null>(null);
  const [status, setStatus] = useState<SyncKeyStatus | null>(null);
  const labelRef = useRef<HTMLInputElement>(null);
  const headRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (step === 1) labelRef.current?.focus();
    else headRef.current?.focus();
  }, [step]);

  // Live first-sync confirmation: poll the new key from creation until KOReader pushes a position.
  const keyId = created?.device.sync_key?.id;
  const synced = !!(status?.connected && status.latest_sync);
  useInterval(
    () => {
      if (!keyId) return;
      get<SyncKeyStatus>(`/api/devices/sync-keys/${keyId}`)
        .then((s) => {
          setStatus((prev) => {
            if (!prev?.connected && s.connected) toast(`${created?.device.label} signed in`);
            if (!prev?.latest_sync && s.latest_sync) toast(`First sync from ${created?.device.label}`);
            return s;
          });
          if (s.connected) setStep(3);
        })
        .catch(() => {});
    },
    keyId && !synced ? 2500 : null,
  );

  const create = async () => {
    const name = label.trim();
    if (!name) return;
    setBusy(true);
    setErr(null);
    try {
      const out = await post<CreatedDevice>("/api/devices", { label: name });
      setCreated(out);
      setStep(2);
      onCreated();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const steps: { n: Step; label: string }[] = [
    { n: 1, label: "Name" },
    { n: 2, label: "Credentials" },
    { n: 3, label: "First sync" },
  ];

  return (
    <div className="add-device">
      <ol className="steps" aria-label="Steps">
        {steps.map((s) => (
          <li key={s.n} className="step" data-state={s.n === step ? "active" : s.n < step ? "done" : "todo"} aria-current={s.n === step ? "step" : undefined}>
            <i aria-hidden="true">{s.n < step ? <Check width={12} height={12} /> : s.n}</i>
            <span>{s.label}</span>
            {s.n < step && <span className="sr-only"> (done)</span>}
          </li>
        ))}
      </ol>

      {step === 1 && (
        <form
          className="step-body"
          onSubmit={(e) => {
            e.preventDefault();
            create();
          }}
        >
          <p className="lede">One device gets a KOReader sync key and an OPDS catalogue password, revocable together.</p>
          {err && <div className="alert error" role="alert">{err}</div>}
          <label className="field">
            <span>Device name</span>
            <input ref={labelRef} data-autofocus className="input" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Kobo Clara" maxLength={128} required autoComplete="off" />
          </label>
          <div className="chips" role="group" aria-label="Suggestions">
            {SUGGESTIONS.map((s) => (
              <button type="button" key={s} className="chip" aria-pressed={label === s} onClick={() => (setLabel(s), labelRef.current?.focus())}>{s}</button>
            ))}
          </div>
          <div className="sheet-actions">
            <button type="button" className="btn ghost" onClick={onDone}>Cancel</button>
            <button className="btn primary" disabled={busy || !label.trim()}>{busy ? "Creating…" : "Create credentials"}</button>
          </div>
        </form>
      )}

      {step >= 2 && created && me && (
        <div className="step-body">
          {step === 2 && (
            <>
              <h3 ref={headRef} tabIndex={-1} className="step-title">Enter these on {created.device.label}</h3>
              <div className="alert warn" role="note">Passwords are shown <strong>once</strong>. Copy them now. You can revoke the device and add it again at any time.</div>
              <div className="wait" role="status" aria-live="polite">
                <span className="spinner" aria-hidden="true" />
                <div>
                  <strong>Waiting for {created.device.label} to sign in…</strong>
                  <div className="hint">Press Login in KOReader. This updates by itself.</div>
                </div>
              </div>
            </>
          )}

          {step === 3 && (
            <div className={`first-sync ${synced ? "done" : status?.connected ? "half" : ""}`} role="status" aria-live="polite">
              {synced && status?.latest_sync ? (
                <>
                  <span className="big-check" aria-hidden="true"><Check /></span>
                  <div>
                    <h3 ref={headRef} tabIndex={-1} className="step-title">{created.device.label} is syncing</h3>
                    <p>
                      {percent(status.latest_sync.percentage)} into{" "}
                      <Link to={`/app/library/${status.latest_sync.work_id}`} onClick={onDone}>{status.latest_sync.title}</Link>{" "}
                      on {status.latest_sync.device || created.device.label}, {relativeTime(status.latest_sync.at)}.
                    </p>
                  </div>
                </>
              ) : status?.connected ? (
                <>
                  <span className="status-dot live" aria-hidden="true" />
                  <div>
                    <h3 ref={headRef} tabIndex={-1} className="step-title">{created.device.label} signed in</h3>
                    <p>Now open any book from your library on it and turn a page. The position appears here.</p>
                  </div>
                </>
              ) : (
                <>
                  <span className="spinner" aria-hidden="true" />
                  <div>
                    <h3 ref={headRef} tabIndex={-1} className="step-title">Waiting for {created.device.label}…</h3>
                    <p>Press <strong>Login</strong> under Progress sync in KOReader. This updates by itself.</p>
                  </div>
                </>
              )}
            </div>
          )}

          <section className="config" aria-labelledby="ko-h">
            <h4 id="ko-h">1 · KOReader progress sync</h4>
            <p className="hint">Tools → Progress sync → Custom sync server, then <strong>Login</strong> (not Register).</p>
            <Value label="Server" value={me.sync_url} />
            <Value label="Username" value={created.username} />
            {step === 2 ? <Value label="Password" value={created.sync_secret} secret /> : <div className="value-row"><span className="value-label">Password</span><span className="hint">hidden after setup</span></div>}
          </section>
          <section className="config" aria-labelledby="opds-h">
            <h4 id="opds-h">2 · Catalogue (OPDS)</h4>
            <p className="hint">KOReader: Search → OPDS catalog → +. Works in any OPDS reader.</p>
            <Value label="Catalogue URL" value={me.opds_url} />
            <Value label="Username" value={created.username} />
            {step === 2 ? <Value label="Password" value={created.opds_secret} secret /> : <div className="value-row"><span className="value-label">Password</span><span className="hint">hidden after setup</span></div>}
          </section>

          <div className="sheet-actions">
            {step === 2 ? (
              <>
                <button type="button" className="btn ghost" onClick={onDone}>Finish later</button>
                <button type="button" className="btn primary" onClick={() => setStep(3)}>I’ve entered them</button>
              </>
            ) : (
              <button type="button" className={`btn ${synced ? "primary" : ""}`} onClick={onDone}>{synced ? "Done" : "Close; keep waiting in the background"}</button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
