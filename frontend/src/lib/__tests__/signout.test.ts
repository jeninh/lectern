import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { finishSignOut, markSignOut, pendingSignOut } from "../offline/signout";

// A sign-out that has not reached the server is kept until the server confirms the session is
// over. Anything else, including answers that look like success, must keep it.

const store = new Map<string, string>();
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => {
  store.clear();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  });
});
afterEach(() => vi.unstubAllGlobals());

function serverAnswers(...answers: (() => Response | Promise<Response>)[]) {
  const calls: { url: string; body: string }[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    calls.push({ url, body: String(init?.body ?? "") });
    const next = answers.shift();
    if (!next) throw new TypeError("Failed to fetch");
    return next();
  });
  return calls;
}

describe("pending sign-out", () => {
  const notConfirmed: [string, () => Response | Promise<Response>][] = [
    ["offline", () => Promise.reject(new TypeError("Failed to fetch"))],
    ["502 from the proxy while Lectern restarts", () => new Response("Bad Gateway", { status: 502 })],
    ["503", () => new Response("", { status: 503 })],
    ["504", () => new Response("", { status: 504 })],
    ["403 CSRF failure", () => json(403, { detail: "Invalid CSRF token" })],
    ["429", () => json(429, { detail: "rate_limited" })],
    ["500", () => json(500, { detail: "internal_error" })],
    ["200 from a captive portal (HTML)", () => new Response("<html>Sign in to Wi-Fi</html>", { status: 200, headers: { "content-type": "text/html" } })],
    ["200 JSON that does not confirm", () => json(200, { ok: true })],
    ["200 JSON signed_out: false", () => json(200, { signed_out: false })],
    ["200 JSON signed_out: \"true\" (a string)", () => json(200, { signed_out: "true" })],
    ["204", () => new Response(null, { status: 204 })],
    ["303 followed to the sign-in page", () => new Response("<html>login</html>", { status: 200, headers: { "content-type": "text/html" } })],
  ];

  for (const [name, answer] of notConfirmed) {
    it(`keeps the mark on ${name}`, async () => {
      markSignOut("tok-A");
      serverAnswers(answer);
      expect(await finishSignOut()).toBe(false);
      expect(pendingSignOut()).toEqual({ csrf: "tok-A" });
    });
  }

  it("clears the mark only on 200 {signed_out: true}, naming the session by its token", async () => {
    markSignOut("tok-A");
    const calls = serverAnswers(() => new Response("Bad Gateway", { status: 502 }), () => json(200, { signed_out: true }));
    expect(await finishSignOut()).toBe(false);
    expect(await finishSignOut()).toBe(true);
    expect(pendingSignOut()).toBeNull();
    expect(calls.map((c) => c.body)).toEqual(["csrf_token=tok-A", "csrf_token=tok-A"]);
  });

  it("with no usable token, asks for the live session's token and names that one", async () => {
    markSignOut("lost-or-missing");
    const calls = serverAnswers(
      () => json(409, { detail: "signout_token_missing" }),
      () => json(200, { csrf_token: "tok-live" }),
      () => json(200, { signed_out: true }),
    );
    expect(await finishSignOut()).toBe(true);
    expect(calls.map((c) => c.url)).toEqual(["/logout", "/api/me", "/logout"]);
    expect(calls[2].body).toBe("csrf_token=tok-live");
  });

  it("with no usable token, keeps the mark if that retry is not confirmed", async () => {
    markSignOut("");
    serverAnswers(
      () => json(409, { detail: "signout_token_missing" }),
      () => json(200, { csrf_token: "tok-live" }),
      () => new Response("Bad Gateway", { status: 502 }),
    );
    expect(await finishSignOut()).toBe(false);
    expect(pendingSignOut()).toEqual({ csrf: "tok-live" });
  });
});
