import {
  SourceType,
  sharedState,
  notifySubscribers,
  toaster,
  registerKey,
  startPairing,
  setSourceSetting,
  formatMedia,
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

/** Shown in place of the trigger's own glyph when the medium is the key.
 *
 * The row's icon otherwise says which *kind* of medium is present, which is
 * already on the toggle above it — while a key looks exactly like every other
 * disk or tag until you read the label. Swapping the glyph is what makes "this
 * one is not a game" legible at a glance. */
const KEY_ICON = "🔑";

export interface MediaState {
  /** Left-hand text: what is on this trigger right now. */
  text: string;
  /** Button caption, or null when there is nothing to do. */
  action: string | null;
  /** Dim the row — no hardware, so nothing here is actionable. */
  dim: boolean;
  /** Overrides the trigger's glyph for this state. */
  icon?: string;
  /** Work is in progress; show a spinner in place of the medium icon. */
  busy?: boolean;
  /** The action erases the medium, so the row asks before doing it. Only ever
   *  set for a disk the backend flagged as carrying no filesystem. */
  destructive?: boolean;
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
  // The key is not a link and must never be offered a Pair button:
  // writing a game onto it would destroy the only thing that can unlock the
  // device. The backend refuses it too — this stops the user asking.
  //
  // A key payload this device does *not* recognise is a different thing: a
  // stale key, or someone else's. That is an ordinary medium with something
  // unusable on it, so it keeps its Pair button — without one it could never
  // be reused for anything.
  if (medium.key && medium.authorized !== false) {
    return { text: "Key", action: null, dim: false, icon: KEY_ICON };
  }
  if (medium.key) {
    return {
      text: "Unknown key",
      action: target ? "Pair" : null,
      dim: false,
      icon: KEY_ICON,
    };
  }
  if (medium.problem === "unreadable") {
    // Format is offered only when the backend found no filesystem at all — a
    // disk holding one we cannot mount is also unreadable but has data on it,
    // and that distinction is the whole reason this keys off `formattable`
    // rather than off the problem or the error text.
    return {
      text: medium.error || `Unreadable ${row.noun}`,
      action: medium.formattable ? "Format" : null,
      dim: false,
      destructive: !!medium.formattable,
    };
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

/** Format a disk that carries no filesystem.
 *
 * Destroys its contents — but the button is only ever offered for media the
 * backend flagged `formattable`, meaning blkid found nothing on it, so there is
 * nothing on it to lose. The backend re-checks that and every other guard,
 * because the disk can be swapped between the panel drawing the button and the
 * user pressing it.
 */
export async function formatRow(medium: ActiveMedium): Promise<boolean> {
  const { success, error } = await formatMedia(medium.media_id);
  if (!success) {
    toaster.toast({
      title: "Could not format",
      body: error || "The disk was refused.",
      critical: true,
    });
    return false;
  }
  toaster.toast({
    title: "Disk formatted",
    body: "Ready to pair.",
  });
  return true;
}

/** What one trigger offers while a key is being registered.
 *
 * A separate reducer from `mediaStateFor` rather than another flag on it: the
 * questions are different ones. Pairing asks "is there a game to write, and
 * does this medium already hold it"; registering a key asks only "can this
 * trigger be written to, and what would I be overwriting". Folding both into
 * one function meant four arguments that each only mattered half the time.
 *
 * `writable` comes from the backend's `can_pair`, not from anything the panel
 * knows: a camera reads codes it cannot write, and MQTT has no medium at all.
 */
export function keyStateFor(
  row: TriggerRow,
  connected: boolean,
  writable: boolean,
  medium: ActiveMedium | undefined,
): MediaState {
  if (!writable) return { text: "Cannot hold a key", action: null, dim: true };
  if (!connected) return { text: "Not connected", action: null, dim: true };

  if (medium?.problem === "loading") {
    return { text: `Reading ${row.noun}…`, action: null, dim: false, busy: true };
  }

  // Offered with no medium too. The backend arms the source and waits, so
  // "Register" on the Floppy row and then insert a disk is a legitimate order
  // to do this in — and refusing until something is present would make the row
  // look broken for the trigger you are holding the disk for.
  if (!medium) {
    return { text: `No ${row.noun} — insert one`, action: "Register", dim: false };
  }

  // No button: this medium is already the answer to the question the list is
  // asking, and pressing Register on it would write a *second* token over the
  // first — a key that still unlocks, having pointlessly rewritten itself.
  // Choosing a different trigger, or deregistering, are the only two moves
  // from here, and both live elsewhere.
  if (medium.key && medium.authorized) {
    return { text: "Already the key", action: null, dim: false, icon: KEY_ICON };
  }

  // Overwriting a game destroys that pairing, so the row says whose it is and
  // asks first — the same two-press confirm Format uses, for the same reason.
  if (medium.uri) {
    return {
      text: launchTargetName(medium.uri),
      action: "Register",
      dim: false,
      destructive: true,
    };
  }

  return { text: `Empty ${row.noun}`, action: "Register", dim: false };
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

/** Arm key registration on one trigger.
 *
 * The counterpart to `pairRow`, and targeted for the same reason it is: with a
 * tag on the reader and a stick in a drive, an untargeted register wrote the
 * key to whichever the backend happened to read first, which is exactly the
 * ambiguity `pairRow`'s comment above was written about.
 */
export async function registerKeyOn(
  row: TriggerRow,
  sourceId?: string,
): Promise<boolean> {
  const id = sourceId ?? mediumFor(row, sharedState.activeMedia)?.source_id;
  const ok = await registerKey(id);
  if (!ok) {
    toaster.toast({
      title: "Could not register",
      body: `${row.label} cannot be written to.`,
      critical: true,
    });
    return false;
  }
  sharedState.registeringKey = false;
  sharedState.pairing = true;
  notifySubscribers();
  return true;
}

/** Leave key-registration mode without writing anything.
 *
 * Only the choosing half is local — nothing has been armed on the backend yet,
 * which is why this needs no RPC. Once a trigger has been chosen the flow is an
 * ordinary armed pairing and `cancelPairing` is what stops it.
 */
export function cancelKeyRegistration(): void {
  sharedState.registeringKey = false;
  notifySubscribers();
}
