// Generated audiobooks ("Make an audiobook") and the ElevenLabs integration (docs/tts.md).

import { del, get, post, put, csrf } from "./api";

export type EngineId = "kokoro" | "piper" | "elevenlabs";

export interface Voice {
  id: string;
  name: string;
  language?: string | null;
  category?: string | null;
}

export interface Engine {
  id: EngineId;
  name: string;
  available: boolean;
  runs_on: "server" | "worker" | null;
  note: string;
  voices: Voice[] | null;
  default_voice: string | null;
  model: string;
  owner_only?: boolean;
  configured?: boolean;
  /** Kokoro can be queued with no runner yet: a worker picks it up later. */
  queues_without_runner: boolean;
}

export interface ChapterInfo {
  spine_index: number;
  title: string;
  sentences: number;
  characters: number;
  preview: string;
}

export interface TtsJob {
  id: number;
  status: "queued" | "leased" | "succeeded" | "failed" | "cancelled";
  engine: EngineId;
  voice: string;
  chapters: number;
  characters: number;
  attempts: number;
  max_attempts: number;
  progress: number | null;
  message: string | null;
  error: string | null;
  run_after: string | null;
  cap_credits: number | null;
  spent_credits: number | null;
  eta: { seconds: number; basis: string } | null;
  lease: { worker: string } | null;
  crashes?: number;
  waiting?: { summary: string } | null;
}

export interface AudiobookState {
  work_id: number;
  eligible: boolean;
  reason: string | null;
  chapters: ChapterInfo[] | null;
  engines: Engine[];
  recommended: EngineId | null;
  job: TtsJob | null;
  edition: { engine: string; voice: string; chapters: number; duration_s: number } | null;
  disclosure: string;
}

export interface AudiobookRequest {
  engine: EngineId;
  voice: string;
  speed: number;
  model?: string;
  voice_settings?: { stability?: number; similarity_boost?: number; style?: number };
  chapters?: number[] | null;
  confirm?: { characters: number; credits: number; cap_credits: number; send_text: boolean };
}

export interface Estimate {
  chapters: number;
  sentences: number;
  characters: number;
  audio_s: number;
  eta: { seconds: number; basis: string } | null;
  elevenlabs: {
    model: string;
    credits: number;
    credits_per_character: number;
    quota: { tier: string | null; characters_limit: number; remaining: number; resets_at: string | null };
    fits: boolean;
    suggested_cap: number;
    cost: number | null;
  } | null;
}

export interface ElevenLabsStatus {
  configured: boolean;
  undecryptable: boolean;
  key_hint: string | null;
  last_tested_at: string | null;
  last_test_ok: boolean | null;
  last_test_detail: string | null;
  last_used_at: string | null;
  endpoint: string;
  usage: { days: number; characters: number; credits: number };
}

export const audiobookState = (workId: number) => get<AudiobookState>(`/api/works/${workId}/audiobook`);
export const estimateAudiobook = (workId: number, req: AudiobookRequest) =>
  post<Estimate>(`/api/works/${workId}/audiobook/estimate`, req);
export const makeAudiobook = (workId: number, req: AudiobookRequest) => post<TtsJob>(`/api/works/${workId}/audiobook`, req);
export const retryAudiobook = (workId: number) => post<TtsJob>(`/api/works/${workId}/audiobook/retry`);
export const cancelJob = (jobId: number) => post<TtsJob>(`/api/jobs/${jobId}/cancel`);

export const elevenLabsStatus = () => get<ElevenLabsStatus>("/api/integrations/elevenlabs");
export const saveElevenLabsKey = (api_key: string) => put<ElevenLabsStatus>("/api/integrations/elevenlabs", { api_key });
export const testElevenLabs = () => post<ElevenLabsStatus>("/api/integrations/elevenlabs/test");
export const revokeElevenLabs = () => del<ElevenLabsStatus>("/api/integrations/elevenlabs");
export const elevenLabsVoices = () => get<{ voices: Voice[] }>("/api/integrations/elevenlabs/voices");
export const elevenLabsModels = () =>
  get<{ models: { id: string; name: string; credits_per_character: number }[]; default: string }>("/api/integrations/elevenlabs/models");

/** A short spoken sample (billed to the owner's ElevenLabs account); returns an object URL. */
export async function elevenLabsPreview(body: { voice: string; model?: string; speed: number; work_id?: number }): Promise<string> {
  const res = await fetch("/api/integrations/elevenlabs/preview", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json", "x-csrf-token": csrf() },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => null);
    throw new Error(d?.message || d?.detail || `Preview failed (${res.status})`);
  }
  return URL.createObjectURL(await res.blob());
}

export function duration(s: number): string {
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${Math.max(1, m)} min`;
}

export const REASONS: Record<string, string> = {
  needs_epub: "Needs an EPUB edition: narration reads the EPUB's text.",
  has_audio: "This book already has an audiobook.",
  not_allowed: "Only owners and members can make audiobooks.",
  unreadable_epub: "The EPUB could not be read.",
};
