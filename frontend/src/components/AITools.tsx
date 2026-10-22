import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Check, Close, Pencil, Sparkle } from "./Icons";
import { OverflowMenu } from "./Menu";
import { Sheet } from "./Sheet";
import { CopyButton, Value } from "../pages/AddDevice";
import { ApiError, get, post } from "../lib/api";
import {
  SCOPE_TEXT,
  addedScopes,
  sameScopes,
  claudeCodeCommand,
  claudeCodeJson,
  claudeDesktopConfig,
  type AIApp,
  type AIKey,
  type AIKeyList,
  type AIScope,
  type CreatedAIKey,
  type StepUpMethod,
} from "../lib/aitools";
import { StepUpForm, needsStepUp, type StepUpProof } from "./StepUp";
import { absoluteTime, relativeTime } from "../lib/format";
import { useInterval, useVisiblePolling } from "../lib/hooks";
import { useStore } from "../lib/store";

/** Devices → AI tools: keys for AI apps (MCP), and the guided "Connect an AI app" sheet. */
export function AIToolsSection() {
  const { toast, now } = useStore();
  const [data, setData] = useState<AIKeyList | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [confirm, setConfirm] = useState<AIKey | null>(null);
  const [editing, setEditing] = useState<AIKey | null>(null);
  const load = useCallback(async () => setData(await get<AIKeyList>("/api/ai-keys")), []);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);
  useVisiblePolling(() => load().catch(() => {}), 15000);

  const revoke = async () => {
    if (!confirm) return;
    try {
      await post(`/api/ai-keys/${confirm.id}/revoke`);
      toast(`Revoked “${confirm.label}”`);
      setConfirm(null);
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const active = (data?.keys ?? []).filter((k) => !k.revoked_at);
  const revoked = (data?.keys ?? []).filter((k) => k.revoked_at);
  return (
    <section aria-labelledby="ai-h" className="ai-tools">
      <div className="section-head">
        <h2 id="ai-h">AI tools</h2>
        <button className="btn sm" onClick={() => setConnecting(true)}><Sparkle /> Connect an AI app</button>
      </div>
      <p className="hint" style={{ marginTop: 0 }}>
        AI apps on your tailnet, like Claude Desktop or Claude Code, can add and organize books for you. Each gets its own key that acts as you, only with the permissions you choose. It can never download books or audio, or change accounts or settings.
      </p>
      {data && active.length === 0 ? (
        <p className="ai-empty">No AI apps connected.</p>
      ) : (
        <ul className="ai-keys" aria-label="AI keys" aria-busy={!data}>
          {active.map((k) => <KeyRow key={k.id} k={k} now={now} onEdit={() => setEditing(k)} onRevoke={() => setConfirm(k)} />)}
        </ul>
      )}
      {revoked.length > 0 && (
        <details className="ai-revoked">
          <summary>Revoked ({revoked.length})</summary>
          <ul className="ai-keys">{revoked.map((k) => <KeyRow key={k.id} k={k} now={now} />)}</ul>
        </details>
      )}

      <Sheet open={connecting} onClose={() => (setConnecting(false), load().catch(() => {}))} title="Connect an AI app" labelledBy="ai-connect-title">
        {connecting && data && <ConnectFlow list={data} onDone={() => (setConnecting(false), load().catch(() => {}))} onCreated={() => load().catch(() => {})} />}
      </Sheet>
      <Sheet open={!!editing} onClose={() => setEditing(null)} title={`Edit “${editing?.label}”`} labelledBy="ai-edit-title">
        {editing && data && (
          <EditKey
            k={editing}
            list={data}
            onCancel={() => setEditing(null)}
            onSaved={(k) => {
              toast(`Updated “${k.label}”`);
              setEditing(null);
              load().catch(() => {});
            }}
          />
        )}
      </Sheet>
      <Sheet open={!!confirm} onClose={() => setConfirm(null)} title={`Revoke “${confirm?.label}”?`} variant="dialog" labelledBy="ai-revoke-title">
        <p style={{ marginTop: 0 }}>The AI app loses access immediately. Books it added stay in the library.</p>
        <div className="sheet-actions">
          <button className="btn ghost" onClick={() => setConfirm(null)}>Keep key</button>
          <button className="btn danger" onClick={revoke} data-autofocus>Revoke</button>
        </div>
      </Sheet>
    </section>
  );
}

function KeyRow({ k, now, onEdit, onRevoke }: { k: AIKey; now: number; onEdit?: () => void; onRevoke?: () => void }) {
  const used = k.last_used_at ? `Last used ${relativeTime(k.last_used_at, now)}${k.last_client ? ` · ${k.last_client}` : ""}` : "Never used";
  return (
    <li className="ai-key" data-revoked={k.revoked_at ? true : undefined}>
      <span className="device-icon" aria-hidden="true"><Sparkle /></span>
      <span className="ai-key-text">
        <strong>{k.label}</strong>
        <span className="hint">
          <code>{k.prefix}…</code> · <span title={absoluteTime(k.last_used_at ?? k.revoked_at)}>{k.revoked_at ? `Revoked ${relativeTime(k.revoked_at, now)}` : used}</span>
        </span>
        <span className="caps" aria-label="Scopes">{k.scopes.map((s) => <span key={s} className="cap">{SCOPE_TEXT[s].label}</span>)}</span>
      </span>
      {onEdit && <button type="button" className="btn sm ghost" onClick={onEdit} aria-label={`Edit ${k.label}`}><Pencil /> Edit</button>}
      {onRevoke && <OverflowMenu label={`Actions for ${k.label}`} actions={[{ label: "Revoke key", icon: <Close />, danger: true, onSelect: onRevoke }]} />}
    </li>
  );
}

const APPS: { id: AIApp; label: string }[] = [
  { id: "claude-desktop", label: "Claude Desktop" },
  { id: "claude-code", label: "Claude Code" },
];

function ConnectFlow({ list, onDone, onCreated }: { list: AIKeyList; onDone: () => void; onCreated: () => void }) {
  const { toast } = useStore();
  const [app, setApp] = useState<AIApp>("claude-desktop");
  const [label, setLabel] = useState("Claude Desktop");
  const [scopes, setScopes] = useState<AIScope[]>(list.default_scopes);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedAIKey | null>(null);
  const [status, setStatus] = useState<AIKey | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [methods, setMethods] = useState<StepUpMethod[]>(list.step_up);
  const headRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (created) headRef.current?.focus();
  }, [created]);

  // Test connection: poll the new key until the AI app's first request reaches /mcp.
  const connected = !!status?.last_used_at;
  useInterval(
    () => {
      if (!created) return;
      get<{ key: AIKey }>(`/api/ai-keys/${created.key.id}`)
        .then((s) => {
          if (s.key.last_used_at && !status?.last_used_at) {
            toast(`${created.key.label} connected`);
            onCreated(); // refresh the list behind the sheet
          }
          setStatus(s.key);
        })
        .catch(() => {});
    },
    created && !connected ? 2500 : null,
  );

  const pickApp = (a: AIApp) => {
    setApp(a);
    if (APPS.some((x) => x.label === label) || !label.trim()) setLabel(APPS.find((x) => x.id === a)!.label);
  };
  const onTabKey = (e: ReactKeyboardEvent) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    const next = APPS[(APPS.findIndex((a) => a.id === app) + 1) % APPS.length].id;
    pickApp(next);
    document.getElementById(`aitab-${next}`)?.focus();
  };
  const toggle = (s: AIScope) => setScopes((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]));

  // Scopes beyond the defaults need a fresh password or passkey (the server decides).
  const create = async (proof: StepUpProof = {}) => {
    setBusy(true);
    setErr(null);
    try {
      const out = await post<CreatedAIKey>("/api/ai-keys", { label: label.trim(), scopes, ...proof });
      setCreated(out);
      setStatus(out.key);
      setConfirming(false);
      onCreated();
    } catch (e) {
      if (e instanceof ApiError && needsStepUp(e.code)) {
        setMethods((e.data?.methods as StepUpMethod[]) ?? methods);
        setConfirming(true);
      }
      setErr(e instanceof ApiError && e.code === "step_up_required" ? null : (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const tabs = (
    <div className="tabs" role="tablist" aria-label="AI app" onKeyDown={onTabKey}>
      {APPS.map((a) => (
        <button key={a.id} id={`aitab-${a.id}`} role="tab" type="button" aria-selected={app === a.id} aria-controls="ai-app-panel" tabIndex={app === a.id ? 0 : -1} onClick={() => pickApp(a.id)}>
          {a.label}
        </button>
      ))}
    </div>
  );

  if (!created && confirming) {
    const extra = scopes.filter((s) => !list.default_scopes.includes(s));
    return (
      <StepUpForm
        intro={<>Confirm it’s you to create “{label.trim()}” with more than the default permissions: {extra.map((s) => SCOPE_TEXT[s].label).join(", ")}.</>}
        methods={methods}
        err={err}
        cta="Confirm and create"
        onConfirm={create}
        onBack={() => (setConfirming(false), setErr(null))}
      />
    );
  }

  if (!created) {
    return (
      <form className="step-body ai-connect" onSubmit={(e) => (e.preventDefault(), create())}>
        <p className="lede">Lectern speaks MCP at <code>{list.endpoint}</code>. Only devices on your tailnet can reach it.</p>
        {tabs}
        {err && <div className="alert error" role="alert">{err}</div>}
        <label className="field">
          <span>Name</span>
          <input className="input" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={128} required autoComplete="off" data-autofocus />
        </label>
        <fieldset className="check-list">
          <legend>What it may do</legend>
          {list.scopes.map((s) => (
            <label key={s}>
              <input type="checkbox" checked={scopes.includes(s)} onChange={() => toggle(s)} />
              <span><strong>{SCOPE_TEXT[s].label}</strong><span className="hint">{SCOPE_TEXT[s].detail}</span></span>
            </label>
          ))}
        </fieldset>
        <p className="hint">Start small; you can change this later with Edit. More than the defaults asks you to confirm it’s you. Never: downloading books or audio, users, invites, keys, or server settings.</p>
        <div className="sheet-actions">
          <button type="button" className="btn ghost" onClick={onDone}>Cancel</button>
          <button className="btn primary" disabled={busy || !label.trim() || scopes.length === 0}>{busy ? "Creating…" : "Create key"}</button>
        </div>
      </form>
    );
  }

  const { endpoint, secret } = created;
  return (
    <div className="step-body ai-connect">
      <h3 ref={headRef} tabIndex={-1} className="step-title">Add Lectern to {APPS.find((a) => a.id === app)!.label}</h3>
      <div className={`first-sync ${connected ? "done" : ""}`} role="status" aria-live="polite">
        {connected && status ? (
          <>
            <span className="big-check" aria-hidden="true"><Check /></span>
            <div>
              <h3 className="step-title">Connected</h3>
              <p>First request {relativeTime(status.last_used_at)}{status.last_client ? ` from ${status.last_client}` : ""}{status.last_ip ? ` at ${status.last_ip}` : ""}. Try asking it: “What did I add to Lectern recently?”</p>
            </div>
          </>
        ) : (
          <>
            <span className="spinner" aria-hidden="true" />
            <div>
              <h3 className="step-title">Waiting for the first request…</h3>
              <p>Restart or reload the AI app after adding the configuration. This updates by itself.</p>
            </div>
          </>
        )}
      </div>
      <div className="alert warn" role="note">The key is shown <strong>once</strong>. Copy the configuration now; it contains the key.</div>
      {tabs}
      <div id="ai-app-panel" role="tabpanel" aria-labelledby={`aitab-${app}`} className="stack-sm">
        {app === "claude-desktop" ? (
          <>
            <p className="hint">Settings → Developer → Edit Config, add this to <code>claude_desktop_config.json</code>, then restart Claude Desktop. Needs Node.js, and Tailscale running on this computer.</p>
            <Snippet label="Claude Desktop configuration" value={claudeDesktopConfig(endpoint, secret)} />
          </>
        ) : (
          <>
            <p className="hint">Run this in a terminal on a computer with Tailscale running:</p>
            <Snippet label="Claude Code command" value={claudeCodeCommand(endpoint, secret)} />
            <p className="hint">Or add it to a project’s <code>.mcp.json</code>:</p>
            <Snippet label=".mcp.json" value={claudeCodeJson(endpoint, secret)} />
          </>
        )}
        <section className="config" aria-labelledby="ai-raw-h">
          <h4 id="ai-raw-h">Any other MCP app (Streamable HTTP)</h4>
          <Value label="URL" value={endpoint} />
          <Value label="Header" value={`Authorization: Bearer ${secret}`} secret />
        </section>
      </div>
      <div className="sheet-actions">
        <button type="button" className={`btn ${connected ? "primary" : ""}`} onClick={onDone}>{connected ? "Done" : "Close; keep waiting in the background"}</button>
      </div>
    </div>
  );
}

/**
 * Change what a key may do. The key is never shown again or rotated, so the AI app keeps
 * working; the change applies to its next request. Removing permissions saves at once; adding
 * any asks for a fresh passkey or password first (the server enforces both).
 */
function EditKey({ k, list, onCancel, onSaved }: { k: AIKey; list: AIKeyList; onCancel: () => void; onSaved: (k: AIKey) => void }) {
  const [scopes, setScopes] = useState<AIScope[]>(k.scopes);
  const [stage, setStage] = useState<"edit" | "confirm">("edit");
  const [methods, setMethods] = useState<StepUpMethod[]>(list.step_up);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const added = addedScopes(k.scopes, scopes);
  const toggle = (s: AIScope) => setScopes((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]));

  /** Narrowing saves directly; a step-up error moves to the confirm stage. */
  const save = async (proof: StepUpProof = {}) => {
    setBusy(true);
    setErr(null);
    try {
      const out = await post<{ key: AIKey }>(`/api/ai-keys/${k.id}/scopes`, { scopes, ...proof });
      onSaved(out.key);
    } catch (e) {
      if (e instanceof ApiError && needsStepUp(e.code)) {
        setMethods((e.data?.methods as StepUpMethod[]) ?? methods);
        setStage("confirm");
      }
      setErr(e instanceof ApiError && e.code === "step_up_required" ? null : (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (stage === "confirm") {
    return (
      <StepUpForm
        intro={<>Confirm it’s you to give “{k.label}” more permissions: {added.map((s) => SCOPE_TEXT[s].label).join(", ")}.</>}
        methods={methods}
        err={err}
        onConfirm={save}
        onBack={() => (setStage("edit"), setErr(null))}
      />
    );
  }

  return (
    <form className="step-body ai-edit" onSubmit={(e) => (e.preventDefault(), added.length ? (setErr(null), setStage("confirm")) : save())}>
      <p className="hint" style={{ marginTop: 0 }}>
        <code>{k.prefix}…</code> stays the same, so you don’t need to set the app up again. Changes apply to its next request.
      </p>
      {err && <div className="alert error" role="alert">{err}</div>}
      <fieldset className="check-list">
        <legend>What it may do</legend>
        {list.scopes.map((s) => (
          <label key={s}>
            <input type="checkbox" checked={scopes.includes(s)} onChange={() => toggle(s)} data-scope={s} />
            <span><strong>{SCOPE_TEXT[s].label}</strong><span className="hint">{SCOPE_TEXT[s].detail}</span></span>
          </label>
        ))}
      </fieldset>
      <p className="hint" role="status" aria-live="polite">
        {added.length ? "Adding permissions asks you to confirm it’s you." : "Removing permissions takes effect immediately."}
      </p>
      <div className="sheet-actions">
        <button type="button" className="btn ghost" onClick={onCancel}>Cancel</button>
        <button className="btn primary" disabled={busy || scopes.length === 0 || sameScopes(scopes, k.scopes)}>
          {busy ? "Saving…" : added.length ? "Continue" : "Save"}
        </button>
      </div>
    </form>
  );
}

function Snippet({ label, value }: { label: string; value: string }) {
  return (
    <div className="snippet">
      <pre aria-label={label} tabIndex={0}><code>{value}</code></pre>
      <CopyButton value={value} label={label} />
    </div>
  );
}
