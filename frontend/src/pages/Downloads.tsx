// Books on this device (docs/offline.md): what each takes, storage left, whether the browser
// may evict them, update and remove, and positions waiting to sync.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Cover } from "../components/Cover";
import { EmptyState } from "../components/EmptyState";
import { Download } from "../components/Icons";
import { DownloadSheet, InstallHint, SyncNow } from "../components/Offline";
import { PageTitle } from "../components/Shell";
import { relativeTime } from "../lib/format";
import { readerPath } from "../lib/formats";
import { platformOf } from "../lib/offline/install";
import { mb, type OfflineBook, removeAll, remove, requestPersistence, storageInfo, type StorageInfo } from "../lib/offline/store";
import { usePlayer } from "../lib/player";
import { useStore } from "../lib/store";

function parts(b: OfflineBook): string {
  const out: string[] = [];
  if (b.parts.epub) out.push("EPUB");
  if (b.parts.pdf) out.push("PDF");
  if (b.parts.audio) out.push(b.choice.audio === "low" ? "audiobook (data saver)" : "audiobook (full quality)");
  if (b.parts.readalong) out.push("read-along");
  return out.join(" · ");
}

export function DownloadsPage() {
  const { offlineBooks, toast, now, online } = useStore();
  const player = usePlayer();
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [updating, setUpdating] = useState<OfflineBook | null>(null);
  const [confirmAll, setConfirmAll] = useState(false);
  const total = offlineBooks.reduce((s, b) => s + b.bytes, 0);

  useEffect(() => {
    storageInfo().then(setInfo);
  }, [offlineBooks.length]);

  const ios = platformOf(navigator.userAgent, navigator.maxTouchPoints).startsWith("ios");
  const used = info?.usage ?? total;
  const pct = info?.quota ? Math.min(100, (used / info.quota) * 100) : 0;

  return (
    <>
      <PageTitle sub="Books on this device, for reading and listening offline" aside={<SyncNow />}>Downloads</PageTitle>
      <div className="stack downloads">
        <InstallHint />
        <section className="card storage-card" aria-label="Storage on this device">
          <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
            <strong>{mb(total)} downloaded</strong>
            {info?.quota != null && <span className="hint">{mb(used)} of {mb(info.quota)} available to Lectern used</span>}
          </div>
          {info?.quota != null && <div className="progress-bar" aria-hidden="true"><span style={{ width: `${pct}%` }} /></div>}
          <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
            {info?.persisted ? (
              <span className="hint" data-testid="persisted">Protected: the browser will not clear these to free space.</span>
            ) : (
              <>
                <span className="hint" data-testid="persisted">
                  The browser may clear downloads when the device runs low on space{ios ? ", or if you do not open Lectern for a few weeks" : ""}.
                </span>
                {info?.persisted === false && (
                  <button className="btn sm" onClick={async () => {
                    const ok = await requestPersistence();
                    setInfo(await storageInfo());
                    toast(ok ? "Downloads are protected now." : ios ? "Safari decides this itself. Installing Lectern to the Home Screen helps." : "The browser declined for now. Installing Lectern usually helps.");
                  }}>Keep downloads</button>
                )}
              </>
            )}
          </div>
        </section>

        {offlineBooks.length === 0 ? (
          <EmptyState icon={<Download />} title="Nothing downloaded yet" action={<Link className="btn" to="/app/library">Go to the library</Link>}>
            Open a book and choose <strong>Download</strong> to keep it, its audiobook, and read-along on this device.
          </EmptyState>
        ) : (
          <ul className="dl-list" aria-label="Downloaded books">
            {offlineBooks.map((b) => (
              <li key={b.id} className="dl-item" data-testid={`downloaded-${b.id}`}>
                <Cover work={b.work} />
                <div className="dl-meta">
                  <strong>{b.work.title}</strong>
                  <span className="hint">{b.work.author}</span>
                  <span className="hint">{parts(b)} · {mb(b.bytes)} · {relativeTime(b.downloadedAt, now)}</span>
                  {b.status === "changed" && <span className="badge warn">Changed on the server</span>}
                  <div className="row dl-actions">
                    {(b.parts.epub || b.parts.pdf) && <Link className="btn sm primary" to={b.parts.epub ? `/app/reader/${b.id}` : readerPath(b.work)}>Read</Link>}
                    {b.parts.audio && <button className="btn sm" onClick={() => player.open(b.work, { autoplay: true })}>Listen</button>}
                    {b.parts.readalong && <Link className="btn sm" to={`/app/read/${b.id}`}>Read along</Link>}
                    {b.status === "changed" && online && <button className="btn sm" onClick={() => setUpdating(b)}>Update</button>}
                    <button className="btn sm ghost" onClick={async () => { await remove(b.id); toast(`Removed “${b.work.title}” from this device.`); }} aria-label={`Remove ${b.work.title} from this device`}>Remove</button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
        {offlineBooks.length > 1 && (
          <div className="row" style={{ justifyContent: "flex-end" }}>
            {confirmAll ? (
              <>
                <span className="hint">Remove all {offlineBooks.length} books from this device?</span>
                <button className="btn sm ghost" onClick={() => setConfirmAll(false)}>Keep them</button>
                <button className="btn sm danger" onClick={async () => { await removeAll(); setConfirmAll(false); toast("Removed every download from this device."); }}>Remove all</button>
              </>
            ) : (
              <button className="btn sm ghost" onClick={() => setConfirmAll(true)}>Remove all…</button>
            )}
          </div>
        )}
        <p className="hint">Downloads stay on this device only and are deleted when you sign out. A book that is deleted or no longer shared with you is removed the next time Lectern is reachable.</p>
      </div>
      {updating && <DownloadSheet work={updating.work} open onClose={() => setUpdating(null)} />}
    </>
  );
}
