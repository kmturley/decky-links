import { useEffect, useState } from "react";
import { useParams } from "./useParams";
import { appTypes } from "../constants";
import { resolveRungameidTarget } from "../lib/steamIds";
import type { ViewedApp } from "../shared";

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

function normalizeId(value: unknown): string | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  return undefined;
}

/** Resolve the game whose detail page is currently open.
 *
 * Returns the app id, its fully-resolved launch URI and (where Steam exposes
 * it) the display name. Returns null when not on a game detail page.
 *
 * Only usable inside the `/library/app/:appid` route tree — it reads route
 * params, which are not available from the Quick Access panel. The panel gets
 * this data via `sharedState.viewedApp`, published by ViewedAppReporter.
 */
export const useViewedApp = (): ViewedApp | null => {
  const [viewedApp, setViewedApp] = useState<ViewedApp | null>(null);
  const { appid: pathId } = useParams<{ appid: string }>();

  useEffect(() => {
    if (!pathId || pathId === "0") {
      setViewedApp(null);
      return;
    }

    const parsedPathId = parseInt(pathId, 10);
    const appDetails =
      safeCall(() => appStore?.GetAppOverviewByGameID?.(pathId)) ??
      safeCall(() => appStore?.GetAppOverviewByGameID?.(parsedPathId));

    const name =
      typeof appDetails?.display_name === "string" && appDetails.display_name.trim()
        ? appDetails.display_name.trim()
        : undefined;

    const appType = appDetails?.app_type;
    const isSteamGame = Boolean(appTypes[appType as keyof typeof appTypes]);

    if (isSteamGame) {
      setViewedApp({ appId: pathId, launchTarget: `steam://run/${pathId}`, name });
      return;
    }

    const shortcutId =
      normalizeId(appDetails?.shortcut_override_appid) ??
      normalizeId(appDetails?.appid) ??
      pathId;

    const launchTarget = resolveRungameidTarget(shortcutId, true);
    setViewedApp(launchTarget ? { appId: pathId, launchTarget, name } : null);
  }, [pathId]);

  return viewedApp;
};

/** Launch URI for the currently-open game detail page, or undefined. */
const useAppId = () => useViewedApp()?.launchTarget;

export default useAppId;
