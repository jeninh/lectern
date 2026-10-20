import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Copy, Link2, Plus, Users as UsersIcon } from "../components/Icons";
import { OverflowMenu } from "../components/Menu";
import { Sheet } from "../components/Sheet";
import { PageTitle } from "../components/Shell";
import { del, get, post, put } from "../lib/api";
import { absoluteTime, relativeTime } from "../lib/format";
import { useStore } from "../lib/store";
import type { Invite, User } from "../lib/types";
import { CapForm } from "./Devices";

/** Instance-wide switches. Metadata lookup is off by default because it sends data outside. */
function LibrarySettingsCard() {
  const { toast, refreshMe, me } = useStore();
  const on = !!me?.library_settings?.metadata_lookup_enabled;
  const [busy, setBusy] = useState(false);
  const flip = async () => {
    setBusy(true);
    try {
      await put("/api/library-settings", { metadata_lookup_enabled: !on });
      await refreshMe();
      toast(on ? "Metadata suggestions turned off." : "Metadata suggestions turned on.");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card" id="library" aria-labelledby="libset-h" style={{ marginTop: 18 }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
        <div>
          <h2 id="libset-h">Metadata suggestions</h2>
          <p className="sub" id="libset-sub">
            Lets owners and members ask Open Library and Google Books for a book’s details while editing. When used, the ISBN (or the title and first author) is sent to them; no account or key is involved. Suggestions are only applied field by field, never automatically. Off by default.
          </p>
        </div>
        <button type="button" role="switch" className="switch" aria-checked={on} aria-labelledby="libset-h" aria-describedby="libset-sub" onClick={flip} disabled={busy}>
          <span aria-hidden="true" />
        </button>
      </div>
    </section>
  );
}

export function AdminPage() {
  const { toast, me } = useStore();
  const [users, setUsers] = useState<User[] | null>(null);
  const [params, setParams] = useSearchParams();
  const open = params.get("new") === "1";
  const setOpen = (v: boolean) => setParams(v ? { new: "1" } : {}, { replace: true });
  const load = useCallback(() => get<{ users: User[] }>("/api/admin/users").then((d) => setUsers(d.users)), []);
  const [capFor, setCapFor] = useState<User | null>(null);
  const setRole = async (u: User, role: "member" | "reader") => {
    try {
      await post(`/api/admin/users/${u.id}/role`, { role });
      toast(`${u.username} is now a ${role}${role === "reader" ? ". Share shelves with them from the Library." : ""}`);
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  useEffect(() => {
    load().catch(() => {});
  }, [load]);

  const toggle = async (u: User) => {
    try {
      await post(`/api/admin/users/${u.id}/${u.is_active ? "disable" : "enable"}`);
      toast(`${u.username} ${u.is_active ? "disabled" : "enabled"}`);
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  return (
    <>
      <PageTitle aside={<button className="btn primary" onClick={() => setOpen(true)}><Plus /> Add user</button>}sub={me ? `Server ${me.base_url} · ${me.counts.works} books · ${me.counts.users} ${me.counts.users === 1 ? "user" : "users"}` : undefined}>Admin</PageTitle>
      <section className="card" aria-labelledby="users-h">
        <h2 id="users-h">Users</h2>
        <p className="sub">Members see the whole library and can upload. Readers see only the shelves shared with them and cannot upload.</p>
        {users === null ? (
          <div className="hint">Loading…</div>
        ) : (
          <table className="list">
            <thead>
              <tr><th scope="col">User</th><th scope="col">Role</th><th scope="col">Last sign-in</th><th scope="col">Downloads/h</th><th scope="col"><span className="sr-only">Actions</span></th></tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className={u.is_active ? "" : "revoked"}>
                  <td><strong>{u.display_name}</strong><div className="hint">@{u.username}{u.totp_enabled ? " · 2FA" : ""}</div></td>
                  <td><span className={`badge ${u.role === "owner" ? "accent" : ""}`}>{u.role}</span>{!u.is_active && <span className="badge danger" style={{ marginLeft: 6 }}>disabled</span>}</td>
                  <td>{relativeTime(u.last_login_at)}</td>
                  <td>{u.download_cap_per_hour == null ? <span className="hint">default</span> : u.download_cap_per_hour === 0 ? "no limit" : u.download_cap_per_hour}</td>
                  <td style={{ textAlign: "right" }}>
                    <div className="row" style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
                      {u.role !== "owner" && u.id !== me?.user.id && (
                        <button className={`btn sm ${u.is_active ? "danger" : ""}`} onClick={() => toggle(u)}>{u.is_active ? "Disable" : "Enable"}</button>
                      )}
                      <OverflowMenu
                        label={`More for ${u.username}`}
                        actions={[
                          ...(u.role === "member" ? [{ label: "Make reader", onSelect: () => setRole(u, "reader") }] : []),
                          ...(u.role === "reader" ? [{ label: "Make member", onSelect: () => setRole(u, "member") }] : []),
                          { label: "Download limit…", onSelect: () => setCapFor(u) },
                        ]}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <InvitesCard />
      <LibrarySettingsCard />
      <Sheet open={!!capFor} onClose={() => setCapFor(null)} title={capFor ? `Download limit for @${capFor.username}` : ""} variant="dialog" labelledBy="ucap-title">
        {capFor && (
          <CapForm
            initial={capFor.download_cap_per_hour ?? null}
            what={capFor.username}
            onSave={async (cap) => {
              await post(`/api/admin/users/${capFor.id}/download-cap`, { cap });
              toast(`Limit for ${capFor.username} saved`);
              setCapFor(null);
              await load();
            }}
          />
        )}
      </Sheet>
      <Sheet open={open} onClose={() => setOpen(false)} title="Add a user" variant="dialog" labelledBy="add-user">
        <NewUserForm
          onDone={() => {
            setOpen(false);
            load();
          }}
        />
      </Sheet>
    </>
  );
}

function NewUserForm({ onDone }: { onDone: () => void }) {
  const { toast } = useStore();
  const [f, setF] = useState({ username: "", display_name: "", role: "member", password: "" });
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await post("/api/admin/users", f);
          toast(`Created ${f.username}`);
          onDone();
        } catch (ex) {
          setErr((ex as Error).message);
        }
      }}
    >
      {err && <div className="alert error" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <label className="field"><span>Username</span><input className="input" value={f.username} onChange={set("username")} pattern="[a-z0-9][a-z0-9._-]{1,31}" required data-autofocus autoCapitalize="none" /></label>
      <label className="field"><span>Display name</span><input className="input" value={f.display_name} onChange={set("display_name")} maxLength={128} /></label>
      <label className="field"><span>Role</span>
        <select className="select" value={f.role} onChange={set("role")}>
          <option value="member">member — reads and syncs</option>
          <option value="reader">reader — only shelves you share</option>
        </select>
      </label>
      <label className="field"><span>Initial password (12+)</span><input className="input" type="password" value={f.password} onChange={set("password")} minLength={12} required autoComplete="new-password" /></label>
      <p className="hint"><UsersIcon width={14} height={14} /> Share the password out of band. They should change it and add a passkey on first sign-in.</p>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn primary">Create user</button>
      </div>
    </form>
  );
}

const STATE_BADGE: Record<Invite["state"], string> = { pending: "accent", used: "ok", expired: "", revoked: "danger" };

function InvitesCard() {
  const { toast } = useStore();
  const [invites, setInvites] = useState<Invite[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<Invite | null>(null);
  const load = useCallback(() => get<{ invites: Invite[] }>("/api/invites").then((d) => setInvites(d.invites)), []);
  useEffect(() => {
    load().catch(() => setInvites([]));
  }, [load]);
  const revoke = async (inv: Invite) => {
    try {
      await del(`/api/invites/${inv.id}`);
      toast("Invite revoked");
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  return (
    <section className="card" id="invites" aria-labelledby="inv-h" style={{ marginTop: 18 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h2 id="inv-h">Household invites</h2>
          <p className="sub">A one-use link that expires. The person opening it picks their own username and password.</p>
        </div>
        <button className="btn" onClick={() => { setCreated(null); setCreating(true); }}><Link2 /> New invite</button>
      </div>
      {invites === null ? (
        <div className="hint">Loading…</div>
      ) : invites.length === 0 ? (
        <div className="hint">No invites yet.</div>
      ) : (
        <table className="list">
          <thead><tr><th scope="col">Invite</th><th scope="col">Role</th><th scope="col">Status</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>
            {invites.map((i) => (
              <tr key={i.id} className={i.state === "pending" ? "" : "revoked"}>
                <td><strong>{i.note || `Invite #${i.id}`}</strong><div className="hint">by @{i.created_by} · {relativeTime(i.created_at)}</div></td>
                <td><span className="badge">{i.role}</span></td>
                <td>
                  <span className={`badge ${STATE_BADGE[i.state]}`}>{i.state}</span>
                  <div className="hint">
                    {i.state === "pending" ? <>expires <time title={absoluteTime(i.expires_at)}>{relativeTime(i.expires_at).replace(" ago", "")}</time></> : i.state === "used" ? <>by @{i.used_by} {relativeTime(i.used_at)}</> : null}
                  </div>
                </td>
                <td style={{ textAlign: "right" }}>{i.state === "pending" && <button className="btn sm danger" onClick={() => revoke(i)}>Revoke</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Sheet open={creating} onClose={() => setCreating(false)} title={created ? "Invite link" : "New invite"} variant="dialog" labelledBy="inv-title">
        {created ? <InviteLink inv={created} onDone={() => setCreating(false)} /> : <InviteForm onCreated={(inv) => { setCreated(inv); load(); }} />}
      </Sheet>
    </section>
  );
}

function InviteForm({ onCreated }: { onCreated: (i: Invite) => void }) {
  const [role, setRole] = useState<"member" | "reader">("reader");
  const [note, setNote] = useState("");
  const [hours, setHours] = useState(72);
  const [err, setErr] = useState<string | null>(null);
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          onCreated(await post<Invite>("/api/invites", { role, note, expires_in_hours: hours }));
        } catch (ex) {
          setErr((ex as Error).message);
        }
      }}
    >
      {err && <div className="alert error" role="alert" style={{ marginBottom: 12 }}>{err}</div>}
      <label className="field"><span>Who is it for? (optional)</span><input className="input" value={note} onChange={(e) => setNote(e.target.value)} maxLength={128} placeholder="e.g. Sam’s tablet" data-autofocus /></label>
      <label className="field"><span>Role</span>
        <select className="select" value={role} onChange={(e) => setRole(e.target.value as "member" | "reader")}>
          <option value="reader">Reader — sees only shelves you share</option>
          <option value="member">Member — sees the whole library and can upload</option>
        </select>
      </label>
      <label className="field"><span>Expires after</span>
        <select className="select" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
          <option value={24}>1 day</option>
          <option value={72}>3 days</option>
          <option value={168}>1 week</option>
          <option value={720}>30 days</option>
        </select>
      </label>
      <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn primary"><Plus /> Create link</button></div>
    </form>
  );
}

function InviteLink({ inv, onDone }: { inv: Invite; onDone: () => void }) {
  const { toast } = useStore();
  const [copied, setCopied] = useState(false);
  return (
    <div className="stack">
      <p className="sub" style={{ margin: 0 }}>Send this link privately. It works once and is shown only now; Lectern keeps only a hash.</p>
      <div className="secret"><code>{inv.url}</code></div>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button
          className="btn"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(inv.url ?? "");
              setCopied(true);
            } catch {
              toast("Copy failed; select the link instead.", "error");
            }
          }}
        >
          <Copy /> {copied ? "Copied" : "Copy link"}
        </button>
        <button className="btn primary" onClick={onDone} data-autofocus>Done</button>
      </div>
      <p className="hint">{inv.role === "reader" ? "After they join, share shelves with them from Library → Shelves." : "Members see the whole library as soon as they join."}</p>
    </div>
  );
}
