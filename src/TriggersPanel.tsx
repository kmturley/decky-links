import { FC, ReactNode } from "react";
import {
  ButtonItem,
  DialogButton,
  Field,
  PanelSection,
  PanelSectionRow,
  ToggleField,
} from "@decky/ui";
import { FaGamepad, FaLink } from "react-icons/fa";
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
import { isSameLaunchTarget } from "./lib/steamIds";
import { launchTargetName } from "./lib/appNames";

/** One row in the Triggers list.
 *
 * A row is a *category* of hardware, not a device: "Floppy", not "the TEAC in
 * the left port". Neither the CH340 reader nor the TEAC drive exposes a USB
 * serial number, so per-device identity would really mean per-USB-socket — and
 * swapping ports or replacing a dead reader would break the pairing.
 * Categories survive both.
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
   *  and repeating one on the toggle row above read as visual noise. The icon
   *  is also what distinguishes a media row from a trigger row now that the
   *  media row is no longer indented. */
  icon: string;
  /** What the user physically puts on this trigger. "Empty disk" and "No card"
   *  read like the hardware; "Empty media" reads like a spec document. */
  noun: string;
}

export const TRIGGER_ROWS: TriggerRow[] = [
  { key: "nfc",     label: "NFC",         sourceType: SourceType.NFC,        icon: "🏷️", noun: "tag" },
  { key: "floppy",  label: "Floppy",      sourceType: SourceType.STORAGE,    icon: "💾", noun: "disk",    driveKind: "floppy" },
  { key: "optical", label: "Optical",     sourceType: SourceType.STORAGE,    icon: "💿", noun: "disc",    driveKind: "optical" },
  { key: "flash",   label: "Memory Card", sourceType: SourceType.STORAGE,    icon: "💳", noun: "card",    driveKind: "flash" },
  { key: "usb",     label: "USB Storage", sourceType: SourceType.STORAGE,    icon: "🔌", noun: "drive",   driveKind: "usb" },
  { key: "mqtt",    label: "IoT",         sourceType: SourceType.MQTT,       icon: "📡", noun: "message" },
  { key: "serial",  label: "Serial",      sourceType: SourceType.SERIAL,     icon: "📟", noun: "code" },
  { key: "camera",  label: "Camera",      sourceType: SourceType.CAMERA,     icon: "📷", noun: "code" },
  { key: "file",    label: "File",        sourceType: SourceType.FILE_WATCH, icon: "📁", noun: "file" },
];

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

interface MediaState {
  /** Left-hand text: what is on this trigger right now. */
  text: string;
  /** Button caption, or null when there is nothing to do. */
  action: string | null;
  /** Dim the row — no hardware, so nothing here is actionable. */
  dim: boolean;
}

/** Reduce a trigger's hardware + medium into the one line the panel shows. */
export function mediaStateFor(
  row: TriggerRow,
  connected: boolean,
  medium: ActiveMedium | undefined,
  target: { uri: string; label: string } | null,
): MediaState {
  if (!connected) return { text: "Not connected", action: null, dim: true };
  if (!medium) return { text: `No ${row.noun}`, action: null, dim: true };

  if (medium.problem === "unreadable") {
    return { text: medium.error || `Unreadable ${row.noun}`, action: null, dim: false };
  }
  if (medium.problem === "blocked") {
    return { text: "Blocked by allowlist", action: null, dim: false };
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

const MediaRow: FC<{
  row: TriggerRow;
  medium?: ActiveMedium;
  connected: boolean;
  target: { uri: string; label: string } | null;
}> = ({ row, medium, connected, target }) => {
  const state = mediaStateFor(row, connected, medium, target);

  // The medium's own icon, so a glance says "there is a disk in there".
  const icon: ReactNode = (
    <span style={{ fontSize: "1.1em", opacity: medium ? 1 : 0.35 }}>{row.icon}</span>
  );

  if (!state.action) {
    return (
      <PanelSectionRow>
        <Field
          icon={icon}
          label={state.text}
          focusable={false}
          bottomSeparator="standard"
          highlightOnFocus={false}
        />
      </PanelSectionRow>
    );
  }

  // A Field with its own DialogButton rather than ButtonItem: ButtonItem's
  // inline layout sizes the button from the row instead of from its label, so
  // "Pair" came out ~500px wide and overhung the panel's right padding, which
  // every other control respects. Styling the button directly is the only way
  // to pin it to its content — ButtonItem exposes no style hook.
  return (
    <PanelSectionRow>
      <Field
        icon={icon}
        label={state.text}
        childrenContainerWidth="min"
        bottomSeparator="standard"
      >
        <DialogButton
          onClick={() => void pairRow(row, target!)}
          style={{
            minWidth: 0,
            width: "fit-content",
            padding: "8px 16px",
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <FaLink size={12} />
          {state.action}
        </DialogButton>
      </Field>
    </PanelSectionRow>
  );
};

export const TriggersPanel: FC<{
  statuses: SourceStatus[];
  media: Record<string, ActiveMedium>;
  target: { uri: string; label: string } | null;
  pairing: boolean;
}> = ({ statuses, media, target, pairing }) => {
  return (
    <PanelSection title="Triggers">
      {/* The pairing target, once. Putting it on every Pair button instead
          would repeat a name long enough to wrap ("Vampire Survivors: Ode to
          Castlevania") on up to nine rows. */}
      <PanelSectionRow>
        <Field
          icon={<FaGamepad />}
          label={target ? target.label : "No game selected"}
          description={target ? "Game to be paired" : "Open a game to pair"}
          bottomSeparator="thick"
          focusable={false}
          highlightOnFocus={false}
        />
      </PanelSectionRow>

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

      {/* Flattened deliberately: a wrapper element between PanelSection and
          PanelSectionRow sits in the middle of Steam's own child selectors, so
          rows inside one lose the panel's horizontal padding and run to the
          edge of the screen. Keys go on the rows themselves instead. */}
      {TRIGGER_ROWS.flatMap((row) => {
        const status = statusFor(row, statuses);
        const enabled = isRowEnabled(row, status);
        const connected = isRowConnected(row, status);
        const rows = [
          <PanelSectionRow key={row.key}>
            <ToggleField
              label={row.label}
              checked={enabled}
              bottomSeparator={enabled ? "none" : "standard"}
              onChange={(v: boolean) => void toggleRow(row, v, status)}
            />
          </PanelSectionRow>,
        ];
        if (enabled) {
          rows.push(
            <MediaRow
              key={`${row.key}-media`}
              row={row}
              medium={mediumFor(row, media)}
              connected={connected}
              target={target}
            />,
          );
        }
        return rows;
      })}
    </PanelSection>
  );
};
