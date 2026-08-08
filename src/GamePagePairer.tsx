import React, { FC, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { FaLink } from "react-icons/fa";
import {
  cancelPairing,
  addEventListener,
  removeEventListener,
  setPairingToastSuppressed,
  sharedState,
  notifySubscribers,
} from "./shared";
import { useViewedApp } from "./hooks/useAppId";
import { mediumNoun } from "./lib/sourceIcons";
import PairModal, { type PairTarget } from "./PairModal";

const RETRY_DELAY_MS = 500;
const MODAL_WAITING_TEXT = "Pair this game to a trigger, or print a code.";

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
  const viewedApp = useViewedApp();
  const target: PairTarget | null = viewedApp?.launchTarget
    ? {
        uri: viewedApp.launchTarget,
        label: viewedApp.name || `App ${viewedApp.appId}`,
        appid: viewedApp.appId,
      }
    : null;

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
      sharedState.pairing = false;
      notifySubscribers();
      setPairingToastSuppressed(false);
      setModalVisible(false);
    }
  }, []);

  useEffect(() => {
    return () => {
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

  // Nothing is armed when the modal opens. Every trigger gets its own Pair
  // button, so arming all of them up front would write to whichever medium the
  // backend happened to see first — the exact ambiguity per-trigger pairing
  // exists to remove. Suppress the global toast while the modal is showing the
  // result itself.
  useEffect(() => {
    if (!modalVisible) return;
    setPairingToastSuppressed(true);
    return () => setPairingToastSuppressed(false);
  }, [modalVisible]);

  // A medium was presented — say so immediately. Mounting a floppy takes
  // several seconds, during which an unchanged "insert a disk" prompt looks
  // like the insert was simply not noticed.
  useEffect(() => {
    if (!modalVisible) return;
    const listener = addEventListener<[data: { uid: string; source_type?: string }]>(
      "media_detected",
      (data) => {
        if (!data?.uid) return;
        const noun = mediumNoun(data.source_type ?? "nfc");
        const label = data.uid.replace(/^\/dev\//, "");
        setStatusMessage(`Writing to ${noun} ${label}…`);
      }
    );
    return () => removeEventListener("media_detected", listener);
  }, [modalVisible]);

  // Report the result, but leave the modal open.
  //
  // It used to close itself after a successful pair, which made sense when it
  // did one thing. Now it is a hub — pair a tag, then a disk, then save a
  // printable card — and closing after the first of those throws away the rest.
  // sharedState.pairing is cleared by the background manager, which is what
  // releases the armed row in the list.
  useEffect(() => {
    const listener = addEventListener<[data: { success: boolean; uid: string; error?: string; source_type?: string }]>(
      "pairing_result",
      (data) => {
        if (!modalVisible) return;
        const noun = mediumNoun(data.source_type ?? "nfc");
        // A storage media_id is a device node; "/dev/" is noise in a message.
        const label = data.uid?.replace(/^\/dev\//, "") ?? "";
        setStatusMessage(
          data.success
            ? `Paired to ${noun} ${label}`
            : `Pairing failed: ${data.error || "unknown"}`
        );
      }
    );
    return () => removeEventListener("pairing_result", listener);
  }, [modalVisible]);

  const onClickButton = () => {
    setStatusMessage(MODAL_WAITING_TEXT);
    setModalVisible(true);
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

  const modal = modalVisible && !sharedState.restricted?.locked ? (
    <PairModal target={target} statusMessage={statusMessage} onClose={closeModal} />
  ) : null;

  // Locked: the link icon leads to a modal whose every button arms a pairing
  // the backend refuses. Read from sharedState directly rather than through a
  // subscription — the panel re-renders this tree on a lock change, and the
  // icon lives on a game page that is rebuilt on navigation anyway.
  const locked = !!sharedState.restricted?.locked;

  let iconNode: React.ReactNode = null;
  if (locked) {
    iconNode = null;
  } else if (show && embedded) {
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
