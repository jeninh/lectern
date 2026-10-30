// The Library's filter chips (docs/filters.md): a Filter button that adds one, chips that edit
// on click and go with ×, each one "is" or "is not", several values ORed. The chips are a
// filter in the syntax of lib/filters, kept in the URL (`f`); the server evaluates it.

import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { get } from "../lib/api";
import { clauseLabel, FIELDS, fieldOf, FilterError, formatClause, GROUPS, isComparable, parse, valueLabel, type Clause, type Field, type Names, type Op, type Value } from "../lib/filters";
import { pathLabel } from "../lib/shelves";
import { useStore } from "../lib/store";
import type { AppPassword } from "../lib/types";
import { Close, Filter } from "./Icons";

/** Shelf and device names for chip labels ("#12" is a shelf this person sees). */
export function useNames(): { names: Names; devices: AppPassword[] } {
  const { shelves } = useStore();
  const [devices, setDevices] = useState<AppPassword[]>([]);
  useEffect(() => {
    get<{ app_passwords: AppPassword[] }>("/api/devices")
      .then((d) => setDevices(d.app_passwords.filter((p) => !p.revoked_at)))
      .catch(() => {});
  }, []);
  const names = useMemo<Names>(() => {
    const byShelf = new Map(shelves.map((s) => [`#${s.id}`, pathLabel(s)]));
    const byDevice = new Map(devices.map((d) => [`#${d.id}`, d.label]));
    return (key, ref) => (key === "shelf" ? byShelf.get(ref) : byDevice.get(ref));
  }, [shelves, devices]);
  return { names, devices };
}

/** A small anchored panel that closes on Escape and outside clicks. */
function Popover({ open, onClose, anchor, label, children, wide }: { open: boolean; onClose: () => void; anchor: ReactNode; label: string; children: ReactNode; wide?: boolean }) {
  const wrap = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) onClose();
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open, onClose]);
  return (
    <div className="pop-wrap" ref={wrap} onKeyDown={(e) => {
      if (e.key === "Escape" && open) {
        e.stopPropagation();
        onClose();
        (wrap.current?.querySelector("[data-anchor]") as HTMLElement | null)?.focus();
      }
    }}>
      {anchor}
      {open && (
        <div className={`popover${wide ? " wide" : ""}`} role="dialog" aria-label={label}>
          {children}
        </div>
      )}
    </div>
  );
}

const OPS: { op: Op; label: string }[] = [
  { op: "=", label: "is" },
  { op: "<", label: "less than" },
  { op: "<=", label: "at most" },
  { op: ">", label: "more than" },
  { op: ">=", label: "at least" },
  { op: "..", label: "between" },
];
const DATE_OPS: { op: Op; label: string }[] = [
  { op: "=", label: "in" },
  { op: "<", label: "before" },
  { op: ">", label: "after" },
  { op: "..", label: "between" },
];

/** Edit one clause: values for its field, and "is" or "is not". */
function ClauseEditor({ field, initial, onApply, onCancel, names, devices }: { field: Field; initial: Clause | null; onApply: (c: Clause) => void; onCancel: () => void; names: Names; devices: AppPassword[] }) {
  const { shelves } = useStore();
  const [negate, setNegate] = useState(initial?.negate ?? false);
  const [values, setValues] = useState<Value[]>(initial?.values ?? []);
  const first = initial?.values[0];
  const [op, setOp] = useState<Op>(first?.op ?? "=");
  const [a, setA] = useState(first?.a ?? "");
  const [b, setB] = useState(first?.b ?? "");
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [suggest, setSuggest] = useState<string[]>([]);
  const listId = useId();
  const comparable = isComparable(field.kind);

  useEffect(() => {
    if (field.kind === "text" || field.kind === "user") {
      const key = field.key === "lang" ? "lang" : field.key;
      if (["author", "series", "tag", "narrator", "lang", "publisher", "by"].includes(key))
        get<{ values: { value: string }[] }>(`/api/library/facets?field=${key}`)
          .then((d) => setSuggest(d.values.map((v) => v.value)))
          .catch(() => {});
    }
  }, [field.kind, field.key]);

  const refs: { value: string; label: string }[] =
    field.kind === "shelf"
      ? [{ value: "none", label: "Not on a shelf" }, { value: "any", label: "On any shelf" }, ...shelves.map((s) => ({ value: `#${s.id}`, label: pathLabel(s) }))]
      : field.kind === "device"
        ? [{ value: "any", label: "Any device" }, ...devices.map((d) => ({ value: `#${d.id}`, label: d.label }))]
        : [];

  const build = (): Clause | null => {
    let vals = values;
    if (comparable) vals = [{ op, a: op === ".." ? a : a, b: op === ".." ? b : "" }];
    else if (text.trim()) vals = [...values, { op: "=", a: text.trim(), b: "" }];
    if (!vals.length || (comparable && !a.trim() && !b.trim())) {
      setError("Choose or type a value.");
      return null;
    }
    const c: Clause = { key: field.key, values: vals, negate };
    try {
      // Round-trip through the parser: the same check the server makes.
      const parsed = parse(formatClause(c)).clauses[0];
      setError(null);
      return parsed;
    } catch (e) {
      setError(e instanceof FilterError ? e.message : String(e));
      return null;
    }
  };
  const toggle = (v: string) => setValues((cur) => (cur.some((x) => x.a === v) ? cur.filter((x) => x.a !== v) : [...cur, { op: "=", a: v, b: "" }]));
  const has = (v: string) => values.some((x) => x.a === v);

  return (
    <form
      className="clause-editor"
      onSubmit={(e) => {
        e.preventDefault();
        const c = build();
        if (c) onApply(c);
      }}
    >
      <div className="clause-head">
        <strong>{field.label}</strong>
        <div className="seg sm" role="group" aria-label="Match">
          <button type="button" aria-pressed={!negate} onClick={() => setNegate(false)}>is</button>
          <button type="button" aria-pressed={negate} onClick={() => setNegate(true)}>is not</button>
        </div>
      </div>
      {field.kind === "enum" ? (
        <div className="clause-options" role="group" aria-label={`${field.label} values`}>
          {field.options.map((opt) => (
            <label key={opt.value} className="check">
              <input type="checkbox" checked={has(opt.value)} onChange={() => toggle(opt.value)} />
              {opt.label}
            </label>
          ))}
        </div>
      ) : comparable ? (
        <div className="clause-range">
          <label className="sr-only" htmlFor={`${listId}-op`}>Comparison</label>
          <select id={`${listId}-op`} className="select sm" value={op} onChange={(e) => setOp(e.target.value as Op)}>
            {(field.kind === "date" ? DATE_OPS : OPS).map((x) => <option key={x.op} value={x.op}>{x.label}</option>)}
          </select>
          <input className="input sm" aria-label={op === ".." ? "From" : "Value"} value={a} onChange={(e) => setA(e.target.value)} placeholder={field.hint ?? ""} data-autofocus autoFocus />
          {op === ".." && (
            <>
              <span className="hint">and</span>
              <input className="input sm" aria-label="To" value={b} onChange={(e) => setB(e.target.value)} placeholder={field.hint ?? ""} />
            </>
          )}
          <p className="hint clause-help">{HELP[field.kind]}</p>
        </div>
      ) : (
        <div className="clause-values">
          {values.length > 0 && (
            <ul className="chips" aria-label="Chosen values">
              {values.map((v) => (
                <li key={v.a}>
                  <span className="chip subtle static">
                    {valueLabel(field, v, names)}
                    <button type="button" className="chip-x" aria-label={`Remove ${valueLabel(field, v, names)}`} onClick={() => toggle(v.a)}><Close /></button>
                  </span>
                </li>
              ))}
            </ul>
          )}
          {refs.length > 0 ? (
            <select className="select sm" aria-label={`Add a ${field.label.toLowerCase()}`} value="" onChange={(e) => e.target.value && toggle(e.target.value)} autoFocus>
              <option value="">{values.length ? "Or also…" : "Choose…"}</option>
              {refs.filter((r) => !has(r.value)).map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          ) : (
            <>
              <input
                className="input sm"
                aria-label={values.length ? `Or another ${field.label.toLowerCase()}` : field.label}
                placeholder={values.length ? "Or also… (Enter adds)" : "Type, then Enter"}
                list={suggest.length ? listId : undefined}
                value={text}
                autoFocus
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && text.trim() && !e.metaKey && !e.ctrlKey) {
                    e.preventDefault();
                    if (!has(text.trim())) setValues((cur) => [...cur, { op: "=", a: text.trim(), b: "" }]);
                    setText("");
                  }
                }}
              />
              {suggest.length > 0 && (
                <datalist id={listId}>
                  {suggest.map((s) => <option key={s} value={s} />)}
                </datalist>
              )}
              <p className="hint clause-help">{field.key === "lang" ? "A language code, such as en or fr." : field.key === "tag" ? "The whole tag; accents and case don’t matter." : "Matches part of the name; accents and case don’t matter."}</p>
            </>
          )}
        </div>
      )}
      {error && <p className="field-error" role="alert">{error}</p>}
      <div className="row clause-actions">
        <button type="button" className="btn sm ghost" onClick={onCancel}>Cancel</button>
        <button className="btn sm primary">{initial ? "Apply" : "Add filter"}</button>
      </div>
    </form>
  );
}

const HELP: Partial<Record<Field["kind"], string>> = {
  number: "A whole number.",
  size: "Such as 100MB or 1.5GB (a bare number is MB).",
  duration: "Such as 10h or 1h30m (a bare number is hours).",
  percent: "Such as 50%.",
  date: "A date (2026, 2026-03, 2026-03-15) or an age: 30d, 2w, 6m, 1y.",
  rating: "Stars, 0 to 5 in halves: 4.5, or >=4. Only you see your ratings.",
};

/** One chip: click to edit, × to remove. */
function ChipButton({ clause, onChange, onRemove, names, devices, editable }: { clause: Clause; onChange: (c: Clause) => void; onRemove: () => void; names: Names; devices: AppPassword[]; editable: boolean }) {
  const [open, setOpen] = useState(false);
  const field = fieldOf(clause.key)!;
  const { field: name, values } = clauseLabel(clause, names);
  const text = `${name}: ${values}`;
  if (!editable)
    return (
      <span className={`filter-chip static${clause.negate ? " negated" : ""}`}>
        <span className="k">{name}:</span> {values}
      </span>
    );
  return (
    <Popover
      open={open}
      onClose={() => setOpen(false)}
      label={`Edit ${name}`}
      anchor={
        <span className={`filter-chip${clause.negate ? " negated" : ""}`}>
          <button type="button" data-anchor className="chip-main" aria-expanded={open} aria-label={`${text}. Edit`} onClick={() => setOpen((o) => !o)}>
            <span className="k">{name}:</span> {values}
          </button>
          <button type="button" className="chip-x" aria-label={`Remove ${text}`} onClick={onRemove}><Close /></button>
        </span>
      }
    >
      <ClauseEditor field={field} initial={clause} names={names} devices={devices} onCancel={() => setOpen(false)} onApply={(c) => { setOpen(false); onChange(c); }} />
    </Popover>
  );
}

/** The Filter button: pick a field, then its values. */
function AddFilter({ onAdd, names, devices, compact }: { onAdd: (c: Clause) => void; names: Names; devices: AppPassword[]; compact: boolean }) {
  const { me } = useStore();
  const [open, setOpen] = useState(false);
  const [field, setField] = useState<Field | null>(null);
  const reader = me?.user.role === "reader";
  const close = () => {
    setOpen(false);
    setField(null);
  };
  return (
    <Popover
      open={open}
      onClose={close}
      label={field ? `Filter by ${field.label}` : "Add a filter"}
      wide={!field}
      anchor={
        <button type="button" data-anchor className={`btn ${compact ? "sm" : ""} ghost filter-btn`} aria-expanded={open} aria-haspopup="dialog" onClick={() => (open ? close() : setOpen(true))}>
          <Filter /> Filter
        </button>
      }
    >
      {field ? (
        <ClauseEditor field={field} initial={null} names={names} devices={devices} onCancel={() => setField(null)} onApply={(c) => { close(); onAdd(c); }} />
      ) : (
        <div className="field-menu">
          {GROUPS.map((g) => (
            <div key={g} role="group" aria-label={g}>
              <h4>{g}</h4>
              {FIELDS.filter((x) => x.group === g && !(reader && x.members_only)).map((x) => (
                <button key={x.key} type="button" className="field-item" onClick={() => setField(x)}>
                  {x.label}
                  <code>{x.key}:</code>
                </button>
              ))}
            </div>
          ))}
          <p className="hint field-menu-tip">Or type them in the search box, like <code>format:epub -has:audio year:1850..1900</code>.</p>
        </div>
      )}
    </Popover>
  );
}

/** The chips row: the Filter button, each chip, and what to do with them. */
export function FilterBar({ clauses, onChange, actions, error }: { clauses: Clause[]; onChange: (next: Clause[]) => void; actions?: ReactNode; error?: string | null }) {
  const { names, devices } = useNames();
  return (
    <div className="filter-chips" role="group" aria-label="Filters">
      <AddFilter onAdd={(c) => onChange([...clauses, c])} names={names} devices={devices} compact={clauses.length > 0} />
      {clauses.map((c, i) => (
        <ChipButton
          key={`${i}-${formatClause(c)}`}
          clause={c}
          names={names}
          devices={devices}
          editable
          onChange={(next) => onChange(clauses.map((x, j) => (j === i ? next : x)))}
          onRemove={() => onChange(clauses.filter((_, j) => j !== i))}
        />
      ))}
      {clauses.length > 1 && (
        <button type="button" className="btn sm ghost" onClick={() => onChange([])}>Clear filters</button>
      )}
      {actions}
      {error && <span className="field-error" role="status">{error}</span>}
    </div>
  );
}

/** A smart shelf's rule, read-only. */
export function RuleChips({ clauses }: { clauses: Clause[] }) {
  const { names, devices } = useNames();
  return (
    <span className="filter-chips static" aria-label="Smart shelf filter">
      {clauses.map((c, i) => (
        <ChipButton key={i} clause={c} names={names} devices={devices} editable={false} onChange={() => {}} onRemove={() => {}} />
      ))}
    </span>
  );
}
