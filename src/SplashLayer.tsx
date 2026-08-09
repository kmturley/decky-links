import { FC, useEffect, useRef, useState } from "react";
import { scenes, Scene, MIN_VISIBLE_MS, type SceneChange } from "./lib/presentation";
import { sharedState, subscribeToState } from "./shared";
import { themeFor, type SceneVisual } from "./lib/themes";

/** The full-screen layer a theme paints on (issue #8).
 *
 * Mounted once via routerHook.addGlobalComponent and rendering null nearly all
 * the time. Verified on hardware, because the whole design depends on it: a
 * fixed, inset-0 layer paints above Home, above the library, and above Steam's
 * own "Starting launch…" card — and vanishes by itself the moment gamescope
 * hands the screen to the game.
 *
 * That last part is why there is no dismissal timer for the normal case. The
 * splash is covered by exactly as long as the loading takes, whether that is
 * two seconds or twenty, and nothing has to guess. LAUNCH_ABANDONED_MS below
 * covers the other case — a launch that never happens — because that is the
 * one where a stuck full-screen layer would be unrecoverable.
 */

/** How long to keep a launch splash up when no game ever arrives.
 *
 * A launch that fails leaves the scene at LAUNCHING forever: Steam never
 * paints, so nothing composites us away, and the user is left looking at an
 * animation over a Deck that will not respond to a button they cannot see.
 * Generous, because a cold shader cache on a big game is genuinely slow, but
 * finite, because "finite" is the whole point.
 */
const LAUNCH_ABANDONED_MS = 45_000;

/** Never rendered, whatever a theme says.
 *
 * IN_GAME has no layer to render into — gamescope owns the screen, so a visual
 * here would be a promise the runtime cannot keep. READY is the normal state
 * of a Deck sitting on Home; painting over it would make the plugin something
 * you have to dismiss to use your own device.
 */
const NEVER_VISIBLE = new Set<Scene>([Scene.IN_GAME, Scene.READY]);

export const SplashLayer: FC = () => {
  const [visual, setVisual] = useState<SceneVisual | null>(null);
  const shownAt = useRef(0);
  const timers = useRef<number[]>([]);

  const clearTimers = () => {
    timers.current.forEach((t) => window.clearTimeout(t));
    timers.current = [];
  };

  useEffect(() => {
    const show = (change: SceneChange) => {
      clearTimers();

      if (!sharedState.settings?.splash) {
        console.debug("[ Decky Links ] Splash off; not painting", change.to);
        return setVisual(null);
      }
      if (NEVER_VISIBLE.has(change.to)) return setVisual(null);

      const next = themeFor(sharedState.settings?.theme).scenes[change.to];
      if (!next) {
        console.debug("[ Decky Links ] Theme has no visual for", change.to);
        return setVisual(null);
      }

      // A tag reads in well under 200ms, so a READING visual would appear and
      // be replaced inside a couple of frames. A flicker reads as a fault, so
      // short-lived scenes wait to see whether they are still current before
      // painting anything at all. Sounds do not do this — they fire on the
      // edge, which is why edges and levels are modelled separately.
      const delay = next.immediate ? 0 : MIN_VISIBLE_MS;
      timers.current.push(window.setTimeout(() => {
        if (scenes.scene !== change.to) return;
        shownAt.current = Date.now();
        setVisual(next);
      }, delay));

      if (change.to === Scene.LAUNCHING) {
        timers.current.push(window.setTimeout(() => {
          console.warn("[ Decky Links ] Launch splash abandoned — no game appeared");
          setVisual(null);
        }, LAUNCH_ABANDONED_MS));
      }
    };

    const unsubscribeScene = scenes.subscribe(show);
    // The setting can be switched off while a splash is up, and the panel that
    // switches it is behind the splash. Honour it immediately.
    const unsubscribeState = subscribeToState(() => {
      if (!sharedState.settings?.splash) {
        clearTimers();
        setVisual(null);
      }
    });

    return () => {
      clearTimers();
      unsubscribeScene();
      unsubscribeState();
    };
  }, []);

  if (!visual) return null;

  return (
    <div
      data-decky-links-splash={visual.scene}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 7000,
        // Never swallows input. The splash is decoration over a device someone
        // is holding; if it ever failed to clear, an unresponsive Deck would
        // be a far worse bug than a visible one.
        pointerEvents: "none",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: visual.background,
        animation: `dl-splash-in ${visual.fadeMs ?? 180}ms ease-out`,
      }}
    >
      {/* Scoped to the layer rather than injected into the tab: it lives and
          dies with the splash, so nothing of ours is left in Steam's stylesheet
          when the plugin unmounts. */}
      <style>{"@keyframes dl-splash-in { from { opacity: 0 } to { opacity: 1 } }"}</style>
      {visual.video ? (
        <video
          src={visual.video}
          autoPlay
          // Unmuted autoplay is allowed here — verified on the device — so a
          // theme's video can carry its own audio track rather than needing
          // the sound to be fired separately and kept in sync by hand.
          muted={!visual.sound}
          playsInline
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      ) : (
        <div style={visual.style}>{visual.label}</div>
      )}
    </div>
  );
};
