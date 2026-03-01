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

// Backend calls
const getSettings = callable<[], any>("get_settings");
const setSetting = callable<[key: string, value: any], boolean>("set_setting");
const startPairing = callable<[uri: string], boolean>("start_pairing");
const cancelPairing = callable<[], boolean>("cancel_pairing");
const getReaderStatus = callable<[], { connected: boolean, path: string }>("get_reader_status");
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

  useEffect(() => {
    let active = true;

    const init = async () => {
      const s = await getSettings();
      if (active) setSettings(s);
      const stat = await getReaderStatus();
      if (active) setStatus(stat);
    };
    init();

    // Listen for events
    const tagListener = addEventListener<[data: { uid: string }]>("tag_detected", (data) => {
      setTagUid(data.uid);
      setTagUri(null); // Reset URL on new scan
    });

    const removeListener = addEventListener("tag_removed", () => {
      setTagUid(null);
      setTagUri(null);
    });

    const statusListener = addEventListener<[data: { connected: boolean, path: string }]>("reader_status", (data) => {
      setStatus(data);
    });

    const uriListener = addEventListener<[data: { uri: string, uid: string }]>("uri_detected", (data) => {
      setTagUri(data.uri);

      toaster.toast({
        title: data.uri === "None" ? "NFC Tag Detected" : `Tag: ${data.uid}`,
        body: `Url: ${data.uri}`
      });

      if (data.uri !== "None") {
        if (settings?.auto_launch) {
          // If it's a Steam game, use the recommended RunGame API
          if (data.uri.startsWith("steam://rungameid/")) {
            const appId = data.uri.replace("steam://rungameid/", "").split("/")[0];
            console.log(`Frontend initiating launch for Steam AppID: ${appId}`);
            try {
              // @ts-ignore
              if (window.SteamClient?.Apps?.RunGame) {
                // @ts-ignore
                window.SteamClient.Apps.RunGame(String(appId), "", -1, 100);
                return;
              }
            } catch (e) {
              console.error("Failed to launch via SteamClient, falling back:", e);
              // If it fails with an exception, we let it fall through to Navigation.Navigate
            }
          }

          // Fallback for non-steam or if SteamClient is missing
          console.log(`Frontend using navigation fallback: ${data.uri}`);
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

    const gameRemovalListener = addEventListener<[data: { appid: number }]>("card_removed_during_game", () => {
      // Logic for pausing/menu: only if we aren't busy pairing
      Navigation.CloseSideMenus();
      Navigation.OpenSideMenu(SideMenu.QuickAccess);
    });

    // idiomatic Decky polling pattern
    const pollGame = async () => {
      while (active) {
        const app = Router.MainRunningApp;
        const currentId = (app && app.appid !== "0") ? app.appid : null;

        if (currentId !== activeAppId) {
          setActiveAppId(currentId);
          await setRunningGame(currentId ? parseInt(currentId) : null);
        }
        await sleep(1500);
      }
    };
    pollGame();

    return () => {
      active = false;
      removeEventListener("tag_detected", tagListener);
      removeEventListener("tag_removed", removeListener);
      removeEventListener("reader_status", statusListener);
      removeEventListener("uri_detected", uriListener);
      removeEventListener("pairing_result", pairingListener);
      removeEventListener("card_removed_during_game", gameRemovalListener);
    };
  }, [activeAppId, settings]);

  const updateSetting = async (key: string, value: any) => {
    await setSetting(key, value);
    setSettings({ ...settings, [key]: value });
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
        console.log(`Starting pairing for: ${uri}`);
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
          value={tagUri ? tagUri : "Empty"}
          active={!!tagUri && tagUri !== "None"}
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
