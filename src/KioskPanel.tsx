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
  clearMasterKey,
  notifySubscribers,
  registerMasterKey,
  setFamilyViewPin,
  setKioskLocked,
  sharedState,
  toaster,
  type KioskState,
} from "./shared";
import { familyViewStatus, FAMILY_VIEW_SETUP_URL } from "./lib/familyView";

/** What Steam's half of kid mode is currently able to do.
 *
 * Worth a row of its own rather than a footnote: the plugin's lock and Family
 * View are independent, and a user who locks with Family View unconfigured
 * gets a device that still refuses tag writes but happily launches anything.
 * Saying which half is missing is the difference between a setting that looks
 * broken and one that has a next step.
 */
const FamilyViewRow: FC = () => {
  const status = familyViewStatus();

  if (!status.available) {
    return (
      <PanelSectionRow>
        <Field
          label="Family View"
          description="Not available on this Steam build."
          focusable={false}
          highlightOnFocus={false}
          bottomSeparator="standard"
        />
      </PanelSectionRow>
    );
  }

  if (status.enabled) {
    return (
      <PanelSectionRow>
        <Field
          label="Family View"
          description={
            status.locked
              ? "Set up and locked. Steam is enforcing the allowed games."
              : "Set up. Locking will restrict games and menus."
          }
          focusable={false}
          highlightOnFocus={false}
          bottomSeparator="standard"
        />
      </PanelSectionRow>
    );
  }

  return (
    <PanelSectionRow>
      <Field
        label="Family View"
        description="Not set up — kid mode will not restrict which games run."
        childrenContainerWidth="min"
        bottomSeparator="standard"
      >
        <DialogButton
          onClick={() => Navigation.NavigateToExternalWeb(FAMILY_VIEW_SETUP_URL)}
          style={{ minWidth: 0, width: "fit-content", padding: "8px 16px" }}
        >
          Set up
        </DialogButton>
      </Field>
    </PanelSectionRow>
  );
};

/** The Family View PIN, stored so a tap can unlock as well as lock.
 *
 * Optional on purpose. Locking never needs a secret; only unlocking does, and
 * keeping one here means Steam's PIN lives in the plugin's settings on the
 * device it protects. Left empty, the master key still locks, and unlocking
 * goes through Steam's own prompt.
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
        ? "The master key can now unlock Family View."
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
              ? "Stored. The master key unlocks Family View as well as locking it."
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

/** Kid mode, as configured while unlocked.
 *
 * Everything here writes through an RPC the backend refuses once locked, so
 * this panel disappearing is a courtesy rather than the enforcement.
 */
export const KioskPanel: FC<{ kiosk: KioskState }> = ({ kiosk }) => {
  const [registering, setRegistering] = useState(false);

  const register = async () => {
    // Untargeted: the user chooses which trigger by presenting a medium on it,
    // which is also how the game-page link button arms pairing.
    const ok = await registerMasterKey();
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
    if (await clearMasterKey()) {
      toaster.toast({ title: "Master key cleared", body: "Kid mode cannot be locked." });
    }
  };

  const lock = async () => {
    if (!(await setKioskLocked(true))) {
      toaster.toast({
        title: "Could not lock",
        body: "Register a master key first.",
        critical: true,
      });
    }
  };

  return (
    <PanelSection title="Kid Mode">
      <PanelSectionRow>
        <Field
          icon={<FaKey />}
          label={kiosk.has_master_key ? `Master key: ${kiosk.label}` : "No master key"}
          description={
            registering
              ? "Present the medium to use as the key…"
              : kiosk.has_master_key
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
            {kiosk.has_master_key ? "Replace" : "Register"}
          </DialogButton>
        </Field>
      </PanelSectionRow>

      {kiosk.has_master_key && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void forget()}>
            Forget Master Key
          </ButtonItem>
        </PanelSectionRow>
      )}

      <FamilyViewRow />
      <PinRow hasPin={kiosk.has_pin} />

      {/* A toggle rather than a button, because it reflects a state — but it
          only ever moves one way from here. Unlocking is the master key's job,
          or Steam's PIN prompt; a switch in the panel that undid the lock
          would mean the lock protected nothing. */}
      <PanelSectionRow>
        <ToggleField
          label="Lock now"
          description={
            kiosk.has_master_key
              ? "Hides pairing and settings until the master key is presented."
              : "Register a master key first."
          }
          checked={false}
          disabled={!kiosk.has_master_key}
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
export const LockedPanel: FC<{ kiosk: KioskState }> = ({ kiosk }) => (
  <PanelSection title="Kid Mode">
    <PanelSectionRow>
      <Field
        icon={<FaLock />}
        label="Locked"
        description={
          kiosk.label
            ? `Present the master key (${kiosk.label}) to unlock.`
            : "Present the master key to unlock."
        }
        focusable={false}
        highlightOnFocus={false}
        bottomSeparator="standard"
      />
    </PanelSectionRow>
  </PanelSection>
);
