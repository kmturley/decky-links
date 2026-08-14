import { useEffect, FC } from "react";
import {
  getSettings,
  getReaderStatus,
  getSourceStatuses,
  getActiveMedia,
  getKioskState,
  setRunningGame,
  sharedState,
  settingsRef,
  restrictedRef,
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
import { playSound, preloadSounds } from "./lib/sounds";
import { scenes } from "./lib/presentation";

let stopBackgroundManagerFn: (() => void) | null = null;
/** How long a launch may take before the plugin stops believing in it. */
const LAUNCH_ABANDONED_MS = 45_000;
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

    const statuses = await getSourceStatuses();
    if (active) {
      sharedState.sourceStatuses = statuses;
    }

    // Before the media seed below: a locked device must not draw the unlocked
    // panel, however briefly, and the launch path consults this.
    const restricted = await getKioskState();
    if (active) {
      sharedState.restricted = restricted;
      restrictedRef.current = restricted;
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

  // The scene, recomputed from what the plugin already knows.
  //
  // Derived rather than assigned: every caller pushes the same snapshot
  // through the same reducer, so a scene the facts do not support cannot be
  // reached by some path forgetting to reset a flag. `launching` is the one
  // fact with no backend equivalent — it is the window between accepting a URI
  // and the game painting, which only this side can see.
  let launching = false;
  const restate = (touch = true) => {
    if (touch) scenes.touch();
    const media = Object.values(sharedState.activeMedia);
    scenes.apply({
      reading: media.some((m) => m.problem === "loading"),
      launching,
      inGame: !!activeAppIdRef.current,
      failed: media.some((m) => m.problem === "unreadable" || m.problem === "blocked"),
      locked: !!restrictedRef.current?.locked,
    });
  };
  let abandonTimer: number | undefined;
  const setLaunching = (value: boolean) => {
    launching = value;
    window.clearTimeout(abandonTimer);
    if (value) {
      // A launch that never completes would otherwise leave the scene at
      // LAUNCHING for good: Steam never paints, so nothing composites the
      // layer away, and the Deck sits on a loading screen for a game that is
      // not coming. Generous, because a cold shader cache on a big game is
      // genuinely slow, but finite. Cleared here rather than in the layer so
      // the *scene* stops being a lie, and every renderer benefits.
      abandonTimer = window.setTimeout(() => {
        console.warn("[ Decky Links ] Launch abandoned — no game appeared");
        setLaunching(false);
      }, LAUNCH_ABANDONED_MS);
    }
    restate();
  };

  // What ends a launch, measured rather than assumed.
  //
  // The obvious signal — Router.MainRunningApp becoming non-null — fires 501ms
  // after RunGame on this device, while Steam's own launch flow runs for
  // another six seconds behind its "Starting launch…" card. Ending the splash
  // there cut it off almost immediately, which is exactly the bug this
  // instrumentation was written to find:
  //
  //   115ms  task CheckShaderDepotManifest
  //   501ms  MainRunningApp set        <- not the end of anything
  //   6835ms task WaitingGameWindow
  //   6841ms task Completed
  //   6918ms GameActionEnd             <- the end
  //
  // Deliberately not held until the game paints, even though the compositor
  // would remove the layer for us. The scene has to *stop* being LAUNCHING
  // sometime, and if it did not, pressing STEAM mid-game would bring the
  // splash back over a running game.
  const gameActionEnd = (window as any).SteamClient?.Apps?.RegisterForGameActionEnd?.(
    () => setLaunching(false),
  );

  // Input ends the ambient screen.
  //
  // Someone who picks the Deck up and presses a button has announced they are
  // there, and an attract screen that carries on regardless reads as a device
  // that has stopped listening. Nothing else can tell us: the plugin's own
  // events all come from media, and a person waking a Deck up touches no
  // media at all.
  //
  // Two doors, because there are two ways to touch a Deck. Buttons, sticks and
  // trackpads arrive here; the touchscreen arrives at VisualsLayer, which is
  // already intercepting taps so they cannot reach the Steam UI underneath,
  // and calls scenes.activity() with them.
  //
  // Measured before being used: with nobody touching the device this fired
  // exactly zero times in six seconds, which is the property that matters —
  // a stream that reported stick jitter or gyro drift would mean the ambient
  // screen could never be reached at all.
  const controllerInput =
    (window as any).SteamClient?.Input?.RegisterForControllerInputMessages?.(
      () => scenes.activity(),
    );

  // restate(false) rather than restate(): activity() has already restarted the
  // idle clock, and this only has to recompute now that it has.
  const stopWatchingActivity = scenes.onActivity(() => restate(false));

  // Sounds, played here rather than in the backend.
  //
  // The backend still decides which sound belongs to which event — it owns the
  // state machine — and sends the name. Only the speaker moved, because
  // `paplay` costs ~512ms of fixed overhead on a Deck and the feedback has to
  // land within 200ms of the tag touching the reader. See src/lib/sounds.ts.
  preloadSounds();
  const soundListener = addEventListener<[data: { sound?: string }]>(
    "play_sound",
    (data) => {
      if (data?.sound) playSound(data.sound);
    },
  );

  // A medium is present but not yet readable. Recorded as a normal entry with
  // problem: "loading" so it occupies its row the same way a real medium does
  // — the row is what the user is watching, and it has to stop saying "No
  // disk" the moment the disk goes in, not a minute later when it mounts.
  // Always superseded by media_detected/uri_detected for the same source.
  const loadingListener = addEventListener<[data: {
    source_id?: string, source_type?: string, media_id?: string, drive_kind?: string,
  }]>("media_loading", (data) => {
    if (!data?.source_id) return;
    sharedState.activeMedia = {
      ...sharedState.activeMedia,
      [data.source_id]: {
        source_id: data.source_id,
        source_type: (data.source_type as SourceType) ?? SourceType.STORAGE,
        media_id: data.media_id ?? "",
        uri: null,
        drive_kind: data.drive_kind ?? null,
        problem: "loading",
      },
    };
    notifySubscribers();
    restate();
  });

  // event listeners
  const tagListener = addEventListener<[data: {
    uid: string, source_type?: string, source_id?: string, drive_kind?: string,
  }]>("media_detected", (data) => {
    if (!data || typeof data.uid !== "string") return;
    // Per-source record, so a tag and a disk can be present at once and each
    // gets its own row and Pair button in the Triggers list. Absent
    // source_type means NFC: that is the only source that predates the field,
    // and every other one sets it explicitly.
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
    notifySubscribers();
  });

  const removeListener = addEventListener<[data?: { source_id?: string }]>("media_removed", (data) => {
    // Removing the medium ends whatever it was doing, including a launch that
    // was still in flight.
    launching = false;
    // Only the source that reported the removal loses its row. Clearing the
    // whole map when source_id was absent meant one trigger losing its medium
    // blanked every other trigger's row too — a floppy ejecting would erase
    // the tag still sitting on the reader. The backend always sends
    // source_id; without one there is nothing to act on.
    if (!data?.source_id) return;
    const { [data.source_id]: gone, ...rest } = sharedState.activeMedia;
    if (!gone) return;
    sharedState.activeMedia = rest;
    notifySubscribers();
  });

  const statusListener = addEventListener<[data: { connected: boolean, path?: string, source_type?: string }]>("source_connection", (data) => {
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
    source_type?: SourceType,
    blank?: boolean, unreadable?: boolean, blocked?: boolean, error?: string,
    formattable?: boolean, key?: boolean, authorized?: boolean,
  }]>("uri_detected", (data) => {
    if (!data || typeof data.uid !== "string") return;

    // uri_detected does not always carry source_id — the pairing sync path
    // addresses the medium by id — so fall back to the entry we already hold
    // for this media id.
    const existing = data.source_id
      ? sharedState.activeMedia[data.source_id]
      : Object.values(sharedState.activeMedia).find((m) => m.media_id === data.uid);
    const key = data.source_id ?? existing?.source_id;

    // A storage media_id is a device node, whose case is meaningful; an NFC
    // uid is hex and normalises upper. Take the source from the event, then
    // from the medium this actually refers to.
    const sourceType = data.source_type ?? existing?.source_type ?? SourceType.NFC;
    const normalizedUid = sourceType === SourceType.STORAGE
      ? data.uid
      : data.uid.toUpperCase();
    const uri = typeof data.uri === "string" ? data.uri : null;

    // A key carries no URI by design, so every "no URI" branch below
    // would mislabel it — as a blank tag, and then as an error sound and a
    // Pair button offering to overwrite the key with a game.
    if (data.key) {
      if (key && sharedState.activeMedia[key]) {
        sharedState.activeMedia = {
          ...sharedState.activeMedia,
          [key]: {
            ...sharedState.activeMedia[key],
            media_id: normalizedUid,
            uri: null,
            problem: null,
            key: true,
            authorized: data.authorized !== false,
          },
        };
      }
      notifySubscribers();
      if (data.authorized === false) {
        // Named rather than silent: a key that has stopped being recognised
        // looks exactly like a reader that has stopped reading.
        toaster.toast({
          title: "Not the key",
          body: "This medium is not registered on this device.",
          critical: true,
        });
      }
      return;
    }

    const problem = uri
      ? null
      : data.unreadable
        ? ({ kind: "unreadable", error: data.error } as const)
        : data.blocked
          ? ({ kind: "blocked" } as const)
          : ({ kind: "blank" } as const);

    if (key && sharedState.activeMedia[key]) {
      sharedState.activeMedia = {
        ...sharedState.activeMedia,
        [key]: {
          ...sharedState.activeMedia[key],
          media_id: normalizedUid,
          uri,
          problem: problem?.kind ?? null,
          error: problem?.kind === "unreadable" ? data.error : undefined,
          // Only meaningful while the medium is unreadable; cleared otherwise
          // so a disk that later mounts cannot keep offering to erase itself.
          formattable: problem?.kind === "unreadable" && !!data.formattable,
        },
      };
    }
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

      // No allowlist check while locked, deliberately. A URI arriving here came
      // off a medium someone physically presented, and "a medium vouches for
      // it" is the whole launch rule (SPEC §16.3) — the box of tags left out is
      // the allowlist. This used to ask Steam's Family View whether the game
      // was permitted, which refused games on a list the user never built.

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
          //
          // The splash goes up here rather than on uri_detected: a URI that
          // turns out to be blocked, or a game already running, never reaches
          // this line, and a splash for a launch that was never attempted is
          // the flicker MIN_VISIBLE_MS exists to prevent.
          setLaunching(true);
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

  // The lock changed — the key was presented, or taken away. The backend owns
  // the state and derives it from the key; this half only mirrors it into the
  // panel, which is why locking no longer calls out to Steam at all.
  const restrictedLockListener = addEventListener<[data: {
    locked: boolean, has_key: boolean, label: string, reason?: string,
  }]>("restricted_lock", (data) => {
    if (!data || typeof data.locked !== "boolean") return;
    const { reason, ...state } = data;
    sharedState.restricted = state;
    restrictedRef.current = state;
    notifySubscribers();
    restate();
    console.info(`[ Decky Links ] Restricted mode ${data.locked ? "on" : "off"} (${reason ?? "?"})`);
  });

  // Registering or deregistering the key: state only, no lock change.
  const restrictedStateListener = addEventListener<[data: {
    locked: boolean, has_key: boolean, label: string,
  }]>("restricted_state", (data) => {
    if (!data || typeof data.locked !== "boolean") return;
    sharedState.restricted = data;
    restrictedRef.current = data;
    notifySubscribers();
    restate();
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

  // Restricted mode: a game started without a medium to vouch for it.
  //
  // The backend decides — it holds every presented medium and the launch
  // attribution — and this end carries it out, because TerminateApp lives on
  // SteamClient. The game does visibly start before it closes; that is the
  // honest cost of enforcing this without Valve's cooperation, and it is a
  // deterrent rather than a boundary, which is what the README says.
  const restrictedListener = addEventListener<[data: { appid: number }]>(
    "restricted_game",
    (data) => {
      if (!data || data.appid === undefined || data.appid === null) return;
      const appId = String(data.appid);
      console.info(`[ Decky Links ] Restricted mode: closing unauthorised game ${appId}.`);
      void (async () => {
        const closed = await terminateSteamApp(appId);
        toaster.toast({
          title: "Restricted title",
          body: closed
            ? "In restricted mode, present the tag or disk for a game to play it."
            : "This game is not allowed, and Steam would not close it.",
          critical: true,
        });
      })();
    },
  );

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
            // Say so. The user took the tag off expecting the game to close;
            // when it does not, silence reads as "the plugin didn't notice"
            // and sends them to re-tap the tag, which cannot help. Naming the
            // game makes clear the removal *was* seen and the close is what
            // failed.
            toaster.toast({
              title: "Could not close game",
              body: "Steam did not close it. Exit from the game's menu.",
              critical: true,
            });
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

  // Backstop for the event stream above.
  //
  // Only game state genuinely needs the fast tick: nothing on the backend can
  // see Router.MainRunningApp, so a launch or exit is invisible until we look
  // — and it is a local read, not an RPC.
  //
  // Everything else here duplicates something the backend already pushes
  // (source_connection, source_statuses), so it is a recovery path for a dropped
  // event rather than the way state normally arrives. Running those at 2 Hz
  // cost two RPC round-trips a second for the life of the plugin, on a
  // battery-powered handheld, to re-learn things that had not changed.
  const pollLoop = async () => {
    let sourcePollTick = 0;
    while (active) {
      try {
        // 1. Game status — local read, every tick.
        const app = getMainRunningApp();
        const currentId = (app && app.appid !== "0") ? String(app.appid) : null;

        if (currentId !== activeAppIdRef.current) {
          console.info(`[ Decky Links ] Game change: ${activeAppIdRef.current} -> ${currentId}`);
          activeAppIdRef.current = currentId;
          sharedState.activeAppId = currentId;
          notifySubscribers();
          // Not the end of the launch — see RegisterForGameActionEnd above.
          // A game *disappearing*, though, ends anything in flight.
          if (!currentId) launching = false;
          restate();
          await setRunningGame(currentId ? parseInt(currentId) : null);
        }

        // 1b. Is anything of Steam's on top of its own interface?
        //
        // Two tests, because one is not enough and it took measuring to find
        // that out. Sampling Steam's state twice a second while the theme
        // picker's dropdown was opened by hand gave:
        //
        //   focused=SP BPM_uid0  root=SP BPM_uid0  sideMenu=0   (idle)
        //   focused=QuickAccess  root=SP BPM_uid0  sideMenu=2   (menu open)
        //   focused=SP BPM_uid0  root=SP BPM_uid0  sideMenu=0   (dropdown open!)
        //
        // Opening the dropdown closes the Quick Access menu *and* hands focus
        // back to the main window, while the option list stays on screen. So
        // no window-level signal distinguishes "dropdown open" from "idle" —
        // both a menu-store test and a focus test report nothing, and the
        // layer painted over the list the user was reading.
        //
        // What does distinguish it is the list itself, in the main window's
        // DOM. Plugin code runs in SharedJSContext, so that document is
        // reached through Steam's own window handle; it is same-origin, so
        // this is a plain query rather than anything clever.
        //
        // Both are evaluated in the same tick deliberately: the menu closes
        // and the popup appears together, so a sample sees one or the other
        // and never a gap between them.
        const ctx = (window as any).FocusNavController?.m_ActiveContext;
        const focused = ctx?.m_activeWindow?.name;
        const root = ctx?.m_rootWindow?.name;
        const focusElsewhere = !!focused && !!root && focused !== root;

        let popupOpen = false;
        try {
          const doc = (window as any).SteamUIStore?.WindowStore
            ?.GamepadUIMainWindowInstance?.BrowserWindow?.document;
          // Steam leaves menu markup in the DOM after a menu closes, so
          // presence is not enough — only a laid-out box counts.
          const items = doc?.querySelectorAll?.(
            '.contextMenuItem, [class*="contextmenu" i]',
          );
          popupOpen = !!items && [...items].some((el: any) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          });
        } catch {
          // A future Steam build could rename the class or move the window
          // handle. Then this reports false and the behaviour is what it was
          // before this fix — a theme over a dropdown — rather than a crash in
          // the loop that also drives launching.
          popupOpen = false;
        }

        const overlayOpen = focusElsewhere || popupOpen;
        if (overlayOpen !== sharedState.steamOverlayOpen) {
          sharedState.steamOverlayOpen = overlayOpen;
          notifySubscribers();
        }

        // 2. Everything reached over RPC, every 10th tick (~5s).
        //
        // All three are pushed by the backend when they change, so this is
        // the dropped-event backstop. The media re-sync used to poll the NFC
        // reader alone and could not recover a missed floppy insert or QR
        // frame; the per-source registry covers every trigger.
        sourcePollTick++;
        if (sourcePollTick >= 10) {
          sourcePollTick = 0;
          const [reader, statuses, media] = await Promise.all([
            getReaderStatus(),
            getSourceStatuses(),
            getActiveMedia(),
          ]);
          if (
            active &&
            (sharedState.readerStatus.connected !== reader.connected ||
              sharedState.readerStatus.path !== reader.path ||
              sharedState.readerStatus.source_type !== reader.source_type)
          ) {
            sharedState.readerStatus = reader;
          }
          if (active) {
            sharedState.sourceStatuses = statuses;
            sharedState.activeMedia = Object.fromEntries(
              (media ?? []).map((m) => [m.source_id, {
                ...m,
                // Preserve the richer local view: the backend registry has no
                // notion of "loading" or "unreadable", so a blind overwrite
                // would flick a mounting floppy back to "blank".
                problem: sharedState.activeMedia[m.source_id]?.problem
                  ?? (m.uri ? null : ("blank" as const)),
                error: sharedState.activeMedia[m.source_id]?.error,
              }]),
            );
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

  // AMBIENT is reached by nothing happening, which no event can announce. One
  // slow tick, deliberately far slower than the poll loop: it only has to
  // notice a 90-second threshold, and this is the one timer that runs while
  // the Deck is idle.
  const sceneTicker = window.setInterval(() => restate(false), 10_000);

  stopBackgroundManagerFn = () => {
    active = false;
    clearInterval(sceneTicker);
    clearTimeout(abandonTimer);
    gameActionEnd?.unregister?.();
    controllerInput?.unregister?.();
    stopWatchingActivity();
    removeEventListener("play_sound", soundListener);
    removeEventListener("media_loading", loadingListener);
    removeEventListener("media_detected", tagListener);
    removeEventListener("media_removed", removeListener);
    removeEventListener("source_connection", statusListener);
    removeEventListener("uri_detected", uriListener);
    removeEventListener("pairing_result", pairingListener);
    removeEventListener("restricted_lock", restrictedLockListener);
    removeEventListener("restricted_state", restrictedStateListener);
    removeEventListener("restricted_game", restrictedListener);
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
