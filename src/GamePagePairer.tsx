import React, { FC, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { FaLink, FaTimes } from "react-icons/fa";
import {
  getReaderStatus,
  startPairing,
  cancelPairing,
  addEventListener,
  removeEventListener,
  setPairingToastSuppressed,
} from "./shared";
import useAppId from "./hooks/useAppId";

const RETRY_DELAY_MS = 500;
const MODAL_CLOSE_DELAY_MS = 1500;
const MODAL_WAITING_TEXT = "Waiting for tag…";

// Helper lifted from protondb plugin; used to watch for fullscreen mode so we
// can hide our button when the header disappears.
// Attempt to locate the `<div class="TopCapsule">` element inside the
// game header.  The structure varies a little between different Decky
// releases, so we try a couple of strategies.  If nothing is present yet we
// just return null and the caller will retry later (no console error needed).
function findTopCapsuleParent(ref: HTMLDivElement | null): Element | null {
  // 1. Preferred strategy: walk up from our container element
  let headerContainer: Element | null = null;
  const children = ref?.parentElement?.children;
  if (children) {
    for (const child of children) {
      if (child.className.includes("appDetailsHeader")) {
        headerContainer = child;
        break;
      }
    }
  }

  // 2. Fallback: global query once the header exists somewhere in the tree
  if (!headerContainer) {
    headerContainer = document.querySelector(".appDetailsHeader");
  }

  if (!headerContainer) {
    return null;
  }

  const topCapsule = headerContainer.querySelector(".TopCapsule");
  return topCapsule;
}

function isFullscreenTransition(className: string): boolean {
  return (
    className.includes("FullscreenEnterStart") ||
    className.includes("FullscreenEnterActive") ||
    className.includes("FullscreenEnterDone") ||
    className.includes("FullscreenExitStart") ||
    className.includes("FullscreenExitActive")
  );
}

function clearTimer(timerRef: { current: number | null }) {
  if (timerRef.current !== null) {
    clearTimeout(timerRef.current);
    timerRef.current = null;
  }
}

interface GamePagePairerProps {
  /** If true the component is rendered directly by a route patch and therefore
   * does not need to find an anchor in the DOM.  The icon will be positioned by
   * the patch itself. */
  embedded?: boolean;
}

const GamePagePairer: FC<GamePagePairerProps> = ({ embedded = false }) => {
  const [show, setShow] = useState<boolean>(true);
  const [modalVisible, setModalVisible] = useState<boolean>(false);
  const [statusMessage, setStatusMessage] = useState<string>(MODAL_WAITING_TEXT);
  const ref = useRef<HTMLDivElement | null>(null);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const topCapsuleRetryRef = useRef<number | null>(null);
  const anchorRetryRef = useRef<number | null>(null);
  const modalCloseTimerRef = useRef<number | null>(null);
  const launchTarget = useAppId();

  // Watch for the page header being hidden in fullscreen.
  useEffect(() => {
    let observer: MutationObserver | null = null;
    let cancelled = false;

    const attachObserver = () => {
      if (cancelled) return;

      const topCapsule = findTopCapsuleParent(ref.current);
      if (!topCapsule) {
        // header not in DOM yet; try again shortly
        topCapsuleRetryRef.current = window.setTimeout(attachObserver, RETRY_DELAY_MS);
        return;
      }

      observer = new MutationObserver((entries) => {
        for (const entry of entries) {
          if (entry.type !== "attributes" || entry.attributeName !== "class") {
            continue;
          }

          const className = (entry.target as Element).className;
          const fullscreenMode = isFullscreenTransition(className);
          const fullscreenAborted = className.includes("FullscreenExitDone");

          setShow(!fullscreenMode || fullscreenAborted);
        }
      });
      observer.observe(topCapsule, { attributes: true, attributeFilter: ["class"] });
    };

    attachObserver();
    return () => {
      cancelled = true;
      observer?.disconnect();
      clearTimer(topCapsuleRetryRef);
    };
  }, []);

  // open/close helpers
  const closeModal = useCallback(async () => {
    try {
      await cancelPairing();
    } finally {
      setPairingToastSuppressed(false);
      setModalVisible(false);
    }
  }, []);

  useEffect(() => {
    return () => {
      clearTimer(modalCloseTimerRef);
      clearTimer(anchorRetryRef);
      clearTimer(topCapsuleRetryRef);
      void cancelPairing();
      setPairingToastSuppressed(false);
    };
  }, []);

  // when embedded we rely on the parent patch to position us and therefore
  // we don't need to search the DOM; otherwise fall back to the old controller
  // icon lookup.
  useEffect(() => {
    if (embedded) {
      // anchor will be provided by the patch container, so there's nothing to
      // do here. however we still keep the ref so that the fullscreen observer
      // can work later on.
      return;
    }

    let cancelled = false;

    const tryFind = () => {
      const btn = document.querySelector('[aria-label="Configure Controller"]');
      if (btn && btn.parentElement) {
        if (!cancelled) setAnchor(btn.parentElement as HTMLElement);
      } else {
        anchorRetryRef.current = window.setTimeout(() => {
          if (!cancelled) tryFind();
        }, RETRY_DELAY_MS);
      }
    };
    tryFind();
    return () => {
      cancelled = true;
      clearTimer(anchorRetryRef);
    };
  }, [embedded]);

  // start pairing when the dialog opens
  useEffect(() => {
    if (!modalVisible) return;

    (async () => {
      const reader = await getReaderStatus();
      if (!reader.connected) {
        setStatusMessage("Reader not detected. Configure in Decky Links settings");
        return;
      }

      if (!launchTarget) {
        setStatusMessage("Unable to determine launch target.");
        return;
      }

      setPairingToastSuppressed(true);
      const ok = await startPairing(launchTarget);
      if (!ok) {
        setPairingToastSuppressed(false);
        setStatusMessage("Failed to initiate pairing.");
      } else {
        setStatusMessage("Pairing mode active – tap a tag");
      }
    })();
  }, [modalVisible, launchTarget]);

  // listen for pairing results so we can show status and close the modal
  useEffect(() => {
    const listener = addEventListener<[data: { success: boolean; uid: string; error?: string }]>(
      "pairing_result",
      (data) => {
        if (!modalVisible) return;
        setStatusMessage(data.success ? `Paired to tag ${data.uid}` : `Pairing failed: ${data.error || "unknown"}`);
        clearTimer(modalCloseTimerRef);
        modalCloseTimerRef.current = window.setTimeout(() => {
          void closeModal();
        }, MODAL_CLOSE_DELAY_MS);
      }
    );
    return () => {
      removeEventListener("pairing_result", listener);
      clearTimer(modalCloseTimerRef);
    };
  }, [modalVisible, closeModal]);

  const onClickButton = () => {
    setStatusMessage(MODAL_WAITING_TEXT);
    setModalVisible(true);
  };

  // modal styling
  const modalStyle: React.CSSProperties = {
    position: "fixed",
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: "rgba(0,0,0,0.7)",
    display: "flex",
    justifyContent: "center",
    alignItems: "center",
    zIndex: 10000,
  };

  const boxStyle: React.CSSProperties = {
    backgroundColor: "#222",
    padding: 20,
    borderRadius: 8,
    width: "80%",
    maxWidth: 400,
    textAlign: "center",
    position: "relative",
  };

  const icon = (
    <div
      className="decky-links-pair-icon"
      style={
        embedded
          ? {
              position: "absolute",
              top: 8,
              right: 8,
              display: "flex",
              alignItems: "center",
              cursor: "pointer",
              zIndex: 10000,
            }
          : {
              display: "flex",
              alignItems: "center",
              cursor: "pointer",
              marginLeft: "8px",
            }
      }
      onClick={onClickButton}
      ref={embedded ? (ref as any) : undefined}
    >
      <FaLink size={24} color="#fff" />
    </div>
  );

  const modal = modalVisible ? (
    <div style={modalStyle} onClick={closeModal}>
      <div style={boxStyle} onClick={(e) => e.stopPropagation()}>
        <FaTimes
          style={{ position: "absolute", top: 8, right: 8, cursor: "pointer" }}
          onClick={closeModal}
        />
        <p style={{ margin: 0 }}>{statusMessage}</p>
      </div>
    </div>
  ) : null;

  let iconNode: React.ReactNode = null;
  if (show && embedded) {
    iconNode = icon;
  } else if (show && anchor) {
    iconNode = createPortal(icon, anchor);
  }

  return (
    <>
      {iconNode}
      {modal}
    </>
  );
};

export default GamePagePairer;
