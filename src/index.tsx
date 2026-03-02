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
import { useState, useEffect, FC, useRef } from "react";
import { FaLink, FaCircle, FaGamepad, FaMicrochip, FaHashtag } from "react-icons/fa";

// Backend calls
const getSettings = callable<[], any>("get_settings");
const setSetting = callable<[key: string, value: any], boolean>("set_setting");
const startPairing = callable<[uri: string], boolean>("start_pairing");
const cancelPairing = callable<[], boolean>("cancel_pairing");
const getReaderStatus = callable<[], { connected: boolean, path: string }>("get_reader_status");
const getTagStatus = callable<[], { uid: string | null, uri: string | null }>("get_tag_status");
const setRunningGame = callable<[appid: number | null], void>("set_running_game");

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
  const [settings, setSettings] = useState<any>(null);
  const [status, setStatus] = useState({ connected: false, path: "" });
  const [pairing, setPairing] = useState(false);
  const [tagUid, setTagUid] = useState<string | null>(null);
  const [tagUri, setTagUri] = useState<string | null>(null);
  const [activeAppId, setActiveAppId] = useState<string | null>(null);

  // REFS for stable access in async callbacks/loops
  const activeAppIdRef = useRef<string | null>(null);
  const tagUidRef = useRef<string | null>(null);
  const settingsRef = useRef<any>(null);

  useEffect(() => {
    let active = true;

    const init = async () => {
      const s = await getSettings();
      if (active) {
        setSettings(s);
        settingsRef.current = s;
      }
      // Initial poll for immediate UI
      const stat = await getReaderStatus();
      if (active) setStatus(stat);

      const tag = await getTagStatus();
      if (active && tag.uid) {
        setTagUid(tag.uid);
        setTagUri(tag.uri);
        tagUidRef.current = tag.uid;
      }
    };
    init();

    // Listen for events
    const tagListener = addEventListener<[data: { uid: string }]>("tag_detected", (data) => {
      setTagUid(data.uid);
      tagUidRef.current = data.uid;
      setTagUri(null); // Reset URL on new scan
    });

    const removeListener = addEventListener("tag_removed", () => {
      setTagUid(null);
      tagUidRef.current = null;
      setTagUri(null);
    });

    const statusListener = addEventListener<[data: { connected: boolean, path: string }]>("reader_status", (data) => {
      setStatus(data);
    });

    const uriListener = addEventListener<[data: { uri: string | null, uid: string }]>("uri_detected", (data) => {
      // null means no valid URI was found on the tag (Fix #6: replaced "None" sentinel)
      setTagUri(data.uri);
      setTagUid(data.uid.toUpperCase());
      tagUidRef.current = data.uid.toUpperCase();

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
                // Fix #2: immediately notify backend so it can enforce no-stacking
                // without waiting for the 500ms polling loop to detect the game.
                const numId = parseInt(appId!);
                activeAppIdRef.current = String(appId);
                setActiveAppId(String(appId));
                setRunningGame(numId).catch(console.error);
                return;
              }
            } catch (e) {
              console.error("RunGame failed, falling back:", e);
            }
          }

          // Non-Steam URI fallback (heroic://, https://, etc.) — backend handles launch
          // via xdg-open; frontend just navigates to trigger the handler if needed.
          console.info(`[ Decky Links ] Navigation fallback: ${data.uri}`);
          Navigation.Navigate(data.uri);
        }
      }
    });

    const pairingListener = addEventListener<[data: { success: boolean, uid: string, error?: string }]>("pairing_result", (data) => {
      setPairing(false);
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
      // The window.location.pathname check was removed because SteamOS always shows a
      // system path (e.g. /library) even while the user is actively playing a game.
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

    // Unified Polling Loop
    const pollLoop = async () => {
      while (active) {
        try {
          // 1. Poll Game Status (Support both function and property signatures)
          const appRaw = Router.MainRunningApp;
          const app = typeof appRaw === 'function' ? (appRaw as any)() : appRaw;
          const currentId = (app && app.appid !== "0") ? String(app.appid) : null;

          if (currentId !== activeAppIdRef.current) {
            console.info(`[ Decky Links ] Game change: ${activeAppIdRef.current} -> ${currentId}`);
            activeAppIdRef.current = currentId;
            setActiveAppId(currentId);
            // Sync with backend
            await setRunningGame(currentId ? parseInt(currentId) : null);
          }

          // 2. Poll Tag Status (if missing)
          if (!tagUidRef.current) {
            const t = await getTagStatus();
            if (active && t.uid) {
              setTagUid(t.uid);
              tagUidRef.current = t.uid;
              setTagUri(t.uri);
            }
          }

          // 3. Poll Reader Status (to fix "Not Found" on slow start)
          const reader = await getReaderStatus();
          if (active) setStatus(reader);

        } catch (e) {
          console.error("[ Decky Links ] Polling loop error:", e);
        }

        // Fix #2: 500ms polling gives faster game-state detection (was 1500ms)
        await sleep(500);
      }
    };
    pollLoop();

    return () => {
      active = false;
      removeEventListener("tag_detected", tagListener);
      removeEventListener("tag_removed", removeListener);
      removeEventListener("reader_status", statusListener);
      removeEventListener("uri_detected", uriListener);
      removeEventListener("pairing_result", pairingListener);
      removeEventListener("card_removed_during_game", gameRemovalListener);
    };
  }, []); // Run ONCE on mount

  const updateSetting = async (key: string, value: any) => {
    await setSetting(key, value);
    const newSettings = { ...settings, [key]: value };
    setSettings(newSettings);
    settingsRef.current = newSettings;
  };

  const handlePairing = async () => {
    if (pairing) {
      await cancelPairing();
      setPairing(false);
      toaster.toast({ title: "Pairing Cancelled", body: "Mode exited." });
    } else {
      const app = Router.MainRunningApp;
      if (app && app.appid && app.appid !== "0") {
        const uri = `steam://rungameid/${app.appid}`;
        console.info(`[ Decky Links ] Starting pairing for: ${uri}`);
        await startPairing(uri);
        setPairing(true);
        toaster.toast({
          title: "Pairing Mode",
          body: `Tap a tag to pair with Game ID: ${app.appid}`,
          duration: 5000
        });
      } else {
        toaster.toast({ title: "Pairing Error", body: "No active game detected to pair.", critical: true });
      }
    }
  };

  if (!settings) return null;

  return (
    <PanelSection title="Decky Links">
      <PanelSection title="Status">
        <StatusRow
          icon={<FaMicrochip />}
          label="Reader"
          value={status.connected ? status.path.split('/').pop() || status.path : "Not Found"}
          active={status.connected}
        />
        <StatusRow
          icon={<FaHashtag />}
          label="Tag"
          value={tagUid ? tagUid : "Not Connected"}
          active={!!tagUid}
        />
        <StatusRow
          icon={<FaLink />}
          label="Url"
          value={tagUri ?? "Empty"}
          active={!!tagUri}
        />
        <StatusRow
          icon={<FaGamepad />}
          label="Game"
          value={activeAppId ? `Playing ${activeAppId}` : "Not Playing"}
          active={!!activeAppId}
        />
      </PanelSection>

      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={handlePairing}
          disabled={!status.connected}
        >
          {pairing ? "Cancel Pairing" : "Pair Current Game"}
        </ButtonItem>
      </PanelSectionRow>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <TextField
            label="Device Path"
            value={settings.device_path}
            onChange={(e) => updateSetting("device_path", e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Auto-Launch"
            description="Launch games automatically on tap"
            checked={settings.auto_launch}
            onChange={(v: boolean) => updateSetting("auto_launch", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Auto-Close"
            description="Exit game automatically on removal"
            checked={settings.auto_close}
            onChange={(v: boolean) => updateSetting("auto_close", v)}
          />
        </PanelSectionRow>
      </PanelSection>
    </PanelSection>
  );
};

export default definePlugin(() => {
  return {
    name: "Decky Links",
    titleView: <div className={staticClasses.Title}>Decky Links</div>,
    content: <Content />,
    icon: <FaLink />,
  };
});
