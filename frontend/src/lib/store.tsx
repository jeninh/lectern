import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { get, setCsrfToken } from "./api";
import { useLocalStorage } from "./hooks";
import type { Me, Work } from "./types";

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
    }),
    [me, works, worksLoaded, refreshMe, refreshLibrary, theme, setThemeRaw, resolvedTheme, toasts, toast, paletteOpen, helpOpen],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore(): Store {
  const s = useContext(Ctx);
  if (!s) throw new Error("StoreProvider missing");
  return s;
}
