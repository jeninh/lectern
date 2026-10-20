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
