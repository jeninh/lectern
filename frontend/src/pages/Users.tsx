import { useCallback, useEffect, useState } from "react";
import { Plus, Users as UsersIcon } from "../components/Icons";
import { Sheet } from "../components/Sheet";
import { PageTitle } from "../components/Shell";
import { get, post } from "../lib/api";
import { relativeTime } from "../lib/format";
import { useStore } from "../lib/store";
import type { User } from "../lib/types";

export function UsersPage() {
  const { toast, me } = useStore();
  const [users, setUsers] = useState<User[] | null>(null);
  const [open, setOpen] = useState(false);
  const load = useCallback(() => get<{ users: User[] }>("/api/admin/users").then((d) => setUsers(d.users)), []);
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
      <PageTitle aside={<button className="btn primary" onClick={() => setOpen(true)}><Plus /> Add user</button>}>Users</PageTitle>
      <div className="card">
        {users === null ? (
          <div className="hint">Loading…</div>
        ) : (
          <table className="list">
            <thead>
              <tr><th scope="col">User</th><th scope="col">Role</th><th scope="col">Last sign-in</th><th scope="col"><span className="sr-only">Actions</span></th></tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className={u.is_active ? "" : "revoked"}>
                  <td><strong>{u.display_name}</strong><div className="hint">@{u.username}{u.totp_enabled ? " · 2FA" : ""}</div></td>
                  <td><span className={`badge ${u.role === "owner" ? "accent" : ""}`}>{u.role}</span>{!u.is_active && <span className="badge danger" style={{ marginLeft: 6 }}>disabled</span>}</td>
                  <td>{relativeTime(u.last_login_at)}</td>
                  <td style={{ textAlign: "right" }}>
                    {u.role !== "owner" && u.id !== me?.user.id && (
                      <button className={`btn sm ${u.is_active ? "danger" : ""}`} onClick={() => toggle(u)}>{u.is_active ? "Disable" : "Enable"}</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
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
          <option value="reader">reader — reads and syncs (limited later)</option>
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
