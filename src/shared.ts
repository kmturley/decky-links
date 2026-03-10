import { addEventListener, removeEventListener, callable, toaster } from "@decky/api";
import { useState, useEffect } from "react";

// ─────────────────────────────────────────────────────────────────────────────
// Shared state & helpers
// These were originally in index.tsx; moving them here avoids circular
// dependencies when other components (like the game-page pairer) need to
// import them.
// ─────────────────────────────────────────────────────────────────────────────

export interface SharedState {
  settings: any;
  readerStatus: { connected: boolean; path: string };
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

export const getSettings = callable<[], any>("get_settings");
export const setSetting = callable<[key: string, value: any], boolean>("set_setting");
export const startPairing = callable<[uri: string], boolean>("start_pairing");
export const cancelPairing = callable<[], boolean>("cancel_pairing");
export const getReaderStatus = callable<[], { connected: boolean; path: string }>("get_reader_status");
export const getTagStatus = callable<[], { uid: string | null; uri: string | null }>("get_tag_status");
export const setRunningGame = callable<[appid: number | null], void>("set_running_game");
export const setTagKey = callable<[uid: string, key_a: string, key_b: string], boolean>("set_tag_key");
export const getTagKey = callable<[uid: string], { key_a?: string; key_b?: string }>("get_tag_key");
export const listTagKeys = callable<[], string[]>("list_tag_keys");

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
