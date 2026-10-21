import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, csrf } from "../lib/api";
import {
  REASONS,
  audiobookState,
  cancelJob,
  duration,
  elevenLabsModels,
  elevenLabsPreview,
  elevenLabsVoices,
  estimateAudiobook,
  makeAudiobook,
  retryAudiobook,
  type AudiobookRequest,
  type AudiobookState,
  type EngineId,
  type Estimate,
  type Voice,
} from "../lib/audiobook";
import { useStore } from "../lib/store";
import type { Work } from "../lib/types";
import { Headphones, Play } from "./Icons";
import { Sheet } from "./Sheet";

const POLL_MS = 4000;

/**
 * "Make an audiobook" row for a text work without audio: opens the narration sheet, and shows
 * a running narration's progress, ETA, and Cancel, or a failed one's error and Retry.
 */
export function MakeAudiobookRow({ work }: { work: Work }) {
  const { refreshLibrary, toast } = useStore();
  const [st, setSt] = useState<AudiobookState | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await audiobookState(work.id);
      setSt((prev) => {
        if (prev?.job && prev.job.status !== "succeeded" && next.job?.status === "succeeded") {
          toast(`The audiobook of ${work.title} is ready.`);
          refreshLibrary().catch(() => {});
        }
        return next;
      });
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [work.id, work.title, refreshLibrary, toast]);

  useEffect(() => {
    refresh();
  }, [refresh]);
  const running = st?.job?.status === "queued" || st?.job?.status === "leased";
  useEffect(() => {
    if (!running) return;
    const id = window.setTimeout(refresh, POLL_MS);
    return () => window.clearTimeout(id);
  }, [running, st, refresh]);

  if (!st) return null;
  if (!st.eligible && st.reason !== null && !running) return null;
  const job = st.job;

  const act = async (fn: () => Promise<unknown>, fail: string) => {
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : fail);
    }
  };

  let hint = "Narrate the text with Piper, Kokoro, or ElevenLabs";
  let action = <button className="btn sm" onClick={() => setOpen(true)}>Make an audiobook…</button>;
  if (job && running) {
    const pct = job.progress != null ? Math.floor(job.progress * 100) : null;
    hint =
      job.status === "queued"
        ? job.attempts > 0 && job.error
          ? `Retrying after: ${job.error}`
          : job.engine === "kokoro" && !st.engines.find((e) => e.id === "kokoro")?.available
            ? "Queued: waiting for a worker with Kokoro"
            : "Queued"
        : `Narrating${pct != null ? ` · ${pct}%` : ""}${job.eta ? ` · about ${duration(job.eta.seconds)} left` : ""}${job.lease && job.lease.worker !== "local" ? ` on ${job.lease.worker}` : ""}`;
    if (job.engine === "elevenlabs" && job.spent_credits != null) hint += ` · ${Math.round(job.spent_credits)} of ${Math.round(job.cap_credits ?? 0)} credits`;
    action = <button className="btn sm ghost" onClick={() => act(() => cancelJob(job.id), "Could not cancel")}>Cancel</button>;
  } else if (job && (job.status === "failed" || job.status === "cancelled")) {
    hint = job.status === "cancelled" ? "Narration cancelled" : `Narration failed: ${job.error ?? "unknown error"}`;
    action = (
      <span className="row" style={{ gap: 4 }}>
        <button className="btn sm ghost" onClick={() => act(() => retryAudiobook(work.id), "Could not retry")}>Retry</button>
        <button className="btn sm ghost" onClick={() => setOpen(true)}>Change…</button>
      </span>
    );
  }

  return (
    <>
      <li className="slot" data-state={job?.status ?? "none"}>
        <Headphones />
        <span>
          <strong>Audiobook</strong>{" "}
          <span className="hint" aria-live="polite">{hint}{error && ` · ${error}`}</span>
          {running && job?.progress != null && (
            <span className="progress-bar" role="progressbar" aria-label="Narration progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.floor(job.progress * 100)}>
              <span style={{ width: `${job.progress * 100}%` }} />
            </span>
          )}
        </span>
        {action}
      </li>
      {open && st.chapters && (
        <MakeAudiobookSheet work={work} state={st} onClose={() => setOpen(false)} onQueued={async () => { setOpen(false); await refresh(); }} />
      )}
    </>
  );
}

function MakeAudiobookSheet({ work, state, onClose, onQueued }: { work: Work; state: AudiobookState; onClose: () => void; onQueued: () => void }) {
  const { me, toast } = useStore();
  const owner = me?.user.role === "owner";
  const usable = state.engines.filter((e) => e.available || e.queues_without_runner).filter((e) => !e.owner_only || owner);
  const [engine, setEngine] = useState<EngineId>(state.recommended ?? usable[0]?.id ?? "kokoro");
  const eng = state.engines.find((e) => e.id === engine)!;
  const [elVoices, setElVoices] = useState<Voice[] | null>(null);
  const [elModels, setElModels] = useState<{ id: string; name: string }[] | null>(null);
  const voices = engine === "elevenlabs" ? elVoices ?? [] : eng.voices ?? [];
  const [voice, setVoice] = useState<string>(eng.default_voice ?? "");
  const [speed, setSpeed] = useState(1);
  const [model, setModel] = useState<string>("");
  const [stability, setStability] = useState(0.5);
  const [similarity, setSimilarity] = useState(0.75);
  const all = state.chapters!;
  const [chosen, setChosen] = useState<Set<number>>(() => new Set(all.map((c) => c.spine_index)));
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [cap, setCap] = useState<number>(0);
  const [agree, setAgree] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sample, setSample] = useState<string | null>(null);
  const audio = useRef<HTMLAudioElement>(null);

  useEffect(() => {
    setVoice(eng.default_voice ?? "");
    setEstimate(null);
    setAgree(false);
    if (engine === "elevenlabs" && elVoices === null) {
      Promise.all([elevenLabsVoices(), elevenLabsModels()])
        .then(([v, m]) => {
          setElVoices(v.voices);
          setElModels(m.models);
          setModel(m.default);
          if (v.voices[0]) setVoice(v.voices[0].id);
        })
        .catch((e) => setError((e as Error).message));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engine]);

  const request = useMemo<AudiobookRequest>(() => {
    const selected = all.filter((c) => chosen.has(c.spine_index)).map((c) => c.spine_index);
    const req: AudiobookRequest = { engine, voice, speed, chapters: selected.length === all.length ? null : selected };
    if (engine === "elevenlabs") {
      req.model = model || undefined;
      req.voice_settings = { stability, similarity_boost: similarity };
    }
    return req;
  }, [all, chosen, engine, voice, speed, model, stability, similarity]);

  // Any change to what would be narrated invalidates an earlier estimate and agreement.
  useEffect(() => {
    setEstimate(null);
    setAgree(false);
  }, [request]);

  const selectedChars = all.filter((c) => chosen.has(c.spine_index)).reduce((n, c) => n + c.characters, 0);
  const isEl = engine === "elevenlabs";

  const runEstimate = async () => {
    setBusy(true);
    setError(null);
    try {
      const est = await estimateAudiobook(work.id, request);
      setEstimate(est);
      if (est.elevenlabs) setCap(est.elevenlabs.suggested_cap);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const playSample = async () => {
    setBusy(true);
    setError(null);
    try {
      let url: string;
      if (isEl) {
        url = await elevenLabsPreview({ voice, model: model || undefined, speed, work_id: work.id });
      } else {
        const res = await fetch(`/api/works/${work.id}/audiobook/preview`, {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json", "x-csrf-token": csrf() },
          body: JSON.stringify({ engine, voice, speed }),
        });
        if (!res.ok) throw new Error((await res.json().catch(() => null))?.message ?? "This voice cannot be previewed on the server");
        url = URL.createObjectURL(await res.blob());
      }
      if (sample) URL.revokeObjectURL(sample);
      setSample(url);
      window.setTimeout(() => audio.current?.play().catch(() => {}), 0);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const req = { ...request };
      if (isEl && estimate?.elevenlabs) {
        req.confirm = { characters: estimate.characters, credits: estimate.elevenlabs.credits, cap_credits: cap, send_text: agree };
      }
      await makeAudiobook(work.id, req);
      toast(`Narration of ${work.title} queued.`);
      onQueued();
    } catch (e) {
      if (e instanceof ApiError && e.code === "estimate_changed") setEstimate(null);
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const canPreview = isEl || (eng.available && eng.runs_on === "server");
  const canSubmit =
    !busy && voice && chosen.size > 0 &&
    (!isEl || (estimate?.elevenlabs && agree && cap >= estimate.elevenlabs.credits && estimate.elevenlabs.fits));

  return (
    <Sheet
      open
      onClose={onClose}
      title={`Make an audiobook of ${work.title}`}
      labelledBy="mkab-title"
      wide
      footer={
        <div className="row" style={{ justifyContent: "space-between", width: "100%" }}>
          <span className="hint">{chosen.size} of {all.length} chapters · {selectedChars.toLocaleString()} characters</span>
          <span className="row" style={{ gap: 8 }}>
            <button className="btn ghost" onClick={onClose}>Close</button>
            {isEl && !estimate ? (
              <button className="btn primary" onClick={runEstimate} disabled={busy || !voice || chosen.size === 0}>Estimate cost</button>
            ) : (
              <button className="btn primary" onClick={submit} disabled={!canSubmit}>{isEl ? "Narrate and spend credits" : "Make audiobook"}</button>
            )}
          </span>
        </div>
      }
    >
      <div className="stack" style={{ gap: 18, flexShrink: 0 }}>
        <p className="sub">
          The new audiobook is an ordinary edition of this book, one track per chapter. Read-along works right away: the timings come from the narration itself.
        </p>

        <fieldset className="stack" style={{ gap: 8, border: 0, padding: 0 }}>
          <legend className="detail-h">Voice engine</legend>
          {state.engines.filter((e) => !e.owner_only || owner).map((e) => (
            <label key={e.id} className="row" style={{ gap: 8, alignItems: "flex-start", flexWrap: "nowrap", opacity: e.available || e.queues_without_runner ? 1 : 0.55 }}>
              <input type="radio" name="engine" value={e.id} checked={engine === e.id} disabled={!(e.available || e.queues_without_runner)} onChange={() => setEngine(e.id)} />
              <span><strong>{e.name}</strong> <span className="hint">{e.note}</span></span>
            </label>
          ))}
        </fieldset>

        {isEl && (
          <div className="callout warn" role="note" id="el-disclosure">
            <strong>The text is sent to ElevenLabs.</strong> {state.disclosure} Nothing is sent until you confirm below.
          </div>
        )}

        <div className="grid2" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <label className="field">
            <span>Voice</span>
            <select value={voice} onChange={(e) => setVoice(e.target.value)} disabled={!voices.length}>
              {!voices.length && <option value="">{isEl ? "Loading your voices…" : "No voices installed"}</option>}
              {voices.map((v) => <option key={v.id} value={v.id}>{v.name}{v.language ? ` (${v.language})` : ""}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Speed · {speed.toFixed(2)}×</span>
            <input type="range" min={isEl ? 0.7 : 0.5} max={isEl ? 1.2 : 2} step={0.05} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} />
          </label>
          {isEl && (
            <>
              <label className="field">
                <span>Model</span>
                <select value={model} onChange={(e) => setModel(e.target.value)}>
                  {(elModels ?? []).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                </select>
              </label>
              <span />
              <label className="field">
                <span>Stability · {stability.toFixed(2)}</span>
                <input type="range" min={0} max={1} step={0.05} value={stability} onChange={(e) => setStability(Number(e.target.value))} />
              </label>
              <label className="field">
                <span>Similarity · {similarity.toFixed(2)}</span>
                <input type="range" min={0} max={1} step={0.05} value={similarity} onChange={(e) => setSimilarity(Number(e.target.value))} />
              </label>
            </>
          )}
        </div>

        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" onClick={playSample} disabled={busy || !voice || !canPreview} title={canPreview ? undefined : "Kokoro samples play once a narration runs on a worker"}>
            <Play /> Play a sample
          </button>
          <span className="hint">{isEl ? "A sample uses up to 300 credits." : canPreview ? "Made on this server." : "Samples need the engine on this server."}</span>
          {sample && <audio ref={audio} src={sample} controls style={{ height: 32 }} />}
        </div>

        <fieldset className="stack" style={{ gap: 4, border: 0, padding: 0 }}>
          <legend className="detail-h">
            Chapters{" "}
            <button type="button" className="btn sm ghost" onClick={() => setChosen(chosen.size === all.length ? new Set() : new Set(all.map((c) => c.spine_index)))}>
              {chosen.size === all.length ? "Select none" : "Select all"}
            </button>
          </legend>
          <ul className="list" style={{ maxHeight: 260, overflow: "auto", margin: 0, padding: 0, listStyle: "none" }}>
            {all.map((c) => (
              <li key={c.spine_index}>
                <label className="row" style={{ gap: 8, alignItems: "flex-start", flexWrap: "nowrap" }}>
                  <input
                    type="checkbox"
                    checked={chosen.has(c.spine_index)}
                    onChange={(e) => {
                      const next = new Set(chosen);
                      if (e.target.checked) next.add(c.spine_index);
                      else next.delete(c.spine_index);
                      setChosen(next);
                    }}
                  />
                  <span><strong>{c.title}</strong> <span className="hint">{c.characters.toLocaleString()} characters · {c.preview}</span></span>
                </label>
              </li>
            ))}
          </ul>
        </fieldset>

        {estimate && (
          <div className="card" style={{ padding: 12 }} aria-live="polite">
            <div>
              About <strong>{duration(estimate.audio_s)}</strong> of audio from {estimate.characters.toLocaleString()} characters
              {estimate.eta ? `, ready in about ${duration(estimate.eta.seconds)}` : ""}.
            </div>
            {estimate.elevenlabs && (
              <div className="stack" style={{ gap: 8, marginTop: 8 }}>
                <div>
                  Estimated <strong>{Math.round(estimate.elevenlabs.credits).toLocaleString()} credits</strong>
                  {estimate.elevenlabs.cost != null && ` (about ${estimate.elevenlabs.cost.toFixed(2)} at your price)`}
                  {" · "}{estimate.elevenlabs.quota.remaining.toLocaleString()} of {estimate.elevenlabs.quota.characters_limit.toLocaleString()} left on your {estimate.elevenlabs.quota.tier ?? ""} plan
                  {estimate.elevenlabs.quota.resets_at && `, resets ${new Date(estimate.elevenlabs.quota.resets_at).toLocaleDateString()}`}.
                </div>
                {!estimate.elevenlabs.fits && <div className="error" role="alert">Not enough credits left for this narration. Choose fewer chapters.</div>}
                <label className="field" style={{ maxWidth: 260 }}>
                  <span>Stop if this job would spend more than (credits)</span>
                  <input type="number" min={Math.ceil(estimate.elevenlabs.credits)} step={100} value={cap} onChange={(e) => setCap(Number(e.target.value))} />
                </label>
                <label className="row" style={{ gap: 8, alignItems: "flex-start", flexWrap: "nowrap" }}>
                  <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} aria-describedby="el-disclosure" />
                  <span>Send the text of these {estimate.chapters} chapters to ElevenLabs and spend up to {cap.toLocaleString()} credits.</span>
                </label>
              </div>
            )}
          </div>
        )}
        {error && <div className="error" role="alert">{error}</div>}
        {!state.eligible && state.reason && <div className="hint">{REASONS[state.reason] ?? state.reason}</div>}
      </div>
    </Sheet>
  );
}
