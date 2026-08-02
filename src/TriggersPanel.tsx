import { FC, ReactNode } from "react";
import {
  ButtonItem,
  DialogButton,
  Field,
  PanelSection,
  PanelSectionRow,
  Spinner,
  ToggleField,
} from "@decky/ui";
import { FaGamepad, FaLink } from "react-icons/fa";
import {
  sharedState,
  notifySubscribers,
  cancelPairing,
  type ActiveMedium,
  type SourceStatus,
} from "./shared";
import {
  TRIGGER_ROWS,
  isRowConnected,
  isRowEnabled,
  mediaStateFor,
  mediumFor,
  pairRow,
  statusFor,
  toggleRow,
  type TriggerRow,
} from "./lib/triggerRows";

export { TRIGGER_ROWS } from "./lib/triggerRows";

const MediaRow: FC<{
  row: TriggerRow;
  medium?: ActiveMedium;
  connected: boolean;
  target: { uri: string; label: string } | null;
}> = ({ row, medium, connected, target }) => {
  const state = mediaStateFor(row, connected, medium, target);

  // The medium's own icon, so a glance says "there is a disk in there".
  // While reading, a spinner takes its place: the disk is in the drive but
  // there is nothing to say about it yet, and a static icon next to
  // "Reading disk…" reads as stalled.
  const icon: ReactNode = state.busy
    ? <Spinner style={{ width: "1.1em", height: "1.1em" }} />
    : <span style={{ fontSize: "1.1em", opacity: medium ? 1 : 0.35 }}>{row.icon}</span>;

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
