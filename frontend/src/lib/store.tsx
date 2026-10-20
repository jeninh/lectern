import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { get, post, setCsrfToken, uploadBook } from "./api";
import { useLocalStorage } from "./hooks";
import type { Me, Preferences, Work } from "./types";

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
  /** Opens the file picker; chosen EPUBs are streamed to the inbox. */
  pickBooks: () => void;
  /** Saved per user on the server. */
  setPreference: <K extends keyof Preferences>(key: K, value: Preferences[K]) => Promise<void>;
}

const Ctx = createContext<Store | null>(null);

function systemTheme(): "light" | "dark" {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
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
  const fileInput = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 20000);
    return () => window.clearInterval(id);
  }, []);

  const refreshMe = useCallback(async () => {
    const data = await get<Me>("/api/me");
    setCsrfToken(data.csrf_token);
    setMe(data);
  }, []);

  const refreshLibrary = useCallback(async () => {
    const data = await get<{ works: Work[] }>("/api/library");
    setWorks(data.works);
    setWorksLoaded(true);
  }, []);

  useEffect(() => {
    refreshMe().catch(() => {});
    refreshLibrary().catch(() => {});
  }, [refreshMe, refreshLibrary]);

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
  const pickBooks = useCallback(() => fileInput.current?.click(), []);
  const onFiles = async (files: FileList | null) => {
    const list = Array.from(files ?? []);
    if (fileInput.current) fileInput.current.value = "";
    if (!list.length) return;
    let ok = 0;
    for (const f of list) {
      try {
        await uploadBook(f);
        ok++;
      } catch (e) {
        toast(`${f.name}: ${(e as Error).message}`, "error");
      }
    }
    if (!ok) return;
    toast(ok === 1 ? `Uploaded ${list[0].name}. Filing it now…` : `Uploaded ${ok} books. Filing them now…`);
    // The inbox watcher picks files up within a few seconds; refresh until they land.
    for (const delay of [2500, 5000, 9000, 15000]) window.setTimeout(() => refreshLibrary().catch(() => {}), delay);
  };

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
      setPreference,
    }),
    [me, works, worksLoaded, refreshMe, refreshLibrary, theme, setThemeRaw, resolvedTheme, toasts, toast, paletteOpen, helpOpen, now, canUpload, pickBooks, setPreference],
  );
  return (
    <Ctx.Provider value={value}>
      {children}
      {canUpload && (
        <input
          ref={fileInput}
          type="file"
          accept=".epub,application/epub+zip"
          multiple
          hidden
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e) => onFiles(e.target.files)}
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
