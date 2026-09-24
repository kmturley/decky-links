import { FC, useEffect, useMemo, useState } from "react";
import { DialogButton, Spinner } from "@decky/ui";
import { FaExternalLinkAlt, FaLink, FaTimes } from "react-icons/fa";
import {
  getQrPreview,
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

/** CoverForge (https://github.com/kmturley/cover-forge) generates printable
 *  covers and cards for a game across every physical format this plugin (and
 *  a few it doesn't) supports — dvd, bluray, vhs, cd, cassette, floppy, and
 *  three NFC layouts. Rendering that on the Deck would mean carrying its whole
 *  template set and a full layout engine in Python for a feature used rarely
 *  and never urgently; CoverForge already exists, is kept current
 *  independently of this plugin's release cycle, and runs wherever the QR
 *  code ends up — a phone is a better device for trimming and printing a card
 *  than a Deck is anyway.
 *
 *  `app` and `template` are read once on load and stripped from the address
 *  bar, so opening this twice for two different games never leaves stale
 *  params behind for the second to inherit. */
const COVERFORGE_URL = "https://kmturley.github.io/cover-forge";
const COVERFORGE_DEFAULT_TEMPLATE = "nfc-card";

function coverForgeUrl(appid: string): string {
  const params = new URLSearchParams({
    template: COVERFORGE_DEFAULT_TEMPLATE,
    app: appid,
  });
  return `${COVERFORGE_URL}?${params.toString()}`;
}

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
  const state = mediaStateFor(row, connected, medium, target, armed, status?.error);

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

  // Pure string construction, not a source of truth from the backend — so
  // unlike the reader/writer flows above, there is nothing here that can
  // fail independently of the QR render itself.
  const url = useMemo(() => coverForgeUrl(target.appid), [target.appid]);

  useEffect(() => {
    let cancelled = false;
    setPreview(null);
    setError(null);
    (async () => {
      // Unlike the steam:// URI this used to encode, this QR is meant to be
      // scanned and opened — a normal https:// link a phone's camera already
      // knows what to do with.
      const result = await getQrPreview(url, PREVIEW_MODULE_PX);
      if (cancelled) return;
      if (result?.ok && result.data_uri) setPreview(result.data_uri);
      else setError(result?.error || "Could not generate a code");
    })();
    return () => { cancelled = true; };
  }, [url]);

  // Only whether the call exists is captured here — never the function
  // itself. SteamClient's methods are proxy stubs bound to their parent
  // namespace object; detaching one into a standalone reference (as
  // `const f = window.SteamClient.System.OpenInSystemBrowser`) loses that
  // binding, and the stub then throws "Unknown method" when called — a real
  // bug caught live on a Deck, not a hypothetical. Below, the call stays
  // attached to `SteamClient.System` at the point it is made.
  const canOpenInSystemBrowser = !!window.SteamClient?.System?.OpenInSystemBrowser;

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

      <div style={{ fontSize: "0.75em", opacity: 0.75, textAlign: "center", maxWidth: 220 }}>
        Scan the QR code with your phone camera to design and print cover art
        using CoverForge.
      </div>

      {canOpenInSystemBrowser && (
        <DialogButton
          onClick={() => window.SteamClient.System.OpenInSystemBrowser(url)}
          style={{
            minWidth: 0,
            width: "fit-content",
            padding: "8px 16px",
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          <FaExternalLinkAlt size={11} />
          Open in CoverForge
        </DialogButton>
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
