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
  download_cap_per_hour?: number | null;
}

export interface Me {
  user: User;
  csrf_token: string;
  base_url: string;
  passkeys_possible: boolean;
  opds_url: string;
  sync_url: string;
  inbox_path: string;
  counts: { works: number; users: number; unread_notifications: number };
  preferences: Preferences;
  upload: { max_bytes: number; chunk_bytes: number; max_active: number };
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
  audio: AudioSummary | null;
  position: PositionSummary | null;
  progress?: Progress | null;
  progress_file_id?: number;
  /** /api/continue: which record is newer, KOReader's or the canonical one. */
  latest?: "koreader" | "canonical";
}

export interface SyncKey {
  id: number;
  label: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface AppPassword extends SyncKey {
  download_cap_per_hour?: number | null;
}

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
  downloads: { used: number; cap: number; device_cap: number; burst_count: number; burst_window_s: number };
  known_devices: KnownDevice[];
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

/** Canonical position summary (web reader / audio player) on list items. */
export interface PositionSummary {
  fraction: number | null;
  source: "web" | "audio" | "koreader";
  device: string;
  received_at: string | null;
  audio_s: number | null;
}

export interface AudioSummary {
  tracks: number;
  duration_s: number;
}

export interface Chapter {
  title: string;
  start_s: number;
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
  chapters: Chapter[];
  stream_url: string;
  download_url: string;
  variants: { low: { codec: string; bitrate_kbps: number; size: number; stream_url: string } | null };
}

export interface TracksResponse {
  work_id: number;
  audio_fingerprint: string;
  total_duration_s: number;
  alignment: AlignmentSummary | null;
  tracks: Track[];
  transcode: { status: string; progress: number | null; total: number } | null;
}

export interface TextPosition {
  spine_index: number;
  char: number;
}

export interface HandoffCandidate {
  source: "web" | "audio" | "koreader";
  device: string;
  received_at: string | null;
  percent: number | null;
  raw: string | null;
  text: TextPosition | null;
  audio_s: number | null;
  estimated: boolean;
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
  source: "web" | "audio" | null;
  raw: string | null;
  device: string | null;
  received_at: string | null;
  text: TextPosition | null;
  audio_s: number | null;
  percent: number | null;
  estimated: boolean;
  count: number;
  total_duration_s: number;
  koreader: HandoffCandidate | null;
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

export interface Shelf {
  id: number;
  name: string;
  owner: string | null;
  owner_id: number;
  permission: "owner" | "edit" | "view";
  work_count: number;
  grant_count: number;
  smart: boolean;
}

export interface ShelfGrant {
  user: { id: number; username: string; display_name: string; role: Role };
  permission: "view" | "edit";
  granted_by: string | null;
  created_at: string | null;
}

export interface Invite {
  id: number;
  role: "member" | "reader";
  note: string;
  state: "pending" | "used" | "expired" | "revoked";
  created_by: string | null;
  created_at: string | null;
  expires_at: string | null;
  used_at: string | null;
  used_by: string | null;
  revoked_at: string | null;
  url?: string;
  token?: string;
}

export interface AppNotification {
  id: number;
  kind: string;
  severity: "info" | "warning";
  title: string;
  body: string;
  data: Record<string, unknown>;
  created_at: string | null;
  read_at: string | null;
}

export interface UploadSessionInfo {
  id: string;
  filename: string;
  media_type: string;
  size: number;
  chunk_size: number;
  received_bytes: number;
  status: "active" | "completed" | "cancelled" | "expired" | "failed";
  error: string | null;
  created_at: string | null;
  expires_at: string | null;
  inbox_name: string | null;
  user?: string;
}

export interface MatchSuggestion {
  id: number;
  score: number;
  reason: string;
  created_at: string | null;
  work: Work;
  candidate: Work;
}

export interface KnownDevice {
  id: number;
  label: string;
  first_seen_at: string | null;
  last_seen_at: string | null;
  last_ip: string | null;
}
