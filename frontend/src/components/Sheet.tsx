import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useFocusTrap } from "../lib/hooks";
import { Close } from "./Icons";

interface Props {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  /** "sheet" slides from the side (bottom on phones); "dialog" is centred. */
  variant?: "sheet" | "dialog";
  labelledBy?: string;
  /** Wider side sheet for editors (still full screen on phones). */
  wide?: boolean;
  /** Sticky action bar at the bottom of the sheet. */
  footer?: ReactNode;
}

export function Sheet({ open, onClose, title, children, variant = "sheet", labelledBy = "sheet-title", wide, footer }: Props) {
  const ref = useFocusTrap(open);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, onClose]);
  if (!open) return null;
  const panel = (
    <div className="sheet-head">
      <h2 id={labelledBy}>{title}</h2>
      <button className="icon-btn close" onClick={onClose} aria-label="Close">
        <Close />
      </button>
    </div>
  );
  return createPortal(
    <>
      <div className="scrim" onClick={onClose} aria-hidden="true" />
      {variant === "sheet" ? (
        <div className={`sheet${wide ? " wide" : ""}${footer ? " has-footer" : ""}`} role="dialog" aria-modal="true" aria-labelledby={labelledBy} ref={ref} tabIndex={-1}>
          {panel}
          {children}
          {footer && <div className="sheet-foot">{footer}</div>}
        </div>
      ) : (
        <div className="dialog">
          <div className="panel" role="dialog" aria-modal="true" aria-labelledby={labelledBy} ref={ref} tabIndex={-1}>
            {panel}
            {children}
          </div>
        </div>
      )}
    </>,
    document.body,
  );
}
