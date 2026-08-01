import { useEffect, FC } from "react";
import {
  getSettings,
  getReaderStatus,
  getTagStatus,
  getSourceStatuses,
  getActiveMedia,
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
  SourceType,
} from "./shared";
import { Navigation, Router, sleep, SideMenu } from "@decky/ui";
import { comparableAppIdFromUri as parseSteamAppIdFromUri } from "./lib/steamIds";

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

function extractRungameidFromUri(uri: string | null): string | null {
  if (!uri || !uri.startsWith(STEAM_RUNGAMEID_PREFIX)) return null;
  return uri.replace(STEAM_RUNGAMEID_PREFIX, "").split("/")[0] || null;
}

function canSkipLaunch(currentAppId: string | null, uriAppId: string | null): boolean {
  return !!(currentAppId && uriAppId && String(currentAppId) === String(uriAppId));
}

/** True when the user is already looking at this game's detail page.
 *
 * Executing a steam:// URL is not free: Steam tears down and rebuilds UI around
 * it, which closes the Quick Access menu. Navigating to a page that is already
 * open is pure cost, so check before issuing the command rather than after.
 */
function isAlreadyViewing(uriAppId: string | null): boolean {
  const viewed = sharedState.viewedApp;
  return !!(viewed && uriAppId && String(viewed.appId) === String(uriAppId));
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
  if (typeof terminate !== "function") {
    console.warn(`[ Decky Links ] TerminateApp not available on SteamClient.Apps`);
    return false;
  }

  const rungameid = extractRungameidFromUri(launchUri ?? null);
  const targetId = String(rungameid ?? appId);

  try {
    // Non-Steam shortcuts are reliably terminated by rungameid (gameID64).
    (terminate as any).call((window as any).SteamClient.Apps, targetId, true);
    console.info(`[ Decky Links ] TerminateApp invoked with args=${JSON.stringify([targetId, true])}`);
  } catch (e) {
    console.error(`[ Decky Links ] TerminateApp call failed for args=${JSON.stringify([targetId, true])}:`, e);
    return false;
  }

  // Verify closure instead of assuming success from an accepted API call.
  for (let i = 0; i < 6; i++) {
    await sleep(500);
    if (!isAppStillRunning(appId)) {
      console.info(`[ Decky Links ] App ${appId} terminated successfully after ${(i + 1) * 500}ms`);
      return true;
    }
  }

  console.warn(`[ Decky Links ] App ${appId} did not terminate within 3000ms timeout`);
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
      // get_tag_status only ever reports the NFC reader's view.
      sharedState.tagSourceType = SourceType.NFC;
      tagUidRef.current = tag.uid;
    } else if (active) {
      sharedState.tagUid = null;
      sharedState.tagUri = null;
      sharedState.tagSourceType = null;
      tagUidRef.current = null;
    }

    const statuses = await getSourceStatuses();
    if (active) {
      sharedState.sourceStatuses = statuses;
    }

    // Seed the per-source view: media already presented before the panel was
    // ever opened would otherwise be invisible until it is removed and
    // re-presented.
    const media = await getActiveMedia();
    if (active) {
      sharedState.activeMedia = Object.fromEntries(
        (media ?? []).map((m) => [m.source_id, {
          ...m,
          problem: m.uri ? null : ("blank" as const),
        }]),
      );
    }

    notifySubscribers();
  };
  init();

  // event listeners
  const tagListener = addEventListener<[data: {
    uid: string, source_type?: string, source_id?: string, drive_kind?: string,
  }]>("tag_detected", (data) => {
    if (!data || typeof data.uid !== "string") return;
    sharedState.tagUid = data.uid;
    sharedState.tagUri = null;
    // Absent source_type means NFC: that is the only source that predates the
    // field, and every other one sets it explicitly.
    sharedState.tagSourceType = (data.source_type as SourceType) ?? SourceType.NFC;
    sharedState.mediaProblem = null;
    // Per-source record, so a tag and a disk can be present at once and each
    // gets its own row and Pair button in the Triggers list.
    if (data.source_id) {
      sharedState.activeMedia = {
        ...sharedState.activeMedia,
        [data.source_id]: {
          source_id: data.source_id,
          source_type: (data.source_type as SourceType) ?? SourceType.NFC,
          media_id: data.uid,
          uri: null,
          drive_kind: data.drive_kind ?? null,
          problem: null,
        },
      };
    }
    tagUidRef.current = data.uid;
    notifySubscribers();
  });

  const removeListener = addEventListener<[data?: { source_id?: string }]>("tag_removed", (data) => {
    sharedState.tagUid = null;
    sharedState.tagUri = null;
    sharedState.tagSourceType = null;
    sharedState.mediaProblem = null;
    if (data?.source_id) {
      const { [data.source_id]: _gone, ...rest } = sharedState.activeMedia;
      sharedState.activeMedia = rest;
    } else {
      sharedState.activeMedia = {};
    }
    tagUidRef.current = null;
    notifySubscribers();
  });

  const statusListener = addEventListener<[data: { connected: boolean, path?: string, source_type?: string }]>("reader_status", (data) => {
    if (!data || typeof data.connected !== "boolean") return;
    sharedState.readerStatus = {
      connected: data.connected,
      path: data.path,
      source_type: data.source_type as SourceType | undefined,
    };
    notifySubscribers();
  });

  const sourceStatusesListener = addEventListener<[data: any[]]>("source_statuses", (data) => {
    if (!Array.isArray(data)) return;
    sharedState.sourceStatuses = data;
    notifySubscribers();
  });

  const uriListener = addEventListener<[data: {
    uri: string | null, uid: string, paired?: boolean, source_id?: string,
    blank?: boolean, unreadable?: boolean, blocked?: boolean, error?: string,
  }]>("uri_detected", (data) => {
    if (!data || typeof data.uid !== "string") return;
    // A storage media_id is a device node, whose case is meaningful.
    const normalizedUid = sharedState.tagSourceType === SourceType.STORAGE
      ? data.uid
      : data.uid.toUpperCase();
    const uri = typeof data.uri === "string" ? data.uri : null;

    sharedState.tagUri = uri;
    sharedState.tagUid = normalizedUid;
    const problem = uri
      ? null
      : data.unreadable
        ? ({ kind: "unreadable", error: data.error } as const)
        : data.blocked
          ? ({ kind: "blocked" } as const)
          : ({ kind: "blank" } as const);
    sharedState.mediaProblem = problem;

    // uri_detected does not always carry source_id (the pairing sync path
    // emits it by media id), so fall back to matching the medium we already
    // recorded for this uid.
    const key = data.source_id
      ?? Object.values(sharedState.activeMedia).find((m) => m.media_id === data.uid)?.source_id;
    if (key && sharedState.activeMedia[key]) {
      sharedState.activeMedia = {
        ...sharedState.activeMedia,
        [key]: {
          ...sharedState.activeMedia[key],
          uri,
          problem: problem?.kind ?? null,
          error: problem?.kind === "unreadable" ? data.error : undefined,
        },
      };
    }
    tagUidRef.current = normalizedUid;
    notifySubscribers();

    // Emitted by the backend right after writing a tag, purely so the panel
    // stops showing "Url: Empty". Pairing must not also launch the game --
    // the user pressed a button that only promised to write the card.
    if (data.paired) {
      console.info(`[ Decky Links ] Tag paired with ${uri}; updating display only.`);
      return;
    }

    if (uri) {
      const currentSettings = settingsRef.current;
      const uriAppId = parseSteamAppIdFromUri(uri);

      if (currentSettings?.auto_launch) {
        const currentAppId = activeAppIdRef.current;

        if (canSkipLaunch(currentAppId, uriAppId)) {
          console.info(`[ Decky Links ] Game ${currentAppId} is already running. Skipping redundant launch.`);
          return;
        }

        if (uri.startsWith(STEAM_RUN_PREFIX) || uri.startsWith(STEAM_RUNGAMEID_PREFIX)) {
          console.info(`[ Decky Links ] Launching Steam URI: ${uri}`);
          // Set backend state BEFORE launch to prevent race condition
          // This ensures the backend knows a game is launching before the frontend triggers it
          if (uriAppId) {
            setRunningGame(parseInt(uriAppId))
              .then(() => {
                console.info(`[ Decky Links ] Backend state updated to game ${uriAppId}`);
              })
              .catch((e) => {
                console.error(`[ Decky Links ] Failed to update backend state: ${e}`);
              });
          }
          // Now launch the game after backend is ready
          launchSteamUri(uri);
          return;
        }

        console.info(`[ Decky Links ] Navigation fallback: ${uri}`);
        Navigation.Navigate(uri);
        return;
      }

      // Auto-launch disabled: surface the linked game by opening details page.
      if (uriAppId) {
        if (isAlreadyViewing(uriAppId)) {
          console.info(`[ Decky Links ] Already viewing game ${uriAppId}. Skipping redundant navigation.`);
          return;
        }
        if (canSkipLaunch(activeAppIdRef.current, uriAppId)) {
          console.info(`[ Decky Links ] Game ${uriAppId} is already running. Skipping redundant navigation.`);
          return;
        }
        const detailsUri = `steam://open/games/details/${uriAppId}`;
        console.info(`[ Decky Links ] Auto-launch disabled. Opening game details: ${detailsUri}`);
        executeSteamUri(detailsUri);
      }
    }
  });

  const pairingListener = addEventListener<[data: { success: boolean, uid: string, error?: string }]>("pairing_result", (data) => {
    if (!data || typeof data.success !== "boolean") return;
    sharedState.pairing = false;
    notifySubscribers();
    if (!data.success && !pairingToastsSuppressed()) {
      toaster.toast({
        title: "Pairing Failed",
        body: data.error || "Write failed.",
        critical: true,
        duration: 3000
      });
    }
  });

  const gameRemovalListener = addEventListener<[data: {
    appid: number, uid: string, uri: string, action?: "close" | "pause",
  }]>("card_removed_during_game", (data) => {
    if (!data || typeof data.uri !== "string") return;
    const currentAppId = activeAppIdRef.current;
    const currentSettings = settingsRef.current;
    const uriAppId = parseSteamAppIdFromUri(data.uri);

    // The backend decides close-vs-pause: it is the only side that knows which
    // medium launched this game. Fall back to the local setting only if we are
    // talking to an older backend that does not send the field.
    const action = data.action ?? (currentSettings?.auto_close ? "close" : "pause");

    if (canSkipLaunch(currentAppId, uriAppId)) {
      if (action === "close") {
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
    let sourcePollTick = 0;
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
              sharedState.tagSourceType = SourceType.NFC;
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
            sharedState.readerStatus.path !== reader.path ||
            sharedState.readerStatus.source_type !== reader.source_type)
        ) {
          sharedState.readerStatus = reader;
          notifySubscribers();
        }

        // 4. Poll Source Statuses every 10 iterations (~5s)
        sourcePollTick++;
        if (sourcePollTick >= 10) {
          sourcePollTick = 0;
          const statuses = await getSourceStatuses();
          if (active) {
            sharedState.sourceStatuses = statuses;
            notifySubscribers();
          }
        }

      } catch (e) {
        console.error("[ Decky Links ] Polling loop error:", e);
      }

      if (active) {
        await sleep(500);
      }
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
    removeEventListener("source_statuses", sourceStatusesListener);
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
