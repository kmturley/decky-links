import {
  PanelSection,
  PanelSectionRow,
  Router,
  staticClasses,
  TextField,
  ToggleField,
} from "@decky/ui";
import { definePlugin, routerHook } from "@decky/api";
import { FC, ReactNode } from "react";
import { FaLink, FaCircle, FaGamepad } from "react-icons/fa";

// shared utilities extracted to avoid circular imports
import {
  useSharedState,
  toaster,
  setSetting,
  sharedState,
  notifySubscribers,
  settingsRef,
  SourceType,
  type SettingKey,
} from "./shared";

// ─────────────────────────────────────────────────────────────────────────────
// (the rest of the file remains unchanged)


import { KeyManagementPanel } from "./KeyManagementPanel";
import { SectorManagementPanel } from "./SectorManagementPanel";
import patchLibraryApp from "./lib/patchLibraryApp";
import { startBackgroundManager } from "./BackgroundManager";
import { resolveRungameidTarget } from "./lib/steamIds";
import { TriggersPanel } from "./TriggersPanel";

function getMainRunningApp() {
  const appRaw = Router.MainRunningApp;
  return typeof appRaw === "function" ? (appRaw as any)() : appRaw;
}

/** The game "Pair Current Game" should target.
 *
 * A running game wins; otherwise fall back to the detail page the user is
 * looking at. Pairing a card for a game you are browsing is the common case —
 * requiring it to be running first made the button useless most of the time.
 * Returns null when neither is available, which also drives the disabled state.
 */
function resolvePairTarget(): { uri: string; label: string } | null {
  const app = getMainRunningApp();
  if (app && app.appid && app.appid !== "0") {
    const uri = resolveRungameidTarget(String(app.appid));
    if (uri) {
      return { uri, label: app.display_name || `App ${app.appid}` };
    }
  }

  const viewed = sharedState.viewedApp;
  if (viewed?.launchTarget) {
    return { uri: viewed.launchTarget, label: viewed.name || `App ${viewed.appId}` };
  }

  return null;
}

async function triggerUpdateSetting(key: SettingKey, value: any) {
  const ok = await setSetting(key, value);
  if (!ok) {
    toaster.toast({ title: "Settings Error", body: `Invalid value for ${key}.`, critical: true });
    return;
  }
  if (sharedState.settings) {
    if (key === "auto_launch" || key === "auto_close") {
      sharedState.settings = { ...sharedState.settings, [key]: value };
    } else {
      sharedState.settings = {
        ...sharedState.settings,
        sources: {
          ...sharedState.settings.sources,
          nfc: {
            ...sharedState.settings.sources.nfc,
            [key]: value,
          },
        },
      };
    }
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

  const nfcSettings = state.settings.sources.nfc;


  // Recomputed each render: sharedState.viewedApp changes trigger a re-render
  // via notifySubscribers(), so this stays in step with what's on screen.
  const pairTarget = resolvePairTarget();

  // Mifare tooling below is NFC-only, and now reads the per-source registry
  // rather than the single global slot — a disk in a drive must not put the
  // sector editor on screen.
  const nfcMedium = Object.values(state.activeMedia).find(
    (m) => m.source_type === SourceType.NFC,
  );

  const gameStatusValue = state.activeAppId
    ? `Playing ${state.activeAppId}`
    : state.viewedApp
      ? `Viewing ${state.viewedApp.name || state.viewedApp.appId}`
      : "Not Playing";


  return (
    <PanelSection>
      <PanelSection title="Status">
        <StatusRow
          icon={<FaGamepad />}
          label="Game"
          value={gameStatusValue}
          active={!!state.activeAppId || !!state.viewedApp}
        />
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
            label="Reader Type"
            value={nfcSettings.reader_type}
            onChange={(e) => triggerUpdateSetting("reader_type", e.target.value)}
          />
        </PanelSectionRow>
      </PanelSection>

      <TriggersPanel
        statuses={state.sourceStatuses}
        media={state.activeMedia}
        target={pairTarget}
        pairing={state.pairing}
      />

      <KeyManagementPanel />

      {/* Keys and sectors are Mifare concepts; a floppy has neither, so this
          follows the NFC medium specifically rather than whatever was last
          presented on any trigger. */}
      {nfcMedium && (
        <SectorManagementPanel tagUid={nfcMedium.media_id} />
      )}
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
