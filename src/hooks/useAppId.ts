import { useEffect, useState } from "react";
import { useParams } from "./useParams";
import { appTypes } from "../constants";
import { resolveRungameidTarget } from "../lib/steamIds";

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

const useAppId = () => {
  const [launchTarget, setLaunchTarget] = useState<string>();
  const { appid: pathId } = useParams<{ appid: string }>();

  useEffect(() => {
    if (!pathId || pathId === "0") {
      setLaunchTarget(undefined);
      return;
    }

    const parsedPathId = parseInt(pathId, 10);
    const appDetails =
      safeCall(() => appStore?.GetAppOverviewByGameID?.(pathId)) ??
      safeCall(() => appStore?.GetAppOverviewByGameID?.(parsedPathId));

    const appType = appDetails?.app_type;
    const isSteamGame = Boolean(appTypes[appType as keyof typeof appTypes]);

    if (isSteamGame) {
      setLaunchTarget(`steam://run/${pathId}`);
      return;
    }

    const shortcutId =
      normalizeId(appDetails?.shortcut_override_appid) ??
      normalizeId(appDetails?.appid) ??
      pathId;

    setLaunchTarget(resolveRungameidTarget(shortcutId, true) ?? undefined);
  }, [pathId]);

  return launchTarget;
};

export default useAppId;
