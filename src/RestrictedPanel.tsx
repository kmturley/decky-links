import { FC, useState } from "react";
import {
  ButtonItem,
  DialogButton,
  Field,
  Navigation,
  PanelSection,
  PanelSectionRow,
  TextField,
} from "@decky/ui";
import { FaKey, FaLock, FaLockOpen } from "react-icons/fa";
import {
  disableKey,
  notifySubscribers,
  setFamilyViewPin,
  sharedState,
  toaster,
  type RestrictedState,
} from "./shared";
import { cancelKeyRegistration } from "./lib/triggerRows";
import { familyViewStatus, FAMILY_VIEW_SETUP_URL } from "./lib/familyView";

/** Steam's half of the lock, where the account has it.
 *
 * Shown only when the account actually has Family View. It used to head this
 * section as the thing that decided which games may run — it is not, for most
 * accounts: Family View is Steam's older per-account PIN mode, and the client
 * only offers to set it up on accounts that already had it. A modern account
 * gets Steam Families instead, whose controls apply to *child* accounts and so
 * cannot restrict the one holding the library. Offering a "Set up" button for
 * something Steam will not let most accounts turn on is worse than not
 * mentioning it, so an account without it never sees this row.
 */
const FamilyViewRow: FC<{ locked: boolean }> = ({ locked }) => (
  <PanelSectionRow>
    <Field
      label="Family View"
      description={
        locked
          ? "Locked too — Steam is also restricting its own menus and store."
          : "Set up on this account. Restricted mode will lock it as well."
      }
      childrenContainerWidth="min"
      bottomSeparator="standard"
    >
      <DialogButton
        onClick={() => Navigation.NavigateToExternalWeb(FAMILY_VIEW_SETUP_URL)}
        style={{ minWidth: 0, width: "fit-content", padding: "8px 16px" }}
      >
        Manage
      </DialogButton>
    </Field>
  </PanelSectionRow>
);

/** The Family View PIN, stored so a tap can unlock Steam's half as well.
 *
 * Only rendered for accounts that actually have Family View, since it is the
 * only thing this PIN is for. Optional even then: locking never needs a
 * secret, only unlocking does, and keeping one here means Steam's PIN lives in
 * the plugin's settings on the device it protects. Left empty, the key
 * still locks, and unlocking goes through Steam's own prompt.
 */
const PinRow: FC<{ hasPin: boolean }> = ({ hasPin }) => {
  const [pin, setPin] = useState("");

  const save = async (value: string) => {
    const ok = await setFamilyViewPin(value);
    if (!ok) {
      toaster.toast({
        title: "PIN not saved",
        body: "Family View PINs are 4-8 digits.",
        critical: true,
      });
      return;
    }
    setPin("");
    toaster.toast({
      title: value ? "PIN saved" : "PIN cleared",
      body: value
        ? "The key can now unlock Family View."
        : "Unlock with Steam's own prompt.",
    });
  };

  return (
    <>
      <PanelSectionRow>
        <TextField
          label="Family View PIN"
          description={
            hasPin
              ? "Stored. The key unlocks Family View as well as locking it."
              : "Optional. Without it, the key locks only and Steam asks for the PIN."
          }
          value={pin}
          bIsPassword={true}
          onChange={(e: any) => setPin(e.target.value)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => void save(pin)} disabled={!pin}>
          Save PIN
        </ButtonItem>
      </PanelSectionRow>
      {hasPin && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void save("")}>
            Forget PIN
          </ButtonItem>
        </PanelSectionRow>
      )}
    </>
  );
};

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

  const deregister = async () => {
    if (await disableKey()) {
      toaster.toast({
        title: "Key deregistered",
        body: "Restricted mode is off and the medium has been wiped.",
      });
      return;
    }
    toaster.toast({
      title: "Could not deregister",
      body: "The key must be present, and writable, to be wiped.",
      critical: true,
    });
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
      ? { label: "Deregister", run: () => void deregister() }
      : { label: "Register", run: register };

  const familyView = familyViewStatus();
  const showFamilyView = familyView.available && familyView.enabled;

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

      {showFamilyView && <FamilyViewRow locked={familyView.locked} />}
      {showFamilyView && <PinRow hasPin={restricted.has_pin} />}
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
