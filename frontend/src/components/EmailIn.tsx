import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { relativeTime } from "../lib/format";
import { emailInReason, emailInState, newEmailAddress, parseSenderEntries, removeEmailAddress, setEmailShelf, setSenderFilter, type EmailInState } from "../lib/ingest";
import { pathLabel } from "../lib/shelves";
import { useStore } from "../lib/store";
import { Copy } from "./Icons";

function useEmailIn() {
  const [state, setState] = useState<EmailInState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => emailInState().then(setState).catch((e) => setError((e as Error).message)), []);
  useEffect(() => {
    load();
  }, [load]);
  return { state, setState, error };
}

/** Settings → Email books in: this person's address and destination shelf. */
export function EmailInCard({ highlighted }: { highlighted?: boolean }) {
  const { toast, shelves } = useStore();
  const { state, setState, error } = useEmailIn();
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const run = async (fn: () => Promise<EmailInState | void>, done?: string) => {
    setBusy(true);
    try {
      const next = await fn();
      setState(next ?? (await emailInState()));
      if (done) toast(done);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const reason = state ? emailInReason(state) : null;
  const choices = shelves.filter((s) => !s.smart && (s.permission === "owner" || s.permission === "edit"));

  return (
    <section className="card" id="email-in" aria-labelledby="ei-h" data-highlight={highlighted || undefined}>
      <h2 id="ei-h">Email books in</h2>
      <p className="sub">
        Mail EPUB, PDF, CBZ, MOBI, or AZW3 files to your own address; they wait for you in Library → Review before anything is filed. <Link to="/app/docs/email-in">How it works</Link>
      </p>
      {error && <div className="alert error" role="alert">{error}</div>}
      {!state && !error && <div className="hint">Loading…</div>}
      {state && reason && (
        <div className={`alert ${state.reason === "offline" ? "warn" : ""}`} role="status" style={{ marginBottom: 12 }}>
          {reason}
        </div>
      )}
      {state && state.reason !== "off" && state.reason !== "not_configured" && (
        <div className="stack" style={{ gap: 14 }}>
          {state.address ? (
            <>
              <div className="email-address">
                <code aria-label="Your email-in address">{state.address}</code>
                <button
                  type="button"
                  className="btn sm"
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(state.address!);
                      setCopied(true);
                      window.setTimeout(() => setCopied(false), 2000);
                    } catch {
                      toast("Copy failed; select the address instead.", "error");
                    }
                  }}
                >
                  <Copy /> {copied ? "Copied" : "Copy"}
                </button>
              </div>
              <div className="hint">
                {state.filter.mode === "blocklist"
                  ? "Accepted from anyone except the senders the owner blocked: keep the address private."
                  : state.filter.count
                    ? `Accepted only from the ${state.filter.count === 1 ? "sender" : `${state.filter.count} senders`} the owner allowed.`
                    : "No senders are allowed yet, so mail is rejected until the owner adds some in Admin → Email-in."}{" "}
                Up to {state.max_mb} MB a message; checked every {state.poll_minutes} minutes
                {state.last_poll.at ? `, last ${relativeTime(state.last_poll.at)}` : ""}.
              </div>
              <label className="field">
                <span>Put emailed books on a shelf when you file them</span>
                <select
                  className="input"
                  value={state.shelf?.id ?? ""}
                  disabled={busy}
                  onChange={(e) => run(() => setEmailShelf(e.target.value ? Number(e.target.value) : null), "Shelf saved.")}
                >
                  <option value="">No shelf</option>
                  {choices.map((s) => (
                    <option key={s.id} value={s.id}>{pathLabel(s)}</option>
                  ))}
                </select>
              </label>
              {state.shelf_missing && (
                <div className="alert warn" role="status">The shelf you chose for emailed books is gone, so they are filed without one. Choose another shelf.</div>
              )}
              <div className="row">
                <button type="button" className="btn" disabled={busy} onClick={() => run(newEmailAddress, "New address made; the old one no longer works.")}>New address</button>
                <button type="button" className="btn ghost" disabled={busy} onClick={() => run(removeEmailAddress, "Email-in turned off for you.")}>Turn off</button>
              </div>
            </>
          ) : (
            <div>
              <button type="button" className="btn primary" disabled={busy || !state.enabled} onClick={() => run(newEmailAddress, "Your email-in address is ready.")}>Get my address</button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

/** Admin → Email-in (owner): whether it runs, what .env lacks, and the sender filter. */
export function EmailInAdminCard() {
  const { toast } = useStore();
  const { state, setState, error } = useEmailIn();
  const [mode, setMode] = useState<"allowlist" | "blocklist">("allowlist");
  const [text, setText] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [invalid, setInvalid] = useState<string[]>([]);
  useEffect(() => {
    if (!state) return;
    setMode(state.filter.mode);
    setText((state.filter.entries ?? []).join("\n"));
    setAck(false);
  }, [state]);
  if (!state) {
    return (
      <section className="card" id="email-in" style={{ marginTop: 18 }}>
        <h2>Email-in</h2>
        {error ? <div className="alert error" role="alert">{error}</div> : <div className="hint">Loading…</div>}
      </section>
    );
  }
  const reason = emailInReason(state);
  const fromEnv = state.filter.source === "env";
  const opening = mode === "blocklist" && state.filter.mode !== "blocklist";
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const parsed = parseSenderEntries(text);
    setInvalid(parsed.invalid);
    if (parsed.invalid.length) return;
    setBusy(true);
    try {
      setState(await setSenderFilter(mode, parsed.entries, ack));
      toast("Sender filter saved.");
    } catch (ex) {
      toast((ex as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const poll = state.last_poll;
  return (
    <section className="card" id="email-in" aria-labelledby="eia-h" style={{ marginTop: 18 }}>
      <h2 id="eia-h">Email-in</h2>
      <p className="sub">
        People mail books to their own address on one mailbox that Lectern reads over IMAP with TLS (outbound only). The mailbox is set in <code>.env</code> only; see <Link to="/app/docs/email-in">Docs → Email books in</Link>.
      </p>
      <div className={`alert ${state.enabled ? "ok" : state.reason === "offline" ? "warn" : ""}`} role="status" style={{ marginBottom: 12 }}>
        {state.enabled
          ? `Set up: checks the mailbox every ${state.poll_minutes} minutes; ${poll.at ? `last check ${relativeTime(poll.at)}${poll.ok === false && poll.error ? ` failed (${poll.error.replace(/_/g, " ")})` : ""}` : "no check yet"}.`
          : reason}
      </div>
      <form onSubmit={save} className="stack" style={{ gap: 12 }} aria-labelledby="eif-h">
        <h3 className="detail-h" id="eif-h">Who may send books</h3>
        {fromEnv ? (
          <div className="hint">
            Set in <code>.env</code> ({state.filter.mode}, {state.filter.count} {state.filter.count === 1 ? "entry" : "entries"}). Change <code>LECTERN_EMAIL_IN_SENDER_MODE</code> and <code>LECTERN_EMAIL_IN_SENDERS</code> there, or clear both to manage the list here.
          </div>
        ) : (
          <>
            <fieldset className="send-list">
              <legend className="sr-only">Mode</legend>
              <label className="send-option plain" data-checked={mode === "allowlist" || undefined}>
                <input type="radio" name="ei-mode" checked={mode === "allowlist"} onChange={() => setMode("allowlist")} />
                <span><strong>Allowlist</strong> (recommended): only the senders below</span>
              </label>
              <label className="send-option plain" data-checked={mode === "blocklist" || undefined}>
                <input type="radio" name="ei-mode" checked={mode === "blocklist"} onChange={() => setMode("blocklist")} />
                <span><strong>Blocklist</strong>: everyone except the senders below</span>
              </label>
            </fieldset>
            {mode === "blocklist" && (
              <div className="alert warn" role="alert">
                With a blocklist, anyone who learns someone’s Lectern address can send them files. They still have to pass the file checks and wait for review, but prefer an allowlist.
                {opening && (
                  <label className="row" style={{ marginTop: 8, gap: 8, flexWrap: "nowrap" }}>
                    <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
                    <span>I understand: accept mail from anyone not on the list.</span>
                  </label>
                )}
              </div>
            )}
            <label className="field">
              <span>{mode === "allowlist" ? "Allowed senders" : "Blocked senders"}: addresses or whole domains like @example.org, one per line</span>
              <textarea className="input" rows={4} value={text} onChange={(e) => (setText(e.target.value), setInvalid([]))} spellCheck={false} aria-invalid={invalid.length ? true : undefined} aria-describedby={invalid.length ? "eif-err" : undefined} />
            </label>
            {invalid.length > 0 && (
              <div className="field-error" id="eif-err" role="alert">Not an address or @domain: {invalid.join(", ")}</div>
            )}
            {mode === "allowlist" && !text.trim() && <div className="hint">An empty allowlist accepts nobody.</div>}
            <div>
              <button type="submit" className="btn primary" disabled={busy || (opening && !ack)}>Save</button>
            </div>
          </>
        )}
      </form>
    </section>
  );
}
