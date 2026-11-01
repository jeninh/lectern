import QRCode from "qrcode";
import { useCallback, useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { EmailInCard } from "../components/EmailIn";
import { Check, Key, Logout, Shield } from "../components/Icons";
import { Sheet } from "../components/Sheet";
import { PageTitle } from "../components/Shell";
import { StepUpForm, needsStepUp } from "../components/StepUp";
import { ApiError, get, logout, post } from "../lib/api";
import type { StepUpMethod } from "../lib/aitools";
import { clearDevice } from "../lib/offline/store";
import { absoluteTime, relativeTime } from "../lib/format";
import { useStore } from "../lib/store";
import type { SecurityOverview } from "../lib/types";
import { passkeysSupported, preparePasskey, registerPasskey } from "../lib/webauthn";

export function SecurityPage() {
  const { toast, refreshMe, me, setPreference, online } = useStore();
  const showTech = !!me?.preferences.show_technical_details;
  const [sec, setSec] = useState<SecurityOverview | null>(null);
  const [dialog, setDialog] = useState<"password" | "totp" | "totp-off" | "passkey" | null>(null);
  const load = useCallback(() => get<SecurityOverview>("/api/security").then(setSec), []);
  const hash = useLocation().hash.slice(1);
  useEffect(() => {
    if (sec && hash) document.getElementById(hash)?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [sec, hash]);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);

  const done = async (msg: string) => {
    toast(msg);
    setDialog(null);
    await Promise.all([load(), refreshMe()]);
  };

  // Sign out stays reachable while the rest of the page cannot load (offline).
  if (!sec) return <><PageTitle aside={<button className="btn ghost" onClick={() => logout(clearDevice)}><Logout /> Sign out</button>}>Security</PageTitle><div className="hint">{online ? "Loading…" : "Security settings need a connection."}</div></>;
  const score = (sec.passkeys.length > 0 ? 1 : 0) + (sec.totp_enabled ? 1 : 0);

  return (
    <>
      {/* Phones have no sidebar, so signing out of this device lives here too. */}
      <PageTitle aside={<button className="btn ghost" onClick={() => logout(clearDevice)}><Logout /> Sign out</button>}>Security</PageTitle>
      <div className="stack">
        <div className={`alert ${score === 2 ? "ok" : score === 1 ? "warn" : "error"}`} role="status">
          {score === 2
            ? "Strong: passkey sign-in and two-factor codes are both set up."
            : score === 1
              ? sec.passkeys.length
                ? "Good: you have a passkey. Add two-factor codes to protect the password fallback too."
                : "Good: two-factor codes are on. Add a passkey for phishing-resistant sign-in."
              : "Your account relies on a password alone. Add a passkey or two-factor codes."}
        </div>

        <section className="card" id="passkeys" aria-labelledby="pk-h" data-highlight={hash === "passkeys" || undefined}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <h2 id="pk-h">Passkeys</h2>
              <p className="sub">Sign in with Face ID, Touch ID, Windows Hello, or a security key. Preferred.</p>
            </div>
            <button className="btn primary" disabled={!sec.passkeys_possible || !passkeysSupported()} onClick={() => setDialog("passkey")}>
              <Key /> Add passkey
            </button>
          </div>
          {!sec.passkeys_possible && <div className="alert warn">Passkeys need Lectern served over HTTPS (set LECTERN_BASE_URL to an https origin).</div>}
          {sec.passkeys.length === 0 ? (
            <div className="hint">No passkeys yet.</div>
          ) : (
            <table className="list">
              <tbody>
                {sec.passkeys.map((p) => (
                  <tr key={p.id}>
                    <td>
                      <strong>{p.label}</strong>
                      <div className="hint">added {relativeTime(p.created_at)} · last used {relativeTime(p.last_used_at)}{p.backed_up ? " · synced passkey" : ""}</div>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn sm danger" onClick={() => post(`/api/security/passkeys/${p.id}/delete`).then(() => done(`Removed ${p.label}`)).catch((e) => toast(e.message, "error"))}>
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card" id="totp" aria-labelledby="totp-h" data-highlight={hash === "totp" || undefined}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <h2 id="totp-h">Two-factor codes</h2>
              <p className="sub">Required alongside the password when you are not using a passkey.</p>
            </div>
            {sec.totp_enabled ? (
              <div className="row">
                <span className="badge ok"><Check width={12} height={12} /> enabled</span>
                <button className="btn sm danger" onClick={() => setDialog("totp-off")}>Disable</button>
              </div>
            ) : (
              <button className="btn" onClick={() => setDialog("totp")}><Shield /> Enable</button>
            )}
          </div>
        </section>

        <section className="card" id="display" aria-labelledby="disp-h" data-highlight={hash === "display" || undefined}>
          <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
            <div>
              <h2 id="disp-h">Show technical details</h2>
              <p className="sub" id="disp-sub">KOReader positions, document ids, and storage paths on book cards and in the library. Off by default.</p>
            </div>
            <button
              type="button"
              role="switch"
              className="switch"
              aria-checked={showTech}
              aria-labelledby="disp-h"
              aria-describedby="disp-sub"
              onClick={() => setPreference("show_technical_details", !showTech)}
            >
              <span aria-hidden="true" />
            </button>
          </div>
        </section>

        <section className="card" id="data-saver" aria-labelledby="ds-h" data-highlight={hash === "data-saver" || undefined}>
          <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
            <div>
              <h2 id="ds-h">Data saver</h2>
              <p className="sub" id="ds-sub">Play and download the smaller copy of audiobooks when there is one, on every device that has not chosen otherwise with the Data saver switch in its player. Without a copy, the original plays. Off by default.</p>
            </div>
            <button
              type="button"
              role="switch"
              className="switch"
              aria-checked={!!me?.preferences.data_saver}
              aria-labelledby="ds-h"
              aria-describedby="ds-sub"
              onClick={() => setPreference("data_saver", !me?.preferences.data_saver)}
            >
              <span aria-hidden="true" />
            </button>
          </div>
        </section>

        {(me?.user.role === "owner" || me?.user.role === "member") && (
          <section className="card" id="adding" aria-labelledby="add-h" data-highlight={hash === "adding" || undefined}>
            <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
              <div>
                <h2 id="add-h">File certain matches automatically</h2>
                <p className="sub" id="add-sub">When an upload’s ISBN, or its exact title and author, matches a book you already have, add it without asking. Anything less certain still waits for your review. Off by default.</p>
              </div>
              <button
                type="button"
                role="switch"
                className="switch"
                aria-checked={!!me?.preferences.auto_file_imports}
                aria-labelledby="add-h"
                aria-describedby="add-sub"
                onClick={() => setPreference("auto_file_imports", !me?.preferences.auto_file_imports)}
              >
                <span aria-hidden="true" />
              </button>
            </div>
          </section>
        )}

        {(me?.user.role === "owner" || me?.user.role === "member") && <EmailInCard highlighted={hash === "email-in"} />}

        <section className="card" id="downloads" aria-labelledby="dl-h" data-highlight={hash === "downloads" || undefined}>
          <h2 id="dl-h">Downloads and alerts</h2>
          <p className="sub">
            Each account may download {sec.downloads.cap ? `${sec.downloads.cap} books an hour` : "without an hourly limit"}
            {sec.downloads.device_cap ? `, and each catalogue device ${sec.downloads.device_cap}` : ""}. More than {sec.downloads.burst_count} in {Math.round(sec.downloads.burst_window_s / 60)} minutes alerts you and the owner. Blocked downloads are recorded in Activity.
          </p>
          <div className="meter" role="meter" aria-valuenow={sec.downloads.used} aria-valuemin={0} aria-valuemax={sec.downloads.cap || sec.downloads.used || 1} aria-label="Downloads in the last hour">
            <span style={{ width: `${sec.downloads.cap ? Math.min(100, (sec.downloads.used / sec.downloads.cap) * 100) : 0}%` }} />
          </div>
          <div className="hint" style={{ marginTop: 6 }}>{sec.downloads.used} in the last hour{sec.downloads.cap ? ` of ${sec.downloads.cap}` : ""}. Per-device limits are on the Devices page; the owner sets yours under Admin.</div>
        </section>

        <section className="card" id="browsers" aria-labelledby="kd-h">
          <h2 id="kd-h">Recognised browsers</h2>
          <p className="sub">Signing in from a browser not on this list sends you and the owner an alert. Forget one to be alerted the next time it signs in.</p>
          {sec.known_devices.length === 0 ? (
            <div className="hint">None yet.</div>
          ) : (
            <table className="list">
              <tbody>
                {sec.known_devices.map((d) => (
                  <tr key={d.id}>
                    <td>
                      <strong>{d.label || "Browser"}</strong>
                      <div className="hint">first seen {relativeTime(d.first_seen_at)} · last {relativeTime(d.last_seen_at)}{d.last_ip ? ` · ${d.last_ip}` : ""}</div>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn sm ghost" onClick={() => post(`/api/security/known-devices/${d.id}/forget`).then(() => done(`Forgot ${d.label || "browser"}`)).catch((e) => toast(e.message, "error"))}>Forget</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card" id="export" aria-labelledby="exp-h" data-highlight={hash === "export" || undefined}>
          <h2 id="exp-h">Export your data</h2>
          <p className="sub">One zip of your own highlights, notes, ratings, reading stats, vocabulary, shelves, and queues, as Markdown, CSV, and JSON. No passwords or keys.</p>
          <a className="btn" href="/api/export" download>Download my data</a>
        </section>

        <section className="card" id="password" aria-labelledby="pw-h" data-highlight={hash === "password" || undefined}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <h2 id="pw-h">Password</h2>
              <p className="sub">Argon2id-hashed. Changing it signs out every browser session.</p>
            </div>
            <button className="btn" onClick={() => setDialog("password")}>Change password</button>
          </div>
        </section>

        <section className="card" id="sessions" aria-labelledby="ses-h" data-highlight={hash === "sessions" || undefined}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <h2 id="ses-h">Sessions</h2>
              <p className="sub">Browsers currently signed in as you.</p>
            </div>
            {sec.sessions.length > 1 && (
              <button className="btn sm danger" onClick={() => post("/api/security/sessions/revoke-others").then(() => done("Other sessions signed out")).catch((e) => toast(e.message, "error"))}>
                Sign out others
              </button>
            )}
          </div>
          <table className="list">
            <tbody>
              {sec.sessions.map((s) => (
                <tr key={s.id}>
                  <td>
                    <strong>{s.user_agent ? ua(s.user_agent) : "Unknown browser"}</strong> {s.current && <span className="badge accent">this browser</span>}
                    <div className="hint">{s.ip ?? "?"} · signed in {relativeTime(s.created_at)} · expires {absoluteTime(s.expires_at)}</div>
                  </td>
                  <td style={{ textAlign: "right" }}>
                    {!s.current && (
                      <button className="btn sm" onClick={() => post(`/api/security/sessions/${s.id}/revoke`).then(() => done("Session signed out")).catch((e) => toast(e.message, "error"))}>
                        Sign out
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>

      <Sheet open={dialog === "password"} onClose={() => setDialog(null)} title="Change password" variant="dialog" labelledBy="pw-dialog">
        <PasswordForm onDone={() => (window.location.href = "/login")} />
      </Sheet>
      <Sheet open={dialog === "totp"} onClose={() => setDialog(null)} title="Enable two-factor codes" variant="dialog" labelledBy="totp-dialog">
        <TotpEnroll onDone={() => done("Two-factor codes enabled")} />
      </Sheet>
      <Sheet open={dialog === "totp-off"} onClose={() => setDialog(null)} title="Disable two-factor codes" variant="dialog" labelledBy="totp-off-dialog">
        <PasswordConfirm path="/api/security/totp/disable" cta="Disable" onDone={() => done("Two-factor codes disabled")} />
      </Sheet>
      <Sheet open={dialog === "passkey"} onClose={() => setDialog(null)} title="Add a passkey" variant="dialog" labelledBy="pk-dialog">
        <PasskeyEnroll
          methods={[...(sec.passkeys_possible && sec.passkeys.length ? (["passkey"] as const) : []), ...(sec.has_password ? (["password"] as const) : [])]}
          onDone={(label) => done(`Passkey “${label}” added`)}
        />
      </Sheet>
    </>
  );
}

function ua(s: string): string {
  if (/iPhone|iPad/.test(s)) return "Safari on iOS";
  if (/Android/.test(s)) return /Firefox/.test(s) ? "Firefox on Android" : "Chrome on Android";
  const os = /Mac OS/.test(s) ? "macOS" : /Windows/.test(s) ? "Windows" : /Linux/.test(s) ? "Linux" : "";
  const br = /Firefox/.test(s) ? "Firefox" : /Edg\//.test(s) ? "Edge" : /Chrome/.test(s) ? "Chrome" : /Safari/.test(s) ? "Safari" : "Browser";
  return os ? `${br} on ${os}` : br;
}

function PasswordForm({ onDone }: { onDone: () => void }) {
  const { toast } = useStore();
  const [cur, setCur] = useState("");
  const [nw, setNw] = useState("");
  const [nw2, setNw2] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        if (nw !== nw2) return setErr("New passwords do not match.");
        setBusy(true);
        try {
          await post("/api/security/password", { current_password: cur, new_password: nw });
          toast("Password changed; sign in again");
          onDone();
        } catch (ex) {
          setErr((ex as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      {err && <div className="alert error" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <label className="field"><span>Current password</span><input className="input" type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} required data-autofocus /></label>
      <label className="field"><span>New password (12+ characters)</span><input className="input" type="password" autoComplete="new-password" minLength={12} value={nw} onChange={(e) => setNw(e.target.value)} required /></label>
      <label className="field"><span>Confirm new password</span><input className="input" type="password" autoComplete="new-password" minLength={12} value={nw2} onChange={(e) => setNw2(e.target.value)} required /></label>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn primary" disabled={busy}>Change password</button>
      </div>
    </form>
  );
}

function PasswordConfirm({ path, cta, onDone }: { path: string; cta: string; onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await post(path, { password: pw });
          onDone();
        } catch (ex) {
          setErr((ex as Error).message);
        }
      }}
    >
      {err && <div className="alert error" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <label className="field"><span>Confirm with your password</span><input className="input" type="password" autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} required data-autofocus /></label>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn danger">{cta}</button>
      </div>
    </form>
  );
}

function TotpEnroll({ onDone }: { onDone: () => void }) {
  const [data, setData] = useState<{ secret: string; uri: string } | null>(null);
  const [qr, setQr] = useState<string>("");
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    post<{ secret: string; uri: string }>("/api/security/totp/begin")
      .then(async (d) => {
        setData(d);
        setQr(await QRCode.toDataURL(d.uri, { margin: 0, width: 360, color: { dark: "#1f3a93", light: "#ffffff" } }));
      })
      .catch((e) => setErr(e.message));
  }, []);
  if (err) return <div className="alert error" role="alert">{err}</div>;
  if (!data) return <div className="hint">Preparing…</div>;
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await post("/api/security/totp/confirm", { code });
          onDone();
        } catch (ex) {
          setErr(null);
          setCode("");
          (e.target as HTMLFormElement).querySelector("input")?.focus();
          setErr((ex as Error).message);
        }
      }}
    >
      <div style={{ display: "grid", gridTemplateColumns: "180px 1fr", gap: 18, alignItems: "start" }}>
        {qr && <img className="qr" src={qr} alt="QR code for your authenticator app" />}
        <div>
          <p style={{ marginTop: 0 }}>Scan with your authenticator app, or enter the key by hand:</p>
          <div className="secret"><code>{data.secret}</code></div>
        </div>
      </div>
      <label className="field" style={{ marginTop: 16 }}>
        <span>Enter the 6-digit code it shows</span>
        <input className="input" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} value={code} onChange={(e) => setCode(e.target.value)} required data-autofocus />
      </label>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn primary">Turn on</button>
      </div>
    </form>
  );
}

/** Adding a passkey adds a way to confirm sensitive actions, so it needs one itself: name it,
 * confirm it's you (which also fetches the registration options), then tap Create so the
 * browser's passkey sheet opens inside that tap (Safari requires it). */
function PasskeyEnroll({ methods, onDone }: { methods: StepUpMethod[]; onDone: (label: string) => void }) {
  const [label, setLabel] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [stage, setStage] = useState<"label" | "confirm" | "create">("label");
  const [busy, setBusy] = useState(false);
  const name = label.trim() || "Passkey";
  if (stage === "confirm") {
    return (
      <StepUpForm
        intro="Confirm it’s you before adding a passkey."
        methods={methods}
        err={err}
        cta="Confirm and continue"
        onBack={() => (setStage("label"), setErr(null))}
        onConfirm={async (proof) => {
          setErr(null);
          try {
            await preparePasskey(proof);
            setStage("create");
          } catch (ex) {
            setErr((ex as Error).message);
          }
        }}
      />
    );
  }
  if (stage === "create") {
    return (
      <div>
        {err && <div className="alert error" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
        <p className="hint">Your browser will prompt for Face ID, Touch ID, a PIN, or a security key.</p>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button
            className="btn primary"
            disabled={busy}
            data-autofocus
            onClick={async () => {
              setBusy(true);
              setErr(null);
              try {
                await registerPasskey(name);
                onDone(name);
              } catch (ex) {
                // The options were used up or went stale: confirm again for fresh ones.
                if (ex instanceof ApiError && needsStepUp(ex.code)) setStage("confirm");
                setErr((ex as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Waiting for browser…" : `Create passkey “${name}”`}
          </button>
        </div>
      </div>
    );
  }
  return (
    <form onSubmit={(e) => (e.preventDefault(), setErr(null), setStage("confirm"))}>
      {err && <div className="alert error" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <label className="field"><span>Label</span><input className="input" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="MacBook Touch ID, YubiKey…" maxLength={128} data-autofocus /></label>
      <p className="hint">You’ll confirm it’s you, then your browser will prompt for Face ID, Touch ID, a PIN, or a security key.</p>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn primary">Continue</button>
      </div>
    </form>
  );
}
