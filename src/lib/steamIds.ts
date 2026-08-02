const SHORTCUT_FLAG = 0x80000000;
const SHORTCUT_TYPE = 0x02000000n;
const U32_MASK = 0xFFFFFFFFn;

export function toUnsigned32(id: string | number): number | null {
  const n = Number(id);
  if (!Number.isFinite(n)) return null;
  return n >>> 0;
}

export function isLikelyNonSteamShortcutId(id: string | number): boolean {
  const u = toUnsigned32(id);
  return u !== null && (u & SHORTCUT_FLAG) !== 0;
}

export function shortcutAppIdToGameId64(appId: string | number): string | null {
  const u = toUnsigned32(appId);
  if (u === null) return null;
  const gameId = ((BigInt(u) | BigInt(SHORTCUT_FLAG)) << 32n) | SHORTCUT_TYPE;
  return gameId.toString();
}

export function resolveRungameidTarget(appId: string | number, forceShortcut = false): string | null {
  const idStr = String(appId);
  if (forceShortcut || isLikelyNonSteamShortcutId(idStr)) {
    const gameId64 = shortcutAppIdToGameId64(idStr);
    if (gameId64) return `steam://rungameid/${gameId64}`;
  }
  return `steam://rungameid/${idStr}`;
}

const STEAM_RUN_PREFIX = "steam://run/";
const STEAM_RUNGAMEID_PREFIX = "steam://rungameid/";

/** Reduce a launch URI to an app id that can be compared across URI forms.
 *
 * `steam://run/400` and `steam://rungameid/400` name the same game, and a
 * non-Steam shortcut's rungameid packs the app id into its high 32 bits, so
 * raw string comparison of two URIs is not a reliable identity test.
 */
export function comparableAppIdFromUri(uri: string | null | undefined): string | null {
  if (!uri) return null;
  if (uri.startsWith(STEAM_RUN_PREFIX)) {
    return uri.replace(STEAM_RUN_PREFIX, "").split("/")[0] || null;
  }
  if (uri.startsWith(STEAM_RUNGAMEID_PREFIX)) {
    const value = uri.replace(STEAM_RUNGAMEID_PREFIX, "").split("/")[0] || null;
    return value ? extractComparableAppIdFromRungameid(value) : null;
  }
  return null;
}

/** True when two launch URIs refer to the same game. */
export function isSameLaunchTarget(
  a: string | null | undefined,
  b: string | null | undefined,
): boolean {
  if (!a || !b) return false;
  if (a === b) return true;
  const idA = comparableAppIdFromUri(a);
  const idB = comparableAppIdFromUri(b);
  return !!(idA && idB && idA === idB);
}

export function extractComparableAppIdFromRungameid(value: string): string {
  if (!/^\d+$/.test(value)) return value;
  try {
    const n = BigInt(value);
    if (n > U32_MASK) {
      return ((n >> 32n) & U32_MASK).toString();
    }
  } catch {
    return value;
  }
  return value;
}
