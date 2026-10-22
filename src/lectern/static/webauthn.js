// Passkey login (on /login) and passkey registration (on /account). No dependencies.
(function () {
  "use strict";
  const b64uToBuf = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=")), (c) => c.charCodeAt(0));
  const bufToB64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const status = (msg) => { const el = document.getElementById("passkey-status"); if (el) { el.hidden = !msg; el.textContent = msg || ""; } };

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() }, body: body ? JSON.stringify(body) : "{}" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.message || ("HTTP " + r.status));
    return data;
  }

  function parseOptions(json) {
    const pk = JSON.parse(json);
    pk.challenge = b64uToBuf(pk.challenge);
    if (pk.user) pk.user.id = b64uToBuf(pk.user.id);
    for (const k of ["excludeCredentials", "allowCredentials"]) if (pk[k]) pk[k] = pk[k].map((c) => ({ ...c, id: b64uToBuf(c.id) }));
    return pk;
  }

  function serialize(cred) {
    const r = cred.response;
    const out = { id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type, authenticatorAttachment: cred.authenticatorAttachment,
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {}, response: { clientDataJSON: bufToB64u(r.clientDataJSON) } };
    if (r.attestationObject) { out.response.attestationObject = bufToB64u(r.attestationObject); out.response.transports = r.getTransports ? r.getTransports() : []; }
    if (r.authenticatorData) { out.response.authenticatorData = bufToB64u(r.authenticatorData); out.response.signature = bufToB64u(r.signature);
      if (r.userHandle) out.response.userHandle = bufToB64u(r.userHandle); }
    return out;
  }

  // Safari (and the home-screen app on iPhone, where a passkey is the easiest way in) only
  // shows the passkey sheet when navigator.credentials.get runs inside the tap. Awaiting a
  // network request first can use up that tap, so the challenge is fetched before it: on load,
  // when the page is shown again, and well before it expires (5 min on the server).
  const loginBtn = document.getElementById("passkey-login");
  let prepared = null; // { at, publicKey }
  async function prepare() {
    try {
      const { publicKey } = await post("/login/passkey/options");
      prepared = { at: Date.now(), publicKey };
    } catch (e) { prepared = null; }
  }
  if (loginBtn) {
    if (!window.PublicKeyCredential) { loginBtn.disabled = true; status("This browser does not support passkeys."); }
    else {
      prepare();
      window.setInterval(prepare, 4 * 60 * 1000);
      window.addEventListener("pageshow", (e) => { if (e.persisted) prepare(); });
    }
    loginBtn.addEventListener("click", async () => {
      try {
        status("Waiting for your passkey…");
        const fresh = prepared && Date.now() - prepared.at < 4 * 60 * 1000 ? prepared : null;
        prepared = null; // a challenge is single-use
        const publicKey = fresh ? fresh.publicKey : (await post("/login/passkey/options")).publicKey;
        const cred = await navigator.credentials.get({ publicKey: parseOptions(publicKey) });
        const next = encodeURIComponent((document.querySelector("script[data-next]") || {}).dataset?.next || "/");
        const res = await post("/login/passkey/verify?next=" + next, serialize(cred));
        window.location.assign(res.next || "/");
      } catch (e) { status("Passkey sign-in failed: " + e.message); prepare(); }
    });
  }

  const regBtn = document.getElementById("passkey-register");
  if (regBtn) {
    regBtn.addEventListener("click", async () => {
      try {
        status("Follow your browser's prompt…");
        const { publicKey } = await post("/account/passkeys/options");
        const cred = await navigator.credentials.create({ publicKey: parseOptions(publicKey) });
        const body = serialize(cred);
        body.label = (document.getElementById("passkey-label") || {}).value || "Passkey";
        await post("/account/passkeys/verify", body);
        window.location.reload();
      } catch (e) { status("Could not add passkey: " + e.message); }
    });
  }
})();
