// Admin → Dictionaries (issue #78): the offline dictionaries the reader looks words up in.
// Owner only. An upload is converted once on the server; nothing is ever downloaded.

import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { del, get, patch, postBlob } from "../lib/api";
import { type InstalledDictionary, languageName } from "../lib/dictionary";
import { bytes } from "../lib/format";
import { useStore } from "../lib/store";

export function DictionariesCard() {
  const { toast } = useStore();
  const [list, setList] = useState<InstalledDictionary[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [lang, setLang] = useState("en");
  const [to, setTo] = useState("");
  const [name, setName] = useState("");
  const file = useRef<HTMLInputElement>(null);
  const load = useCallback(() => get<{ dictionaries: InstalledDictionary[] }>("/api/admin/dictionaries").then((r) => setList(r.dictionaries), () => {}), []);
  useEffect(() => {
    load();
  }, [load]);

  const upload = async (e: React.FormEvent) => {
    e.preventDefault();
    const f = file.current?.files?.[0];
    if (!f) return;
    setBusy(true);
    try {
      const params = new URLSearchParams({ lang: lang.trim() });
      if (to.trim()) params.set("to", to.trim());
      if (name.trim()) params.set("name", name.trim());
      const r = await postBlob<{ dictionary: InstalledDictionary }>(`/api/admin/dictionaries?${params}`, f);
      toast(`Installed ${r.dictionary.name} (${r.dictionary.entries.toLocaleString()} entries).`);
      if (file.current) file.current.value = "";
      setName("");
      await load();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const flip = async (d: InstalledDictionary) => {
    try {
      await patch(`/api/admin/dictionaries/${d.id}`, { enabled: !d.enabled });
      await load();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };
  const remove = async (d: InstalledDictionary) => {
    if (!window.confirm(`Remove ${d.name}? Its files are deleted from the server.`)) return;
    try {
      await del(`/api/admin/dictionaries/${d.id}`);
      toast(`Removed ${d.name}.`);
      await load();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <section className="card" id="dictionaries" aria-labelledby="dict-h" style={{ marginTop: 18 }}>
      <h2 id="dict-h">Dictionaries</h2>
      <p className="sub">
        Look up words in the reader from dictionaries stored on this server; nothing is looked up on the internet. Install StarDict dictionaries (a .zip or .tar.gz with the .ifo, .idx, and .dict files). Their licence files are kept with them. <Link to="/app/docs/dictionary">Which dictionaries, and licences</Link>
      </p>
      {list && list.length === 0 && <p className="hint">No dictionaries installed. Readers see how to add one when they look a word up.</p>}
      {list && list.length > 0 && (
        <ul className="dict-admin-list">
          {list.map((d) => (
            <li key={d.id}>
              <div>
                <strong>{d.name}</strong>
                <span className="hint">
                  {" "}{d.bilingual ? `${languageName(d.lang)} → ${languageName(d.to)}` : languageName(d.lang)} · {d.entries.toLocaleString()} entries · {bytes(d.size_bytes)}
                  {d.copyright ? ` · ${d.copyright}` : ""}
                </span>
              </div>
              <span className="row">
                <button type="button" role="switch" className="switch" aria-checked={d.enabled} aria-label={`${d.name} on`} onClick={() => flip(d)}><span aria-hidden="true" /></button>
                <button type="button" className="btn sm ghost" onClick={() => remove(d)}>Remove</button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <form className="dict-admin-form" onSubmit={upload}>
        <label className="field"><span>Dictionary file</span><input className="input sm" ref={file} type="file" accept=".zip,.tar,.gz,.tgz,.bz2,.xz" required disabled={busy} /></label>
        <label className="field"><span>Words in</span><input className="input sm" value={lang} onChange={(e) => setLang(e.target.value)} placeholder="en" size={6} required pattern="[A-Za-z]{2,3}([-_][A-Za-z0-9]{1,8})*" disabled={busy} /></label>
        <label className="field"><span>Definitions in</span><input className="input sm" value={to} onChange={(e) => setTo(e.target.value)} placeholder="same" size={6} pattern="[A-Za-z]{2,3}([-_][A-Za-z0-9]{1,8})*" disabled={busy} /></label>
        <label className="field"><span>Name</span><input className="input sm" value={name} onChange={(e) => setName(e.target.value)} placeholder="from the file" maxLength={120} disabled={busy} /></label>
        <button className="btn primary" type="submit" disabled={busy}>{busy ? "Installing…" : "Install"}</button>
      </form>
      <p className="hint">Or on the server: <code>lectern dictionary-import &lt;folder or archive&gt; --lang en</code>.</p>
    </section>
  );
}
