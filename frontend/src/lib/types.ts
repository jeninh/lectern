export type Role = "owner" | "member" | "reader";

export interface User {
  id: number;
  username: string;
  display_name: string;
  role: Role;
  totp_enabled: boolean;
  is_active: boolean;
  created_at: string | null;
  last_login_at: string | null;
}

export interface Me {
  user: User;
  csrf_token: string;
  base_url: string;
  passkeys_possible: boolean;
  opds_url: string;
  sync_url: string;
  inbox_path: string;
  counts: { works: number; users: number };
  preferences: Preferences;
}

export interface Preferences {
  show_technical_details: boolean;
}

export interface Progress {
  document: string;
  progress: string;
  percentage: number;
  device: string;
  device_id: string;
  timestamp: number;
  updated_at: string | null;
  sync_key_id: number | null;
  sync_key_label: string | null;
}

export interface BookFile {
  id: number;
  kind: "ebook" | "audio";
  rel_path: string;
  media_type: string;
  size: number;
  sha256: string;
  partial_md5: string | null;
  download_url: string;
  added_at: string | null;
  progress?: Progress | null;
}

export interface Work {
  id: number;
  title: string;
  author: string;
  sort_title: string;
  sort_author: string;
  language: string | null;
  isbn: string | null;
  description: string | null;
  publisher: string | null;
  published: string | null;
  dir_path: string;
  has_cover: boolean;
  cover_url: string | null;
  cover_tint: string | null;
  created_at: string | null;
  updated_at: string | null;
  files: BookFile[];
  progress?: Progress | null;
  progress_file_id?: number;
}

export interface SyncKey {
  id: number;
  label: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

export type AppPassword = SyncKey;

export interface Passkey {
  id: number;
  label: string;
  backed_up: boolean;
  transports: string | null;
  created_at: string | null;
  last_used_at: string | null;
}

export interface SessionInfo {
  id: number;
  created_at: string | null;
  expires_at: string | null;
  ip: string | null;
  user_agent: string | null;
  current: boolean;
}

export interface SecurityOverview {
  passkeys: Passkey[];
  passkeys_possible: boolean;
  totp_enabled: boolean;
  has_password: boolean;
  sessions: SessionInfo[];
}

export interface ActivityEvent {
  id: string;
  kind: "audit" | "sync";
  action: string;
  at: string | null;
  actor: string | null;
  target: string | null;
  detail?: string | null;
  ip?: string | null;
  work_id?: number;
  device?: string | null;
  sync_key_label?: string | null;
  percentage?: number;
}

export interface SyncKeyStatus {
  sync_key: SyncKey;
  connected: boolean;
  latest_sync: {
    work_id: number;
    title: string;
    author: string;
    percentage: number;
    device: string;
    at: string | null;
  } | null;
}

/** A KOReader sync key and/or OPDS app password issued together. */
export interface Device {
  id: string;
  label: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  sync_key: SyncKey | null;
  app_password: AppPassword | null;
}

export interface CreatedDevice {
  device: Device;
  username: string;
  sync_secret: string;
  opds_secret: string;
}

// --- Read-along (docs/contracts.md: tracks, sentences, progress, read-aloud EPUB) ---------

export interface AlignmentSummary {
  status: "pending" | "running" | "succeeded" | "failed" | "stale";
  model: string;
  coverage: number | null;
}

export interface Track {
  id: number;
  position: number;
  media_type: string;
  codec: string;
  duration_s: number;
  offset_s: number;
  size: number;
  sha256: string;
  chapters: { title: string; start_s: number }[];
  stream_url: string;
}

export interface TracksResponse {
  work_id: number;
  audio_fingerprint: string | null;
  total_duration_s: number;
  alignment: AlignmentSummary | null;
  tracks: Track[];
}

export interface SentenceInfo {
  n: number;
  spine_index: number;
  char_start: number;
  char_end: number;
  audio_start_s: number | null;
  audio_end_s: number | null;
  matched: boolean;
}

export interface SentencesResponse {
  work_id: number;
  count: number;
  alignment: AlignmentSummary | null;
  sentences: SentenceInfo[];
}

export interface TextBlock {
  kind: "p" | "li" | "quote" | "pre" | "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
  /** Offset (code points) of the block's first character in the spine item's normalised text. */
  start: number;
  text: string;
}

export interface SpineTextResponse {
  work_id: number;
  spine_index: number;
  length: number;
  blocks: TextBlock[];
}

export interface WorkProgressResponse {
  work_id: number;
  position: { sentence_n: number; offset: number } | null;
  source: "koreader" | "web" | "audio" | null;
  raw: string | null;
  device: string | null;
  received_at: string | null;
  text: { spine_index: number; char: number } | null;
  audio_s: number | null;
  percent: number | null;
}

export type ReadAloudState = "unavailable" | "not_generated" | "generating" | "ready" | "failed";

export interface ReadAloudStatus {
  work_id: number;
  status: ReadAloudState;
  reason: "no_alignment" | "alignment_stale" | "no_audio" | "no_ebook" | "missing_file" | null;
  message: string | null;
  readalong: boolean;
  alignment: AlignmentSummary | null;
  download_url: string | null;
  size: number | null;
  generated_at: string | null;
  duration_s: number | null;
}
