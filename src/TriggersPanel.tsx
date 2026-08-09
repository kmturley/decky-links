import { FC, ReactNode, useEffect, useState } from "react";
import {
  ButtonItem,
  DialogButton,
  Field,
  PanelSection,
  PanelSectionRow,
  Spinner,
  ToggleField,
} from "@decky/ui";
import { FaEraser, FaGamepad, FaKey, FaLink } from "react-icons/fa";
import {
  sharedState,
  notifySubscribers,
  cancelPairing,
  toaster,
  type ActiveMedium,
  type SourceStatus,
} from "./shared";
import {
  TRIGGER_ROWS,
  deregisterKey,
  isRowConnected,
  isRowEnabled,
  mediaStateFor,
  mediumFor,
  formatRow,
  keyStateFor,
  pairRow,
  registerKeyOn,
  statusFor,
  toggleRow,
  type TriggerRow,
} from "./lib/triggerRows";

export { TRIGGER_ROWS } from "./lib/triggerRows";

/** Copy text, and say whether it worked.
 *
 * The Steam UI is Chromium, so navigator.clipboard is normally there — but it
 * is gated on a secure context and on focus, neither of which is guaranteed in
 * a side menu. Silently failing would leave the user thinking they had the
 * secret on the clipboard, which is worse than knowing they need to read it
 * off the screen.
 */
async function copyToClipboard(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    toaster.toast({ title: "Copied", body: "Secret copied to clipboard." });
  } catch (e) {
    console.error("[ Decky Links ] Clipboard write failed:", e);
    toaster.toast({
      title: "Could not copy",
      body: "Read it from the panel instead.",
      critical: true,
    });
  }
}

const MediaRow: FC<{
  row: TriggerRow;
  medium?: ActiveMedium;
  connected: boolean;
  writable: boolean;
  target: { uri: string; label: string } | null;
  /** Registering a key: the row's button targets *this* trigger instead of
   *  pairing a game to it. */
  registeringKey: boolean;
  sourceId?: string;
}> = ({ row, medium, connected, writable, target, registeringKey, sourceId }) => {
  const state = registeringKey
    ? keyStateFor(row, connected, writable, medium)
    : mediaStateFor(row, connected, medium, target);
  const [confirming, setConfirming] = useState(false);

  // Drop a pending confirm if the disk changes underneath it — ejected,
  // swapped, or newly readable. Leaving "Confirm" armed across a swap would
  // aim the second press at a disk the user never saw the first one on.
  useEffect(() => {
    setConfirming(false);
  }, [medium?.media_id, state.destructive]);

  // The medium's own icon, so a glance says "there is a disk in there".
  // While reading, a spinner takes its place: the disk is in the drive but
  // there is nothing to say about it yet, and a static icon next to
  // "Reading disk…" reads as stalled.
  // A state may override the glyph — a key wears a key rather than the disk it
  // happens to be written on.
  const icon: ReactNode = state.busy
    ? <Spinner style={{ width: "1.1em", height: "1.1em" }} />
    : <span style={{ fontSize: "1.1em", opacity: medium ? 1 : 0.35 }}>{state.icon ?? row.icon}</span>;

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
  // Formatting erases the disk, so it asks first. The confirm is a second
  // press of the same button rather than a modal: the guarantee that makes this
  // safe is the backend's — it refuses any disk that has a filesystem — and a
  // modal would imply the button is the thing standing between the user and
  // data loss, which would be the wrong thing to believe.
  //
  // Registering a key over a medium that holds a game destroys that pairing,
  // which is the same kind of loss by the same kind of press, so it asks in
  // the same way rather than inventing a second idiom for it.
  const act = () => {
    if (registeringKey) {
      void registerKeyOn(row, sourceId);
      return;
    }
    if (state.action === "Deregister") {
      void deregisterKey();
      return;
    }
    if (state.destructive) {
      void formatRow(medium!);
      return;
    }
    void pairRow(row, target!);
  };

  const onPress = state.destructive
    ? () => {
        if (!confirming) {
          setConfirming(true);
          return;
        }
        setConfirming(false);
        act();
      }
    : act;

  const confirmLabel = registeringKey
    ? `Replace ${state.text} with a key?`
    : `Erase this ${row.noun}?`;

  return (
    <PanelSectionRow>
      <Field
        icon={icon}
        label={confirming ? confirmLabel : state.text}
        childrenContainerWidth="min"
        bottomSeparator="standard"
      >
        <DialogButton
          onClick={onPress}
          style={{
            minWidth: 0,
            width: "fit-content",
            padding: "8px 16px",
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          {registeringKey || state.action === "Deregister"
            ? <FaKey size={12} />
            : state.destructive ? <FaEraser size={12} /> : <FaLink size={12} />}
          {confirming ? "Confirm" : state.action}
        </DialogButton>
      </Field>
    </PanelSectionRow>
  );
};

/** The MQTT shared secret, shown so it can be given to a publisher.
 *
 * Enabling MQTT mints a secret, because the source refuses to start without one
 * — without that, switching the toggle on produced a trigger that silently
 * dropped every message. But a minted secret nobody can read is the same dead
 * end from the other direction: every publisher has to include it, and it
 * existed only in settings.json on the device.
 *
 * Shown in full rather than masked. It authenticates messages on a home LAN,
 * the panel is on the device you already unlocked, and a secret you cannot read
 * is one you cannot use — masking would restore the problem this row exists to
 * fix.
 */
const MqttSecretRow: FC<{ secret: string }> = ({ secret }) => (
  <PanelSectionRow>
    <Field
      label="Secret"
      description={
        <span style={{ fontFamily: "monospace", wordBreak: "break-all", fontSize: "0.9em" }}>
          {secret}
        </span>
      }
      focusable={false}
      highlightOnFocus={false}
      bottomSeparator="standard"
      childrenContainerWidth="min"
    >
      <DialogButton
        onClick={() => void copyToClipboard(secret)}
        style={{ minWidth: 0, width: "fit-content", padding: "8px 16px" }}
      >
        Copy
      </DialogButton>
    </Field>
  </PanelSectionRow>
);

export const TriggersPanel: FC<{
  statuses: SourceStatus[];
  media: Record<string, ActiveMedium>;
  target: { uri: string; label: string } | null;
  pairing: boolean;
  registeringKey: boolean;
}> = ({ statuses, media, target, pairing, registeringKey }) => {
  return (
    <PanelSection title="Triggers">
      {/* What the rows below are for, once. Putting it on every button instead
          would repeat a name long enough to wrap ("Vampire Survivors: Ode to
          Castlevania") on up to nine rows.
          While registering a key there is no game involved at all, so this says
          what the buttons will do instead — the list changing what it writes is
          not something to leave the user to infer from the button captions.
          The section keeps its own name through both: retitling the whole list
          "Choose the key" moved the one landmark the user navigates by, to say
          something this row already says. */}
      <PanelSectionRow>
        <Field
          icon={registeringKey ? <FaKey /> : <FaGamepad />}
          label={
            registeringKey
              ? "Register a key"
              : target ? target.label : "No game selected"
          }
          description={
            registeringKey
              ? "Choose a trigger below. Its medium then locks and unlocks restricted mode."
              : target ? "Game to be paired" : "Open a game to pair"
          }
          bottomSeparator="thick"
          focusable={false}
          highlightOnFocus={false}
        />
      </PanelSectionRow>

      {/* No Cancel here: the Restricted Mode section's button becomes one
          while registering, and that is where the user pressed Register. Two
          buttons for one action, one of them where the action did not start,
          is a choice to have to read. */}

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
        const medium = mediumFor(row, media);
        // Switching off the trigger the key sits on would lock the plugin with
        // no way to switch it back on — the backend refuses it outright, and
        // this stops the panel offering a switch that cannot move.
        const holdsKey = !!(medium?.key && medium.authorized);
        const rows = [
          <PanelSectionRow key={row.key}>
            <ToggleField
              label={row.label}
              checked={enabled}
              disabled={holdsKey}
              description={holdsKey ? "Deregister the key to disable this trigger" : undefined}
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
              medium={medium}
              connected={connected}
              // The backend's own answer to "can this be written to", rather
              // than the panel guessing from the row: a camera reads codes it
              // cannot write, and MQTT has no medium to write to at all.
              writable={!!status?.can_pair}
              target={target}
              registeringKey={registeringKey}
              sourceId={status?.source_id}
            />,
          );
          const secret = row.key === "mqtt"
            ? sharedState.settings?.sources?.mqtt?.secret
            : null;
          if (secret) {
            rows.push(<MqttSecretRow key={`${row.key}-secret`} secret={secret} />);
          }
        }
        return rows;
      })}
    </PanelSection>
  );
};
