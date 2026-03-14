import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  Router,
  staticClasses,
  TextField,
  ToggleField,
} from "@decky/ui";
import { definePlugin, routerHook } from "@decky/api";
import { FC, ReactNode } from "react";
import { FaLink, FaCircle, FaGamepad, FaMicrochip, FaHashtag } from "react-icons/fa";

// shared utilities extracted to avoid circular imports
import {
  useSharedState,
  toaster,
  setSetting,
  sharedState,
  cancelPairing,
  startPairing,
  notifySubscribers,
  settingsRef,
  type Settings,
} from "./shared";

// ─────────────────────────────────────────────────────────────────────────────
// (the rest of the file remains unchanged)


import { KeyManagementPanel } from "./KeyManagementPanel";
import { SectorManagementPanel } from "./SectorManagementPanel";
import patchLibraryApp from "./lib/patchLibraryApp";
import { startBackgroundManager } from "./BackgroundManager";
import { resolveRungameidTarget } from "./lib/steamIds";

function getMainRunningApp() {
  const appRaw = Router.MainRunningApp;
  return typeof appRaw === "function" ? (appRaw as any)() : appRaw;
}

async function triggerPairing() {
  if (sharedState.pairing) {
    await cancelPairing();
    sharedState.pairing = false;
    notifySubscribers();
    return;
  }

  const app = getMainRunningApp();
  if (!app || !app.appid || app.appid === "0") {
    toaster.toast({ title: "Pairing Error", body: "No active game detected to pair.", critical: true });
    return;
  }

  const uri = resolveRungameidTarget(String(app.appid));
  if (!uri) {
    toaster.toast({ title: "Pairing Error", body: "Invalid app ID.", critical: true });
    return;
  }
  console.info(`[ Decky Links ] Starting pairing for: ${uri}`);
  const ok = await startPairing(uri);
  if (!ok) {
    toaster.toast({ title: "Pairing Error", body: "Failed to start pairing mode.", critical: true });
    return;
  }

  sharedState.pairing = true;
  notifySubscribers();
}

async function triggerUpdateSetting(key: keyof Settings, value: any) {
  const ok = await setSetting(key, value);
  if (!ok) {
    toaster.toast({ title: "Settings Error", body: `Invalid value for ${key}.`, critical: true });
    return;
  }
  if (sharedState.settings) {
    sharedState.settings = { ...sharedState.settings, [key]: value };
  }
  settingsRef.current = sharedState.settings;
  notifySubscribers();
}

const StatusRow: FC<{ icon: ReactNode; label: string; value: string; active: boolean }> = ({ icon, label, value, active }) => (
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
    <PanelSection>
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
        <ButtonItem
          layout="below"
          onClick={triggerPairing}
          disabled={!state.readerStatus.connected}
        >
          {state.pairing ? "Cancel Pairing" : "Pair Current Game"}
        </ButtonItem>
      </PanelSection>

      <PanelSection title="Settings">
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
        <PanelSectionRow>
          <TextField
            label="Device Path"
            value={state.settings.device_path}
            onChange={(e) => triggerUpdateSetting("device_path", e.target.value)}
          />
        </PanelSectionRow>
      </PanelSection>

      <KeyManagementPanel />

      <SectorManagementPanel tagUid={state.tagUid || undefined} />
    </PanelSection>
  );
};

export default definePlugin(() => {
  const embeddedPatch = patchLibraryApp();
  const stopBackground = startBackgroundManager();

  return {
    name: "Decky Links",
    titleView: <div className={staticClasses.Title}>Decky Links</div>,
    alwaysRender: true,
    content: <Content />,
    icon: <FaLink />,
    onDismount() {
      stopBackground();
      routerHook.removePatch('/library/app/:appid', embeddedPatch);
    },
  };
});
