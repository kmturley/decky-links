import { FC, useEffect, useState } from "react";
import { DialogButton, Spinner } from "@decky/ui";
import { FaLink, FaTimes } from "react-icons/fa";
import {
  getQrPreview,
  saveGameCard,
  useSharedState,
  type SourceStatus,
  type ActiveMedium,
} from "./shared";
import {
  TRIGGER_ROWS,
  holdsTarget,
  isRowConnected,
  isRowEnabled,
  mediaStateFor,
  mediumFor,
  pairRow,
  statusFor,
  type TriggerRow,
} from "./lib/triggerRows";

/** On-screen module size. Small enough to fit the modal, still an integer
 *  number of pixels per module — the same no-resampling rule as print, because
 *  a soft QR photographs badly and photographing this is the point. */
const PREVIEW_MODULE_PX = 5;

export interface PairTarget {
  uri: string;
  label: string;
  appid: string;
}

/** Which trigger, if any, is currently armed and waiting for a medium. */
type ArmedRow = string | null;

const TriggerLine: FC<{
  row: TriggerRow;
  statuses: SourceStatus[];
  media: Record<string, ActiveMedium>;
  target: PairTarget;
  armed: boolean;
  onPair: () => void;
}> = ({ row, statuses, media, target, armed, onPair }) => {
  const status = statusFor(row, statuses);
  const connected = isRowConnected(row, status);
  const medium = mediumFor(row, media);
  const state = mediaStateFor(row, connected, medium, target, armed);

  // Unlike the Quick Access panel, a connected row is normally pairable here
  // even with nothing on it: the modal is where you tap a tag you have not
  // presented yet, so waiting for a medium is the flow rather than an error.
  // Three exceptions, and all three are cases where the press could only be a
  // mistake:
  //
  // Generated triggers — there is nothing to write to a camera.
  //
  // The key: writing a game over it would destroy the only thing that can
  // unlock the device, so the backend refuses it, and offering the button made
  // a press that could only ever fail.
  //
  // And a medium that already carries this game. The row says so — it shows
  // the game's name — and the panel drops its button for the same reason.
  const holdsKey = !!(medium?.key && medium.authorized !== false);
  const canPair = connected && !armed && !state.busy && !row.generated
    && !holdsKey && !holdsTarget(medium, target);

  return (
    <div style={{
      display: "flex",
      alignItems: "center",
      gap: 10,
      padding: "8px 4px",
      borderBottom: "1px solid rgba(255,255,255,0.08)",
      opacity: connected ? 1 : 0.45,
    }}>
      <span style={{ fontSize: "1.2em", width: "1.4em", textAlign: "center" }}>
        {state.busy ? <Spinner style={{ width: "1em", height: "1em" }} /> : state.icon ?? row.icon}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: "0.9em" }}>{row.label}</div>
        <div style={{
          fontSize: "0.75em",
          opacity: 0.7,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}>
          {state.text}
        </div>
      </div>
      {canPair && (
        <DialogButton
          onClick={onPair}
          style={{
            minWidth: 0,
            width: "fit-content",
            padding: "6px 14px",
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <FaLink size={11} />
          Pair
        </DialogButton>
      )}
    </div>
  );
};

const CardPanel: FC<{ target: PairTarget }> = ({ target }) => {
  const [preview, setPreview] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setPreview(null);
    setError(null);
    setSaved(null);
    (async () => {
      const result = await getQrPreview(target.uri, PREVIEW_MODULE_PX);
      if (cancelled) return;
      if (result?.ok && result.data_uri) setPreview(result.data_uri);
      else setError(result?.error || "Could not generate a code");
    })();
    return () => { cancelled = true; };
  }, [target.uri]);

  const onSave = async () => {
    setSaving(true);
    try {
      const result = await saveGameCard(target.uri, target.label, target.appid);
      setSaved(result?.ok ? (result.dir ?? "saved") : null);
      if (!result?.ok) setError(result?.error || "Could not save the card");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10 }}>
      <div style={{
        width: 200,
        height: 200,
        background: "#fff",
        borderRadius: 6,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}>
        {preview
          ? <img src={preview} alt="" style={{ width: "100%", imageRendering: "pixelated" }} />
          : error
            ? <span style={{ color: "#900", fontSize: "0.75em", padding: 12, textAlign: "center" }}>{error}</span>
            : <Spinner />}
      </div>

      {/* Deliberately not "scan this". A phone that scans it gets a steam://
          URI it cannot open, which reads as a broken feature. The code is
          something to show to the camera, not something to open. */}
      <div style={{ fontSize: "0.75em", opacity: 0.75, textAlign: "center", maxWidth: 220 }}>
        Present this QR code either printed or on a screen to a connected camera.
      </div>

      <DialogButton
        onClick={() => void onSave()}
        disabled={saving || !preview}
        style={{ minWidth: 0, width: "fit-content", padding: "8px 16px" }}
      >
        {saving ? "Saving…" : "Save printable card"}
      </DialogButton>

      {saved && (
        <div style={{ fontSize: "0.7em", opacity: 0.7, textAlign: "center", wordBreak: "break-all" }}>
          Saved to {saved}
        </div>
      )}
    </div>
  );
};

export const PairModal: FC<{
  target: PairTarget | null;
  statusMessage: string;
  onClose: () => void;
}> = ({ target, statusMessage, onClose }) => {
  const state = useSharedState();
  const [armed, setArmed] = useState<ArmedRow>(null);

  // Any pairing result — success or failure — releases the armed row so the
  // list stops claiming it is still waiting.
  useEffect(() => {
    if (!state.pairing) setArmed(null);
  }, [state.pairing]);

  if (!target) return null;

  // Only rows the user has switched on. A disabled trigger is not a thing they
  // can present, and listing all nine here would bury the two that work.
  //
  // Camera is included even though it cannot be paired: it is the trigger that
  // reads the code on the right, and seeing that it is enabled and connected is
  // exactly what someone would otherwise open the sidebar to check.
  const rows = TRIGGER_ROWS.filter((row) =>
    isRowEnabled(row, statusFor(row, state.sourceStatuses)),
  );

  return (
    <div
      style={{
        position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.75)",
        display: "flex", justifyContent: "center", alignItems: "center", zIndex: 10000,
      }}
      onClick={onClose}
    >
      <div
        style={{
          backgroundColor: "#1a1d23", padding: 20, borderRadius: 8,
          width: "90%", maxWidth: 620, position: "relative",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <FaTimes
          style={{ position: "absolute", top: 10, right: 10, cursor: "pointer" }}
          onClick={onClose}
        />

        <div style={{ marginBottom: 14, paddingRight: 24 }}>
          <div style={{ fontSize: "1.1em", fontWeight: "bold" }}>{target.label}</div>
          <div style={{ fontSize: "0.75em", opacity: 0.7 }}>{statusMessage}</div>
        </div>

        <div style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
          {/* Left: physical media, which has to be written to. First because
              it is the list that changes as you plug things in — the code on
              the right is the same every time you open this game. */}
          <div style={{ flex: 1, minWidth: 0 }}>
            {rows.length === 0 ? (
              <div style={{ fontSize: "0.8em", opacity: 0.7, padding: "8px 4px" }}>
                No triggers are switched on. Enable one in the Decky Links panel,
                or use the QR code.
              </div>
            ) : (
              rows.map((row) => (
                <TriggerLine
                  key={row.key}
                  row={row}
                  statuses={state.sourceStatuses}
                  media={state.activeMedia}
                  target={target}
                  armed={armed === row.key}
                  onPair={() => {
                    setArmed(row.key);
                    const status = statusFor(row, state.sourceStatuses);
                    void pairRow(row, target, status?.source_id).then((ok) => {
                      if (!ok) setArmed(null);
                    });
                  }}
                />
              ))
            )}
          </div>

          {/* Right: generated media. Nothing is written, so there is no Pair
              button — the code exists the moment the game does. */}
          <CardPanel target={target} />
        </div>
      </div>
    </div>
  );
};

export default PairModal;
