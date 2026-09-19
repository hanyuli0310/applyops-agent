import { describe, expect, it } from "vitest";
import {
  browserGrantStorage,
  forgetGrant,
  grantKeyFor,
  readGrant,
  rememberGrant,
  type GrantStorage,
} from "../src/grants";

/** A Map-backed storage, so the rule is tested without a browser. */
function fakeStorage(): GrantStorage & { dump: () => Record<string, string> } {
  const map = new Map<string, string>();
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => void map.set(key, value),
    removeItem: (key) => void map.delete(key),
    keys: () => [...map.keys()],
    dump: () => Object.fromEntries(map),
  };
}

describe("grant storage", () => {
  it("keys a grant by the application it was approved for", () => {
    expect(grantKeyFor("app-1")).toBe("applyops-grant:app-1");
  });

  it("returns the grant only for its own application", () => {
    const storage = fakeStorage();
    rememberGrant(storage, {
      applicationId: "app-A",
      grantId: "grant-A",
      requestId: "req-A",
      jobKey: "job-A",
    });

    expect(readGrant(storage, "app-A")?.grantId).toBe("grant-A");
    // The important assertion: posting B cannot reach posting A's approval.
    expect(readGrant(storage, "app-B")).toBeNull();
  });

  it("keeps consecutive applications apart", () => {
    const storage = fakeStorage();
    for (const id of ["app-1", "app-2", "app-3"]) {
      rememberGrant(storage, {
        applicationId: id,
        grantId: `grant-${id}`,
        requestId: `req-${id}`,
        jobKey: `job-${id}`,
      });
    }

    expect(readGrant(storage, "app-1")?.grantId).toBe("grant-app-1");
    expect(readGrant(storage, "app-2")?.grantId).toBe("grant-app-2");
    expect(readGrant(storage, "app-3")?.grantId).toBe("grant-app-3");
  });

  it("forgets a grant once it has been spent", () => {
    const storage = fakeStorage();
    rememberGrant(storage, {
      applicationId: "app-A",
      grantId: "grant-A",
      requestId: "req-A",
      jobKey: "job-A",
    });
    forgetGrant(storage, "app-A");
    expect(readGrant(storage, "app-A")).toBeNull();
  });

  it("refuses to store a grant with no application to bind it to", () => {
    const storage = fakeStorage();
    expect(() =>
      rememberGrant(storage, {
        applicationId: "",
        grantId: "grant-X",
        requestId: "req-X",
        jobKey: "job-X",
      })
    ).toThrow(/application id/);
    expect(storage.keys()).toHaveLength(0);
  });

  it("treats corrupt storage as no grant rather than crashing", () => {
    const storage = fakeStorage();
    storage.setItem(grantKeyFor("app-A"), "{not json");
    expect(readGrant(storage, "app-A")).toBeNull();
  });

  it("uses the real localStorage when asked for browser storage", () => {
    // Cheap guard that the browser adapter reads the right global.
    const original = (globalThis as { localStorage?: unknown }).localStorage;
    // Real localStorage exposes stored keys as own enumerable properties, which
    // is what `keys()` relies on; the fake has to do the same to be a fair test.
    const backing: Record<string, string> = {};
    const store = {
      getItem: (k: string) => (k in backing ? backing[k] : null),
      setItem: (k: string, v: string) => {
        backing[k] = v;
      },
      removeItem: (k: string) => {
        delete backing[k];
      },
      key: (index: number) => Object.keys(backing)[index] ?? null,
      get length() {
        return Object.keys(backing).length;
      },
      ...backing,
    };
    (globalThis as { localStorage?: unknown }).localStorage = {
      ...store,
      setItem: (k: string, v: string) => {
        backing[k] = v;
        (globalThis as unknown as { localStorage: Record<string, unknown> }).localStorage[k] = v;
      },
    };
    const storage = browserGrantStorage();
    storage.setItem("x", "1");
    expect(storage.getItem("x")).toBe("1");
    expect(storage.keys()).toContain("x");
    (globalThis as { localStorage?: unknown }).localStorage = original;
  });
});
