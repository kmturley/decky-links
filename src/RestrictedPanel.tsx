import { FC } from "react";
import {
  DialogButton,
  Field,
  PanelSection,
  PanelSectionRow,
} from "@decky/ui";
import { FaKey, FaLock, FaLockOpen } from "react-icons/fa";
import {
  notifySubscribers,
  sharedState,
  type RestrictedState,
} from "./shared";
import { cancelKeyRegistration, deregisterKey } from "./lib/triggerRows";

/** Restricted mode, as configured while unlocked.
 *
 * Everything here writes through an RPC the backend refuses once locked, so
 * this panel disappearing is a courtesy rather than the enforcement.
 */
export const RestrictedPanel: FC<{ restricted: RestrictedState }> = ({ restricted }) => {
  // Read straight from the shared state rather than subscribing: the panel's
  // Content already re-renders this whole tree on notifySubscribers, so a
  // second subscription here would only mean two renders for one change.
  const registering = sharedState.registeringKey;

  // Hands over to the Triggers list rather than arming anything here.
  //
  // This used to call registerKey() with no trigger, which meant the key went
  // to whichever source the backend read first — with a tag on the reader and
  // a stick in a drive, the user had no way to say which. pairRow solved that
  // for games by making the row the target, and this is the same fix: the list
  // above enters a "choose the key" state, and the row you press is the one
  // that gets written.
  const register = () => {
    sharedState.registeringKey = true;
    notifySubscribers();
  };

  // One button, because there is only ever one move to make here.
  //
  // It used to be two — "Replace" beside the key row and a "Disable Key"
  // button below it — which offered a choice that is not really a choice:
  // replacing is deregistering and registering again, and the intermediate
  // state (a key registered, a second write in flight) is one neither the user
  // nor the backend has a use for. Register ⇄ Deregister is the whole switch.
  const action = registering
    ? { label: "Cancel", run: cancelKeyRegistration }
    : restricted.has_key
      ? { label: "Deregister", run: () => void deregisterKey() }
      : { label: "Register", run: register };

  return (
    <PanelSection title="Restricted Mode">
      {/* Whether the mode is on, and what it does — above the control that
          arms it. This used to sit *below* the key row, so the button asking
          to be pressed was explained by text you reached after pressing it.

          The padlock answers a question the key row below cannot: "Key" tells
          you a key exists, which is not the same as being told the mode is on.
          It says on/off, never locked/unlocked — this panel only renders while
          unlocked, so a padlock that flipped shut would never be seen doing
          it. LockedPanel is the locked face of the feature. */}
      <PanelSectionRow>
        <Field
          icon={restricted.has_key ? <FaLock /> : <FaLockOpen />}
          label={restricted.has_key ? "On" : "Off"}
          description={
            restricted.has_key
              // The one sentence that explains the whole feature. There is no
              // lock button, and this is why: the key *is* the switch.
              ? "Removing the key locks these controls, and allows only games " +
                "started from a tag, disk or code. Steam's own menus stay open."
              : "Register a key. Removing it locks these controls, and allows " +
                "only games started from a tag, disk or code. Steam's own menus stay open."
          }
          focusable={false}
          highlightOnFocus={false}
          bottomSeparator="standard"
        />
      </PanelSectionRow>

      <PanelSectionRow>
        <Field
          icon={<FaKey />}
          label={restricted.has_key ? "Key" : "No key"}
          description={
            registering
              ? "Choose a trigger in the list above"
              // Just the medium: "USB drive" under a row already labelled
              // "Key" is the whole fact, and "On the USB drive" spends a line
              // on grammar.
              : restricted.has_key && restricted.label
                ? restricted.label
                : undefined
          }
          childrenContainerWidth="min"
          bottomSeparator="standard"
        >
          <DialogButton
            onClick={action.run}
            style={{ minWidth: 0, width: "fit-content", padding: "8px 16px" }}
          >
            {action.label}
          </DialogButton>
        </Field>
      </PanelSectionRow>

    </PanelSection>
  );
};

/** What the panel is while locked.
 *
 * Deliberately a dead end: no controls, because every control here writes
 * something the backend now refuses, and offering one would produce a button
 * that fails rather than a mode that holds.
 */
export const LockedPanel: FC<{ restricted: RestrictedState }> = ({ restricted }) => (
  <PanelSection title="Restricted Mode">
    <PanelSectionRow>
      <Field
        icon={<FaLock />}
        label="Locked"
        description={
          restricted.label
            ? `Present the key (${restricted.label}) to unlock.`
            : "Present the key to unlock."
        }
        focusable={false}
        highlightOnFocus={false}
        bottomSeparator="standard"
      />
    </PanelSectionRow>
  </PanelSection>
);
