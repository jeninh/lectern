import { post } from "./api";

const b64uToBuf = (s: string) =>
  Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=")), (c) =>
    c.charCodeAt(0),
  );
const bufToB64u = (buf: ArrayBuffer) =>
  btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && !!window.PublicKeyCredential;
}

// Safari (and the home-screen app on iPhone) shows the passkey sheet only when
// navigator.credentials.create runs inside the tap; awaiting the options request first can use
// up that tap. So the options are fetched before the tap, right after the person confirms it's
// them (registering a passkey needs a step-up: a session alone cannot add one), and reused if
// still fresh (the server keeps a challenge for 5 minutes).
let prepared: { at: number; publicKey: Promise<string> } | null = null;
const FRESH_MS = 4 * 60 * 1000;

/** Fetch registration options with a step-up proof (password or an existing passkey). */
export async function preparePasskey(stepUp: Record<string, unknown>): Promise<void> {
  const publicKey = post<{ publicKey: string }>("/api/security/passkeys/options", stepUp).then((r) => r.publicKey);
  prepared = { at: Date.now(), publicKey };
  try {
    await publicKey;
  } catch (e) {
    prepared = null;
    throw e;
  }
}

/** Register a passkey with the options from ``preparePasskey``. Without fresh ones the server
 * answers ``step_up_required`` and the caller asks the person to confirm again. */
export async function registerPasskey(label: string): Promise<void> {
  const fresh = prepared && Date.now() - prepared.at < FRESH_MS ? prepared : null;
  prepared = null; // single-use challenge
  const publicKey = await (fresh?.publicKey ?? post<{ publicKey: string }>("/api/security/passkeys/options").then((r) => r.publicKey));
  const pk = JSON.parse(publicKey);
  pk.challenge = b64uToBuf(pk.challenge);
  pk.user.id = b64uToBuf(pk.user.id);
  if (pk.excludeCredentials)
    pk.excludeCredentials = pk.excludeCredentials.map((c: { id: string }) => ({ ...c, id: b64uToBuf(c.id) }));
  const cred = (await navigator.credentials.create({ publicKey: pk })) as PublicKeyCredential | null;
  if (!cred) throw new Error("No credential returned");
  const r = cred.response as AuthenticatorAttestationResponse;
  const body = {
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    authenticatorAttachment: (cred as PublicKeyCredential & { authenticatorAttachment?: string }).authenticatorAttachment,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: bufToB64u(r.clientDataJSON),
      attestationObject: bufToB64u(r.attestationObject),
      transports: typeof r.getTransports === "function" ? r.getTransports() : [],
    },
    label,
  };
  await post("/api/security/passkeys/verify", body);
}

/**
 * Sign a fresh passkey challenge from ``optionsPath`` (a step-up for a sensitive action).
 * Returns the assertion for the action's request body; the server checks it against the
 * challenge it set in a cookie, once.
 */
export async function passkeyAssertion(optionsPath: string): Promise<Record<string, unknown>> {
  const { publicKey } = await post<{ publicKey: string }>(optionsPath);
  const pk = JSON.parse(publicKey);
  pk.challenge = b64uToBuf(pk.challenge);
  if (pk.allowCredentials)
    pk.allowCredentials = pk.allowCredentials.map((c: { id: string }) => ({ ...c, id: b64uToBuf(c.id) }));
  const cred = (await navigator.credentials.get({ publicKey: pk })) as PublicKeyCredential | null;
  if (!cred) throw new Error("No passkey was used");
  const r = cred.response as AuthenticatorAssertionResponse;
  return {
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    authenticatorAttachment: (cred as PublicKeyCredential & { authenticatorAttachment?: string }).authenticatorAttachment,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: bufToB64u(r.clientDataJSON),
      authenticatorData: bufToB64u(r.authenticatorData),
      signature: bufToB64u(r.signature),
      ...(r.userHandle ? { userHandle: bufToB64u(r.userHandle) } : {}),
    },
  };
}
