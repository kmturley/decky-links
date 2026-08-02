import { addEventListener, removeEventListener, callable, toaster } from "@decky/api";
import { useState, useEffect } from "react";

// ─────────────────────────────────────────────────────────────────────────────
// Type definitions
// ─────────────────────────────────────────────────────────────────────────────

export interface Settings {
    auto_launch: boolean;
    auto_close: boolean;
    sources: {
        nfc: {
            device_path: string;
            baudrate: number;
            polling_interval: number;
            reader_type: "pn532_uart" | "acr122u" | "proxmark" | "nfcpy";
        };
        storage?: { enabled: boolean; drive_kinds?: Record<string, boolean> };
        camera?: { enabled: boolean; device: string; poll_interval: number };
        mqtt?: { enabled: boolean; broker_host: string; broker_port: number; topic: string; secret: string };
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

export interface TagStatus {
    uid: string | null;
    uri: string | null;
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
  tagUid: string | null;
  tagUri: string | null;
  /** Which source is presenting the current medium — an NFC tag and a floppy
   *  both land in tagUid, but only one of them has sectors to manage. */
  tagSourceType: SourceType | null;
  /** Why the current medium carries no URI. "blank" is ready to pair;
   *  "unreadable" is the user's problem to fix and must say so. */
  mediaProblem: { kind: "blank" | "unreadable" | "blocked" | "loading"; error?: string } | null;
  /** Every medium presented anywhere, keyed by source_id. The single tagUid
   *  slot above cannot express "a tag AND a disk are both here", which is
   *  exactly what the Triggers list has to show. */
  activeMedia: Record<string, ActiveMedium>;
  activeAppId: string | null;
  /** Game detail page currently open, or null when not on one. */
  viewedApp: ViewedApp | null;
  pairing: boolean;
  sourceStatuses: SourceStatus[];
}

export type SettingKey =
  | "auto_launch"
  | "auto_close"
  | "device_path"
  | "baudrate"
  | "polling_interval"
  | "reader_type";

export const sharedState: SharedState = {
  settings: null,
  readerStatus: { connected: false, path: "", source_type: SourceType.NFC },
  tagUid: null,
  tagUri: null,
  tagSourceType: null,
  mediaProblem: null,
  activeMedia: {},
  activeAppId: null,
  viewedApp: null,
  pairing: false,
  sourceStatuses: [],
};

// These refs are updated from BackgroundManager and read by various
// asynchronous callbacks. They live outside of React so that closures keep a
// stable handle to the current value.
export const activeAppIdRef = { current: null as string | null };
export const tagUidRef = { current: null as string | null };
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
export const startPairing = callable<[uri: string, source_id?: string], boolean>("start_pairing");
export const getActiveMedia = callable<[], ActiveMedium[]>("get_active_media");
export const cancelPairing = callable<[], boolean>("cancel_pairing");
export const getReaderStatus = callable<[], ReaderStatus>("get_reader_status");
export const getTagStatus = callable<[], TagStatus>("get_tag_status");
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
