import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState } from "../components/EmptyState";
import { Devices as DevicesIcon, Plus } from "../components/Icons";
import { Sheet } from "../components/Sheet";
import { PageTitle } from "../components/Shell";
import { get, post } from "../lib/api";
import { absoluteTime, relativeTime } from "../lib/format";
import { useVisiblePolling } from "../lib/hooks";
import { useStore } from "../lib/store";
import type { AppPassword, SyncKey } from "../lib/types";

export function DevicesPage() {
  const { toast, me } = useStore();
  const [keys, setKeys] = useState<SyncKey[] | null>(null);
  const [pws, setPws] = useState<AppPassword[] | null>(null);
  const [confirm, setConfirm] = useState<{ kind: "sync" | "opds"; row: SyncKey } | null>(null);

  const load = useCallback(async () => {
    const d = await get<{ sync_keys: SyncKey[]; app_passwords: AppPassword[] }>("/api/devices");
    setKeys(d.sync_keys);
    setPws(d.app_passwords);
  }, []);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);
  useVisiblePolling(() => load().catch(() => {}), 15000);

  const revoke = async () => {
    if (!confirm) return;
    const path = confirm.kind === "sync" ? `/api/devices/sync-keys/${confirm.row.id}/revoke` : `/api/devices/app-passwords/${confirm.row.id}/revoke`;
    try {
      await post(path);
      toast(`Revoked “${confirm.row.label}”`);
      setConfirm(null);
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const active = (keys ?? []).filter((k) => !k.revoked_at);
  const empty = keys && pws && keys.length === 0 && pws.length === 0;

  return (
    <>
      <PageTitle aside={<Link className="btn primary" to="/app/devices/new"><Plus /> Add a device</Link>}>Devices</PageTitle>
      {empty ? (
        <EmptyState
          icon={<DevicesIcon />}
          title="No devices yet"
          action={<Link className="btn primary" to="/app/devices/new"><Plus /> Add your first device</Link>}
        >
          Each device gets its own credentials so you can revoke one without touching the others. The guided flow walks you through KOReader.
        </EmptyState>
      ) : (
        <div className="stack">
          <section className="card" aria-labelledby="sync-h">
            <h2 id="sync-h">Progress sync</h2>
            <p className="sub">
              KOReader sync keys. {active.length} active · server <code style={{ fontFamily: "var(--font-mono)" }}>{me?.sync_url}</code>
            </p>
            <Table rows={keys} kind="sync" onRevoke={(row) => setConfirm({ kind: "sync", row })} />
          </section>
          <section className="card" aria-labelledby="opds-h">
            <h2 id="opds-h">Catalogue access</h2>
            <p className="sub">
              OPDS app passwords for browsing and downloading · <code style={{ fontFamily: "var(--font-mono)" }}>{me?.opds_url}</code>
            </p>
            <Table rows={pws} kind="opds" onRevoke={(row) => setConfirm({ kind: "opds", row })} />
          </section>
        </div>
      )}
      <Sheet open={!!confirm} onClose={() => setConfirm(null)} title={`Revoke “${confirm?.row.label}”?`} variant="dialog" labelledBy="revoke-title">
        <p style={{ marginTop: 0 }}>
          {confirm?.kind === "sync"
            ? "That device will stop syncing immediately. Its saved positions stay on the server."
            : "That device will no longer be able to browse or download from the catalogue."}
        </p>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn" onClick={() => setConfirm(null)}>Keep</button>
          <button className="btn danger" onClick={revoke} data-autofocus>Revoke</button>
        </div>
      </Sheet>
    </>
  );
}

function Table({ rows, kind, onRevoke }: { rows: SyncKey[] | null; kind: "sync" | "opds"; onRevoke: (r: SyncKey) => void }) {
  if (rows === null) return <div className="hint">Loading…</div>;
  if (rows.length === 0)
    return (
      <div className="hint">
        None yet. <Link to="/app/devices/new">Add a device</Link> to create one.
      </div>
    );
  return (
    <table className="list">
      <thead>
        <tr>
          <th scope="col">Device</th>
          <th scope="col">Status</th>
          <th scope="col">Last used</th>
          <th scope="col"><span className="sr-only">Actions</span></th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const recent = r.last_used_at && Date.now() - new Date(r.last_used_at).getTime() < 5 * 60 * 1000;
          return (
            <tr key={r.id} className={r.revoked_at ? "revoked" : ""}>
              <td>
                <strong>{r.label}</strong>
                <div className="hint">added {relativeTime(r.created_at)}</div>
              </td>
              <td>
                {r.revoked_at ? (
                  <span className="badge">revoked {relativeTime(r.revoked_at)}</span>
                ) : recent ? (
                  <span className="badge ok"><span className="live-dot" aria-hidden="true" /> active now</span>
                ) : r.last_used_at ? (
                  <span className="badge ok">connected</span>
                ) : (
                  <span className="badge warn">never used</span>
                )}
              </td>
              <td title={absoluteTime(r.last_used_at)}>{relativeTime(r.last_used_at)}</td>
              <td style={{ textAlign: "right" }}>
                {!r.revoked_at && (
                  <button className="btn sm danger" onClick={() => onRevoke(r)} aria-label={`Revoke ${kind === "sync" ? "sync key" : "app password"} ${r.label}`}>
                    Revoke
                  </button>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
