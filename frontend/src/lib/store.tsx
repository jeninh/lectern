import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { get, post, setCsrfToken } from "./api";
import { useLocalStorage, useVisiblePolling } from "./hooks";
import type { AppNotification, Me, Preferences, Work } from "./types";
import { ACCEPT, httpTransport, localStore, UploadManager, workerHasher, type UploadItem } from "./uploads";

type Theme = "light" | "dark" | "system";

interface Toast {
  id: number;
  text: string;
  kind?: "info" | "error";
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
  toast: (text: string, kind?: Toast["kind"]) => void;
  paletteOpen: boolean;
  setPaletteOpen: (v: boolean) => void;
  helpOpen: boolean;
  setHelpOpen: (v: boolean) => void;
  /** Wall clock that ticks every 20 s so relative times re-render on their own. */
  now: number;
  canUpload: boolean;
  /** Opens the file picker; chosen EPUBs and audiobooks go through resumable uploads. */
  pickBooks: () => void;
  addFiles: (files: File[]) => void;
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

  const toast = useCallback((text: string, kind: Toast["kind"] = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, kind }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3800);
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
  const uploadManager = useMemo(() => (canUpload ? new UploadManager(httpTransport(), workerHasher(), localStore()) : null), [canUpload]);
  useEffect(() => {
    if (!uploadManager) return;
    uploadManager.onDone = (item) => {
      toast(`${item.title || item.name} uploaded. Filing it now…`);
      // The inbox watcher picks files up within a few seconds; refresh until they land.
      for (const delay of [3000, 6000, 10000, 16000, 25000]) window.setTimeout(() => refreshLibrary().catch(() => {}), delay);
    };
    return uploadManager.subscribe(setUploads);
  }, [uploadManager, toast, refreshLibrary]);

  const addFiles = useCallback(
    (files: File[]) => {
      if (!uploadManager || !files.length) return;
      const rejected = uploadManager.add(files);
      if (rejected.length) toast(`Not a book or audiobook: ${rejected.join(", ")}`, "error");
    },
    [uploadManager, toast],
  );
  const pickBooks = useCallback(() => fileInput.current?.click(), []);

  // Drop files anywhere in the app to upload them.
  useEffect(() => {
    if (!canUpload) return;
    const over = (e: DragEvent) => {
      if (e.dataTransfer?.types.includes("Files")) e.preventDefault();
    };
    const drop = (e: DragEvent) => {
      if (!e.dataTransfer?.files.length) return;
      e.preventDefault();
      addFiles(Array.from(e.dataTransfer.files));
    };
    window.addEventListener("dragover", over);
    window.addEventListener("drop", drop);
    return () => {
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
      paletteOpen,
      setPaletteOpen,
      helpOpen,
      setHelpOpen,
      now,
      canUpload,
      pickBooks,
      addFiles,
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
    [me, works, worksLoaded, refreshMe, refreshLibrary, theme, setThemeRaw, resolvedTheme, toasts, toast, paletteOpen, helpOpen, now, canUpload, pickBooks, addFiles, uploads, uploadManager, setPreference, notifications, unread, refreshNotifications, markAllRead, deviceName, setDeviceName],
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
    </Ctx.Provider>
  );
}

export function useStore(): Store {
  const s = useContext(Ctx);
  if (!s) throw new Error("StoreProvider missing");
  return s;
}
