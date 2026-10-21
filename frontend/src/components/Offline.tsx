// The installed app's offline UI (docs/offline.md): the online/offline indicator, "Update
// ready", conflict prompts for positions saved offline, install hints, and the download sheet.

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { relativeTime } from "../lib/format";
import { useLocalStorage } from "../lib/hooks";
import { canPrompt, isStandalone, onInstallChange, platformOf, promptInstall } from "../lib/offline/install";
import { cancel, type Choice, defaultChoice, lowWorthIt, download, DownloadError, downloading, manifestFor, mb, type OfflineManifest, plannedBytes, remove, storageInfo, type StorageInfo } from "../lib/offline/store";
import { applyUpdate, flush, onSync, onUpdateReady, resolveConflict, syncState, type SyncState } from "../lib/offline/sync";
import type { QueuedPosition } from "../lib/offline/queue";
import { useStore } from "../lib/store";
import type { Work } from "../lib/types";
import { Check, Download, Sync } from "./Icons";
import { Sheet } from "./Sheet";

export function useSync(): SyncState {
  const [s, setS] = useState(syncState);
  useEffect(() => onSync(setS), []);
  return s;
}

/** Top-bar pill: offline, syncing, or positions waiting. Nothing when all is well. */
export function OfflineStatus() {
  const { online } = useStore();
  const sync = useSync();
  const waiting = sync.pending + sync.conflicts.length;
  if (online && !sync.syncing && !waiting) return null;
  const label = !online
    ? "Offline"
    : sync.syncing
      ? "Syncing…"
      : `${waiting} ${waiting === 1 ? "position" : "positions"} to sync`;
  const detail = !online
    ? `Lectern cannot be reached. Downloaded books still open${waiting ? `; ${waiting} reading ${waiting === 1 ? "position waits" : "positions wait"} to sync` : ""}.`
    : "Positions saved while offline are being sent.";
  if (online && sync.conflicts.length && !sync.syncing) {
    return (
      <button className="net-status" data-state="sync" onClick={() => window.dispatchEvent(new Event("lectern:conflicts"))} title="Choose which positions to keep" aria-label={`${label}. Choose which positions to keep.`}>
        <span className="dot" aria-hidden="true" />
        {label}
      </button>
    );
  }
  return (
    <span className="net-status" data-state={!online ? "offline" : "sync"} role="status" title={detail} aria-label={`${label}. ${detail}`}>
      <span className="dot" aria-hidden="true" />
      {label}
    </span>
  );
}

/** Under the top bar while offline: what still works. */
export function OfflineBanner() {
  const { online, offlineBooks } = useStore();
  if (online) return null;
  return (
    <div className="offline-banner" role="note">
      <strong>You are offline.</strong>{" "}
      {offlineBooks.length ? (
        <>Your {offlineBooks.length} downloaded {offlineBooks.length === 1 ? "book opens" : "books open"} as usual. <Link to="/app/downloads">Downloads</Link></>
      ) : (
        <>Books you download for offline use open without a connection.</>
      )}
    </div>
  );
}

/** A new version of the app is installed and waits for a reload. */
export function UpdateReady() {
  const [ready, setReady] = useState(false);
  useEffect(() => onUpdateReady(setReady), []);
  if (!ready) return null;
  return (
    <div className="update-ready" role="status">
      <span>A new version of Lectern is ready.</span>
      <button className="btn sm primary" onClick={applyUpdate}>Reload</button>
    </div>
  );
}

function describe(e: QueuedPosition): string {
  const b = e.body;
  if (e.kind === "page") return `page ${b.page}`;
  if (b.source === "audio" && typeof b.audio_s === "number") return `${Math.floor(b.audio_s / 60)} min into the audiobook`;
  return "where you stopped reading";
}

function describeServer(e: QueuedPosition): string {
  const s = e.conflict?.server;
  if (!s) return "another position";
  if (e.kind === "page") return `page ${s.page}`;
  if (s.source === "audio" && s.audio_s != null) return `${Math.floor(s.audio_s / 60)} min into the audiobook`;
  return s.percent != null ? `${Math.round(s.percent * 100)}%` : "another position";
}

/** One conflict at a time: a position saved here offline vs. a newer one from another device. */
export function ConflictPrompt() {
  const { now, toast } = useStore();
  const sync = useSync();
  // "Decide later" hides a conflict until the top-bar pill is tapped; it stays queued.
  const [later, setLater] = useState<Set<string>>(new Set());
  useEffect(() => {
    const show = () => setLater(new Set());
    window.addEventListener("lectern:conflicts", show);
    return () => window.removeEventListener("lectern:conflicts", show);
  }, []);
  const open = sync.conflicts.filter((c) => !later.has(c.key));
  const e = open[0];
  const [busy, setBusy] = useState(false);
  if (!e) return null;
  const server = e.conflict?.server;
  const theirs = server?.device || "Another device";
  const choose = async (c: "mine" | "theirs") => {
    setBusy(true);
    try {
      await resolveConflict(e, c);
    } catch {
      toast("Could not reach Lectern. Try again when you are online.", "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Sheet open onClose={() => setLater(new Set([...later, e.key]))} title="Which position should Lectern keep?" variant="dialog" labelledBy="conflict-title">
      <div className="stack conflict" data-testid="conflict">
        <p style={{ margin: 0 }}>
          While this device was offline you were at <strong>{describe(e)}</strong> in <strong>{e.title}</strong> ({relativeTime(e.recordedAt, now)}).
          Later, <strong>{theirs}</strong> saved <strong>{describeServer(e)}</strong> ({relativeTime(server?.received_at ?? server?.updated_at ?? null, now)}).
        </p>
        <div className="row" style={{ justifyContent: "flex-end", flexWrap: "wrap" }}>
          <button className="btn" disabled={busy} onClick={() => choose("theirs")}>Use {theirs}’s</button>
          <button className="btn primary" disabled={busy} onClick={() => choose("mine")} data-autofocus>Keep this device’s</button>
        </div>
        {open.length > 1 && <p className="hint" style={{ margin: 0 }}>{open.length - 1} more after this.</p>}
        <p className="hint" style={{ margin: 0 }}>Close to decide later: it waits under the sync count in the top bar.</p>
      </div>
    </Sheet>
  );
}

/** How to install the app here, or nothing when it is installed or cannot be. */
export function InstallHint({ compact = false }: { compact?: boolean }) {
  const [dismissed, setDismissed] = useLocalStorage<boolean>("lectern.installHintDismissed", false);
  const [, rerender] = useState(0);
  useEffect(() => onInstallChange(() => rerender((n) => n + 1)), []);
  if (isStandalone() || (compact && dismissed)) return null;
  const platform = platformOf(navigator.userAgent, navigator.maxTouchPoints);
  let body: JSX.Element | null = null;
  if (canPrompt()) {
    body = (
      <>
        <span>Install Lectern for its own icon, full-screen reading, and offline books.</span>
        <button className="btn sm primary" onClick={() => promptInstall()}>Install app</button>
      </>
    );
  } else if (platform === "ios-safari") {
    body = <span>Install Lectern: tap <strong>Share</strong> <ShareGlyph />, then <strong>Add to Home Screen</strong>. Lectern then opens full screen, keeps your downloads longer, and signs in with your passkey.</span>;
  } else if (platform === "ios-other") {
    body = <span>To install Lectern on iPhone, open it in Safari, tap <strong>Share</strong> <ShareGlyph />, then <strong>Add to Home Screen</strong>.</span>;
  } else if (platform === "android") {
    body = <span>Install Lectern: open Chrome’s menu <strong>⋮</strong>, then <strong>Add to Home screen</strong> or <strong>Install app</strong>.</span>;
  }
  if (!body) return null;
  return (
    <div className="install-hint" data-platform={platform} data-testid="install-hint">
      {body}
      {compact && <button className="btn sm ghost" onClick={() => setDismissed(true)} aria-label="Hide install hint">Not now</button>}
    </div>
  );
}

const ShareGlyph = () => (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-label="(the square with an arrow)" role="img" style={{ verticalAlign: "-2px" }}>
    <path d="M12 3v12M8 7l4-4 4 4" />
    <path d="M6 11H5v10h14V11h-1" />
  </svg>
);

/** "Download" / "Downloaded" on a book's details, opening the download sheet. */
export function DownloadButton({ work }: { work: Work }) {
  const { offlineBooks } = useStore();
  const [open, setOpen] = useState(false);
  const have = offlineBooks.find((b) => b.id === work.id);
  return (
    <>
      <button className="btn ghost" onClick={() => setOpen(true)} data-testid="download-offline">
        {have ? <Check /> : <Download />} {have ? (have.status === "changed" ? "Update download" : "Downloaded") : "Download"}
      </button>
      <DownloadSheet work={work} open={open} onClose={() => setOpen(false)} />
    </>
  );
}

export function DownloadSheet({ work, open, onClose }: { work: Work; open: boolean; onClose: () => void }) {
  const { offlineBooks, toast, online } = useStore();
  const have = offlineBooks.find((b) => b.id === work.id);
  const [fullQuality, setFullQuality] = useLocalStorage<boolean>("lectern.offline.fullQuality", false);
  const [dataSaver] = useLocalStorage<boolean>("lectern.dataSaver", false);
  const [m, setM] = useState<OfflineManifest | null>(null);
  const [choice, setChoice] = useState<Choice | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [store, setStore] = useState<StorageInfo | null>(null);

  useEffect(() => {
    if (!open) return;
    setErr(null);
    storageInfo().then(setStore);
    if (!online) return setErr("Connect to Lectern to download.");
    manifestFor(work.id).then(
      (x) => {
        setM(x);
        setChoice(have?.choice ?? defaultChoice(x, fullQuality && !dataSaver));
      },
      (e) => setErr((e as Error).message),
    );
  }, [open, work.id, online]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = useCallback(async () => {
    if (!choice) return;
    setErr(null);
    setProgress({ done: 0, total: 0 });
    try {
      await download(work.id, choice, setProgress);
      toast(`“${work.title}” is on this device.`);
      onClose();
    } catch (e) {
      setErr(e instanceof DownloadError ? e.message : (e as Error).message);
    } finally {
      setProgress(null);
      storageInfo().then(setStore);
    }
  }, [choice, work, toast, onClose]);

  const size = m && choice ? plannedBytes(m, choice) : 0;
  const free = store?.quota != null && store.usage != null ? store.quota - store.usage : null;
  const epub = m?.files.find((f) => f.kind === "epub");
  const pdf = m?.files.find((f) => f.kind === "pdf");
  const busy = progress != null || downloading(work.id);
  const set = (patch: Partial<Choice>) => choice && setChoice({ ...choice, ...patch });

  return (
    <Sheet open={open} onClose={onClose} title={have ? "Downloaded to this device" : "Download for offline"} labelledBy="dl-title"
      footer={
        <div className="row" style={{ justifyContent: "space-between", width: "100%" }}>
          {have ? <button className="btn ghost danger" disabled={busy} onClick={async () => { await remove(work.id); toast(`Removed “${work.title}” from this device.`); onClose(); }}>Remove download</button> : <span />}
          {busy ? (
            <button className="btn" onClick={() => cancel(work.id)}>Cancel</button>
          ) : (
            <button className="btn primary" disabled={!m || !choice || !size} onClick={start} data-testid="download-start">
              <Download /> {have ? "Download again" : "Download"} {size ? mb(size) : ""}
            </button>
          )}
        </div>
      }
    >
      <div className="stack dl-sheet">
        <p className="hint" style={{ margin: 0 }}>“{work.title}” stays on this device to read and listen without a connection. Positions you save offline sync when you are back.</p>
        {err && <div className="alert error" role="alert">{err}</div>}
        {!m && !err && <div className="hint" aria-busy="true">Checking what can be downloaded…</div>}
        {m && choice && (
          <ul className="dl-parts">
            {epub && (
              <li><label><input type="checkbox" checked={choice.epub} disabled={busy} onChange={(e) => set({ epub: e.target.checked, readalong: e.target.checked && choice.readalong })} /> Book (EPUB) <span className="hint">{mb(epub.size)}</span></label></li>
            )}
            {pdf && (
              <li><label><input type="checkbox" checked={choice.pdf} disabled={busy} onChange={(e) => set({ pdf: e.target.checked })} /> PDF <span className="hint">{mb(pdf.size)}</span></label></li>
            )}
            {m.audio && (
              <li>
                <label><input type="checkbox" checked={!!choice.audio} disabled={busy} onChange={(e) => set({ audio: e.target.checked ? (fullQuality || !lowWorthIt(m) ? "full" : "low") : null, readalong: e.target.checked && choice.readalong })} /> Audiobook <span className="hint">{mb(choice.audio === "full" || m.audio.low_bytes == null ? m.audio.full_bytes : m.audio.low_bytes)}</span></label>
                {choice.audio && (
                  <div className="type-row dl-quality">
                    <span id="dl-q">Full quality <span className="hint">{m.audio.low_bytes == null ? "(no data-saver copy yet)" : `${mb(m.audio.full_bytes)} instead of ${mb(m.audio.low_bytes)} data saver`}</span></span>
                    <button className="switch" role="switch" aria-labelledby="dl-q" aria-checked={choice.audio === "full"} disabled={busy || m.audio.low_bytes == null}
                      onClick={() => {
                        const full = choice.audio !== "full";
                        setFullQuality(full);
                        set({ audio: full ? "full" : "low" });
                      }}><span /></button>
                  </div>
                )}
              </li>
            )}
            {m.readalong && (
              <li><label><input type="checkbox" checked={choice.readalong} disabled={busy || !choice.epub || !choice.audio} onChange={(e) => set({ readalong: e.target.checked })} /> Read-along <span className="hint">text highlighted as it is read</span></label></li>
            )}
          </ul>
        )}
        {progress && (
          <div role="progressbar" aria-label="Download progress" aria-valuemin={0} aria-valuemax={progress.total || 1} aria-valuenow={progress.done}>
            <div className="progress-bar"><span style={{ width: `${progress.total ? Math.min(100, (progress.done / progress.total) * 100) : 0}%` }} /></div>
            <div className="hint" style={{ marginTop: 6 }}>{mb(progress.done)} of {mb(progress.total)}</div>
          </div>
        )}
        {free != null && <p className="hint" style={{ margin: 0 }}>About {mb(free)} free for Lectern on this device. Downloads count against your hourly download limit.</p>}
      </div>
    </Sheet>
  );
}

/** Sync now, for the Downloads page. */
export function SyncNow() {
  const { online } = useStore();
  const s = useSync();
  if (!s.pending) return null;
  return (
    <button className="btn sm" disabled={!online || s.syncing} onClick={() => flush()}>
      <Sync /> {s.syncing ? "Syncing…" : `Sync ${s.pending} ${s.pending === 1 ? "position" : "positions"}`}
    </button>
  );
}
