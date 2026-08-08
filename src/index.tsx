import {
  PanelSection,
  PanelSectionRow,
  Router,
  staticClasses,
  ToggleField,
} from "@decky/ui";
import { definePlugin, routerHook } from "@decky/api";
import { FC } from "react";
import { FaLink } from "react-icons/fa";

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

import { RestrictedPanel, LockedPanel } from "./RestrictedPanel";
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

/** Update one of the two top-level behaviour switches.
 *
 * Per-source settings do not come through here — the Triggers panel writes
 * those with setSourceSetting, which the backend validates per source. */
async function triggerUpdateSetting(key: SettingKey, value: any) {
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

const Content: FC = () => {
  // Subscribe to sharedState — re-renders automatically when BackgroundManager
  // calls notifySubscribers(), even while QA panel was closed in between.
  const state = useSharedState();

  if (!state.settings) return null;

  // Locked: the triggers list, the settings and the Mifare tools all write
  // something the backend now refuses, so none of them is drawn. The lock is
  // enforced there, not here — this only stops the panel offering buttons that
  // would fail.
  if (state.restricted?.locked) {
    return (
      <PanelSection>
        <LockedPanel restricted={state.restricted} />
      </PanelSection>
    );
  }

  // Recomputed each render: sharedState.viewedApp changes trigger a re-render
  // via notifySubscribers(), so this stays in step with what's on screen.
  const pairTarget = resolvePairTarget();

  // Mifare tooling below is NFC-only, and now reads the per-source registry
  // rather than the single global slot — a disk in a drive must not put the
  // sector editor on screen.
  const nfcMedium = Object.values(state.activeMedia).find(
    (m) => m.source_type === SourceType.NFC,
  );

  return (
    <PanelSection>
      <TriggersPanel
        statuses={state.sourceStatuses}
        media={state.activeMedia}
        target={pairTarget}
        pairing={state.pairing}
      />

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
      </PanelSection>

      {state.restricted && <RestrictedPanel restricted={state.restricted} />}

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
