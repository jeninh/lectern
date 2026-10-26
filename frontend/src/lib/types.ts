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
  library_settings: LibrarySettings;
  /** Owners and members add, edit, and trash books; readers never do. */
  can_edit: boolean;
  /** Server clock now, and until when the installed app may keep downloads and this identity
   * without reaching the server (docs/offline.md#privacy). */
  offline?: { now: string; until: string };
  /** Offline mode (docs/offline-mode.md): the server makes no internet connections. */
  offline_mode?: boolean;
}

export interface OfflineFeature {
  id: string;
  name: string;
  contacts: string[];
  /** Set up to contact the internet (it would, with offline mode off). */
  on: boolean;
  /** On, and refused because offline mode is on. */
  blocked: boolean;
  when: string;
}

export interface OfflineModeView {
  on: boolean;
  /** LECTERN_OFFLINE=1: Admin cannot turn it off. */
  forced: boolean;
  features: OfflineFeature[];
  models: {
    align: { local: boolean; model: string; ready: boolean; downloaded: string[] };
    kokoro: { local: boolean; ready: boolean };
    piper: { installed: boolean; voices: string[] };
  };
  prepare: { running: boolean; ok: boolean | null; started_at: string | null; finished_at: string | null; log: string[] };
}

export interface BackupView {
  config: { enabled: boolean; include_library: boolean; retention: number; hour_utc: number };
  status: {
    last_attempt_at?: string;
    last_success_at?: string;
    last_result?: "ok" | "failed";
    last_error?: string | null;
    last_error_at?: string;
    last_location?: string;
    last_size_bytes?: number | null;
    last_duration_s?: number;
    last_skipped_files?: number;
    last_skipped_examples?: string[];
  };
  running: boolean;
  target: {
    path: string;
    exists: boolean;
    writable: boolean;
    free_bytes: number | null;
    same_disk_as_data: boolean | null;
    restic_available: boolean;
    restic_password_file: string;
  };
}

export interface Preferences {
  show_technical_details: boolean;
  /** File uploads whose proposed match is certain without asking. */
  auto_file_imports: boolean;
}

export interface LibrarySettings {
  /** Outbound metadata suggestions (Open Library, Google Books); off by default. */
  metadata_lookup_enabled: boolean;
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

export interface AuthorName {
  name: string;
  sort?: string | null;
}

export interface Work {
  id: number;
  title: string;
  author: string;
  authors?: AuthorName[];
  series?: string | null;
  series_index?: number | null;
  tags?: string[];
  narrators?: string[];
  playback_default?: PlaybackDefault;
  readalong_enabled?: boolean;
  primary_file_id?: number | null;
  version?: number;
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
  /** No cover art: ``cover_url`` serves the server-generated cover (title and author drawn on it). */
  cover_generated?: boolean;
  created_at: string | null;
  updated_at: string | null;
  files: BookFile[];
  audio: AudioSummary | null;
  position: PositionSummary | null;
  progress?: Progress | null;
  progress_file_id?: number;
  /** /api/continue: which record is newer, KOReader's or the canonical one. */
  latest?: "koreader" | "canonical";
  /** /api/library: the visible shelves it is directly on; [] = the Library's root. */
  shelf_ids?: number[];
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
  /** Holds both credentials, so the KOReader plugin can be set up for it. */
  koplugin?: boolean;
  plugin?: PluginSetup | null;
}

/** The device's newest lectern.koplugin setup secret (never the secret itself). */
export interface PluginSetup {
  setup_state: "pending" | "used" | "expired" | "revoked";
  server_url: string;
  downloaded_at: string | null;
  expires_at: string | null;
  set_up_at: string | null;
}

export interface PluginDefaults {
  server_url: string | null;
  source: "config" | "tailscale" | "none";
  port: number;
}

export interface PairingCode {
  code: string;
  expires_at: string;
  attempts: number;
  username: string;
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
  error?: string | null;
  /** The latest align job, from its worker's heartbeats (read-aloud status only). */
  job?: AlignmentJob | null;
  /** Set while a job is queued and nothing (local engine or external worker) can run it. */
  unavailable?: { reason: string; message: string; docs_url: string } | null;
  /** Model a Retry of a failed alignment runs (smaller after running out of memory); owners and members only. */
  retry_model?: string | null;
}

export interface AlignmentJob {
  id: number;
  status: "queued" | "leased" | "succeeded" | "failed" | "cancelled";
  attempts: number;
  max_attempts: number;
  progress: number | null;
  message: string | null;
  heartbeat_at: string | null;
  run_after: string | null;
  eta_s: number | null;
  error: string | null;
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
  reason: "no_alignment" | "alignment_stale" | "no_audio" | "no_ebook" | "needs_epub" | "export_needs_epub" | "missing_file" | null;
  message: string | null;
  readalong: boolean;
  /** Where the work's text comes from (an EPUB, a text PDF, or nothing). */
  text_source?: "epub" | "pdf" | null;
  /** Every PDF of the work and its text state. */
  pdfs?: PdfTextState[];
  alignment: AlignmentSummary | null;
  download_url: string | null;
  size: number | null;
  generated_at: string | null;
  duration_s: number | null;
}

export interface Shelf {
  id: number;
  name: string;
  /** The parent shelf, or null at this person's top level (also when the parent is hidden from them). */
  parent_id: number | null;
  /** Names from this person's top level down to this shelf. */
  path: string[];
  owner: string | null;
  owner_id: number;
  permission: "owner" | "edit" | "view";
  /** Books directly on it that this person sees. */
  work_count: number;
  /** Sub-shelves directly in it. */
  shelf_count: number;
  grant_count: number;
  smart: boolean;
}

export interface ShelfGrant {
  user: { id: number; username: string; display_name: string; role: Role };
  permission: "view" | "edit";
  granted_by: string | null;
  created_at: string | null;
  /** Inherited grants: the shelf above this one that holds it. */
  shelf?: { id: number; name: string };
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
  import_id?: string | null;
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

// --- Add & edit books ------------------------------------------------------------------------

export type PlaybackDefault = "read" | "listen" | "readalong" | null;

/** Every editable descriptive field; the server validates the same schema. */
export interface Metadata {
  title: string;
  sort_title: string | null;
  authors: AuthorName[];
  series: string | null;
  series_index: number | null;
  description: string | null;
  language: string | null;
  isbn: string | null;
  publisher: string | null;
  published: string | null;
  tags: string[];
  narrators: string[];
}

export interface WorkSummary {
  id: number;
  title: string;
  author: string;
  series: string | null;
  series_index: number | null;
  isbn: string | null;
  cover_url: string | null;
  cover_tint: string | null;
  has_ebook: boolean;
  has_audio: boolean;
}

export interface Candidate {
  work: WorkSummary;
  score: number;
  certain: boolean;
  reason: string;
}

export interface Proposal {
  action: "join" | "new";
  work_id: number | null;
  confidence: "high" | "medium" | "low";
  score: number | null;
  reason: string;
  candidates: Candidate[];
  siblings: { id: string; label: string; title: string | null }[];
}

export interface ImportFileInfo {
  id: number;
  filename: string;
  kind: "ebook" | "audio";
  media_type: string;
  size: number;
  sha256: string;
  status: "ok" | "duplicate";
  duplicate_of: { work_id: number; title: string; in_trash: boolean } | null;
  duration_s: number | null;
  track_no: number | null;
  disc_no: number | null;
  chapters: number;
  /** "MOBI" or "AZW3" when the upload was converted to this EPUB. */
  converted_from?: string | null;
}

export interface ImportInfo {
  id: string;
  label: string;
  status: "uploading" | "ready" | "filed" | "discarded" | "failed";
  error: string | null;
  version: number;
  created_at: string | null;
  updated_at: string | null;
  expires_at: string | null;
  filed_at: string | null;
  auto_filed: boolean;
  files: ImportFileInfo[];
  uploads_active: number;
  detected: Metadata | null;
  metadata: Metadata | null;
  proposal: Proposal | null;
  target: { action: "join" | "new"; work_id?: number | null } | null;
  use_cover: boolean | null;
  cover_url: string | null;
  work: { id: number; title: string; author: string } | null;
}

export interface EditorTrack {
  id: number;
  position: number;
  title: string;
  rel_path: string;
  duration_s: number;
  size: number;
  codec: string;
  sha256: string;
  chapters: { title: string; start_s: number }[];
}

export interface EditorFile {
  id: number;
  kind: "ebook";
  rel_path: string;
  size: number;
  sha256: string;
  partial_md5: string | null;
  media_type: string;
  format: "EPUB" | "PDF" | "CBZ";
  added_at: string | null;
  primary: boolean;
}

export type CoverStyle = "classic" | "modern" | "minimal";

export interface EditorState {
  work: Work;
  version: number;
  metadata: Metadata;
  playback_default: PlaybackDefault;
  readalong_enabled: boolean;
  primary_file_id: number | null;
  cover: {
    has_cover: boolean;
    url: string | null;
    source: string | null;
    embedded_available: boolean;
    generated: { url: string | null; style: CoverStyle | null; seed: number; effective_style: CoverStyle; styles: CoverStyle[] };
  };
  files: EditorFile[];
  tracks: EditorTrack[];
  shelves: { id: number; name: string; path?: string[]; member: boolean }[];
  alignment: AlignmentInfo | null;
  align_models: string[];
  align_default_model: string;
  read_aloud: ReadAloudInfo;
  folder: { dir_path: string; pending: boolean };
  last_revision: { id: number; created_at: string | null; actor: string | null; changed: string[]; undoable: boolean } | null;
  lookup_enabled: boolean;
  /** Offline mode is on: suggestions cannot be fetched. */
  lookup_offline?: boolean;
}

export interface AlignmentInfo {
  status: string;
  model?: string | null;
  coverage?: number | null;
  [k: string]: unknown;
}

export interface ReadAloudInfo {
  work_id: number;
  status: string;
  reason: string | null;
  message: string | null;
  readalong: boolean;
  readalong_enabled?: boolean;
  download_url: string | null;
  size: number | null;
  generated_at: string | null;
  duration_s: number | null;
}

export interface LookupSuggestion {
  provider: "openlibrary" | "googlebooks";
  provider_name: string;
  source_url: string;
  fetched_at: string;
  fields: Partial<Metadata>;
}

export interface LookupResult {
  sent: { isbn?: string; title?: string; author?: string };
  suggestions: LookupSuggestion[];
  errors: { provider: string; error: string }[];
  cached: boolean;
}

export interface TrashList {
  retention_days: number;
  can_purge: boolean;
  works: { work: { id: number; title: string; author: string; has_cover: boolean }; trashed_at: string | null; trashed_by: string | null; purge_at: string | null; days_left: number; from: string | null }[];
  files: { id: number; kind: "ebook" | "audio"; name: string; size: number; work: { id: number; title: string; in_trash: boolean } | null; trashed_at: string | null; trashed_by: string | null; purge_at: string | null }[];
}

export interface BulkResult {
  batch_id: string;
  action: string;
  results: { work_id: number; ok: boolean; changed?: string[]; error?: string }[];
}

// --- Per-device queues (docs/device-queues.md) --------------------------------------------

/** An OPDS device (app password) the caller may send books to. */
export interface QueueDevice {
  id: number;
  /** The Devices page id of the paired device ("k12") or lone app password ("p3"). */
  device_id: string;
  label: string;
  own: boolean;
  user: { id: number; username: string; display_name: string };
  last_used_at: string | null;
  revoked: boolean;
  pending: number;
  /** With ?work_id=: that work is already waiting on this device. */
  queued?: boolean;
}

export type QueueStatus = "pending" | "delivered" | "removed";

export interface QueueItem {
  id: number;
  status: QueueStatus;
  position: number;
  queued_at: string | null;
  queued_by: string | null;
  delivered_at: string | null;
  removed_at: string | null;
  removed_by: string | null;
  work: Pick<Work, "id" | "title" | "author" | "has_cover" | "cover_url" | "cover_tint">;
}

export interface DeviceQueue {
  device: QueueDevice;
  pending: QueueItem[];
  history: QueueItem[];
}

export type QueueResultStatus = "queued" | "requeued" | "already_queued" | "not_found" | "not_visible_to_device" | "no_ebook";

export interface QueueResult {
  work_id: number;
  status: QueueResultStatus;
}

// --- Page-based books (PDF, CBZ): GET /api/files/:id/pages ----------------------------------

export interface PageProgress {
  page: number | null;
  percentage: number;
  device: string;
  from_web: boolean;
  updated_at: string | null;
}

export interface PagesInfo {
  file_id: number;
  work_id: number;
  title: string;
  media_type: string;
  format: "PDF" | "CBZ";
  /** Comics: the page count. PDFs: null (the PDF itself says). */
  pages: number | null;
  rtl: boolean;
  /** PDFs: where pdf.js loads the file. Comics: null (pages are images). */
  url: string | null;
  progress: PageProgress | null;
  /** PDFs: what was read from the text layer, and whether read-along works on the pages. */
  text?: PdfTextState;
  /** PDFs with a sentence map: a newer position from elsewhere (audio, EPUB, KOReader), as a page. */
  position?: { page: number; sentence_n: number; source: "web" | "audio" | "koreader"; device: string | null; updated_at: string | null } | null;
}

/** A PDF's text layer (docs/pdf-read-along.md): a text PDF ("ready"), a scan ("needs_ocr"), being read, or unreadable. */
export type PdfTextStatus = "pending" | "ready" | "needs_ocr" | "failed";

export interface PdfSection {
  title: string;
  page_start: number;
  page_end: number;
  from_outline: boolean;
  sentences: number;
  characters: number;
}

export interface PdfTextState {
  file_id: number;
  work_id: number;
  status: PdfTextStatus;
  pages: number | null;
  pages_with_text: number | null;
  sections: PdfSection[];
  sentences: number;
  error: string | null;
  /** Where the work's text comes from: its EPUB, this (or another) PDF, or nothing yet. */
  text_source: "epub" | "pdf" | null;
  /** "self": this PDF is the work's text; "epub": its sentences point at the EPUB's; null: no map. */
  map: "self" | "epub" | null;
  readalong: boolean;
}

/** A PDF sentence as highlight rectangles: [page (0-based), x0, y0, x1, y1] in PDF user space. */
export type PdfBox = [number, number, number, number, number];

export interface PdfSentenceBoxes {
  n: number;
  /** The work sentence (audio timeline) this PDF sentence is; null when the EPUB lacks it. */
  work_n: number | null;
  page: number;
  boxes: PdfBox[];
}

export interface PdfSentencesResponse {
  file_id: number;
  count: number;
  map: "self" | "epub";
  sentences: PdfSentenceBoxes[];
}
