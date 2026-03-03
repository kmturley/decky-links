import { useEffect, FC } from "react";
import {
  getSettings,
  getReaderStatus,
  getTagStatus,
  setRunningGame,
  sharedState,
  settingsRef,
  tagUidRef,
  activeAppIdRef,
  notifySubscribers,
  addEventListener,
  removeEventListener,
  toaster,
  pairingToastsSuppressed,
} from "./shared";
import { Navigation, Router, sleep, SideMenu } from "@decky/ui";
import { extractComparableAppIdFromRungameid } from "./lib/steamIds";

let stopBackgroundManagerFn: (() => void) | null = null;
const STEAM_RUN_PREFIX = "steam://run/";
const STEAM_RUNGAMEID_PREFIX = "steam://rungameid/";
let cachedSteamUriLauncher: ((uri: string) => void) | null = null;
const failedSteamUriLaunchers = new Set<string>();

function toSignedInt32String(id: string): string {
  const n = Number(id);
  if (!Number.isFinite(n)) return id;
  const u32 = n >>> 0;
  return u32 > 0x7FFFFFFF ? String(u32 - 0x100000000) : String(u32);
}

function getMainRunningApp() {
  const appRaw = Router.MainRunningApp;
  return typeof appRaw === "function" ? (appRaw as any)() : appRaw;
}

function parseSteamAppIdFromUri(uri: string | null): string | null {
  if (!uri) {
    return null;
  }
  if (uri.startsWith(STEAM_RUN_PREFIX)) {
    return uri.replace(STEAM_RUN_PREFIX, "").split("/")[0] || null;
  }
  if (uri.startsWith(STEAM_RUNGAMEID_PREFIX)) {
    const value = uri.replace(STEAM_RUNGAMEID_PREFIX, "").split("/")[0] || null;
    return value ? extractComparableAppIdFromRungameid(value) : null;
  }
  return null;
}

function extractRungameidFromUri(uri: string | null): string | null {
  if (!uri || !uri.startsWith(STEAM_RUNGAMEID_PREFIX)) return null;
  return uri.replace(STEAM_RUNGAMEID_PREFIX, "").split("/")[0] || null;
}

function canSkipLaunch(currentAppId: string | null, uriAppId: string | null): boolean {
  return !!(currentAppId && uriAppId && String(currentAppId) === String(uriAppId));
}

function launchViaSteamClientUri(uri: string): boolean {
  if (cachedSteamUriLauncher) {
    try {
      cachedSteamUriLauncher(uri);
      return true;
    } catch (e) {
      console.warn("[ Decky Links ] Cached Steam URI launcher failed. Re-probing:", e);
      cachedSteamUriLauncher = null;
    }
  }

  // Steam runtime variants expose URL launchers on different namespaces.
  const candidates: Array<{ path: string[]; args: unknown[] }> = [
    { path: ["URL", "ExecuteSteamURL"], args: [uri] },
    { path: ["System", "ExecuteSteamURL"], args: [uri] },
    { path: ["URL", "Open"], args: [uri] },
    { path: ["URL", "OpenURL"], args: [uri] },
    { path: ["URL", "Navigate"], args: [uri] },
    { path: ["System", "OpenURL"], args: [uri] },
    { path: ["Browser", "OpenURL"], args: [uri] },
    { path: ["BrowserView", "OpenURL"], args: [uri] },
  ];

  for (const { path, args } of candidates) {
    const key = path.join(".");
    if (failedSteamUriLaunchers.has(key)) continue;

    try {
      const root = (window as any).SteamClient;
      const target = path.slice(0, -1).reduce((obj, key) => obj?.[key], root);
      const method = target?.[path[path.length - 1]];
      if (typeof method !== "function") continue;
      method.call(target, ...args);
      cachedSteamUriLauncher = (nextUri: string) => method.call(target, nextUri);
      console.info(`[ Decky Links ] Launching Steam URI via SteamClient.${key}: ${uri}`);
      return true;
    } catch (e) {
      failedSteamUriLaunchers.add(key);
      console.debug(`[ Decky Links ] SteamClient.${key} unavailable for URI launch:`, e);
    }
  }

  return false;
}

function executeSteamUri(uri: string): void {
  if (launchViaSteamClientUri(uri)) return;
  console.info(`[ Decky Links ] Launching Steam URI via navigation fallback: ${uri}`);
  Navigation.Navigate(uri);
}

function launchSteamUri(uri: string): void {
  if (uri.startsWith(STEAM_RUNGAMEID_PREFIX)) {
    // Shortcut/non-Steam launches are reliably handled through rungameid URIs.
    executeSteamUri(uri);
    return;
  }

  const appId = parseSteamAppIdFromUri(uri);
  if (!appId) {
    console.warn(`[ Decky Links ] Unable to parse Steam URI: ${uri}`);
    executeSteamUri(uri);
    return;
  }

  const signedAppId = toSignedInt32String(appId);

  try {
    // @ts-ignore
    if (window.SteamClient?.Apps?.RunGame) {
      // @ts-ignore
      window.SteamClient.Apps.RunGame(signedAppId, "", -1, 100);
      return;
    }
  } catch (e) {
    console.error(`[ Decky Links ] RunGame failed for ${signedAppId}, falling back to URI execution:`, e);
  }

  executeSteamUri(uri);
}

function isAppStillRunning(appId: string): boolean {
  const app = getMainRunningApp();
  const currentId = (app && app.appid !== "0") ? String(app.appid) : null;
  return currentId === appId;
}

async function terminateSteamApp(appId: string, launchUri?: string): Promise<boolean> {
  // @ts-ignore
  const terminate = window.SteamClient?.Apps?.TerminateApp;
  if (typeof terminate !== "function") return false;

  const rungameid = extractRungameidFromUri(launchUri ?? null);
  const targetId = String(rungameid ?? appId);

  try {
    // Non-Steam shortcuts are reliably terminated by rungameid (gameID64).
    (terminate as any).call((window as any).SteamClient.Apps, targetId, true);
    console.info(`[ Decky Links ] TerminateApp invoked with args=${JSON.stringify([targetId, true])}`);
  } catch (e) {
    console.debug(`[ Decky Links ] TerminateApp failed for args=${JSON.stringify([targetId, true])}:`, e);
    return false;
  }

  // Verify closure instead of assuming success from an accepted API call.
  for (let i = 0; i < 6; i++) {
    await sleep(500);
    if (!isAppStillRunning(appId)) {
      return true;
    }
  }

  return false;
}

export function startBackgroundManager(): () => void {
  if (stopBackgroundManagerFn) {
    return stopBackgroundManagerFn;
  }

  let active = true;

  const init = async () => {
    const s = await getSettings();
    if (!active) return;
    sharedState.settings = s;
    settingsRef.current = s;

    const stat = await getReaderStatus();
    if (active) {
      sharedState.readerStatus = stat;
    }

    const tag = await getTagStatus();
    if (active && tag.uid) {
      sharedState.tagUid = tag.uid;
      sharedState.tagUri = tag.uri;
      tagUidRef.current = tag.uid;
    } else if (active) {
      sharedState.tagUid = null;
      sharedState.tagUri = null;
      tagUidRef.current = null;
    }

    notifySubscribers();
  };
  init();

  // event listeners
  const tagListener = addEventListener<[data: { uid: string }]>("tag_detected", (data) => {
    sharedState.tagUid = data.uid;
    sharedState.tagUri = null;
    tagUidRef.current = data.uid;
    notifySubscribers();
  });

  const removeListener = addEventListener("tag_removed", () => {
    sharedState.tagUid = null;
    sharedState.tagUri = null;
    tagUidRef.current = null;
    notifySubscribers();
  });

  const statusListener = addEventListener<[data: { connected: boolean, path: string }]>("reader_status", (data) => {
    sharedState.readerStatus = data;
    notifySubscribers();
  });

  const uriListener = addEventListener<[data: { uri: string | null, uid: string }]>("uri_detected", (data) => {
    sharedState.tagUri = data.uri;
    sharedState.tagUid = data.uid.toUpperCase();
    tagUidRef.current = data.uid.toUpperCase();
    notifySubscribers();

    toaster.toast({
      title: data.uri ? `Tag: ${data.uid}` : "NFC Tag Detected",
      body: data.uri ? `Url: ${data.uri}` : "No URI found on tag",
    });

    if (data.uri) {
      const currentSettings = settingsRef.current;
      if (currentSettings?.auto_launch) {
        const currentAppId = activeAppIdRef.current;
        const uriAppId = parseSteamAppIdFromUri(data.uri);

        if (canSkipLaunch(currentAppId, uriAppId)) {
          console.info(`[ Decky Links ] Game ${currentAppId} is already running. Skipping redundant launch.`);
          return;
        }

        if (data.uri.startsWith(STEAM_RUN_PREFIX) || data.uri.startsWith(STEAM_RUNGAMEID_PREFIX)) {
          console.info(`[ Decky Links ] Launching Steam URI: ${data.uri}`);
          launchSteamUri(data.uri);
          return;
        }

        console.info(`[ Decky Links ] Navigation fallback: ${data.uri}`);
        Navigation.Navigate(data.uri);
      }
    }
  });

  const pairingListener = addEventListener<[data: { success: boolean, uid: string, error?: string }]>("pairing_result", (data) => {
    sharedState.pairing = false;
    notifySubscribers();
    if (!pairingToastsSuppressed()) {
      toaster.toast({
        title: data.success ? "Pairing Success" : "Pairing Failed",
        body: data.success ? `Game paired to tag ${data.uid}` : (data.error || "Write failed."),
        critical: !data.success,
        duration: 3000
      });
    }
  });

  const gameRemovalListener = addEventListener<[data: { appid: number, uid: string, uri: string }]>("card_removed_during_game", (data) => {
    const currentAppId = activeAppIdRef.current;
    const currentSettings = settingsRef.current;
    const uriAppId = parseSteamAppIdFromUri(data.uri);

    if (canSkipLaunch(currentAppId, uriAppId)) {
      if (currentSettings?.auto_close) {
        console.info(`[ Decky Links ] Paired tag removed. Auto-closing game: ${currentAppId}`);
        void (async () => {
          if (!currentAppId || !(await terminateSteamApp(String(currentAppId), data.uri))) {
            console.warn(`[ Decky Links ] Failed to terminate app ${currentAppId ?? "unknown"}.`);
          }
        })();
      } else {
        console.info(`[ Decky Links ] Paired tag removed. Pausing game: ${currentAppId}`);
        Navigation.CloseSideMenus();
        Navigation.OpenSideMenu(SideMenu.Main);
      }
    } else {
      console.info(`[ Decky Links ] Tag removed but game not running (currentAppId=${currentAppId}, uriAppId=${uriAppId}). Ignoring.`);
    }
  });

  // polling loop omitted for brevity

  const pollLoop = async () => {
    while (active) {
      try {
        // 1. Poll Game Status
        const app = getMainRunningApp();
        const currentId = (app && app.appid !== "0") ? String(app.appid) : null;

        if (currentId !== activeAppIdRef.current) {
          console.info(`[ Decky Links ] Game change: ${activeAppIdRef.current} -> ${currentId}`);
          activeAppIdRef.current = currentId;
          sharedState.activeAppId = currentId;
          notifySubscribers();
          await setRunningGame(currentId ? parseInt(currentId) : null);
        }

        // 2. Poll Tag Status (if missing)
        if (!tagUidRef.current) {
          const t = await getTagStatus();
          if (active && t.uid) {
            if (sharedState.tagUid !== t.uid || sharedState.tagUri !== t.uri) {
              sharedState.tagUid = t.uid;
              sharedState.tagUri = t.uri;
              notifySubscribers();
            }
            tagUidRef.current = t.uid;
          }
        }

        // 3. Poll Reader Status
        const reader = await getReaderStatus();
        if (
          active &&
          (sharedState.readerStatus.connected !== reader.connected ||
            sharedState.readerStatus.path !== reader.path)
        ) {
          sharedState.readerStatus = reader;
          notifySubscribers();
        }

      } catch (e) {
        console.error("[ Decky Links ] Polling loop error:", e);
      }

      await sleep(500);
    }
  };
  pollLoop();

  stopBackgroundManagerFn = () => {
    active = false;
    removeEventListener("tag_detected", tagListener);
    removeEventListener("tag_removed", removeListener);
    removeEventListener("reader_status", statusListener);
    removeEventListener("uri_detected", uriListener);
    removeEventListener("pairing_result", pairingListener);
    removeEventListener("card_removed_during_game", gameRemovalListener);
    stopBackgroundManagerFn = null;
  };

  return stopBackgroundManagerFn;
}

// Backward-compatible wrapper if rendered as a component.
export const BackgroundManager: FC = () => {
  useEffect(() => startBackgroundManager(), []);
  // Background manager itself doesn't render anything visible.  The
  // game-page pairer is now injected via a router patch, so there is no
  // need to render it here any more.
  return null;
};
