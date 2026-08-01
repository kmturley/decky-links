import { FC } from "react";
import { ButtonItem, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { FaLink } from "react-icons/fa";
import {
  SourceType,
  sharedState,
  notifySubscribers,
  toaster,
  startPairing,
  cancelPairing,
  setSourceSetting,
  type ActiveMedium,
  type SourceStatus,
} from "./shared";
import { sourceIcon, mediumNoun } from "./lib/sourceIcons";
import { isSameLaunchTarget } from "./lib/steamIds";

/** One row in the Triggers list.
 *
 * A row is a *category* of hardware, not a device: "Floppy Drives", not
 * "the TEAC in the left port". Neither the CH340 reader nor the TEAC drive
 * exposes a USB serial number, so per-device identity would really mean
 * per-USB-socket — and swapping ports or replacing a dead reader would break
 * the pairing. Categories survive both.
 */
export interface TriggerRow {
  key: string;
  label: string;
  /** Backend source this row toggles. */
  sourceType: SourceType;
  /** Storage only: which drive category within that source. */
  driveKind?: string;
  /** Settings key the toggle writes, when it is not the source's own `enabled`. */
  description?: string;
}

export const TRIGGER_ROWS: TriggerRow[] = [
  { key: "nfc",     label: "NFC",          sourceType: SourceType.NFC },
  { key: "floppy",  label: "Floppy",       sourceType: SourceType.STORAGE, driveKind: "floppy" },
  { key: "optical", label: "Optical",      sourceType: SourceType.STORAGE, driveKind: "optical" },
  { key: "flash",   label: "Memory Card",  sourceType: SourceType.STORAGE, driveKind: "flash" },
  { key: "usb",     label: "USB Storage",  sourceType: SourceType.STORAGE, driveKind: "usb" },
  { key: "mqtt",    label: "IoT",          sourceType: SourceType.MQTT },
  { key: "serial",  label: "Serial",       sourceType: SourceType.SERIAL },
  { key: "camera",  label: "Camera",       sourceType: SourceType.CAMERA },
  { key: "file",    label: "File",         sourceType: SourceType.FILE_WATCH },
];

const MEDIUM_ICON: Record<string, string> = {
  [SourceType.NFC]: "🎴",
  floppy: "💾",
  optical: "💿",
  flash: "🗃️",
  usb: "🔌",
};

function statusFor(row: TriggerRow, statuses: SourceStatus[]) {
  return statuses.find((s) => s.source_type === row.sourceType);
}

/** Is this row switched on? Storage rows read their drive category; every
 *  other row reads its source's own `enabled`. */
function isRowEnabled(row: TriggerRow, status?: SourceStatus): boolean {
  if (row.driveKind) {
    if (status?.enabled === false) return false;   // whole source switched off
    return status?.drive_kinds?.[row.driveKind]?.enabled ?? false;
  }
  return status?.enabled ?? false;
}

/** Is the hardware for this row actually attached? */
function isRowConnected(row: TriggerRow, status?: SourceStatus): boolean {
  if (!status?.active) return false;
  if (row.driveKind) return status.drive_kinds?.[row.driveKind]?.present ?? false;
  return true;
}

/** The medium sitting on this row's hardware, if any. */
function mediumFor(row: TriggerRow, media: Record<string, ActiveMedium>): ActiveMedium | undefined {
  return Object.values(media).find((m) => {
    if (m.source_type !== row.sourceType) return false;
    if (row.driveKind) return m.drive_kind === row.driveKind;
    return true;
  });
}

async function toggleRow(row: TriggerRow, value: boolean, status?: SourceStatus) {
  let ok: boolean;
  if (row.driveKind) {
    // The backend stores drive categories as one dict, so a single toggle has
    // to send the whole merged map rather than just the key that changed.
    const merged: Record<string, boolean> = {};
    for (const other of TRIGGER_ROWS) {
      if (other.driveKind) {
        merged[other.driveKind] = status?.drive_kinds?.[other.driveKind]?.enabled ?? false;
      }
    }
    merged[row.driveKind] = value;
    ok = await setSourceSetting("storage", "drive_kinds", merged);
    // Switching a category on is meaningless while the source itself is off.
    if (ok && value && status?.enabled === false) {
      await setSourceSetting("storage", "enabled", true);
    }
  } else {
    ok = await setSourceSetting(row.sourceType, "enabled", value);
  }

  if (!ok) {
    toaster.toast({ title: "Triggers", body: `Could not change ${row.label}.`, critical: true });
    return;
  }
  notifySubscribers();
}

const MediaLine: FC<{
  row: TriggerRow;
  medium?: ActiveMedium;
  connected: boolean;
  target: { uri: string; label: string } | null;
}> = ({ row, medium, connected, target }) => {
  const noun = mediumNoun(row.driveKind ? SourceType.STORAGE : row.sourceType);

  let text: string;
  let canPair = false;
  if (!connected) {
    text = "not connected";
  } else if (!medium) {
    text = `no ${noun}`;
  } else if (medium.problem === "unreadable") {
    text = medium.error || `unreadable ${noun}`;
  } else if (medium.problem === "blocked") {
    text = "blocked by allowlist";
  } else if (!medium.uri) {
    text = "available";
    canPair = true;
  } else if (target && isSameLaunchTarget(medium.uri, target.uri)) {
    text = `→ ${target.label} ✓`;
  } else {
    text = `→ ${medium.uri.replace(/^steam:\/\/(run|rungameid)\//, "app ")}`;
    canPair = true;
  }

  const pairable = canPair && !!target;
  const icon = MEDIUM_ICON[row.driveKind ?? row.sourceType] ?? "•";

  return (
    <div style={{
      display: "flex",
      alignItems: "center",
      gap: 8,
      padding: "0 8px 4px 24px",
      fontSize: "0.8em",
      opacity: connected ? 0.9 : 0.45,
    }}>
      <span style={{ color: connected ? "#4CAF50" : "#757575", display: "flex" }}>
        {sourceIcon(row.sourceType)}
      </span>
      <span>{medium ? icon : "·"}</span>
      <span style={{ flex: 1, fontFamily: "monospace" }}>{text}</span>
      {pairable && (
        <span
          style={{ display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}
          onClick={() => void pairRow(row, target!)}
        >
          pair <FaLink size={11} />
        </span>
      )}
    </div>
  );
};

async function pairRow(row: TriggerRow, target: { uri: string; label: string }) {
  const medium = mediumFor(row, sharedState.activeMedia);
  if (!medium) return;

  // Targeted: with a tag on the reader and a disk in the drive, an untargeted
  // pair would write to whichever the backend saw first.
  const ok = await startPairing(target.uri, medium.source_id);
  if (!ok) {
    toaster.toast({ title: "Pairing Error", body: `Could not pair ${row.label}.`, critical: true });
    return;
  }
  sharedState.pairing = true;
  notifySubscribers();
}

export const TriggersPanel: FC<{
  statuses: SourceStatus[];
  media: Record<string, ActiveMedium>;
  target: { uri: string; label: string } | null;
  pairing: boolean;
}> = ({ statuses, media, target, pairing }) => {
  return (
    <PanelSection title="Triggers">
      <div style={{ padding: "0 8px 6px", fontSize: "0.75em", opacity: 0.7 }}>
        {target
          ? <>Pairing with: <b>{target.label}</b></>
          : "Open a game's page to pair"}
      </div>

      {pairing && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={async () => {
            await cancelPairing();
            sharedState.pairing = false;
            notifySubscribers();
          }}>
            Cancel Pairing
          </ButtonItem>
        </PanelSectionRow>
      )}

      {TRIGGER_ROWS.map((row) => {
        const status = statusFor(row, statuses);
        const enabled = isRowEnabled(row, status);
        const connected = isRowConnected(row, status);
        return (
          <div key={row.key}>
            <PanelSectionRow>
              <ToggleField
                label={row.label}
                checked={enabled}
                onChange={(v: boolean) => void toggleRow(row, v, status)}
              />
            </PanelSectionRow>
            {enabled && (
              <MediaLine
                row={row}
                medium={mediumFor(row, media)}
                connected={connected}
                target={target}
              />
            )}
          </div>
        );
      })}
    </PanelSection>
  );
};
