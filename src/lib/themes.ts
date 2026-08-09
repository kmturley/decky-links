import { CSSProperties } from "react";
import { Scene } from "./presentation";

/** What a theme paints for one scene.
 *
 * `video` and the CSS pair are alternatives, not a fallback chain: a theme
 * that names a video and a label wants the video, and silently showing the
 * label when the file is missing would hide the fault rather than report it.
 */
export interface SceneVisual {
  scene: Scene;
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
}

export interface Theme {
  id: string;
  name: string;
  scenes: Partial<Record<Scene, SceneVisual>>;
}

const centred: CSSProperties = {
  fontSize: "2.2em",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  fontWeight: 300,
  textShadow: "0 2px 24px rgba(0,0,0,0.6)",
};

/** The theme that ships, and the fallback for any slot another theme leaves
 *  empty — so a theme can be two files and still be a theme.
 *
 *  Deliberately built from CSS rather than video. A default that shipped
 *  megabytes of animation would make every install pay for a feature that is
 *  off by default, and the point of the first theme is to prove the layer, not
 *  to be the one anybody keeps.
 *
 *  READY is the important one, and the reason the feature exists: it is what a
 *  Deck shows while it waits, in place of Steam's Home. Everything else here
 *  is a transition away from it and back.
 */
export const DEFAULT_THEME: Theme = {
  id: "default",
  name: "Default",
  scenes: {
    [Scene.READY]: {
      scene: Scene.READY,
      label: "Present a tag or disk",
      background: "radial-gradient(circle at 50% 40%, #101c2c 0%, #05070c 75%)",
      style: { ...centred, color: "#9ec8ea", fontSize: "1.9em" },
      // Immediate, because any delay here is a window in which Steam's Home
      // shows through — which is the one thing this feature exists to prevent.
      immediate: true,
    },
    [Scene.AMBIENT]: {
      scene: Scene.AMBIENT,
      label: "Decky Links",
      background: "radial-gradient(circle at 50% 45%, #0b1420 0%, #04060a 80%)",
      style: { ...centred, color: "#4f6a85", fontSize: "1.5em" },
      immediate: true,
      fadeMs: 900,
    },
    [Scene.LAUNCHING]: {
      scene: Scene.LAUNCHING,
      label: "Loading",
      background: "radial-gradient(circle at 50% 45%, #16283d 0%, #05070c 70%)",
      style: { ...centred, color: "#8fd0ff" },
    },
    [Scene.READING]: {
      scene: Scene.READING,
      label: "Reading",
      background: "rgba(4, 8, 14, 0.72)",
      style: { ...centred, color: "#cfe6ff", fontSize: "1.6em" },
    },
    [Scene.ERROR]: {
      scene: Scene.ERROR,
      label: "Unreadable",
      background: "rgba(40, 6, 6, 0.78)",
      style: { ...centred, color: "#ff9d9d", fontSize: "1.8em" },
      // Shown even if it lasts a moment: an error the user never sees is an
      // error they will report as "nothing happened".
      immediate: true,
    },
    [Scene.LOCKED]: {
      scene: Scene.LOCKED,
      label: "Locked",
      background: "rgba(6, 10, 18, 0.85)",
      style: { ...centred, color: "#9fb4cc", fontSize: "1.8em" },
    },
  },
};

const THEMES: Record<string, Theme> = {
  [DEFAULT_THEME.id]: DEFAULT_THEME,
};

/** Resolve a theme id, falling back rather than failing.
 *
 * A theme named in settings that is no longer installed must not take the
 * splash down with it — losing the look is a disappointment, losing the layer
 * is a bug.
 */
export function themeFor(id?: string | null): Theme {
  return (id && THEMES[id]) || DEFAULT_THEME;
}

export function allThemes(): Theme[] {
  return Object.values(THEMES);
}
