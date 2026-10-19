import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "../lib/store";
import { Cover } from "./Cover";
import { Devices, Home, Library, Moon, Plus, Pulse, Shield, Sun, Users, Help } from "./Icons";

interface Item {
  id: string;
  group: "Pages" | "Actions" | "Books";
  label: string;
  hint?: string;
  icon?: JSX.Element;
  work?: import("../lib/types").Work;
  run: () => void;
}

export function CommandPalette() {
  const { paletteOpen, setPaletteOpen, works, me, resolvedTheme, setTheme, setHelpOpen } = useStore();
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const close = () => {
    setPaletteOpen(false);
    setQ("");
    setSel(0);
  };
  const go = (path: string) => {
    close();
    nav(path);
  };

  const items = useMemo<Item[]>(() => {
    const pages: Item[] = [
      { id: "p-home", group: "Pages", label: "Home", hint: "g h", icon: <Home />, run: () => go("/app") },
      { id: "p-lib", group: "Pages", label: "Library", hint: "g l", icon: <Library />, run: () => go("/app/library") },
      { id: "p-dev", group: "Pages", label: "Devices", hint: "g d", icon: <Devices />, run: () => go("/app/devices") },
      { id: "p-sec", group: "Pages", label: "Security", hint: "g s", icon: <Shield />, run: () => go("/app/security") },
      { id: "p-act", group: "Pages", label: "Activity", hint: "g a", icon: <Pulse />, run: () => go("/app/activity") },
    ];
    if (me?.user.role === "owner") pages.push({ id: "p-users", group: "Pages", label: "Users", hint: "g u", icon: <Users />, run: () => go("/app/users") });
    const actions: Item[] = [
      { id: "a-add", group: "Actions", label: "Add a device", hint: "n", icon: <Plus />, run: () => go("/app/devices/new") },
      {
        id: "a-theme",
        group: "Actions",
        label: resolvedTheme === "dark" ? "Switch to light theme" : "Switch to dark theme",
        hint: "t",
        icon: resolvedTheme === "dark" ? <Sun /> : <Moon />,
        run: () => {
          setTheme(resolvedTheme === "dark" ? "light" : "dark");
          close();
        },
      },
      { id: "a-help", group: "Actions", label: "Keyboard shortcuts", hint: "?", icon: <Help />, run: () => { close(); setHelpOpen(true); } },
    ];
    const needle = q.trim().toLowerCase();
    const books: Item[] = (needle ? works.filter((w) => `${w.title} ${w.author}`.toLowerCase().includes(needle)) : works.slice(0, 5))
      .slice(0, 8)
      .map((w) => ({ id: `w${w.id}`, group: "Books", label: w.title, hint: w.author, work: w, run: () => go(`/app/library/${w.id}`) }));
    const filt = (xs: Item[]) => (needle ? xs.filter((x) => x.label.toLowerCase().includes(needle)) : xs);
    return [...filt(pages), ...filt(actions), ...books];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, works, me, resolvedTheme]);

  useEffect(() => {
    if (paletteOpen) {
      setSel(0);
      window.setTimeout(() => input.current?.focus(), 0);
    }
  }, [paletteOpen]);
  useEffect(() => setSel(0), [q]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  if (!paletteOpen) return null;
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((s) => Math.min(items.length - 1, s + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => Math.max(0, s - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      items[sel]?.run();
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  };
  let lastGroup = "";
  return (
    <div className="palette" onClick={close}>
      <div className="panel" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label="Command palette">
        <input
          ref={input}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKey}
          placeholder="Search books, pages, actions…"
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-list"
          aria-activedescendant={items[sel]?.id}
          aria-autocomplete="list"
        />
        <ul id="palette-list" role="listbox" ref={listRef}>
          {items.length === 0 && <li className="group">No matches</li>}
          {items.map((it, i) => {
            const header = it.group !== lastGroup ? <li key={`g-${it.group}`} className="group" role="presentation">{it.group}</li> : null;
            lastGroup = it.group;
            return (
              <>
                {header}
                <li key={it.id} id={it.id} role="option" aria-selected={i === sel} onMouseEnter={() => setSel(i)} onClick={it.run}>
                  {it.work ? <Cover work={it.work} /> : <span style={{ width: 18, display: "inline-flex" }}>{it.icon}</span>}
                  <div className="meta">
                    {it.label}
                    {it.work && <span>{it.hint}</span>}
                  </div>
                  {!it.work && it.hint && <kbd>{it.hint}</kbd>}
                </li>
              </>
            );
          })}
        </ul>
        <div className="foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
          <span><kbd>↵</kbd> open</span>
          <span><kbd>esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}
