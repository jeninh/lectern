// The work editor's draft: a plain copy of the editable state, compared with what the server
// last sent to decide what is unsaved and to build one minimal PATCH (docs/contracts.md "Editing").

import { changedFields, clean } from "./metadata";
import type { EditorState, Metadata, PlaybackDefault } from "./types";

export interface TrackDraft {
  id: number;
  title: string;
  chapters: string[];
}

export interface CoverDraft {
  action: "upload" | "embedded" | "remove";
  token?: string;
  preview?: string;
}

export interface Draft {
  metadata: Metadata;
  shelves: number[];
  primary_file_id: number | null;
  playback_default: PlaybackDefault;
  readalong_enabled: boolean;
  tracks: TrackDraft[];
  remove_file_ids: number[];
  remove_track_ids: number[];
  cover: CoverDraft | null;
}

export function initialDraft(s: EditorState): Draft {
  return {
    metadata: s.metadata,
    shelves: s.shelves.filter((x) => x.member).map((x) => x.id).sort((a, b) => a - b),
    primary_file_id: s.primary_file_id,
    playback_default: s.playback_default,
    readalong_enabled: s.readalong_enabled,
    tracks: s.tracks.map((t) => ({ id: t.id, title: t.title, chapters: t.chapters.map((c) => c.title) })),
    remove_file_ids: [],
    remove_track_ids: [],
    cover: null,
  };
}

export interface WorkPatchBody {
  version: number;
  metadata?: Metadata;
  shelves?: number[];
  primary_file_id?: number | null;
  playback_default?: PlaybackDefault;
  readalong_enabled?: boolean;
  tracks?: { id: number; title?: string; chapters?: string[] }[];
  remove_file_ids?: number[];
  remove_track_ids?: number[];
  cover?: { action: CoverDraft["action"]; token?: string };
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Human names for what the draft changes, for the save bar and the discard prompt. */
export function changes(s: EditorState, d: Draft): string[] {
  const base = initialDraft(s);
  const out: string[] = changedFields(clean(base.metadata), clean(d.metadata)).map((k) => k.replace("_", " "));
  if (!same(base.shelves, [...d.shelves].sort((a, b) => a - b))) out.push("shelves");
  if (d.primary_file_id !== base.primary_file_id) out.push("primary EPUB");
  if (d.playback_default !== base.playback_default) out.push("playback");
  if (d.readalong_enabled !== base.readalong_enabled) out.push("read-along");
  const kept = d.tracks.filter((t) => !d.remove_track_ids.includes(t.id));
  const baseKept = base.tracks.filter((t) => !d.remove_track_ids.includes(t.id));
  if (!same(kept.map((t) => t.id), baseKept.map((t) => t.id))) out.push("track order");
  if (kept.some((t) => base.tracks.find((b) => b.id === t.id)?.title !== t.title)) out.push("track titles");
  if (kept.some((t) => !same(base.tracks.find((b) => b.id === t.id)?.chapters, t.chapters))) out.push("chapter titles");
  if (d.remove_file_ids.length) out.push(`${d.remove_file_ids.length} EPUB${d.remove_file_ids.length > 1 ? "s" : ""} to trash`);
  if (d.remove_track_ids.length) out.push(`${d.remove_track_ids.length} track${d.remove_track_ids.length > 1 ? "s" : ""} to trash`);
  if (d.cover) out.push("cover");
  return out;
}

/** The smallest PATCH that turns the server's state into the draft; untouched parts are omitted. */
export function buildPatch(s: EditorState, d: Draft): WorkPatchBody {
  const base = initialDraft(s);
  const body: WorkPatchBody = { version: s.version };
  const meta = clean(d.metadata);
  if (changedFields(clean(base.metadata), meta).length) body.metadata = meta;
  const shelves = [...d.shelves].sort((a, b) => a - b);
  if (!same(base.shelves, shelves)) body.shelves = shelves;
  if (d.primary_file_id !== base.primary_file_id && !d.remove_file_ids.includes(d.primary_file_id ?? -1)) body.primary_file_id = d.primary_file_id;
  if (d.playback_default !== base.playback_default) body.playback_default = d.playback_default;
  if (d.readalong_enabled !== base.readalong_enabled) body.readalong_enabled = d.readalong_enabled;
  const kept = d.tracks.filter((t) => !d.remove_track_ids.includes(t.id));
  const baseById = new Map(base.tracks.map((t) => [t.id, t]));
  const reordered = !same(kept.map((t) => t.id), base.tracks.filter((t) => !d.remove_track_ids.includes(t.id)).map((t) => t.id));
  const edited = kept.some((t) => baseById.get(t.id)?.title !== t.title || !same(baseById.get(t.id)?.chapters, t.chapters));
  if (reordered || edited) {
    body.tracks = kept.map((t) => {
      const b = baseById.get(t.id);
      const e: { id: number; title?: string; chapters?: string[] } = { id: t.id };
      if (b?.title !== t.title && t.title.trim()) e.title = t.title.trim();
      if (!same(b?.chapters, t.chapters)) e.chapters = t.chapters.map((c, i) => c.trim() || b?.chapters[i] || `Chapter ${i + 1}`);
      return e;
    });
  }
  if (d.remove_file_ids.length) body.remove_file_ids = d.remove_file_ids;
  if (d.remove_track_ids.length) body.remove_track_ids = d.remove_track_ids;
  if (d.cover) body.cover = d.cover.action === "upload" ? { action: "upload", token: d.cover.token } : { action: d.cover.action };
  return body;
}

export function isDirty(s: EditorState, d: Draft): boolean {
  return Object.keys(buildPatch(s, d)).length > 1;
}

/** After someone else saved: keep only the person's own edits, on top of the newer state. */
export function rebase(oldBase: EditorState, fresh: EditorState, d: Draft): Draft {
  const mine = initialDraft(oldBase);
  const theirs = initialDraft(fresh);
  const meta = { ...theirs.metadata } as Record<string, unknown>;
  for (const k of changedFields(clean(mine.metadata), clean(d.metadata))) meta[k] = d.metadata[k];
  const freshIds = new Set(fresh.tracks.map((t) => t.id));
  return {
    metadata: meta as unknown as Metadata,
    shelves: same(mine.shelves, d.shelves) ? theirs.shelves : d.shelves,
    primary_file_id: d.primary_file_id !== mine.primary_file_id ? d.primary_file_id : theirs.primary_file_id,
    playback_default: d.playback_default !== mine.playback_default ? d.playback_default : theirs.playback_default,
    readalong_enabled: d.readalong_enabled !== mine.readalong_enabled ? d.readalong_enabled : theirs.readalong_enabled,
    // Track edits survive only when nobody else changed the tracks in the meantime.
    tracks: same(mine.tracks, theirs.tracks) ? d.tracks : theirs.tracks,
    remove_file_ids: d.remove_file_ids.filter((id) => fresh.files.some((f) => f.id === id)),
    remove_track_ids: d.remove_track_ids.filter((id) => freshIds.has(id)),
    cover: d.cover,
  };
}
