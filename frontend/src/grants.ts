/**
 * Grant storage: one grant, bound to the application it was minted for.
 *
 * A grant is a one-time authorization to submit *one specific application*. If
 * the UI kept them in a bag and handed out "the first one it found", then a
 * grant approved for posting A could be spent on posting B -- which is exactly
 * what the storage key below makes impossible: the application id is part of the
 * key, so a lookup can only ever return the grant for the application being
 * submitted.
 *
 * The storage is injected rather than reaching for `localStorage` directly, so
 * the rule can be tested without a browser.
 */

export interface GrantRecord {
  grantId: string;
  requestId: string;
  jobKey: string;
  storedAt: number;
}

export interface GrantStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
  keys(): string[];
}

const PREFIX = "applyops-grant:";

export function grantKeyFor(applicationId: string): string {
  return `${PREFIX}${applicationId}`;
}

/** Remember the grant the user just approved, against that application only. */
export function rememberGrant(
  storage: GrantStorage,
  args: { applicationId: string; grantId: string; requestId: string; jobKey: string }
): void {
  if (!args.applicationId) {
    // Refusing beats guessing: a grant with no application cannot be bound to
    // one later without inventing the link.
    throw new Error("refusing to store a grant without an application id");
  }
  const record: GrantRecord = {
    grantId: args.grantId,
    requestId: args.requestId,
    jobKey: args.jobKey,
    storedAt: Date.now(),
  };
  storage.setItem(grantKeyFor(args.applicationId), JSON.stringify(record));
}

/** The grant for this application, or null. Never another application's grant. */
export function readGrant(storage: GrantStorage, applicationId: string): GrantRecord | null {
  const raw = storage.getItem(grantKeyFor(applicationId));
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as GrantRecord;
    return parsed.grantId ? parsed : null;
  } catch {
    return null;
  }
}

export function forgetGrant(storage: GrantStorage, applicationId: string): void {
  storage.removeItem(grantKeyFor(applicationId));
}

/** The real browser storage. `globalThis` so it is also testable in node. */
export function browserGrantStorage(): GrantStorage {
  const store = (globalThis as { localStorage: Storage }).localStorage;
  return {
    getItem: (key) => store.getItem(key),
    setItem: (key, value) => store.setItem(key, value),
    removeItem: (key) => store.removeItem(key),
    keys: () => Object.keys(store),
  };
}
