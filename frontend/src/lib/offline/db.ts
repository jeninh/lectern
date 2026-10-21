// IndexedDB for what the installed app keeps on the device (docs/offline.md): the signed-in
// person (to keep one person's downloads from another), each downloaded book's details and
// read-along data, and positions waiting to sync. Book bytes are in Cache Storage
// (routes.ts BOOKS_CACHE). Everything here is deleted on sign-out.

const NAME = "lectern-offline";
const VERSION = 1;
export const STORES = ["meta", "books", "readalong", "queue"] as const;
export type StoreName = (typeof STORES)[number];

let opening: Promise<IDBDatabase> | null = null;

function open(): Promise<IDBDatabase> {
  if (opening) return opening;
  opening = new Promise<IDBDatabase>((resolve, reject) => {
    const req = indexedDB.open(NAME, VERSION);
    req.onupgradeneeded = () => {
      for (const s of STORES) if (!req.result.objectStoreNames.contains(s)) req.result.createObjectStore(s);
    };
    req.onsuccess = () => {
      const db = req.result;
      db.onversionchange = () => {
        db.close();
        opening = null;
      };
      resolve(db);
    };
    req.onerror = () => {
      opening = null;
      reject(req.error);
    };
    req.onblocked = () => reject(new Error("offline storage is blocked by another tab"));
  });
  return opening;
}

function wrap<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function tx<T>(store: StoreName, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open();
  const t = db.transaction(store, mode);
  const result = wrap(fn(t.objectStore(store)));
  await new Promise<void>((resolve, reject) => {
    t.oncomplete = () => resolve();
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error ?? new Error("aborted"));
  });
  return result;
}

export const idb = {
  get: <T>(store: StoreName, key: IDBValidKey) => tx<T | undefined>(store, "readonly", (s) => s.get(key) as IDBRequest<T | undefined>),
  all: <T>(store: StoreName) => tx<T[]>(store, "readonly", (s) => s.getAll() as IDBRequest<T[]>),
  put: (store: StoreName, key: IDBValidKey, value: unknown) => tx(store, "readwrite", (s) => s.put(value, key)).then(() => undefined),
  del: (store: StoreName, key: IDBValidKey) => tx(store, "readwrite", (s) => s.delete(key)).then(() => undefined),
  clear: (store: StoreName) => tx(store, "readwrite", (s) => s.clear()).then(() => undefined),
};

/** Remove the whole database (sign-out). Closes our connection first so the delete is not blocked. */
export async function dropDatabase(): Promise<void> {
  if (opening) {
    try {
      (await opening).close();
    } catch {
      /* never opened */
    }
    opening = null;
  }
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase(NAME);
    req.onsuccess = req.onerror = req.onblocked = () => resolve();
  });
}
