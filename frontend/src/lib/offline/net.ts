// Whether Lectern's server can be reached. `navigator.onLine` is not enough: a phone on mobile
// data with Tailscale switched off is "online" but cannot reach a home server. So a failed
// request also counts as offline, and while offline the app checks /healthz now and then and on
// every `online`, focus, and visibility event. Listeners hear about every change.

type Listener = (online: boolean) => void;

let online = typeof navigator === "undefined" ? true : navigator.onLine;
const listeners = new Set<Listener>();
let timer: number | null = null;
let probing: Promise<boolean> | null = null;

function set(next: boolean) {
  if (next === online) return;
  online = next;
  for (const l of listeners) l(online);
  if (!online) schedule();
}

function schedule() {
  if (timer != null || typeof window === "undefined") return;
  timer = window.setInterval(() => {
    if (online) {
      window.clearInterval(timer!);
      timer = null;
    } else void probe();
  }, 20000);
}

export function isOnline(): boolean {
  return online;
}

export function subscribe(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** A request failed with a network error (not an HTTP error). */
export function reportUnreachable() {
  set(false);
}

export function reportReachable() {
  set(true);
}

/** Check the server now; resolves to whether it answered. */
export function probe(): Promise<boolean> {
  if (probing) return probing;
  probing = (async () => {
    if (!navigator.onLine) {
      set(false);
      return false;
    }
    try {
      const r = await fetch("/healthz", { cache: "no-store", credentials: "omit" });
      set(r.ok);
      return r.ok;
    } catch {
      set(false);
      return false;
    } finally {
      probing = null;
    }
  })();
  return probing;
}

/** True for the error `fetch` throws when the network is down (as opposed to an HTTP error). */
export function isNetworkError(e: unknown): boolean {
  return e instanceof TypeError || (e instanceof DOMException && e.name === "NetworkError");
}

if (typeof window !== "undefined") {
  window.addEventListener("online", () => void probe());
  window.addEventListener("offline", () => set(false));
  window.addEventListener("focus", () => !online && void probe());
  document.addEventListener("visibilitychange", () => !document.hidden && !online && void probe());
}
