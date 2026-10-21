import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { createPortal } from "react-dom";
import { get } from "../lib/api";
import { makeBackgroundInert } from "../lib/hooks";
import { relativeTime } from "../lib/format";
import { fuzzyScore } from "../lib/fuzzy";
import { useStore } from "../lib/store";
import type { Device, Work } from "../lib/types";
import { Cover } from "./Cover";
import { usePlayer } from "../lib/player";
import { bookFiles, readerPath } from "../lib/formats";
import { Devices, Folder, Gear, Headphones, Help, Home, Key, Library, Link2, Moon, Pencil, Person, Plus, Pulse, Reader, Search, Shield, Sun, Trash, Upload } from "./Icons";

type Group = "Actions" | "Pages" | "Books" | "Authors" | "Devices" | "Settings";
const ORDER: Group[] = ["Actions", "Books", "Authors", "Devices", "Pages", "Settings"];

interface Item {
  id: string;
  group: Group;
  label: string;
  /** Extra words that match but are not shown. */
  keywords?: string;
  detail?: string;
  hint?: string;
  icon?: JSX.Element;
  work?: Work;
  run: () => void;
}

export function CommandPalette() {
  const { paletteOpen, setPaletteOpen, works, me, resolvedTheme, setTheme, setHelpOpen, canUpload, canEdit, pickBooks, pickFolder, imports, setReviewOpen, openEditor } = useStore();
  const nav = useNavigate();
  const player = usePlayer();
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [devices, setDevices] = useState<Device[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const restore = useRef<HTMLElement | null>(null);

  const close = () => {
    setPaletteOpen(false);
    setQ("");
    setSel(0);
  };
  const go = (path: string) => {
    setPaletteOpen(false);
    setQ("");
    nav(path);
  };

  useEffect(() => {
    if (!paletteOpen) return;
    restore.current = document.activeElement as HTMLElement | null;
    const undoInert = makeBackgroundInert();
    setSel(0);
    input.current?.focus();
    get<{ devices: Device[] }>("/api/devices")
      .then((d) => setDevices(d.devices.filter((x) => !x.revoked_at)))
      .catch(() => {});
    return () => {
      undoInert();
      // Navigation moves focus to the new page itself; otherwise return to where we were.
      if (document.activeElement === document.body || !document.activeElement) restore.current?.focus?.();
    };
  }, [paletteOpen]);

  const groups = useMemo(() => {
    const isOwner = me?.user.role === "owner";
    const all: Item[] = [
      { id: "a-add", group: "Actions", label: "Add device", keywords: "new koreader kobo kindle opds connect", hint: "N", icon: <Plus />, run: () => go("/app/devices/new") },
      ...(canUpload
        ? [
            { id: "a-upload", group: "Actions" as Group, label: "Add books or audiobooks", keywords: "upload epub pdf cbz comic m4b mp3 audio import add file", hint: "B", icon: <Upload />, run: () => { close(); pickBooks(); } },
            { id: "a-folder", group: "Actions" as Group, label: "Add an audiobook folder", keywords: "upload directory mp3 tracks multi-track import", icon: <Folder />, run: () => { close(); pickFolder(); } },
            ...(imports.some((i) => i.status === "ready")
              ? [{ id: "a-review", group: "Actions" as Group, label: `Review ${imports.filter((i) => i.status === "ready").length} uploaded before filing`, keywords: "imports pending match", icon: <Upload />, run: () => { close(); setReviewOpen(true); } }]
              : []),
          ]
        : []),
      ...(canEdit ? [{ id: "p-trash", group: "Pages" as Group, label: "Trash", keywords: "deleted restore recover removed", icon: <Trash />, run: () => go("/app/trash") }] : []),
      {
        id: "a-theme",
        group: "Actions",
        label: resolvedTheme === "dark" ? "Switch to light theme" : "Switch to dark theme",
        keywords: "theme dark light mode appearance",
        hint: "T",
        icon: resolvedTheme === "dark" ? <Sun /> : <Moon />,
        run: () => {
          setTheme(resolvedTheme === "dark" ? "light" : "dark");
          close();
        },
      },
      { id: "a-help", group: "Actions", label: "Keyboard shortcuts", keywords: "help keys hotkeys", hint: "?", icon: <Help />, run: () => { close(); setHelpOpen(true); } },

      ...(player.work ? [{ id: "a-play", group: "Actions" as Group, label: player.playing ? `Pause ${player.work.title}` : `Play ${player.work.title}`, keywords: "audio listen resume", hint: "Space", icon: <Headphones />, run: () => { close(); player.toggle(); } }] : []),
      ...(isOwner ? [{ id: "a-invite", group: "Actions" as Group, label: "Invite someone to the household", keywords: "invite member reader family link", icon: <Link2 />, run: () => go("/app/admin#invites") }] : []),
      { id: "p-home", group: "Pages", label: "Home", keywords: "continue reading", hint: "G H", icon: <Home />, run: () => go("/app") },
      { id: "p-lib", group: "Pages", label: "Library", keywords: "books shelves", hint: "G L", icon: <Library />, run: () => go("/app/library") },
      { id: "p-listen", group: "Pages", label: "Listening", keywords: "audiobooks player audio", hint: "G P", icon: <Headphones />, run: () => go("/app/listening") },
      { id: "p-dev", group: "Pages", label: "Devices", hint: "G D", icon: <Devices />, run: () => go("/app/devices") },
      { id: "p-sec", group: "Pages", label: "Security", hint: "G S", icon: <Shield />, run: () => go("/app/security") },
      { id: "p-act", group: "Pages", label: "Activity", keywords: "audit log", hint: "G A", icon: <Pulse />, run: () => go("/app/activity") },
      ...(isOwner ? [{ id: "p-admin", group: "Pages" as Group, label: "Admin", keywords: "users members", hint: "G U", icon: <Gear />, run: () => go("/app/admin") }] : []),

      { id: "s-pk", group: "Settings", label: "Passkeys", keywords: "webauthn face id touch id security key", icon: <Key />, run: () => go("/app/security#passkeys") },
      { id: "s-totp", group: "Settings", label: "Two-factor codes", keywords: "totp authenticator 2fa otp", icon: <Shield />, run: () => go("/app/security#totp") },
      { id: "s-tech", group: "Settings", label: "Show technical details", keywords: "koreader xpointer md5 path debug developer", icon: <Gear />, run: () => go("/app/security#display") },
      { id: "s-pw", group: "Settings", label: "Change password", icon: <Key />, run: () => go("/app/security#password") },
      { id: "s-ses", group: "Settings", label: "Signed-in sessions", keywords: "browsers sign out", icon: <Shield />, run: () => go("/app/security#sessions") },
      ...(isOwner ? [{ id: "s-users", group: "Settings" as Group, label: "Add a user", keywords: "member reader invite", icon: <Person />, run: () => go("/app/admin?new=1") }] : []),

      ...works.map<Item>((w) => ({ id: `w${w.id}`, group: "Books", label: w.title, detail: w.author, keywords: w.isbn ?? "", work: w, run: () => go(`/app/library/${w.id}`) })),
      ...works.filter((w) => bookFiles(w).length > 0).map<Item>((w) => ({ id: `r${w.id}`, group: "Actions", label: `Read ${w.title}`, keywords: `open ${w.author} pdf comic`, icon: <Reader />, run: () => go(readerPath(w)) })),
      ...(canEdit ? works.map<Item>((w) => ({ id: `e${w.id}`, group: "Actions", label: `Edit ${w.title}`, keywords: `metadata cover tracks ${w.author}`, icon: <Pencil />, run: () => { close(); openEditor(w.id); } })) : []),
      ...works.filter((w) => w.audio).map<Item>((w) => ({ id: `l${w.id}`, group: "Actions", label: `Listen to ${w.title}`, keywords: `play audiobook ${w.author}`, icon: <Headphones />, run: () => { close(); player.open(w, { autoplay: true }); } })),
      ...[...new Set(works.map((w) => w.author))].map<Item>((a) => {
        const n = works.filter((w) => w.author === a).length;
        return { id: `au-${a}`, group: "Authors", label: a, detail: `${n} ${n === 1 ? "book" : "books"}`, icon: <Person />, run: () => go(`/app/library?q=${encodeURIComponent(a)}`) };
      }),
      ...devices.map<Item>((d) => ({
        id: `d-${d.id}`,
        group: "Devices",
        label: d.label,
        detail: d.last_used_at ? `last seen ${relativeTime(d.last_used_at)}` : "waiting for first sync",
        icon: <Reader />,
        run: () => go(`/app/devices#${d.id}`),
      })),
    ];

    const needle = q.trim();
    let picked: { item: Item; score: number }[];
    if (!needle) {
      // Without a query: actions, a few recent books, and pages.
      const recent = [...works].sort((a, b) => (b.progress?.updated_at ?? b.created_at ?? "").localeCompare(a.progress?.updated_at ?? a.created_at ?? "")).slice(0, 4);
      const ids = new Set(recent.map((w) => `w${w.id}`));
      picked = all.filter((i) => (i.group === "Actions" && !/^[rle]\d/.test(i.id)) || i.group === "Pages" || ids.has(i.id)).map((item) => ({ item, score: 0 }));
    } else {
      picked = all
        .map((item) => {
          const main = fuzzyScore(needle, item.label);
          const extra = fuzzyScore(needle, `${item.label} ${item.detail ?? ""} ${item.keywords ?? ""}`);
          const score = main ?? (extra == null ? null : extra - 8);
          return score == null ? null : { item, score };
        })
        .filter((x): x is { item: Item; score: number } => x !== null)
        .sort((a, b) => b.score - a.score);
    }
    const byGroup = new Map<Group, { items: Item[]; best: number }>();
    for (const { item, score } of picked) {
      const g = byGroup.get(item.group) ?? { items: [], best: score };
      if (g.items.length < (item.group === "Books" || item.group === "Actions" ? 7 : 5)) g.items.push(item);
      byGroup.set(item.group, g);
    }
    return [...byGroup.entries()]
      .sort((a, b) => (needle ? b[1].best - a[1].best : 0) || ORDER.indexOf(a[0]) - ORDER.indexOf(b[0]))
      .map(([group, g]) => ({ group, items: g.items }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, works, me, resolvedTheme, devices, canUpload, canEdit, imports, player.work, player.playing]);

  const flat = groups.flatMap((g) => g.items);

  useEffect(() => setSel(0), [q]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  if (!paletteOpen) return null;
  const move = (d: number) => setSel((s) => (flat.length ? (s + d + flat.length) % flat.length : 0));
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey) || (e.ctrlKey && e.key === "n")) {
      e.preventDefault();
      move(1);
    } else if (e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey) || (e.ctrlKey && e.key === "p")) {
      e.preventDefault();
      move(-1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      flat[sel]?.run();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  };
  let i = -1;
  return createPortal(
    <div className="palette" onMouseDown={(e) => e.target === e.currentTarget && close()}>
      <div className="panel" role="dialog" aria-modal="true" aria-label="Search and commands">
        <div className="palette-input">
          <Search aria-hidden="true" />
          <input
            ref={input}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKey}
            placeholder="Search books, authors, devices, settings…"
            role="combobox"
            aria-expanded="true"
            aria-haspopup="listbox"
            aria-controls="palette-list"
            aria-activedescendant={flat[sel] ? `opt-${flat[sel].id}` : undefined}
            aria-autocomplete="list"
            aria-label="Search books, authors, devices, settings, and actions"
            spellCheck={false}
            autoComplete="off"
          />
          <kbd aria-hidden="true">esc</kbd>
        </div>
        <div id="palette-list" role="listbox" ref={listRef} aria-label="Results">
          {flat.length === 0 && (
            <div className="palette-empty" role="presentation">
              <strong>No matches for “{q}”</strong>
              <span>Try an author’s surname, a device name, or “passkey”.</span>
            </div>
          )}
          {groups.map((g) => (
            <div key={g.group} role="group" aria-labelledby={`pg-${g.group}`}>
              <div className="group" id={`pg-${g.group}`} role="presentation">{g.group}</div>
              {g.items.map((it) => {
                i += 1;
                const idx = i;
                return (
                    <div
                      key={it.id}
                      id={`opt-${it.id}`}
                      role="option"
                      aria-selected={idx === sel}
                      onMouseMove={() => idx !== sel && setSel(idx)}
                      onClick={it.run}
                      className="option"
                    >
                      {it.work ? <Cover work={it.work} /> : <span className="opt-icon">{it.icon}</span>}
                      <div className="meta">
                        <span className="opt-label">{it.label}</span>
                        {it.detail && <span className="opt-detail">{it.detail}</span>}
                      </div>
                      {it.hint && <kbd aria-hidden="true">{it.hint}</kbd>}
                    </div>
                );
              })}
            </div>
          ))}
        </div>
        <div className="foot" aria-hidden="true">
          <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
          <span><kbd>↵</kbd> open</span>
          <span><kbd>esc</kbd> close</span>
        </div>
      </div>
    </div>,
    document.body,
  );
}
