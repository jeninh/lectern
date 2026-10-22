import { useState } from "react";
import type { StepUpMethod } from "../lib/aitools";
import { passkeyAssertion, passkeysSupported } from "../lib/webauthn";

/** A fresh password or passkey sent with a sensitive action (lectern.security.stepup). */
export type StepUpProof = { password?: string; passkey?: Record<string, unknown> };

/** Whether an API error asks for (another) step-up. */
export function needsStepUp(code: string | null | undefined): boolean {
  return code === "step_up_required" || code === "step_up_failed";
}

/**
 * Confirm it's you: a passkey (a fresh challenge from /api/security/step-up/options) or the
 * account password. ``onConfirm`` sends the action again with the proof.
 */
export function StepUpForm({
  intro,
  methods,
  err,
  cta = "Confirm and save",
  onConfirm,
  onBack,
}: {
  intro: React.ReactNode;
  methods: StepUpMethod[];
  err: string | null;
  cta?: string;
  onConfirm: (proof: StepUpProof) => Promise<void>;
  onBack: () => void;
}) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [localErr, setLocalErr] = useState<string | null>(null);
  const canPasskey = methods.includes("passkey") && passkeysSupported();
  const canPassword = methods.includes("password");
  const run = async (proof: () => Promise<StepUpProof>) => {
    setBusy(true);
    setLocalErr(null);
    try {
      await onConfirm(await proof());
    } catch (e) {
      setLocalErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const shown = localErr ?? err;
  return (
    <form className="step-body step-up" onSubmit={(e) => (e.preventDefault(), run(async () => ({ password })))}>
      <p className="lede" style={{ marginTop: 0 }}>{intro}</p>
      {shown && <div className="alert error" role="alert">{shown}</div>}
      {canPasskey && (
        <button type="button" className="btn primary" disabled={busy} data-autofocus
          onClick={() => run(async () => ({ passkey: await passkeyAssertion("/api/security/step-up/options") }))}>
          Use a passkey
        </button>
      )}
      {canPassword && (
        <label className="field">
          <span>{canPasskey ? "Or confirm with your password" : "Confirm with your password"}</span>
          <input className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required {...(canPasskey ? {} : { "data-autofocus": true })} />
        </label>
      )}
      {!canPasskey && !canPassword && <p className="hint">This browser cannot use your passkey here. Try one that can, or sign in with your password.</p>}
      <div className="sheet-actions">
        <button type="button" className="btn ghost" onClick={onBack}>Back</button>
        {canPassword && <button className={`btn ${canPasskey ? "" : "primary"}`} disabled={busy || !password}>{busy ? "Saving…" : cta}</button>}
      </div>
    </form>
  );
}
