import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { get, post, setCsrfToken } from "./api";
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

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 20000);
    return () => window.clearInterval(id);
  }, []);

  const refreshMe = useCallback(async () => {
    const data = await get<Me>("/api/me");
    setCsrfToken(data.csrf_token);
    setMe(data);
    setUnread(data.counts.unread_notifications);
  }, []);

  const refreshLibrary = useCallback(async () => {
    const data = await get<{ works: Work[] }>("/api/library");
    setWorks(data.works);
    setWorksLoaded(true);
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
    refreshMe().then(() => refreshNotifications()).catch(() => {});
    refreshLibrary().catch(() => {});
  }, [refreshMe, refreshLibrary, refreshNotifications]);
  useVisiblePolling(() => me && refreshNotifications().catch(() => {}), 60000);

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
      if (shown.length) toast(`Wrong format, skipped: ${shown.slice(0, 4).join(", ")}${shown.length > 4 ? ` and ${shown.length - 4} more` : ""}. Lectern takes EPUB, M4B, MP3, OGG/Opus, and FLAC.`, "error");
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
      deviceName,
      setDeviceName,
    }),
    [me, works, worksLoaded, refreshMe, refreshLibrary, theme, setThemeRaw, resolvedTheme, toasts, toast, dismissToast, paletteOpen, helpOpen, now, canUpload, canEdit, pickBooks, pickFolder, addFiles, imports, refreshImports, reviewOpen, dragging, editWorkId, uploads, uploadManager, setPreference, notifications, unread, refreshNotifications, markAllRead, deviceName, setDeviceName],
  );
  return (
    <Ctx.Provider value={value}>
      {children}
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
