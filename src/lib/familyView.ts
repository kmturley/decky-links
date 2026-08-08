/** Steam's Family View, reached from the plugin.
 *
 * Kid mode splits in two. Decky Links owns the physical key and its own write
 * lock; *which games may run and which system menus are reachable* is Family
 * View's job, and Steam already does it properly — the allowlist is edited in
 * Steam's own UI, enforced by the client, and survives this plugin being
 * uninstalled. Reimplementing it here would produce a weaker copy that only
 * holds while Decky is running.
 *
 * Everything below is Steam internals, so it is reached by *shape* rather than
 * by module id: ids change with every client build, and a numeric one baked in
 * here would break silently on a Tuesday. The store is identified by the two
 * members no other object has together.
 */

import { findModuleExport } from "@decky/ui";

/** The slice of Steam's parental store this plugin uses. */
interface ParentalStore {
  /** Family View is configured on this account at all. */
  isEnabled: boolean;
  /** Configured *and* currently locked — i.e. kid mode as Steam sees it. */
  isParentalLocked: boolean;
  /** A PIN has been set, so there is something to unlock with. */
  hasPassword: boolean;
  BIsAppBlocked(appid: number): boolean;
}

/** k_EResultOK. UnlockParentalLock resolves to an EResult, not a boolean. */
const E_RESULT_OK = 1;

let cachedStore: ParentalStore | null | undefined;

function parentalApi(): any {
  return (window as any).SteamClient?.Parental;
}

/** Steam's parental store, or null on a build that does not expose it.
 *
 * Cached after the first successful lookup — the search walks every webpack
 * module, and this is consulted on every medium presented. A failed lookup is
 * cached as null too: a client that does not have it will not grow it, and
 * re-walking the bundle on every tap to rediscover that would be worse than
 * the missing feature.
 */
export function getParentalStore(): ParentalStore | null {
  if (cachedStore !== undefined) return cachedStore;
  try {
    cachedStore =
      findModuleExport(
        (e: any) =>
          e &&
          typeof e === "object" &&
          "isParentalLocked" in e &&
          typeof e.BIsAppBlocked === "function",
      ) ?? null;
  } catch (e) {
    console.error("[ Decky Links ] Could not locate Steam's parental store:", e);
    cachedStore = null;
  }
  if (!cachedStore) {
    console.warn("[ Decky Links ] Steam parental store not found; Family View integration off.");
  }
  return cachedStore ?? null;
}

export interface FamilyViewStatus {
  /** The store was found. False means this client cannot be driven at all. */
  available: boolean;
  /** Family View is set up on this account. */
  enabled: boolean;
  /** Family View is set up *and* currently locked. */
  locked: boolean;
  /** A PIN exists, so unlocking is possible. */
  hasPin: boolean;
}

export function familyViewStatus(): FamilyViewStatus {
  const store = getParentalStore();
  if (!store) return { available: false, enabled: false, locked: false, hasPin: false };
  return {
    available: true,
    enabled: !!store.isEnabled,
    locked: !!store.isParentalLocked,
    hasPin: !!store.hasPassword,
  };
}

/** Lock Family View. Needs no secret, which is why locking always works.
 *
 * Returns false when Family View is not set up — there is nothing to lock, and
 * the caller says so rather than reporting a lockdown that did not happen.
 */
export function lockFamilyView(): boolean {
  const status = familyViewStatus();
  if (!status.enabled) return false;
  try {
    parentalApi()?.LockParentalLock();
    return true;
  } catch (e) {
    console.error("[ Decky Links ] LockParentalLock failed:", e);
    return false;
  }
}

/** Unlock Family View with a stored PIN.
 *
 * The second argument is Steam's "this is a Steam Guard code" flag, which this
 * never is. Resolves to an EResult; anything but OK means the PIN was refused,
 * and the user still has Steam's own prompt to fall back on.
 */
export async function unlockFamilyView(pin: string): Promise<boolean> {
  if (!pin) return false;
  const api = parentalApi();
  if (typeof api?.UnlockParentalLock !== "function") return false;
  try {
    const result = await api.UnlockParentalLock(pin, false);
    if (result !== E_RESULT_OK) {
      console.warn(`[ Decky Links ] Family View unlock refused (EResult ${result}).`);
      return false;
    }
    return true;
  } catch (e) {
    console.error("[ Decky Links ] UnlockParentalLock failed:", e);
    return false;
  }
}

/** Whether Family View currently forbids this game.
 *
 * Only meaningful while Family View is locked — Steam's own check returns
 * false otherwise, and this deliberately does not second-guess it. An app id
 * that will not parse is treated as allowed: refusing to launch on a number we
 * failed to read would break non-Steam shortcuts, which are not in the
 * allowlist at all.
 */
export function isAppBlocked(appid: string | number | null): boolean {
  if (appid === null) return false;
  const id = typeof appid === "number" ? appid : parseInt(appid, 10);
  if (!Number.isFinite(id)) return false;
  const store = getParentalStore();
  if (!store) return false;
  try {
    return store.BIsAppBlocked(id);
  } catch (e) {
    console.error("[ Decky Links ] BIsAppBlocked failed:", e);
    return false;
  }
}

/** Steam's Family View setup page, which is on the web rather than in-client. */
export const FAMILY_VIEW_SETUP_URL = "https://store.steampowered.com/parental/set/";
