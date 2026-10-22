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
// up that tap. So the options are fetched when the form opens and reused if still fresh
// (the server keeps a challenge for 5 minutes).
let prepared: { at: number; publicKey: Promise<string> } | null = null;
const FRESH_MS = 4 * 60 * 1000;

export function preparePasskey(): void {
  const publicKey = post<{ publicKey: string }>("/api/security/passkeys/options").then((r) => r.publicKey);
  publicKey.catch(() => (prepared = null));
  prepared = { at: Date.now(), publicKey };
}

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
