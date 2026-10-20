// Thin JSON client. Every write carries the per-session CSRF token; the browser adds the
// Origin header itself, which the server checks for same-site.

let csrfToken = "";

export function setCsrfToken(token: string) {
  csrfToken = token;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function parse(res: Response) {
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    if (res.status === 401) {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    }
    const d = data as { detail?: unknown; message?: string } | null;
    const detail = typeof d?.detail === "string" ? d.detail : d?.message;
    throw new ApiError(res.status, detail || `Request failed (${res.status})`);
  }
  return data;
}

export async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", headers: { accept: "application/json" } });
  return (await parse(res)) as T;
}

export async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      "x-csrf-token": csrfToken,
    },
    body: JSON.stringify(body ?? {}),
  });
  return (await parse(res)) as T;
}

export async function put<T>(path: string, body: unknown, init?: { keepalive?: boolean }): Promise<T> {
  const res = await fetch(path, {
    method: "PUT",
    credentials: "same-origin",
    keepalive: init?.keepalive,
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      "x-csrf-token": csrfToken,
    },
    body: JSON.stringify(body),
  });
  return (await parse(res)) as T;
}

/** Streams one EPUB to the inbox. The body is the raw file; its name rides in a header. */
export async function uploadBook(file: File): Promise<{ queued: string; size: number }> {
  const res = await fetch("/api/books/upload", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      accept: "application/json",
      "content-type": "application/epub+zip",
      "x-csrf-token": csrfToken,
      "x-filename": encodeURIComponent(file.name),
    },
    body: file,
  });
  return (await parse(res)) as { queued: string; size: number };
}

export function logout() {
  const form = document.createElement("form");
  form.method = "post";
  form.action = "/logout";
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "csrf_token";
  input.value = csrfToken;
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
}
