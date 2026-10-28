// A browser shared by two people: one person's reader settings must never reach the other's
// account (they would be sent as pending changes, or as a first-time default).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearDevice } from "../offline/store";
import { claimReaderStorage } from "../reader";

// Enumerable own keys are the stored items, as with the real localStorage.
function storage(items: Record<string, string>) {
  const s: Record<string, string> = { ...items };
  const hidden = (value: unknown) => ({ enumerable: false, value });
  Object.defineProperties(s, {
    getItem: hidden((k: string) => (Object.hasOwn(s, k) ? s[k] : null)),
    setItem: hidden((k: string, v: string) => (s[k] = String(v))),
    removeItem: hidden((k: string) => delete s[k]),
  });
  return s;
}

const A_SETTINGS = {
  "lectern.reader": '{"theme":"dark"}',
  "lectern.reader.book.7": '{"size":85}',
  "lectern.reader.pending": '["default",7]',
};

let ls: Record<string, string>;
beforeEach(() => {
  vi.stubGlobal("indexedDB", {
    deleteDatabase: () => {
      const req: { onsuccess?: () => void } = {};
      setTimeout(() => req.onsuccess?.());
      return req;
    },
  });
});
afterEach(() => vi.unstubAllGlobals());
const use = (items: Record<string, string>) => vi.stubGlobal("localStorage", (ls = storage(items)));

describe("clearDevice (someone else signs in)", () => {
  it("forgets the reader settings, their pending changes, and whose they were", async () => {
    use({ ...A_SETTINGS, "lectern.reader.user": "1", "lectern.read.7": '{"cfi":""}', "lectern.theme": '"dark"', "lectern.readerish": "x", "lectern.deviceName": '"Laptop"' });
    await clearDevice({ keepWorker: true });
    expect(Object.keys(ls).sort()).toEqual(["lectern.deviceName", "lectern.readerish", "lectern.theme"]);
  });
});

describe("claimReaderStorage (the reader opens before clearDevice has run)", () => {
  it("deletes another person's settings before they can be sent", () => {
    use({ ...A_SETTINGS, "lectern.reader.user": "1", "lectern.theme": '"dark"' });
    claimReaderStorage(2);
    expect(ls).toEqual({ "lectern.reader.user": "2", "lectern.theme": '"dark"' });
  });

  it("keeps the same person's settings and a copy from before syncing", () => {
    use({ ...A_SETTINGS, "lectern.reader.user": "1" });
    claimReaderStorage(1);
    expect(ls).toEqual({ ...A_SETTINGS, "lectern.reader.user": "1" });
    use({ "lectern.reader": '{"theme":"sepia"}' });
    claimReaderStorage(2);
    expect(ls).toEqual({ "lectern.reader": '{"theme":"sepia"}', "lectern.reader.user": "2" });
  });
});
