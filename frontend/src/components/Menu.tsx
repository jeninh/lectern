import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { More } from "./Icons";

export interface MenuAction {
  label: string;
  icon?: ReactNode;
  danger?: boolean;
  onSelect: () => void;
}

/** Overflow menu button: ARIA menu pattern with arrow keys, Home/End, Esc, and outside click. */
export function OverflowMenu({ label, actions }: { label: string; actions: MenuAction[] }) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();

  const items = () => Array.from(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
  const close = (refocus = true) => {
    setOpen(false);
    if (refocus) btn.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    items()[0]?.focus();
    const onDown = (e: MouseEvent) => {
      if (!menu.current?.contains(e.target as Node) && !btn.current?.contains(e.target as Node)) close(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const onKey = (e: React.KeyboardEvent) => {
    const list = items();
    const at = list.indexOf(document.activeElement as HTMLElement);
    const focus = (i: number) => list[(i + list.length) % list.length]?.focus();
    if (e.key === "ArrowDown") focus(at + 1);
    else if (e.key === "ArrowUp") focus(at - 1);
    else if (e.key === "Home") focus(0);
    else if (e.key === "End") focus(list.length - 1);
    else if (e.key === "Escape") {
      e.stopPropagation();
      close();
    } else if (e.key === "Tab") close(false);
    else return;
    if (e.key !== "Tab") e.preventDefault();
  };

  return (
    <div className="menu-wrap">
      <button
        ref={btn}
        className="icon-btn ghost sm"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        <More />
      </button>
      {open && (
        <div className="menu" role="menu" id={id} ref={menu} aria-label={label} onKeyDown={onKey}>
          {actions.map((a) => (
            <button
              key={a.label}
              role="menuitem"
              tabIndex={-1}
              className={a.danger ? "danger" : undefined}
              onClick={() => {
                close(); // return focus to the trigger first so a follow-up dialog restores to it
                a.onSelect();
              }}
            >
              {a.icon}
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
