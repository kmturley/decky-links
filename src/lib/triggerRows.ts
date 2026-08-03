import {
  SourceType,
  sharedState,
  notifySubscribers,
  toaster,
  startPairing,
  setSourceSetting,
  type ActiveMedium,
  type SourceStatus,
} from "../shared";
import { isSameLaunchTarget } from "./steamIds";
import { launchTargetName } from "./appNames";

/** One row in the Triggers list.
 *
 * A row is a *category* of hardware, not a device: "Floppy", not "the TEAC in
 * the left port". Neither the CH340 reader nor the TEAC drive exposes a USB
 * serial number, so per-device identity would really mean per-USB-socket — and
 * swapping ports or replacing a dead reader would break the pairing.
 * Categories survive both.
 *
 * Shared by the Quick Access panel and the game-page modal so a trigger cannot
 * be described one way in one place and another way in the other.
 */
export interface TriggerRow {
  key: string;
  label: string;
  /** Backend source this row toggles. */
  sourceType: SourceType;
  /** Storage only: which drive category within that source. */
  driveKind?: string;
  /** Emoji for the medium this trigger reads. It sits on the media row only:
   *  the glyphs depict media (a disk, a card), not the drives that read them,
   *  and repeating one on the toggle row above read as visual noise. */
  icon: string;
  /** What the user physically puts on this trigger. "Empty disk" and "No card"
   *  read like the hardware; "Empty media" reads like a spec document. */
  noun: string;
  /** Nothing is written to this trigger — its medium is generated, not paired.
   *  QR codes are printed from an app id, so a Pair button would be lying. */
  generated?: boolean;
}

export const TRIGGER_ROWS: TriggerRow[] = [
  { key: "nfc",     label: "NFC",         sourceType: SourceType.NFC,        icon: "🏷️", noun: "tag" },
  { key: "floppy",  label: "Floppy",      sourceType: SourceType.STORAGE,    icon: "💾", noun: "disk",    driveKind: "floppy" },
  { key: "optical", label: "Optical",     sourceType: SourceType.STORAGE,    icon: "💿", noun: "disc",    driveKind: "optical" },
  { key: "flash",   label: "Memory Card", sourceType: SourceType.STORAGE,    icon: "💳", noun: "card",    driveKind: "flash" },
  { key: "usb",     label: "USB Storage", sourceType: SourceType.STORAGE,    icon: "🔌", noun: "drive",   driveKind: "usb" },
  { key: "mqtt",    label: "IoT",         sourceType: SourceType.MQTT,       icon: "📡", noun: "message" },
  { key: "serial",  label: "Serial",      sourceType: SourceType.SERIAL,     icon: "📟", noun: "code" },
  { key: "camera",  label: "Camera",      sourceType: SourceType.CAMERA,     icon: "📷", noun: "code", generated: true },
  { key: "file",    label: "File",        sourceType: SourceType.FILE_WATCH, icon: "📁", noun: "file" },
];

export function statusFor(row: TriggerRow, statuses: SourceStatus[]) {
  return statuses.find((s) => s.source_type === row.sourceType);
}

/** Is this row switched on? Storage rows read their drive category; every
 *  other row reads its source's own `enabled`. */
export function isRowEnabled(row: TriggerRow, status?: SourceStatus): boolean {
  if (row.driveKind) {
    if (status?.enabled === false) return false;   // whole source switched off
    return status?.drive_kinds?.[row.driveKind]?.enabled ?? false;
  }
  return status?.enabled ?? false;
}

/** Is the hardware for this row actually attached? */
export function isRowConnected(row: TriggerRow, status?: SourceStatus): boolean {
  if (!status?.active) return false;
  if (row.driveKind) return status.drive_kinds?.[row.driveKind]?.present ?? false;
  return true;
}

/** The medium sitting on this row's hardware, if any. */
export function mediumFor(
  row: TriggerRow,
  media: Record<string, ActiveMedium>,
): ActiveMedium | undefined {
  return Object.values(media).find((m) => {
    if (m.source_type !== row.sourceType) return false;
    if (row.driveKind) return m.drive_kind === row.driveKind;
    return true;
  });
}

export async function toggleRow(row: TriggerRow, value: boolean, status?: SourceStatus) {
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

export interface MediaState {
  /** Left-hand text: what is on this trigger right now. */
  text: string;
  /** Button caption, or null when there is nothing to do. */
  action: string | null;
  /** Dim the row — no hardware, so nothing here is actionable. */
  dim: boolean;
  /** Work is in progress; show a spinner in place of the medium icon. */
  busy?: boolean;
}

/** Reduce a trigger's hardware + medium into the one line the UI shows.
 *
 * `armed` is for the game-page modal, where pressing Pair on a connected row
 * arms it and then waits for a medium to be presented — the panel's rows are
 * only ever pressed when a medium is already there.
 */
export function mediaStateFor(
  row: TriggerRow,
  connected: boolean,
  medium: ActiveMedium | undefined,
  target: { uri: string; label: string } | null,
  armed = false,
): MediaState {
  if (!connected) return { text: "Not connected", action: null, dim: true };

  if (!medium) {
    if (armed) {
      return { text: `Present a ${row.noun}…`, action: null, dim: false, busy: true };
    }
    // A generated trigger has no medium to be missing — the camera is either
    // watching or it is not, and "No code" would read as a fault.
    if (row.generated) return { text: "Ready", action: null, dim: false };
    return { text: `No ${row.noun}`, action: null, dim: true };
  }

  if (medium.problem === "loading") {
    return { text: `Reading ${row.noun}…`, action: null, dim: false, busy: true };
  }
  if (medium.problem === "unreadable") {
    return { text: medium.error || `Unreadable ${row.noun}`, action: null, dim: false };
  }
  if (medium.problem === "blocked") {
    return { text: "Blocked by allowlist", action: null, dim: false };
  }

  if (armed) {
    return { text: `Writing to ${row.noun}…`, action: null, dim: false, busy: true };
  }
  // Nothing is ever written to a generated trigger, so a code in view is
  // reported and never offered a Pair button.
  if (row.generated) {
    return {
      text: medium.uri ? launchTargetName(medium.uri) : `Unrecognised ${row.noun}`,
      action: null,
      dim: false,
    };
  }
  if (!medium.uri) {
    return { text: `Empty ${row.noun}`, action: target ? "Pair" : null, dim: false };
  }
  if (target && isSameLaunchTarget(medium.uri, target.uri)) {
    // No button and no tick: the absent button already says "nothing to do
    // here", and the row names the game it is paired to.
    return { text: target.label, action: null, dim: false };
  }
  // "Pair", not "Re-pair" — the action is identical either way, and the row
  // already shows what would be overwritten.
  return { text: launchTargetName(medium.uri), action: target ? "Pair" : null, dim: false };
}

/** Arm pairing for one trigger.
 *
 * Targeted: with a tag on the reader and a disk in the drive, an untargeted
 * pair would write to whichever the backend saw first. Works with or without a
 * medium already present — the backend re-arms its sources, so a card already
 * resting on the reader is picked up on the next poll rather than needing to be
 * lifted and re-tapped.
 */
export async function pairRow(
  row: TriggerRow,
  target: { uri: string; label: string },
  sourceId?: string,
): Promise<boolean> {
  const id = sourceId ?? mediumFor(row, sharedState.activeMedia)?.source_id;
  // target.label is the game name, already resolved for the button's own text.
  // Passing it lets the backend record it alongside the URI, so a disk read on
  // another machine names the game instead of just carrying an app id.
  const ok = await startPairing(target.uri, id, target.label);
  if (!ok) {
    toaster.toast({ title: "Pairing Error", body: `Could not pair ${row.label}.`, critical: true });
    return false;
  }
  sharedState.pairing = true;
  notifySubscribers();
  return true;
}
