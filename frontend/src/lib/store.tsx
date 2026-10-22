import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { get, OfflineError, post, setCsrfToken, setUnauthorizedHandler } from "./api";
import { isOnline, probe, subscribe as onNetwork } from "./offline/net";
import { bindOwner, checkFresh, clearDevice, listBooks, onChange as onOfflineChange, ownerSnapshot, revalidate, type OfflineBook } from "./offline/store";
import { finishSignOut, pendingSignOut } from "./offline/signout";
import { flush, refreshSyncState, registerWorker } from "./offline/sync";
import { useLocalStorage, useVisiblePolling } from "./hooks";
import type { AppNotification, ImportInfo, Me, Preferences, Work } from "./types";
import { ACCEPT, filesFromDataTransfer, groupFiles, httpTransport, localStore, UploadManager, workerHasher, type PathedFile, type UploadItem } from "./uploads";

type Theme = "light" | "dark" | "system";

export interface ToastAction {
  label: string;
  run: () => void;
}

interface Toast {
  id: number;
  text: string;
  kind?: "info" | "error";
  action?: ToastAction;
}

interface Store {
  me: Me | null;
  works: Work[];
  worksLoaded: boolean;
  refreshMe: () => Promise<void>;
  refreshLibrary: () => Promise<void>;
  theme: Theme;
  setTheme: (t: Theme) => void;
  resolvedTheme: "light" | "dark";
  toasts: Toast[];
  toast: (text: string, kind?: Toast["kind"], action?: ToastAction) => void;
  dismissToast: (id: number) => void;
  paletteOpen: boolean;
  setPaletteOpen: (v: boolean) => void;
  helpOpen: boolean;
  setHelpOpen: (v: boolean) => void;
  /** Wall clock that ticks every 20 s so relative times re-render on their own. */
  now: number;
  canUpload: boolean;
  /** Owners and members add, edit, and trash books. */
  canEdit: boolean;
  /** Opens the file picker; chosen EPUBs and audiobooks go through resumable uploads. */
  pickBooks: () => void;
  /** Opens a folder picker: one folder of tracks is one audiobook. */
  pickFolder: () => void;
  addFiles: (files: PathedFile[]) => void;
  /** Uploaded books waiting for review before they are filed. */
  imports: ImportInfo[];
  refreshImports: () => Promise<void>;
  reviewOpen: boolean;
  setReviewOpen: (v: boolean) => void;
  /** A file is being dragged over the window. */
  dragging: boolean;
  /** The work open in the editor sheet (rendered by the shell, above every page). */
  editWorkId: number | null;
  openEditor: (id: number | null) => void;
  uploads: UploadItem[];
  uploadManager: UploadManager | null;
  /** Saved per user on the server. */
  setPreference: <K extends keyof Preferences>(key: K, value: Preferences[K]) => Promise<void>;
  notifications: AppNotification[];
  unread: number;
  refreshNotifications: () => Promise<void>;
  markAllRead: () => Promise<void>;
  /** Lectern's server can be reached (lib/offline/net.ts). */
  online: boolean;
  /** Books downloaded to this device. */
  offlineBooks: OfflineBook[];
  /** How this browser names itself in positions it saves ("Firefox on Mac"). */
  deviceName: string;
  setDeviceName: (v: string) => void;
}

const Ctx = createContext<Store | null>(null);

function systemTheme(): "light" | "dark" {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function browserLabel(ua = navigator.userAgent): string {
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Mac OS/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "";
  const br = /Firefox|FxiOS/.test(ua) ? "Firefox" : /Edg\//.test(ua) ? "Edge" : /Chrome|CriOS/.test(ua) ? "Chrome" : /Safari/.test(ua) ? "Safari" : "Browser";
  return os ? `${br} on ${os}` : br;
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [works, setWorks] = useState<Work[]>([]);
  const [worksLoaded, setWorksLoaded] = useState(false);
  const [theme, setThemeRaw] = useLocalStorage<Theme>("lectern.theme", "system");
  const [sys, setSys] = useState(systemTheme);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [now, setNow] = useState(Date.now);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [notifications, setNotifications] = useState<AppNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const [deviceName, setDeviceName] = useLocalStorage<string>("lectern.deviceName", browserLabel());
  const fileInput = useRef<HTMLInputElement | null>(null);
  const folderInput = useRef<HTMLInputElement | null>(null);
  const [imports, setImports] = useState<ImportInfo[]>([]);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [editWorkId, openEditor] = useState<number | null>(null);
  const [online, setOnline] = useState(isOnline);
  const [offlineBooks, setOfflineBooks] = useState<OfflineBook[]>([]);
  // A sign-out that has not reached the server yet (lib/offline/signout.ts): the app shows
  // only that, and never uses the session, until /logout gets through.
  const [signingOut] = useState(() => !!pendingSignOut());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 20000);
    return () => window.clearInterval(id);
  }, []);

  // What is kept offline expires (lib/offline/store.ts checkFresh): forget the identity too.
  const expire = useCallback(() => {
    setMe(null);
    setOfflineBooks([]);
    setWorks([]);
  }, []);

  const refreshMe = useCallback(async () => {
    if (pendingSignOut()) return;
    try {
      const data = await get<Me>("/api/me");
      setCsrfToken(data.csrf_token);
      setMe(data);
      setUnread(data.counts.unread_notifications);
      // Downloads belong to one person: someone else signing in here never sees them.
      if (await bindOwner(data)) setOfflineBooks([]);
    } catch (e) {
      if (!(e instanceof OfflineError)) throw e;
      // Offline: who was signed in here, so the app can open downloaded books. No CSRF token,
      // so nothing is written to the server until it is reachable again.
      const snap = await ownerSnapshot();
      if (snap) setMe((m) => m ?? (snap.me as Me));
      else expire();
    }
  }, [expire]);

  const refreshLibrary = useCallback(async () => {
    try {
      const data = await get<{ works: Work[] }>("/api/library");
      setWorks(data.works);
    } catch (e) {
      if (!(e instanceof OfflineError)) throw e;
      setWorks((await listBooks()).map((b) => b.work));
    }
    setWorksLoaded(true);
  }, []);

  // Offline support: the service worker, downloads, and the position queue.
  useEffect(() => {
    registerWorker();
    // A session that ended (signed out elsewhere, revoked, expired) takes the downloads with it.
    setUnauthorizedHandler(() => clearDevice());
    const load = () => listBooks().then(setOfflineBooks);
    load();
    refreshSyncState();
    const offChange = onOfflineChange(load);
    const offNet = onNetwork(setOnline);
    return () => {
      offChange();
      offNet();
    };
  }, []);

  const refreshNotifications = useCallback(async () => {
    const d = await get<{ items: AppNotification[]; unread: number }>("/api/notifications?limit=40");
    setNotifications(d.items);
    setUnread(d.unread);
  }, []);

  const markAllRead = useCallback(async () => {
    await post("/api/notifications/read", { all: true });
    setUnread(0);
    setNotifications((n) => n.map((x) => (x.read_at ? x : { ...x, read_at: new Date().toISOString() })));
  }, []);

  useEffect(() => {
    if (signingOut) return;
    refreshMe().then(() => refreshNotifications()).catch(() => {});
    refreshLibrary().catch(() => {});
  }, [signingOut, refreshMe, refreshLibrary, refreshNotifications]);

  // Finish a pending sign-out on start and whenever the server may be reachable again.
  useEffect(() => {
    if (!signingOut) return;
    // One attempt at a time: reconnect, the 20 s timer, and the start can fire together.
    let busy = false;
    let done = false;
    const attempt = async () => {
      if (busy || done) return;
      busy = true;
      try {
        done = await finishSignOut();
      } finally {
        busy = false;
      }
      if (!done) return;
      await clearDevice().catch(() => {});
      // Stay on "Signed out" until the page is gone: rendering the app again would call the
      // API, meet the 401, and navigate a second time.
      window.location.assign("/login");
    };
    attempt();
    const off = onNetwork((up) => up && attempt());
    const id = window.setInterval(() => (isOnline() ? attempt() : probe()), 20000);
    return () => {
      off();
      window.clearInterval(id);
    };
  }, [signingOut]);

  // Offline for longer than the server allows: delete the downloads and the identity, even with
  // the app left open.
  useEffect(() => {
    const id = window.setInterval(async () => {
      if (!isOnline() && !(await checkFresh())) expire();
    }, 60000);
    return () => window.clearInterval(id);
  }, [expire]);
  useVisiblePolling(() => me && online && refreshNotifications().catch(() => {}), 60000);


  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const on = () => setSys(systemTheme());
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  const resolvedTheme = theme === "system" ? sys : theme;
  useEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme;
  }, [resolvedTheme]);

  const dismissToast = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const toast = useCallback((text: string, kind: Toast["kind"] = "info", action?: ToastAction) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, kind, action }]);
    // Toasts with an action (Undo, Review) stay long enough to reach with a keyboard.
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), action ? 10000 : 3800);
  }, []);

  // Back online, or back in the foreground: refresh the session, send queued positions, and
  // check every downloaded book is still current and still shared with this person.
  const reconnect = useCallback(async () => {
    if (!isOnline()) return;
    await refreshMe().catch(() => {});
    await flush().catch(() => {});
    try {
      const { removed } = await revalidate();
      if (removed.length) toast(`Removed from this device, no longer available to you: ${removed.join(", ")}.`, "info");
    } catch {
      /* try again next time */
    }
  }, [refreshMe, toast]);
  const wasOnline = useRef(online);
  useEffect(() => {
    if (online && !wasOnline.current && !signingOut) {
      reconnect();
      refreshLibrary().catch(() => {});
    }
    wasOnline.current = online;
  }, [online, reconnect, refreshLibrary, signingOut]);
  useEffect(() => {
    if (!me) return;
    reconnect();
    const onFocus = () => (isOnline() ? flush().catch(() => {}) : probe());
    const onVisible = () => !document.hidden && onFocus();
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [!!me]); // eslint-disable-line react-hooks/exhaustive-deps

  const setPreference = useCallback(async <K extends keyof Preferences>(key: K, value: Preferences[K]) => {
    setMe((m) => (m ? { ...m, preferences: { ...m.preferences, [key]: value } } : m));
    try {
      const r = await post<{ preferences: Preferences }>("/api/me/preferences", { [key]: value });
      setMe((m) => (m ? { ...m, preferences: r.preferences } : m));
    } catch (e) {
      toast((e as Error).message, "error");
      await refreshMe().catch(() => {});
    }
  }, [refreshMe, toast]);

  const canUpload = me?.user.role === "owner" || me?.user.role === "member";
  const canEdit = canUpload && me?.can_edit !== false;
  const uploadManager = useMemo(() => (canUpload ? new UploadManager(httpTransport(), workerHasher(), localStore()) : null), [canUpload]);

  const refreshImports = useCallback(async () => {
    const d = await get<{ imports: ImportInfo[] }>("/api/imports");
    setImports(d.imports);
  }, []);
  useEffect(() => {
    if (canUpload) refreshImports().catch(() => {});
  }, [canUpload, refreshImports]);

  useEffect(() => {
    if (!uploadManager) return;
    if (me?.upload.max_bytes) uploadManager.maxBytes = me.upload.max_bytes;
    uploadManager.onImportReady = (info, group) => {
      refreshImports().catch(() => {});
      window.setTimeout(() => uploadManager.clearDone(group), 2500);
      if (info.status === "failed") {
        const dup = info.files.find((f) => f.duplicate_of)?.duplicate_of;
        toast(
          info.error === "all_duplicates"
            ? `Already in your library${dup ? ` as “${dup.title}”${dup.in_trash ? " (in the trash)" : ""}` : ""}. Nothing new to add.`
            : `${info.label}: Lectern could not read these files.`,
          "error",
          { label: "Details", run: () => setReviewOpen(true) },
        );
      } else if (info.status === "filed") {
        toast(`Filed “${info.work?.title ?? info.label}” automatically (a certain match).`);
        refreshLibrary().catch(() => {});
      } else {
        toast(`“${info.detected?.title || info.label}” is ready to review.`, "info", { label: "Review", run: () => setReviewOpen(true) });
      }
    };
    uploadManager.onImportError = (label, err) => toast(`${label}: ${err.message}`, "error");
    return uploadManager.subscribe(setUploads);
  }, [uploadManager, toast, refreshLibrary, refreshImports, me?.upload.max_bytes]);

  const addFiles = useCallback(
    (files: PathedFile[]) => {
      if (!uploadManager || !files.length) return;
      const rejected: string[] = [];
      for (const book of groupFiles(files)) rejected.push(...uploadManager.add(book.files, { label: book.label }));
      // Folders carry cover images and cue sheets; only a lone wrong file deserves a warning.
      const shown = files.some((f) => f.relPath || f.webkitRelativePath) ? rejected.filter((n) => !/\.(jpe?g|png|webp|gif|txt|nfo|cue|m3u8?|sfv|db|ini|ds_store|pdf|json|xml|opf)$/i.test(n) && !n.startsWith(".")) : rejected;
      if (shown.length) toast(`Wrong format, skipped: ${shown.slice(0, 4).join(", ")}${shown.length > 4 ? ` and ${shown.length - 4} more` : ""}. Lectern takes EPUB, PDF, CBZ, M4B, MP3, OGG/Opus, and FLAC.`, "error");
    },
    [uploadManager, toast],
  );
  const pickBooks = useCallback(() => fileInput.current?.click(), []);
  const pickFolder = useCallback(() => folderInput.current?.click(), []);

  // Drop files or whole folders anywhere in the app to add them.
  useEffect(() => {
    if (!canUpload) return;
    let depth = 0;
    const hasFiles = (e: DragEvent) => !!e.dataTransfer?.types.includes("Files");
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth++;
      setDragging(true);
    };
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) setDragging(false);
    };
    const over = (e: DragEvent) => {
      if (hasFiles(e)) e.preventDefault();
    };
    const drop = (e: DragEvent) => {
      depth = 0;
      setDragging(false);
      if (!e.dataTransfer?.files.length && !e.dataTransfer?.items.length) return;
      e.preventDefault();
      filesFromDataTransfer(e.dataTransfer).then(addFiles, () => addFiles(Array.from(e.dataTransfer?.files ?? [])));
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragleave", leave);
    window.addEventListener("dragover", over);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("dragover", over);
      window.removeEventListener("drop", drop);
    };
  }, [canUpload, addFiles]);

  const value = useMemo<Store>(
    () => ({
      me,
      works,
      worksLoaded,
      refreshMe,
      refreshLibrary,
      theme,
      setTheme: setThemeRaw,
      resolvedTheme,
      toasts,
      toast,
      dismissToast,
      paletteOpen,
      setPaletteOpen,
      helpOpen,
      setHelpOpen,
      now,
      canUpload,
      canEdit,
      pickBooks,
      pickFolder,
      addFiles,
      imports,
      refreshImports,
      reviewOpen,
      setReviewOpen,
      dragging,
      editWorkId,
      openEditor,
      uploads,
      uploadManager,
      setPreference,
      notifications,
      unread,
      refreshNotifications,
      markAllRead,
      online,
      offlineBooks,
      deviceName,
      setDeviceName,
    }),
    [online, offlineBooks, me, works, worksLoaded, refreshMe, refreshLibrary, theme, setThemeRaw, resolvedTheme, toasts, toast, dismissToast, paletteOpen, helpOpen, now, canUpload, canEdit, pickBooks, pickFolder, addFiles, imports, refreshImports, reviewOpen, dragging, editWorkId, uploads, uploadManager, setPreference, notifications, unread, refreshNotifications, markAllRead, deviceName, setDeviceName],
  );
  return (
    <Ctx.Provider value={value}>
      {signingOut ? (
        <main className="signing-out" data-testid="signing-out">
          <h1>Signed out</h1>
          <p>This device's downloads and positions are deleted. Lectern finishes signing out on the server as soon as it can reach it; until then nobody can use this account here.</p>
        </main>
      ) : (
        children
      )}
      {canUpload && (
        <input
          ref={fileInput}
          type="file"
          accept={ACCEPT}
          multiple
          hidden
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e) => {
            const list = Array.from(e.target.files ?? []);
            if (fileInput.current) fileInput.current.value = "";
            addFiles(list);
          }}
        />
      )}
      {canUpload && (
        <input
          ref={folderInput}
          type="file"
          multiple
          hidden
          aria-hidden="true"
          tabIndex={-1}
          {...{ webkitdirectory: "", directory: "" }}
          onChange={(e) => {
            const list = Array.from(e.target.files ?? []);
            if (folderInput.current) folderInput.current.value = "";
            addFiles(list);
          }}
        />
      )}
    </Ctx.Provider>
  );
}

export function useStore(): Store {
  const s = useContext(Ctx);
  if (!s) throw new Error("StoreProvider missing");
  return s;
}
