import { CSSProperties, FC } from "react";
import { Scene } from "./presentation";
import { DOS_THEME } from "../themes/dos";

/** What a theme paints for one scene.
 *
 * Three ways to fill a scene, in the order the layer checks them: a component,
 * a video, or a styled label. `render` exists because the most convincing
 * themes are not footage — a DOS prompt is text, a box and a blinking block,
 * and rendering it is both smaller and sharper than any recording of it. The
 * DOS theme is ~20KB of markup where a video of the same thing would be
 * megabytes and would still be the wrong resolution on someone else's screen.
 */
export interface SceneVisual {
  scene: Scene;
  /** Rendered inside the full-screen layer. Gets the whole viewport. */
  render?: FC<SceneProps>;
  /** Absolute URL. Built-in themes live under dist/, which is the only
   *  directory Decky serves over HTTP; user themes arrive as blob: URLs. */
  video?: string;
  /** True when the video carries its own audio track. Kept explicit because
   *  autoplay-with-sound is allowed here and a theme that did not expect it
   *  would double up with the event sound. */
  sound?: boolean;
  /** Loop the video. Held scenes — home, ambient — want this; a loading
   *  animation that loops would outlast what it is reporting. */
  loop?: boolean;
  label?: string;
  style?: CSSProperties;
  background: string;
  fadeMs?: number;
  /** Paint without waiting out MIN_VISIBLE_MS. For scenes that are worth
   *  showing even if they last one frame — an error the user must see. */
  immediate?: boolean;
  /** Played once on entering the scene, as a filename in the theme's sounds
   *  directory. The edge half of the model: a latch closing happens *at* the
   *  moment the disk goes in. */
  enterSound?: string;
  /** Looped for as long as the scene lasts. The level half: a drive seeking
   *  is a thing that continues, and a one-shot of it would stop while the
   *  drive on screen was still working. */
  loopSound?: string;
}

/** Everything a scene component is given.
 *
 * Deliberately small. A theme that needed more of the plugin's internals would
 * be coupled to them, and themes are the part most likely to be written by
 * someone who has never read the rest of this.
 */
export interface SceneProps {
  scene: Scene;
  /** The game being launched, when one is known. */
  title?: string;
  /** Which trigger the medium is on: "floppy", "nfc", … */
  trigger?: string;
}

export interface Theme {
  id: string;
  name: string;
  /** One-line description for the panel. */
  blurb: string;
  scenes: Partial<Record<Scene, SceneVisual>>;
  /** Replacements for the backend's logical sounds (scan, success, error,
   *  lock, unlock), as filenames in this theme's sounds directory. A theme
   *  that overrides none of them keeps the plugin's own. */
  sounds?: Record<string, string>;
}

const THEMES: Record<string, Theme> = {
  [DOS_THEME.id]: DOS_THEME,
};

/** What an unknown or missing theme id resolves to.
 *
 * There was a "default" theme of plain dark screens alongside this. It existed
 * to prove the layer worked before there was anything to look at, and once
 * there was, it was a second entry in a two-item list that nobody would ever
 * choose — so it went, and "no theme" is now spelled by switching the feature
 * off rather than by picking a blank one.
 */
const FALLBACK = DOS_THEME;

/** Resolve a theme id, falling back rather than failing.
 *
 * A theme named in settings that is no longer installed must not take the
 * layer down with it — losing the look is a disappointment, losing the layer
 * is a bug.
 */
export function themeFor(id?: string | null): Theme {
  return (id && THEMES[id]) || FALLBACK;
}

/** A theme's scene, or its standby screen, or nothing.
 *
 * The fallback is *within* the theme rather than to another one: a theme that
 * does not draw an ambient screen should idle on its own standby screen, not
 * borrow a stranger's. Falling through to Steam would be worse still — a gap
 * in a takeover is the one thing this feature exists to prevent.
 */
export function visualFor(id: string | undefined, scene: Scene): SceneVisual | undefined {
  const theme = themeFor(id);
  return theme.scenes[scene] ?? theme.scenes[Scene.READY];
}

export function allThemes(): Theme[] {
  return Object.values(THEMES);
}

/** URL for a file inside a built-in theme's directory.
 *
 * dist/ is the only directory Decky Loader serves over HTTP — verified on a
 * Deck: dist/index.js is 200, main.py is 403, assets/logo.png is 404 — so the
 * build copies assets/themes there.
 */
export function themeAssetUrl(themeId: string, file: string): string {
  const base = `http://localhost:1337/plugins/${encodeURIComponent("Decky Links")}/dist/themes`;
  return `${base}/${themeId}/${file}`;
}
