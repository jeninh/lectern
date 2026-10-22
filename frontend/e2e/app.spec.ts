// Installable app: manifest, icons, launch screens, safe areas, install hints, "Update ready",
// passkey sign-in, and one person's downloads never reaching the next.
import { expect, test } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { cacheKeys, controlled, downloadBook, MAGI, PASSWORD, shot, signIn, userOf, workId } from "./helpers";

test.describe.configure({ mode: "serial" });

test("install metadata: manifest, icons, launch screens, standalone, safe areas", async ({ page, request, playwright, baseURL }, info) => {
  await signIn(page, userOf(info));
  const head = await page.evaluate(() => ({
    manifest: document.querySelector<HTMLLinkElement>('link[rel="manifest"]')?.getAttribute("href"),
    viewport: document.querySelector('meta[name="viewport"]')?.getAttribute("content"),
    capable: document.querySelector('meta[name="apple-mobile-web-app-capable"]')?.getAttribute("content"),
    statusBar: document.querySelector('meta[name="apple-mobile-web-app-status-bar-style"]')?.getAttribute("content"),
    touchIcon: document.querySelector<HTMLLinkElement>('link[rel="apple-touch-icon"]')?.getAttribute("href"),
    splash: [...document.querySelectorAll<HTMLLinkElement>('link[rel="apple-touch-startup-image"]')].map((l) => [l.getAttribute("href"), l.media]),
    themes: document.querySelectorAll('meta[name="theme-color"]').length,
  }));
  expect(head.viewport).toContain("viewport-fit=cover");
  expect(head).toMatchObject({ manifest: "/app/manifest.webmanifest", capable: "yes", statusBar: "black-translucent", touchIcon: "/app/pwa/apple-touch-icon.png", themes: 2 });
  // A launch screen for this phone's exact screen.
  const [w, h, dpr] = await page.evaluate(() => [screen.width, screen.height, devicePixelRatio]);
  if (info.project.name === "iphone") expect(head.splash.some(([, m]) => m.includes(`device-width: ${w}px`) && m.includes(`device-height: ${h}px`) && m.includes(`ratio: ${dpr}`))).toBe(true);
  for (const [href] of head.splash) expect((await request.get(href!)).headers()["content-type"]).toBe("image/png");

  const res = await request.get("/app/manifest.webmanifest");
  expect(res.headers()["content-type"]).toContain("application/manifest+json");
  const m = await res.json();
  expect(m).toMatchObject({ id: "/app", start_url: "/app", display: "standalone", scope: "/" });
  // Scope "/" keeps /login inside the installed app: signing in (passkey included) never
  // opens a browser sheet with a separate cookie jar.
  expect(new URL("/login", "http://x").pathname.startsWith(m.scope)).toBe(true);
  for (const icon of m.icons) expect((await request.get(icon.src)).ok()).toBe(true);
  expect(m.icons.some((i: { purpose: string }) => i.purpose === "maskable")).toBe(true);
  // These are public: fetched by the OS and the browser without the session.
  const anon = await playwright.request.newContext({ baseURL });
  for (const url of ["/app/sw.js", "/app/manifest.webmanifest", "/app/pwa/icon-512.png", "/app/shell.html"]) {
    expect((await anon.get(url, { maxRedirects: 0 })).status(), url).toBe(200);
  }
  // ...while the app itself and its data still need the session.
  expect((await anon.get("/api/me")).status()).toBe(401);
  await anon.dispose();

  // Safe areas: the top bar clears the status bar; the tab bar clears the home indicator.
  const css = await page.evaluate(() => [...document.styleSheets].flatMap((s) => [...s.cssRules].map((r) => r.cssText)).join("\n"));
  expect(css).toMatch(/\.main\s*\{[^}]*safe-area-inset-top/);
  expect(css).toMatch(/\.bottom-nav\s*\{[^}]*safe-area-inset-bottom/);
});

test("install hint for this platform", async ({ page }, info) => {
  await signIn(page, userOf(info));
  await page.goto("/app/downloads");
  const hint = page.getByTestId("install-hint");
  await expect(hint).toBeVisible();
  if (info.project.name === "iphone") {
    await expect(hint).toHaveAttribute("data-platform", "ios-safari");
    await expect(hint).toContainText("Add to Home Screen");
  } else {
    await expect(hint).toHaveAttribute("data-platform", "android");
    await expect(hint).toContainText(/Install app|Add to Home screen/);
  }
  await shot(page, info, "install-hint");
});

test("a new version waits, shows Update ready, and reloads into it", async ({ page }, info) => {
  await signIn(page, userOf(info));
  await controlled(page);
  const before = await page.evaluate(() => navigator.serviceWorker.controller!.scriptURL);
  const file = resolve(info.project.testDir, "../../src/lectern/static/app/sw.js");
  const original = readFileSync(file, "utf8");
  try {
    writeFileSync(file, `${original}\n// e2e: a new deploy ${Date.now()}\n`);
    await page.evaluate(async () => (await navigator.serviceWorker.getRegistration("/app"))!.update());
    const banner = page.locator(".update-ready");
    await expect(banner).toContainText("A new version of Lectern is ready", { timeout: 20_000 });
    // It waits: still the old worker until the reader chooses to reload.
    expect(await page.evaluate(async () => !!(await navigator.serviceWorker.getRegistration("/app"))!.waiting)).toBe(true);
    await shot(page, info, "update-ready");
    await Promise.all([page.waitForEvent("load"), banner.getByRole("button", { name: "Reload" }).click()]);
    await expect(page.locator(".update-ready")).toHaveCount(0);
    const after = await page.evaluate(async () => {
      const r = (await navigator.serviceWorker.getRegistration("/app"))!;
      return { waiting: !!r.waiting, controlled: !!navigator.serviceWorker.controller, url: navigator.serviceWorker.controller?.scriptURL };
    });
    expect(after).toMatchObject({ waiting: false, controlled: true, url: before });
  } finally {
    writeFileSync(file, original);
  }
});

test("someone else signing in on this device never sees the previous person's downloads", async ({ page, context }, info) => {
  await signIn(page, userOf(info));
  await controlled(page);
  await downloadBook(page, await workId(page, MAGI));
  expect((await cacheKeys(page))["lectern-books"]?.length).toBeGreaterThan(0);
  // The session goes away without signing out (expired, or cookies cleared); the owner signs in.
  await context.clearCookies();
  await signIn(page, "owner");
  await page.goto("/app/downloads");
  await expect(page.getByText("Nothing downloaded yet")).toBeVisible();
  await expect.poll(async () => (await cacheKeys(page))["lectern-books"] ?? []).toEqual([]);
});

test("passkey sign-in asks for the passkey straight from the tap", async ({ page, context, browserName }, info) => {
  await signIn(page, userOf(info));
  if (browserName === "chromium") {
    // A real ceremony against the server with a virtual platform authenticator.
    const cdp = await context.newCDPSession(page);
    await cdp.send("WebAuthn.enable");
    await cdp.send("WebAuthn.addVirtualAuthenticator", {
      options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true },
    });
    // Registering a passkey needs a step-up (the account password) in the options request.
    const registered = await page.evaluate(async (password) => {
      const b64 = (s: string) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=")), (c) => c.charCodeAt(0));
      const u = (buf: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
      const csrf = (await (await fetch("/api/me")).json()).csrf_token;
      const h = { "content-type": "application/json", "x-csrf-token": csrf };
      const pk = JSON.parse((await (await fetch("/api/security/passkeys/options", { method: "POST", headers: h, body: JSON.stringify({ password }) })).json()).publicKey);
      pk.challenge = b64(pk.challenge);
      pk.user.id = b64(pk.user.id);
      pk.excludeCredentials = [];
      const cred = (await navigator.credentials.create({ publicKey: pk })) as PublicKeyCredential;
      const r = cred.response as AuthenticatorAttestationResponse;
      const body = { id: cred.id, rawId: u(cred.rawId), type: cred.type, clientExtensionResults: {}, response: { clientDataJSON: u(r.clientDataJSON), attestationObject: u(r.attestationObject), transports: ["internal"] }, label: "Phone" };
      return (await fetch("/api/security/passkeys/verify", { method: "POST", headers: h, body: JSON.stringify(body) })).status;
    }, PASSWORD);
    expect(registered).toBe(200);
    await context.clearCookies();
    await page.goto("/login");
    await page.waitForTimeout(500); // the challenge is fetched on load, before the tap
    await page.getByRole("button", { name: "Sign in with a passkey" }).click();
    await page.waitForURL(/\/app/);
    await expect(page.locator(".user-chip:visible, .bottom-nav:visible").first()).toBeVisible();
  } else {
    // WebKit has no virtual authenticator. What Safari needs is that credentials.get() runs in
    // the tap itself, with no network wait before it: the challenge must already be here.
    await context.clearCookies();
    await page.addInitScript(() => {
      const w = window as unknown as { __opts: number; __atGet: number | null };
      w.__opts = 0;
      w.__atGet = null;
      const f = window.fetch.bind(window);
      window.fetch = (input, init) => {
        if (String(input).includes("/login/passkey/options")) w.__opts++;
        return f(input, init);
      };
      // Playwright's Linux WebKit build has no WebAuthn; the page only needs to see the API.
      if (!("PublicKeyCredential" in window)) Object.defineProperty(window, "PublicKeyCredential", { value: function PublicKeyCredential() {} });
      const creds = (navigator.credentials ?? {}) as CredentialsContainer;
      if (!navigator.credentials) Object.defineProperty(Navigator.prototype, "credentials", { value: creds, configurable: true });
      creds.get = async () => {
        w.__atGet = w.__opts;
        throw new DOMException("test", "NotAllowedError");
      };
    });
    await page.goto("/login");
    await page.waitForFunction(() => (window as unknown as { __opts: number }).__opts >= 1);
    await page.waitForTimeout(300);
    const before = await page.evaluate(() => (window as unknown as { __opts: number }).__opts);
    await page.getByRole("button", { name: "Sign in with a passkey" }).click();
    await page.waitForFunction(() => (window as unknown as { __atGet: number | null }).__atGet !== null);
    expect(await page.evaluate(() => (window as unknown as { __atGet: number }).__atGet)).toBe(before);
  }
});
