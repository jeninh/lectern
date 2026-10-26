import { createContext, Fragment, useContext, useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { logout } from "../lib/api";
import { initials } from "../lib/format";
import { MOD, useHotkeys, useLocalStorage } from "../lib/hooks";
import { useStore } from "../lib/store";
import { usePlayer } from "../lib/player";
import { CommandPalette } from "./CommandPalette";
import { NotificationBell, UploadTray } from "./Notifications";
import { MiniPlayer } from "./Player";
import { Devices, Download, Gear, Headphones, Help, Highlighter, Home, Library, Logout, Moon, Pulse, Search, Shield, Sidebar, Sun, Trash, Upload } from "./Icons";
import { clearDevice } from "../lib/offline/store";
import { ConflictPrompt, OfflineBanner, OfflineStatus, UpdateReady } from "./Offline";
import { Sheet } from "./Sheet";
import { ShelfTree } from "./Folders";
import { ReviewSheet } from "./ReviewSheet";
import { WorkEditor } from "./WorkEditor";

interface NavEntry {
  to: string;
  label: string;
  icon: JSX.Element;
  keys?: string;
  end?: boolean;
  soon?: boolean;
}

const READ: NavEntry[] = [
  { to: "/app", label: "Home", icon: <Home />, keys: "g h", end: true },
  { to: "/app/library", label: "Library", icon: <Library />, keys: "g l" },
  { to: "/app/listening", label: "Listening", icon: <Headphones />, keys: "g p" },
  { to: "/app/downloads", label: "Downloads", icon: <Download />, keys: "g o" },
  { to: "/app/highlights", label: "Highlights", icon: <Highlighter />, soon: true },
];
const MANAGE: NavEntry[] = [
  { to: "/app/devices", label: "Devices", icon: <Devices />, keys: "g d" },
  { to: "/app/security", label: "Security", icon: <Shield />, keys: "g s" },
  { to: "/app/activity", label: "Activity", icon: <Pulse />, keys: "g a" },
];
const ADMIN: NavEntry = { to: "/app/admin", label: "Admin", icon: <Gear />, keys: "g u" };
const TRASH: NavEntry = { to: "/app/trash", label: "Trash", icon: <Trash /> };


const TitleSlot = createContext<HTMLElement | null>(null);

function NavItem({ n, collapsed }: { n: NavEntry; collapsed: boolean }) {
  return (
    <NavLink to={n.to} end={n.end} className="nav-item" title={collapsed ? n.label : undefined}>
      {n.icon}
      <span className="label">{n.label}</span>
      {n.soon ? <span className="soon">Soon</span> : n.keys && <kbd aria-hidden="true">{n.keys.toUpperCase()}</kbd>}
    </NavLink>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const { me, resolvedTheme, setTheme, toasts, dismissToast, setPaletteOpen, helpOpen, setHelpOpen, canEdit, canUpload, pickBooks, dragging } = useStore();
  const [collapsed, setCollapsed] = useLocalStorage("lectern.sidebarCollapsed", false);
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  const nav = useNavigate();
  const loc = useLocation();
  const isOwner = me?.user.role === "owner";
  const player = usePlayer();
  // The reader takes the whole window; the sidebar, top bar, and tab bar step aside.
  const immersive = loc.pathname.startsWith("/app/reader/");
  const fullPlayer = loc.pathname === "/app/listening";
  // The read-along page (milestone 4) has its own audio and Space shortcut.
  const readAlong = loc.pathname.startsWith("/app/read/");
  useEffect(() => {
    if (readAlong && player.playing) player.pause();
  }, [readAlong, player]);

  // Sheet sub-routes (/app/library/12, /app/devices/new) belong to their section; opening or
  // closing them must not steal focus from the sheet or from the element that opened it.
  const section = loc.pathname.replace(/\/(\d+|new)$/, "");
  useEffect(() => {
    // Move focus to the main region on navigation so screen readers announce the new page.
    if (document.querySelector('[aria-modal="true"]')) return;
    document.getElementById("main")?.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }, [section]);

  // Space plays or pauses the audiobook when focus is not on a control (buttons and fields keep
  // their own Space behaviour) and no dialog is open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== " " || e.repeat || e.metaKey || e.ctrlKey || e.altKey || !player.work || immersive || readAlong) return;
      const a = document.activeElement;
      if (a && a !== document.body && a.id !== "main" && a.closest("a, button, input, select, textarea, [role=slider], [contenteditable], [tabindex]:not(#main)")) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      e.preventDefault();
      player.toggle();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [player, immersive, readAlong]);

  const toggleTheme = () => setTheme(resolvedTheme === "dark" ? "light" : "dark");

  useHotkeys([
    { keys: "mod+k", handler: () => setPaletteOpen(true), inInputs: true },
    {
      // "/" focuses the page's filter when it has one (Library), otherwise opens search.
      keys: "/",
      handler: () => {
        const filter = document.querySelector<HTMLInputElement>("[data-page-filter]");
        if (filter) {
          filter.focus();
          filter.select();
        } else setPaletteOpen(true);
      },
    },
    { keys: "?", handler: () => setHelpOpen(true) },
    { keys: "t", handler: toggleTheme },
    { keys: "[", handler: () => setCollapsed(!collapsed) },
    { keys: "n", handler: () => nav("/app/devices/new") },
    { keys: "b", handler: () => canUpload && pickBooks() },
    { keys: "g h", handler: () => nav("/app") },
    { keys: "g l", handler: () => nav("/app/library") },
    { keys: "g p", handler: () => nav("/app/listening") },
    { keys: "g o", handler: () => nav("/app/downloads") },
    { keys: "g d", handler: () => nav("/app/devices") },
    { keys: "g s", handler: () => nav("/app/security") },
    { keys: "g a", handler: () => nav("/app/activity") },
    { keys: "g u", handler: () => isOwner && nav("/app/admin") },
  ]);

  const manage = [...MANAGE, ...(canEdit ? [TRASH] : []), ...(isOwner ? [ADMIN] : [])];
  const name = me?.user.display_name || me?.user.username || "";

  return (
    <div className="shell" data-collapsed={collapsed || undefined} data-immersive={immersive || undefined} data-playing={(player.work && !immersive && !fullPlayer && !readAlong) || undefined}>
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className="sidebar" aria-label="Sidebar">
        <div className="brand-row">
          <NavLink to="/app" className="brand" aria-label="Lectern home">
            <span className="brand-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="M6 4h9a3 3 0 0 1 3 3v13l-4.5-2.5L9 20V4.5" opacity=".55" /><path d="M6 4h3v16l-3-1.7z" /></svg>
            </span>
            <span className="label">Lectern</span>
          </NavLink>
          <button
            className="icon-btn ghost collapse-btn"
            onClick={() => setCollapsed(!collapsed)}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
            title={`${collapsed ? "Expand" : "Collapse"} sidebar ([)`}
          >
            <Sidebar />
          </button>
        </div>
        <button className="sidebar-search" onClick={() => setPaletteOpen(true)} aria-label="Search books, pages, and actions" aria-keyshortcuts="Meta+K Control+K" title={collapsed ? `Search (${MOD} K)` : undefined}>
          <Search />
          <span className="label">Search</span>
          <kbd aria-hidden="true">{MOD} K</kbd>
        </button>
        <nav className="nav-groups" aria-label="Primary">
          <div className="nav-section" aria-hidden={collapsed || undefined}>Read</div>
          {READ.map((n) => (
            <Fragment key={n.to}>
              <NavItem n={n} collapsed={collapsed} />
              {n.to === "/app/library" && !collapsed && <ShelfTree />}
            </Fragment>
          ))}
          <div className="nav-section" aria-hidden={collapsed || undefined}>Manage</div>
          {manage.map((n) => <NavItem key={n.to} n={n} collapsed={collapsed} />)}
        </nav>
        <div className="sidebar-footer">
          <button className="nav-item" onClick={() => setHelpOpen(true)} title={collapsed ? "Keyboard shortcuts (?)" : undefined}>
            <Help />
            <span className="label">Shortcuts</span>
            <kbd aria-hidden="true">?</kbd>
          </button>
          <button className="nav-item" onClick={toggleTheme} title={collapsed ? "Toggle theme (T)" : undefined}>
            {resolvedTheme === "dark" ? <Sun /> : <Moon />}
            <span className="label">{resolvedTheme === "dark" ? "Light theme" : "Dark theme"}</span>
            <kbd aria-hidden="true">T</kbd>
          </button>
          {me && (
            <div className="user-chip">
              <span className="avatar" aria-hidden="true">{initials(name)}</span>
              <div className="meta label">
                <strong>{name}</strong>
                <span>@{me.user.username} · {me.user.role}</span>
              </div>
              <button className="icon-btn ghost sm" onClick={() => logout(clearDevice)} aria-label="Sign out" title="Sign out">
                <Logout />
              </button>
            </div>
          )}
        </div>
      </aside>

      <main className="main" id="main" tabIndex={-1}>
        <header className="topbar">
          <div className="title-slot" ref={setSlot} />
          <OfflineStatus />
          <NotificationBell />
          <button className="icon-btn mobile-only" onClick={() => setPaletteOpen(true)} aria-label="Search books, pages, and actions">
            <Search />
          </button>
        </header>
        <OfflineBanner />
        <UpdateReady />
        <TitleSlot.Provider value={slot}>{children}</TitleSlot.Provider>
      </main>

      {!immersive && !fullPlayer && !readAlong && <MiniPlayer />}
      {!immersive && <UploadTray />}
      {canUpload && <ReviewSheet />}
      {canEdit && <WorkEditor />}
      {dragging && canUpload && (
        <div className="drop-overlay" aria-hidden="true">
          <div>
            <Upload />
            <strong>Drop to add books</strong>
            <span>EPUB, PDF, CBZ comics, MOBI/AZW3, M4B, MP3 — or a whole audiobook folder. You review before anything is filed.</span>
          </div>
        </div>
      )}

      <nav className="bottom-nav" aria-label="Primary">
        {[READ[0], READ[1], READ[2], READ[3], MANAGE[0]].map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}>
            {n.icon}
            <span>{n.label}</span>
          </NavLink>
        ))}
      </nav>

      <CommandPalette />
      <ConflictPrompt />
      <Sheet open={helpOpen} onClose={() => setHelpOpen(false)} title="Keyboard shortcuts" variant="dialog" labelledBy="help-title">
        <dl className="shortcut-list">
          <dt><kbd>{MOD}</kbd><kbd>K</kbd></dt><dd>Search books, authors, devices, settings, and actions</dd>
          <dt><kbd>/</kbd></dt><dd>Filter the library (search elsewhere)</dd>
          <dt><kbd>N</kbd></dt><dd>Add a device</dd>
          {canUpload && <><dt><kbd>B</kbd></dt><dd>Add books (or drop files anywhere)</dd></>}
          <dt><kbd>G</kbd> then <kbd>H</kbd></dt><dd>Home</dd>
          <dt><kbd>G</kbd> then <kbd>L</kbd></dt><dd>Library</dd>
          <dt><kbd>G</kbd> then <kbd>P</kbd></dt><dd>Listening (player)</dd>
          <dt><kbd>G</kbd> then <kbd>O</kbd></dt><dd>Downloads (offline books)</dd>
          <dt><kbd>Space</kbd></dt><dd>Play or pause the audiobook</dd>
          <dt><kbd>G</kbd> then <kbd>D</kbd></dt><dd>Devices</dd>
          <dt><kbd>G</kbd> then <kbd>S</kbd></dt><dd>Security</dd>
          <dt><kbd>G</kbd> then <kbd>A</kbd></dt><dd>Activity</dd>
          {isOwner && <><dt><kbd>G</kbd> then <kbd>U</kbd></dt><dd>Admin</dd></>}
          <dt><kbd>T</kbd></dt><dd>Toggle light / dark</dd>
          <dt><kbd>[</kbd></dt><dd>Collapse or expand the sidebar</dd>
          <dt><kbd>Esc</kbd></dt><dd>Clear the selection; close a sheet, menu, or the palette</dd>
        </dl>
        <h3 className="detail-h">Selecting books, uploads, and trash</h3>
        <dl className="shortcut-list">
          <dt>Click</dt><dd>Open a book or shelf; click its circle to start selecting, then clicks add or remove (<kbd>Enter</kbd> still opens). In Trash and uploads, select one</dd>
          <dt><kbd>Enter</kbd></dt><dd>Open the focused book or shelf</dd>
          <dt><kbd>{MOD}</kbd> click</dt><dd>Add or remove one</dd>
          <dt><kbd>Shift</kbd> click</dt><dd>Select a range; with <kbd>{MOD}</kbd>, add the range</dd>
          <dt><kbd>{MOD}</kbd><kbd>A</kbd></dt><dd>Select everything in this view, loaded or not</dd>
          <dt><kbd>←</kbd><kbd>↑</kbd><kbd>→</kbd><kbd>↓</kbd></dt><dd>Move; with <kbd>Shift</kbd>, extend the selection</dd>
          <dt><kbd>Home</kbd> <kbd>End</kbd></dt><dd>Start or end of the row; with <kbd>{MOD}</kbd> (or <kbd>{MOD}</kbd> + arrows), of everything</dd>
          <dt><kbd>Space</kbd></dt><dd>Select or deselect the focused one</dd>
          <dt>Drag</dt><dd>Onto a shelf (in the grid, the path above it, or the sidebar) to move there; hold <kbd>{MOD === "⌘" ? "⌥" : "Alt"}</kbd> to add instead</dd>
          <dt>Long press</dt><dd>On a touch screen: start selecting, then tap to add or remove</dd>
          <dt><kbd>?</kbd></dt><dd>This list</dd>
        </dl>
      </Sheet>
      {createPortal(
        // Outside #root so announcements still reach screen readers while a sheet makes #root inert.
        <div className="toasts" role="status" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={`toast ${t.kind ?? ""}`}>
              <span>{t.text}</span>
              {t.action && (
                <button className="toast-action" onClick={() => { dismissToast(t.id); t.action!.run(); }}>{t.action.label}</button>
              )}
            </div>
          ))}
        </div>,
        document.body,
      )}
    </div>
  );
}

/** Renders the page title (and optional actions) into the top bar. */
export function PageTitle({ children, aside, sub }: { children: ReactNode; aside?: ReactNode; sub?: ReactNode }) {
  const slot = useContext(TitleSlot);
  useEffect(() => {
    const text = typeof children === "string" ? children : "";
    document.title = text ? `${text} · Lectern` : "Lectern";
  }, [children]);
  if (!slot) return null;
  return createPortal(
    <>
      <div className="title-text">
        <h1>{children}</h1>
        {sub && <p className="page-sub">{sub}</p>}
      </div>
      {aside && <div className="title-actions">{aside}</div>}
    </>,
    slot,
  );
}
