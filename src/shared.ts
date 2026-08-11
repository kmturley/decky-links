import { addEventListener, removeEventListener, callable, toaster } from "@decky/api";
import { useState, useEffect } from "react";

// ─────────────────────────────────────────────────────────────────────────────
// Type definitions
// ─────────────────────────────────────────────────────────────────────────────

export interface Settings {
    auto_launch: boolean;
    auto_close: boolean;
    /** Custom home and loading screens (issue #8), which replace Steam's own
     *  interface while on. Off by default: a plugin should not take over the
     *  whole device uninvited. */
    custom_visuals?: boolean;
    /** Which theme paints it. Unknown ids fall back rather than fail. */
    theme?: string;
    sources: {
        nfc: {
            device_path: string;
            baudrate: number;
            polling_interval: number;
            reader_type: "pn532_uart" | "acr122u" | "proxmark" | "nfcpy";
        };
        storage?: { enabled: boolean; drive_kinds?: Record<string, boolean> };
        camera?: { enabled: boolean; device: string; poll_interval: number };
        /** `secret` is mandatory: MQTT will not start without one, because
         *  anything able to publish to the topic can launch games on this
         *  device. Enabling the source mints one if it is empty. */
        mqtt?: {
            enabled: boolean;
            broker_host: string;
            broker_port: number;
            topic: string;
            secret: string;
            tls?: boolean;
            username?: string;
            password?: string;
        };
        serial?: { enabled: boolean; port: string; baudrate: number };
        file_watch?: { enabled: boolean; watch_dir: string; poll_interval: number };
    };
}

/** A medium currently presented on some trigger. Keyed by source_id, because
 *  a tag on the reader and a disk in the drive are simultaneously present and
 *  each needs its own row and its own Pair button. */
export interface ActiveMedium {
    source_id: string;
    source_type: SourceType;
    media_id: string;
    uri: string | null;
    drive_kind?: string | null;
    /** "loading" is a medium we know is there but cannot read yet — a floppy
     *  takes up to a minute to mount, and the row must not read "No disk"
     *  for that whole time. Always replaced by a real state. */
    problem?: "blank" | "unreadable" | "blocked" | "loading" | null;
    error?: string;
    /** This medium carries a key rather than a link. The token itself
     *  never reaches the frontend — the backend recognises it and sends this
     *  flag in its place. */
    key?: boolean;
    /** Set on a key medium the backend did not recognise. */
    authorized?: boolean;
    /** Whether offering to format this medium would destroy anything.
     *
     *  Set by the backend only when blkid found no filesystem at all. A disk
     *  holding a filesystem we do not mount (ntfs, hfsplus) is also
     *  "unreadable" but has data on it, so the Format button must key off this
     *  flag rather than off `problem === "unreadable"`. */
    formattable?: boolean;
}

/** Restricted mode, as much of it as the panel is allowed to know.
 *
 *  The key's hash never appears here — the panel needs to know *that* a key
 *  exists to decide what to offer, never what it is. */
export interface RestrictedState {
    locked: boolean;
    has_key: boolean;
    /** What the key is, in words: "USB drive", "NFC tag". The user has to find
     *  the object again, and a device node will not help them do that. */
    label: string;
}

export interface DriveKindStatus {
    present: boolean;
    enabled: boolean;
}

export interface SourceStatus {
    source_id: string;
    source_type: SourceType;
    /** The hardware is connected — a floppy drive with no disk in it counts. */
    active: boolean;
    /** Media is actually loaded: a disk in the drive, a tag on the reader. */
    has_media?: boolean;
    /** Media on this source can be written to, i.e. paired. */
    can_pair?: boolean;
    /** The user's on/off switch. A disabled source idles rather than
     *  retrying its hardware forever, so "off" and "not plugged in" are
     *  different things and must look different. */
    enabled?: boolean;
    /** Storage only: one source covers several kinds of drive, and the panel
     *  shows a row per kind. */
    drive_kinds?: Record<string, DriveKindStatus>;
}

export interface ReaderStatus {
    connected: boolean;
    path?: string;
    source_type?: SourceType;
}

export interface SectorInfo {
    sector: number;
    first_block: number;
    trailer_block: number;
    locked: boolean;
    readable: boolean;
    writable: boolean;
}

export enum SourceType {
    NFC = "nfc",
    STORAGE = "storage",
    CAMERA = "camera",
    MQTT = "mqtt",
    SERIAL = "serial",
    FILE_WATCH = "file_watch",
}

export enum SourceEventKind {
    CONNECTED = "connected",
    DISCONNECTED = "disconnected",
}

export enum MediaEventKind {
    LOAD = "load",
    UNLOAD = "unload",
}

// ─────────────────────────────────────────────────────────────────────────────
// Shared state & helpers
// These were originally in index.tsx; moving them here avoids circular
// dependencies when other components (like the game-page pairer) need to
// import them.
// ─────────────────────────────────────────────────────────────────────────────

/** A game the user is currently *looking at* (its detail page), which may or
 * may not be running. Published by the library route patch so the Quick Access
 * panel can offer to pair it without a game having to be launched first. */
export interface ViewedApp {
  appId: string;
  /** Fully resolved launch URI, e.g. steam://run/… or steam://rungameid/… */
  launchTarget: string;
  name?: string;
}

export interface SharedState {
  settings: Settings | null;
  readerStatus: ReaderStatus;
  /** Every medium presented anywhere, keyed by source_id.
   *
   *  The single source of truth for what is present. There used to be a
   *  parallel `tagUid`/`tagUri`/`tagSourceType`/`mediaProblem` slot holding
   *  whichever medium was seen last on any source, which no component ever
   *  read — every one of them already derives from this map, because one
   *  global slot cannot express "a tag AND a disk are both here", which is
   *  exactly what the Triggers list shows. */
  activeMedia: Record<string, ActiveMedium>;
  activeAppId: string | null;
  /** A theme is painting over Steam's interface right now.
   *
   *  Read by anything of ours that decorates a Steam page: while a theme is
   *  up, that page is not on screen, so an icon floating over it is an offer
   *  to interact with something invisible. z-index cannot solve this — the
   *  two live in different stacking contexts, which is why lowering the icon
   *  below the layer's z-index changed nothing. */
  visualsPainting: boolean;
  /** One of Steam's side menus is open — Quick Access or the main menu.
   *
   *  Polled from Steam's own store rather than Decky's useQuickAccessVisible,
   *  which reports nothing to a global component: the layer stayed up over an
   *  open menu, and the theme picker's dropdown opened *behind* it. */
  menuOpen: boolean;
  /** Game detail page currently open, or null when not on one. */
  viewedApp: ViewedApp | null;
  pairing: boolean;
  /** The user has asked to register a key and is choosing which trigger to
   *  write it to. Local to the panel: nothing is armed on the backend until a
   *  trigger is picked, which is why cancelling it needs no RPC. */
  registeringKey: boolean;
  sourceStatuses: SourceStatus[];
  /** Restricted mode. Null until the first fetch completes, so the panel can avoid
   *  flashing the unlocked view at someone who locked the device. */
  restricted: RestrictedState | null;
}

export type SettingKey =
  | "auto_launch"
  | "custom_visuals"
  | "theme"
  | "auto_close"
  | "device_path"
  | "baudrate"
  | "polling_interval"
  | "reader_type";

export const sharedState: SharedState = {
  settings: null,
  readerStatus: { connected: false, path: "", source_type: SourceType.NFC },
  activeMedia: {},
  activeAppId: null,
  visualsPainting: false,
  menuOpen: false,
  viewedApp: null,
  pairing: false,
  registeringKey: false,
  sourceStatuses: [],
  restricted: null,
};

/** Read by asynchronous callbacks that must not close over a stale lock. */
export const restrictedRef = { current: null as RestrictedState | null };

// These refs are updated from BackgroundManager and read by various
// asynchronous callbacks. They live outside of React so that closures keep a
// stable handle to the current value.
export const activeAppIdRef = { current: null as string | null };
export const settingsRef = { current: null as any };
export const viewedAppRef = { current: null as ViewedApp | null };

/** Publish (or clear) the game detail page currently on screen.
 *
 * Called by the library route patch. Skips the notify when nothing actually
 * changed, so navigating within the same app page doesn't churn the panel. */
export function setViewedApp(app: ViewedApp | null) {
  const prev = sharedState.viewedApp;
  if (prev?.appId === app?.appId && prev?.launchTarget === app?.launchTarget) {
    return;
  }
  sharedState.viewedApp = app;
  viewedAppRef.current = app;
  notifySubscribers();
}

// Subscription system used by the old useSharedState hook.
type Listener = () => void;
const subscribers = new Set<Listener>();
export function notifySubscribers() {
  subscribers.forEach(fn => fn());
}

/** Subscribe outside React.
 *
 * The splash layer is a global component that lives longer than any panel and
 * needs the settings the panel writes — chiefly whether it is switched on at
 * all, since the switch is behind the layer it switches off. */
export function subscribeToState(listener: Listener): () => void {
  subscribers.add(listener);
  return () => { subscribers.delete(listener); };
}

export function useSharedState(): SharedState {
  const [, rerender] = useState(0);
  useEffect(() => {
    const fn = () => rerender(n => n + 1);
    subscribers.add(fn);
    return () => {
      subscribers.delete(fn);
    };
  }, []);
  return sharedState;
}

// ─────────────────────────────────────────────────────────────────────────────
// Backend calls
// ─────────────────────────────────────────────────────────────────────────────

export const getSettings = callable<[], Settings>("get_settings");
export const setSetting = callable<[key: SettingKey, value: any], boolean>("set_setting");
// source_id targets one trigger; omitted, any writable trigger may claim it.
// `title` is the game's name. It is written onto media whose format has room
// for it, so a disk says what it is without resolving an app id against Steam.
export const startPairing =
  callable<[uri: string, source_id?: string, title?: string], boolean>("start_pairing");
export const getActiveMedia = callable<[], ActiveMedium[]>("get_active_media");

/** Write a fresh FAT filesystem to a disk. Destroys its contents.
 *
 * Only ever called for media the backend flagged `formattable` — no filesystem
 * found, so nothing to lose. The backend re-checks every guard regardless. */
export const formatMedia =
  callable<[media_id: string], { success: boolean; error: string | null }>("format_media");
export const cancelPairing = callable<[], boolean>("cancel_pairing");
export const getReaderStatus = callable<[], ReaderStatus>("get_reader_status");
export const setRunningGame = callable<[appid: number | null], void>("set_running_game");
export const setTagKey = callable<[uid: string, key_a: string, key_b: string], boolean>("set_tag_key");
export const getTagKey = callable<[uid: string], { key_a?: string; key_b?: string }>("get_tag_key");
export const listTagKeys = callable<[], string[]>("list_tag_keys");
export const getSectorInfo = callable<[uid?: string], SectorInfo[]>("get_sector_info");
export const lockSector = callable<[uid: string, sector: number, key_a: string, key_b: string], boolean>("lock_sector");
export const getSourceStatuses = callable<[], SourceStatus[]>("get_source_statuses");

/** A QR code for a launch URI, as a PNG data URI. Generation, not pairing:
 *  nothing is written to anything, so this works whether or not the camera
 *  trigger is switched on. */
export const getQrPreview = callable<
  [uri: string, module_px?: number],
  { ok: boolean; data_uri?: string; size?: number; error?: string }
>("get_qr_preview");

/** Write a two-sided printable card to the user's Documents folder. */
export const saveGameCard = callable<
  [uri: string, title?: string, appid?: string],
  { ok: boolean; dir?: string; paths?: Record<string, string>; error?: string }
>("save_game_card");
export const setSourceSetting = callable<[source_type: string, key: string, value: any], boolean>("set_source_setting");

// ── Restricted mode ────────────────────────────────────────────────────────────────
//
// The backend refuses each of these while locked, so hiding the controls that
// call them is presentation, not enforcement.

export const getKioskState = callable<[], RestrictedState>("get_restricted_state");
/** Arm pairing to write a fresh key token onto the next medium presented.
 *  With a source_id, only that trigger may claim it. */
export const registerKey =
    callable<[source_id?: string], boolean>("register_key");
/** Switch restricted mode off and wipe the key from its medium. Needs the key
 *  present, which being unlocked already guarantees. */
export const disableKey = callable<[], boolean>("disable_key");

// Pairing listener may want to suppress the toast when our custom modal is
// showing the result itself.
let pairingToastSuppressed = false;
export function setPairingToastSuppressed(s: boolean) {
  pairingToastSuppressed = s;
}
export function pairingToastsSuppressed(): boolean {
  return pairingToastSuppressed;
}

// re-export some utilities from @decky/api that other modules use
export { addEventListener, removeEventListener, toaster };
