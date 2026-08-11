import { FC, useEffect, useState } from "react";
import { scenes, Scene, MIN_VISIBLE_MS, type SceneChange } from "./lib/presentation";
import { notifySubscribers, sharedState, subscribeToState } from "./shared";
import {
  configFor, discoverThemes, fillScreen, loadTheme, screenFor,
  type Theme,
} from "./lib/themes";
import { LAYER_VISUALS } from "./lib/layers";
import { playThemeSound, setThemeSounds, startLoop, stopLoop } from "./lib/sounds";
import { launchTargetName } from "./lib/appNames";

/** The layer that replaces Steam's interface while custom visuals are on.
 *
 * Not a splash. A splash that only covered the loading was worth very little,
 * because Steam already draws a loading screen and ours simply sat on top.
 * What the feature is for is the *other* state: a Deck on a shelf showing its
 * own attract screen instead of Steam's Home, switching to a loading screen
 * the moment a tag lands, and never showing the library at all.
 *
 * What it paints comes from a theme file rather than from this bundle — see
 * lib/themes.ts. This component knows about scenes, timing and sound; it does
 * not know what a DOS prompt looks like, which is exactly what makes a theme
 * something a person can write without a toolchain.
 *
 * Two things are verified on hardware rather than assumed, because the design
 * rests on them: a fixed, inset-0 layer paints above Home, the library and
 * Steam's own "Starting launch…" card; and gamescope composites it away the
 * moment a game paints, which is why nothing here times the end of a launch.
 * The way out stays visible — Steam's menus render above this — and the layer
 * additionally yields whenever one is open.
 */

/** Never rendered, whatever a theme provides: gamescope owns the screen once a
 *  game is up, so a visual here would be a promise the runtime cannot keep. */
const NEVER_VISIBLE = new Set<Scene>([Scene.IN_GAME]);

interface Painted {
  scene: Scene;
  html: string;
  css: string;
  fadeMs?: number;
}

export const VisualsLayer: FC = () => {
  const [painted, setPainted] = useState<Painted | null>(null);
  const [menuOpen, setMenuOpen] = useState(!!sharedState.steamOverlayOpen);
  const [theme, setTheme] = useState<Theme | null>(null);
  const themeId = theme?.id ?? null;

  // Load whichever theme is selected, and reload when the selection changes.
  useEffect(() => {
    let cancelled = false;

    const sync = async () => {
      const wanted = sharedState.settings?.custom_visuals
        ? (sharedState.settings?.theme ?? "")
        : "";
      if (!wanted) {
        if (!cancelled && themeId) {
          setThemeSounds(null, {});
          setTheme(null);
        }
        return;
      }
      if (wanted === themeId) return;
      await discoverThemes();
      const next = await loadTheme(wanted);
      if (cancelled) return;
      // The plugin's own event sounds follow the theme — the DOS theme's
      // "error" is a PC speaker, not the plugin's chime.
      setThemeSounds(next?.id ?? null, next?.sounds ?? {});
      setTheme(next);
    };

    void sync();
    const unsubscribe = subscribeToState(() => void sync());
    return () => { cancelled = true; unsubscribe(); };
  }, [themeId]);

  useEffect(() => {
    let timer: number | undefined;

    const paint = (scene: Scene | null) => {
      window.clearTimeout(timer);

      if (!sharedState.settings?.custom_visuals || !theme) {
        stopLoop();
        return setPainted(null);
      }
      if (!scene || NEVER_VISIBLE.has(scene)) {
        // Includes IN_GAME: a drive seeking under someone's game would be the
        // single most annoying bug this feature could ship.
        stopLoop();
        return setPainted(null);
      }

      const markup = screenFor(theme, scene);
      if (!markup) {
        stopLoop();
        return setPainted(null);
      }
      const config = configFor(theme, scene);

      // Sound follows the scene, not the paint: an edge sound belongs to the
      // moment the scene changed, and waiting out a dwell for it would put the
      // latch click after the disk had already been read.
      if (config.enterSound) void playThemeSound(theme.id, config.enterSound);
      if (config.loopSound) void startLoop(theme.id, config.loopSound);
      else stopLoop();

      // Scenes that pass in a blink never paint, when the theme asks for that.
      // The states that are *held* — standby, ambient — ask for nothing and
      // appear at once, because a gap before them is a gap in which Steam
      // shows through.
      const show = () => {
        if (scenes.scene !== scene) return;
        setPainted({
          scene,
          html: fillScreen(markup, {
            title: mediumTitle() ?? "Program",
            drive: mediumTrigger() === "nfc" ? "T" : "A",
          }),
          css: theme.css,
          fadeMs: config.fadeMs,
        });
      };
      const delay = config.minVisibleMs;
      // Capped, so a theme cannot leave the screen on Steam by asking for a
      // ten-second dwell: the ceiling is this component's, not the theme's.
      if (delay && delay > 0) timer = window.setTimeout(show, Math.min(delay, MIN_VISIBLE_MS * 4));
      else show();
    };

    const unsubscribeScene = scenes.subscribe((change: SceneChange) => paint(change.to));
    const unsubscribeState = subscribeToState(() => {
      setMenuOpen(!!sharedState.steamOverlayOpen);
      paint(scenes.scene);
    });
    paint(scenes.scene);

    return () => {
      window.clearTimeout(timer);
      stopLoop();
      unsubscribeScene();
      unsubscribeState();
    };
  }, [theme]);

  const showing = !!painted && !menuOpen;

  // Published so the game-page pair icon can hide behind a theme it cannot
  // out-stack — the two live in different stacking contexts, so z-index was
  // never going to settle it. In an effect rather than during render, because
  // writing shared state mid-render is how two components end up disagreeing
  // about the frame they are in.
  useEffect(() => {
    if (sharedState.visualsPainting === showing) return;
    sharedState.visualsPainting = showing;
    notifySubscribers();
  }, [showing]);

  if (!showing || !painted) return null;

  return (
    <div
      data-decky-links-visual={painted.scene}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: LAYER_VISUALS,
        // Never swallows input, however long it is up. Steam keeps receiving
        // everything underneath, so a layer that somehow failed to clear
        // leaves a Deck that looks wrong rather than one that cannot be used.
        pointerEvents: "none",
        animation: `dl-visual-in ${painted.fadeMs ?? 220}ms ease-out`,
      }}
    >
      <style>{`@keyframes dl-visual-in { from { opacity: 0 } to { opacity: 1 } }\n${painted.css}`}</style>
      {/* The theme's own markup. Inert by construction: it is set as
          innerHTML, which never executes script, and it came out of an inert
          <template>. A theme is markup, and stays markup. */}
      <div
        style={{ width: "100%", height: "100%" }}
        dangerouslySetInnerHTML={{ __html: painted.html }}
      />
    </div>
  );
};

/** What a theme is told about the medium in play.
 *
 * Read at paint time from the registry the panel already uses, rather than
 * threaded through the scene: a scene is a state, and states with payloads
 * attached are how state machines rot.
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
