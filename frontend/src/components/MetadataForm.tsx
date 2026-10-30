import { useId, useState } from "react";
import { FIELD_LABEL, sortName, type FieldErrors } from "../lib/metadata";
import type { AuthorName, Contributor, ContributorRole, Metadata } from "../lib/types";
import { Close, Plus } from "./Icons";

/** Free-form list (tags, narrators): Enter or comma adds, Backspace on empty removes the last. */
export function ChipInput({ id, values, onChange, placeholder, label }: { id: string; values: string[]; onChange: (v: string[]) => void; placeholder?: string; label: string }) {
  const [text, setText] = useState("");
  const add = (raw: string) => {
    const parts = raw.split(/[,;\n]/).map((s) => s.trim()).filter(Boolean);
    if (!parts.length) return;
    const seen = new Set(values.map((v) => v.toLowerCase()));
    onChange([...values, ...parts.filter((p) => !seen.has(p.toLowerCase()) && (seen.add(p.toLowerCase()), true))]);
    setText("");
  };
  return (
    <div className="chip-input" onClick={(e) => (e.currentTarget.querySelector("input") as HTMLInputElement | null)?.focus()}>
      {values.map((v, i) => (
        <span key={v + i} className="chip-token">
          {v}
          <button type="button" aria-label={`Remove ${label.toLowerCase()} ${v}`} onClick={() => onChange(values.filter((_, j) => j !== i))}>
            <Close width={12} height={12} />
          </button>
        </span>
      ))}
      <input
        id={id}
        value={text}
        placeholder={values.length ? "" : placeholder}
        onChange={(e) => (e.target.value.includes(",") ? add(e.target.value) : setText(e.target.value))}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            add(text);
          } else if (e.key === "Backspace" && !text && values.length) onChange(values.slice(0, -1));
        }}
        onBlur={() => add(text)}
        maxLength={128}
        autoComplete="off"
      />
    </div>
  );
}

function AuthorsField({ value, onChange, error }: { value: AuthorName[]; onChange: (v: AuthorName[]) => void; error?: string }) {
  const id = useId();
  const rows = value.length ? value : [{ name: "", sort: null }];
  const set = (i: number, patch: Partial<AuthorName>) => onChange(rows.map((a, j) => (j === i ? { ...a, ...patch } : a)));
  return (
    <fieldset className="field authors" aria-describedby={error ? `${id}-err` : undefined}>
      <legend>Authors</legend>
      {rows.map((a, i) => (
        <div key={i} className="author-row">
          <label className="sr-only" htmlFor={`${id}-n${i}`}>Author {i + 1} name</label>
          <input id={`${id}-n${i}`} className="input" value={a.name} placeholder="Name, e.g. Ursula K. Le Guin" maxLength={256} onChange={(e) => set(i, { name: e.target.value })} aria-invalid={!!error && !a.name.trim()} />
          <label className="sr-only" htmlFor={`${id}-s${i}`}>Author {i + 1} sort name</label>
          <input id={`${id}-s${i}`} className="input" value={a.sort ?? ""} placeholder={a.name ? sortName(a.name) : "Sort as"} maxLength={256} onChange={(e) => set(i, { sort: e.target.value || null })} title="Sort name: how the author files in the library" />
          <button type="button" className="icon-btn ghost sm" aria-label={`Remove author ${a.name || i + 1}`} onClick={() => onChange(rows.filter((_, j) => j !== i))} disabled={rows.length === 1 && !a.name}>
            <Close />
          </button>
        </div>
      ))}
      <button type="button" className="btn sm ghost" onClick={() => onChange([...rows, { name: "", sort: null }])} disabled={rows.length >= 20}>
        <Plus /> Add author
      </button>
      <span className="hint">The second box is the sort name; leave it empty to file under the surname.</span>
      {error && <span className="field-error" id={`${id}-err`}>{error}</span>}
    </fieldset>
  );
}

export const CONTRIBUTOR_ROLES: [ContributorRole, string][] = [["translator", "Translator"], ["editor", "Editor"], ["illustrator", "Illustrator"], ["foreword", "Foreword by"]];
export const MAX_CONTRIBUTORS = 20;

/** Translators, editors, illustrators, and foreword writers: a name and a role per row. */
export function ContributorsField({ value, onChange, error }: { value: Contributor[]; onChange: (v: Contributor[]) => void; error?: string }) {
  const id = useId();
  const set = (i: number, patch: Partial<Contributor>) => onChange(value.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  return (
    <fieldset className="field contributors span-2" aria-describedby={error ? `${id}-err` : undefined}>
      <legend>Contributors</legend>
      {value.map((c, i) => (
        <div key={i} className="author-row contributor-row">
          <label className="sr-only" htmlFor={`${id}-n${i}`}>Contributor {i + 1} name</label>
          <input id={`${id}-n${i}`} className="input" value={c.name} placeholder="Name" maxLength={256} onChange={(e) => set(i, { name: e.target.value })} aria-invalid={!!error && !c.name.trim()} />
          <label className="sr-only" htmlFor={`${id}-r${i}`}>Contributor {i + 1} role</label>
          <select id={`${id}-r${i}`} className="select" value={c.role} onChange={(e) => set(i, { role: e.target.value as ContributorRole })}>
            {CONTRIBUTOR_ROLES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <button type="button" className="icon-btn ghost sm" aria-label={`Remove contributor ${c.name || i + 1}`} onClick={() => onChange(value.filter((_, j) => j !== i))}>
            <Close />
          </button>
        </div>
      ))}
      <button type="button" className="btn sm ghost" onClick={() => onChange([...value, { name: "", role: "translator" }])} disabled={value.length >= MAX_CONTRIBUTORS}>
        <Plus /> Add contributor
      </button>
      {value.length >= MAX_CONTRIBUTORS && <span className="hint">At most {MAX_CONTRIBUTORS} contributors.</span>}
      {error && <span className="field-error" id={`${id}-err`}>{error}</span>}
    </fieldset>
  );
}

/** Every descriptive field; shared by review-before-filing and the work editor. */
export function MetadataForm({ value, onChange, errors = {}, detected }: { value: Metadata; onChange: (m: Metadata) => void; errors?: FieldErrors; detected?: Metadata | null }) {
  const id = useId();
  const set = <K extends keyof Metadata>(k: K, v: Metadata[K]) => onChange({ ...value, [k]: v });
  const text = (k: "title" | "sort_title" | "series" | "language" | "isbn" | "publisher" | "published" | "subtitle" | "original_published", opts: { placeholder?: string; max?: number; mode?: "numeric" | "text"; wide?: boolean } = {}) => (
    <label className={`field${opts.wide ? " span-2" : ""}`}>
      <span>{FIELD_LABEL[k]}</span>
      <input
        className="input"
        value={(value[k] as string | null) ?? ""}
        onChange={(e) => set(k, (k === "title" ? e.target.value : e.target.value || null) as Metadata[typeof k])}
        placeholder={opts.placeholder}
        maxLength={opts.max ?? 256}
        inputMode={opts.mode}
        required={k === "title"}
        aria-invalid={!!errors[k]}
        aria-describedby={errors[k] ? `${id}-${k}` : undefined}
        data-autofocus={k === "title" ? true : undefined}
      />
      {errors[k] && <span className="field-error" id={`${id}-${k}`}>{errors[k]}</span>}
      {detected && k !== "sort_title" && detected[k] && detected[k] !== value[k] && (
        <button type="button" className="linkish" onClick={() => set(k, detected[k] as Metadata[typeof k])}>Detected: {String(detected[k])}</button>
      )}
    </label>
  );
  return (
    <div className="meta-form">
      {text("title", { max: 512, wide: true })}
      {text("subtitle", { max: 512, wide: true })}
      {text("sort_title", { placeholder: value.title.replace(/^(the|a|an)\s+(.*)$/i, "$2, $1") || "Same as title", max: 512, wide: true })}
      <AuthorsField value={value.authors} onChange={(a) => set("authors", a)} error={errors.authors} />
      <ContributorsField value={value.contributors ?? []} onChange={(c) => set("contributors", c)} error={errors.contributors} />
      <div className="field-pair span-2">
        {text("series", { placeholder: "e.g. Earthsea" })}
        <label className="field index">
          <span>{FIELD_LABEL.series_index}</span>
          <input
            className="input"
            type="number"
            step="any"
            min={0}
            value={value.series_index ?? ""}
            onChange={(e) => set("series_index", e.target.value === "" ? null : Number(e.target.value))}
            disabled={!value.series}
            aria-invalid={!!errors.series_index}
          />
          {errors.series_index && <span className="field-error">{errors.series_index}</span>}
        </label>
      </div>
      <label className="field span-2">
        <span>Description</span>
        <textarea className="input" rows={5} value={value.description ?? ""} maxLength={20000} onChange={(e) => set("description", e.target.value || null)} />
      </label>
      {text("language", { placeholder: "en", max: 16 })}
      {text("isbn", { placeholder: "978…", max: 20, mode: "numeric" })}
      {text("publisher")}
      {text("published", { placeholder: "YYYY or YYYY-MM-DD", max: 32 })}
      {text("original_published", { placeholder: "YYYY or YYYY-MM-DD", max: 32 })}
      <label className="field span-2" htmlFor={`${id}-tags`}>
        <span>Tags and genres</span>
        <ChipInput id={`${id}-tags`} label="Tag" values={value.tags} onChange={(v) => set("tags", v)} placeholder="Fantasy, Classics… (Enter to add)" />
      </label>
      <label className="field span-2" htmlFor={`${id}-narr`}>
        <span>Narrators</span>
        <ChipInput id={`${id}-narr`} label="Narrator" values={value.narrators} onChange={(v) => set("narrators", v)} placeholder="For the audiobook (Enter to add)" />
      </label>
    </div>
  );
}
