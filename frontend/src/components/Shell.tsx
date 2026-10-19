import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { logout } from "../lib/api";
import { useHotkeys } from "../lib/hooks";
import { useStore } from "../lib/store";
import { initials } from "../lib/format";
import { CommandPalette } from "./CommandPalette";
import { Devices, Help, Home, Library, Logout, Moon, Pulse, Search, Shield, Sun, Users } from "./Icons";
import { Sheet } from "./Sheet";

const NAV = [
  { to: "/app", label: "Home", icon: <Home />, key: "g h", end: true },
  { to: "/app/library", label: "Library", icon: <Library />, key: "g l" },
  { to: "/app/devices", label: "Devices", icon: <Devices />, key: "g d" },
  { to: "/app/security", label: "Security", icon: <Shield />, key: "g s" },
  { to: "/app/activity", label: "Activity", icon: <Pulse />, key: "g a" },
];

const TitleSlot = createContext<HTMLElement | null>(null);

export function Shell({ children }: { children: ReactNode }) {
  const { me, resolvedTheme, setTheme, toasts, setPaletteOpen, helpOpen, setHelpOpen } = useStore();
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  const nav = useNavigate();
  const loc = useLocation();
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform);

  useEffect(() => {
    // Move focus to the main region on route change so screen readers announce the new page.
    document.getElementById("main")?.focus({ preventScroll: true });
  }, [loc.pathname]);

  const toggleTheme = () => setTheme(resolvedTheme === "dark" ? "light" : "dark");

  useHotkeys([
    { keys: "mod+k", handler: () => setPaletteOpen(true), inInputs: true },
    { keys: "/", handler: () => setPaletteOpen(true) },
    { keys: "?", handler: () => setHelpOpen(true) },
    { keys: "t", handler: toggleTheme },
    { keys: "n", handler: () => nav("/app/devices/new") },
    { keys: "g h", handler: () => nav("/app") },
    { keys: "g l", handler: () => nav("/app/library") },
    { keys: "g d", handler: () => nav("/app/devices") },
    { keys: "g s", handler: () => nav("/app/security") },
    { keys: "g a", handler: () => nav("/app/activity") },
    { keys: "g u", handler: () => me?.user.role === "owner" && nav("/app/users") },
  ]);

  const items = me?.user.role === "owner" ? [...NAV, { to: "/app/users", label: "Users", icon: <Users />, key: "g u" }] : NAV;

  return (
    <div className="shell">
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className="sidebar" aria-label="Primary">
        <NavLink to="/app" className="brand">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M5 5h14v2H5zm0 5h14v2H5zm0 5h9v2H5z" /></svg>
          </span>
          Lectern
        </NavLink>
        <div className="nav-section">Read</div>
        {items.slice(0, 2).map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end} className="nav-item">
            {n.icon}
            {n.label}
            <kbd>{n.key}</kbd>
          </NavLink>
        ))}
        <div className="nav-section">Manage</div>
        {items.slice(2).map((n) => (
          <NavLink key={n.to} to={n.to} className="nav-item">
            {n.icon}
            {n.label}
            <kbd>{n.key}</kbd>
          </NavLink>
        ))}
        <div className="sidebar-footer">
          <button className="nav-item" onClick={() => setHelpOpen(true)}>
            <Help />
            Shortcuts
            <kbd>?</kbd>
          </button>
          <button className="nav-item" onClick={toggleTheme} aria-pressed={resolvedTheme === "dark"}>
            {resolvedTheme === "dark" ? <Sun /> : <Moon />}
            {resolvedTheme === "dark" ? "Light theme" : "Dark theme"}
            <kbd>t</kbd>
          </button>
          {me && (
            <div className="user-chip">
              <span className="avatar" aria-hidden="true">{initials(me.user.display_name || me.user.username)}</span>
              <div className="meta">
                <strong>{me.user.display_name || me.user.username}</strong>
                <span>{me.user.role}</span>
              </div>
              <button className="icon-btn" style={{ marginLeft: "auto", width: 32, height: 32 }} onClick={logout} aria-label="Sign out" title="Sign out">
                <Logout />
              </button>
            </div>
          )}
        </div>
      </aside>

      <main className="main" id="main" tabIndex={-1}>
        <div className="topbar" id="topbar">
          <div id="page-title" ref={setSlot} style={{ display: "contents" }} />
          <div className="spacer" />
          <button className="search-btn" onClick={() => setPaletteOpen(true)} aria-label="Search and commands">
            <Search />
            <span>Search or jump to…</span>
            <kbd>{isMac ? "⌘" : "Ctrl"} K</kbd>
          </button>
        </div>
        <TitleSlot.Provider value={slot}>{children}</TitleSlot.Provider>
      </main>

      <nav className="bottom-nav" aria-label="Primary">
        {items.slice(0, 5).map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}>
            {n.icon}
            {n.label}
          </NavLink>
        ))}
      </nav>

      <CommandPalette />
      <Sheet open={helpOpen} onClose={() => setHelpOpen(false)} title="Keyboard shortcuts" variant="dialog" labelledBy="help-title">
        <div className="shortcut-list">
          <span><kbd>{isMac ? "⌘" : "Ctrl"}</kbd><kbd>K</kbd> or <kbd>/</kbd></span><span>Search books, pages, and actions</span>
          <span><kbd>g</kbd><kbd>h</kbd></span><span>Home</span>
          <span><kbd>g</kbd><kbd>l</kbd></span><span>Library</span>
          <span><kbd>g</kbd><kbd>d</kbd></span><span>Devices</span>
          <span><kbd>g</kbd><kbd>s</kbd></span><span>Security</span>
          <span><kbd>g</kbd><kbd>a</kbd></span><span>Activity</span>
          <span><kbd>n</kbd></span><span>Add a device</span>
          <span><kbd>t</kbd></span><span>Toggle theme</span>
          <span><kbd>f</kbd></span><span>Focus the library filter</span>
          <span><kbd>Esc</kbd></span><span>Close sheet or palette</span>
          <span><kbd>?</kbd></span><span>This help</span>
        </div>
      </Sheet>
      <div className="toasts" aria-live="polite" aria-atomic="false">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind ?? ""}`}>{t.text}</div>
        ))}
      </div>
    </div>
  );
}

/** Renders the page title (and optional actions) into the top bar. */
export function PageTitle({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  const slot = useContext(TitleSlot);
  useEffect(() => {
    const text = typeof children === "string" ? children : "";
    document.title = text ? `${text} · Lectern` : "Lectern";
  }, [children]);
  if (!slot) return null;
  return createPortal(
    <>
      <h1>{children}</h1>
      {aside && <div className="row" style={{ marginLeft: 12 }}>{aside}</div>}
    </>,
    slot,
  );
}
