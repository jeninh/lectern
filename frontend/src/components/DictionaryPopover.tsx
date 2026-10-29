// The reader's word lookup (issue #78): a small popover with the word's definitions from the
// offline dictionaries on the server, and translations from bilingual ones into the reader's
// chosen language. Opened by selecting a word or phrase (double-click, or long-press on a
// phone) in the EPUB reader, or by double-clicking a word on a PDF page.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { defaultTarget, type DictionaryEntry, languageName, lookupWord, type LookupResult, placePopover } from "../lib/dictionary";
import { useLocalStorage } from "../lib/hooks";
import { useStore } from "../lib/store";
import { type Place, saveBody, saveWord } from "../lib/vocabulary";
import { Close } from "./Icons";

export interface LookupRequest {
  query: string;
  /** The word on screen, in viewport pixels. */
  rect: { left: number; top: number; bottom: number; width: number };
  /** Where in the book the word is; goes only into the explicit Save, never the lookup. */
  place?: Place;
}

export function DictionaryPopover({ request, lang, onClose }: { request: LookupRequest; lang: string | null | undefined; onClose: () => void }) {
  const { me } = useStore();
  const [to, setTo] = useLocalStorage<string>("lectern.dictionary.to", defaultTarget());
  const [result, setResult] = useState<LookupResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setResult(null);
    setError(null);
    lookupWord(request.query, lang, to)
      .then((r) => !cancelled && setResult(r))
      .catch((e) => !cancelled && setError((e as Error).message));
    return () => {
      cancelled = true;
    };
  }, [request.query, lang, to]);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const p = placePopover(request.rect, { w: el.offsetWidth, h: el.offsetHeight }, { w: window.innerWidth, h: window.innerHeight });
    setPos({ left: p.left, top: p.top });
  }, [request, result, error]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    const onDown = (e: PointerEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) onClose();
    };
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("pointerdown", onDown, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("pointerdown", onDown, true);
    };
  }, [onClose]);

  const owner = me?.user.role === "owner";
  const nothing = result && !result.entries.length && !result.translations.length;
  return (
    <div
      ref={box}
      className="dict-pop"
      role="dialog"
      aria-label={`Dictionary: ${request.query}`}
      style={pos ? { left: pos.left, top: pos.top } : { visibility: "hidden", left: 0, top: 0 }}
    >
      <div className="dict-head">
        <strong lang={lang ?? undefined}>{request.query}</strong>
        <button className="icon-btn ghost sm" onClick={onClose} aria-label="Close dictionary"><Close /></button>
      </div>
      <div className="dict-body" aria-live="polite">
        {!result && !error && <p className="hint"><span className="spinner" aria-hidden="true" /> Looking it up…</p>}
        {error && <p className="hint" role="alert">{error}</p>}
        {result && !result.installed && (
          <p className="hint dict-empty">
            No dictionary is available on this server yet. {owner ? <>Add one in <Link to="/app/admin#dictionaries" onClick={onClose}>Admin → Dictionaries</Link>.</> : "Ask the owner to add one in Admin → Dictionaries."}{" "}
            <Link to="/app/docs/dictionary" onClick={onClose}>How it works</Link>
          </p>
        )}
        {result?.installed && result.dictionaries === 0 && (
          <p className="hint dict-empty">
            No dictionary for {languageName(result.lang) || "this book’s language"} is installed{result.languages.length ? ` (there are ${result.languages.map(languageName).join(", ")})` : ""}.{" "}
            <Link to="/app/docs/dictionary" onClick={onClose}>How to add one</Link>
          </p>
        )}
        {result?.installed && result.dictionaries > 0 && nothing && <p className="hint">Not found in {result.dictionaries === 1 ? "the dictionary" : `${result.dictionaries} dictionaries`}.</p>}
        {result?.entries.map((e, i) => <Entry key={`d${i}`} e={e} request={request} result={result} />)}
        {result && result.translations.length > 0 && <h3 className="dict-section">{languageName(result.to)}</h3>}
        {result?.translations.map((e, i) => <Entry key={`t${i}`} e={e} request={request} result={result} />)}
      </div>
      {result && result.targets.length > 0 && (
        <label className="dict-to">
          Translate into
          <select value={to} onChange={(e) => setTo(e.target.value)}>
            {Array.from(new Set([to, ...result.targets])).map((t) => <option key={t} value={t}>{languageName(t)}</option>)}
          </select>
        </label>
      )}
    </div>
  );
}

function Entry({ e, request, result }: { e: DictionaryEntry; request: LookupRequest; result: LookupResult }) {
  const [state, setState] = useState<{ kind: "idle" | "saving" } | { kind: "saved"; text: string } | { kind: "error"; text: string }>({ kind: "idle" });
  const save = async () => {
    setState({ kind: "saving" });
    try {
      const r = await saveWord(saveBody(e, request, result));
      setState({ kind: "saved", text: r.created ? "Saved" : `Saved again · seen ${r.word.seen_count}×` });
    } catch (err) {
      setState({ kind: "error", text: (err as Error).message });
    }
  };
  return (
    <section className="dict-entry">
      <p className="dict-word">
        <strong>{e.headword}</strong>
        {e.pronunciation && <span className="dict-pron"> {e.pronunciation}</span>}
        {e.pos && <em className="dict-pos"> {e.pos}</em>}
        {e.lemma && <span className="hint"> (for “{e.query}”)</span>}
      </p>
      <ol className="dict-defs">
        {e.definitions.map((d, i) => <li key={i}>{d}</li>)}
      </ol>
      {e.more > 0 && <p className="hint">and {e.more} more</p>}
      <p className="dict-src hint">{e.dictionary.name}</p>
      <div className="dict-save">
        <button type="button" className="btn ghost sm" onClick={() => void save()} disabled={state.kind === "saving"}>Save to vocabulary</button>
        {state.kind === "saved" && <span className="hint" role="status">{state.text}</span>}
        {state.kind === "error" && <span className="dict-save-err" role="alert">{state.text}</span>}
      </div>
    </section>
  );
}
