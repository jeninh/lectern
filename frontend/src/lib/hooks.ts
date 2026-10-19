import { useCallback, useEffect, useRef, useState } from "react";

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setMatches(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return matches;
}

export function useReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

export function useInterval(fn: () => void, ms: number | null) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (ms == null) return;
    const id = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(id);
  }, [ms]);
}

/** Polls while the tab is visible; pauses when hidden. */
export function useVisiblePolling(fn: () => void, ms: number) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    let id: number | null = null;
    const start = () => {
      if (id == null) id = window.setInterval(() => ref.current(), ms);
    };
    const stop = () => {
      if (id != null) window.clearInterval(id);
      id = null;
    };
    const onVis = () => (document.hidden ? stop() : (ref.current(), start()));
    document.addEventListener("visibilitychange", onVis);
    if (!document.hidden) start();
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [ms]);
}

function isEditable(el: Element | null): boolean {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (el as HTMLElement).isContentEditable;
}

export interface Hotkey {
  /** e.g. "mod+k", "/", "?", "g l", "Escape" */
  keys: string;
  handler: (e: KeyboardEvent) => void;
  /** fire even when focus is in an input */
  inInputs?: boolean;
}

/** Global hotkeys with two-key sequences ("g l"). */
export function useHotkeys(hotkeys: Hotkey[]) {
  const pending = useRef<string | null>(null);
  const timer = useRef<number | null>(null);
  const ref = useRef(hotkeys);
  ref.current = hotkeys;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
      const editing = isEditable(document.activeElement);
      for (const hk of ref.current) {
        if (editing && !hk.inInputs) continue;
        const parts = hk.keys.split(" ");
        if (parts.length === 2) {
          if (mod || e.altKey) continue;
          if (pending.current === parts[0] && key === parts[1]) {
            pending.current = null;
            e.preventDefault();
            hk.handler(e);
            return;
          }
        } else {
          const [wantMod, wantKey] = hk.keys.startsWith("mod+") ? [true, hk.keys.slice(4)] : [false, hk.keys];
          if (wantMod === mod && key === wantKey && !e.altKey) {
            e.preventDefault();
            hk.handler(e);
            return;
          }
        }
      }
      if (!editing && !mod && key.length === 1) {
        pending.current = key;
        if (timer.current) window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => (pending.current = null), 900);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Traps Tab inside the element and restores focus on unmount. */
export function useFocusTrap(active: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!active || !ref.current) return;
    const root = ref.current;
    const prev = document.activeElement as HTMLElement | null;
    const first = root.querySelector<HTMLElement>("[data-autofocus]") ?? root.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? root).focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.offsetParent !== null);
      if (!items.length) return;
      const a = items[0];
      const z = items[items.length - 1];
      if (e.shiftKey && document.activeElement === a) {
        e.preventDefault();
        z.focus();
      } else if (!e.shiftKey && document.activeElement === z) {
        e.preventDefault();
        a.focus();
      }
    };
    root.addEventListener("keydown", onKey);
    return () => {
      root.removeEventListener("keydown", onKey);
      prev?.focus?.();
    };
  }, [active]);
  return ref;
}

export function useLocalStorage<T>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (v: T) => {
      setValue(v);
      try {
        localStorage.setItem(key, JSON.stringify(v));
      } catch {
        /* ignore quota */
      }
    },
    [key],
  );
  return [value, set];
}
