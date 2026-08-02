import { comparableAppIdFromUri } from "./steamIds";

// `appStore` is injected by the Decky runtime and has no TypeScript
// definitions available in this repo.
declare const appStore: any;

function safeCall<T>(fn: (() => T) | undefined): T | undefined {
  try {
    return fn ? fn() : undefined;
  } catch {
    return undefined;
  }
}

/** Display name for a launch URI: "steam://rungameid/220" → "Half-Life 2".
 *
 * A paired medium only stores the URI, so the panel used to show "app 220" —
 * accurate but meaningless to the person holding the disk. Falls back to the
 * app id when Steam has no overview (an uninstalled or non-Steam target), and
 * to the raw URI when it isn't a Steam launch at all.
 */
export function launchTargetName(uri: string | null | undefined): string {
  if (!uri) return "";
  const appId = comparableAppIdFromUri(uri);
  if (!appId) return uri;

  const overview =
    safeCall(() => appStore?.GetAppOverviewByAppID?.(Number(appId))) ??
    safeCall(() => appStore?.GetAppOverviewByAppID?.(appId));

  const name = overview?.display_name;
  return typeof name === "string" && name.trim() ? name.trim() : `App ${appId}`;
}
