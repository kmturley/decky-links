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
import { FaLink, FaCircle, FaGamepad, FaMicrochip, FaHashtag, FaHdd } from "react-icons/fa";

// shared utilities extracted to avoid circular imports
import {
  useSharedState,
  toaster,
  setSetting,
  setSourceSetting,
  sharedState,
  cancelPairing,
  startPairing,
  notifySubscribers,
  settingsRef,
  SourceType,
  type SettingKey,
  type SourceStatus,
} from "./shared";

// ─────────────────────────────────────────────────────────────────────────────
// (the rest of the file remains unchanged)


import { KeyManagementPanel } from "./KeyManagementPanel";
import { SectorManagementPanel } from "./SectorManagementPanel";
import patchLibraryApp from "./lib/patchLibraryApp";
import { startBackgroundManager } from "./BackgroundManager";
import { resolveRungameidTarget, isSameLaunchTarget } from "./lib/steamIds";
import { sourceIcon, mediumNoun, presentMediaVerb, joinWithOr } from "./lib/sourceIcons";

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

async function triggerPairing() {
  if (sharedState.pairing) {
    await cancelPairing();
    sharedState.pairing = false;
    notifySubscribers();
    return;
  }

  const target = resolvePairTarget();
  if (!target) {
    // The button is disabled in this state, so this is a safety net only.
    toaster.toast({
      title: "Pairing Error",
      body: "Open a game's page or start a game first.",
      critical: true,
    });
    return;
  }

  const uri = target.uri;
  console.info(`[ Decky Links ] Starting pairing for: ${uri}`);
  const ok = await startPairing(uri);
  if (!ok) {
    toaster.toast({ title: "Pairing Error", body: "Failed to start pairing mode.", critical: true });
    return;
  }

  sharedState.pairing = true;
  notifySubscribers();
}

async function triggerUpdateSourceSetting(sourceType: string, key: string, value: any) {
  const ok = await setSourceSetting(sourceType, key, value);
  if (!ok) {
    toaster.toast({ title: "Settings Error", body: `Invalid value for ${sourceType}.${key}.`, critical: true });
    return;
  }
  if (sharedState.settings) {
    const sources = sharedState.settings.sources as any;
    sharedState.settings = {
      ...sharedState.settings,
      sources: {
        ...sources,
        [sourceType]: { ...(sources[sourceType] ?? {}), [key]: value },
      },
    };
  }
  settingsRef.current = sharedState.settings;
  notifySubscribers();
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

/** Drive categories, as udev distinguishes them. Defaults mirror the backend:
 *  collectible media on, the user's own storage off. */
const DRIVE_KINDS: { key: string; label: string; description: string; fallback: boolean }[] = [
  { key: "floppy",  label: "Floppy Drives",  description: "USB and internal floppy drives", fallback: true },
  { key: "optical", label: "Optical Drives", description: "CD and DVD drives",              fallback: true },
  { key: "usb",     label: "USB Storage",    description: "Thumb drives and external disks", fallback: false },
  { key: "flash",   label: "Memory Cards",   description: "SD and other card readers",       fallback: false },
];

function driveKindEnabled(storage: { drive_kinds?: Record<string, boolean> }, key: string): boolean {
  const configured = storage.drive_kinds?.[key];
  if (typeof configured === "boolean") return configured;
  return DRIVE_KINDS.find((k) => k.key === key)?.fallback ?? false;
}

/** The backend stores drive_kinds as one dict, so a single toggle has to send
 *  the whole merged map rather than just the key that changed. */
async function triggerToggleDriveKind(
  storage: { drive_kinds?: Record<string, boolean> },
  key: string,
  value: boolean,
) {
  const merged: Record<string, boolean> = {};
  for (const kind of DRIVE_KINDS) merged[kind.key] = driveKindEnabled(storage, kind.key);
  merged[key] = value;
  await triggerUpdateSourceSetting("storage", "drive_kinds", merged);
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
  const sourceLabel = state.readerStatus.source_type ? state.readerStatus.source_type.toUpperCase() : "NFC";

  const storageSettings = state.settings.sources.storage;
  const cameraSettings = state.settings.sources.camera;
  const mqttSettings = state.settings.sources.mqtt;
  const serialSettings = state.settings.sources.serial;
  const fileWatchSettings = state.settings.sources.file_watch;

  // Recomputed each render: sharedState.viewedApp changes trigger a re-render
  // via notifySubscribers(), so this stays in step with what's on screen.
  const pairTarget = resolvePairTarget();

  // A card on the reader that already points at this game needs no rewrite.
  // Compared by app id rather than string: the card may hold steam://run/400
  // while the button would write steam://rungameid/400 — same game.
  const alreadyPaired = !!(
    state.tagUid && pairTarget && isSameLaunchTarget(state.tagUri, pairTarget.uri)
  );
  const gameStatusValue = state.activeAppId
    ? `Playing ${state.activeAppId}`
    : state.viewedApp
      ? `Viewing ${state.viewedApp.name || state.viewedApp.appId}`
      : "Not Playing";

  // Pairing needs both halves: something to write, and something to write it to.
  // Enabling the button without a medium meant pressing it just armed pairing
  // and waited, which reads as a no-op. The game-page icon still supports the
  // press-then-tap flow for when you want to arm it deliberately.
  const mediaPresent = !!state.tagUid;
  const isStorage = state.tagSourceType === SourceType.STORAGE;
  const noun = mediumNoun(state.tagSourceType ?? SourceType.NFC);

  // Everything the panel says about media is phrased from what is actually
  // connected. With only a floppy drive plugged in, telling the user to tap a
  // card is simply wrong.
  const pairableSources = state.sourceStatuses.filter((s) => s.can_pair && s.active);
  const presentVerbs = joinWithOr(pairableSources.map((s) => presentMediaVerb(s.source_type)));
  const waitingLabel = pairableSources.length > 0
    ? `Waiting for ${joinWithOr(pairableSources.map((s) => mediumNoun(s.source_type)))}`
    : "No Device Connected";

  const pairBlockedReason = !pairTarget
    ? "Open a game's page to pair it."
    : pairableSources.length === 0
      ? "Connect an NFC reader or a disk drive to pair."
      : state.mediaProblem?.kind === "unreadable"
        ? state.mediaProblem.error || `This ${noun} could not be read.`
        : !mediaPresent
          ? `To pair, ${presentVerbs}.`
          : alreadyPaired
            ? `This ${noun} already launches ${pairTarget.label}.`
            : null;

  const pairLabel = state.pairing
    ? "Cancel Pairing"
    : alreadyPaired
      ? "Already Paired"
      : state.mediaProblem?.kind === "unreadable"
        ? `Unreadable ${noun.charAt(0).toUpperCase()}${noun.slice(1)}`
        : !mediaPresent
          ? waitingLabel
          : pairTarget
            ? `Pair ${pairTarget.label}`
            : "Pair Current Game";

  return (
    <PanelSection>
      <PanelSection title="Status">
        {state.sourceStatuses.length > 0 ? (
          state.sourceStatuses.map((src: SourceStatus) => (
            <StatusRow
              key={src.source_id}
              icon={sourceIcon(src.source_type)}
              label={src.source_type.toUpperCase()}
              // "Connected" means the drive/reader is there; media presence is
              // its own line below. Collapsing the two made ejecting a floppy
              // look like the drive had been unplugged.
              value={
                src.enabled === false
                  ? "Off"
                  : !src.active
                    ? "Not Connected"
                    : src.has_media === false
                      ? "Connected, empty"
                      : src.source_id.split(":").pop() || "Active"
              }
              active={src.active}
            />
          ))
        ) : (
          <StatusRow
            icon={<FaMicrochip />}
            label={sourceLabel}
            value={state.readerStatus.connected ? (state.readerStatus.path?.split('/').pop() || state.readerStatus.path || "Connected") : "Not Found"}
            active={state.readerStatus.connected}
          />
        )}
        <StatusRow
          icon={isStorage ? <FaHdd /> : <FaHashtag />}
          label={isStorage ? "Disk" : "Tag"}
          // A storage media_id is a device node; the leading /dev/ is noise.
          value={state.tagUid ? (isStorage ? state.tagUid.replace(/^\/dev\//, "") : state.tagUid) : "Not Connected"}
          active={!!state.tagUid}
        />
        <StatusRow
          icon={<FaLink />}
          label="Url"
          value={
            state.tagUri
              ?? (state.mediaProblem?.kind === "unreadable"
                    ? "Unreadable"
                    : state.mediaProblem?.kind === "blocked"
                      ? "Blocked"
                      : "Empty")
          }
          active={!!state.tagUri}
        />
        <StatusRow
          icon={<FaGamepad />}
          label="Game"
          value={gameStatusValue}
          active={!!state.activeAppId || !!state.viewedApp}
        />
        <ButtonItem
          layout="below"
          onClick={triggerPairing}
          // Gated on media being present, not on the NFC reader being connected:
          // a floppy is pairable on a Deck with no reader plugged in at all.
          // Never disable while pairing — the button is the only way to cancel.
          disabled={!state.pairing && pairBlockedReason !== null}
        >
          {pairLabel}
        </ButtonItem>
        {!state.pairing && pairBlockedReason && (
          <div style={{ fontSize: "0.7rem", opacity: 0.6, padding: "0 16px 8px" }}>
            {pairBlockedReason}
          </div>
        )}
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

      <PanelSection title="Sources">
        <PanelSectionRow>
          <TextField
            label="NFC Device Path"
            value={nfcSettings.device_path}
            onChange={(e) => triggerUpdateSetting("device_path", e.target.value)}
          />
        </PanelSectionRow>
        {storageSettings && (
          <PanelSectionRow>
            <ToggleField
              label="Disk Triggers"
              description="Watch removable drives for paired media"
              checked={storageSettings.enabled}
              onChange={(v: boolean) => triggerUpdateSourceSetting("storage", "enabled", v)}
            />
          </PanelSectionRow>
        )}
        {/* Which kinds of drive to act on. A floppy and a thumb drive are both
            "removable" to the kernel but not to a person: one holds collectible
            media, the other usually holds the user's own files. */}
        {storageSettings?.enabled && DRIVE_KINDS.map(({ key, label, description }) => (
          <PanelSectionRow key={key}>
            <ToggleField
              label={label}
              description={description}
              checked={driveKindEnabled(storageSettings, key)}
              onChange={(v: boolean) => triggerToggleDriveKind(storageSettings, key, v)}
            />
          </PanelSectionRow>
        ))}
        {cameraSettings && (
          <PanelSectionRow>
            <ToggleField
              label="Camera Trigger"
              description={`QR codes via ${cameraSettings.device}`}
              checked={cameraSettings.enabled}
              onChange={(v: boolean) => triggerUpdateSourceSetting("camera", "enabled", v)}
            />
          </PanelSectionRow>
        )}
        {mqttSettings && (
          <PanelSectionRow>
            <ToggleField
              label="MQTT Trigger"
              description={`Broker: ${mqttSettings.broker_host}:${mqttSettings.broker_port}`}
              checked={mqttSettings.enabled}
              onChange={(v: boolean) => triggerUpdateSourceSetting("mqtt", "enabled", v)}
            />
          </PanelSectionRow>
        )}
        {serialSettings && (
          <PanelSectionRow>
            <ToggleField
              label="Serial Trigger"
              description={`Port: ${serialSettings.port}`}
              checked={serialSettings.enabled}
              onChange={(v: boolean) => triggerUpdateSourceSetting("serial", "enabled", v)}
            />
          </PanelSectionRow>
        )}
        {fileWatchSettings && (
          <PanelSectionRow>
            <ToggleField
              label="File Watch Trigger"
              description={fileWatchSettings.watch_dir || "No directory set"}
              checked={fileWatchSettings.enabled}
              onChange={(v: boolean) => triggerUpdateSourceSetting("file_watch", "enabled", v)}
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      <KeyManagementPanel />

      {/* Keys and sectors are Mifare concepts; a floppy has neither. */}
      {!isStorage && (
        <SectorManagementPanel tagUid={state.tagUid || undefined} />
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
