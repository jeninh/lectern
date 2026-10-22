// Signing out when the server may be out of reach (docs/offline.md#signing-out).
//
// The device's downloads, read-along data, and queued positions are deleted at once. The
// server session ends only when /logout reaches the server, so until then this device keeps a
// mark (with the session's CSRF token, which /logout needs) and the app shows nothing but
// "Signing out": it never uses the session again. On every start and reconnect it retries
// /logout first. Signing in as anyone on the server's sign-in page also ends that session
// (routes/auth.py), so the next person on a shared phone never inherits it.

import { idb } from "./db";

const KEY = "lectern.signout";

/** The session's CSRF token: the app's own when it has one, else (app opened offline) the one
 * stored with the signed-in person on the last successful /api/me. */
export async function signOutToken(current: string): Promise<string> {
  if (current) return current;
  try {
    return (await idb.get<{ csrf?: string }>("meta", "owner"))?.csrf ?? "";
  } catch {
    return "";
  }
}

export function pendingSignOut(): { csrf: string } | null {
  try {
    return JSON.parse(localStorage.getItem(KEY) || "null");
  } catch {
    return null;
  }
}

export function markSignOut(csrf: string) {
  try {
    localStorage.setItem(KEY, JSON.stringify({ csrf }));
  } catch {
    /* no storage: the attempt below is all there is */
  }
}

function clearMark() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* no storage */
  }
}

/**
 * Send the pending /logout. True once the server has answered: the session is over (or a
 * 403, see below).
 * False while the server cannot be reached.
 */
export async function finishSignOut(): Promise<boolean> {
  const p = pendingSignOut();
  if (!p) return true;
  try {
    await fetch("/logout", {
      method: "POST",
      credentials: "same-origin",
      redirect: "manual",
      // Tells the server not to send Clear-Site-Data, which WebKit never lets this fetch finish.
      headers: { "content-type": "application/x-www-form-urlencoded", "x-lectern-app": "signout" },
      body: new URLSearchParams({ csrf_token: p.csrf }),
    });
  } catch {
    return false;
  }
  // 403 without a token: this device never had one (nothing was stored), so the server cannot
  // be told; signing in on this phone still ends that session (routes/auth.py). 403 with one:
  // the cookie belongs to a newer sign-in, which already ended the session the token was for.
  clearMark();
  return true;
}
