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
