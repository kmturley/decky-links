import { FC, useEffect, useState } from "react";
import { scenes, Scene, MIN_VISIBLE_MS, type SceneChange } from "./lib/presentation";
import { notifySubscribers, sharedState, subscribeToState } from "./shared";
import { themeFor, visualFor, type SceneVisual } from "./lib/themes";
import { LAYER_VISUALS } from "./lib/layers";
import { playThemeSound, startLoop, stopLoop } from "./lib/sounds";
import { launchTargetName } from "./lib/appNames";

/** The layer that replaces Steam's interface while custom visuals are on.
 *
 * Not a splash. A splash that only covered the loading was worth very little,
 * because Steam already draws a loading screen and the plugin's version simply
 * sat on top of it. What the feature is actually for is the *other* state: a
 * Deck sitting on a shelf showing your own attract screen rather than Steam's
 * Home, and switching to a loading screen the moment a tag lands. So the layer
 * is up by default while the feature is on, and Steam's Home and game pages
 * are never seen.
 *
 * The way out stays visible. Verified on hardware: the Quick Access menu
 * renders *above* this layer, so the toggle that switches the feature off is
 * reachable even while the whole screen is covered. That is the property that
 * makes a takeover safe to ship, and it is checked rather than assumed.
 *
 * The one scene never painted is IN_GAME: gamescope owns the screen once a
 * game is up, so there is no layer to paint into. It composites this away by
 * itself, which is also why nothing here has to time the end of a launch.
 */

/** Never rendered, whatever a theme provides — see above. */
const NEVER_VISIBLE = new Set<Scene>([Scene.IN_GAME]);

/** What a theme is told about the medium in play.
 *
 * Read at paint time from the registry the panel already uses, rather than
 * threaded through the scene: a scene is a state, and states with payloads
 * attached are how state machines rot. A theme that shows the game's name is
 * making a presentation decision from presentation-time facts.
 */
function presentedMedium() {
  return Object.values(sharedState.activeMedia).find((m) => m.uri) ??
    Object.values(sharedState.activeMedia)[0];
}

function mediumTitle(): string | undefined {
  const uri = presentedMedium()?.uri;
  return uri ? launchTargetName(uri) : undefined;
}

function mediumTrigger(): string | undefined {
  const medium = presentedMedium();
  return medium?.drive_kind ?? medium?.source_type;
}

export const VisualsLayer: FC = () => {
  // Stand aside whenever Steam's focus is off its main window.
  //
  // The Quick Access panel renders above this layer, but the popups it opens
  // — a dropdown's option list — do not: they are mounted elsewhere in the
  // tree, so the layer covered them and the picker appeared to do nothing.
  // Rather than hunt for a z-index above one and below the other across
  // stacking contexts we do not control, the layer yields. While a menu or one
  // of its popups has focus the user is configuring the Deck, not looking at
  // an attract screen. See sharedState.steamOverlayOpen for why this is a
  // focus test rather than a menu test.
  const [menuOpen, setMenuOpen] = useState(!!sharedState.steamOverlayOpen);

  // One piece of state, deliberately. An earlier version also kept an
  // `enabled` flag seeded from the settings at mount — but this layer mounts
  // at plugin start, *before* the first getSettings resolves, so it seeded
  // false and only a later notify could correct it. Nothing else was
  // guaranteed to notify, so the layer stayed blank on a Deck whose setting
  // was plainly on. `paint` reads the setting live instead, so the only state
  // here is what is on screen.
  const [visual, setVisual] = useState<SceneVisual | null>(null);

  useEffect(() => {
    let timer: number | undefined;

    const paint = (scene: Scene | null) => {
      window.clearTimeout(timer);

      if (!sharedState.settings?.custom_visuals) {
        stopLoop();
        return setVisual(null);
      }
      if (!scene || NEVER_VISIBLE.has(scene)) {
        // Includes IN_GAME: a drive seeking under someone's game would be the
        // single most annoying bug this feature could ship.
        stopLoop();
        return setVisual(null);
      }

      const themeId = themeFor(sharedState.settings?.theme).id;
      const next = visualFor(sharedState.settings?.theme, scene);
      if (!next) {
        console.debug("[ Decky Links ] Theme has no visual for", scene);
        return setVisual(null);
      }

      // Sound follows the scene, not the paint: an edge sound belongs to the
      // moment the scene changed, and waiting out MIN_VISIBLE_MS for it would
      // put the latch click after the disk was already read.
      if (next.enterSound) playThemeSound(themeId, next.enterSound);
      if (next.loopSound) startLoop(themeId, next.loopSound);
      else stopLoop();

      // Scenes that pass through in a blink never paint. A tag reads in well
      // under 200ms, so a READING visual on a fast tag would appear and be
      // replaced inside a couple of frames, and a flicker reads as a fault.
      // The states that are *held* — home, ambient — mark themselves immediate,
      // because a gap before them is a gap in which Steam shows through.
      const delay = next.immediate ? 0 : MIN_VISIBLE_MS;
      timer = window.setTimeout(() => {
        if (scenes.scene !== scene) return;
        setVisual(next);
      }, delay);
    };

    const onScene = (change: SceneChange) => paint(change.to);
    const unsubscribeScene = scenes.subscribe(onScene);

    // The switch is in a panel that renders above this layer, so it can be
    // thrown while the takeover is up — and it has to take effect at once,
    // both ways.
    const unsubscribeState = subscribeToState(() => {
      setMenuOpen(!!sharedState.steamOverlayOpen);
      paint(scenes.scene);
    });

    // Paint whatever is already true. The layer mounts once at plugin start,
    // long after the first scene in a session may have been decided.
    paint(scenes.scene);

    return () => {
      window.clearTimeout(timer);
      stopLoop();
      unsubscribeScene();
      unsubscribeState();
    };
  }, []);

  const painting = !!visual && !menuOpen;

  // Published so the game-page pair icon can hide behind the theme it cannot
  // out-stack. In an effect rather than during render, because writing shared
  // state mid-render is how two components end up disagreeing about the frame
  // they are in.
  useEffect(() => {
    if (sharedState.visualsPainting === painting) return;
    sharedState.visualsPainting = painting;
    notifySubscribers();
  }, [painting]);

  if (!painting) return null;

  return (
    <div
      data-decky-links-visual={visual.scene}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: LAYER_VISUALS,
        // Never swallows input, however long it is up. Steam keeps receiving
        // everything underneath, so a layer that somehow failed to clear
        // leaves a Deck that looks wrong rather than one that cannot be used.
        pointerEvents: "none",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: visual.background,
        animation: `dl-visual-in ${visual.fadeMs ?? 220}ms ease-out`,
      }}
    >
      {/* Scoped to the layer rather than injected into the tab, so nothing of
          ours is left in Steam's stylesheet when the plugin unmounts. */}
      <style>{"@keyframes dl-visual-in { from { opacity: 0 } to { opacity: 1 } }"}</style>
      {visual.render ? (
        <visual.render scene={visual.scene} title={mediumTitle()} trigger={mediumTrigger()} />
      ) : visual.video ? (
        <video
          src={visual.video}
          autoPlay
          loop={visual.loop}
          // Unmuted autoplay is allowed here — verified on the device — so a
          // theme's video can carry its own audio rather than needing a
          // separate sound kept in sync by hand.
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
