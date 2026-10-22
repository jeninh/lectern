// Signing out when the server may be out of reach (docs/offline.md#signing-out).
//
// The device's downloads, read-along data, and queued positions are deleted at once. The
// server session ends only when /logout reaches the server, so until then this device keeps a
// mark (with the session's CSRF token, which names the session to end) and the app shows
// nothing but "Signed out": it never uses the session again. It retries /logout on start, on
// reconnect, and every 20 s, until the server confirms the session is over. Signing in as anyone on the server's sign-in page also ends that session
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

async function post(csrf: string): Promise<Response | null> {
  try {
    return await fetch("/logout", {
      method: "POST",
      credentials: "same-origin",
      // The app's own sign-out: answered with JSON, without Clear-Site-Data (which WebKit
      // never lets this fetch finish) and without a redirect (routes/auth.py _app_sign_out).
      headers: { "content-type": "application/x-www-form-urlencoded", "x-lectern-app": "signout" },
      body: new URLSearchParams({ csrf_token: csrf }),
    });
  } catch {
    return null;
  }
}

async function confirmed(res: Response | null): Promise<boolean> {
  if (!res || res.status !== 200) return false;
  try {
    return (await res.json())?.signed_out === true;
  } catch {
    return false; // a proxy's or portal's page, not Lectern's answer
  }
}

/**
 * Send the pending /logout. True only when the server confirmed the session is over
 * (`200 {"signed_out": true}`); the mark is kept on anything else (offline, a 502 while the
 * server restarts behind its proxy, a 403, a captive portal's page) and retried later.
 */
export async function finishSignOut(): Promise<boolean> {
  const p = pendingSignOut();
  if (!p) return true;
  let res = await post(p.csrf);
  if (res?.status === 409) {
    // The token named no session while one is live in this browser (the app had none stored,
    // or it was lost). Ask for the live session's token, without handing it to the app, and
    // end that one: whatever is signed in here is signed out.
    try {
      const me = await fetch("/api/me", { credentials: "same-origin", headers: { accept: "application/json" } });
      const csrf = me.ok ? (await me.json())?.csrf_token : "";
      if (typeof csrf === "string" && csrf) {
        markSignOut(csrf);
        res = await post(csrf);
      }
    } catch {
      return false;
    }
  }
  if (!(await confirmed(res))) return false;
  clearMark();
  return true;
}
