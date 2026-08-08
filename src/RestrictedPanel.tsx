import { FC, useState } from "react";
import {
  ButtonItem,
  DialogButton,
  Field,
  Navigation,
  PanelSection,
  PanelSectionRow,
  TextField,
  ToggleField,
} from "@decky/ui";
import { FaKey, FaLock } from "react-icons/fa";
import {
  disableKey,
  notifySubscribers,
  registerKey,
  setFamilyViewPin,
  setKioskLocked,
  sharedState,
  toaster,
  type RestrictedState,
} from "./shared";
import { familyViewStatus, FAMILY_VIEW_SETUP_URL } from "./lib/familyView";

/** What restricted mode restricts, stated plainly.
 *
 * This row used to sell Family View as the thing that decided which games may
 * run. It is not, for most accounts: Family View is Steam's older per-account
 * PIN mode, and the client only offers to set it up on accounts that already
 * had it — a modern account gets Steam Families instead, whose controls apply
 * to *child* accounts and so cannot restrict the one holding the library.
 *
 * So the rule is the plugin's own, and it is worth saying out loud, because a
 * restricted mode that silently closes a game the child launched is alarming unless
 * you were told that is what it does.
 */
const ScopeRow: FC = () => {
  const status = familyViewStatus();

  return (
    <>
      <PanelSectionRow>
        <Field
          label="While locked"
          description={
            "Only games started by presenting a tag, disk or code will run. " +
            "Anything launched from the library is closed. Steam's own menus " +
            "stay reachable."
          }
          focusable={false}
          highlightOnFocus={false}
          bottomSeparator="standard"
        />
      </PanelSectionRow>

      {/* Shown only when the account actually has Family View. Offering a
          "Set up" button for something Steam will not let most accounts turn
          on is worse than not mentioning it. */}
      {status.available && status.enabled && (
        <PanelSectionRow>
          <Field
            label="Family View"
            description={
              status.locked
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
      )}
    </>
  );
};

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
  const [registering, setRegistering] = useState(false);

  const register = async () => {
    // Untargeted: the user chooses which trigger by presenting a medium on it,
    // which is also how the game-page link button arms pairing.
    const ok = await registerKey();
    if (!ok) {
      toaster.toast({
        title: "Could not start",
        body: "No trigger is able to write a key.",
        critical: true,
      });
      return;
    }
    setRegistering(true);
    sharedState.pairing = true;
    notifySubscribers();
  };

  const forget = async () => {
    if (await disableKey()) {
      toaster.toast({ title: "Key cleared", body: "Restricted mode cannot be locked." });
    }
  };

  const lock = async () => {
    if (!(await setKioskLocked(true))) {
      toaster.toast({
        title: "Could not lock",
        body: "Register a key first.",
        critical: true,
      });
    }
  };

  return (
    <PanelSection title="Restricted Mode">
      <PanelSectionRow>
        <Field
          icon={<FaKey />}
          label={restricted.has_key ? `Key: ${restricted.label}` : "No key"}
          description={
            registering
              ? "Present the medium to use as the key…"
              : restricted.has_key
                ? "Present it to lock or unlock."
                : "Register a medium to lock the plugin with."
          }
          childrenContainerWidth="min"
          bottomSeparator="standard"
        >
          <DialogButton
            onClick={() => void register()}
            style={{ minWidth: 0, width: "fit-content", padding: "8px 16px" }}
          >
            {restricted.has_key ? "Replace" : "Register"}
          </DialogButton>
        </Field>
      </PanelSectionRow>

      {restricted.has_key && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void forget()}>
            Forget Key
          </ButtonItem>
        </PanelSectionRow>
      )}

      <ScopeRow />
      {familyViewStatus().enabled && <PinRow hasPin={restricted.has_pin} />}

      {/* A toggle rather than a button, because it reflects a state — but it
          only ever moves one way from here. Unlocking is the key's job,
          or Steam's PIN prompt; a switch in the panel that undid the lock
          would mean the lock protected nothing. */}
      <PanelSectionRow>
        <ToggleField
          label="Lock now"
          description={
            restricted.has_key
              ? "Hides pairing and settings until the key is presented."
              : "Register a key first."
          }
          checked={false}
          disabled={!restricted.has_key}
          onChange={(v: boolean) => { if (v) void lock(); }}
        />
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
