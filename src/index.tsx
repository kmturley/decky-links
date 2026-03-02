import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  Navigation,
  staticClasses,
  TextField,
  ToggleField,
  SideMenu,
  Router,
  sleep
} from "@decky/ui";
import {
  addEventListener,
  removeEventListener,
  callable,
  definePlugin,
  toaster,
} from "@decky/api"
import { useState, useEffect, FC } from "react";
import { FaLink, FaCircle, FaGamepad, FaMicrochip, FaHashtag } from "react-icons/fa";

// ─────────────────────────────────────────────────────────────────────────────
// Backend calls
// ─────────────────────────────────────────────────────────────────────────────

const getSettings = callable<[], any>("get_settings");
const setSetting = callable<[key: string, value: any], boolean>("set_setting");
const startPairing = callable<[uri: string], boolean>("start_pairing");
const cancelPairing = callable<[], boolean>("cancel_pairing");
const getReaderStatus = callable<[], { connected: boolean, path: string }>("get_reader_status");
const getTagStatus = callable<[], { uid: string | null, uri: string | null }>("get_tag_status");
const setRunningGame = callable<[appid: number | null], void>("set_running_game");

// ─────────────────────────────────────────────────────────────────────────────
// Module-level shared state
// Lives for the entire plugin lifetime — survives QA panel open/close cycles.
// BackgroundManager writes here; Content subscribes for re-renders.
// ─────────────────────────────────────────────────────────────────────────────

interface SharedState {
  settings: any;
  readerStatus: { connected: boolean; path: string };
  tagUid: string | null;
  tagUri: string | null;
  activeAppId: string | null;
  pairing: boolean;
}

const sharedState: SharedState = {
  settings: null,
  readerStatus: { connected: false, path: "" },
  tagUid: null,
  tagUri: null,
  activeAppId: null,
  pairing: false,
};

// Stable refs used by BackgroundManager's async closures
const activeAppIdRef = { current: null as string | null };
const tagUidRef = { current: null as string | null };
const settingsRef = { current: null as any };

// Subscriber list — Content registers here so it re-renders on state changes
type Listener = () => void;
const subscribers = new Set<Listener>();

function notifySubscribers() {
  subscribers.forEach(fn => fn());
}

// Hook for Content to subscribe to shared state updates
function useSharedState(): SharedState {
  const [, rerender] = useState(0);
  useEffect(() => {
    const fn = () => rerender(n => n + 1);
    subscribers.add(fn);
    return () => { subscribers.delete(fn); };
  }, []);
  return sharedState;
}

// ─────────────────────────────────────────────────────────────────────────────
// BackgroundManager — always mounted via alwaysRender
// Owns all event listeners and the polling loop.
// Renders nothing visible.
// ─────────────────────────────────────────────────────────────────────────────

const BackgroundManager: FC = () => {
  useEffect(() => {
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
      }

      notifySubscribers();
    };
    init();

    // ── Event Listeners ────────────────────────────────────────────────────

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

          const uriAppId = data.uri.includes("steam://rungameid/")
            ? data.uri.replace("steam://rungameid/", "").split("/")[0]
            : null;

          // Spec §8: Do not launch if this exact game is already running
          if (currentAppId && uriAppId && String(currentAppId) === String(uriAppId)) {
            console.info(`[ Decky Links ] Game ${currentAppId} is already running. Skipping redundant launch.`);
            return;
          }

          if (data.uri.startsWith("steam://rungameid/")) {
            const appId = uriAppId;
            console.info(`[ Decky Links ] Initiating Steam Launch: ${appId}`);
            try {
              // @ts-ignore
              if (window.SteamClient?.Apps?.RunGame) {
                // @ts-ignore
                window.SteamClient.Apps.RunGame(String(appId), "", -1, 100);
                const numId = parseInt(appId!);
                activeAppIdRef.current = String(appId);
                sharedState.activeAppId = String(appId);
                notifySubscribers();
                setRunningGame(numId).catch(console.error);
                return;
              }
            } catch (e) {
              console.error("RunGame failed, falling back:", e);
            }
          }

          // Non-Steam URI fallback — backend handles via xdg-open
          console.info(`[ Decky Links ] Navigation fallback: ${data.uri}`);
          Navigation.Navigate(data.uri);
        }
      }
    });

    const pairingListener = addEventListener<[data: { success: boolean, uid: string, error?: string }]>("pairing_result", (data) => {
      sharedState.pairing = false;
      notifySubscribers();
      toaster.toast({
        title: data.success ? "Pairing Success" : "Pairing Failed",
        body: data.success ? `Game paired to tag ${data.uid}` : (data.error || "Write failed."),
        critical: !data.success,
        duration: 3000
      });
    });

    const gameRemovalListener = addEventListener<[data: { appid: number, uid: string, uri: string }]>("card_removed_during_game", (data) => {
      const currentAppId = activeAppIdRef.current;
      const currentSettings = settingsRef.current;

      const uriAppId = data.uri && data.uri.includes("steam://rungameid/")
        ? data.uri.replace("steam://rungameid/", "").split("/")[0]
        : null;

      // Only trigger if the removed tag's game matches the currently running game.
      // activeAppIdRef is kept in sync by the polling loop — if it's set, a game is running.
      if (currentAppId && uriAppId && String(currentAppId) === String(uriAppId)) {
        if (currentSettings?.auto_close) {
          console.info(`[ Decky Links ] Paired tag removed. Auto-closing game: ${currentAppId}`);
          // @ts-ignore
          if (window.SteamClient?.Apps?.TerminateApp) {
            // TerminateApp requires 2 args: (appId: string, bForce: boolean)
            // @ts-ignore
            window.SteamClient.Apps.TerminateApp(String(currentAppId), true);
          }
        } else {
          console.info(`[ Decky Links ] Paired tag removed. Pausing game: ${currentAppId}`);
          Navigation.CloseSideMenus();
          Navigation.OpenSideMenu(SideMenu.Main);
        }
      } else {
        console.info(`[ Decky Links ] Tag removed but game not running (currentAppId=${currentAppId}, uriAppId=${uriAppId}). Ignoring.`);
      }
    });

    // ── Polling Loop ───────────────────────────────────────────────────────

    const pollLoop = async () => {
      while (active) {
        try {
          // 1. Poll Game Status
          const appRaw = Router.MainRunningApp;
          const app = typeof appRaw === 'function' ? (appRaw as any)() : appRaw;
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
              sharedState.tagUid = t.uid;
              sharedState.tagUri = t.uri;
              tagUidRef.current = t.uid;
              notifySubscribers();
            }
          }

          // 3. Poll Reader Status
          const reader = await getReaderStatus();
          if (active) {
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

    // ── Cleanup ────────────────────────────────────────────────────────────

    return () => {
      active = false;
      removeEventListener("tag_detected", tagListener);
      removeEventListener("tag_removed", removeListener);
      removeEventListener("reader_status", statusListener);
      removeEventListener("uri_detected", uriListener);
      removeEventListener("pairing_result", pairingListener);
      removeEventListener("card_removed_during_game", gameRemovalListener);
    };
  }, []); // Run ONCE on plugin mount — BackgroundManager is never remounted

  return null;
};

// ─────────────────────────────────────────────────────────────────────────────
// Shared pairing action — called from Content, safe to use from anywhere
// ─────────────────────────────────────────────────────────────────────────────

async function triggerPairing() {
  if (sharedState.pairing) {
    await cancelPairing();
    sharedState.pairing = false;
    notifySubscribers();
    toaster.toast({ title: "Pairing Cancelled", body: "Mode exited." });
  } else {
    const app = Router.MainRunningApp;
    if (app && app.appid && app.appid !== "0") {
      const uri = `steam://rungameid/${app.appid}`;
      console.info(`[ Decky Links ] Starting pairing for: ${uri}`);
      await startPairing(uri);
      sharedState.pairing = true;
      notifySubscribers();
      toaster.toast({
        title: "Pairing Mode",
        body: `Tap a tag to pair with Game ID: ${app.appid}`,
        duration: 5000
      });
    } else {
      toaster.toast({ title: "Pairing Error", body: "No active game detected to pair.", critical: true });
    }
  }
}

async function triggerUpdateSetting(key: string, value: any) {
  await setSetting(key, value);
  sharedState.settings = { ...sharedState.settings, [key]: value };
  settingsRef.current = sharedState.settings;
  notifySubscribers();
}

// ─────────────────────────────────────────────────────────────────────────────
// Pure display component
// ─────────────────────────────────────────────────────────────────────────────

const StatusRow: FC<{ icon: any, label: string, value: string, active: boolean }> = ({ icon, label, value, active }) => (
  <div style={{
    display: "flex",
    alignItems: "center",
    gap: "12px",
    padding: "4px 8px",
    fontSize: "0.9em"
  }}>
    <div style={{ color: active ? "#4CAF50" : "#757575", display: "flex", alignItems: "center" }}>
      {icon}
    </div>
    <div style={{ flex: 1, opacity: active ? 1 : 0.6 }}>
      <span style={{ fontWeight: "bold" }}>{label}: </span>
      <span style={{ fontFamily: "monospace" }}>{value}</span>
    </div>
    <FaCircle size={8} color={active ? "#4CAF50" : "#333"} />
  </div>
);

const Content: FC = () => {
  // Subscribe to sharedState — re-renders automatically when BackgroundManager
  // calls notifySubscribers(), even while QA panel was closed in between.
  const state = useSharedState();

  if (!state.settings) return null;

  return (
    <PanelSection title="Decky Links">
      <PanelSection title="Status">
        <StatusRow
          icon={<FaMicrochip />}
          label="Reader"
          value={state.readerStatus.connected ? state.readerStatus.path.split('/').pop() || state.readerStatus.path : "Not Found"}
          active={state.readerStatus.connected}
        />
        <StatusRow
          icon={<FaHashtag />}
          label="Tag"
          value={state.tagUid ? state.tagUid : "Not Connected"}
          active={!!state.tagUid}
        />
        <StatusRow
          icon={<FaLink />}
          label="Url"
          value={state.tagUri ?? "Empty"}
          active={!!state.tagUri}
        />
        <StatusRow
          icon={<FaGamepad />}
          label="Game"
          value={state.activeAppId ? `Playing ${state.activeAppId}` : "Not Playing"}
          active={!!state.activeAppId}
        />
      </PanelSection>

      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={triggerPairing}
          disabled={!state.readerStatus.connected}
        >
          {state.pairing ? "Cancel Pairing" : "Pair Current Game"}
        </ButtonItem>
      </PanelSectionRow>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <TextField
            label="Device Path"
            value={state.settings.device_path}
            onChange={(e) => triggerUpdateSetting("device_path", e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Auto-Launch"
            description="Launch games automatically on tap"
            checked={state.settings.auto_launch}
            onChange={(v: boolean) => triggerUpdateSetting("auto_launch", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Auto-Close"
            description="Exit game automatically on removal"
            checked={state.settings.auto_close}
            onChange={(v: boolean) => triggerUpdateSetting("auto_close", v)}
          />
        </PanelSectionRow>
      </PanelSection>
    </PanelSection>
  );
};

// ─────────────────────────────────────────────────────────────────────────────
// Plugin entry point
// ─────────────────────────────────────────────────────────────────────────────

export default definePlugin(() => {
  // `alwaysRender` now expects a boolean; previous versions allowed a
  // React element which we relied on to mount the background manager
  // even when the panel was closed.  To keep the manager alive we set
  // `alwaysRender: true` and include the component inside the
  // content itself.  With `alwaysRender` enabled the content stays
  // mounted in the background, so the manager will still run.
  return {
    name: "Decky Links",
    titleView: <div className={staticClasses.Title}>Decky Links</div>,
    alwaysRender: true,
    content: (
      <>
        <BackgroundManager />
        <Content />
      </>
    ),
    icon: <FaLink />,
  };
});
