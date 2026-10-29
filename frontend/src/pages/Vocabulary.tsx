import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState } from "../components/EmptyState";
import { Book, Download } from "../components/Icons";
import { PageTitle } from "../components/Shell";
import { get } from "../lib/api";
import { useStore } from "../lib/store";
import { editWord, removeWord, vocabularyPath, type VocabularyWord } from "../lib/vocabulary";

const KEYS = ["work", "lang", "since", "until"] as const;
type Filters = Record<(typeof KEYS)[number], string>;

interface Listing {
  words: VocabularyWord[];
  total: number;
  facets: { languages: { lang: string; count: number }[]; works: { id: number; title: string; author: string | null; count: number }[] };
}

const filterQuery = (f: Filters) => KEYS.filter((k) => f[k]).map((k) => `${k}=${encodeURIComponent(f[k])}`).join("&");

/** Your saved words (docs/vocabulary.md): filtered by book, language, and date, edited in place,
 * and exported for Anki or as CSV with the same filters. The filters live in the URL. */
export function VocabularyPage() {
  const { works } = useStore();
  const [params, setParams] = useSearchParams();
  const filters = useMemo(() => Object.fromEntries(KEYS.map((k) => [k, params.get(k) ?? ""])) as Filters, [params]);
  const query = filterQuery(filters);
  const [data, setData] = useState<Listing | null>(null);
  const [words, setWords] = useState<VocabularyWord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(false);

  useEffect(() => {
    let live = true;
    get<Listing>(`/api/vocabulary?${query}${query ? "&" : ""}offset=0`)
      .then((d) => {
        if (!live) return;
        setData(d);
        setWords(d.words);
        setError(null);
      })
      .catch((e) => live && setError((e as Error).message));
    return () => {
      live = false;
    };
  }, [query]);

  const set = (next: Partial<Filters>) => setParams(filterQuery({ ...filters, ...next }), { replace: true });
  const clear = () => setParams(new URLSearchParams(), { replace: true });
  const loadMore = () => {
    setMore(true);
    get<Listing>(`/api/vocabulary?${query}${query ? "&" : ""}offset=${words.length}`)
      .then((d) => setWords((w) => [...w, ...d.words]))
      .catch((e) => setError((e as Error).message))
      .finally(() => setMore(false));
  };
  const remove = async (w: VocabularyWord) => {
    try {
      await removeWord(w.id);
      setWords((all) => all.filter((x) => x.id !== w.id));
      setData((d) => d && { ...d, total: d.total - 1 });
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const filtered = Boolean(query);
  const exportUrl = (format: "anki" | "csv") => `/api/vocabulary/export?format=${format}${query ? `&${query}` : ""}`;
  const facets = data?.facets;
  const empty = data && data.total === 0 && !filtered;

  return (
    <>
      <PageTitle sub="Words you saved from the dictionary">Vocabulary</PageTitle>
      {error && <p className="hint" role="alert">{error}</p>}
      {empty && (
        <EmptyState icon={<Book />} title="No saved words yet">
          Look a word up while you read and choose Save to vocabulary in the dictionary popover. It shows up here with the sentence it was in.
        </EmptyState>
      )}
      {data && !empty && (
        <div className="stack vocab">
          <div className="filter-bar" role="search" aria-label="Vocabulary">
            <label className="sr-only" htmlFor="vocab-work">Book</label>
            <select id="vocab-work" className="select sm" value={filters.work} onChange={(e) => set({ work: e.target.value })}>
              <option value="">All books</option>
              {facets?.works.map((w) => (
                <option key={w.id} value={String(w.id)}>{w.title} ({w.count})</option>
              ))}
            </select>
            <label className="sr-only" htmlFor="vocab-lang">Language</label>
            <select id="vocab-lang" className="select sm" value={filters.lang} onChange={(e) => set({ lang: e.target.value })}>
              <option value="">All languages</option>
              {facets?.languages.map((l) => (
                <option key={l.lang} value={l.lang}>{l.lang} ({l.count})</option>
              ))}
            </select>
            <div className="row" role="group" aria-label="Date saved">
              <label className="row">
                <span className="hint">From</span>
                <input className="input sm" type="date" value={filters.since} max={filters.until || undefined} onChange={(e) => set({ since: e.target.value })} />
              </label>
              <label className="row">
                <span className="hint">to</span>
                <input className="input sm" type="date" value={filters.until} min={filters.since || undefined} onChange={(e) => set({ until: e.target.value })} />
              </label>
            </div>
          </div>
          <div className="row vocab-head">
            <span className="hint" role="status">
              {data.total} {data.total === 1 ? "word" : "words"}
              {filtered && " match these filters"}
            </span>
            <span className="row">
              {filtered && <button className="btn sm ghost" onClick={clear}>Clear filters</button>}
              <a className="btn sm" href={exportUrl("anki")} download>
                <Download /> Export for Anki
              </a>
              <a className="btn sm" href={exportUrl("csv")} download>
                <Download /> Export CSV
              </a>
            </span>
          </div>
          {data.total === 0 && (
            <EmptyState icon={<Book />} title="No words match these filters" action={<button className="btn" onClick={clear}>Clear filters</button>}>
              Try another book, language, or a wider date range.
            </EmptyState>
          )}
          {words.length > 0 && (
            <ul className="vocab-list">
              {words.map((w) => (
                <WordItem
                  key={w.id}
                  word={w}
                  to={vocabularyPath(w, works.find((x) => x.id === w.book?.id)?.files)}
                  onSaved={(n) => setWords((all) => all.map((x) => (x.id === n.id ? n : x)))}
                  onDelete={() => remove(w)}
                />
              ))}
            </ul>
          )}
          {words.length < data.total && (
            <button className="btn sm" onClick={loadMore} disabled={more}>Show more</button>
          )}
        </div>
      )}
    </>
  );
}

function WordItem({ word: w, to, onSaved, onDelete }: { word: VocabularyWord; to: string | null; onSaved: (w: VocabularyWord) => void; onDelete: () => void }) {
  const [editing, setEditing] = useState(false);
  const [definition, setDefinition] = useState(w.definition);
  const [note, setNote] = useState(w.note ?? "");
  const [err, setErr] = useState<string | null>(null);

  const save = async () => {
    try {
      const r = await editWord(w.id, { definition, note: note.trim() || null });
      onSaved(r.word);
      setEditing(false);
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const cancel = () => {
    setDefinition(w.definition);
    setNote(w.note ?? "");
    setEditing(false);
    setErr(null);
  };

  return (
    <li className="vocab-item">
      <div className="vocab-top">
        <strong className="vocab-word">{w.word}</strong>
        {w.lemma && w.lemma !== w.word && <span className="hint">{w.lemma}</span>}
        <span className="hint">{w.pos ? `${w.pos} · ` : ""}{w.to ? `${w.lang} → ${w.to}` : w.lang}</span>
        <span className="badge">seen {w.seen_count}×</span>
      </div>
      {editing ? (
        <div className="vocab-edit stack">
          <label className="field">
            <span className="hint">Definition</span>
            <textarea className="input" rows={3} value={definition} onChange={(e) => setDefinition(e.target.value)} />
          </label>
          <label className="field">
            <span className="hint">Note</span>
            <textarea className="input" rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          {err && <p className="hint" role="alert">{err}</p>}
          <span className="row">
            <button className="btn sm" onClick={save} disabled={!definition.trim()}>Save</button>
            <button className="btn sm ghost" onClick={cancel}>Cancel</button>
          </span>
        </div>
      ) : (
        <>
          <p className="vocab-def">{w.definition}</p>
          {w.note && <p className="vocab-note"><strong>Note:</strong> {w.note}</p>}
        </>
      )}
      {w.context && <blockquote className="vocab-context">{w.context}</blockquote>}
      <div className="hint vocab-meta">
        {w.book && <Link to={`/app/library/${w.book.id}`}>{w.book.title}</Link>}
        <span>Saved {w.created_at.slice(0, 10)}</span>
        {to && <Link className="btn sm ghost" to={to}>Open in book</Link>}
        {!editing && <button className="btn sm ghost" onClick={() => setEditing(true)}>Edit</button>}
        <button className="btn sm ghost" onClick={onDelete}>Delete</button>
      </div>
    </li>
  );
}
