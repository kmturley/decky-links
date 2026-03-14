import { addEventListener, removeEventListener, callable, toaster } from "@decky/api";
import { useState, useEffect } from "react";

// ─────────────────────────────────────────────────────────────────────────────
// Type definitions
// ─────────────────────────────────────────────────────────────────────────────

export interface Settings {
    device_path: string;
    baudrate: number;
    polling_interval: number;
    auto_launch: boolean;
    auto_close: boolean;
    reader_type: "pn532_uart" | "acr122u" | "proxmark" | "nfcpy";
}

export interface ReaderStatus {
    connected: boolean;
    path: string;
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

// ─────────────────────────────────────────────────────────────────────────────
// Shared state & helpers
// These were originally in index.tsx; moving them here avoids circular
// dependencies when other components (like the game-page pairer) need to
// import them.
// ─────────────────────────────────────────────────────────────────────────────

export interface SharedState {
  settings: Settings | null;
  readerStatus: ReaderStatus;
  tagUid: string | null;
  tagUri: string | null;
  activeAppId: string | null;
  pairing: boolean;
}

export const sharedState: SharedState = {
  settings: null,
  readerStatus: { connected: false, path: "" },
  tagUid: null,
  tagUri: null,
  activeAppId: null,
  pairing: false,
};

// These refs are updated from BackgroundManager and read by various
// asynchronous callbacks. They live outside of React so that closures keep a
// stable handle to the current value.
export const activeAppIdRef = { current: null as string | null };
export const tagUidRef = { current: null as string | null };
export const settingsRef = { current: null as any };

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
export const setSetting = callable<[key: keyof Settings, value: any], boolean>("set_setting");
export const startPairing = callable<[uri: string], boolean>("start_pairing");
export const cancelPairing = callable<[], boolean>("cancel_pairing");
export const getReaderStatus = callable<[], ReaderStatus>("get_reader_status");
export const getTagStatus = callable<[], TagStatus>("get_tag_status");
export const setRunningGame = callable<[appid: number | null], void>("set_running_game");
export const setTagKey = callable<[uid: string, key_a: string, key_b: string], boolean>("set_tag_key");
export const getTagKey = callable<[uid: string], { key_a?: string; key_b?: string }>("get_tag_key");
export const listTagKeys = callable<[], string[]>("list_tag_keys");
export const getSectorInfo = callable<[uid?: string], SectorInfo[]>("get_sector_info");
export const lockSector = callable<[uid: string, sector: number, key_a: string, key_b: string], boolean>("lock_sector");

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
